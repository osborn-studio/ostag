"""Strict, server-side Vercel Production deployment client.

The endpoint is fixed to Vercel's official API. Team, project, target, and Git
source are taken only from validated server configuration and a frozen GitHub
PR receipt; none are accepted from free-form chat text.

Source: https://vercel.com/docs/rest-api/deployments/create-a-new-deployment
Source: https://vercel.com/docs/rest-api/deployments/get-a-deployment-by-id-or-url
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlencode

from ostag.config import Settings
from ostag.connectors.github import PullRequestSource

_API_BASE = "https://api.vercel.com"
_MAX_RESPONSE_BYTES = 1_048_576
_DEPLOYMENT_ID = re.compile(r"^dpl_[A-Za-z0-9_-]{1,128}$")
_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
_STATES = {"BLOCKED", "BUILDING", "CANCELED", "ERROR", "INITIALIZING", "QUEUED", "READY"}
_FAILURE_STATES = {"BLOCKED", "CANCELED", "ERROR"}


class VercelAPIError(RuntimeError):
    pass


class VercelDeploymentError(RuntimeError):
    pass


@dataclass(frozen=True)
class VercelDeployment:
    deployment_id: str
    hostname: str
    state: str
    target: str | None
    alias_assigned: bool
    aliases: tuple[str, ...]
    git_sha: str

    @property
    def production_url(self) -> str:
        hostname = self.aliases[0] if self.aliases else self.hostname
        return f"https://{hostname}"


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read(_MAX_RESPONSE_BYTES + 1)
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return f"HTTP {exc.code}"
    error = payload.get("error") if isinstance(payload, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(message, str):
        label = f"{code}: " if isinstance(code, str) and code else ""
        return f"HTTP {exc.code}: {label}{message[:400]}"
    return f"HTTP {exc.code}"


def _open_vercel_json(request: urllib.request.Request, timeout: float) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise VercelAPIError(_http_error_message(exc)) from exc
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise VercelAPIError(f"transport failure: {type(exc).__name__}") from exc
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise VercelAPIError("Vercel response exceeded 1 MiB")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VercelAPIError("Vercel returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise VercelAPIError("Vercel response is not a JSON object")
    return payload


def _valid_hostname(value: object) -> str:
    if not isinstance(value, str) or not _HOSTNAME.fullmatch(value):
        return ""
    return value


def _parse_deployment(payload: object) -> VercelDeployment:
    if not isinstance(payload, dict):
        raise VercelDeploymentError("Vercel deployment response is not an object")
    deployment_id = payload.get("id")
    hostname = _valid_hostname(payload.get("url"))
    state = payload.get("readyState")
    target = payload.get("target")
    alias_assigned = payload.get("aliasAssigned")
    aliases = payload.get("alias", [])
    git_source = payload.get("gitSource")
    git_sha = git_source.get("sha", "") if isinstance(git_source, dict) else ""
    valid_aliases = (
        isinstance(aliases, list)
        and len(aliases) <= 100
        and all(bool(_valid_hostname(alias)) for alias in aliases)
    )
    if (
        not isinstance(deployment_id, str)
        or not _DEPLOYMENT_ID.fullmatch(deployment_id)
        or not hostname
        or state not in _STATES
        or target not in {"production", "staging", None}
        or not isinstance(alias_assigned, bool)
        or not valid_aliases
        or not isinstance(git_sha, str)
        or len(git_sha) > 64
    ):
        raise VercelDeploymentError("Vercel deployment response is malformed")
    return VercelDeployment(
        deployment_id=deployment_id,
        hostname=hostname,
        state=state,
        target=target,
        alias_assigned=alias_assigned,
        aliases=tuple(aliases),
        git_sha=git_sha,
    )


class VercelClient:
    def __init__(
        self,
        config: Settings,
        *,
        opener: Callable[[urllib.request.Request, float], dict[str, Any]] = _open_vercel_json,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._opener = opener
        self._sleep = sleeper
        self._monotonic = monotonic

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        query = {"teamId": self.config.vercel_team_id}
        if method == "GET":
            query["withGitRepoInfo"] = "true"
        url = f"{_API_BASE}{path}?{urlencode(query)}"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.config.vercel_access_token}",
                "Content-Type": "application/json",
                "User-Agent": "OStag-GitHub-Vercel-Release/1",
            },
        )
        request_timeout = min(
            30.0,
            float(self.config.vercel_deploy_timeout_seconds),
            timeout_seconds if timeout_seconds is not None else 30.0,
        )
        if request_timeout <= 0:
            raise VercelAPIError("Vercel request deadline expired")
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._opener, request, request_timeout),
                timeout=request_timeout,
            )
        except Exception as exc:
            safe = str(exc)
            if self.config.vercel_access_token:
                safe = safe.replace(self.config.vercel_access_token, "[REDACTED]")
            raise VercelAPIError(f"Vercel request failed: {safe[-500:]}") from exc

    async def create_production_deployment(
        self,
        source: PullRequestSource,
        *,
        action_id: str,
        _timeout_seconds: float | None = None,
    ) -> VercelDeployment:
        owner, repo = source.repository.split("/", 1)
        payload = {
            "name": self.config.vercel_project_name,
            "project": self.config.vercel_project_id,
            "target": "production",
            "gitSource": {
                "type": "github",
                "org": owner,
                "repo": repo,
                "ref": source.head_ref,
                "sha": source.head_sha,
            },
            "meta": {
                "ostagActionId": action_id,
                "githubPullRequest": str(source.number),
                "githubCommitSha": source.head_sha,
            },
        }
        return _parse_deployment(
            await self._request(
                "POST",
                "/v13/deployments",
                payload=payload,
                timeout_seconds=_timeout_seconds,
            )
        )

    async def get_deployment(
        self,
        deployment_id: str,
        *,
        _timeout_seconds: float | None = None,
    ) -> VercelDeployment:
        if not _DEPLOYMENT_ID.fullmatch(deployment_id):
            raise VercelDeploymentError("Vercel deployment ID is invalid")
        path = f"/v13/deployments/{quote(deployment_id, safe='')}"
        return _parse_deployment(
            await self._request("GET", path, timeout_seconds=_timeout_seconds)
        )

    async def deploy_production(
        self, source: PullRequestSource, *, action_id: str
    ) -> VercelDeployment:
        timeout = float(self.config.vercel_deploy_timeout_seconds)
        deadline = self._monotonic() + timeout
        current = await self.create_production_deployment(
            source,
            action_id=action_id,
            _timeout_seconds=max(0.0, deadline - self._monotonic()),
        )
        attempts = max(
            1,
            self.config.vercel_deploy_timeout_seconds
            // self.config.vercel_poll_interval_seconds,
        )
        for _ in range(attempts):
            if current.state in _FAILURE_STATES:
                raise VercelDeploymentError(
                    f"Vercel deployment {current.deployment_id} ended in {current.state}"
                )
            if (
                current.state == "READY"
                and current.target == "production"
                and current.alias_assigned
                and current.aliases
            ):
                if current.git_sha != source.head_sha:
                    raise VercelDeploymentError(
                        "Vercel READY deployment did not confirm the frozen GitHub SHA"
                    )
                return current
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            await self._sleep(min(self.config.vercel_poll_interval_seconds, remaining))
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            current = await self.get_deployment(
                current.deployment_id,
                _timeout_seconds=remaining,
            )
        raise VercelDeploymentError(
            f"Vercel deployment {current.deployment_id} timed out before Production alias"
        )
