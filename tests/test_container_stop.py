"""Container exit evidence is not authority for ledger maintenance."""

import copy
import json

import pytest


def setup():
    from ostag.core.container_stop import ContainerStopper, StopRequest

    request = StopRequest(
        daemon_id="daemon-test",
        container_id="a" * 64,
        image_id="sha256:" + "b" * 64,
        started_at="2026-09-21T00:00:00Z",
        volume="owned-data",
        destination="/var/lib/ostag",
        project="owned-test",
        service="work-monitor",
        request_id="c" * 64,
    )
    cli = FakeDocker()
    authorizations = []
    stopper = ContainerStopper(cli, authorize=lambda value: authorizations.append(value))
    return request, cli, stopper, authorizations


class FakeDocker:
    def __init__(self):
        self.calls = []
        self.other = False
        self.wait_result = "0"
        self.stop_effect = True
        self.daemon = "daemon-test"
        self.row = dict(
            Id="a" * 64,
            Image="sha256:" + "b" * 64,
            State=dict(
                Running=True,
                Paused=False,
                Restarting=False,
                Dead=False,
                Pid=42,
                Status="running",
                StartedAt="2026-09-21T00:00:00Z",
                FinishedAt="0001-01-01T00:00:00Z",
                ExitCode=0,
                Error="",
            ),
            Privileged=False,
            PidMode="",
            AutoRemove=False,
            RestartPolicy=dict(Name="unless-stopped", MaximumRetryCount=0),
            Project="owned-test",
            Service="work-monitor",
            Mounts=[
                dict(
                    Type="volume",
                    Name="owned-data",
                    Source="/var/lib/docker/volumes/owned-data/_data",
                    Destination="/var/lib/ostag",
                    RW=True,
                )
            ],
        )

    def run(self, args, *, timeout):
        self.calls.append(tuple(args))
        if args[0] == "info":
            return json.dumps(dict(ID=self.daemon, OSType="linux", Swarm="inactive"))
        if args[:2] == ["volume", "inspect"]:
            return json.dumps(
                dict(
                    Driver="local",
                    Options=None,
                    Mountpoint="/var/lib/docker/volumes/owned-data/_data",
                )
            )
        if args[:2] == ["container", "ls"]:
            return "a" * 64 + ("\n" + "d" * 64 if self.other else "")
        if args[:2] == ["container", "inspect"]:
            row = copy.deepcopy(self.row)
            if args[-1] == "d" * 64:
                row["Id"] = "d" * 64
            return json.dumps(row)
        if args[:2] == ["container", "update"]:
            self.row["RestartPolicy"]["Name"] = "no"
            return "a" * 64
        if args[:2] == ["container", "stop"]:
            if self.stop_effect:
                self.row["State"].update(
                    Running=False, Pid=0, Status="exited", FinishedAt="2026-09-21T00:00:01Z"
                )
            return "a" * 64
        if args[:2] == ["container", "wait"]:
            return self.wait_result
        raise AssertionError(args)


def test_stop_requires_authorization_then_seals_waits_and_inspects():
    request, cli, stopper, authorized = setup()
    result = stopper.stop(request)
    assert authorized == [request, request, request]
    names = [c[1] for c in cli.calls if c[0] == "container"]
    assert names.index("update") < names.index("stop") < names.index("wait")
    assert names[-1] == "inspect"
    assert result.container_id == request.container_id
    assert result.maintenance_authorized is False
    assert result.exit_code == 0
    stopper.recheck(request, result)


def test_authorization_denial_performs_no_docker_calls():
    from ostag.core.container_stop import ContainerStopper, ContainerStopDenied

    request, cli, _, _ = setup()

    def deny(_):
        raise RuntimeError("private authorizer failure")

    with pytest.raises(ContainerStopDenied) as error:
        ContainerStopper(cli, authorize=deny).stop(request)
    assert "private" not in str(error.value)
    assert cli.calls == []


@pytest.mark.parametrize(
    "damage",
    [
        "daemon",
        "image",
        "started",
        "volume",
        "destination",
        "project",
        "privileged",
        "pidmode",
        "autoremove",
        "other_writer",
    ],
)
def test_changed_or_unmanaged_domain_denies_before_any_mutation(damage):
    from ostag.core.container_stop import ContainerStopDenied

    request, cli, stopper, _ = setup()
    if damage == "daemon":
        cli.daemon = "another"
    elif damage == "image":
        cli.row["Image"] = "sha256:" + "f" * 64
    elif damage == "started":
        cli.row["State"]["StartedAt"] = "2026-09-20T00:00:00Z"
    elif damage == "volume":
        cli.row["Mounts"][0]["Name"] = "other"
    elif damage == "destination":
        cli.row["Mounts"][0]["Destination"] = "/other"
    elif damage == "project":
        cli.row["Project"] = "other"
    elif damage == "privileged":
        cli.row["Privileged"] = True
    elif damage == "pidmode":
        cli.row["PidMode"] = "host"
    elif damage == "autoremove":
        cli.row["AutoRemove"] = True
    else:
        cli.other = True
    with pytest.raises(ContainerStopDenied):
        stopper.stop(request)
    assert not any(c[:2] in (("container", "stop"), ("container", "update")) for c in cli.calls)


