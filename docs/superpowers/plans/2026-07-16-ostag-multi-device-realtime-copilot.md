# OStag Multi-Device Realtime Copilot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and productionize one identity-scoped OStag control plane that shows MacBook Pro/Codex and Mac mini/Claude Code as distinct realtime devices and routes every Feishu reply to the exact device, runtime, session, turn, and version.

**Architecture:** Extend the existing outbound Copilot Relay rather than creating a second transport. Each Mac runs one Device Gateway backed by the current durable SQLite spool and osstack session RPC; the central service adds device pairing, runtime-aware v2 envelopes, heartbeats, device-first projections, exact command routing, and Feishu push cards while continuing to accept the current v1 MacBook worker during migration.

**Tech Stack:** Python 3.11, FastAPI, SQLite, PyCryptodome Ed25519, pytest, Ruff, TypeScript, better-sqlite3, Vitest, osstack daemon/session RPC, Feishu interactive cards, launchd, Docker Compose, osstack Loop Engineering.

**Approved design:** `docs/superpowers/specs/2026-07-16-ostag-multi-device-realtime-copilot-design.md`

**Managed delivery card:** `card_0ef7b775` in the local osstack kanban.

**Wave cards:**

- Wave A identity/transport: `card_4fc13668`
- Wave B Feishu UX/routing: `card_6c4ac308`
- Wave C dual-device release: `card_0edc87f4`

For backward-compatible v2 envelopes, `SessionBinding.instance_id` carries the
stable `device_id`; the new `runtime_id` distinguishes the Agent runtime on that
device.

---

## Worktree and delivery rules

- OStag implementation workspace: `/Users/gilbertchen/Documents/CODE/agentsteam/ostag-copilot-relay` on `codex/copilot-relay`.
- osstack session implementation workspace: `/Users/gilbertchen/Documents/CODE/agentsteam/osstack-copilot-session` on `codex/copilot-session`.
- Preserve unrelated modified generated files in `osstack-copilot-session/packages/knowledge-cloud/dist/`; do not stage or rewrite them.
- Every task starts with a failing test, ends with focused tests plus lint, and receives its own commit.
- At each wave gate, enqueue one osstack Loop Engineering verification task and require score `1.00` before proceeding.
- Production secrets remain in the protected environment/Keychain. Test fixtures use fixed fake keys only.

## File responsibility map

### OStag repository

- `ostag/copilot_relay/models.py`: backward-compatible v1/v2 wire contracts.
- `ostag/copilot_relay/device_identity.py`: local stable device identity and macOS Keychain private key access.
- `ostag/copilot_relay/reporter.py`: durable event, heartbeat, command, and receipt transport.
- `ostag/copilot_relay/osstack_port.py`: strict runtime-aware osstack session adapter.
- `ostag/copilot_relay/worker.py`: Device Gateway lifecycle and cadence.
- `ostag/copilot_relay/routes.py`: central device/event/command HTTP endpoints.
- `ostag/copilot_relay/store.py`: runtime-aware session and command persistence.
- `ostag/copilot_relay/status_board.py`: device-first authorized projection.
- `ostag/copilot_relay/feishu_cards.py`: overview, device detail, decision, recovery, and terminal push cards.
- `ostag/copilot_relay/feishu_routes.py`: exact target selection and callback handling.
- `ostag/copilot_relay/scheduler.py`: deduplicated critical-event proactive delivery.
- `ostag/work_monitor/device_registry.py`: pairing codes, public keys, device revocation, and display metadata.
- `ostag/work_monitor/store.py`: additive central schema migrations.
- `ostag/work_monitor/server.py`: authentication and route composition.
- `ostag/config.py`, `ostag/main.py`: device/pairing CLI and configuration.
- `deploy/copilot-relay/*`: launchd installer and safe migration.

### osstack session repository

- `packages/core/src/state/sessions.ts`: runtime identity in session list/snapshot models.
- `packages/cli/src/daemon.ts`: public governed session RPC response.
- `packages/cli/src/session-rpc.test.ts`: Codex/Claude runtime isolation and stale routing tests.
- `packages/cli/src/bin.ts`: stable JSON CLI output consumed by OStag.

---

## Wave A — Identity, protocol, and realtime transport

### Task 1: Add runtime-aware v2 relay contracts without breaking v1

**Files:**
- Modify: `ostag/copilot_relay/models.py`
- Modify: `tests/test_copilot_relay_models.py`
- Modify: `tests/test_copilot_relay_integration.py`

- [ ] **Step 1: Write failing v2 contract tests**

Add fixtures that assert a v2 `SessionBinding` contains exactly:

```python
V2_BINDING = {
    "instance_id": "device_macbook",
    "runtime_id": "codex-cli",
    "task_id": "task_board",
    "run_id": "run_board",
    "session_id": "session_board",
    "turn_id": "turn_7",
    "session_version": 7,
}
```

Assert v1 envelopes still accept the old six binding fields and expose `runtime_id == "legacy"` internally. Assert v2 rejects missing, empty, boolean, oversized, and unexpected `runtime_id` values. Assert `to_dict()` emits the exact key set for its schema version.

