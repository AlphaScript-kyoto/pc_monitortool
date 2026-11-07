"""数値を見る窓と、記録の開始・停止を担当します。"""

from __future__ import annotations

import os
import sys
import time
import traceback
import winsound
from collections import deque
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import ttk

import psutil

from fps_tracker import IGNORED_PROCESSES, FpsTracker, presentmon_path
from hotkey import HOTKEYS, HotkeyService
from recorder import SessionRecorder
from sensors import Sample, SystemSensors

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
TICK_MS = 500
HISTORY_POINTS = 120

BG = "#16181d"
CARD = "#232733"
FG = "#e8eaed"
MUTED = "#9aa0a6"
LINE = "#343a4a"
ACCENT = "#7aa2f7"
REC = "#f7768e"
OK = "#9ece6a"

SERIES = [
    ("fps", "FPS", "#9ece6a", 240),
    ("gpu", "GPU%", "#e0af68", 100),
    ("gpu_temp", "GPU温度", "#f7768e", 100),
    ("cpu", "CPU%", "#7aa2f7", 100),
    ("mem", "メモリ%", "#bb9af7", 100),
]


def enable_dpi() -> None:
    try:
        ctypes_windll = __import__("ctypes").windll
        ctypes_windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            __import__("ctypes").windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def is_admin() -> bool:
    try:
        return bool(__import__("ctypes").windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    ctypes = __import__("ctypes")
    shell = ctypes.windll.shell32
    shell.ShellExecuteW.restype = ctypes.c_void_p
    script = str(Path(sys.argv[0]).resolve())
    import subprocess

    params = subprocess.list2cmdline([script, *sys.argv[1:]])
    result = shell.ShellExecuteW(None, "runas", sys.executable, params, str(ROOT), 1)
    if result and result > 32:
        os._exit(0)
    return False


def foreground_exe() -> str | None:
    ctypes = __import__("ctypes")
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None
    try:
        return psutil.Process(pid.value).name()
    except (psutil.Error, OSError):
        return None


def remember_error(text: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (LOG_DIR / "error.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now().isoformat(timespec='seconds')} {text}\n")
    except OSError:
        pass


class TrendChart(tk.Canvas):
    def __init__(self, parent: tk.Widget) -> None:
        super().__init__(parent, bg=CARD, height=200, highlightthickness=0)

    def render(self, history: deque[dict]) -> None:
        self.delete("all")
        width = max(self.winfo_width(), 20)
        height = max(self.winfo_height(), 20)
        left, right, top, bottom = 16, width - 12, 28, height - 16
        self.create_line(left, top, left, bottom, fill=LINE)
        self.create_line(left, bottom, right, bottom, fill=LINE)
        mid = (top + bottom) / 2
        self.create_line(left, mid, right, mid, fill=LINE)
        points = list(history)
        if len(points) >= 2:
            span = max(len(points) - 1, 1)
            for key, _label, color, scale in SERIES:
                previous = None
                for index, item in enumerate(points):
                    value = item.get(key)
                    if value is None:
                        previous = None
                        continue
                    x = left + (right - left) * (index / span)
                    ratio = min(max(float(value), 0.0), scale) / scale
                    y = bottom - ratio * (bottom - top)
                    if previous is not None:
                        self.create_line(*previous, x, y, fill=color, width=2)
                    previous = (x, y)
        legend_x = left
        for _key, label, color, _scale in SERIES:
            self.create_rectangle(legend_x, 8, legend_x + 12, 20, fill=color, outline=color)
            self.create_text(legend_x + 16, 14, text=label, anchor="w", fill=FG, font=("Yu Gothic UI", 9))
            legend_x += 110


class MonitorApp:
    def __init__(self) -> None:
        enable_dpi()
        self.sensors = SystemSensors()
        self.fps = FpsTracker()
        self.recorder: SessionRecorder | None = None
        self.recording = False
        self.target = ""
        self._closing = False
        self._toggle_lock = False
        self._last_write = 0.0
        self.history: deque[dict] = deque(maxlen=HISTORY_POINTS)
        self.hotkeys = HotkeyService(self._on_hotkey)

        self.root = tk.Tk()
        self.root.title("PCモニターツール")
        self.root.geometry("880x700")
        self.root.minsize(760, 640)
        self.root.configure(bg=BG)
        self._font = self._pick_font()
        self._build()
        self.hotkeys.set_vk(HOTKEYS["F11"])
        self.fps.boot(presentmon_path(ROOT))
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(TICK_MS, self._tick)

    def _pick_font(self) -> str:
        families = set(tkfont_families(self.root))
        for name in ("Yu Gothic UI", "Meiryo UI", "Meiryo", "Segoe UI"):
            if name in families:
                return name
        return "TkDefaultFont"

    def _build(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Dark.TCheckbutton", background=BG, foreground=FG)

        outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill="both", expand=True, padx=18, pady=14)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        header = tk.Frame(outer, bg=BG)
        header.grid(row=0, column=0, sticky="ew")
        tk.Label(
            header,
            text="PCモニターツール",
            bg=BG,
            fg=FG,
            font=(self._font, 20, "bold"),
        ).pack(side="left")
        self.lamp = tk.Canvas(header, width=16, height=16, bg=BG, highlightthickness=0)
        self.lamp.pack(side="right", padx=(8, 0))
        self._lamp_id = self.lamp.create_oval(2, 2, 14, 14, fill=MUTED, outline=MUTED)
        self.state_label = tk.Label(header, text="待機中", bg=BG, fg=MUTED, font=(self._font, 12))
        self.state_label.pack(side="right")

        machine = f"{self.sensors.cpu_name}    {self.sensors.gpu.name}"
        tk.Label(outer, text=machine, bg=BG, fg=MUTED, font=(self._font, 10), anchor="w").grid(
            row=1, column=0, sticky="ew", pady=(2, 10)
        )

        cards = tk.Frame(outer, bg=BG)
        cards.grid(row=2, column=0, sticky="ew")
        for column in range(3):
            cards.columnconfigure(column, weight=1)
        self.value_labels: dict[str, tk.Label] = {}
        self.sub_labels: dict[str, tk.Label] = {}
        titles = [
            ("fps", "FPS"),
            ("gpu", "GPU稼働率"),
            ("gpu_temp", "GPU温度"),
            ("cpu", "CPU稼働率"),
            ("cpu_temp", "CPU温度"),
            ("mem", "使用メモリ"),
        ]
        for index, (key, title) in enumerate(titles):
            value, sub = self._card(cards, title)
            value.master.grid(row=index // 3, column=index % 3, sticky="nsew", padx=4, pady=4)
            self.value_labels[key] = value
            self.sub_labels[key] = sub

        chart_box = tk.Frame(outer, bg=CARD)
        chart_box.grid(row=3, column=0, sticky="nsew", pady=(10, 8))
        tk.Label(
            chart_box,
            text="直近60秒の動き    FPSは240、温度は100℃がグラフの一番上です",
            bg=CARD,
            fg=MUTED,
            font=(self._font, 9),
            anchor="w",
        ).pack(fill="x", padx=12, pady=(8, 0))
        self.chart = TrendChart(chart_box)
        self.chart.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        controls = tk.Frame(outer, bg=BG)
        controls.grid(row=4, column=0, sticky="ew", pady=(4, 0))
        self.record_button = tk.Button(
            controls,
            text="記録を開始",
            command=self.toggle_recording,
            bg=ACCENT,
            fg="#101218",
            activebackground="#9ab6ff",
            activeforeground="#101218",
            relief="flat",
            padx=16,
            pady=8,
            font=(self._font, 12, "bold"),
        )
        self.record_button.pack(side="left")

        self.format_var = tk.StringVar(value="CSV")
        self.interval_var = tk.StringVar(value="1秒")
        self.hotkey_var = tk.StringVar(value="F11")
        self.top_var = tk.BooleanVar(value=True)
        self.sound_var = tk.BooleanVar(value=True)
        self._option(controls, "形式", self.format_var, ["CSV", "JSON"])
        self._option(controls, "間隔", self.interval_var, ["0.5秒", "1秒", "2秒"])
        self._option(controls, "キー", self.hotkey_var, list(HOTKEYS), self._change_hotkey)
        ttk.Checkbutton(
            controls,
            text="常に手前",
            variable=self.top_var,
            command=self._apply_topmost,
            style="Dark.TCheckbutton",
        ).pack(side="left", padx=(12, 0))
        ttk.Checkbutton(
            controls,
            text="音",
            variable=self.sound_var,
            style="Dark.TCheckbutton",
        ).pack(side="left", padx=(8, 0))
        self._apply_topmost()

        footer = tk.Frame(outer, bg=BG)
        footer.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        self.path_var = tk.StringVar(value="まだ記録ファイルはありません。logs フォルダに保存されます。")
        tk.Label(
            footer,
            textvariable=self.path_var,
            bg=BG,
            fg=MUTED,
            font=(self._font, 9),
            anchor="w",
            justify="left",
        ).pack(side="left", fill="x", expand=True)
        tk.Button(
            footer,
            text="フォルダを開く",
            command=self.open_logs,
            bg=CARD,
            fg=FG,
            relief="flat",
            padx=10,
            pady=4,
            font=(self._font, 9),
        ).pack(side="right")

        self.hint_var = tk.StringVar(value=self._hint_text())
        tk.Label(
            outer,
            textvariable=self.hint_var,
            bg=BG,
            fg=MUTED,
            font=(self._font, 9),
            anchor="w",
            justify="left",
        ).grid(row=6, column=0, sticky="ew", pady=(8, 0))

        if not is_admin():
            tk.Button(
                outer,
                text="管理者として再起動（FPSが取れないゲーム用）",
                command=self.become_admin,
                bg=CARD,
                fg=FG,
                relief="flat",
                padx=10,
                pady=6,
                font=(self._font, 9),
            ).grid(row=7, column=0, sticky="w", pady=(8, 0))

    def _card(self, parent: tk.Widget, title: str) -> tuple[tk.Label, tk.Label]:
        frame = tk.Frame(parent, bg=CARD, padx=14, pady=10)
        tk.Label(frame, text=title, bg=CARD, fg=MUTED, font=(self._font, 10)).pack(anchor="w")
        value = tk.Label(frame, text="--", bg=CARD, fg=FG, font=(self._font, 26, "bold"))
        value.pack(anchor="w", pady=(2, 0))
        sub = tk.Label(frame, text="", bg=CARD, fg=MUTED, font=(self._font, 9))
        sub.pack(anchor="w")
        return value, sub

    def _option(self, parent, label, variable, values, command=None) -> None:
        tk.Label(parent, text=label, bg=BG, fg=MUTED, font=(self._font, 10)).pack(side="left", padx=(14, 4))
        menu = tk.OptionMenu(parent, variable, *values, command=command if command else (lambda _value: None))
        menu.configure(
            bg=CARD,
            fg=FG,
            activebackground="#2f3548",
            activeforeground=FG,
            relief="flat",
            highlightthickness=1,
            highlightbackground=LINE,
            font=(self._font, 10),
        )
        menu["menu"].configure(bg=CARD, fg=FG, activebackground=ACCENT, activeforeground="#101218", font=(self._font, 10))
        menu.pack(side="left")

    def _hint_text(self) -> str:
        key = self.hotkey_var.get()
        text = f"{key} で記録の開始と停止が切り替わります。開始は高い音が2回、停止は低い音が1回です。"
        if not is_admin():
            text += " 管理者ではないため、一部のゲームは FPS が空になることがあります。"
        return text

    def _apply_topmost(self) -> None:
        self.root.attributes("-topmost", bool(self.top_var.get()))

    def _change_hotkey(self, _value=None) -> None:
        name = self.hotkey_var.get()
        vk = HOTKEYS.get(name)
        if vk is None:
            return
        self.hotkeys.set_vk(vk)
        self.hint_var.set(self._hint_text())

    def _on_hotkey(self) -> None:
        if self._closing:
            return
        self.root.after(0, self.toggle_recording)

    def toggle_recording(self) -> None:
        if self._toggle_lock or self._closing:
            return
        self._toggle_lock = True
        try:
            if self.recording:
                self.stop_recording()
            else:
                self.start_recording()
        finally:
            self.root.after(350, self._unlock)

    def _unlock(self) -> None:
        self._toggle_lock = False

    def start_recording(self) -> None:
        target = self._choose_target()
        kind = "csv" if self.format_var.get() == "CSV" else "json"
        try:
            self.recorder = SessionRecorder(LOG_DIR, kind)
        except OSError as error:
            self.path_var.set(f"ファイルを作れませんでした: {error}")
            remember_error(str(error))
            return
        self.recording = True
        self.target = target
        self._last_write = 0.0
        self.record_button.configure(text="記録を停止", bg=REC, activebackground="#ff9aab")
        self.root.title("記録中 - PCモニターツール")
        self._beep_start()
        target_text = target if target else "未確定（ハードウェア数値のみ）"
        self.path_var.set(f"記録中: {self.recorder.path.name}    対象: {target_text}")

    def stop_recording(self, beep: bool = True) -> None:
        recorder = self.recorder
        self.recording = False
        self.recorder = None
        if recorder is not None:
            count = recorder.count
            recorder.close()
            self.path_var.set(f"保存しました（{count}件）: {recorder.path}")
        self.record_button.configure(text="記録を開始", bg=ACCENT, activebackground="#9ab6ff")
        self.root.title("PCモニターツール")
        self.state_label.configure(text="待機中", fg=MUTED)
        self.lamp.itemconfigure(self._lamp_id, fill=MUTED, outline=MUTED)
        if beep:
            self._beep_stop()

    def _choose_target(self) -> str:
        current = foreground_exe()
        if current and current.lower() not in IGNORED_PROCESSES:
            return current
        return self.fps.busiest_name()

    def _interval_seconds(self) -> float:
        text = self.interval_var.get().replace("秒", "")
        try:
            return max(float(text), 0.2)
        except ValueError:
            return 1.0

    def _tick(self) -> None:
        if self._closing:
            return
        try:
            self._update()
        except Exception:
            detail = traceback.format_exc()
            remember_error(detail)
            self.path_var.set("更新中にエラーが出ました。logs/error.log を確認できます。")
        self.root.after(TICK_MS, self._tick)

    def _update(self) -> None:
        sample = self.sensors.read()
        shown_target = self.target if self.recording else self._choose_target()
        fps = self.fps.fps_for(shown_target) if shown_target else None
        self._show(sample, fps, shown_target)
        self.history.append(
            {
                "fps": None if fps is None else fps.displayed,
                "gpu": sample.gpu_util,
                "gpu_temp": sample.gpu_temp,
                "cpu": sample.cpu_util,
                "mem": sample.memory_percent,
            }
        )
        self.chart.render(self.history)
        hotkey_error = self.hotkeys.error()
        self.hint_var.set(hotkey_error or self._hint_text())
        if self.recording and self.recorder is not None:
            now = time.monotonic()
            if now - self._last_write >= self._interval_seconds():
                self.recorder.write(self._row(sample, fps))
                self._last_write = now
                self.state_label.configure(text=f"記録中  {self.recorder.count}件", fg=REC)
                self.path_var.set(
                    f"記録中: {self.recorder.path.name}    対象: {self.target or '未確定'}"
                )
            self.lamp.itemconfigure(self._lamp_id, fill=REC, outline=REC)
        else:
            self.state_label.configure(text="待機中", fg=OK if self.fps.ready else MUTED)
            self.lamp.itemconfigure(self._lamp_id, fill=OK if self.fps.ready else MUTED, outline=OK if self.fps.ready else MUTED)

    def _show(self, sample: Sample, fps, target: str) -> None:
        if fps is None:
            fps_text = "--"
        else:
            fps_text = fps.label()
        fps_size = 20 if "/" in fps_text else 26
        self.value_labels["fps"].configure(text=fps_text, font=(self._font, fps_size, "bold"))
        if not self.fps.ready:
            fps_sub = self.fps.status
        elif fps is not None and fps.frame_gen:
            fps_sub = f"表示 / 生成前    {target}" if target else "表示 / 生成前"
        elif target:
            fps_sub = f"対象: {target}"
        else:
            fps_sub = "対象ソフトを探しています"
        self.sub_labels["fps"].configure(text=fps_sub)

        self.value_labels["gpu"].configure(text=blank(sample.gpu_util, "%"))
        self.sub_labels["gpu"].configure(text=sample.gpu_name)

        self.value_labels["gpu_temp"].configure(text=blank(sample.gpu_temp, "℃"))
        if sample.gpu_hotspot is None:
            hot_text = self.sensors.last_error or ""
        else:
            hot_text = f"ホットスポット {sample.gpu_hotspot:.0f}℃"
        self.sub_labels["gpu_temp"].configure(text=hot_text)

        self.value_labels["cpu"].configure(text=blank(sample.cpu_util, "%"))
        self.sub_labels["cpu"].configure(text="全コアの平均")

        if sample.cpu_temp is None:
            self.value_labels["cpu_temp"].configure(text="取得不可", font=(self._font, 18, "bold"))
            self.sub_labels["cpu_temp"].configure(text="追加ドライバなしでは非公開です")
        else:
            self.value_labels["cpu_temp"].configure(text=f"{sample.cpu_temp:.0f}℃", font=(self._font, 26, "bold"))
            self.sub_labels["cpu_temp"].configure(text="CPU温度")

        used_gb = sample.memory_used_mb / 1024
        total_gb = sample.memory_total_mb / 1024
        self.value_labels["mem"].configure(text=f"{used_gb:.1f} GB")
        self.sub_labels["mem"].configure(text=f"/ {total_gb:.1f} GB（{sample.memory_percent:.0f}%）")

    def _row(self, sample: Sample, fps) -> dict:
        return {
            "時刻": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "対象ソフト": self.target,
            "FPS": None if fps is None else fps.displayed,
            "生成前FPS": None if fps is None else fps.base,
            "GPU稼働率": sample.gpu_util,
            "GPU温度": sample.gpu_temp,
            "GPUホットスポット": sample.gpu_hotspot,
            "CPU稼働率": sample.cpu_util,
            "CPU温度": sample.cpu_temp,
            "使用メモリMB": round(sample.memory_used_mb),
            "メモリ使用率": sample.memory_percent,
        }

    def _beep_start(self) -> None:
        if not self.sound_var.get():
            return
        try:
            winsound.Beep(880, 90)
            winsound.Beep(1180, 90)
        except RuntimeError:
            pass

    def _beep_stop(self) -> None:
        if not self.sound_var.get():
            return
        try:
            winsound.Beep(520, 140)
        except RuntimeError:
            pass

    def open_logs(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(LOG_DIR)

    def become_admin(self) -> None:
        if not relaunch_as_admin():
            self.path_var.set("管理者としての起動はキャンセルされました。このまま使えます。")

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self.recording:
            self.stop_recording(beep=False)
        self.hotkeys.close()
        self.fps.stop()
        self.sensors.close()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def tkfont_families(root: tk.Tk) -> tuple[str, ...]:
    from tkinter import font

    return font.families(root)


def blank(value: float | None, suffix: str) -> str:
    if value is None:
        return "--"
    if float(value).is_integer():
        return f"{value:.0f}{suffix}"
    return f"{value:.1f}{suffix}"


def build_row_for_test(sample: Sample) -> dict:
    """画面を開かずに、記録1行の形を確認するための関数です。"""
    return {
        "時刻": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "対象ソフト": "selftest",
        "FPS": None,
        "生成前FPS": None,
        "GPU稼働率": sample.gpu_util,
        "GPU温度": sample.gpu_temp,
        "GPUホットスポット": sample.gpu_hotspot,
        "CPU稼働率": sample.cpu_util,
        "CPU温度": sample.cpu_temp,
        "使用メモリMB": round(sample.memory_used_mb),
        "メモリ使用率": sample.memory_percent,
    }

