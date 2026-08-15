from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from social_signals.core.browser_utils import page_text
from social_signals.core.session import BrowserSession
from social_signals.guardrails import GuardrailViolation
from social_signals.models import ActionKind, ActionResult, Draft, HealthStatus, Signal
from social_signals.platforms.base import PlatformAdapter
from social_signals.platforms.reddit.engage import upvote_post
from social_signals.platforms.reddit.scrape import (
    post_comment,
    scrape_post,
    scrape_search,
    scrape_subreddit,
    submit_post,
)


# Listing attributes that are more reliable than the rendered detail page.
# `comments` is deliberately absent: on a listing it is a count, on a detail
# page it is the comment list.
_LISTING_WINS = frozenset(
    {"url", "permalink", "created_at", "score", "flair", "domain", "search_query"}
)


def _as_int(value: Any) -> int:
    """Reddit renders scores as strings, and as '•' on hidden-score posts."""
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0


class RedditAdapter(PlatformAdapter):
    name = "reddit"

    def scrape_watch_targets(self) -> list[Signal]:
        watch = self.cfg.get("watch", {}).get("targets", {}).get("reddit", {})
        subs = watch.get("subreddits") or []
        sort = watch.get("sort", "hot")
        limit = int(self.cfg.get("scrape", {}).get("per_target_limit", 8))
        # Listing pages carry no post body and no comments. Opt in to a second
        # pass that opens each permalink, so downstream consumers get quotable
        # text instead of a headline. Off by default — it costs a page load per
        # post.
        fetch_bodies = bool(watch.get("fetch_bodies", False))
        bodies_limit = int(watch.get("fetch_bodies_limit", 10))
        comment_limit = int(watch.get("comment_limit", 15))
        signals: list[Signal] = []

        # Site-wide search finds complaints in subs the product has no
        # community in. Subreddit listings alone miss those entirely.
        queries = watch.get("search_queries") or []
        search_sort = watch.get("search_sort", "relevance")
        search_time = watch.get("search_time", "month")

        time_filter = watch.get("time_filter")
        # A post can surface from both a subreddit listing and a search. Dedup
        # by permalink before the detail pass, so a duplicate never costs a
        # second page load or double-counts in the issue clusters.
        seen_urls: set[str] = set()

        def fresh(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
            out = []
            for post in posts:
                url = post.get("url")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                out.append(post)
            return out

        with BrowserSession(self.cfg, self.name) as session:
            page = session.page
            for sub in subs:
                # One slow subreddit or a navigation timeout must not discard
                # every signal collected before it.
                def run_sub(sub=sub):
                    posts = fresh(
                        scrape_subreddit(
                            page, sub, sort=sort, limit=limit, time_filter=time_filter
                        )
                    )
                    if fetch_bodies:
                        posts = self._enrich_posts(page, posts, bodies_limit, comment_limit)
                    return self._map_posts(posts, sub)

                signals.extend(self._collect_target(f"r/{sub}", run_sub))

            for query in queries:
                def run_query(query=query):
                    posts = fresh(
                        scrape_search(
                            page, query, limit=limit, sort=search_sort, time_filter=search_time
                        )
                    )
                    # Search results carry only a permalink and a title, so the
                    # detail pass is mandatory here — not opt-in as it is for
                    # subreddit listings.
                    posts = self._enrich_posts(page, posts, len(posts), comment_limit)
                    return self._map_posts(posts, f"search:{query}")

                signals.extend(self._collect_target(f"search:{query}", run_query))
        return signals

    def _collect_target(self, label: str, work) -> list[Signal]:
        """Run one watch target, isolating its failures from the rest.

        Returns the target's signals, or an empty list if it failed. The error
        is recorded rather than swallowed, so a partial run is visible as a
        partial run.
        """
        try:
            self.guardrails.assert_action(self.name, ActionKind.SCRAPE)
        except GuardrailViolation as exc:
            # Never record a guardrail refusal as an error. Doing so re-arms
            # error_cooldown on every blocked target, so one failure locks the
            # platform out forever instead of for the cooldown window.
            print(f"[reddit] target {label} skipped: {exc}", file=sys.stderr)
            return []
        try:
            signals = work()
        except Exception as exc:  # noqa: BLE001 - one target must not kill the run
            # Deliberately NOT record_error: a navigation timeout is not a ban
            # signal, and recording it triggers a 60-minute platform cooldown
            # that blocks every later run. Isolation must not become silence,
            # so it is loud on stderr instead.
            print(f"[reddit] target {label} failed: {exc.__class__.__name__}: {exc}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return []
        self.guardrails.record_success(self.name, ActionKind.SCRAPE)
        self.guardrails.sleep_for(self.name, ActionKind.SCRAPE)
        return signals

    def _enrich_posts(
        self,
        page,
        posts: list[dict[str, Any]],
        bodies_limit: int,
        comment_limit: int,
    ) -> list[dict[str, Any]]:
        """Open each permalink for body + comments. Returns new dicts.

        Ranked by score so a small limit spends its page loads on the posts
        most likely to matter. A single failed post never aborts the run.
        """
        ranked = sorted(posts, key=lambda p: _as_int(p.get("score")), reverse=True)
        targets = {p.get("url") for p in ranked[:bodies_limit] if p.get("url")}
        out: list[dict[str, Any]] = []
        for post in posts:
            url = post.get("url")
            if url not in targets:
                out.append(post)
                continue
            try:
                detail = scrape_post(page, url, comment_limit=comment_limit)
            except Exception as exc:  # noqa: BLE001 - one bad post must not kill the run
                out.append({**post, "detail_error": str(exc)})
                continue
            # Listing values win only for the structured attributes it reads
            # reliably. Everything else must come from the detail page — the
            # listing's `comments` is a count string, and letting it win
            # silently overwrote the detail page's comment list.
            keep = {
                k: v
                for k, v in post.items()
                if v and k in _LISTING_WINS
            }
            out.append({**detail, **keep})
        return out

    def scrape_url(self, url: str) -> Signal:
        with BrowserSession(self.cfg, self.name) as session:
            post = scrape_post(session.page, url)
            return self._map_posts([post], "url")[0]

    def publish(self, draft: Draft) -> ActionResult:
        action = draft.action
        sub = draft.metadata.get("subreddit", "")
        try:
            if action == ActionKind.COMMENT:
                self.guardrails.assert_action(self.name, ActionKind.COMMENT, draft.text)
            elif action == ActionKind.POST:
                self.guardrails.assert_action(
                    self.name, ActionKind.POST, draft.text, subreddit=sub
                )
            elif action == ActionKind.LIKE:
                self.guardrails.assert_action(self.name, ActionKind.LIKE)
            else:
                return ActionResult(False, self.name, action, "unsupported action")
        except GuardrailViolation as exc:
            return ActionResult(False, self.name, action, str(exc))

        if self.guardrails.dry_run:
            return self._dry_result(draft)

        with BrowserSession(self.cfg, self.name) as session:
            page = session.page
            try:
                body_text = page_text(page) if page.url else ""
                hits = self.guardrails.scan_ban_signals(self.name, body_text)
                if hits:
                    msg = f"ban signals: {hits}"
                    self.guardrails.record_error(self.name, msg)
                    return ActionResult(False, self.name, action, msg)

                if action == ActionKind.COMMENT:
                    post_comment(page, draft.target_url, draft.text)
                    self.guardrails.record_success(self.name, ActionKind.COMMENT, draft.text)
                    return ActionResult(True, self.name, action, "posted comment", draft.target_url)
                if action == ActionKind.POST:
                    title = draft.metadata.get("title") or draft.text.split("\n", 1)[0][:300]
                    body = draft.metadata.get("body") or draft.text
                    url = submit_post(page, sub, title, body)
                    self.guardrails.record_success(self.name, ActionKind.POST, draft.text)
                    return ActionResult(True, self.name, action, "posted submission", url)
                if action == ActionKind.LIKE:
                    outcome = upvote_post(page, draft.target_url)
                    self.guardrails.record_success(self.name, ActionKind.LIKE, draft.target_url)
                    return ActionResult(True, self.name, action, outcome, draft.target_url)
                return ActionResult(False, self.name, action, "unsupported")
            except Exception as exc:
                self.guardrails.record_error(self.name, str(exc))
                return ActionResult(False, self.name, action, str(exc))

    def health_check(self) -> HealthStatus:
        checks = {"enabled": self.guardrails.platform_enabled(self.name)}
        messages: list[str] = []
        with BrowserSession(self.cfg, self.name) as session:
            page = session.page
            try:
                page.goto("https://www.reddit.com/", wait_until="domcontentloaded", timeout=90_000)
                body = page_text(page).lower()
                checks["login"] = "log in" not in body or "avatar" in body
                ban = self.guardrails.scan_ban_signals(self.name, body)
                checks["ban_signals"] = not ban
                if ban:
                    messages.append(str(ban))
            except Exception as exc:
                checks["reachable"] = False
                messages.append(str(exc))
        return HealthStatus(ok=all(checks.values()), platform=self.name, checks=checks, messages=messages)

    def _map_posts(self, posts: list[dict[str, Any]], source: str) -> list[Signal]:
        now = datetime.now(timezone.utc).isoformat()
        out: list[Signal] = []
        for p in posts:
            url = p.get("url") or p.get("post_url") or ""
            sid = p.get("id") or url
            out.append(
                Signal(
                    platform=self.name,
                    signal_id=str(sid),
                    url=url,
                    title=p.get("title") or "",
                    body=p.get("selftext") or p.get("body") or "",
                    author=p.get("author") or source,
                    score=str(p.get("score") or ""),
                    engagement={"comments": p.get("comments"), "score": p.get("score")},
                    scraped_at=now,
                    raw=p,
                )
            )
        return out

    @staticmethod
    def subreddit_from_url(url: str) -> str:
        path = urlparse(url).path.strip("/").split("/")
        if len(path) >= 2 and path[0] == "r":
            return path[1]
        return ""
