from dataclasses import dataclass, field
import re
from urllib.parse import parse_qs, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry


DEFAULT_API_BASE_URL = "https://v18.teachermate.cn"
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 15.0
DEFAULT_REQUEST_RETRIES = 2
DEFAULT_RETRY_BACKOFF = 1.0
ACTIVE_SIGNS_PATH = "/wechat-api/v1/class-attendance/student/active_signs"
SIGN_IN_PATH = "/wechat-api/v1/class-attendance/student-sign-in"


class OpenIDExpired(requests.RequestException):
    """The API explicitly rejected the current OpenID or login."""


class APIResponseError(requests.RequestException):
    """The API returned an unexpected response."""


_EXPIRED_LOGIN_MESSAGE = re.compile(
    r"\bopen[\s_-]*id\b\s*(?:(?:is|has|was)\s+)?"
    r"(?:invalid|expired|(?:not|no longer)\s+valid)\b"
    r"|\b(?:invalid|expired)\s+open[\s_-]*id\b"
    r"|\b(?:login|log[\s-]*in)\s+(?:(?:is|has)\s+)?(?:invalid|expired)\b"
    r"|\b(?:please\s+)?(?:log\s*in|login|sign\s*in)\s+again\b"
    r"|\bnot\s+(?:logged|signed)\s+in\b"
    r"|open[\s_-]*id\s*(?:\u5df2)?"
    r"(?:\u5931\u6548|\u8fc7\u671f|\u65e0\u6548|\u4e0d\u5408\u6cd5|\u4e0d\u5b58\u5728)"
    r"|(?:\u5931\u6548|\u8fc7\u671f|\u65e0\u6548|\u4e0d\u5408\u6cd5)"
    r"(?:\u7684)?\s*open[\s_-]*id"
    r"|\u767b\u5f55(?:\u72b6\u6001|\u4fe1\u606f|\u51ed\u8bc1)?(?:\u5df2)?"
    r"(?:\u8fc7\u671f|\u5931\u6548|\u65e0\u6548)"
    r"|\u672a\u767b\u5f55"
    r"|\u8bf7(?:\u5148|\u91cd\u65b0)\u767b\u5f55"
    r"|\u9700\u8981\u91cd\u65b0\u767b\u5f55",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RequestConfig:
    open_id: str = field(repr=False)
    api_base_url: str
    connect_timeout: float
    read_timeout: float
    request_retries: int
    retry_backoff: float

    @property
    def timeout(self) -> tuple[float, float]:
        return (self.connect_timeout, self.read_timeout)

    def api_url(self, path: str) -> str:
        return f"{self.api_base_url.rstrip('/')}/{path.lstrip('/')}"


@dataclass(frozen=True)
class SignTarget:
    course_name: str
    course_id: int
    sign_id: int
    positions: tuple[str, str] | None = None


def build_request_config(
    open_id: str,
    api_base_url: str | None,
    connect_timeout: float,
    read_timeout: float,
    request_retries: int,
    retry_backoff: float,
) -> RequestConfig:
    normalized = _normalize_open_id(open_id)
    extracted_open_id, derived_base_url = _extract_open_id_and_base_url(normalized)
    if not extracted_open_id.strip():
        raise ValueError("OPEN_ID is required")
    selected_base_url = (api_base_url or derived_base_url or DEFAULT_API_BASE_URL).rstrip("/")
    _validate_timeouts(connect_timeout, read_timeout)
    _validate_retry_settings(request_retries, retry_backoff)
    return RequestConfig(
        open_id=extracted_open_id,
        api_base_url=selected_base_url,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        request_retries=request_retries,
        retry_backoff=retry_backoff,
    )


def make_session(user_agent: str, request_config: RequestConfig) -> requests.Session:
    session = requests.Session()
    # Disable environment proxies for the TeacherMate API.
    session.trust_env = False
    session.headers.update({"User-Agent": user_agent, "openId": request_config.open_id})
    adapter = HTTPAdapter(max_retries=_build_retry(request_config))
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def get_active_signs(
    session: requests.Session,
    request_config: RequestConfig,
) -> list[dict]:
    response = session.get(
        request_config.api_url(ACTIVE_SIGNS_PATH),
        timeout=request_config.timeout,
    )
    payload = _response_payload(response)
    if not isinstance(payload, list):
        raise APIResponseError("Active signs response must be a list")
    return payload


def submit_sign_in(
    session: requests.Session,
    request_config: RequestConfig,
    sign_target: SignTarget,
) -> dict:
    data = {"courseId": sign_target.course_id, "signId": sign_target.sign_id}
    if sign_target.positions:
        data["lon"] = sign_target.positions[0]
        data["lat"] = sign_target.positions[1]

    response = session.post(
        request_config.api_url(SIGN_IN_PATH),
        data=data,
        timeout=request_config.timeout,
    )
    payload = _response_payload(response)
    if not isinstance(payload, dict):
        raise APIResponseError("Sign-in response must be an object")
    return payload


def _response_payload(response: requests.Response):
    if response.status_code == 401:
        raise OpenIDExpired("OpenID is invalid or expired")
    if response.status_code >= 500:
        response.raise_for_status()
    try:
        payload = response.json()
    except ValueError:
        response.raise_for_status()
        raise APIResponseError("API returned invalid JSON") from None
    if _has_expired_login_message(payload):
        raise OpenIDExpired("OpenID is invalid or expired")
    response.raise_for_status()
    return payload


def _has_expired_login_message(payload) -> bool:
    if not isinstance(payload, dict):
        return False
    for field_name in ("msgClient", "msg", "message"):
        message = payload.get(field_name)
        if isinstance(message, str) and _EXPIRED_LOGIN_MESSAGE.search(message):
            return True
    error = payload.get("error")
    if isinstance(error, str):
        return bool(_EXPIRED_LOGIN_MESSAGE.search(error))
    return _has_expired_login_message(error)


def _build_retry(request_config: RequestConfig) -> Retry:
    return Retry(
        total=None,
        connect=request_config.request_retries,
        read=0,
        status=0,
        other=0,
        allowed_methods=frozenset({"GET", "POST"}),
        backoff_factor=request_config.retry_backoff,
        raise_on_status=False,
    )


def _extract_open_id_and_base_url(open_id: str) -> tuple[str, str | None]:
    if not open_id.startswith(("http://", "https://")):
        return open_id, None

    parsed = urlsplit(open_id)
    open_id_values = parse_qs(parsed.query).get("openid")
    if not open_id_values:
        raise ValueError("OPEN_ID URL missing openid parameter")
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    return open_id_values[0], base_url


def _normalize_open_id(open_id: str) -> str:
    return open_id.replace(r"\?", "?").replace(r"\=", "=").replace(r"\&", "&").strip()


def _validate_timeouts(connect_timeout: float, read_timeout: float) -> None:
    if connect_timeout <= 0:
        raise ValueError("CONNECT_TIMEOUT must be greater than 0")
    if read_timeout <= 0:
        raise ValueError("READ_TIMEOUT must be greater than 0")


def _validate_retry_settings(request_retries: int, retry_backoff: float) -> None:
    if request_retries < 0:
        raise ValueError("REQUEST_RETRIES cannot be negative")
    if retry_backoff < 0:
        raise ValueError("RETRY_BACKOFF cannot be negative")
