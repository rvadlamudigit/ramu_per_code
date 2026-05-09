"""
runner.py -- CLI entry point for the Oracle -> S3 extract framework.

This program owns BOTH:
  1. The orchestration: it explicitly calls each step on the class in
     the right order (connect -> execute_query -> write_csv_files ->
     upload_to_s3 -> cleanup_local -> close).
  2. The error presentation: a single main exception block maps each
     framework exception type to a clear message and a distinct exit
     code. The class itself never prints or sys.exits.

Usage:
    python runner.py path/to/extract.yaml
    python runner.py -v path/to/extract.yaml      # debug logging
"""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from typing import List

from oracle_to_s3 import (
    OracleToS3Extract,
    OracleToS3ExtractError,
    ConfigError,
    SecretsManagerError,
    OracleConnectionError,
    OracleQueryError,
    CSVWriteError,
    S3UploadError,
)


# ----------------------------- exit codes -----------------------------------
# 0   success
# 1   generic framework error
# 2   config error
# 3   secrets manager error
# 4   oracle connection error
# 5   oracle query error
# 6   csv write error
# 7   s3 upload error
# 99  unexpected (non-framework) exception
# 130 interrupted (SIGINT)
# ----------------------------------------------------------------------------

EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_CONFIG = 2
EXIT_SECRETS = 3
EXIT_ORACLE_CONNECT = 4
EXIT_ORACLE_QUERY = 5
EXIT_CSV = 6
EXIT_S3 = 7
EXIT_UNEXPECTED = 99
EXIT_INTERRUPT = 130


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run an Oracle -> CSV -> S3 extract from a YAML config. "
            "All metadata (DB, SQL, output, S3, KMS) is read from the YAML."
        )
    )
    parser.add_argument("config", help="Path to the YAML config file.")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging."
    )
    return parser.parse_args()


# ----------------------------------------------------------- orchestration --

def run_extract(config_path: str) -> List[str]:
    """Build the job and walk the pipeline step-by-step.

    Any framework exception raised by a step propagates out so the
    caller's main exception block can present it.
    """
    log = logging.getLogger("runner")
    log.info("Loading config: %s", config_path)

    job = OracleToS3Extract(config_path)

    try:
        # Step 1: establish Oracle connection
        log.info("Step 1/5: connecting to Oracle...")
        job.connect()

        # Step 2: execute SQL (returns a generator over batches)
        log.info("Step 2/5: executing SQL...")
        batches = job.execute_query()

        # Step 3: stream batches into chunked CSV files on local disk
        log.info("Step 3/5: writing CSV files to local disk...")
        files = job.write_csv_files(batches)

        if not files:
            log.warning("No data extracted; nothing to upload.")
            return []

        # Step 4: upload local files to S3 (with optional KMS)
        log.info("Step 4/5: uploading %d file(s) to S3...", len(files))
        uris = job.upload_to_s3(files)

        # Step 5: delete local copies after successful upload
        log.info("Step 5/5: cleaning up local files...")
        job.cleanup_local(files)

        return uris
    finally:
        # Always close the Oracle connection, even on error.
        job.close()


# --------------------------------------------------------------------- main --

def main() -> int:
    args = parse_args()
    setup_logging(args.verbose)
    log = logging.getLogger("runner")

    # ============================ MAIN EXCEPTION BLOCK ===========================
    try:
        uris = run_extract(args.config)

        if not uris:
            log.warning("Extract complete but no files were uploaded.")
            return EXIT_OK

        print(f"Extract complete. {len(uris)} file(s) uploaded:")
        for uri in uris:
            print(f"  - {uri}")
        return EXIT_OK

    except ConfigError as e:
        print(f"[ERROR] Configuration error: {e}", file=sys.stderr)
        return EXIT_CONFIG

    except SecretsManagerError as e:
        print(f"[ERROR] AWS Secrets Manager error: {e}", file=sys.stderr)
        return EXIT_SECRETS

    except OracleConnectionError as e:
        print(f"[ERROR] Oracle connection error: {e}", file=sys.stderr)
        return EXIT_ORACLE_CONNECT

    except OracleQueryError as e:
        print(f"[ERROR] Oracle query error: {e}", file=sys.stderr)
        return EXIT_ORACLE_QUERY

    except CSVWriteError as e:
        print(f"[ERROR] CSV write error: {e}", file=sys.stderr)
        return EXIT_CSV

    except S3UploadError as e:
        print(f"[ERROR] S3 upload error: {e}", file=sys.stderr)
        return EXIT_S3

    except OracleToS3ExtractError as e:
        # Fallback for any new framework exception subclass we haven't
        # added a specific handler for above.
        print(f"[ERROR] Extract failed: {e}", file=sys.stderr)
        return EXIT_GENERIC

    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        return EXIT_INTERRUPT

    except Exception as e:  # noqa: BLE001
        # Final safety net: anything not raised by the framework.
        print(
            f"[ERROR] Unexpected error: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        if args.verbose:
            traceback.print_exc(file=sys.stderr)
        else:
            log.exception("Unexpected error during extract")
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    sys.exit(main())