- [ ] **Step 2: Run the contract tests and verify RED**

Run:

```bash
python -m pytest tests/test_copilot_relay_models.py tests/test_copilot_relay_integration.py -q
```

Expected: FAIL because `runtime_id` and schema version 2 are unsupported.

- [ ] **Step 3: Implement schema-version-aware bindings**

Change the public shape to:

```python
@dataclass(frozen=True)
class SessionBinding:
    instance_id: str
    task_id: str
    run_id: str
    session_id: str
    turn_id: str
    session_version: int
    runtime_id: str = "legacy"

    @classmethod
    def from_dict(cls, payload: object, *, schema_version: int) -> "SessionBinding":
        keys = cls._V2_KEYS if schema_version == 2 else cls._V1_KEYS
        _validate_keys(payload, required=keys, model_name=cls.__name__)
        assert isinstance(payload, Mapping)
        values = {key: payload[key] for key in keys}
        if schema_version == 1:
            values["runtime_id"] = "legacy"
        return cls(**values)

    def to_dict(self, *, schema_version: int) -> dict[str, str | int]:
        keys = self._V2_KEYS if schema_version == 2 else self._V1_KEYS
        return {key: getattr(self, key) for key in keys}
```

Update `RelayEnvelope.from_dict()` and `to_dict()` to pass `schema_version`; allow only versions 1 and 2. Keep all payload limits and timestamp checks unchanged.

- [ ] **Step 4: Run focused tests and lint**

Run:

```bash
python -m pytest tests/test_copilot_relay_models.py tests/test_copilot_relay_integration.py -q
ruff check ostag/copilot_relay/models.py tests/test_copilot_relay_models.py tests/test_copilot_relay_integration.py
```

Expected: PASS and `All checks passed!`.

- [ ] **Step 5: Commit**

```bash
git add ostag/copilot_relay/models.py tests/test_copilot_relay_models.py tests/test_copilot_relay_integration.py
git commit -m "feat(copilot): add runtime-aware relay v2 contracts"
```

### Task 2: Expose a stable runtime identity from osstack session RPC

**Files:**
- Modify: `/Users/gilbertchen/Documents/CODE/agentsteam/osstack-copilot-session/packages/core/src/state/sessions.ts`
- Modify: `/Users/gilbertchen/Documents/CODE/agentsteam/osstack-copilot-session/packages/cli/src/daemon.ts`
- Modify: `/Users/gilbertchen/Documents/CODE/agentsteam/osstack-copilot-session/packages/cli/src/session-rpc.test.ts`
- Modify: `/Users/gilbertchen/Documents/CODE/agentsteam/osstack-copilot-session/packages/cli/src/bin.ts`

- [ ] **Step 1: Write failing runtime isolation tests**

Extend `FakeSessionAdapter` tests to seed one `codex-cli` session and one `claude-code` session. Assert `session.list`, `session.snapshot`, and `session.start` each return a non-empty `runtime_id`, and that the value equals the registered adapter ID for the first release:

```ts
expect(listed.result).toMatchObject({
  sessions: expect.arrayContaining([
    expect.objectContaining({ agent_id: "codex-cli", runtime_id: "codex-cli" }),
    expect.objectContaining({ agent_id: "claude-code", runtime_id: "claude-code" }),
  ]),
});
```

Add a `session.send` request with the wrong `runtime_id` and expect `runtime_mismatch` without invoking either adapter.

- [ ] **Step 2: Run the osstack tests and verify RED**

Run from `/Users/gilbertchen/Documents/CODE/agentsteam/osstack-copilot-session`:

```bash
pnpm --filter @osstack/cli test -- session-rpc.test.ts
```

Expected: FAIL because governed session RPC does not expose or validate `runtime_id`.

- [ ] **Step 3: Add runtime identity to the public contract**

Add `runtime_id: string` to `SessionSnapshot`, derive it from `session.agent_id`, and include it in `publicSession()` and snapshot responses. Extend `session.send` params with `runtime_id` and reject when it differs from the stored session's `agent_id`:

```ts
if (params.runtime_id !== session.agent_id) {
  return singleErr(req.id, "runtime_mismatch", "session belongs to another runtime");
}
```

Update CLI JSON types and argument forwarding without exposing `native_session_id`.

- [ ] **Step 4: Run focused osstack verification**

```bash
pnpm --filter @osstack/cli test -- session-rpc.test.ts
pnpm --filter @osstack/cli typecheck
```

Expected: PASS with no TypeScript errors.

- [ ] **Step 5: Commit only source and test files**

```bash
git add packages/core/src/state/sessions.ts packages/cli/src/daemon.ts packages/cli/src/session-rpc.test.ts packages/cli/src/bin.ts
git commit -m "feat(session): expose stable agent runtime identity"
```

Do not stage existing `packages/knowledge-cloud/dist/*` modifications.

### Task 3: Add local device identity and macOS Keychain signing

**Files:**
- Create: `ostag/copilot_relay/device_identity.py`
- Create: `tests/test_copilot_relay_device_identity.py`
- Modify: `ostag/config.py`

