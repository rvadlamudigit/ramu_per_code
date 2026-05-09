"""
oracle_to_s3.py
Reusable Oracle -> CSV -> S3 extraction framework.

The class is a TOOLKIT of independent functions. The constructor only
stores the config path; nothing is read or validated until the runner
explicitly calls load_config(). Each public method does ONE thing and
raises typed framework exceptions on failure -- it never prints or
sys.exits. The runner program owns the orchestration order and the
final exception handling.

Exception hierarchy (all subclass OracleToS3ExtractError):
    OracleToS3ExtractError    -- base class for everything below
        ConfigError           -- bad / missing YAML configuration
        SecretsManagerError   -- failed to fetch secret from AWS
        OracleConnectionError -- could not connect to Oracle
        OracleQueryError      -- query execution failed
        CSVWriteError         -- failed to write a local CSV file
        S3UploadError         -- failed to upload to S3

Typical usage from a runner program:
    job = OracleToS3Extract("config/extract_example.yaml")
    try:
        config  = job.load_config()             # Step 1
        job.connect()                           # Step 2
        batches = job.execute_query()           # Step 3 (generator)
        files   = job.write_csv_files(batches)  # Step 4
        uris    = job.upload_to_s3(files)       # Step 5
        job.cleanup_local(files)                # Step 6
    finally:
        job.close()
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Tuple

import boto3
import oracledb
import yaml
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)


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
    """Reusable Oracle -> CSV -> S3 extraction toolkit driven by a YAML config.

    Each public method is independent. The runner is expected to call
    load_config() first; subsequent methods rely on self.config being
    populated and will raise ConfigError if it is not.
    """

    # ------------------------------------------------------------------ init

    def __init__(self, config_path: str):
        """Light constructor: just stores the config path.

        Nothing is read or validated until load_config() is called. This
        keeps each piece of functionality independent and explicit.
        """
        self.config_path = config_path
        self.config: Optional[dict] = None
        self.connection: Optional[oracledb.Connection] = None
        self.local_files: List[Path] = []

    # ---------------------------------------------------------------- config

    def load_config(self) -> dict:
        """Step 1: read the YAML config file and return it as a dict.

        Side effects:
            - Stores the parsed dict on self.config so other methods can use it.
            - Validates the schema; raises ConfigError on any problem.
            - Logs an INFO message when reading is complete.

        Returns:
            The parsed YAML as a Python dict (also returned to the runner
            so the runner can inspect / log keys if it wants).

        Raises:
            ConfigError on missing file, invalid YAML, non-mapping root,
            or any schema violation.
        """
        path = self.config_path
        try:
            with open(path, "r") as fh:
                cfg = yaml.safe_load(fh)
        except FileNotFoundError as e:
            raise ConfigError(f"Config file not found: {path}") from e
        except yaml.YAMLError as e:
            raise ConfigError(f"Invalid YAML in {path}: {e}") from e

        if not isinstance(cfg, dict):
            raise ConfigError(f"Config at {path} did not parse to a mapping.")

        self._validate_config(cfg)
        self.config = cfg
        logger.info("Config file read completed: %s", path)
        return cfg

    @staticmethod
    def _validate_config(cfg: dict) -> None:
        for key in ("oracle", "sql", "output", "s3"):
            if key not in cfg:
                raise ConfigError(f"Missing required config key: {key}")

        oracle_cfg = cfg["oracle"]
        if "dsn" not in oracle_cfg:
            raise ConfigError("oracle.dsn is required")

        method = str(oracle_cfg.get("auth_method", "plain")).lower()
        if method == "plain":
            if not oracle_cfg.get("user") or not oracle_cfg.get("password"):
                raise ConfigError(
                    "auth_method 'plain' requires oracle.user and oracle.password"
                )
        elif method == "aws_secret":
            if not oracle_cfg.get("secret_name"):
                raise ConfigError(
                    "auth_method 'aws_secret' requires oracle.secret_name"
                )
        else:
            raise ConfigError(
                f"Unknown oracle.auth_method '{method}'; "
                "expected 'plain' or 'aws_secret'"
            )

        out = cfg["output"]
        for k in ("local_dir", "base_filename", "records_per_file"):
            if k not in out:
                raise ConfigError(f"output.{k} is required")
        if int(out["records_per_file"]) <= 0:
            raise ConfigError("output.records_per_file must be > 0")

        if "bucket" not in cfg["s3"]:
            raise ConfigError("s3.bucket is required")

    def _require_config(self) -> dict:
        """Internal guard: ensures load_config() was called before use."""
        if self.config is None:
            raise ConfigError(
                "Config not loaded. Call load_config() before this method."
            )
        return self.config

    # --------------------------------------------------------------- secrets

    @staticmethod
    def get_secret_from_aws(
        secret_name: str, region: Optional[str] = None
    ) -> dict:
        """Fetch a secret from AWS Secrets Manager and return it as a dict.

        The secret is expected to be a JSON object containing at minimum
        'username' (or 'user') and 'password' fields.

        Raises SecretsManagerError on any failure.
        """
        try:
            session = boto3.session.Session(region_name=region)
            client = session.client("secretsmanager")
            response = client.get_secret_value(SecretId=secret_name)
        except (BotoCoreError, ClientError) as e:
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
            return json.loads(secret_string)
        except json.JSONDecodeError as e:
            raise SecretsManagerError(
                f"Secret '{secret_name}' is not valid JSON: {e}"
            ) from e

    def _resolve_credentials(self) -> Tuple[str, str]:
        """Return (user, password) based on the configured auth_method.

        Raises ConfigError or SecretsManagerError on failure.
        """
        cfg = self._require_config()
        oracle_cfg = cfg["oracle"]
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
        Raises ConfigError if load_config() was not called first.
        Raises SecretsManagerError on credential fetch failure.
        """
        cfg = self._require_config()
        user, password = self._resolve_credentials()
        dsn = cfg["oracle"]["dsn"]
        logger.info("Connecting to Oracle dsn=%s as user=%s", dsn, user)

        try:
            self.connection = oracledb.connect(
                user=user, password=password, dsn=dsn
            )
            logger.info("Oracle connection established.")
            return self.connection
        except oracledb.Error as e:
            raise OracleConnectionError(f"Oracle connection failed: {e}") from e
        except Exception as e:
            raise OracleConnectionError(
                f"Unexpected error establishing Oracle connection: {e}"
            ) from e

    def close(self) -> None:
        """Close the Oracle connection if it is open. Safe to call multiple times."""
        if self.connection is not None:
            try:
                self.connection.close()
                logger.info("Oracle connection closed.")
            except Exception as e:  # noqa: BLE001
                logger.warning("Error closing Oracle connection: %s", e)
            self.connection = None

    # ---------------------------------------------------------------- query

    def execute_query(self) -> Iterator[Tuple[List[str], List[tuple]]]:
        """Step 3: execute the configured SQL and yield (column_names, batch_rows).

        Generator-based: result sets larger than memory are streamed in
        chunks of `fetch.array_size` rows. The writer consumes the
        batches lazily.

        Raises ConfigError if load_config() was not called.
        Raises OracleConnectionError if connect() was not called.
        Raises OracleQueryError on cursor / fetch failures.
        """
        cfg = self._require_config()
        if self.connection is None:
            raise OracleConnectionError(
                "Call connect() before execute_query()."
            )

        sql = cfg["sql"]
        array_size = int(cfg.get("fetch", {}).get("array_size", 5000))

        try:
            cursor = self.connection.cursor()
            cursor.arraysize = array_size
            try:
                cursor.prefetchrows = array_size + 1
            except AttributeError:
                pass

            logger.info("Executing query (arraysize=%s):\n%s", array_size, sql)
            cursor.execute(sql)
            column_names = [d[0] for d in cursor.description]
        except oracledb.Error as e:
            raise OracleQueryError(f"Failed to execute query: {e}") from e

        try:
            while True:
                try:
                    batch = cursor.fetchmany(array_size)
                except oracledb.Error as e:
                    raise OracleQueryError(
                        f"Failed while fetching rows: {e}"
                    ) from e
                if not batch:
                    break
                yield column_names, batch
        finally:
            try:
                cursor.close()
            except Exception:  # noqa: BLE001
                pass

    # --------------------------------------------------------------- writing

    def write_csv_files(
        self, batches: Iterable[Tuple[List[str], List[tuple]]]
    ) -> List[Path]:
        """Step 4: write a stream of batches to one or more local CSV files.

        Splits when the row count for the current file reaches
        records_per_file. File names follow <base>_<n>.csv (1-indexed).
        Returns the list of file paths written.

        Raises ConfigError if load_config() was not called.
        Raises CSVWriteError on filesystem errors.
        """
        cfg = self._require_config()
        out_cfg = cfg["output"]
        local_dir = Path(out_cfg["local_dir"])
        base = out_cfg["base_filename"]
        records_per_file = int(out_cfg["records_per_file"])
        csv_cfg = out_cfg.get("csv") or {}
        delimiter = csv_cfg.get("delimiter", ",")
        include_header = csv_cfg.get("include_header", True)
        quote_all = csv_cfg.get("quote_all", False)

        try:
            local_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise CSVWriteError(
                f"Could not create local_dir {local_dir}: {e}"
            ) from e

        file_index = 0
        rows_in_current = 0
        file_handle = None
        writer = None
        column_names: Optional[List[str]] = None
        files: List[Path] = []

        def open_new_file() -> Path:
            nonlocal file_handle, writer, file_index, rows_in_current
            file_index += 1
            path = local_dir / f"{base}_{file_index}.csv"
            try:
                file_handle = open(path, "w", newline="", encoding="utf-8")
            except OSError as e:
                raise CSVWriteError(f"Could not open {path}: {e}") from e
            writer = csv.writer(
                file_handle,
                delimiter=delimiter,
                quoting=csv.QUOTE_ALL if quote_all else csv.QUOTE_MINIMAL,
            )
            if include_header and column_names is not None:
                writer.writerow(column_names)
            files.append(path)
            rows_in_current = 0
            logger.info("Opened new output file: %s", path)
            return path

        try:
            for cols, batch in batches:
                column_names = cols
                if writer is None:
                    open_new_file()
                for row in batch:
                    if rows_in_current >= records_per_file:
                        file_handle.close()
                        open_new_file()
                    try:
                        writer.writerow(row)
                    except (OSError, csv.Error) as e:
                        raise CSVWriteError(
                            f"Failed writing row to "
                            f"{files[-1] if files else '<unknown>'}: {e}"
                        ) from e
                    rows_in_current += 1
        finally:
            if file_handle and not file_handle.closed:
                try:
                    file_handle.close()
                except Exception:  # noqa: BLE001
                    pass

        self.local_files = files
        logger.info("Wrote %d file(s).", len(files))
        return files

    # -------------------------------------------------------------------- s3

    def upload_to_s3(self, files: Iterable[Path]) -> List[str]:
        """Step 5: upload local CSV files to S3 with optional SSE-KMS encryption.

        Returns the list of s3:// URIs uploaded.
        Raises ConfigError if load_config() was not called.
        Raises S3UploadError on the first failed upload.
        """
        cfg = self._require_config()
        s3_cfg = cfg["s3"]
        bucket = s3_cfg["bucket"]
        prefix = str(s3_cfg.get("prefix", "")).lstrip("/")
        kms_key_id = s3_cfg.get("kms_key_id")
        region = s3_cfg.get("aws_region")

        try:
            session = boto3.session.Session(region_name=region)
            s3 = session.client("s3")
        except (BotoCoreError, ClientError) as e:
            raise S3UploadError(f"Failed to create S3 client: {e}") from e

        uris: List[str] = []
        for path in files:
            key = f"{prefix.rstrip('/')}/{path.name}" if prefix else path.name
            extra: dict = {}
            if kms_key_id:
                extra["ServerSideEncryption"] = "aws:kms"
                extra["SSEKMSKeyId"] = kms_key_id
            try:
                s3.upload_file(str(path), bucket, key, ExtraArgs=extra)
                uri = f"s3://{bucket}/{key}"
                uris.append(uri)
                logger.info("Uploaded %s -> %s", path, uri)
            except (BotoCoreError, ClientError, OSError) as e:
                raise S3UploadError(
                    f"Failed to upload {path} to s3://{bucket}/{key}: {e}"
                ) from e
        return uris

    def cleanup_local(self, files: Iterable[Path]) -> None:
        """Step 6: delete local CSV files after a successful S3 upload.

        Honors the optional `cleanup_local` flag in the YAML (default true).
        Best-effort: logs a warning on per-file failure, does not raise.
        """
        cfg = self._require_config()
        if not bool(cfg.get("cleanup_local", True)):
            logger.info("cleanup_local=false; keeping local files.")
            return
        for path in files:
            try:
                path.unlink()
                logger.info("Deleted local file %s", path)
            except OSError as e:
                logger.warning("Could not delete %s: %s", path, e)
