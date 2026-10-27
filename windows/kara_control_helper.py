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

# single-instance guard: Task Scheduler "Run" must not spawn a duplicate
ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\KaraFunControlHelper")
if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
    raise SystemExit(0)

KEYEVENTF_KEYUP = 0x0002
VK_MEDIA_PLAY_PAUSE = 0xB3
VK_MEDIA_NEXT_TRACK = 0xB0


def tap(vk: int) -> None:
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.05)
    ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


import subprocess

def find_karafun_hwnd():
    """Find KaraFun's main window by process ownership (window title varies)."""
    user32 = ctypes.windll.user32
    kf = set()
    out = subprocess.run(['tasklist', '/fi', 'imagename eq KaraFun.exe', '/fo', 'csv', '/nh'],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.strip('"').split('","')
        if len(parts) > 1 and parts[0].lower() == 'karafun.exe':
            try:
                kf.add(int(parts[1]))
            except ValueError:
                pass
    if not kf:
        return None
    result = []
    pid_out = ctypes.c_ulong()
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, lparam):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_out))
            if pid_out.value in kf:
                result.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return result[0] if result else None


def send_key_to_karafun(vk: int) -> bool:
    """Post a key press directly to KaraFun's window — no focus needed."""
    hwnd = find_karafun_hwnd()
    if not hwnd:
        return False
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    ctypes.windll.user32.PostMessageW(hwnd, WM_KEYDOWN, vk, 0)
    time.sleep(0.05)
    ctypes.windll.user32.PostMessageW(hwnd, WM_KEYUP, vk, 0)
    return True


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
        elif self.path == "/start":
            ok = send_key_to_karafun(0x20)  # VK_SPACE — KaraFun's start/play
            self._json({"ok": ok, "sent": "start" if ok else "no KaraFun window"})
        elif self.path == "/enter":
            ok = send_key_to_karafun(0x0D)  # VK_RETURN — alternative activation
            self._json({"ok": ok, "sent": "enter" if ok else "no KaraFun window"})
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