- [ ] **Step 1: Write failing identity and key tests**

Test these exact behaviors with an injected subprocess runner:

```python
identity = store.load_or_create(display_name="MacBook Pro")
assert identity.device_id.startswith("device_")
assert identity.display_name == "MacBook Pro"
assert identity.public_key.startswith("-----BEGIN PUBLIC KEY-----")
assert store.load_or_create(display_name="Renamed").device_id == identity.device_id
assert identity.sign(b"canonical-request") != identity.sign(b"other-request")
```

Also assert the metadata file is mode `0600`, contains no private key, rejects symlinks, and fails closed when Keychain returns an unexpected account.

- [ ] **Step 2: Run and verify RED**

```bash
python -m pytest tests/test_copilot_relay_device_identity.py -q
```

Expected: FAIL importing `DeviceIdentityStore`.

- [ ] **Step 3: Implement the focused identity module**

Use `Crypto.PublicKey.ECC.generate(curve="Ed25519")` and `Crypto.Signature.eddsa`. Persist only this bounded JSON:

```json
{"device_id":"device_<uuid-hex>","display_name":"MacBook Pro","public_key":"<PEM>"}
```

Store the private PEM under Keychain service `ai.ostag.device-gateway` and account `<device_id>` via `/usr/bin/security`. Provide `canonical_sign(body: bytes) -> str` returning URL-safe base64. Never print key material.

Add settings for `OSTAG_DEVICE_IDENTITY_FILE` and `OSTAG_DEVICE_DISPLAY_NAME`; default the file under `~/Library/Application Support/OStag/device-identity.json`.

- [ ] **Step 4: Verify module and configuration**

```bash
python -m pytest tests/test_copilot_relay_device_identity.py tests/test_config.py -q
ruff check ostag/copilot_relay/device_identity.py ostag/config.py tests/test_copilot_relay_device_identity.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ostag/copilot_relay/device_identity.py ostag/config.py tests/test_copilot_relay_device_identity.py tests/test_config.py
git commit -m "feat(gateway): persist a signed device identity"
```

### Task 4: Add one-time device pairing and dynamic device authentication

**Files:**
- Create: `ostag/work_monitor/device_registry.py`
- Create: `tests/test_work_monitor_device_registry.py`
- Modify: `ostag/work_monitor/store.py`
- Modify: `ostag/work_monitor/server.py`
- Modify: `ostag/copilot_relay/routes.py`
- Modify: `ostag/main.py`
- Modify: `tests/test_copilot_relay_routes.py`
- Modify: `tests/test_cli_run.py`

- [ ] **Step 1: Write failing registry and pairing tests**

Cover pairing-code creation by an authenticated private owner, one successful public-key exchange, replay rejection, expiry, revocation, display-name bounds, signature authentication, and public-key rotation signed by the old private key. Use fixed Ed25519 test keys and assert no private key is written centrally.

The accepted exchange request is exactly:

```json
{
  "pairing_code": "ABCD-EFGH-IJKL",
  "device_id": "device_macbook",
  "display_name": "MacBook Pro",
  "public_key": "-----BEGIN PUBLIC KEY-----\n<fixture>\n-----END PUBLIC KEY-----"
}
```

The response contains `{"paired":true,"device_id":"device_macbook"}` and no credential secret.

- [ ] **Step 2: Run and verify RED**

```bash
python -m pytest tests/test_work_monitor_device_registry.py tests/test_copilot_relay_routes.py tests/test_cli_run.py -q
```

Expected: FAIL because pairing storage and routes do not exist.

- [ ] **Step 3: Add additive migrations and registry API**

Create:

```sql
CREATE TABLE monitor_devices(
  device_id TEXT PRIMARY KEY,
  principal_platform TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  display_name TEXT NOT NULL,
  public_key TEXT NOT NULL,
  gateway_version TEXT NOT NULL DEFAULT '',
  capabilities_json TEXT NOT NULL DEFAULT '[]',
  paired_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL DEFAULT '',
  revoked_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE monitor_device_pairing_codes(
  code_digest TEXT PRIMARY KEY,
  principal_platform TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  consumed_at INTEGER
);
```

Store SHA-256 digests of pairing codes. Generate 96 bits of randomness, display it as three uppercase groups, expire it after 10 minutes, and consume it atomically.

- [ ] **Step 4: Add v2 signature verification and CLI**

Verify `X-OStag-Device`, timestamp, nonce, body digest, and Ed25519 signature against the registered public key. Retain current HMAC authentication only for configured v1 migration instances.

Add:

```text
ostag copilot device pairing-code --json
ostag copilot device pair --code <code> --display-name <name> --central-url <https-url>
ostag copilot device list --json
ostag copilot device rotate-key --device <device_id>
ostag copilot device revoke --device <device_id>
```

Never accept principal or owner IDs from the device exchange body. Rotation
generates a new Keychain key, sends the new public key in a request signed by the
currently registered old key, updates the registry atomically, and removes the
old Keychain item only after the server confirms the new key.

