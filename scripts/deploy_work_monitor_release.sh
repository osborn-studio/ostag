#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage:
  scripts/deploy_work_monitor_release.sh preflight
  scripts/deploy_work_monitor_release.sh deploy RELEASE_SHA SOURCE_ARCHIVE SOURCE_SHA256 IMAGE_ARCHIVE IMAGE_SHA256

The deploy command installs one immutable work-monitor release on the production
host. Runtime paths can be overridden with OSTAG_RELEASE_ROOT,
OSTAG_PRODUCTION_ENV_FILE, OSTAG_COMPOSE_PROJECT, or OSTAG_LOCAL_HEALTH_URL.
EOF
  exit 64
}

command_name="${1:-}"
[[ -n "$command_name" ]] || usage
shift

fail() {
  echo "production deploy refused: $*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command is unavailable: $1"
}

current_container() {
  local containers=()
  mapfile -t containers < <(
    docker ps \
      --filter "label=com.docker.compose.project=$compose_project" \
      --filter "label=com.docker.compose.service=$service_name" \
      --format '{{.ID}}'
  )
  [[ "${#containers[@]}" -eq 1 ]] ||
    fail "expected exactly one running $compose_project/$service_name container"
  printf '%s\n' "${containers[0]}"
}

check_environment_file() {
  [[ -f "$production_env_file" ]] || fail "production environment file is missing"
  local mode
  mode="$(stat -c '%a' "$production_env_file")"
  [[ "$mode" == "600" ]] || fail "production environment file must have mode 0600"
}

check_current_service() {
  local container health
  container="$(current_container)"
  health="$(docker inspect "$container" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}')"
  [[ "$health" == "healthy" ]] || fail "current production container is not healthy"
  curl --fail --silent --show-error --max-time 15 "$local_health_url" >/dev/null ||
    fail "current production health endpoint is unavailable"
  printf '%s\n' "$container"
}

check_database_integrity() {
  local container="$1"
  docker exec -i "$container" python - "$database_path" <<'PY'
import sqlite3
import sys

database = sys.argv[1]
with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
    result = connection.execute("PRAGMA integrity_check").fetchone()
if result != ("ok",):
    raise SystemExit("production database integrity check failed")
print("database integrity: ok")
PY
}

runtime_preflight() {
  require_command docker
  require_command curl
  require_command gzip
  require_command python3
  require_command sha256sum
  require_command tar
  docker compose version >/dev/null
  [[ -d "$release_root" && ! -L "$release_root" ]] ||
    fail "release root must be an existing real directory"
  check_environment_file
  local container
  container="$(check_current_service)"
  check_database_integrity "$container"
  echo "production deploy preflight passed"
  echo "container=$container"
  echo "release_root=$release_root"
  echo "health_url=$local_health_url"
}

verify_archive() {
  local archive="$1"
  local expected_sha="$2"
  local label="$3"
  [[ -f "$archive" && ! -L "$archive" ]] || fail "$label archive is missing or unsafe"
  [[ "$expected_sha" =~ ^[0-9a-f]{64}$ ]] || fail "$label checksum is malformed"
  local actual_sha
  actual_sha="$(sha256sum "$archive" | awk '{print $1}')"
  [[ "$actual_sha" == "$expected_sha" ]] || fail "$label archive checksum mismatch"
  gzip --test "$archive"
}

verify_source_members() {
  local source_archive="$1"
  python3 - "$source_archive" <<'PY'
from pathlib import PurePosixPath
import sys
import tarfile

with tarfile.open(sys.argv[1], "r:gz") as archive:
    members = archive.getmembers()
    if len(members) > 100_000:
        raise SystemExit("source archive contains too many members")
    if sum(member.size for member in members) > 1024 * 1024 * 1024:
        raise SystemExit("source archive expands beyond one GiB")
    for member in members:
        name = PurePosixPath(member.name)
        if name.is_absolute() or ".." in name.parts:
            raise SystemExit(f"unsafe source archive member: {member.name}")
        if member.issym() or member.islnk():
            link = PurePosixPath(member.linkname)
            if link.is_absolute() or ".." in link.parts:
                raise SystemExit(f"unsafe source archive link: {member.name}")
print("source archive paths: safe")
PY
}

