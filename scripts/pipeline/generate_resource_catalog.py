#!/usr/bin/env python3
"""Generate the pinned AAP CasC resource catalog.

The human catalog is generated from three explicit sources:

* schemas/resource-types.yml: engine support and merge contract;
* schemas/resource-parameters-4.7.0.yml: normalized pinned collection docs;
* examples/resource-examples.yml: reviewed, non-secret examples.

Use ``--extract-collection-root`` only when refreshing the pinned collection
snapshot. Normal generation and CI checks do not require a collection install.
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
RESOURCE_TYPES = ROOT / "schemas" / "resource-types.yml"
DISPATCH_INVENTORY = ROOT / "schemas" / "collection-dispatch-4.7.0.yml"
PARAMETERS = ROOT / "schemas" / "resource-parameters-4.7.0.yml"
EXAMPLES = ROOT / "examples" / "resource-examples.yml"
OUTPUT = ROOT / "docs" / "RESOURCE_CATALOG.md"

ENGINE_EXTENSION_ROLES = {
    "hub_roles": "hub_role",
    "hub_group_roles": "hub_group_roles",
}

# Pinned 4.7.0 role inputs accepted by task code but omitted from that role's
# README Data Structure table. Keep this list narrow and version-specific.
SUPPLEMENTAL_PARAMETERS = {
    "aap_organizations": [
        ("execution_environment", "str", "no", "", "Default execution environment for the Organization."),
    ],
    "aap_user_accounts": [
        ("is_platform_auditor", "bool", "no", "false", "Grant platform-auditor privileges."),
        ("organizations", "list", "no", "", "Organizations to associate with the user."),
    ],
    "gateway_service_clusters": [
        ("health_checks_enabled", "bool", "no", "", "Enable service-cluster health checks."),
        ("health_check_interval_seconds", "int", "no", "", "Seconds between health checks."),
        ("health_check_timeout_seconds", "int", "no", "", "Health-check timeout in seconds."),
        ("health_check_healthy_threshold", "int", "no", "", "Successful checks required before healthy."),
        ("health_check_unhealthy_threshold", "int", "no", "", "Failed checks required before unhealthy."),
        ("outlier_detection_enabled", "bool", "no", "", "Enable outlier detection."),
        ("outlier_detection_interval_seconds", "int", "no", "", "Outlier-detection interval in seconds."),
        ("outlier_detection_base_ejection_time_seconds", "int", "no", "", "Base ejection time in seconds."),
        ("outlier_detection_max_ejection_percent", "int", "no", "", "Maximum percentage of nodes that may be ejected."),
        ("outlier_detection_consecutive_5xx", "int", "no", "", "Consecutive 5xx responses before ejection."),
    ],
    "gateway_services": [
        ("node_tags", "list", "no", "", "Service-node tags used to select nodes."),
    ],
    "gateway_routes": [
        ("node_tags", "list", "no", "", "Service-node tags used by the route."),
    ],
    "hub_ee_registries": [
        ("proxy_url", "str", "no", "", "Proxy URL for registry access."),
        ("proxy_username", "str", "no", "", "Proxy username; inject securely."),
        ("proxy_password", "str", "no", "", "Proxy password; inject securely."),
    ],
    "hub_collection_repositories": [
        ("update_repo", "bool", "no", "", "Update repository content after configuration."),
    ],
    "controller_projects": [
        ("job_timeout", "int", "no", "", "Alias accepted by the role for timeout."),
        ("scm_credential", "str", "no", "", "Alias accepted by the role for credential."),
    ],
    "controller_templates": [
        ("ask_tags", "bool", "no", "", "Alias accepted by the role for ask_tags_on_launch."),
        ("ask_skip_tags", "bool", "no", "", "Alias accepted by the role for ask_skip_tags_on_launch."),
    ],
    "controller_workflows": [
        ("ask_tags", "bool", "no", "", "Alias accepted by the role for ask_tags_on_launch."),
        ("ask_skip_tags", "bool", "no", "", "Alias accepted by the role for ask_skip_tags_on_launch."),
        ("destroy_current_schema", "bool", "no", "", "Alias accepted by the role for destroy_current_nodes."),
        ("simplified_workflow_nodes", "list", "no", "", "Complete simplified workflow-node declarations processed by the role."),
    ],
    "eda_projects": [
        ("scm_url", "str", "no", "", "Alias accepted by the role for url."),
    ],
}

# Corrections where the pinned README marks a field required even though the
# role supplies a value or the underlying module treats a rename field as
# optional. These describe the engine consumer contract, not API response data.
REQUIREMENT_OVERRIDES = {
    "hub_namespaces": {
        "new_name": "no",
        "description": "no",
        "avatar_url": "no",
        "groups": "no",
    },
    "aap_applications": {
        "authorization_grant_type": "no",
        "client_type": "no",
        "skip_authorization": "no",
    },
    "controller_notifications": {"new_name": "no"},
    "controller_hosts": {"new_name": "no"},
    "controller_groups": {"new_name": "no"},
}

TYPE_OVERRIDES = {
    "hub_roles": {"perms": "list"},
    "hub_group_roles": {"groups": "list", "role_list": "list"},
    "hub_ee_registries": {"tls_validation": "bool"},
    "hub_ee_images": {"tags": "list"},
    "controller_notifications": {"notification_configuration": "dict"},
    "controller_hosts": {"variables": "dict"},
    "controller_workflows": {"organization": "str"},
    "eda_rulebook_activations": {"enabled": "bool", "extra_vars": "dict"},
}

DESCRIPTION_OVERRIDES = {
    "gateway_settings": (
        "Manage Ansible Automation Platform Gateway settings as one declarative "
        "settings mapping."
    ),
    "controller_settings": (
        "Manage Automation Controller settings through the collection settings "
        "role."
    ),
}

FOLDER_OVERRIDES = {
    "aap_organizations": "organizations",
    "aap_teams": "teams",
    "aap_user_accounts": "users",
    "gateway_role_definitions": "role_definitions",
    "gateway_role_user_assignments": "rbac_user_assignments",
    "gateway_role_team_assignments": "rbac_team_assignments",
    "controller_credential_types": "credential_types",
    "controller_execution_environments": "execution_environments",
    "controller_settings": "settings",
    "controller_projects": "projects",
    "controller_credentials": "credentials",
    "controller_inventories": "inventories",
    "controller_templates": "templates",
    "controller_workflows": "workflows",
    "controller_schedules": "schedules",
    "controller_notifications": "notifications",
}


class UnsafeScalar(str):
    """Preserve Ansible's !unsafe tag in catalog examples."""


