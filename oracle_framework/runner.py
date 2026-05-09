"""
runner.py -- CLI entry point for the Oracle -> S3 extract framework.

Drives the toolkit and integrates two utility modules:

  - logger.py     LoggerClient is wired up if the YAML config contains a
                  `logging:` section. Its handlers are attached to the
                  root logger so the framework's `logging.getLogger(...)`
                  calls flow into the same files / stream.

  - sdc_email.py  SdcAlertUtil is used to send success and/or failure
                  notifications if the YAML config contains a
                  `notifications:` section. Notifications are
                  best-effort: a notification failure is logged but does
                  not change the program's exit code.

Pipeline:
    Step 1  load_config         -- read YAML, return dict
    Step 2  connect             -- open Oracle connection
    Step 3  execute_query       -- generator over result batches
    Step 4  write_csv_files     -- chunked CSV files on local disk
    Step 5  upload_to_s3        -- upload (optionally KMS-encrypted)
    Step 6  cleanup_local       -- delete local copies

Usage:
    python runner.py path/to/extract.yaml
    python runner.py -v path/to/extract.yaml      # debug logging
"""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from typing import List, Optional

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
from logger import LoggerClient
from sdc_email import SdcAlertUtil, EmailSendError


# ----------------------------- exit codes -----------------------------------
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


# ----------------------------------------------------------- bootstrap setup

