"""
sdc_email.py — SMTP relay email utility for the oracle_framework.

Originally authored by Vivek Gautam (2017). Cleaned up and integrated
with the oracle_framework runner.

This relay-style helper assumes the SMTP server does NOT require
authentication (typical for internal relays). It supports plain text
or HTML bodies, optional file attachments, retries with exponential
backoff, and optional STARTTLS for transport encryption.

Fixes vs. the original:
  - `s.quit()` is now in `finally` and guarded so it cannot raise
    NameError when SMTP() construction itself failed.
  - Attachments are opened in binary mode and sent as MIME application
    parts (text MIME types are still attached as MIMEText). No more
    text-mode corruption of binary or non-UTF-8 files.
  - Recipient list is whitespace-stripped per address.
  - Retries use exponential backoff (1s, 2s, 4s).
  - `print()` replaced with logging.
  - One consolidated `send_email` method; `send_email_text` and
    `send_email_html` are kept as backwards-compatible wrappers.
  - On full failure raises EmailSendError so callers can react.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import smtplib
import time
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Optional

logger = logging.getLogger(__name__)


class EmailSendError(Exception):
    """Raised when email sending fails on every retry."""


class SdcAlertUtil(object):
    """SMTP email helper supporting plain text or HTML bodies and attachments."""

    DEFAULT_RETRIES = 3
    DEFAULT_TIMEOUT = 30  # seconds

    # ----------------------------------------------------------------- core

    def send_email(
        self,
        smtp_server: str,
        from_addr: str,
        to_addr: str,
        subject: str,
        body: str,
        body_subtype: str = "plain",   # 'plain' or 'html'
        filename: Optional[str] = None,
        port: int = 25,
        use_tls: bool = False,
        retries: int = DEFAULT_RETRIES,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> str:
        """Send an email via an SMTP relay (no authentication).

        Returns a status string on success.
        Raises EmailSendError if all retry attempts fail.
        """
        if body_subtype not in ("plain", "html"):
            raise ValueError(
                f"body_subtype must be 'plain' or 'html', got {body_subtype!r}"
            )

        recipients: List[str] = [
            x.strip() for x in to_addr.split(",") if x.strip()
        ]
        if not recipients:
            raise ValueError("to_addr produced no valid recipient addresses")

        msg = self._build_message(
            from_addr, to_addr, subject, body, body_subtype, filename
        )

        last_error: Optional[BaseException] = None
        for attempt in range(1, retries + 1):
            s: Optional[smtplib.SMTP] = None
            try:
                s = smtplib.SMTP(host=smtp_server, port=port, timeout=timeout)
                if use_tls:
                    s.starttls()
                s.sendmail(from_addr, recipients, msg.as_string())
                logger.info(
                    "Email sent successfully (attempt %d/%d) to %s",
                    attempt, retries, recipients,
                )
                return "Successfully sent email"
            except Exception as e:  # noqa: BLE001
                last_error = e
                logger.warning(
                    "Email attempt %d/%d failed: %s", attempt, retries, e
                )
            finally:
                if s is not None:
                    try:
                        s.quit()
                    except Exception:  # noqa: BLE001
                        pass

            if attempt < retries:
                # Exponential backoff: 1s, 2s, 4s, ...
                sleep_s = 2 ** (attempt - 1)
                time.sleep(sleep_s)

        # All retries failed
        raise EmailSendError(
            f"Failed to send email after {retries} attempt(s): {last_error}"
        )

    # ------------------------------------------------------- message builder

    @staticmethod
    def _build_message(
        from_addr: str,
        to_addr: str,
        subject: str,
        body: str,
        body_subtype: str,
        filename: Optional[str],
    ) -> MIMEMultipart:
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to_addr

        msg.attach(MIMEText(body, body_subtype, "utf-8"))

        if filename and os.path.exists(filename) and os.path.getsize(filename) > 0:
            ctype, _ = mimetypes.guess_type(filename)
            maintype = (ctype or "application/octet-stream").split("/")[0]
            with open(filename, "rb") as fp:
                data = fp.read()
            if maintype == "text":
                attachment = MIMEText(
                    data.decode("utf-8", errors="replace"),
                    _subtype=ctype.split("/")[1] if ctype else "plain",
                    _charset="utf-8",
                )
            else:
                attachment = MIMEApplication(data)
            attachment.add_header(
                "Content-Disposition",
                "attachment",
                filename=os.path.basename(filename),
            )
            msg.attach(attachment)

        return msg

    # --------------------------------------------------- backward-compat API

    def send_email_text(
        self,
        smtp_server: str,
        from_addr: str,
        to_addr: str,
        subject: str,
        text: str,
        filename: Optional[str] = None,
    ) -> str:
        """Backwards-compatible plain-text wrapper.

        Returns a status string. Catches EmailSendError and returns its
        message instead of raising, so existing callers don't break.
        """
        try:
            return self.send_email(
                smtp_server, from_addr, to_addr, subject, text,
                body_subtype="plain", filename=filename,
            )
        except EmailSendError as e:
            return f"Error sending email :{e}"

    def send_email_html(
        self,
        smtp_server: str,
        from_addr: str,
        to_addr: str,
        subject: str,
        text: str,
        filename: Optional[str] = None,
    ) -> str:
        """Backwards-compatible HTML wrapper. See send_email_text."""
        try:
            return self.send_email(
                smtp_server, from_addr, to_addr, subject, text,
                body_subtype="html", filename=filename,
            )
        except EmailSendError as e:
            return f"Error sending email :{e}"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    s = SdcAlertUtil()
    # Example (commented out — fill in real values to actually send):
    # status = s.send_email(
    #     smtp_server="smtp.example.com",
    #     from_addr="noreply@example.com",
    #     to_addr="rvadlamudi@scholastic.com",
    #     subject="Email testing - please ignore",
    #     body="Email testing - please ignore",
    # )
    # print(status)