- [ ] **Step 5: Run focused tests and lint**

```bash
python -m pytest tests/test_work_monitor_device_registry.py tests/test_copilot_relay_routes.py tests/test_cli_run.py tests/test_work_monitor_server.py -q
ruff check ostag/work_monitor/device_registry.py ostag/work_monitor/store.py ostag/work_monitor/server.py ostag/copilot_relay/routes.py ostag/main.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add ostag/work_monitor/device_registry.py ostag/work_monitor/store.py ostag/work_monitor/server.py ostag/copilot_relay/routes.py ostag/main.py tests/test_work_monitor_device_registry.py tests/test_copilot_relay_routes.py tests/test_cli_run.py tests/test_work_monitor_server.py
git commit -m "feat(copilot): pair and authenticate independent devices"
```

### Task 5: Add device heartbeats, runtime inventory, and connection states

**Files:**
- Modify: `ostag/copilot_relay/reporter.py`
- Modify: `ostag/copilot_relay/worker.py`
- Modify: `ostag/copilot_relay/osstack_port.py`
- Modify: `ostag/copilot_relay/routes.py`
- Modify: `ostag/work_monitor/device_registry.py`
- Modify: `tests/test_copilot_relay_reporter.py`
- Modify: `tests/test_copilot_relay_worker.py`
- Modify: `tests/test_copilot_relay_osstack_port.py`
- Modify: `tests/test_copilot_relay_routes.py`

- [ ] **Step 1: Write failing 5-second heartbeat tests**

Use an injected clock and assert the reporter posts this exact bounded payload no more than once per five seconds:

```json
{
  "device_id": "device_macbook",
  "gateway_version": "0.2.0",
  "runtimes": [
    {"runtime_id":"codex-cli","agent_type":"codex","state":"online","session_count":1}
  ]
}
```

Assert the server ignores device-supplied owner identity, updates `last_seen_at` from server time, reports `online` under 30 seconds, `degraded` from 30 through 119 seconds, and `offline` at 120 seconds.

- [ ] **Step 2: Run and verify RED**

```bash
python -m pytest tests/test_copilot_relay_reporter.py tests/test_copilot_relay_worker.py tests/test_copilot_relay_osstack_port.py tests/test_copilot_relay_routes.py -q
```

Expected: FAIL because heartbeat and runtime inventory are absent.

- [ ] **Step 3: Implement heartbeat transport**

Add `OsstackSessionPort.runtime_inventory()` from the strict session list. Map agent IDs with this allowlist:

```python
AGENT_TYPES = {
    "codex-cli": "codex",
    "claude-code": "claude-code",
    "claude-cli": "claude-code",
}
```

Unknown IDs remain visible as `other` but receive no write capability until configured. Add `/v1/copilot/device/heartbeat`; authenticate it with the registered device public key and update server-owned timestamps.

- [ ] **Step 4: Make worker cadence independent**

Track heartbeat due time separately from session discovery and command polling so a slow snapshot cannot suppress health updates. One failed operation must not skip the other operations in the same cycle.

- [ ] **Step 5: Verify focused behavior**

```bash
python -m pytest tests/test_copilot_relay_reporter.py tests/test_copilot_relay_worker.py tests/test_copilot_relay_osstack_port.py tests/test_copilot_relay_routes.py -q
ruff check ostag/copilot_relay ostag/work_monitor/device_registry.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add ostag/copilot_relay/reporter.py ostag/copilot_relay/worker.py ostag/copilot_relay/osstack_port.py ostag/copilot_relay/routes.py ostag/work_monitor/device_registry.py tests/test_copilot_relay_reporter.py tests/test_copilot_relay_worker.py tests/test_copilot_relay_osstack_port.py tests/test_copilot_relay_routes.py
git commit -m "feat(gateway): report realtime device and runtime health"
```

### Task 6: Persist runtime-aware sessions and exact command targets

**Files:**
- Modify: `ostag/work_monitor/store.py`
- Modify: `ostag/copilot_relay/store.py`
- Modify: `ostag/copilot_relay/reporter.py`
- Modify: `ostag/copilot_relay/service.py`
- Modify: `tests/test_copilot_relay_store.py`
- Modify: `tests/test_copilot_relay_service.py`
- Modify: `tests/test_copilot_relay_reporter.py`

- [ ] **Step 1: Write failing migration and routing tests**

Seed a v1 database and prove migration preserves its session as runtime `legacy`. Then insert two sessions with the same `session_id` on different devices/runtimes and prove both survive. Assert a command targeting `device_macmini/claude-code/session_shared` cannot be leased or applied by `device_macbook/codex-cli`.

- [ ] **Step 2: Run and verify RED**

```bash
python -m pytest tests/test_copilot_relay_store.py tests/test_copilot_relay_service.py tests/test_copilot_relay_reporter.py -q
```

Expected: FAIL because current primary keys omit `runtime_id`.

- [ ] **Step 3: Add a transactional table-rebuild migration**

