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
    Step 1a load_config         -- read & parse YAML, return dict
    Step 1b validate_config     -- schema check (lives in this runner;
                                   raises ConfigError on any violation)
    Step 2  connect             -- open Oracle connection
    Step 3  execute_query       -- generator over result batches
    Step 4  write_csv_files     -- chunked CSV files on local disk
    Step 5  upload_to_s3        -- upload (optionally KMS-encrypted)
    Step 6  cleanup_local       -- delete local copies

Usage:
    python runner.py path/to/extract.yaml
    python runner.py -v path/to/extract.yaml      # stdlib DEBUG only
    python runner.py --debug path/to/extract.yaml # full framework debug mode
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import sys
import time
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

def setup_basic_logging(verbose: bool = False, debug: bool = False) -> None:
    """Bootstrap stdlib logging so messages from before LoggerClient setup
    (config-load errors, etc.) are still visible.

    --debug always implies DEBUG level. --verbose alone also enables DEBUG.
    The debug formatter additionally includes module name and line number
    so traces back to source code are easy.
    """
    if debug:
        fmt = (
            "%(asctime)s [%(levelname)-7s] %(name)s "
            "(%(filename)s:%(lineno)d): %(message)s"
        )
        level = logging.DEBUG
    elif verbose:
        fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        level = logging.DEBUG
    else:
        fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        level = logging.INFO
    logging.basicConfig(level=level, format=fmt)


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
        "-v", "--verbose", action="store_true",
        help="Enable stdlib DEBUG-level logging (similar to --debug but "
             "without the per-batch / metrics traces).",
    )
    parser.add_argument(
        "-d", "--debug", action="store_true",
        help="Enable full framework debug mode: per-batch row counts, "
             "per-file sizes, query/write/upload timings, redacted config "
             "dump, Oracle/boto3 versions, and source file/line in log "
             "lines. Also forces DEBUG-level on every handler.",
    )
    return parser.parse_args()


# --------------------------------------------------------- config validation

def validate_config(cfg: dict) -> None:
    """Validate the YAML config dict against the framework's schema.

    Lives in the runner (not in oracle_to_s3.py) so that the framework
    class stays a pure toolkit -- the runner is the single place that
    decides what counts as a valid extract job.

    Raises ConfigError on the first violation; logs a DEBUG line on
    success summarising the key knobs that were checked.
    """
    log = logging.getLogger("runner")
    log.debug("validate_config: checking required top-level keys")
    if not isinstance(cfg, dict):
        raise ConfigError("Config did not parse to a mapping.")

    for key in ("oracle", "sql", "output", "s3"):
        if key not in cfg:
            raise ConfigError(f"Missing required config key: {key}")

    oracle_cfg = cfg["oracle"]
    if not isinstance(oracle_cfg, dict):
        raise ConfigError("'oracle' section must be a mapping")
    if "dsn" not in oracle_cfg:
        raise ConfigError("oracle.dsn is required")

    method = str(oracle_cfg.get("auth_method", "plain")).lower()
    log.debug("validate_config: oracle.auth_method=%s", method)
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
    if not isinstance(out, dict):
        raise ConfigError("'output' section must be a mapping")
    for k in ("local_dir", "base_filename", "records_per_file"):
        if k not in out:
            raise ConfigError(f"output.{k} is required")
    try:
        rpf = int(out["records_per_file"])
    except (TypeError, ValueError) as e:
        raise ConfigError(
            f"output.records_per_file must be an integer, got "
            f"{out['records_per_file']!r}"
        ) from e
    if rpf <= 0:
        raise ConfigError("output.records_per_file must be > 0")

    s3_cfg = cfg["s3"]
    if not isinstance(s3_cfg, dict):
        raise ConfigError("'s3' section must be a mapping")
    if "bucket" not in s3_cfg:
        raise ConfigError("s3.bucket is required")

    log.debug(
        "validate_config: OK (records_per_file=%s, bucket=%s, prefix=%s, "
        "kms=%s)",
        rpf, s3_cfg["bucket"],
        s3_cfg.get("prefix") or "<none>",
        "yes" if s3_cfg.get("kms_key_id") else "no",
    )


