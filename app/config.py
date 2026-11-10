from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class ServoConfig(BaseModel):
    controller: str = ""
    reference_confirmed: bool = Field(default=False, strict=True)
    channel: int = Field(default=0, ge=0, le=31, strict=True)
    # SG90 实测可用行程约 800-2200us，留出安全余量后接受 500-2500us
    closed_pulse_us: int = Field(default=1500, ge=500, le=2500, strict=True)
    feed_pulse_us: int = Field(default=1700, ge=500, le=2500, strict=True)
    settle_ms: int = Field(default=300, ge=100, le=3000, strict=True)
    hold_ms: int = Field(default=500, ge=50, le=3000, strict=True)
    # 单程扫动步数：越大越慢越平滑（800->2200 分多少小步走过去）
    sweep_steps: int = Field(default=24, ge=1, le=200, strict=True)
    # 每个小步之间的间隔，配合 sweep_steps 控制整体速度
    sweep_step_ms: int = Field(default=25, ge=5, le=500, strict=True)
    # 一次投喂往返几次（1=打食一次，2=来回两趟更明显）
    feed_cycles: int = Field(default=2, ge=1, le=10, strict=True)

    @model_validator(mode="after")
    def validate_pulses(self) -> "ServoConfig":
        if self.closed_pulse_us == self.feed_pulse_us:
            raise ValueError("servo closed and feed pulses must differ")
        return self


class FeederConfig(BaseModel):
    backend: str = "mock"
    command: str = ""
    command_timeout_sec: int = 5
    servo: ServoConfig = Field(default_factory=ServoConfig)


class RuleConfig(BaseModel):
    allowed_labels: List[str] = Field(default_factory=lambda: ["cat", "dog"])
    min_score: float = 0.6
    required_hits: int = 3
    hit_window_sec: float = 2.5
    cooldown_sec: int = Field(default=300, ge=0, le=86400)
    daily_limit: int = 6
    block_when_person: bool = True
    person_block_sec: int = 15
    # Optional normalized zone: [x1, y1, x2, y2]
    feed_zone: Optional[List[float]] = None
    active_time_windows: List[str] = Field(
        default_factory=lambda: ["07:00-09:00", "18:00-21:00"]
    )
    # person 检测达到该分数才计入"人在场"阻断；低分误检（设备实测 0.45~0.46）不阻断。
    person_min_score: float = Field(default=0.5, ge=0.0, le=1.0)

    @field_validator("feed_zone")
    @classmethod
    def validate_zone(cls, value: Optional[List[float]]) -> Optional[List[float]]:
        if value is None:
            return value
        if len(value) != 4:
            raise ValueError("feed_zone must have 4 values")
        if any(v < 0 or v > 1 for v in value):
            raise ValueError("feed_zone values must be in [0,1]")
        if not (value[0] < value[2] and value[1] < value[3]):
            raise ValueError("feed_zone must satisfy x1<x2 and y1<y2")
        return value


class StorageConfig(BaseModel):
    data_dir: str = "./data"
    max_events: int = 300
    reset_on_start: bool = False


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8787


class LiveConfig(BaseModel):
    camera_device: str = "/dev/video0"
    width: int = 640
    height: int = 480
    fps: int = Field(default=30, ge=1, le=60)
    threshold: float = 0.45
    model_cfg: str = "models/yolov4-tiny.cfg"
    model_weights: str = "models/yolov4-tiny.weights"
    # OpenCV 内部线程数。设备实测 8 线程会把 8 核打满（CPU 690%+）却只比 2 线程快一点，
    # 相机本身也只有 ~8.6fps，因此默认限制为 2，CPU 降到约 195%。
    cv_threads: int = Field(default=2, ge=1, le=16, strict=True)
    # 推理限频（每秒最多推理多少帧）。0 表示不限制。
    inference_fps_limit: float = Field(default=5.0, ge=0, le=60)
    # 推理后端目标：auto 会优先尝试 OpenCL，失败时回退 CPU。
    dnn_target: str = Field(default="auto")

    @field_validator("dnn_target")
    @classmethod
    def validate_dnn_target(cls, value: str) -> str:
        normalized = str(value).strip().lower()
        if normalized not in {"auto", "cpu", "opencl", "opencl_fp16"}:
            raise ValueError("dnn_target must be one of: auto, cpu, opencl, opencl_fp16")
        return normalized


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    live: LiveConfig = Field(default_factory=LiveConfig)
    rules: RuleConfig = Field(default_factory=RuleConfig)
    feeder: FeederConfig = Field(default_factory=FeederConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)

    def parse_time_windows(self) -> List[Tuple[int, int]]:
        parsed: List[Tuple[int, int]] = []
        for item in self.rules.active_time_windows:
            start, end = item.split("-")
            parsed.append((_to_minutes(start), _to_minutes(end)))
        return parsed


def _to_minutes(hhmm: str) -> int:
    hh, mm = hhmm.split(":")
    return int(hh) * 60 + int(mm)


def load_config(path: str) -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return AppConfig.model_validate(data)