def setup_basic_logging(verbose: bool = False) -> None:
    """Bootstrap stdlib logging so messages from before LoggerClient setup
    (config-load errors, etc.) are still visible.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run an Oracle -> CSV -> S3 extract from a YAML config. "
            "All metadata (DB, SQL, output, S3, KMS, logging, notifications) "
            "is read from the YAML."
        )
    )
    parser.add_argument("config", help="Path to the YAML config file.")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging."
    )
    return parser.parse_args()


# --------------------------------------------------------- logger integration

def configure_logger_from_yaml(
    config: dict, verbose: bool = False
) -> Optional[LoggerClient]:
    """If YAML has a `logging:` section, build LoggerClient and attach
    its handlers to the root logger. Returns the LoggerClient (or None
    if the section is absent).

    Expected YAML:
        logging:
          project_name:    oracle_framework
          process_name:    big_table_extract
          root_directory:  /var/log/oracle_framework
          output:          BOTH        # STDOUT | LOGFILE | BOTH
    """
    log_cfg = config.get("logging")
    if not log_cfg:
        return None

    lf = LoggerClient(
        project_name=log_cfg.get("project_name", "oracle_framework"),
        process_name=log_cfg.get("process_name", "extract"),
        root_directory=log_cfg.get("root_directory"),
        logoutput=str(log_cfg.get("output", "BOTH")).upper(),
    )
    lf.attach_to_root()
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    lf.logfile.info("LoggerClient initialized; output=%s", lf.logoutput)
    if lf.log_file:
        lf.logfile.info(
            "Log files: %s.log / %s.error / %s.critical",
            lf.log_file, lf.log_file, lf.log_file,
        )
    return lf


# -------------------------------------------------------- email notifications

def send_notification(
    config: Optional[dict],
    on: str,                                  # 'on_success' or 'on_failure'
    *,
    error: Optional[BaseException] = None,
    files: Optional[List[str]] = None,
    log_file_path: Optional[str] = None,
) -> None:
    """Send an SMTP notification using sdc_email if YAML configures it.

    Best-effort: any error from the email step is logged at WARNING and
    swallowed so that notification problems never change the program's
    exit code.

    Expected YAML:
        notifications:
          on_failure:
            enabled: true
            smtp_server: smtp.example.com
            from_addr:   noreply@example.com
            to_addr:     ops@example.com,backup@example.com
            subject_prefix: "[oracle_framework]"
            port: 25
            use_tls: false
            username: null
            password: null
          on_success:
            enabled: false
            ... (same fields)
    """
    log = logging.getLogger("runner")
    if config is None:
        return
    notif_root = config.get("notifications") or {}
    cfg = notif_root.get(on) or {}
    if not cfg.get("enabled"):
        return

    try:
        smtp_server = cfg["smtp_server"]
        from_addr = cfg["from_addr"]
        to_addr = cfg["to_addr"]
    except KeyError as e:
        log.warning(
            "notifications.%s is enabled but missing required field %s; "
            "skipping email.", on, e,
        )
        return

    prefix = cfg.get("subject_prefix", "[oracle_framework]")
    if on == "on_success":
        subject = f"{prefix} SUCCESS"
        lines = ["Oracle -> S3 extract completed successfully."]
        if files:
            lines.append("")
            lines.append(f"Uploaded {len(files)} file(s):")
            lines.extend(f"  - {f}" for f in files)
    else:
        subject = f"{prefix} FAILURE"
        lines = ["Oracle -> S3 extract failed."]
        if error is not None:
            lines.append("")
            lines.append(f"Error type: {type(error).__name__}")
            lines.append(f"Error message: {error}")
    body = "\n".join(lines)

    try:
        SdcAlertUtil().send_email(
            smtp_server=smtp_server,
            from_addr=from_addr,
            to_addr=to_addr,
            subject=subject,
            body=body,
            body_subtype=cfg.get("body_subtype", "plain"),
            filename=log_file_path,
            port=int(cfg.get("port", 25)),
            use_tls=bool(cfg.get("use_tls", False)),
            username=cfg.get("username"),
            password=cfg.get("password"),
        )
        log.info("Notification email sent (%s).", on)
    except EmailSendError as e:
        log.warning("Notification email (%s) failed after retries: %s", on, e)
    except Exception as e:  # noqa: BLE001
        log.warning("Notification email (%s) raised unexpectedly: %s", on, e)


# ----------------------------------------------------------- orchestration --

def run_pipeline(job: OracleToS3Extract) -> List[str]:
    """Drive Steps 2-6 on an already-configured job. Errors propagate."""
    log = logging.getLogger("runner")
    try:
        log.info("Step 2/6: connecting to Oracle ...")
        job.connect()

        log.info("Step 3/6: executing SQL ...")
        batches = job.execute_query()

        log.info("Step 4/6: writing CSV files to local disk ...")
        files = job.write_csv_files(batches)

        if not files:
            log.warning("No data extracted; nothing to upload.")
            return []

        log.info("Step 5/6: uploading %d file(s) to S3 ...", len(files))
        uris = job.upload_to_s3(files)

        log.info("Step 6/6: cleaning up local files ...")
        job.cleanup_local(files)

        return uris
    finally:
        job.close()


# --------------------------------------------------------------------- main --

def _log_file_for_attachment(lf: Optional[LoggerClient]) -> Optional[str]:
    """Return the .log file path for email attachment, or None."""
    if lf is None or not lf.log_file:
        return None
    return f"{lf.log_file}.log"


def _handle_error(
    error: BaseException,
    exit_code: int,
    label: str,
    config: Optional[dict],
    lf: Optional[LoggerClient],
    verbose: bool = False,
) -> int:
    print(f"[ERROR] {label}: {error}", file=sys.stderr)
    if verbose and not isinstance(error, OracleToS3ExtractError):
        traceback.print_exc(file=sys.stderr)
    send_notification(
        config, "on_failure",
        error=error,
        log_file_path=_log_file_for_attachment(lf),
    )
    return exit_code


def main() -> int:
    args = parse_args()
    setup_basic_logging(args.verbose)
    log = logging.getLogger("runner")

    config: Optional[dict] = None
    lf: Optional[LoggerClient] = None

    # ============================ MAIN EXCEPTION BLOCK ===========================
    try:
        # Step 1: build job and load config
        job = OracleToS3Extract(args.config)
        log.info("Step 1/6: loading YAML config from %s ...", args.config)
        config = job.load_config()
        log.info("Loaded config sections: %s", sorted(config.keys()))

        # Switch to LoggerClient if YAML configures it
        lf = configure_logger_from_yaml(config, verbose=args.verbose)

        # Steps 2-6
        uris = run_pipeline(job)

        if not uris:
            log.warning("Extract complete but no files were uploaded.")
        else:
            print(f"Extract complete. {len(uris)} file(s) uploaded:")
            for uri in uris:
                print(f"  - {uri}")

        # Success notification (best-effort)
        send_notification(
            config, "on_success",
            files=uris,
            log_file_path=_log_file_for_attachment(lf),
        )
        return EXIT_OK

    except ConfigError as e:
        return _handle_error(
            e, EXIT_CONFIG, "Configuration error", config, lf, args.verbose
        )
    except SecretsManagerError as e:
        return _handle_error(
            e, EXIT_SECRETS, "AWS Secrets Manager error",
            config, lf, args.verbose,
        )
    except OracleConnectionError as e:
        return _handle_error(
            e, EXIT_ORACLE_CONNECT, "Oracle connection error",
            config, lf, args.verbose,
        )
    except OracleQueryError as e:
        return _handle_error(
            e, EXIT_ORACLE_QUERY, "Oracle query error",
            config, lf, args.verbose,
        )
    except CSVWriteError as e:
        return _handle_error(
            e, EXIT_CSV, "CSV write error", config, lf, args.verbose
        )
    except S3UploadError as e:
        return _handle_error(
            e, EXIT_S3, "S3 upload error", config, lf, args.verbose
        )
    except OracleToS3ExtractError as e:
        # Fallback for any new framework exception subclass.
        return _handle_error(
            e, EXIT_GENERIC, "Extract failed", config, lf, args.verbose
        )

    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        # No notification on user interrupt.
        return EXIT_INTERRUPT

    except Exception as e:  # noqa: BLE001
        # Final safety net.
        print(
            f"[ERROR] Unexpected error: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        if args.verbose:
            traceback.print_exc(file=sys.stderr)
        else:
            log.exception("Unexpected error during extract")
        send_notification(
            config, "on_failure",
            error=e,
            log_file_path=_log_file_for_attachment(lf),
        )
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    sys.exit(main())
