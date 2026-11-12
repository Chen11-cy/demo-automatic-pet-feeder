from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, List, Optional

from app.config import AppConfig
from app.feeder import FeederBackend
from app.models import ActionResult, DemoEventLog, DetectionEvent


@dataclass
class RuntimeState:
    auto_enabled: bool = True
    last_feed_at: Optional[datetime] = None
    daily_date: str = ""
    daily_count: int = 0
    daily_limit: Optional[int] = None
    cooldown_sec: Optional[int] = None
    reset_on_start: Optional[bool] = None
    last_reset_at: Optional[datetime] = None
    last_person_seen_at: Optional[datetime] = None
    hit_buffer: Deque[datetime] = field(default_factory=deque)


class PetFeederEngine:
    def __init__(self, config: AppConfig, feeder: FeederBackend):
        self.config = config
        self.feeder = feeder
        self.events: Deque[DemoEventLog] = deque(maxlen=config.storage.max_events)
        self.state = RuntimeState()
        self._lock = threading.RLock()

        self.data_dir = Path(self.config.storage.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.data_dir / "state.json"
        self._last_skip_reason = ""
        self._last_skip_logged_at = 0.0
        self._load_state()
        if self.reset_on_start:
            self.reset_records()
        self._log("system", "engine initialized", {})

    def set_auto_enabled(self, enabled: bool) -> None:
        with self._lock:
            if enabled and not self._feeder_ready():
                raise ValueError("feeder is not ready; auto mode is blocked")
            self.state.auto_enabled = enabled
            self._save_state()
            self._log("mode_change", f"auto_enabled set to {enabled}", {"auto_enabled": enabled})

    @property
    def daily_limit(self) -> int:
        return self.state.daily_limit if self.state.daily_limit is not None else self.config.rules.daily_limit

    def set_daily_limit(self, daily_limit: int) -> None:
        self.set_feeding_settings(daily_limit=daily_limit)

    @property
    def cooldown_sec(self) -> int:
        return self.state.cooldown_sec if self.state.cooldown_sec is not None else self.config.rules.cooldown_sec

    @property
    def reset_on_start(self) -> bool:
        return self.state.reset_on_start if self.state.reset_on_start is not None else self.config.storage.reset_on_start

    def set_feeding_settings(
        self,
        daily_limit: Optional[int] = None,
        cooldown_sec: Optional[int] = None,
        reset_on_start: Optional[bool] = None,
    ) -> None:
        if daily_limit is not None and (type(daily_limit) is not int or not 1 <= daily_limit <= 100):
            raise ValueError("daily_limit must be an integer from 1 to 100")
        if cooldown_sec is not None and (type(cooldown_sec) is not int or not 0 <= cooldown_sec <= 86400):
            raise ValueError("cooldown_sec must be an integer from 0 to 86400")
        if reset_on_start is not None and type(reset_on_start) is not bool:
            raise ValueError("reset_on_start must be a boolean")
        with self._lock:
            settings = {name: value for name, value in {
                "daily_limit": daily_limit, "cooldown_sec": cooldown_sec, "reset_on_start": reset_on_start,
            }.items() if value is not None}
            for name, value in settings.items():
                setattr(self.state, name, value)
            self._save_state()
            self._log("system", "feeding settings updated", settings)

    def reset_records(self) -> None:
        with self._lock:
            self.state.auto_enabled = False
            self.state.last_reset_at = datetime.now(timezone.utc)
            self.state.daily_date = self.state.last_reset_at.strftime("%Y-%m-%d")
            self.state.daily_count = 0
            self.state.last_feed_at = None
            self.state.last_person_seen_at = None
            self.state.hit_buffer.clear()
            self.events.clear()
            self._save_state()
            self._log("system", "feeding records reset", {})

    def manual_feed(self, reason: str, bypass_cooldown: bool = False) -> ActionResult:
        with self._lock:
            now = datetime.now(timezone.utc)
            self._roll_daily_counter(now)
            if not self._feeder_ready():
                result = ActionResult(
                    ok=False,
                    fed=False,
                    reason="feeder not ready",
                    details={"manual_check_required": True, "feeder_status": self.feeder.get_status()},
                )
                self._log("feed_skip", result.reason, {"reason": reason})
                return result
            if self.state.daily_count >= self.daily_limit:
                result = ActionResult(ok=False, fed=False, reason="daily limit reached")
                self._log("feed_skip", result.reason, {"reason": reason})
                return result
            if self._is_cooldown(now):
                result = ActionResult(ok=False, fed=False, reason="cooldown active")
                self._log("feed_skip", result.reason, {"reason": reason})
                return result

            result = self.feeder.feed_once(reason=reason)
            self._after_feed_attempt(result, reason=reason)
            return result

    def process_detection(self, event: DetectionEvent) -> ActionResult:
        with self._lock:
            return self._process_detection(event)

    def _process_detection(self, event: DetectionEvent) -> ActionResult:
        now = event.timestamp.replace(tzinfo=timezone.utc) if event.timestamp.tzinfo is None else event.timestamp.astimezone(timezone.utc)
        # 分日计数一律按本机墙钟推进，避免历史/伪造事件重置配额
        self._roll_daily_counter(datetime.now(timezone.utc))

        self._log(
            "detection",
            f"detect {event.label} score={event.score:.2f}",
            {
                "label": event.label,
                "score": event.score,
                "camera": event.camera,
                "bbox": event.bbox,
                "source": event.source,
            },
        )

        if event.label == "person":
            # 低置信度的 person 误检（设备实测 0.45~0.46）不应长时间阻断自动投喂，
            # 只有达到 person_min_score 才记为"人在场"。
            person_min_score = self.config.rules.person_min_score
            if event.score < person_min_score:
                return self._auto_skip(
                    "person score below threshold",
                    {
                        "score": round(event.score, 3),
                        "person_min_score": person_min_score,
                    },
                )
            self.state.last_person_seen_at = now
            self._save_state()
            return ActionResult(ok=True, fed=False, reason="person seen; no feed")

        if not self.state.auto_enabled:
            return self._auto_skip("auto mode disabled")

        if event.label not in self.config.rules.allowed_labels:
            return self._auto_skip("label not allowed")

        if event.score < self.config.rules.min_score:
            return self._auto_skip(
                "score below threshold",
                {
                    "label": event.label,
                    "score": round(event.score, 3),
                    "min_score": self.config.rules.min_score,
                },
            )

        if self._outside_active_window(datetime.now().astimezone()):
            return self._auto_skip("outside active time")

        if self.state.daily_count >= self.daily_limit:
            return self._auto_skip("daily limit reached")

        if self.config.rules.block_when_person and self._person_recently_seen(now):
            return self._auto_skip("person block active")

        if self._is_cooldown(datetime.now(timezone.utc)):
            return self._auto_skip("cooldown active")

        if not self._within_zone(event):
            return self._auto_skip("outside feed zone")

        self._append_hit(now)
        if len(self.state.hit_buffer) < self.config.rules.required_hits:
            return self._auto_skip(
                "waiting for consecutive hits",
                {
                    "hits": len(self.state.hit_buffer),
                    "required_hits": self.config.rules.required_hits,
                },
            )

        # Hits confirmed, execute feeding
        if not self._feeder_ready():
            self.state.hit_buffer.clear()
            result = ActionResult(
                ok=False,
                fed=False,
                reason="feeder not ready",
                details={"manual_check_required": True, "feeder_status": self.feeder.get_status()},
            )
            self._log("feed_skip", result.reason, {"reason": f"auto:{event.label}"})
            return result
        result = self.feeder.feed_once(reason=f"auto:{event.label}")
        self._after_feed_attempt(result, reason=f"auto:{event.label}")
        self.state.hit_buffer.clear()
        return result

    def get_status(self) -> Dict[str, object]:
        with self._lock:
            return self._get_status()

    def _get_status(self) -> Dict[str, object]:
        now = datetime.now(timezone.utc)
        feeder_status = self.feeder.get_status() if isinstance(self.feeder, FeederBackend) else {"ready": True, "error": None}
        self._roll_daily_counter(now)
        cooldown_left = 0
        if self.state.last_feed_at is not None:
            elapsed = (now - self.state.last_feed_at).total_seconds()
            cooldown_left = math.ceil(max(0, self.cooldown_sec - elapsed))

        return {
            "auto_enabled": self.state.auto_enabled,
            "feeder_backend": self.config.feeder.backend,
            "feeder_ready": bool(feeder_status.get("ready", True)),
            "feeder_status": feeder_status,
            "daily_count": self.state.daily_count,
            "daily_limit": self.daily_limit,
            "remaining_feeds": max(0, self.daily_limit - self.state.daily_count),
            "limit_reached": self.state.daily_count >= self.daily_limit,
            "cooldown_sec": self.cooldown_sec,
            "reset_on_start": self.reset_on_start,
            "last_reset_at": self.state.last_reset_at.isoformat() if self.state.last_reset_at else None,
            "cooldown_left_sec": cooldown_left,
            "last_feed_at": self.state.last_feed_at.isoformat() if self.state.last_feed_at else None,
            "last_person_seen_at": self.state.last_person_seen_at.isoformat()
            if self.state.last_person_seen_at
            else None,
            "required_hits": self.config.rules.required_hits,
            "current_hits": len(self.state.hit_buffer),
            "allowed_labels": self.config.rules.allowed_labels,
            "active_time_windows": self.config.rules.active_time_windows,
            "min_score": self.config.rules.min_score,
            "block_when_person": self.config.rules.block_when_person,
            "person_block_sec": self.config.rules.person_block_sec,
            "person_min_score": self.config.rules.person_min_score,
        }

    def get_events(self, limit: int = 50) -> List[Dict[str, object]]:
        with self._lock:
            return [item.model_dump(mode="json") for item in list(self.events)[-limit:]][::-1]

    def _within_zone(self, event: DetectionEvent) -> bool:
        zone = self.config.rules.feed_zone
        if zone is None:
            return True
        if not event.bbox or len(event.bbox) != 4:
            return False
        cx = (event.bbox[0] + event.bbox[2]) / 2.0
        cy = (event.bbox[1] + event.bbox[3]) / 2.0
        return zone[0] <= cx <= zone[2] and zone[1] <= cy <= zone[3]

    def _append_hit(self, now: datetime) -> None:
        self.state.hit_buffer.append(now)
        window = self.config.rules.hit_window_sec
        while self.state.hit_buffer and (now - self.state.hit_buffer[0]).total_seconds() > window:
            self.state.hit_buffer.popleft()

    def _outside_active_window(self, now: datetime) -> bool:
        windows = self.config.parse_time_windows()
        current = now.hour * 60 + now.minute
        for start, end in windows:
            if start <= current <= end:
                return False
        return True

    def _person_recently_seen(self, now: datetime) -> bool:
        if self.state.last_person_seen_at is None:
            return False
        return (now - self.state.last_person_seen_at).total_seconds() <= self.config.rules.person_block_sec

    def _is_cooldown(self, now: datetime) -> bool:
        if self.state.last_feed_at is None:
            return False
        return (now - self.state.last_feed_at).total_seconds() < self.cooldown_sec

    def _roll_daily_counter(self, now: datetime) -> None:
        day = now.strftime("%Y-%m-%d")
        if self.state.daily_date != day:
            self.state.daily_date = day
            self.state.daily_count = 0
            self.state.hit_buffer.clear()
            self._save_state()

    def _after_feed_attempt(self, result: ActionResult, reason: str) -> None:
        if result.ok and result.fed:
            now = datetime.now(timezone.utc)
            self.state.last_feed_at = now
            self._roll_daily_counter(now)
            self.state.daily_count += 1
            self._save_state()
            self._log("feed_success", result.reason, {"reason": reason, **result.details})
            return

        self._log("feed_failure", result.reason, {"reason": reason, **result.details})

    def _auto_skip(self, reason: str, details: Optional[Dict[str, object]] = None) -> ActionResult:
        payload: Dict[str, object] = dict(details or {})
        now = time.monotonic()
        # 连续同因限频写日志，避免每秒刷爆事件列表。
        if reason != self._last_skip_reason or (now - self._last_skip_logged_at) >= 2.0:
            self._last_skip_reason = reason
            self._last_skip_logged_at = now
            self._log("feed_skip", reason, payload)
        return ActionResult(ok=True, fed=False, reason=reason, details=payload)

    def _log(self, kind: str, message: str, data: Dict[str, object]) -> None:
        self.events.append(DemoEventLog(kind=kind, message=message, data=data))

    def _load_state(self) -> None:
        if not self.state_file.exists():
            return
        try:
            content = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            # 状态文件损坏时 fail-closed：暂停自动投喂，不猜测配额
            self.state.auto_enabled = False
            self._log("system", "state file is unreadable; auto mode disabled", {})
            return
        if not isinstance(content, dict):
            self.state.auto_enabled = False
            self._log("system", "state file has unexpected format; auto mode disabled", {})
            return

        self.state.auto_enabled = content.get("auto_enabled") is True
        self.state.daily_date = str(content.get("daily_date") or "")

        saved_count = content.get("daily_count")
        if type(saved_count) is int and 0 <= saved_count <= 100000:
            self.state.daily_count = saved_count
        else:
            self.state.daily_count = 0
            self._log("system", "invalid saved daily_count ignored", {"value": repr(saved_count)})

        saved_limit = content.get("daily_limit")
        if type(saved_limit) is int and 1 <= saved_limit <= 100:
            self.state.daily_limit = saved_limit
        saved_cooldown = content.get("cooldown_sec")
        if type(saved_cooldown) is int and 0 <= saved_cooldown <= 86400:
            self.state.cooldown_sec = saved_cooldown
        saved_reset = content.get("reset_on_start")
        if type(saved_reset) is bool:
            self.state.reset_on_start = saved_reset

        self.state.last_reset_at = self._parse_saved_datetime(content.get("last_reset_at"), "last_reset_at")
        self.state.last_feed_at = self._parse_saved_datetime(content.get("last_feed_at"), "last_feed_at")
        self.state.last_person_seen_at = self._parse_saved_datetime(
            content.get("last_person_seen_at"), "last_person_seen_at"
        )

    def _parse_saved_datetime(self, raw: object, field_name: str) -> Optional[datetime]:
        if not raw or not isinstance(raw, str):
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except Exception:
            self._log("system", f"invalid saved {field_name} ignored", {"value": raw})
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)

    def _save_state(self) -> None:
        payload = {
            "auto_enabled": self.state.auto_enabled,
            "daily_date": self.state.daily_date,
            "daily_count": self.state.daily_count,
            "daily_limit": self.state.daily_limit,
            "cooldown_sec": self.state.cooldown_sec,
            "reset_on_start": self.state.reset_on_start,
            "last_reset_at": self.state.last_reset_at.isoformat() if self.state.last_reset_at else None,
            "last_feed_at": self.state.last_feed_at.isoformat() if self.state.last_feed_at else None,
            "last_person_seen_at": self.state.last_person_seen_at.isoformat()
            if self.state.last_person_seen_at
            else None,
        }
        temporary = self.state_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
        temporary.replace(self.state_file)

    def _feeder_ready(self) -> bool:
        """投喂门禁：已校准且无锁存故障。未初始化时 feed_once 会自行初始化。"""
        get_status = getattr(self.feeder, "get_status", None)
        if get_status is None:
            return True
        status = get_status()
        if not isinstance(status, dict):
            return True
        return bool(status.get("can_feed", status.get("ready", True)))