class CatalogLoader(yaml.SafeLoader):
    pass


class CatalogDumper(yaml.SafeDumper):
    pass


def _unsafe_constructor(
    loader: yaml.SafeLoader, node: yaml.Node
) -> UnsafeScalar:
    return UnsafeScalar(loader.construct_scalar(node))


def _unsafe_representer(
    dumper: yaml.SafeDumper, value: UnsafeScalar
) -> yaml.Node:
    return dumper.represent_scalar("!unsafe", str(value))


CatalogLoader.add_constructor("!unsafe", _unsafe_constructor)
CatalogDumper.add_representer(UnsafeScalar, _unsafe_representer)


def load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.load(path.read_text(), Loader=CatalogLoader)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def dump_yaml(data: Any) -> str:
    return yaml.dump(
        data,
        Dumper=CatalogDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=False,
    ).rstrip()


def current_drift_keys() -> set[str]:
    """Keys with drift_comparison: identity_presence in resource-types.yml."""
    schema = load_yaml(RESOURCE_TYPES)
    defaults = schema.get("defaults", {})
    exceptions = schema.get("exceptions", {})
    compared: set[str] = set()
    for key, value in exceptions.items():
        meta = {**defaults, **(value or {})}
        mode = str(meta.get("drift_comparison", "unsupported")).strip()
        if mode == "identity_presence":
            compared.add(key)
    return compared


def supported_catalog() -> tuple[dict[str, Any], dict[str, Any]]:
    schema = load_yaml(RESOURCE_TYPES)
    defaults = schema.get("defaults", {})
    exceptions = schema.get("exceptions", {})
    if not isinstance(defaults, dict) or not isinstance(exceptions, dict):
        raise ValueError("resource-types.yml defaults/exceptions must be mappings")
    merged = {
        key: {**defaults, **value}
        for key, value in exceptions.items()
    }
    return schema, merged


