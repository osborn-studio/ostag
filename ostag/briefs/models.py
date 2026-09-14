"""Immutable v1 evidence contracts; no collection, truth checking or approval.

Timestamps are strict UTC strings (Z or +00:00, normalized to Z). Sources
come from trusted collection code; model output supplies only claim source IDs.
URLs must later be resolved through a trusted connector resource mapping.
SHA256 records input integrity, not the truth of a source or a claim.
"""

from __future__ import annotations

import re
import zoneinfo
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from functools import lru_cache
from typing import ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ostag.core.identity import PLATFORMS, Principal
from ostag.core.schemas import SchemaError


def _invalid(message: str) -> SchemaError:
    return SchemaError("invalid_brief_contract", message, "Supply a valid v1 brief contract.")


def _keys(payload: object, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> dict:
    if not isinstance(payload, Mapping) or any(type(key) is not str for key in payload):
        raise _invalid("payload must be a mapping with string keys")
    if set(payload) - set(required + optional) or set(required) - set(payload):
        raise _invalid("payload has unknown or missing keys")
    return dict(payload)


def _text(value: object, field: str, maximum: int = 256, *, empty: bool = False) -> str:
    if type(value) is not str or len(value) > maximum or (not empty and not value.strip()):
        raise _invalid(f"{field} must be a bounded {'optional' if empty else 'nonempty'} string")
    return value


def _identifier(value: object, field: str) -> str:
    value = _text(value, field)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]*", value) or "://" in value:
        raise _invalid(f"{field} must be a stable identifier, without URLs or wildcards")
    return value


def _positive_int(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise _invalid(f"{field} must be a positive integer")
    return value


def _utc(value: object, field: str) -> datetime:
    value = _text(value, field, 32)
    if not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
        r"(?:\.[0-9]{1,6})?(?:Z|\+00:00)", value
    ):
        raise _invalid(f"{field} must be an explicit UTC ISO timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _invalid(f"{field} must be a real calendar timestamp") from exc


def _timestamp(value: object, field: str) -> str:
    return _utc(value, field).isoformat().replace("+00:00", "Z")


def _sequence(value: object, field: str, maximum: int = 500, *, empty: bool = False) -> tuple:
    if type(value) not in (list, tuple) or not (0 if empty else 1) <= len(value) <= maximum:
        raise _invalid(f"{field} must be a bounded {'possibly empty' if empty else 'nonempty'} list")
    return tuple(value)


def _ids(value: object, field: str) -> tuple[str, ...]:
    result = tuple(_identifier(item, field) for item in _sequence(value, field))
    if len(set(result)) != len(result):
        raise _invalid(f"{field} contains duplicates")
    return result


def _principal(value: object) -> Principal:
    if type(value) is not Principal:
        raise _invalid("identity must be a Principal")
    _identifier(value.actor_id, "actor_id")
    if type(value.platform) is not str or value.platform not in PLATFORMS:
        raise _invalid("unsupported principal platform")
    return Principal(value.platform, value.actor_id)


def _principal_from_dict(value: object) -> Principal:
    payload = _keys(value, ("platform", "actor_id"))
    try:
        return _principal(Principal(**payload))
    except ValueError as exc:
        raise _invalid("invalid principal") from exc


def _audience(value: object) -> tuple[Principal, ...]:
    audience = tuple(_principal(item) for item in _sequence(value, "allowed_audience", 100))
    if len(set(audience)) != len(audience):
        raise _invalid("allowed_audience contains duplicates")
    return audience


def _audience_from_dict(value: object) -> tuple[Principal, ...]:
    return tuple(_principal_from_dict(item) for item in _sequence(value, "allowed_audience", 100))


@lru_cache(maxsize=1)
def _timezone_keys() -> frozenset[str]:
    """Exact IANA keys, including aliases; restart the process after tzdata updates."""
    try:
        return frozenset(zoneinfo.available_timezones())
    except OSError as exc:
        raise _invalid("display_timezone must be a valid IANA zone") from exc


@dataclass(frozen=True)
class TimeWindow:
    """Half-open [start, end) UTC interval, with an IANA zone for display only."""

    start: str
    end: str
    display_timezone: str

    _KEYS: ClassVar = ("start", "end", "display_timezone")

    def __post_init__(self) -> None:
        start, end = _utc(self.start, "start"), _utc(self.end, "end")
        if start >= end:
            raise _invalid("window start must precede end")
        _text(self.display_timezone, "display_timezone")
        if self.display_timezone not in _timezone_keys():
            raise _invalid("display_timezone must be a valid IANA zone")
        try:
            ZoneInfo(self.display_timezone)
        except (ValueError, ZoneInfoNotFoundError, OSError) as exc:
            raise _invalid("display_timezone must be a valid IANA zone") from exc
        object.__setattr__(self, "start", _timestamp(self.start, "start"))
        object.__setattr__(self, "end", _timestamp(self.end, "end"))

    @classmethod
    def from_dict(cls, payload: object) -> TimeWindow:
        return cls(**_keys(payload, cls._KEYS))

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "display_timezone": self.display_timezone}


