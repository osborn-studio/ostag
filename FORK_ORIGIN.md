# OS Tag Fork Origin

OS Tag is a fork-derived product based on the Open Tag example in `zilliztech/mfs`.

## Upstream

- Repository: https://github.com/zilliztech/mfs
- Baseline commit: 16574f9b365bc67f3716c552dad416da730088b8
- Upstream area: `examples/open-tag-skill`
- License: Apache License 2.0

## Product Direction

- Product name: OS Tag
- Internal code name: `ostag`
- Target: deeply rebuild Open Tag into a Feishu/Lark AI group-chat context assistant.

## Migration Policy

Every upstream Open Tag file must be classified as one of:

- `ported`
- `rewritten`
- `deleted`
- `deferred`

The migration report lives at `docs/fork_migration_report.md`.

## Product Remote

The local fork currently preserves the upstream remote as `upstream`. Add the
product remote as `origin` when the destination repository exists.
