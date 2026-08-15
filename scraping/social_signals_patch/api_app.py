"""HTTP API for social-signals — sessions, automations, and job polling."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from social_signals.api.auth import api_key_configured, require_api_key
from social_signals.api.deps import config_name, get_cfg
from social_signals.api.jobs import JobRunner
from social_signals.config import resolve_path
from social_signals.core.login_ui import login_ui_info
from social_signals.pipelines.run_context import start_run

app = FastAPI(
    title="Social Signals API",
    version="1.0.0",
    description=(
        "Unified session status and automation endpoints for Hermes, OpenClaw, and other agents."
    ),
)
_jobs = JobRunner(max_workers=int(os.environ.get("SOCIAL_API_WORKERS", "2")))


class JobResponse(BaseModel):
    job_id: str
    status: str
    poll_url: str


class WatchDiffRequest(BaseModel):
    config: str = Field(default_factory=config_name)
    platform: str | None = None
    emit_alerts: bool = True


class WatchRequest(BaseModel):
    """Watch with targets supplied per request instead of from a config file.

    A product-signal pipeline picks its own subreddits and search queries per
    product at runtime, so it cannot use a static vertical YAML. `targets` is
    deep-merged over the base config's watch targets.
    """

    config: str = Field(default_factory=config_name)
    platform: str | None = None
    targets: dict[str, Any] = Field(default_factory=dict)
    enable_platforms: list[str] = Field(default_factory=list)
    per_target_limit: int | None = None
    include_signals: bool = True


class ScrapeRequest(BaseModel):
    config: str = Field(default_factory=config_name)
    url: str


class PublishRequest(BaseModel):
    config: str = Field(default_factory=config_name)
    platform: str
    action: str
    target_url: str
    text: str
    subreddit: str = ""
    title: str = ""
    display_name: str = Field(
        default="",
        description="Instagram DM recipient display name (e.g. Alekhya Adiraju)",
    )
    recipient_handle: str = Field(
        default="",
        description="Override DM recipient handle when target_url is not the handle",
    )
    thread_id: str = Field(
        default="",
        description="Existing DM thread id — Instagram /direct/t/{id}/ or X /messages/{id}",
    )
    conversation_id: str = Field(
        default="",
        description="X DM conversation id (alias for thread_id on platform x)",
    )


class LoginRequest(BaseModel):
    config: str = Field(default_factory=config_name)
    headed: bool = False
    check_only: bool = False


class CookieUploadRequest(BaseModel):
    content: str = Field(
        ..., description="Cookie file contents — Playwright JSON array or Netscape cookies.txt"
    )


@app.get("/health")
def service_health() -> dict[str, Any]:
    ui = login_ui_info()
    return {
        "ok": True,
        "service": "social-signals",
        "auth_required": api_key_configured(),
        "default_config": config_name(),
        "vnc_url": ui.get("vnc_url"),
        "vnc_available": ui.get("vnc_available"),
    }


@app.get("/v1/login-ui", dependencies=[Depends(require_api_key)])
def get_login_ui() -> dict[str, Any]:
    """How to complete interactive browser login (noVNC sidecar)."""
    return login_ui_info()


@app.get("/v1/sessions", dependencies=[Depends(require_api_key)])
def list_sessions(
    config: str | None = None,
    platform: str | None = None,
    quick: bool = False,
) -> dict[str, Any]:
    """Unified account status — which platforms need login and what to do next."""
    cfg = get_cfg(config or config_name())
    from social_signals.pipelines.sessions import run_sessions_status

    return run_sessions_status(cfg, platform=platform, skip_browser=quick)


@app.post("/v1/sessions/{platform}/check", dependencies=[Depends(require_api_key)])
def check_session(platform: str, body: LoginRequest | None = None) -> dict[str, Any]:
    cfg = get_cfg((body.config if body else None) or config_name())
    from social_signals.pipelines.sessions import check_account_session

    status = check_account_session(cfg, platform, skip_browser=False)
    return status.to_dict()


@app.post("/v1/sessions/{platform}/login", dependencies=[Depends(require_api_key)])
def login_session(platform: str, body: LoginRequest | None = None) -> JobResponse:
    req = body or LoginRequest()
    cfg = get_cfg(req.config)

    def _run() -> dict[str, Any]:
        from social_signals.pipelines.login import run_login

        result = run_login(
            cfg,
            platform,
            check_only=req.check_only,
            headed=req.headed,
        )
        return {
            "ok": result.ok,
            "platform": result.platform,
            "status": result.status,
            "message": result.message,
            "profile_dir": result.profile_dir,
        }

    job = _jobs.submit("login", _run, meta={"platform": platform, "config": req.config})
    return JobResponse(
        job_id=job.id,
        status=job.status.value,
        poll_url=f"/v1/jobs/{job.id}",
    )


_COOKIE_PLATFORMS = {"instagram", "x", "linkedin", "reddit", "facebook", "tiktok"}


@app.post("/v1/sessions/{platform}/cookies", dependencies=[Depends(require_api_key)])
def upload_cookies(platform: str, body: CookieUploadRequest) -> dict[str, Any]:
    """Persist an exported cookie file so the platform can authenticate without login."""
    platform = platform.lower().strip()
    if platform not in _COOKIE_PLATFORMS:
        raise HTTPException(status_code=400, detail=f"unsupported platform: {platform}")
    content = (body.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="empty cookie content")

    from social_signals.core.cookie_import import (
        COOKIE_DATA_DIR,
        load_playwright_cookies,
        parse_netscape,
    )

    is_json = content[0] in "[{"
    ext = ".json" if is_json else ".txt"
    COOKIE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Remove any prior file for this platform (other extension) so discovery is unambiguous.
    for other in (".json", ".txt"):
        stale = COOKIE_DATA_DIR / f"{platform}-cookies{other}"
        if other != ext and stale.is_file():
            stale.unlink()
    path = COOKIE_DATA_DIR / f"{platform}-cookies{ext}"
    path.write_text(content, encoding="utf-8")

    try:
        cookies = load_playwright_cookies(path) if is_json else parse_netscape(path)
    except Exception as exc:  # malformed upload — keep nothing
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"could not parse cookies: {exc}") from exc
    if not cookies:
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="no cookies found in upload")

    return {
        "ok": True,
        "platform": platform,
        "count": len(cookies),
        "format": ext.lstrip("."),
        "path": str(path),
    }


@app.get("/v1/jobs/{job_id}", dependencies=[Depends(require_api_key)])
def get_job(job_id: str) -> dict[str, Any]:
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()


@app.get("/v1/jobs", dependencies=[Depends(require_api_key)])
def list_jobs(limit: int = 20) -> dict[str, Any]:
    return {"jobs": [j.to_dict() for j in _jobs.list_jobs(limit=limit)]}


@app.post("/v1/jobs/watch-diff", dependencies=[Depends(require_api_key)])
def job_watch_diff(body: WatchDiffRequest) -> JobResponse:
    cfg = get_cfg(body.config)

    def _run() -> dict[str, Any]:
        from social_signals.pipelines import run_watch_diff

        ctx = start_run(cfg, "watch-diff", platform=body.platform or "")
        result = run_watch_diff(
            cfg,
            platform=body.platform,
            emit_alerts=body.emit_alerts,
            ctx=ctx,
        )
        return {
            "watch_path": str(result.watch_path),
            "new_count": result.new_count,
            "previous_watch_path": str(result.previous_path) if result.previous_path else None,
            "run_id": ctx.manifest.run_id,
        }

    job = _jobs.submit("watch-diff", _run, meta=body.model_dump())
    return JobResponse(job_id=job.id, status=job.status.value, poll_url=f"/v1/jobs/{job.id}")


@app.post("/v1/jobs/watch", dependencies=[Depends(require_api_key)])
def job_watch(body: WatchRequest) -> JobResponse:
    """Run watch with per-request targets and return the signals inline.

    `watch-diff` returns a filesystem path, which only helps a caller sharing
    this host. This returns the signal objects themselves so a remote backend
    can consume them directly.
    """
    base = get_cfg(body.config)

    def _run() -> dict[str, Any]:
        import copy

        from social_signals.config import _deep_merge
        from social_signals.pipelines import run_watch

        # copy.deepcopy: get_cfg is lru_cached, so mutating its result would
        # leak this request's targets into every later request.
        cfg = copy.deepcopy(base)
        if body.targets:
            cfg["watch"] = _deep_merge(
                cfg.get("watch", {}), {"targets": body.targets}
            )
        for name in body.enable_platforms:
            cfg.setdefault("guardrails", {}).setdefault("platforms", {}).setdefault(
                name, {}
            )["enabled"] = True
        if body.per_target_limit:
            cfg.setdefault("scrape", {})["per_target_limit"] = body.per_target_limit

        ctx = start_run(cfg, "watch", platform=body.platform or "")
        path = run_watch(cfg, platform=body.platform, ctx=ctx)
        result: dict[str, Any] = {
            "output_path": str(path),
            "run_id": ctx.manifest.run_id,
        }
        signals = json.loads(Path(path).read_text())
        result["count"] = len(signals)
        if body.include_signals:
            result["signals"] = signals
        return result

    job = _jobs.submit("watch", _run, meta=body.model_dump())
    return JobResponse(job_id=job.id, status=job.status.value, poll_url=f"/v1/jobs/{job.id}")


@app.post("/v1/jobs/scrape", dependencies=[Depends(require_api_key)])
def job_scrape(body: ScrapeRequest) -> JobResponse:
    cfg = get_cfg(body.config)

    def _run() -> dict[str, Any]:
        from social_signals.pipelines import run_scrape

        path = run_scrape(cfg, body.url)
        return {"output_path": path}

    job = _jobs.submit("scrape", _run, meta=body.model_dump())
    return JobResponse(job_id=job.id, status=job.status.value, poll_url=f"/v1/jobs/{job.id}")


@app.post("/v1/jobs/publish", dependencies=[Depends(require_api_key)])
def job_publish(body: PublishRequest) -> JobResponse:
    cfg = get_cfg(body.config)

    def _run() -> dict[str, Any]:
        from social_signals.pipelines import run_publish

        ctx = start_run(cfg, "publish", platform=body.platform)
        meta: dict[str, str] = {}
        if body.subreddit:
            meta["subreddit"] = body.subreddit
        if body.title:
            meta["title"] = body.title
        if body.display_name:
            meta["display_name"] = body.display_name
        if body.recipient_handle:
            meta["recipient_handle"] = body.recipient_handle
        if body.thread_id:
            meta["thread_id"] = body.thread_id
        if body.conversation_id:
            meta["conversation_id"] = body.conversation_id
        return run_publish(
            cfg,
            body.platform,
            body.action,
            body.target_url,
            body.text,
            ctx=ctx,
            **meta,
        )

    job = _jobs.submit("publish", _run, meta=body.model_dump())
    return JobResponse(job_id=job.id, status=job.status.value, poll_url=f"/v1/jobs/{job.id}")


@app.get("/v1/limits", dependencies=[Depends(require_api_key)])
def get_limits(config: str | None = None, platform: str | None = None) -> dict[str, Any]:
    cfg = get_cfg(config or config_name())
    from social_signals.cli_platforms import PLATFORMS
    from social_signals.guardrails import ActionLedger
    from social_signals.models import ActionKind

    ledger = ActionLedger(resolve_path(cfg, cfg["product"]["ledger_dir"]))
    names = [platform] if platform else PLATFORMS
    out: dict[str, dict[str, int]] = {}
    for name in names:
        out[name] = {kind.value: ledger.count(name, kind) for kind in ActionKind}
    return {"platforms": out}


@app.get("/v1/alerts", dependencies=[Depends(require_api_key)])
def get_alerts(config: str | None = None, limit: int = 50) -> dict[str, Any]:
    cfg = get_cfg(config or config_name())
    alerts_path = resolve_path(cfg, cfg["product"]["state_dir"]) / "alerts.json"
    if not alerts_path.exists():
        return {"alerts": [], "path": str(alerts_path)}
    data = json.loads(alerts_path.read_text(encoding="utf-8"))
    alerts = data.get("alerts", [])
    if limit > 0:
        alerts = alerts[-limit:]
    return {"alerts": alerts, "path": str(alerts_path), "count": len(alerts)}


class PublishQueueEnqueueRequest(BaseModel):
    config: str = Field(default_factory=config_name)
    platform: str
    action: str
    target_url: str
    text: str
    approved: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class PublishQueueProcessRequest(BaseModel):
    config: str = Field(default_factory=config_name)
    limit: int = 10
    only_approved: bool = True


@app.get("/v1/publish-queue", dependencies=[Depends(require_api_key)])
def get_publish_queue(config: str | None = None) -> dict[str, Any]:
    cfg = get_cfg(config or config_name())
    from social_signals.pipelines.publish_queue import list_pending

    items = list_pending(cfg)
    return {"pending": items, "count": len(items)}


@app.post("/v1/publish-queue", dependencies=[Depends(require_api_key)])
def enqueue_publish(body: PublishQueueEnqueueRequest) -> dict[str, Any]:
    cfg = get_cfg(body.config)
    from social_signals.pipelines.publish_queue import enqueue_item

    path = enqueue_item(
        cfg,
        platform=body.platform,
        action=body.action,
        target_url=body.target_url,
        text=body.text,
        approved=body.approved,
        metadata=body.metadata,
    )
    return {"ok": True, "path": str(path)}


@app.post("/v1/jobs/publish-queue", dependencies=[Depends(require_api_key)])
def job_publish_queue(body: PublishQueueProcessRequest) -> JobResponse:
    cfg = get_cfg(body.config)

    def _run() -> dict[str, Any]:
        from social_signals.pipelines import run_publish_queue

        ctx = start_run(cfg, "publish-queue")
        return run_publish_queue(
            cfg,
            ctx=ctx,
            only_approved=body.only_approved,
            limit=body.limit,
        )

    job = _jobs.submit("publish-queue", _run, meta=body.model_dump())
    return JobResponse(job_id=job.id, status=job.status.value, poll_url=f"/v1/jobs/{job.id}")


@app.get("/v1/capabilities", dependencies=[Depends(require_api_key)])
def get_capabilities(platform: str | None = None) -> dict[str, Any]:
    from social_signals.cli_platforms import PLATFORMS
    from social_signals.platforms.capabilities import get_capabilities as caps

    names = [platform] if platform else PLATFORMS
    out = {}
    for name in names:
        c = caps(name)
        out[name] = {
            k: v
            for k, v in c.__dict__.items()
            if k in ("transport", "notes") or v is True
        }
    return {"platforms": out}
