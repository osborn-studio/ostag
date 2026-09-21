# Container stop evidence for offline ledger maintenance

Status: trusted-host adapter under development. No production command, forced-SSH
wrapper permission, data migration, automatic resume or maintenance authorization
is installed by this module. Existing product startup is unchanged.

`ContainerStopper` accepts an exact request bound to Docker daemon identity,
full container ID, immutable image ID, original start timestamp, Compose
project/service labels, local named volume/destination and an operator request ID.
A trusted host authorizer must validate that request and maintain deployment
exclusion throughout the call. This callback is host code, never a client-supplied
boolean. It is checked on admission and immediately before each mutation.

The adapter checks the standalone Linux daemon, private PID namespace,
non-privileged/non-auto-remove container, local volume with no driver options,
and all current container mounts. Any other container holding that volume or an
overlapping host source path denies admission, even if stopped or read-only.
Missing or malformed inspection is not proof of absence.

The sequence is: disable automatic restart, verify the new policy, stop with a
bounded grace period, obtain Docker's exit status through `wait`, then inspect
the exact domain again. An observation requires an exited container, PID zero,
nonzero finished timestamp, no paused/restarting/dead/error state, restart policy
`no`, and agreement between the wait status and inspected exit code. Graceful
exit is not required: forced exit is recorded as such, with no claim of an
application-level clean drain. Unknown external effects remain unknown.

`recheck` rejects manual restart, changed bindings or a new overlapping container.
These are sequential observations, not an atomic snapshot, lock or saved
authorization token. A
`StopObservation` always reports `maintenance_authorized=False`. It cannot prove
absence of arbitrary host/manual writers, block future Docker starts, validate
ledger content or authorize replacing it. A future maintenance controller must
hold the trusted deployment guard, seal all managed start paths, bind the complete
deployment scope and validate the ledger generation before rewriting anything.

Failures never trigger blind retry or restart. In particular, timeout does not
prove that a Docker operation failed to take effect. The container may remain
running with restart disabled, or may already be stopped; the host must inspect
and recover explicitly. The adapter never restarts an old image that could be
incompatible with partially migrated data. CLI error text is suppressed; inspect
templates omit container environment variables. Command timeouts are bounded;
the current CLI wrapper checks output size after capture, not as a streaming cap.

Tests include synthetic failure injection and a separate isolated Docker probe
with parent and child processes repeatedly writing a test volume while ignoring
SIGTERM. The probe verifies forced exit, stable file size, rejection of an unknown
volume holder, and invalidation after a manual restart. Those are local container
observations, not production supervisor or complete retention acceptance.

Docker's official contracts distinguish [stop](https://docs.docker.com/reference/cli/docker/container/stop/),
[wait](https://docs.docker.com/reference/cli/docker/container/wait/), and
[restart-policy update](https://docs.docker.com/reference/cli/docker/container/update/).
Disabling automatic restart does not disable manual start or a separate process
manager; see [restart policies](https://docs.docker.com/engine/containers/start-containers-automatically/).
