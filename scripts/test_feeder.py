import errno
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import AppConfig, ServoConfig
from app.engine import PetFeederEngine
from app.feeder import ServoFeederBackend
from app.main import _build_engine, create_app, main as run_server
from app.models import DetectionEvent
from scripts.inspect_servo_pwm import inspect_pwm
from scripts.calibrate_servo_reference import hold_reference, main as calibration_main


class ServoFeederTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.controller = self.root / "27330000.pwm"
        self.controller.mkdir()
        self.chip = self.root / "pwmchip7"
        self.chip.mkdir()
        (self.chip / "device").symlink_to(self.controller, target_is_directory=True)
        (self.chip / "npwm").write_text("1")
        (self.chip / "export").touch()
        (self.chip / "unexport").touch()
        self.channel = self.chip / "pwm0"
        self.writes = []
        self.backend = ServoFeederBackend(ServoConfig(controller=self.controller.name, reference_confirmed=True), pwm_root=self.root, sleep_fn=Mock())
        self.writer = patch.object(self.backend, "_write", side_effect=self.write_pwm)
        self.writer.start()
        self.addCleanup(self.writer.stop)

    def write_pwm(self, path, value):
        self.writes.append((path.name, value))
        if path.name == "export":
            self.channel.mkdir()
            for name, initial in {"enable": "0", "period": "0", "duty_cycle": "0", "polarity": "normal"}.items():
                (self.channel / name).write_text(initial)
        elif path.name == "unexport":
            shutil.rmtree(self.channel)
        else:
            period = int((self.channel / "period").read_text())
            duty = int((self.channel / "duty_cycle").read_text())
            next_period = int(value) if path.name == "period" else period
            next_duty = int(value) if path.name == "duty_cycle" else duty
            if next_period == 0 or next_duty > next_period:
                raise OSError(errno.EINVAL, "PWM requires a valid period before applying state")
            path.write_text(str(value))

    def test_initialization_does_not_touch_pwm(self):
        self.assertEqual(self.writes, [])
        self.assertFalse(self.channel.exists())

    def test_feed_initializes_opens_returns_and_holds(self):
        result = self.backend.feed_once("manual")
        self.assertTrue(result.ok and result.fed)
        # 初始化序列（导出/周期/极性/原点）后，应看到从原点平滑扫到放食位再扫回
        self.assertEqual(self.writes[:5], [
            ("export", 0), ("period", 20_000_000),
            ("polarity", "normal"), ("duty_cycle", 1_500_000), ("enable", 1),
        ])
        duties = [value for name, value in self.writes if name == "duty_cycle"]
        # 初始化时先写原点，随后是扫动序列
        self.assertEqual(duties[0], 1_500_000)
        sweep = duties[1:]
        self.assertEqual(sweep[0], 1_508_000)
        self.assertEqual(max(sweep), 1_700_000)
        self.assertEqual(sweep[-1], 1_500_000)
        self.assertEqual((self.channel / "enable").read_text(), "1")
        self.assertEqual((self.channel / "duty_cycle").read_text(), "1500000")
        self.backend.sleep_fn.assert_any_call(0.5)

    def test_sweep_is_gradual_not_a_single_jump(self):
        self.assertTrue(self.backend.feed_once("manual").fed)
        duties = [int(value) for name, value in self.writes if name == "duty_cycle"]
        sweep = duties[1:]
        rises = [b for a, b in zip(sweep, sweep[1:]) if b > a]
        self.assertGreater(len(rises), 2, "扫动应分多步上升而不是一次跳变")
        biggest = max(b - a for a, b in zip(sweep, sweep[1:]) if b > a)
        self.assertLess(biggest, 200_000, "单步跳变不应超过 200us")

    def test_feed_cycles_configured_number_of_round_trips(self):
        self.backend.config.feed_cycles = 3
        self.assertTrue(self.backend.feed_once("manual").fed)
        duties = [int(value) for name, value in self.writes if name == "duty_cycle"][1:]
        peaks = sum(1 for value in duties if value == 1_700_000)
        self.assertEqual(peaks, 3)

    def test_unconfirmed_controller_has_no_output(self):
        self.backend.config.controller = ""
        self.assertFalse(self.backend.feed_once("test").fed)
        self.assertEqual(self.writes, [])

    def test_controller_not_chip_number_selects_the_device(self):
        result = self.backend.feed_once("test")
        self.assertTrue(result.fed)
        self.assertEqual(result.details["controller"], "27330000.pwm")

    def test_exported_channel_is_never_taken_over(self):
        self.channel.mkdir()
        self.assertFalse(self.backend.feed_once("test").fed)
        self.assertEqual(self.writes, [])

    def test_out_of_range_channel_has_no_output(self):
        self.backend.config.channel = 1
        self.assertFalse(self.backend.feed_once("test").fed)
        self.assertEqual(self.writes, [])

    def test_open_failure_returns_to_closed_and_latches_fault(self):
        def fail_open(path, value):
            if path.name == "duty_cycle" and value == 1_700_000:
                raise OSError("simulated PWM failure")
            self.write_pwm(path, value)

        self.backend._write.side_effect = fail_open
        result = self.backend.feed_once("test")
        self.assertFalse(result.fed)
        self.assertTrue(result.details["manual_check_required"])
        self.assertEqual(self.writes[-3:], [("duty_cycle", 1_500_000), ("enable", 0), ("unexport", 0)])
        count = len(self.writes)
        self.assertFalse(self.backend.feed_once("test").fed)
        self.assertEqual(len(self.writes), count)

    def engine_config(self, cooldown_sec=0):
        return AppConfig.model_validate({
            "storage": {"data_dir": str(self.root / "state")},
            "feeder": {"backend": "servo", "servo": self.backend.config.model_dump()},
            "rules": {
                "daily_limit": 2, "cooldown_sec": cooldown_sec, "required_hits": 1,
                "active_time_windows": ["00:00-23:59"], "block_when_person": False,
            },
        })

    def test_application_selects_servo_without_driving_on_start(self):
        engine = _build_engine(self.engine_config())
        self.assertIsInstance(engine.feeder, ServoFeederBackend)
        self.assertEqual(self.writes, [])
        self.assertEqual(engine.get_status()["feeder_backend"], "servo")

    def test_manual_and_auto_each_run_one_cycle_and_respect_cap(self):
        engine = PetFeederEngine(self.engine_config(), self.backend)
        self.assertTrue(engine.manual_feed("manual").fed)
        self.assertTrue(engine.process_detection(DetectionEvent(label="cat", score=0.95)).fed)
        self.assertFalse(engine.manual_feed("blocked").fed)
        self.assertEqual(self.writes.count(("export", 0)), 1)
        # 每次投喂按配置来回 feed_cycles 趟，默认 2 趟 -> 每次 2 个峰值
        cycles = self.backend.config.feed_cycles
        self.assertEqual(self.writes.count(("duty_cycle", 1_700_000)), 2 * cycles)
        self.assertEqual(engine.get_status()["daily_count"], 2)

    def test_minimum_interval_prevents_extra_servo_cycle(self):
        engine = PetFeederEngine(self.engine_config(cooldown_sec=60), self.backend)
        self.assertTrue(engine.manual_feed("manual").fed)
        self.assertFalse(engine.manual_feed("blocked", bypass_cooldown=True).fed)
        self.assertFalse(engine.process_detection(DetectionEvent(label="dog", score=0.95)).fed)
        self.assertEqual(self.writes.count(("export", 0)), 1)

    def test_inspection_is_read_only_and_resolves_controller(self):
        reports = inspect_pwm(self.root, self.controller.name)
        self.assertEqual(reports[0]["controller"], self.controller.name)
        self.assertEqual(reports[0]["npwm"], 1)
        self.assertFalse(reports[0]["channels"][0]["sysfs_exported"])
        self.assertFalse(self.channel.exists())
        self.assertEqual(self.writes, [])
        self.assertEqual(inspect_pwm(self.root, "missing.pwm"), [])

    def test_period_failure_never_enables_output_and_latches(self):
        def fail_period(path, value):
            if path.name == "period":
                raise OSError("period rejected")
            self.write_pwm(path, value)

        self.backend._write.side_effect = fail_period
        self.assertFalse(self.backend.feed_once("test").fed)
        self.assertNotIn(("enable", 1), self.writes)
        self.assertNotIn(("enable", 0), self.writes)
        self.assertFalse(self.channel.exists())
        count = len(self.writes)
        self.assertFalse(self.backend.feed_once("retry").fed)
        self.assertEqual(len(self.writes), count)

    def test_existing_period_clears_duty_before_shortening(self):
        def initialize_cached_state(path, value):
            self.write_pwm(path, value)
            if path.name == "export":
                (self.channel / "period").write_text("40000000")
                (self.channel / "duty_cycle").write_text("30000000")

        self.backend._write.side_effect = initialize_cached_state
        self.assertTrue(self.backend.feed_once("test").fed)
        self.assertEqual(self.writes[:3], [("export", 0), ("duty_cycle", 0), ("period", 20_000_000)])

    def test_disable_failure_does_not_unexport_or_retry(self):
        def fail_disable(path, value):
            if path.name == "enable" and value == 0:
                raise OSError("disable rejected")
            self.write_pwm(path, value)

        self.backend._write.side_effect = fail_disable
        result = self.backend.feed_once("test")
        self.assertTrue(result.fed)
        with self.assertRaises(OSError):
            self.backend.close()
        self.assertNotIn(("unexport", 0), self.writes)
        self.assertTrue(self.channel.exists())
        count = len(self.writes)
        retry = self.backend.feed_once("retry")
        self.assertFalse(retry.fed)
        self.assertTrue(retry.details["manual_check_required"])
        self.assertEqual(len(self.writes), count)

    def test_uncalibrated_reference_blocks_all_pwm(self):
        self.backend.config.reference_confirmed = False
        self.assertFalse(self.backend.initialize().ok)
        self.assertFalse(self.backend.feed_once("test").fed)
        self.assertEqual(self.writes, [])

    def test_explicit_initialization_applies_only_the_reference(self):
        self.backend.config.closed_pulse_us = 1300
        result = self.backend.initialize()
        self.assertTrue(result.ok)
        self.assertFalse(result.fed)
        self.assertEqual((self.channel / "enable").read_text(), "1")
        self.assertEqual((self.channel / "duty_cycle").read_text(), "1300000")
        self.assertNotIn(("duty_cycle", 1_700_000), self.writes)

    def test_repeated_feeds_keep_one_stable_pwm_session(self):
        self.assertTrue(self.backend.initialize().ok)
        self.writes.clear()
        self.assertTrue(self.backend.feed_once("first").fed)
        first_count = len(self.writes)
        self.assertTrue(self.backend.feed_once("second").fed)
        # 不重复导出/启用，两次投喂写入量一致
        self.assertEqual(len(self.writes), first_count * 2)
        self.assertNotIn(("export", 0), self.writes)
        self.assertNotIn(("enable", 0), self.writes)
        duties = [int(value) for name, value in self.writes if name == "duty_cycle"]
        self.assertEqual(duties[-1], 1_500_000)
        self.assertIn(1_700_000, duties)

    def test_close_releases_only_the_owned_channel(self):
        self.backend.close()
        self.assertEqual(self.writes, [])
        self.assertTrue(self.backend.initialize().ok)
        self.writes.clear()
        self.backend.close()
        self.assertEqual(self.writes, [("enable", 0), ("unexport", 0)])
        self.assertFalse(self.channel.exists())
        self.backend.close()
        self.assertEqual(len(self.writes), 2)

    def test_service_startup_initializes_and_shutdown_releases(self):
        config = self.engine_config()
        engine = PetFeederEngine(config, self.backend)
        with patch("app.main.load_config", return_value=config), patch("app.main._build_engine", return_value=engine), patch("app.main.LiveCameraDetector"):
            app = create_app("unused.yaml")
        with TestClient(app) as client:
            self.assertEqual((self.channel / "enable").read_text(), "1")
            self.assertEqual((self.channel / "duty_cycle").read_text(), "1500000")
            status = client.get("/api/status").json()
            self.assertTrue(status["feeder_ready"])
            self.assertEqual(status["daily_count"], 0)
            self.assertNotIn(("duty_cycle", 1_700_000), self.writes)
        self.assertFalse(self.channel.exists())

    def test_uncalibrated_startup_pauses_without_reset_or_output(self):
        config = self.engine_config()
        engine = PetFeederEngine(config, self.backend)
        self.assertTrue(engine.manual_feed("prepare").fed)
        self.backend.close()
        self.backend.config.reference_confirmed = False
        self.writes.clear()
        with patch("app.main.load_config", return_value=config), patch("app.main._build_engine", return_value=engine), patch("app.main.LiveCameraDetector"):
            app = create_app("unused.yaml")
        with TestClient(app) as client:
            status = client.get("/api/status").json()
            self.assertFalse(status["feeder_ready"])
            self.assertFalse(status["auto_enabled"])
            self.assertEqual(status["daily_count"], 1)
            self.assertEqual(status["daily_limit"], 2)
        self.assertEqual(self.writes, [])

    def test_calibration_only_holds_one_pulse_and_releases(self):
        self.backend.config.closed_pulse_us = 1300
        wait = Mock()
        result = hold_reference(self.backend, 3, sleep_fn=wait)
        self.assertTrue(result["ok"])
        self.assertFalse(result["mechanical_reference_confirmed"])
        self.assertNotIn(("duty_cycle", 1_700_000), self.writes)
        self.assertEqual([value for name, value in self.writes if name == "duty_cycle"], [1_300_000])
        wait.assert_called_once_with(3)
        self.assertFalse(self.channel.exists())

    def test_interrupted_calibration_releases_output(self):
        with self.assertRaises(KeyboardInterrupt):
            hold_reference(self.backend, 3, sleep_fn=Mock(side_effect=KeyboardInterrupt()))
        self.assertFalse(self.channel.exists())

    def test_calibration_rejects_unbounded_hold_before_output(self):
        for seconds in (0, 6, True, 1.5):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                hold_reference(self.backend, seconds)
        self.assertEqual(self.writes, [])

    def test_calibration_requires_explicit_permission(self):
        with patch("scripts.calibrate_servo_reference.ServoFeederBackend") as factory, patch("sys.stderr"), self.assertRaises(SystemExit):
            calibration_main(["--controller", "2ade3000.pwm", "--pulse-us", "1500"])
        factory.assert_not_called()

    def test_calibration_refuses_active_demo(self):
        process = Mock(stdout="active\n")
        with patch("scripts.calibrate_servo_reference.ServoFeederBackend") as factory, patch("scripts.calibrate_servo_reference.subprocess.run", return_value=process), patch("sys.stderr"), self.assertRaises(SystemExit):
            calibration_main(["--controller", "2ade3000.pwm", "--pulse-us", "1500", "--confirm-move"])
        factory.assert_not_called()

    def test_server_shutdown_is_bounded_for_stream_cleanup(self):
        config = self.engine_config()
        with patch("sys.argv", ["pet-feeder"]), patch("app.main.load_config", return_value=config), patch("app.main.create_app"), patch("app.main.uvicorn.run") as run:
            run_server()
        self.assertEqual(run.call_args.kwargs["timeout_graceful_shutdown"], 5)


if __name__ == "__main__":
    unittest.main()