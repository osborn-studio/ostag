"""Normalized, SHA-bound pull/merge request completion evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from typing import Any, Mapping
from urllib.parse import urlparse

from ostag.core.completion import (
    CompletionEvidenceFact,
    EvidenceClaim,
    EvidenceProvenance,
    EvidenceSubject,
)
from ostag.core.schemas import SchemaError


_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_CHECK_STATES = {"passed", "failed", "pending"}
_CHANGE_STATES = {"open", "closed", "merged"}


def _strict(data: Mapping[str, Any], allowed: frozenset[str], schema: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise SchemaError(
            "unknown_field",
            f"{schema} does not accept: {', '.join(sorted(unknown))}",
            f"Allowed fields: {', '.join(sorted(allowed))}.",
        )


def _text(value: object, field: str, schema: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(
            "invalid_provider_evidence",
            f"{schema}.{field} must be a non-empty string",
            "Refresh provider evidence.",
        )
    return value.strip()


def normalize_utc_timestamp(value: object) -> str:
    text = _text(value, "observedAt", "ProviderChangeEvidence")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SchemaError(
            "invalid_provider_evidence",
            "Provider timestamp is not ISO-8601",
            "Refresh provider evidence.",
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise SchemaError(
            "invalid_provider_evidence",
            "Provider timestamp must be UTC",
            "Refresh provider evidence.",
        )
    return parsed.isoformat()


def require_sha(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value.casefold()):
        raise SchemaError(
            "invalid_provider_evidence",
            f"{field} must be a full Git object SHA",
            "Refresh provider evidence with a full SHA.",
        )
    return value.casefold()


def require_https_url(value: object, field: str) -> str:
    text = _text(value, field, "ProviderChangeEvidence")
    parsed = urlparse(text)
    if parsed.scheme != "https" or not parsed.netloc:
        raise SchemaError(
            "invalid_provider_evidence",
            f"{field} must be an absolute HTTPS URL",
            "Use the provider web URL.",
        )
    return text


@dataclass(frozen=True)
class ProviderCheck:
    name: str
    state: str
    sha: str
    url: str = ""

    def __post_init__(self) -> None:
        _text(self.name, "name", "ProviderCheck")
        if self.state not in _CHECK_STATES:
            raise SchemaError(
                "invalid_provider_evidence",
                f"Unsupported provider check state: {self.state}",
                "Use passed, failed, or pending.",
            )
        object.__setattr__(self, "sha", require_sha(self.sha, "ProviderCheck.sha"))
        if self.url:
            object.__setattr__(self, "url", require_https_url(self.url, "ProviderCheck.url"))

    def to_dict(self) -> dict[str, Any]:
        payload = {"name": self.name, "state": self.state, "sha": self.sha}
        if self.url:
            payload["url"] = self.url
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ProviderCheck:
        _strict(data, frozenset({"name", "state", "sha", "url"}), "ProviderCheck")
        return cls(
            name=data.get("name", ""),
            state=data.get("state", ""),
            sha=data.get("sha", ""),
            url=data.get("url", ""),
        )


@dataclass(frozen=True)
class ProviderChangeEvidence:
    provider: str
    repository: str
    number: int
    url: str
    state: str
    draft: bool
    head_sha: str
    merge_sha: str
    observed_at: str
    checks: tuple[ProviderCheck, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise SchemaError(
                "invalid_provider_evidence", "Unsupported evidence schema", "Use schema version 1."
            )
        if self.provider not in {"github", "gitlab"}:
            raise SchemaError(
                "invalid_provider_evidence", "Unsupported evidence provider", "Use github or gitlab."
            )
        repository = _text(self.repository, "repository", "ProviderChangeEvidence")
        if "/" not in repository:
            raise SchemaError(
                "invalid_provider_evidence", "Repository must use owner/name", "Fix repository."
            )
        if isinstance(self.number, bool) or not isinstance(self.number, int) or self.number < 1:
            raise SchemaError(
                "invalid_provider_evidence", "Change number must be positive", "Fix number."
            )
        if self.state not in _CHANGE_STATES:
            raise SchemaError(
                "invalid_provider_evidence", "Unsupported change state", "Use open, closed, or merged."
            )
        if not isinstance(self.draft, bool):
            raise SchemaError(
                "invalid_provider_evidence", "draft must be boolean", "Refresh provider evidence."
            )
        object.__setattr__(self, "url", require_https_url(self.url, "url"))
        object.__setattr__(self, "head_sha", require_sha(self.head_sha, "headSha"))
        if self.state == "merged":
            object.__setattr__(self, "merge_sha", require_sha(self.merge_sha, "mergeSha"))
        elif self.merge_sha:
            object.__setattr__(self, "merge_sha", require_sha(self.merge_sha, "mergeSha"))
        object.__setattr__(self, "observed_at", normalize_utc_timestamp(self.observed_at))
        if any(check.sha != self.head_sha for check in self.checks):
            raise SchemaError(
                "provider_sha_mismatch",
                "Provider checks are not bound to the change head SHA",
                "Refresh checks for the current head SHA.",
            )

    @property
    def checks_state(self) -> str:
        states = {check.state for check in self.checks}
        if "failed" in states:
            return "failed"
        if self.checks and states == {"passed"}:
            return "passed"
        return "pending"

    @property
    def completion_state(self) -> str:
        if self.checks_state == "failed":
            return "failed"
        if self.state == "merged" and self.checks_state == "passed" and not self.draft:
            return "passed"
        return "pending"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schemaVersion": self.schema_version,
            "provider": self.provider,
            "repository": self.repository,
            "number": self.number,
            "url": self.url,
            "state": self.state,
            "draft": self.draft,
            "headSha": self.head_sha,
            "observedAt": self.observed_at,
            "checks": [check.to_dict() for check in self.checks],
            "checksState": self.checks_state,
            "completionState": self.completion_state,
        }
        if self.merge_sha:
            payload["mergeSha"] = self.merge_sha
        return payload

    def _canonical_payload(self) -> dict[str, Any]:
        payload = self.to_dict()
        payload.pop("checksState", None)
        payload.pop("completionState", None)
        return payload

    @property
    def payload_digest(self) -> str:
        raw = json.dumps(
            self._canonical_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ProviderChangeEvidence:
        _strict(
            data,
            frozenset(
                {
                    "schemaVersion",
                    "provider",
                    "repository",
                    "number",
                    "url",
                    "state",
                    "draft",
                    "headSha",
                    "mergeSha",
                    "observedAt",
                    "checks",
                    "checksState",
                    "completionState",
                }
            ),
            "ProviderChangeEvidence",
        )
        checks = data.get("checks", ())
        if not isinstance(checks, (list, tuple)) or not all(
            isinstance(item, Mapping) for item in checks
        ):
            raise SchemaError(
                "invalid_provider_evidence", "checks must be objects", "Refresh provider evidence."
            )
        result = cls(
            schema_version=data.get("schemaVersion", 0),
            provider=data.get("provider", ""),
            repository=data.get("repository", ""),
            number=data.get("number", 0),
            url=data.get("url", ""),
            state=data.get("state", ""),
            draft=data.get("draft", None),
            head_sha=data.get("headSha", ""),
            merge_sha=data.get("mergeSha", ""),
            observed_at=data.get("observedAt", ""),
            checks=tuple(ProviderCheck.from_dict(item) for item in checks),
        )
        if data.get("checksState", result.checks_state) != result.checks_state:
            raise SchemaError(
                "invalid_provider_evidence",
                "checksState does not match normalized checks",
                "Regenerate provider evidence.",
            )
        if data.get("completionState", result.completion_state) != result.completion_state:
            raise SchemaError(
                "invalid_provider_evidence",
                "completionState does not match provider state and checks",
                "Regenerate provider evidence.",
            )
        return result

    def to_completion_facts(
        self, *, work_thread_id: str, cycle: int, received_at: str
    ) -> tuple[CompletionEvidenceFact, CompletionEvidenceFact]:
        received = normalize_utc_timestamp(received_at)
        subject = EvidenceSubject(
            provider=self.provider,
            resource_ref=(
                f"{self.repository}#{self.number}"
                if self.provider == "github"
                else f"{self.repository}!{self.number}"
            ),
            resource_version=self.head_sha,
        )
        provenance = EvidenceProvenance(
            adapter=f"ostag-{self.provider}",
            adapter_version="1",
            payload_digest=self.payload_digest,
        )
        prefix = self.payload_digest[:12]
        state_fact = CompletionEvidenceFact(
            id=f"evidence_{prefix}_state",
            work_thread_id=work_thread_id,
            cycle=cycle,
            kind="provider_state",
            assurance="verified",
            subject=subject,
            claim=EvidenceClaim(
                predicate="state",
                outcome=self.state,
                observations={"mergeSha": self.merge_sha, "draft": str(self.draft).lower()},
            ),
            provenance=provenance,
            observed_at=self.observed_at,
            received_at=received,
        )
        checks_fact = CompletionEvidenceFact(
            id=f"evidence_{prefix}_checks",
            work_thread_id=work_thread_id,
            cycle=cycle,
            kind="provider_checks",
            assurance="verified",
            subject=subject,
            claim=EvidenceClaim(
                predicate="checks",
                outcome=self.checks_state,
                observations={"count": str(len(self.checks)), "headSha": self.head_sha},
            ),
            provenance=provenance,
            observed_at=self.observed_at,
            received_at=received,
        )
        return state_fact, checks_fact