Rebuild `relay_sessions`, `relay_decisions`, and `relay_current_tasks` with `runtime_id TEXT NOT NULL`. Use `(instance_id,runtime_id,session_id)` for session uniqueness and copy existing rows with runtime `legacy`. Add indexes for device projection and outbound command leasing.

- [ ] **Step 4: Enforce exact runtime through store and reporter**

Every v2 command and receipt must preserve the same `SessionBinding`. Change local inbound selection to:

```python
session = sessions.get((message.binding.runtime_id, message.binding.session_id))
if session is None or session.version != message.binding.session_version:
    self._finish_inbound(message, "stale", "exact runtime session is unavailable")
    continue
```

Extend receipt states to include `stale` and keep command IDs idempotent.

- [ ] **Step 5: Verify migration and routing**

```bash
python -m pytest tests/test_copilot_relay_store.py tests/test_copilot_relay_service.py tests/test_copilot_relay_reporter.py tests/test_copilot_relay_routes.py -q
ruff check ostag/copilot_relay ostag/work_monitor/store.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add ostag/work_monitor/store.py ostag/copilot_relay/store.py ostag/copilot_relay/reporter.py ostag/copilot_relay/service.py tests/test_copilot_relay_store.py tests/test_copilot_relay_service.py tests/test_copilot_relay_reporter.py tests/test_copilot_relay_routes.py
git commit -m "feat(copilot): isolate commands by device runtime and session"
```

### Wave A gate

- [ ] Create child kanban cards and mark completed tasks:

```bash
osstack kanban comment card_0ef7b775 "Wave A complete: device identity, pairing, heartbeat, runtime-aware sessions and exact routing"
osstack kanban move card_0ef7b775 running "Wave A verification"
```

- [ ] Enqueue and run the gate:

```bash
/Users/gilbertchen/Documents/CODE/agentsteam/osstack/loop-engineering/scripts/osstack-loop.sh enqueue "Verify OStag multi-device Wave A contracts and transport" --capability code.test --priority 100
LOOP_EXECUTE=true LOOP_WORKTREE=0 LOOP_ARCHIVE_COMPLETED=1 LOOP_VERIFY='python -m pytest -q && ruff check .' /Users/gilbertchen/Documents/CODE/agentsteam/osstack/loop-engineering/scripts/loop-run.sh --cycles 1 --stop-when-idle
```

Expected: osstack reports success rate `100%` and verifier score `1.00`.

---

## Wave B — Device-first Feishu experience and critical pushes

### Task 7: Build a device-first authorized projection

**Files:**
- Modify: `ostag/copilot_relay/status_board.py`
- Modify: `tests/test_copilot_status_board.py`

- [ ] **Step 1: Write failing projection tests**

Create fixtures for MacBook Pro/Codex online, Mac mini/Claude Code awaiting decision, and an offline bound device with no session. Assert all three devices remain visible, status thresholds use server heartbeat, sessions nest under the correct device/runtime, and personal/team authorization cannot be expanded by input IDs.

The new top-level shape is:

```python
from collections.abc import Sequence

@dataclass(frozen=True)
class DeviceStatusItem:
    device_id: str
    display_name: str
    connection_state: str
    last_seen_at: str
    runtimes: Sequence["RuntimeStatusItem"]
    sessions: Sequence["StatusSessionItem"]
    attention_count: int
```

- [ ] **Step 2: Run and verify RED**

```bash
python -m pytest tests/test_copilot_status_board.py -q
```

Expected: FAIL because the current view is session-first and drops empty devices.

- [ ] **Step 3: Implement device grouping and ranking**

Build authorized device rows first, then attach runtime and session rows. Sort by awaiting decision, failure/degraded, running, idle, offline. Keep Beijing timestamps and bound all lists before rendering.

- [ ] **Step 4: Verify projection**

```bash
python -m pytest tests/test_copilot_status_board.py -q
ruff check ostag/copilot_relay/status_board.py tests/test_copilot_status_board.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ostag/copilot_relay/status_board.py tests/test_copilot_status_board.py
git commit -m "feat(copilot): project device-first realtime status"
```

### Task 8: Render overview, device detail, decision, and recovery cards

**Files:**
- Modify: `ostag/copilot_relay/feishu_cards.py`
- Modify: `tests/test_copilot_relay_feishu_cards.py`

- [ ] **Step 1: Write failing card-shape tests**

Assert the overview card includes two persistent device sections, clear status color/label, Agent, current session, last update, and one context-sensitive action. Assert a decision card includes this exact safe target text:

```text
本次回复将发送到：Mac mini · Claude Code · 数据同步部署
```

Assert the expired-action recovery card contains a fresh opaque callback labeled `获取最新看板`, not only a toast. Ensure no native session IDs, paths, secrets, raw transcripts, or source snippets appear.

- [ ] **Step 2: Run and verify RED**

```bash
python -m pytest tests/test_copilot_relay_feishu_cards.py -q
```

Expected: FAIL because device detail and recovery cards do not exist.

- [ ] **Step 3: Implement bounded renderers**

Add `render_device_overview_card`, `render_device_detail_card`,
`render_targeted_decision_card`, and `render_expired_status_recovery_card`. The
recovery renderer has this complete behavior:

