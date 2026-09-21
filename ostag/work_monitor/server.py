"""FastAPI composition root for the persistent work-monitor service."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal, Protocol

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from ostag.briefs.runtime import BriefStatusOwner
from ostag.briefs.production_owner import BriefProductionOwner, TrustedBriefComposition
from ostag.config import Settings, load_settings
from ostag.core.ledger_factory import configured_ledger
from ostag.core.identity import Principal
from ostag.core.schemas import SchemaError

from .access_snapshots import AccessSnapshotStore
from .identity import Destination, IdentityError, IdentityStore
from .models import MonitorEvent
from .projection import Projector
from .service import AmbiguousTask, TaskNotFound, WorkMonitorService
from .store import MonitorStore


class LifespanOwner(Protocol):
    async def run(self, stop: asyncio.Event) -> None: ...


@dataclass(frozen=True)
class HumanCredential:
    principal: Principal
    destination: Destination


@dataclass(frozen=True)
class SecurityContract:
    kind: Literal["verified_platform", "authenticated_dependency"]
    dependency: Callable[..., object]


@dataclass(frozen=True)
class ServerAuth:
    instance_secrets: dict[str, str]
    human_credentials: dict[str, HumanCredential] | None = None
    admin_token: str = ""
    max_clock_skew_seconds: int = 300
    clock: Callable[[], float] = time.time

    def instance(self, request: Request, body: bytes, store: MonitorStore) -> str:
        instance_id = request.headers.get("X-OStag-Instance", "")
        timestamp = request.headers.get("X-OStag-Timestamp", "")
        supplied = request.headers.get("X-OStag-Signature", "")
        nonce = request.headers.get("X-OStag-Nonce", "")
        secret = self.instance_secrets.get(instance_id, "")
        try:
            signed_at = int(timestamp)
            now = int(self.clock())
            fresh = abs(now - signed_at) <= self.max_clock_skew_seconds
        except ValueError:
            signed_at = 0
            now = int(self.clock())
            fresh = False
        canonical = "\n".join(
            (
                "v1",
                request.method.upper(),
                request.url.path,
                instance_id,
                timestamp,
                nonce,
                hashlib.sha256(body).hexdigest(),
            )
        ).encode()
        expected = hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
        valid = bool(secret and nonce and fresh) and hmac.compare_digest(supplied, expected)
        if not valid or not store.consume_instance_nonce(
            instance_id,
            nonce,
            now=now,
            expires_at=signed_at + self.max_clock_skew_seconds,
        ):
            raise HTTPException(status_code=401, detail="invalid instance credentials")
        return instance_id

    def human(self, request: Request) -> tuple[Principal, Destination]:
        authorization = request.headers.get("Authorization", "")
        token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        matched: HumanCredential | None = None
        for candidate, credential in (self.human_credentials or {}).items():
            if hmac.compare_digest(token, candidate):
                matched = credential
        if matched is None:
            raise HTTPException(status_code=401, detail="invalid human credentials")
        return matched.principal, matched.destination

    def admin(self, request: Request) -> None:
        authorization = request.headers.get("Authorization", "")
        token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        if not self.admin_token or not hmac.compare_digest(token, self.admin_token):
            raise HTTPException(status_code=401, detail="invalid admin credentials")


class _Runtime:
    def __init__(self, service: WorkMonitorService | None, projector: Projector | None) -> None:
        self.service = service
        self.projector = projector
        self.copilot: object | None = None


async def _supervise(owner: LifespanOwner, stop: asyncio.Event, backoff: tuple[float, ...]) -> None:
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
    routers: Iterable[tuple[APIRouter, SecurityContract]] = (),
    feishu_ws: LifespanOwner | None = None,
    scheduler: LifespanOwner | None = None,
    daily_scheduler: LifespanOwner | None = None,
    outbox: LifespanOwner | None = None,
    telegram_client: object | None = None,
    supervisor_backoff: tuple[float, ...] = (1, 2, 5, 15, 30),
    brief_composition: TrustedBriefComposition | None = None,
) -> FastAPI:
    """Build an inert app; databases and background owners start only in lifespan."""
    if outbox is not None:
        raise ValueError("scheduler owns outbox delivery; a separate outbox owner is forbidden")
    configured = settings.validate() if settings is not None else load_settings()
    managed_ledger_paths = configured.managed_ledger_paths
    if managed_ledger_paths and (feishu_ws is not None or daily_scheduler is not None):
        raise ValueError('Managed ledger policy requires host-created transport and daily scheduler owners.')
    runtime = _Runtime(service, projector)
    auto_feishu_ws: LifespanOwner | None = None
    auto_scheduler: LifespanOwner | None = None
    auto_daily_scheduler: LifespanOwner | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal auth, auto_feishu_ws, auto_scheduler, auto_daily_scheduler
        # Validate every configured domain before starting databases or owners.
        for path in managed_ledger_paths:
            configured_ledger(path, managed_paths=managed_ledger_paths)
        if runtime.service is not None:
            configured_ledger(runtime.service.store.path.with_name('run_ledger.jsonl'),
                managed_paths=managed_ledger_paths, existing=runtime.service.ledger)
        if scheduler is not None and managed_ledger_paths:
            # Unknown injected owners cannot attest to their ledger wiring.
            from .scheduler import WorkMonitorScheduler
            if type(scheduler) is not WorkMonitorScheduler:
                raise ValueError('Managed ledger policy requires a validated scheduler.')
            configured_ledger(scheduler.store.path.with_name('run_ledger.jsonl'),
                managed_paths=managed_ledger_paths, existing=scheduler.ledger)
        if runtime.service is None or runtime.projector is None:
            store = MonitorStore(configured.monitor_server_db)
            identities = IdentityStore(
                store.path, enrollment_secret=configured.monitor_enrollment_secret
            )
            runtime.service = WorkMonitorService(store, identities,
                                                managed_ledger_paths=managed_ledger_paths)
            # Phase 3 shadow hook: best-effort access-profile capture after
            # projection commits; never on the routing path. Startup
            # reconciliation recovers captures lost to a crash or a failed
            # hook (also best-effort — it must not block serving).
            snapshots = AccessSnapshotStore(store, identities)
            runtime.projector = Projector(store, access_snapshots=snapshots)
            try:
                snapshots.reconcile()
            except Exception:  # noqa: BLE001 - shadow recovery must not block startup
                pass
        if auth is None:
            auth = ServerAuth(
                dict(configured.monitor_instance_secrets),
                human_credentials=configured.monitor_human_credentials_dict(),
                admin_token=configured.monitor_admin_token,
            )
        if runtime.copilot is None and auth is not None:
            owner = next(
                (
                    credential.principal
                    for credential in (auth.human_credentials or {}).values()
                    if runtime.service.identities.is_global_owner(credential.principal)
                ),
                None,
            )
            if owner is not None:
                from ostag.copilot_relay.feishu_routes import CopilotFeishuController
                from ostag.copilot_relay.policy import RelayPolicy
                from ostag.copilot_relay.service import CopilotRelayService
                from ostag.copilot_relay.status_board import OsstackStatusBoardService
                from ostag.copilot_relay.store import RelayStore

                from ostag.copilot_relay.route_explainer import (
                    RouteExplainer,
                    RoutingDecisionStore,
                )

                relay_store = RelayStore(runtime.service.store)
                relay_service = CopilotRelayService(
                    relay_store,
                    RelayPolicy(
                        owner,
                        scope_provider=runtime.service.identities.allowed_instances,
                    ),
                )
                runtime.copilot = CopilotFeishuController(
                    relay_service,
                    # Phase 5: read-only explanations for every routing
                    # decision; recording is best-effort and never on the
                    # routing path.
                    route_explainer=RouteExplainer(
                        relay_service,
                        RoutingDecisionStore(
                            runtime.service.store, runtime.service.identities
                        ),
                    ),
                    write_enabled=configured.copilot_relay_write_enabled,
                    canary_session_ids=frozenset(
                        configured.copilot_relay_canary_session_ids
                    ),
                    status_service=OsstackStatusBoardService(
                        relay_store,
                        runtime.service.identities,
                        owner=owner,
                        offline_seconds=configured.monitor_offline_seconds,
                    ),
                    status_board_enabled=configured.copilot_status_board_enabled,
                )
        if (
            feishu_ws is None
            and auto_feishu_ws is None
            and configured.feishu_use_websocket
            and configured.feishu_app_id
            and configured.feishu_app_secret
        ):
            from ostag.adapters.feishu import FeishuAdapter
            from ostag.core.runner import OSTagRunner

            assert runtime.service is not None
            runner = OSTagRunner(
                configured, work_monitor=runtime.service, copilot_relay=runtime.copilot
            )
            auto_feishu_ws = FeishuAdapter(configured, runner=runner)
        if scheduler is None and auto_scheduler is None and configured.monitor_scheduler_enabled:
            from ostag.adapters.feishu import FeishuAdapter
            from ostag.adapters.telegram import TelegramAdapter, TelegramBotClient
            from .scheduler import WorkMonitorScheduler

            assert runtime.service is not None
            transports = {}
            if configured.feishu_app_id and configured.feishu_app_secret:
                transports["feishu"] = FeishuAdapter(configured)
            if configured.telegram_bot_token:
                transports["telegram"] = TelegramAdapter(
                    configured, client=TelegramBotClient(configured.telegram_bot_token)
                )
            auto_scheduler = WorkMonitorScheduler(
                runtime.service.store,
                runtime.service.identities,
                transports,
                managed_ledger_paths=managed_ledger_paths,
                morning_hour=configured.monitor_morning_digest_hour,
                evening_hour=configured.monitor_evening_digest_hour,
                degraded_seconds=configured.monitor_degraded_seconds,
                offline_seconds=configured.monitor_offline_seconds,
                retry_cap=configured.monitor_notification_retry_cap,
                copilot=runtime.copilot,
            )
        if (
            daily_scheduler is None
            and auto_daily_scheduler is None
            and (configured.daily_task_boards_enabled or configured.ambient_daily_board_enabled)
        ):
            from ostag.adapters.dingtalk import DingTalkAdapter
            from ostag.adapters.feishu import FeishuAdapter
            from ostag.connectors.requirements import (
                DwsCLI,
                DwsRequirementTableConnector,
                FeishuBaseReadConnector,
            )
            from .daily_digest_scheduler import (
                DailyDigestProfile,
                DailyRequirementBoardProfile,
                DailyTaskBoardScheduler,
            )

            assert runtime.service is not None
            feishu_base_connector = FeishuBaseReadConnector(
                api_base_url=configured.feishu_api_base_url,
                app_id=configured.feishu_app_id,
                app_secret=configured.feishu_app_secret,
            )
            profiles = (
                DailyDigestProfile(
                    platform="feishu",
                    destination_id=configured.daily_task_board_feishu_chat_id,
                    base_token=configured.daily_task_board_feishu_base_token,
                    table_id=configured.daily_task_board_feishu_table_id,
                    table_url=configured.daily_task_board_feishu_table_url,
                    manager_ids=configured.daily_task_board_feishu_manager_ids,
                    connector=feishu_base_connector,
                    transport=FeishuAdapter(configured),
                    record_limit=configured.daily_task_board_record_limit,
                ),
                DailyDigestProfile(
                    platform="dingtalk",
                    destination_id=configured.daily_task_board_dingtalk_chat_id,
                    base_token=configured.daily_task_board_dingtalk_base_token,
                    table_id=configured.daily_task_board_dingtalk_table_id,
                    table_url=configured.daily_task_board_dingtalk_table_url,
                    manager_ids=configured.daily_task_board_dingtalk_manager_ids,
                    connector=DwsRequirementTableConnector(DwsCLI(configured)),
                    transport=DingTalkAdapter(configured),
                    record_limit=configured.daily_task_board_record_limit,
                ),
            ) if configured.daily_task_boards_enabled else ()
            requirement_profiles = (
                DailyRequirementBoardProfile(
                    destination_id=configured.ambient_chat_ids[0],
                    base_token=configured.auto_base_default_base_token,
                    table_id=configured.auto_base_default_table_id,
                    table_url=configured.ambient_daily_board_base_url,
                    group_name=configured.ambient_daily_board_group_name,
                    connector=feishu_base_connector,
                    transport=FeishuAdapter(configured),
                    record_limit=configured.ambient_record_limit,
                ),
            ) if configured.ambient_daily_board_enabled else ()
            poll_values = []
            if configured.daily_task_boards_enabled:
                poll_values.append(configured.daily_task_board_poll_seconds)
            if configured.ambient_daily_board_enabled:
                poll_values.append(configured.ambient_interval_seconds)
            auto_daily_scheduler = DailyTaskBoardScheduler(
                runtime.service.store,
                profiles,
                morning_hour=configured.daily_task_board_morning_hour,
                evening_hour=configured.daily_task_board_evening_hour,
                poll_seconds=min(poll_values),
                requirement_profiles=requirement_profiles,
                requirement_hour=configured.ambient_daily_board_hour,
                requirement_window_minutes=(
                    configured.ambient_daily_board_window_minutes
                ),
            )
        stop = asyncio.Event()
        owners = tuple(
            owner
            for owner in (
                feishu_ws or auto_feishu_ws,
                scheduler or auto_scheduler,
                daily_scheduler or auto_daily_scheduler,
            )
            if owner is not None
        )
        # Flags are authoritative even when trusted host code supplies a composition.
        brief_owner = (BriefProductionOwner(configured, brief_composition) if configured.brief_enabled
                       else BriefStatusOwner(configured.brief_status_dir, configured.brief_control_path))
        app.state.brief_owner = brief_owner
        app.state.brief_status_owner = (brief_owner.status if configured.brief_enabled else brief_owner)
        app.state.brief_application = brief_owner.application if configured.brief_enabled else None
        tasks = [
            asyncio.create_task(_supervise(owner, stop, supervisor_backoff)) for owner in owners
        ]
        try:
            await brief_owner.start()
            yield
        finally:
            stop.set()
            try:
                await brief_owner.stop()
            finally:
                if tasks:
                    try:
                        await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
                    except TimeoutError:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(lifespan=lifespan)

    def active_service() -> WorkMonitorService:
        if runtime.service is None:
            raise HTTPException(status_code=503, detail="service not started")
        return runtime.service

    def active_copilot() -> object | None:
        return runtime.copilot

    if all(
        (
            configured.feishu_app_id,
            configured.feishu_app_secret,
            configured.feishu_encrypt_key,
            configured.feishu_verification_token,
        )
    ):
        from .feishu_card_security import FeishuCardSecurity
        from .feishu_routes import create_feishu_card_router

        card_security = FeishuCardSecurity(
            encrypt_key=configured.feishu_encrypt_key,
            verification_token=configured.feishu_verification_token,
            expected_app_id=configured.feishu_app_id,
            store=lambda: active_service().store,
            max_body_bytes=configured.monitor_server_max_body_bytes,
        )
        register_feishu_card_router(
            app,
            create_feishu_card_router(active_service, copilot=active_copilot),
            security=SecurityContract("verified_platform", card_security),
        )

    if all(
        (
            configured.telegram_bot_token,
            configured.telegram_webhook_secret,
            configured.telegram_webhook_url,
        )
    ):
        from ostag.adapters.telegram import TelegramAdapter, TelegramBotClient
        from .telegram_routes import TelegramWebhookSecurity, create_telegram_router

        if configured.env.casefold() in {
            "prod",
            "production",
        } and not configured.telegram_webhook_url.startswith("https://"):
            raise ValueError("Telegram production webhook URL must use HTTPS")
        bot_client = telegram_client or TelegramBotClient(configured.telegram_bot_token)
        if not isinstance(bot_client, TelegramBotClient):
            raise ValueError("telegram_client must be TelegramBotClient")
        telegram_adapter = TelegramAdapter(configured, client=bot_client)
        telegram_security = TelegramWebhookSecurity(
            secret=configured.telegram_webhook_secret,
            service=active_service,
            adapter=telegram_adapter,
            group_allowlist=configured.telegram_group_allowlist,
            max_body_bytes=configured.monitor_server_max_body_bytes,
        )
        register_telegram_router(
            app,
            create_telegram_router(
                active_service, bot_client, admin_ids=configured.telegram_admin_ids
            ),
            security=SecurityContract("verified_platform", telegram_security),
        )

    async def bounded_body(request: Request) -> bytes:
        limit = configured.monitor_server_max_body_bytes if configured else 1_048_576
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > limit:
                    raise HTTPException(413, "request body too large")
            except ValueError as exc:
                raise HTTPException(400, "invalid content-length") from exc
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > limit:
                raise HTTPException(413, "request body too large")
        return bytes(body)

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
        body = await bounded_body(request)
        instance_id = credentials.instance(request, body, svc.store)
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

    if configured.operational_report_enabled:
        from .operational_report import OperationalReportService, render_operational_report_html

        def report_service() -> OperationalReportService:
            svc = active_service()
            return OperationalReportService(
                svc.store,
                svc.identities,
                workstream_path=configured.workstream_store_path,
                connector_evidence_dir=configured.operational_report_connector_evidence_dir,
                degraded_seconds=configured.monitor_degraded_seconds,
                offline_seconds=configured.monitor_offline_seconds,
                max_items=configured.operational_report_max_items,
            )

        @app.get("/v1/report")
        async def operational_report(
            request: Request,
            response: Response,
            scope: Literal["personal", "team"] = "personal",
        ) -> dict[str, object]:
            _, principal, _ = human(request)
            try:
                response.headers["Cache-Control"] = "no-store"
                return report_service().build(principal, scope=scope).to_dict()
            except Exception as exc:
                raise translate(exc) from exc

        @app.get("/v1/report.html", response_class=HTMLResponse)
        async def operational_report_html(
            request: Request, scope: Literal["personal", "team"] = "personal"
        ) -> HTMLResponse:
            _, principal, _ = human(request)
            try:
                report = report_service().build(principal, scope=scope).to_dict()
                return HTMLResponse(
                    render_operational_report_html(report),
                    headers={
                        "Cache-Control": "no-store",
                        "Content-Security-Policy": (
                            "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
                            "frame-ancestors 'none'"
                        ),
                        "X-Content-Type-Options": "nosniff",
                    },
                )
            except Exception as exc:
                raise translate(exc) from exc

    @app.post("/v1/subscriptions")
    async def subscriptions(request: Request) -> dict[str, str]:
        svc, principal, destination = human(request)
        try:
            payload = await request.json()
            message = svc.watch(
                principal,
                destination,
                instance_id=payload.get("instance_id", ""),
                task_id=payload.get("task_id", ""),
            )
            return {"message": message}
        except Exception as exc:
            raise translate(exc) from exc

    @app.post("/v1/commands/poll")
    async def poll(request: Request) -> dict[str, list[dict[str, object]]]:
        svc, _, credentials = dependencies()
        body = await bounded_body(request)
        instance_id = credentials.instance(request, body, svc.store)
        if body not in (b"", b"{}"):
            raise HTTPException(422, "poll body must be empty")
        return {"commands": list(svc.poll_commands(instance_id))}

    @app.post("/v1/commands/{command_id}/ack")
    async def ack(request: Request, command_id: str) -> dict[str, object]:
        svc, _, credentials = dependencies()
        body = await bounded_body(request)
        instance_id = credentials.instance(request, body, svc.store)
        try:
            result = json.loads(body)["result"]
            receipt = svc.acknowledge_control(instance_id, command_id, result)
            return {"state": receipt.state}
        except Exception as exc:
            raise translate(exc) from exc

    @app.get("/healthz")
    async def health(response: Response) -> dict[str, str]:
        dependencies()
        owner = getattr(app.state, "brief_owner", None)
        response.headers["X-OStag-Brief-Status"] = owner.health if owner else "stopped"
        return {"status": "ok"}

    from ostag.copilot_relay.routes import create_copilot_router

    if configured.copilot_relay_server_enabled:
        app.include_router(
            create_copilot_router(
                dependencies=lambda: (dependencies()[0], dependencies()[2]),
                bounded_body=bounded_body,
                write_enabled=configured.copilot_relay_write_enabled,
                canary_session_ids=frozenset(configured.copilot_relay_canary_session_ids),
            )
        )

    for extension in routers:
        if not isinstance(extension, tuple) or len(extension) != 2:
            raise ValueError("router registration requires an explicit security contract")
        register_router(app, extension[0], security=extension[1])
    return app


def register_router(
    app: FastAPI,
    router: APIRouter,
    *,
    security: SecurityContract,
) -> None:
    if (
        not isinstance(security, SecurityContract)
        or security.kind not in {"verified_platform", "authenticated_dependency"}
        or not callable(security.dependency)
    ):
        raise ValueError("unsupported router security contract")
    app.include_router(router, dependencies=[Depends(security.dependency)])


def register_feishu_card_router(
    app: FastAPI,
    router: APIRouter,
    *,
    security: SecurityContract,
) -> None:
    """Register only the card-action callback behind verified-platform security."""
    if security.kind != "verified_platform":
        raise ValueError("Feishu card callbacks require verified_platform security")
    paths = _route_paths(router.routes)
    if paths != {"/webhooks/feishu/card-action"}:
        raise ValueError("Feishu router may expose only /webhooks/feishu/card-action")
    register_router(app, router, security=security)


def register_telegram_router(
    app: FastAPI,
    router: APIRouter,
    *,
    security: SecurityContract,
) -> None:
    """Register only the production Telegram webhook behind platform verification."""
    if security.kind != "verified_platform":
        raise ValueError("Telegram callbacks require verified_platform security")
    if _route_paths(router.routes) != {"/webhooks/telegram"}:
        raise ValueError("Telegram router may expose only /webhooks/telegram")
    register_router(app, router, security=security)


def _route_paths(routes: Iterable[object]) -> set[str]:
    """Flatten public paths without depending on FastAPI's router wrapper type."""
    paths: set[str] = set()
    for route in routes:
        path = getattr(route, "path", None)
        if isinstance(path, str):
            paths.add(path)
        nested = getattr(route, "routes", None)
        if nested is not None:
            paths.update(_route_paths(nested))
    return paths


app = create_app()
