"""M01 immutable meeting contracts; no collection, approval, storage or generation.

Use project_mapping.freeze_meeting_scope with trusted lookups for intake. Wire
parsing and content hashes establish shape/integrity only, never authority.
All timestamps are UTC except local_time_window's explicitly offset-bearing
input; display zones are retained. Collection describes observations over an
interval, not an atomic snapshot. Historical reconstruction is unsupported.
Budgets are GLOBAL per job across all attempts. FrozenScope pins durations;
ExecutionTiming anchors deadlines to the trusted FIRST job claim, excluding
human confirmation and queue wait. The lifecycle must persist that timing and
consumption across retries, never re-intake/reset time or replenish budgets
(A02/M06). Parsing these records does not authenticate their clock or provenance.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ostag.briefs.models import (
    TimeWindow, _audience, _audience_from_dict, _identifier, _ids, _invalid, _keys,
    _positive_int, _principal, _principal_from_dict, _sequence, _text, _timestamp,
    _timezone_keys, _utc, _window,
)
from ostag.briefs.project_mapping import (
    Chat, PersonMapping, Repository, _chat_id, _copy, _dump, _hash, _people,
    _records, _repo_id, _resource_id,
)
from ostag.core.identity import Principal


class MeetingPurpose(str, Enum):
    PROJECT_REVIEW = "meeting-prep"
    DECISION_REVIEW = "decision-review"
    RELEASE_REVIEW = "release-review"


class SourceCategory(str, Enum):
    PULLS = "pulls"
    ISSUES = "issues"
    ISSUE_COMMENTS = "issue_comments"
    REVIEWS = "reviews"
    REVIEW_COMMENTS = "review_comments"
    COMMITS = "commits"
    RELEASES = "releases"
    CHECKS = "checks"
    DEPLOYMENTS = "deployments"


def _enum_value(kind, value):
    if type(value) is not str:
        raise _invalid(f"{kind.__name__} must be a string on the wire")
    try:
        return kind(value)
    except ValueError:
        raise _invalid(f"unknown {kind.__name__}") from None


def _bool(value, field):
    if type(value) is not bool:
        raise _invalid(f"{field} must be an explicit boolean")
    return value


def _digest(value, field):
    _text(value, field, 64)
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise _invalid(f"{field} must be a lowercase SHA256")


def _unique_ids(value, field, validator):
    result = tuple(validator(v) for v in _sequence(value, field, empty=True))
    if len(result) != len(set(result)):
        raise _invalid(f"duplicate {field}")
    return result


def local_time_window(start: str, end: str, display_timezone: str) -> TimeWindow:
    """Convert exact offset-bearing ISO wall times; reject gaps and wrong offsets.

    For a fall-back fold, callers must provide the intended offset for EACH end.
    Relative phrases and naive dates are unsupported. No server timezone is read.
    """
    _text(display_timezone, "display_timezone")
    if display_timezone not in _timezone_keys():
        raise _invalid("display_timezone must be an exact IANA zone")
    try:
        zone = ZoneInfo(display_timezone)
    except (ValueError, ZoneInfoNotFoundError, OSError) as exc:
        raise _invalid("display_timezone must be a valid IANA zone") from exc

    def convert(value):
        _text(value, "local timestamp", 40)
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                            r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})", value):
            raise _invalid("local timestamp requires an explicit ISO date, time and offset")
        if value.endswith("-00:00"):
            raise _invalid("unknown local offset is not supported")
        if not value.endswith("Z") and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
            raise _invalid("invalid ISO offset")
        try:
            local = datetime.fromisoformat(value.replace("Z", "+00:00"))
            utc = local.astimezone(timezone.utc)
            back = utc.astimezone(zone)
        except (ValueError, OverflowError):
            raise _invalid("invalid local calendar timestamp") from None
        if (back.replace(tzinfo=None) != local.replace(tzinfo=None)
                or back.utcoffset() != local.utcoffset()):
            raise _invalid("local wall time/offset does not round-trip in the specified zone")
        return utc.isoformat().replace("+00:00", "Z")

    return TimeWindow(convert(start), convert(end), display_timezone)


@dataclass(frozen=True)
class MeetingRequest:
    """Explicit private scope proposal. It is neither a grant nor an approval."""

    namespace: str
    requesting_principal: Principal
    goal: str
    purpose: MeetingPurpose
    mapping_id: str
    mapping_revision: int
    window: TimeWindow
    repo_ids: tuple[str, ...]
    chat_ids: tuple[str, ...]
    source_categories: tuple[SourceCategory, ...]
    required_partition_ids: tuple[str, ...]
    audience: tuple[Principal, ...]
    shareable_sources: tuple[str, ...]
    grant_id: str
    grant_revision: int
    budget_profile_id: str
    budget_profile_revision: int
    requested_as_of: str | None = None

    _KEYS: ClassVar = (
        "namespace",
        "requesting_principal",
        "goal",
        "purpose",
        "mapping_id",
        "mapping_revision",
        "window",
        "repo_ids",
        "chat_ids",
        "source_categories",
        "required_partition_ids",
        "audience",
        "shareable_sources",
        "grant_id",
        "grant_revision",
        "budget_profile_id",
        "budget_profile_revision",
    )

    def __post_init__(self):
        for name in ("namespace", "mapping_id", "grant_id", "budget_profile_id"):
            _identifier(getattr(self, name), name)
        for name in ("mapping_revision", "grant_revision", "budget_profile_revision"):
            _positive_int(getattr(self, name), name)
        _text(self.goal, "goal", 4000)
        if type(self.purpose) is not MeetingPurpose:
            raise _invalid("purpose must be a MeetingPurpose")
        principal = _principal(self.requesting_principal)
        audience = _audience(self.audience)
        if audience != (principal,):
            raise _invalid("meeting audience must be exactly the requester")
        shareable = _sequence(self.shareable_sources, "shareable_sources", empty=True)
        if shareable:
            raise _invalid("M01 shareable_sources must be empty")
        repos = _unique_ids(self.repo_ids, "repo_ids", _repo_id)
        chats = _unique_ids(self.chat_ids, "chat_ids", _chat_id)
        if not repos and not chats:
            raise _invalid("meeting scope must be nonempty")
        categories = _sequence(self.source_categories, "source_categories", len(SourceCategory),
                               empty=not repos)
        if (any(type(c) is not SourceCategory for c in categories)
                or len(set(categories)) != len(categories)):
            raise _invalid("source categories must be distinct typed categories")
        if not repos and categories:
            raise _invalid("GitHub source categories require selected repositories")
        required = _ids(self.required_partition_ids, "required_partition_ids")
        manifest = {f"github:repo:{rid}:{c.value}" for rid in repos for c in categories}
        manifest.update(f"feishu:chat:{cid}:messages" for cid in chats)
        if not set(required) <= manifest:
            raise _invalid("required partition IDs must belong to explicitly selected scope")
        if len(manifest) > 500:
            raise _invalid("meeting partition manifest exceeds contract bounds")
        for name, value in (
            ("requesting_principal", principal),
            ("audience", audience),
            ("repo_ids", repos),
            ("chat_ids", chats),
            ("source_categories", categories),
            ("required_partition_ids", required),
            ("shareable_sources", shareable),
            ("window", _window(self.window)),
        ):
            object.__setattr__(self, name, value)
        if self.requested_as_of is not None:
            object.__setattr__(
                self, "requested_as_of", _timestamp(self.requested_as_of, "requested_as_of")
            )

    @property
    def resource_ids(self) -> tuple[str, ...]:
        return tuple(f"github:repo:{rid}" for rid in self.repo_ids) + tuple(
            f"feishu:chat:{cid}" for cid in self.chat_ids)

    @property
    def partitions(self) -> tuple[Partition, ...]:
        ids = [
            f"github:repo:{rid}:{c.value}" for rid in self.repo_ids for c in self.source_categories
        ]
        ids.extend(f"feishu:chat:{cid}:messages" for cid in self.chat_ids)
        return tuple(Partition(pid, pid in self.required_partition_ids) for pid in ids)

    @classmethod
    def from_dict(cls, payload):
        values = _keys(payload, cls._KEYS, ("requested_as_of",))
        if "requested_as_of" in values:
            _utc(values["requested_as_of"], "requested_as_of")
        values["requesting_principal"] = _principal_from_dict(values["requesting_principal"])
        values["audience"] = _audience_from_dict(values["audience"])
        values["window"] = TimeWindow.from_dict(values["window"])
        values["purpose"] = _enum_value(MeetingPurpose, values["purpose"])
        values["source_categories"] = tuple(
            _enum_value(SourceCategory, v)
            for v in _sequence(values["source_categories"], "categories", empty=True)
        )
        return cls(**values)

    def to_dict(self):
        return _dump(self)


@dataclass(frozen=True)
class BudgetProfile:
    """Trusted pinned GLOBAL per-job maxima shared across attempts, never per repo.

    Lower profiles are allowed; unbounded profiles are not.
    One metadata call per repo is the minimum overhead for identity/branch
    validation. A transport needing more must configure that overhead upward.
    The call estimate counts one request per source category/chat, not pagination.
    """

    profile_id: str
    revision: int
    max_repos: int = 20
    max_github_facts: int = 500
    max_feishu_messages: int = 500
    max_provider_calls: int = 200
    max_model_input_bytes: int = 1048576
    overall_seconds: int = 180
    collection_seconds: int = 120
    generation_reserve_seconds: int = 60
    conservative_call_seconds: float = 1.0
    metadata_calls_per_repo: int = 1

    _KEYS: ClassVar = (
        "profile_id", "revision", "max_repos", "max_github_facts", "max_feishu_messages",
        "max_provider_calls", "max_model_input_bytes", "overall_seconds", "collection_seconds",
        "generation_reserve_seconds", "conservative_call_seconds", "metadata_calls_per_repo",
    )

    def __post_init__(self):
        _identifier(self.profile_id, "profile_id")
        _positive_int(self.revision, "revision")
        for name, cap in (
            ("max_repos", 20),
            ("max_github_facts", 500),
            ("max_feishu_messages", 500),
            ("max_provider_calls", 200),
            ("max_model_input_bytes", 1048576),
            ("overall_seconds", 180),
            ("collection_seconds", 120),
            ("generation_reserve_seconds", 60),
            ("metadata_calls_per_repo", 200),
        ):
            if _positive_int(getattr(self, name), name) > cap:
                raise _invalid(f"{name} exceeds M01 maximum")
        if self.collection_seconds + self.generation_reserve_seconds > self.overall_seconds:
            raise _invalid("collection ceiling plus generation reserve exceeds hard deadline")
        latency = self.conservative_call_seconds
        if (
            type(latency) not in (float, int)
            or not 0 < latency <= 180
            or not math.isfinite(latency)
        ):
            raise _invalid("conservative call latency must be positive, finite and bounded")
        object.__setattr__(self, "conservative_call_seconds", float(latency))

    @classmethod
    def from_dict(cls, payload):
        return cls(**_keys(payload, cls._KEYS))

    def to_dict(self):
        return _dump(self)


@dataclass(frozen=True)
class BudgetEstimate:
    minimum_provider_calls: int
    minimum_collection_seconds: float

    _KEYS: ClassVar = ("minimum_provider_calls", "minimum_collection_seconds")

    def __post_init__(self):
        if _positive_int(self.minimum_provider_calls, "minimum_provider_calls") > 100500:
            raise _invalid("estimate exceeds bounded scope")
        seconds = self.minimum_collection_seconds
        if (
            type(seconds) not in (float, int)
            or not 0 < seconds <= 18090000
            or not math.isfinite(seconds)
        ):
            raise _invalid("estimated time must be positive, finite and bounded")
        object.__setattr__(self, "minimum_collection_seconds", float(seconds))

    @classmethod
    def from_dict(cls, payload):
        return cls(**_keys(payload, cls._KEYS))

    def to_dict(self):
        return _dump(self)


def _partition_id(value):
    _text(value, "partition_id")
    resource, sep, category = value.rpartition(":")
    _resource_id(resource)
    if not sep or (
        resource.startswith("github:") and category not in {c.value for c in SourceCategory}
    ):
        raise _invalid("unknown GitHub partition category")
    if resource.startswith("feishu:") and category != "messages":
        raise _invalid("unknown Feishu partition category")
    return value


@dataclass(frozen=True)
class Partition:
    """Manifest entry frozen before collection, regardless of arriving responses."""

    partition_id: str
    required: bool
    _KEYS: ClassVar = ("partition_id", "required")

    def __post_init__(self):
        _partition_id(self.partition_id)
        _bool(self.required, "required")

    @classmethod
    def from_dict(cls, payload):
        return cls(**_keys(payload, cls._KEYS))

    def to_dict(self):
        return _dump(self)


@dataclass(frozen=True)
class FrozenScope:
    """Selected metadata/policy snapshot. Constructing or hashing it grants nothing.

    Full mapping/grant digests bind even unselected policy fields without exposing
    unselected private resource names in confirmations. The partition manifest,
    estimates and budget durations derive only from these immutable pinned inputs.
    No execution clock runs before the first job claim. content_hash includes
    intake_at: it identifies one intake snapshot, not an identical request across
    separate intakes. Hashes are integrity identifiers, not authorization.
    """

    request: MeetingRequest
    mapping_name: str
    mapping_policy_sha256: str
    grant_sha256: str
    core_policy_sha256: str
    history_window: TimeWindow
    repositories: tuple[Repository, ...]
    chats: tuple[Chat, ...]
    people: tuple[PersonMapping, ...]
    budget: BudgetProfile
    intake_at: str

    _KEYS: ClassVar = (
        "request",
        "mapping_name",
        "mapping_policy_sha256",
        "grant_sha256",
        "core_policy_sha256",
        "history_window",
        "repositories",
        "chats",
        "people",
        "budget",
        "intake_at",
    )
    _DERIVED: ClassVar = ("contract_version", "partitions", "estimate")

    def __post_init__(self):
        request = _copy(self.request, MeetingRequest)
        budget = _copy(self.budget, BudgetProfile)
        repos = _records(self.repositories, Repository, "repositories")
        chats = _records(self.chats, Chat, "chats")
        history = _window(self.history_window)
        instant = _utc(self.intake_at, "intake_at")
        _text(self.mapping_name, "mapping_name")
        _digest(self.mapping_policy_sha256, "mapping_policy_sha256")
        _digest(self.grant_sha256, "grant_sha256")
        _digest(self.core_policy_sha256, "core_policy_sha256")
        if (tuple(r.repo_id for r in repos) != request.repo_ids
                or tuple(c.chat_id for c in chats) != request.chat_ids
                or any(r.namespace != request.namespace for r in (*repos, *chats))
                or len({r.full_name.lower() for r in repos}) != len(repos)):
            raise _invalid("frozen resources must exactly match selected namespace/scope")
        if (budget.profile_id != request.budget_profile_id
                or budget.revision != request.budget_profile_revision):
            raise _invalid("budget pin mismatch")
        if not (
            _utc(history.start, "history start")
            <= _utc(request.window.start, "start")
            < _utc(request.window.end, "end")
            <= min(instant, _utc(history.end, "history end"))
        ):
            raise _invalid("request window outside frozen history or trusted now")
        if request.requested_as_of is not None and _utc(request.requested_as_of, "as_of") > instant:
            raise _invalid("requested_as_of cannot be in the future")
        for name, value in (
            ("request", request),
            ("budget", budget),
            ("repositories", repos),
            ("chats", chats),
            ("people", _people(self.people)),
            ("history_window", history),
            ("intake_at", _timestamp(self.intake_at, "intake_at")),
        ):
            object.__setattr__(self, name, value)

    @property
    def partitions(self):
        return self.request.partitions

    @property
    def estimate(self):
        calls = (len(self.repositories) * (len(self.request.source_categories)
                                          + self.budget.metadata_calls_per_repo) + len(self.chats))
        return BudgetEstimate(calls, calls * self.budget.conservative_call_seconds)

    @property
    def intake_reasons(self) -> tuple[str, ...]:
        reasons = []
        if len(self.repositories) > self.budget.max_repos:
            reasons.append("repo_limit_exceeded")
        if self.estimate.minimum_provider_calls > self.budget.max_provider_calls:
            reasons.append("provider_call_limit_exceeded")
        if self.estimate.minimum_collection_seconds > self.budget.collection_seconds:
            reasons.append("collection_time_infeasible")
        if self.request.requested_as_of is not None:
            # Even an as-of equal to intake is not evidence of an atomic snapshot.
            reasons.append("historical_as_of_unproven")
        return tuple(reasons)

    @property
    def content_hash(self) -> str:
        return _hash({"contract_version": "meeting-scope-v1", "scope": self.to_dict()})

    @classmethod
    def from_dict(cls, payload):
        wire = _keys(payload, cls._KEYS + cls._DERIVED)
        values = {key: wire[key] for key in cls._KEYS}
        values["request"] = MeetingRequest.from_dict(values["request"])
        values["budget"] = BudgetProfile.from_dict(values["budget"])
        values["history_window"] = TimeWindow.from_dict(values["history_window"])
        for name, kind in (
            ("repositories", Repository),
            ("chats", Chat),
            ("people", PersonMapping),
        ):
            values[name] = tuple(kind.from_dict(v) for v in
                                 _sequence(values[name], name, empty=True))
        scope = cls(**values)
        partitions = tuple(
            Partition.from_dict(v) for v in _sequence(wire["partitions"], "partitions")
        )
        estimate = BudgetEstimate.from_dict(wire["estimate"])
        if (
            type(wire["contract_version"]) is not str
            or wire["contract_version"] != "meeting-scope-v1"
            or partitions != scope.partitions
            or estimate != scope.estimate
        ):
            raise _invalid("restored scope derived fields do not match frozen inputs")
        return scope

    def to_dict(self):
        return _dump(self) | {
            "contract_version": "meeting-scope-v1",
            "partitions": [p.to_dict() for p in self.partitions],
            "estimate": self.estimate.to_dict(),
        }


@dataclass(frozen=True)
class ExecutionTiming:
    """Job clock bound to its confirmed scope and trusted first claim timestamp.

    Construct once at FIRST claim; restore the same record on every retry.
    A02/M06 must persist it alongside consumed per-job budgets and verify its
    provenance. Constructors/from_dict validate shape and consistency only;
    they cannot distinguish a forged clock or reset from a trusted first claim.
    Collection may finish at its ceiling; a new result must commit strictly
    before the hard deadline. Queue wait and confirmation consume neither.
    """

    scope: FrozenScope
    first_claimed_at: str
    _KEYS: ClassVar = ("scope", "first_claimed_at")
    _DERIVED: ClassVar = ("collection_deadline", "hard_deadline")

    def __post_init__(self):
        scope = _copy(self.scope, FrozenScope)
        claim = _timestamp(self.first_claimed_at, "first_claimed_at")
        if _utc(claim, "first_claimed_at") < _utc(scope.intake_at, "intake_at"):
            raise _invalid("first claim cannot precede intake")
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "first_claimed_at", claim)
        # Normalize overflow at construction, not during a later decision.
        self._deadline(scope.budget.overall_seconds)

    def _deadline(self, seconds):
        try:
            instant = _utc(self.first_claimed_at, "first_claimed_at") + timedelta(seconds=seconds)
            return instant.isoformat().replace("+00:00", "Z")
        except OverflowError:
            raise _invalid("deadline exceeds supported calendar") from None

    @property
    def collection_deadline(self):
        return self._deadline(self.scope.budget.collection_seconds)

    @property
    def hard_deadline(self):
        return self._deadline(self.scope.budget.overall_seconds)

    @classmethod
    def from_dict(cls, payload):
        wire = _keys(payload, cls._KEYS + cls._DERIVED)
        timing = cls(FrozenScope.from_dict(wire["scope"]), wire["first_claimed_at"])
        if any(_timestamp(wire[key], key) != getattr(timing, key) for key in cls._DERIVED):
            raise _invalid("restored deadlines must match first claim and frozen durations")
        return timing

    def to_dict(self):
        return _dump(self) | {"collection_deadline": self.collection_deadline,
                             "hard_deadline": self.hard_deadline}


def _execution_timing(value, scope):
    timing = _copy(value, ExecutionTiming)
    if timing.scope.content_hash != scope.content_hash:
        raise _invalid("execution timing must bind the exact frozen scope")
    return timing


@dataclass(frozen=True)
class IntakeResult:
    """Authorized intake outcome; confirmation is a data preview, never an approval.

    Callers must not display parsed/user-constructed IntakeResults as proof of
    authorization. Only the trusted freeze function establishes current access.
    """

    scope: FrozenScope
    _KEYS: ClassVar = ("scope", "state", "reasons", "estimate")

    def __post_init__(self):
        object.__setattr__(self, "scope", _copy(self.scope, FrozenScope))

    @property
    def state(self):
        return "needs_input" if self.reasons else "accepted"

    @property
    def reasons(self):
        return self.scope.intake_reasons

    @property
    def estimate(self):
        return self.scope.estimate

    @property
    def confirmation(self) -> dict:
        scope, req = self.scope, self.scope.request
        payload = {
            "goal": req.goal, "purpose": req.purpose.value, "namespace": req.namespace,
            "window": req.window.to_dict(),
            "requester": req.requesting_principal.to_dict(),
            "audience": [p.to_dict() for p in req.audience], "shareable_sources": [],
            "repositories": [r.to_dict() for r in scope.repositories],
            "chats": [c.to_dict() for c in scope.chats],
            "partitions": [p.to_dict() for p in scope.partitions],
            "coverage_state": "not_collected", "gaps": [p.partition_id for p in scope.partitions],
            "mapping_name": scope.mapping_name, "mapping_id": req.mapping_id,
            "mapping_revision": req.mapping_revision, "grant_id": req.grant_id,
            "grant_revision": req.grant_revision, "budget": scope.budget.to_dict(),
            "estimate": self.estimate.to_dict(), "reasons": list(self.reasons),
            "content_hash": scope.content_hash,
        }
        if req.requested_as_of is not None:
            payload["requested_as_of"] = req.requested_as_of
        return payload

    @classmethod
    def from_dict(cls, payload):
        values = _keys(payload, cls._KEYS)
        result = cls(FrozenScope.from_dict(values["scope"]))
        reasons = _unique_ids(values["reasons"], "reasons", lambda v: _identifier(v, "reason"))
        if (
            type(values["state"]) is not str
            or values["state"] != result.state
            or reasons != result.reasons
            or BudgetEstimate.from_dict(values["estimate"]) != result.estimate
        ):
            raise _invalid("intake outcome must match frozen scope preflight")
        return result

    def to_dict(self):
        return _dump(self) | {"state": self.state, "reasons": list(self.reasons),
                              "estimate": self.estimate.to_dict()}


@dataclass(frozen=True)
class ProviderObservation:
    resource_id: str
    observed_at: str
    _KEYS: ClassVar = ("resource_id", "observed_at")

    def __post_init__(self):
        _resource_id(self.resource_id)
        object.__setattr__(self, "observed_at", _timestamp(self.observed_at, "observed_at"))

    @classmethod
    def from_dict(cls, payload):
        return cls(**_keys(payload, cls._KEYS))

    def to_dict(self):
        return _dump(self)


@dataclass(frozen=True)
class RepoHead:
    repository: Repository
    head_sha: str
    observed_at: str
    _KEYS: ClassVar = ("repository", "head_sha", "observed_at")

    def __post_init__(self):
        object.__setattr__(self, "repository", _copy(self.repository, Repository))
        _text(self.head_sha, "head_sha", 64)
        if not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", self.head_sha):
            raise _invalid("head_sha must be a full 40/64 character hexadecimal SHA")
        object.__setattr__(self, "head_sha", self.head_sha.lower())
        object.__setattr__(self, "observed_at", _timestamp(self.observed_at, "observed_at"))

    @classmethod
    def from_dict(cls, payload):
        values = _keys(payload, cls._KEYS)
        values["repository"] = Repository.from_dict(values["repository"])
        return cls(**values)

    def to_dict(self):
        return _dump(self)


@dataclass(frozen=True)
class CollectionSeal:
    """Observed interval, not historical proof. `sealed_at` comes from a trusted clock.

    Actual provider observations and repository heads must come from collection
    code. Shape validation cannot authenticate a provider or user-supplied JSON.
    All requested_as_of constraints are unproven until reconstruction exists.
    Requires at least one provider observation overall and one head per repo,
    not an observation for every chat/partition. A seal does not establish
    coverage; completeness is decided separately against the frozen manifest.
    Collection ends by the first-claim collection ceiling and sealing occurs
    before the same job's hard deadline, including on retries.
    """

    scope: FrozenScope
    timing: ExecutionTiming
    collection_started_at: str
    collection_finished_at: str
    observations: tuple[ProviderObservation, ...]
    repo_heads: tuple[RepoHead, ...]
    sealed_at: str
    _KEYS: ClassVar = (
        "scope",
        "timing",
        "collection_started_at",
        "collection_finished_at",
        "observations",
        "repo_heads",
        "sealed_at",
    )
    _DERIVED: ClassVar = ("scope_hash", "observation_cutoff", "historical_state")

    def __post_init__(self):
        scope = _copy(self.scope, FrozenScope)
        timing = _execution_timing(self.timing, scope)
        observations = _records(self.observations, ProviderObservation, "observations", empty=False)
        heads = _records(self.repo_heads, RepoHead, "repo_heads")
        start = _utc(self.collection_started_at, "collection_started_at")
        finish = _utc(self.collection_finished_at, "collection_finished_at")
        sealed = _utc(self.sealed_at, "sealed_at")
        if not _utc(timing.first_claimed_at, "first_claimed_at") <= start <= finish <= sealed:
            raise _invalid("collection interval must follow first claim and end by trusted now")
        if (finish > _utc(timing.collection_deadline, "collection_deadline")
                or sealed >= _utc(timing.hard_deadline, "hard_deadline")):
            raise _invalid("collection or sealing exceeds the frozen job deadlines")
        expected = {r.repo_id: r for r in scope.repositories}
        if len(heads) != len(expected) or {h.repository.repo_id for h in heads} != set(expected):
            raise _invalid("collection requires exactly one head observation per selected repo")
        if any(h.repository != expected[h.repository.repo_id] for h in heads):
            raise _invalid("repo head identity/name/branch must match frozen scope")
        if any(o.resource_id not in scope.request.resource_ids for o in observations):
            raise _invalid("observation resource outside frozen scope")
        if any(
            not start <= _utc(o.observed_at, "observed_at") <= finish
            for o in (*observations, *heads)
        ):
            raise _invalid("observations must lie within collection interval")
        for name, value in (("scope", scope), ("timing", timing),
                            ("observations", observations), ("repo_heads", heads)):
            object.__setattr__(self, name, value)
        for name in ("collection_started_at", "collection_finished_at", "sealed_at"):
            object.__setattr__(self, name, _timestamp(getattr(self, name), name))

    @property
    def scope_hash(self):
        return self.scope.content_hash

    @property
    def observation_cutoff(self):
        return max((o.observed_at for o in (*self.observations, *self.repo_heads)),
                   key=lambda value: _utc(value, "observed_at"))

    @property
    def requested_as_of(self):
        return self.scope.request.requested_as_of

    @property
    def historical_state(self):
        return "unproven" if self.requested_as_of is not None else "current_observations_only"

    @classmethod
    def from_dict(cls, payload):
        wire = _keys(payload, cls._KEYS + cls._DERIVED, ("requested_as_of",))
        values = {key: wire[key] for key in cls._KEYS}
        values["scope"] = FrozenScope.from_dict(values["scope"])
        values["timing"] = ExecutionTiming.from_dict(values["timing"])
        values["observations"] = tuple(ProviderObservation.from_dict(v) for v in
                                       _sequence(values["observations"], "observations"))
        values["repo_heads"] = tuple(RepoHead.from_dict(v) for v in
                                     _sequence(values["repo_heads"], "repo_heads", empty=True))
        seal = cls(**values)
        as_of = (
            _timestamp(wire["requested_as_of"], "requested_as_of")
            if "requested_as_of" in wire
            else None
        )
        _digest(wire["scope_hash"], "scope_hash")
        if (
            wire["scope_hash"] != seal.scope_hash
            or _timestamp(wire["observation_cutoff"], "observation_cutoff")
            != seal.observation_cutoff
            or type(wire["historical_state"]) is not str
            or wire["historical_state"] != seal.historical_state
            or as_of != seal.requested_as_of
        ):
            raise _invalid("seal derived fields do not match actual observations and scope")
        return seal

    def to_dict(self):
        payload = _dump(self) | {
            "scope_hash": self.scope_hash,
            "observation_cutoff": self.observation_cutoff,
            "historical_state": self.historical_state,
        }
        if self.requested_as_of is not None:
            payload["requested_as_of"] = self.requested_as_of
        return payload


def seal_collection(*, scope: FrozenScope, timing: ExecutionTiming,
                    collection_started_at: str, collection_finished_at: str,
                    observations: tuple[ProviderObservation, ...], repo_heads: tuple[RepoHead, ...],
                    now: str) -> CollectionSeal:
    """Seal after collection with trusted now and actual provider observations."""
    return CollectionSeal(scope, timing, collection_started_at, collection_finished_at,
                          observations, repo_heads, now)


class PartitionStatus(str, Enum):
    COMPLETE = "complete"
    MISSING = "missing"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True)
class PartitionReport:
    partition_id: str
    status: PartitionStatus
    _KEYS: ClassVar = ("partition_id", "status")

    def __post_init__(self):
        _partition_id(self.partition_id)
        if type(self.status) is not PartitionStatus:
            raise _invalid("partition report requires a typed status")

    @classmethod
    def from_dict(cls, payload):
        values = _keys(payload, cls._KEYS)
        values["status"] = _enum_value(PartitionStatus, values["status"])
        return cls(**values)

    def to_dict(self):
        return _dump(self)


class ResultState(str, Enum):
    CANCELLED = "cancelled"
    NEEDS_INPUT = "needs_input"
    STALE_ATTEMPT = "stale_attempt"
    FAILED = "failed"
    DRAFT_READY = "draft_ready"
    COLLECTING = "collecting"


@dataclass(frozen=True)
class ResultDecision:
    """can_commit admits a status record, NOT delivery or current authorization.

    Cancellation/revocation/deadline failure can record only sanitized status. Stale owners may
    not commit even that. Live side-effect boundaries must repeat all checks.
    """

    state: ResultState
    meeting_ready: bool
    can_commit: bool
    reasons: tuple[str, ...]
    gaps: tuple[str, ...]
    _KEYS: ClassVar = ("state", "meeting_ready", "can_commit", "reasons", "gaps")

    def __post_init__(self):
        if type(self.state) is not ResultState:
            raise _invalid("decision requires a typed state")
        _bool(self.meeting_ready, "meeting_ready")
        _bool(self.can_commit, "can_commit")
        if self.meeting_ready != (self.state is ResultState.DRAFT_READY):
            raise _invalid("readiness must match decision state")
        if (self.meeting_ready and not self.can_commit
                or self.state is ResultState.STALE_ATTEMPT and self.can_commit):
            raise _invalid("stale attempt cannot commit")
        reasons = _unique_ids(self.reasons, "reasons", lambda v: _identifier(v, "reason"))
        gaps = _unique_ids(self.gaps, "gaps", _partition_id)
        object.__setattr__(self, "reasons", reasons)
        object.__setattr__(self, "gaps", gaps)

    @classmethod
    def from_dict(cls, payload):
        values = _keys(payload, cls._KEYS)
        values["state"] = _enum_value(ResultState, values["state"])
        return cls(**values)

    def to_dict(self):
        return _dump(self)


def decide_result(
    *, scope: FrozenScope, timing: ExecutionTiming, reports: tuple[PartitionReport, ...], now: str,
    cancelled: bool, grant_current: bool, lease_current: bool, fence_current: bool,
    finished: bool, model_valid: bool, key_claims_valid: bool, unsupported_required_question: bool,
) -> ResultDecision:
    """Pure priority table. Flags come from trusted lifecycle/validation code.

    `finished` means collection and generation finished, not merely page fetching.
    It provides no evidence of on-time completion. This table gates a NEW result
    commit: at/after the job hard deadline only sanitized status may be recorded
    by the current owner, even if finished=True. Already-persisted result reads
    are outside this function. Retries must use the original trusted timing.
    Invalid reports raise SchemaError even on cancelled attempts; absent reports
    are missing. Passing intake estimates says nothing about pagination coverage.
    """
    scope = _copy(scope, FrozenScope)
    timing = _execution_timing(timing, scope)
    reports = _records(reports, PartitionReport, "reports")
    instant = _utc(now, "now")
    if instant < _utc(timing.first_claimed_at, "first_claimed_at"):
        raise _invalid("decision time cannot precede first claim")
    for name, flag in (("cancelled", cancelled), ("grant_current", grant_current),
                       ("lease_current", lease_current), ("fence_current", fence_current),
                       ("finished", finished), ("model_valid", model_valid),
                       ("key_claims_valid", key_claims_valid),
                       ("unsupported_required_question", unsupported_required_question)):
        _bool(flag, name)
    known = {p.partition_id for p in scope.partitions}
    by_id = {r.partition_id: r.status for r in reports}
    if len(by_id) != len(reports) or set(by_id) - known:
        raise _invalid("duplicate or unknown partition reports")
    gaps = tuple(p.partition_id for p in scope.partitions
                 if by_id.get(p.partition_id) is not PartitionStatus.COMPLETE)
    can_commit = lease_current and fence_current

    def result(state, *reasons, visible_gaps=gaps):
        return ResultDecision(state, state is ResultState.DRAFT_READY, can_commit,
                              tuple(reasons), visible_gaps)

    if cancelled:
        return result(ResultState.CANCELLED, "cancelled", visible_gaps=())
    if not grant_current:
        return result(ResultState.NEEDS_INPUT, "authorization_revoked", visible_gaps=())
    if not can_commit:
        return result(ResultState.STALE_ATTEMPT, "stale_lease_or_fence", visible_gaps=())
    if instant >= _utc(timing.hard_deadline, "hard_deadline"):
        return result(ResultState.FAILED, "deadline_exceeded", visible_gaps=())
    if scope.intake_reasons:
        return result(ResultState.NEEDS_INPUT, *scope.intake_reasons)
    if any(p.required and p.partition_id in gaps for p in scope.partitions):
        return result(ResultState.NEEDS_INPUT, "required_coverage_incomplete")
    if unsupported_required_question:
        return result(ResultState.NEEDS_INPUT, "unsupported_required_question")
    if not model_valid:
        return result(ResultState.FAILED, "model_validation_failed")
    if not key_claims_valid:
        return result(ResultState.NEEDS_INPUT, "key_claims_unproven")
    if not finished:
        return result(ResultState.COLLECTING, "unfinished")
    return result(ResultState.DRAFT_READY, *(("optional_coverage_gaps",) if gaps else ()))
