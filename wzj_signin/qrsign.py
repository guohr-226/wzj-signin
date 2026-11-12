import json
import threading

import segno
import websocket

from wzj_signin.logger import log


class QRSign:
    """Subscribe to QR channel via WebSocket, display QR code and wait for sign-in result."""

    WS_URL = "wss://www.teachermate.com.cn/faye"

    def __init__(self, course_id: str, sign_id: int, check_student_number):
        self.course_id = course_id
        self.sign_id = sign_id
        self.check_student_number = check_student_number
        self._seq = 0
        self._client_id = ""
        self._heartbeat: threading.Timer | None = None
        self._done = threading.Event()
        self._stopped = threading.Event()
        self._state_lock = threading.RLock()
        self._subscribed = False
        self._ws: websocket.WebSocketApp | None = None

    def _next_id(self) -> str:
        with self._state_lock:
            self._seq += 1
            return str(self._seq)

    def _send(self, msg=None):
        raw = json.dumps([msg] if msg else [])
        if not self._stopped.is_set():
            self._ws.send(raw)

    def _start_heartbeat(self, timeout_ms: int):
        interval = max(timeout_ms / 2 / 1000, 0.1)

        def beat():
            if self._stopped.is_set():
                return
            try:
                self._send()
                self._send(
                    {
                        "channel": "/meta/connect",
                        "clientId": self._client_id,
                        "connectionType": "websocket",
                        "id": self._next_id(),
                    }
                )
                with self._state_lock:
                    self._heartbeat = None
                    self._start_heartbeat(timeout_ms)
            except Exception as err:
                self._on_error(self._ws, err)

        with self._state_lock:
            if self._stopped.is_set() or self._heartbeat is not None:
                return
            self._heartbeat = threading.Timer(interval, beat)
            self._heartbeat.daemon = True
            self._heartbeat.start()

    def _on_message(self, _ws, raw):
        try:
            messages = json.loads(raw)
            for msg in messages:
                if self._stopped.is_set():
                    return
                channel = msg.get("channel", "")
                if channel == f"/attendance/{self.course_id}/{self.sign_id}/qr":
                    self._handle_qr(msg)
                    continue
                if not channel.startswith("/meta/"):
                    continue
                if not msg.get("successful"):
                    log.warning("faye %s request failed", channel)
                    self.stop()
                    return
                if channel == "/meta/handshake":
                    if self._client_id:
                        continue
                    self._client_id = msg["clientId"]
                    self._send(
                        {
                            "channel": "/meta/connect",
                            "clientId": self._client_id,
                            "connectionType": "websocket",
                            "id": self._next_id(),
                        }
                    )
                elif channel == "/meta/connect":
                    timeout = msg.get("advice", {}).get("timeout", 15000)
                    self._start_heartbeat(timeout)
                    if not self._subscribed:
                        self._subscribed = True
                        self._send(
                            {
                                "channel": "/meta/subscribe",
                                "clientId": self._client_id,
                                "subscription": f"/attendance/{self.course_id}/{self.sign_id}/qr",
                                "id": self._next_id(),
                            }
                        )
        except Exception as err:
            self._on_error(self._ws, err)

    def _handle_qr(self, msg):
        if self._stopped.is_set():
            return
        data = msg.get("data", {})
        qr_url = data.get("qrUrl")
        if qr_url:
            log.info("QR code received, please scan:")
            segno.make_qr(qr_url).terminal(compact=True, border=2)
        student = data.get("student")
        if not student:
            return
        sid = str(student.get("studentNumber", "")).strip()
        expected = str(self.check_student_number).strip()
        rank = student.get("rank", "")
        if sid and expected and sid == expected:
            log.info("Sign-in success! rank: %s", rank)
            self._done.set()
            self.stop()
        else:
            log.info("Other student signed in, rank: %s", rank)

    def _on_open(self, ws):
        log.info("WebSocket connected, handshaking...")
        if self._stopped.is_set():
            ws.close(timeout=0)
            return
        self._send(
            {
                "channel": "/meta/handshake",
                "version": "1.0",
                "supportedConnectionTypes": [
                    "websocket",
                    "eventsource",
                    "long-polling",
                    "cross-origin-long-polling",
                    "callback-polling",
                ],
                "id": self._next_id(),
            }
        )

    def _on_error(self, _ws, err):
        log.error("WebSocket error (%s)", type(err).__name__)
        self.stop()

    def _on_close(self, _ws, code, reason):
        self.stop()
        log.info("WebSocket closed (code=%s)", code)

    def stop(self):
        """Stop heartbeats and close the socket without waiting for a close reply."""
        with self._state_lock:
            if self._stopped.is_set():
                return
            self._stopped.set()
            if self._heartbeat:
                self._heartbeat.cancel()
                self._heartbeat = None
        if self._ws:
            try:
                self._ws.close(timeout=0)
            except Exception as err:
                log.warning("WebSocket cleanup failed (%s)", type(err).__name__)

    def start(self):
        """Block until sign-in succeeds or connection closes."""
        with self._state_lock:
            if self._stopped.is_set():
                return False
            self._ws = websocket.WebSocketApp(
                self.WS_URL,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
        try:
            # Transport pings detect dead peers independently of Bayeux messages.
            self._ws.run_forever(
                http_no_proxy=["*"], ping_interval=30, ping_timeout=10
            )
        except Exception as err:
            self._on_error(self._ws, err)
        finally:
            self.stop()
            self._ws = None
        return self._done.is_set()
