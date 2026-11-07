"""ゲームが画面へ描画した回数から、直近1秒の FPS を計算します。

計測そのものは Intel の PresentMon に任せています。
このファイルは、その出力から「今の FPS」だけを拾います。
"""

from __future__ import annotations

import csv
import ctypes
import hashlib
import shutil
import subprocess
import threading
import time
import urllib.request
from collections import deque
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

PRESENTMON_NAME = "PresentMon-2.4.1-x64.exe"
PRESENTMON_URL = (
    "https://github.com/GameTechDev/PresentMon/releases/download/v2.4.1/"
    + PRESENTMON_NAME
)
# Intel の署名が付いた PresentMon 2.4.1 x64 の SHA-256 です。
PRESENTMON_SHA256 = "d74183e7ae630f72cd3690be0373ecbfdc6cbb86578148aab8fa2a7166068f34"

# ゲームそのものではない、よく画面に出るソフトです。
IGNORED_PROCESSES = {
    "dwm.exe",
    "explorer.exe",
    "searchhost.exe",
    "applicationframehost.exe",
    "textinputhost.exe",
    "shellexperiencehost.exe",
    "systemsettings.exe",
    "startmenuexperiencehost.exe",
    "lockapp.exe",
    "gamebar.exe",
    "gamebarftserver.exe",
    "widgets.exe",
    "widgetservice.exe",
    "cursor.exe",
    "code.exe",
    "windowsterminal.exe",
    "wt.exe",
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "conhost.exe",
    "sihost.exe",
    "runtimebroker.exe",
    "taskhostw.exe",
    "ctfmon.exe",
    "python.exe",
    "pythonw.exe",
    "py.exe",
    "steam.exe",
    "steamwebhelper.exe",
    "discord.exe",
}


def presentmon_path(root: Path) -> Path:
    return root / "tools" / PRESENTMON_NAME


def ensure_presentmon(path: Path) -> Path:
    if _trusted_presentmon(path):
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".part")
    request = urllib.request.Request(
        PRESENTMON_URL,
        headers={"User-Agent": "pc-monitortool"},
    )
    with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as handle:
        shutil.copyfileobj(response, handle)
    if not _trusted_presentmon(temporary):
        temporary.unlink(missing_ok=True)
        raise RuntimeError("FPS 計測用の PresentMon を確認できませんでした。")
    temporary.replace(path)
    return path


