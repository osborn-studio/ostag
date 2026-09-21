"""Explicit host policy for coordinated ledger domains; never installs locks.

This closes cooperating product constructor paths. It does not prove that old
binaries/manual writers stopped, and does not authorize maintenance/replacement.
"""
import json
from pathlib import Path

from ostag.core.ledger_coordination import LedgerCoordination
from ostag.core.run_ledger import RunLedger
from ostag.core.schemas import SchemaError


def validate_managed_paths(paths):
    if type(paths) is not tuple or any(not isinstance(p, Path) for p in paths):
        raise ValueError('Managed ledger paths must be a tuple of absolute Paths.')
    if (len(set(paths)) != len(paths)
            or any(not p.is_absolute() or '..' in p.parts for p in paths)):
        raise ValueError('Managed ledger paths must be unique absolute direct paths.')
    return paths


def parse_managed_paths(raw):
    try:
        values = json.loads(raw)
        if type(values) is not list or any(type(v) is not str or not v for v in values):
            raise ValueError
        return validate_managed_paths(tuple(Path(v) for v in values))
    except (ValueError, TypeError):
        raise ValueError('OSTAG_MANAGED_LEDGER_PATHS must be a JSON array of unique absolute paths.') from None


def _denied():
    return SchemaError('ledger_coordination_unavailable', 'Managed ledger admission failed.',
                       'Verify the configured domains and installed locks; do not fall back to legacy writes.')


def configured_ledger(path, *, managed_paths=(), clock=None, existing=None):
    paths = validate_managed_paths(managed_paths)
    if not paths:
        return existing if existing is not None else RunLedger(path, clock=clock)
    target = Path(path).absolute()
    # A symlink alias must not evade membership by looking like another path.
    # Do not resolve it away: the approved domain names must remain direct.
    if '..' in target.parts or any(p.is_symlink() for p in (target, *target.parents)):
        raise _denied()
    domains = {p: LedgerCoordination.open(p) for p in paths}
    required = domains.get(target)
    if existing is not None:
        if (type(existing) is not RunLedger or existing.path.absolute() != target
                or existing.coordination != required):
            raise _denied()
        return existing
    return RunLedger(target, clock=clock, coordination=required)
