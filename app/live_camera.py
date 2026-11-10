from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from app.models import DetectionEvent

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None


VOC_CLASSES = [
    "person",
    "bicycle",
    "car",
    "motorcycle",
    "airplane",
    "bus",
    "train",
    "truck",
    "boat",
    "traffic light",
    "fire hydrant",
    "stop sign",
    "parking meter",
    "bench",
    "bird",
    "cat",
    "dog",
    "horse",
    "sheep",
    "cow",
    "elephant",
    "bear",
    "zebra",
    "giraffe",
    "backpack",
    "umbrella",
    "handbag",
    "tie",
    "suitcase",
    "frisbee",
    "skis",
    "snowboard",
    "sports ball",
    "kite",
    "baseball bat",
    "baseball glove",
    "skateboard",
    "surfboard",
    "tennis racket",
    "bottle",
    "wine glass",
    "cup",
    "fork",
    "knife",
    "spoon",
    "bowl",
    "banana",
    "apple",
    "sandwich",
    "orange",
    "broccoli",
    "carrot",
    "hot dog",
    "pizza",
    "donut",
    "cake",
    "chair",
    "couch",
    "potted plant",
    "bed",
    "dining table",
    "toilet",
    "tv",
    "laptop",
    "mouse",
    "remote",
    "keyboard",
    "cell phone",
    "microwave",
    "oven",
    "toaster",
    "sink",
    "refrigerator",
    "book",
    "clock",
    "vase",
    "scissors",
    "teddy bear",
    "hair drier",
    "toothbrush",
]


@dataclass
class LiveConfig:
    camera_device: str = "/dev/video0"
    width: int = 640
    height: int = 480
    fps: int = 30
    threshold: float = 0.45
    model_cfg: str = "models/yolov4-tiny.cfg"
    model_weights: str = "models/yolov4-tiny.weights"
    dnn_target: str = "auto"


