"""Behavioral and static contracts for the naming-policy-neutral engine baseline.

Run with: python3 -m unittest tests/test_topology_contract.py
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error

import yaml
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "pipeline"))
sys.path.insert(0, str(ROOT / "scripts" / "migration"))
sys.path.insert(0, str(ROOT / "schemas"))

import casc_runtime  # noqa: E402
import migrate_control_plane  # noqa: E402
import repo_name_overrides  # noqa: E402
import validate_naming  # noqa: E402


PIPELINES = (
    ROOT / ".github/workflows/casc-validate-and-trigger.yml",
    ROOT / "pipeline-templates/github/casc-validate-and-trigger.yml",
    ROOT / "pipeline-templates/gitlab/.gitlab-ci-template.yml",
)
PROVIDER_TASKS = (
    ROOT / "tasks/bootstrap_scm_github.yml",
    ROOT / "tasks/bootstrap_scm_gitlab.yml",
)


def base_config(**overrides):
    cfg = {
        "control_scm_org": "ww-platform",
        "control_repo": "casc-platform-control",
        "control_branch": "main",
        "platform_scm_org": "ww-platform",
        "platform_repo_pattern": "combined",
        "platform_repo": "casc-platform-global",
        "env_branch_map": {"dev": "develop", "prd": "main"},
    }
    cfg.update(overrides)
    return cfg


def greenfield(tenant_id="stores", **overrides):
    record = {
        "tenant_id": tenant_id,
        "team_name": "Stores Automation",
        "tenant_scm_org": "ww-tenants",
        "repo_pattern": "combined",
        "onboarding_mode": "greenfield",
    }
    record.update(overrides)
    return record


def brownfield(tenant_id="legacy", **overrides):
    record = {
        "tenant_id": tenant_id,
        "aap_organization": "Legacy LDAP Organization",
        "tenant_scm_org": "ww-tenants",
        "repo_pattern": "combined",
        "onboarding_mode": "brownfield",
    }
    record.update(overrides)
    return record


def argparse_namespace(**kwargs):
    class NS:
        pass

    result = NS()
    for key, value in kwargs.items():
        setattr(result, key, value)
    return result


class TenantIdentityTests(unittest.TestCase):
    def test_greenfield_defaults_aap_organization_to_tenant_id(self):
        runtime = casc_runtime.public_tenant_runtime(greenfield())
        self.assertEqual(runtime["tenant_id"], "stores")
        self.assertEqual(runtime["aap_organization"], "stores")
        self.assertEqual(runtime["repositories"], ["casc-tenant-stores"])
        self.assertNotIn("org-stores", json.dumps(runtime))

    def test_greenfield_accepts_exact_customer_identities(self):
        runtime = casc_runtime.public_tenant_runtime(
            greenfield(
                aap_organization='WW Stores: Automation #1 "Primary"',
                team_name="Storekeepers' Automation",
            )
        )
        self.assertEqual(runtime["aap_organization"], 'WW Stores: Automation #1 "Primary"')
        self.assertEqual(runtime["team_name"], "Storekeepers' Automation")

    def test_brownfield_contract_is_scm_only_identity(self):
        runtime = casc_runtime.public_tenant_runtime(brownfield())
        self.assertEqual(runtime["aap_organization"], "Legacy LDAP Organization")
        self.assertNotIn("team_name", runtime)
        with self.assertRaisesRegex(ValueError, "requires explicit aap_organization"):
            casc_runtime.normalize_tenant_record(
                brownfield(aap_organization=None)
            )
        with self.assertRaisesRegex(ValueError, "does not accept team_name"):
            casc_runtime.normalize_tenant_record(brownfield(team_name="Legacy Team"))

    def test_tenant_id_safe_key_limits(self):
        for valid in ("a", "stores", "tenant_01", "a" * 64):
            self.assertEqual(repo_name_overrides.validate_tenant_id(valid), valid)
        for invalid in (
            "",
            "A",
            "Stores",
            "1stores",
            "stores-team",
            "stores.team",
            "stores/team",
            "../stores",
            "stores team",
            " stores",
            "stores ",
            "\tstores",
            "a" * 65,
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                repo_name_overrides.validate_tenant_id(invalid)

    def test_derived_runtime_fields_are_not_accepted_as_registry_inputs(self):
        for field in ("derived_repositories", "repository_cache", "access_principal"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "Unsupported"):
                casc_runtime.normalize_tenant_record(greenfield(**{field: "x"}))

    def test_registry_rejects_duplicate_identity_and_repo_ownership(self):
        cfg = base_config()
        with self.assertRaisesRegex(ValueError, "Duplicate tenant_id"):
            casc_runtime.validate_tenant_registry(
                {"tenants": [greenfield(), greenfield()]}, cfg
            )
        with self.assertRaisesRegex(ValueError, "AAP Organization"):
            casc_runtime.validate_tenant_registry(
                {
                    "tenants": [
                        greenfield("stores", aap_organization="Shared Org"),
                        greenfield("network", aap_organization="Shared Org"),
                    ]
                },
                cfg,
            )
        with self.assertRaisesRegex(ValueError, "owned by both"):
            casc_runtime.validate_tenant_registry(
                {
                    "tenants": [
                        greenfield("stores", repo_name="shared-casc"),
                        greenfield("network", repo_name="shared-casc"),
                    ]
                },
                cfg,
            )
        with self.assertRaisesRegex(ValueError, "owned by both"):
            casc_runtime.validate_tenant_registry(
                {
                    "tenants": [
                        greenfield("stores", tenant_scm_org="ww-platform", repo_name="casc-platform-global")
                    ]
                },
                cfg,
            )

    def test_inactive_records_still_reserve_identities(self):
        with self.assertRaisesRegex(ValueError, "AAP Organization"):
            casc_runtime.validate_tenant_registry(
                {
                    "tenants": [
                        greenfield("old", aap_organization="Reserved Org", status="inactive"),
                        greenfield("new", aap_organization="Reserved Org"),
                    ]
                },
                base_config(),
            )

    def test_custom_repo_mapping_is_deterministic(self):
        overrides = {"projects": "stores-projects", "inventories": "stores-inventory"}
        mapping = repo_name_overrides.resolve_tenant_repo_map(
            repo_pattern="per-resource-type",
            tenant_id="stores",
            repo_names=overrides,
        )
        self.assertEqual(mapping["projects"], "stores-projects")
        self.assertEqual(mapping["inventories"], "stores-inventory")
        self.assertEqual(mapping["templates"], "controller-templates-stores")
        with self.assertRaisesRegex(ValueError, "mapping"):
            repo_name_overrides.resolve_tenant_repo_map(
                repo_pattern="per-resource-type", tenant_id="stores", repo_names=["x"]
            )
        with self.assertRaisesRegex(ValueError, "unknown folder"):
            repo_name_overrides.resolve_tenant_repo_map(
                repo_pattern="per-resource-type",
                tenant_id="stores",
                repo_names={"unknown": "x"},
            )
        with self.assertRaisesRegex(ValueError, "duplicate tenant repo"):
            repo_name_overrides.resolve_tenant_repo_map(
                repo_pattern="per-resource-type",
                tenant_id="stores",
                repo_names={"projects": "same", "inventories": "same"},
            )

    def test_same_short_repo_name_is_safe_across_scm_namespaces(self):
        normalized = casc_runtime.validate_tenant_registry(
            {
                "tenants": [
                    greenfield("stores", tenant_scm_org="ww-stores", repo_name="aap-casc"),
                    greenfield("network", tenant_scm_org="ww-network", repo_name="aap-casc"),
                ]
            },
            base_config(),
        )
        self.assertEqual(len(normalized), 2)
        site = (ROOT / "site.yml").read_text()
        self.assertIn("'repo_path': item.0.tenant_scm_org + '/' + item.1", site)
        for pipeline in PIPELINES:
            content = pipeline.read_text()
            if "gitlab" in str(pipeline):
                self.assertIn("CI_PROJECT_PATH", content)
            else:
                self.assertIn("GITHUB_REPOSITORY", content)

    def test_platform_repo_overrides_are_mapping_only(self):
        folders = ["organizations", "teams"]
        defaults = [
            {"folder": "organizations", "name": "orgs"},
            {"folder": "teams", "name": "teams"},
        ]
        mapped = repo_name_overrides.normalize_platform_repo_names(
            folders, {"organizations": "customer-orgs"}
        )
        result = repo_name_overrides.apply_platform_repo_names(defaults, mapped)
        self.assertEqual([item["name"] for item in result], ["customer-orgs", "teams"])
        with self.assertRaisesRegex(ValueError, "mapping"):
            repo_name_overrides.normalize_platform_repo_names(folders, ["a", "b"])
        with self.assertRaisesRegex(ValueError, "unknown folder"):
            repo_name_overrides.normalize_platform_repo_names(folders, {"users": "x"})
        with self.assertRaisesRegex(ValueError, "duplicate platform repo"):
            repo_name_overrides.apply_platform_repo_names(
                defaults, {"organizations": "teams"}
            )


class LifecycleTests(unittest.TestCase):
    def test_added_active_tenant_is_actionable(self):
        actions = casc_runtime.diff_tenant_actions(
            {"tenants": []},
            {"tenants": [greenfield()]},
            base_config(),
            marker_exists=lambda _tenant: False,
        )
        self.assertEqual([item["action"] for item in actions], ["added"])

    def test_pre_scaffold_correction_and_removal_are_allowed(self):
        old = greenfield(aap_organization="Typo Org")
        new = greenfield(aap_organization="Correct Org")
        actions = casc_runtime.diff_tenant_actions(
            {"tenants": [old]},
            {"tenants": [new]},
            base_config(),
            marker_exists=lambda _tenant: False,
        )
        self.assertEqual(actions[0]["action"], "corrected")
        self.assertEqual(
            casc_runtime.diff_tenant_actions(
                {"tenants": [old]},
                {"tenants": []},
                base_config(),
                marker_exists=lambda _tenant: False,
            ),
            [],
        )

    def test_post_scaffold_identity_change_and_removal_fail(self):
        old = greenfield()
        with self.assertRaisesRegex(ValueError, "immutable"):
            casc_runtime.diff_tenant_actions(
                {"tenants": [old]},
                {"tenants": [greenfield(team_name="Renamed Team")]},
                base_config(),
                marker_exists=lambda _tenant: True,
            )
        with self.assertRaisesRegex(ValueError, "cannot be removed"):
            casc_runtime.diff_tenant_actions(
                {"tenants": [old]},
                {"tenants": []},
                base_config(),
                marker_exists=lambda _tenant: True,
            )

    def test_mutable_status_and_dispatch_do_not_bootstrap(self):
        old = greenfield(status="active", dispatch_enabled=True)
        new = greenfield(status="inactive", dispatch_enabled=False)
        actions = casc_runtime.diff_tenant_actions(
            {"tenants": [old]},
            {"tenants": [new]},
            base_config(),
            marker_exists=lambda _tenant: True,
        )
        self.assertEqual(actions, [])

    def test_marker_is_strict_and_mode_specific(self):
        tenant = greenfield()
        expected = casc_runtime.build_scaffold_marker(
            tenant, repository="casc-tenant-stores", resource_type="combined"
        )
        casc_runtime.validate_scaffold_marker(dict(expected), expected)
        changed = dict(expected, aap_organization="Other")
        with self.assertRaisesRegex(ValueError, "aap_organization"):
            casc_runtime.validate_scaffold_marker(changed, expected)
        extra = dict(expected, unexpected_identity="someone")
        with self.assertRaisesRegex(ValueError, "unexpected_identity"):
            casc_runtime.validate_scaffold_marker(extra, expected)

        brown = brownfield()
        marker = casc_runtime.build_scaffold_marker(
            brown, repository="casc-tenant-legacy", resource_type="combined"
        )
        self.assertNotIn("team_name", marker)

    def test_survey_resolution_uses_git_as_authority(self):
        doc = {"tenants": [greenfield(aap_organization="WW Stores")]}
        resolved, registered = casc_runtime.resolve_bootstrap_request(
            doc, base_config(), {"tenant_id": "stores"}
        )
        self.assertTrue(registered)
        self.assertEqual(resolved["aap_organization"], "WW Stores")
        with self.assertRaisesRegex(ValueError, "conflict"):
            casc_runtime.resolve_bootstrap_request(
                doc,
                base_config(),
                {"tenant_id": "stores", "aap_organization": "Other"},
            )

    def test_unregistered_survey_resolution_is_lean_and_validated(self):
        resolved, registered = casc_runtime.resolve_bootstrap_request(
            {"tenants": []}, base_config(), greenfield()
        )
        self.assertFalse(registered)
        self.assertEqual(resolved["aap_organization"], "stores")
        self.assertEqual(resolved["team_name"], "Stores Automation")


class FoundationAndTemplateTests(unittest.TestCase):
    def setUp(self):
        self.jinja = Environment(loader=FileSystemLoader(str(ROOT)))
        self.jinja.filters["to_json"] = json.dumps
        self.jinja.filters["to_nice_yaml"] = lambda value, indent=2: yaml.safe_dump(
            value, sort_keys=False, default_flow_style=False, indent=indent
        )

    def test_two_neutral_foundation_paths(self):
        combined = casc_runtime.iter_foundation_targets(base_config(), "stores")
        self.assertEqual(
            combined,
            [
                ("casc-platform-global", "base/organizations/stores.yml"),
                ("casc-platform-global", "base/teams/stores.yml"),
            ],
        )
        per_type = casc_runtime.iter_foundation_targets(
            base_config(
                platform_repo_pattern="per-resource-type",
                platform_repos=[
                    {"resource_type": "organizations", "name": "ww-org-config"},
                    {"resource_type": "teams", "name": "ww-team-config"},
                ],
            ),
            "stores",
        )
        self.assertEqual(
            per_type,
            [("ww-org-config", "base/stores.yml"), ("ww-team-config", "base/stores.yml")],
        )
        with self.assertRaisesRegex(ValueError, "missing a repository"):
            casc_runtime.iter_foundation_targets(
                base_config(platform_repo_pattern="per-resource-type", platform_repos=[]),
                "stores",
            )

    def test_free_form_foundation_values_round_trip_yaml(self):
        context = {
            "_effective_tenant_id": "stores",
            "_effective_aap_organization": 'WW Stores: Automation #1 "Primary"',
            "_effective_team_name": "Storekeepers' Automation",
        }
        org = yaml.safe_load(
            self.jinja.get_template("templates/org-template.yml.j2").render(**context)
        )
        team = yaml.safe_load(
            self.jinja.get_template("templates/team-template.yml.j2").render(**context)
        )
        self.assertEqual(
            org["aap_organizations"][0]["name"], context["_effective_aap_organization"]
        )
        self.assertEqual(team["aap_teams"][0]["name"], context["_effective_team_name"])
        self.assertEqual(
            team["aap_teams"][0]["organization"], context["_effective_aap_organization"]
        )

    def test_tenant_samples_use_the_exact_aap_organization(self):
        context = {
            "tenant_id": "stores",
            "_effective_aap_organization": 'WW Stores: Automation #1 "Primary"',
            "scm_base_url": "https://github.example/ww",
        }
        resource_keys = (
            ("templates/seed-controller-projects.yml.j2", "controller_projects"),
            ("templates/seed-controller-credentials.yml.j2", "controller_credentials"),
            ("templates/seed-controller-inventories.yml.j2", "controller_inventories"),
            ("templates/seed-controller-templates.yml.j2", "controller_templates"),
            ("templates/seed-controller-workflows.yml.j2", "controller_workflows"),
            ("templates/seed-controller-schedules.yml.j2", "controller_schedules"),
            ("templates/seed-controller-notifications.yml.j2", "controller_notifications"),
        )
        for template, resource_key in resource_keys:
            with self.subTest(template=template):
                rendered = self.jinja.get_template(template).render(**context)
                item = yaml.safe_load(rendered)[resource_key][0]
                self.assertEqual(item["organization"], context["_effective_aap_organization"])

    def test_bootstrap_foundation_is_org_and_team_only(self):
        bootstrap = (ROOT / "bootstrap.yml").read_text()
        for deleted in (
            "user-template.yml.j2",
            "rbac-user-template.yml.j2",
            "rbac-team-template.yml.j2",
            "default_ee",
            "Ansible Galaxy",
        ):
            self.assertNotIn(deleted, bootstrap)
        for task in PROVIDER_TASKS:
            content = task.read_text()
            self.assertIn("Build Greenfield foundation targets", content)
            self.assertNotIn("rbac-user", content)
            self.assertNotIn("rbac-team", content)
            self.assertIn("Verify final Greenfield foundation content", content)

    def test_user_sample_is_password_free_and_uses_organizations_list(self):
        sample = yaml.safe_load((ROOT / "templates/seed-aap-users.yml.j2").read_text())
        user = sample["aap_user_accounts"][0]
        self.assertNotIn("password", user)
        self.assertNotIn("change_me", json.dumps(user))
        self.assertIsInstance(user["organizations"], list)

    def test_no_generic_galaxy_or_default_ee_assumptions(self):
        checked = [
            ROOT / "bootstrap.yml",
            ROOT / "templates/org-template.yml.j2",
            ROOT / "templates/seed-aap-organizations.yml.j2",
        ] + list((ROOT / "examples/v2").rglob("*.yml"))
        text = "\n".join(path.read_text() for path in checked)
        for marker in (
            "Ansible Galaxy",
            "Default execution environment",
            "default_environment",
            "galaxy_credentials",
            "DEFAULT_EE",
            "default_ee",
        ):
            self.assertNotIn(marker, text)

    def test_password_default_is_disabled_at_apply_boundaries(self):
        self.assertIn('users_default_password: ""', (ROOT / "site.yml").read_text())
        self.assertIn('users_default_password: ""', (ROOT / "remediate.yml").read_text())


class NamingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.resource_types = str(ROOT / "schemas/resource-types.yml")
        self.allowed = str(ROOT / "roles/process_casc_config/defaults/main.yml")

    def load(self, rules_path):
        return validate_naming.load_policy(
            str(rules_path), self.resource_types, self.allowed
        )[0]

    def test_empty_policy_is_inactive(self):
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "naming-rules.yml"
            rules.write_text("---\n", encoding="utf-8")
            self.assertEqual(self.load(rules), {})

    def test_shipped_type_prefixed_policy_has_real_schema(self):
        rules = self.load(ROOT / "examples/naming-rules-type-prefixed.yml.sample")
        self.assertIn("aap_organizations", rules)
        self.assertIn("identity_field", rules["aap_organizations"])

    def test_customer_policy_uses_registered_identity_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rules_path = root / "naming-rules.yml"
            rules_path.write_text(
                "aap_user_accounts:\n  pattern: '^user_[a-z]+$'\n", encoding="utf-8"
            )
            rules = self.load(rules_path)
            good = root / "good.yml"
            good.write_text("aap_user_accounts:\n  - username: user_stores\n", encoding="utf-8")
            bad = root / "bad.yml"
            bad.write_text("aap_user_accounts:\n  - username: Stores User\n", encoding="utf-8")
            self.assertEqual(validate_naming.validate_file(str(good), rules), [])
            self.assertTrue(validate_naming.validate_file(str(bad), rules))

    def test_day_zero_policy_validates_rendered_foundation(self):
        jinja = Environment(loader=FileSystemLoader(str(ROOT)))
        jinja.filters["to_json"] = json.dumps
        context = {
            "_effective_tenant_id": "stores",
            "_effective_aap_organization": "WW Stores Automation",
            "_effective_team_name": "Stores Automation",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rules_path = root / "naming-rules.yml"
            rules_path.write_text(
                "aap_organizations:\n  pattern: '^WW .+ Automation$'\n"
                "aap_teams:\n  pattern: '^.+ Automation$'\n",
                encoding="utf-8",
            )
            desired = root / "desired"
            desired.mkdir()
            (desired / "organizations.yml").write_text(
                jinja.get_template("templates/org-template.yml.j2").render(**context),
                encoding="utf-8",
            )
            (desired / "teams.yml").write_text(
                jinja.get_template("templates/team-template.yml.j2").render(**context),
                encoding="utf-8",
            )
            rules = self.load(rules_path)
            self.assertEqual(validate_naming.validate_tree(str(desired), rules), [])

            context["_effective_team_name"] = "Stores"
            (desired / "teams.yml").write_text(
                jinja.get_template("templates/team-template.yml.j2").render(**context),
                encoding="utf-8",
            )
            self.assertTrue(validate_naming.validate_tree(str(desired), rules))

    def test_policy_schema_fails_closed(self):
        cases = {
            "list": "- aap_organizations\n",
            "unknown": "not_a_resource:\n  pattern: x\n",
            "bad-rule": "aap_organizations: x\n",
            "missing-pattern": "aap_organizations:\n  example: x\n",
            "bad-regex": "aap_organizations:\n  pattern: '[unterminated'\n",
            "raw": "controller_settings:\n  pattern: x\n",
            "non-scalar": "hub_group_roles:\n  pattern: x\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            for label, content in cases.items():
                with self.subTest(label=label):
                    path = Path(tmp) / f"{label}.yml"
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        self.load(path)

    def test_naming_rules_control_file_is_not_scanned_as_desired_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = Path(tmp) / "policy-source.txt"
            policy.write_text("aap_organizations:\n  pattern: '^WW .+$'\n", encoding="utf-8")
            (root / "naming-rules.yml").write_text(
                "aap_organizations:\n  pattern: invalid-as-resource-data\n", encoding="utf-8"
            )
            rules = self.load(policy)
            self.assertEqual(validate_naming.validate_tree(str(root), rules), [])

    def test_genesis_does_not_seed_active_policy(self):
        for task in (
            ROOT / "tasks/genesis_scm_github.yml",
            ROOT / "tasks/genesis_scm_gitlab.yml",
        ):
            self.assertNotIn("Render naming-rules", task.read_text())
        self.assertFalse((ROOT / "schemas/naming-rules.yml").exists())
        self.assertFalse((ROOT / "templates/naming-rules.yml.j2").exists())


class DeletionSafetyTests(unittest.TestCase):
    def test_unsupported_explicit_deletion_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "project.yml").write_text(
                "controller_projects:\n  - name: demo\n    state: absent\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "deletion is not audited"):
                casc_runtime.validate_explicit_deletions(
                    str(root), str(ROOT / "schemas/resource-types.yml")
                )

            (root / "project.yml").write_text(
                "controller_projects:\n  - name: demo\n    state: present\n",
                encoding="utf-8",
            )
            casc_runtime.validate_explicit_deletions(
                str(root), str(ROOT / "schemas/resource-types.yml")
            )

    def test_every_allowed_key_resolves_fail_closed_deletion_metadata(self):
        schema = yaml.safe_load((ROOT / "schemas/resource-types.yml").read_text())
        role_defaults = yaml.safe_load(
            (ROOT / "roles/process_casc_config/defaults/main.yml").read_text()
        )
        for key in role_defaults["casc_allowed_resource_keys"]:
            metadata = dict(schema["defaults"])
            metadata.update(schema.get("exceptions", {}).get(key, {}))
            self.assertIsInstance(metadata.get("deletion_supported"), bool, key)
            self.assertEqual(metadata.get("deletion_field"), "state", key)
            self.assertIn("absent", metadata.get("deletion_values", []), key)

    def test_ci_and_dispatcher_repeat_deletion_validation(self):
        for pipeline in PIPELINES:
            self.assertIn("validate-deletions", pipeline.read_text(), pipeline)
        process_role = (ROOT / "roles/process_casc_config/tasks/main.yml").read_text()
        self.assertIn("validate-deletions", process_role)


class ProviderAndPipelineParityTests(unittest.TestCase):
    def test_provider_tasks_use_two_file_all_branch_transaction(self):
        for task in PROVIDER_TASKS:
            content = task.read_text()
            self.assertIn("product(_mapped_branches)", content, task)
            self.assertIn("before content scaffolding", content, task)
            self.assertIn("Verify final scaffold marker", content, task)
            self.assertIn("Verify final thin caller", content, task)
            self.assertIn("Verify required scaffold files", content, task)
            self.assertIn("Verify final Greenfield foundation", content, task)
            self.assertIn("default-branch scaffold marker", content, task)
            self.assertIn("Validate latest survey registry candidate", content, task)
            self.assertNotIn("default('aap-organizations-global')", content, task)
            self.assertNotIn("default('aap-teams-global')", content, task)

    def test_default_branch_marker_validation_skips_missing_repos(self):
        for task in PROVIDER_TASKS:
            content = task.read_text()
            self.assertIn("selectattr('status', 'equalto', 200)", content, task)
            self.assertIn("default_branch | default('-')", content, task)

    def test_dispatcher_selected_repos_preserves_native_lists(self):
        content = (ROOT / "site.yml").read_text()
        self.assertNotIn("selected_repos: >-", content)
        self.assertIn('selected_repos: "{{ _platform_repos if dispatch_scope == \'platform\'', content)
        self.assertIn("Build platform repos list (combined)", content)

    def test_drift_platform_repos_preserves_native_lists(self):
        content = (ROOT / "drift-detect.yml").read_text()
        self.assertIn("Build platform repos list for drift check (combined)", content)
        self.assertIn("clone_name: \"platform__{{ _platform_repo }}\"", content)
        self.assertIn("clone_depth: 0", content)

    def test_brownfield_excluded_from_onboarding_fanout_outputs(self):
        workflows = (
            ROOT / ".github/workflows/casc-validate-and-trigger.yml",
            ROOT / "pipeline-templates/github/casc-validate-and-trigger.yml",
        )
        for workflow_path in workflows:
            workflow = workflow_path.read_text()
            self.assertIn('onboarding_mode", "greenfield") == "greenfield"', workflow, workflow_path)
            self.assertIn("fanout_tenant_ids", workflow, workflow_path)
            self.assertIn("fanout_tenant_ids != '[]'", workflow, workflow_path)

        gitlab = (ROOT / "pipeline-templates/gitlab/.gitlab-ci-template.yml").read_text()
        self.assertIn('onboarding_mode", "greenfield") == "greenfield"', gitlab)
        self.assertIn("BOOTSTRAP_FANOUT_TENANT_IDS", gitlab)
        self.assertIn("No greenfield onboarding tenants", gitlab)

    def test_pipelines_reject_username_password_creds_twice(self):
        """Token-only parity: each GitHub workflow and GitLab template reject basic auth twice."""
        rejection = "username/password credentials are rejected; bearer token only"
        for workflow_path in (
            ROOT / ".github/workflows/casc-validate-and-trigger.yml",
            ROOT / "pipeline-templates/github/casc-validate-and-trigger.yml",
        ):
            count = workflow_path.read_text().count(rejection)
            self.assertEqual(count, 2, f"{workflow_path} expected 2 rejection checks, found {count}")

        gitlab = ROOT / "pipeline-templates/gitlab/.gitlab-ci-template.yml"
        count = gitlab.read_text().count(rejection)
        self.assertEqual(count, 2, f"{gitlab} expected 2 rejection checks, found {count}")

    def test_genesis_converges_platform_scaffold_all_branches(self):
        for task in (
            ROOT / "tasks/genesis_scm_github.yml",
            ROOT / "tasks/genesis_scm_gitlab.yml",
        ):
            content = task.read_text()
            self.assertIn("every mapped branch", content, task)
            self.assertIn("platform branch scaffold", content, task)
            self.assertIn("Converge platform", content, task)

    def test_genesis_builds_control_repo_record_before_inventory(self):
        """Ansible cannot reference a key set in the same set_fact task."""
        content = (ROOT / "genesis.yml").read_text()
        control_idx = content.index("Build control repository inventory record")
        inventory_idx = content.index("Build complete Genesis repository inventory")
        self.assertLess(control_idx, inventory_idx)
        control_block = content[control_idx:inventory_idx]
        inventory_block = content[inventory_idx : inventory_idx + 400]
        self.assertIn("control_repo_record:", control_block)
        self.assertNotIn("genesis_repos:", control_block)
        self.assertIn("genesis_repos:", inventory_block)
        self.assertIn("control_repo_record", inventory_block)

    def test_precreated_empty_repositories_use_existing_managed_content(self):
        gh_genesis = (ROOT / "tasks/genesis_scm_github.yml").read_text()
        gl_genesis = (ROOT / "tasks/genesis_scm_gitlab.yml").read_text()
        gh_bootstrap = (ROOT / "tasks/bootstrap_scm_github.yml").read_text()
        gl_bootstrap = (ROOT / "tasks/bootstrap_scm_gitlab.yml").read_text()

        for content in (gh_genesis, gl_genesis):
            self.assertIn("Initialize empty repositories with their final README", content)
            self.assertIn("Genesis: initialize managed repository [skip ci]", content)
        for content in (gh_bootstrap, gl_bootstrap):
            self.assertIn("Initialize empty tenant", content)
            self.assertIn("immutable marker", content)
            self.assertIn("Bootstrap: initialize tenant scaffold identity [skip ci]", content)

        self.assertIn("status_code: [200, 404, 409]", gh_bootstrap)
        self.assertIn("item.json.empty_repo", gl_bootstrap)
        all_provider_tasks = gh_genesis + gl_genesis + gh_bootstrap + gl_bootstrap
        self.assertNotIn("repository-init", all_provider_tasks)

    def test_pipelines_share_registry_lifecycle_and_identity_contract(self):
        for pipeline in PIPELINES:
            content = pipeline.read_text()
            self.assertIn("validate-registry", content, pipeline)
            self.assertIn("diff-tenants", content, pipeline)
            self.assertIn("tenant_id", content, pipeline)
            self.assertIn("aap_organization", content, pipeline)
            self.assertIn("team_name", content, pipeline)
            self.assertIn("repo_names", content, pipeline)
            self.assertNotIn(".engine/schemas/naming-rules.yml", content, pipeline)

    def test_pipelines_validate_folder_and_flat_layouts_consistently(self):
        for pipeline in PIPELINES:
            content = pipeline.read_text()
            self.assertIn("if os.path.isdir('base') else ['.']", content, pipeline)
            self.assertIn("if search_dir == '.'", content, pipeline)
            self.assertIn("fname in skip_files", content, pipeline)
            self.assertIn(".aap-casc-engine", content, pipeline)

        naming_validator = (ROOT / "schemas/validate_naming.py").read_text()
        self.assertIn('".aap-casc-engine"', naming_validator)

    def test_tenant_lifecycle_diff_fails_when_previous_commit_is_unavailable(self):
        for pipeline in PIPELINES:
            content = pipeline.read_text()
            self.assertIn("git cat-file -e", content, pipeline)
            self.assertIn("refusing an unsafe tenant lifecycle diff", content, pipeline)

    def test_dispatcher_and_drift_have_no_naming_policy_dependency(self):
        for path in (ROOT / "site.yml", ROOT / "drift-detect.yml"):
            content = path.read_text()
            self.assertNotIn("naming-rules", content, path)
            self.assertNotIn("validate_naming", content, path)

    def test_generated_callers_use_tenant_id_and_control_token(self):
        callers = (
            ROOT / "templates/github-workflow-caller.yml.j2",
            ROOT / "templates/gitlab-ci-caller.yml.j2",
        )
        for caller in callers:
            content = caller.read_text()
            self.assertIn("tenant_id", content, caller)
            self.assertIn("CONTROL", content, caller)

    def test_generated_callers_render_for_every_role(self):
        jinja = Environment(loader=FileSystemLoader(str(ROOT)))
        context = {
            "platform_scm_org": "ww-platform",
            "engine_repo": "aap-casc-engine",
            "control_scm_org": "ww-platform",
            "control_repo": "casc-platform-control",
            "control_branch": "main",
        }
        for role in ("control", "platform", "tenant"):
            with self.subTest(provider="github", role=role):
                rendered = jinja.get_template(
                    "templates/github-workflow-caller.yml.j2"
                ).render(**context, caller_role=role)
                yaml.safe_load(rendered)
                self.assertIn(f"caller_role: {role}", rendered)
                self.assertEqual("AAP_ENGINE_TOKEN" in rendered, role == "control")
            with self.subTest(provider="gitlab", role=role):
                rendered = jinja.get_template(
                    "templates/gitlab-ci-caller.yml.j2"
                ).render(**context, caller_role=role)
                yaml.safe_load(rendered)
                self.assertIn(f"CASC_CALLER_ROLE: '{role}'", rendered)

    def test_dispatch_pause_skips_only_tenant_onboarding_scope(self):
        with mock.patch.object(
            casc_runtime, "resolve_env_creds", return_value=("https://aap", ["-H", "token"])
        ), mock.patch.object(casc_runtime, "launch_dispatcher", return_value=42) as launch, mock.patch.object(
            casc_runtime, "wait_for_terminal"
        ):
            casc_runtime.run_bounded_onboarding(
                environments=["dev"],
                tenant_id="stores",
                control_revision="a" * 40,
                poll_timeout=1,
                jt_name="dispatcher",
                tenant_dispatch_enabled=False,
            )
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(launch.call_args.kwargs["extra_vars"]["dispatch_scope"], "platform")
        for pipeline in PIPELINES:
            content = pipeline.read_text()
            self.assertIn("dispatch_enabled", content, pipeline)
            if "gitlab" in str(pipeline):
                self.assertIn("BOOTSTRAP_DISPATCH_TENANT_IDS", content)
            else:
                self.assertIn("dispatch_tenant_ids", content)

    def test_cross_namespace_clone_paths_are_collision_safe(self):
        role = (ROOT / "roles/git_clone_repos/tasks/main.yml").read_text()
        self.assertIn("item.clone_name | default(item.name)", role)
        for playbook in (ROOT / "site.yml", ROOT / "drift-detect.yml"):
            content = playbook.read_text()
            self.assertIn("'clone_name': item.0.tenant_id + '__' + item.1", content)
            self.assertIn("casc_repo.clone_name | default(casc_repo.name)", content)

    def test_control_revision_is_an_immutable_commit_pin(self):
        with self.assertRaisesRegex(ValueError, "full hexadecimal commit SHA"):
            casc_runtime.ensure_control_files(
                provider="github",
                org="ww-platform",
                repo="casc-platform-control",
                branch="main",
                token="token",
                revision="main",
            )

        def control_file(**kwargs):
            if kwargs["path"] == "config.yml":
                return "env_branch_map:\n  dev: develop\n"
            if kwargs["path"] == "tenants.yml":
                return "tenants: []\n"
            raise urllib.error.HTTPError("url", 404, "missing", {}, None)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            casc_runtime, "fetch_control_text", side_effect=control_file
        ):
            revision = casc_runtime.ensure_control_files(
                provider="github",
                org="ww-platform",
                repo="casc-platform-control",
                branch="main",
                token="token",
                revision="A" * 40,
                dest_dir=tmp,
            )
            self.assertEqual(revision, "a" * 40)
            self.assertTrue((Path(tmp) / "config.yml").exists())
            self.assertTrue((Path(tmp) / "tenants.yml").exists())
            self.assertFalse((Path(tmp) / "naming-rules.yml").exists())

    def test_deleted_bootstrap_templates_have_no_consumers(self):
        deleted = (
            "user-template.yml.j2",
            "rbac-user-template.yml.j2",
            "rbac-team-template.yml.j2",
            "seed-combined-tenant.yml.j2",
        )
        all_text = "\n".join(
            path.read_text(errors="ignore")
            for path in ROOT.rglob("*")
            if path.is_file()
            and ".git" not in path.parts
            and "tests" not in path.parts
            and path.suffix in {".yml", ".j2", ".py", ".md"}
        )
        for name in deleted:
            self.assertFalse((ROOT / "templates" / name).exists())
            self.assertNotIn(name, all_text)


class MigrationAndDocumentationTests(unittest.TestCase):
    def test_migration_does_not_synthesize_naming_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "legacy"
            source.mkdir()
            (source / "config.yml").write_text(
                "platform_scm_org: example\nplatform_repo_pattern: combined\n"
                "env_branch_map:\n  dev: develop\n  prd: main\n",
                encoding="utf-8",
            )
            (source / "tenants.yml").write_text("tenants: []\n", encoding="utf-8")
            (source / "base").mkdir()
            output = Path(tmp) / "out"
            rc = migrate_control_plane.plan_legacy_split(
                argparse_namespace(
                    source_repo=str(source),
                    output_dir=str(output),
                    control_scm_org="example",
                    platform_scm_org="example",
                    control_repo="casc-platform-control",
                    control_branch="main",
                    platform_repo_name="casc-platform-global",
                    apply=False,
                )
            )
            self.assertEqual(rc, 0)
            self.assertFalse(
                (output / "casc-platform-control" / "naming-rules.yml").exists()
            )

    def test_required_docs_cover_lean_contract(self):
        docs = [
            ROOT / "README.md",
            ROOT / "docs/ENGINE_SETUP_AND_OPERATIONS_GUIDE.md",
            ROOT / "docs/NONPRODUCTION_VALIDATION.md",
            ROOT / "docs/pipeline-trigger-logic.md",
            ROOT / "docs/resource-deletion-capabilities.md",
            ROOT / "templates/genesis-readme.md.j2",
        ]
        combined = "\n".join(path.read_text() for path in docs)
        for required in (
            "tenant_id",
            "aap_organization",
            "Brownfield",
            "naming-rules.yml",
            "Organization",
            "Team",
        ):
            self.assertIn(required, combined)


if __name__ == "__main__":
    unittest.main()
