import contextlib
import io
import json
import queue
import threading
import unittest
from unittest.mock import Mock, patch

import websocket

from wzj_signin import main, qrsign


class ManualTimer:
    instances = []

    def __init__(self, interval, callback):
        self.interval = interval
        self.callback = callback
        self.cancelled = False
        self.daemon = False
        self.instances.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.callback()


class FakeSocket:
    def __init__(self, url, **callbacks):
        self.callbacks = callbacks
        self.sent = []
        self.opened = threading.Event()
        self.closed = threading.Event()
        self.send_error = None

    def send(self, raw):
        if self.send_error:
            raise self.send_error
        self.sent.extend(json.loads(raw))

    def emit(self, messages):
        self.callbacks["on_message"](self, json.dumps(messages))

    def connect_reply(self):
        self.emit([{"channel": "/meta/connect", "successful": True, "advice": {"timeout": 15000}}])

    def run_forever(self, **options):
        self.callbacks["on_open"](self)
        self.emit([{"channel": "/meta/handshake", "successful": True, "clientId": "test-client"}])
        self.connect_reply()
        self.opened.set()
        if not self.closed.wait(3):
            raise TimeoutError("The test socket was not stopped")

    def close(self, **options):
        self.closed.set()


class StopMonitor(BaseException):
    pass


