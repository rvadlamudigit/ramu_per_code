"""
logger.py — Logger utility for the oracle_framework.

Originally authored by Vivek Gautam (2018). Cleaned up and integrated
with the oracle_framework runner. Fixes in this revision:

  - Replaced check-then-makedirs with `os.makedirs(..., exist_ok=True)`
  - Always sets propagate=False to avoid double logging
  - Clears existing handlers on the named logger so re-instantiation
    with the same process_name does not stack duplicates
  - File handlers now use encoding='utf-8'
  - `.warn()` -> `.warning()` in the __main__ demo
  - Added attach_to_root() so other modules' loggers (e.g. the framework's
    `logging.getLogger("oracle_to_s3")`) flow into the same handlers
  - Added safe defaults when root_directory is None and logoutput=='STDOUT'

Backward-compatible API: `LoggerClient(...).logfile` is still the
`logging.Logger` you call .info / .error / etc. on.
"""

from __future__ import annotations

import datetime
import logging
import os
from typing import Optional


class LoggerClient(object):
    """Configurable logger that can write to stdout, file, or both.

    Parameters
    ----------
    project_name : str
        Folder name under root_directory.
    process_name : str
        Subfolder + log filename prefix; also the underlying logger name.
    root_directory : str | None
        Top-level log directory. Required when logoutput != 'STDOUT'.
    logoutput : {'STDOUT', 'LOGFILE', 'BOTH'}
        Output sinks. Defaults to 'BOTH'.
    errorfiledel : str
        Field delimiter used in the .error file format.
    start_time_yyyymmddhh24miss : str | None
        Override the run timestamp (otherwise generated from now()).
    """

    VALID_OUTPUTS = ("STDOUT", "LOGFILE", "BOTH")

    def __init__(
        self,
        project_name: str,
        process_name: str,
        root_directory: Optional[str] = None,
        logoutput: str = "BOTH",
        errorfiledel: str = "~",
        start_time_yyyymmddhh24miss: Optional[str] = None,
        debug: bool = False,
    ):
        if logoutput not in self.VALID_OUTPUTS:
            raise ValueError(
                f"logoutput must be one of {self.VALID_OUTPUTS}, got {logoutput!r}"
            )
        if logoutput != "STDOUT" and not root_directory:
            raise ValueError(
                "root_directory is required when logoutput != 'STDOUT'"
            )

        self.project_name = project_name
        self.process_name = process_name
        self.logoutput = logoutput
        self.errorfiledel = errorfiledel
        self.debug = bool(debug)

        if start_time_yyyymmddhh24miss is None:
            self.start_time = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            subdir = process_name
        else:
            self.start_time = start_time_yyyymmddhh24miss
            subdir = self.start_time[:8]

        self.logfiledir: Optional[str] = None
        self.log_file: Optional[str] = None
        if logoutput != "STDOUT":
            self.logfiledir = os.path.join(root_directory, project_name, "logfiles", subdir)
            os.makedirs(self.logfiledir, exist_ok=True)
            self.log_file = os.path.join(
                self.logfiledir, f"{process_name}_{self.start_time}"
            )

        self.logfile: logging.Logger = self._build_logger()

    # ----------------------------------------------------------------- build

    def _build_logger(self) -> logging.Logger:
        log = logging.getLogger(self.process_name)
        # Avoid stacking duplicate handlers on repeat instantiations
        for h in list(log.handlers):
            log.removeHandler(h)
            try:
                h.close()
            except Exception:  # noqa: BLE001
                pass
        log.propagate = False
        # The logger itself stays open at DEBUG so individual handlers
        # decide what to emit. The handler-level filter (below) controls
        # what actually shows up.
        log.setLevel(logging.DEBUG)
        # Effective handler level: DEBUG only when debug=True, else INFO.
        handler_level = logging.DEBUG if self.debug else logging.INFO

        # Standard formatter (production-friendly, single-line).
        log_formatter = logging.Formatter(
            f"%(asctime)s.%(msecs)03d :{self.process_name.upper()}: "
            "%(levelname)-8s ~%(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        # In debug mode the formatter also carries module / line / thread so
        # we can map a log line straight back to source code.
        debug_formatter = logging.Formatter(
            f"%(asctime)s.%(msecs)03d :{self.process_name.upper()}: "
            "%(levelname)-8s [%(name)s %(filename)s:%(lineno)d %(threadName)s] "
            "~%(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        active_formatter = debug_formatter if self.debug else log_formatter

        error_formatter = logging.Formatter(
            f"%(asctime)s{self.errorfiledel}{self.process_name.upper()}"
            f"{self.errorfiledel}%(levelname)-8s{self.errorfiledel}%(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        # Stream handler (stdout)
        if self.logoutput in ("STDOUT", "BOTH"):
            sh = logging.StreamHandler()
            sh.setFormatter(active_formatter)
            sh.setLevel(handler_level)
            log.addHandler(sh)

        if self.logoutput == "STDOUT":
            return log

        # File handlers (LOGFILE or BOTH)
        fh_main = logging.FileHandler(
            f"{self.log_file}.log", mode="w", encoding="utf-8"
        )
        fh_main.setFormatter(active_formatter)
        fh_main.setLevel(handler_level)
        log.addHandler(fh_main)

        fh_error = logging.FileHandler(
            f"{self.log_file}.error", mode="w", encoding="utf-8"
        )
        fh_error.setFormatter(error_formatter)
        fh_error.setLevel(logging.ERROR)
        log.addHandler(fh_error)

        fh_critical = logging.FileHandler(
            f"{self.log_file}.critical", mode="w", encoding="utf-8"
        )
        fh_critical.setFormatter(active_formatter)
        fh_critical.setLevel(logging.CRITICAL)
        log.addHandler(fh_critical)
        # NOTE: .critical is intentionally a CRITICAL-only sink so it is
        # always small. Independent of the debug flag.

        # Debug-only firehose file. Keeps DEBUG noise out of the main .log
        # for consumers that already pipe .log to ops dashboards.
        if self.debug:
            fh_debug = logging.FileHandler(
                f"{self.log_file}.debug", mode="w", encoding="utf-8"
            )
            fh_debug.setFormatter(debug_formatter)
            fh_debug.setLevel(logging.DEBUG)
            log.addHandler(fh_debug)

        return log

    # ----------------------------------------------------------- integration

    def attach_to_root(self) -> None:
        """Attach this logger's handlers to the root logger.

        Call this once after construction so other modules using
        `logging.getLogger(__name__)` (for example the oracle_to_s3
        framework) flow their messages into the same files / stream.
        """
        root = logging.getLogger()
        # Remove pre-existing handlers from basicConfig if any, so we
        # don't double-log on stdout.
        for h in list(root.handlers):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:  # noqa: BLE001
                pass
        for h in self.logfile.handlers:
            root.addHandler(h)
        # The root logger filters at the same level as the LoggerClient's
        # handlers so non-debug runs aren't flooded with DEBUG noise from
        # the framework or third-party modules.
        root.setLevel(logging.DEBUG if self.debug else logging.INFO)

    def close(self) -> None:
        """Close all handlers attached to this logger."""
        for h in list(self.logfile.handlers):
            try:
                h.close()
            except Exception:  # noqa: BLE001
                pass
            self.logfile.removeHandler(h)


if __name__ == "__main__":
    lf = LoggerClient("project", "process", "/tmp")
    lf.logfile.error("error writing")
    lf.logfile.warning("warn writing")
    lf.logfile.info("info writing")
    lf.logfile.debug("debug writing")
    lf.logfile.critical("log writing")
