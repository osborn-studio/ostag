# OS Tag Manual Live Validation Handoff

This handoff is for the human operator who will upgrade OS Tag from
pilot-ready to production-candidate after real Feishu/Lark credentials and a
test group are available.

## Current Automated Status

- Score: `96 / 100`
- Readiness gate: `PASS_WITH_EXTERNAL_BLOCKER`
- External blocker: real Feishu/Lark credentials and group validation evidence
- Main evidence pack: `release/ostag-launch-evidence.md`
- Current validation record: `release/live-validation-feishu.md`

## 1. Prepare Credentials

Create `.env` from `.env.example` and fill the platform credentials:

```bash
cp .env.example .env
$EDITOR .env
```

Required Feishu values:

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`

Required Lark values:

- `LARK_APP_ID`
- `LARK_APP_SECRET`

Do not paste secrets into release records, screenshots, transcripts, or chat.

## 2. Re-run Automated Gates

```bash
scripts/deploy_preflight.sh
scripts/live_validation_record.sh feishu
scripts/validate_live_validation_record.sh feishu
```

Expected before the manual group message:

- `deploy_preflight.sh` passes;
- `live_validation_record.sh` writes a record;
- `validate_live_validation_record.sh` may fail until manual fields are filled.

## 3. Run Live Bot

Start with the mock backend:

```bash
OSTAG_BACKEND=mock scripts/live_smoke.sh feishu
```

In the target Feishu group, send:

```text
@OS Tag 请总结这个项目下一步应该做什么
```

Expected result:

- OS Tag receives the message event.
- OS Tag replies in the same group or message thread.
- `logs/audit.log` contains a row with `"status": "ok"`.

## 4. Fill The Validation Record

Edit `release/live-validation-feishu.md`:

```text
- Status: `manual_group_validation_passed`
- Test chat id: <chat id>
- Incoming message id: <message id>
- Backend: mock
- Audit log path: logs/audit.log
- Audit timestamp: <matching audit timestamp>
- Result: pass
- Screenshot/transcript: release/manual-live-validation-transcript.txt
```

Store any screenshot or transcript at the path written in the record.

## 5. Final Gate

```bash
scripts/validate_live_validation_record.sh feishu
scripts/mfs_validation_record.sh
scripts/validate_mfs_validation_record.sh
scripts/release_readiness_audit.sh
```

Production-candidate requires:

- `validate_live_validation_record.sh feishu` exits `0`;
- `validate_mfs_validation_record.sh` exits `0`, or MFS is intentionally
  disabled for the production-candidate scope and that decision is recorded;
- `release/readiness-audit.md` no longer reports the manual live validation
  warnings;
- `release/ostag-scorecard.md` can be updated from pilot-ready to
  production-candidate evidence.

## Rollback

Stop the live process, keep `OSTAG_BACKEND=mock`, and return to dry-run:

```bash
OSTAG_DRY_RUN=true python -m ostag.main --platform feishu --dry-run --event examples/feishu_event.json
```