def source_roles() -> dict[str, str]:
    inventory = load_yaml(DISPATCH_INVENTORY)
    roles = {
        item["var"]: item["role"]
        for item in inventory.get("dispatch_variables", [])
    }
    roles.update(ENGINE_EXTENSION_ROLES)
    return roles


def _section(text: str, heading: str) -> str:
    match = re.search(
        rf"(?ms)^{re.escape(heading)}\s*$\n(.*?)(?=^##\s|\Z)",
        text,
    )
    return match.group(1).strip() if match else ""


def _strip_markdown_links(value: str) -> str:
    return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)


def _plain_description(readme: str) -> str:
    value = _section(readme, "## Description")
    paragraphs = [
        re.sub(r"\s+", " ", paragraph).strip()
        for paragraph in re.split(r"\n\s*\n", value)
        if paragraph.strip() and not paragraph.lstrip().startswith(("#", "|"))
    ]
    value = paragraphs[0] if paragraphs else "Pinned collection resource role."
    return _strip_markdown_links(value)


def _split_row(line: str) -> list[str]:
    value = line.strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|"):
        value = value[:-1]
    return [
        cell.replace(r"\|", "|").strip()
        for cell in re.split(r"(?<!\\)\|", value)
    ]


def _clean_cell(value: str) -> str:
    value = _strip_markdown_links(value.strip().replace("\n", " "))
    if value.startswith("`") and value.endswith("`") and len(value) > 1:
        value = value[1:-1]
    return re.sub(r"\s+", " ", value)


def _is_separator(line: str) -> bool:
    cells = _split_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _normalize_parameter(
    headers: list[str], cells: list[str]
) -> dict[str, str]:
    row = {
        re.sub(r"[^a-z]+", "_", header.lower()).strip("_"): _clean_cell(value)
        for header, value in zip(headers, cells)
    }
    name = row.get("variable_name") or row.get("parameter") or row.get("option") or ""
    default = row.get("default_value") or row.get("default") or ""
    required = row.get("required") or ""
    value_type = row.get("type") or ""
    description = row.get("description") or row.get("variable_description") or ""

    known_types = {
        "any",
        "bool",
        "boolean",
        "dict",
        "float",
        "int",
        "integer",
        "list",
        "mapping",
        "obj",
        "object",
        "raw",
        "str",
        "string",
    }
    if required.lower() in known_types and value_type.lower() in {
        "yes",
        "no",
        "conditional",
    }:
        required, value_type = value_type, required
    if not required:
        required = "conditional / nested"
    if not value_type:
        value_type = "not specified in role README"

    return {
        "name": name,
        "type": value_type,
        "required": required,
        "default": default,
        "description": description,
    }


def _parameter_groups(readme: str) -> list[dict[str, Any]]:
    section = _section(readme, "## Data Structure")
    lines = section.splitlines()
    groups: list[dict[str, Any]] = []
    heading = "Resource parameters"
    index = 0

    while index < len(lines):
        line = lines[index]
        if line.startswith("###"):
            heading = _clean_cell(line.lstrip("#").strip())
            index += 1
            continue
        if (
            line.lstrip().startswith("|")
            and index + 1 < len(lines)
            and _is_separator(lines[index + 1])
        ):
            headers = [_clean_cell(cell) for cell in _split_row(line)]
            first = re.sub(r"[^a-z]+", " ", headers[0].lower()).strip()
            table_lines: list[str] = []
            index += 2
            while index < len(lines) and lines[index].lstrip().startswith("|"):
                table_lines.append(lines[index])
                index += 1
            if first not in {"variable name", "parameter", "option"}:
                continue
            parameters = [
                _normalize_parameter(headers, _split_row(row))
                for row in table_lines
            ]
            parameters = [item for item in parameters if item["name"]]
            if parameters:
                groups.append({"name": heading, "parameters": parameters})
            continue
        index += 1
    return groups