def _window(value: object) -> TimeWindow:
    if type(value) is not TimeWindow:
        raise _invalid("window must be a TimeWindow")
    return replace(value)


@dataclass(frozen=True)
class SourceRef:
    """Collected evidence metadata and bounded excerpt, never a model-authored URL.

    resource_version must explicitly say 'unknown' when unavailable. Collection
    identity is separate from the requesting Principal: GitHub collectors use
    platform='github', which is not a core chat/permission Principal platform.
    A1 excerpts are private and their audience is recorded explicitly.
    """

    namespace: str
    source_id: str
    resource_id: str
    source_kind: str
    resource_version: str
    window: TimeWindow
    event_at: str
    observed_at: str
    content_sha256: str
    grant_id: str
    grant_revision: int
    collection_platform: str
    collection_actor_id: str
    purpose: str
    allowed_audience: tuple[Principal, ...]
    visibility: str
    excerpt: str

    _KEYS: ClassVar = (
        "namespace", "source_id", "resource_id", "source_kind", "resource_version", "window",
        "event_at", "observed_at", "content_sha256", "grant_id", "grant_revision",
        "collection_platform", "collection_actor_id", "purpose", "allowed_audience",
        "visibility", "excerpt",
    )

    def __post_init__(self) -> None:
        for field in ("namespace", "source_id", "resource_id", "grant_id",
                      "collection_actor_id", "purpose"):
            _identifier(getattr(self, field), field)
        _text(self.resource_version, "resource_version")
        _positive_int(self.grant_revision, "grant_revision")
        _text(self.content_sha256, "content_sha256", 64)
        if not re.fullmatch(r"[0-9a-f]{64}", self.content_sha256):
            raise _invalid("content_sha256 must be 64 lowercase hexadecimal characters")
        _text(self.source_kind, "source_kind")
        _text(self.collection_platform, "collection_platform")
        platforms = {
            "manual": PLATFORMS | {"manual"},
            "feishu_message": {"feishu", "lark"},
            "github_event": {"github"},
        }
        if self.collection_platform not in platforms.get(self.source_kind, ()):
            raise _invalid("source kind and collection platform do not match")
        if type(self.visibility) is not str or self.visibility != "private":
            raise _invalid("A1 source visibility must be private")
        _text(self.excerpt, "excerpt", 4000, empty=True)
        window = _window(self.window)
        event, observed = _utc(self.event_at, "event_at"), _utc(self.observed_at, "observed_at")
        if not _utc(window.start, "start") <= event < _utc(window.end, "end"):
            raise _invalid("source event must lie within [start, end)")
        if observed < event:
            raise _invalid("source cannot be observed before its event")
        object.__setattr__(self, "window", window)
        object.__setattr__(self, "event_at", _timestamp(self.event_at, "event_at"))
        object.__setattr__(self, "observed_at", _timestamp(self.observed_at, "observed_at"))
        object.__setattr__(self, "allowed_audience", _audience(self.allowed_audience))

    @classmethod
    def from_dict(cls, payload: object) -> SourceRef:
        values = _keys(payload, cls._KEYS)
        values["window"] = TimeWindow.from_dict(values["window"])
        values["allowed_audience"] = _audience_from_dict(values["allowed_audience"])
        return cls(**values)

    def to_dict(self) -> dict:
        return {key: getattr(self, key) for key in self._KEYS} | {
            "window": self.window.to_dict(),
            "allowed_audience": [actor.to_dict() for actor in self.allowed_audience],
        }


