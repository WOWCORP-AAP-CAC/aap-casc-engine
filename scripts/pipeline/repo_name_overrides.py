#!/usr/bin/env python3
"""Fail-closed repository-name and tenant-key validation."""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any


TENANT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
DEFAULT_TENANT_FOLDERS = (
    "projects",
    "credentials",
    "inventories",
    "templates",
    "workflows",
    "schedules",
    "notifications",
)


def _as_nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def validate_tenant_id(value: Any) -> str:
    """Return the canonical safe tenant key or fail closed."""
    if not isinstance(value, str) or not value:
        raise ValueError("tenant_id must be a non-empty string")
    if not TENANT_ID_PATTERN.fullmatch(value):
        raise ValueError(
            "tenant_id must match ^[a-z][a-z0-9_]*$ and contain at most 64 characters"
        )
    return value


def normalize_platform_repo_names(folders: list[str], raw: Any) -> dict[str, str]:
    """Normalize mapping-only platform repository overrides."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("platform_repo_names must be a folder-to-repository mapping")
    unknown = sorted(set(raw) - set(folders))
    if unknown:
        raise ValueError(
            "platform_repo_names contains unknown folder keys: "
            + ", ".join(unknown)
            + f"; allowed={folders}"
        )
    normalized: dict[str, str] = {}
    for folder, name in raw.items():
        key = _as_nonempty_string(folder, "platform_repo_names key")
        if key not in folders:
            raise ValueError(f"platform_repo_names key '{key}' is not a valid folder")
        normalized[key] = _as_nonempty_string(name, f"platform_repo_names['{key}']")
    return normalized


def apply_platform_repo_names(
    default_repos: list[dict[str, Any]], overrides: dict[str, str]
) -> list[dict[str, Any]]:
    """Apply validated overrides and reject duplicate resulting names."""
    result: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    for entry in default_repos:
        folder = entry["folder"]
        name = _as_nonempty_string(
            overrides.get(folder, entry["name"]), f"platform repo name for {folder}"
        )
        if name in seen:
            raise ValueError(
                f"duplicate platform repo name '{name}' for folders {seen[name]} and {folder}"
            )
        seen[name] = folder
        merged = dict(entry)
        merged["name"] = name
        result.append(merged)
    return result


def normalize_tenant_repo_names(
    *, repo_pattern: str, repo_name: str = "", repo_names: Any = None
) -> tuple[str, dict[str, str]]:
    """Validate the scalar combined override or mapping per-resource overrides."""
    if repo_pattern not in ("combined", "per-resource-type"):
        raise ValueError("repo_pattern must be combined or per-resource-type")
    combined_name = (repo_name or "").strip()
    if repo_names is None:
        repo_names = {}
    if not isinstance(repo_names, dict):
        raise ValueError("repo_names must be a resource-folder-to-repository mapping")

    unknown = sorted(set(repo_names) - set(DEFAULT_TENANT_FOLDERS))
    if unknown:
        raise ValueError(
            "repo_names contains unknown folder keys: "
            + ", ".join(unknown)
            + f"; allowed={list(DEFAULT_TENANT_FOLDERS)}"
        )
    normalized = {
        _as_nonempty_string(folder, "repo_names key"): _as_nonempty_string(
            name, f"repo_names['{folder}']"
        )
        for folder, name in repo_names.items()
    }

    if repo_pattern == "combined":
        if normalized:
            raise ValueError("repo_names is only valid for per-resource-type repo_pattern")
        return combined_name, {}
    if combined_name:
        raise ValueError("repo_name is only valid for combined repo_pattern")
    return "", normalized


def resolve_tenant_repo_map(
    *,
    repo_pattern: str,
    tenant_id: str,
    repo_name: str = "",
    repo_names: Any = None,
) -> dict[str, str]:
    """Return the effective resource-folder-to-repository map."""
    tenant = validate_tenant_id(tenant_id)
    combined_name, overrides = normalize_tenant_repo_names(
        repo_pattern=repo_pattern,
        repo_name=repo_name,
        repo_names=repo_names,
    )
    if repo_pattern == "combined":
        name = combined_name or f"casc-tenant-{tenant}"
        return {folder: name for folder in DEFAULT_TENANT_FOLDERS}

    resolved = {
        folder: overrides.get(folder, f"controller-{folder}-{tenant}")
        for folder in DEFAULT_TENANT_FOLDERS
    }
    seen: dict[str, str] = {}
    for folder, name in resolved.items():
        if name in seen:
            raise ValueError(
                f"duplicate tenant repo name '{name}' for folders {seen[name]} and {folder}"
            )
        seen[name] = folder
    return resolved


def resolve_tenant_repos(
    *,
    repo_pattern: str,
    tenant_id: str,
    repo_name: str = "",
    repo_names: Any = None,
) -> list[str]:
    """Return unique effective tenant repositories in stable folder order."""
    mapping = resolve_tenant_repo_map(
        repo_pattern=repo_pattern,
        tenant_id=tenant_id,
        repo_name=repo_name,
        repo_names=repo_names,
    )
    return list(dict.fromkeys(mapping[folder] for folder in DEFAULT_TENANT_FOLDERS))


def _cli_validate_platform(args: argparse.Namespace) -> int:
    folders = json.loads(args.folders_json)
    raw = json.loads(args.overrides_json)
    defaults = json.loads(args.defaults_json)
    overrides = normalize_platform_repo_names(folders, raw)
    applied = apply_platform_repo_names(defaults, overrides)
    print(json.dumps({"overrides": overrides, "repos": applied}))
    return 0


def _cli_validate_tenant(args: argparse.Namespace) -> int:
    repo_names = json.loads(args.repo_names_json)
    mapping = resolve_tenant_repo_map(
        repo_pattern=args.repo_pattern,
        tenant_id=args.tenant_id,
        repo_name=args.repo_name,
        repo_names=repo_names,
    )
    names = list(dict.fromkeys(mapping[folder] for folder in DEFAULT_TENANT_FOLDERS))
    print(json.dumps({"repositories": names, "repo_by_folder": mapping}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    platform = sub.add_parser("validate-platform")
    platform.add_argument("--folders-json", required=True)
    platform.add_argument("--overrides-json", required=True)
    platform.add_argument("--defaults-json", required=True)
    platform.set_defaults(func=_cli_validate_platform)

    tenant = sub.add_parser("validate-tenant")
    tenant.add_argument("--repo-pattern", required=True)
    tenant.add_argument("--tenant-id", required=True)
    tenant.add_argument("--repo-name", default="")
    tenant.add_argument("--repo-names-json", default="{}")
    tenant.set_defaults(func=_cli_validate_tenant)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
