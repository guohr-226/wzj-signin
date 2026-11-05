import json
import unittest
from unittest.mock import Mock

import requests

from wzj_signin.http_client import (
    APIResponseError,
    OpenIDExpired,
    SignTarget,
    build_request_config,
    get_active_signs,
    make_session,
    submit_sign_in,
)


def _config(open_id="test-open-id", api_base_url=None):
    return build_request_config(
        open_id=open_id,
        api_base_url=api_base_url,
        connect_timeout=5.0,
        read_timeout=15.0,
        request_retries=2,
        retry_backoff=1.0,
    )


def _response(payload, status=200):
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(payload).encode("utf-8")
    response.url = "https://example.invalid/?openid=private-test-token"
    return response


class HttpClientTests(unittest.TestCase):
    def setUp(self):
        self.config = _config()
        self.session = Mock(spec=requests.Session)
        self.target = SignTarget("test", 101, 202)

    def test_build_request_config_extracts_escaped_openid_url(self):
        config = _config(
            r"https://v18.teachermate.cn/wechat-pro-ssr/\?openid\=abc123\&from\=wzj"
        )

        self.assertEqual(config.open_id, "abc123")
        self.assertEqual(config.api_base_url, "https://v18.teachermate.cn")
        self.assertEqual(config.timeout, (5.0, 15.0))

    def test_explicit_api_base_overrides_url_origin_and_trims_slash(self):
        config = _config(
            "https://original.invalid/path?openid=abc%2B123&other=value",
            "https://override.invalid/",
        )

        self.assertEqual(config.open_id, "abc+123")
        self.assertEqual(config.api_url("/api/path"), "https://override.invalid/api/path")

    def test_build_request_config_requires_openid_query_param(self):
        with self.assertRaisesRegex(ValueError, "missing openid"):
            _config("https://example.invalid/?from=wzj")

    def test_build_request_config_rejects_empty_openid(self):
        for open_id in ("", "  \n", "https://example.invalid/?openid=", "https://example.invalid/?openid=%20"):
            with self.subTest(open_id=open_id), self.assertRaises(ValueError):
                _config(open_id)

    def test_request_config_repr_hides_openid(self):
        self.assertNotIn("private-test-token", repr(_config("private-test-token")))

    def test_session_retries_connections_but_not_reads_or_statuses(self):
        session = make_session("ua-test", self.config)
        self.addCleanup(session.close)
        retry = session.get_adapter("https://example.invalid").max_retries

        self.assertFalse(session.trust_env)
        self.assertEqual(retry.connect, 2)
        self.assertEqual(retry.read, 0)
        self.assertEqual(retry.status, 0)
        self.assertEqual(retry.other, 0)
        self.assertIsNone(retry.total)
        self.assertEqual(retry.backoff_factor, 1.0)
        self.assertEqual(retry.allowed_methods, frozenset({"GET", "POST"}))
        self.assertFalse(retry.raise_on_status)

    def test_get_active_signs_accepts_empty_list(self):
        self.session.get.return_value = _response([])

        self.assertEqual(get_active_signs(self.session, self.config), [])

    def test_get_active_signs_accepts_sign_list(self):
        self.session.get.return_value = _response([{"signId": 1, "courseId": 2}])

        self.assertEqual(
            get_active_signs(self.session, self.config),
            [{"signId": 1, "courseId": 2}],
        )

    def test_explicit_login_expiry_messages_raise_openid_expired(self):
        payloads = (
            {"msgClient": "OpenID is expired"},
            {"msg": "invalid openId"},
            {"message": "Login has expired. Please log in again."},
            {"error": {"message": "openid is invalid"}},
            {"error": "Not logged in"},
            {"msgClient": "openId\u5df2\u5931\u6548"},
            {"msg": "\u767b\u5f55\u5df2\u8fc7\u671f\uff0c\u8bf7\u91cd\u65b0\u767b\u5f55"},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                self.session.get.return_value = _response(payload)
                with self.assertRaises(OpenIDExpired):
                    get_active_signs(self.session, self.config)

    def test_explicit_expiry_in_client_error_response_is_recognized(self):
        for status in (400, 403):
            with self.subTest(status=status):
                self.session.get.return_value = _response(
                    {"message": "openid private-test-token is invalid; login expired"},
                    status=status,
                )
                with self.assertRaises(OpenIDExpired) as raised:
                    get_active_signs(self.session, self.config)
                self.assertNotIn("private-test-token", str(raised.exception))
                self.assertNotIn("https://", str(raised.exception))

    def test_other_http_client_errors_remain_http_errors(self):
        self.session.get.return_value = _response(
            {"message": "Course unavailable"}, status=403
        )

        with self.assertRaises(requests.HTTPError):
            get_active_signs(self.session, self.config)

    def test_http_401_raises_expiry_without_requiring_json(self):
        response = _response(None, status=401)
        response._content = b"unauthorized, not JSON"
        self.session.get.return_value = response

        with self.assertRaises(OpenIDExpired) as raised:
            get_active_signs(self.session, self.config)

        self.assertIsInstance(raised.exception, requests.RequestException)
        self.assertNotIn("private-test-token", str(raised.exception))
        self.assertNotIn("https://", str(raised.exception))

    def test_5xx_is_http_error_even_when_body_claims_expiry(self):
        for status in (500, 503):
            for operation in ("active", "submit"):
                with self.subTest(status=status, operation=operation):
                    response = _response({"message": "openid expired"}, status=status)
                    self.session.get.return_value = response
                    self.session.post.return_value = response
                    with self.assertRaises(requests.HTTPError) as raised:
                        if operation == "active":
                            get_active_signs(self.session, self.config)
                        else:
                            submit_sign_in(self.session, self.config, self.target)
                    self.assertNotIsInstance(raised.exception, OpenIDExpired)
                    self.assertIs(raised.exception.response, response)

    def test_timeouts_propagate_without_becoming_expiry(self):
        for operation in ("active", "submit"):
            with self.subTest(operation=operation):
                timeout = requests.Timeout("temporary timeout")
                self.session.get.side_effect = timeout
                self.session.post.side_effect = timeout
                with self.assertRaises(requests.Timeout) as raised:
                    if operation == "active":
                        get_active_signs(self.session, self.config)
                    else:
                        submit_sign_in(self.session, self.config, self.target)
                self.assertIs(raised.exception, timeout)

    def test_unrecognized_active_payload_is_not_mistaken_for_expiry(self):
        payloads = (
            {},
            None,
            "openid expired",
            {"errorCode": 401},
            {"errorCode": 1001, "msgClient": "Invalid course ID"},
            {"message": "OpenID request failed"},
            {"message": "Invalid login settings"},
            {"data": {"message": "openid expired"}},
            {"message": {"detail": "openid expired"}},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                self.session.get.return_value = _response(payload)
                with self.assertRaises(APIResponseError):
                    get_active_signs(self.session, self.config)

    def test_unknown_payload_error_does_not_expose_payload_or_url(self):
        self.session.get.return_value = _response(
            {"msgClient": "private-test-token", "url": "https://private.invalid/"}
        )

        with self.assertRaises(APIResponseError) as raised:
            get_active_signs(self.session, self.config)

        self.assertIsInstance(raised.exception, requests.RequestException)
        self.assertNotIn("private-test-token", str(raised.exception))
        self.assertNotIn("https://", str(raised.exception))

    def test_invalid_json_is_safe_api_response_error(self):
        response = _response(None)
        response._content = b"private-test-token is not JSON"
        self.session.get.return_value = response

        with self.assertRaises(APIResponseError) as raised:
            get_active_signs(self.session, self.config)

        self.assertNotIn("private-test-token", str(raised.exception))
        self.assertNotIn("https://", str(raised.exception))
        self.assertTrue(raised.exception.__suppress_context__)

    def test_submit_preserves_success_already_signed_and_other_error_results(self):
        payloads = (
            {"studentRank": 3},
            {"errorCode": 305, "msgClient": "Already signed"},
            {"errorCode": 401, "msgClient": "Course unavailable"},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                self.session.post.return_value = _response(payload)
                self.assertEqual(
                    submit_sign_in(self.session, self.config, self.target), payload
                )

    def test_submit_detects_explicit_expiry(self):
        self.session.post.return_value = _response(
            {"error": {"msgClient": "Please log in again"}}
        )

        with self.assertRaises(OpenIDExpired):
            submit_sign_in(self.session, self.config, self.target)

    def test_submit_detects_http_401(self):
        self.session.post.return_value = _response({}, status=401)

        with self.assertRaises(OpenIDExpired):
            submit_sign_in(self.session, self.config, self.target)

    def test_submit_rejects_non_object_payload(self):
        self.session.post.return_value = _response([])

        with self.assertRaises(APIResponseError):
            submit_sign_in(self.session, self.config, self.target)


if __name__ == "__main__":
    unittest.main()
