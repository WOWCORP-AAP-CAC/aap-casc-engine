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
        "onboarding_mode": "greenfield",
    }
    record.update(overrides)
    return record


def brownfield(tenant_id="legacy", **overrides):
    record = {
        "tenant_id": tenant_id,
        "aap_organization": "Legacy LDAP Organization",
        "tenant_scm_org": "ww-tenants",
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
        self.assertEqual(runtime["repository"], "casc-tenant-stores")
        self.assertNotIn("repositories", runtime)
        self.assertNotIn("repo_by_folder", runtime)
        self.assertNotIn("repo_pattern", runtime)
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
            self.assertEqual(casc_runtime.validate_tenant_id(valid), valid)
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
                casc_runtime.validate_tenant_id(invalid)

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

    def test_custom_scalar_repository_and_legacy_rejection(self):
        runtime = casc_runtime.public_tenant_runtime(greenfield(repo_name="ww-tenant-stores"))
        self.assertEqual(runtime["repository"], "ww-tenant-stores")
        self.assertEqual(
            casc_runtime.resolve_tenant_repository("stores"), "casc-tenant-stores"
        )
        self.assertEqual(
            casc_runtime.platform_repo_name(base_config()), "casc-platform-global"
        )
        with self.assertRaisesRegex(ValueError, "removed topology fields"):
            casc_runtime.normalize_tenant_record(greenfield(repo_pattern="combined"))
        with self.assertRaisesRegex(ValueError, "removed topology fields"):
            casc_runtime.normalize_tenant_record(greenfield(repo_names={"projects": "x"}))
        with self.assertRaisesRegex(ValueError, "removed topology fields"):
            casc_runtime.platform_repo_name(
                base_config(platform_repo_pattern="combined")
            )
        with self.assertRaisesRegex(ValueError, "removed topology fields"):
            casc_runtime.reject_legacy_config_fields(
                base_config(repo_mode="create")
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
        self.assertIn("item.tenant_scm_org + '/' + item.repository", site)
        for pipeline in PIPELINES:
            content = pipeline.read_text()
            if "gitlab" in str(pipeline):
                self.assertIn("CI_PROJECT_PATH", content)
            else:
                self.assertIn("GITHUB_REPOSITORY", content)

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
            tenant, repository="casc-tenant-stores"
        )
        self.assertEqual(expected["scaffold_version"], 3)
        self.assertEqual(expected["repository"], "casc-tenant-stores")
        self.assertNotIn("resource_type", expected)
        casc_runtime.validate_scaffold_marker(dict(expected), expected)
        changed = dict(expected, aap_organization="Other")
        with self.assertRaisesRegex(ValueError, "aap_organization"):
            casc_runtime.validate_scaffold_marker(changed, expected)
        extra = dict(expected, unexpected_identity="someone")
        with self.assertRaisesRegex(ValueError, "unexpected_identity"):
            casc_runtime.validate_scaffold_marker(extra, expected)

        brown = brownfield()
        marker = casc_runtime.build_scaffold_marker(
            brown, repository="casc-tenant-legacy"
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

    def test_second_survey_tenant_onboards_with_nonempty_registry(self):
        existing = greenfield("stores", repo_name="ww-tenant-stores")
        request = greenfield("network", team_name="Network Automation")
        resolved, registered = casc_runtime.resolve_bootstrap_request(
            {"tenants": [existing]}, base_config(), request
        )
        self.assertFalse(registered)
        self.assertEqual(resolved["tenant_id"], "network")
        self.assertEqual(resolved["repository"], "casc-tenant-network")
        self.assertNotIn("repositories", resolved)

    def test_registered_repo_name_compares_against_repository(self):
        doc = {"tenants": [greenfield(repo_name="ww-tenant-stores")]}
        cfg = base_config()
        matched, registered = casc_runtime.resolve_bootstrap_request(
            doc, cfg, {"tenant_id": "stores", "repo_name": "ww-tenant-stores"}
        )
        self.assertTrue(registered)
        self.assertEqual(matched["repository"], "ww-tenant-stores")
        omitted, registered = casc_runtime.resolve_bootstrap_request(
            doc, cfg, {"tenant_id": "stores"}
        )
        self.assertTrue(registered)
        self.assertEqual(omitted["repository"], "ww-tenant-stores")
        with self.assertRaisesRegex(ValueError, "conflict"):
            casc_runtime.resolve_bootstrap_request(
                doc, cfg, {"tenant_id": "stores", "repo_name": "other-repo"}
            )

    def test_resolve_jt_names_rejects_legacy_config_fields(self):
        names = casc_runtime.resolve_jt_names(base_config())
        self.assertEqual(names["bootstrap"], "jt-platform-bootstrap_tenant")
        with self.assertRaisesRegex(ValueError, "removed topology fields"):
            casc_runtime.resolve_jt_names(
                base_config(platform_repo_pattern="combined")
            )


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
        custom = casc_runtime.iter_foundation_targets(
            base_config(platform_repo="ww-governed-platform"), "stores"
        )
        self.assertEqual(
            custom,
            [
                ("ww-governed-platform", "base/organizations/stores.yml"),
                ("ww-governed-platform", "base/teams/stores.yml"),
            ],
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

    def test_canonical_naming_sample_is_inert_commented_source(self):
        sample_path = ROOT / "examples/naming-rules.yml.sample"
        sample = sample_path.read_text(encoding="utf-8")
        self.assertIn("rename", sample.lower())
        self.assertIn("adapt", sample.lower())
        self.assertIn("uncomment", sample.lower())
        self.assertIn("REPLACE_ME", sample)
        self.assertNotIn("WW ", sample)
        self.assertFalse((ROOT / "examples/naming-rules-type-prefixed.yml.sample").exists())
        # Commented-only sample body loads as an inactive empty policy.
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "naming-rules.yml"
            rules.write_text(sample, encoding="utf-8")
            self.assertEqual(self.load(rules), {})

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
            control = desired / ".control"
            control.mkdir(parents=True)
            (control / "config.yml").write_text(
                "env_branch_map:\n  poc: dev\n  prod: main\n",
                encoding="utf-8",
            )
            org_dir = desired / "base" / "organizations"
            team_dir = desired / "base" / "teams"
            org_dir.mkdir(parents=True)
            team_dir.mkdir(parents=True)
            (org_dir / "organizations.yml").write_text(
                jinja.get_template("templates/org-template.yml.j2").render(**context),
                encoding="utf-8",
            )
            (team_dir / "teams.yml").write_text(
                jinja.get_template("templates/team-template.yml.j2").render(**context),
                encoding="utf-8",
            )
            rules = self.load(rules_path)
            self.assertEqual(validate_naming.validate_tree(str(desired), rules), [])

            context["_effective_team_name"] = "Stores"
            (team_dir / "teams.yml").write_text(
                jinja.get_template("templates/team-template.yml.j2").render(**context),
                encoding="utf-8",
            )
            self.assertTrue(validate_naming.validate_tree(str(desired), rules))

            # Restore a valid team, then prove unrelated docs/ YAML is ignored.
            context["_effective_team_name"] = "Stores Automation"
            (team_dir / "teams.yml").write_text(
                jinja.get_template("templates/team-template.yml.j2").render(**context),
                encoding="utf-8",
            )
            docs = desired / "docs"
            docs.mkdir()
            (docs / "notes.yml").write_text(
                "aap_organizations:\n  - name: BAD NAME\n",
                encoding="utf-8",
            )
            self.assertEqual(validate_naming.validate_tree(str(desired), rules), [])

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
            control = root / ".control"
            control.mkdir()
            (control / "config.yml").write_text(
                "env_branch_map:\n  poc: dev\n  prod: main\n",
                encoding="utf-8",
            )
            policy = Path(tmp) / "policy-source.txt"
            policy.write_text("aap_organizations:\n  pattern: '^WW .+$'\n", encoding="utf-8")
            (root / "naming-rules.yml").write_text(
                "aap_organizations:\n  pattern: invalid-as-resource-data\n", encoding="utf-8"
            )
            rules = self.load(policy)
            self.assertEqual(validate_naming.validate_tree(str(root), rules), [])

    def test_genesis_seeds_inert_sample_not_active_policy(self):
        genesis = (ROOT / "genesis.yml").read_text(encoding="utf-8")
        self.assertIn("examples/naming-rules.yml.sample", genesis)
        self.assertIn("_naming_rules_sample_content", genesis)
        self.assertIn("rstrip=false", genesis)
        self.assertNotIn("_control_sample_branches", genesis)
        # lookup must preserve trailing newline of the canonical sample bytes.
        self.assertRegex(
            genesis,
            r"lookup\('file',\s*playbook_dir\s*~\s*'/examples/naming-rules\.yml\.sample',\s*rstrip=false\)",
        )
        for task in (
            ROOT / "tasks/genesis_scm_github.yml",
            ROOT / "tasks/genesis_scm_gitlab.yml",
        ):
            content = task.read_text(encoding="utf-8")
            self.assertIn("naming-rules.yml.sample", content)
            self.assertIn("Keep customer-modified naming-rules.yml.sample unchanged", content)
            self.assertIn("control_branch", content)
            self.assertNotIn("_control_sample_branches", content)
            self.assertNotIn("Render naming-rules", content)
            # Never write active naming-rules.yml from Genesis.
            self.assertNotRegex(content, r"contents/naming-rules\.yml[^.]")
            self.assertNotRegex(content, r"files/naming-rules\.yml[^.]")
        sample_bytes = (ROOT / "examples/naming-rules.yml.sample").read_bytes()
        self.assertTrue(sample_bytes.endswith(b"\n"))
        self.assertFalse((ROOT / "schemas/naming-rules.yml").exists())
        self.assertFalse((ROOT / "templates/naming-rules.yml.j2").exists())
        self.assertFalse((ROOT / "templates/naming-rules.yml.sample.j2").exists())

    def test_bootstrap_naming_preflight_uses_base_layout_and_pinned_control(self):
        bootstrap = (ROOT / "bootstrap.yml").read_text(encoding="utf-8")
        self.assertIn(
            'path: "base/organizations/{{ _effective_tenant_id }}.yml"',
            bootstrap,
        )
        self.assertIn(
            'path: "base/teams/{{ _effective_tenant_id }}.yml"',
            bootstrap,
        )
        self.assertIn("--control-config", bootstrap)
        self.assertIn(
            "{{ bootstrap_clone_dir }}/{{ control_repo }}/config.yml",
            bootstrap,
        )
        # Flat root foundation files are not scanned by desired_state_search_dirs.
        self.assertNotIn(
            '{ name: organizations.yml, content: "{{ _org_foundation_content | default(\'\') }}" }',
            bootstrap,
        )


class DeletionSafetyTests(unittest.TestCase):
    @staticmethod
    def _write_pinned_control(root: Path) -> Path:
        control = root / ".control"
        control.mkdir(parents=True, exist_ok=True)
        cfg = control / "config.yml"
        cfg.write_text(
            "scm_provider: github\n"
            "control_scm_org: org\n"
            "control_repo: control\n"
            "control_branch: main\n"
            "platform_scm_org: org\n"
            "platform_repo: casc-platform-global\n"
            "env_branch_map:\n  poc: dev\n  prod: main\n",
            encoding="utf-8",
        )
        return cfg

    def test_unsupported_explicit_deletion_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_pinned_control(root)
            base = root / "base" / "projects"
            base.mkdir(parents=True)
            target = base / "project.yml"
            target.write_text(
                "controller_projects:\n  - name: demo\n    state: absent\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "deletion is not audited"):
                casc_runtime.validate_explicit_deletions(
                    str(root), str(ROOT / "schemas/resource-types.yml")
                )

            target.write_text(
                "controller_projects:\n  - name: demo\n    state: present\n",
                encoding="utf-8",
            )
            casc_runtime.validate_explicit_deletions(
                str(root), str(ROOT / "schemas/resource-types.yml")
            )

    def test_control_repo_ignores_unrelated_root_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_pinned_control(root)
            (root / "platform-policy.yml").write_text(
                "unrelated-platform-policy:\n  owner: platform-governance\n",
                encoding="utf-8",
            )
            (root / "GOVERNANCE.md").write_text("# keep me\n", encoding="utf-8")

            # Control callers must ignore arbitrary root YAML as desired state.
            self.assertEqual(
                casc_runtime.iter_resource_yaml_files(str(root), caller_role="control"),
                [],
            )
            casc_runtime.validate_structure(
                str(root),
                str(ROOT / "schemas/resource-types.yml"),
                allowed_keys_path=str(
                    ROOT / "roles/process_casc_config/defaults/main.yml"
                ),
                caller_role="control",
            )
            casc_runtime.validate_explicit_deletions(
                str(root),
                str(ROOT / "schemas/resource-types.yml"),
                caller_role="control",
            )

    def test_platform_tenant_scan_only_base_and_env_branch_map_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_pinned_control(root)

            base = root / "base" / "projects"
            poc = root / "poc" / "projects"
            prod = root / "prod" / "projects"
            docs = root / "docs"
            governance = root / "governance"
            for path in (base, poc, prod, docs, governance):
                path.mkdir(parents=True)

            (base / "valid.yml").write_text(
                "controller_projects:\n  - name: base-demo\n",
                encoding="utf-8",
            )
            (poc / "valid.yml").write_text(
                "controller_projects:\n  - name: poc-demo\n",
                encoding="utf-8",
            )
            (prod / "valid.yml").write_text(
                "controller_projects:\n  - name: prod-demo\n",
                encoding="utf-8",
            )
            (docs / "notes.yml").write_text(
                "unrelated-docs:\n  keep: true\n",
                encoding="utf-8",
            )
            (governance / "policy.yml").write_text(
                "unrelated-governance:\n  keep: true\n",
                encoding="utf-8",
            )

            for role in ("platform", "tenant"):
                with self.subTest(role=role):
                    paths = casc_runtime.iter_resource_yaml_files(
                        str(root), caller_role=role
                    )
                    names = sorted(Path(p).name for p in paths)
                    self.assertEqual(names, ["valid.yml", "valid.yml", "valid.yml"])
                    joined = "\n".join(paths)
                    self.assertIn("/base/", joined)
                    self.assertIn("/poc/", joined)
                    self.assertIn("/prod/", joined)
                    self.assertNotIn("/docs/", joined)
                    self.assertNotIn("/governance/", joined)

                    casc_runtime.validate_structure(
                        str(root),
                        str(ROOT / "schemas/resource-types.yml"),
                        allowed_keys_path=str(
                            ROOT / "roles/process_casc_config/defaults/main.yml"
                        ),
                        caller_role=role,
                    )

            # Invalid YAML under a mapped env directory must fail closed.
            (poc / "bad.yml").write_text(
                "not_a_casc_resource:\n  - name: nope\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Unknown resource key"):
                casc_runtime.validate_structure(
                    str(root),
                    str(ROOT / "schemas/resource-types.yml"),
                    allowed_keys_path=str(
                        ROOT / "roles/process_casc_config/defaults/main.yml"
                    ),
                    caller_role="tenant",
                )

            # Invalid YAML under docs/ must remain ignored.
            (poc / "bad.yml").unlink()
            (docs / "also-bad.yml").write_text(
                "not_a_casc_resource:\n  - name: ignored\n",
                encoding="utf-8",
            )
            casc_runtime.validate_structure(
                str(root),
                str(ROOT / "schemas/resource-types.yml"),
                allowed_keys_path=str(
                    ROOT / "roles/process_casc_config/defaults/main.yml"
                ),
                caller_role="platform",
            )

    def test_explicit_control_config_is_authoritative_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Legacy root config.yml must not be used as a fallback.
            (root / "config.yml").write_text(
                "env_branch_map:\n  poc: dev\n  prod: main\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Pinned control config"):
                casc_runtime.desired_state_search_dirs(str(root))

            missing = root / "missing-control.yml"
            with self.assertRaisesRegex(ValueError, "Pinned control config not found"):
                casc_runtime.resolve_control_config_path(
                    str(root), control_config=str(missing)
                )

            pinned = self._write_pinned_control(root)
            self.assertEqual(
                casc_runtime.resolve_control_config_path(
                    str(root), control_config=str(pinned)
                ),
                str(pinned),
            )
            self.assertEqual(
                casc_runtime.resolve_control_config_path(str(root)),
                str(pinned),
            )

    def test_cli_default_control_config_resolves_under_root_not_cwd(self):
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp)
            cwd = outer / "cwd"
            root = outer / "desired-state"
            cwd.mkdir()
            control = root / ".control"
            projects = root / "base" / "projects"
            orgs = root / "base" / "organizations"
            control.mkdir(parents=True)
            projects.mkdir(parents=True)
            orgs.mkdir(parents=True)
            (control / "config.yml").write_text(
                "env_branch_map:\n  poc: dev\n  prod: main\n",
                encoding="utf-8",
            )
            (projects / "demo.yml").write_text(
                "controller_projects:\n  - name: demo\n",
                encoding="utf-8",
            )
            (orgs / "org.yml").write_text(
                "aap_organizations:\n  - name: WW Demo Org\n",
                encoding="utf-8",
            )
            policy = outer / "naming-rules.yml"
            policy.write_text(
                "aap_organizations:\n  pattern: '^WW .+$'\n",
                encoding="utf-8",
            )

            # No --control-config: must use <root>/.control/config.yml even when
            # the process cwd is elsewhere and has no .control/ directory.
            listed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/pipeline/casc_runtime.py"),
                    "list-desired-state-dirs",
                    "--root",
                    str(root),
                    "--caller-role",
                    "tenant",
                ],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertEqual(listed.stdout.strip(), "base")

            structure = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/pipeline/casc_runtime.py"),
                    "validate-structure",
                    "--root",
                    str(root),
                    "--caller-role",
                    "tenant",
                    "--resource-types",
                    str(ROOT / "schemas/resource-types.yml"),
                    "--allowed-keys",
                    str(ROOT / "roles/process_casc_config/defaults/main.yml"),
                ],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(structure.returncode, 0, structure.stderr + structure.stdout)

            naming = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "schemas/validate_naming.py"),
                    "--config-dir",
                    str(root),
                    "--rules",
                    str(policy),
                    "--resource-types",
                    str(ROOT / "schemas/resource-types.yml"),
                    "--allowed-keys",
                    str(ROOT / "roles/process_casc_config/defaults/main.yml"),
                    "--caller-role",
                    "tenant",
                ],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(naming.returncode, 0, naming.stderr + naming.stdout)
            self.assertIn("All configured naming rules passed", naming.stdout)

    def test_env_branch_map_keys_reject_traversal_and_invalid_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control = root / ".control"
            control.mkdir()
            cfg = control / "config.yml"

            bad_keys = {
                "../outside": 'env_branch_map:\n  "../outside": main\n',
                "BadEnv": "env_branch_map:\n  BadEnv: main\n",
                "poc-1": "env_branch_map:\n  poc-1: main\n",
                " poc": 'env_branch_map:\n  " poc": main\n',
                "": 'env_branch_map:\n  "": main\n',
            }
            for bad_key, content in bad_keys.items():
                with self.subTest(bad_key=bad_key):
                    cfg.write_text(content, encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, r"must match \^\[a-z\]"):
                        casc_runtime.load_env_names(str(root))
                    with self.assertRaisesRegex(ValueError, r"must match \^\[a-z\]"):
                        validate_naming.load_env_names(str(root))

            cfg.write_text(
                "env_branch_map:\n  poc: dev\n  prod: main\n",
                encoding="utf-8",
            )
            self.assertEqual(
                casc_runtime.load_env_names(str(root)),
                ["poc", "prod"],
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
        self.assertIn("Build platform repos list", content)
        self.assertIn("item.repository", content)

    def test_drift_platform_repos_preserves_native_lists(self):
        content = (ROOT / "drift-detect.yml").read_text()
        self.assertIn("Build platform repos list for drift check", content)
        self.assertIn("item.repository", content)
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

    def test_genesis_and_bootstrap_reject_removed_topology_inputs(self):
        genesis = (ROOT / "genesis.yml").read_text()
        bootstrap = (ROOT / "bootstrap.yml").read_text()
        self.assertIn("Reject removed topology launch inputs", genesis)
        self.assertIn("PLATFORM_REPO_PATTERN", genesis)
        self.assertIn("platform_repo | length > 0", genesis)
        self.assertIn("Reject removed topology launch inputs", bootstrap)
        self.assertIn("REPO_PATTERN", bootstrap)
        self.assertIn("control_config.platform_repo_pattern is not defined", bootstrap)
        readme = (ROOT / "templates/genesis-readme.md.j2").read_text()
        self.assertIn("platform desired-state repository", readme)
        self.assertNotIn("platform desired-state repositories", readme)

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
            self.assertIn("repo_name", content, pipeline)
            self.assertIn("platform_repo", content, pipeline)
            self.assertNotIn("platform_repo_pattern", content, pipeline)
            self.assertNotIn("repo_name_overrides", content, pipeline)
            self.assertNotIn(".engine/schemas/naming-rules.yml", content, pipeline)

    def test_pipelines_validate_folder_and_flat_layouts_consistently(self):
        for pipeline in PIPELINES:
            content = pipeline.read_text()
            self.assertIn("validate-structure", content, pipeline)
            self.assertIn("--caller-role", content, pipeline)
            self.assertIn("--control-config .control/config.yml", content, pipeline)
            self.assertIn("list-desired-state-dirs", content, pipeline)
            self.assertIn("paste -sd ' ' -", content, pipeline)
            self.assertNotIn("| tr '", content, pipeline)
            self.assertIn("Control repo: skipping desired-state", content, pipeline)
            self.assertNotIn("if os.path.isdir('base') else ['.']", content, pipeline)
            self.assertNotIn("ls -d */", content, pipeline)
            # All pipeline entrypoints must remain valid YAML.
            yaml.safe_load(content)

        naming_validator = (ROOT / "schemas/validate_naming.py").read_text()
        self.assertIn('".aap-casc-engine"', naming_validator)
        self.assertIn("caller_role", naming_validator)
        self.assertIn("desired_state_search_dirs", naming_validator)
        self.assertIn("env_branch_map", naming_validator)
        runtime = (ROOT / "scripts/pipeline/casc_runtime.py").read_text()
        self.assertIn("load_env_names", runtime)
        self.assertIn("env_branch_map", runtime)
        self.assertIn(".aap-casc-engine", runtime)
        self.assertIn("ENV_NAME_RE", runtime)

    def test_tenant_lifecycle_diff_fails_when_previous_commit_is_unavailable(self):
        for pipeline in PIPELINES:
            content = pipeline.read_text()
            self.assertIn("git cat-file -e", content, pipeline)
            self.assertIn("refusing an unsafe tenant lifecycle diff", content, pipeline)

    def test_reusable_onboarding_dispatch_uses_validate_engine_repo(self):
        """Thin callers make workflow_ref point at the caller, not the engine."""
        reusable = (
            ROOT / ".github/workflows/casc-validate-and-trigger.yml"
        ).read_text()
        onboarding = reusable.split("name: Protected Onboarding Dispatch", 1)[1]
        onboarding = onboarding.split("name: Trigger Dispatcher", 1)[0]
        # Protected continuation must reuse validate's derived engine_repo output.
        self.assertIn(
            "repository: ${{ needs.validate.outputs.engine_repo }}",
            onboarding,
        )
        # The broken pattern assigned ENGINE_REPO from workflow_ref and checked
        # out the thin caller, missing casc_runtime.py.
        self.assertNotIn('ENGINE_REPO="${{ github.workflow_ref }}"', onboarding)
        self.assertIn("scripts/pipeline", onboarding)

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
            self.assertIn("item.tenant_id + '__' + item.repository", content)
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
                "platform_scm_org: example\nplatform_repo: casc-platform-global\n"
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
                    platform_repo_name="",
                    apply=False,
                )
            )
            self.assertEqual(rc, 0)
            self.assertFalse(
                (output / "casc-platform-control" / "naming-rules.yml").exists()
            )
            migrated = yaml.safe_load(
                (output / "casc-platform-control" / "config.yml").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(migrated["platform_repo"], "casc-platform-global")
            self.assertNotIn("platform_repo_pattern", migrated)
            self.assertNotIn("repo_mode", migrated)

    def test_migration_preserves_scalars_and_emits_runtime_valid_tenants(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "legacy"
            source.mkdir()
            (source / "config.yml").write_text(
                "platform_scm_org: example\n"
                "platform_repo_pattern: combined\n"
                "platform_repos:\n"
                "  - resource_type: combined\n"
                "    name: ww-governed-platform\n"
                "repo_mode: existing\n"
                "env_branch_map:\n  dev: develop\n  prd: main\n",
                encoding="utf-8",
            )
            (source / "tenants.yml").write_text(
                "tenants:\n"
                "  - tenant_id: stores\n"
                "    team_name: Stores Automation\n"
                "    tenant_scm_org: ww-tenants\n"
                "    repo_pattern: combined\n"
                "    repositories:\n"
                "      - ww-tenant-stores\n"
                "    onboarding_mode: greenfield\n"
                "    status: active\n",
                encoding="utf-8",
            )
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
                    platform_repo_name="",
                    apply=False,
                )
            )
            self.assertEqual(rc, 0)
            self.assertTrue((output / "ww-governed-platform" / "base").exists())
            migrated_cfg = yaml.safe_load(
                (output / "casc-platform-control" / "config.yml").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(migrated_cfg["platform_repo"], "ww-governed-platform")
            self.assertNotIn("platform_repos", migrated_cfg)
            self.assertNotIn("platform_repo_pattern", migrated_cfg)
            self.assertNotIn("repo_mode", migrated_cfg)
            migrated_tenants = yaml.safe_load(
                (output / "casc-platform-control" / "tenants.yml").read_text(
                    encoding="utf-8"
                )
            )
            tenant = migrated_tenants["tenants"][0]
            self.assertEqual(tenant["repo_name"], "ww-tenant-stores")
            self.assertNotIn("repo_pattern", tenant)
            self.assertNotIn("repositories", tenant)
            # Transformed tenants.yml must validate under the new runtime contract.
            casc_runtime.validate_tenant_registry(migrated_tenants, migrated_cfg)

    def test_migration_rejects_invalid_scalars_and_preserves_repository(self):
        def run_split(tmp, config_body, tenants_body="tenants: []\n"):
            source = Path(tmp) / "legacy"
            source.mkdir()
            (source / "config.yml").write_text(config_body, encoding="utf-8")
            (source / "tenants.yml").write_text(tenants_body, encoding="utf-8")
            (source / "base").mkdir()
            return migrate_control_plane.plan_legacy_split(
                argparse_namespace(
                    source_repo=str(source),
                    output_dir=str(Path(tmp) / "out"),
                    control_scm_org="example",
                    platform_scm_org="example",
                    control_repo="casc-platform-control",
                    control_branch="main",
                    platform_repo_name="",
                    apply=False,
                )
            )

        for invalid in ("platform_repo: 123\n", "platform_repo: []\n", "platform_repo: ''\n"):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(SystemExit) as ctx:
                    run_split(
                        tmp,
                        "platform_scm_org: example\n"
                        + invalid
                        + "env_branch_map:\n  dev: develop\n",
                    )
                self.assertIn("platform_repo must be a non-empty string", str(ctx.exception))

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as ctx:
                run_split(
                    tmp,
                    "platform_scm_org: example\n"
                    "platform_repo: casc-platform-global\n"
                    "env_branch_map:\n  dev: develop\n",
                    "tenants:\n"
                    "  - tenant_id: stores\n"
                    "    team_name: Stores\n"
                    "    tenant_scm_org: ww\n"
                    "    repo_name: 123\n"
                    "    onboarding_mode: greenfield\n",
                )
            message = str(ctx.exception)
            self.assertTrue(
                "repo_name must be a non-empty string" in message
                or "not runtime-valid" in message
            )

        with tempfile.TemporaryDirectory() as tmp:
            rc = run_split(
                tmp,
                "platform_scm_org: example\n"
                "platform_repo: casc-platform-global\n"
                "env_branch_map:\n  dev: develop\n",
                "tenants:\n"
                "  - tenant_id: stores\n"
                "    team_name: Stores Automation\n"
                "    tenant_scm_org: ww-tenants\n"
                "    repository: ww-custom\n"
                "    onboarding_mode: greenfield\n"
                "    status: active\n",
            )
            self.assertEqual(rc, 0)
            migrated = yaml.safe_load(
                (Path(tmp) / "out/casc-platform-control/tenants.yml").read_text(
                    encoding="utf-8"
                )
            )
            tenant = migrated["tenants"][0]
            self.assertEqual(tenant["repo_name"], "ww-custom")
            self.assertNotIn("repository", tenant)
            cfg = yaml.safe_load(
                (Path(tmp) / "out/casc-platform-control/config.yml").read_text(
                    encoding="utf-8"
                )
            )
            casc_runtime.validate_tenant_registry(migrated, cfg)

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as ctx:
                run_split(
                    tmp,
                    "platform_scm_org: example\n"
                    "platform_repo: casc-platform-global\n"
                    "env_branch_map:\n  dev: develop\n",
                    "tenants:\n"
                    "  - tenant_id: stores\n"
                    "    team_name: Stores\n"
                    "    tenant_scm_org: ww\n"
                    "    repo_name: one-repo\n"
                    "    repository: other-repo\n"
                    "    onboarding_mode: greenfield\n",
                )
            self.assertIn("conflicting repository scalars", str(ctx.exception).lower())

    def test_migration_fails_closed_on_unconsolidated_per_resource(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "legacy"
            source.mkdir()
            (source / "config.yml").write_text(
                "platform_scm_org: example\n"
                "platform_repo_pattern: per-resource-type\n"
                "platform_repos:\n"
                "  - resource_type: organizations\n"
                "    name: ww-orgs\n"
                "  - resource_type: teams\n"
                "    name: ww-teams\n"
                "env_branch_map:\n  dev: develop\n  prd: main\n",
                encoding="utf-8",
            )
            (source / "tenants.yml").write_text("tenants: []\n", encoding="utf-8")
            with self.assertRaises(SystemExit) as ctx:
                migrate_control_plane.plan_legacy_split(
                    argparse_namespace(
                        source_repo=str(source),
                        output_dir=str(Path(tmp) / "out"),
                        control_scm_org="example",
                        platform_scm_org="example",
                        control_repo="casc-platform-control",
                        control_branch="main",
                        platform_repo_name="",
                        apply=False,
                    )
                )
            self.assertIn("manually consolidated", str(ctx.exception))

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "legacy"
            source.mkdir()
            (source / "config.yml").write_text(
                "platform_scm_org: example\n"
                "platform_repos:\n"
                "  - resource_type: organizations\n"
                "    name: ww-orgs\n"
                "env_branch_map:\n  dev: develop\n",
                encoding="utf-8",
            )
            (source / "tenants.yml").write_text("tenants: []\n", encoding="utf-8")
            with self.assertRaises(SystemExit) as ctx:
                migrate_control_plane.plan_legacy_split(
                    argparse_namespace(
                        source_repo=str(source),
                        output_dir=str(Path(tmp) / "out"),
                        control_scm_org="example",
                        platform_scm_org="example",
                        control_repo="casc-platform-control",
                        control_branch="main",
                        platform_repo_name="",
                        apply=False,
                    )
                )
            self.assertIn("manually consolidated", str(ctx.exception))
            self.assertIn("platform_repos", str(ctx.exception))

        with tempfile.TemporaryDirectory() as tmp:
            tenants = Path(tmp) / "tenants.yml"
            tenants.write_text(
                "tenants:\n"
                "  - tenant_id: stores\n"
                "    team_name: Stores\n"
                "    tenant_scm_org: ww\n"
                "    repo_names:\n"
                "      - stores-projects\n",
                encoding="utf-8",
            )
            with self.assertRaises(SystemExit) as ctx:
                migrate_control_plane.plan_tenant_identity_migration(
                    argparse_namespace(
                        tenants_file=str(tenants),
                        from_tenant_id="stores",
                        to_tenant_id="",
                        to_scm_org="",
                        to_repo_name="stores-combined",
                        to_aap_organization="",
                        output_dir=str(Path(tmp) / "out"),
                    )
                )
            self.assertIn("manually consolidated", str(ctx.exception))
            self.assertIn("repo_names", str(ctx.exception))

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
        guide = (ROOT / "docs/ENGINE_SETUP_AND_OPERATIONS_GUIDE.md").read_text()
        self.assertIn("combined-only", guide)
        self.assertNotIn("Per-resource-type layouts remain available", guide)


if __name__ == "__main__":
    unittest.main()
