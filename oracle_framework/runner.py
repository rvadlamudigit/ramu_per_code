"""
runner.py -- CLI entry point for the Oracle -> S3 extract framework.

Orchestrates a single extract:
  1. Load YAML config
  2. Establish Oracle connection
  3. Execute SQL (streamed via generator)
  4. Write results to chunked CSV files on local disk
  5. Upload files to S3 (with optional KMS)
  6. Delete local files after successful upload

Usage:
    python runner.py path/to/extract.yaml
"""

from __future__ import annotations

import argparse
import logging
import sys

from oracle_to_s3 import OracleToS3Extract, OracleToS3ExtractError


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def main() -> int:
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
    args = parser.parse_args()

    setup_logging(args.verbose)
    log = logging.getLogger("runner")

    try:
        log.info("Loading config: %s", args.config)
        job = OracleToS3Extract(args.config)

        # The class also exposes connect/execute_query/write_csv_files/
        # upload_to_s3/cleanup_local individually, but run() orchestrates
        # them in the correct order with cleanup on failure.
        uris = job.run()

        if not uris:
            log.warning("Extract complete but no files were uploaded.")
            return 0

        print(f"Extract complete. {len(uris)} file(s) uploaded:")
        for uri in uris:
            print(f"  - {uri}")
        return 0

    except OracleToS3ExtractError as e:
        print(f"[ERROR] Extract failed: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
