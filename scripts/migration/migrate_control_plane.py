#!/usr/bin/env python3
"""One-way migration helpers for the dedicated control-plane topology.

Supports:
1) Legacy colocated platform-home -> casc-platform-control split (plan mode)
2) Tenant identity/repository migration plan generation

This tool never silently keeps a steady-state legacy fallback. It emits a
reviewed migration plan and optional local workspace rewrite. Live SCM mutation
requires explicit --apply with credentials and remains operator-gated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml

_PIPELINE_DIR = Path(__file__).resolve().parents[1] / "pipeline"
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))

import casc_runtime  # noqa: E402


CONTROL_FILES = ("config.yml", "tenants.yml", "naming-rules.yml")

LEGACY_MULTI_REPO_TENANT_KEYS = (
    "repo_pattern",
    "repo_names",
    "repositories",
    "repo_by_folder",
    "resource_type",
)


def load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    return data


def dump_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _nonempty_string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _platform_repos_scalar_name(repos: Any) -> str | None:
    """Return a single unambiguous platform repo name from platform_repos, if any."""
    if repos in (None, "", []):
        return None
    if not isinstance(repos, list) or len(repos) != 1 or not isinstance(repos[0], dict):
        return None
    entry = repos[0]
    resource_type = entry.get("resource_type")
    if resource_type not in (None, "", "combined", "platform"):
        return None
    return _nonempty_string(entry.get("name"))


def require_combined_only_source(
    cfg: dict[str, Any], tenants: list[Any] | None = None
) -> None:
    """Fail closed when source still uses per-resource or ambiguous multi-repo shapes.

    Unambiguous scalar names may be preserved. Everything else requires manual
    consolidation before migration.
    """
    reasons: list[str] = []
    pattern = cfg.get("platform_repo_pattern")
    if pattern not in (None, "", "combined"):
        reasons.append(
            f"platform_repo_pattern={pattern!r} (per-resource layouts require manual consolidation)"
        )

    names = cfg.get("platform_repo_names")
    if names not in (None, "", {}):
        reasons.append(
            "platform_repo_names must be absent or an empty mapping; "
            "non-empty or non-mapping values require manual consolidation"
        )

    repos = cfg.get("platform_repos")
    if repos not in (None, "", []):
        if not isinstance(repos, list):
            reasons.append("platform_repos must be a list when present")
        elif len(repos) > 1:
            reasons.append(f"platform_repos lists {len(repos)} repositories")
        elif _platform_repos_scalar_name(repos) is None:
            reasons.append(
                "platform_repos must be a single combined/platform entry with a "
                "non-empty name, or be removed in favor of scalar platform_repo"
            )
        else:
            scalar = _nonempty_string(cfg.get("platform_repo"))
            from_list = _platform_repos_scalar_name(repos)
            if scalar and from_list and scalar != from_list:
                reasons.append(
                    f"platform_repo={scalar!r} disagrees with platform_repos[0].name={from_list!r}"
                )

    if "platform_repo" in cfg and _nonempty_string(cfg.get("platform_repo")) is None:
        reasons.append(
            f"platform_repo must be a non-empty string when present; "
            f"got {cfg.get('platform_repo')!r}"
        )

    for index, tenant in enumerate(tenants or []):
        if not isinstance(tenant, dict):
            reasons.append(f"tenant index={index} must be a mapping")
            continue
        tenant_id = tenant.get("tenant_id") or f"index={index}"
        tenant_pattern = tenant.get("repo_pattern")
        if tenant_pattern not in (None, "", "combined"):
            reasons.append(
                f"tenant {tenant_id}: repo_pattern={tenant_pattern!r} requires manual consolidation"
            )

        repo_names = tenant.get("repo_names")
        if repo_names not in (None, "", {}):
            reasons.append(
                f"tenant {tenant_id}: repo_names must be absent or an empty mapping; "
                "list or non-empty mapping shapes require manual consolidation"
            )

        if "resource_type" in tenant:
            reasons.append(
                f"tenant {tenant_id}: resource_type is not a valid tenants.yml field"
            )

        repo_by_folder = tenant.get("repo_by_folder")
        if repo_by_folder not in (None, "", {}):
            reasons.append(
                f"tenant {tenant_id}: repo_by_folder must be absent or empty"
            )

        repo_name = tenant.get("repo_name")
        if repo_name not in (None, "") and _nonempty_string(repo_name) is None:
            reasons.append(
                f"tenant {tenant_id}: repo_name must be a non-empty string when present; "
                f"got {repo_name!r}"
            )

        repository = tenant.get("repository")
        repository_name = None
        if repository not in (None, ""):
            repository_name = _nonempty_string(repository)
            if repository_name is None:
                reasons.append(
                    f"tenant {tenant_id}: repository must be a non-empty string when present; "
                    f"got {repository!r}"
                )

        repositories = tenant.get("repositories")
        repositories_name = None
        if repositories not in (None, "", []):
            if (
                not isinstance(repositories, list)
                or len(repositories) != 1
                or not _nonempty_string(repositories[0])
            ):
                reasons.append(
                    f"tenant {tenant_id}: repositories must be absent or a "
                    "one-item list of a single repository name"
                )
            else:
                repositories_name = _nonempty_string(repositories[0])

        explicit = _nonempty_string(repo_name)
        scalars = [
            value
            for value in (explicit, repository_name, repositories_name)
            if value is not None
        ]
        if len(set(scalars)) > 1:
            reasons.append(
                f"tenant {tenant_id}: conflicting repository scalars "
                f"repo_name={explicit!r}, repository={repository_name!r}, "
                f"repositories[0]={repositories_name!r}"
            )

    if reasons:
        raise SystemExit(
            "Refusing combined-only migration until per-resource / multi-repository "
            "topology is manually consolidated to scalar platform_repo and tenant "
            "repo_name contracts. Blocking reasons:\n  - "
            + "\n  - ".join(reasons)
        )


def resolve_platform_repo_name(cfg: dict[str, Any], override: str = "") -> str:
    """Resolve the scalar platform repo, preserving unambiguous custom names."""
    if override not in (None, ""):
        name = _nonempty_string(override)
        if name is None:
            raise SystemExit(
                f"--platform-repo-name must be a non-empty string; got {override!r}"
            )
        return name

    # Explicit invalid platform_repo must fail closed — never silently default.
    if "platform_repo" in cfg:
        scalar = _nonempty_string(cfg.get("platform_repo"))
        if scalar is None:
            raise SystemExit(
                f"platform_repo must be a non-empty string when present; "
                f"got {cfg.get('platform_repo')!r}"
            )
        from_list = _platform_repos_scalar_name(cfg.get("platform_repos"))
        if from_list and from_list != scalar:
            raise SystemExit(
                f"platform_repo={scalar!r} disagrees with platform_repos[0].name={from_list!r}"
            )
        return scalar

    from_list = _platform_repos_scalar_name(cfg.get("platform_repos"))
    return from_list or "casc-platform-global"


def transform_tenant_record(tenant: dict[str, Any]) -> dict[str, Any]:
    """Emit a runtime-valid tenants.yml record (no legacy topology fields)."""
    if not isinstance(tenant, dict):
        raise SystemExit("Each tenants.yml entry must be a mapping")
    out = dict(tenant)

    candidates: list[str] = []
    for value in (
        out.get("repo_name"),
        out.get("repository"),
        (
            out.get("repositories")[0]
            if isinstance(out.get("repositories"), list) and out.get("repositories")
            else None
        ),
    ):
        name = _nonempty_string(value)
        if name is not None:
            candidates.append(name)
    if candidates:
        unique = sorted(set(candidates))
        if len(unique) > 1:
            raise SystemExit(
                "Conflicting tenant repository scalars during transform: "
                + ", ".join(unique)
            )
        out["repo_name"] = unique[0]

    for legacy_key in LEGACY_MULTI_REPO_TENANT_KEYS:
        out.pop(legacy_key, None)
    # Persist as customer input repo_name only; never leave derived repository.
    out.pop("repository", None)
    return out


def transform_tenants_doc(tenants_doc: dict[str, Any]) -> dict[str, Any]:
    tenants = tenants_doc.get("tenants", [])
    if tenants in (None, []):
        return {"tenants": []}
    if not isinstance(tenants, list):
        raise SystemExit("tenants.yml tenants must be a list")
    return {
        **{key: value for key, value in tenants_doc.items() if key != "tenants"},
        "tenants": [transform_tenant_record(item) for item in tenants],
    }


def validate_migrated_control(
    cfg: dict[str, Any], tenants_doc: dict[str, Any]
) -> None:
    """Fail closed unless migrated outputs satisfy the current runtime contract."""
    try:
        casc_runtime.reject_legacy_config_fields(cfg)
        casc_runtime.platform_repo_name(cfg)
        casc_runtime.validate_tenant_registry(tenants_doc, cfg)
    except ValueError as exc:
        raise SystemExit(
            f"Migrated control plane is not runtime-valid: {exc}"
        ) from exc


def plan_legacy_split(args: argparse.Namespace) -> int:
    source = Path(args.source_repo).resolve()
    if not source.exists():
        raise SystemExit(f"Source repo not found: {source}")

    missing = [name for name in ("config.yml", "tenants.yml") if not (source / name).exists()]
    if missing:
        raise SystemExit(f"Source is not a legacy colocated control source; missing {missing}")

    desired_state_dirs = [p for p in source.iterdir() if p.is_dir() and p.name in {"base"} or p.name.isalpha()]
    # Detect any base/ or env-like folders
    has_desired = (source / "base").exists() or any(
        (source / name).is_dir() and name not in {".git", ".github", ".gitlab"} and name != "base"
        for name in [p.name for p in source.iterdir()]
    )

    cfg = load_yaml(source / "config.yml")
    tenants_doc = load_yaml(source / "tenants.yml")
    tenants = tenants_doc.get("tenants") or []
    if tenants and not isinstance(tenants, list):
        raise SystemExit("tenants.yml tenants must be a list")
    require_combined_only_source(cfg, tenants if isinstance(tenants, list) else [])

    platform_repo = resolve_platform_repo_name(cfg, getattr(args, "platform_repo_name", "") or "")

    out = Path(args.output_dir).resolve()
    control_out = out / "casc-platform-control"
    platform_out = out / platform_repo
    control_out.mkdir(parents=True, exist_ok=True)
    platform_out.mkdir(parents=True, exist_ok=True)

    cfg["control_scm_org"] = args.control_scm_org or cfg.get("platform_scm_org") or args.platform_scm_org
    cfg["control_repo"] = args.control_repo or "casc-platform-control"
    cfg["control_branch"] = args.control_branch or cfg.get("control_branch") or "main"
    cfg["platform_scm_org"] = args.platform_scm_org or cfg.get("platform_scm_org")
    cfg["platform_repo"] = platform_repo
    for legacy_key in (
        "platform_home_repo",
        "platform_repo_pattern",
        "platform_repo_names",
        "platform_repos",
        "repo_mode",
    ):
        cfg.pop(legacy_key, None)
    cfg.setdefault("create_missing_env_branches", True)
    cfg.setdefault("bootstrap_dispatch_fanout", True)
    cfg.setdefault("dispatcher_concurrency", "serialized")
    cfg.setdefault(
        "job_templates",
        {
            "genesis": "jt-platform-genesis",
            "bootstrap": "jt-platform-bootstrap_tenant",
            "dispatcher": "jt-platform-casc_dispatcher",
            "drift_detection": "jt-platform-drift_detection",
        },
    )
    cfg.pop("naming_rules_file", None)

    transformed_tenants = transform_tenants_doc(tenants_doc)
    validate_migrated_control(cfg, transformed_tenants)

    dump_yaml(control_out / "config.yml", cfg)
    dump_yaml(control_out / "tenants.yml", transformed_tenants)
    naming_src = source / "naming-rules.yml"
    if naming_src.exists():
        shutil.copy2(naming_src, control_out / "naming-rules.yml")

    # Copy desired-state content into platform workspace; never delete source.
    for item in source.iterdir():
        if item.name in {".git", ".github", ".gitlab", "config.yml", "tenants.yml", "naming-rules.yml"}:
            continue
        target = platform_out / item.name
        if item.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)

    report = {
        "migration": "legacy_colocated_to_control_plane",
        "source": str(source),
        "control_repo_workspace": str(control_out),
        "platform_desired_state_workspace": str(platform_out),
        "platform_repo": platform_repo,
        "had_desired_state_content": bool(has_desired or desired_state_dirs),
        "removed_runtime_keys": [
            "platform_home_repo",
            "platform_repo_pattern",
            "platform_repo_names",
            "platform_repos",
            "repo_mode",
            "tenant.repo_pattern",
            "tenant.repo_names",
            "tenant.repositories",
            "tenant.repo_by_folder",
            "tenant.resource_type",
            "tenant.repository (promoted to repo_name when unambiguous)",
        ],
        "next_steps": [
            "Create/verify remote casc-platform-control and push control workspace.",
            "Push platform desired-state workspace to the platform repo on every managed branch.",
            "Regenerate callers with CONTROL_REPO_TOKEN and token-only secrets.",
            "Update AAP JT extra vars to control_scm_org/control_repo/control_branch.",
            "Validate feature/PR paths, then one serialized platform and tenant dispatch.",
            "Do not keep colocated control files as a runtime fallback.",
        ],
        "rollback_boundary": (
            "If cutover fails before remote deletion/retirement, keep pipelines paused and "
            "retain the untouched legacy source repository."
        ),
    }
    (out / "migration-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    if not args.apply:
        print("Plan/workspace written. Re-run with --apply only after operator review for remote mutation gateways.")
    return 0


def plan_tenant_identity_migration(args: argparse.Namespace) -> int:
    tenants_path = Path(args.tenants_file)
    doc = load_yaml(tenants_path)
    tenants = doc.get("tenants") or []
    if not isinstance(tenants, list):
        raise SystemExit("tenants.yml tenants must be a list")
    current = next((t for t in tenants if t.get("tenant_id") == args.from_tenant_id), None)
    if current is None:
        raise SystemExit(f"Tenant {args.from_tenant_id} not found")
    # Fail closed before rewriting: do not drop multi-repo maps silently.
    require_combined_only_source({}, [current])
    current = transform_tenant_record(current)

    immutable = {
        "tenant_id": current.get("tenant_id"),
        "aap_organization": current.get("aap_organization"),
        "tenant_scm_org": current.get("tenant_scm_org"),
        "repo_name": current.get("repo_name"),
        "onboarding_mode": current.get("onboarding_mode", "greenfield"),
    }
    digest = hashlib.sha256(json.dumps(immutable, sort_keys=True).encode()).hexdigest()[:16]

    proposed = dict(current)
    if args.to_tenant_id:
        proposed["tenant_id"] = args.to_tenant_id
    if args.to_scm_org:
        proposed["tenant_scm_org"] = args.to_scm_org
    if args.to_repo_name:
        proposed["repo_name"] = args.to_repo_name
    for legacy_key in LEGACY_MULTI_REPO_TENANT_KEYS:
        proposed.pop(legacy_key, None)
    proposed.pop("repository", None)
    if args.to_aap_organization:
        proposed["aap_organization"] = args.to_aap_organization

    # Validate the proposed tenant record against the current runtime contract.
    validate_migrated_control(
        {
            "control_scm_org": "control-org",
            "control_repo": "casc-platform-control",
            "control_branch": "main",
            "platform_scm_org": "platform-org",
            "platform_repo": "casc-platform-global",
            "env_branch_map": {"dev": "develop"},
        },
        {"tenants": [proposed]},
    )

    plan = {
        "migration": "tenant_identity_repository",
        "from": immutable,
        "to": {
            "tenant_id": proposed.get("tenant_id"),
            "aap_organization": proposed.get("aap_organization"),
            "tenant_scm_org": proposed.get("tenant_scm_org"),
            "repo_name": proposed.get("repo_name"),
            "onboarding_mode": proposed.get("onboarding_mode", "greenfield"),
        },
        "immutable_hash": digest,
        "required_steps": [
            "Pause the affected tenant pipeline (dispatch_enabled=false).",
            "Scaffold/verify destination repos with repo_mode=existing without overwriting content.",
            "Copy desired-state YAML and caller files through reviewed commits.",
            "Update tenants.yml atomically to the new mapping.",
            "Validate destination mapping with feature/PR and one mapped dispatch.",
            "Retain old repositories until retention policy permits retirement.",
        ],
        "forbidden": [
            "In-place edit of immutable fields through normal Bootstrap.",
            "Automatic rename/move interpretation by tenants.yml Bootstrap.",
            "Deleting the old repository during failed cutover.",
        ],
    }
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"tenant-migration-{args.from_tenant_id}.json").write_text(
        json.dumps(plan, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(plan, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="CasC control-plane migration helpers")
    sub = parser.add_subparsers(dest="command", required=True)

    legacy = sub.add_parser("legacy-split", help="Plan/workspace split for colocated platform-home")
    legacy.add_argument("--source-repo", required=True)
    legacy.add_argument("--output-dir", required=True)
    legacy.add_argument("--control-scm-org", default="")
    legacy.add_argument("--platform-scm-org", default="")
    legacy.add_argument("--control-repo", default="casc-platform-control")
    legacy.add_argument("--control-branch", default="main")
    legacy.add_argument(
        "--platform-repo-name",
        default="",
        help="Optional override. When empty, preserve source platform_repo / unambiguous platform_repos name.",
    )
    legacy.add_argument("--apply", action="store_true", help="Reserved for operator-gated remote apply")
    legacy.set_defaults(func=plan_legacy_split)

    tenant = sub.add_parser("tenant-identity", help="Generate tenant identity/repository migration plan")
    tenant.add_argument("--tenants-file", required=True)
    tenant.add_argument("--from-tenant-id", required=True)
    tenant.add_argument("--to-tenant-id", default="")
    tenant.add_argument("--to-scm-org", default="")
    tenant.add_argument("--to-repo-name", default="")
    tenant.add_argument("--to-aap-organization", default="")
    tenant.add_argument("--output-dir", required=True)
    tenant.set_defaults(func=plan_tenant_identity_migration)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
