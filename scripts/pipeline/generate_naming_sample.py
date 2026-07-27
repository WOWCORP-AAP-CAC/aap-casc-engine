#!/usr/bin/env python3
"""Generate the inert comprehensive naming-rules.yml.sample from the catalog."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG = ROOT / "schemas" / "resource-types.yml"
DEFAULT_OUTPUT = ROOT / "examples" / "naming-rules.yml.sample"


def load_catalog(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("resource-types.yml must be a mapping")
    return data


def naming_supported_types(catalog: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    defaults = catalog.get("defaults") or {}
    exceptions = catalog.get("exceptions") or {}
    if not isinstance(defaults, dict) or not isinstance(exceptions, dict):
        raise ValueError("defaults/exceptions must be mappings")
    supported: list[tuple[str, dict[str, Any]]] = []
    for key in sorted(exceptions):
        meta = dict(defaults)
        meta.update(exceptions[key] or {})
        if meta.get("naming_supported", True) is True and meta.get("value_type", "list") == "list":
            if meta.get("identity_scalar", True) is True:
                supported.append((key, meta))
    return supported


def render_sample(catalog: dict[str, Any]) -> str:
    collection = catalog.get("collection") or {}
    version = collection.get("version", "unknown")
    unsupported = catalog.get("unsupported") or {}
    defaults = catalog.get("defaults") or {}
    exceptions = catalog.get("exceptions") or {}

    lines: list[str] = [
        "---",
        "# Optional customer naming policy example (inert sample).",
        "#",
        "# Rename this file to naming-rules.yml, adapt the patterns for your",
        "# organization, and uncomment rules to activate naming validation.",
        "# Omit resource types that should remain unrestricted. Patterns validate",
        "# resource identities in YAML, not filenames.",
        "#",
        f"# Generated from schemas/resource-types.yml against pinned",
        f"# {collection.get('name', 'infra.aap_configuration')} {version}.",
        "# Do not invent engine-mandated type prefixes (org-/prj-/jt-). Use",
        "# neutral placeholders and replace them with your standards.",
        "#",
        "# Exclusions (no naming rules here):",
    ]

    # Raw / naming_supported:false
    for key in sorted(exceptions):
        meta = dict(defaults)
        meta.update(exceptions[key] or {})
        if meta.get("naming_supported", True) is not True:
            reason = "raw/settings-style type" if meta.get("value_type") == "raw" else "naming_supported: false"
            lines.append(f"#   - {key}: {reason}")

    for key in sorted(unsupported):
        reason = (unsupported[key] or {}).get("reason", "unsupported")
        # Keep exclusion lines short without truncating mid-parenthesis.
        short = " ".join(str(reason).split())
        if len(short) > 100:
            short = short[:97].rstrip() + "..."
        lines.append(f"#   - {key}: {short}")

    lines.append("#")

    for key, meta in naming_supported_types(catalog):
        identity = meta.get("identity_field", "name")
        lines.extend(
            [
                f"# {key}:",
                f'#   pattern: "^REPLACE_ME_[a-z0-9_]+$"',
                f'#   example: "REPLACE_ME_example"',
                f'#   description: "Identity field: {identity}. Replace REPLACE_ME with your standard."',
                "#",
            ]
        )

    # Ensure trailing newline; drop final lone "#" blank if present
    while lines and lines[-1] == "#":
        lines.pop()
    return "\n".join(lines) + "\n"


def validate_sample(sample_text: str, catalog: dict[str, Any]) -> None:
    """Ensure the inert sample comments cover every naming_supported type."""
    expected = {key for key, _ in naming_supported_types(catalog)}
    found = set()
    for line in sample_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and stripped.endswith(":"):
            key = stripped[2:-1].strip()
            if key in expected:
                found.add(key)
    missing = sorted(expected - found)
    if missing:
        raise ValueError(
            "naming-rules.yml.sample missing commented rules for: "
            + ", ".join(missing)
        )
    # Must remain inactive when loaded as YAML (all rules commented).
    loaded = yaml.safe_load(sample_text)
    if loaded not in (None, {}):
        raise ValueError(
            "naming-rules.yml.sample must be fully commented (inactive when loaded)"
        )
    lowered = sample_text.lower()
    for token in ("rename", "adapt", "uncomment"):
        if token not in lowered:
            raise ValueError(f"naming-rules.yml.sample must mention '{token}'")
    if "REPLACE_ME" not in sample_text:
        raise ValueError("naming-rules.yml.sample must use REPLACE_ME placeholders")
    # No opinionated mandatory engine prefixes in active (uncommented) form —
    # sample is fully commented, but also ban WW demo leftovers.
    if "WW " in sample_text:
        raise ValueError("naming-rules.yml.sample must not contain WW demo prefixes")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource-types", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate existing sample against catalog; do not write",
    )
    args = parser.parse_args(argv)
    catalog = load_catalog(args.resource_types)
    rendered = render_sample(catalog)
    if args.check:
        current = args.output.read_text(encoding="utf-8")
        validate_sample(current, catalog)
        if current != rendered:
            raise ValueError(
                f"{args.output} is stale — run generate_naming_sample.py to regenerate"
            )
        print(f"OK: {args.output} matches catalog naming_supported coverage")
        return 0
    validate_sample(rendered, catalog)
    args.output.write_text(rendered, encoding="utf-8")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