@dataclass(frozen=True)
class SourceSet:
    """Frozen, namespace/purpose/window/audience-bound input to claim validation."""

    namespace: str
    purpose: str
    window: TimeWindow
    allowed_audience: tuple[Principal, ...]
    sources: tuple[SourceRef, ...]

    _KEYS: ClassVar = ("namespace", "purpose", "window", "allowed_audience", "sources")

    def __post_init__(self) -> None:
        _identifier(self.namespace, "namespace")
        _identifier(self.purpose, "purpose")
        window, audience = _window(self.window), _audience(self.allowed_audience)
        sources = _sequence(self.sources, "sources")
        if any(type(item) is not SourceRef for item in sources):
            raise _invalid("sources must contain SourceRef instances")
        sources = tuple(replace(item) for item in sources)
        if len({item.source_id for item in sources}) != len(sources):
            raise _invalid("duplicate source IDs")
        for item in sources:
            if (item.namespace != self.namespace or item.purpose != self.purpose
                    or item.window != window or set(item.allowed_audience) != set(audience)):
                raise _invalid("source namespace/purpose/window/audience does not match source set")
        object.__setattr__(self, "window", window)
        object.__setattr__(self, "allowed_audience", audience)
        object.__setattr__(self, "sources", sources)

    @classmethod
    def from_dict(cls, payload: object) -> SourceSet:
        values = _keys(payload, cls._KEYS)
        return cls(
            namespace=values["namespace"], purpose=values["purpose"],
            window=TimeWindow.from_dict(values["window"]),
            allowed_audience=_audience_from_dict(values["allowed_audience"]),
            sources=tuple(SourceRef.from_dict(item)
                          for item in _sequence(values["sources"], "sources")),
        )

    def to_dict(self) -> dict:
        return {
            "namespace": self.namespace, "purpose": self.purpose, "window": self.window.to_dict(),
            "allowed_audience": [actor.to_dict() for actor in self.allowed_audience],
            "sources": [item.to_dict() for item in self.sources],
        }


@dataclass(frozen=True)
class BriefClaim:
    """A fact, reported commitment, or proposed action with evidence references.

    A commitment describes a source's commitment; it is not permission to act.
    A proposal remains a suggestion. This contract has no approval/execution state.
    """

    kind: str
    text: str
    source_ids: tuple[str, ...]

    _KEYS: ClassVar = ("kind", "text", "source_ids")

    def __post_init__(self) -> None:
        if type(self.kind) is not str or self.kind not in ("fact", "commitment", "proposal"):
            raise _invalid("claim kind must be fact, commitment or proposal")
        _text(self.text, "text", 4000)
        object.__setattr__(self, "source_ids", _ids(self.source_ids, "source_ids"))

    @classmethod
    def from_dict(cls, payload: object) -> BriefClaim:
        return cls(**_keys(payload, cls._KEYS))

    def to_dict(self) -> dict:
        return {"kind": self.kind, "text": self.text, "source_ids": list(self.source_ids)}


def validate_claims(sources: SourceSet, claims: tuple[BriefClaim, ...]) -> tuple[BriefClaim, ...]:
    """Bind every claim to the frozen inputs; this checks references, not truth."""
    if type(sources) is not SourceSet:
        raise _invalid("sources must be a SourceSet")
    sources = replace(sources)
    claims = _sequence(claims, "claims", empty=True)
    if any(type(claim) is not BriefClaim for claim in claims):
        raise _invalid("claims must contain BriefClaim instances")
    claims = tuple(replace(claim) for claim in claims)
    known = {item.source_id for item in sources.sources}
    if any(set(claim.source_ids) - known for claim in claims):
        raise _invalid("claim references an unknown source ID")
    return claims
