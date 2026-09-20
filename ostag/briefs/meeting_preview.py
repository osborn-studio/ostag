"""Deterministic synthetic meeting preview. Input is data, never authority or code.

No provider, model, settings discovery, business storage or subprocess is used.
Fixture lookups deliberately simulate current policy; they authenticate nothing.
"""
from __future__ import annotations

import hashlib
import html
import io
import json
import os
import re
import stat
import sys
import unicodedata
from contextlib import redirect_stderr
from urllib.parse import quote

from ostag.briefs.grants import PilotGrant
from ostag.briefs.meeting_models import BudgetProfile, MeetingRequest
from ostag.briefs.models import SourceRef, _keys, _positive_int, _text, _utc
from ostag.briefs.project_mapping import ProjectMapping, freeze_meeting_scope
from ostag.config import Settings

MAX_BYTES = 1024 * 1024
NAMESPACE = "synthetic-preview"
BANNER = (
    "SYNTHETIC LOCAL PREVIEW · 模拟授权，仅使用本地 fixture。"
    "synthetic 标记不证明内容是虚构的；输入 grant 不是经过认证的生产签发。"
    "draft_ready / meeting_ready 仅表示本地必需分区技术完整；始终需要人工语义审阅。"
    "不认证会议质量、实时完整性、历史重建或人工验收；不是 M05 完成或上线授权。"
)
# Exact event schemas, not a provider registry. ref means the associated target branch.
KINDS = {
    "pr_merged": ("pulls", ("pr_number", "head_sha", "merge_sha", "ref",
                            "created_at", "updated_at"), "PR 已合并（不等于上线）"),
    "issue": ("issues", ("issue_number",), "议题记录（不代表决策）"),
    "issue_comment": ("issue_comments", ("issue_number", "comment_id"), "议题评论（说法）"),
    "commit": ("commits", ("sha", "ref"), "提交记录（不等于上线）"),
    "release_published": ("releases", ("tag", "sha", "ref"), "版本已发布（不等于部署）"),
    "check_success": ("checks", ("pr_number", "head_sha", "check_sha", "check_run_id", "ref"),
                      "检查成功记录（需匹配 PR 精确 head，不等于部署）"),
    "deployment_success": ("deployments", ("deployment_id", "sha", "environment", "ref"),
                           "明确环境与 SHA 的部署成功记录"),
}
URL_TEXT = re.compile(
    r"(?<![A-Za-z0-9.+-])(?:[a-z][a-z0-9+.-]*://|(?:javascript|data|vbscript|file|mailto):)"
    r"[^\s<>\[\]\"']*"
    r"|(?://|www\.)[^\s<>\[\]\"']+"
    r"|(?<![A-Za-z0-9.-])(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?![A-Za-z0-9])"
    r"(?:[/:?#][^\s<>\[\]\"']*)?", re.IGNORECASE | re.ASCII,
)


def safe_text(value: str) -> str:
    """Inert display projection; provenance hashes use the ORIGINAL text."""
    value = "".join(c if c not in "\r\n\t" else " " for c in value
                    if not unicodedata.category(c).startswith("C") or c in "\r\n\t")
    value = URL_TEXT.sub("（链接已省略）", value)
    return _escape_display(value)


def _escape_display(value: str) -> str:
    value = html.escape(value, quote=True)
    return re.sub(r"([\\`*_{}\[\]()#!|~>])", r"\\\1", value)


def _display_identifier(value: str, field: str) -> str:
    # Tags already have an exact ASCII schema. Ref/environment remain untrusted
    # text; exempt only simple path segments and numeric version forms from the
    # broad domain heuristic (e.g. release/1.2, staging-v1.2, not rogue.invalid).
    segment = r"(?:[A-Za-z0-9_-]+|[A-Za-z_-]*[0-9]+(?:\.[0-9]+){1,2}(?:[-_][A-Za-z0-9_-]+)?)"
    if field == "tag" or re.fullmatch(rf"{segment}(?:/{segment})*", value):
        return _escape_display(value)
    return safe_text(value)


