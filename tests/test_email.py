import importlib.util
import io
import os
import smtplib
import threading
import unittest
from contextlib import ExitStack
from email import message_from_string
from email.header import decode_header, make_header
from pathlib import Path
from unittest import mock


_EMAIL_PATH = Path(__file__).resolve().parents[1] / "wzj_signin" / "email.py"
_CONFIG = {
    "enable": True,
    "smtp_server": "smtp.qq.com",
    "sender": "sender@example.test",
    "password": "private-password",
    "receiver": "receiver@example.test",
}


def _load_module(environment=None):
    spec = importlib.util.spec_from_file_location("wzj_signin._email_test_instance", _EMAIL_PATH)
    module = importlib.util.module_from_spec(spec)
    with (
        mock.patch.dict(os.environ, environment or {}, clear=True),
        mock.patch.object(Path, "exists", return_value=False),
        mock.patch.object(smtplib, "SMTP_SSL") as smtp,
    ):
        spec.loader.exec_module(module)
        smtp.assert_not_called()
    return module


class EmailTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_module()
        self.module.cfg = dict(_CONFIG)
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.release_events = []
        self.module.log = mock.Mock()

    def tearDown(self):
        # Release every blocked fake before stopping this test's private worker.
        for event in self.release_events:
            event.set()
        worker = self.module._worker_thread
        if worker is not None:
            self.module._queue.put(None, timeout=2)
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive(), "The isolated email worker did not stop")
            self.assertEqual(self.module._queue.unfinished_tasks, 0)

    def _smtp(self, **kwargs):
        return self.patches.enter_context(mock.patch.object(self.module.smtplib, "SMTP_SSL", **kwargs))

    def _drain(self):
        work_queue = self.module._queue
        with work_queue.all_tasks_done:
            completed = work_queue.all_tasks_done.wait_for(
                lambda: work_queue.unfinished_tasks == 0, timeout=2
            )
        self.assertTrue(completed, "The email worker did not finish its queued messages")

    def _log_messages(self, level):
        return [args[0] % args[1:] for args, _ in getattr(self.module.log, level).call_args_list]

    def _block_smtp(self):
        entered = threading.Event()
        release = threading.Event()
        self.release_events.append(release)
        server = mock.Mock()

        def connect(*args, **kwargs):
            entered.set()
            if not release.wait(timeout=2):
                raise TimeoutError("Fake SMTP was not released")
            return server

        smtp = self._smtp(side_effect=connect)
        return smtp, server, entered, release

    def test_import_never_connects_or_starts_a_worker(self):
        environment = {
            "ENABLE_SEND_EMAIL": "true",
            "EMAIL_SENDER": _CONFIG["sender"],
            "EMAIL_PASSWORD": _CONFIG["password"],
            "EMAIL_RECEIVER": _CONFIG["receiver"],
        }
        module = _load_module(environment)
        self.assertTrue(module.cfg["enable"])
        self.assertIsNone(module._worker_thread)
        self.assertEqual(module._queue.maxsize, 32)

    def test_import_with_incomplete_config_does_not_exit(self):
        module = _load_module({"ENABLE_SEND_EMAIL": "true"})
        self.assertFalse(module.send_email("subject", "body"))
        self.assertIsNone(module._worker_thread)

    def test_explicit_false_environment_overrides_enabled_toml(self):
        for value in ("false", "0", "", "FALSE"):
            with self.subTest(value=value):
                with (
                    mock.patch.dict(os.environ, {"ENABLE_SEND_EMAIL": value}, clear=True),
                    mock.patch.object(Path, "exists", return_value=True),
                    mock.patch("builtins.open", side_effect=lambda *args: io.BytesIO(b"[email]\nenable_send_email = true\n")),
                ):
                    self.assertFalse(self.module._load_email_config()["enable"])

    def test_toml_enable_applies_when_environment_is_absent(self):
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(Path, "exists", return_value=True),
            mock.patch("builtins.open", side_effect=lambda *args: io.BytesIO(b"[email]\nenable_send_email = true\n")),
        ):
            self.assertTrue(self.module._load_email_config()["enable"])

    def test_malformed_config_is_safe_and_does_not_log_raw_values(self):
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(Path, "exists", return_value=True),
            mock.patch("builtins.open", return_value=io.BytesIO(b"password = private-password")),
        ):
            self.assertFalse(self.module._load_email_config()["enable"])
        self.assertNotIn("private-password", " ".join(self._log_messages("warning")))

    def test_shared_mail_settings_and_private_credentials_are_combined(self):
        settings = b'[email]\nenable_send_email=true\nsmtp_server="smtp.qq.com"\n'
        credentials = b'[email]\nsender="sender@example.test"\npassword="private-password"\nreceiver="receiver@example.test"\n'
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(Path, "exists", return_value=True),
            mock.patch("builtins.open", side_effect=[io.BytesIO(settings), io.BytesIO(credentials)]),
        ):
            self.assertEqual(self.module._load_email_config(), _CONFIG)

    def test_disabled_or_incomplete_config_does_not_start_worker(self):
        smtp = self._smtp()
        self.module.cfg["enable"] = False
        self.assertFalse(self.module.send_email("subject", "body"))
        self.module.cfg["enable"] = True
        for key in ("smtp_server", "sender", "password", "receiver"):
            with self.subTest(key=key):
                self.module.cfg = dict(_CONFIG, **{key: ""})
                self.assertFalse(self.module.send_email("subject", "body"))
        self.assertIsNone(self.module._worker_thread)
        smtp.assert_not_called()

    def test_worker_enqueues_without_waiting_for_blocked_smtp(self):
        smtp, server, entered, release = self._block_smtp()
        self.assertTrue(self.module.send_email("first", "body"))
        self.assertTrue(entered.wait(timeout=2))
        self.assertTrue(self.module.send_email("second", "body"))
        self.assertFalse(release.is_set())
        server.login.assert_not_called()
        self.assertTrue(self.module._worker_thread.daemon)
        self.assertEqual(self.module._queue.qsize(), 1)
        queued_logs = self._log_messages("info")
        self.assertEqual(sum("queued; delivery pending" in entry for entry in queued_logs), 2)
        self.assertFalse(any("delivery succeeded" in entry for entry in queued_logs))

        release.set()
        self._drain()
        self.assertEqual(smtp.call_count, 2)
        smtp.assert_called_with("smtp.qq.com", 465, timeout=10)
        server.login.assert_called_with(_CONFIG["sender"], _CONFIG["password"])
        self.assertEqual(server.sendmail.call_count, 2)
        sender, receivers, message = server.sendmail.call_args.args
        self.assertEqual((sender, receivers), (_CONFIG["sender"], [_CONFIG["receiver"]]))
        subject = str(make_header(decode_header(message_from_string(message)["Subject"])))
        self.assertEqual(subject, "[wzj-signin] second")
        logs = self._log_messages("info")
        for stage in ("connect", "auth", "send"):
            self.assertTrue(any(f"SMTP {stage} completed in " in entry for entry in logs))
        self.assertEqual(sum("delivery succeeded (SMTP accepted) in " in entry for entry in logs), 2)
        self.assertEqual(server.quit.call_count, 2)
        self.assertEqual(server.close.call_count, 2)

    def test_concurrent_senders_start_only_one_worker(self):
        self._smtp(return_value=mock.Mock())
        barrier = threading.Barrier(9)
        results = []

        def enqueue():
            barrier.wait(timeout=2)
            results.append(self.module.send_email("subject", "body"))

        producers = [threading.Thread(target=enqueue) for _ in range(8)]
        real_thread = threading.Thread
        with mock.patch.object(self.module.threading, "Thread", wraps=real_thread) as thread_factory:
            for producer in producers:
                producer.start()
            barrier.wait(timeout=2)
            for producer in producers:
                producer.join(timeout=2)
                self.assertFalse(producer.is_alive())
            self.assertEqual(thread_factory.call_count, 1)
            self.assertTrue(thread_factory.call_args.kwargs["daemon"])
        self.assertEqual(results, [True] * 8)
        self._drain()

    def test_smtp_failure_does_not_kill_worker_or_leak_details(self):
        failed = mock.Mock()
        failed.login.side_effect = smtplib.SMTPAuthenticationError(
            535, b"private-password sender@example.test raw-server-secret"
        )
        delivered = mock.Mock()
        self._smtp(side_effect=[failed, delivered])
        self.assertTrue(self.module.send_email("private-subject", "private-body"))
        self.assertTrue(self.module.send_email("next", "body"))
        self._drain()
        self.assertTrue(self.module._worker_thread.is_alive())
        failed.sendmail.assert_not_called()
        failed.close.assert_called_once_with()
        delivered.sendmail.assert_called_once()
        self.assertTrue(any("failed during auth after " in entry for entry in self._log_messages("error")))
        logs = " ".join(self._log_messages("info") + self._log_messages("error"))
        for secret in (_CONFIG["sender"], _CONFIG["receiver"], _CONFIG["password"], "raw-server-secret", "private-subject", "private-body"):
            self.assertNotIn(secret, logs)

    def test_connection_timeout_does_not_stop_later_notifications(self):
        delivered = mock.Mock()
        smtp = self._smtp(side_effect=[TimeoutError("private-password"), delivered])
        self.assertTrue(self.module.send_email("first", "body"))
        self.assertTrue(self.module.send_email("second", "body"))
        self._drain()
        self.assertTrue(self.module._worker_thread.is_alive())
        self.assertEqual(smtp.call_count, 2)
        self.assertEqual(smtp.call_args.kwargs["timeout"], 10)
        delivered.sendmail.assert_called_once()
        errors = " ".join(self._log_messages("error"))
        self.assertIn("failed during connect", errors)
        self.assertNotIn("private-password", errors)

    def test_unexpected_delivery_failure_still_marks_done_and_continues(self):
        deliver = self.patches.enter_context(
            mock.patch.object(self.module, "_deliver_email", side_effect=[RuntimeError("secret"), None])
        )
        self.assertTrue(self.module.send_email("first", "body"))
        self.assertTrue(self.module.send_email("second", "body"))
        self._drain()
        self.assertEqual(deliver.call_count, 2)
        self.assertTrue(self.module._worker_thread.is_alive())
        errors = self._log_messages("error")
        self.assertEqual(len(errors), 1)
        self.assertNotIn("secret", errors[0])

    def test_full_queue_is_bounded_warns_and_returns_false(self):
        _, _, entered, release = self._block_smtp()
        self.assertTrue(self.module.send_email("in flight", "body"))
        self.assertTrue(entered.wait(timeout=2))
        for number in range(32):
            self.assertTrue(self.module.send_email(str(number), "body"))
        self.assertEqual(self.module._queue.qsize(), 32)
        self.assertFalse(self.module.send_email("overflow", "body"))
        self.assertEqual(self.module._queue.qsize(), 32)
        self.assertTrue(any("queue full" in entry for entry in self._log_messages("warning")))
        release.set()
        self._drain()

    def test_quit_failure_does_not_turn_accepted_delivery_into_failure(self):
        server = mock.Mock()
        server.quit.side_effect = OSError("raw-server-secret")
        self._smtp(return_value=server)
        self.assertTrue(self.module.send_email("subject", "body"))
        self._drain()
        server.close.assert_called_once_with()
        self.assertTrue(any("delivery succeeded" in entry for entry in self._log_messages("info")))
        self.module.log.error.assert_not_called()
        warnings = self._log_messages("warning")
        self.assertTrue(any("cleanup failed after " in entry for entry in warnings))
        self.assertNotIn("raw-server-secret", " ".join(warnings))


if __name__ == "__main__":
    unittest.main()
