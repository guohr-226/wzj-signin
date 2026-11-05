import os
import queue
import smtplib
import threading
import time
import tomllib
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path

from wzj_signin.logger import log


def _load_email_config() -> dict:
    """Load shared mail settings and local credentials, with environment overrides."""
    file_cfg = {}
    config_path = Path.cwd() / "config.toml"
    secrets_cfg = {}
    secrets_path = Path.cwd() / "config.secrets.toml"
    try:
        if config_path.exists():
            with open(config_path, "rb") as f:
                file_cfg = tomllib.load(f).get("email", {})
            if not isinstance(file_cfg, dict):
                file_cfg = {}
                log.warning("Email configuration section is invalid")
        if secrets_path.exists():
            with open(secrets_path, "rb") as f:
                secrets_cfg = tomllib.load(f).get("email", {})
            if not isinstance(secrets_cfg, dict):
                secrets_cfg = {}
                log.warning("Email credentials section is invalid")
    except (OSError, tomllib.TOMLDecodeError):
        log.warning("Email configuration file could not be loaded")

    enabled = os.environ.get("ENABLE_SEND_EMAIL")
    return {
        "enable": (
            enabled.strip().lower() in ("1", "true")
            if enabled is not None
            else file_cfg.get("enable_send_email", False)
        ),
        "smtp_server": os.environ.get("SMTP_SERVER")
        or file_cfg.get("smtp_server", "smtp.qq.com"),
        "sender": os.environ.get("EMAIL_SENDER") or secrets_cfg.get("sender", ""),
        "password": os.environ.get("EMAIL_PASSWORD") or secrets_cfg.get("password", ""),
        "receiver": os.environ.get("EMAIL_RECEIVER") or secrets_cfg.get("receiver", ""),
    }


cfg = _load_email_config()
_queue = queue.Queue(maxsize=32)
_worker_lock = threading.Lock()
_worker_thread = None


def _deliver_email(subject: str, content: str) -> None:
    """Deliver one queued message, keeping SMTP details out of logs."""
    server = None
    started = time.monotonic()
    phase = "prepare"
    phase_started = started
    try:
        message = MIMEText(content, "plain", "utf-8")
        message["From"] = cfg["sender"]
        message["To"] = cfg["receiver"]
        message["Subject"] = Header("[wzj-signin] " + subject, "utf-8")

        phase = "connect"
        phase_started = time.monotonic()
        log.info("Email SMTP connecting")
        server = smtplib.SMTP_SSL(cfg["smtp_server"], 465, timeout=10)
        log.info("Email SMTP connect completed in %.3fs", time.monotonic() - phase_started)

        phase = "auth"
        phase_started = time.monotonic()
        log.info("Email SMTP authenticating")
        server.login(cfg["sender"], cfg["password"])
        log.info("Email SMTP auth completed in %.3fs", time.monotonic() - phase_started)

        phase = "send"
        phase_started = time.monotonic()
        log.info("Email SMTP sending")
        server.sendmail(cfg["sender"], [cfg["receiver"]], message.as_string())
        log.info("Email SMTP send completed in %.3fs", time.monotonic() - phase_started)
        log.info("Email delivery succeeded (SMTP accepted) in %.3fs", time.monotonic() - started)
    except Exception:
        log.error(
            "Email delivery failed during %s after %.3fs (total %.3fs)",
            phase,
            time.monotonic() - phase_started,
            time.monotonic() - started,
        )
    finally:
        if server is not None:
            cleanup_started = time.monotonic()
            try:
                server.quit()
            except Exception:
                log.warning("Email SMTP cleanup failed after %.3fs", time.monotonic() - cleanup_started)
            finally:
                try:
                    server.close()
                except Exception:
                    log.warning("Email SMTP close failed after %.3fs", time.monotonic() - cleanup_started)


def _worker() -> None:
    while True:
        item = _queue.get()
        try:
            # A private sentinel lets isolated tests stop their own worker.
            if item is None:
                return
            started = time.monotonic()
            try:
                _deliver_email(*item)
            except Exception:
                log.error("Email worker delivery failed after %.3fs", time.monotonic() - started)
        finally:
            _queue.task_done()


def _start_worker() -> None:
    global _worker_thread
    with _worker_lock:
        if _worker_thread is None:
            worker = threading.Thread(target=_worker, name="email-worker", daemon=True)
            worker.start()
            _worker_thread = worker


def send_email(subject: str, content: str) -> bool:
    """Return True only when queued, not when SMTP delivery has completed.

    The daemon worker does not delay process exit; pending mail may be dropped.
    """
    if not cfg["enable"]:
        return False
    if not all(cfg.get(key) for key in ("smtp_server", "sender", "password", "receiver")):
        log.warning("Email notification enabled but configuration is incomplete")
        return False

    try:
        _start_worker()
        _queue.put_nowait((subject, content))
    except queue.Full:
        log.warning("Email queue full; notification not queued")
        return False
    except RuntimeError:
        log.warning("Email worker could not be started")
        return False
    log.info("Email notification queued; delivery pending")
    return True