def _manual_parameter_groups(key: str) -> list[dict[str, Any]]:
    if key == "gateway_settings":
        return [
            {
                "name": "Settings mapping",
                "parameters": [
                    {
                        "name": "<gateway setting name>",
                        "type": "any",
                        "required": "no",
                        "default": "AAP API default",
                        "description": (
                            "A Gateway setting key accepted by "
                            "ansible.platform.settings. The role accepts a "
                            "single mapping rather than a fixed object schema."
                        ),
                    }
                ],
            }
        ]
    if key == "hub_collections":
        values = [
            ("namespace", "str", "yes", "", "Hub namespace containing the collection."),
            ("name", "str", "yes", "", "Collection name."),
            ("version", "str", "conditional", "", "Collection version; alternative to path when identifying existing content."),
            ("path", "str", "conditional", "", "Path to the collection artifact to upload."),
            ("repository", "str", "no", "", "Target Hub repository."),
            ("wait", "bool", "no", "", "Wait for the import task."),
            ("auto_approve", "bool", "no", "", "Automatically approve the import."),
            ("timeout", "int", "no", "", "Maximum import wait in seconds."),
            ("interval", "int", "no", "", "Polling interval in seconds."),
            ("overwrite_existing", "bool", "no", "", "Allow replacement of an existing version."),
            ("state", "str", "no", "present", "Desired state."),
        ]
        return [
            {
                "name": "Collection parameters",
                "parameters": [
                    {
                        "name": name,
                        "type": value_type,
                        "required": required,
                        "default": default,
                        "description": description,
                    }
                    for name, value_type, required, default, description in values
                ],
            }
        ]
    return []


def _supplemental_parameter_group(key: str) -> list[dict[str, Any]]:
    values = SUPPLEMENTAL_PARAMETERS.get(key, [])
    if not values:
        return []
    return [
        {
            "name": "Pinned role task inputs omitted from the role README table",
            "parameters": [
                {
                    "name": name,
                    "type": value_type,
                    "required": required,
                    "default": default,
                    "description": description,
                }
                for name, value_type, required, default, description in values
            ],
        }
    ]


def _apply_requirement_overrides(
    key: str, groups: list[dict[str, Any]]
) -> None:
    overrides = REQUIREMENT_OVERRIDES.get(key, {})
    for group in groups:
        for parameter in group["parameters"]:
            if parameter["name"] in overrides:
                parameter["required"] = overrides[parameter["name"]]


def _apply_type_overrides(key: str, groups: list[dict[str, Any]]) -> None:
    overrides = TYPE_OVERRIDES.get(key, {})
    for group in groups:
        for parameter in group["parameters"]:
            if parameter["name"] in overrides:
                parameter["type"] = overrides[parameter["name"]]


def extract_parameter_snapshot(collection_root: Path) -> dict[str, Any]:
    schema, supported = supported_catalog()
    roles = source_roles()
    resources: dict[str, Any] = {}

    for key in supported:
        role = roles.get(key)
        if not role:
            raise ValueError(f"No source role recorded for {key}")
        readme_path = collection_root / "roles" / role / "README.md"
        if not readme_path.is_file():
            raise ValueError(f"Missing pinned role README: {readme_path}")
        readme = readme_path.read_text()
        groups = _parameter_groups(readme) or _manual_parameter_groups(key)
        groups.extend(_supplemental_parameter_group(key))
        _apply_requirement_overrides(key, groups)
        _apply_type_overrides(key, groups)
        if not groups:
            raise ValueError(f"No parameter table found for {key} ({role})")
        resources[key] = {
            "role": role,
            "source": f"roles/{role}/README.md",
            "description": DESCRIPTION_OVERRIDES.get(
                key, _plain_description(readme)
            ),
            "parameter_groups": groups,
        }

    return {
        "collection": schema["collection"],
        "generated_from": (
            "Pinned collection role README Data Structure sections plus "
            "version-specific role task inputs omitted from those tables"
        ),
        "resources": resources,
    }


def domain_for(key: str) -> str:
    if key in ENGINE_EXTENSION_ROLES:
        return "Engine extensions"
    if key.startswith(("aap_", "gateway_")):
        return "Gateway"
    if key.startswith("controller_"):
        return "Controller"
    if key.startswith("hub_"):
        return "Automation Hub"
    if key.startswith("eda_"):
        return "Event-Driven Ansible"
    return "Other"


