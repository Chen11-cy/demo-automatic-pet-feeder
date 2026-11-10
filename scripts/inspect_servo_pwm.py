import argparse
import json
from pathlib import Path


def inspect_pwm(root: Path, controller: str = ""):
    result = []
    for chip in sorted(root.glob("pwmchip*")):
        device = (chip / "device").resolve()
        if controller and device.name != controller:
            continue
        record = {"chip": str(chip), "controller": device.name}
        try:
            count = int((chip / "npwm").read_text().strip())
            record["npwm"] = count
            record["channels"] = []
            for index in range(count):
                channel = chip / f"pwm{index}"
                attributes = {"channel": index, "sysfs_exported": channel.exists()}
                if channel.exists():
                    for name in ("enable", "period", "duty_cycle", "polarity"):
                        attributes[name] = (channel / name).read_text().strip()
                record["channels"].append(attributes)
        except (OSError, ValueError) as exc:
            record["error"] = str(exc)
        result.append(record)
    return result


def main():
    parser = argparse.ArgumentParser(description="Read-only servo PWM inspection; never exports or drives PWM")
    parser.add_argument("--controller", default="")
    parser.add_argument("--pwm-root", type=Path, default=Path("/sys/class/pwm"))
    args = parser.parse_args()
    controllers = inspect_pwm(args.pwm_root, args.controller)
    report = {"read_only": True, "controllers": controllers}
    try:
        report["kernel_pwm_consumers"] = Path("/sys/kernel/debug/pwm").read_text()[:16000]
    except OSError as exc:
        report["kernel_pwm_consumers_unavailable"] = str(exc)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not controllers or any("error" in record for record in controllers):
        raise SystemExit(1)


if __name__ == "__main__":
    main()