```python
def render_expired_status_recovery_card(*, callback_factory):
    callback_id = callback_factory("status_recover", "personal")
    elements = [
        {
            "tag": "markdown",
            "content": "原看板操作已过期。获取最新状态后再继续操作。",
        },
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "type": "primary",
                    "text": {"tag": "plain_text", "content": "获取最新看板"},
                    "value": {"callback_id": callback_id},
                }
            ],
        },
    ]
    return _card("OStag · 操作已过期", elements)
```

Build the other three renderers from explicit bounded Feishu elements using
`_safe`, `_card`, `_action`, and Beijing time helpers. Keep cards under the
repository's existing byte and element limits.

- [ ] **Step 4: Verify renderer safety and snapshots**

```bash
python -m pytest tests/test_copilot_relay_feishu_cards.py tests/test_feishu_card_security.py -q
ruff check ostag/copilot_relay/feishu_cards.py tests/test_copilot_relay_feishu_cards.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ostag/copilot_relay/feishu_cards.py tests/test_copilot_relay_feishu_cards.py
git commit -m "feat(feishu): render device-first copilot cards"
```

### Task 9: Implement target selection and safe callback recovery

**Files:**
- Modify: `ostag/copilot_relay/feishu_routes.py`
- Modify: `ostag/work_monitor/feishu_routes.py`
- Modify: `tests/test_copilot_relay_feishu_routes.py`
- Modify: `tests/test_feishu_card_security.py`

- [ ] **Step 1: Write failing flow tests**

Cover overview → device detail → continue session → decision answer. Prove direct text with one target routes automatically, multiple candidates returns a picker, and an expired status callback returns a new recovery card. Prove all callbacks remain principal/destination-bound and 24-hour status callbacks are one-use.

- [ ] **Step 2: Run and verify RED**

```bash
python -m pytest tests/test_copilot_relay_feishu_routes.py tests/test_feishu_card_security.py -q
```

Expected: FAIL because device callbacks and recovery behavior are missing.

- [ ] **Step 3: Add explicit callback actions**

Add actions `device_detail`, `device_continue`, `device_sessions`, `decision_answer`, and `status_recover`. Store exact target references server-side; callback payload remains `{"callback_id":"cr_<opaque>"}`. Before any write, reload device ownership, runtime/session existence, current turn, and session version.

- [ ] **Step 4: Return recoverable expiry behavior**

When a consumed/expired callback belongs to a status action, return a short warning toast plus `render_expired_status_recovery_card()`. Do not revive the old target or infer a new write target.

- [ ] **Step 5: Verify flow and security**

```bash
python -m pytest tests/test_copilot_relay_feishu_routes.py tests/test_feishu_card_security.py tests/test_copilot_relay_service.py -q
ruff check ostag/copilot_relay/feishu_routes.py ostag/work_monitor/feishu_routes.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add ostag/copilot_relay/feishu_routes.py ostag/work_monitor/feishu_routes.py tests/test_copilot_relay_feishu_routes.py tests/test_feishu_card_security.py
git commit -m "feat(feishu): route replies to exact device sessions"
```

### Task 10: Push only critical lifecycle events with durable deduplication

**Files:**
- Modify: `ostag/copilot_relay/scheduler.py`
- Modify: `ostag/copilot_relay/store.py`
- Modify: `ostag/work_monitor/store.py`
- Modify: `tests/test_copilot_relay_scheduler.py`

- [ ] **Step 1: Write failing push-policy tests**

Assert `decision.request`, failed terminal, and completed terminal events produce one delivery; ordinary running/testing/deploying snapshots produce none. Assert retry after transport failure, no duplicate after successful delivery, and one reminder when an offline device reconnects with an unresolved decision.

- [ ] **Step 2: Run and verify RED**

```bash
python -m pytest tests/test_copilot_relay_scheduler.py -q
```

Expected: FAIL because the scheduler does not implement this device lifecycle policy.

- [ ] **Step 3: Add durable critical-event deliveries**

Persist a unique key `(principal_platform,actor_id,event_id,delivery_kind)` with state, attempts, next attempt, delivered time, and last error. Resolve the owner's current private destination from server-owned bindings. Render targeted decision or terminal cards and use stable Feishu delivery IDs.

- [ ] **Step 4: Verify scheduling and lint**

```bash
python -m pytest tests/test_copilot_relay_scheduler.py tests/test_work_monitor_server.py -q
ruff check ostag/copilot_relay/scheduler.py ostag/copilot_relay/store.py ostag/work_monitor/store.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ostag/copilot_relay/scheduler.py ostag/copilot_relay/store.py ostag/work_monitor/store.py tests/test_copilot_relay_scheduler.py tests/test_work_monitor_server.py
git commit -m "feat(copilot): push critical device session events"
```

### Wave B gate

- [ ] Record and verify the wave:

