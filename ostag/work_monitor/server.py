"""FastAPI composition root for the persistent work-monitor service."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

from fastapi import APIRouter, FastAPI, HTTPException, Request

from ostag.config import Settings, load_settings
from ostag.core.identity import Principal
from ostag.core.schemas import SchemaError

from .identity import Destination, IdentityError, IdentityStore
from .models import MonitorEvent
from .projection import Projector
from .service import AmbiguousTask, TaskNotFound, WorkMonitorService
from .store import MonitorStore


class LifespanOwner(Protocol):
    async def run(self, stop: asyncio.Event) -> None: ...


@dataclass(frozen=True)
class ServerAuth:
    instance_secrets: dict[str, str]
    human_token: str = ""
    admin_token: str = ""
    max_clock_skew_seconds: int = 300

    def instance(self, request: Request, body: bytes) -> str:
        instance_id = request.headers.get("X-OStag-Instance", "")
        timestamp = request.headers.get("X-OStag-Timestamp", "")
        supplied = request.headers.get("X-OStag-Signature", "")
        secret = self.instance_secrets.get(instance_id, "")
        try:
            fresh = abs(int(time.time()) - int(timestamp)) <= self.max_clock_skew_seconds
        except ValueError:
            fresh = False
        expected = hmac.new(
            secret.encode(), timestamp.encode() + b"\n" + body, hashlib.sha256
        ).hexdigest()
        if not secret or not fresh or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="invalid instance credentials")
        return instance_id

    def human(self, request: Request) -> tuple[Principal, Destination]:
        authorization = request.headers.get("Authorization", "")
        token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        if not self.human_token or not hmac.compare_digest(token, self.human_token):
            raise HTTPException(status_code=401, detail="invalid human credentials")
        try:
            principal = Principal(
                request.headers["X-OStag-Platform"], request.headers["X-OStag-Actor"]
            )
            destination = Destination(
                request.headers["X-OStag-Platform"],
                request.headers["X-OStag-Destination"],
                request.headers["X-OStag-Chat-Type"],
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="invalid human identity headers") from exc
        return principal, destination

    def admin(self, request: Request) -> None:
        authorization = request.headers.get("Authorization", "")
        token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        if not self.admin_token or not hmac.compare_digest(token, self.admin_token):
            raise HTTPException(status_code=401, detail="invalid admin credentials")


class _Runtime:
    def __init__(self, service: WorkMonitorService | None, projector: Projector | None) -> None:
        self.service = service
        self.projector = projector


async def _supervise(
    owner: LifespanOwner, stop: asyncio.Event, backoff: tuple[float, ...]
) -> None:
    failures = 0
    while not stop.is_set():
        try:
            await owner.run(stop)
            if not stop.is_set():
                raise RuntimeError("lifespan owner exited unexpectedly")
        except asyncio.CancelledError:
            raise
        except Exception:
            delay = backoff[min(failures, len(backoff) - 1)]
            failures += 1
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                continue


def create_app(
    *,
    settings: Settings | None = None,
    service: WorkMonitorService | None = None,
    projector: Projector | None = None,
    auth: ServerAuth | None = None,
    routers: Iterable[APIRouter] = (),
    feishu_ws: LifespanOwner | None = None,
    scheduler: LifespanOwner | None = None,
    outbox: LifespanOwner | None = None,
    supervisor_backoff: tuple[float, ...] = (1, 2, 5, 15, 30),
) -> FastAPI:
    """Build an inert app; databases and background owners start only in lifespan."""
    configured = settings
    runtime = _Runtime(service, projector)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal configured, auth
        if runtime.service is None or runtime.projector is None:
            configured = configured or load_settings()
            store = MonitorStore(configured.monitor_server_db)
            identities = IdentityStore(store.path, enrollment_secret=configured.monitor_enrollment_secret)
            runtime.service = WorkMonitorService(store, identities)
            runtime.projector = Projector(store)
        if auth is None:
            assert configured is not None
            auth = ServerAuth(
                dict(configured.monitor_instance_secrets),
                human_token=configured.monitor_human_token,
                admin_token=configured.monitor_admin_token,
            )
        stop = asyncio.Event()
        owners = tuple(owner for owner in (feishu_ws, scheduler, outbox) if owner is not None)
        tasks = [asyncio.create_task(_supervise(owner, stop, supervisor_backoff)) for owner in owners]
        try:
            yield
        finally:
            stop.set()
            if tasks:
                try:
                    await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
                except TimeoutError:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(lifespan=lifespan)

    def dependencies() -> tuple[WorkMonitorService, Projector, ServerAuth]:
        if runtime.service is None or runtime.projector is None or auth is None:
            raise HTTPException(status_code=503, detail="service not started")
        return runtime.service, runtime.projector, auth

    def translate(exc: Exception) -> HTTPException:
        if isinstance(exc, (IdentityError, PermissionError)):
            return HTTPException(403, str(exc))
        if isinstance(exc, TaskNotFound):
            return HTTPException(404, str(exc))
        if isinstance(exc, AmbiguousTask):
            return HTTPException(409, {"candidates": exc.candidates})
        if isinstance(exc, (ValueError, SchemaError)):
            return HTTPException(422, str(exc))
        return HTTPException(500, "internal server error")

    @app.post("/v1/events")
    async def events(request: Request) -> dict[str, int]:
        svc, project, credentials = dependencies()
        body = await request.body()
        instance_id = credentials.instance(request, body)
        try:
            payload = json.loads(body)
            raw_events = payload["events"]
            parsed = [MonitorEvent.from_dict(item) for item in raw_events]
            if any(event.instance_id != instance_id for event in parsed):
                raise ValueError("event instance does not match authenticated instance")
            for event in parsed:
                project.apply(event)
            return {"ack_sequence": svc.store.last_sequence(instance_id)}
        except Exception as exc:
            raise translate(exc) from exc

    def human(request: Request) -> tuple[WorkMonitorService, Principal, Destination]:
        svc, _, credentials = dependencies()
        principal, destination = credentials.human(request)
        return svc, principal, destination

    @app.get("/v1/board")
    async def board(request: Request, page: int = 1, page_size: int = 6) -> dict[str, object]:
        svc, principal, destination = human(request)
        try:
            return svc.board(principal, destination, page=page, page_size=page_size).to_dict()
        except Exception as exc:
            raise translate(exc) from exc

    @app.get("/v1/tasks/{task_id}")
    async def task(request: Request, task_id: str) -> dict[str, object]:
        svc, principal, destination = human(request)
        try:
            return svc.task(principal, destination, task_id).to_dict()
        except Exception as exc:
            raise translate(exc) from exc

    @app.get("/v1/instances")
    async def instances(request: Request) -> list[dict[str, object]]:
        svc, principal, destination = human(request)
        try:
            return svc.instances(principal, destination)
        except Exception as exc:
            raise translate(exc) from exc

    @app.post("/v1/subscriptions")
    async def subscriptions(request: Request) -> dict[str, str]:
        svc, principal, destination = human(request)
        try:
            payload = await request.json()
            message = svc.watch(principal, destination, instance_id=payload.get("instance_id", ""),
                                task_id=payload.get("task_id", ""))
            return {"message": message}
        except Exception as exc:
            raise translate(exc) from exc

    @app.post("/v1/commands/poll")
    async def poll(request: Request) -> dict[str, list[dict[str, object]]]:
        svc, _, credentials = dependencies()
        body = await request.body()
        instance_id = credentials.instance(request, body)
        if body not in (b"", b"{}"):
            raise HTTPException(422, "poll body must be empty")
        return {"commands": list(svc.poll_commands(instance_id))}

    @app.post("/v1/commands/{command_id}/ack")
    async def ack(request: Request, command_id: str) -> dict[str, object]:
        svc, _, credentials = dependencies()
        body = await request.body()
        instance_id = credentials.instance(request, body)
        try:
            result = json.loads(body)["result"]
            receipt = svc.acknowledge_control(instance_id, command_id, result)
            return {"state": receipt.state}
        except Exception as exc:
            raise translate(exc) from exc

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        dependencies()
        return {"status": "ok"}

    for extension in routers:
        app.include_router(extension)
    return app
app = create_app()
