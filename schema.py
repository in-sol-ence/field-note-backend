"""Pydantic models for T0 product understanding.

This module is the contract between T0 and the downstream pipeline stages
(T1 source selection, T2 post filtering), and the source of the Go CLI's
generated structs. Treat field renames here as breaking changes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# Request
# --------------------------------------------------------------------------


class PreprocessRequest(BaseModel):
    """Onboarding inputs. Only the website is required."""

    website: str
    name: str | None = None
    repo: str | None = None
    form: str | None = Field(
        default=None,
        description="Free-text detail form. Treated as trusted, high-weight context.",
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
]


class StageEvent(BaseModel):
    event: Literal["stage"] = "stage"
    stage: Stage
    status: Literal["running", "done"]
    detail: str | None = None


class ErrorEvent(BaseModel):
    event: Literal["error"] = "error"
    stage: Stage | None = None
    detail: str
    fatal: bool = False


class ResultEvent(BaseModel):
    event: Literal["result"] = "result"
    dossier: ProductDossier


Event = StageEvent | ErrorEvent | ResultEvent
