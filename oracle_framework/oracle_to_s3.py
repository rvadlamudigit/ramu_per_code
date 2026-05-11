"""
oracle_to_s3.py
Reusable Oracle -> CSV -> S3 extraction framework.

The class is a TOOLKIT of independent functions. The runner is the
orchestrator: it reads the YAML, validates the schema, builds the
LoggerClient, and then constructs the framework class with both the
logger and the already-parsed config dict. The class itself never
opens files, never validates, never prints, and never sys.exits --
it just runs the steps you call on it and raises typed framework
exceptions on failure.

Exception hierarchy (all subclass OracleToS3ExtractError):
    OracleToS3ExtractError    -- base class for everything below
        ConfigError           -- raised by runner.validate_config or by
                                 runtime guard rails inside the class
        SecretsManagerError   -- failed to fetch secret from AWS
        OracleConnectionError -- could not connect to Oracle
        OracleQueryError      -- query execution failed
        CSVWriteError         -- failed to write a local CSV file
        S3UploadError         -- failed to upload to S3

Typical usage from a runner program:
    from logger import LoggerClient
    from oracle_to_s3 import OracleToS3Extract
    from runner import read_yaml, validate_config

    cfg = read_yaml("config/extract_example.yaml")
    validate_config(cfg)
    lc  = LoggerClient(project_name="oracle_framework",
                       process_name="big_table_extract",
                       root_directory="/var/log/oracle_framework",
                       logoutput="BOTH")
    job = OracleToS3Extract(lc, cfg)
    try:
        job.connect()
        batches = job.execute_query()
        files   = job.write_csv_files(batches)
        uris    = job.upload_to_s3(files)
        job.cleanup_local(files)
    finally:
        job.close()
"""

from __future__ import annotations

import csv
import json
import platform
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Iterator, List, Optional, Tuple

import boto3
import oracledb
from botocore.exceptions import BotoCoreError, ClientError

from logger import LoggerClient


# ---------------------------------------------------------- helpers / utilities

# Keys whose values should never be written to logs.
_SENSITIVE_KEYS = {
    "password", "passwd", "pwd", "secret", "token",
    "access_key", "secret_key", "secret_access_key",
    "aws_secret_access_key", "kms_key_id",
}


def redact(obj: Any, _depth: int = 0) -> Any:
    """Return a deep copy of `obj` with sensitive values masked.

    Strings under any key in _SENSITIVE_KEYS become ``***REDACTED***``.
    Used by debug-mode config dumps so we never leak credentials into logs.
    """
    if _depth > 20:
        return "<max-depth>"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in _SENSITIVE_KEYS and v:
                out[k] = "***REDACTED***"
            else:
                out[k] = redact(v, _depth + 1)
        return out
    if isinstance(obj, list):
        return [redact(v, _depth + 1) for v in obj]
    if isinstance(obj, tuple):
        return tuple(redact(v, _depth + 1) for v in obj)
    return obj


def _human_bytes(n: int) -> str:
    """Pretty-print a byte count (e.g. 1536 -> '1.5 KiB')."""
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    f = float(n)
    for u in units:
        if f < 1024.0 or u == units[-1]:
            return f"{f:0.2f} {u}"
        f /= 1024.0
    return f"{n} B"


def _safe_file_size(path: Path) -> int:
    """Return file size in bytes, or -1 if it cannot be stat'd."""
    try:
        return path.stat().st_size
    except OSError:
        return -1


def _rate(count: int, seconds: float) -> str:
    """Return a 'X items/s' string. Avoids div-by-zero."""
    if seconds <= 0:
        return f"{count}/s"
    return f"{count / seconds:0.1f}/s"


# --------------------------------------------------------------- exceptions

class OracleToS3ExtractError(Exception):
    """Base exception for the framework. All framework errors subclass this."""


class ConfigError(OracleToS3ExtractError):
    """Invalid or missing YAML configuration."""


class SecretsManagerError(OracleToS3ExtractError):
    """Failed to fetch credentials from AWS Secrets Manager."""