def _tie_child_to_parent(process: subprocess.Popen) -> None:
    """親のツールが落ちたとき、FPS計測も一緒に終了するようにします。"""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    kernel32.SetInformationJobObject.restype = ctypes.c_int
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [("ReadOperationCount", ctypes.c_uint64),
                    ("WriteOperationCount", ctypes.c_uint64),
                    ("OtherOperationCount", ctypes.c_uint64),
                    ("ReadTransferCount", ctypes.c_uint64),
                    ("WriteTransferCount", ctypes.c_uint64),
                    ("OtherTransferCount", ctypes.c_uint64)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return
    info = ExtendedLimits()
    info.BasicLimitInformation.LimitFlags = 0x2000
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        return
    kernel32.AssignProcessToJobObject(job, process._handle)
    process._pc_monitor_job = job  # type: ignore[attr-defined]


def _live_csv_path(exe: Path) -> Path:
    return exe.parent.parent / "logs" / "_fps_live.csv"


def _release_trace_session() -> None:
    """前回の計測が残っていると、新しいFPSが届かなくなります。"""
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(
        ["logman", "stop", "PCMonitorTool", "-ets"],
        capture_output=True,
        creationflags=creationflags,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _trusted_presentmon(path: Path) -> bool:
    """公式の SHA-256 と、Intel のデジタル署名が両方そろっているときだけ使います。"""
    if not path.is_file():
        return False
    try:
        if _sha256(path) != PRESENTMON_SHA256:
            return False
    except OSError:
        return False
    return _authenticode_valid(path)


def _authenticode_valid(path: Path) -> bool:
    class Guid(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class TrustFile(ctypes.Structure):
        _fields_ = [
            ("cbStruct", ctypes.c_ulong),
            ("pcwszFilePath", ctypes.c_wchar_p),
            ("hFile", ctypes.c_void_p),
            ("pgKnownSubject", ctypes.c_void_p),
        ]

    class TrustData(ctypes.Structure):
        _fields_ = [
            ("cbStruct", ctypes.c_ulong),
            ("pPolicyCallbackData", ctypes.c_void_p),
            ("pSIPClientData", ctypes.c_void_p),
            ("dwUIChoice", ctypes.c_ulong),
            ("fdwRevocationChecks", ctypes.c_ulong),
            ("dwUnionChoice", ctypes.c_ulong),
            ("pFile", ctypes.c_void_p),
            ("dwStateAction", ctypes.c_ulong),
            ("hWVTStateData", ctypes.c_void_p),
            ("pwszURLReference", ctypes.c_wchar_p),
            ("dwProvFlags", ctypes.c_ulong),
            ("dwUIContext", ctypes.c_ulong),
            ("pSignatureSettings", ctypes.c_void_p),
        ]

    guid = Guid(
        0x00AAC56B,
        0xCD44,
        0x11D0,
        (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE),
    )
    info = TrustFile(ctypes.sizeof(TrustFile), str(path.resolve()), None, None)
    data = TrustData()
    data.cbStruct = ctypes.sizeof(TrustData)
    data.dwUIChoice = 2
    data.fdwRevocationChecks = 0
    data.dwUnionChoice = 1
    data.pFile = ctypes.cast(ctypes.pointer(info), ctypes.c_void_p)
    data.dwStateAction = 1
    data.dwProvFlags = 0x10
    verify = ctypes.WinDLL("wintrust").WinVerifyTrust
    verify.argtypes = [wintypes.HWND, ctypes.POINTER(Guid), ctypes.POINTER(TrustData)]
    verify.restype = ctypes.c_long
    result = verify(None, ctypes.byref(guid), ctypes.byref(data))
    data.dwStateAction = 2
    verify(None, ctypes.byref(guid), ctypes.byref(data))
    return result == 0


@dataclass(frozen=True)
class FpsReading:
    """displayed は画面に出たFPS、rendered は描画したFPS、base はフレーム生成前のFPSです。"""

    displayed: float
    base: float | None = None
    frame_gen: bool = False
    rendered: float | None = None

    def label(self) -> str:
        if self.frame_gen and self.base is not None:
            return f"{self.displayed:.0f}/{self.base:.0f}"
        return f"{self.displayed:.0f}"


@dataclass
class _Frame:
    arrived: float
    present_ms: float | None
    display_ms: float | None
    app_ms: float | None
    generated: bool


class _ProcessFrames:
    def __init__(self, name: str) -> None:
        self.name = name
        self.frames: deque[_Frame] = deque()


def _optional_ms(columns: list[str], index: int | None) -> float | None:
    if index is None or index >= len(columns):
        return None
    text = columns[index].strip()
    if not text or text.upper() == "NA":
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if value < 0.05 or value > 5000:
        return None
    return value


def _is_generated_frame(frame_type: str) -> bool:
    text = frame_type.strip().lower().replace("_", " ").replace("-", " ")
    if text in {"", "na", "application", "notset", "unspecified", "unknown", "none"}:
        return False
    markers = ("afmf", "xefg", "xess", "frame gen", "framegen", "generated", "dlss", "fsr")
    return any(marker in text for marker in markers)


def _mean_fps(intervals: list[float]) -> float | None:
    if not intervals:
        return None
    average = sum(intervals) / len(intervals)
    if average <= 0:
        return None
    return 1000.0 / average


def _generation_ratio(displayed: float, base: float) -> bool:
    """表示FPSが描画FPSのおよそ2倍・3倍・4倍なら、フレーム生成とみなします。"""
    if base < 20 or displayed < 30:
        return False
    ratio = displayed / base
    return any(abs(ratio - target) <= 0.35 for target in (2.0, 3.0, 4.0))


def summarize_frames(frames: list[_Frame]) -> FpsReading | None:
    displayed_frames = [frame for frame in frames if frame.display_ms is not None]
    if len(displayed_frames) < 2:
        return None
    displayed = _mean_fps([frame.display_ms for frame in displayed_frames if frame.display_ms is not None])
    rendered = _mean_fps([frame.present_ms for frame in frames if frame.present_ms is not None])
    app_intervals = [
        frame.app_ms
        for frame in displayed_frames
        if frame.app_ms is not None and not frame.generated
    ]
    base = _mean_fps([value for value in app_intervals if value is not None])
    if displayed is None:
        return None
    generated_count = sum(1 for frame in displayed_frames if frame.generated)
    generated_ratio = generated_count / len(displayed_frames)
    if base is None and generated_ratio >= 0.2:
        base = displayed * (1.0 - generated_ratio)
    frame_gen = False
    if base is not None and generated_ratio >= 0.2 and displayed > base * 1.4:
        frame_gen = True
    elif base is not None and _generation_ratio(displayed, base):
        frame_gen = True
    if not frame_gen:
        base = None
    shown = round(displayed, 1)
    native = None if base is None else round(base, 1)
    drawn = None if rendered is None else round(rendered, 1)
    return FpsReading(displayed=shown, base=native, frame_gen=frame_gen, rendered=drawn)


class FpsTracker:
    def __init__(self) -> None:
        self.status = "停止中"
        self.message = ""
        self.needs_admin = False
        self.ready = False
        self._running = False
        self._closed = False
        self._live_csv: Path | None = None
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._exe: Path | None = None
        self._generation = 0
        self._restarts = 0
        self._lock = threading.Lock()
        self._frames: dict[tuple[str, int], _ProcessFrames] = {}

    def boot(self, exe: Path) -> None:
        thread = threading.Thread(target=self._boot, args=(exe,), name="fps-boot", daemon=True)
        thread.start()

    def _boot(self, exe: Path) -> None:
        try:
            self.status = "FPS計測の準備をしています"
            ensure_presentmon(exe)
            if self._closed:
                return
            self.start(exe)
        except Exception as error:
            self.status = "FPSを開始できませんでした"
            self.message = str(error)

    def start(self, exe: Path) -> None:
        if self._closed or self._running:
            return
        self._restarts = 0
        self._launch(exe)

    def _launch(self, exe: Path) -> None:
        if self._closed:
            return
        if not exe.exists():
            raise FileNotFoundError(f"PresentMon が見つかりません: {exe}")
        self._exe = exe
        self._generation += 1
        generation = self._generation
        self._shutdown_child()
        _release_trace_session()
        self._running = True
        self.ready = False
        self.status = "FPS計測を起動しています"
        command = [
            str(exe),
            "--output_stdout",
            "--no_console_stats",
            "--no_track_input",
            "--no_track_gpu",
            "--track_frame_type",
            "--track_app_timing",
            "--set_circular_buffer_size",
            "8192",
            "--session_name",
            "PCMonitorTool",
            "--stop_existing_session",
            "--exclude",
            "dwm.exe",
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if self._closed:
            self._running = False
            return
        self._proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
        )
        if self._closed:
            self._shutdown_child()
            self._running = False
            self.ready = False
            return
        _tie_child_to_parent(self._proc)
        self._thread = threading.Thread(target=self._read_output, name="fps-read", daemon=True)
        self._thread.start()
        threading.Thread(target=self._read_errors, name="fps-err", daemon=True).start()
        threading.Thread(
            target=self._watch_ready,
            args=(generation,),
            name="fps-watch",
            daemon=True,
        ).start()

    def _watch_ready(self, generation: int) -> None:
        time.sleep(5)
        if self._closed or generation != self._generation or not self._running or self.ready:
            return
        if self._restarts >= 1:
            self.status = "FPS計測を起動しています"
            return
        self._restarts += 1
        self.status = "FPS計測を起動しています"
        exe = self._exe
        if exe is not None:
            self._launch(exe)

    def _shutdown_child(self) -> None:
        process = self._proc
        thread = self._thread
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._proc = None
        self._thread = None

    def stop(self) -> None:
        self._closed = True
        self._running = False
        self._generation += 1
        self._shutdown_child()
        _release_trace_session()
        self.ready = False
        if self.status != "FPSを開始できませんでした":
            self.status = "停止中"

    def fps_for(self, process_name: str) -> FpsReading | None:
        if not process_name:
            return None
        target = process_name.lower()
        now = time.monotonic()
        best: FpsReading | None = None
        with self._lock:
            for (name, _pid), bucket in list(self._frames.items()):
                reading = self._reading(bucket, now)
                if reading is None:
                    continue
                if name == target and (best is None or reading.displayed > best.displayed):
                    best = reading
            self._drop_empty()
        return best

    def busiest_name(self) -> str:
        now = time.monotonic()
        best_name = ""
        best_fps = 0.0
        with self._lock:
            for (name, _pid), bucket in list(self._frames.items()):
                if name in IGNORED_PROCESSES:
                    continue
                reading = self._reading(bucket, now)
                if reading is not None and reading.displayed > best_fps:
                    best_fps = reading.displayed
                    best_name = bucket.name
            self._drop_empty()
        return best_name

    def _reading(self, bucket: _ProcessFrames, now: float) -> FpsReading | None:
        frames = bucket.frames
        cutoff = now - 1.0
        while frames and frames[0].arrived < cutoff:
            frames.popleft()
        return summarize_frames(list(frames))

    def _drop_empty(self) -> None:
        empty = [key for key, bucket in self._frames.items() if not bucket.frames]
        for key in empty:
            del self._frames[key]

    def _read_errors(self) -> None:
        process = self._proc
        if process is None or process.stderr is None:
            return
        collected: list[str] = []
        for raw in process.stderr:
            text = raw.decode("utf-8", "replace").strip()
            if text:
                collected.append(text)
        message = " ".join(collected)
        lowered = message.lower()
        if "elevated privilege" in lowered or "administrator" in lowered:
            self.needs_admin = True
        elif message:
            self.message = message[:300]

    def _read_output(self) -> None:
        generation = self._generation
        process = self._proc
        if process is None or process.stdout is None:
            return
        app_index = present_index = pid_index = None
        display_index: int | None = None
        app_ms_index: int | None = None
        type_index: int | None = None
        for raw in process.stdout:
            if not self._running or generation != self._generation:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                columns = next(csv.reader([line]))
            except csv.Error:
                continue
            if app_index is None:
                if "Application" not in columns or "MsBetweenPresents" not in columns:
                    continue
                app_index = columns.index("Application")
                present_index = columns.index("MsBetweenPresents")
                pid_index = columns.index("ProcessID")
                display_index = columns.index("MsBetweenDisplayChange") if "MsBetweenDisplayChange" in columns else None
                app_ms_index = columns.index("MsBetweenAppStart") if "MsBetweenAppStart" in columns else None
                type_index = columns.index("FrameType") if "FrameType" in columns else None
                if generation == self._generation:
                    self.ready = True
                    self.status = "計測中"
                continue
            assert present_index is not None and pid_index is not None
            if len(columns) <= max(app_index, present_index, pid_index):
                continue
            name = columns[app_index].strip()
            if not name or name == "<unknown>":
                continue
            try:
                pid = int(columns[pid_index])
            except ValueError:
                continue
            present_ms = _optional_ms(columns, present_index)
            display_ms = _optional_ms(columns, display_index)
            if present_ms is None and display_ms is None:
                continue
            frame_type = columns[type_index].strip() if type_index is not None and type_index < len(columns) else ""
            sample = _Frame(
                arrived=time.monotonic(),
                present_ms=present_ms,
                display_ms=display_ms,
                app_ms=_optional_ms(columns, app_ms_index),
                generated=_is_generated_frame(frame_type),
            )
            key = (name.lower(), pid)
            with self._lock:
                bucket = self._frames.get(key)
                if bucket is None:
                    bucket = _ProcessFrames(name)
                    self._frames[key] = bucket
                bucket.frames.append(sample)
        if generation == self._generation and self._running and process.poll() not in (None, 0):
            self.status = "FPS計測が停止しました"
            self.ready = False
