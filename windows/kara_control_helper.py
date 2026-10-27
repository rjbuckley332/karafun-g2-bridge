#!/usr/bin/env python3
"""KaraFun control helper — runs in the user's interactive session and taps
system media keys for KaraFun (play/pause, next track). The SYSTEM bridge
service can't inject keystrokes into the interactive desktop, so it proxies
commands here.

Listens on 127.0.0.1:8769. Pure stdlib + ctypes."""
import ctypes
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEYEVENTF_KEYUP = 0x0002
VK_MEDIA_PLAY_PAUSE = 0xB3
VK_MEDIA_NEXT_TRACK = 0xB0


def tap(vk: int) -> None:
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.05)
    ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


class H(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/playpause", "/pause", "/play", "/toggle"):
            tap(VK_MEDIA_PLAY_PAUSE)
            self._json({"ok": True, "sent": "playpause"})
        elif self.path == "/next":
            tap(VK_MEDIA_NEXT_TRACK)
            self._json({"ok": True, "sent": "next"})
        elif self.path in ("/", "/health"):
            self._json({"ok": True, "helper": "karafun-control"})
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        self.do_GET()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8769), H).serve_forever()
