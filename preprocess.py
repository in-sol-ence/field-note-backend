"""T0 — product understanding.

Turns three onboarding inputs (website, optional repo, optional detail form)
into a ProductDossier: what the product is, the vocabulary real users use for
it, and — the part that actually matters downstream — which *other* things
share its name.

Public API (this is the seam main.py calls across):

    preprocess(website, repo, form)        -> ProductDossier
    preprocess_stream(website, repo, form) -> AsyncIterator[Event]

Knows nothing about HTTP. Testable by direct call.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator, Awaitable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from schema import (
    Disambiguation,
    DossierEvent,
    ErrorEvent,
    Event,
    ProductDossier,
    Provenance,
    Source,
    StageEvent,
    SynthesisDraft,
)

# --------------------------------------------------------------------------
# Settings and clients
# --------------------------------------------------------------------------


class Settings(BaseSettings):
    firecrawl_api_key: str = ""
    exa_api_key: str = ""

    # XAI_API_KEY is the team-wide name, shared with models/grok.py. Override
    # LLM_BASE_URL/LLM_MODEL to borrow an OpenAI-compatible gateway; clearing
    # the overrides restores grok-4.6.
    xai_api_key: str = ""
    llm_base_url: str = "https://api.x.ai/v1"
    llm_model: str = "grok-4.6"

    # T1 scraping. Not in require_keys: an unreachable scraper degrades the run
    # to recorded signals rather than ending it, so it is never a hard stop.
    social_signals_url: str = "http://127.0.0.1:8899"
    social_signals_api_key: str = "demo-key"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


class MissingCredentials(RuntimeError):
    """Raised before any spend when a required key is absent."""


def require_keys() -> Settings:
    s = get_settings()
    missing = [
        name
        for name, value in (
            ("FIRECRAWL_API_KEY", s.firecrawl_api_key),
            ("EXA_API_KEY", s.exa_api_key),
            ("XAI_API_KEY", s.xai_api_key),
        )
        if not value
    ]
    if missing:
        raise MissingCredentials(f"missing required env var(s): {', '.join(missing)}")
    return s


def _firecrawl():
    from firecrawl import AsyncFirecrawl

    return AsyncFirecrawl(api_key=get_settings().firecrawl_api_key)


def _exa():
    from exa_py import AsyncExa

    return AsyncExa(get_settings().exa_api_key)


def _llm():
    from openai import AsyncOpenAI

    s = get_settings()
    return AsyncOpenAI(api_key=s.xai_api_key, base_url=s.llm_base_url)


# --------------------------------------------------------------------------
# Pure helpers — the unit-tested core. No network, no globals.
# --------------------------------------------------------------------------

_HIGH_VALUE = {
    "pricing": 100,
    "plans": 90,
    "docs": 90,
    "documentation": 90,
    "features": 85,
    "about": 80,
    "product": 75,
    "how-it-works": 70,
    "use-cases": 70,
    "usecases": 70,
    "faq": 65,
    "changelog": 60,
    "getting-started": 60,
    "quickstart": 60,
}

_PENALTY = {
    "privacy": -200,
    "terms": -200,
    "legal": -200,
    "cookies": -200,
    "careers": -150,
    "jobs": -150,
    "login": -150,
    "signin": -150,
    "signup": -150,
    "tag": -80,
    "author": -80,
}

_DATED = re.compile(r"(?:^|/)(?:19|20)\d{2}(?:[/-]|$)")


def slugify(name: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.strip().lower())).strip("-") or "product"


def normalize_website(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url.rstrip("/")


def normalize_repo(repo: str | None) -> str | None:
    """Accept `owner/repo`, a full GitHub URL, or a `.git` suffix -> `owner/repo`."""
    if not repo or not repo.strip():
        return None
    value = repo.strip().rstrip("/")
    value = re.sub(r"\.git$", "", value)
    if "github.com" in value:
        path = urlparse(value if "://" in value else "https://" + value).path
        value = path.strip("/")
    parts = [p for p in value.split("/") if p]
    if len(parts) < 2:
        return None
    return f"{parts[0]}/{parts[1]}"


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


# Two-part public suffixes common enough to matter. Not the full Public Suffix
# List, which would mean a network-fetched dependency; enough that two unrelated
# .co.uk sites are not read as one owner, which would suppress a real collision.
_MULTI_PART_TLDS = frozenset({
    "ac.uk", "co.uk", "gov.uk", "me.uk", "net.uk", "org.uk",
    "com.au", "net.au", "org.au", "co.nz", "co.za", "co.il", "co.in",
    "co.jp", "ne.jp", "or.jp", "co.kr", "co.th", "co.id", "co.ke",
    "com.ar", "com.bd", "com.br", "com.cn", "com.eg", "com.hk", "com.mx",
    "com.my", "com.ng", "com.pk", "com.ph", "com.pl", "com.sa", "com.sg",
    "com.tr", "com.tw", "com.ua", "com.vn", "net.in", "org.in",
})


def registrable(hostname: str) -> str:
    """eTLD+1, so docs.foo.com and foo.com read as one owner but foo.co.uk and
    bar.co.uk do not."""
    parts = hostname.split(".")
    if len(parts) < 2:
        return hostname
    if len(parts) >= 3 and ".".join(parts[-2:]) in _MULTI_PART_TLDS:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def score_url(url: str) -> int:
    path = urlparse(url).path.strip("/").lower()
    if not path:
        return -1000  # homepage is scraped separately
    segments = [s for s in path.split("/") if s]
    # max, not sum: a deep path shouldn't outrank /pricing by accumulating
    # segments. Penalties stay additive so one bad segment still sinks the URL.
    score = max((_HIGH_VALUE.get(s, 0) for s in segments), default=0)
    score += sum(_PENALTY.get(s, 0) for s in segments)
    if _DATED.search("/" + path):
        score -= 60
    score -= 8 * (len(segments) - 1)
    return score


def rank_sitemap_urls(urls: Sequence[str], limit: int = 5) -> list[str]:
    """Pick the pages most likely to explain the product. Order is stable."""
    seen = list(dict.fromkeys(urls))
    scored = sorted(
        ((score_url(u), i, u) for i, u in enumerate(seen)),
        key=lambda t: (-t[0], t[1]),
    )
    return [u for s, _, u in scored if s > 0][:limit]


def partition_results(
    urls: Sequence[str], product_hosts: Iterable[str]
) -> tuple[list[str], list[str]]:
    """Split search hits into product-owned vs everything else."""
    owned = {registrable(h) for h in product_hosts if h}
    mine, other = [], []
    for u in urls:
        (mine if registrable(host_of(u)) in owned else other).append(u)
    return mine, other


def collision_hosts(collisions: Iterable) -> set[str]:
    """Registrable domains attributable to identified rivals."""
    hosts = set()
    for c in collisions:
        if getattr(c, "evidence_url", ""):
            hosts.add(registrable(host_of(c.evidence_url)))
        if getattr(c, "domain", None):
            hosts.add(registrable(str(c.domain).lower().removeprefix("www.")))
    return {h for h in hosts if h}


def ambiguity_score(urls: Sequence[str], collisions: Iterable) -> float:
    """Share of the bare-name result space taken by *identified* rivals.

    Grounded in the observed collision list rather than in mere non-ownership.
    Scoring "results that aren't mine" reads 1.00 for any product too new to
    rank for its own name — maximally contested — when in truth nothing else
    shares the name, and that would tell T2 to filter hard against a clean one.
    A score can therefore never contradict the collision list.
    """
    hosts = collision_hosts(collisions)
    if not urls or not hosts:
        return 0.0
    hits = sum(1 for u in urls if registrable(host_of(u)) in hosts)
    return round(hits / len(urls), 3)


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def inputs_look_related(site: str, repo: str | None, site_text: str, repo_text: str) -> bool:
    """Do the website and the repo describe the same product?

    A mistyped or pasted-from-elsewhere repo produces a confident dossier that
    silently blends two unrelated products, which is worse than failing: every
    downstream stage inherits the mixture.
    """
    if not repo:
        return True
    domain = registrable(host_of(site))
    owner, _, name = repo.partition("/")
    if domain and domain in repo_text.lower():
        return True
    squashed_site = _squash(site_text)
    for token in (_squash(repo), _squash(name), _squash(owner)):
        if len(token) >= 5 and token in squashed_site:
            return True
    # the site's own domain label, e.g. "codexisland" from codexisland.com
    label = _squash(domain.split(".")[0]) if domain else ""
    return bool(label) and len(label) >= 5 and label in _squash(repo_text)


def drop_unsourced_collisions(
    draft: SynthesisDraft, fetched_urls: Iterable[str]
) -> tuple[SynthesisDraft, list[str]]:
    """Discard any collision citing a URL we never actually fetched.

    The prompt says "use only supplied evidence", but prompts leak. This is the
    deterministic backstop: a fabricated namesake would misdirect every
    downstream scrape, so membership must trace to something we really saw.
    """
    allowed = {u.rstrip("/") for u in fetched_urls}
    kept, dropped = [], []
    for c in draft.disambiguation.name_collisions:
        if c.evidence_url.rstrip("/") in allowed:
            kept.append(c)
        else:
            dropped.append(f"dropped unsourced collision {c.name!r} ({c.evidence_url})")
    draft.disambiguation.name_collisions = kept
    return draft, dropped


# --------------------------------------------------------------------------
# Evidence accumulator
# --------------------------------------------------------------------------


@dataclass
class Evidence:
    blocks: list[tuple[str, str]] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    hosts: set[str] = field(default_factory=set)
    bare_name_urls: list[str] = field(default_factory=list)

    def add(self, label: str, text: str, url: str, via: str, cap: int = 6000) -> None:
        if not text:
            return
        self.blocks.append((label, text[:cap]))
        self.sources.append(
            Source(url=url, fetched_at=datetime.now(timezone.utc), via=via)  # type: ignore[arg-type]
        )

    def urls(self) -> list[str]:
        return [s.url for s in self.sources]

    def prompt_text(self) -> str:
        return "\n\n".join(f"### {label}\n{body}" for label, body in self.blocks)


# --------------------------------------------------------------------------
# Source adapters
# --------------------------------------------------------------------------

_MANIFESTS = ("package.json", "pyproject.toml", "Cargo.toml", "go.mod")


async def _map_site(url: str, limit: int = 150) -> list[str]:
    data = await _firecrawl().map(url, limit=limit)
    return [ln.url for ln in (data.links or []) if getattr(ln, "url", None)]


async def _scrape(url: str) -> str:
    doc = await _firecrawl().scrape(url, formats=["markdown"], only_main_content=True)
    return getattr(doc, "markdown", "") or ""


async def _scrape_meta(url: str) -> tuple[str, dict]:
    doc = await _firecrawl().scrape(url, formats=["markdown"], only_main_content=True)
    meta = getattr(doc, "metadata", None) or {}
    if not isinstance(meta, dict):
        meta = getattr(meta, "model_dump", lambda: {})()
    return (getattr(doc, "markdown", "") or ""), meta


async def _fetch_manifest(repo: str) -> tuple[str, str] | None:
    """Read a dependency manifest straight off raw.githubusercontent.

    Static file host, no API key, no Firecrawl credit — the package name it
    yields is the least collision-prone identity signal we get.
    """
    async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
        for fname in _MANIFESTS:
            url = f"https://raw.githubusercontent.com/{repo}/HEAD/{fname}"
            try:
                r = await client.get(url)
            except httpx.HTTPError:
                continue
            if r.status_code == 200 and r.text.strip():
                return url, r.text[:4000]
    return None


async def _exa_search(
    query: str, n: int = 10, exclude_domains: list[str] | None = None
) -> list[tuple[str, str, str]]:
    res = await _exa().search(query, num_results=n, exclude_domains=exclude_domains or None)
    return [
        (r.url, getattr(r, "title", "") or "", (getattr(r, "text", "") or "")[:400])
        for r in res.results
    ]


async def _exa_similar(url: str, n: int = 10) -> list[tuple[str, str, str]]:
    res = await _exa().find_similar(url, num_results=n, exclude_source_domain=True)
    return [
        (r.url, getattr(r, "title", "") or "", (getattr(r, "text", "") or "")[:300])
        for r in res.results
    ]


async def _collision_probe(
    name: str, product_host: str
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    """Two searches, because one cannot answer both questions.

    Exa's neural search resolves a bare product name to its dominant entity, so
    the unfiltered pass measures how much of the name space the product owns
    (that is the ambiguity score) but buries every namesake. Excluding the
    product's own domain is what surfaces the rivals worth labelling.
    """
    namespace, rivals = await asyncio.gather(
        _exa_search(name, n=12),
        _exa_search(name, n=10, exclude_domains=[product_host] if product_host else None),
    )
    return namespace, rivals


def _fmt_hits(hits: Sequence[tuple[str, str, str]]) -> str:
    return "\n".join(f"- {url}\n  title: {title}\n  excerpt: {snip}" for url, title, snip in hits)


# --------------------------------------------------------------------------
# Synthesis
# --------------------------------------------------------------------------

_SYSTEM = """You analyse software products so that a downstream scraper can \
find real users discussing THIS product and not something else that happens to \
share its name.

