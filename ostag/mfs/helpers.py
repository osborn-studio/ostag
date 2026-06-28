from __future__ import annotations

from ostag.config import Settings
from ostag.core.safety import validate_scope


def allowed_mfs_scopes(config: Settings) -> tuple[str, ...]:
    return tuple(scope for scope in config.mfs_allowed_scopes if scope.strip())


def build_mfs_search_command(config: Settings, query: str, scope: str) -> list[str]:
    require_allowed_scope(scope, config)
    return [
        config.mfs_bin,
        "search",
        query,
        scope,
        "--limit",
        str(config.mfs_max_results),
    ]


def build_mfs_cat_command(config: Settings, uri: str) -> list[str]:
    require_allowed_scope(uri, config)
    return [config.mfs_bin, "cat", uri]


def require_allowed_scope(scope_or_uri: str, config: Settings) -> None:
    if not validate_scope(scope_or_uri, allowed_mfs_scopes(config)):
        allowed = ", ".join(allowed_mfs_scopes(config))
        raise ValueError(f"MFS scope is not allowlisted: {scope_or_uri}; allowed={allowed}")