def ownership_for(key: str) -> str:
    platform_only = {
        "aap_organizations",
        "aap_user_accounts",
        "gateway_authenticators",
        "gateway_service_keys",
        "gateway_role_definitions",
        "gateway_settings",
        "aap_teams",
        "aap_applications",
        "gateway_authenticator_maps",
        "gateway_http_ports",
        "gateway_service_clusters",
        "gateway_service_nodes",
        "gateway_services",
        "gateway_routes",
        "gateway_role_user_assignments",
        "gateway_role_team_assignments",
        "hub_namespaces",
        "hub_collections",
        "hub_roles",
        "hub_group_roles",
        "hub_ee_registries",
        "hub_ee_repositories",
        "hub_ee_images",
        "hub_collection_remotes",
        "hub_collection_repositories",
        "controller_settings",
        "controller_instances",
        "controller_instance_groups",
        "controller_credential_types",
        "eda_credential_types",
    }
    return "Platform" if key in platform_only else "Platform or tenant, subject to AAP RBAC"


def folder_for(key: str) -> str:
    if key in FOLDER_OVERRIDES:
        return FOLDER_OVERRIDES[key]
    for prefix, domain in (
        ("gateway_", "gateway"),
        ("controller_", "controller"),
        ("hub_", "hub"),
        ("eda_", "eda"),
        ("aap_", "gateway"),
    ):
        if key.startswith(prefix):
            return f"{domain}/{key[len(prefix):]}"
    return key


def _escape_table(value: Any) -> str:
    text = str(value or "—").replace("|", r"\|").replace("\n", "<br>")
    return text


def _anchor(value: str) -> str:
    # GFM preserves underscores in heading IDs.
    return re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-")


def render_catalog() -> str:
    schema, supported = supported_catalog()
    parameters = load_yaml(PARAMETERS)
    examples = load_yaml(EXAMPLES)
    parameter_resources = parameters.get("resources", {})
    example_resources = examples.get("resources", {})
    drift_keys = current_drift_keys()

    supported_keys = set(supported)
    if set(parameter_resources) != supported_keys:
        raise ValueError(
            "Parameter snapshot keys differ from supported catalog: "
            f"missing={sorted(supported_keys - set(parameter_resources))}, "
            f"extra={sorted(set(parameter_resources) - supported_keys)}"
        )
    if set(example_resources) != supported_keys:
        raise ValueError(
            "Example keys differ from supported catalog: "
            f"missing={sorted(supported_keys - set(example_resources))}, "
            f"extra={sorted(set(example_resources) - supported_keys)}"
        )

    version = schema["collection"]["version"]
    lines = [
        "# AAP CasC Resource Catalog",
        "",
        "<!-- Generated by scripts/pipeline/generate_resource_catalog.py; do not edit manually. -->",
        "",
        f"This catalog documents every declarative key supported by the engine for "
        f"`infra.aap_configuration=={version}`. It is a consumer reference, not "
        "a replacement for the pinned collection documentation.",
        "",
        "## How to use this catalog",
        "",
        "- Put each YAML document under `base/` or a mapped environment directory.",
        "- The top-level key selects the collection resource role; filenames and subfolders are organizational only.",
        "- Examples are non-secret and intentionally use `state: present` behavior. Referenced organizations, credentials, inventories, projects, and other dependencies must already exist.",
        "- Parameter requirements are normalized from the pinned role README and executable role defaults. “Conditional” means another field or operation changes the requirement.",
        "- Examples are naming-policy-neutral. If `naming-rules.yml` is active, adapt identity values to that customer-owned policy before use.",
        "- Never store passwords or tokens in Git. Use AAP credentials or approved runtime injection.",
        "- `deletion_supported: false` means the engine rejects explicit deletion even if the upstream role documents `state: absent`.",
        "",
        "## Merge modes",
        "",
        "| Mode | Consumer behavior |",
        "|---|---|",
        "| `keyed` | Items merge by the catalog `identity_field`; duplicate identities in one layer fail. |",
        "| `raw` | Settings mappings combine recursively; environment values win. |",
        "| `atomic` | Complete declarations concatenate; the same relative env path replaces the base path contribution. |",
        "",
        "## Supported resources",
        "",
        "| Key | Domain | Merge | Ownership | Drift identity presence |",
        "|---|---|---|---|---|",
    ]
    for key, metadata in supported.items():
        drift_label = (
            "Compared by identity"
            if key in drift_keys
            else "Not currently compared"
        )
        lines.append(
            f"| [`{key}`](#{_anchor(key)}) | {domain_for(key)} | "
            f"`{metadata['merge_mode']}` | {ownership_for(key)} | "
            f"{drift_label} |"
        )

    domains = [
        "Gateway",
        "Controller",
        "Automation Hub",
        "Event-Driven Ansible",
        "Engine extensions",
    ]
    for domain in domains:
        lines.extend(["", f"## {domain}", ""])
        for key, metadata in supported.items():
            if domain_for(key) != domain:
                continue
            source = parameter_resources[key]
            example = example_resources[key].get("example")
            if not isinstance(example, dict) or set(example) != {key}:
                raise ValueError(f"{key} example must contain only its top-level key")
            identity = (
                metadata["identity_field"]
                if metadata["merge_mode"] == "keyed"
                else "not applicable"
            )
            lines.extend(
                [
                    f"### `{key}`",
                    "",
                    source["description"],
                    "",
                    "| Contract | Value |",
                    "|---|---|",
                    f"| Source role | `infra.aap_configuration.{source['role']}` |",
                    f"| Recommended path | `base/{folder_for(key)}/<resource>.yml` |",
                    f"| Ownership | {ownership_for(key)} |",
                    f"| Value type | `{metadata['value_type']}` |",
                    f"| Merge mode | `{metadata['merge_mode']}` |",
                    f"| Identity field | `{identity}` |",
                    f"| Naming policy | {'Supported' if metadata['naming_supported'] else 'Not supported'} |",
                    f"| Drift comparison | {'identity_presence (declared identities only)' if key in drift_keys else 'unsupported'} |",
                    f"| Explicit deletion | {'Supported' if metadata['deletion_supported'] else 'Rejected by engine'} |",
                    "",
                    "#### Valid YAML example",
                    "",
                    f"<!-- catalog-example:{key} -->",
                    "```yaml",
                    "---",
                    dump_yaml(example),
                    "```",
                    "",
                    "#### Parameter reference",
                    "",
                    f"Source: pinned `{source['source']}` at collection version `{version}`.",
                ]
            )
            for group in source["parameter_groups"]:
                lines.extend(
                    [
                        "",
                        f"##### {group['name']}",
                        "",
                        "| Parameter | Type | Requirement | Default | Description |",
                        "|---|---|---|---|---|",
                    ]
                )
                for parameter in group["parameters"]:
                    lines.append(
                        "| `{}` | {} | {} | {} | {} |".format(
                            _escape_table(parameter["name"]),
                            _escape_table(parameter.get("type")),
                            _escape_table(parameter.get("required")),
                            _escape_table(parameter.get("default")),
                            _escape_table(parameter.get("description")),
                        )
                    )
            lines.append("")

    lines.extend(
        [
            "",
            "## Unsupported action keys",
            "",
            "These collection dispatch variables are intentionally rejected because they perform actions rather than declare desired state:",
            "",
            "| Key | Reason |",
            "|---|---|",
        ]
    )
    for key, value in schema.get("unsupported", {}).items():
        lines.append(f"| `{key}` | {_escape_table(value.get('reason'))} |")
    lines.append("")
    return "\n".join(lines)


