#!/usr/bin/env python3
"""Fail-closed validation for custom platform/tenant repository name overrides."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def _as_nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def normalize_platform_repo_names(
    folders: list[str], raw: Any
) -> dict[str, str]:
    """Normalize platform_repo_names into a folder->name map.

    Accepts:
      - mapping keyed by folder
      - ordered list matching folders
      - empty mapping/list (no overrides)
    """
    if raw is None:
        return {}
    if isinstance(raw, str):
        raise ValueError("platform_repo_names must be a mapping or list, not a string")
    if isinstance(raw, dict):
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
            normalized[key] = _as_nonempty_string(
                name, f"platform_repo_names['{key}']"
            )
        return normalized
    if isinstance(raw, list):
        if not raw:
            return {}
        if len(raw) != len(folders):
            raise ValueError(
                f"platform_repo_names list must contain exactly {len(folders)} "
                f"names in folder order {folders}"
            )
        normalized = {}
        for folder, name in zip(folders, raw):
            normalized[folder] = _as_nonempty_string(
                name, f"platform_repo_names[{folder}]"
            )
        return normalized
    raise ValueError("platform_repo_names must be a mapping or list")


def apply_platform_repo_names(
    default_repos: list[dict[str, Any]],
    overrides: dict[str, str],
) -> list[dict[str, Any]]:
    """Apply validated overrides and reject blank/duplicate resulting names."""
    result: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    for entry in default_repos:
        folder = entry["folder"]
        name = overrides.get(folder, entry["name"])
        name = _as_nonempty_string(name, f"platform repo name for {folder}")
        if name in seen:
            raise ValueError(
                f"duplicate platform repo name '{name}' for folders "
                f"{seen[name]} and {folder}"
            )
        seen[name] = folder
        merged = dict(entry)
        merged["name"] = name
        result.append(merged)
    return result


DEFAULT_TENANT_FOLDERS = (
    "projects",
    "credentials",
    "inventories",
    "templates",
    "workflows",
    "schedules",
    "notifications",
)


def validate_tenant_repo_names(
    *,
    repo_pattern: str,
    repo_name: str = "",
    repo_names: Any = None,
) -> list[str]:
    """Validate tenant repo_name/repo_names overrides.

    Returns the custom name list when overrides are present, otherwise [].
    """
    if repo_pattern not in ("combined", "per-resource-type"):
        raise ValueError("repo_pattern must be combined or per-resource-type")

    repo_name = (repo_name or "").strip()
    if repo_names is None:
        repo_names = []

    if isinstance(repo_names, dict):
        raise ValueError("repo_names must be a list of strings, not a mapping")
    if isinstance(repo_names, str):
        raise ValueError("repo_names must be a list of strings, not a string")
    if not isinstance(repo_names, list):
        raise ValueError("repo_names must be a list of strings")

    cleaned_names = []
    for idx, value in enumerate(repo_names):
        cleaned_names.append(_as_nonempty_string(value, f"repo_names[{idx}]"))

    if cleaned_names and len(cleaned_names) != len(set(cleaned_names)):
        raise ValueError("repo_names values must be unique")

    if repo_name and cleaned_names:
        raise ValueError("provide either repo_name or repo_names, not both")

    if repo_pattern == "combined":
        if cleaned_names:
            if len(cleaned_names) != 1:
                raise ValueError("combined repo_pattern requires exactly one repo name")
            return cleaned_names
        if repo_name:
            return [_as_nonempty_string(repo_name, "repo_name")]
        return []

    # per-resource-type
    if repo_name:
        raise ValueError("repo_name is only valid for combined repo_pattern")
    if cleaned_names:
        if len(cleaned_names) != len(DEFAULT_TENANT_FOLDERS):
            raise ValueError(
                "per-resource-type repo_names must contain exactly "
                f"{len(DEFAULT_TENANT_FOLDERS)} unique non-empty names in folder order "
                f"{list(DEFAULT_TENANT_FOLDERS)}"
            )
        return cleaned_names
    return []


def resolve_tenant_repos(
    *,
    repo_pattern: str,
    org_id: str,
    repo_name: str = "",
    repo_names: Any = None,
) -> list[str]:
    """Return effective tenant repo names after fail-closed override validation."""
    org = _as_nonempty_string(org_id, "org_id")
    custom = validate_tenant_repo_names(
        repo_pattern=repo_pattern,
        repo_name=repo_name,
        repo_names=repo_names,
    )
    if custom:
        return custom
    if repo_pattern == "combined":
        return [f"casc-tenant-{org}"]
    return [f"controller-{folder}-{org}" for folder in DEFAULT_TENANT_FOLDERS]


def _cli_validate_platform(args: argparse.Namespace) -> int:
    folders = json.loads(args.folders_json)
    raw = json.loads(args.overrides_json)
    defaults = json.loads(args.defaults_json)
    overrides = normalize_platform_repo_names(folders, raw)
    applied = apply_platform_repo_names(defaults, overrides)
    print(json.dumps({"overrides": overrides, "repos": applied}))
    return 0


def _cli_validate_tenant(args: argparse.Namespace) -> int:
    names = resolve_tenant_repos(
        repo_pattern=args.repo_pattern,
        org_id=args.org_id,
        repo_name=args.repo_name,
        repo_names=json.loads(args.repo_names_json),
    )
    print(json.dumps({"tenant_repos": names}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_plat = sub.add_parser("validate-platform")
    p_plat.add_argument("--folders-json", required=True)
    p_plat.add_argument("--overrides-json", required=True)
    p_plat.add_argument("--defaults-json", required=True)
    p_plat.set_defaults(func=_cli_validate_platform)

    p_ten = sub.add_parser("validate-tenant")
    p_ten.add_argument("--repo-pattern", required=True)
    p_ten.add_argument("--org-id", required=True)
    p_ten.add_argument("--repo-name", default="")
    p_ten.add_argument("--repo-names-json", default="[]")
    p_ten.set_defaults(func=_cli_validate_tenant)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
