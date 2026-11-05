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
        self._ws: websocket.WebSocketApp | None = None

    def _next_id(self) -> str:
        self._seq += 1
        return str(self._seq)

    def _send(self, msg=None):
        raw = json.dumps([msg] if msg else [])
        self._ws.send(raw)

    def _start_heartbeat(self, timeout_ms: int):
        interval = timeout_ms / 2 / 1000

        def beat():
            if self._done.is_set():
                return
            self._send()
            self._send(
                {
                    "channel": "/meta/connect",
                    "clientId": self._client_id,
                    "connectionType": "websocket",
                    "id": self._next_id(),
                }
            )
            self._heartbeat = threading.Timer(interval, beat)
            self._heartbeat.daemon = True
            self._heartbeat.start()

        beat()

    def _on_message(self, _ws, raw):
        messages = json.loads(raw)
        if not messages:
            return
        for msg in messages:
            channel = msg.get("channel", "")
            if "/attendance/" in channel and "/qr" in channel:
                self._handle_qr(msg)
                continue
            if not msg.get("successful"):
                log.warning("faye %s request failed", channel)
                continue
            if channel == "/meta/handshake":
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
                self._send(
                    {
                        "channel": "/meta/subscribe",
                        "clientId": self._client_id,
                        "subscription": f"/attendance/{self.course_id}/{self.sign_id}/qr",
                        "id": self._next_id(),
                    }
                )

    def _handle_qr(self, msg):
        data = msg.get("data", {})
        qr_url = data.get("qrUrl")
        if qr_url:
            log.info("QR code received, please scan:")
            segno.make_qr(qr_url).terminal(compact=True, border=2)
            return
        student = data.get("student", {})
        sid = str(student.get("studentNumber", ""))
        rank = student.get("rank", "")
        if sid == str(self.check_student_number).strip():
            log.info("Sign-in success! rank: %s", rank)
            self._done.set()
            self._ws.close()
        else:
            log.info("Other student signed in, rank: %s", rank)

    def _on_open(self, ws):
        log.info("WebSocket connected, handshaking...")
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

    def _on_close(self, _ws, code, reason):
        if self._heartbeat:
            self._heartbeat.cancel()
        log.info("WebSocket closed (code=%s)", code)

    def start(self):
        """Block until sign-in succeeds or connection closes."""
        self._ws = websocket.WebSocketApp(
            self.WS_URL,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        # http_no_proxy=["*"] 跳过所有代理，避免被本地翻墙代理拦截国内 wss。
        self._ws.run_forever(http_no_proxy=["*"])
        return self._done.is_set()
