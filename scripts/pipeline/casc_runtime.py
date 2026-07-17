#!/usr/bin/env python3
"""Shared CasC CI runtime helpers for GitHub and GitLab pipelines.

Token-only credential model. No username/password or fixed AAP_<ENV>_* paths.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import yaml


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
        control_revision = revision
    elif provider == "github":
        control_revision = github_commit_sha(org, repo, branch, token)
    else:
        control_revision = gitlab_commit_sha(
            gitlab_api or os.environ["CI_API_V4_URL"], f"{org}/{repo}", branch, token
        )

    os.makedirs(dest_dir, exist_ok=True)
    for relative in ("config.yml", "tenants.yml", "naming-rules.yml"):
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
    return control_revision


def find_tenant(tenants_doc: dict[str, Any], org_id: str) -> dict[str, Any]:
    for tenant in tenants_doc.get("tenants") or []:
        if isinstance(tenant, dict) and tenant.get("org_id") == org_id:
            return tenant
    raise ValueError(f"Tenant org_id={org_id} is not registered in tenants.yml")


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


# Greenfield foundation artifacts written by Bootstrap. Combined layout uses one
# platform repo with typed folders; per-resource-type uses configured platform_repos.
FOUNDATION_RESOURCES = (
    ("organizations", "aap-organizations-global", "org-{org_id}.yml", "organizations"),
    ("teams", "aap-teams-global", "team-{org_id}.yml", "teams"),
    ("users", "aap-users-global", "user-{org_id}.yml", "users"),
    (
        "rbac_user_assignments",
        "gateway-role_user_assignments-global",
        "rbac-user-{org_id}.yml",
        "rbac_user_assignments",
    ),
    (
        "rbac_team_assignments",
        "gateway-role_team_assignments-global",
        "rbac-team-{org_id}.yml",
        "rbac_team_assignments",
    ),
)


def iter_foundation_targets(cfg: dict[str, Any], tenant_org_id: str) -> list[tuple[str, str]]:
    """Return (repo_name, path) pairs for every required greenfield foundation file."""
    if not tenant_org_id:
        raise ValueError("tenant_org_id is required for foundation validation")
    platform_pattern = cfg.get("platform_repo_pattern", "combined")
    platform_repo = cfg.get("platform_repo", "casc-platform-global")
    repo_lookup = {
        entry.get("resource_type"): entry.get("name")
        for entry in (cfg.get("platform_repos") or [])
        if isinstance(entry, dict) and entry.get("resource_type") and entry.get("name")
    }
    targets: list[tuple[str, str]] = []
    for resource_type, default_repo, filename_tmpl, combined_folder in FOUNDATION_RESOURCES:
        filename = filename_tmpl.format(org_id=tenant_org_id)
        if platform_pattern == "combined":
            targets.append((platform_repo, f"base/{combined_folder}/{filename}"))
            continue
        repo_name = repo_lookup.get(resource_type) or default_repo
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
    tenant_org_id: str,
    scm_token: str | None = None,
    gitlab_api: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    cfg = load_yaml_file("config.yml")
    tenants_doc = load_yaml_file("tenants.yml")
    tenant = find_tenant(tenants_doc, tenant_org_id)
    status = tenant.get("status", "active")
    if status != "active":
        raise ValueError(f"Tenant {tenant_org_id} status={status}; onboarding_dispatch requires active")
    onboarding_mode = tenant.get("onboarding_mode", "greenfield")
    if onboarding_mode != "greenfield":
        raise ValueError(
            f"Tenant {tenant_org_id} onboarding_mode={onboarding_mode}; "
            "onboarding_dispatch is greenfield-only"
        )
    if tenant.get("dispatch_enabled", True) is False:
        raise ValueError(f"Tenant {tenant_org_id} has dispatch_enabled=false")

    tenant_scm_org = tenant.get("tenant_scm_org")
    if not tenant_scm_org:
        raise ValueError(f"Tenant {tenant_org_id} is missing tenant_scm_org")
    repos = tenant.get("tenant_repos") or []
    if not repos:
        pattern = tenant.get("repo_pattern", "combined")
        if pattern == "combined":
            repos = [tenant.get("repo_name") or f"casc-tenant-{tenant_org_id}"]
        else:
            raise ValueError(f"Tenant {tenant_org_id} has no tenant_repos for per-resource-type pattern")

    token = scm_token or control_token
    missing_markers = []
    for repo_name in repos:
        marker = ".aap-casc-engine/tenant-scaffold.yml"
        if provider == "github":
            exists = github_file_exists(tenant_scm_org, repo_name, marker, "HEAD", token)
        else:
            exists = gitlab_file_exists(
                gitlab_api or os.environ["CI_API_V4_URL"],
                f"{tenant_scm_org}/{repo_name}",
                marker,
                "HEAD",
                token,
            )
        if not exists:
            missing_markers.append(f"{tenant_scm_org}/{repo_name}:{marker}")
    if missing_markers:
        raise ValueError(
            "Incomplete scaffold markers for onboarding_dispatch: " + ", ".join(missing_markers)
        )

    env_map = cfg.get("env_branch_map") or {}
    if not env_map:
        raise ValueError("config.yml env_branch_map is required")
    platform_scm_org = cfg.get("platform_scm_org") or control_org
    missing_foundation = []
    for branch in env_map.values():
        for repo_name, path in iter_foundation_targets(cfg, tenant_org_id):
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
        f"Onboarding preflight OK for tenant={tenant_org_id} "
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
    tenant_org_id: str,
    control_revision: str,
    poll_timeout: int,
    jt_name: str,
    trigger_source: str = "onboarding-dispatch",
) -> None:
    if not tenant_org_id:
        raise ValueError("tenant_org_id is required")
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
        for scope, oid in [("platform", "")] + [("tenant", tenant_org_id)]:
            extra_vars = {
                "target_env": env,
                "dispatch_scope": scope,
                "tenant_org_id": oid,
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
            print(f"  Dispatcher launched: job_id={job_id} scope={scope} tenant={oid or '-'}")
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
    cfg, _tenant, environments = validate_onboarding_preflight(
        provider=args.provider,
        control_org=args.control_scm_org,
        control_repo=args.control_repo,
        control_revision=control_revision,
        control_token=args.control_token,
        tenant_org_id=args.tenant_org_id,
        scm_token=args.scm_token or args.control_token,
        gitlab_api=args.gitlab_api,
    )
    jt_names = resolve_jt_names(cfg)
    run_bounded_onboarding(
        environments=environments,
        tenant_org_id=args.tenant_org_id,
        control_revision=control_revision,
        poll_timeout=args.poll_timeout,
        jt_name=args.dispatcher_jt_name or jt_names["dispatcher"],
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
    onboard.add_argument("--tenant-org-id", required=True)
    onboard.add_argument("--control-revision", default="")
    onboard.add_argument("--dispatcher-jt-name", default="")
    onboard.add_argument("--poll-timeout", type=int, default=30)
    onboard.add_argument("--gitlab-api", default="")
    onboard.set_defaults(func=cmd_onboarding_dispatch)

    jt = sub.add_parser("resolve-jt-names", help="Resolve JT names from config.yml")
    jt.add_argument("--config", default="config.yml")
    jt.add_argument("--github-output", default="")
    jt.set_defaults(func=cmd_resolve_jt_names)
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
