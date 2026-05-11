"""
runner.py -- CLI entry point for the Oracle -> S3 extract framework.

Drives the toolkit and OWNS:

  - YAML reading                  read_yaml()  reads & parses the file.
  - YAML schema validation        validate_config()  checks the schema.
  - LoggerClient construction     make_logger() builds `lc`, the single
                                  LoggerClient instance that the entire
                                  pipeline logs through. Handlers are
                                  attached to the root logger so any
                                  stdlib `logging.getLogger(...)` call
                                  (boto3, oracledb, our own bootstrap)
                                  flows into the same files / stream.
  - Class construction            OracleToS3Extract(lc, cfg, debug=...)
                                  is built with the logger AND the
                                  parsed config dict; the class never
                                  reads files itself.

  - sdc_email.py SdcAlertUtil is used to send success and/or failure
    notifications if the YAML config contains a `notifications:` section.
    Notifications are best-effort: a notification failure is logged but
    does not change the program's exit code.

Pipeline:
    Step 1a read_yaml                   -- read & parse YAML, return dict
    Step 1b validate_config             -- schema check (raises ConfigError)
    Step 1c make_logger                 -- build `lc` (LoggerClient)
    Step 1c.5 init_oracle_client_if_requested
                                        -- thick-mode init (no-op for thin)
    Step 1d build job                   -- OracleToS3Extract(lc, cfg, debug)
    Step 2  connect                     -- open Oracle connection
    Step 3  execute_query               -- generator over result batches
    Step 4  write_csv_files             -- chunked CSV files on local disk
    Step 5  upload_to_s3                -- upload (optionally KMS-encrypted)
    Step 6  cleanup_local               -- delete local copies

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

import oracledb
import yaml

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


# --------------------------------------------------------------- yaml reading

def read_yaml(path: str) -> dict:
    """Step 1a: read the YAML config from disk and return a dict.

    The runner -- not the framework class -- owns file I/O. Raises
    ConfigError on missing file, invalid YAML, or a non-mapping root.
    """
    log = logging.getLogger("runner")
    log.debug("read_yaml: opening %s", path)
    try:
        file_size = os.path.getsize(path)
    except OSError:
        file_size = -1
    try:
        with open(path, "r") as fh:
            cfg = yaml.safe_load(fh)
    except FileNotFoundError as e:
        log.error("Config file not found: %s", path)
        raise ConfigError(f"Config file not found: {path}") from e
    except yaml.YAMLError as e:
        log.error("YAML parse failure in %s: %s", path, e)
        raise ConfigError(f"Invalid YAML in {path}: {e}") from e

    if not isinstance(cfg, dict):
        raise ConfigError(f"Config at {path} did not parse to a mapping.")

    log.debug(
        "read_yaml: parsed %s (%s on disk); top-level keys=%s",
        path,
        f"{file_size} B" if file_size >= 0 else "?",
        sorted(cfg.keys()),
    )
    log.info("Config file read completed: %s", path)
    return cfg


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

    # Thick-mode keys are optional; only type-check what's present.
    if "thick_mode" in oracle_cfg and not isinstance(
        oracle_cfg["thick_mode"], bool
    ):
        raise ConfigError(
            f"oracle.thick_mode must be a boolean, got "
            f"{oracle_cfg['thick_mode']!r}"
        )
    for k in ("client_lib_dir", "client_config_dir"):
        if k in oracle_cfg and not isinstance(oracle_cfg[k], str):
            raise ConfigError(
                f"oracle.{k} must be a string path, got "
                f"{type(oracle_cfg[k]).__name__}"
            )

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

def make_logger(
    config: dict, *, verbose: bool = False, debug: bool = False
) -> LoggerClient:
    """Step 1c: build the LoggerClient ('lc') used by the rest of the run.

    Always returns a LoggerClient -- this is the single place where the
    runner instantiates ``logger.py``. If the YAML has a ``logging:``
    section, it drives project/process names, root_directory and output
    mode; otherwise we default to STDOUT only so the class still gets a
    real ``lc`` object.

    `lc.attach_to_root()` is called so that any module using stdlib
    ``logging.getLogger(...)`` -- boto3, oracledb, our own bootstrap
    code -- flows through the same handlers.

    Expected YAML:
        logging:
          project_name:    oracle_framework
          process_name:    big_table_extract
          root_directory:  /var/log/oracle_framework
          output:          BOTH        # STDOUT | LOGFILE | BOTH
    """
    log_cfg = config.get("logging") or {}

    # Default output: STDOUT if no logging: section, BOTH otherwise.
    default_output = "BOTH" if log_cfg else "STDOUT"
    output = str(log_cfg.get("output", default_output)).upper()

    # If files were requested but no root_directory supplied, gracefully
    # fall back to STDOUT instead of raising -- the runner is still
    # responsible for producing logs even when YAML is light.
    if output != "STDOUT" and not log_cfg.get("root_directory"):
        logging.getLogger("runner").warning(
            "logging.output=%s requested but no root_directory set; "
            "falling back to STDOUT only.", output,
        )
        output = "STDOUT"

    lc = LoggerClient(
        project_name=log_cfg.get("project_name", "oracle_framework"),
        process_name=log_cfg.get("process_name", "extract"),
        root_directory=log_cfg.get("root_directory"),
        logoutput=output,
        debug=debug,
    )
    lc.attach_to_root()
    if verbose or debug:
        logging.getLogger().setLevel(logging.DEBUG)

    lc.logfile.info(
        "LoggerClient initialized; output=%s, debug=%s",
        lc.logoutput, debug,
    )
    if lc.log_file:
        lc.logfile.info(
            "Log files: %s.log / %s.error / %s.critical%s",
            lc.log_file, lc.log_file, lc.log_file,
            f" / {lc.log_file}.debug" if debug else "",
        )
    return lc


# ----------------------------------------------------- oracle client (thick) -

def init_oracle_client_if_requested(cfg: dict, lc: LoggerClient) -> None:
    """Step 1c.5: optionally enable Oracle thick mode.

    Called by the runner between `make_logger` and constructing the
    framework class. Must run BEFORE any ``oracledb.connect()`` because
    oracledb cannot switch modes after the first connection is opened.

    YAML knobs (all under the ``oracle:`` section, all optional):

      oracle:
        thick_mode:        true                # default: false (thin)
        client_lib_dir:    /opt/oracle/instantclient_21_12   # optional
        client_config_dir: /etc/oracle          # optional, tnsnames/wallets

    Behaviour:
      * ``thick_mode`` false / missing      -> stay in thin mode, no-op.
      * ``thick_mode`` true, already thick  -> no-op (idempotent).
      * ``thick_mode`` true, currently thin -> call
        ``oracledb.init_oracle_client(lib_dir=..., config_dir=...)``
        once. ``lib_dir`` may be omitted, in which case oracledb falls
        back to the OS library search path (LD_LIBRARY_PATH / PATH /
        DYLD_LIBRARY_PATH / Oracle Instant Client installer registry).

    Errors are surfaced as OracleConnectionError so the runner's main
    exception block routes them through the same handler that catches
    the later ``connect()`` failures (clear log, on_failure email,
    EXIT_ORACLE_CONNECT exit code).
    """
    log = lc.logfile
    oracle_cfg = cfg.get("oracle") or {}
    if not bool(oracle_cfg.get("thick_mode", False)):
        log.debug("oracle.thick_mode not requested; staying in thin mode.")
        return

    # is_thin_mode() returns False once thick mode is active. Guard
    # against duplicate init -- oracledb raises DPI-1014 otherwise.
    try:
        already_thick = not oracledb.is_thin_mode()
    except Exception:  # noqa: BLE001
        already_thick = False
    if already_thick:
        log.info(
            "oracledb already in thick mode (client_version=%s); "
            "skipping init_oracle_client().",
            getattr(oracledb, "clientversion", lambda: "?")(),
        )
        return

    lib_dir = oracle_cfg.get("client_lib_dir")
    config_dir = oracle_cfg.get("client_config_dir")

    log.info(
        "Initializing Oracle Client for thick mode (lib_dir=%s, config_dir=%s)",
        lib_dir or "<system path>",
        config_dir or "<system path>",
    )

    kwargs: dict = {}
    if lib_dir:
        kwargs["lib_dir"] = lib_dir
    if config_dir:
        kwargs["config_dir"] = config_dir

    try:
        oracledb.init_oracle_client(**kwargs)
    except oracledb.Error as e:
        log.error("oracledb.init_oracle_client() failed: %s", e)
        raise OracleConnectionError(
            f"Failed to initialize Oracle Client (thick mode). "
            f"Check that Instant Client is installed and lib_dir is "
            f"correct: {e}"
        ) from e

    # Log the resolved client version so post-mortems can see which
    # Instant Client was actually picked up.
    try:
        client_version = oracledb.clientversion()
    except Exception:  # noqa: BLE001
        client_version = "<unknown>"
    log.info(
        "Oracle Client initialized successfully "
        "(thin_mode=%s, client_version=%s)",
        oracledb.is_thin_mode(), client_version,
    )


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

def _log_file_for_attachment(lc: Optional[LoggerClient]) -> Optional[str]:
    """Return the .log file path for email attachment, or None."""
    if lc is None or not lc.log_file:
        return None
    return f"{lc.log_file}.log"


def _handle_error(
    error: BaseException,
    exit_code: int,
    label: str,
    config: Optional[dict],
    lc: Optional[LoggerClient],
    verbose: bool = False,
    debug: bool = False,
) -> int:
    # If lc is up, log through it; otherwise fall back to the bootstrap
    # stdlib runner logger.
    log = lc.logfile if lc is not None else logging.getLogger("runner")
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
        log_file_path=_log_file_for_attachment(lc),
    )
    return exit_code


def main() -> int:
    args = parse_args()
    # Bootstrap stdlib logging only for the few lines that run BEFORE the
    # LoggerClient is built (read_yaml / validate_config). After lc is
    # created, lc.attach_to_root() takes over and any stdlib log call
    # flows through LoggerClient handlers.
    setup_basic_logging(args.verbose, debug=args.debug)
    log = logging.getLogger("runner")

    if args.debug:
        log.debug("DEBUG mode enabled via --debug")
        _log_runtime_environment(log)

    overall_start = time.monotonic()
    config: Optional[dict] = None
    lc: Optional[LoggerClient] = None

    # ============================ MAIN EXCEPTION BLOCK ===========================
    try:
        # Step 1a: read the YAML file (runner-owned).
        log.info("Step 1/6: reading YAML config from %s ...", args.config)
        config = read_yaml(args.config)
        log.info("Loaded config sections: %s", sorted(config.keys()))

        # Step 1b: schema validation (runner-owned).
        log.info("Step 1/6: validating config schema ...")
        validate_config(config)
        log.info("Config validation passed.")

        # Step 1c: build the LoggerClient. The YAML's `debug: true` is
        # honoured here too, so a config-only debug request reaches lc.
        debug_on = bool(args.debug) or bool(config.get("debug", False))
        lc = make_logger(config, verbose=args.verbose, debug=debug_on)
        log = lc.logfile  # everything from here on logs through lc

        # Step 1c.5: if YAML requests thick mode, init Oracle Client now
        # -- BEFORE any oracledb.connect(). Safe no-op otherwise.
        init_oracle_client_if_requested(config, lc)

        # Step 1d: build the framework class with the logger and the
        # already-validated config dict.
        job = OracleToS3Extract(lc, config, debug=debug_on)

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
            log_file_path=_log_file_for_attachment(lc),
        )
        return EXIT_OK

    except ConfigError as e:
        return _handle_error(
            e, EXIT_CONFIG, "Configuration error",
            config, lc, args.verbose, args.debug,
        )
    except SecretsManagerError as e:
        return _handle_error(
            e, EXIT_SECRETS, "AWS Secrets Manager error",
            config, lc, args.verbose, args.debug,
        )
    except OracleConnectionError as e:
        return _handle_error(
            e, EXIT_ORACLE_CONNECT, "Oracle connection error",
            config, lc, args.verbose, args.debug,
        )
    except OracleQueryError as e:
        return _handle_error(
            e, EXIT_ORACLE_QUERY, "Oracle query error",
            config, lc, args.verbose, args.debug,
        )
    except CSVWriteError as e:
        return _handle_error(
            e, EXIT_CSV, "CSV write error",
            config, lc, args.verbose, args.debug,
        )
    except S3UploadError as e:
        return _handle_error(
            e, EXIT_S3, "S3 upload error",
            config, lc, args.verbose, args.debug,
        )
    except OracleToS3ExtractError as e:
        # Fallback for any new framework exception subclass.
        return _handle_error(
            e, EXIT_GENERIC, "Extract failed",
            config, lc, args.verbose, args.debug,
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
            log_file_path=_log_file_for_attachment(lc),
        )
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    sys.exit(main())
