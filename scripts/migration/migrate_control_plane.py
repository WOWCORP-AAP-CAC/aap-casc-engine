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
from pathlib import Path
from typing import Any

import yaml


CONTROL_FILES = ("config.yml", "tenants.yml", "naming-rules.yml")


def load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    return data


def dump_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


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

    out = Path(args.output_dir).resolve()
    control_out = out / "casc-platform-control"
    platform_out = out / (args.platform_repo_name or "casc-platform-global")
    control_out.mkdir(parents=True, exist_ok=True)
    platform_out.mkdir(parents=True, exist_ok=True)

    cfg = load_yaml(source / "config.yml")
    cfg["control_scm_org"] = args.control_scm_org or cfg.get("platform_scm_org") or args.platform_scm_org
    cfg["control_repo"] = args.control_repo or "casc-platform-control"
    cfg["control_branch"] = args.control_branch or cfg.get("control_branch") or "main"
    cfg["platform_scm_org"] = args.platform_scm_org or cfg.get("platform_scm_org")
    cfg["platform_repo_pattern"] = cfg.get("platform_repo_pattern") or "combined"
    cfg["platform_repo"] = args.platform_repo_name or cfg.get("platform_repo") or "casc-platform-global"
    cfg.pop("platform_home_repo", None)
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
    cfg.setdefault("naming_rules_file", "naming-rules.yml")

    dump_yaml(control_out / "config.yml", cfg)
    shutil.copy2(source / "tenants.yml", control_out / "tenants.yml")
    naming_src = source / "naming-rules.yml"
    if naming_src.exists():
        shutil.copy2(naming_src, control_out / "naming-rules.yml")
    else:
        engine_rules = Path(__file__).resolve().parents[2] / "schemas" / "naming-rules.yml"
        if engine_rules.exists():
            shutil.copy2(engine_rules, control_out / "naming-rules.yml")

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
        "had_desired_state_content": bool(has_desired or desired_state_dirs),
        "removed_runtime_keys": ["platform_home_repo"],
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
    current = next((t for t in tenants if t.get("org_id") == args.from_org_id), None)
    if current is None:
        raise SystemExit(f"Tenant {args.from_org_id} not found")

    immutable = {
        "org_id": current.get("org_id"),
        "aap_organization": current.get("aap_organization"),
        "tenant_scm_org": current.get("tenant_scm_org"),
        "repo_pattern": current.get("repo_pattern"),
        "repo_name": current.get("repo_name"),
        "repo_names": current.get("repo_names"),
        "onboarding_mode": current.get("onboarding_mode", "greenfield"),
    }
    digest = hashlib.sha256(json.dumps(immutable, sort_keys=True).encode()).hexdigest()[:16]

    proposed = dict(current)
    if args.to_org_id:
        proposed["org_id"] = args.to_org_id
    if args.to_scm_org:
        proposed["tenant_scm_org"] = args.to_scm_org
    if args.to_repo_name:
        proposed["repo_name"] = args.to_repo_name
        proposed["repo_pattern"] = proposed.get("repo_pattern") or "combined"
    if args.to_aap_organization:
        proposed["aap_organization"] = args.to_aap_organization

    plan = {
        "migration": "tenant_identity_repository",
        "from": immutable,
        "to": {
            "org_id": proposed.get("org_id"),
            "aap_organization": proposed.get("aap_organization"),
            "tenant_scm_org": proposed.get("tenant_scm_org"),
            "repo_pattern": proposed.get("repo_pattern"),
            "repo_name": proposed.get("repo_name"),
            "repo_names": proposed.get("repo_names"),
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
    (out / f"tenant-migration-{args.from_org_id}.json").write_text(
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
    legacy.add_argument("--platform-repo-name", default="casc-platform-global")
    legacy.add_argument("--apply", action="store_true", help="Reserved for operator-gated remote apply")
    legacy.set_defaults(func=plan_legacy_split)

    tenant = sub.add_parser("tenant-identity", help="Generate tenant identity/repository migration plan")
    tenant.add_argument("--tenants-file", required=True)
    tenant.add_argument("--from-org-id", required=True)
    tenant.add_argument("--to-org-id", default="")
    tenant.add_argument("--to-scm-org", default="")
    tenant.add_argument("--to-repo-name", default="")
    tenant.add_argument("--to-aap-organization", default="")
    tenant.add_argument("--output-dir", required=True)
    tenant.set_defaults(func=plan_tenant_identity_migration)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
