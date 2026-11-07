"""ゲーム中でも届く、全体向けのホットキーです。"""

from __future__ import annotations

import ctypes
import queue
import threading
from ctypes import wintypes

user32 = ctypes.windll.user32

WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000
QS_ALLINPUT = 0x04FF

HOTKEYS: dict[str, int] = {
    "F8": 0x77,
    "F9": 0x78,
    "F10": 0x79,
    "F11": 0x7A,
    "F12": 0x7B,
}


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.PeekMessageW.argtypes = [
    ctypes.POINTER(MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
    wintypes.UINT,
]
user32.PeekMessageW.restype = wintypes.BOOL
user32.MsgWaitForMultipleObjects.argtypes = [
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.BOOL,
    wintypes.DWORD,
    wintypes.DWORD,
]
user32.MsgWaitForMultipleObjects.restype = wintypes.DWORD


class HotkeyService:
    """F8〜F12 のどれか1つを、ほかのウィンドウが前面でも受け取ります。"""

    def __init__(self, on_press) -> None:
        self._on_press = on_press
        self._keys: queue.Queue[int] = queue.Queue()
        self._stop = threading.Event()
        self._error = ""
        self._error_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="hotkey", daemon=True)
        self._thread.start()

    def set_vk(self, vk: int) -> None:
        self._keys.put(vk)

    def error(self) -> str:
        with self._error_lock:
            return self._error

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.5)

    def _set_error(self, text: str) -> None:
        with self._error_lock:
            self._error = text

    def _run(self) -> None:
        current: int | None = None
        while not self._stop.is_set():
            current = self._apply_pending_key(current)
            user32.MsgWaitForMultipleObjects(0, None, False, 200, QS_ALLINPUT)
            message = MSG()
            while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                if message.message == WM_HOTKEY:
                    try:
                        self._on_press()
                    except Exception:
                        pass
        if current is not None:
            user32.UnregisterHotKey(None, 1)

    def _apply_pending_key(self, current: int | None) -> int | None:
        latest: int | None = None
        while True:
            try:
                latest = self._keys.get_nowait()
            except queue.Empty:
                break
        if latest is None or latest == current:
            return current
        if current is not None:
            user32.UnregisterHotKey(None, 1)
        if not user32.RegisterHotKey(None, 1, MOD_NOREPEAT, latest):
            self._set_error("このキーはほかのソフトが使用中です。別のキーを選んでください。")
            return None
        self._set_error("")
        return latest
