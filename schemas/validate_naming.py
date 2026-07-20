#!/usr/bin/env python3
"""Validate optional customer naming policy against CasC YAML identities."""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Any

import yaml


def _load_mapping(path: str, label: str, *, empty_ok: bool = False) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"{label} could not be loaded: {exc}") from exc
    if data is None and empty_ok:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{label} must contain a YAML mapping")
    return data


def load_policy(
    rules_path: str, resource_types_path: str, allowed_keys_path: str
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    rules = _load_mapping(rules_path, "naming-rules.yml", empty_ok=True)
    metadata = _load_mapping(resource_types_path, "resource-types.yml")
    allowed_doc = _load_mapping(allowed_keys_path, "engine allowed resource keys")
    allowed = set(allowed_doc.get("casc_allowed_resource_keys") or [])
    if not allowed:
        raise ValueError("engine allowed resource keys must be a non-empty list")
    defaults = metadata.get("defaults") or {}
    exceptions = metadata.get("exceptions") or {}
    if not isinstance(defaults, dict) or not isinstance(exceptions, dict):
        raise ValueError("resource-types.yml defaults/exceptions must be mappings")

    normalized: dict[str, dict[str, Any]] = {}
    for resource_type, rule in rules.items():
        if resource_type not in allowed:
            raise ValueError(f"Unknown naming-policy resource type: {resource_type}")
        if not isinstance(rule, dict):
            raise ValueError(f"Naming rule for {resource_type} must be a mapping")
        unknown_fields = sorted(set(rule) - {"pattern", "example", "description"})
        if unknown_fields:
            raise ValueError(
                f"Naming rule for {resource_type} has unknown fields: {', '.join(unknown_fields)}"
            )
        pattern = rule.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ValueError(f"Naming rule for {resource_type} requires non-empty pattern")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"Naming rule for {resource_type} has invalid regex: {exc}") from exc
        resource_meta = dict(defaults)
        resource_meta.update(exceptions.get(resource_type) or {})
        if resource_meta.get("value_type", "list") != "list":
            raise ValueError(
                f"Naming policy is unsupported for raw resource type {resource_type}"
            )
        identity_field = resource_meta.get("identity_field")
        if not isinstance(identity_field, str) or not identity_field:
            raise ValueError(f"Resource type {resource_type} has no identity_field")
        if resource_meta.get("identity_scalar", True) is not True:
            raise ValueError(
                f"Naming policy is unsupported for non-scalar identity "
                f"{resource_type}.{identity_field}"
            )
        normalized[resource_type] = dict(rule)
        normalized[resource_type]["identity_field"] = identity_field
    return normalized, exceptions, defaults


def validate_file(file_path: str, rules: dict[str, dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    try:
        with open(file_path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        return [f"{file_path}: Failed to parse - {exc}"]
    if not isinstance(data, dict):
        return errors

    for resource_type, items in data.items():
        rule = rules.get(resource_type)
        if rule is None:
            continue
        if not isinstance(items, list):
            errors.append(f"{file_path}: {resource_type} must be a list for naming validation")
            continue
        identity_field = rule["identity_field"]
        pattern = re.compile(rule["pattern"])
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                errors.append(f"{file_path}: {resource_type}[{index}] must be a mapping")
                continue
            identity = item.get(identity_field)
            if not isinstance(identity, str) or not identity:
                errors.append(
                    f"{file_path}: {resource_type}[{index}] requires scalar identity "
                    f"field '{identity_field}'"
                )
                continue
            if pattern.fullmatch(identity) is None:
                errors.append(
                    f"{file_path}: '{identity}' does not match '{rule['pattern']}' "
                    f"for {resource_type}.{identity_field} "
                    f"(example: {rule.get('example', 'N/A')})"
                )
    return errors


def validate_tree(config_dir: str, rules: dict[str, dict[str, Any]]) -> list[str]:
    if not rules:
        return []
    errors: list[str] = []
    skip_dirs = {
        ".schemas",
        ".engine",
        ".engine-runtime",
        ".git",
        ".github",
        ".scripts",
        ".control",
        ".aap-casc-engine",
    }
    skip_files = {"config.yml", "tenants.yml", "naming-rules.yml"}
    for root, dirs, files in os.walk(config_dir):
        dirs[:] = [directory for directory in dirs if directory not in skip_dirs]
        for filename in files:
            if not filename.endswith((".yml", ".yaml")) or filename.endswith(".sample"):
                continue
            if filename in skip_files:
                continue
            errors.extend(validate_file(os.path.join(root, filename), rules))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", required=True)
    parser.add_argument("--rules", required=True)
    parser.add_argument("--resource-types", required=True)
    parser.add_argument("--allowed-keys", required=True)
    args = parser.parse_args(argv)
    try:
        rules, _exceptions, _defaults = load_policy(
            args.rules, args.resource_types, args.allowed_keys
        )
        errors = validate_tree(args.config_dir, rules)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if errors:
        print("Naming convention violations found:")
        for error in errors:
            print(f"  ERROR: {error}")
        return 1
    print("Naming policy inactive." if not rules else "All configured naming rules passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