install_release_tree() {
  local release_sha="$1"
  local source_archive="$2"
  local release_dir="$release_root/$release_sha"
  local marker="$release_dir/.ostag-release-sha"

  if [[ -e "$release_dir" || -L "$release_dir" ]]; then
    [[ -d "$release_dir" && ! -L "$release_dir" && -f "$marker" ]] ||
      fail "existing release path is not a verified release directory"
    [[ "$(tr -d '\r\n' < "$marker")" == "$release_sha" ]] ||
      fail "existing release directory belongs to another commit"
  else
    local staging_dir
    staging_dir="$(mktemp -d "$release_root/.${release_sha}.XXXXXX")"
    tar -xzf "$source_archive" -C "$staging_dir"
    printf '%s\n' "$release_sha" > "$staging_dir/.ostag-release-sha"
    mv "$staging_dir" "$release_dir"
  fi

  local env_link="$release_dir/deploy/work-monitor/.env"
  mkdir -p "$(dirname "$env_link")"
  if [[ -L "$env_link" ]]; then
    [[ "$(readlink -f "$env_link")" == "$(readlink -f "$production_env_file")" ]] ||
      fail "release environment link points to an unexpected file"
  elif [[ -e "$env_link" ]]; then
    fail "release environment path exists and is not a symlink"
  else
    ln -s "$production_env_file" "$env_link"
  fi

  printf '%s\n' "$release_dir"
}

create_database_backup() {
  local container="$1"
  local release_sha="$2"
  local timestamp backup_path
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  backup_path="$backup_dir/work_monitor-before-${release_sha:0:12}-$timestamp.sqlite3"

  docker exec -i "$container" python - "$database_path" "$backup_path" <<'PY'
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import sys
import tempfile

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
if destination.exists() or destination.is_symlink():
    raise SystemExit("backup destination already exists")

fd, temporary_text = tempfile.mkstemp(prefix=f".{destination.name}.tmp.", dir=destination.parent)
os.close(fd)
temporary = Path(temporary_text)
try:
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_connection:
        if source_connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise SystemExit("source database integrity check failed")
        with sqlite3.connect(temporary) as destination_connection:
            source_connection.backup(destination_connection)
        with sqlite3.connect(f"file:{temporary}?mode=ro", uri=True) as verification:
            if verification.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise SystemExit("backup database integrity check failed")
    os.chmod(temporary, 0o600)
    os.replace(temporary, destination)
finally:
    temporary.unlink(missing_ok=True)

print(destination)
PY
}