class LiveCameraDetector:
    def __init__(self, base_dir: Path, live_cfg: LiveConfig, event_callback):
        self.base_dir = base_dir
        self.cfg = live_cfg
        self.event_callback = event_callback

        self._lock = threading.Lock()
        self._latest_jpeg: Optional[bytes] = None
        self._frame_sequence = 0
        self._pending_frame = None
        self._frame_ready = threading.Event()
        self._detections: List[Dict[str, object]] = []
        self._detections_at = 0.0
        self._latest_info: Dict[str, object] = {
            "running": False,
            "camera_opened": False,
            "cv2_available": cv2 is not None,
            "last_error": None,
            "detections": [],
            "frame_ts": None,
            "resolution": [self.cfg.width, self.cfg.height],
            "preview_fps": 0.0,
            "inference_fps": 0.0,
            "inference_ms": 0.0,
            "inference_error": None,
            "frame_sequence": 0,
            "dnn_target_requested": str(getattr(self.cfg, "dnn_target", "auto") or "auto"),
            "dnn_target_selected": "cpu",
            "opencl_available": False,
            "opencl_enabled": False,
        }

        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._inference_thread: Optional[threading.Thread] = None

    @staticmethod
    def _configure_cv_threads(threads: int) -> None:
        """限制 OpenCV 内部线程数。

        设备实测（RK3576 / 8 核 / YOLOv4-tiny 416）：
          threads=8 -> CPU 692%, 6.7fps   （相机本身只有 ~8.6fps，纯属空转浪费）
          threads=4 -> CPU 383%, 5.3fps
          threads=2 -> CPU 195%, 4.0fps
          threads=1 -> CPU  99%, 3.0fps
        推理线程与预览线程并发时，8 线程会把 CPU 打满并拖慢预览。
        """
        if cv2 is None:
            return
        try:
            cv2.setNumThreads(max(1, int(threads)))
        except Exception:  # pragma: no cover
            pass

    def start(self) -> None:
        if any(worker and worker.is_alive() for worker in (self._thread, self._inference_thread)):
            return
        self._stop_evt.clear()
        self._frame_ready.clear()
        with self._lock:
            self._pending_frame = None
            self._detections = []
            self._detections_at = 0.0
        self._thread = threading.Thread(target=self._run, name="pet-camera-preview", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        self._frame_ready.set()
        for worker in (self._thread, self._inference_thread):
            if worker:
                worker.join(timeout=2)

    def get_jpeg(self) -> Optional[bytes]:
        with self._lock:
            return self._latest_jpeg

    def get_frame(self) -> Tuple[int, Optional[bytes]]:
        with self._lock:
            return self._frame_sequence, self._latest_jpeg

    def get_info(self) -> Dict[str, object]:
        with self._lock:
            return dict(self._latest_info)

    def _set_error(self, message: str) -> None:
        with self._lock:
            self._latest_info["last_error"] = message

    def _set_info(self, **kwargs) -> None:
        with self._lock:
            self._latest_info.update(kwargs)

    def _run(self) -> None:
        self._set_info(running=True)

        if cv2 is None:
            self._set_error("opencv not installed")
            self._set_info(running=False)
            return

        self._configure_cv_threads(int(getattr(self.cfg, "cv_threads", 2) or 2))

        cfg = self.base_dir / self.cfg.model_cfg
        weights = self.base_dir / self.cfg.model_weights
        if not cfg.exists() or not weights.exists():
            self._set_error(f"model files missing: {cfg} / {weights}")
            self._set_info(running=False)
            return

        try:
            net = cv2.dnn.readNetFromDarknet(str(cfg), str(weights))
            dnn_target = self._configure_dnn_target(net)
            out_names = net.getUnconnectedOutLayersNames()
            self._set_info(dnn_target_selected=dnn_target)
        except Exception as exc:  # pragma: no cover
            self._set_error(f"load model failed: {exc}")
            self._set_info(running=False)
            return

        cap = cv2.VideoCapture(self.cfg.camera_device)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(self.cfg.width))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self.cfg.height))
        cap.set(cv2.CAP_PROP_FPS, float(self.cfg.fps))
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)

        if not cap.isOpened():
            cap.release()
            self._set_error(f"camera open failed: {self.cfg.camera_device}")
            self._set_info(running=False, camera_opened=False)
            return

        self._set_info(camera_opened=True, last_error=None)
        frame_interval = max(0.01, 1.0 / max(1, self.cfg.fps))
        self._inference_thread = threading.Thread(
            target=self._run_inference,
            args=(net, out_names),
            name="pet-camera-inference",
            daemon=True,
        )
        self._inference_thread.start()
        report_started = time.monotonic()
        preview_frames = 0

        try:
            while not self._stop_evt.is_set():
                cycle_started = time.monotonic()
                ok, frame = cap.read()
                if not ok or frame is None:
                    self._set_error("camera read failed")
                    self._stop_evt.wait(0.2)
                    continue

                captured_at = time.monotonic()
                with self._lock:
                    self._pending_frame = (frame.copy(), captured_at)
                    self._frame_ready.set()
                    detections = self._detections if captured_at - self._detections_at <= 1.0 else []
                self._draw(frame, detections)

                ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
                if ok:
                    preview_frames += 1
                    now = time.monotonic()
                    with self._lock:
                        self._latest_jpeg = encoded.tobytes()
                        self._frame_sequence += 1
                        self._latest_info.update(
                            detections=detections,
                            frame_ts=datetime.utcnow().isoformat() + "Z",
                            frame_sequence=self._frame_sequence,
                            last_error=None,
                        )
                        if now - report_started >= 1.0:
                            self._latest_info["preview_fps"] = round(preview_frames / (now - report_started), 2)
                            report_started = now
                            preview_frames = 0
                self._stop_evt.wait(max(0.0, frame_interval - (time.monotonic() - cycle_started)))
        except Exception as exc:
            self._set_error(f"camera preview failed: {exc}")
        finally:
            cap.release()
            self._stop_evt.set()
            self._frame_ready.set()
            self._set_info(running=False, camera_opened=False)

    def _configure_dnn_target(self, net) -> str:
        requested = str(getattr(self.cfg, "dnn_target", "auto") or "auto").lower()
        requested = requested if requested in {"auto", "cpu", "opencl", "opencl_fp16"} else "auto"

        opencl_available = False
        opencl_enabled = False
        if cv2 is not None and hasattr(cv2, "ocl"):
            try:
                opencl_available = bool(cv2.ocl.haveOpenCL())
            except Exception:  # pragma: no cover
                opencl_available = False

        def use_cpu() -> str:
            if cv2 is not None and hasattr(cv2, "dnn"):
                try:
                    net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
                    net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
                except Exception:  # pragma: no cover
                    pass
            if cv2 is not None and hasattr(cv2, "ocl"):
                try:
                    cv2.ocl.setUseOpenCL(False)
                except Exception:  # pragma: no cover
                    pass
            return "cpu"

        self._set_info(
            dnn_target_requested=requested,
            opencl_available=opencl_available,
            opencl_enabled=False,
        )

        wants_opencl = requested in {"auto", "opencl", "opencl_fp16"}
        if wants_opencl and opencl_available and cv2 is not None and hasattr(cv2, "dnn") and hasattr(cv2, "ocl"):
            try:
                cv2.ocl.setUseOpenCL(True)
                net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
                if requested == "opencl_fp16":
                    net.setPreferableTarget(cv2.dnn.DNN_TARGET_OPENCL_FP16)
                    selected = "opencl_fp16"
                else:
                    net.setPreferableTarget(cv2.dnn.DNN_TARGET_OPENCL)
                    selected = "opencl"
                opencl_enabled = bool(cv2.ocl.useOpenCL())
                self._set_info(opencl_enabled=opencl_enabled)
                return selected if opencl_enabled else use_cpu()
            except Exception:  # pragma: no cover
                return use_cpu()

        return use_cpu()

    def _run_inference(self, net, out_names) -> None:
        report_started = time.monotonic()
        inferred_frames = 0
        fps_limit = float(getattr(self.cfg, "inference_fps_limit", 0) or 0)
        min_interval = (1.0 / fps_limit) if fps_limit > 0 else 0.0
        last_inferred_at = time.monotonic()
        while not self._stop_evt.is_set():
            if not self._frame_ready.wait(timeout=0.2):
                continue
            with self._lock:
                sample = self._pending_frame
                self._pending_frame = None
                self._frame_ready.clear()
            if sample is None or self._stop_evt.is_set():
                continue
            # 限频：相机帧率不高，没必要每帧都推理，省 CPU 也降负载
            wait_left = min_interval - (time.monotonic() - last_inferred_at)
            if wait_left > 0:
                self._stop_evt.wait(wait_left)
                if self._stop_evt.is_set():
                    break
            frame, captured_at = sample
            started = time.monotonic()
            try:
                detections = self._detect(frame, net, out_names)
                if self._stop_evt.is_set():
                    break
                completed = time.monotonic()
                inferred_frames += 1
                last_inferred_at = completed
                with self._lock:
                    self._detections = detections
                    self._detections_at = captured_at
                    self._latest_info["inference_ms"] = round((completed - started) * 1000, 1)
                    self._latest_info["inference_error"] = None
                    if completed - report_started >= 1.0:
                        self._latest_info["inference_fps"] = round(inferred_frames / (completed - report_started), 2)
                        report_started = completed
                        inferred_frames = 0
                self._emit_events(frame.shape[1], frame.shape[0], detections)
            except Exception as exc:
                self._set_info(inference_error=str(exc))

    def _detect(self, frame: np.ndarray, net, out_names) -> List[Dict[str, object]]:
        h, w = frame.shape[:2]
        blob = cv2.dnn.blobFromImage(
            frame,
            scalefactor=1 / 255.0,
            size=(416, 416),
            swapRB=True,
            crop=False,
        )
        net.setInput(blob)
        layer_outputs = net.forward(out_names)

        boxes = []
        confidences = []
        class_ids = []

        for out in layer_outputs:
            for det in out:
                scores = det[5:]
                class_id = int(np.argmax(scores))
                conf = float(scores[class_id])
                if conf < self.cfg.threshold:
                    continue

                if class_id < 0 or class_id >= len(VOC_CLASSES):
                    continue
                label = VOC_CLASSES[class_id]
                if label not in {"cat", "dog", "person"}:
                    continue

                cx, cy, bw, bh = det[0:4] * np.array([w, h, w, h])
                x1 = int(cx - bw / 2)
                y1 = int(cy - bh / 2)
                boxes.append([x1, y1, int(bw), int(bh)])
                confidences.append(conf)
                class_ids.append(class_id)

        idxs = cv2.dnn.NMSBoxes(boxes, confidences, self.cfg.threshold, 0.4)
        result: List[Dict[str, object]] = []
        if len(idxs) > 0:
            for idx in idxs.flatten():
                x1, y1, bw, bh = boxes[idx]
                x2 = x1 + bw
                y2 = y1 + bh
                x1 = max(0, min(x1, w - 1))
                y1 = max(0, min(y1, h - 1))
                x2 = max(0, min(x2, w - 1))
                y2 = max(0, min(y2, h - 1))
                if x2 <= x1 or y2 <= y1:
                    continue

                label = VOC_CLASSES[class_ids[idx]]
                score = float(confidences[idx])
                result.append(
                    {
                        "label": label,
                        "score": round(score, 3),
                        "bbox": [x1, y1, x2, y2],
                        "bbox_norm": [x1 / w, y1 / h, x2 / w, y2 / h],
                    }
                )

        return result

    def _draw(self, frame: np.ndarray, detections: List[Dict[str, object]]) -> None:
        for det in detections:
            label = det["label"]
            score = det["score"]
            x1, y1, x2, y2 = det["bbox"]

            color: Tuple[int, int, int]
            if label == "cat":
                color = (0, 200, 255)
            elif label == "dog":
                color = (0, 255, 0)
            else:
                color = (255, 180, 0)

            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            text = f"{label}:{score:.2f}"
            cv2.rectangle(frame, (x1, max(0, y1 - 22)), (x1 + 140, y1), color, -1)
            cv2.putText(
                frame,
                text,
                (x1 + 4, max(12, y1 - 6)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (20, 20, 20),
                1,
                cv2.LINE_AA,
            )

    def _emit_events(self, width: int, height: int, detections: List[Dict[str, object]]) -> None:
        for det in detections:
            event = DetectionEvent(
                source="system",
                label=str(det["label"]),
                score=float(det["score"]),
                frame_width=width,
                frame_height=height,
                bbox=list(det["bbox_norm"]),
            )
            self.event_callback(event)