def _require(condition):
    if not condition:
        raise ValueError("invalid synthetic preview")


def _token(value):
    _require(type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value))
    return value


def _list(value):
    _require(type(value) is list and len(value) <= 500)
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def load_bundle(path) -> dict:
    """Bounded regular file only; O_NONBLOCK avoids blocking if replaced by a FIFO."""
    _require(stat.S_ISREG(os.lstat(path).st_mode))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        _require(stat.S_ISREG(info.st_mode) and info.st_size <= MAX_BYTES)
        raw = stream.read(MAX_BYTES + 1)
    _require(len(raw) <= MAX_BYTES)
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                      parse_constant=lambda _: _require(False))


def _freeze(bundle):
    request = MeetingRequest.from_dict(bundle["request"])
    mapping = ProjectMapping.from_dict(bundle["mapping"])
    grant = PilotGrant.from_dict(bundle["grant"])
    # This fixed local fixture policy is intentionally NOT a production issuer/ACL.
    _require(request.namespace == NAMESPACE)
    _require(request.requesting_principal.to_dict() == {
        "platform": "feishu", "actor_id": "synthetic-reviewer",
    })
    _token(grant.grant_id)
    repos = {r.repo_id: r for r in mapping.repositories}
    budget = BudgetProfile(NAMESPACE, 1, max_repos=2)
    intake = freeze_meeting_scope(
        request=request,
        mapping_lookup=lambda ns, mid: mapping if (ns, mid) == (
            mapping.namespace, mapping.mapping_id) else None,
        repository_lookup=lambda ns, rid, branch: repos.get(rid) if ns == NAMESPACE else None,
        requester_can_read=lambda ns, principal, rid: (
            ns == NAMESPACE and principal == grant.principal and rid in grant.resource_ids),
        budget_lookup=lambda bid: budget if bid == budget.profile_id else None,
        grant_lookup=lambda gid: grant if gid == grant.grant_id else None,
        config=Settings(env="synthetic-preview", dry_run=True, backend="mock",
                        default_permission_level="read",
                        actor_permissions=("feishu:synthetic-reviewer:propose",)),
        now=bundle["observed_at"],
    )
    return intake.scope