write_receipt() {
  local receipt_path="$1"
  local status="$2"
  local release_sha="$3"
  local image="$4"
  local previous_image="$5"
  local previous_compose_file="$6"
  local backup_path="$7"
  python3 - "$receipt_path" "$status" "$release_sha" "$image" \
    "$previous_image" "$previous_compose_file" "$backup_path" <<'PY'
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile

path = Path(sys.argv[1])
record = {
    "schemaVersion": 1,
    "status": sys.argv[2],
    "releaseSha": sys.argv[3],
    "image": sys.argv[4],
    "previousImage": sys.argv[5],
    "previousComposeFile": sys.argv[6],
    "backupPath": sys.argv[7],
    "recordedAt": datetime.now(timezone.utc).isoformat(),
}
fd, temporary_text = tempfile.mkstemp(prefix=f".{path.name}.tmp.", dir=path.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(record, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary_text, path)
finally:
    Path(temporary_text).unlink(missing_ok=True)
PY
}

rollback_previous_release() {
  local previous_image="$1"
  local previous_compose_file="$2"
  echo "new release failed; restoring previous image" >&2
  OSTAG_WORK_MONITOR_IMAGE="$previous_image" \
    docker compose -p "$compose_project" -f "$previous_compose_file" \
      up -d --no-build --wait --wait-timeout 180
  curl --fail --silent --show-error --max-time 15 "$local_health_url" >/dev/null
}

deploy_release() {
  local release_sha="$1"
  local source_archive="$2"
  local source_sha256="$3"
  local image_archive="$4"
  local image_sha256="$5"

  runtime_preflight
  verify_archive "$source_archive" "$source_sha256" source
  verify_archive "$image_archive" "$image_sha256" image
  verify_source_members "$source_archive"

  local release_dir image compose_file receipt_path
  release_dir="$(install_release_tree "$release_sha" "$source_archive")"
  image="ostag-work-monitor:sha-${release_sha:0:12}"
  compose_file="$release_dir/deploy/work-monitor/docker-compose.yml"
  receipt_path="$release_dir/deploy-receipt.json"
  [[ -f "$compose_file" ]] || fail "release compose file is missing"

  gzip -dc "$image_archive" | docker load >/dev/null
  docker image inspect "$image" >/dev/null

  local container previous_image previous_compose_file backup_path
  container="$(check_current_service)"
  previous_image="$(docker inspect "$container" --format '{{.Config.Image}}')"
  previous_compose_file="$(
    docker inspect "$container" \
      --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'
  )"
  [[ -f "$previous_compose_file" ]] || fail "previous compose file is unavailable"
  backup_path="$(create_database_backup "$container" "$release_sha" | tail -1)"

  OSTAG_WORK_MONITOR_IMAGE="$image" \
    docker compose -p "$compose_project" -f "$compose_file" config >/dev/null
  write_receipt "$receipt_path" ready "$release_sha" "$image" \
    "$previous_image" "$previous_compose_file" "$backup_path"

  local deploy_status=0
  OSTAG_WORK_MONITOR_IMAGE="$image" \
    docker compose -p "$compose_project" -f "$compose_file" \
      up -d --no-build --wait --wait-timeout 180 || deploy_status=$?
  if [[ "$deploy_status" -eq 0 ]]; then
    curl --fail --silent --show-error --max-time 15 "$local_health_url" >/dev/null ||
      deploy_status=$?
  fi

  if [[ "$deploy_status" -ne 0 ]]; then
    local rollback_status=0 receipt_status=rolled_back
    rollback_previous_release "$previous_image" "$previous_compose_file" || rollback_status=$?
    if [[ "$rollback_status" -ne 0 ]]; then
      receipt_status=rollback_failed
    fi
    write_receipt "$receipt_path" "$receipt_status" "$release_sha" "$image" \
      "$previous_image" "$previous_compose_file" "$backup_path"
    fail "new release did not become healthy; rollback status: $receipt_status"
  fi

  local deployed_container deployed_image_id expected_image_id verify_status=0
  set +e
  deployed_container="$(check_current_service)"
  verify_status=$?
  set -e
  if [[ "$verify_status" -eq 0 ]]; then
    deployed_image_id="$(docker inspect "$deployed_container" --format '{{.Image}}')"
    expected_image_id="$(docker image inspect "$image" --format '{{.Id}}')"
    if [[ "$deployed_image_id" != "$expected_image_id" ]]; then
      verify_status=1
      echo "healthy container does not use the expected immutable image" >&2
    fi
  fi
  if [[ "$verify_status" -ne 0 ]]; then
    local rollback_status=0 receipt_status=rolled_back
    rollback_previous_release "$previous_image" "$previous_compose_file" || rollback_status=$?
    if [[ "$rollback_status" -ne 0 ]]; then
      receipt_status=rollback_failed
    fi
    write_receipt "$receipt_path" "$receipt_status" "$release_sha" "$image" \
      "$previous_image" "$previous_compose_file" "$backup_path"
    fail "deployed image verification failed; rollback status: $receipt_status"
  fi

  write_receipt "$receipt_path" deployed "$release_sha" "$image" \
    "$previous_image" "$previous_compose_file" "$backup_path"
  rm -f "$source_archive" "$image_archive"
  echo "production deploy complete"
  echo "release_sha=$release_sha"
  echo "image=$image"
  echo "backup=$backup_path"
  echo "receipt=$receipt_path"
}

# Only this continuation is handed to Bash after acquisition. There is no reentry
# argument or environment marker that can bypass the lock on a fresh invocation.
locked_main() {
  release_root="${OSTAG_RELEASE_ROOT:-/opt/ostag-releases}"
  production_env_file="${OSTAG_PRODUCTION_ENV_FILE:-/opt/ostag-work-monitor/deploy/work-monitor/.env}"
  compose_project="${OSTAG_COMPOSE_PROJECT:-work-monitor}"
  service_name="work-monitor"
  local_health_url="${OSTAG_LOCAL_HEALTH_URL:-http://127.0.0.1:8000/healthz}"
  database_path="/var/lib/ostag/work_monitor.sqlite3"
  backup_dir="/var/lib/ostag/backups"

  local command_name="$1"
  shift
  case "$command_name" in
    preflight) runtime_preflight ;;
    deploy) deploy_release "$@" ;;
  esac
}

# Validate syntax before acquiring the lock or inspecting any production state.
case "$command_name" in
  preflight)
    [[ "$#" -eq 0 ]] || usage
    ;;
  deploy)
    [[ "$#" -eq 5 ]] || usage
    [[ "$1" =~ ^[0-9a-f]{40}$ ]] || fail "release SHA must be 40 lowercase hex characters"
    [[ "$3" =~ ^[0-9a-f]{64}$ ]] || fail "source checksum is malformed"
    [[ "$5" =~ ^[0-9a-f]{64}$ ]] || fail "image checksum is malformed"
    ;;
  *) usage ;;
esac

