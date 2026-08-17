"""Pydantic models for T0 product understanding.

This module is the contract between T0 and the downstream pipeline stages
(T1 source selection, T2 post filtering), and the source of the Go CLI's
generated structs. Treat field renames here as breaking changes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# Request
# --------------------------------------------------------------------------


class PreprocessRequest(BaseModel):
    """Onboarding inputs. At least one of website or repo is required."""

    website: str | None = None
    name: str | None = None
    repo: str | None = None
    form: str | None = Field(
        default=None,
        description="Free-text detail form. Treated as trusted, high-weight context.",
    )
    stop_after: Literal["t0", "t1", "t2"] = Field(
        default="t1",
        description="Last stage to run. 't0' returns the dossier without scraping.",
    )
    # T1 scrape backends — one or both. Go CLI: --scrape-x / --scrape-social.
    scrape_x: bool = Field(
        default=True,
        description="Live X via local x-scraper (Playwright).",
    )
    scrape_social: bool = Field(
        default=True,
        description="Reddit + Hacker News via social-signals (:8899).",
    )


# --------------------------------------------------------------------------
# Dossier
# --------------------------------------------------------------------------


class PackageRef(BaseModel):
    registry: str
    name: str


class Identity(BaseModel):
    canonical_name: str
    slug: str
    aliases: list[str] = Field(default_factory=list)
    tagline: str | None = None
    homepage: str | None = None
    repo: str | None = None
    docs_url: str | None = None
    package_names: list[PackageRef] = Field(default_factory=list)
    cli_commands: list[str] = Field(default_factory=list)
    org_or_owner: str | None = None
    official_accounts: dict[str, str] = Field(default_factory=dict)


class What(BaseModel):
    category: str | None = None
    subcategory: str | None = None
    description: str | None = None
    primary_use_cases: list[str] = Field(default_factory=list)
    target_users: list[str] = Field(default_factory=list)
    key_features: list[str] = Field(default_factory=list)
    tech_stack: list[str] = Field(default_factory=list)
    pricing_model: str | None = None
    license: str | None = None
    maturity: str | None = None


class Vocabulary(BaseModel):
    """How real users refer to the product when they talk about it."""

    user_terms: list[str] = Field(default_factory=list)
    common_misspellings: list[str] = Field(default_factory=list)
    feature_jargon: list[str] = Field(
        default_factory=list,
        description="Product-specific words with no namesakes. Often a better "
        "search key than the product name itself.",
    )
    adjacent_products: list[str] = Field(default_factory=list)


class NameCollision(BaseModel):
    """Something else that shares this product's name. Observed, never recalled."""

    name: str
    what_it_is: str
    domain: str | None = None
    why_confusable: str | None = None
    evidence_url: str


class DisambiguationDraft(BaseModel):
    """The part of disambiguation the model is allowed to write."""

    name_collisions: list[NameCollision] = Field(default_factory=list)
    positive_signals: list[str] = Field(default_factory=list)
    negative_signals: list[str] = Field(default_factory=list)
    notes: str | None = None


class Disambiguation(DisambiguationDraft):
    """Draft plus the score we compute ourselves rather than ask for."""

    ambiguity_score: float = Field(ge=0.0, le=1.0)


class Source(BaseModel):
    url: str
    fetched_at: datetime
    via: Literal["firecrawl", "exa", "http"]


class Provenance(BaseModel):
    sources: list[Source] = Field(default_factory=list)
    field_confidence: dict[str, float] = Field(default_factory=dict)
    degraded_sources: list[str] = Field(default_factory=list)
    generated_at: datetime
    runtime_ms: int


class SynthesisDraft(BaseModel):
    """Exactly what Grok returns. Provenance and ambiguity_score are ours."""

    identity: Identity
    what: What
    vocabulary: Vocabulary
    disambiguation: DisambiguationDraft
    field_confidence: dict[str, float] = Field(default_factory=dict)


class ProductDossier(BaseModel):
    identity: Identity
    what: What
    vocabulary: Vocabulary
    disambiguation: Disambiguation
    provenance: Provenance


# --------------------------------------------------------------------------
# T1 — sources and posts
# --------------------------------------------------------------------------

Platform = Literal["reddit", "x", "hackernews", "github", "substack"]


class RedditTargets(BaseModel):
    """Where on Reddit this product is actually discussed.

    Subreddits alone miss most of it: in the reference scrape, 12 of 27 Reddit
    signals came from site-wide search, in subreddits nobody would have listed
    up front. So queries carry at least as much weight as subreddits.
    """

    subreddits: list[str] = Field(default_factory=list)
    search_queries: list[str] = Field(default_factory=list)


class HackerNewsTargets(BaseModel):
    search_queries: list[str] = Field(default_factory=list)


class XTargets(BaseModel):
    search_queries: list[str] = Field(default_factory=list)