```bash
osstack kanban comment card_0ef7b775 "Wave B complete: device-first Feishu overview, detail, exact decisions and critical pushes"
/Users/gilbertchen/Documents/CODE/agentsteam/osstack/loop-engineering/scripts/osstack-loop.sh enqueue "Verify OStag multi-device Wave B Feishu UX and security" --capability code.test --priority 100
LOOP_EXECUTE=true LOOP_WORKTREE=0 LOOP_ARCHIVE_COMPLETED=1 LOOP_VERIFY='python -m pytest -q && ruff check .' /Users/gilbertchen/Documents/CODE/agentsteam/osstack/loop-engineering/scripts/loop-run.sh --cycles 1 --stop-when-idle
```

Expected: success rate `100%`, verifier score `1.00`, and no unresolved issues in `STATE.md`.

---

## Wave C — Installation, dual-device acceptance, and production rollout

### Task 11: Build an idempotent Device Gateway installer and legacy migration

**Files:**
- Modify: `deploy/copilot-relay/ai.ostag.copilot-relay.plist`
- Modify: `deploy/copilot-relay/install-launchd.sh`
- Modify: `deploy/copilot-relay/run-worker.sh`
- Create: `deploy/copilot-relay/migrate-legacy-instance.sh`
- Modify: `tests/test_copilot_relay_deploy.py`

- [ ] **Step 1: Write failing deploy tests**

Assert dry-run output never prints secrets, install refuses relative paths and weak file permissions, repeated install produces one LaunchAgent, and migration refuses to assign `ostag-local` to two devices. Assert the plist carries only non-secret file paths; the private key stays in Keychain.

- [ ] **Step 2: Run and verify RED**

```bash
python -m pytest tests/test_copilot_relay_deploy.py -q
```

Expected: FAIL because device pairing and legacy migration are not installed.

- [ ] **Step 3: Implement installer and migration command**

Installer sequence:

```text
validate repo and virtualenv
load/create Keychain device identity
exchange one-time pairing code when supplied
write mode-600 non-secret config
render launchd plist
bootout old worker
bootstrap and kickstart new worker
wait for local health/status
print device_id, display name, runtime inventory, and central reachability only
```

Migration marks `ostag-local` read-only after the new MacBook device has reported one session successfully. It never deletes old audit data.

- [ ] **Step 4: Verify installer**

```bash
python -m pytest tests/test_copilot_relay_deploy.py -q
bash -n deploy/copilot-relay/install-launchd.sh deploy/copilot-relay/run-worker.sh deploy/copilot-relay/migrate-legacy-instance.sh
ruff check tests/test_copilot_relay_deploy.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add deploy/copilot-relay tests/test_copilot_relay_deploy.py
git commit -m "feat(deploy): install paired device gateways safely"
```

### Task 12: Add deterministic dual-device end-to-end acceptance

**Files:**
- Create: `tests/test_multi_device_copilot_e2e.py`
- Create: `scripts/multi_device_copilot_acceptance.py`
- Modify: `scripts/copilot_relay_smoke.sh`

- [ ] **Step 1: Write the failing in-process E2E test**

Start one central app and two reporters with separate spools and fixed device keys. Seed MacBook/Codex and Mac mini/Claude Code sessions with the same session ID. Assert:

```python
assert board.device_count == 2
assert {item.display_name for item in board.devices} == {"MacBook Pro", "Mac mini"}
assert projection_latency_seconds < 10
assert macbook_port.received == ["继续 Codex 测试"]
assert macmini_port.received == ["采用 Claude 灰度方案"]
```

Disconnect Mac mini, queue events and a command, reconnect, and assert ordered once-only recovery.

- [ ] **Step 2: Run and verify RED**

```bash
python -m pytest tests/test_multi_device_copilot_e2e.py -q
```

Expected: FAIL before the full device flow is wired.

- [ ] **Step 3: Complete the acceptance harness**

The script accepts two sanitized JSON status files or queries two remote gateways, sends no write command unless `--allow-write-canary` is present, and outputs only device display name, Agent type, session state, latency, callback result, and pass/fail evidence.

- [ ] **Step 4: Run all local release checks**

```bash
python -m pytest -q
ruff check .
python scripts/multi_device_copilot_acceptance.py --fixtures tests/fixtures/multi-device
```

Expected: all pytest tests pass, Ruff passes, and the acceptance script reports `status=passed devices=2 crossed_routes=0`.

- [ ] **Step 5: Commit**

```bash
git add tests/test_multi_device_copilot_e2e.py scripts/multi_device_copilot_acceptance.py scripts/copilot_relay_smoke.sh tests/fixtures/multi-device
git commit -m "test(copilot): prove dual-device realtime isolation"
```

### Task 13: Deploy the MacBook Pro canary and migrate `ostag-local`

**Files:**
- Runtime only; no source edits expected.

- [ ] **Step 1: Back up local and central state**

Create timestamped copies of the local relay spool and production SQLite database; run `PRAGMA integrity_check` on both copies. Record checksums without printing identity or secret values.

- [ ] **Step 2: Deploy the central candidate with flags limited to the owner**

Build an immutable image from the exact commit. Keep new device writes off globally and enable them only for the verified owner canary. Verify internal and external `/healthz`, schema version, image tag, restart count zero, and logs flowing.