class OracleConnectionError(OracleToS3ExtractError):
    """Failed to establish Oracle database connection."""


class OracleQueryError(OracleToS3ExtractError):
    """Failed to execute the configured Oracle query."""


class CSVWriteError(OracleToS3ExtractError):
    """Failed to write a CSV file to local disk."""


class S3UploadError(OracleToS3ExtractError):
    """Failed to upload one or more files to S3."""


# ----------------------------------------------------------------- main class

class OracleToS3Extract:
    """Reusable Oracle -> CSV -> S3 extraction toolkit.

    Construction takes the LoggerClient and the already-parsed,
    already-validated config dict. After construction the class is
    self-contained: every method uses the same `lc.logfile` logger
    and the same `self.config` dict.
    """

    # ------------------------------------------------------------------ init

    def __init__(
        self,
        lc: LoggerClient,
        config: dict,
        *,
        debug: bool = False,
    ):
        """Build a job from a LoggerClient and a parsed/validated config.

        Parameters
        ----------
        lc : LoggerClient
            Already-constructed logger. The class uses ``lc.logfile`` --
            a real ``logging.Logger`` -- for every log call. This
            replaces the old module-level ``logging.getLogger(__name__)``
            and guarantees that every framework log line flows through
            the same handlers (stdout, .log, .error, .critical, .debug)
            that the runner configured.
        config : dict
            The fully-parsed YAML config. The runner is responsible for
            reading the file (``runner.read_yaml``) and validating the
            schema (``runner.validate_config``) before constructing the
            class. The class trusts the dict and never re-validates it.
        debug : bool, default False
            Mirrors the runner's --debug flag. When True, every step
            emits substantially more verbose logs (per-batch row counts,
            per-file sizes, timings, redacted config dumps, Oracle/boto3
            versions, environment metadata).
        """
        if lc is None:
            raise ConfigError("OracleToS3Extract requires a LoggerClient")
        if not isinstance(config, dict):
            raise ConfigError(
                "OracleToS3Extract requires a dict config "
                "(use runner.read_yaml + runner.validate_config first)"
            )

        self.lc = lc
        # Convenience handle: the actual stdlib Logger inside the
        # LoggerClient. All log calls in this file go through it.
        self.log = lc.logfile

        # In-YAML `debug: true` is honoured the same way it was in the
        # old load_config(). The runner's --debug always wins because
        # it is OR'd in at construction time.
        self.debug = bool(debug) or bool(config.get("debug", False))

        self.config: dict = config
        self.connection: Optional[oracledb.Connection] = None
        self.local_files: List[Path] = []

        # Cumulative metrics; populated as the pipeline runs.
        self.metrics: dict = {
            "rows_total": 0,
            "batches_total": 0,
            "files_written": 0,
            "bytes_written": 0,
            "bytes_uploaded": 0,
            "uploads": 0,
            "elapsed_query_s": 0.0,
            "elapsed_write_s": 0.0,
            "elapsed_upload_s": 0.0,
        }

        self.log.debug(
            "OracleToS3Extract instantiated (debug=%s, python=%s, "
            "oracledb=%s, boto3=%s, platform=%s)",
            self.debug, sys.version.split()[0],
            getattr(oracledb, "__version__", "?"),
            getattr(boto3, "__version__", "?"),
            platform.platform(),
        )
        self.log.debug(
            "Config received: top-level keys=%s", sorted(config.keys())
        )
        # Debug-mode dumps the *entire* configuration, with secrets masked.
        if self.debug:
            try:
                dump = json.dumps(redact(config), indent=2, default=str)
            except Exception as e:  # noqa: BLE001
                dump = f"<could not dump config: {e}>"
            self.log.debug("Effective configuration (redacted):\n%s", dump)

    # --------------------------------------------------------------- secrets

    def get_secret_from_aws(
        self, secret_name: str, region: Optional[str] = None
    ) -> dict:
        """Fetch a secret from AWS Secrets Manager and return it as a dict.

        The secret is expected to be a JSON object containing at minimum
        'username' (or 'user') and 'password' fields.

        Raises SecretsManagerError on any failure.
        """
        self.log.info(
            "Fetching secret '%s' from AWS Secrets Manager (region=%s)",
            secret_name, region or "<default>",
        )
        t0 = time.monotonic()
        try:
            session = boto3.session.Session(region_name=region)
            client = session.client("secretsmanager")
            response = client.get_secret_value(SecretId=secret_name)
        except (BotoCoreError, ClientError) as e:
            self.log.error(
                "Secrets Manager fetch failed for '%s': %s", secret_name, e
            )
            raise SecretsManagerError(
                f"Failed to fetch secret '{secret_name}' from "
                f"Secrets Manager: {e}"
            ) from e

        secret_string = response.get("SecretString")
        if not secret_string:
            raise SecretsManagerError(
                f"Secret '{secret_name}' is empty or binary; "
                "expected a JSON string."
            )
        try:
            payload = json.loads(secret_string)
        except json.JSONDecodeError as e:
            raise SecretsManagerError(
                f"Secret '{secret_name}' is not valid JSON: {e}"
            ) from e

        self.log.debug(
            "Secrets Manager fetch OK in %.3fs (arn=%s, version=%s, keys=%s)",
            time.monotonic() - t0,
            response.get("ARN", "<?>"),
            response.get("VersionId", "<?>"),
            sorted(payload.keys()) if isinstance(payload, dict) else "<not-dict>",
        )
        return payload

    def _resolve_credentials(self) -> Tuple[str, str]:
        """Return (user, password) based on the configured auth_method.

        Raises ConfigError or SecretsManagerError on failure.
        """
        oracle_cfg = self.config["oracle"]
        method = str(oracle_cfg.get("auth_method", "plain")).lower()

        if method == "plain":
            return oracle_cfg["user"], oracle_cfg["password"]

        # aws_secret
        secret_name = oracle_cfg["secret_name"]
        region = oracle_cfg.get("aws_region")
        secret = self.get_secret_from_aws(secret_name, region)
        user = secret.get("username") or secret.get("user")
        password = secret.get("password")
        if not user or not password:
            raise SecretsManagerError(
                f"Secret '{secret_name}' missing 'username'/'password' fields"
            )
        return user, password

    # ------------------------------------------------------------ connection

    def connect(self) -> oracledb.Connection:
        """Step 2: establish the Oracle connection.

        Raises OracleConnectionError on driver/network failure.
        Raises SecretsManagerError on credential fetch failure.
        """
        user, password = self._resolve_credentials()
        dsn = self.config["oracle"]["dsn"]
        method = str(self.config["oracle"].get("auth_method", "plain")).lower()
        self.log.info(
            "Connecting to Oracle dsn=%s as user=%s (auth_method=%s)",
            dsn, user, method,
        )
        self.log.debug(
            "oracledb thin_mode=%s, client_version=%s",
            getattr(oracledb, "is_thin_mode", lambda: True)(),
            getattr(oracledb, "clientversion", lambda: "thin")(),
        )

        t0 = time.monotonic()
        try:
            self.connection = oracledb.connect(
                user=user, password=password, dsn=dsn
            )
            elapsed = time.monotonic() - t0
            # Best-effort: server metadata is informational only.
            server_version = getattr(self.connection, "version", "<unknown>")
            try:
                instance = self.connection.instance_name  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                instance = "<unknown>"
            self.log.info(
                "Oracle connection established in %.3fs "
                "(server_version=%s, instance=%s)",
                elapsed, server_version, instance,
            )
            if self.debug:
                self.log.debug(
                    "Connection details: autocommit=%s, encoding=%s, "
                    "stmtcachesize=%s, dsn=%s",
                    getattr(self.connection, "autocommit", "?"),
                    getattr(self.connection, "encoding", "?"),
                    getattr(self.connection, "stmtcachesize", "?"),
                    dsn,
                )
            return self.connection
        except oracledb.Error as e:
            self.log.error(
                "Oracle connection failed after %.3fs: %s",
                time.monotonic() - t0, e,
            )
            raise OracleConnectionError(f"Oracle connection failed: {e}") from e
        except Exception as e:
            self.log.exception(
                "Unexpected error establishing Oracle connection after %.3fs",
                time.monotonic() - t0,
            )
            raise OracleConnectionError(
                f"Unexpected error establishing Oracle connection: {e}"
            ) from e

    def close(self) -> None:
        """Close the Oracle connection if it is open. Safe to call multiple times."""
        if self.connection is not None:
            self.log.debug("close: tearing down Oracle connection")
            try:
                self.connection.close()
                self.log.info("Oracle connection closed.")
            except Exception as e:  # noqa: BLE001
                self.log.warning("Error closing Oracle connection: %s", e)
            self.connection = None
        else:
            self.log.debug("close: no active Oracle connection to close")

    # ---------------------------------------------------------------- query

    def execute_query(self) -> Iterator[Tuple[List[str], List[tuple]]]:
        """Step 3: execute the configured SQL and yield (column_names, batch_rows).

        Generator-based: result sets larger than memory are streamed in
        chunks of `fetch.array_size` rows. The writer consumes the
        batches lazily.

        Raises OracleConnectionError if connect() was not called.
        Raises OracleQueryError on cursor / fetch failures.
        """
        if self.connection is None:
            raise OracleConnectionError(
                "Call connect() before execute_query()."
            )

        sql = self.config["sql"]
        array_size = int(self.config.get("fetch", {}).get("array_size", 5000))

        # In debug mode log the full SQL; otherwise log the first non-empty
        # line to avoid spamming production logs with large multi-line queries.
        if self.debug:
            self.log.debug(
                "execute_query: full SQL (arraysize=%s):\n%s",
                array_size, sql,
            )
        else:
            first_line = next(
                (line.strip() for line in sql.splitlines() if line.strip()),
                "<empty>",
            )
            self.log.info(
                "Executing query (arraysize=%s, first line: %s)",
                array_size, first_line,
            )

        prepare_t0 = time.monotonic()
        try:
            cursor = self.connection.cursor()
            cursor.arraysize = array_size
            try:
                cursor.prefetchrows = array_size + 1
            except AttributeError:
                pass

            self.log.debug(
                "Cursor configured (arraysize=%s, prefetchrows=%s)",
                array_size,
                getattr(cursor, "prefetchrows", "?"),
            )

            cursor.execute(sql)
            description = cursor.description or []
            column_names = [d[0] for d in description]
            self.log.info(
                "Query prepared in %.3fs (%d columns)",
                time.monotonic() - prepare_t0, len(column_names),
            )
            if self.debug:
                # Log column metadata: name, type, internal size, precision, scale
                for d in description:
                    name, type_obj = d[0], d[1]
                    internal_size = d[3] if len(d) > 3 else None
                    precision = d[4] if len(d) > 4 else None
                    scale = d[5] if len(d) > 5 else None
                    nullable = d[6] if len(d) > 6 else None
                    self.log.debug(
                        "  column: name=%s type=%s size=%s precision=%s "
                        "scale=%s nullable=%s",
                        name, getattr(type_obj, "name", type_obj),
                        internal_size, precision, scale, nullable,
                    )
        except oracledb.Error as e:
            self.log.error("Query failed during prepare/execute: %s", e)
            raise OracleQueryError(f"Failed to execute query: {e}") from e

        rows_total = 0
        batches_total = 0
        fetch_start = time.monotonic()
        try:
            while True:
                batch_t0 = time.monotonic()
                try:
                    batch = cursor.fetchmany(array_size)
                except oracledb.Error as e:
                    self.log.error(
                        "fetchmany failed after %d rows / %d batches: %s",
                        rows_total, batches_total, e,
                    )
                    raise OracleQueryError(
                        f"Failed while fetching rows: {e}"
                    ) from e
                if not batch:
                    break
                batches_total += 1
                rows_total += len(batch)
                # Per-batch DEBUG; periodic INFO every ~10 batches so even
                # non-debug runs get a heartbeat for very large extracts.
                self.log.debug(
                    "Fetched batch #%d (%d rows in %.3fs); running total=%d",
                    batches_total, len(batch),
                    time.monotonic() - batch_t0, rows_total,
                )
                if batches_total % 10 == 0:
                    elapsed = time.monotonic() - fetch_start
                    self.log.info(
                        "Fetched %d rows in %d batches (%.1fs, %s)",
                        rows_total, batches_total, elapsed,
                        _rate(rows_total, elapsed),
                    )
                yield column_names, batch
        finally:
            elapsed = time.monotonic() - fetch_start
            self.metrics["rows_total"] = rows_total
            self.metrics["batches_total"] = batches_total
            self.metrics["elapsed_query_s"] = elapsed
            self.log.info(
                "Query streaming complete: %d row(s) in %d batch(es), "
                "elapsed=%.3fs (%s)",
                rows_total, batches_total, elapsed,
                _rate(rows_total, elapsed),
            )
            try:
                cursor.close()
                self.log.debug("Cursor closed.")
            except Exception as e:  # noqa: BLE001
                self.log.debug("Error closing cursor (ignored): %s", e)

    # --------------------------------------------------------------- writing

    def write_csv_files(
        self, batches: Iterable[Tuple[List[str], List[tuple]]]
    ) -> List[Path]:
        """Step 4: write a stream of batches to one or more local CSV files.

        Splits when the row count for the current file reaches
        records_per_file. File names follow <base>_<n>.csv (1-indexed).
        Returns the list of file paths written.

        Raises CSVWriteError on filesystem errors.
        """
        out_cfg = self.config["output"]
        local_dir = Path(out_cfg["local_dir"])
        base = out_cfg["base_filename"]
        records_per_file = int(out_cfg["records_per_file"])
        csv_cfg = out_cfg.get("csv") or {}
        delimiter = csv_cfg.get("delimiter", ",")
        include_header = csv_cfg.get("include_header", True)
        quote_all = csv_cfg.get("quote_all", False)

        self.log.info(
            "write_csv_files: dir=%s base=%s records_per_file=%d "
            "delimiter=%r include_header=%s quote_all=%s",
            local_dir, base, records_per_file,
            delimiter, include_header, quote_all,
        )

        try:
            local_dir.mkdir(parents=True, exist_ok=True)
            self.log.debug("Ensured local_dir exists: %s", local_dir.resolve())
        except OSError as e:
            self.log.error("Could not create local_dir %s: %s", local_dir, e)
            raise CSVWriteError(
                f"Could not create local_dir {local_dir}: {e}"
            ) from e

        file_index = 0
        rows_in_current = 0
        file_handle = None
        writer = None
        column_names: Optional[List[str]] = None
        files: List[Path] = []
        per_file_rows: List[int] = []
        rows_total = 0
        write_start = time.monotonic()
        last_progress_log = write_start

        def open_new_file() -> Path:
            nonlocal file_handle, writer, file_index, rows_in_current
            file_index += 1
            path = local_dir / f"{base}_{file_index}.csv"
            try:
                file_handle = open(path, "w", newline="", encoding="utf-8")
            except OSError as e:
                self.log.error("Could not open %s for writing: %s", path, e)
                raise CSVWriteError(f"Could not open {path}: {e}") from e
            writer = csv.writer(
                file_handle,
                delimiter=delimiter,
                quoting=csv.QUOTE_ALL if quote_all else csv.QUOTE_MINIMAL,
            )
            if include_header and column_names is not None:
                writer.writerow(column_names)
                self.log.debug(
                    "Wrote header row to %s (%d columns)",
                    path.name, len(column_names),
                )
            files.append(path)
            per_file_rows.append(0)
            rows_in_current = 0
            self.log.info("Opened new output file: %s", path)
            return path

        def close_current_file() -> None:
            """Close the active file handle and emit a per-file size log."""
            nonlocal file_handle
            if file_handle and not file_handle.closed:
                try:
                    file_handle.flush()
                    file_handle.close()
                except Exception as e:  # noqa: BLE001
                    self.log.warning(
                        "Error closing file %s: %s",
                        files[-1] if files else "<unknown>", e,
                    )
            if files:
                size = _safe_file_size(files[-1])
                if size >= 0:
                    self.metrics["bytes_written"] += size
                    self.log.info(
                        "Closed %s (%d row(s), %s)",
                        files[-1].name,
                        per_file_rows[-1] if per_file_rows else 0,
                        _human_bytes(size),
                    )

        try:
            for cols, batch in batches:
                column_names = cols
                if writer is None:
                    open_new_file()
                for row in batch:
                    if rows_in_current >= records_per_file:
                        close_current_file()
                        open_new_file()
                    try:
                        writer.writerow(row)
                    except (OSError, csv.Error) as e:
                        self.log.error(
                            "Row write failure (file=%s, row#%d): %s",
                            files[-1] if files else "<unknown>",
                            rows_total + 1, e,
                        )
                        raise CSVWriteError(
                            f"Failed writing row to "
                            f"{files[-1] if files else '<unknown>'}: {e}"
                        ) from e
                    rows_in_current += 1
                    if per_file_rows:
                        per_file_rows[-1] += 1
                    rows_total += 1

                # Periodic progress heartbeat (every ~5s) so long-running
                # writes show life even without --debug.
                now = time.monotonic()
                if now - last_progress_log >= 5.0:
                    elapsed = now - write_start
                    self.log.info(
                        "Write progress: %d row(s) across %d file(s) "
                        "(%.1fs, %s)",
                        rows_total, len(files), elapsed,
                        _rate(rows_total, elapsed),
                    )
                    last_progress_log = now
        finally:
            close_current_file()

        elapsed = time.monotonic() - write_start
        self.local_files = files
        self.metrics["files_written"] = len(files)
        self.metrics["elapsed_write_s"] = elapsed
        self.log.info(
            "Wrote %d file(s) totalling %s rows in %.3fs (%s); "
            "on-disk size=%s",
            len(files), rows_total, elapsed,
            _rate(rows_total, elapsed),
            _human_bytes(self.metrics["bytes_written"]),
        )
        if self.debug:
            for p, n in zip(files, per_file_rows):
                self.log.debug(
                    "  file=%s rows=%d size=%s",
                    p, n, _human_bytes(_safe_file_size(p)),
                )
        return files

    # -------------------------------------------------------------------- s3

    def upload_to_s3(self, files: Iterable[Path]) -> List[str]:
        """Step 5: upload local CSV files to S3 with optional SSE-KMS encryption.

        Returns the list of s3:// URIs uploaded.
        Raises S3UploadError on the first failed upload.
        """
        s3_cfg = self.config["s3"]
        bucket = s3_cfg["bucket"]
        prefix = str(s3_cfg.get("prefix", "")).lstrip("/")
        kms_key_id = s3_cfg.get("kms_key_id")
        region = s3_cfg.get("aws_region")

        self.log.info(
            "upload_to_s3: bucket=%s prefix=%s region=%s kms=%s",
            bucket, prefix or "<none>", region or "<default>",
            "enabled" if kms_key_id else "disabled",
        )
        if self.debug and kms_key_id:
            self.log.debug(
                "SSE-KMS will be applied to every object (key id length=%d)",
                len(str(kms_key_id)),
            )

        try:
            session = boto3.session.Session(region_name=region)
            s3 = session.client("s3")
            self.log.debug(
                "S3 client created (boto3=%s, region=%s)",
                getattr(boto3, "__version__", "?"),
                session.region_name or "<default>",
            )
        except (BotoCoreError, ClientError) as e:
            self.log.error("Failed to create S3 client: %s", e)
            raise S3UploadError(f"Failed to create S3 client: {e}") from e

        uris: List[str] = []
        upload_start = time.monotonic()
        files_list = list(files)
        for idx, path in enumerate(files_list, 1):
            key = f"{prefix.rstrip('/')}/{path.name}" if prefix else path.name
            size = _safe_file_size(path)
            extra: dict = {}
            if kms_key_id:
                extra["ServerSideEncryption"] = "aws:kms"
                extra["SSEKMSKeyId"] = kms_key_id
            self.log.info(
                "Uploading [%d/%d] %s (%s) -> s3://%s/%s",
                idx, len(files_list), path.name,
                _human_bytes(size) if size >= 0 else "?",
                bucket, key,
            )
            t0 = time.monotonic()
            try:
                s3.upload_file(str(path), bucket, key, ExtraArgs=extra)
                elapsed = time.monotonic() - t0
                uri = f"s3://{bucket}/{key}"
                uris.append(uri)
                self.metrics["uploads"] += 1
                if size >= 0:
                    self.metrics["bytes_uploaded"] += size
                throughput = (
                    f"{(size / elapsed) / (1024 * 1024):.2f} MiB/s"
                    if size >= 0 and elapsed > 0
                    else "?"
                )
                self.log.info(
                    "Uploaded %s -> %s in %.3fs (%s)",
                    path.name, uri, elapsed, throughput,
                )
            except (BotoCoreError, ClientError, OSError) as e:
                self.log.error(
                    "Upload failed for %s -> s3://%s/%s after %.3fs: %s",
                    path, bucket, key, time.monotonic() - t0, e,
                )
                raise S3UploadError(
                    f"Failed to upload {path} to s3://{bucket}/{key}: {e}"
                ) from e

        elapsed_total = time.monotonic() - upload_start
        self.metrics["elapsed_upload_s"] = elapsed_total
        self.log.info(
            "S3 upload phase complete: %d file(s), %s in %.3fs",
            len(uris),
            _human_bytes(self.metrics["bytes_uploaded"]),
            elapsed_total,
        )
        return uris

    def cleanup_local(self, files: Iterable[Path]) -> None:
        """Step 6: delete local CSV files after a successful S3 upload.

        Honors the optional `cleanup_local` flag in the YAML (default true).
        Best-effort: logs a warning on per-file failure, does not raise.
        """
        if not bool(self.config.get("cleanup_local", True)):
            self.log.info("cleanup_local=false; keeping local files.")
            if self.debug:
                for p in files:
                    self.log.debug("  retained local file: %s", p)
            return
        deleted = 0
        for path in files:
            try:
                size = _safe_file_size(path)
                path.unlink()
                deleted += 1
                self.log.info(
                    "Deleted local file %s%s", path,
                    f" ({_human_bytes(size)})" if size >= 0 else "",
                )
            except OSError as e:
                self.log.warning("Could not delete %s: %s", path, e)
        self.log.info("cleanup_local complete: %d file(s) deleted.", deleted)

    # ------------------------------------------------------------- diagnostics

    def log_summary(self) -> None:
        """Emit an INFO-level metrics summary covering the whole run.

        Safe to call even if the pipeline failed mid-way -- prints whatever
        metrics were accumulated so far.
        """
        m = self.metrics
        self.log.info(
            "Run summary: rows=%s, batches=%s, files=%s, "
            "written=%s, uploaded=%s/%d files, "
            "timings: query=%.2fs write=%.2fs upload=%.2fs",
            m["rows_total"], m["batches_total"], m["files_written"],
            _human_bytes(m["bytes_written"]),
            _human_bytes(m["bytes_uploaded"]), m["uploads"],
            m["elapsed_query_s"], m["elapsed_write_s"], m["elapsed_upload_s"],
        )
        if self.debug:
            self.log.debug("Full metrics dump: %s", m)
