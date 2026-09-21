"""Trusted-host Docker stop observations, not maintenance authorization.

No product ingress calls this adapter. A host authorizer must bind the request
and hold its deployment/maintenance exclusion for the entire operation. Docker
state cannot prove absence of arbitrary host/manual writers or future starts.
Failures leave restart disabled/stopped state for explicit operator recovery;
the adapter never automatically restarts an old binary after possible migration.
"""

from dataclasses import dataclass, asdict
from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess


class ContainerStopDenied(RuntimeError):
    def __init__(self):
        super().__init__(
            "Container stop evidence is unavailable; inspect the managed maintenance operation."
        )


@dataclass(frozen=True)
class StopRequest:
    daemon_id: str
    container_id: str
    image_id: str
    started_at: str
    volume: str
    destination: str
    project: str
    service: str
    request_id: str

    @property
    def digest(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class StopObservation:
    request_sha256: str
    container_id: str
    image_id: str
    started_at: str
    finished_at: str
    exit_code: int

    @property
    def maintenance_authorized(self):
        return False


_INFO = '{"ID":{{json .ID}},"OSType":{{json .OSType}},"Swarm":{{json .Swarm.LocalNodeState}}}'
_INSPECT = (
    '{"Id":{{json .Id}},"Image":{{json .Image}},"State":{{json .State}},'
    '"Privileged":{{json .HostConfig.Privileged}},"PidMode":{{json .HostConfig.PidMode}},'
    '"AutoRemove":{{json .HostConfig.AutoRemove}},"RestartPolicy":{{json .HostConfig.RestartPolicy}},'
    '"Project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"Service":{{json (index .Config.Labels "com.docker.compose.service")}},"Mounts":{{json .Mounts}}}'
)
_VOLUME = (
    '{"Driver":{{json .Driver}},"Options":{{json .Options}},"Mountpoint":{{json .Mountpoint}}}'
)


class DockerCLI:
    """Explicit binary/context; inspect excludes environment variables/secrets."""

    def __init__(self, binary, *, context):
        if not Path(binary).is_absolute() or not re.fullmatch(r"[A-Za-z0-9_.-]+", context):
            raise ContainerStopDenied()
        self.prefix = [str(binary), "--context", context]

    def run(self, args, *, timeout):
        try:
            result = subprocess.run(
                self.prefix + args, capture_output=True, text=True, timeout=timeout, check=False
            )
            if result.returncode or len(result.stdout.encode()) > 2_000_000:
                raise ContainerStopDenied()
            return result.stdout.strip()
        except Exception:
            raise ContainerStopDenied() from None


def _timestamp(value):
    if type(value) is not str or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z", value
    ):
        raise ContainerStopDenied()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.year < 1970:
        raise ContainerStopDenied()
    return parsed


def _path(value):
    if type(value) is not str or not value.startswith("/") or ".." in value.split("/"):
        raise ContainerStopDenied()
    return PurePosixPath(value)


