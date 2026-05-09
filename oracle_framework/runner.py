"""
runner.py -- CLI entry point for the Oracle -> S3 extract framework.

This program owns ALL error presentation. The framework class only
raises exceptions; this runner has a single main exception block that
maps each exception type to a clear message and an appropriate exit
code.

Pipeline:
    1. Load YAML config (constructor of OracleToS3Extract)
    2. Establish Oracle connection
    3. Execute SQL (streamed via generator)
    4. Write results to chunked CSV files on local disk
    5. Upload files to S3 (with optional KMS)
    6. Delete local files after successful upload

Usage:
    python runner.py path/to/extract.yaml
    python runner.py -v path/to/extract.yaml      # debug logging
"""

from __future__ import annotations

import argparse
import logging
import sys
import traceback

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


def run_extract(config_path: str) -> list:
    """Build the job and run the full pipeline. Errors propagate."""
    log = logging.getLogger("runner")
    log.info("Loading config: %s", config_path)
    job = OracleToS3Extract(config_path)
    return job.run()


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
        # Catches any framework error not covered by the specific cases above.
        print(f"[ERROR] Extract failed: {e}", file=sys.stderr)
        return EXIT_GENERIC

    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        return EXIT_INTERRUPT

    except Exception as e:  # noqa: BLE001
        # Catch-all for anything not raised by the framework. We print the
        # traceback so unexpected bugs are visible during development.
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
