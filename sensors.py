"""CPU・メモリ・AMD GPU の今の数値を読みます。"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

import psutil

ADL_MAX_SENSORS = 256
PMLOG_TEMPERATURE_EDGE = 8
PMLOG_INFO_ACTIVITY_GFX = 19
PMLOG_TEMPERATURE_HOTSPOT = 27
ADL_OK = 0


class SensorData(ctypes.Structure):
    _fields_ = [("supported", ctypes.c_int), ("value", ctypes.c_int)]


class PMLog(ctypes.Structure):
    _fields_ = [("size", ctypes.c_int), ("sensors", SensorData * ADL_MAX_SENSORS)]


class AdapterInfo(ctypes.Structure):
    _fields_ = [
        ("iSize", ctypes.c_int),
        ("iAdapterIndex", ctypes.c_int),
        ("strUDID", ctypes.c_char * 256),
        ("iBusNumber", ctypes.c_int),
        ("iDeviceNumber", ctypes.c_int),
        ("iFunctionNumber", ctypes.c_int),
        ("iVendorID", ctypes.c_int),
        ("strAdapterName", ctypes.c_char * 256),
        ("strDisplayName", ctypes.c_char * 256),
        ("iPresent", ctypes.c_int),
        ("iExist", ctypes.c_int),
        ("strDriverPath", ctypes.c_char * 256),
        ("strDriverPathExt", ctypes.c_char * 256),
        ("strPNPString", ctypes.c_char * 256),
        ("iOSDisplayIndex", ctypes.c_int),
    ]


@dataclass
class Sample:
    cpu_name: str
    gpu_name: str
    cpu_util: float | None
    cpu_temp: float | None
    memory_used_mb: float
    memory_total_mb: float
    memory_percent: float
    gpu_util: float | None
    gpu_temp: float | None
    gpu_hotspot: float | None


def cpu_name() -> str:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        ) as key:
            value, _kind = winreg.QueryValueEx(key, "ProcessorNameString")
        return str(value).strip()
    except OSError:
        return "CPU"


def cpu_temperature_available() -> bool:
    """起動時に一度だけ、Windows が CPU 温度を出しているか確認します。"""
    try:
        readings = psutil.sensors_temperatures()
    except Exception:
        return False
    if not readings:
        return False
    for entries in readings.values():
        for entry in entries:
            if entry.current is not None and entry.current > 0:
                return True
    return False


def read_cpu_temperature() -> float | None:
    """Windows が公開している温度があれば使います。

    Ryzen のパッケージ温度は、追加のドライバなしでは出てきません。
    ゲームのアンチチートとぶつかりやすいドライバは入れないようにしています。
    """
    try:
        readings = psutil.sensors_temperatures()
    except Exception:
        return None
    if not readings:
        return None
    for entries in readings.values():
        for entry in entries:
            if entry.current is not None and entry.current > 0:
                return float(entry.current)
    return None


def _c_string(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()


class AmdGpu:
    """AMD の公式ドライバ DLL（atiadlxx.dll）から稼働率と温度を読みます。"""

    def __init__(self) -> None:
        self.ok = False
        self.name = "AMD GPU"
        self.last_error = ""
        self._adl = None
        self._ctx = ctypes.c_void_p()
        self._alloc_cb = None
        self._adapter_index = 0
        self._open()

    def _open(self) -> None:
        dll_path = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "atiadlxx.dll"
        if not dll_path.exists():
            self.last_error = "AMD のドライバが見つからないため、GPU の数値は取れません。"
            return
        try:
            adl = ctypes.WinDLL(str(dll_path))
            alloc_type = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_int)
            kernel32 = ctypes.windll.kernel32
            kernel32.LocalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
            kernel32.LocalAlloc.restype = ctypes.c_void_p

            def alloc(size: int):
                if size <= 0:
                    return None
                return kernel32.LocalAlloc(0x0040, size)

            callback = alloc_type(alloc)
            adl.ADL2_Main_Control_Create.argtypes = [
                alloc_type,
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_void_p),
            ]
            adl.ADL2_Main_Control_Create.restype = ctypes.c_int
            adl.ADL2_Main_Control_Destroy.argtypes = [ctypes.c_void_p]
            adl.ADL2_Main_Control_Destroy.restype = ctypes.c_int
            adl.ADL2_Adapter_NumberOfAdapters_Get.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_int),
            ]
            adl.ADL2_Adapter_NumberOfAdapters_Get.restype = ctypes.c_int
            adl.ADL2_New_QueryPMLogData_Get.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.POINTER(PMLog),
            ]
            adl.ADL2_New_QueryPMLogData_Get.restype = ctypes.c_int
            adl.ADL2_Adapter_AdapterInfo_Get.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            adl.ADL2_Adapter_AdapterInfo_Get.restype = ctypes.c_int

            ctx = ctypes.c_void_p()
            created = adl.ADL2_Main_Control_Create(callback, 1, ctypes.byref(ctx))
            if created != ADL_OK or not ctx:
                self.last_error = f"GPU ドライバの初期化に失敗しました（コード {created}）。"
                return

            count = ctypes.c_int()
            counted = adl.ADL2_Adapter_NumberOfAdapters_Get(ctx, ctypes.byref(count))
            if counted != ADL_OK or count.value <= 0:
                adl.ADL2_Main_Control_Destroy(ctx)
                self.last_error = "GPU が見つかりませんでした。"
                return

            self._adl = adl
            self._ctx = ctx
            self._alloc_cb = callback
            self._adapter_index = self._choose_adapter(count.value)
            self._read_name(count.value)
            self.ok = True
        except OSError as error:
            self.last_error = f"GPU の読み取りを開始できませんでした: {error}"

    def _query(self, index: int) -> PMLog | None:
        if self._adl is None:
            return None
        log = PMLog()
        log.size = ctypes.sizeof(PMLog)
        result = self._adl.ADL2_New_QueryPMLogData_Get(self._ctx, index, ctypes.byref(log))
        if result != ADL_OK:
            return None
        return log

    def _choose_adapter(self, count: int) -> int:
        for index in range(count):
            log = self._query(index)
            if log is None:
                continue
            if log.sensors[PMLOG_TEMPERATURE_EDGE].supported:
                return index
        return 0

    def _read_name(self, count: int) -> None:
        if self._adl is None:
            return
        infos = (AdapterInfo * count)()
        for info in infos:
            info.iSize = ctypes.sizeof(AdapterInfo)
        result = self._adl.ADL2_Adapter_AdapterInfo_Get(
            self._ctx,
            ctypes.cast(infos, ctypes.c_void_p),
            ctypes.sizeof(AdapterInfo) * count,
        )
        if result != ADL_OK:
            return
        chosen = infos[min(self._adapter_index, count - 1)]
        name = _c_string(chosen.strAdapterName)
        if name:
            self.name = name

    def read(self) -> tuple[float | None, float | None, float | None]:
        log = self._query(self._adapter_index)
        if log is None:
            self.last_error = "GPU の数値を読み取れませんでした。"
            return None, None, None
        self.last_error = ""
        return (
            _percent(log.sensors[PMLOG_INFO_ACTIVITY_GFX]),
            _temperature(log.sensors[PMLOG_TEMPERATURE_EDGE]),
            _temperature(log.sensors[PMLOG_TEMPERATURE_HOTSPOT]),
        )

    def close(self) -> None:
        if self._adl is not None and self._ctx:
            self._adl.ADL2_Main_Control_Destroy(self._ctx)
            self._ctx = ctypes.c_void_p()


def _percent(sensor: SensorData) -> float | None:
    if not sensor.supported:
        return None
    value = sensor.value
    if value < 0:
        return None
    if value <= 100:
        return float(value)
    if value <= 10000:
        return value / 100.0
    return None


def _temperature(sensor: SensorData) -> float | None:
    if not sensor.supported:
        return None
    if sensor.value < 0 or sensor.value > 150:
        return None
    return float(sensor.value)


class SystemSensors:
    def __init__(self) -> None:
        self.cpu_name = cpu_name()
        self.gpu = AmdGpu()
        self.last_error = self.gpu.last_error
        self._cpu_temp_available = cpu_temperature_available()
        psutil.cpu_percent(interval=None)

    def read(self) -> Sample:
        cpu_util = psutil.cpu_percent(interval=None)
        memory = psutil.virtual_memory()
        used_bytes = memory.total - memory.available
        gpu_util = gpu_temp = gpu_hotspot = None
        if self.gpu.ok:
            try:
                gpu_util, gpu_temp, gpu_hotspot = self.gpu.read()
                self.last_error = self.gpu.last_error
            except Exception as error:
                self.last_error = f"GPU の読み取りでエラーが出ました: {error}"
        return Sample(
            cpu_name=self.cpu_name,
            gpu_name=self.gpu.name,
            cpu_util=round(cpu_util, 1),
            cpu_temp=read_cpu_temperature() if self._cpu_temp_available else None,
            memory_used_mb=round(used_bytes / (1024 * 1024), 1),
            memory_total_mb=round(memory.total / (1024 * 1024), 1),
            memory_percent=round(memory.percent, 1),
            gpu_util=None if gpu_util is None else round(gpu_util, 1),
            gpu_temp=gpu_temp,
            gpu_hotspot=gpu_hotspot,
        )

    def close(self) -> None:
        self.gpu.close()
