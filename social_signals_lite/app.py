"""FastAPI surface compatible with field-note harvest's social-signals client."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from social_signals_lite import __version__
from social_signals_lite.jobs import JobStore
from social_signals_lite.reddit_scrape import cookie_paths
from social_signals_lite.watch import run_watch

app = FastAPI(title="social-signals-lite", version=__version__)
_jobs = JobStore(workers=int(os.environ.get("SS_LITE_WORKERS", "2")))


def _expected_key() -> str:
    return os.environ.get("SOCIAL_SIGNALS_API_KEY", "demo-key")


def require_api_key(authorization: str | None = Header(default=None)) -> None:
    expected = _expected_key()
    if not expected:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if token != expected:
        raise HTTPException(status_code=401, detail="invalid api key")


class JobResponse(BaseModel):
    job_id: str
    status: str
    poll_url: str


class WatchRequest(BaseModel):
    config: str = "crispy-pancake"
    platform: str | None = None
    targets: dict[str, Any] = Field(default_factory=dict)
    enable_platforms: list[str] = Field(default_factory=list)
    per_target_limit: int | None = None
    include_signals: bool = True


class CookieUploadRequest(BaseModel):
    content: str = Field(
        ..., description="Cookie file contents — Playwright JSON array"
    )


@app.get("/")
def root() -> dict[str, Any]:
    """Browsers hit ``/``; point them at health instead of a bare 404."""
    return service_health()


@app.get("/health")
def service_health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "social-signals-lite",
        "version": __version__,
        "auth_required": bool(_expected_key()),
        "default_config": os.environ.get("SIGNALS_CONFIG", "crispy-pancake"),
        "platforms": ["hackernews", "reddit"],
        "reddit_cookies": any(p.is_file() for p in cookie_paths()),
    }


@app.post("/v1/jobs/watch", dependencies=[Depends(require_api_key)])
def job_watch(body: WatchRequest) -> JobResponse:
    platform = (body.platform or "").lower().strip()
    if not platform:
        enabled = [p.lower() for p in body.enable_platforms]
        if len(enabled) == 1:
            platform = enabled[0]
        else:
            raise HTTPException(
                status_code=400,
                detail="platform is required (reddit or hackernews)",
            )
    if platform not in {"reddit", "hackernews"}:
        raise HTTPException(status_code=400, detail=f"unsupported platform: {platform}")

    limit = body.per_target_limit or 15
    include = body.include_signals

    def _run() -> dict[str, Any]:
        signals = run_watch(platform, body.targets, per_target_limit=limit)
        result: dict[str, Any] = {"count": len(signals), "run_id": "lite"}
        if include:
            result["signals"] = signals
        return result

    job = _jobs.submit("watch", _run, meta=body.model_dump())
    return JobResponse(
        job_id=job.id,
        status=job.status.value,
        poll_url=f"/v1/jobs/{job.id}",
    )


@app.get("/v1/jobs/{job_id}", dependencies=[Depends(require_api_key)])
def get_job(job_id: str) -> dict[str, Any]:
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job.to_dict()


@app.post("/v1/sessions/reddit/cookies", dependencies=[Depends(require_api_key)])
def upload_reddit_cookies(body: CookieUploadRequest) -> dict[str, Any]:
    content = (body.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="empty cookie content")
    if content[0] not in "[{":
        raise HTTPException(status_code=400, detail="expected Playwright JSON cookie array")
    path = Path(__file__).resolve().parent / "data" / "reddit-cookies.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    try:
        import json

        cookies = json.loads(content)
    except Exception as exc:  # noqa: BLE001
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"could not parse cookies: {exc}") from exc
    if not isinstance(cookies, list) or not cookies:
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="no cookies found in upload")
    return {"ok": True, "platform": "reddit", "path": str(path), "count": len(cookies)}
