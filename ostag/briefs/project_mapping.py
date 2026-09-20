"""Trusted, versioned meeting mappings and pure intake authorization.

Lookup callbacks are supplied by application code, never request data. Repository
metadata lookup(namespace, stable_id, selected_branch) must verify that branch
exists and return its current full name, selected branch and default branch.
requester_can_read(namespace, Principal, canonical_resource_id) must establish
THIS user's current access; app/bot installation visibility is not sufficient.
No lookup here implements transport or reads credentials.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, fields, replace
from enum import Enum
from typing import TYPE_CHECKING, ClassVar

from ostag.briefs.grants import GrantLookup, authorize
from ostag.briefs.models import (
    TimeWindow, _audience, _audience_from_dict, _identifier, _invalid, _keys,
    _positive_int, _sequence, _text, _utc, _window,
)
from ostag.config import Settings
from ostag.core.identity import Principal
from ostag.core.schemas import SchemaError

if TYPE_CHECKING:
    from ostag.briefs.meeting_models import BudgetProfile, IntakeResult, MeetingRequest


def _copy(value, cls):
    if type(value) is not cls:
        raise _invalid(f"expected {cls.__name__}")
    return replace(value)


def _records(value, cls, field, *, empty=True, maximum=500):
    return tuple(_copy(item, cls) for item in _sequence(value, field, maximum, empty=empty))


def _dump(record) -> dict:
    """JSON projection only; validation stays in explicit constructors and parsers."""
    def value(item):
        if isinstance(item, Enum):
            return item.value
        if isinstance(item, tuple):
            return [value(v) for v in item]
        if hasattr(item, "to_dict"):
            return item.to_dict()
        return item
    return {f.name: value(getattr(record, f.name)) for f in fields(record)
            if getattr(record, f.name) is not None}


def _hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _repo_id(value):
    _text(value, "repo_id", 32)
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise _invalid("repo_id must be a canonical positive GitHub numeric ID string")
    return value


def _chat_id(value):
    _text(value, "chat_id", 128)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise _invalid("chat_id must be an unqualified provider ID")
    return value


def _resource_id(value):
    _text(value, "resource_id")
    if value.startswith("github:repo:"):
        _repo_id(value.removeprefix("github:repo:"))
    elif value.startswith("feishu:chat:"):
        _chat_id(value.removeprefix("feishu:chat:"))
    else:
        raise _invalid("resource_id must be provider scoped")
    return value


@dataclass(frozen=True)
class Repository:
    namespace: str
    repo_id: str
    full_name: str
    branch: str
    default_branch: str

    _KEYS: ClassVar = ("namespace", "repo_id", "full_name", "branch", "default_branch")

    def __post_init__(self):
        _identifier(self.namespace, "namespace")
        _repo_id(self.repo_id)
        _text(self.full_name, "full_name", 256)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.full_name):
            raise _invalid("repository requires full owner/name")
        if any(part in (".", "..") for part in self.full_name.split("/")):
            raise _invalid("invalid repository owner/name")
        for branch in (self.branch, self.default_branch):
            _text(branch, "branch", 256)
            if (branch.startswith(("/", ".", "-")) or branch.endswith(("/", "."))
                    or any(token in branch for token in ("..", "@{", "//", "\\"))
                    or re.search(r"[\s\x00-\x1f\x7f~^:?*\[]", branch)
                    or any(part.startswith(".") or part.endswith(".lock")
                           for part in branch.split("/")) or branch == "@"):
                raise _invalid("invalid branch name")

    @property
    def resource_id(self):
        return f"github:repo:{self.repo_id}"

    @classmethod
    def from_dict(cls, payload):
        return cls(**_keys(payload, cls._KEYS))

    def to_dict(self):
        return _dump(self)


@dataclass(frozen=True)
class Chat:
    namespace: str
    chat_id: str
    name: str

    _KEYS: ClassVar = ("namespace", "chat_id", "name")

    def __post_init__(self):
        _identifier(self.namespace, "namespace")
        _chat_id(self.chat_id)
        _text(self.name, "chat name")

    @property
    def resource_id(self):
        return f"feishu:chat:{self.chat_id}"

    @classmethod
    def from_dict(cls, payload):
        return cls(**_keys(payload, cls._KEYS))

    def to_dict(self):
        return _dump(self)


@dataclass(frozen=True)
class PersonMapping:
    """Owner-approved identifiers only; matching display names conveys no identity."""

    open_id: str
    github_user_id: str
    github_login: str

    _KEYS: ClassVar = ("open_id", "github_user_id", "github_login")

    def __post_init__(self):
        _chat_id(self.open_id)
        _repo_id(self.github_user_id)
        _text(self.github_login, "github_login", 39)
        if not re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", self.github_login):
            raise _invalid("invalid GitHub login")

    @classmethod
    def from_dict(cls, payload):
        return cls(**_keys(payload, cls._KEYS))

    def to_dict(self):
        return _dump(self)


def _people(value):
    people = _records(value, PersonMapping, "people")
    for identifiers in ([p.open_id for p in people], [p.github_user_id for p in people],
                        [p.github_login.lower() for p in people]):
        if len(set(identifiers)) != len(identifiers):
            raise _invalid("duplicate or conflicting approved people mappings")
    return people


@dataclass(frozen=True)
class ProjectMapping:
    """Current trusted policy. History bounds are independent of grant issuance."""

    namespace: str
    mapping_id: str
    revision: int
    name: str
    allowed_requesters: tuple[Principal, ...]
    repositories: tuple[Repository, ...]
    chats: tuple[Chat, ...]
    people: tuple[PersonMapping, ...]
    history_window: TimeWindow

    _KEYS: ClassVar = ("namespace", "mapping_id", "revision", "name", "allowed_requesters",
                      "repositories", "chats", "people", "history_window")

    def __post_init__(self):
        _identifier(self.namespace, "namespace")
        _identifier(self.mapping_id, "mapping_id")
        _positive_int(self.revision, "revision")
        _text(self.name, "mapping name")
        repos = _records(self.repositories, Repository, "repositories")
        chats = _records(self.chats, Chat, "chats")
        for values in ([r.repo_id for r in repos], [r.full_name.lower() for r in repos],
                       [c.chat_id for c in chats]):
            if len(set(values)) != len(values):
                raise _invalid("duplicate mapping resources")
        if any(r.namespace != self.namespace for r in (*repos, *chats)):
            raise _invalid("cross-namespace mapping resource")
        object.__setattr__(self, "repositories", repos)
        object.__setattr__(self, "chats", chats)
        object.__setattr__(self, "people", _people(self.people))
        object.__setattr__(self, "allowed_requesters", _audience(self.allowed_requesters))
        object.__setattr__(self, "history_window", _window(self.history_window))

    @classmethod
    def from_dict(cls, payload):
        values = _keys(payload, cls._KEYS)
        values["allowed_requesters"] = _audience_from_dict(values["allowed_requesters"])
        values["history_window"] = TimeWindow.from_dict(values["history_window"])
        for name, kind in (
            ("repositories", Repository),
            ("chats", Chat),
            ("people", PersonMapping),
        ):
            values[name] = tuple(kind.from_dict(v) for v in
                                 _sequence(values[name], name, empty=True))
        return cls(**values)

    def to_dict(self):
        return _dump(self)


MappingLookup = Callable[[str, str], ProjectMapping | None]
RepositoryLookup = Callable[[str, str, str], Repository | None]
RequesterReadAccess = Callable[[str, Principal, str], bool]


def freeze_meeting_scope(
    *, request: MeetingRequest, mapping_lookup: MappingLookup,
    repository_lookup: RepositoryLookup, requester_can_read: RequesterReadAccess,
    budget_lookup: Callable[[str], BudgetProfile | None], grant_lookup: GrantLookup,
    config: Settings, now: str, channel_grants: tuple[str, ...] = (),
) -> IntakeResult:
    """Authorize generation intent and return a reviewable immutable snapshot.

    Every invocation resolves CURRENT mapping, budget, grant, core permission,
    repository metadata and requester ACL. Failures expose no private names.
    A frozen scope/hash is data, not authorization: later read/egress/commit/
    delivery integrations must repeat current checks. No side effects occur here.
    """
    from ostag.briefs.meeting_models import BudgetProfile, FrozenScope, IntakeResult, MeetingRequest

    request = _copy(request, MeetingRequest)
    instant = _utc(now, "now")
    hooks = (mapping_lookup, repository_lookup, requester_can_read, budget_lookup, grant_lookup)
    if not all(callable(hook) for hook in hooks):
        raise _invalid("intake requires trusted current lookup callables")
    # Configuration/provider failures can contain private names. Do not expose
    # their messages or chained exceptions through this public boundary.
    try:
        current = _copy(mapping_lookup(request.namespace, request.mapping_id), ProjectMapping)
        budget = _copy(budget_lookup(request.budget_profile_id), BudgetProfile)
        if (current.namespace != request.namespace or current.mapping_id != request.mapping_id
                or current.revision != request.mapping_revision
                or request.requesting_principal not in current.allowed_requesters
                or budget.profile_id != request.budget_profile_id
                or budget.revision != request.budget_profile_revision):
            raise _invalid("current policy mismatch")
        if not (_utc(current.history_window.start, "history start")
                <= _utc(request.window.start, "start")
                < _utc(request.window.end, "end")
                <= min(instant, _utc(current.history_window.end, "history end"))):
            raise _invalid("window outside current history policy")
        if request.requested_as_of is not None and _utc(request.requested_as_of, "as_of") > instant:
            raise _invalid("requested_as_of is in the future")
        grant = authorize(
            lookup=grant_lookup, config=config, grant_id=request.grant_id,
            grant_revision=request.grant_revision, namespace=request.namespace,
            principal=request.requesting_principal, resource_ids=request.resource_ids,
            purpose=request.purpose.value, allowed_audience=request.audience,
            operation="generate", now=now, channel_grants=channel_grants,
        )
        repo_by_id = {r.repo_id: r for r in current.repositories}
        chat_by_id = {c.chat_id: c for c in current.chats}
        repos = tuple(repo_by_id[rid] for rid in request.repo_ids)
        chats = tuple(chat_by_id[cid] for cid in request.chat_ids)
        for resource in (*repos, *chats):
            # `is True` deliberately rejects 1, truthy containers and bot proofs.
            if requester_can_read(request.namespace, request.requesting_principal,
                                  resource.resource_id) is not True:
                raise _invalid("requester read access not established")
        for repo in repos:
            metadata = _copy(
                repository_lookup(request.namespace, repo.repo_id, repo.branch), Repository
            )
            if metadata != repo:
                raise _invalid("current repository metadata changed")
        scope = FrozenScope(
            request=request,
            mapping_name=current.name,
            mapping_policy_sha256=_hash(current.to_dict()),
            grant_sha256=_hash(grant.to_dict()),
            history_window=current.history_window,
            core_policy_sha256=_hash(
                {
                    "default_permission_level": config.default_permission_level,
                    "actor_permissions": list(config.actor_permissions),
                    "channel_grants": list(channel_grants),
                }
            ),
            repositories=repos,
            chats=chats,
            people=current.people,
            budget=budget,
            intake_at=now,
        )
    except Exception:
        raise SchemaError(
            "meeting_intake_denied",
            "Meeting scope could not be authorized.",
            "Re-check current policy, scope, versions and requester access.",
        ) from None
    return IntakeResult(scope)