class SubstackTargets(BaseModel):
    """Where on Substack to look. Topic is the primary key.

    Publications are optional — when omitted, publication search is derived
    from `topics`. Search queries are an alias for topics.
    """

    topics: list[str] = Field(default_factory=list)
    publications: list[str] = Field(default_factory=list)
    search_queries: list[str] = Field(default_factory=list)


class ScrapeTargets(BaseModel):
    """T1's output: what to scrape.

    `topic` is the primary social-signal aim (general sentiment). Product /
    repo discriminators are optional and live on the dossier, not here.
    """

    topic: str | None = Field(
        default=None,
        description="Primary scrape aim. When set, Substack (and fallbacks) "
        "search this instead of requiring a product name.",
    )
    reddit: RedditTargets = Field(default_factory=RedditTargets)
    hackernews: HackerNewsTargets = Field(default_factory=HackerNewsTargets)
    x: XTargets = Field(default_factory=XTargets)
    substack: SubstackTargets = Field(default_factory=SubstackTargets)
    rationale: str | None = Field(
        default=None,
        description="Why these targets, in one or two sentences. Shown to the "
        "operator so a bad expansion is visible before budget is spent.",
    )


class PostComment(BaseModel):
    author: str | None = None
    body: str
    score: int | None = None
    depth: int = 0
    url: str | None = None
    is_op: bool = False


class Post(BaseModel):
    """One scraped discussion, normalized across platforms by scraping/mapper.py.

    This is the frozen contract T2 consumes. Field names match mapper.py's
    output exactly — it is the producer, this is the schema for what it makes.
    """

    id: str
    source: Platform
    source_id: str | None = None
    url: str
    channel: str | None = None
    title: str | None = None
    body: str = ""
    author: str | None = None
    created_at: str | None = None
    created_at_is_scrape_time: bool = False
    score: int | None = None
    num_comments: int | None = None
    comments: list[PostComment] = Field(default_factory=list)
    scraped_at: str | None = None
    search_query: str | None = None
    language: str = "en"
    relevance: float | None = None


class Harvest(BaseModel):
    """T1's result: the targets chosen, and what came back from them."""

    targets: ScrapeTargets
    posts: list[Post] = Field(default_factory=list)
    live: bool = Field(
        description="True when posts came from a live scrape. False means the "
        "scraper was unreachable and fixtures were substituted."
    )
    source_note: str
    mapping_errors: list[str] = Field(default_factory=list)


class FieldNote(BaseModel):
    """Everything the run produced. Grows a field per stage as T3-T5 land."""

    dossier: ProductDossier
    harvest: Harvest | None = None
    # T2's clustered findings, shaped for the dashboard. A plain dict because
    # enriched.py owns the shape and re-declaring it here would give two
    # definitions to keep in step.
    report: dict[str, Any] | None = None


# --------------------------------------------------------------------------
# SSE events
# --------------------------------------------------------------------------

Stage = Literal[
    "map",
    "scrape_site",
    "scrape_repo",
    "search_collisions",
    "find_similar",
    "search_context",
    "synthesize",
    # T1
    "select_sources",
    "scrape_reddit",
    "scrape_hackernews",
    "scrape_substack",
    "scrape_x",
    "map_posts",
    # T2
    "extract_issues",
    "build_report",
]


class StageEvent(BaseModel):
    event: Literal["stage"] = "stage"
    stage: Stage
    # "failed" means the stage finished having produced nothing. The run
    # continues — every stage is degradable — but the row should not read as a
    # success in the UI. "skipped" means the stage was never attempted (a
    # repo-only run has no site to map).
    status: Literal["running", "done", "failed", "skipped"]
    detail: str | None = None


class ErrorEvent(BaseModel):
    event: Literal["error"] = "error"
    stage: Stage | None = None
    detail: str
    fatal: bool = False


class DossierEvent(BaseModel):
    """T0 landed. Emitted before T1 starts so the CLI can render it during the
    minutes the scrape takes, rather than holding a finished dossier back."""

    event: Literal["dossier"] = "dossier"
    dossier: ProductDossier


class HarvestEvent(BaseModel):
    event: Literal["harvest"] = "harvest"
    harvest: Harvest


class HeartbeatEvent(BaseModel):
    """Keeps the connection and the UI alive across a multi-minute scrape."""

    event: Literal["heartbeat"] = "heartbeat"
    elapsed_ms: int


class ReportEvent(BaseModel):
    """T2's findings, emitted as soon as they are built so the dashboard can
    render before the run formally ends."""

    event: Literal["report"] = "report"
    report: dict[str, Any]


class ResultEvent(BaseModel):
    """Terminal event. Always last, always exactly one."""

    event: Literal["result"] = "result"
    note: FieldNote


Event = (
    StageEvent
    | ErrorEvent
    | DossierEvent
    | HarvestEvent
    | HeartbeatEvent
    | ReportEvent
    | ResultEvent
)
