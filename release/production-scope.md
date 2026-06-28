# OS Tag Production Scope

- Generated at: `2026-06-28T14:24:36Z`
- MFS mode: `excluded`
- Owner: `Codex`
- MFS rationale: Current production candidate is limited to Feishu/Lark OS Tag bot runtime. Live MFS smoke requires the Rust mfs CLI, but cargo/mfs is not available on this host; MFS remains implemented and tested with fake CLI coverage and will be promoted when the CLI binary is provisioned.

## MFS Mode Contract

- `required`: live MFS smoke must pass before production-candidate.
- `excluded`: MFS is explicitly out of the current production-candidate scope; owner and rationale are required.
