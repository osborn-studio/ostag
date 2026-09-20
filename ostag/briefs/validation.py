"""A03 structural validation of private draft candidates, never truth or approval.

A source reference proves only membership in frozen evidence. All prose remains
unverified, including source-reported commitments; numeric/key-fact extraction
is deliberately unsupported here. Real-model and human semantic evaluation are
still required. Coverage comes from trusted collection, not generated claims.

All model text is inert plain text. A05 must escape it and disable Markdown,
HTML and automatic links. Only SourceLink from the trusted connector mapping may
be rendered as a provenance link. The link/completeness checks below reject
obvious escape/assertion forms, not every possible natural-language paraphrase.
A deterministic coverage notice must accompany every rendering of a candidate.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import re
from urllib.parse import urlsplit

from ostag.briefs.models import (
    BriefClaim, SourceSet, _identifier, _invalid, _keys, _sequence, _text, validate_claims,
)

VALIDATION_VERSION = "brief-validation-v1"
OUTPUT_VERSION = "brief-output-v1"
SECTIONS = ("progress", "conflicts", "decisions", "proposals")


def bounded_int(value: object, field: str, maximum: int = 10**12, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise _invalid(f"{field} must be an exact integer in [{minimum}, {maximum}]")
    return value


def strict_json(body: object, maximum: int) -> dict:
    bounded_int(maximum, "JSON byte bound", 1048576, 1)
    if type(body) is not str or len(body) > maximum:
        raise _invalid("response must be bounded JSON text")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise _invalid("duplicate JSON key")
            result[key] = value
        return result
    def constant(_):
        raise _invalid("nonfinite JSON number")
    try:
        if len(body.encode("utf-8")) > maximum:
            raise _invalid("whole response exceeds UTF-8 byte bound")
        value = json.loads(body, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise _invalid("invalid bounded JSON response") from exc
    if type(value) is not dict:
        raise _invalid("response must be one JSON object")
    return value


@dataclass(frozen=True)
class Coverage:
    complete: bool
    gaps: tuple[str, ...]
    version: str = "brief-coverage-v1"

    def __post_init__(self):
        if self.version != "brief-coverage-v1" or type(self.complete) is not bool:
            raise _invalid("unknown coverage version or invalid completeness flag")
        gaps = tuple(_text(g, "gap", 1000) for g in _sequence(self.gaps, "gaps", empty=True))
        if len(set(gaps)) != len(gaps) or self.complete == bool(gaps):
            raise _invalid("partial coverage requires distinct visible gaps; complete forbids gaps")
        object.__setattr__(self, "gaps", gaps)

    def to_dict(self):
        return {"version": self.version, "complete": self.complete, "gaps": list(self.gaps)}

    @classmethod
    def from_dict(cls, value):
        return cls(**_keys(value, ("version", "complete", "gaps")))


@dataclass(frozen=True)
class SourceLink:
    """Only trusted collection code constructs these; never parse model links."""

    source_id: str
    url: str

    def __post_init__(self):
        _identifier(self.source_id, "source_id")
        _text(self.url, "url", 2000)
        try:
            parsed = urlsplit(self.url)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                    or parsed.password or any(c.isspace() or ord(c) < 32 for c in self.url)):
                raise ValueError("unsafe URL")
        except ValueError as exc:
            raise _invalid("trusted provenance URL must be HTTPS without credentials") from exc


def source_links(sources: SourceSet, links: tuple[SourceLink, ...]) -> tuple[SourceLink, ...]:
    links = _sequence(links, "links", empty=True)
    if any(type(link) is not SourceLink for link in links):
        raise _invalid("links require trusted SourceLink instances")
    links = tuple(replace(link) for link in links)
    known = {s.source_id for s in sources.sources}
    if len({link.source_id for link in links}) != len(links) or any(
        link.source_id not in known for link in links
    ):
        raise _invalid("duplicate or unknown source link")
    return links


@dataclass(frozen=True)
class ClaimItem:
    id: str
    claim: BriefClaim

    def __post_init__(self):
        _identifier(self.id, "claim id")
        if type(self.claim) is not BriefClaim:
            raise _invalid("claim must be BriefClaim")
        object.__setattr__(self, "claim", replace(self.claim))

    def to_dict(self):
        return {"id": self.id, "claim": self.claim.to_dict()}


@dataclass(frozen=True)
class ValidatedDraft:
    progress: tuple[ClaimItem, ...]
    conflicts: tuple[ClaimItem, ...]
    decisions: tuple[ClaimItem, ...]
    proposals: tuple[ClaimItem, ...]
    coverage: Coverage
    links: tuple[SourceLink, ...]

    def __post_init__(self):
        for section in SECTIONS:
            items = _sequence(getattr(self, section), section, empty=True)
            if any(type(item) is not ClaimItem for item in items):
                raise _invalid("draft sections require ClaimItems")
            object.__setattr__(self, section, tuple(replace(item) for item in items))
        if type(self.coverage) is not Coverage:
            raise _invalid("trusted coverage required")
        object.__setattr__(self, "coverage", replace(self.coverage))
        links = _sequence(self.links, "links", empty=True)
        if any(type(link) is not SourceLink for link in links):
            raise _invalid("trusted links required")
        object.__setattr__(self, "links", tuple(replace(link) for link in links))

    @property
    def status(self):
        return ("" if self.coverage.complete else "partial_") + "semantic_review_required"

    @property
    def notice(self):
        coverage = ("Collection reports complete coverage." if self.coverage.complete else
                    "Partial collection: " + "; ".join(self.coverage.gaps) + ".")
        return (coverage + " Model claims are not verified; semantic review required. "
                "Commitments are source-reported; proposals and decisions await human review.")

    def to_dict(self):
        return {section: [item.to_dict() for item in getattr(self, section)]
                for section in SECTIONS} | {
            "coverage": self.coverage.to_dict(), "status": self.status, "notice": self.notice,
            "links": [{"source_id": link.source_id, "url": link.url} for link in self.links],
            "validation_version": VALIDATION_VERSION,
        }


# Match explicit URI/link syntax, not every word followed by a colon or every
# dotted filename. This is an escape-form filter, not a semantic truth check;
# preserving text verbatim still requires the plain-text rendering contract above.
# Schemes need a word boundary and a URI body, so "profile:" and "Data: ..."
# remain prose. HTML needs a tag-like opener, not spaced comparison operators.
_LINK = re.compile(
    r"[a-z][a-z0-9+.-]*://|(?<!\w)(?:javascript|vbscript|data|file|mailto):(?=\S)|"
    r"//|www\.|\]\s*\(|\]\s*:|<(?:/?[a-z][a-z0-9:-]*(?=[\s/>])|[!?])[^>]*>|"
    r"\b[a-z0-9-]+\.[a-z]{2,}/|"
    r"\b[a-z0-9-]+\.(?:com|org|net|io|dev|test|invalid|example)\b", re.I)

_COMPLETENESS = re.compile(
    r"\b(?:all|every)\s+(?:sources?|materials?|records?|pages?|data)\b|"
    r"\bcomplete\s+(?:overview|coverage|summary|picture)\b|"
    r"(?:全部|所有|完整).{0,12}(?:资料|材料|覆盖|读取|总结)|已覆盖全部", re.I)


def validate_output(body: str, *, sources: SourceSet, coverage: Coverage,
                    max_output_bytes: int, links: tuple[SourceLink, ...] = (),
                    version: str = VALIDATION_VERSION) -> ValidatedDraft:
    if version != VALIDATION_VERSION or type(sources) is not SourceSet or type(coverage) is not Coverage:
        raise _invalid("known validation version and trusted source/coverage contracts required")
    sources, coverage = replace(sources), replace(coverage)
    links = source_links(sources, links)
    payload = _keys(strict_json(body, max_output_bytes), ("version", *SECTIONS))
    if payload["version"] != OUTPUT_VERSION:
        raise _invalid("unknown output version")
    sections, ids, claims = {}, set(), []
    usable = {s.source_id for s in sources.sources if s.excerpt.strip()}
    for section in SECTIONS:
        items = []
        for value in _sequence(payload[section], section, empty=True):
            value = _keys(value, ("id", "claim"))
            item = ClaimItem(value["id"], BriefClaim.from_dict(value["claim"]))
            if item.id in ids:
                raise _invalid("duplicate claim ID")
            ids.add(item.id)
            claim = item.claim
            if not set(claim.source_ids) <= usable:
                raise _invalid("claim cites unknown or unusable evidence")
            if (section in ("decisions", "proposals")) != (claim.kind == "proposal"):
                raise _invalid("facts/commitments and pending decisions/proposals must stay distinct")
            if _LINK.search(claim.text):
                raise _invalid("model text contains a link escape; only trusted mapping supplies links")
            if not coverage.complete and _COMPLETENESS.search(claim.text):
                raise _invalid("partial collection cannot assert global completeness")
            claims.append(claim)
            items.append(item)
        sections[section] = tuple(items)
    validate_claims(sources, tuple(claims))
    if not claims:
        raise _invalid("empty draft has no usable claims")
    return ValidatedDraft(**sections, coverage=coverage, links=links)
