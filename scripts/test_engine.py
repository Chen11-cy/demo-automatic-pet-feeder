from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import AppConfig
from app.engine import PetFeederEngine
from app.main import create_app
from app.models import ActionResult, DetectionEvent


class FeedingLimitTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = AppConfig.model_validate({
            "storage": {"data_dir": self.directory.name},
            "rules": {
                "daily_limit": 2, "cooldown_sec": 0, "required_hits": 1,
                "active_time_windows": ["00:00-23:59"], "block_when_person": False,
            },
        })
        self.feeder = Mock()
        self.feeder.feed_once.side_effect = lambda **kwargs: ActionResult(ok=True, fed=True, reason="fed")
        self.engine = PetFeederEngine(self.config, self.feeder)

    def detection(self):
        return DetectionEvent(label="cat", score=0.95, timestamp=datetime.now(timezone.utc))

    def test_manual_feed_cannot_bypass_daily_limit(self):
        self.assertTrue(self.engine.manual_feed("test").fed)
        self.assertTrue(self.engine.manual_feed("test").fed)
        result = self.engine.manual_feed("test", bypass_cooldown=True)
        self.assertFalse(result.fed)
        self.assertEqual(result.reason, "daily limit reached")
        self.assertEqual(self.feeder.feed_once.call_count, 2)

    def test_automatic_and_manual_share_the_same_limit(self):
        self.assertTrue(self.engine.process_detection(self.detection()).fed)
        self.assertTrue(self.engine.manual_feed("test").fed)
        self.assertFalse(self.engine.process_detection(self.detection()).fed)
        self.assertFalse(self.engine.manual_feed("test").fed)
        self.assertTrue(self.engine.get_status()["limit_reached"])

    def test_lowering_limit_preserves_count_and_immediately_blocks(self):
        self.engine.manual_feed("test")
        self.engine.manual_feed("test")
        self.engine.set_daily_limit(1)
        status = self.engine.get_status()
        self.assertEqual(status["daily_count"], 2)
        self.assertEqual(status["remaining_feeds"], 0)
        self.assertFalse(self.engine.manual_feed("test").fed)
        self.engine.set_daily_limit(3)
        self.assertEqual(self.engine.get_status()["remaining_feeds"], 1)
        self.assertTrue(self.engine.manual_feed("test").fed)
        self.assertFalse(self.engine.manual_feed("test").fed)

    def test_settings_and_count_survive_engine_restart(self):
        self.engine.set_daily_limit(5)
        self.engine.manual_feed("test")
        restored = PetFeederEngine(self.config, self.feeder)
        self.assertEqual(restored.get_status()["daily_limit"], 5)
        self.assertEqual(restored.get_status()["daily_count"], 1)

    def test_new_day_resets_count_but_keeps_saved_limit(self):
        self.engine.set_daily_limit(1)
        self.engine.manual_feed("test")
        self.engine.state.daily_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        status = self.engine.get_status()
        self.assertEqual(status["daily_count"], 0)
        self.assertEqual(status["daily_limit"], 1)
        self.assertTrue(self.engine.manual_feed("test").fed)

    def test_concurrent_requests_cannot_overfeed(self):
        self.engine.set_daily_limit(1)

        def delayed_feed(**kwargs):
            threading.Event().wait(timeout=0.02)
            return ActionResult(ok=True, fed=True, reason="fed")

        self.feeder.feed_once.side_effect = delayed_feed
        barrier = threading.Barrier(8)

        def attempt(index):
            barrier.wait(timeout=2)
            return self.engine.manual_feed(str(index))

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(attempt, range(8)))
        self.assertEqual(sum(result.fed for result in results), 1)
        self.feeder.feed_once.assert_called_once()

    def test_invalid_limits_are_rejected_without_changing_state(self):
        for value in (0, -1, 101, 2.5, True, "3"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.engine.set_daily_limit(value)
        self.assertEqual(self.engine.get_status()["daily_limit"], 2)

    def test_failed_feed_does_not_consume_quota(self):
        self.feeder.feed_once.side_effect = None
        self.feeder.feed_once.return_value = ActionResult(ok=False, fed=False, reason="offline")
        self.assertFalse(self.engine.manual_feed("test").fed)
        self.assertEqual(self.engine.get_status()["daily_count"], 0)

    def test_old_detection_cannot_reset_today_quota(self):
        self.engine.set_daily_limit(1)
        self.engine.manual_feed("test")
        event = self.detection()
        event.timestamp -= timedelta(days=1)
        self.assertFalse(self.engine.process_detection(event).fed)
        self.assertEqual(self.engine.get_status()["daily_count"], 1)
        self.feeder.feed_once.assert_called_once()

    def test_detection_defaults_to_explicit_utc(self):
        event = DetectionEvent(label="cat", score=0.95)
        self.assertEqual(event.timestamp.utcoffset(), timedelta(0))

    def test_minimum_interval_applies_to_auto_and_manual(self):
        self.engine.set_feeding_settings(cooldown_sec=60)
        self.assertTrue(self.engine.process_detection(self.detection()).fed)
        self.assertEqual(self.engine.process_detection(self.detection()).reason, "cooldown active")
        self.assertEqual(self.engine.manual_feed("test", bypass_cooldown=True).reason, "cooldown active")
        self.engine.state.last_feed_at = datetime.now(timezone.utc) - timedelta(seconds=61)
        self.assertTrue(self.engine.process_detection(self.detection()).fed)
        self.assertEqual(self.feeder.feed_once.call_count, 2)

    def test_interval_and_policy_persist_without_reset(self):
        self.engine.set_feeding_settings(cooldown_sec=45, reset_on_start=False, daily_limit=5)
        self.engine.manual_feed("test")
        restored = PetFeederEngine(self.config, self.feeder).get_status()
        self.assertEqual(restored["cooldown_sec"], 45)
        self.assertFalse(restored["reset_on_start"])
        self.assertEqual(restored["daily_count"], 1)
        self.assertIsNotNone(restored["last_feed_at"])

    def test_startup_reset_is_deferred_and_pauses_auto(self):
        self.engine.manual_feed("test")
        self.engine.set_feeding_settings(cooldown_sec=60, reset_on_start=True, daily_limit=8)
        self.assertEqual(self.engine.get_status()["daily_count"], 1)
        restored = PetFeederEngine(self.config, self.feeder)
        status = restored.get_status()
        self.assertEqual(status["daily_count"], 0)
        self.assertEqual(status["cooldown_left_sec"], 0)
        self.assertFalse(status["auto_enabled"])
        self.assertEqual(status["daily_limit"], 8)
        self.assertEqual(status["cooldown_sec"], 60)
        self.assertTrue(status["reset_on_start"])
        self.assertIsNotNone(status["last_reset_at"])
        self.assertEqual(restored.process_detection(self.detection()).reason, "auto mode disabled")

    def test_reset_clears_records_but_keeps_settings(self):
        self.engine.set_feeding_settings(cooldown_sec=60, daily_limit=8)
        self.engine.manual_feed("test")
        self.engine.state.last_person_seen_at = datetime.now(timezone.utc)
        self.engine.state.hit_buffer.append(datetime.now(timezone.utc))
        self.engine.reset_records()
        status = self.engine.get_status()
        self.assertEqual(status["daily_count"], 0)
        self.assertIsNone(status["last_feed_at"])
        self.assertIsNone(status["last_person_seen_at"])
        self.assertEqual(status["current_hits"], 0)
        self.assertFalse(status["auto_enabled"])
        self.assertEqual(status["daily_limit"], 8)
        self.assertEqual(status["cooldown_sec"], 60)
        self.assertEqual([event["message"] for event in self.engine.get_events()], ["feeding records reset"])

    def test_invalid_interval_settings_are_atomic(self):
        for value in (-1, 86401, 1.5, True, "60"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.engine.set_feeding_settings(daily_limit=10, cooldown_sec=value)
        for value in (0, 1, "false"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.engine.set_feeding_settings(reset_on_start=value)
        self.assertEqual(self.engine.daily_limit, 2)
        self.assertEqual(self.engine.cooldown_sec, 0)

    def test_future_event_cannot_skip_minimum_interval(self):
        self.engine.set_feeding_settings(cooldown_sec=60)
        self.engine.manual_feed("test")
        event = self.detection()
        event.timestamp += timedelta(hours=1)
        self.assertEqual(self.engine.process_detection(event).reason, "cooldown active")
        self.feeder.feed_once.assert_called_once()

    def test_zero_interval_still_enforces_daily_limit(self):
        self.engine.set_feeding_settings(cooldown_sec=0, daily_limit=1)
        self.assertTrue(self.engine.process_detection(self.detection()).fed)
        self.assertEqual(self.engine.manual_feed("test").reason, "daily limit reached")

    def test_low_score_person_detection_does_not_block_auto_feed(self):
        """低分 person 误检不应阻断自动投喂（设备实测 0.45~0.46）。"""
        self.config.rules.block_when_person = True
        self.config.rules.person_block_sec = 15
        self.config.rules.person_min_score = 0.55
        self.engine.manual_feed("prepare")  # 触发 states 初始化，且不依赖冷却
        self.engine.state.last_feed_at = None
        self.engine.state.daily_count = 0

        weak_person = DetectionEvent(label="person", score=0.46, timestamp=datetime.now(timezone.utc))
        self.engine.process_detection(weak_person)
        self.assertIsNone(self.engine.state.last_person_seen_at)

        result = self.engine.process_detection(self.detection())
        self.assertTrue(result.fed)

    def test_high_score_person_detection_blocks_auto_feed(self):
        """足够高分的 person 仍应阻断自动投喂。"""
        self.config.rules.block_when_person = True
        self.config.rules.person_block_sec = 15
        self.config.rules.person_min_score = 0.55
        self.engine.state.last_feed_at = None
        self.engine.state.daily_count = 0

        strong_person = DetectionEvent(label="person", score=0.92, timestamp=datetime.now(timezone.utc))
        self.engine.process_detection(strong_person)
        self.assertIsNotNone(self.engine.state.last_person_seen_at)

        result = self.engine.process_detection(self.detection())
        self.assertFalse(result.fed)
        self.assertEqual(result.reason, "person block active")


class SafetyGuardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = AppConfig.model_validate({
            "storage": {"data_dir": self.directory.name},
            "rules": {
                "daily_limit": 5, "cooldown_sec": 0, "required_hits": 1,
                "active_time_windows": ["00:00-23:59"], "block_when_person": False,
            },
        })
        self.feeder = Mock()

    def test_auto_mode_is_refused_when_feeder_not_ready(self):
        self.feeder.get_status.return_value = {"ready": True, "can_feed": True}
        self.feeder.feed_once.side_effect = lambda **kwargs: ActionResult(ok=True, fed=True, reason="fed")
        engine = PetFeederEngine(self.config, self.feeder)
        engine.set_auto_enabled(False)
        self.feeder.get_status.return_value = {"ready": False, "can_feed": False, "error": "not calibrated"}
        with self.assertRaises(ValueError):
            engine.set_auto_enabled(True)
        self.assertFalse(engine.get_status()["auto_enabled"])

    def test_manual_feed_is_refused_when_feeder_not_ready(self):
        self.feeder.get_status.return_value = {"ready": False, "can_feed": False, "error": "fault"}
        self.feeder.feed_once.side_effect = lambda **kwargs: ActionResult(ok=True, fed=True, reason="fed")
        engine = PetFeederEngine(self.config, self.feeder)
        result = engine.manual_feed("test")
        self.assertFalse(result.fed)
        self.assertEqual(result.reason, "feeder not ready")
        self.assertEqual(engine.get_status()["daily_count"], 0)
        self.feeder.feed_once.assert_not_called()

    def test_active_window_uses_wall_clock_not_event_timestamp(self):
        engine = PetFeederEngine(self.config, self.feeder)
        self.feeder.get_status.return_value = {"ready": True, "can_feed": True}
        self.feeder.feed_once.side_effect = lambda **kwargs: ActionResult(ok=True, fed=True, reason="fed")
        engine.set_auto_enabled(True)
        spoofed = DetectionEvent(
            label="cat", score=0.95,
            timestamp=datetime(2026, 11, 5, 8, 0, tzinfo=timezone.utc),
        )
        with patch.object(engine, "_outside_active_window", wraps=engine._outside_active_window) as spy:
            engine.process_detection(spoofed)
        passed_clock = spy.call_args[0][0]
        self.assertLess(abs((datetime.now().astimezone() - passed_clock).total_seconds()), 5)

    def test_corrupt_state_file_disables_auto_without_crashing(self):
        state_file = Path(self.directory.name) / "state.json"
        state_file.write_text("{not json", encoding="utf-8")
        self.feeder.get_status.return_value = {"ready": True, "can_feed": True}
        engine = PetFeederEngine(self.config, self.feeder)
        status = engine.get_status()
        self.assertFalse(status["auto_enabled"])
        self.assertEqual(status["daily_count"], 0)

    def test_invalid_saved_fields_do_not_crash_startup(self):
        state_file = Path(self.directory.name) / "state.json"
        state_file.write_text(
            '{"auto_enabled": true, "daily_count": "invalid", "last_feed_at": "not-a-date"}',
            encoding="utf-8",
        )
        self.feeder.get_status.return_value = {"ready": True, "can_feed": True}
        engine = PetFeederEngine(self.config, self.feeder)
        status = engine.get_status()
        self.assertEqual(status["daily_count"], 0)
        self.assertIsNone(status["last_feed_at"])
        self.assertTrue(status["auto_enabled"])


class FeedingAPITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        config = AppConfig.model_validate({"storage": {"data_dir": self.directory.name}})
        self.feeder = Mock()
        self.feeder.feed_once.side_effect = lambda **kwargs: ActionResult(ok=True, fed=True, reason="fed")
        self.engine = PetFeederEngine(config, self.feeder)
        with patch("app.main.load_config", return_value=config), patch("app.main._build_engine", return_value=self.engine):
            self.client = TestClient(create_app("unused.yaml"))
        self.addCleanup(self.client.close)

    def test_settings_endpoint_returns_saved_quota(self):
        response = self.client.put("/api/settings/feeding", json={"daily_limit": 3})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["daily_limit"], 3)
        self.assertEqual(response.json()["remaining_feeds"], 3)
        self.assertFalse(response.json()["limit_reached"])

    def test_mode_api_refuses_auto_when_feeder_not_ready(self):
        self.feeder.get_status.return_value = {"ready": True, "can_feed": True}
        self.assertEqual(self.client.post("/api/mode", json={"auto_enabled": False}).status_code, 200)
        self.feeder.get_status.return_value = {"ready": False, "can_feed": False, "error": "not calibrated"}
        response = self.client.post("/api/mode", json={"auto_enabled": True})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.client.get("/api/status").json()["auto_enabled"])

    def test_mode_api_allows_auto_when_feeder_ready(self):
        self.feeder.get_status.return_value = {"ready": True, "can_feed": True}
        response = self.client.post("/api/mode", json={"auto_enabled": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.client.get("/api/status").json()["auto_enabled"])

    def test_invalid_api_values_leave_settings_unchanged(self):
        for value in (0, 101, 2.5, True, "3"):
            with self.subTest(value=value):
                response = self.client.put("/api/settings/feeding", json={"daily_limit": value})
                self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.get("/api/status").json()["daily_limit"], 6)

    def test_manual_api_refuses_feeding_after_last_allowed_feed(self):
        self.client.put("/api/settings/feeding", json={"daily_limit": 1})
        first = self.client.post("/api/feed/manual", json={"bypass_cooldown": True})
        second = self.client.post("/api/feed/manual", json={"bypass_cooldown": True})
        self.assertTrue(first.json()["fed"])
        self.assertFalse(second.json()["fed"])
        self.assertEqual(second.json()["reason"], "daily limit reached")
        self.feeder.feed_once.assert_called_once()

    def test_partial_settings_preserve_unmodified_values(self):
        response = self.client.put("/api/settings/feeding", json={"cooldown_sec": 75, "reset_on_start": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["daily_limit"], 6)
        self.assertEqual(response.json()["cooldown_sec"], 75)
        self.assertTrue(response.json()["reset_on_start"])
        response = self.client.put("/api/settings/feeding", json={"daily_limit": 4})
        self.assertEqual(response.json()["cooldown_sec"], 75)
        self.assertTrue(response.json()["reset_on_start"])

    def test_new_settings_reject_invalid_values(self):
        invalid = [{}, {"cooldown_sec": None}, {"cooldown_sec": -1}, {"cooldown_sec": 86401}, {"cooldown_sec": 1.5}, {"cooldown_sec": True}, {"reset_on_start": 1}, {"reset_on_start": "false"}, {"daily_limit": 3, "reset_on_start": None}]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertEqual(self.client.put("/api/settings/feeding", json=payload).status_code, 422)
        self.assertEqual(self.client.get("/api/status").json()["cooldown_sec"], 300)

    def test_manual_api_cannot_bypass_interval(self):
        self.client.post("/api/feed/manual", json={})
        blocked = self.client.post("/api/feed/manual", json={"bypass_cooldown": True})
        self.assertFalse(blocked.json()["fed"])
        self.assertEqual(blocked.json()["reason"], "cooldown active")
        self.feeder.feed_once.assert_called_once()

    def test_reset_api_requires_confirmation_and_preserves_settings(self):
        self.client.put("/api/settings/feeding", json={"daily_limit": 1, "cooldown_sec": 45})
        self.client.post("/api/feed/manual", json={})
        self.assertEqual(self.client.post("/api/records/reset", json={"confirmed": False}).status_code, 400)
        self.assertEqual(self.engine.get_status()["daily_count"], 1)
        response = self.client.post("/api/records/reset", json={"confirmed": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["daily_count"], 0)
        self.assertEqual(response.json()["cooldown_left_sec"], 0)
        self.assertFalse(response.json()["auto_enabled"])
        self.assertEqual(response.json()["daily_limit"], 1)
        self.assertEqual(response.json()["cooldown_sec"], 45)
        self.assertEqual(self.client.get("/api/status").json()["daily_count"], 0)


if __name__ == "__main__":
    unittest.main()