class QRTests(unittest.TestCase):
    def setUp(self):
        ManualTimer.instances = []
        self.sockets = queue.Queue()
        self.workers = []
        self.qrs = []
        self.saved_signed_ids = main.signed_ids.copy()
        main.signed_ids.clear()
        self.timer_patch = patch.object(qrsign.threading, "Timer", ManualTimer)
        self.timer_patch.start()
        self.socket_patch = patch.object(qrsign.websocket, "WebSocketApp", side_effect=self.make_socket)
        self.socket_factory = self.socket_patch.start()
        self.mail_patch = patch.object(main, "send_email")
        self.mail = self.mail_patch.start()
        self.addCleanup(self.timer_patch.stop)
        self.addCleanup(self.socket_patch.stop)
        self.addCleanup(self.mail_patch.stop)

    def tearDown(self):
        for qr in self.qrs:
            qr.stop()
        main._stop_qr_tasks()
        for worker in self.workers:
            worker.join(2)
            self.assertFalse(worker.is_alive(), "A QR test worker did not exit")
        self.assertFalse(main._qr_tasks)
        main.signed_ids.clear()
        main.signed_ids.update(self.saved_signed_ids)

    def make_socket(self, url, **callbacks):
        socket = FakeSocket(url, **callbacks)
        self.sockets.put(socket)
        return socket

    def opened_socket(self):
        socket = self.sockets.get(timeout=2)
        self.assertTrue(socket.opened.wait(2), "The QR socket did not finish handshaking")
        return socket

    def start_qr(self, student=" 12345 "):
        qr = qrsign.QRSign("course", 10, student)
        self.qrs.append(qr)
        result = []
        worker = threading.Thread(target=lambda: result.append(qr.start()), daemon=True)
        self.workers.append(worker)
        worker.start()
        return qr, self.opened_socket(), worker, result

    @staticmethod
    def student_event(student, sign_id=10):
        return {"channel": f"/attendance/course/{sign_id}/qr", "data": {"student": {"studentNumber": student, "rank": 1}}}

    def test_matching_student_exits_and_cancels_heartbeat(self):
        qr, socket, worker, result = self.start_qr()
        timer = ManualTimer.instances[-1]
        socket.emit([self.student_event(" 12345 "), self.student_event("12345")])
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [True])
        self.assertTrue(socket.closed.is_set())
        self.assertTrue(timer.cancelled)
        self.assertIsNone(qr._heartbeat)
        self.assertIsNone(qr._ws)

    def test_missing_other_or_wrong_channel_student_does_not_finish(self):
        for expected in ("12345", ""):
            with self.subTest(expected=expected):
                qr, socket, worker, result = self.start_qr(expected)
                socket.emit([
                    self.student_event("other-student"),
                    self.student_event("12345", sign_id=99),
                    self.student_event(""),
                    {"channel": "/attendance/course/10/qr", "data": {}},
                ])
                self.assertFalse(qr._done.is_set())
                self.assertFalse(socket.closed.is_set())
                qr.stop()
                worker.join(1)
                self.assertEqual(result, [False])

    def test_qr_url_in_success_event_does_not_hide_student_result(self):
        _, socket, worker, result = self.start_qr()
        event = self.student_event("12345")
        event["data"]["qrUrl"] = "https://example.invalid/test-qr"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            socket.emit([event])
        worker.join(1)
        self.assertEqual(result, [True])

    def test_repeated_connect_replies_keep_one_heartbeat_and_subscription(self):
        qr, socket, worker, _ = self.start_qr()
        for _ in range(4):
            socket.connect_reply()
        self.assertEqual(len(ManualTimer.instances), 1)
        channels = [message["channel"] for message in socket.sent]
        self.assertEqual(channels.count("/meta/connect"), 1)
        self.assertEqual(channels.count("/meta/subscribe"), 1)
        ManualTimer.instances[0].fire()
        self.assertEqual(len(ManualTimer.instances), 2)
        for _ in range(4):
            socket.connect_reply()
        self.assertEqual(sum(message["channel"] == "/meta/connect" for message in socket.sent), 2)
        self.assertEqual(sum(message["channel"] == "/meta/subscribe" for message in socket.sent), 1)
        qr.stop()
        sent = list(socket.sent)
        ManualTimer.instances[-1].callback()
        self.assertEqual(socket.sent, sent)
        self.assertEqual(len(ManualTimer.instances), 2)
        self.assertTrue(ManualTimer.instances[-1].cancelled)
        worker.join(1)

    def test_disconnect_timeout_protocol_and_heartbeat_errors_release_resources(self):
        for failure in ("disconnect", "timeout", "malformed", "subscription", "heartbeat"):
            with self.subTest(failure=failure):
                qr, socket, worker, result = self.start_qr()
                timer = ManualTimer.instances[-1]
                if failure == "disconnect":
                    socket.callbacks["on_close"](socket, 1006, "test disconnect")
                elif failure == "timeout":
                    socket.callbacks["on_error"](socket, websocket.WebSocketTimeoutException("test timeout"))
                elif failure == "malformed":
                    socket.callbacks["on_message"](socket, "not JSON")
                elif failure == "subscription":
                    socket.emit([{"channel": "/meta/subscribe", "successful": False}])
                else:
                    socket.send_error = websocket.WebSocketConnectionClosedException("test disconnect")
                    timer.fire()
                worker.join(1)
                self.assertFalse(worker.is_alive())
                self.assertEqual(result, [False])
                self.assertTrue(socket.closed.is_set())
                self.assertTrue(timer.cancelled)
                self.assertIsNone(qr._heartbeat)
                self.assertIsNone(qr._ws)

    def test_run_forever_exception_always_closes_socket(self):
        socket = Mock()
        socket.run_forever.side_effect = websocket.WebSocketTimeoutException("test timeout")
        qr = qrsign.QRSign("course", 10, "12345")
        self.qrs.append(qr)
        with patch.object(qrsign.websocket, "WebSocketApp", return_value=socket):
            self.assertFalse(qr.start())
        socket.close.assert_called_once()
        self.assertIsNone(qr._ws)

    def test_failed_task_is_removed_and_can_retry_then_remains_completed(self):
        sign = {"signId": 10, "courseId": "course", "name": "Course", "isQR": 1}
        main.process_signs(None, None, [sign], "12345", None)
        socket = self.opened_socket()
        worker = main._qr_tasks[10][1]
        socket.callbacks["on_error"](socket, RuntimeError("test disconnect"))
        worker.join(1)
        self.assertNotIn(10, main._qr_tasks)
        self.assertNotIn(10, main.signed_ids)
        main.process_signs(None, None, [sign], "12345", None)
        socket = self.opened_socket()
        worker = main._qr_tasks[10][1]
        for _ in range(3):
            main.process_signs(None, None, [sign], "12345", None)
        socket.emit([self.student_event("12345")])
        worker.join(1)
        self.assertNotIn(10, main._qr_tasks)
        self.assertIn(10, main.signed_ids)
        main.process_signs(None, None, [sign], "12345", None)
        self.assertEqual(self.socket_factory.call_count, 2)
        self.assertEqual([call.args[0] for call in self.mail.call_args_list], ["QR Sign-in Found", "QR Sign-in Found", "Sign-in Success"])

    def test_thread_start_failure_releases_task_for_retry(self):
        sign = {"signId": 10, "courseId": "course", "name": "Course", "isQR": 1}
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("test startup error")):
            main.process_signs(None, None, [sign], "12345", None)
        self.assertNotIn(10, main._qr_tasks)
        self.assertNotIn(10, main.signed_ids)
        main.process_signs(None, None, [sign], "12345", None)
        socket = self.opened_socket()
        worker = main._qr_tasks[10][1]
        socket.emit([self.student_event("12345")])
        worker.join(1)
        self.assertIn(10, main.signed_ids)

    def test_main_keeps_polling_deduplicates_and_cleans_up_on_exit(self):
        sign = {"signId": 10, "courseId": "course", "name": "Course", "isQR": 1}
        other = dict(sign, signId=11)
        pauses = queue.Queue()
        advance = threading.Semaphore(0)
        stopping = threading.Event()
        failures = []
        poll_count = []
        session = Mock()
        config = {"requestConfig": Mock(connect_timeout=5, read_timeout=15, request_retries=2), "studentId": "12345", "pollInterval": 7, "apiBaseUrl": None, "locations": []}

        def active(*args):
            poll_count.append(1)
            return [sign, other] if len(poll_count) >= 3 else [sign]

        def pause(interval):
            pauses.put(interval)
            if not advance.acquire(timeout=3) or stopping.is_set():
                raise StopMonitor()

        def monitor():
            try:
                main.main()
            except StopMonitor:
                pass
            except BaseException as exc:
                failures.append(exc)

        with patch.object(main, "_load_config", return_value=config), patch.object(
            main, "make_session", return_value=session
        ), patch.object(main, "_validate_open_id", return_value=config["requestConfig"]), patch.object(
            main, "_select_location", return_value=("114.4", "30.5")
        ), patch.object(main, "get_active_signs", side_effect=active), patch.object(main, "sleep_with_progress", side_effect=pause):
            monitor_thread = threading.Thread(target=monitor, daemon=True)
            monitor_thread.start()
            try:
                self.assertEqual(pauses.get(timeout=1), 7)
                socket = self.opened_socket()
                advance.release()
                self.assertEqual(pauses.get(timeout=1), 7)
                self.assertFalse(socket.closed.is_set())
                self.assertEqual(self.socket_factory.call_count, 1)
                self.assertEqual(self.mail.call_count, 1)
                worker = main._qr_tasks[10][1]
                socket.emit([self.student_event("12345")])
                worker.join(1)
                self.assertIn(10, main.signed_ids)
                self.assertNotIn(10, main._qr_tasks)
                advance.release()
                self.assertEqual(pauses.get(timeout=1), 7)
                other_socket = self.opened_socket()
                self.assertEqual(self.socket_factory.call_count, 2)
            finally:
                stopping.set()
                advance.release()
                monitor_thread.join(2)
            self.assertFalse(monitor_thread.is_alive())
        self.assertFalse(failures)
        self.assertEqual(len(poll_count), 3)
        self.assertTrue(other_socket.closed.is_set())
        self.assertNotIn(11, main.signed_ids)
        self.assertFalse(main._qr_tasks)


if __name__ == "__main__":
    unittest.main()
