#!/usr/bin/env python3
"""Check that naming-rules.yml patterns and *.schema.json name patterns are in sync.

This CI guardrail prevents drift between the canonical naming rules and the
JSON schemas that enforce them. It loads each resource type's regex from
naming-rules.yml and compares it to the corresponding schema.json file's
properties.<resource>.items.properties.name.pattern field.

Exit 0 if all patterns match; exit 1 with details on any mismatch.
"""

import json
import sys
from pathlib import Path

import yaml


def load_naming_rules(rules_path: Path) -> dict:
    with open(rules_path) as f:
        return yaml.safe_load(f)


def extract_schema_name_pattern(schema_path: Path) -> str | None:
    with open(schema_path) as f:
        schema = json.load(f)

    try:
        resource_key = list(schema["properties"].keys())[0]
        return schema["properties"][resource_key]["items"]["properties"]["name"]["pattern"]
    except (KeyError, IndexError):
        return None


def main():
    script_dir = Path(__file__).resolve().parent
    rules_path = script_dir / "naming-rules.yml"

    if not rules_path.exists():
        print(f"ERROR: {rules_path} not found")
        sys.exit(1)

    rules = load_naming_rules(rules_path)
    errors = []
    checked = 0

    for resource_type, rule_data in rules.items():
        schema_file = script_dir / f"{resource_type}.schema.json"
        if not schema_file.exists():
            continue

        checked += 1
        rule_pattern = rule_data["pattern"]
        schema_pattern = extract_schema_name_pattern(schema_file)

        if schema_pattern is None:
            errors.append(
                f"  {resource_type}: schema has no name.pattern field"
            )
        elif rule_pattern != schema_pattern:
            errors.append(
                f"  {resource_type}:\n"
                f"    naming-rules.yml : {rule_pattern}\n"
                f"    {schema_file.name}: {schema_pattern}"
            )

    if errors:
        print("=== NAMING SYNC CHECK FAILED ===")
        print("Mismatches between naming-rules.yml and schema files:")
        print()
        for err in errors:
            print(err)
        print()
        print("Fix: update the schema pattern to match naming-rules.yml (the canonical source).")
        sys.exit(1)
    else:
        print(f"=== NAMING SYNC CHECK PASSED ({checked} resource types verified) ===")
        sys.exit(0)


if __name__ == "__main__":
    main()
