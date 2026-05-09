"""
oracle_to_s3.py
Reusable Oracle -> CSV -> S3 extraction framework.

Reads all metadata from a YAML config file. Streams Oracle query results
through generators so very large tables don't blow memory, splits output
into multiple CSV files at a configurable row count, and uploads the
files to S3 with optional KMS encryption. Local files are removed after
a successful upload.

Usage:
    from oracle_to_s3 import OracleToS3Extract
    job = OracleToS3Extract("config/extract_example.yaml")
    job.run()
"""

from __future__ import annotations

import csv
import json
import logging
import os
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Tuple

import boto3
import oracledb
import yaml
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)


class OracleToS3ExtractError(Exception):
    """Raised for any framework-level extraction failure."""


class OracleToS3Extract:
    """Reusable Oracle -> CSV -> S3 extraction job driven by a YAML config.

    Public methods:
        get_secret_from_aws  -- fetch credentials from AWS Secrets Manager
        connect              -- establish Oracle connection
        execute_query        -- run SQL and yield rows in batches (generator)
        write_csv_files      -- write a stream of batches to chunked CSV files
        upload_to_s3         -- upload local files to S3 with optional KMS
        cleanup_local        -- delete local files after successful upload
        run                  -- end-to-end orchestration
        close                -- close the Oracle connection
    """

    # ------------------------------------------------------------------ init

    def __init__(self, config_path: str):
        self.config_path = config_path
        self.config = self._load_config(config_path)
        self._validate_config(self.config)
        self.connection: Optional[oracledb.Connection] = None
        self.local_files: List[Path] = []

    # ---------------------------------------------------------------- config

    @staticmethod
    def _load_config(path: str) -> dict:
        with open(path, "r") as fh:
            cfg = yaml.safe_load(fh)
        if not isinstance(cfg, dict):
            raise OracleToS3ExtractError(
                f"Config at {path} did not parse to a mapping."
            )
        return cfg

    @staticmethod
    def _validate_config(cfg: dict) -> None:
        for key in ("oracle", "sql", "output", "s3"):
            if key not in cfg:
                raise OracleToS3ExtractError(f"Missing required config key: {key}")

        oracle_cfg = cfg["oracle"]
        if "dsn" not in oracle_cfg:
            raise OracleToS3ExtractError("oracle.dsn is required")

        method = str(oracle_cfg.get("auth_method", "plain")).lower()
        if method == "plain":
            if not oracle_cfg.get("user") or not oracle_cfg.get("password"):
                raise OracleToS3ExtractError(
                    "auth_method 'plain' requires oracle.user and oracle.password"
                )
        elif method == "aws_secret":
            if not oracle_cfg.get("secret_name"):
                raise OracleToS3ExtractError(
                    "auth_method 'aws_secret' requires oracle.secret_name"
                )
        else:
            raise OracleToS3ExtractError(
                f"Unknown oracle.auth_method '{method}'; "
                "expected 'plain' or 'aws_secret'"
            )

        out = cfg["output"]
        for k in ("local_dir", "base_filename", "records_per_file"):
            if k not in out:
                raise OracleToS3ExtractError(f"output.{k} is required")
        if int(out["records_per_file"]) <= 0:
            raise OracleToS3ExtractError("output.records_per_file must be > 0")

        if "bucket" not in cfg["s3"]:
            raise OracleToS3ExtractError("s3.bucket is required")

    # --------------------------------------------------------------- secrets

    @staticmethod
    def get_secret_from_aws(
        secret_name: str, region: Optional[str] = None
    ) -> dict:
        """Fetch a secret from AWS Secrets Manager and return it as a dict.

        The secret is expected to be a JSON object containing at minimum
        'username' (or 'user') and 'password' fields.
        """
        try:
            session = boto3.session.Session(region_name=region)
            client = session.client("secretsmanager")
            response = client.get_secret_value(SecretId=secret_name)
        except (BotoCoreError, ClientError) as e:
            raise OracleToS3ExtractError(
                f"Failed to fetch secret '{secret_name}' from "
                f"Secrets Manager: {e}"
            ) from e

        secret_string = response.get("SecretString")
        if not secret_string:
            raise OracleToS3ExtractError(
                f"Secret '{secret_name}' is empty or binary; "
                "expected a JSON string."
            )
        try:
            return json.loads(secret_string)
        except json.JSONDecodeError as e:
            raise OracleToS3ExtractError(
                f"Secret '{secret_name}' is not valid JSON: {e}"
            ) from e

    def _resolve_credentials(self) -> Tuple[str, str]:
        """Return (user, password) based on configured auth_method."""
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
            raise OracleToS3ExtractError(
                f"Secret '{secret_name}' missing 'username'/'password' fields"
            )
        return user, password

    # ------------------------------------------------------------ connection

    def connect(self) -> oracledb.Connection:
        """Establish the Oracle connection. Print the error and re-raise on failure."""
        try:
            user, password = self._resolve_credentials()
            dsn = self.config["oracle"]["dsn"]
            logger.info("Connecting to Oracle dsn=%s as user=%s", dsn, user)
            self.connection = oracledb.connect(
                user=user, password=password, dsn=dsn
            )
            logger.info("Oracle connection established.")
            return self.connection
        except oracledb.Error as e:
            print(f"[ERROR] Failed to connect to Oracle: {e}")
            raise OracleToS3ExtractError(f"Oracle connection failed: {e}") from e
        except OracleToS3ExtractError:
            raise
        except Exception as e:
            print(f"[ERROR] Unexpected error establishing Oracle connection: {e}")
            raise OracleToS3ExtractError(str(e)) from e

    def close(self) -> None:
        """Close the Oracle connection if it is open."""
        if self.connection is not None:
            try:
                self.connection.close()
                logger.info("Oracle connection closed.")
            except Exception as e:  # noqa: BLE001
                logger.warning("Error closing Oracle connection: %s", e)
            self.connection = None

    # ---------------------------------------------------------------- query

    def execute_query(self) -> Iterator[Tuple[List[str], List[tuple]]]:
        """Execute the configured SQL and yield (column_names, batch_rows).

        This is a generator so result sets larger than memory are streamed
        in chunks of `fetch.array_size` rows. The writer consumes these
        batches lazily.
        """
        if self.connection is None:
            raise OracleToS3ExtractError("Call connect() before execute_query().")

        sql = self.config["sql"]
        array_size = int(self.config.get("fetch", {}).get("array_size", 5000))

        cursor = self.connection.cursor()
        cursor.arraysize = array_size
        # Recommended: prefetchrows == arraysize + 1 in oracledb
        try:
            cursor.prefetchrows = array_size + 1
        except AttributeError:
            pass

        logger.info("Executing query (arraysize=%s):\n%s", array_size, sql)
        cursor.execute(sql)
        column_names = [d[0] for d in cursor.description]

        try:
            while True:
                batch = cursor.fetchmany(array_size)
                if not batch:
                    break
                yield column_names, batch
        finally:
            cursor.close()

    # --------------------------------------------------------------- writing

    def write_csv_files(
        self, batches: Iterable[Tuple[List[str], List[tuple]]]
    ) -> List[Path]:
        """Write a stream of batches to one or more local CSV files.

        Splits when the row count for the current file reaches
        records_per_file. File names follow <base>_<n>.csv (1-indexed).
        Returns the list of file paths written.
        """
        out_cfg = self.config["output"]
        local_dir = Path(out_cfg["local_dir"])
        base = out_cfg["base_filename"]
        records_per_file = int(out_cfg["records_per_file"])
        csv_cfg = out_cfg.get("csv") or {}
        delimiter = csv_cfg.get("delimiter", ",")
        include_header = csv_cfg.get("include_header", True)
        quote_all = csv_cfg.get("quote_all", False)

        local_dir.mkdir(parents=True, exist_ok=True)

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
            file_handle = open(path, "w", newline="", encoding="utf-8")
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
                    writer.writerow(row)
                    rows_in_current += 1
        finally:
            if file_handle and not file_handle.closed:
                file_handle.close()

        self.local_files = files
        logger.info("Wrote %d file(s).", len(files))
        return files

    # -------------------------------------------------------------------- s3

    def upload_to_s3(self, files: Iterable[Path]) -> List[str]:
        """Upload local CSV files to S3 with optional SSE-KMS encryption.

        Returns the list of s3:// URIs uploaded. Raises on any failure.
        """
        s3_cfg = self.config["s3"]
        bucket = s3_cfg["bucket"]
        prefix = str(s3_cfg.get("prefix", "")).lstrip("/")
        kms_key_id = s3_cfg.get("kms_key_id")
        region = s3_cfg.get("aws_region")

        session = boto3.session.Session(region_name=region)
        s3 = session.client("s3")
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
            except (BotoCoreError, ClientError) as e:
                raise OracleToS3ExtractError(
                    f"Failed to upload {path} to s3://{bucket}/{key}: {e}"
                ) from e
        return uris

    def cleanup_local(self, files: Iterable[Path]) -> None:
        """Delete local CSV files after a successful S3 upload."""
        if not bool(self.config.get("cleanup_local", True)):
            logger.info("cleanup_local=false; keeping local files.")
            return
        for path in files:
            try:
                path.unlink()
                logger.info("Deleted local file %s", path)
            except OSError as e:
                logger.warning("Could not delete %s: %s", path, e)

    # ------------------------------------------------------------ orchestrate

    def run(self) -> List[str]:
        """End-to-end pipeline: connect -> query -> CSV -> S3 -> cleanup.

        Returns the list of s3:// URIs uploaded.
        """
        try:
            self.connect()
            batches = self.execute_query()
            files = self.write_csv_files(batches)
            if not files:
                logger.warning("No data extracted; nothing to upload.")
                return []
            uris = self.upload_to_s3(files)
            self.cleanup_local(files)
            return uris
        finally:
            self.close()
