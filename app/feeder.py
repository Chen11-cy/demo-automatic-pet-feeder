from __future__ import annotations

import shlex
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.config import ServoConfig
from app.models import ActionResult


class FeederBackend(ABC):
    def get_status(self) -> dict:
        return {"ready": True, "can_feed": True, "error": None}

    @abstractmethod
    def feed_once(self, reason: str) -> ActionResult:
        raise NotImplementedError


@dataclass
class MockFeederBackend(FeederBackend):
    def feed_once(self, reason: str) -> ActionResult:
        return ActionResult(ok=True, fed=True, reason="mock feed success", details={"reason": reason})


@dataclass
class CommandFeederBackend(FeederBackend):
    command: str
    timeout_sec: int = 5

    def feed_once(self, reason: str) -> ActionResult:
        if not self.command.strip():
            return ActionResult(ok=False, fed=False, reason="empty command")

        command = self.command.replace("{reason}", reason)
        try:
            proc = subprocess.run(
                shlex.split(command),
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ActionResult(ok=False, fed=False, reason="feed command timeout")
        except Exception as exc:  # pylint: disable=broad-except
            return ActionResult(ok=False, fed=False, reason=f"feed command error: {exc}")

        if proc.returncode != 0:
            return ActionResult(
                ok=False,
                fed=False,
                reason=f"feed command failed rc={proc.returncode}",
                details={"stderr": proc.stderr.strip(), "stdout": proc.stdout.strip()},
            )

        return ActionResult(
            ok=True,
            fed=True,
            reason="feed command success",
            details={"stdout": proc.stdout.strip(), "reason": reason},
        )


@dataclass
class ServoFeederBackend(FeederBackend):
    config: ServoConfig
    pwm_root: Path = Path("/sys/class/pwm")
    sleep_fn: Callable[[float], None] = time.sleep
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _fault: str = field(default="", init=False, repr=False)
    _channel: Path | None = field(default=None, init=False, repr=False)
    _chip: Path | None = field(default=None, init=False, repr=False)
    _enabled: bool = field(default=False, init=False, repr=False)

    def get_status(self) -> dict:
        with self._lock:
            can_feed = self.config.reference_confirmed and not self._fault
            return {
                "ready": can_feed and self._enabled,
                "can_feed": can_feed,
                "reference_confirmed": self.config.reference_confirmed,
                "initialized": self._enabled,
                "reference_pulse_us": self.config.closed_pulse_us,
                "error": self._fault or (None if self.config.reference_confirmed else "servo reference is not calibrated"),
            }

    def _verify_position_command(self, pulse_us: int) -> None:
        expected = {"enable": "1", "period": "20000000", "duty_cycle": str(pulse_us * 1000), "polarity": "normal"}
        for name, value in expected.items():
            if (self._channel / name).read_text().strip() != value:
                raise RuntimeError(f"servo PWM readback mismatch: {name}")

    def _release(self) -> None:
        if self._channel is None:
            return
        if (self._channel / "enable").read_text().strip() != "0":
            self._write(self._channel / "enable", 0)
        if (self._channel / "enable").read_text().strip() != "0":
            raise RuntimeError("PWM output did not become disabled")
        self._enabled = False
        self._write(self._chip / "unexport", self.config.channel)
        self._channel = None
        self._chip = None

    def close(self) -> None:
        with self._lock:
            try:
                self._release()
            except Exception as exc:
                self._fault = str(exc)
                raise

    def _fail(self, error: Exception) -> ActionResult:
        self._fault = str(error)
        if self._channel is not None:
            if self._enabled:
                try:
                    self._write(self._channel / "duty_cycle", self.config.closed_pulse_us * 1000)
                    self.sleep_fn(self.config.settle_ms / 1000)
                except Exception as exc:
                    self._fault += f"; return command failed: {exc}"
            try:
                self._release()
            except Exception as exc:
                self._fault += f"; cleanup failed: {exc}"
        return ActionResult(ok=False, fed=False, reason="servo fault; inspect hardware before restarting", details={"error": self._fault, "manual_check_required": True})

    def _initialize(self) -> None:
        if self._fault:
            raise RuntimeError(self._fault)
        if not self.config.reference_confirmed:
            raise ValueError("servo reference is not calibrated; PWM output is blocked")
        if self.config.closed_pulse_us == self.config.feed_pulse_us:
            raise ValueError("servo closed and feed pulses must differ")
        if self._channel is not None:
            self._verify_position_command(self.config.closed_pulse_us)
            return
        chip = self._find_chip()
        channel = chip / f"pwm{self.config.channel}"
        if channel.exists():
            raise RuntimeError("PWM channel already exported; refusing to take over")
        self._write(chip / "export", self.config.channel)
        self._chip = chip
        self._channel = channel
        self._wait_ready(channel)
        if (channel / "enable").read_text().strip() != "0":
            self._write(channel / "enable", 0)
        if int((channel / "period").read_text().strip()) > 0:
            self._write(channel / "duty_cycle", 0)
        self._write(channel / "period", 20_000_000)
        self._write(channel / "polarity", "normal")
        self._write(channel / "duty_cycle", self.config.closed_pulse_us * 1000)
        self._write(channel / "enable", 1)
        self._enabled = True
        self.sleep_fn(self.config.settle_ms / 1000)
        self._verify_position_command(self.config.closed_pulse_us)

    def initialize(self) -> ActionResult:
        with self._lock:
            try:
                self._initialize()
                return ActionResult(ok=True, fed=False, reason="servo reference command applied and held")
            except Exception as exc:
                return self._fail(exc)

    def _find_chip(self) -> Path:
        controller = self.config.controller.strip()
        if not controller or Path(controller).name != controller:
            raise ValueError("servo PWM controller has not been confirmed")
        matches = [
            chip for chip in self.pwm_root.glob("pwmchip*")
            if (chip / "device").exists() and (chip / "device").resolve().name == controller
        ]
        if len(matches) != 1:
            raise ValueError(f"expected one PWM controller for {controller}, found {len(matches)}")
        chip = matches[0]
        if self.config.channel >= int((chip / "npwm").read_text().strip()):
            raise ValueError("servo PWM channel is outside controller range")
        return chip

    def _write(self, path: Path, value) -> None:
        path.write_text(str(value), encoding="ascii")

    def _wait_ready(self, channel: Path) -> None:
        deadline = time.monotonic() + 1.0
        while not all((channel / name).exists() for name in ("enable", "period", "duty_cycle", "polarity")):
            if time.monotonic() >= deadline:
                raise TimeoutError("exported PWM channel did not become ready")
            self.sleep_fn(0.01)

    def _sweep_to(self, target_us: int, steps: int, step_seconds: float) -> None:
        """从一个位置分步平滑移动到目标，避免瞬间跳变造成"啪一下"的观感。"""
        current = (self._channel / "duty_cycle").read_text().strip()
        start_us = int(int(current) / 1000) if current else self.config.closed_pulse_us
        if start_us == target_us or steps <= 1:
            self._write(self._channel / "duty_cycle", target_us * 1000)
            self.sleep_fn(step_seconds)
            return
        for step in range(1, steps + 1):
            pulse = start_us + round((target_us - start_us) * step / steps)
            self._write(self._channel / "duty_cycle", pulse * 1000)
            self.sleep_fn(step_seconds)

    def feed_once(self, reason: str) -> ActionResult:
        with self._lock:
            if self._fault:
                return ActionResult(ok=False, fed=False, reason="servo fault latched; inspect hardware before restarting", details={"error": self._fault, "manual_check_required": True})
            try:
                self._initialize()
                step_seconds = self.config.sweep_step_ms / 1000
                for cycle in range(self.config.feed_cycles):
                    # 缓慢扫到放食位置
                    self._sweep_to(self.config.feed_pulse_us, self.config.sweep_steps, step_seconds)
                    self._verify_position_command(self.config.feed_pulse_us)
                    self.sleep_fn(self.config.hold_ms / 1000)
                    # 缓慢扫回原点
                    self._sweep_to(self.config.closed_pulse_us, self.config.sweep_steps, step_seconds)
                    self._verify_position_command(self.config.closed_pulse_us)
                    if cycle < self.config.feed_cycles - 1:
                        self.sleep_fn(0.1)
                self.sleep_fn(self.config.settle_ms / 1000)
            except Exception as exc:
                return self._fail(exc)
            return ActionResult(
                ok=True,
                fed=True,
                reason="servo feed cycle complete; reference PWM held",
                details={
                    "reason": reason,
                    "controller": self.config.controller,
                    "channel": self.config.channel,
                    "reference_pulse_us": self.config.closed_pulse_us,
                    "sweep_cycles": self.config.feed_cycles,
                    "sweep_steps": self.config.sweep_steps,
                    "sweep_step_ms": self.config.sweep_step_ms,
                },
            )
