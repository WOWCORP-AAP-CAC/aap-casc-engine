#!/usr/bin/env python3
"""Shared CasC CI runtime helpers for GitHub and GitLab pipelines.

Token-only credential model. No username/password or fixed AAP_<ENV>_* paths.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import yaml

from repo_name_overrides import (
    DEFAULT_TENANT_FOLDERS,
    resolve_tenant_repo_map,
    validate_tenant_id,
)


DEFAULT_JT = {
    "genesis": "jt-platform-genesis",
    "bootstrap": "jt-platform-bootstrap_tenant",
    "dispatcher": "jt-platform-casc_dispatcher",
    "drift_detection": "jt-platform-drift_detection",
}


def normalize_host(raw: str) -> str:
    raw = (raw or "").rstrip("/")
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return f"https://{raw}"


def mask_token(token: str) -> None:
    if not token:
        return
    # GitHub Actions
    print(f"::add-mask::{token}")
    # Also emit a sanitized notice for GitLab logs
    if os.environ.get("GITLAB_CI"):
        print("Masked AAP launcher token for subsequent log lines.")


def resolve_env_creds(env_key: str, targets_json: str | None = None) -> tuple[str, list[str]]:
    raw = targets_json if targets_json is not None else os.environ.get("AAP_ENV_TARGETS_JSON", "")
    if not raw:
        raise ValueError(f"AAP_ENV_TARGETS_JSON is required for env={env_key}")
    try:
        targets = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("AAP_ENV_TARGETS_JSON must be valid JSON") from exc
    if not isinstance(targets, dict):
        raise ValueError("AAP_ENV_TARGETS_JSON must be a JSON object keyed by environment")
    entry = targets.get(env_key)
    if not isinstance(entry, dict):
        raise ValueError(f"No credentials found for env={env_key}")
    host = entry.get("host")
    token = entry.get("token")
    if not host or not token:
        raise ValueError(f"env={env_key} requires non-empty host and token")
    if entry.get("username") or entry.get("password"):
        raise ValueError(f"env={env_key} must use bearer token only; username/password is rejected")
    mask_token(str(token))
    return normalize_host(str(host)), ["-H", f"Authorization: Bearer {token}"]


def wait_for_terminal(host: str, auth_args: list[str], job_id: int, timeout_minutes: int) -> None:
    checks = max(1, int(timeout_minutes) * 6)
    for i in range(1, checks + 1):
        subprocess.run(["sleep", "10"], check=False)
        result = subprocess.run(
            ["curl", "-sk"] + auth_args + [f"{host}/api/controller/v2/jobs/{job_id}/"],
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            status = json.loads(result.stdout).get("status", "unknown")
        except json.JSONDecodeError:
            status = "unknown"
        print(f"  Status: {status} ({i}/{checks})")
        if status == "successful":
            return
        if status in ("failed", "error", "canceled"):
            raise RuntimeError(f"Dispatcher job {job_id} ended {status}")
    raise RuntimeError(f"Dispatcher job {job_id} did not complete within {timeout_minutes} minutes")


def load_yaml_file(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


EXCLUDED_RESOURCE_DIRS = {
    ".git",
    ".github",
    ".schemas",
    ".engine",
    ".engine-runtime",
    ".scripts",
    ".control",
    ".aap-casc-engine",
}
EXCLUDED_RESOURCE_FILES = {"config.yml", "tenants.yml", "naming-rules.yml"}


def desired_state_search_dirs(root: str) -> list[str]:
    """Return top-level desired-state directories (base/ + env dirs), never repo root."""
    dirs: list[str] = []
    base = os.path.join(root, "base")
    if os.path.isdir(base):
        dirs.append("base")
    try:
        names = sorted(os.listdir(root))
    except FileNotFoundError:
        return dirs
    for name in names:
        if name == "base" or name in EXCLUDED_RESOURCE_DIRS:
            continue
        path = os.path.join(root, name)
        if os.path.isdir(path):
            dirs.append(name)
    return dirs


def iter_resource_yaml_files(root: str, caller_role: str = "tenant") -> list[str]:
    """Return desired-state resource YAML for platform/tenant callers.

    Control repositories hold control metadata (config.yml, tenants.yml, optional
    naming-rules.yml), not AAP desired state. Arbitrary root YAML in a control
    repo must not be treated as CasC resources.
    """
    role = (caller_role or "tenant").strip().lower()
    if role == "control":
        return []

    paths: list[str] = []
    for search_dir in desired_state_search_dirs(root):
        start = os.path.join(root, search_dir)
        for current, dirs, files in os.walk(start):
            dirs[:] = [name for name in dirs if name not in EXCLUDED_RESOURCE_DIRS]
            for name in files:
                if name in EXCLUDED_RESOURCE_FILES or name.endswith(".sample"):
                    continue
                if not name.endswith((".yml", ".yaml")):
                    continue
                paths.append(os.path.join(current, name))
    return sorted(paths)


def validate_structure(
    root: str,
    resource_types_path: str,
    allowed_keys_path: str = "",
    caller_role: str = "tenant",
) -> None:
    """Validate desired-state YAML shape for platform/tenant callers."""
    role = (caller_role or "tenant").strip().lower()
    if role == "control":
        print("Control repo: skipping desired-state structural validation")
        return

    resource_types: dict[str, Any] = {
        "defaults": {"value_type": "list", "identity_field": "name"},
        "exceptions": {},
    }
    if os.path.exists(resource_types_path):
        loaded = load_yaml_file(resource_types_path)
        resource_types.update(loaded)
    defaults = resource_types.get("defaults") or {}
    exceptions = resource_types.get("exceptions") or {}

    allowed_keys = None
    key_candidates = []
    if allowed_keys_path:
        key_candidates.append(allowed_keys_path)
    key_candidates.extend(
        [
            os.path.join(root, ".schemas", "engine_defaults.yml"),
            os.path.join(
                root, ".engine", "roles", "process_casc_config", "defaults", "main.yml"
            ),
        ]
    )
    for candidate in key_candidates:
        if candidate and os.path.exists(candidate):
            allowed_keys = set(
                (load_yaml_file(candidate).get("casc_allowed_resource_keys") or [])
            )
            break

    errors: list[str] = []
    paths = iter_resource_yaml_files(root, caller_role=role)
    if not paths:
        print("No desired-state YAML found — structural validation skipped")
        return

    for fpath in paths:
        try:
            data = load_yaml_file(fpath)
        except Exception as exc:  # noqa: BLE001 - surface parse errors to CI
            errors.append(f"{fpath}: Failed to parse YAML — {exc}")
            continue
        keys = list(data.keys())
        if len(keys) != 1:
            errors.append(
                f"{fpath}: Expected exactly 1 top-level key, got {len(keys)}: {keys}"
            )
            continue
        key = keys[0]
        if allowed_keys is not None and key not in allowed_keys:
            errors.append(
                f'{fpath}: Unknown resource key "{key}" (not in casc_allowed_resource_keys)'
            )
            continue
        exc_meta = exceptions.get(key) or {}
        vtype = exc_meta.get("value_type", defaults.get("value_type", "list"))
        id_field = exc_meta.get(
            "identity_field", defaults.get("identity_field", "name")
        )
        value = data[key]
        if vtype == "list":
            if not isinstance(value, list):
                errors.append(
                    f'{fpath}: Key "{key}" expected list, got {type(value).__name__}'
                )
                continue
            for index, item in enumerate(value):
                if isinstance(item, dict) and id_field not in item:
                    errors.append(
                        f'{fpath}: Item {index} in "{key}" missing identity field '
                        f'"{id_field}"'
                    )

    if errors:
        raise ValueError(
            "Structural validation errors:\n  " + "\n  ".join(errors)
        )
    print("=== ALL YAML FILES PASSED STRUCTURAL VALIDATION ===")


def validate_explicit_deletions(
    root: str, resource_types_path: str, caller_role: str = "tenant"
) -> None:
    """Fail closed when YAML requests deletion without audited schema support."""
    role = (caller_role or "tenant").strip().lower()
    if role == "control":
        print("Control repo: skipping desired-state deletion validation")
        return

    schema = load_yaml_file(resource_types_path)
    defaults = schema.get("defaults") or {}
    exceptions = schema.get("exceptions") or {}
    errors: list[str] = []

    for path in iter_resource_yaml_files(root, caller_role=role):
        document = load_yaml_file(path)
        if len(document) != 1:
            continue  # Structural validation reports this with better context.
        resource_key, value = next(iter(document.items()))
        metadata = dict(defaults)
        metadata.update(exceptions.get(resource_key) or {})
        field = metadata.get("deletion_field", "state")
        values = metadata.get("deletion_values", ["absent"])
        if not isinstance(field, str) or not field:
            raise ValueError(f"{resource_key}: deletion_field must be a non-empty string")
        if not isinstance(values, list) or not values:
            raise ValueError(f"{resource_key}: deletion_values must be a non-empty list")

        candidates = value if isinstance(value, list) else [value]
        for index, item in enumerate(candidates):
            if not isinstance(item, dict) or item.get(field) not in values:
                continue
            if not metadata.get("deletion_supported", False):
                errors.append(
                    f'{path}: item {index} in "{resource_key}" requests '
                    f'{field}={item.get(field)!r}, but deletion is not audited'
                )
                continue
            if not str(metadata.get("deletion_evidence") or "").strip():
                errors.append(
                    f'{path}: "{resource_key}" enables deletion without deletion_evidence'
                )

    if errors:
        raise ValueError("Unsupported explicit deletion:\n  " + "\n  ".join(errors))

def resolve_jt_names(cfg: dict[str, Any]) -> dict[str, str]:
    configured = cfg.get("job_templates") or {}
    if configured and not isinstance(configured, dict):
        raise ValueError("config.yml job_templates must be a mapping")
    names = dict(DEFAULT_JT)
    for key, default in DEFAULT_JT.items():
        value = configured.get(key, default)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"job_templates.{key} must be a non-empty string")
        names[key] = value.strip()
    return names


def github_raw(org: str, repo: str, path: str, ref: str, token: str) -> bytes:
    url = (
        f"https://api.github.com/repos/{org}/{repo}/contents/"
        f"{urllib.parse.quote(path)}?ref={urllib.parse.quote(ref)}"
    )
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3.raw",
            "User-Agent": "aap-casc-engine",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def github_commit_sha(org: str, repo: str, ref: str, token: str) -> str:
    url = f"https://api.github.com/repos/{org}/{repo}/commits/{urllib.parse.quote(ref)}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "aap-casc-engine",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    sha = payload.get("sha")
    if not sha:
        raise RuntimeError(f"Could not resolve control revision for {org}/{repo}@{ref}")
    return sha


def gitlab_raw(api_url: str, project: str, path: str, ref: str, token: str) -> bytes:
    project_enc = urllib.parse.quote(project, safe="")
    path_enc = urllib.parse.quote(path, safe="")
    ref_enc = urllib.parse.quote(ref, safe="")
    url = f"{api_url.rstrip('/')}/projects/{project_enc}/repository/files/{path_enc}/raw?ref={ref_enc}"
    req = urllib.request.Request(
        url,
        headers={"PRIVATE-TOKEN": token, "User-Agent": "aap-casc-engine"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def gitlab_commit_sha(api_url: str, project: str, ref: str, token: str) -> str:
    project_enc = urllib.parse.quote(project, safe="")
    ref_enc = urllib.parse.quote(ref, safe="")
    url = f"{api_url.rstrip('/')}/projects/{project_enc}/repository/commits/{ref_enc}"
    req = urllib.request.Request(
        url,
        headers={"PRIVATE-TOKEN": token, "User-Agent": "aap-casc-engine"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    sha = payload.get("id")
    if not sha:
        raise RuntimeError(f"Could not resolve control revision for {project}@{ref}")
    return sha


def fetch_control_text(
    *,
    provider: str,
    org: str,
    repo: str,
    path: str,
    revision: str,
    token: str,
    gitlab_api: str | None = None,
) -> str:
    if provider == "github":
        return github_raw(org, repo, path, revision, token).decode("utf-8")
    if provider == "gitlab":
        project = f"{org}/{repo}"
        return gitlab_raw(gitlab_api or os.environ["CI_API_V4_URL"], project, path, revision, token).decode(
            "utf-8"
        )
    raise ValueError(f"Unsupported provider: {provider}")


def ensure_control_files(
    *,
    provider: str,
    org: str,
    repo: str,
    branch: str,
    token: str,
    revision: str | None = None,
    gitlab_api: str | None = None,
    dest_dir: str = ".",
) -> str:
    if not org or not repo or not branch:
        raise ValueError("control_scm_org, control_repo, and control_branch are required")
    if not token:
        raise ValueError("CONTROL_REPO_TOKEN is required to fetch authoritative control metadata")
    if revision:
        if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", revision) is None:
            raise ValueError("control_revision must be a full hexadecimal commit SHA")
        control_revision = revision.lower()
    elif provider == "github":
        control_revision = github_commit_sha(org, repo, branch, token)
    else:
        control_revision = gitlab_commit_sha(
            gitlab_api or os.environ["CI_API_V4_URL"], f"{org}/{repo}", branch, token
        )

    os.makedirs(dest_dir, exist_ok=True)
    for relative in ("config.yml", "tenants.yml"):
        try:
            content = fetch_control_text(
                provider=provider,
                org=org,
                repo=repo,
                path=relative,
                revision=control_revision,
                token=token,
                gitlab_api=gitlab_api,
            )
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"Failed to fetch {org}/{repo}:{relative}@{control_revision}: HTTP {exc.code}"
            ) from exc
        out = os.path.join(dest_dir, relative)
        with open(out, "w", encoding="utf-8") as handle:
            handle.write(content)
    naming_path = os.path.join(dest_dir, "naming-rules.yml")
    if os.path.exists(naming_path):
        os.remove(naming_path)
    try:
        naming_rules = fetch_control_text(
            provider=provider,
            org=org,
            repo=repo,
            path="naming-rules.yml",
            revision=control_revision,
            token=token,
            gitlab_api=gitlab_api,
        )
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise RuntimeError(
                f"Failed to fetch {org}/{repo}:naming-rules.yml@{control_revision}: "
                f"HTTP {exc.code}"
            ) from exc
    else:
        with open(naming_path, "w", encoding="utf-8") as handle:
            handle.write(naming_rules)
    return control_revision


def _required_string(record: dict[str, Any], field: str, tenant_id: str = "") -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        prefix = f"Tenant {tenant_id} " if tenant_id else "Tenant "
        raise ValueError(f"{prefix}{field} must be a non-empty string")
    return value.strip()


TENANT_RECORD_FIELDS = {
    "tenant_id",
    "aap_organization",
    "team_name",
    "tenant_scm_org",
    "tenant_scm_namespace_id",
    "repo_pattern",
    "repo_mode",
    "repo_visibility",
    "repo_name",
    "repo_names",
    "onboarding_mode",
    "status",
    "dispatch_enabled",
}


def normalize_tenant_record(record: dict[str, Any]) -> dict[str, Any]:
    """Validate one customer-facing tenant record and derive runtime-only fields."""
    if not isinstance(record, dict):
        raise ValueError("Each tenants.yml entry must be a mapping")
    unknown = sorted(set(record) - TENANT_RECORD_FIELDS)
    if unknown:
        raise ValueError("Unsupported tenant fields: " + ", ".join(unknown))

    tenant_id = validate_tenant_id(record.get("tenant_id"))
    onboarding_mode = record.get("onboarding_mode", "greenfield")
    if onboarding_mode not in ("greenfield", "brownfield"):
        raise ValueError(
            f"Tenant {tenant_id} onboarding_mode must be greenfield or brownfield"
        )

    explicit_org = record.get("aap_organization")
    if explicit_org is not None and (
        not isinstance(explicit_org, str) or not explicit_org.strip()
    ):
        raise ValueError(f"Tenant {tenant_id} aap_organization must be a non-empty string")
    if onboarding_mode == "brownfield" and not explicit_org:
        raise ValueError(
            f"Tenant {tenant_id} brownfield onboarding requires explicit aap_organization"
        )
    aap_organization = explicit_org.strip() if explicit_org else tenant_id

    team_name = record.get("team_name")
    if onboarding_mode == "greenfield":
        team_name = _required_string(record, "team_name", tenant_id)
    elif team_name not in (None, ""):
        raise ValueError(f"Tenant {tenant_id} brownfield onboarding does not accept team_name")

    tenant_scm_org = _required_string(record, "tenant_scm_org", tenant_id)
    namespace_id = record.get("tenant_scm_namespace_id")
    if namespace_id not in (None, ""):
        if not isinstance(namespace_id, (str, int)) or not str(namespace_id).strip():
            raise ValueError(
                f"Tenant {tenant_id} tenant_scm_namespace_id must be a non-empty scalar"
            )
        namespace_id = str(namespace_id).strip()
    repo_pattern = record.get("repo_pattern", "combined")
    repo_mode = record.get("repo_mode", "create")
    status = record.get("status", "active")
    repo_visibility = record.get("repo_visibility", "private")
    if repo_mode not in ("create", "existing"):
        raise ValueError(f"Tenant {tenant_id} repo_mode must be create or existing")
    if status not in ("active", "inactive"):
        raise ValueError(f"Tenant {tenant_id} status must be active or inactive")
    if repo_visibility not in ("private", "public"):
        raise ValueError(f"Tenant {tenant_id} repo_visibility must be private or public")
    dispatch_enabled = record.get("dispatch_enabled", True)
    if not isinstance(dispatch_enabled, bool):
        raise ValueError(f"Tenant {tenant_id} dispatch_enabled must be a boolean")

    repo_map = resolve_tenant_repo_map(
        repo_pattern=repo_pattern,
        tenant_id=tenant_id,
        repo_name=record.get("repo_name", ""),
        repo_names=record.get("repo_names"),
    )
    repos = list(dict.fromkeys(repo_map[folder] for folder in DEFAULT_TENANT_FOLDERS))
    normalized = dict(record)
    normalized.update(
        {
            "tenant_id": tenant_id,
            "aap_organization": aap_organization,
            "tenant_scm_org": tenant_scm_org,
            "repo_pattern": repo_pattern,
            "repo_mode": repo_mode,
            "onboarding_mode": onboarding_mode,
            "repo_visibility": repo_visibility,
            "status": status,
            "dispatch_enabled": dispatch_enabled,
            "_repo_by_folder": repo_map,
            "_repositories": repos,
        }
    )
    if namespace_id not in (None, ""):
        normalized["tenant_scm_namespace_id"] = namespace_id
    else:
        normalized.pop("tenant_scm_namespace_id", None)
    if onboarding_mode == "greenfield":
        normalized["team_name"] = team_name
    else:
        normalized.pop("team_name", None)
    return normalized


def normalize_runtime_tenant(record: dict[str, Any]) -> dict[str, Any]:
    """Normalize raw or engine-produced tenant data without widening tenants.yml."""
    if not isinstance(record, dict):
        raise ValueError("Tenant runtime data must be a mapping")
    normalized = normalize_tenant_record(
        {key: value for key, value in record.items() if key in TENANT_RECORD_FIELDS}
    )
    supplied_repo_map = record.get("_repo_by_folder", record.get("repo_by_folder"))
    supplied_repos = record.get("_repositories", record.get("repositories"))
    if supplied_repo_map is not None and supplied_repo_map != normalized["_repo_by_folder"]:
        raise ValueError("Derived repo_by_folder does not match the canonical tenant resolver")
    if supplied_repos is not None and supplied_repos != normalized["_repositories"]:
        raise ValueError("Derived repositories do not match the canonical tenant resolver")
    return normalized


def _platform_repo_owners(cfg: dict[str, Any]) -> set[tuple[str, str]]:
    control_org = str(cfg.get("control_scm_org") or cfg.get("platform_scm_org") or "").strip()
    platform_org = str(cfg.get("platform_scm_org") or "").strip()
    owners: set[tuple[str, str]] = set()
    if control_org and cfg.get("control_repo"):
        owners.add((control_org, str(cfg["control_repo"]).strip()))
    if platform_org:
        if cfg.get("platform_repo_pattern", "combined") == "combined":
            owners.add((platform_org, str(cfg.get("platform_repo", "casc-platform-global")).strip()))
        else:
            for entry in cfg.get("platform_repos") or []:
                if isinstance(entry, dict) and entry.get("name"):
                    owners.add((platform_org, str(entry["name"]).strip()))
    return owners


def validate_tenant_registry(
    tenants_doc: dict[str, Any], cfg: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Validate global tenant identity and repository ownership invariants."""
    if not isinstance(tenants_doc, dict):
        raise ValueError("tenants.yml must contain a mapping")
    tenants = tenants_doc.get("tenants", [])
    if not isinstance(tenants, list):
        raise ValueError("tenants.yml tenants must be a list")
    normalized = [normalize_tenant_record(record) for record in tenants]
    ids: dict[str, int] = {}
    orgs: dict[str, str] = {}
    repo_owners: dict[tuple[str, str], str] = {
        owner: "control/platform" for owner in _platform_repo_owners(cfg or {})
    }
    for index, tenant in enumerate(normalized):
        tenant_id = tenant["tenant_id"]
        if tenant_id in ids:
            raise ValueError(f"Duplicate tenant_id '{tenant_id}' at entries {ids[tenant_id]} and {index}")
        ids[tenant_id] = index
        aap_org = tenant["aap_organization"]
        if aap_org in orgs:
            raise ValueError(
                f"AAP Organization '{aap_org}' is assigned to both {orgs[aap_org]} and {tenant_id}"
            )
        orgs[aap_org] = tenant_id
        for repo in tenant["_repositories"]:
            owner = (tenant["tenant_scm_org"], repo)
            if owner in repo_owners:
                raise ValueError(
                    f"Repository {owner[0]}/{owner[1]} is owned by both "
                    f"{repo_owners[owner]} and {tenant_id}"
                )
            repo_owners[owner] = tenant_id
    return normalized


