#!/usr/bin/env python3
"""
Naming Convention Validator for AAP CasC configurations.

Validates that resource names in YAML config files follow the naming
conventions defined in the rules file (naming-rules.yml).

Scans base/ and environment directories for YAML files. Skips control
files (config.yml, tenants.yml), sample files, and resource types without
naming rules defined.

Usage:
    python3 validate_naming.py --config-dir <path> --rules <rules_file>
"""

import argparse
import os
import re
import sys

import yaml


def load_rules(rules_path: str) -> dict:
    """Load naming rules from YAML file."""
    with open(rules_path, "r") as f:
        return yaml.safe_load(f)


def validate_file(file_path: str, rules: dict) -> list:
    """Validate naming conventions in a single YAML file."""
    errors = []
    try:
        with open(file_path, "r") as f:
            data = yaml.safe_load(f)
    except (yaml.YAMLError, IOError) as e:
        return [f"{file_path}: Failed to parse — {e}"]

    if not isinstance(data, dict):
        return []

    for resource_type, items in data.items():
        if resource_type not in rules:
            continue

        rule = rules[resource_type]
        pattern = re.compile(rule["pattern"])

        if not isinstance(items, list):
            continue

        for item in items:
            name = item.get("name", "")
            if not name:
                continue
            if not pattern.match(name):
                errors.append(
                    f"{file_path}: '{name}' does not match pattern '{rule['pattern']}' "
                    f"(expected: {rule.get('example', 'N/A')})"
                )
    return errors


def main():
    parser = argparse.ArgumentParser(description="Validate CasC naming conventions")
    parser.add_argument(
        "--config-dir", required=True, help="Directory containing CasC config files"
    )
    parser.add_argument(
        "--rules", required=True, help="Path to naming rules YAML file"
    )
    args = parser.parse_args()

    rules = load_rules(args.rules)
    all_errors = []

    skip_dirs = {".schemas", ".engine", ".git"}
    skip_files = {"config.yml", "tenants.yml"}

    for root, dirs, files in os.walk(args.config_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in files:
            if not fname.endswith((".yml", ".yaml")):
                continue
            if fname.endswith(".sample"):
                continue
            if fname in skip_files:
                continue
            fpath = os.path.join(root, fname)
            if "schema" in fpath.lower():
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
