"""Shared data objects for the feedback-analysis pipeline."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


@dataclass
class Comment:
    author: str
    body: str
    score: str


@dataclass
class Engagement:
    # Reddit fields
    score: str | None = None
    comments: list[Comment] = field(default_factory=list)

    # X fields
    likes: int | None = None
    replies: int | None = None
    retweets: int | None = None


@dataclass
class Signal:
    platform: Literal["reddit", "x"]
    signal_id: str
    url: str
    title: str
    body: str
    author: str
    score: str
    engagement: Engagement
    scraped_at: str
    raw: dict[str, Any]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Signal":
        payload = data.copy()
        engagement_data = payload.pop("engagement").copy()
        engagement_data["comments"] = [
            Comment(**comment) for comment in engagement_data.get("comments", [])
        ]
        return cls(**payload, engagement=Engagement(**engagement_data))


@dataclass
class Issue:
    title: str
    summary: str
    product_area: str
    severity: Literal["low", "medium", "high", "critical"]
    signal_ids: list[str]
    evidence: list[str]
    suggested_action: str


@dataclass
class RecommendedFeature:
    title: str
    summary: str
    product_area: str
    priority: Literal["low", "medium", "high"]
    signal_ids: list[str]
    evidence: list[str]
    suggested_action: str


@dataclass
class LovedFeature:
    title: str
    summary: str
    product_area: str
    signal_ids: list[str]
    evidence: list[str]


@dataclass
class SignalFindings:
    """One structured Grok response containing every finding category."""

    issues: list[Issue]
    recommended_features: list[RecommendedFeature]
    loved_features: list[LovedFeature]