def find_tenant(tenants_doc: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    requested = validate_tenant_id(tenant_id)
    for tenant in validate_tenant_registry(tenants_doc):
        if tenant["tenant_id"] == requested:
            return tenant
    raise ValueError(f"Tenant tenant_id={requested} is not registered in tenants.yml")


SCAFFOLD_VERSION = 2


def build_scaffold_marker(
    tenant: dict[str, Any], *, repository: str, resource_type: str
) -> dict[str, Any]:
    """Build the immutable provider marker for one resolved tenant repository."""
    normalized = normalize_runtime_tenant(tenant)
    if repository not in normalized["_repositories"]:
        raise ValueError(
            f"Repository {repository} is not part of tenant {normalized['tenant_id']} topology"
        )
    if normalized["repo_pattern"] == "combined":
        expected_type = "combined"
    else:
        expected_types = [
            folder
            for folder, repo_name in normalized["_repo_by_folder"].items()
            if repo_name == repository
        ]
        if len(expected_types) != 1:
            raise ValueError(f"Could not resolve one resource type for repository {repository}")
        expected_type = expected_types[0]
    if resource_type != expected_type:
        raise ValueError(
            f"Repository {repository} resource_type={resource_type}; expected {expected_type}"
        )

    marker: dict[str, Any] = {
        "scaffold_version": SCAFFOLD_VERSION,
        "tenant_id": normalized["tenant_id"],
        "aap_organization": normalized["aap_organization"],
        "tenant_scm_org": normalized["tenant_scm_org"],
        "repo_pattern": normalized["repo_pattern"],
        "repo_mode": normalized["repo_mode"],
        "repo_visibility": normalized["repo_visibility"],
        "onboarding_mode": normalized["onboarding_mode"],
        "repository": repository,
        "resource_type": expected_type,
        "repositories": normalized["_repo_by_folder"],
    }
    namespace_id = normalized.get("tenant_scm_namespace_id")
    if namespace_id not in (None, ""):
        marker["tenant_scm_namespace_id"] = str(namespace_id)
    if normalized["onboarding_mode"] == "greenfield":
        marker["team_name"] = normalized["team_name"]
    return marker


def validate_scaffold_marker(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    """Fail when an existing marker does not exactly own the requested scaffold."""
    if not isinstance(actual, dict):
        raise ValueError("Existing tenant scaffold marker must be a YAML mapping")
    if actual.get("scaffold_version") != SCAFFOLD_VERSION:
        raise ValueError(
            f"Unsupported scaffold marker version {actual.get('scaffold_version')}; "
            f"expected {SCAFFOLD_VERSION}"
        )
    mismatches = [key for key, value in expected.items() if actual.get(key) != value]
    mismatches.extend(sorted(set(actual) - set(expected)))
    if mismatches:
        raise ValueError(
            "Existing scaffold marker conflicts with requested tenant fields: "
            + ", ".join(sorted(set(mismatches)))
        )


def public_tenant_runtime(tenant: dict[str, Any]) -> dict[str, Any]:
    """Return normalized runtime data suitable for JSON/Ansible consumption."""
    normalized = normalize_runtime_tenant(tenant)
    return {
        key: value
        for key, value in normalized.items()
        if not key.startswith("_")
    } | {
        "repo_by_folder": normalized["_repo_by_folder"],
        "repositories": normalized["_repositories"],
    }


def tenant_immutable_projection(tenant: dict[str, Any]) -> dict[str, Any]:
    """Return the post-scaffold immutable tenant identity/topology contract."""
    normalized = normalize_runtime_tenant(tenant)
    projection = {
        "tenant_id": normalized["tenant_id"],
        "aap_organization": normalized["aap_organization"],
        "tenant_scm_org": normalized["tenant_scm_org"],
        "tenant_scm_namespace_id": str(
            normalized.get("tenant_scm_namespace_id") or ""
        ),
        "repo_pattern": normalized["repo_pattern"],
        "repo_mode": normalized["repo_mode"],
        "repo_visibility": normalized["repo_visibility"],
        "onboarding_mode": normalized["onboarding_mode"],
        "repositories": normalized["_repo_by_folder"],
    }
    if normalized["onboarding_mode"] == "greenfield":
        projection["team_name"] = normalized["team_name"]
    return projection


def _tenant_marker_exists(
    tenant: dict[str, Any],
    *,
    provider: str,
    token: str,
    refs: list[str],
    gitlab_api: str = "",
) -> bool:
    normalized = normalize_runtime_tenant(tenant)
    marker = ".aap-casc-engine/tenant-scaffold.yml"
    for repo_name in normalized["_repositories"]:
        for ref in refs:
            if provider == "github":
                exists = github_file_exists(
                    normalized["tenant_scm_org"], repo_name, marker, ref, token
                )
            elif provider == "gitlab":
                exists = gitlab_file_exists(
                    gitlab_api or os.environ["CI_API_V4_URL"],
                    f"{normalized['tenant_scm_org']}/{repo_name}",
                    marker,
                    ref,
                    token,
                )
            else:
                raise ValueError("provider must be github or gitlab")
            if exists:
                return True
    return False


def diff_tenant_actions(
    previous_doc: dict[str, Any],
    current_doc: dict[str, Any],
    cfg: dict[str, Any],
    *,
    marker_exists: Any,
) -> list[dict[str, Any]]:
    """Return Bootstrap actions while enforcing marker-based lifecycle immutability."""
    previous = {
        item["tenant_id"]: item for item in validate_tenant_registry(previous_doc, cfg)
    }
    current = {
        item["tenant_id"]: item for item in validate_tenant_registry(current_doc, cfg)
    }
    actions: list[dict[str, Any]] = []

    for tenant_id in sorted(set(previous) | set(current)):
        old = previous.get(tenant_id)
        new = current.get(tenant_id)
        if new is None:
            if old is not None and marker_exists(old):
                raise ValueError(
                    f"Tenant {tenant_id} has scaffold markers and cannot be removed in place; "
                    "use an explicit retirement/migration procedure"
                )
            continue
        if old is None:
            if new["status"] == "active":
                actions.append({"action": "added", "tenant": public_tenant_runtime(new)})
            continue

        immutable_changed = tenant_immutable_projection(old) != tenant_immutable_projection(new)
        if immutable_changed:
            if marker_exists(old) or marker_exists(new):
                raise ValueError(
                    f"Tenant {tenant_id} scaffold identity/topology is immutable after the first marker; "
                    "use an explicit migration procedure"
                )
            if new["status"] == "active":
                actions.append(
                    {"action": "corrected", "tenant": public_tenant_runtime(new)}
                )
            continue

        if old["status"] == "inactive" and new["status"] == "active":
            if not marker_exists(new):
                actions.append(
                    {"action": "activated", "tenant": public_tenant_runtime(new)}
                )

    return actions


def resolve_bootstrap_request(
    tenants_doc: dict[str, Any], cfg: dict[str, Any], request: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Resolve a registered authoritative tenant or validate an unregistered request."""
    normalized_registry = validate_tenant_registry(tenants_doc, cfg)
    requested_id = validate_tenant_id(request.get("tenant_id"))
    registered = next(
        (tenant for tenant in normalized_registry if tenant["tenant_id"] == requested_id),
        None,
    )
    if registered is None:
        candidate_doc = {
            "tenants": [
                {key: value for key, value in tenant.items() if not key.startswith("_")}
                for tenant in normalized_registry
            ]
            + [request]
        }
        candidate = next(
            tenant
            for tenant in validate_tenant_registry(candidate_doc, cfg)
            if tenant["tenant_id"] == requested_id
        )
        return public_tenant_runtime(candidate), False

    comparable_fields = (
        "aap_organization",
        "team_name",
        "tenant_scm_org",
        "tenant_scm_namespace_id",
        "repo_pattern",
        "repo_mode",
        "repo_name",
        "repo_names",
        "onboarding_mode",
        "repo_visibility",
    )
    conflicts = []
    registered_public = public_tenant_runtime(registered)
    for field in comparable_fields:
        supplied = request.get(field)
        if supplied in (None, "", {}):
            continue
        authoritative = registered_public.get(field)
        if supplied != authoritative:
            conflicts.append(field)
    if conflicts:
        raise ValueError(
            f"Tenant {requested_id} is registered; direct inputs conflict with Git for: "
            + ", ".join(conflicts)
        )
    return registered_public, True


def github_file_exists(org: str, repo: str, path: str, ref: str, token: str) -> bool:
    try:
        github_raw(org, repo, path, ref, token)
        return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise


def gitlab_file_exists(
    api_url: str, project: str, path: str, ref: str, token: str
) -> bool:
    try:
        gitlab_raw(api_url, project, path, ref, token)
        return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise


# Greenfield foundation artifacts written by Bootstrap.
FOUNDATION_RESOURCES = (("organizations", "organizations"), ("teams", "teams"))


def iter_foundation_targets(cfg: dict[str, Any], tenant_id: str) -> list[tuple[str, str]]:
    """Return (repo_name, path) pairs for every required greenfield foundation file."""
    tenant_id = validate_tenant_id(tenant_id)
    platform_pattern = cfg.get("platform_repo_pattern", "combined")
    platform_repo = cfg.get("platform_repo", "casc-platform-global")
    repo_lookup = {
        entry.get("resource_type"): entry.get("name")
        for entry in (cfg.get("platform_repos") or [])
        if isinstance(entry, dict) and entry.get("resource_type") and entry.get("name")
    }
    targets: list[tuple[str, str]] = []
    for resource_type, combined_folder in FOUNDATION_RESOURCES:
        filename = f"{tenant_id}.yml"
        if platform_pattern == "combined":
            targets.append((platform_repo, f"base/{combined_folder}/{filename}"))
            continue
        repo_name = repo_lookup.get(resource_type)
        if not repo_name:
            raise ValueError(
                f"platform_repos is missing a repository for resource_type={resource_type}"
            )
        targets.append((repo_name, f"base/{filename}"))
    return targets


def validate_onboarding_preflight(
    *,
    provider: str,
    control_org: str,
    control_repo: str,
    control_revision: str,
    control_token: str,
    tenant_id: str,
    scm_token: str | None = None,
    gitlab_api: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    cfg = load_yaml_file("config.yml")
    tenants_doc = load_yaml_file("tenants.yml")
    normalized_tenants = validate_tenant_registry(tenants_doc, cfg)
    tenant = next((item for item in normalized_tenants if item["tenant_id"] == validate_tenant_id(tenant_id)), None)
    if tenant is None:
        raise ValueError(f"Tenant tenant_id={tenant_id} is not registered in tenants.yml")
    status = tenant.get("status", "active")
    if status != "active":
        raise ValueError(f"Tenant {tenant_id} status={status}; onboarding_dispatch requires active")
    onboarding_mode = tenant.get("onboarding_mode", "greenfield")
    if onboarding_mode != "greenfield":
        raise ValueError(
            f"Tenant {tenant_id} onboarding_mode={onboarding_mode}; "
            "onboarding_dispatch is greenfield-only"
        )
    tenant_scm_org = tenant.get("tenant_scm_org")
    if not tenant_scm_org:
        raise ValueError(f"Tenant {tenant_id} is missing tenant_scm_org")
    repos = tenant["_repositories"]
    env_map = cfg.get("env_branch_map") or {}
    if not isinstance(env_map, dict) or not env_map:
        raise ValueError("config.yml env_branch_map must be a non-empty mapping")
    mapped_branches = list(dict.fromkeys(env_map.values()))

    token = scm_token or control_token
    missing_markers = []
    for repo_name in repos:
        for branch in mapped_branches:
            marker = ".aap-casc-engine/tenant-scaffold.yml"
            if provider == "github":
                exists = github_file_exists(
                    tenant_scm_org, repo_name, marker, branch, token
                )
            else:
                exists = gitlab_file_exists(
                    gitlab_api or os.environ["CI_API_V4_URL"],
                    f"{tenant_scm_org}/{repo_name}",
                    marker,
                    branch,
                    token,
                )
            if not exists:
                missing_markers.append(
                    f"{tenant_scm_org}/{repo_name}@{branch}:{marker}"
                )
    if missing_markers:
        raise ValueError(
            "Incomplete scaffold markers for onboarding_dispatch: " + ", ".join(missing_markers)
        )

    platform_scm_org = cfg.get("platform_scm_org") or control_org
    missing_foundation = []
    for branch in dict.fromkeys(env_map.values()):
        for repo_name, path in iter_foundation_targets(cfg, tenant_id):
            if provider == "github":
                ok = github_file_exists(platform_scm_org, repo_name, path, branch, token)
            else:
                ok = gitlab_file_exists(
                    gitlab_api or os.environ["CI_API_V4_URL"],
                    f"{platform_scm_org}/{repo_name}",
                    path,
                    branch,
                    token,
                )
            if not ok:
                missing_foundation.append(f"{platform_scm_org}/{repo_name}@{branch}:{path}")
    if missing_foundation:
        raise ValueError(
            "Missing greenfield foundation files for onboarding_dispatch: "
            + ", ".join(missing_foundation[:12])
            + (" ..." if len(missing_foundation) > 12 else "")
        )

    print(
        f"Onboarding preflight OK for tenant={tenant_id} "
        f"control={control_org}/{control_repo}@{control_revision}"
    )
    return cfg, tenant, list(env_map.keys())


def launch_dispatcher(
    *,
    host: str,
    auth_args: list[str],
    jt_name: str,
    extra_vars: dict[str, Any],
    require_serialized: bool = True,
) -> int:
    jt_result = subprocess.run(
        ["curl", "-sk"] + auth_args + [f"{host}/api/controller/v2/job_templates/?name={jt_name}"],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        jt_data = json.loads(jt_result.stdout)
        jt_id = jt_data["results"][0]["id"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"JT '{jt_name}' not found on {host}") from exc
    if require_serialized and jt_data["results"][0].get("allow_simultaneous", False):
        raise RuntimeError("Dispatcher JT has allow_simultaneous=true — must be false for serialized mode")
    launch = subprocess.run(
        ["curl", "-sk", "-X", "POST"]
        + auth_args
        + [
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps({"extra_vars": json.dumps(extra_vars)}),
            f"{host}/api/controller/v2/job_templates/{jt_id}/launch/",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        job_id = json.loads(launch.stdout).get("id")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to launch dispatcher: {launch.stdout[:200]}") from exc
    if not job_id:
        raise RuntimeError(f"Failed to launch dispatcher: {launch.stdout[:200]}")
    return int(job_id)


def run_bounded_onboarding(
    *,
    environments: list[str],
    tenant_id: str,
    control_revision: str,
    poll_timeout: int,
    jt_name: str,
    tenant_dispatch_enabled: bool = True,
    trigger_source: str = "onboarding-dispatch",
) -> None:
    if not tenant_id:
        raise ValueError("tenant_id is required")
    if not environments:
        raise ValueError("No environments in env_branch_map")

    resolved: dict[str, tuple[str, list[str]]] = {}
    errors = []
    for env in environments:
        try:
            resolved[env] = resolve_env_creds(env)
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        for err in errors:
            print(f"ERROR: {err}")
        raise SystemExit(1)

    for env in environments:
        host, auth_args = resolved[env]
        print(f"\n=== Onboarding env={env} on {host} ===")
        scopes = [("platform", "")]
        if tenant_dispatch_enabled:
            scopes.append(("tenant", tenant_id))
        else:
            print(f"  Tenant {tenant_id} dispatch is paused; applying platform foundation only.")
        for scope, selected_tenant_id in scopes:
            extra_vars = {
                "target_env": env,
                "dispatch_scope": scope,
                "tenant_id": selected_tenant_id,
                "control_revision": control_revision,
                "trigger_source": trigger_source,
            }
            if scope == "full":
                raise RuntimeError("Refusing to launch full scope from onboarding path")
            job_id = launch_dispatcher(
                host=host,
                auth_args=auth_args,
                jt_name=jt_name,
                extra_vars=extra_vars,
            )
            print(
                f"  Dispatcher launched: job_id={job_id} scope={scope} "
                f"tenant={selected_tenant_id or '-'}"
            )
            wait_for_terminal(host, auth_args, job_id, poll_timeout)
    print("\n=== Bounded onboarding dispatch complete ===")


def cmd_ensure_control(args: argparse.Namespace) -> int:
    revision = ensure_control_files(
        provider=args.provider,
        org=args.control_scm_org,
        repo=args.control_repo,
        branch=args.control_branch,
        token=args.token,
        revision=args.control_revision or None,
        gitlab_api=args.gitlab_api,
        dest_dir=args.dest_dir,
    )
    print(f"control_revision={revision}")
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as handle:
            handle.write(f"control_revision={revision}\n")
    return 0


def cmd_onboarding_dispatch(args: argparse.Namespace) -> int:
    if args.operation != "onboarding_dispatch":
        raise SystemExit("operation must be onboarding_dispatch")
    if args.caller_role != "control":
        raise SystemExit("onboarding_dispatch requires caller_role=control")
    if args.repository != f"{args.control_scm_org}/{args.control_repo}":
        raise SystemExit(
            f"onboarding_dispatch requires repository {args.control_scm_org}/{args.control_repo}, "
            f"got {args.repository}"
        )
    if args.ref_name and args.ref_name != args.control_branch:
        raise SystemExit(
            f"onboarding_dispatch requires control_branch={args.control_branch}, got {args.ref_name}"
        )

    control_revision = ensure_control_files(
        provider=args.provider,
        org=args.control_scm_org,
        repo=args.control_repo,
        branch=args.control_branch,
        token=args.control_token,
        revision=args.control_revision or None,
        gitlab_api=args.gitlab_api,
        dest_dir=".",
    )
    cfg, tenant, environments = validate_onboarding_preflight(
        provider=args.provider,
        control_org=args.control_scm_org,
        control_repo=args.control_repo,
        control_revision=control_revision,
        control_token=args.control_token,
        tenant_id=args.tenant_id,
        scm_token=args.scm_token or args.control_token,
        gitlab_api=args.gitlab_api,
    )
    jt_names = resolve_jt_names(cfg)
    run_bounded_onboarding(
        environments=environments,
        tenant_id=args.tenant_id,
        control_revision=control_revision,
        poll_timeout=args.poll_timeout,
        jt_name=jt_names["dispatcher"],
        tenant_dispatch_enabled=tenant.get("dispatch_enabled", True),
        trigger_source="onboarding-dispatch",
    )
    return 0


def cmd_resolve_jt_names(args: argparse.Namespace) -> int:
    cfg = load_yaml_file(args.config)
    names = resolve_jt_names(cfg)
    print(json.dumps(names))
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as handle:
            for key, value in names.items():
                handle.write(f"{key}_jt_name={value}\n")
    return 0


def cmd_validate_registry(args: argparse.Namespace) -> int:
    cfg = load_yaml_file(args.config)
    tenants_doc = load_yaml_file(args.tenants)
    normalized = [public_tenant_runtime(item) for item in validate_tenant_registry(tenants_doc, cfg)]
    print(json.dumps({"tenants": normalized}, sort_keys=True))
    return 0


def cmd_validate_structure(args: argparse.Namespace) -> int:
    try:
        validate_structure(
            args.root,
            args.resource_types,
            allowed_keys_path=args.allowed_keys,
            caller_role=args.caller_role,
        )
    except ValueError as exc:
        print(str(exc))
        return 1
    return 0


def cmd_validate_deletions(args: argparse.Namespace) -> int:
    try:
        validate_explicit_deletions(
            args.root, args.resource_types, caller_role=args.caller_role
        )
    except ValueError as exc:
        print(str(exc))
        return 1
    print("Explicit deletion validation passed")
    return 0


def cmd_resolve_bootstrap(args: argparse.Namespace) -> int:
    cfg = load_yaml_file(args.config)
    tenants_doc = load_yaml_file(args.tenants)
    request = json.loads(args.request_json)
    tenant, registered = resolve_bootstrap_request(tenants_doc, cfg, request)
    print(json.dumps({"tenant": tenant, "registered": registered}, sort_keys=True))
    return 0


def cmd_scaffold_marker(args: argparse.Namespace) -> int:
    tenant = json.loads(args.tenant_json)
    marker = build_scaffold_marker(
        tenant, repository=args.repository, resource_type=args.resource_type
    )
    if args.actual_json:
        validate_scaffold_marker(json.loads(args.actual_json), marker)
    print(json.dumps(marker, sort_keys=True))
    return 0


def cmd_diff_tenants(args: argparse.Namespace) -> int:
    cfg = load_yaml_file(args.config)
    previous = load_yaml_file(args.previous)
    current = load_yaml_file(args.current)
    mapped_branches = list((cfg.get("env_branch_map") or {}).values())
    if not mapped_branches:
        raise ValueError("config.yml env_branch_map must be a non-empty mapping")
    marker_refs = [args.marker_ref] if args.marker_ref else list(dict.fromkeys(mapped_branches))

    def marker_exists(tenant: dict[str, Any]) -> bool:
        return _tenant_marker_exists(
            tenant,
            provider=args.provider,
            token=args.scm_token,
            refs=marker_refs,
            gitlab_api=args.gitlab_api,
        )

    actions = diff_tenant_actions(
        previous, current, cfg, marker_exists=marker_exists
    )
    payload = json.dumps(actions, sort_keys=True)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
    print(payload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CasC CI runtime helpers")
    sub = parser.add_subparsers(dest="command", required=True)

    ensure = sub.add_parser("ensure-control", help="Fetch control files at pinned revision")
    ensure.add_argument("--provider", choices=["github", "gitlab"], required=True)
    ensure.add_argument("--control-scm-org", required=True)
    ensure.add_argument("--control-repo", required=True)
    ensure.add_argument("--control-branch", required=True)
    ensure.add_argument("--token", required=True)
    ensure.add_argument("--control-revision", default="")
    ensure.add_argument("--gitlab-api", default="")
    ensure.add_argument("--dest-dir", default=".")
    ensure.add_argument("--github-output", default="")
    ensure.set_defaults(func=cmd_ensure_control)

    onboard = sub.add_parser("onboarding-dispatch", help="Protected bounded onboarding continuation")
    onboard.add_argument("--provider", choices=["github", "gitlab"], required=True)
    onboard.add_argument("--operation", required=True)
    onboard.add_argument("--caller-role", required=True)
    onboard.add_argument("--repository", required=True)
    onboard.add_argument("--ref-name", default="")
    onboard.add_argument("--control-scm-org", required=True)
    onboard.add_argument("--control-repo", required=True)
    onboard.add_argument("--control-branch", required=True)
    onboard.add_argument("--control-token", required=True)
    onboard.add_argument("--scm-token", default="")
    onboard.add_argument("--tenant-id", required=True)
    onboard.add_argument("--control-revision", default="")
    onboard.add_argument("--poll-timeout", type=int, default=30)
    onboard.add_argument("--gitlab-api", default="")
    onboard.set_defaults(func=cmd_onboarding_dispatch)

    jt = sub.add_parser("resolve-jt-names", help="Resolve JT names from config.yml")
    jt.add_argument("--config", default="config.yml")
    jt.add_argument("--github-output", default="")
    jt.set_defaults(func=cmd_resolve_jt_names)

    registry = sub.add_parser("validate-registry", help="Validate tenants.yml globally")
    registry.add_argument("--config", default="config.yml")
    registry.add_argument("--tenants", default="tenants.yml")
    registry.set_defaults(func=cmd_validate_registry)

    structure = sub.add_parser(
        "validate-structure",
        help="Validate desired-state YAML structure (role-aware)",
    )
    structure.add_argument("--root", default=".")
    structure.add_argument("--resource-types", required=True)
    structure.add_argument("--allowed-keys", default="")
    structure.add_argument(
        "--caller-role",
        default="tenant",
        choices=["control", "platform", "tenant"],
    )
    structure.set_defaults(func=cmd_validate_structure)

    deletions = sub.add_parser(
        "validate-deletions", help="Reject explicit deletion without audited support"
    )
    deletions.add_argument("--root", default=".")
    deletions.add_argument("--resource-types", required=True)
    deletions.add_argument(
        "--caller-role",
        default="tenant",
        choices=["control", "platform", "tenant"],
    )
    deletions.set_defaults(func=cmd_validate_deletions)
    bootstrap = sub.add_parser("resolve-bootstrap", help="Resolve one Bootstrap request")
    bootstrap.add_argument("--config", required=True)
    bootstrap.add_argument("--tenants", required=True)
    bootstrap.add_argument("--request-json", required=True)
    bootstrap.set_defaults(func=cmd_resolve_bootstrap)

    marker = sub.add_parser("scaffold-marker", help="Render or compare a scaffold marker")
    marker.add_argument("--tenant-json", required=True)
    marker.add_argument("--repository", required=True)
    marker.add_argument("--resource-type", required=True)
    marker.add_argument("--actual-json", default="")
    marker.set_defaults(func=cmd_scaffold_marker)

    tenant_diff = sub.add_parser(
        "diff-tenants", help="Resolve actionable tenant changes and enforce lifecycle"
    )
    tenant_diff.add_argument("--provider", choices=["github", "gitlab"], required=True)
    tenant_diff.add_argument("--config", required=True)
    tenant_diff.add_argument("--previous", required=True)
    tenant_diff.add_argument("--current", required=True)
    tenant_diff.add_argument("--scm-token", required=True)
    tenant_diff.add_argument("--marker-ref", default="")
    tenant_diff.add_argument("--gitlab-api", default="")
    tenant_diff.add_argument("--output", default="tenant_actions.json")
    tenant_diff.set_defaults(func=cmd_diff_tenants)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 - CI entrypoint must fail closed with message
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