def _log_runtime_environment(log: logging.Logger) -> None:
    """Emit a one-shot DEBUG block describing the runtime environment.

    Called once when --debug is on, so that any post-mortem investigation
    can see exactly which Python / OS / interpreter / cwd produced the run.
    """
    log.debug("---- runtime environment ----")
    log.debug("  python      : %s", sys.version.replace("\n", " "))
    log.debug("  executable  : %s", sys.executable)
    log.debug("  platform    : %s", platform.platform())
    log.debug("  pid         : %s", os.getpid())
    log.debug("  cwd         : %s", os.getcwd())
    log.debug("  argv        : %s", sys.argv)
    # Surface AWS-related env vars (presence only, never values).
    aws_env = sorted(
        k for k in os.environ
        if k.startswith("AWS_") and k not in {"AWS_SECRET_ACCESS_KEY"}
    )
    log.debug("  AWS_* env   : %s", aws_env or "<none>")
    log.debug("-----------------------------")


# --------------------------------------------------------- logger integration

def configure_logger_from_yaml(
    config: dict, verbose: bool = False, debug: bool = False
) -> Optional[LoggerClient]:
    """If YAML has a `logging:` section, build LoggerClient and attach
    its handlers to the root logger. Returns the LoggerClient (or None
    if the section is absent).

    When debug=True the LoggerClient is constructed in debug mode, which
    forces every handler (stream + files) to DEBUG and uses a richer
    formatter that includes module/line info.

    Expected YAML:
        logging:
          project_name:    oracle_framework
          process_name:    big_table_extract
          root_directory:  /var/log/oracle_framework
          output:          BOTH        # STDOUT | LOGFILE | BOTH
    """
    log_cfg = config.get("logging")
    if not log_cfg:
        logging.getLogger("runner").debug(
            "No `logging:` section in YAML; staying on stdlib basicConfig."
        )
        return None

    lf = LoggerClient(
        project_name=log_cfg.get("project_name", "oracle_framework"),
        process_name=log_cfg.get("process_name", "extract"),
        root_directory=log_cfg.get("root_directory"),
        logoutput=str(log_cfg.get("output", "BOTH")).upper(),
        debug=debug,
    )
    lf.attach_to_root()
    if verbose or debug:
        logging.getLogger().setLevel(logging.DEBUG)

    lf.logfile.info(
        "LoggerClient initialized; output=%s, debug=%s",
        lf.logoutput, debug,
    )
    if lf.log_file:
        lf.logfile.info(
            "Log files: %s.log / %s.error / %s.critical%s",
            lf.log_file, lf.log_file, lf.log_file,
            f" / {lf.log_file}.debug" if debug else "",
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
    pipeline_start = time.monotonic()
    try:
        log.info("Step 2/6: connecting to Oracle ...")
        t = time.monotonic()
        job.connect()
        log.debug("Step 2 elapsed: %.3fs", time.monotonic() - t)

        log.info("Step 3/6: executing SQL ...")
        batches = job.execute_query()  # generator -- timing logged in writer

        log.info("Step 4/6: writing CSV files to local disk ...")
        t = time.monotonic()
        files = job.write_csv_files(batches)
        log.debug("Step 4 elapsed: %.3fs", time.monotonic() - t)

        if not files:
            log.warning("No data extracted; nothing to upload.")
            return []

        log.info("Step 5/6: uploading %d file(s) to S3 ...", len(files))
        t = time.monotonic()
        uris = job.upload_to_s3(files)
        log.debug("Step 5 elapsed: %.3fs", time.monotonic() - t)

        log.info("Step 6/6: cleaning up local files ...")
        t = time.monotonic()
        job.cleanup_local(files)
        log.debug("Step 6 elapsed: %.3fs", time.monotonic() - t)

        return uris
    finally:
        log.info(
            "Pipeline phase finished in %.3fs (will close DB).",
            time.monotonic() - pipeline_start,
        )
        job.close()
        # Always emit the metrics summary, success or failure.
        try:
            job.log_summary()
        except Exception as e:  # noqa: BLE001
            log.debug("log_summary raised (ignored): %s", e)


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
    debug: bool = False,
) -> int:
    log = logging.getLogger("runner")
    print(f"[ERROR] {label}: {error}", file=sys.stderr)
    # In --debug or --verbose, ALWAYS dump the chained traceback so the
    # user sees the original exception too (chained via `raise ... from e`).
    if debug or verbose:
        log.exception("%s -> %s: %s", label, type(error).__name__, error)
    if (verbose or debug) and not isinstance(error, OracleToS3ExtractError):
        traceback.print_exc(file=sys.stderr)
    send_notification(
        config, "on_failure",
        error=error,
        log_file_path=_log_file_for_attachment(lf),
    )
    return exit_code


