import os
import sys
import tomllib
import random
import time
import threading
from pathlib import Path

import requests

from wzj_signin.email import send_email
from wzj_signin.http_client import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_READ_TIMEOUT,
    DEFAULT_RETRY_BACKOFF,
    DEFAULT_REQUEST_RETRIES,
    RequestConfig,
    SignTarget,
    build_request_config,
    OpenIDExpired,
    get_active_signs,
    make_session,
    submit_sign_in,
)
from wzj_signin.logger import log
from wzj_signin.qrsign import QRSign
from wzj_signin.utils import jitter_position, sleep_with_progress


def _load_config() -> dict:
    """Load shared settings and local credentials, preferring the saved OpenID."""
    file_cfg = {}
    locations = []
    config_path = Path.cwd() / "config.toml"
    if config_path.exists():
        with open(config_path, "rb") as f:
            document = tomllib.load(f)
            file_cfg = document.get("default", {})
            locations = document.get("locations", [])

    secrets_cfg = {}
    secrets_path = Path.cwd() / "config.secrets.toml"
    if secrets_path.exists():
        with open(secrets_path, "rb") as f:
            secrets_cfg = tomllib.load(f).get("default", {})

    open_id = secrets_cfg.get("openId") or os.environ.get("OPEN_ID")
    course_id = os.environ.get("COURSE_ID") or secrets_cfg.get("courseId")
    student_id = os.environ.get("STUDENT_ID") or secrets_cfg.get("studentId", "")
    poll_interval = _parse_int(
        os.environ.get("POLL_INTERVAL") or file_cfg.get("pollInterval", 10),
        "POLL_INTERVAL",
    )
    if not course_id:
        log.error("Missing COURSE_ID, set env var or provide config.toml")
        sys.exit(1)

    while True:
        if not open_id:
            open_id = _read_input("Enter OpenID or full TeacherMate URL: ", secret=True)
        try:
            request_config = build_request_config(
                open_id=open_id,
                api_base_url=os.environ.get("API_BASE_URL") or file_cfg.get("apiBaseUrl"),
                connect_timeout=_parse_float(
                    os.environ.get("CONNECT_TIMEOUT")
                    or file_cfg.get("connectTimeout", DEFAULT_CONNECT_TIMEOUT),
                    "CONNECT_TIMEOUT",
                ),
                read_timeout=_parse_float(
                    os.environ.get("READ_TIMEOUT")
                    or file_cfg.get("readTimeout", DEFAULT_READ_TIMEOUT),
                    "READ_TIMEOUT",
                ),
                request_retries=_parse_int(
                    os.environ.get("REQUEST_RETRIES")
                    or file_cfg.get("requestRetries", DEFAULT_REQUEST_RETRIES),
                    "REQUEST_RETRIES",
                ),
                retry_backoff=_parse_float(
                    os.environ.get("RETRY_BACKOFF")
                    or file_cfg.get("retryBackoff", DEFAULT_RETRY_BACKOFF),
                    "RETRY_BACKOFF",
                ),
            )
            break
        except ValueError as exc:
            if "OPEN_ID" not in str(exc):
                raise SystemExit(str(exc)) from None
            log.warning("Invalid OpenID format; enter an OpenID or full URL")
            open_id = None

    return {
        "courseId": course_id,
        "studentId": student_id,
        "pollInterval": poll_interval,
        "locations": locations,
        "apiBaseUrl": os.environ.get("API_BASE_URL") or file_cfg.get("apiBaseUrl"),
        "requestConfig": request_config,
    }


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)

signed_ids: set[int] = set()
_qr_tasks: dict[int, tuple[QRSign, threading.Thread]] = {}
_qr_tasks_lock = threading.Lock()