require_command python3
# exec preserves the parent PID across Python -> Bash. The inheritable descriptor
# owns the kernel lock throughout the continuation and its runtime children,
# including internal rollback. Never unlink or truncate this file.
exec python3 - "$BASH" -c "$(declare -f)
set -euo pipefail
locked_main \"\$@\"" "$0" "$command_name" "$@" <<'PY'
import fcntl
import json
import os
import re
import stat
import sys

try:
    path = os.environ.get("OSTAG_DEPLOY_LOCK_PATH", "/run/ostag-work-monitor-deploy.lock")
    parts = path.split("/")
    if not path.startswith("/") or any(part in ("", ".", "..") for part in parts[1:]):
        raise ValueError("invalid path")
    uid = os.geteuid()
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    # Walk pinned directory descriptors, never following a symlink. Root-owned
    # sticky ancestors allow a private test directory under /tmp; the immediate
    # parent must still be protected from group/other writes.
    for index, part in enumerate(parts[1:]):
        metadata = os.fstat(directory)
        sticky_ancestor = (
            index < len(parts) - 2
            and metadata.st_uid == 0
            and metadata.st_mode & stat.S_ISVTX
        )
        if metadata.st_uid not in (0, uid) or (
            metadata.st_mode & 0o022 and not sticky_ancestor
        ):
            raise ValueError("untrusted directory")
        if index == len(parts) - 2:
            try:
                descriptor = os.open(
                    part, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
                    0o600, dir_fd=directory,
                )
            except FileExistsError:
                descriptor = os.open(part, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                                     dir_fd=directory)
            else:
                os.fchmod(descriptor, 0o600)
        else:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != uid
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ValueError("untrusted lock file")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("production deploy refused: deployment lock is busy")
    current = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
    if (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino):
        raise ValueError("lock identity changed")
    os.close(directory)
    # Persistent state is deliberately required even on preflight. It is never
    # initialized here: a missing file is not evidence maintenance completed.
    # The installed host launcher must pin this path for every invocation.
    state_directory = None
    state_descriptor = None
    try:
        state_path = os.environ.get("OSTAG_MAINTENANCE_STATE_PATH", "/var/lib/ostag-maintenance/work-monitor.json")
        state_parts = state_path.split("/")
        if not state_path.startswith("/") or any(p in ("", ".", "..") for p in state_parts[1:]):
            raise ValueError("invalid state path")
        state_directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        for index, part in enumerate(state_parts[1:]):
            metadata = os.fstat(state_directory)
            sticky = index < len(state_parts)-2 and metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX
            if metadata.st_uid not in (0, uid) or (metadata.st_mode & 0o022 and not sticky):
                raise ValueError("untrusted state directory")
            if index == len(state_parts)-2:
                state_descriptor = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=state_directory)
            else:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=state_directory)
                os.close(state_directory)
                state_directory = child
        metadata = os.fstat(state_descriptor)
        current = os.stat(state_parts[-1], dir_fd=state_directory, follow_symlinks=False)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != uid or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600 or not 0 < metadata.st_size <= 4096
                or (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino)):
            raise ValueError("unsafe state")
        raw = os.read(state_descriptor, 4097)
        if len(raw) != metadata.st_size:
            raise ValueError("changed state")
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate state key")
                result[key] = value
            return result
        state = json.loads(raw, object_pairs_hook=unique)
        if (type(state) is not dict or set(state) != {"schema_version","generation","state","maintenance_id","request_sha256"}
                or type(state["schema_version"]) is not int or state["schema_version"] != 1
                or type(state["generation"]) is not int or state["generation"] < 0):
            raise ValueError("invalid maintenance state")
        if state["state"] == "sealed":
            if state["generation"] < 1 or any(type(state[k]) is not str or not re.fullmatch("[0-9a-f]{64}", state[k]) for k in ("maintenance_id", "request_sha256")):
                raise ValueError("invalid seal")
            raise SystemExit("production deploy refused: maintenance sealed")
        if state["state"] != "open" or state["maintenance_id"] is not None or state["request_sha256"] is not None:
            raise ValueError("invalid open state")
    except Exception:
        raise SystemExit("production deploy refused: maintenance state unavailable")
    finally:
        if state_descriptor is not None:
            os.close(state_descriptor)
        if state_directory is not None:
            os.close(state_directory)
    os.set_inheritable(descriptor, True)
    os.execv(sys.argv[1], sys.argv[1:])
except Exception:
    # Do not disclose paths, environment values, or exception details.
    raise SystemExit("production deploy refused: deployment lock is unavailable or unsafe")
PY
