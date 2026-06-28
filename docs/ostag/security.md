# OS Tag Security And Governance

OS Tag is a controlled-pilot bot, not a hardened enterprise sandbox. Operators
must keep the default safety boundaries unless they are testing in an isolated
environment.

## Default Boundaries

- `OSTAG_DRY_RUN=true` in the sample environment.
- `MFS_ENABLED=false` by default.
- `OSTAG_ALLOW_SHELL=false` by default.
- `OSTAG_CODEX_UNSAFE_MODE=false` by default.
- `OSTAG_CLAUDE_UNSAFE_MODE=false` by default.
- Backend calls have `OSTAG_AGENT_TIMEOUT_SECONDS`.
- MFS context is limited by `MFS_ALLOWED_SCOPES`.
- Prompt context is capped by `OSTAG_MAX_CONTEXT_CHARS`.
- User input is capped by `OSTAG_MAX_INPUT_CHARS`.

## Audit Policy

Every handled event should write JSONL audit data with status `ok`, `ignored`,
`rejected`, or `error`.

Development mode may include a short redacted input preview. Production mode
must store a short input hash and lengths rather than full user text.

## Prompt Injection Note

Group chat messages are untrusted input. A user can ask the bot to ignore
instructions, reveal secrets, or exceed scope. OS Tag should treat every message
as data, apply scope checks before MFS access, and avoid enabling CLI unsafe
flags unless the process is running on an isolated machine.

## Live Bot Governance

- Start live validation with `OSTAG_BACKEND=mock`.
- Invite the bot only to approved pilot groups.
- Keep audit logs on local disk or approved infrastructure.
- Rotate Feishu/Lark secrets if they appear in terminal history or logs.
- Record manual validation evidence before declaring a launch candidate.