def _parse_int(value: str | int, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _parse_float(value: str | float, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc


def _read_input(prompt: str, *, secret: bool = False) -> str:
    if not sys.stdin.isatty():
        raise SystemExit("Interactive terminal required for OpenID input and location selection")
    try:
        if secret:
            from getpass import getpass

            return getpass(prompt).strip()
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("Interactive input cancelled") from None


def _renew_open_id(
    session: requests.Session,
    request_config: RequestConfig,
    api_base_url: str | None,
) -> RequestConfig:
    while True:
        raw = _read_input("Enter new OpenID or full TeacherMate URL (not saved): ", secret=True)
        try:
            updated = build_request_config(
                open_id=raw,
                api_base_url=api_base_url or (
                    None if raw.startswith(("http://", "https://")) else request_config.api_base_url
                ),
                connect_timeout=request_config.connect_timeout,
                read_timeout=request_config.read_timeout,
                request_retries=request_config.request_retries,
                retry_backoff=request_config.retry_backoff,
            )
        except ValueError:
            log.warning("Invalid OpenID format; please try again")
            continue
        session.headers["openId"] = updated.open_id
        return updated


def _validate_open_id(
    session: requests.Session,
    request_config: RequestConfig,
    poll_interval: int,
    api_base_url: str | None,
    *,
    renew: bool = False,
) -> RequestConfig:
    if renew:
        request_config = _renew_open_id(session, request_config, api_base_url)
    while True:
        try:
            get_active_signs(session, request_config)
            log.info("OpenID verified")
            return request_config
        except OpenIDExpired:
            log.warning("OpenID expired; a new OpenID is required")
            request_config = _renew_open_id(session, request_config, api_base_url)
        except requests.RequestException as exc:
            _log_request_error("OpenID check failed; keeping credentials and retrying", exc)
            sleep_with_progress(poll_interval)


def _select_location(locations: list[dict]) -> tuple[str, str]:
    if not locations:
        raise SystemExit("No locations configured; add [[locations]] entries to config.toml")
    if not sys.stdin.isatty():
        raise SystemExit("Interactive terminal required to select a GPS location")
    for number, location in enumerate(locations, 1):
        print(f"{number}. {location['name']}")
    while True:
        choice = _read_input("Select GPS location number: ")
        try:
            index = int(choice) - 1
        except ValueError:
            index = -1
        if not 0 <= index < len(locations):
            print("Invalid location number; please try again")
            continue
        location = locations[index]
        log.info("Selected GPS location: %s", location["name"])
        return jitter_position(str(location["lon"]), str(location["lat"]))


def _log_request_error(message: str, exc: requests.RequestException) -> None:
    status = exc.response.status_code if exc.response is not None else None
    log.error("%s (%s, HTTP status=%s)", message, type(exc).__name__, status)


def sign_in_one(
    session: requests.Session,
    request_config: RequestConfig,
    sign_target: SignTarget,
) -> bool:
    # 普通签到 / GPS 签到：随机等待 8–10 秒
    delay = random.uniform(8.0, 10.0)
    log.info(
        "Sign-in delay: %.2fs (signId=%s)",
        delay,
        sign_target.sign_id,
    )
    time.sleep(delay)

    result = submit_sign_in(session, request_config, sign_target)
    if "errorCode" in result:
        if result.get("errorCode") == 305:
            log.info(
                "%s already signed signId=%s",
                sign_target.course_name,
                sign_target.sign_id,
            )
            return True
        log.warning(
            "%s sign-in failed signId=%s errorCode=%s",
            sign_target.course_name,
            sign_target.sign_id,
            result.get("errorCode"),
        )
        return False
    log.info(
        "%s sign-in success signId=%s rank:%s",
        sign_target.course_name,
        sign_target.sign_id,
        result.get("studentRank"),
    )
    return True


def _start_qr_task(course_id, sign_id: int, course_name: str, student_id: str) -> None:
    def run():
        try:
            if qr.start():
                with _qr_tasks_lock:
                    signed_ids.add(sign_id)
                send_email(
                    "Sign-in Success", f"{course_name} QR sign-in success signId={sign_id}"
                )
        except Exception as err:
            log.error("QR task failed signId=%s (%s)", sign_id, type(err).__name__)
        finally:
            qr.stop()
            with _qr_tasks_lock:
                _qr_tasks.pop(sign_id, None)

    with _qr_tasks_lock:
        if sign_id in signed_ids or sign_id in _qr_tasks:
            return
        qr = QRSign(str(course_id), sign_id, student_id)
        worker = threading.Thread(target=run, name=f"qr-sign-{sign_id}", daemon=True)
        _qr_tasks[sign_id] = (qr, worker)
    try:
        log.info("QR sign-in found: %s (signId=%s)", course_name, sign_id)
        send_email(
            "QR Sign-in Found",
            f"{course_name} QR sign-in signId={sign_id}, please scan the QR code ASAP...",
        )
        worker.start()
    except Exception as err:
        qr.stop()
        with _qr_tasks_lock:
            _qr_tasks.pop(sign_id, None)
        log.error("QR task could not start signId=%s (%s)", sign_id, type(err).__name__)


def _stop_qr_tasks() -> None:
    with _qr_tasks_lock:
        tasks = list(_qr_tasks.values())
    for qr, _ in tasks:
        qr.stop()
    for _, worker in tasks:
        if worker.ident is not None:
            worker.join(timeout=2)


def process_signs(
    session: requests.Session,
    request_config: RequestConfig,
    signs: list[dict],
    student_id: str,
    positions: tuple[str, str] | None,
) -> None:
    for sign in signs:
        sign_id = sign["signId"]
        course_id = sign["courseId"]
        course_name = sign.get("name", "")
        name = sign.get("name", "")

        with _qr_tasks_lock:
            if sign_id in signed_ids:
                continue

        if sign.get("isGPS") == 1:
            log.info("GPS sign-in found: %s (signId=%s)", name, sign_id)
            if not positions:
                log.warning("GPS coordinates not configured, cannot sign in")
                send_email(
                    "GPS Sign-in Failed",
                    f"{course_name} GPS sign-in signId={sign_id}, but GPS coordinates not configured",
                )
                signed_ids.add(sign_id)

            elif sign_in_one(
                session,
                request_config,
                SignTarget(course_name, course_id, sign_id, positions),
            ):
                send_email(
                    "Sign-in Success", f"{course_name} GPS sign-in success signId={sign_id}"
                )
                signed_ids.add(sign_id)

            continue

        if sign.get("isQR") == 1:
            _start_qr_task(course_id, sign_id, course_name, student_id)
            continue

        log.info("Normal sign-in found: %s (signId=%s)", name, sign_id)
        if sign_in_one(
            session,
            request_config,
            SignTarget(course_name, course_id, sign_id),
        ):
            signed_ids.add(sign_id)
            send_email("Sign-in Success", f"{course_name} normal sign-in success signId={sign_id}")


def main() -> None:
    config = _load_config()
    request_config = config["requestConfig"]
    session = make_session(USER_AGENT, request_config)
    student_id = config["studentId"]
    poll_interval = config["pollInterval"]
    request_config = _validate_open_id(
        session, request_config, poll_interval, config["apiBaseUrl"]
    )
    positions = _select_location(config["locations"])

    log.info(
        "Sign-in monitor started, polling every %d seconds (connect timeout %.1fs, read timeout %.1fs, retries %d)",
        poll_interval,
        request_config.connect_timeout,
        request_config.read_timeout,
        request_config.request_retries,
    )
    consecutive_errors = 0

    try:
        while True:
            try:
                signs = get_active_signs(session, request_config)
                consecutive_errors = 0
                log.info("Poll succeeded, %d active sign(s)", len(signs))
                if signs:
                    process_signs(session, request_config, signs, student_id, positions)
            except OpenIDExpired:
                log.warning("OpenID expired; pausing monitoring for renewal")
                send_email("Monitor Error", "OpenID expired; terminal input is required")
                request_config = _validate_open_id(
                    session, request_config, poll_interval, config["apiBaseUrl"], renew=True
                )
                consecutive_errors = 0
            except requests.RequestException as exc:
                consecutive_errors += 1
                _log_request_error(f"Request failed (consecutive #{consecutive_errors})", exc)
            except Exception as exc:
                log.error("Unexpected monitor error (%s)", type(exc).__name__)
            sleep_with_progress(poll_interval)
    finally:
        _stop_qr_tasks()