def write_or_check(rendered: str, check: bool) -> None:
    if check:
        current = OUTPUT.read_text() if OUTPUT.exists() else ""
        if current != rendered:
            diff = "\n".join(
                difflib.unified_diff(
                    current.splitlines(),
                    rendered.splitlines(),
                    fromfile=str(OUTPUT),
                    tofile="generated",
                    lineterm="",
                )
            )
            print(diff)
            raise SystemExit("RESOURCE_CATALOG.md is out of date")
        print(f"{OUTPUT.relative_to(ROOT)} is current")
        return
    OUTPUT.write_text(rendered)
    print(f"Wrote {OUTPUT.relative_to(ROOT)}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--extract-collection-root",
        type=Path,
        help="Write the normalized parameter snapshot from a pinned collection root",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail when the generated Markdown differs from the committed file",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.extract_collection_root:
        snapshot = extract_parameter_snapshot(args.extract_collection_root)
        PARAMETERS.write_text(dump_yaml(snapshot) + "\n")
        print(f"Wrote {PARAMETERS.relative_to(ROOT)}")
    if not PARAMETERS.exists():
        raise SystemExit(
            f"{PARAMETERS} is missing; run with --extract-collection-root first"
        )
    write_or_check(render_catalog(), args.check)


if __name__ == "__main__":
    try:
        main()
    except (KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