@pytest.mark.parametrize(
    "damage", ["still_running", "wait_mismatch", "bad_wait", "missing_exit_time"]
)
def test_incomplete_exit_never_returns_observation(damage):
    from ostag.core.container_stop import ContainerStopDenied

    request, cli, stopper, _ = setup()
    if damage == "still_running":
        cli.stop_effect = False
    elif damage == "wait_mismatch":
        cli.wait_result = "137"
    elif damage == "bad_wait":
        cli.wait_result = "private failure"
    else:
        original = cli.run

        def run(args, **kwargs):
            result = original(args, **kwargs)
            if args[:2] == ["container", "wait"]:
                cli.row["State"]["FinishedAt"] = "0001-01-01T00:00:00Z"
            return result

        cli.run = run
    with pytest.raises(ContainerStopDenied):
        stopper.stop(request)
    assert not any(c[:2] == ("container", "start") for c in cli.calls)


def test_saved_observation_cannot_hide_restart_or_new_volume_writer():
    from ostag.core.container_stop import ContainerStopDenied

    request, cli, stopper, _ = setup()
    result = stopper.stop(request)
    cli.other = True
    with pytest.raises(ContainerStopDenied):
        stopper.recheck(request, result)
    cli.other = False
    cli.row["State"]["FinishedAt"] = "2026-09-21T00:00:02Z"
    with pytest.raises(ContainerStopDenied):
        stopper.recheck(request, result)


@pytest.mark.parametrize("deny_at", [2, 3])
def test_authorization_rechecked_before_each_mutation(deny_at):
    from ostag.core.container_stop import ContainerStopper, ContainerStopDenied

    request, cli, _, _ = setup()
    calls = []

    def authorize(value):
        calls.append(value)
        if len(calls) == deny_at:
            raise PermissionError("revoked")

    with pytest.raises(ContainerStopDenied):
        ContainerStopper(cli, authorize=authorize).stop(request)
    assert not any(c[:2] == ("container", "stop") for c in cli.calls)
    assert cli.row["RestartPolicy"]["Name"] == ("unless-stopped" if deny_at == 2 else "no")


@pytest.mark.parametrize("phase", ["update", "stop", "wait"])
def test_docker_failure_is_sanitized_and_never_triggers_automatic_retry(phase):
    from ostag.core.container_stop import ContainerStopDenied

    request, cli, stopper, _ = setup()
    original = cli.run
    attempted = []

    def fail(args, **kwargs):
        if args[:2] == ["container", phase]:
            attempted.append(phase)
            raise RuntimeError("private backend diagnostic")
        return original(args, **kwargs)

    cli.run = fail
    with pytest.raises(ContainerStopDenied) as error:
        stopper.stop(request)
    assert "private" not in str(error.value)
    assert attempted == [phase]
    assert not any(c[:2] == ("container", "start") for c in cli.calls)


def test_cli_inspection_excludes_environment_and_cli_error_is_sanitized(monkeypatch):
    import subprocess
    from ostag.core.container_stop import DockerCLI, ContainerStopDenied, _INSPECT

    assert "Config.Env" not in _INSPECT

    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1, output="private", stderr="private")

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(ContainerStopDenied) as error:
        DockerCLI("/synthetic/docker", context="test").run(["info"], timeout=1)
    assert "private" not in str(error.value)


def test_unrelated_tmpfs_mount_is_not_mistaken_for_host_volume():
    request, cli, stopper, _ = setup()
    original = cli.run
    cli.other = True

    def read(args, **kwargs):
        value = original(args, **kwargs)
        if args[:2] == ["container", "inspect"] and args[-1] == "d" * 64:
            row = json.loads(value)
            row["Mounts"] = [dict(Type="tmpfs", Destination="/cache")]
            return json.dumps(row)
        return value

    cli.run = read
    assert stopper.stop(request).maintenance_authorized is False


@pytest.mark.parametrize(
    "started", ["2026-09-21Z", "2026-09-21T00:00:00+00:00", "0001-01-01T00:00:00Z"]
)
def test_malformed_or_zero_start_time_denied_without_docker(started):
    from dataclasses import replace
    from ostag.core.container_stop import ContainerStopDenied

    request, cli, stopper, _ = setup()
    with pytest.raises(ContainerStopDenied):
        stopper.stop(replace(request, started_at=started))
    assert cli.calls == []
