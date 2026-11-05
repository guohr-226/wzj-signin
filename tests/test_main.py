import contextlib
import io
import os
import unittest
from unittest.mock import patch

import requests

from wzj_signin import main
from wzj_signin.http_client import OpenIDExpired, SignTarget, build_request_config
from wzj_signin.qrsign import QRSign


def request_config(open_id="old-token"):
    return build_request_config(
        open_id=open_id,
        api_base_url="https://example.invalid",
        connect_timeout=5,
        read_timeout=15,
        request_retries=2,
        retry_backoff=1,
    )


class StopMonitoring(BaseException):
    pass


class StartupTests(unittest.TestCase):
    def test_saved_openid_wins_over_environment(self):
        settings = b'[default]\npollInterval=7\n'
        credentials = b'[default]\nopenId="saved-token"\ncourseId="course"\nstudentId="student"\n'
        with patch.dict(os.environ, {"OPEN_ID": "environment-token"}, clear=True), patch(
            "wzj_signin.main.Path.exists", return_value=True
        ), patch("wzj_signin.main.open", side_effect=[io.BytesIO(settings), io.BytesIO(credentials)]):
            config = main._load_config()
        self.assertEqual(config["requestConfig"].open_id, "saved-token")
        self.assertEqual(config["studentId"], "student")
        self.assertEqual(config["pollInterval"], 7)

    def test_empty_sign_list_verifies_without_prompt(self):
        config = request_config()
        with requests.Session() as session, patch.object(
            main, "get_active_signs", return_value=[]
        ), patch.object(main, "_read_input", side_effect=AssertionError("Unexpected prompt")):
            self.assertIs(main._validate_open_id(session, config, 10, None), config)

    def test_expired_credentials_are_reprompted_until_verified(self):
        config = request_config()
        seen = []

        def active(session, current):
            seen.append((current.open_id, session.headers["openId"]))
            if current.open_id != "verified-token":
                raise OpenIDExpired()
            return []

        with requests.Session() as session, patch.object(
            main, "get_active_signs", side_effect=active
        ), patch.object(main, "_read_input", side_effect=["rejected-token", "verified-token"]):
            session.headers["openId"] = config.open_id
            updated = main._validate_open_id(session, config, 10, None)
            self.assertEqual(session.headers["openId"], "verified-token")
        self.assertEqual(updated.open_id, "verified-token")
        self.assertEqual(seen, [(token, token) for token in ("old-token", "rejected-token", "verified-token")])

    def test_full_url_renewal_updates_api_host_and_header(self):
        with requests.Session() as session, patch.object(
            main, "_read_input", return_value="https://new.example.invalid/path?openid=new-token"
        ):
            updated = main._renew_open_id(session, request_config(), None)
            self.assertEqual(updated.api_base_url, "https://new.example.invalid")
            self.assertEqual(session.headers["openId"], "new-token")
            self.assertEqual(updated.timeout, (5, 15))

    def test_malformed_and_empty_input_are_not_accepted(self):
        with requests.Session() as session, patch.object(
            main, "_read_input", side_effect=["", "https://example.invalid/no-token", "valid-token"]
        ):
            updated = main._renew_open_id(session, request_config(), None)
        self.assertEqual(updated.open_id, "valid-token")

    def test_explicit_api_override_survives_url_renewal(self):
        with requests.Session() as session, patch.object(
            main, "_read_input", return_value="https://new.example.invalid/?openid=new-token"
        ):
            updated = main._renew_open_id(session, request_config(), "https://override.invalid")
        self.assertEqual(updated.api_base_url, "https://override.invalid")

    def test_temporary_failure_retries_same_credentials_without_prompt(self):
        for error in (requests.ReadTimeout("sensitive-token"), requests.HTTPError(response=self._response(503))):
            with self.subTest(error=type(error).__name__), requests.Session() as session, patch.object(
                main, "get_active_signs", side_effect=[error, []]
            ), patch.object(main, "_read_input", side_effect=AssertionError("Unexpected prompt")), patch.object(
                main, "sleep_with_progress"
            ) as pause, self.assertLogs("wzj_signin", level="ERROR") as logs:
                config = request_config()
                self.assertIs(main._validate_open_id(session, config, 10, None), config)
                pause.assert_called_once_with(10)
                self.assertNotIn("sensitive-token", " ".join(logs.output))

    @staticmethod
    def _response(status):
        response = requests.Response()
        response.status_code = status
        return response

    def test_noninteractive_input_fails_clearly(self):
        with patch.object(main.sys.stdin, "isatty", return_value=False):
            with self.assertRaisesRegex(SystemExit, "Interactive terminal required"):
                main._read_input("OpenID: ", secret=True)

    def test_secret_input_uses_hidden_prompt(self):
        with patch.object(main.sys.stdin, "isatty", return_value=True), patch(
            "getpass.getpass", return_value=" token "
        ) as hidden, patch("builtins.input", side_effect=AssertionError("Input must be hidden")):
            self.assertEqual(main._read_input("OpenID: ", secret=True), "token")
            hidden.assert_called_once()

    def test_runtime_expiry_resumes_with_updated_credentials(self):
        original = request_config()
        config = {
            "requestConfig": original,
            "studentId": "private-student",
            "pollInterval": 10,
            "apiBaseUrl": None,
            "locations": [{"name": "Location", "lon": 114.4, "lat": 30.5}],
        }
        seen = []

        def process(session, current, signs, student_id, positions):
            seen.append((current.open_id, session.headers["openId"], signs, positions))

        with patch.object(main, "_load_config", return_value=config), patch.object(
            main, "get_active_signs", side_effect=[[], OpenIDExpired(), [], [{"signId": 2}]]
        ), patch.object(main.sys.stdin, "isatty", return_value=True), patch.object(
            main, "_read_input", side_effect=["1", "new-token"]
        ), patch.object(main, "send_email"), patch.object(main, "process_signs", side_effect=process), patch.object(
            main, "sleep_with_progress", side_effect=[None, StopMonitoring()]
        ), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(StopMonitoring):
                main.main()
        self.assertEqual(seen[0][:3], ("new-token", "new-token", [{"signId": 2}]))
        self.assertAlmostEqual(float(seen[0][3][0]), 114.4, delta=0.000101)
        self.assertEqual(original.open_id, "old-token")


