from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any, Dict

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.config import AppConfig, load_config
from app.engine import PetFeederEngine
from app.feeder import CommandFeederBackend, MockFeederBackend, ServoFeederBackend
from app.live_camera import LiveCameraDetector
from app.models import AutoModeRequest, DailyLimitRequest, DetectionEvent, FeedActionRequest, ResetRecordsRequest


def _build_engine(config: AppConfig) -> PetFeederEngine:
    if config.feeder.backend == "mock":
        backend = MockFeederBackend()
    elif config.feeder.backend == "command":
        backend = CommandFeederBackend(
            command=config.feeder.command,
            timeout_sec=config.feeder.command_timeout_sec,
        )
    elif config.feeder.backend == "servo":
        backend = ServoFeederBackend(config=config.feeder.servo)
    else:
        raise ValueError(f"Unsupported feeder backend: {config.feeder.backend}")
    return PetFeederEngine(config=config, feeder=backend)


def create_app(config_path: str) -> FastAPI:
    config = load_config(config_path)
    engine = _build_engine(config)
    base_dir = Path(__file__).resolve().parent.parent
    live_detector = LiveCameraDetector(
        base_dir=base_dir,
        live_cfg=config.live,
        event_callback=engine.process_detection,
    )

    app = FastAPI(title="Pet Feeder AI Demo", version="0.1.0")
    app.state.engine = engine
    app.state.live_detector = live_detector

    @app.on_event("startup")
    async def on_startup() -> None:
        if isinstance(engine.feeder, ServoFeederBackend):
            result = engine.feeder.initialize()
            if not result.ok:
                engine.set_auto_enabled(False)
        live_detector.start()

    @app.on_event("shutdown")
    async def on_shutdown() -> None:
        try:
            live_detector.stop()
        finally:
            if isinstance(engine.feeder, ServoFeederBackend):
                engine.feeder.close()

    web_dir = base_dir / "web"
    app.mount("/web", StaticFiles(directory=str(web_dir)), name="web")

    @app.get("/")
    def home() -> FileResponse:
        return FileResponse(
            web_dir / "index.html",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    @app.get("/api/status")
    def get_status() -> Dict[str, Any]:
        return engine.get_status()

    @app.get("/api/events")
    def get_events(limit: int = 50) -> Dict[str, Any]:
        safe_limit = max(1, min(limit, 500))
        return {"events": engine.get_events(limit=safe_limit)}

    @app.post("/api/detections")
    def post_detection(event: DetectionEvent) -> Dict[str, Any]:
        result = engine.process_detection(event)
        return result.model_dump(mode="json")

    @app.post("/api/detections/frigate")
    def post_frigate_event(payload: Dict[str, Any]) -> Dict[str, Any]:
        # Frigate event payload usually contains labels under after.label
        after = payload.get("after") or {}
        if not after:
            raise HTTPException(status_code=400, detail="Missing 'after' field")

        label = after.get("label")
        score = after.get("score")
        if label is None or score is None:
            raise HTTPException(status_code=400, detail="Missing label/score")

        box = after.get("box")  # Typically [x1,y1,x2,y2] in pixels
        frame = after.get("frame") or {}
        width = frame.get("width")
        height = frame.get("height")
        bbox_norm = None
        if box and width and height:
            x1, y1, x2, y2 = box
            bbox_norm = [x1 / width, y1 / height, x2 / width, y2 / height]

        event = DetectionEvent(
            source="frigate",
            label=str(label),
            score=float(score),
            camera=after.get("camera"),
            frame_width=width,
            frame_height=height,
            bbox=bbox_norm,
            snapshot_path=after.get("snapshot"),
            raw=payload,
        )
        result = engine.process_detection(event)
        return result.model_dump(mode="json")

    @app.post("/api/feed/manual")
    def manual_feed(req: FeedActionRequest) -> Dict[str, Any]:
        result = engine.manual_feed(reason=req.reason, bypass_cooldown=req.bypass_cooldown)
        return result.model_dump(mode="json")

    @app.post("/api/mode")
    def set_mode(req: AutoModeRequest) -> Dict[str, Any]:
        try:
            engine.set_auto_enabled(req.auto_enabled)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "auto_enabled": req.auto_enabled}

    @app.put("/api/settings/feeding")
    def set_feeding_settings(req: DailyLimitRequest) -> Dict[str, Any]:
        engine.set_feeding_settings(**req.model_dump(exclude_none=True))
        return engine.get_status()

    @app.post("/api/records/reset")
    def reset_records(req: ResetRecordsRequest) -> Dict[str, Any]:
        if not req.confirmed:
            raise HTTPException(status_code=400, detail="Reset confirmation required")
        engine.reset_records()
        return engine.get_status()

    @app.get("/api/live/status")
    def live_status() -> Dict[str, Any]:
        return app.state.live_detector.get_info()

    @app.get("/api/live/snapshot.jpg")
    def live_snapshot() -> Response:
        sequence, jpg = app.state.live_detector.get_frame()
        if not jpg:
            raise HTTPException(status_code=503, detail="camera frame is not ready")
        return Response(
            content=jpg,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "X-Frame-Sequence": str(sequence),
            },
        )

    @app.get("/api/live/mjpeg")
    async def live_mjpeg() -> StreamingResponse:
        async def gen():
            boundary = b"--frame\r\n"
            last_sequence = -1
            while True:
                sequence, jpg = app.state.live_detector.get_frame()
                if jpg and sequence != last_sequence:
                    last_sequence = sequence
                    yield (
                        boundary
                        + b"Content-Type: image/jpeg\r\n"
                        + f"Content-Length: {len(jpg)}\r\n\r\n".encode("ascii")
                        + jpg
                        + b"\r\n"
                    )
                await asyncio.sleep(0.01)

        return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Pet Feeder AI Demo")
    parser.add_argument("--config", default="configs/demo_config.yaml", help="Path to yaml config")
    args = parser.parse_args()

    config = load_config(args.config)
    app = create_app(args.config)
    # 画面轮询频率很高，关闭 access log 避免刷爆 journal
    uvicorn.run(
        app,
        host=config.server.host,
        port=config.server.port,
        timeout_graceful_shutdown=5,
        access_log=False,
    )


if __name__ == "__main__":
    main()
