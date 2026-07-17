"""Static contract checks for the control-plane topology and Phase 1-3 runtime.

Run with: python3 -m unittest tests/test_topology_contract.py
"""

from pathlib import Path
import tempfile
import unittest
import yaml

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "pipeline"))
sys.path.insert(0, str(ROOT / "scripts" / "migration"))

import casc_runtime  # noqa: E402
import migrate_control_plane  # noqa: E402
import repo_name_overrides  # noqa: E402


PIPELINES = (
    ROOT / ".github/workflows/casc-validate-and-trigger.yml",
    ROOT / "pipeline-templates/github/casc-validate-and-trigger.yml",
    ROOT / "pipeline-templates/gitlab/.gitlab-ci-template.yml",
)


class TopologyContractTests(unittest.TestCase):
    def test_seed_config_has_dedicated_control_fields(self):
        template = (ROOT / "templates/seed-config.yml.j2").read_text()
        for key in ("control_scm_org:", "control_repo:", "control_branch:"):
            self.assertIn(key, template)
        self.assertIn("dispatcher_concurrency: serialized", template)

    def test_resource_types_default_to_non_destructive_adoption(self):
        schema = yaml.safe_load((ROOT / "schemas/resource-types.yml").read_text())
        self.assertFalse(schema["defaults"]["deletion_supported"])
        for key in (
            "aap_organizations",
            "controller_projects",
            "controller_templates",
            "controller_credentials",
        ):
            self.assertFalse(schema["exceptions"][key]["deletion_supported"], key)

    def test_generated_caller_uses_control_token(self):
        caller = (ROOT / "templates/github-workflow-caller.yml.j2").read_text()
        self.assertIn("CONTROL_REPO_TOKEN", caller)
        self.assertNotIn("PLATFORM_REPO_TOKEN", caller)
        self.assertIn("permissions:", caller)
        self.assertIn("onboarding_dispatch", caller)

    def test_control_and_desired_state_are_distinct(self):
        genesis = (ROOT / "genesis.yml").read_text()
        self.assertIn("control_repo not in", genesis)
        self.assertIn("genesis_repos", genesis)

    def test_dispatcher_accepts_explicit_tenant_scope_identity(self):
        dispatcher = (ROOT / "site.yml").read_text()
        self.assertIn("tenant_org_id", dispatcher)
        self.assertIn("TENANT_ORG_ID", dispatcher)
        self.assertIn("control_revision", dispatcher)
        self.assertIn("selectattr('aap_scope', 'equalto', tenant_org_id)", dispatcher)
        self.assertIn("subelements('tenant_repos')", dispatcher)
        self.assertIn("naming_rules_file", dispatcher)

    def test_bootstrap_and_drift_share_control_revision_contract(self):
        bootstrap = (ROOT / "bootstrap.yml").read_text()
        drift = (ROOT / "drift-detect.yml").read_text()
        for content, label in ((bootstrap, "bootstrap"), (drift, "drift")):
            self.assertIn("CONTROL_REVISION", content, label)
            self.assertIn("effective_control_revision", content, label)
            self.assertIn("naming-rules.yml", content, label)
            self.assertIn("Authoritative control revision is unavailable or mismatched", content, label)

    def test_pipelines_use_token_only_dispatch_credentials(self):
        legacy_markers = (
            "AAP_FANOUT_ALLOW_BASIC",
            "AAP_ENGINE_USERNAME",
            "AAP_ENGINE_PASSWORD",
            "AAP_DEV_HOST",
            "AAP_HOST",
            "AAP_USERNAME",
            "AAP_PASSWORD",
        )
        for pipeline in PIPELINES:
            content = pipeline.read_text()
            self.assertIn("AAP_ENV_TARGETS_JSON", content, pipeline)
            for marker in legacy_markers:
                self.assertNotIn(marker, content, f"{marker} remains in {pipeline}")

    def test_onboarding_fanout_is_bounded_and_never_full(self):
        for pipeline in PIPELINES:
            content = pipeline.read_text()
            self.assertIn("bootstrap-onboarding", content, pipeline)
            self.assertIn("wait_for_terminal", content, pipeline)
            # Must not launch full during onboarding path.
            self.assertNotIn("'dispatch_scope': 'full'", content, pipeline)
            self.assertTrue(
                "onboarding_dispatch" in content or "CASC_OPERATION" in content,
                pipeline,
            )

    def test_trigger_timeout_fails_closed(self):
        for pipeline in PIPELINES:
            content = pipeline.read_text()
            self.assertNotIn(
                "::warning::Job",
                content,
                f"soft timeout warning remains in {pipeline}",
            )
            # GitHub paths use explicit ERROR + exit 1; GitLab equivalent must fail.
            self.assertTrue(
                "still running after" in content or "did not complete within" in content,
                pipeline,
            )
            self.assertNotIn("FIRE-AND-FORGET", content, pipeline)
            self.assertNotIn("wait_for_completion", content, pipeline)
            self.assertNotIn("WAIT_FOR_COMPLETION", content, pipeline)

    def test_gitlab_declares_onboarding_stage(self):
        gitlab = (ROOT / "pipeline-templates/gitlab/.gitlab-ci-template.yml").read_text()
        stages_block = gitlab.split("stages:", 1)[1].split("variables:", 1)[0]
        self.assertIn("- onboarding", stages_block)
        self.assertLess(stages_block.index("- fanout"), stages_block.index("- onboarding"))
        self.assertLess(stages_block.index("- onboarding"), stages_block.index("- trigger"))
        self.assertIn("stage: onboarding", gitlab)

    def test_tenant_seed_tasks_use_folder_repo_map(self):
        for relative in (
            "tasks/bootstrap_scm_github.yml",
            "tasks/bootstrap_scm_gitlab.yml",
        ):
            content = (ROOT / relative).read_text()
            self.assertIn("_tenant_repo_by_folder", content, relative)
            self.assertNotIn(
                "item.basename + '-' + org_id",
                content,
                relative,
            )
            self.assertNotIn(
                "item.0.basename + '-' + org_id",
                content,
                relative,
            )
            self.assertIn("_tenant_highest_env_branch", content, relative)
            self.assertIn("_tenant_lower_env_branches", content, relative)
            self.assertIn("seeded high branch", content, relative)

    def test_bootstrap_defines_folder_to_repo_map(self):
        bootstrap = (ROOT / "bootstrap.yml").read_text()
        self.assertIn("_tenant_repo_by_folder", bootstrap)
        self.assertIn("repo_name_overrides.py", bootstrap)
        self.assertIn("validate-tenant", bootstrap)

    def test_genesis_supports_platform_repo_name_overrides(self):
        genesis = (ROOT / "genesis.yml").read_text()
        self.assertIn("platform_repo_names", genesis)
        self.assertIn("PLATFORM_REPO_NAMES_JSON", genesis)
        self.assertIn("repo_name_overrides.py", genesis)
        self.assertIn("validate-platform", genesis)
        self.assertIn("_default_per_type_platform_repos", genesis)
        self.assertIn("control_namespace_id", genesis)
        self.assertIn("CONTROL_NAMESPACE_ID", genesis)

    def test_platform_repo_name_overrides_accept_valid_map_and_list(self):
        folders = ["organizations", "teams", "users"]
        defaults = [
            {"folder": "organizations", "name": "aap-organizations-global"},
            {"folder": "teams", "name": "aap-teams-global"},
            {"folder": "users", "name": "aap-users-global"},
        ]
        mapped = repo_name_overrides.normalize_platform_repo_names(
            folders, {"organizations": "custom-orgs", "teams": "custom-teams"}
        )
        applied = repo_name_overrides.apply_platform_repo_names(defaults, mapped)
        self.assertEqual(applied[0]["name"], "custom-orgs")
        self.assertEqual(applied[1]["name"], "custom-teams")
        self.assertEqual(applied[2]["name"], "aap-users-global")

        listed = repo_name_overrides.normalize_platform_repo_names(
            folders, ["orgs-a", "teams-a", "users-a"]
        )
        applied_list = repo_name_overrides.apply_platform_repo_names(defaults, listed)
        self.assertEqual([r["name"] for r in applied_list], ["orgs-a", "teams-a", "users-a"])

    def test_platform_repo_name_overrides_reject_invalid_inputs(self):
        folders = ["organizations", "teams", "users"]
        defaults = [
            {"folder": "organizations", "name": "aap-organizations-global"},
            {"folder": "teams", "name": "aap-teams-global"},
            {"folder": "users", "name": "aap-users-global"},
        ]
        with self.assertRaisesRegex(ValueError, "unknown folder keys"):
            repo_name_overrides.normalize_platform_repo_names(
                folders, {"not_a_folder": "x"}
            )
        with self.assertRaisesRegex(ValueError, "non-empty string"):
            repo_name_overrides.normalize_platform_repo_names(
                folders, {"organizations": "  "}
            )
        with self.assertRaisesRegex(ValueError, "exactly 3"):
            repo_name_overrides.normalize_platform_repo_names(folders, ["only-one"])
        with self.assertRaisesRegex(ValueError, "duplicate platform repo name"):
            repo_name_overrides.apply_platform_repo_names(
                defaults,
                {"organizations": "aap-teams-global"},
            )

    def test_tenant_repo_name_overrides_accept_and_reject(self):
        custom = [
            "t-projects",
            "t-credentials",
            "t-inventories",
            "t-templates",
            "t-workflows",
            "t-schedules",
            "t-notifications",
        ]
        self.assertEqual(
            repo_name_overrides.resolve_tenant_repos(
                repo_pattern="per-resource-type",
                org_id="demo_alpha",
                repo_names=custom,
            ),
            custom,
        )
        self.assertEqual(
            repo_name_overrides.resolve_tenant_repos(
                repo_pattern="combined",
                org_id="demo_alpha",
                repo_name="tenant-alpha-casc",
            ),
            ["tenant-alpha-casc"],
        )
        self.assertEqual(
            repo_name_overrides.resolve_tenant_repos(
                repo_pattern="combined",
                org_id="demo_alpha",
            ),
            ["casc-tenant-demo_alpha"],
        )
        with self.assertRaisesRegex(ValueError, "not a mapping"):
            repo_name_overrides.validate_tenant_repo_names(
                repo_pattern="per-resource-type",
                repo_names={"projects": "x"},
            )
        with self.assertRaisesRegex(ValueError, "unique"):
            repo_name_overrides.validate_tenant_repo_names(
                repo_pattern="per-resource-type",
                repo_names=["a", "b", "c", "d", "e", "f", "a"],
            )
        with self.assertRaisesRegex(ValueError, "non-empty string"):
            repo_name_overrides.validate_tenant_repo_names(
                repo_pattern="combined",
                repo_names=["  "],
            )

    def test_gitlab_genesis_uses_control_namespace_id(self):
        gitlab = (ROOT / "tasks/genesis_scm_gitlab.yml").read_text()
        github = (ROOT / "tasks/genesis_scm_github.yml").read_text()
        self.assertIn("control_namespace_id if item.repository_class == 'control'", gitlab)
        self.assertIn("control repo", gitlab.lower())
        self.assertNotIn("home repo", gitlab.lower())
        self.assertNotIn("home repo", github.lower())

    def test_foundation_targets_cover_per_resource_layout(self):
        combined = casc_runtime.iter_foundation_targets(
            {"platform_repo_pattern": "combined", "platform_repo": "casc-platform-global"},
            "demo_alpha",
        )
        self.assertEqual(len(combined), 5)
        self.assertIn(("casc-platform-global", "base/organizations/org-demo_alpha.yml"), combined)
        self.assertIn(
            ("casc-platform-global", "base/rbac_team_assignments/rbac-team-demo_alpha.yml"),
            combined,
        )

        per_type = casc_runtime.iter_foundation_targets(
            {
                "platform_repo_pattern": "per-resource-type",
                "platform_repos": [
                    {"resource_type": "organizations", "name": "aap-organizations-global"},
                    {"resource_type": "teams", "name": "aap-teams-global"},
                    {"resource_type": "users", "name": "aap-users-global"},
                    {
                        "resource_type": "rbac_user_assignments",
                        "name": "gateway-role_user_assignments-global",
                    },
                    {
                        "resource_type": "rbac_team_assignments",
                        "name": "gateway-role_team_assignments-global",
                    },
                ],
            },
            "demo_alpha",
        )
        self.assertEqual(len(per_type), 5)
        self.assertIn(("aap-organizations-global", "base/org-demo_alpha.yml"), per_type)
        self.assertIn(("aap-teams-global", "base/team-demo_alpha.yml"), per_type)
        self.assertIn(("aap-users-global", "base/user-demo_alpha.yml"), per_type)
        self.assertIn(
            ("gateway-role_user_assignments-global", "base/rbac-user-demo_alpha.yml"),
            per_type,
        )
        self.assertIn(
            ("gateway-role_team_assignments-global", "base/rbac-team-demo_alpha.yml"),
            per_type,
        )

    def test_security_hardening_markers(self):
        gh_pipelines = PIPELINES[:2]
        for pipeline in gh_pipelines:
            content = pipeline.read_text()
            self.assertIn("permissions:", content, pipeline)
            self.assertIn("contents: read", content, pipeline)
            self.assertIn("persist-credentials: false", content, pipeline)
            self.assertIn("::add-mask::", content, pipeline)
            self.assertIn("pull_request", content, pipeline)

    def test_control_metadata_fetch_fails_closed(self):
        for pipeline in PIPELINES:
            content = pipeline.read_text()
            self.assertIn("ensure-control", content, pipeline)
            self.assertIn("naming-rules.yml missing from control revision", content, pipeline)
            self.assertNotIn(
                "falling back to engine-seeded naming rules",
                content,
                pipeline,
            )

    def test_bootstrap_launch_passes_control_revision(self):
        for pipeline in PIPELINES:
            content = pipeline.read_text()
            self.assertIn("'control_revision'", content, pipeline)
            self.assertIn("job_templates", content, pipeline)

    def test_disabled_fanout_does_not_silently_trigger(self):
        for pipeline in PIPELINES:
            content = pipeline.read_text()
            self.assertTrue(
                "onboarding_dispatch_pending" in content
                or "remains pending" in content
                or "Onboarding dispatch remains pending" in content,
                pipeline,
            )

    def test_casc_runtime_rejects_password_entries(self):
        with self.assertRaises(ValueError):
            casc_runtime.resolve_env_creds(
                "dev",
                '{"dev":{"host":"https://example.invalid","token":"t","username":"u"}}',
            )

    def test_casc_runtime_resolves_jt_names(self):
        cfg = {
            "job_templates": {
                "dispatcher": "jt-custom-dispatcher",
                "bootstrap": "jt-custom-bootstrap",
            }
        }
        names = casc_runtime.resolve_jt_names(cfg)
        self.assertEqual(names["dispatcher"], "jt-custom-dispatcher")
        self.assertEqual(names["bootstrap"], "jt-custom-bootstrap")
        self.assertEqual(names["genesis"], "jt-platform-genesis")

    def test_migration_legacy_split_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "legacy-home"
            source.mkdir()
            (source / "config.yml").write_text(
                "platform_scm_org: example\nplatform_repo_pattern: combined\n"
                "env_branch_map:\n  poc: main\n",
                encoding="utf-8",
            )
            (source / "tenants.yml").write_text("tenants: []\n", encoding="utf-8")
            (source / "base").mkdir()
            (source / "base" / "organizations").mkdir()
            (source / "base" / "organizations" / "org-demo.yml").write_text(
                "aap_organizations: []\n", encoding="utf-8"
            )
            out = Path(tmp) / "out"
            rc = migrate_control_plane.plan_legacy_split(
                argparse_namespace(
                    source_repo=str(source),
                    output_dir=str(out),
                    control_scm_org="example",
                    platform_scm_org="example",
                    control_repo="casc-platform-control",
                    control_branch="main",
                    platform_repo_name="casc-platform-global",
                    apply=False,
                )
            )
            self.assertEqual(rc, 0)
            self.assertTrue((out / "casc-platform-control" / "config.yml").exists())
            self.assertTrue((out / "casc-platform-global" / "base" / "organizations").exists())
            cfg = yaml.safe_load((out / "casc-platform-control" / "config.yml").read_text())
            self.assertNotIn("platform_home_repo", cfg)
            self.assertEqual(cfg["control_repo"], "casc-platform-control")

    def test_documentation_mentions_control_topology_and_excludes_oidc(self):
        guide = (ROOT / "docs/ENGINE_SETUP_AND_OPERATIONS_GUIDE.md").read_text()
        nonprod = (ROOT / "docs/NONPRODUCTION_VALIDATION.md").read_text()
        self.assertIn("casc-platform-control", guide)
        self.assertIn("Part A", guide)
        self.assertIn("Part D", guide)
        self.assertIn("onboarding_dispatch", guide)
        self.assertIn("Out of scope", guide)
        self.assertIn("OIDC federation", guide)
        self.assertIn("external secret managers", guide)
        self.assertNotIn("configure OIDC", guide.lower())
        self.assertNotIn("hashicorp vault", guide.lower())
        self.assertIn("serialized", guide.lower())
        self.assertIn("onboarding_dispatch", nonprod)
        self.assertIn("Do **not** use production", nonprod)


def argparse_namespace(**kwargs):
    class NS:
        pass

    ns = NS()
    for key, value in kwargs.items():
        setattr(ns, key, value)
    return ns


if __name__ == "__main__":
    unittest.main()