- [ ] **Step 3: Pair and install MacBook Pro Gateway**

Generate a one-time code through the authenticated owner route, run the installer with display name `MacBook Pro`, and verify a distinct `device_id`, `codex` runtime, one current session, heartbeat under 10 seconds, and no secret output.

- [ ] **Step 4: Run a real Codex decision canary**

Create one bounded decision, receive the Feishu card, answer it, and confirm the exact MacBook/Codex/session/turn tuple applied once. Verify the board updates within 10 seconds.

- [ ] **Step 5: Make `ostag-local` read-only and revoke its callbacks**

Only after the new MacBook path passes, mark the old instance migrated. Keep its audit rows and snapshots; prevent new commands and omit it from the normal device count.

- [ ] **Step 6: Record evidence in osstack**

```bash
osstack kanban comment card_0ef7b775 "MacBook Pro/Codex canary passed; ostag-local migrated read-only"
```

### Task 14: Pair Mac mini/Claude Code and run cross-device production acceptance

**Files:**
- Runtime only; installation uses the committed release bundle.

- [ ] **Step 1: Establish approved Mac mini access**

Use an existing user-approved SSH, Screen Sharing, or local terminal session. Do not enable remote login or create a new account without separate authorization. Record only OS version, architecture, osstack version, Claude adapter availability, and free disk space.

- [ ] **Step 2: Upgrade the governed osstack session build**

Install the exact tested `osstack-copilot-session` artifact, start the daemon, and verify `session list --json` includes `runtime_id: "claude-code"` without native session identifiers.

- [ ] **Step 3: Pair and install Mac mini Gateway**

Generate a separate one-time code, install with display name `Mac mini`, and verify it receives a different `device_id` and key from MacBook Pro.

- [ ] **Step 4: Run simultaneous two-device decisions**

Put MacBook/Codex and Mac mini/Claude Code into distinct waiting-decision states. Answer both from Feishu and assert each local adapter receives exactly its own answer, with `crossed_routes=0`.

- [ ] **Step 5: Run disconnection and restart drills**

For Mac mini, stop the Gateway until the board shows degraded then offline, restart it, and verify ordered once-only replay. Restart the central container and confirm both devices, current sessions, and decisions persist.

- [ ] **Step 6: Validate mobile Feishu UI**

On the latest fresh card, test overview, device detail, continue session, decision answer, refresh, and expired-card recovery. Verify short toasts, 24-hour callbacks, Beijing time, and no `200671`/`200672` errors.

### Task 15: Production gate, documentation, push, and handoff

**Files:**
- Modify: `docs/operations/copilot-relay.md`
- Modify: `docs/superpowers/specs/2026-07-16-ostag-multi-device-realtime-copilot-design.md`
- Modify: `OSTAG_WORK_MONITOR_SESSION_HANDOFF_2026-07-13.md` only if that handoff remains the active operator entry point.

- [ ] **Step 1: Document operations and rollback**

Document pairing, list/revoke, Keychain recovery, heartbeat thresholds, runtime inventory, log locations, dual-device acceptance, legacy-instance migration, central rollback, and per-device Gateway rollback. Include commands but no real IDs or secrets.

- [ ] **Step 2: Run the final osstack gate**

```bash
/Users/gilbertchen/Documents/CODE/agentsteam/osstack/loop-engineering/scripts/osstack-loop.sh enqueue "Final production verification for OStag multi-device realtime Copilot" --capability code.test --priority 100
LOOP_EXECUTE=true LOOP_WORKTREE=0 LOOP_ARCHIVE_COMPLETED=1 LOOP_VERIFY='python -m pytest -q && ruff check . && curl -fsS https://ostag-monitor.ihiking.me/healthz >/dev/null' /Users/gilbertchen/Documents/CODE/agentsteam/osstack/loop-engineering/scripts/loop-run.sh --cycles 1 --stop-when-idle
```

Expected: success rate `100%`, verifier score `1.00`, no pending tasks, exact production image healthy, restart count zero, database integrity `ok`, two fresh device heartbeats, and `crossed_routes=0`.

- [ ] **Step 3: Complete and archive the osstack card**

```bash
osstack kanban comment card_0ef7b775 "Production passed: MacBook Pro/Codex + Mac mini/Claude Code realtime, exact routing, recovery and mobile Feishu acceptance"
osstack kanban move card_0ef7b775 done "All automated and real-device gates passed"
```

- [ ] **Step 4: Commit, push, and perform post-push verification**

Commit documentation separately, push both feature branches, verify local HEAD equals each remote branch, and repeat external health plus exact production image checks. Do not mark the objective complete while Mac mini acceptance or mobile Feishu acceptance remains unverified.

---

## Completion definition

The plan is complete only when the production OStag board simultaneously shows MacBook Pro/Codex and Mac mini/Claude Code with fresh independent heartbeats, both real Agent sessions accept their own Feishu decisions without cross-routing, offline/restart recovery has been exercised, mobile card flows pass, osstack reports score `1.00`, and the exact deployed commits are pushed and documented.