Absolute rules:
1. Use ONLY the supplied evidence. Never rely on prior knowledge of the product.
2. Every name_collision MUST cite an evidence_url copied verbatim from the \
evidence. A collision you cannot cite must be omitted entirely.
3. A name_collision is a DIFFERENT ENTITY that shares the name (a word, another \
company, a game, a technical term) — never a competitor in the same category, \
and never third-party coverage of this product.
3a. The product's WHOLE name must appear in the collision. Direction matters:
    - REJECT things matching only a fragment of the name. For "CodexIsland", \
things called just "Codex" or just "Island" are not collisions — the product's \
own vocabulary contains those words, so filtering on them would discard genuine \
discussion of this very product.
    - ACCEPT longer phrases that contain the whole name in an unrelated sense. \
For "Linear", both "linear algebra" and "linear equation" qualify, because \
someone searching the bare name is flooded with them.
4. If the evidence does not support a field, use null or an empty list. Do not guess.
5. feature_jargon should hold distinctive product-specific terms that would \
rarely appear in unrelated text — these are the highest-value search keys.
6. positive_signals are terms whose presence alongside the name confirms it IS \
this product. negative_signals indicate it is NOT.

Return a single JSON object. No markdown fence, no commentary."""


def _build_prompt(name: str, website: str, repo: str | None, form: str | None, ev: Evidence) -> str:
    parts = [f"PRODUCT NAME (as given): {name}", f"WEBSITE: {website}"]
    if repo:
        parts.append(f"GITHUB REPO: {repo}")
    if form:
        parts.append(
            "OPERATOR-SUPPLIED DESCRIPTION (trusted — weight this heavily):\n" + form.strip()
        )
    parts.append("\n--- EVIDENCE ---\n" + ev.prompt_text())
    parts.append(
        "\n--- OUTPUT SCHEMA ---\n"
        + json.dumps(SynthesisDraft.model_json_schema(), indent=1)
    )
    return "\n\n".join(parts)


# The top-level keys of SynthesisDraft, in the order the model writes them.
# Watching them arrive in the token stream is the only honest way to say what
# synthesis is doing: it is a single call, and it is the run's longest wait.
_DRAFT_SECTIONS: tuple[tuple[str, str], ...] = (
    ("identity", "naming the product"),
    ("what", "describing what it does"),
    ("vocabulary", "collecting its vocabulary"),
    ("disambiguation", "sorting out name collisions"),
    ("field_confidence", "scoring its own confidence"),
)

# How much of the tail to keep while watching for the next section key, and how
# long the note may go unchanged before it reports the draft growing instead.
_TAIL_WINDOW = 64
_NOTE_EVERY = 2.0


def _section_reached(tail: str, seen: set[str]) -> str | None:
    """Label for the first unseen draft section named in tail, if any.

    Reads a sliding window rather than the whole draft so the scan stays cheap
    across thousands of chunks; keys split across a chunk boundary still land
    inside it.
    """
    for key, label in _DRAFT_SECTIONS:
        if key not in seen and f'"{key}"' in tail:
            seen.add(key)
            return label
    return None


def _draft_note(model: str, section: str, written: int) -> str:
    """One sub-step line: the model, what it is writing, and how much of it."""
    if written < 1000:
        return f"{model} · {section}"
    return f"{model} · {section} · {written / 1000:.1f}k chars"


async def _synthesize(prompt: str, blocks: int) -> AsyncIterator[str | SynthesisDraft]:
    """One call, then at most one repair seeded with the validation error.

    Yields a note before each model call and the draft itself last. The notes
    exist because this stage is the run's longest single wait: without them the
    UI can only say "synthesizing" for a minute, and a silent repair attempt is
    indistinguishable from a hang.
    """
    client, s = _llm(), get_settings()
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": prompt},
    ]
    last_err: Exception | None = None
    for attempt in range(2):
        section = "reading %d evidence blocks" % blocks
        if attempt:
            section = "repairing the schema it broke"
        yield _draft_note(s.llm_model, section, 0)

        # Streamed for the notes, not for the output: the draft is only usable
        # once it is whole, but the tokens on the way tell the operator which
        # part of the dossier is being written right now.
        parts: list[str] = []
        written, tail, seen = 0, "", set()
        last_note = time.monotonic()
        try:
            stream = await client.chat.completions.create(
                model=s.llm_model,
                messages=messages,
                response_format={"type": "json_object"},
                stream=True,
            )
            async for chunk in stream:
                piece = chunk.choices[0].delta.content or "" if chunk.choices else ""
                if not piece:
                    continue
                parts.append(piece)
                written += len(piece)
                tail = (tail + piece)[-_TAIL_WINDOW:]
                if label := _section_reached(tail, seen):
                    section = label
                elif time.monotonic() - last_note < _NOTE_EVERY:
                    continue
                yield _draft_note(s.llm_model, section, written)
                last_note = time.monotonic()
            raw = "".join(parts)
        except Exception as exc:  # noqa: BLE001 - degrade, don't die
            # The notes are a nicety; the dossier is the product. A provider
            # that will not stream this request still answers it in one piece,
            # so fall back rather than failing the run for want of progress
            # text. A stream that died mid-draft is a real failure, though —
            # retrying it blind would pay for the same tokens twice.
            if written:
                raise
            # The reason rides along in the note rather than a log line: this
            # module reports everything else it survives to the operator, and
            # a silent fallback is exactly the blind minute being fixed here.
            reason = str(exc).splitlines()[0][:60] or type(exc).__name__
            section = f"cannot stream ({reason}), drafting blind"
            yield _draft_note(s.llm_model, section, 0)
            resp = await client.chat.completions.create(
                model=s.llm_model,
                messages=messages,
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content or ""

        raw = raw or "{}"
        try:
            yield SynthesisDraft.model_validate_json(raw)
            return
        except ValidationError as e:
            last_err = e
            messages += [
                {"role": "assistant", "content": raw[:4000]},
                {
                    "role": "user",
                    "content": (
                        "That JSON failed schema validation. Fix ONLY the listed "
                        f"problems and return the corrected object:\n{e}"
                    ),
                },
            ]
    raise ValueError(f"synthesis failed schema validation after repair: {last_err}")


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


async def _labelled(stage: str, coro: Awaitable):
    try:
        return stage, await coro, None
    except Exception as exc:  # noqa: BLE001 - degrade, don't die
        return stage, None, exc


def _context_terms(meta: dict, fallback: str) -> str:
    text = " ".join(str(meta.get(k, "")) for k in ("description", "title")).strip()
    words = re.findall(r"[A-Za-z][A-Za-z0-9+.-]{2,}", text)[:6]
    return " ".join(words) or fallback


async def preprocess_stream(
    website: str,
    repo: str | None = None,
    form: str | None = None,
    name: str | None = None,
) -> AsyncIterator[Event]:
    """Run T0, surfacing progress as each stage lands."""
    started = time.monotonic()
    settings = require_keys()  # fail before any spend
    site = normalize_website(website)
    repo_slug = normalize_repo(repo)
    guess_name = (name or "").strip() or host_of(site).split(".")[0]

    ev = Evidence()
    ev.hosts.add(host_of(site))
    if repo_slug:
        ev.hosts.add("github.com")

    # ---- Stage A: ground truth + collision hunt, all at once -------------
    jobs: list[tuple[str, Awaitable]] = [
        ("map", _map_site(site)),
        ("scrape_site", _scrape_meta(site)),
        ("search_collisions", _collision_probe(guess_name, host_of(site))),
        ("find_similar", _exa_similar(site, n=8)),
    ]
    if repo_slug:
        jobs.append(("scrape_repo", _scrape(f"https://github.com/{repo_slug}")))

    for stage, _ in jobs:
        yield StageEvent(stage=stage, status="running")  # type: ignore[arg-type]

    sitemap: list[str] = []
    home_meta: dict = {}

    # as_completed, not gather: gather would hold every result until the
    # slowest job lands, so the client would see all five stages finish at the
    # same timestamp instead of watching them arrive.
    tasks = [asyncio.create_task(_labelled(s, c)) for s, c in jobs]
    for finished in asyncio.as_completed(tasks):
        stage, value, err = await finished
        if err is not None:
            ev.degraded.append(f"{stage}: {err}")
            yield ErrorEvent(stage=stage, detail=str(err)[:300], fatal=False)  # type: ignore[arg-type]
            continue
        if stage == "map":
            sitemap = value
            yield StageEvent(stage="map", status="done", detail=f"{len(sitemap)} pages found")
        elif stage == "scrape_site":
            markdown, home_meta = value
            ev.add("Product homepage", markdown, site, "firecrawl")
            yield StageEvent(stage="scrape_site", status="done", detail=site)
        elif stage == "scrape_repo":
            ev.add("GitHub repository page", value, f"https://github.com/{repo_slug}", "firecrawl")
            yield StageEvent(stage="scrape_repo", status="done", detail=repo_slug or "")
        elif stage == "search_collisions":
            namespace, rivals = value
            ev.bare_name_urls = [u for u, _, _ in namespace]
            ev.add(
                f"Open-web search for the bare name {guess_name!r} — who owns this name",
                _fmt_hits(namespace),
                f"exa:search:{guess_name}",
                "exa",
            )
            ev.add(
                f"Same search with {host_of(site)} excluded. These are the strongest "
                "NAME COLLISION candidates: anything here that is a different entity "
                "(a word, a company, a technical term) belongs in name_collisions",
                _fmt_hits(rivals),
                f"exa:search:{guess_name}:rivals",
                "exa",
            )
            # rival URLs must be citable or the guard will drop their collisions
            for u, _, _ in rivals:
                ev.sources.append(
                    Source(url=u, fetched_at=datetime.now(timezone.utc), via="exa")
                )
            yield StageEvent(
                stage="search_collisions",
                status="done",
                detail=f"{len(namespace)} namespace, {len(rivals)} rival hits",
            )
        elif stage == "find_similar":
            ev.add("Semantically adjacent products", _fmt_hits(value), f"exa:similar:{site}", "exa")
            yield StageEvent(stage="find_similar", status="done", detail=f"{len(value)} hits")

    # collision evidence URLs must be citable
    for u in ev.bare_name_urls:
        ev.sources.append(Source(url=u, fetched_at=datetime.now(timezone.utc), via="exa"))

    # ---- coherence: do the website and repo describe the same product? ---
    if repo_slug:
        site_text = " ".join(b for label, b in ev.blocks if "homepage" in label.lower())
        repo_text = " ".join(b for label, b in ev.blocks if "repository" in label.lower())
        if site_text and repo_text and not inputs_look_related(
            site, repo_slug, site_text, repo_text
        ):
            warning = (
                f"website {host_of(site)} and repo {repo_slug} do not appear to "
                "describe the same product — the dossier may blend two of them"
            )
            ev.degraded.append(warning)
            yield ErrorEvent(stage="scrape_repo", detail=warning, fatal=False)

    # ---- Stage B+C: deep read and contextual search, in parallel ---------
    targets = rank_sitemap_urls(sitemap, limit=5)
    context = _context_terms(home_meta, guess_name)

    phase2: list[tuple[str, Awaitable]] = [
        (f"page:{t}", _scrape(t)) for t in targets
    ]
    phase2.append(("search_context", _exa_search(f"{guess_name} {context}", n=8)))
    if repo_slug:
        phase2.append(("manifest", _fetch_manifest(repo_slug)))

    if targets:
        yield StageEvent(stage="scrape_site", status="running", detail=f"{len(targets)} key pages")
    yield StageEvent(stage="search_context", status="running")

    pages_done = 0
    phase2_tasks = [asyncio.create_task(_labelled(s, c)) for s, c in phase2]
    for finished in asyncio.as_completed(phase2_tasks):
        stage, value, err = await finished
        if err is not None:
            ev.degraded.append(f"{stage}: {err}")
            continue
        if stage.startswith("page:"):
            url = stage[5:]
            ev.add(f"Site page {url}", value, url, "firecrawl")
            pages_done += 1
            yield StageEvent(
                stage="scrape_site",
                status="running",
                detail=f"{pages_done}/{len(targets)} key pages",
            )
        elif stage == "manifest" and value:
            url, text = value
            ev.add("Dependency manifest (authoritative package name)", text, url, "http")
        elif stage == "search_context":
            ev.add(
                f"Contextual search {guess_name + ' ' + context!r} (mostly the real product)",
                _fmt_hits(value),
                f"exa:search:{guess_name} {context}",
                "exa",
            )
            yield StageEvent(
                stage="search_context", status="done", detail=f"{len(value)} hits"
            )

    yield StageEvent(
        stage="scrape_site", status="done", detail=f"{len(ev.blocks)} evidence blocks"
    )

    # ---- Stage D: synthesis --------------------------------------------
    yield StageEvent(stage="synthesize", status="running", detail=settings.llm_model)
    draft: SynthesisDraft | None = None
    prompt = _build_prompt(guess_name, site, repo_slug, form, ev)
    async for step in _synthesize(prompt, len(ev.blocks)):
        if isinstance(step, SynthesisDraft):
            draft = step
        else:
            yield StageEvent(stage="synthesize", status="running", detail=step)
    if draft is None:  # pragma: no cover - _synthesize raises instead
        raise ValueError("synthesis produced no draft")

    draft, dropped = drop_unsourced_collisions(draft, ev.urls())
    ev.degraded.extend(dropped)

    dossier = ProductDossier(
        identity=draft.identity,
        what=draft.what,
        vocabulary=draft.vocabulary,
        disambiguation=Disambiguation(
            **draft.disambiguation.model_dump(),
            ambiguity_score=ambiguity_score(
                ev.bare_name_urls, draft.disambiguation.name_collisions
            ),
        ),
        provenance=Provenance(
            sources=ev.sources,
            field_confidence=draft.field_confidence,
            degraded_sources=ev.degraded,
            generated_at=datetime.now(timezone.utc),
            runtime_ms=int((time.monotonic() - started) * 1000),
        ),
    )
    yield StageEvent(stage="synthesize", status="done")
    yield DossierEvent(dossier=dossier)


async def preprocess(
    website: str,
    repo: str | None = None,
    form: str | None = None,
    name: str | None = None,
) -> ProductDossier:
    """Run T0 and return the finished dossier."""
    async for event in preprocess_stream(website, repo, form, name):
        if isinstance(event, DossierEvent):
            return event.dossier
        if isinstance(event, ErrorEvent) and event.fatal:
            raise RuntimeError(event.detail)
    raise RuntimeError("preprocess stream ended without producing a dossier")
