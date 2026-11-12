from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class DetectionEvent(BaseModel):
    source: Literal["frigate", "manual", "system"] = "manual"
    label: str
    score: float = Field(ge=0.0, le=1.0)
    camera: Optional[str] = None
    frame_width: Optional[int] = None
    frame_height: Optional[int] = None
    # Normalized bbox: [x1, y1, x2, y2], each value in [0, 1]
    bbox: Optional[List[float]] = None
    snapshot_path: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    raw: Optional[Dict[str, Any]] = None


class ActionResult(BaseModel):
    ok: bool
    reason: str
    fed: bool = False
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    details: Dict[str, Any] = Field(default_factory=dict)


class FeedActionRequest(BaseModel):
    reason: str = "manual"
    bypass_cooldown: bool = False


class AutoModeRequest(BaseModel):
    auto_enabled: bool


class DailyLimitRequest(BaseModel):
    model_config = {"extra": "forbid"}

    daily_limit: Optional[int] = Field(default=None, ge=1, le=100, strict=True)
    cooldown_sec: Optional[int] = Field(default=None, ge=0, le=86400, strict=True)
    reset_on_start: Optional[bool] = Field(default=None, strict=True)

    @model_validator(mode="after")
    def validate_settings(self) -> "DailyLimitRequest":
        if not self.model_fields_set or any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("Provide at least one setting; null values are not allowed")
        return self


class ResetRecordsRequest(BaseModel):
    confirmed: bool = Field(default=False, strict=True)


class DemoEventLog(BaseModel):
    kind: Literal[
        "detection",
        "feed_success",
        "feed_skip",
        "feed_failure",
        "mode_change",
        "system",
    ]
    message: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    data: Dict[str, Any] = Field(default_factory=dict)
