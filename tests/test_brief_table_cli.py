"""Synthetic v1.0.69 matrix contract evidence, never live A08 acceptance."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
import sys
import time

import pytest

from ostag.briefs import table_cli as cli
from ostag.briefs.meeting_io import run_process
from ostag.briefs.table_quality import (
    FieldMapping, RecordReference, ResourceKey, TableReadPort, analyze_table,
)

KEY = ResourceKey("synthetic-a08", "feishu", "baseSynthetic", "tblSynthetic")
USER = "ou_synthetic"
PROFILE = "synthetic-profile"
NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)
VERSION = b"lark-cli version 1.0.69\n"


def envelope(data, **changes):
    return {"ok": True, "identity": "user", "data": data} | changes


def matrix(rows=None, ids=None, fields=None, more=False, **extra):
    rows = [["task"]] if rows is None else rows
    return {"fields": ["Name"] if fields is None else fields,
            "record_id_list": [f"rec_{i}" for i in range(len(rows))] if ids is None else ids,
            "data": rows, "has_more": more} | extra


class FakeCLI:
    def __init__(self, pages=None, *, version=VERSION, identities=None):
        self.pages = pages if pages is not None else [matrix()]
        self.version = version
        self.identities = list(identities or [USER, USER])
        self.calls = []
        self.offsets = []

    def __call__(self, argv, *, deadline, clock):
        self.calls.append(list(argv))
        assert 0 < deadline - clock() <= 15
        if "--version" in argv:
            assert argv == ["lark-cli", "--profile", PROFILE, "--version"]
            value = self.version
        elif argv[1] == "api":
            assert argv == ["lark-cli", "api", "GET", "/open-apis/authen/v1/user_info",
                            "--as", "user", "--params", "{}", "--format", "json",
                            "--profile", PROFILE]
            value = envelope({"open_id": self.identities.pop(0)})
        else:
            assert argv[-2:] == ["--profile", PROFILE]
            assert argv[:3] == ["lark-cli", "base", "+record-list"]
            assert argv[3:13] == ["--base-token", KEY.base_id, "--table-id", KEY.table_id,
                                  "--as", "user", "--format", "json", "--offset",
                                  str(sum(len(p["data"]) for p in self.pages[:len(self.offsets)]))]
            assert argv[13:15] == ["--limit", "100"]
            page = self.pages[len(self.offsets)]
            self.offsets.append(int(argv[12]))
            value = envelope(page)
        raw = value if type(value) is bytes else json.dumps(value).encode()
        return raw, len(raw)


def reader(fake=None, **kwargs):
    return cli.FeishuTableCLI(profile=PROFILE, expected_open_id=USER, namespace=KEY.namespace,
                              authorize=kwargs.pop("authorize", lambda _: None),
                              runner=fake if fake is not None else FakeCLI(),
                              clock=kwargs.pop("clock", lambda: 10.0),
                              wall=kwargs.pop("wall", lambda: NOW), **kwargs)


def denied(action):
    with pytest.raises(cli.TableReadError, match="^table_read_denied$") as caught:
        action()
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert repr(caught.value) == "TableReadError('table_read_denied')"


def full_fixture():
    fields = ["Title", "State", "Owner", "Manual", "Types"]
    typed = [None, False, True, 0, 1, 1.25, "", [], {}, {"nested": ["人工 😀", 2]}]
    rows = []
    for i in range(48):
        rows.append([f"Task {i}" if i < 44 else "", "done" if i < 31 else "open",
                     [] if 31 <= i < 41 else [{"id": "ou_owner", "name": "Owner"}],
                     {"text": "manual", "nested": [None, {"keep": True}]}, typed])
    return matrix(rows, fields=fields)


def test_whole_48_row_fixture_preserves_cells_and_caller_quality_mapping():
    page = full_fixture()
    fake, guards = FakeCLI([page]), []
    source = reader(fake, authorize=lambda key: guards.append(key))
    assert fake.calls == [] and guards == []  # Constructor does no IO.
    snapshot = TableReadPort(source.snapshot).snapshot(KEY)
    mapping = FieldMapping("Title", "State", "Owner", ("open",), ("done",))
    quality = analyze_table(snapshot, mapping)
    assert quality.mapping is mapping
    assert len(snapshot.records) == 48
    assert sum(row.unfinished is False for row in quality.rows) == 31
    assert quality.total_unfinished == 17 and quality.named_unfinished == 13
    assert quality.blank_title_unfinished == quality.blank_row_count == 4
    assert quality.missing_owner_named_unfinished == 10
    assert snapshot.resource_key == KEY and snapshot.observed_revision == "unknown"
    assert snapshot.observed_at == "2026-09-20T12:00:00Z"
    for record, cells in zip(snapshot.records, page["data"]):
        assert record.fields == dict(zip(page["fields"], cells))
    assert guards == [KEY] * 9  # Before/after four invocations + release.
    assert fake.offsets == [0]
    for value in (source, source.snapshot, snapshot, snapshot.records[0]):
        assert all(private not in repr(value) for private in (USER, PROFILE, "rec_0", "manual"))


@pytest.mark.parametrize("lengths", [(24, 24), (10, 20, 18), (100,) * 10])
def test_sequential_pages_use_actual_progress_and_accept_exact_row_page_cap(lengths):
    pages, start = [], 0
    for index, length in enumerate(lengths):
        pages.append(matrix([[None]] * length, ids=[f"rec_{i}" for i in range(start, start + length)],
                            more=index < len(lengths) - 1))
        start += length
    fake = FakeCLI(pages)
    snapshot = reader(fake).snapshot(KEY)
    assert len(snapshot.records) == sum(lengths)
    assert fake.offsets == [sum(lengths[:i]) for i in range(len(lengths))]
    assert all(r.fields == {"Name": None} for r in snapshot.records)


def test_readback_runs_version_identity_and_table_again_and_sees_manual_change():
    page = full_fixture()
    fake = FakeCLI([page], identities=[USER] * 4)
    ticks = iter([NOW, NOW + timedelta(seconds=1), NOW + timedelta(seconds=2),
                  NOW + timedelta(seconds=3)])
    port = TableReadPort(reader(fake, wall=lambda: next(ticks)).snapshot)
    first = port.snapshot(KEY)
    page["data"][0][3] = {"manual edit": [True, "exact"]}
    fake.offsets.clear()
    fresh = port.readback(RecordReference(KEY, "rec_0"))
    assert len(fake.calls) == 8
    assert fresh.record.fields["Manual"] == {"manual edit": [True, "exact"]}
    assert first.records[0].fields["Manual"] == {"text": "manual", "nested": [None, {"keep": True}]}
    assert fresh.snapshot.content_sha256 != first.content_sha256
    assert fresh.snapshot.observed_at == "2026-09-20T12:00:03Z"
    assert first.observed_at == "2026-09-20T12:00:01Z"
    assert fresh.snapshot.observed_revision == "unknown"


def test_empty_matrix_and_completely_blank_rows_are_preserved():
    assert reader(FakeCLI([matrix([], fields=["Name", "Age"])])).snapshot(KEY).records == ()
    snapshot = reader(FakeCLI([matrix([[None, "", []]], fields=["A", "B", "C"])])).snapshot(KEY)
    assert snapshot.records[0].fields == {"A": None, "B": "", "C": []}


@pytest.mark.parametrize("version", [VERSION, VERSION.rstrip(b"\n")])
def test_version_probe_places_explicit_profile_before_version(version):
    fake, calls = FakeCLI(), []
    def runner(argv, **kwargs):
        calls.append(list(argv))
        if "--version" in argv:
            return version, len(version)
        return fake(argv, **kwargs)
    snapshot = reader(runner).snapshot(KEY)
    assert len(snapshot.records) == 1
    assert calls[0] == ["lark-cli", "--profile", PROFILE, "--version"]
    assert len(calls) == 4
    assert all(argv[-2:] == ["--profile", PROFILE] for argv in calls[1:])


@pytest.mark.parametrize("version", [b"lark-cli version 1.0.56\n", b"1.0.69",
    b"lark-cli version 1.0.690\n", b"lark-cli version 1.0.69-dev\n",
    b"lark-cli version 1.0.69\nSECRET", b'{"version":"1.0.69"}'])
def test_version_is_exact_and_never_assumes_host_contract(version):
    fake = FakeCLI(version=version)
    denied(lambda: reader(fake).snapshot(KEY))
    assert len(fake.calls) == 1


@pytest.mark.parametrize("identities,call_count", [(["ou_other"], 2), ([USER, "ou_other"], 4)])
def test_identity_mismatch_before_or_after_collection_withholds_everything(identities, call_count):
    fake = FakeCLI(identities=identities)
    denied(lambda: reader(fake).snapshot(KEY))
    assert len(fake.calls) == call_count


@pytest.mark.parametrize("deny_on", range(1, 10))
@pytest.mark.parametrize("raises", [False, True])
def test_current_authorization_checked_before_after_every_call_and_release(deny_on, raises):
    fake, checks = FakeCLI(), []
    def authorize(key):
        checks.append(key)
        if len(checks) == deny_on:
            if raises:
                raise PermissionError("SECRET body rec_private")
            return False
    denied(lambda: reader(fake, authorize=authorize).snapshot(KEY))
    assert len(checks) == deny_on
    assert len(fake.calls) == min(deny_on // 2, 4)


@pytest.mark.parametrize("change", [
    {"has_more": None}, {"has_more": 1}, {"has_more": "false"},
    {"fields": []}, {"fields": ["Name", "Name"]}, {"fields": [None]},
    {"record_id_list": []}, {"record_id_list": ["rec_a", "rec_b"]},
    {"record_id_list": ["Name alias"]}, {"data": [["one", "extra"]]},
    {"data": [[]]}, {"data": [None]}, {"data": {}},
    {"count": 2}, {"count": True}, {"count": 1.0},
    {"ignored_fields": ["Manual"]}, {"ignored_fields": False},
    {"record_not_found": ["rec_0"]}, {"record_not_found": {}},
    {"omitted_fields": ["Manual"]}, {"query_context": {"record_scope": "view"}},
    {"revision": "unsupported"}, {"items": []},
    {"data": [[float("inf")]]}, {"data": [[float("nan")]]},
])
def test_malformed_or_partial_matrix_never_becomes_empty_or_complete(change):
    denied(lambda: reader(FakeCLI([matrix() | change])).snapshot(KEY))


@pytest.mark.parametrize("missing", ["fields", "record_id_list", "data", "has_more"])
def test_required_matrix_keys_cannot_be_omitted(missing):
    page = matrix()
    del page[missing]
    denied(lambda: reader(FakeCLI([page])).snapshot(KEY))


def test_supported_optional_empty_markers_and_exact_page_count():
    page = matrix(count=1, ignored_fields=[], record_not_found=None)
    assert len(reader(FakeCLI([page])).snapshot(KEY).records) == 1


@pytest.mark.parametrize("pages", [
    [matrix([], more=True)],
    [matrix(more=True), matrix()],  # Duplicate ID on a later page.
    [matrix([[1], [2]], ids=["rec_same", "rec_same"])],
    [matrix(more=True), matrix(ids=["rec_next"], fields=["Changed"])],
    [matrix([[1, 2]], fields=["A", "B"], more=True),
     matrix([[3, 4]], ids=["rec_next"], fields=["B", "A"])],
    [matrix(more=True), matrix([], more=True)],
])
def test_no_progress_duplicates_and_column_drift_deny(pages):
    denied(lambda: reader(FakeCLI(pages)).snapshot(KEY))


@pytest.mark.parametrize("raw", [
    b'{"ok":true,"identity":"user","data":{},"ok":false}',
    b'{"ok":true,"identity":"user","data":{"open_id":"ou_synthetic","open_id":"ou_other"}}',
    b'NaN', b'null', b'[]', b'\xff', b'{', b'[' * 2000,
    json.dumps(envelope({}, ok=False)).encode(),
    json.dumps(envelope({}, identity="tenant")).encode(),
    json.dumps(envelope({}, error={"secret": "rec_private"})).encode(),
    json.dumps(envelope({}, code=True)).encode(),
    json.dumps(envelope({}, code=403)).encode(),
])
def test_strict_json_and_provider_denial_are_redacted(raw):
    fake = FakeCLI()
    def runner(argv, **kwargs):
        if "--version" in argv:
            return fake(argv, **kwargs)
        return raw, len(raw)
    denied(lambda: reader(runner).snapshot(KEY))


@pytest.mark.parametrize("result", [
    (VERSION, -1), (VERSION, True), (VERSION, 1.0), (VERSION, len(VERSION) - 1),
    (VERSION, cli.RESPONSE_BYTES + 1), (b"x" * (cli.RESPONSE_BYTES + 1), 1),
    (VERSION.decode(), len(VERSION)), (bytearray(VERSION), len(VERSION)),
    ({"data": []}, 100), [VERSION, len(VERSION)], None,
])
def test_injected_runner_cannot_bypass_payload_type_or_size_checks(result):
    denied(lambda: reader(lambda *a, **k: result).snapshot(KEY))


def test_raised_runner_secret_does_not_survive_in_error_context():
    def runner(*args, **kwargs):
        try:
            raise ValueError("SECRET body rec_private")
        except ValueError as error:
            raise RuntimeError("credential") from error
    denied(lambda: reader(runner).snapshot(KEY))


def test_response_and_whole_operation_budgets_include_stderr(monkeypatch):
    fake = FakeCLI()
    def runner(argv, **kwargs):
        raw, _ = fake(argv, **kwargs)
        return raw, cli.RESPONSE_BYTES
    # 13 maximum invocations at 1MiB already imply <16MiB. Tighten the total
    # solely to exercise its independent accounting guard, including stderr.
    monkeypatch.setattr(cli, "TOTAL_BYTES", 3 * cli.RESPONSE_BYTES)
    denied(lambda: reader(runner).snapshot(KEY))
    assert len(fake.calls) == 3


def test_total_budget_rejects_overflow_in_final_identity_response(monkeypatch):
    fake = FakeCLI()
    def runner(argv, **kwargs):
        raw, _ = fake(argv, **kwargs)
        return raw, cli.RESPONSE_BYTES
    monkeypatch.setattr(cli, "TOTAL_BYTES", 4 * cli.RESPONSE_BYTES - 1)
    denied(lambda: reader(runner).snapshot(KEY))
    assert len(fake.calls) == 4


def test_row_and_page_caps_deny_truncation():
    denied(lambda: reader(FakeCLI([matrix([[None]] * 101)])).snapshot(KEY))
    for length in (1, 100):
        pages = [matrix([[None]] * length, ids=[f"rec_{p}_{i}" for i in range(length)],
                        more=True) for p in range(10)]
        fake = FakeCLI(pages)
        denied(lambda: reader(fake).snapshot(KEY))
        assert len(fake.offsets) == 10


def test_canonical_snapshot_limit_counts_escaped_unicode():
    # Each row is under 64KiB; UTF-8 responses are under 1MiB, but canonical
    # ASCII-escaped records exceed 4MiB across three pages.
    pages = [matrix([["界" * 5000]] * 50, ids=[f"rec_{p}_{i}" for i in range(50)],
                    more=p < 2) for p in range(3)]
    fake = FakeCLI(pages)
    def runner(argv, **kwargs):
        raw, size = fake(argv, **kwargs)
        if argv[1] == "base":
            raw = json.dumps(json.loads(raw), ensure_ascii=False).encode()
            size = len(raw)
        return raw, size
    denied(lambda: reader(runner).snapshot(KEY))
    assert len(fake.offsets) == 3


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, True, "10", None])
def test_invalid_monotonic_clocks_do_no_io(bad):
    fake = FakeCLI()
    denied(lambda: reader(fake, clock=lambda: bad).snapshot(KEY))
    assert not fake.calls


@pytest.mark.parametrize("after", [9.0, 25.0, 130.0, float("nan")])
def test_clock_rollback_nonfinite_and_late_injected_runner_deny(after):
    current = [10.0]
    fake = FakeCLI()
    def runner(argv, **kwargs):
        result = fake(argv, **kwargs)
        current[0] = after
        return result
    denied(lambda: reader(runner, clock=lambda: current[0]).snapshot(KEY))
    assert len(fake.calls) == 1


def test_total_120_second_deadline_is_not_reset_for_pages():
    current = [0.0]
    pages = [matrix(ids=[f"rec_{i}"], more=True) for i in range(10)]
    fake = FakeCLI(pages)
    def runner(argv, **kwargs):
        result = fake(argv, **kwargs)
        current[0] += 14
        return result
    denied(lambda: reader(runner, clock=lambda: current[0]).snapshot(KEY))
    assert current[0] == 126 and len(fake.calls) == 9


@pytest.mark.parametrize("wall", [lambda: float("nan"), lambda: NOW.replace(tzinfo=None),
                                  lambda: "2026-09-20T12:00:00Z"])
def test_invalid_wall_clock_denies_without_provider_reads(wall):
    fake = FakeCLI()
    denied(lambda: reader(fake, wall=wall).snapshot(KEY))
    assert not fake.calls


def test_wall_rollback_and_authorization_callback_deadline_deny():
    values = iter([NOW, NOW - timedelta(seconds=1)])
    denied(lambda: reader(wall=lambda: next(values)).snapshot(KEY))
    current = [10.0]
    fake = FakeCLI()
    def authorize(_):
        current[0] = 131
    denied(lambda: reader(fake, clock=lambda: current[0], authorize=authorize).snapshot(KEY))
    assert not fake.calls


@pytest.mark.parametrize("part,value", [
    ("namespace", "wrong"), ("platform", "github"), ("table_id", "TableName"),
    ("table_id", "tblX/alias"), ("base_id", "base/alias"), ("base_id", "base:other"),
])
def test_exact_resource_and_identifier_safety_before_any_io(part, value):
    fake = FakeCLI()
    denied(lambda: reader(fake).snapshot(replace(KEY, **{part: value})))
    assert not fake.calls


@pytest.mark.parametrize("value", ["--help", "x;echo SECRET", "$(touch /tmp/nope)",
                                  "x\n--as tenant", "x`id`", "../default", "x\x00"])
def test_malicious_config_and_forged_resource_identifiers_never_enter_argv(value):
    fake = FakeCLI()
    denied(lambda: cli.FeishuTableCLI(value, USER, KEY.namespace, lambda _: None, runner=fake))
    # A forged object also must not evade the connector boundary validation.
    for part in ("base_id", "table_id"):
        key = replace(KEY)
        object.__setattr__(key, part, value)
        denied(lambda: reader(fake).snapshot(key))
    assert not fake.calls


@pytest.mark.parametrize("changes", [{"authorize": None}, {"runner": None}, {"clock": None},
                                    {"wall": None}, {"expected_open_id": "tenant"}])
def test_trusted_constructor_rejects_missing_authority_or_invalid_hooks(changes):
    args = dict(profile=PROFILE, expected_open_id=USER, namespace=KEY.namespace,
                authorize=lambda _: None)
    denied(lambda: cli.FeishuTableCLI(**(args | changes)))


def test_no_cached_or_caller_snapshot_input_and_default_transport_is_shared():
    assert cli.FeishuTableCLI.__dataclass_fields__["runner"].default is run_process
    source = reader()
    denied(lambda: source.snapshot(source.snapshot(KEY)))


def test_shared_subprocess_timeout_cleanup_through_connector(monkeypatch):
    # No lark binary, credentials or network. Existing test_meeting_io also
    # covers descendants retaining pipes, overflow, cancellation and reaping.
    import subprocess
    leaders = []
    real_popen = subprocess.Popen
    def capture(*args, **kwargs):
        assert "env" not in kwargs and not kwargs.get("shell")
        child = real_popen(*args, **kwargs)
        leaders.append(child)
        return child
    monkeypatch.setattr("ostag.briefs.meeting_io.subprocess.Popen", capture)
    def runner(argv, *, deadline, clock):
        return run_process([sys.executable, "-c", "import time; time.sleep(20)"],
                           deadline=min(deadline, clock() + .1), clock=clock)
    denied(lambda: reader(runner, clock=time.monotonic).snapshot(KEY))
    assert len(leaders) == 1 and leaders[0].returncode is not None
    assert leaders[0].stdout.closed and leaders[0].stderr.closed


@pytest.mark.parametrize("stage", ["initial_identity", "page", "final_identity"])
@pytest.mark.parametrize("bad", ["overflow_number", "denied", "oversized", "underreported"])
def test_failure_at_each_response_boundary_withholds_even_collected_rows(stage, bad):
    fake = FakeCLI()
    def runner(argv, **kwargs):
        raw, size = fake(argv, **kwargs)
        index = {"initial_identity": 2, "page": 3, "final_identity": 4}[stage]
        if len(fake.calls) == index:
            if bad == "overflow_number":
                raw = raw[:-1] + b',"metadata":1e400}'
                size = len(raw)
            elif bad == "denied":
                raw = json.dumps(envelope({}, ok=False, error="SECRET rec_private")).encode()
                size = len(raw)
            elif bad == "oversized":
                raw = b" " * cli.RESPONSE_BYTES + raw
                size = len(raw)
            else:
                size = len(raw) - 1
        return raw, size
    denied(lambda: reader(runner).snapshot(KEY))
    assert len(fake.calls) == {"initial_identity": 2, "page": 3, "final_identity": 4}[stage]


def test_row_limit_and_snapshot_metadata_are_included_in_canonical_cap(monkeypatch):
    denied(lambda: reader(FakeCLI([matrix([["x" * 65536]])])).snapshot(KEY))
    source = reader()
    snapshot = source.snapshot(KEY)
    # The full TableSnapshot validation, not just the running sum of rows,
    # catches a boundary crossed solely by the canonical metadata envelope.
    monkeypatch.setattr("ostag.briefs.table_quality.MAX_SNAPSHOT_BYTES",
                        len(snapshot.records[0]._json) + 1)
    denied(lambda: reader().snapshot(KEY))


def test_unchanged_content_still_requires_a_fresh_complete_read():
    fake = FakeCLI(identities=[USER] * 4)
    port = TableReadPort(reader(fake).snapshot)
    first = port.snapshot(KEY)
    fake.offsets.clear()
    second = port.readback(RecordReference(KEY, "rec_0")).snapshot
    assert len(fake.calls) == 8 and second is not first
    assert first.content_sha256 == second.content_sha256
    assert second.observed_revision == "unknown"