def main() -> int:
    args = parse_args()
    setup_basic_logging(args.verbose, debug=args.debug)
    log = logging.getLogger("runner")

    if args.debug:
        log.debug("DEBUG mode enabled via --debug")
        _log_runtime_environment(log)

    overall_start = time.monotonic()
    config: Optional[dict] = None
    lf: Optional[LoggerClient] = None

    # ============================ MAIN EXCEPTION BLOCK ===========================
    try:
        # Step 1: build job and load config
        job = OracleToS3Extract(args.config, debug=args.debug)
        log.info("Step 1/6: loading YAML config from %s ...", args.config)
        config = job.load_config()
        log.info("Loaded config sections: %s", sorted(config.keys()))

        # Step 1b: schema validation (lives in the runner, not the framework).
        log.info("Step 1/6: validating config schema ...")
        validate_config(config)
        log.info("Config validation passed.")

        # Switch to LoggerClient if YAML configures it
        lf = configure_logger_from_yaml(
            config, verbose=args.verbose, debug=args.debug or job.debug,
        )

        # Steps 2-6
        uris = run_pipeline(job)

        if not uris:
            log.warning("Extract complete but no files were uploaded.")
        else:
            print(f"Extract complete. {len(uris)} file(s) uploaded:")
            for uri in uris:
                print(f"  - {uri}")

        log.info(
            "Total runner wall time: %.3fs",
            time.monotonic() - overall_start,
        )

        # Success notification (best-effort)
        send_notification(
            config, "on_success",
            files=uris,
            log_file_path=_log_file_for_attachment(lf),
        )
        return EXIT_OK

    except ConfigError as e:
        return _handle_error(
            e, EXIT_CONFIG, "Configuration error",
            config, lf, args.verbose, args.debug,
        )
    except SecretsManagerError as e:
        return _handle_error(
            e, EXIT_SECRETS, "AWS Secrets Manager error",
            config, lf, args.verbose, args.debug,
        )
    except OracleConnectionError as e:
        return _handle_error(
            e, EXIT_ORACLE_CONNECT, "Oracle connection error",
            config, lf, args.verbose, args.debug,
        )
    except OracleQueryError as e:
        return _handle_error(
            e, EXIT_ORACLE_QUERY, "Oracle query error",
            config, lf, args.verbose, args.debug,
        )
    except CSVWriteError as e:
        return _handle_error(
            e, EXIT_CSV, "CSV write error",
            config, lf, args.verbose, args.debug,
        )
    except S3UploadError as e:
        return _handle_error(
            e, EXIT_S3, "S3 upload error",
            config, lf, args.verbose, args.debug,
        )
    except OracleToS3ExtractError as e:
        # Fallback for any new framework exception subclass.
        return _handle_error(
            e, EXIT_GENERIC, "Extract failed",
            config, lf, args.verbose, args.debug,
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
        if args.verbose or args.debug:
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
