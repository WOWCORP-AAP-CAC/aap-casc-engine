#!/usr/bin/env python3
"""
Naming Convention Validator for AAP CasC JSON configurations.

Validates that resource names in JSON config files follow the naming
conventions defined in the rules file (naming-rules.yml).

Usage:
    python3 validate_naming.py --config-dir <path> --rules <rules_file>
"""

import argparse
import json
import os
import sys

import yaml


def load_rules(rules_path: str) -> dict:
    """Load naming rules from YAML file."""
    with open(rules_path, "r") as f:
        return yaml.safe_load(f)


def validate_file(json_path: str, rules: dict) -> list:
    """Validate naming conventions in a single JSON file."""
    errors = []
    try:
        with open(json_path, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        return [f"{json_path}: Failed to parse JSON — {e}"]

    for resource_type, items in data.items():
        if resource_type not in rules:
            continue

        rule = rules[resource_type]
        import re

        pattern = re.compile(rule["pattern"])

        if not isinstance(items, list):
            continue

        for item in items:
            name = item.get("name", "")
            if not name:
                errors.append(
                    f"{json_path}: Resource of type '{resource_type}' missing 'name' field"
                )
                continue
            if not pattern.match(name):
                errors.append(
                    f"{json_path}: '{name}' does not match pattern '{rule['pattern']}' "
                    f"(expected: {rule.get('example', 'N/A')})"
                )
    return errors


def main():
    parser = argparse.ArgumentParser(description="Validate CasC naming conventions")
    parser.add_argument(
        "--config-dir", required=True, help="Directory containing JSON config files"
    )
    parser.add_argument(
        "--rules", required=True, help="Path to naming rules YAML file"
    )
    args = parser.parse_args()

    rules = load_rules(args.rules)
    all_errors = []

    for root, _, files in os.walk(args.config_dir):
        for fname in files:
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(root, fname)
            # Skip schema files
            if ".schemas" in fpath or "schema.json" in fpath:
                continue
            all_errors.extend(validate_file(fpath, rules))

    if all_errors:
        print("Naming convention violations found:")
        for err in all_errors:
            print(f"  ERROR: {err}")
        sys.exit(1)
    else:
        print("All naming conventions passed.")
        sys.exit(0)


if __name__ == "__main__":
    main()