def _github(row, repos, categories):
    _require(type(row) is dict)
    kind = _text(row.get("kind"), "kind")
    _require(kind in KINDS)
    category, fields, _ = KINDS[kind]
    row = _keys(row, ("source_id", "repo_id", "kind", "event_at", "observed_at",
                      "title", "excerpt") + fields)
    _require(type(row["repo_id"]) is str and row["repo_id"] in repos and category in categories)
    for field in fields:
        value = row[field]
        if field.endswith(("_number", "_id")):
            _positive_int(value, field)
            _require(value <= 2**63 - 1)
        elif field.endswith("sha"):
            _require(type(value) is str and re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", value))
        elif field.endswith("_at"):
            _utc(value, field)
        else:
            _text(value, field, 256)
    if kind == "pr_merged":
        _require(_utc(row["created_at"], "created_at") <= _utc(row["event_at"], "event_at")
                 <= _utc(row["updated_at"], "updated_at")
                 <= _utc(row["observed_at"], "observed_at"))
    if kind == "release_published":
        _require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", row["tag"]))
    raw = _text(row["title"], "title", 400) + "\n" + _text(
        row["excerpt"], "excerpt", 3000, empty=True)
    base = "https://github.com/" + repos[row["repo_id"]].full_name
    if kind == "pr_merged":
        url = f"{base}/pull/{row['pr_number']}"
    elif kind in ("issue", "issue_comment"):
        url = f"{base}/issues/{row['issue_number']}"
        if kind == "issue_comment":
            url += f"#issuecomment-{row['comment_id']}"
    elif kind == "release_published":
        url = f"{base}/releases/tag/{quote(row['tag'], safe='')}"
    elif kind == "check_success":
        # A check run ID is not an Actions workflow run ID.
        url = f"{base}/pull/{row['pr_number']}/checks?check_run_id={row['check_run_id']}"
    elif kind == "deployment_success":
        url = f"{base}/deployments/{row['deployment_id']}"
    else:
        url = f"{base}/commit/{row['sha']}"
    return row, f"github:repo:{row['repo_id']}:{category}", raw, url


def _feishu(row, repos, chats):
    row = _keys(row, ("source_id", "chat_id", "text", "event_at", "observed_at"),
                ("references",))
    _require(type(row["chat_id"]) is str and row["chat_id"] in chats)
    references = _list(row.get("references", []))
    seen = set()
    for ref in references:
        _keys(ref, ("repo_id", "pr_number"))
        _require(type(ref["repo_id"]) is str and ref["repo_id"] in repos)
        _positive_int(ref["pr_number"], "pr_number")
        _require(ref["pr_number"] <= 2**63 - 1)
        key = (ref["repo_id"], ref["pr_number"])
        _require(key not in seen)
        seen.add(key)
    return row, f"feishu:chat:{row['chat_id']}:messages", _text(row["text"], "text", 4000), None


def _coverage(bundle, scope):
    manifest = {p.partition_id: p for p in scope.partitions}
    reports = {}
    for row in bundle["partitions"]:
        row = _keys(row, ("partition_id", "status", "reason"))
        pid = _text(row["partition_id"], "partition_id")
        _require(pid in manifest and pid not in reports)
        _require(type(row["status"]) is str and row["status"] in (
            "complete", "partial", "failed", "denied"))
        reason = safe_text(_text(row["reason"], "reason", 256))
        reports[pid] = dict(partition_id=pid, required=manifest[pid].required,
                            status=row["status"], reason=reason,
                            included=0, excluded_out_of_window=0)
    _require(reports.keys() == manifest.keys())
    return {pid: reports[pid] for pid in manifest}


def _sources(bundle, scope, coverage):
    repos = {r.repo_id: r for r in scope.repositories}
    chats = {c.chat_id for c in scope.chats}
    categories = {c.value for c in scope.request.source_categories}
    cutoff = _utc(bundle["observed_at"], "observed_at")
    start, end = (_utc(v, "window") for v in (scope.request.window.start, scope.request.window.end))
    seen, merged = set(), set()
    included, sources, citations = [], [], {}
    for platform, records in (("github", bundle["github"]), ("feishu", bundle["feishu"])):
        for record in records:
            row, pid, raw, url = (_github(record, repos, categories) if platform == "github"
                                  else _feishu(record, repos, chats))
            sid = _token(row["source_id"])
            _require(sid not in seen)
            seen.add(sid)
            event, observed = (_utc(row[k], k) for k in ("event_at", "observed_at"))
            _require(event <= observed <= cutoff)
            # Failed/denied partitions cannot claim available records; use partial instead.
            _require(coverage[pid]["status"] in ("complete", "partial"))
            if row.get("kind") == "pr_merged":
                key = (row["repo_id"], row["pr_number"])
                _require(key not in merged)
                merged.add(key)
            if not start <= event < end:
                coverage[pid]["excluded_out_of_window"] += 1
                continue
            coverage[pid]["included"] += 1
            source = SourceRef(
                namespace=NAMESPACE, source_id=sid, resource_id=pid.rsplit(":", 1)[0],
                source_kind="github_event" if platform == "github" else "feishu_message",
                resource_version=row.get("sha", row.get("merge_sha", row.get("check_sha", "unknown"))),
                window=scope.request.window, event_at=row["event_at"], observed_at=row["observed_at"],
                content_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                grant_id=scope.request.grant_id, grant_revision=scope.request.grant_revision,
                collection_platform=platform, collection_actor_id="synthetic-fixture",
                purpose=scope.request.purpose.value, allowed_audience=scope.request.audience,
                visibility="private", excerpt=safe_text(raw),
            )
            sources.append(source.to_dict())
            if url:
                citations[sid] = url
            included.append(row)
    return included, sources, citations


def _facts(rows, sources):
    excerpts = {s["source_id"]: s["excerpt"] for s in sources}
    facts = []
    for row in rows:
        sid, kind = row["source_id"], row.get("kind", "chat_claim")
        fact = dict(kind=kind, source_ids=[sid], event_at=row["event_at"],
                    observed_at=row["observed_at"], text=excerpts[sid])
        if kind == "chat_claim":
            fact.update(label="聊天原文说法（未验证，不是决策）", chat_id=row["chat_id"],
                        references=row.get("references", []))
        else:
            fact.update(label=KINDS[kind][2], repo_id=row["repo_id"])
            for field in KINDS[kind][1]:
                value = row[field]
                fact[field] = (_display_identifier(value, field)
                               if field in ("ref", "environment", "tag") else value)
        if kind == "pr_merged":
            checks = [r for r in rows if r.get("kind") == "check_success"
                      and r["repo_id"] == row["repo_id"] and r["pr_number"] == row["pr_number"]
                      and r["check_sha"] == r["head_sha"] == row["head_sha"]
                      and r["ref"] == row["ref"]]
            deployments = [r for r in rows if r.get("kind") == "deployment_success"
                           and r["repo_id"] == row["repo_id"] and r["sha"] == row["merge_sha"]
                           and r["environment"] == "production" and r["ref"] == row["ref"]]
            fact.update(check_status="success_at_head" if checks else "unknown",
                        production_deployment="success" if deployments else "unknown")
            fact["source_ids"] += [r["source_id"] for r in checks + deployments]
        if kind == "check_success":
            matches = [r for r in rows if r.get("kind") == "pr_merged"
                and r["repo_id"] == row["repo_id"]
                and r["pr_number"] == row["pr_number"] and r["head_sha"] == row["head_sha"]
                == row["check_sha"] and r["ref"] == row["ref"]]
            fact["head_match"] = bool(matches)
            fact["source_ids"] += [r["source_id"] for r in matches]
            fact["check_status"] = "success_at_head" if fact["head_match"] else "unknown"
        facts.append(fact)
    return facts


def _questions(rows, facts, repo_ids):
    merges = {(f["repo_id"], f["pr_number"]): f for f in facts if f["kind"] == "pr_merged"}
    times = {r["source_id"]: r["event_at"] for r in rows}
    conflicts, ambiguities = [], []
    for row in rows:
        if "chat_id" not in row:
            continue
        refs = row.get("references", [])
        for ref in refs:
            if (ref["repo_id"], ref["pr_number"]) not in merges:
                ambiguities.append(dict(
                    kind="referenced_pr_unavailable", source_ids=[row["source_id"]],
                    repo_id=ref["repo_id"], pr_number=ref["pr_number"],
                    text="显式引用的 PR 在窗口内没有合并记录，无法据此核实上线说法。",
                ))
        # Deliberately narrow literal detector, including negations/quotes for human review.
        if "已上线" in row["text"]:
            for ref in refs:
                merged = merges.get((ref["repo_id"], ref["pr_number"]))
                if merged and merged["production_deployment"] == "unknown":
                    ids = [row["source_id"], *merged["source_ids"]]
                    conflicts.append(dict(
                        kind="deployment_unverified", source_ids=ids,
                        event_times={sid: times[sid] for sid in ids},
                        text="聊天含“已上线”说法，相关 PR 已合并，但窗口内未见匹配生产部署证据；"
                             "状态未知，不能证明说法为假或部署失败。",
                        question="建议核实：这条说法指哪个环境、哪个 SHA？能否补充明确的部署记录？",
                    ))
        explicit_numbers = {r["pr_number"] for r in refs}
        for number in sorted({int(n) for n in re.findall(
                r"(?<![A-Za-z0-9_])#([1-9][0-9]{0,18})(?![0-9])",
                URL_TEXT.sub("", row["text"]))}):
            if number not in explicit_numbers:
                ambiguities.append(dict(
                    kind="unqualified_pr_reference", source_ids=[row["source_id"]],
                    pr_number=number, candidate_repo_ids=list(repo_ids),
                    text=f"裸引用 #{number} 未绑定仓库；候选仓库 {len(repo_ids)} 个，不自动关联。",
                ))
    return conflicts, ambiguities


def build_preview(bundle: dict) -> dict:
    """Pure transformation, including contract validation and simulated scope freeze."""
    _require(len(json.dumps(bundle, ensure_ascii=False, allow_nan=False).encode()) <= MAX_BYTES)
    bundle = _keys(bundle, ("synthetic", "version", "request", "mapping", "grant",
                            "observed_at", "github", "feishu", "partitions"))
    _require(bundle["synthetic"] is True and type(bundle["version"]) is int
             and bundle["version"] == 1)
    _require(sum(len(_list(bundle[k])) for k in ("github", "feishu", "partitions")) <= 500)
    scope = _freeze(bundle)
    coverage = _coverage(bundle, scope)
    rows, sources, citations = _sources(bundle, scope, coverage)
    facts = _facts(rows, sources)
    conflicts, ambiguities = _questions(rows, facts, scope.request.repo_ids)
    gaps = [dict(c) for c in coverage.values() if c["status"] != "complete"]
    ready = not scope.intake_reasons and not any(c["required"] for c in gaps)
    fact_ids = [s["source_id"] for s in sources]
    agenda = [
        dict(title="确认目标与待确认决策", rationale="以请求目标为讨论边界；所有建议均待人工确认。",
             source_ids=[]),
        dict(title="核对变化与上线证据", rationale="逐项区分合并、精确 head 检查、版本发布与环境部署；"
             "核实聊天说法和未绑定引用。", source_ids=fact_ids),
        dict(title="确认缺失输入与后续问题", rationale="检查下列分区清单及技术限制，商议需要补充的材料。",
             source_ids=[], partition_ids=[c["partition_id"] for c in gaps]),
    ]
    return dict(
        synthetic=True, status="draft_ready" if ready else "needs_input", meeting_ready=ready,
        semantic_review_required=True, authorization="simulated_only", caveat=BANNER,
        goal=safe_text(scope.request.goal), purpose=scope.request.purpose.value,
        window=scope.request.window.to_dict(), observed_at=scope.intake_at,
        scope_hash=scope.content_hash, sources=sources, citations=citations, facts=facts,
        conflicts=conflicts, ambiguities=ambiguities, agenda=agenda, gaps=gaps,
        coverage=list(coverage.values()), technical_limitations=list(scope.intake_reasons),
    )


def render_markdown(preview: dict) -> str:
    """Render only the sanitized projection returned by build_preview."""
    def refs(ids):
        return " ".join(f"[{sid}]({preview['citations'][sid]})" if sid in preview["citations"]
                        else f"`{sid}`" for sid in ids)

    window = preview["window"]
    lines = ["# 合成双源会议预览", "", f"> {preview['caveat']}", "",
             f"状态：{preview['status']}；meeting_ready={str(preview['meeting_ready']).lower()}；"
             "semantic_review_required=true", "", f"目标：{preview['goal']}",
             f"用途：{preview['purpose']}",
             f"时间窗口：[{window['start']}, {window['end']})；时区：{window['display_timezone']}",
             f"观察截止（fixture 元数据）：{preview['observed_at']}",
             f"范围哈希：`{preview['scope_hash']}`", "", "## 预读", "",
             "先阅读变化与原文说法，再核对证据时间和分区缺口。所有记录均来自本地合成输入。",
             "", "## 变化", ""]
    for fact in preview["facts"]:
        lines.append(f"- {fact['label']}：{fact['text']}；事件 {fact['event_at']}；"
                     f"观察 {fact['observed_at']}。{refs(fact['source_ids'])}")
        if fact["kind"] == "pr_merged":
            lines.append(f"  仓库 {fact['repo_id']} PR #{fact['pr_number']}；"
                         f"head `{fact['head_sha']}`；合并 SHA `{fact['merge_sha']}`；"
                         f"检查：{fact['check_status']}；生产部署：{fact['production_deployment']}。")
        if fact["kind"] == "check_success":
            lines.append(f"  check SHA `{fact['check_sha']}`；head `{fact['head_sha']}`；"
                         f"匹配结果：{fact['check_status']}。")
        if fact["kind"] == "deployment_success":
            lines.append(f"  环境：{fact['environment']}；SHA `{fact['sha']}`；ref：{fact['ref']}。")
    if not preview["facts"]:
        lines.append("无窗口内记录；不代表没有发生变化。")
    lines += ["", "## 差异与待核实", ""]
    for item in preview["conflicts"] + preview["ambiguities"]:
        lines.append(f"- {item['text']} {refs(item['source_ids'])}")
        if "event_times" in item:
            lines.append("  证据事件时间：" + "；".join(
                f"{sid} {time}" for sid, time in item["event_times"].items()))
    if not preview["conflicts"] and not preview["ambiguities"]:
        lines.append("窄规则未发现待核实差异；这不是语义一致性保证。")
    lines += ["", "## 议程（最多三项）", ""]
    for i, item in enumerate(preview["agenda"], 1):
        lines.append(f"{i}. {item['title']}：{item['rationale']} {refs(item['source_ids'])}")
    lines += ["", "## 建议问题与行动（待讨论，未批准、未分派）", "",
              "- 建议确认：本次希望确认什么决策？现有输入是否足够？",
              "- 建议核对：合并、检查和发布分别证明了什么？生产环境与 SHA 的证据在哪里？",
              "- 建议补充：缺失分区与未绑定仓库引用；由参会者讨论是否行动及如何安排。"]
    for item in preview["conflicts"]:
        lines.append(f"- {item['question']} {refs(item['source_ids'])}")
    lines += ["", "## 来源", "", "SHA256 基于原始摘录；展示文本已转义并省略任意链接。"]
    for source in preview["sources"]:
        lines.append(f"- {refs([source['source_id']])}；{source['resource_id']}；"
                     f"事件 {source['event_at']}；观察 {source['observed_at']}；"
                     f"SHA256 `{source['content_sha256']}`")
    lines += ["", "## 覆盖缺口与完整分区清单", ""]
    for c in preview["coverage"]:
        lines.append(f"- `{c['partition_id']}`：{c['status']}；"
                     f"{'必需' if c['required'] else '可选'}；窗口内 {c['included']}；"
                     f"窗口外排除 {c['excluded_out_of_window']}；{c['reason']}")
    lines.append("可见缺口：" + ("、".join(c["partition_id"] for c in preview["gaps"])
                               or "无分区技术缺口；仍需语义审阅。"))
    for reason in preview["technical_limitations"]:
        lines.append(f"技术限制：{reason}")
    return "\n".join(lines)


def run_preview(input_path, output_format: str) -> int:
    try:
        preview = build_preview(load_bundle(input_path))
        output = (json.dumps(preview, ensure_ascii=False, indent=2, allow_nan=False)
                  if output_format == "json" else render_markdown(preview))
    except (ValueError, TypeError, KeyError, OSError, RecursionError, OverflowError):
        print("meeting preview: invalid synthetic input; check the local fixture schema.",
              file=sys.stderr)
        return 2
    print(output)
    return 0 if preview["status"] == "draft_ready" else 3


def run_preview_cli(parser, argv) -> int:
    """Keep argparse help, but never echo untrusted invalid arguments to stderr."""
    try:
        with redirect_stderr(io.StringIO()):
            args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code == 0:
            return 0
        print("meeting preview: invalid arguments; use meeting preview --help.", file=sys.stderr)
        return 2
    return run_preview(args.input, args.format)
