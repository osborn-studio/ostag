"""Durable dual-platform scheduler for daily task board delivery."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ostag.connectors.requirements import RequirementTableConnector
from ostag.core.ambient import AMBIENT_PROJECTION_FIELDS, snapshots_from_records
from ostag.core.daily_digest import DailyDigestView
from ostag.core.daily_digest_renderers import (
    render_daily_digest_text,
    render_daily_dingtalk_payload,
    render_daily_feishu_card,
)
from ostag.adapters.feishu_cards import render_requirement_board_card
from ostag.core.requirement_board import (
    analyze_daily_requirement_board,
    format_daily_requirement_board_text,
)

from .store import MonitorStore


BEIJING = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class DailyDigestProfile:
    platform: str
    destination_id: str
    base_token: str
    table_id: str
    table_url: str
    manager_ids: tuple[str, ...]
    connector: RequirementTableConnector
    transport: Any
    enabled: bool = True
    record_limit: int = 50

    def __post_init__(self) -> None:
        if self.platform not in {"feishu", "dingtalk"}:
            raise ValueError("daily digest platform must be feishu or dingtalk")
        if not all((self.destination_id, self.base_token, self.table_id)):
            raise ValueError("daily digest profile identifiers are required")


@dataclass(frozen=True)
class DailyRequirementBoardProfile:
    destination_id: str
    base_token: str
    table_id: str
    table_url: str
    group_name: str
    connector: RequirementTableConnector
    transport: Any
    enabled: bool = True
    record_limit: int = 200
    platform: str = "feishu"

    def __post_init__(self) -> None:
        if not all((self.destination_id, self.base_token, self.table_id, self.table_url)):
            raise ValueError("daily requirement board profile identifiers are required")


class DailyTaskBoardScheduler:
    def __init__(
        self,
        store: MonitorStore,
        profiles: tuple[DailyDigestProfile, ...],
        *,
        clock: Callable[[], datetime] | None = None,
        morning_hour: int = 9,
        evening_hour: int = 18,
        poll_seconds: float = 30,
        retry_cap: int = 5,
        requirement_profiles: tuple[DailyRequirementBoardProfile, ...] = (),
        requirement_hour: int = 12,
    ) -> None:
        if not 0 <= morning_hour <= 23 or not 0 <= evening_hour <= 23:
            raise ValueError("daily digest hours must be in 0..23")
        if morning_hour == evening_hour:
            raise ValueError("daily digest hours must be different")
        if not 0 <= requirement_hour <= 23:
            raise ValueError("daily requirement board hour must be in 0..23")
        self.store = store
        self.profiles = profiles
        self.clock = clock or (lambda: datetime.now(UTC))
        self.morning_hour = morning_hour
        self.evening_hour = evening_hour
        self.poll_seconds = max(5.0, poll_seconds)
        self.retry_cap = max(1, retry_cap)
        self.requirement_profiles = requirement_profiles
        self.requirement_hour = requirement_hour

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass

    async def tick(self, *, drain: bool = True) -> None:
        now = self.clock()
        local = now.astimezone(BEIJING)
        start = datetime.combine(local.date(), datetime.min.time(), BEIJING)
        due = [
            (start.replace(hour=hour), kind)
            for hour, kind in (
                (self.morning_hour, "morning"),
                (self.evening_hour, "evening"),
            )
            if start.replace(hour=hour) <= local
        ][-2:]
        for window_at, kind in due:
            for profile in self.profiles:
                if not profile.enabled:
                    continue
                window_key = (
                    f"{window_at.date()}:{kind}:{profile.platform}:{profile.destination_id}"
                )
                with self.store.connect() as connection:
                    exists = connection.execute(
                        "SELECT 1 FROM daily_digest_windows WHERE window_key=?", (window_key,)
                    ).fetchone()
                if exists is not None:
                    continue
                try:
                    data = await profile.connector.list_records(
                        profile.base_token,
                        profile.table_id,
                        AMBIENT_PROJECTION_FIELDS,
                        limit=profile.record_limit,
                    )
                    view = DailyDigestView.build(
                        kind,  # type: ignore[arg-type]
                        snapshots_from_records(data),
                        today=local.date(),
                        generated_at=now,
                        source_updated_at=now,
                        table_url=profile.table_url,
                    )
                    payload = (
                        render_daily_feishu_card(view)
                        if profile.platform == "feishu"
                        else render_daily_dingtalk_payload(
                            view, manager_ids=profile.manager_ids
                        )
                    )
                    fallback = render_daily_digest_text(view)
                except Exception:
                    continue
                delivery_id = "daily-" + hashlib.sha256(window_key.encode()).hexdigest()[:32]
                with self.store.transaction() as connection:
                    inserted = connection.execute(
                        "INSERT OR IGNORE INTO daily_digest_windows(window_key,created_at) "
                        "VALUES (?,?)",
                        (window_key, now.isoformat()),
                    ).rowcount
                    if inserted:
                        connection.execute(
                            "INSERT INTO daily_digest_outbox(delivery_id,window_key,platform,"
                            "destination_id,payload,fallback_text,next_attempt_at) "
                            "VALUES (?,?,?,?,?,?,?)",
                            (
                                delivery_id,
                                window_key,
                                profile.platform,
                                profile.destination_id,
                                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                                fallback,
                                now.isoformat(),
                            ),
                        )
        requirement_at = start.replace(hour=self.requirement_hour)
        if requirement_at <= local:
            for profile in self.requirement_profiles:
                if not profile.enabled:
                    continue
                window_key = (
                    f"{requirement_at.date()}:requirement_noon:feishu:{profile.destination_id}"
                )
                with self.store.connect() as connection:
                    exists = connection.execute(
                        "SELECT 1 FROM daily_digest_windows WHERE window_key=?", (window_key,)
                    ).fetchone()
                if exists is not None:
                    continue
                try:
                    data = await profile.connector.list_records(
                        profile.base_token,
                        profile.table_id,
                        AMBIENT_PROJECTION_FIELDS,
                        limit=profile.record_limit,
                    )
                    board = analyze_daily_requirement_board(
                        snapshots_from_records(data), today=local.date()
                    )
                    payload = render_requirement_board_card(
                        board, group_name=profile.group_name, base_url=profile.table_url
                    )
                    fallback = format_daily_requirement_board_text(board)
                except Exception:
                    continue
                delivery_id = "daily-" + hashlib.sha256(window_key.encode()).hexdigest()[:32]
                with self.store.transaction() as connection:
                    inserted = connection.execute(
                        "INSERT OR IGNORE INTO daily_digest_windows(window_key,created_at) "
                        "VALUES (?,?)",
                        (window_key, now.isoformat()),
                    ).rowcount
                    if inserted:
                        connection.execute(
                            "INSERT INTO daily_digest_outbox(delivery_id,window_key,platform,"
                            "destination_id,payload,fallback_text,next_attempt_at) "
                            "VALUES (?,?,?,?,?,?,?)",
                            (
                                delivery_id,
                                window_key,
                                "feishu",
                                profile.destination_id,
                                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                                fallback,
                                now.isoformat(),
                            ),
                        )
        if drain:
            while await self.drain_once():
                pass

    async def drain_once(self) -> bool:
        now = self.clock()
        with self.store.connect() as connection:
            pending = connection.execute(
                "SELECT delivery_id,platform,destination_id FROM daily_digest_outbox "
                "WHERE state='pending' ORDER BY rowid"
            ).fetchall()
        active_targets = {
            (profile.platform, profile.destination_id)
            for profile in self.profiles
            if profile.enabled
        } | {
            (profile.platform, profile.destination_id)
            for profile in self.requirement_profiles
            if profile.enabled
        }
        removed = next(
            (
                row
                for row in pending
                if (row["platform"], row["destination_id"]) not in active_targets
            ),
            None,
        )
        if removed is not None:
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE daily_digest_outbox SET state='suppressed',last_error=? "
                    "WHERE delivery_id=? AND state='pending'",
                    ("profile disabled or removed", removed["delivery_id"]),
                )
            return True
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM daily_digest_outbox WHERE state='pending' "
                "AND next_attempt_at<=? ORDER BY rowid LIMIT 1",
                (now.isoformat(),),
            ).fetchone()
        if row is None:
            return False
        item = dict(row)
        profile = next(
            (
                candidate
                for candidate in (*self.profiles, *self.requirement_profiles)
                if candidate.enabled
                and candidate.platform == item["platform"]
                and candidate.destination_id == item["destination_id"]
            ),
            None,
        )
        if profile is None:
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE daily_digest_outbox SET state='suppressed',last_error=? "
                    "WHERE delivery_id=? AND state='pending'",
                    ("profile disabled or removed", item["delivery_id"]),
                )
            return True
        payload = json.loads(item["payload"])
        try:
            if item["platform"] == "feishu":
                await profile.transport.send_card(
                    item["destination_id"],
                    payload,
                    item["fallback_text"],
                    chat_type="group",
                    delivery_id=item["delivery_id"],
                )
            else:
                await profile.transport.send_card(
                    item["destination_id"],
                    payload,
                    item["fallback_text"],
                    delivery_id=item["delivery_id"],
                )
        except Exception as exc:
            attempts = int(item["attempts"]) + 1
            terminal = attempts >= self.retry_cap
            delay_minutes = (1, 2, 4, 8)[min(attempts - 1, 3)]
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE daily_digest_outbox SET attempts=?,state=?,next_attempt_at=?,"
                    "last_error=? WHERE delivery_id=? AND state='pending'",
                    (
                        attempts,
                        "dead_letter" if terminal else "pending",
                        (now + timedelta(minutes=delay_minutes)).isoformat(),
                        f"{type(exc).__name__}: {str(exc)[:500]}",
                        item["delivery_id"],
                    ),
                )
            return True
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE daily_digest_outbox SET state='delivered',delivered_at=?,last_error='' "
                "WHERE delivery_id=? AND state='pending'",
                (now.isoformat(), item["delivery_id"]),
            )
            connection.execute(
                "UPDATE daily_digest_windows SET delivered_at=? WHERE window_key=?",
                (now.isoformat(), item["window_key"]),
            )
        return True
