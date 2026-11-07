"""PCモニターツールの入口です。ターミナルでは `python main.py` と実行します。"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run_selftest() -> int:
    from app import build_row_for_test
    from fps_tracker import FpsTracker, presentmon_path
    from recorder import SessionRecorder
    from sensors import SystemSensors

    sensors = SystemSensors()
    try:
        time.sleep(0.4)
        sample = sensors.read()
        print(f"CPU: {sample.cpu_name}")
        print(f"GPU: {sample.gpu_name}")
        print(
            "util "
            f"cpu={sample.cpu_util}% gpu={sample.gpu_util}% "
            f"edge={sample.gpu_temp} hotspot={sample.gpu_hotspot} "
            f"cpu_temp={sample.cpu_temp} mem={sample.memory_used_mb}MB"
        )
        if sensors.last_error:
            print(f"sensor_message: {sensors.last_error}")

        row = build_row_for_test(sample)
        folder = ROOT / "logs"
        csv_rec = SessionRecorder(folder, "csv")
        csv_rec.write(row)
        csv_rec.close()
        json_rec = SessionRecorder(folder, "json")
        json_rec.write(row)
        json_rec.close()
        print("--- csv ---")
        print(csv_rec.path.read_text(encoding="utf-8-sig").strip())
        print("--- jsonl ---")
        print(json_rec.path.read_text(encoding="utf-8").strip())
        csv_rec.path.unlink(missing_ok=True)
        json_rec.path.unlink(missing_ok=True)

        tracker = FpsTracker()
        tracker.start(presentmon_path(ROOT))
        time.sleep(4)
        busiest = tracker.busiest_name()
        fps_ready = tracker.ready
        fps_value = tracker.fps_for(busiest)
        print(
            f"fps status={tracker.status} ready={fps_ready} "
            f"busiest={busiest} fps={None if fps_value is None else fps_value.label()} "
            f"needs_admin={tracker.needs_admin}"
        )
        if tracker.message:
            print(f"fps_message: {tracker.message}")
        tracker.stop()
    finally:
        sensors.close()

    if sample.gpu_temp is None or sample.gpu_util is None:
        print("SELFTEST_GPU_FAILED")
        return 1
    if not fps_ready:
        print("SELFTEST_FPS_FAILED")
        return 1
    print("SELFTEST_OK")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="FPS・GPU・CPU・メモリを表示して記録します。")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="画面を開かず、数値を1回読めるか確認します。",
    )
    args = parser.parse_args()
    if args.selftest:
        raise SystemExit(run_selftest())

    from app import MonitorApp

    MonitorApp().run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        traceback.print_exc()
        if "--selftest" not in sys.argv:
            try:
                input("エラーが出ました。上の内容を確認して Enter キーを押してください...")
            except EOFError:
                pass
        raise SystemExit(1)
