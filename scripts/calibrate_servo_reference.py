import argparse
import json
from pathlib import Path
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import ServoConfig
from app.feeder import ServoFeederBackend


def hold_reference(backend, seconds, sleep_fn=time.sleep):
    if type(seconds) is not int or not 1 <= seconds <= 5:
        raise ValueError("Calibration hold must be 1 to 5 seconds")
    try:
        result = backend.initialize()
        if not result.ok:
            return result.model_dump(mode="json")
        sleep_fn(seconds)
        return {"ok": True, "pulse_us": backend.config.closed_pulse_us, "mechanical_reference_confirmed": False}
    finally:
        backend.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Apply one bounded SG90 reference pulse; no feeding or counter changes")
    parser.add_argument("--controller", required=True)
    parser.add_argument("--pulse-us", type=int, required=True)
    parser.add_argument("--seconds", type=int, choices=range(1, 6), default=3)
    parser.add_argument("--confirm-move", action="store_true")
    args = parser.parse_args(argv)
    if not args.confirm_move:
        parser.error("Explicit --confirm-move is required; servo will move")
    if not 1000 <= args.pulse_us <= 2000:
        parser.error("Use a pulse within the conservative 1000 to 2000us range")
    active = subprocess.run(["systemctl", "is-active", "pet-feeder-ai-demo.service"], capture_output=True, text=True, timeout=5)
    if active.stdout.strip() not in {"inactive", "failed"}:
        parser.error("Stop pet-feeder-ai-demo.service and confirm it is inactive before calibration")
    config = ServoConfig(
        controller=args.controller, reference_confirmed=True,
        closed_pulse_us=args.pulse_us,
        feed_pulse_us=1700 if args.pulse_us == 1500 else 1500,
        settle_ms=100,
    )

    def stop(signum, frame):
        raise KeyboardInterrupt("calibration interrupted")

    signal.signal(signal.SIGTERM, stop)
    print(f"One reference pulse: {args.pulse_us}us, hold {args.seconds}s, then disable PWM. Observe the shaft; this does not confirm mechanical zero.", flush=True)
    backend = ServoFeederBackend(config)
    try:
        result = hold_reference(backend, args.seconds)
    except (Exception, KeyboardInterrupt) as exc:
        print(json.dumps({"ok": False, "error": str(exc), "manual_check_required": True}), flush=True)
        raise SystemExit(1)
    print(json.dumps(result), flush=True)
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()