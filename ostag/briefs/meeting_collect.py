"""One bounded local observation, never a grant, historical reconstruction or approval."""
from __future__ import annotations

import hashlib
import io
import json
import re
import time
import unicodedata
from contextlib import redirect_stderr
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from ostag.briefs.meeting_io import CollectionError, LocalTransport, decode_json
from ostag.briefs.models import TimeWindow, _utc
from ostag.core.schemas import SchemaError

PARTITIONS = ("pulls", "issues", "issue_comments", "commits", "releases")
LIMITS = {"overall_seconds": 120, "command_seconds": 15, "pages_per_partition": 4,
          "page_size": 50, "facts": 500, "record_bytes": 65536,
          "response_bytes": 1048576, "total_response_bytes": 16777216}
LIMITATIONS = [
    "Mutable pagination is not an atomic historical snapshot; exhaustion is technical only.",
    "Bodies are observed current text, not reconstructed historical content.",
    "Checks, deployment histories, timelines and reviews are not collected (not full M02/M03).",
    "All PR check/deployment states are unknown; merge and release do not prove deployment.",
    "Commit committer timestamp is not first branch landing; local commits are not collected.",
    "Chat records are claims; threads, media and deleted historical text are not expanded.",
    "No progress, decisions, assignees or deadlines can be inferred from missing evidence.",
]


def require(condition, category="invalid_schema"):
    if not condition:
        raise CollectionError(category)


def string(value, maximum=65536, empty=False):
    require(type(value) is str and (empty or bool(value.strip())) and len(value) <= maximum)
    require(not any(unicodedata.category(c) == "Cs" for c in value))
    return value


def integer(value):
    require(type(value) is int and 0 < value < 2**63)
    return value


def stamp(value):
    try:
        return _utc(value, "timestamp").isoformat().replace("+00:00", "Z")
    except SchemaError:
        raise CollectionError("invalid_schema") from None


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate(repo, start, end, timezone, goal, chats, now):
    window = TimeWindow(start, end, timezone)
    require(_utc(window.end, "end") <= now and
            _utc(window.end, "end") - _utc(window.start, "start") <= timedelta(days=31))
    require(type(repo) is str and re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}", repo)
        and repo.split("/")[1] not in (".", ".."))
    string(goal, 1000)
    require(not any(unicodedata.category(c).startswith("C") for c in goal))
    require(type(chats) in (list, tuple) and len(chats) <= 2)
    require(all(type(c) is str and re.fullmatch(r"oc_[A-Za-z0-9_-]{1,100}", c)
                for c in chats) and len(set(chats)) == len(chats))
    return window


