from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.live_camera import LiveCameraDetector, LiveConfig
from app.config import AppConfig
from app.main import create_app


class LiveCameraTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.config = LiveConfig(fps=100, model_cfg="model.cfg", model_weights="model.weights")
        (self.base / self.config.model_cfg).touch()
        (self.base / self.config.model_weights).touch()
        self.preview_ready = threading.Event()
        self.release_inference = threading.Event()
        self.inference_started = threading.Event()
        self.next_inference = threading.Event()
        self.capture_count = 0
        self.encoded_count = 0
        self.samples = []
        self.camera = Mock()
        self.camera.isOpened.return_value = True
        self.camera.read.side_effect = self.read_frame
        self.opencv = SimpleNamespace(
            dnn=Mock(), VideoCapture=Mock(return_value=self.camera),
            CAP_PROP_FRAME_WIDTH=3, CAP_PROP_FRAME_HEIGHT=4, CAP_PROP_FPS=5,
            CAP_PROP_BUFFERSIZE=38, IMWRITE_JPEG_QUALITY=1,
            imencode=Mock(side_effect=self.encode_frame),
        )
        self.detector = LiveCameraDetector(self.base, self.config, Mock())
        self.detector._draw = Mock()
        self.detector._detect = Mock(side_effect=self.slow_detection)
        self.patch = patch("app.live_camera.cv2", self.opencv)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.shutdown)

    def read_frame(self):
        self.capture_count += 1
        return True, np.full((8, 8, 3), self.capture_count % 255, dtype=np.uint8)

    def encode_frame(self, *args):
        self.encoded_count += 1
        if self.encoded_count >= 6:
            self.preview_ready.set()
        return True, np.array([self.encoded_count % 255], dtype=np.uint8)

    def slow_detection(self, frame, *args):
        self.samples.append(int(frame[0, 0, 0]))
        if len(self.samples) == 1:
            self.inference_started.set()
            self.release_inference.wait(timeout=3)
        else:
            self.next_inference.set()
        return []

    def shutdown(self):
        self.release_inference.set()
        self.detector.stop()

    def test_preview_advances_while_inference_is_blocked(self):
        self.detector.start()
        self.assertTrue(self.inference_started.wait(timeout=1))
        self.assertTrue(self.preview_ready.wait(timeout=1))
        sequence, jpeg = self.detector.get_frame()
        self.assertGreaterEqual(sequence, 5)
        self.assertIsNotNone(jpeg)
        self.assertFalse(self.release_inference.is_set())
        self.assertEqual(len(self.samples), 1)
        self.camera.set.assert_any_call(self.opencv.CAP_PROP_BUFFERSIZE, 2)

    def test_inference_skips_old_frames_without_growing_a_queue(self):
        self.detector.start()
        self.assertTrue(self.inference_started.wait(timeout=1))
        self.assertTrue(self.preview_ready.wait(timeout=1))
        self.release_inference.set()
        self.assertTrue(self.next_inference.wait(timeout=1))
        self.assertGreaterEqual(self.samples[1], 5)

    def test_slow_event_callback_does_not_block_preview(self):
        callback_started = threading.Event()

        def block_callback(*args):
            callback_started.set()
            self.release_inference.wait(timeout=3)

        self.detector._detect.return_value = []
        self.detector._detect.side_effect = None
        self.detector._emit_events = Mock(side_effect=block_callback)
        self.detector.start()
        self.assertTrue(callback_started.wait(timeout=1))
        self.assertTrue(self.preview_ready.wait(timeout=1))
        self.detector._emit_events.assert_called_once()

    def test_stop_releases_camera_and_both_workers(self):
        self.detector.start()
        self.assertTrue(self.inference_started.wait(timeout=1))
        self.shutdown()
        self.camera.release.assert_called_once()
        self.assertFalse(self.detector._thread.is_alive())
        self.assertFalse(self.detector._inference_thread.is_alive())

    def test_inference_failure_does_not_stop_preview(self):
        self.detector._detect.side_effect = RuntimeError("detector failed")
        self.detector.start()
        self.assertTrue(self.preview_ready.wait(timeout=1))
        self.assertIsNotNone(self.detector.get_jpeg())
        self.assertEqual(self.detector.get_info()["inference_error"], "detector failed")
        self.assertIsNone(self.detector.get_info()["last_error"])


class LiveStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_mjpeg_sends_each_frame_sequence_only_once(self):
        with patch("app.main.load_config", return_value=AppConfig()), patch("app.main._build_engine"):
            app = create_app("unused.yaml")
        detector = Mock()
        detector.get_frame.side_effect = [(1, b"jpeg-one"), (1, b"jpeg-one"), (2, b"jpeg-two")]
        app.state.live_detector = detector
        endpoint = next(route.endpoint for route in app.routes if route.path == "/api/live/mjpeg")
        response = await endpoint()
        stream = response.body_iterator
        try:
            with patch("app.main.asyncio.sleep", new_callable=AsyncMock):
                first = await anext(stream)
                second = await anext(stream)
            self.assertTrue(first.startswith(b"--frame\r\nContent-Type: image/jpeg\r\n"))
            self.assertIn(b"Content-Length: 8\r\n\r\njpeg-one\r\n", first)
            self.assertTrue(second.endswith(b"jpeg-two\r\n"))
            self.assertEqual(detector.get_frame.call_count, 3)
        finally:
            await stream.aclose()


if __name__ == "__main__":
    unittest.main()