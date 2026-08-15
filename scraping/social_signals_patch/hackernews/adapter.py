from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from social_signals.core.session import BrowserSession
from social_signals.guardrails import GuardrailViolation
from social_signals.models import ActionKind, ActionResult, Draft, HealthStatus, Signal
from social_signals.platforms.base import PlatformAdapter
from social_signals.platforms.hackernews.comment import comment as hn_comment
from social_signals.platforms.hackernews.scrape import scrape_feed, scrape_url


class HackerNewsAdapter(PlatformAdapter):
    name = "hackernews"

    def scrape_watch_targets(self) -> list[Signal]:
        watch = self.cfg.get("watch", {}).get("targets", {}).get("hackernews", {})
        tags = watch.get("tags") or ["front_page"]
        # `search_queries` is the key reddit and x use; accept both so a config
        # that works for those platforms does not silently no-op here.
        queries = watch.get("queries") or watch.get("search_queries") or []
        hiring_keywords = watch.get("hiring_keywords") or queries
        limit = int(self.cfg.get("scrape", {}).get("per_target_limit", 8))

        self.guardrails.assert_action(self.name, ActionKind.SCRAPE)
        items = scrape_feed(
            tags=tags,
            queries=queries,
            limit=limit,
            hiring_keywords=hiring_keywords,
            fetch_comment_threads=bool(watch.get("fetch_comments", False)),
            top_n_comments=int(watch.get("fetch_comments_limit", 6)),
            comment_limit=int(watch.get("comment_limit", 15)),
        )
        self.guardrails.record_success(self.name, ActionKind.SCRAPE)
        source = "search" if queries else ("who_is_hiring" if "who_is_hiring" in tags else "front_page")
        return self._map_items(items, source)

    def scrape_url(self, url: str) -> Signal:
        item = scrape_url(url)
        return self._map_items([item], "url")[0]

    def publish(self, draft: Draft) -> ActionResult:
        if draft.action not in (ActionKind.COMMENT, ActionKind.REPLY):
            return ActionResult(
                False,
                self.name,
                draft.action,
                f"unsupported HN action: {draft.action.value}",
            )
        try:
            self.guardrails.assert_action(self.name, ActionKind.COMMENT, draft.text)
        except GuardrailViolation as exc:
            return ActionResult(False, self.name, draft.action, str(exc))

        if self.guardrails.dry_run:
            return self._dry_result(draft)

        with BrowserSession(self.cfg, self.name) as session:
            try:
                url = hn_comment(session.page, draft.target_url, draft.text)
                self.guardrails.record_success(self.name, ActionKind.COMMENT, draft.text)
                return ActionResult(True, self.name, ActionKind.COMMENT, "posted comment", url)
            except Exception as exc:
                self.guardrails.record_error(self.name, str(exc))
                return ActionResult(False, self.name, ActionKind.COMMENT, str(exc))

    def health_check(self) -> HealthStatus:
        try:
            items = scrape_feed(limit=1)
            ok = bool(items)
            return HealthStatus(
                ok=ok,
                platform=self.name,
                checks={"api": ok, "enabled": self.guardrails.platform_enabled(self.name)},
                messages=[] if ok else ["Algolia API returned no items"],
            )
        except Exception as exc:
            return HealthStatus(
                ok=False,
                platform=self.name,
                checks={"api": False},
                messages=[str(exc)],
            )

    def _map_items(self, items: list[dict[str, Any]], source: str) -> list[Signal]:
        now = datetime.now(timezone.utc).isoformat()
        out: list[Signal] = []
        for item in items:
            url = item.get("url") or ""
            sid = item.get("id") or url or source
            out.append(
                Signal(
                    platform=self.name,
                    signal_id=str(sid),
                    url=url,
                    title=(item.get("title") or "")[:200],
                    body=item.get("body") or "",
                    author=item.get("author") or source,
                    score=str(item.get("score") or ""),
                    engagement=item.get("engagement") or {},
                    scraped_at=item.get("scraped_at") or now,
                    raw=item.get("raw") or item,
                )
            )
        return out