class ContainerStopper:
    def __init__(self, cli, *, authorize):
        if not callable(authorize):
            raise ContainerStopDenied()
        self.cli, self.authorize = cli, authorize

    def _request(self, request):
        if type(request) is not StopRequest:
            raise ContainerStopDenied()
        for value in (request.container_id, request.request_id):
            if type(value) is not str or not re.fullmatch("[0-9a-f]{64}", value):
                raise ContainerStopDenied()
        if not re.fullmatch("sha256:[0-9a-f]{64}", request.image_id):
            raise ContainerStopDenied()
        if type(request.daemon_id) is not str or not 1 <= len(request.daemon_id) <= 256:
            raise ContainerStopDenied()
        for value in (request.volume, request.project, request.service):
            if type(value) is not str or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}", value
            ):
                raise ContainerStopDenied()
        if _path(request.destination) == PurePosixPath("/"):
            raise ContainerStopDenied()
        _timestamp(request.started_at)
        self._authorize(request)

    def _authorize(self, request):
        if self.authorize(request) is not None:
            raise ContainerStopDenied()

    def _json(self, args):
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ContainerStopDenied()
                result[key] = value
            return result

        value = json.loads(self.cli.run(args, timeout=15), object_pairs_hook=unique)
        if type(value) is not dict:
            raise ContainerStopDenied()
        return value

    def _inventory(self, request):
        info = self._json(["info", "--format", _INFO])
        if info != dict(ID=request.daemon_id, OSType="linux", Swarm="inactive"):
            raise ContainerStopDenied()
        volume = self._json(["volume", "inspect", "--format", _VOLUME, request.volume])
        if volume["Driver"] != "local" or volume["Options"] not in (None, {}):
            raise ContainerStopDenied()
        source = _path(volume["Mountpoint"])
        ids = self.cli.run(
            ["container", "ls", "-a", "--no-trunc", "--format", "{{.ID}}"], timeout=15
        ).splitlines()
        if (
            not 1 <= len(ids) <= 256
            or len(set(ids)) != len(ids)
            or request.container_id not in ids
            or any(not re.fullmatch("[0-9a-f]{64}", value) for value in ids)
        ):
            raise ContainerStopDenied()
        target = None
        for identity in ids:
            row = self._json(["container", "inspect", "--format", _INSPECT, identity])
            if row["Id"] != identity or type(row["Mounts"]) is not list:
                raise ContainerStopDenied()
            if identity == request.container_id:
                target = row
            else:
                for mount in row["Mounts"]:
                    if mount.get("Type") == "tmpfs":
                        continue  # unrelated private memory has no host source
                    if mount.get("Type") not in ("bind", "volume"):
                        raise ContainerStopDenied()
                    other = _path(mount["Source"])
                    if (
                        mount.get("Name") == request.volume
                        or other == source
                        or source in other.parents
                        or other in source.parents
                    ):
                        raise ContainerStopDenied()  # unknown stopped readers also deny
        if (
            target["Image"] != request.image_id
            or target["Project"] != request.project
            or target["Service"] != request.service
            or target["Privileged"] is not False
            or target["PidMode"] != ""
            or target["AutoRemove"] is not False
        ):
            raise ContainerStopDenied()
        matching = [m for m in target["Mounts"] if m.get("Destination") == request.destination]
        if (
            len(matching) != 1
            or matching[0].get("Type") != "volume"
            or matching[0].get("Name") != request.volume
            or _path(matching[0]["Source"]) != source
            or matching[0].get("RW") is not True
        ):
            raise ContainerStopDenied()
        state = target["State"]
        if (
            state["StartedAt"] != request.started_at
            or state["Paused"] is not False
            or state["Restarting"] is not False
            or state["Dead"] is not False
            or state["Error"]
        ):
            raise ContainerStopDenied()
        return target

    def _observation(self, request, row):
        state = row["State"]
        if (
            row["RestartPolicy"] != dict(Name="no", MaximumRetryCount=0)
            or state["Running"] is not False
            or type(state["Pid"]) is not int
            or state["Pid"] != 0
            or state["Status"] != "exited"
            or type(state["ExitCode"]) is not int
            or not 0 <= state["ExitCode"] <= 255
            or _timestamp(state["FinishedAt"]) < _timestamp(request.started_at)
        ):
            raise ContainerStopDenied()
        return StopObservation(
            request.digest,
            request.container_id,
            request.image_id,
            request.started_at,
            state["FinishedAt"],
            state["ExitCode"],
        )

    def stop(self, request, *, grace_seconds=30):
        try:
            self._request(request)
            if type(grace_seconds) is not int or not 1 <= grace_seconds <= 60:
                raise ContainerStopDenied()
            row = self._inventory(request)
            if (
                row["State"]["Running"] is not True
                or row["State"]["Status"] != "running"
                or type(row["State"]["Pid"]) is not int
                or row["State"]["Pid"] <= 0
            ):
                raise ContainerStopDenied()
            self._authorize(request)
            self.cli.run(["container", "update", "--restart=no", request.container_id], timeout=15)
            if self._inventory(request)["RestartPolicy"] != dict(Name="no", MaximumRetryCount=0):
                raise ContainerStopDenied()
            self._authorize(request)
            self.cli.run(
                ["container", "stop", "--timeout", str(grace_seconds), request.container_id],
                timeout=grace_seconds + 15,
            )
            waited = self.cli.run(["container", "wait", request.container_id], timeout=15)
            if not re.fullmatch(r"\d{1,3}", waited):
                raise ContainerStopDenied()
            result = self._observation(request, self._inventory(request))
            if result.exit_code != int(waited):
                raise ContainerStopDenied()
            return result
        except Exception:
            raise ContainerStopDenied() from None

    def recheck(self, request, observation):
        try:
            self._request(request)
            if (
                type(observation) is not StopObservation
                or self._observation(request, self._inventory(request)) != observation
            ):
                raise ContainerStopDenied()
        except Exception:
            raise ContainerStopDenied() from None