class Collector:
    def __init__(self, repo, window, goal, chats, transport, clock, wall):
        self.repo, self.window, self.chats = repo, window, chats
        self.transport, self.clock, self.wall = transport, clock, wall
        self.start = clock()
        self.result = {
            "status": "draft_ready", "synthetic": False, "meeting_ready": False,
            "semantic_review_required": True, "goal": goal, "window": window.to_dict(),
            "repo": repo, "chat_ids": list(chats), "facts": [], "sources": [], "coverage": {},
            "gaps": [], "agenda": [], "questions": [], "identities": {},
            "observation": {"started_at": self.now()}, "limits": LIMITS,
            "limitations": LIMITATIONS,
            "provenance": {"boundary": "private_local_authenticated_cli_accounts",
                           "transport": "gh_GET_github.com_and_lark-cli_GET_as_user",
                           "issuer": None, "semantic_truth_asserted": False},
        }
        for part in PARTITIONS:
            self.coverage("github:" + part)
        for chat in chats:
            self.coverage("feishu:" + chat)

    def now(self):
        return self.wall().astimezone(UTC).isoformat().replace("+00:00", "Z")

    def coverage(self, part):
        return self.result["coverage"].setdefault(part, {
            "pages_received": 0, "pages_included": 0, "pages_excluded": 0,
            "records_received": 0, "records_included": 0, "records_excluded": 0,
            "facts_included": 0, "exhausted": False, "withheld": False, "reasons": {},
        })

    def gap(self, part, reason, count=1):
        cov = self.coverage(part)
        cov["reasons"][reason] = cov["reasons"].get(reason, 0) + count
        if not any(g["partition"] == part and g["reason"] == reason
                   for g in self.result["gaps"]):
            self.result["gaps"].append({"partition": part, "reason": reason})

    def get(self, provider, path, params, end):
        require(self.clock() < end, "deadline")
        value = self.transport.get(provider, path, params, min(end, self.clock() + 15))
        if provider == "feishu":
            require(type(value) is dict and value.get("ok") is True
                    and value.get("identity") == "user", "user_envelope_required")
            value = value.get("data")
        return value

    def gh_identity(self, end):
        user = self.get("github", "user", {}, end)
        repo = self.get("github", "repos/" + self.repo, {}, end)
        require(type(user) is dict and type(repo) is dict)
        identity = {"id": integer(user.get("id")), "login": string(user.get("login"), 100)}
        require(re.fullmatch(r"[A-Za-z0-9-]+", identity["login"]))
        require(repo.get("full_name") == self.repo and type(repo.get("permissions")) is dict
                and repo["permissions"].get("pull") is True, "repo_access_mismatch")
        branch = string(repo.get("default_branch"), 255)
        require(all(segment not in ("", ".", "..") for segment in branch.split("/")) and
                not any(unicodedata.category(c).startswith("C") for c in branch))
        return identity, {"id": integer(repo.get("id")), "full_name": repo["full_name"],
                          "default_branch": branch}

    def lark_identity(self, end):
        user = self.get("feishu", "/open-apis/authen/v1/user_info", {}, end)
        require(type(user) is dict)
        open_id = string(user.get("open_id"), 100)
        require(re.fullmatch(r"ou_[A-Za-z0-9_-]+", open_id))
        return {"open_id": open_id}

    def withhold(self, provider, reason):
        self.result["facts"] = [f for f in self.result["facts"] if f["provider"] != provider]
        self.result["sources"] = [s for s in self.result["sources"] if s["provider"] != provider]
        if provider == "github":
            self.result.pop("github_tip_sha", None)
            self.result.pop("github_repository", None)
        for part, cov in list(self.result["coverage"].items()):
            if part.startswith(provider + ":"):
                cov["records_excluded"] = cov["records_received"]
                cov["records_included"] = cov["facts_included"] = 0
                cov["pages_excluded"] = cov["pages_received"]
                cov["pages_included"] = 0
                cov["withheld"] = True
                cov["exhausted"] = False
                self.gap(part, reason)

    def inside(self, timestamp):
        return _utc(self.window.start, "start") <= _utc(timestamp, "event") < _utc(
            self.window.end, "end")

    def normalize_gh(self, part, item, repo_id):
        """Return resource, current content, trusted citation, timestamped event candidates."""
        events, caveats, meta = [], ["observed_current_text"], {}
        if part == "commits":
            rid = string(item.get("sha"), 40)
            require(re.fullmatch(r"[0-9a-f]{40}", rid))
            commit = item.get("commit")
            require(type(commit) is dict and type(commit.get("committer")) is dict)
            committed = stamp(commit["committer"].get("date"))
            updated = None  # A commit date is not a provider update timestamp.
            content = string(commit.get("message"), empty=True)
            events = [("commit_timestamp", committed)]
            url = f"https://github.com/{self.repo}/commit/{rid}"
            caveats.append("committer_timestamp_not_first_branch_landing")
        else:
            rid = str(integer(item.get("id")))
            updated = stamp(item.get("updated_at")) if part != "releases" else None
            body = item.get("body")
            require(body is None or type(body) is str)
            body = string(body or "", empty=True)
            if part in ("pulls", "issues"):
                number = integer(item.get("number"))
                meta["number"] = number
                if part == "pulls":
                    base = item.get("base")
                    require(type(base) is dict and type(base.get("repo")) is dict)
                    require(base["repo"].get("full_name") == self.repo and
                            type(base["repo"].get("id")) is int and
                            base["repo"]["id"] == repo_id, "resource_mismatch")
                    meta.update(check_state="unknown", deployment_state="unknown")
                else:
                    require(item.get("repository_url") ==
                            f"https://api.github.com/repos/{self.repo}", "resource_mismatch")
                    if "pull_request" in item:
                        return None
                title = string(item.get("title"), empty=True)
                content = title + "\n" + body
                meta["title"] = title
                kind = "pr" if part == "pulls" else "issue"
                events = [(kind + "_created", stamp(item.get("created_at"))),
                          (kind + "_updated_snapshot", updated)]
                field, event = ("merged_at", "pr_merged") if part == "pulls" else (
                    "closed_at", "issue_closed")
                if item.get(field) is not None:
                    events.append((event, stamp(item[field])))
                elif part == "pulls" and item.get("state") == "closed":
                    require("merged_at" in item, "merge_status_unknown")
                    # A missing time cannot be classified as outside the window.
                    require(item.get("closed_at") is not None, "closure_time_unknown")
                    events.append(("pr_closed_unmerged", stamp(item["closed_at"])))
                    caveats.append("closed_not_merged_or_deployed")
                url = f"https://github.com/{self.repo}/{'pull' if part == 'pulls' else 'issues'}/{number}"
            elif part == "issue_comments":
                match = re.fullmatch(re.escape(f"https://api.github.com/repos/{self.repo}/issues/")
                                     + r"([1-9][0-9]{0,18})", string(item.get("issue_url")))
                require(match is not None, "resource_mismatch")
                number = integer(int(match[1]))
                meta["issue_number"] = number
                content = body
                events = [("comment_created", stamp(item.get("created_at"))),
                          ("comment_updated_snapshot", updated)]
                url = f"https://github.com/{self.repo}/issues/{number}#issuecomment-{rid}"
            else:
                require(type(item.get("draft")) is bool)
                name = item.get("name")
                require(name is None or type(name) is str)
                content = string(name or "", empty=True) + "\n" + body
                meta["tag_name"] = string(item.get("tag_name"), 255)
                url = f"https://api.github.com/repos/{self.repo}/releases/{rid}"
                if item.get("published_at") is not None and not item["draft"]:
                    events = [("release_published", stamp(item["published_at"]))]
                caveats.append("release_not_deployment")
        if updated is not None and _utc(updated, "updated") >= _utc(self.window.end, "end"):
            caveats.append("observed_content_updated_at_or_after_window_end")
        return rid, content, url, events, updated, caveats, meta

    def normalize_lark(self, chat, item):
        require(item.get("chat_id") == chat, "resource_mismatch")
        rid = string(item.get("message_id"), 100)
        require(re.fullmatch(r"om_[A-Za-z0-9_-]+", rid))
        def milliseconds(value):
            require(type(value) is str and re.fullmatch(r"[0-9]{1,14}", value))
            try:
                return datetime.fromtimestamp(int(value) / 1000, UTC).isoformat().replace(
                    "+00:00", "Z")
            except (ValueError, OverflowError, OSError):
                raise CollectionError("invalid_schema") from None
        created = milliseconds(item.get("create_time"))
        updated = milliseconds(item.get("update_time"))
        require(type(item.get("deleted")) is bool and type(item.get("body")) is dict
                and type(item.get("sender")) is dict)
        # msg_type is the live CLI contract; accept the legacy alias only when
        # absent or identical, never masking a malformed present field.
        kind = string(item["msg_type"] if "msg_type" in item else item.get("message_type"), 100)
        if "message_type" in item:
            require(string(item["message_type"], 100) == kind)
        content = string(item["body"].get("content"), empty=True)
        if kind == "system" and item["sender"].get("id") == "" and item["sender"].get("id_type") == "":
            sender = None  # Explicitly unattributed; never substitute the collecting user.
        else:
            sender = {"id": string(item["sender"].get("id"), 256),
                      "id_type": string(item["sender"].get("id_type"), 100)}
        if "updated" in item:
            require(type(item["updated"]) is bool)
            edited = item["updated"]
        else:
            # Compatibility for older records without the explicit content-edit flag.
            edited = updated != created
        caveats, text = ["chat_claim_observed_current_content"], None
        if item["deleted"]:
            caveats.append("deleted_message_historical_content_unavailable")
        elif kind == "text":
            body = decode_json(content)
            require(type(body) is dict)
            text = string(body.get("text"), empty=True)
        else:
            caveats.append("non_text_message_not_expanded")
        if edited:
            caveats.append("edited_message_historical_content_unavailable")
        markers = {}
        for marker in ("root_id", "parent_id", "thread_id"):
            if marker in item:
                markers[marker] = string(item[marker], 100, empty=True)
            if markers.get(marker):
                if "thread_not_expanded" not in caveats:
                    caveats.append("thread_not_expanded")
        return (rid, content, None, [("chat_message_created", created)], updated, caveats,
                {"text": text, "message_type": kind, "sender": sender, **markers})

    def pages(self, provider, part, endpoint, params, end, normalize):
        cov, seen, cursors = self.coverage(part), set(), set()
        for page in range(1, 5):
            try:
                query = dict(params)
                if provider == "github":
                    query.update(per_page=50, page=page)
                value = self.get(provider, endpoint, query, end)
                cov["pages_received"] += 1
                items = value if provider == "github" else (
                    value.get("items") if type(value) is dict else None)
                require(type(items) is list)
                cov["records_received"] += len(items)
                if len(items) > 50:
                    cov["records_excluded"] += len(items)
                    raise CollectionError("page_size_limit")
                cov["pages_included"] += 1
                duplicate = False
                for item in items:
                    try:
                        require(type(item) is dict)
                        raw = json.dumps(item, ensure_ascii=True, sort_keys=True)
                        require(len(raw.encode()) <= LIMITS["record_bytes"], "record_limit")
                        record = normalize(item)
                        if record is None:
                            cov["records_excluded"] += 1
                            cov["reasons"]["pr_in_issues_partition"] = cov["reasons"].get(
                                "pr_in_issues_partition", 0) + 1
                            continue
                        rid, content, url, events, updated, caveats, meta = record
                        require(rid not in seen, "duplicate_drift")
                        seen.add(rid)
                        events = [(kind, at) for kind, at in events if self.inside(at)]
                        if not events:
                            cov["records_excluded"] += 1
                            cov["reasons"]["outside_window"] = cov["reasons"].get(
                                "outside_window", 0) + 1
                            continue
                        require(len(self.result["facts"]) + len(events) <= 500, "fact_limit")
                        source_id = f"{part}:{rid}"
                        observed = self.now()
                        self.result["sources"].append({
                            "source_id": source_id, "provider": provider, "resource_id": rid,
                            "partition": part, "content": content, "content_sha256": digest(content),
                            "record_sha256": digest(raw), "observed_at": observed,
                            "provider_updated_at": updated, "citation": url, "caveats": caveats,
                            "provenance_type": ("chat_claim" if provider == "feishu"
                                                else "repository_record"),
                            **meta,
                        })
                        for kind, at in events:
                            self.result["facts"].append({
                                "provider": provider, "kind": kind, "event_at": at,
                                "observed_at": observed, "provider_updated_at": updated,
                                "source_ids": [source_id], "caveats": caveats,
                                **{k: v for k, v in meta.items() if k not in ("text", "title")},
                            })
                        cov["records_included"] += 1
                        cov["facts_included"] += len(events)
                        if provider == "feishu":
                            for caveat in caveats[1:]:
                                self.gap(part, caveat)
                    except CollectionError as exc:
                        cov["records_excluded"] += 1
                        if exc.category == "resource_mismatch":
                            raise
                        self.gap(part, exc.category)
                        duplicate |= exc.category == "duplicate_drift"
                if duplicate:
                    break
                if provider == "github":
                    more = len(items) == 50
                else:
                    require(type(value.get("has_more")) is bool, "pagination_schema")
                    more = value["has_more"]
                    if more:
                        cursor = string(value.get("page_token"), 2048)
                        require(cursor not in cursors, "repeat_cursor")
                        cursors.add(cursor)
                        params["page_token"] = cursor
                if not more:
                    cov["exhausted"] = True
                    break
                if page == 4:
                    self.gap(part, "page_limit")
            except CollectionError as exc:
                cov["pages_excluded"] = cov["pages_received"] - cov["pages_included"]
                if exc.category == "resource_mismatch":
                    raise
                self.gap(part, exc.category)
                break

    def github(self):
        final_end = self.start + (65 if self.chats else 120)
        data_end = final_end - 30  # Reserve two final identity/access calls.
        try:
            identity, repo = self.gh_identity(data_end)
            self.result["identities"]["github_observed"] = identity
            self.result["github_repository"] = repo
            # GitHub since is strict-after. Overlap a whole second, then apply
            # the exact local [start, end) filter, including fractional bounds.
            since = (_utc(self.window.start, "start").replace(microsecond=0)
                     - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
            for part in PARTITIONS:
                params = {}
                endpoint = "repos/" + self.repo + "/" + (
                    "issues/comments" if part == "issue_comments" else part)
                if part in ("pulls", "issues", "issue_comments"):
                    params.update(sort="updated", direction="desc")
                    if part != "issue_comments":
                        params["state"] = "all"
                    if part != "pulls":
                        params["since"] = since
                if part == "commits":
                    try:
                        tip = self.get("github", "repos/" + self.repo + "/commits/" +
                                       quote(repo["default_branch"], safe=""), {}, data_end)
                        require(type(tip) is dict and type(tip.get("sha")) is str and
                                re.fullmatch(r"[0-9a-f]{40}", tip["sha"]))
                        self.result["github_tip_sha"] = tip["sha"]
                        params.update(sha=tip["sha"], since=since, until=self.window.end)
                    except CollectionError as exc:
                        self.gap("github:commits", exc.category)
                        continue
                self.pages("github", "github:" + part, endpoint, params, data_end,
                           lambda item, p=part: self.normalize_gh(p, item, repo["id"]))
            final_identity, final_repo = self.gh_identity(final_end)
            require((identity, repo) == (final_identity, final_repo), "identity_or_repo_changed")
            self.result["identities"]["github_verified"] = True
        except CollectionError as exc:
            self.withhold("github", exc.category)
            self.result["identities"]["github_verified"] = False

    def feishu(self):
        if not self.chats:
            self.gap("feishu", "feishu_scope_missing")
            return
        final_end, data_end = self.start + 120, self.start + 105
        try:
            identity = self.lark_identity(data_end)
            self.result["identities"]["feishu_observed"] = identity
            for chat in self.chats:
                self.pages("feishu", "feishu:" + chat, "/open-apis/im/v1/messages", {
                    "container_id_type": "chat", "container_id": chat,
                    "start_time": str(int(_utc(self.window.start, "start").timestamp())),
                    "end_time": str(int(_utc(self.window.end, "end").timestamp()) +
                                    bool(_utc(self.window.end, "end").microsecond)),
                    "sort_type": "ByCreateTimeAsc", "page_size": 50,
                }, data_end, lambda item, c=chat: self.normalize_lark(c, item))
            require(identity == self.lark_identity(final_end), "identity_changed")
            self.result["identities"]["feishu_verified"] = True
        except CollectionError as exc:
            self.withhold("feishu", exc.category)
            self.result["identities"]["feishu_verified"] = False

    def finish(self):
        r = self.result
        prs = {s["number"]: s for s in r["sources"] if s["partition"] == "github:pulls"}
        for source in r["sources"]:
            if source["provider"] != "feishu" or source.get("text") is None:
                continue
            text = source["text"]
            # Only exact selected repo PR URLs associate, never bare references or other repos.
            matches = re.finditer(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
                                  r"/pull/([1-9][0-9]*)(?![A-Za-z0-9_/#?%.-])", text)
            linked = []
            for match in matches:
                number = int(match[2]) if len(match[2]) < 19 else 0
                if match[1] == self.repo and number in prs:
                    linked.append(prs[number]["source_id"])
                else:
                    r["questions"].append({"proposed": True, "question":
                        "Confirm unresolved explicit PR reference.", "source_ids": [source["source_id"]]})
            source["associated_source_ids"] = sorted(set(linked))
            if re.search(r"#[0-9]+", text):
                r["questions"].append({"proposed": True, "question":
                    "Clarify bare issue/PR reference; no association inferred.",
                    "source_ids": [source["source_id"]]})
        for source in r["sources"]:
            if source["partition"] in ("github:pulls", "github:issues") and len(r["agenda"]) < 2:
                r["agenda"].append({"proposed": True, "question":
                    f"Review observed {source['partition'].split(':')[1]} #{source['number']}: "
                    + source["title"] + " — what needs clarification or follow-up?",
                    "source_ids": [source["source_id"]]})
        if r["gaps"]:
            r["agenda"].append({"proposed": True, "question":
                "Resolve collection gaps before drawing conclusions.", "source_ids": [],
                "gap_references": list(r["gaps"])})
            r["status"] = "needs_input"
        r["observation"]["finished_at"] = self.now()
        r["observation"]["elapsed_seconds"] = max(0, self.clock() - self.start)
        return r


def collect(repo, start, end, timezone, goal, chats=(), *, transport=None,
            clock=time.monotonic, wall=lambda: datetime.now(UTC)):
    window = validate(repo, start, end, timezone, goal, chats, wall())
    collector = Collector(repo, window, goal, chats, transport or LocalTransport(clock=clock),
                          clock, wall)
    collector.github()
    collector.feishu()
    return collector.finish()


def inert(value):
    """Exact readable text except explicitly visible control/bidi projections; no truncation."""
    visible = "".join(f"\\u{ord(c):04x}" if (unicodedata.category(c).startswith("C")
                      or unicodedata.category(c) in ("Zl", "Zp")) and c != "\n" else c for c in value)
    fence = "`" * max(3, 1 + max((len(m[0]) for m in re.finditer(r"`+", visible)), default=0))
    return f"{fence}text\n{visible}\n{fence}"


def markdown(r):
    lines = ["# Coverage gaps", inert(json.dumps(r["gaps"], ensure_ascii=False, indent=2)),
             "# Private local collection draft", "meeting_ready=false; semantic_review_required=true",
             inert(r["status"] + "\n" + r["goal"]), "## Window and observation",
             inert(json.dumps({k: r[k] for k in ("window", "observation", "identities")},
                              ensure_ascii=False, indent=2)), "## Limitations",
             *["- " + line for line in LIMITATIONS], "## Coverage and limits",
             inert(json.dumps({"coverage": r["coverage"], "limits": r["limits"]}, indent=2)),
             "## Relevant changes / chat claims"]
    for fact in r["facts"]:
        lines.append(inert(json.dumps(fact, ensure_ascii=False, indent=2)))
    lines.extend(["## Proposed agenda", inert(json.dumps(r["agenda"], ensure_ascii=False, indent=2)),
                  "## Proposed questions", inert(json.dumps(r["questions"], ensure_ascii=False, indent=2)),
                  "## Sources", "Content is current observed text. Control/bidi characters display as "
                  "\\u escapes; hashes cover original content, without display projection."])
    for source in r["sources"]:
        lines.append(inert(source["source_id"] + "\nSHA256: " + source["content_sha256"]))
        if source["citation"]:
            lines.append(f"[GitHub source]({source['citation']})")
        lines.append(inert(source.get("text") if source.get("text") is not None else source["content"]))
        lines.append(inert("Caveats: " + ", ".join(source["caveats"])))
    return "\n\n".join(lines) + "\n"


def run_collect_cli(parser, argv):
    try:
        with redirect_stderr(io.StringIO()):
            args = parser.parse_args(argv)
        result = collect(args.repo, args.start, args.end, args.timezone, args.goal, args.chat_id)
    except SystemExit as exc:
        if exc.code == 0:
            return 0
        print("meeting collect: invalid input")
        return 2
    except (CollectionError, SchemaError, ValueError, TypeError, OverflowError):
        print("meeting collect: invalid input")
        return 2
    print(json.dumps(result, ensure_ascii=True) if args.format == "json" else markdown(result), end="\n")
    return 0 if result["status"] == "draft_ready" else 3