class LocationTests(unittest.TestCase):
    locations = [
        {"name": "North", "lon": 114.440465, "lat": 30.517877},
        {"name": "South", "lon": 114.433286, "lat": 30.518387},
    ]

    def test_invalid_numbers_reprompt_and_selected_coordinates_are_jittered(self):
        output = io.StringIO()
        with patch.object(main.sys.stdin, "isatty", return_value=True), patch.object(
            main, "_read_input", side_effect=["0", "-1", "3", "text", "", "2"]
        ), contextlib.redirect_stdout(output), self.assertLogs("wzj_signin", level="INFO") as logs:
            lon, lat = main._select_location(self.locations)
        self.assertEqual(output.getvalue().count("Invalid location number"), 5)
        self.assertIn("Selected GPS location: South", " ".join(logs.output))
        self.assertAlmostEqual(float(lon), 114.433286, delta=0.000101)
        self.assertAlmostEqual(float(lat), 30.518387, delta=0.000101)

    def test_missing_locations_do_not_choose_an_implicit_default(self):
        with self.assertRaisesRegex(SystemExit, "No locations configured"):
            main._select_location([])

    def test_noninteractive_location_selection_fails_without_reading(self):
        with patch.object(main.sys.stdin, "isatty", return_value=False), patch.object(
            main, "_read_input", side_effect=AssertionError("Unexpected input")
        ):
            with self.assertRaisesRegex(SystemExit, "Interactive terminal required"):
                main._select_location(self.locations)


class PrivacyTests(unittest.TestCase):
    def test_qr_success_does_not_log_student_number(self):
        qr = QRSign("course", 1, "private-student")
        from unittest.mock import Mock

        qr._ws = Mock()
        with self.assertLogs("wzj_signin", level="INFO") as logs:
            qr._handle_qr({"data": {"student": {"studentNumber": "private-student", "rank": 2}}})
        self.assertTrue(qr._done.is_set())
        self.assertNotIn("private-student", " ".join(logs.output))

    def test_signin_failure_does_not_log_server_payload(self):
        with patch.object(main, "submit_sign_in", return_value={"errorCode": 999, "msgClient": "private-token"}), self.assertLogs(
            "wzj_signin", level="WARNING"
        ) as logs:
            self.assertFalse(main.sign_in_one(None, request_config(), SignTarget("Course", 1, 2)))
        self.assertNotIn("private-token", " ".join(logs.output))


if __name__ == "__main__":
    unittest.main()
