"""Unit/contract tests for ROADMAP-002 identity_presence drift compare."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "pipeline"))

import drift_compare  # noqa: E402
import generate_resource_catalog  # noqa: E402
import yaml  # noqa: E402


class DriftCompareUnitTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "CONTROLLER_HOST": "https://aap.example.com",
            "CONTROLLER_OAUTH_TOKEN": "test-token",
            "CONTROLLER_VERIFY_SSL": "false",
            "TARGET_ENV": "poc",
            "CONTROL_REVISION": "a" * 40,
        }

    def _patch_env(self):
        return mock.patch.dict(os.environ, self.env, clear=False)

    def _json_response(self, results, count=None):
        payload = {"results": results, "count": len(results) if count is None else count}
        body = json.dumps(payload).encode("utf-8")
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = body
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        return response

    def test_adapters_locked_to_five_keys(self):
        self.assertEqual(
            set(drift_compare.COMPARED_KEYS),
            {
                "aap_organizations",
                "aap_teams",
                "controller_credential_types",
                "controller_projects",
                "controller_inventories",
            },
        )
        self.assertNotIn("controller_templates", drift_compare.ADAPTERS)

    def test_missing_found_and_ambiguous(self):
        calls: list[str] = []

        def open_side_effect(request, timeout=60):
            calls.append(request.full_url)
            if "organizations/" in request.full_url:
                return self._json_response([])
            if "teams/" in request.full_url:
                return self._json_response(
                    [{"name": "Stores Automation", "summary_fields": {"organization": {"name": "stores"}}}]
                )
            if "credential_types/" in request.full_url:
                return self._json_response(
                    [{"name": "CasC SCM Token"}, {"name": "CasC SCM Token"}]
                )
            return self._json_response([])

        opener = mock.MagicMock()
        opener.open.side_effect = open_side_effect
        desired = {
            "aap_organizations": [{"name": "stores"}],
            "aap_teams": [{"name": "Stores Automation", "organization": "stores"}],
            "controller_credential_types": [{"name": "CasC SCM Token"}],
            "controller_projects": [],
            "controller_inventories": [],
        }
        with self._patch_env(), mock.patch.object(
            drift_compare, "_opener", return_value=opener
        ):
            with self.assertRaisesRegex(drift_compare.DriftCompareError, "ambiguous"):
                drift_compare.compare_desired(desired)

        # Before ambiguity, org missing and team found paths were exercised
        self.assertTrue(any("organizations/" in url for url in calls))
        self.assertTrue(any("teams/" in url for url in calls))
        self.assertTrue(any("organization__name=stores" in url for url in calls))

    def test_count_zero_is_missing_one_is_found(self):
        def open_side_effect(request, timeout=60):
            if "name=missing-org" in request.full_url:
                return self._json_response([])
            return self._json_response([{"name": "present-org"}])

        opener = mock.MagicMock()
        opener.open.side_effect = open_side_effect
        desired = {
            "aap_organizations": [
                {"name": "missing-org"},
                {"name": "present-org"},
            ],
            "aap_teams": [],
            "controller_credential_types": [],
            "controller_projects": [],
            "controller_inventories": [],
        }
        with self._patch_env(), mock.patch.object(
            drift_compare, "_opener", return_value=opener
        ):
            report = drift_compare.compare_desired(desired)

        self.assertEqual(report["schema_version"], 2)
        self.assertTrue(report["drift_detected"])
        self.assertEqual(report["summary"]["missing_in_live"], 1)
        self.assertEqual(
            report["details"]["aap_organizations"]["missing_in_live"],
            [{"identity": {"name": "missing-org"}}],
        )
        self.assertNotIn("extra_in_live", json.dumps(report))
        self.assertNotIn("unmanaged_live", json.dumps(report))

    def test_incomplete_identity_fails_closed(self):
        with self._patch_env(), mock.patch.object(
            drift_compare, "_opener", return_value=mock.MagicMock()
        ):
            with self.assertRaisesRegex(
                drift_compare.DriftCompareError, "incomplete identity"
            ):
                drift_compare.compare_desired(
                    {
                        "aap_organizations": [],
                        "aap_teams": [{"name": "Team Without Org"}],
                        "controller_credential_types": [],
                        "controller_projects": [],
                        "controller_inventories": [],
                    }
                )

    def test_url_encoding_for_spaces(self):
        captured: list[str] = []

        def open_side_effect(request, timeout=60):
            captured.append(request.full_url)
            return self._json_response([{"name": "CasC SCM Token"}])

        opener = mock.MagicMock()
        opener.open.side_effect = open_side_effect
        with self._patch_env(), mock.patch.object(
            drift_compare, "_opener", return_value=opener
        ):
            drift_compare.compare_desired(
                {
                    "aap_organizations": [],
                    "aap_teams": [],
                    "controller_credential_types": [{"name": "CasC SCM Token"}],
                    "controller_projects": [],
                    "controller_inventories": [],
                }
            )
        self.assertTrue(any("CasC+SCM+Token" in url or "CasC%20SCM%20Token" in url for url in captured))

    def test_undeclared_keys_ignored(self):
        opener = mock.MagicMock()
        opener.open.return_value = self._json_response([])
        desired = {
            "aap_organizations": [],
            "aap_teams": [],
            "controller_credential_types": [],
            "controller_projects": [],
            "controller_inventories": [],
            "controller_templates": [{"name": "should-not-query"}],
            "controller_credentials": [{"name": "also-ignored"}],
        }
        with self._patch_env(), mock.patch.object(
            drift_compare, "_opener", return_value=opener
        ):
            report = drift_compare.compare_desired(desired)
        opener.open.assert_not_called()
        self.assertFalse(report["drift_detected"])
        self.assertEqual(set(report["details"]), set(drift_compare.COMPARED_KEYS))

    def test_empty_desired_requires_credentials(self):
        empty = {
            "aap_organizations": [],
            "aap_teams": [],
            "controller_credential_types": [],
            "controller_projects": [],
            "controller_inventories": [],
        }
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(
                drift_compare.DriftCompareError, "CONTROLLER_HOST|required"
            ):
                drift_compare.compare_desired(empty)
        with mock.patch.dict(
            os.environ,
            {"CONTROLLER_HOST": "https://aap.example.com"},
            clear=True,
        ):
            with self.assertRaisesRegex(
                drift_compare.DriftCompareError, "CONTROLLER_OAUTH_TOKEN|PASSWORD"
            ):
                drift_compare.compare_desired(empty)

    def test_duplicate_declared_identity_fails_closed(self):
        with self._patch_env(), mock.patch.object(
            drift_compare, "_opener", return_value=mock.MagicMock()
        ):
            with self.assertRaisesRegex(
                drift_compare.DriftCompareError, "duplicate declared identity"
            ):
                drift_compare.compare_desired(
                    {
                        "aap_organizations": [
                            {"name": "stores"},
                            {"name": "stores"},
                        ],
                        "aap_teams": [],
                        "controller_credential_types": [],
                        "controller_projects": [],
                        "controller_inventories": [],
                    }
                )

    def test_result_identity_mismatch_fails_closed(self):
        opener = mock.MagicMock()
        opener.open.return_value = self._json_response([{"name": "wrong-org"}])
        with self._patch_env(), mock.patch.object(
            drift_compare, "_opener", return_value=opener
        ):
            with self.assertRaisesRegex(
                drift_compare.DriftCompareError, "identity mismatch"
            ):
                drift_compare.compare_desired(
                    {
                        "aap_organizations": [{"name": "stores"}],
                        "aap_teams": [],
                        "controller_credential_types": [],
                        "controller_projects": [],
                        "controller_inventories": [],
                    }
                )

    def test_count_must_equal_results_length(self):
        opener = mock.MagicMock()
        opener.open.return_value = self._json_response(
            [{"name": "stores"}], count=2
        )
        with self._patch_env(), mock.patch.object(
            drift_compare, "_opener", return_value=opener
        ):
            with self.assertRaisesRegex(
                drift_compare.DriftCompareError, "count=2 != len\\(results\\)=1"
            ):
                drift_compare.compare_desired(
                    {
                        "aap_organizations": [{"name": "stores"}],
                        "aap_teams": [],
                        "controller_credential_types": [],
                        "controller_projects": [],
                        "controller_inventories": [],
                    }
                )

    def test_http_401_and_403_fail_closed(self):
        for code in (401, 403):
            opener = mock.MagicMock()
            opener.open.side_effect = urllib.error.HTTPError(
                "https://aap.example.com/api/gateway/v1/organizations/",
                code,
                "denied",
                hdrs=None,
                fp=io.BytesIO(b""),
            )
            with self._patch_env(), mock.patch.object(
                drift_compare, "_opener", return_value=opener
            ):
                with self.assertRaisesRegex(
                    drift_compare.DriftCompareError, "authorization failure"
                ):
                    drift_compare.compare_desired(
                        {
                            "aap_organizations": [{"name": "stores"}],
                            "aap_teams": [],
                            "controller_credential_types": [],
                            "controller_projects": [],
                            "controller_inventories": [],
                        }
                    )

    def test_main_writes_report_without_secrets(self):
        opener = mock.MagicMock()
        opener.open.return_value = self._json_response([{"name": "stores"}])
        with tempfile.TemporaryDirectory() as tmp:
            desired_path = Path(tmp) / "desired.json"
            report_path = Path(tmp) / "report.json"
            desired_path.write_text(
                json.dumps(
                    {
                        "aap_organizations": [{"name": "stores"}],
                        "aap_teams": [],
                        "controller_credential_types": [],
                        "controller_projects": [],
                        "controller_inventories": [],
                    }
                ),
                encoding="utf-8",
            )
            with self._patch_env(), mock.patch.object(
                drift_compare, "_opener", return_value=opener
            ):
                rc = drift_compare.main(
                    ["--desired", str(desired_path), "--report", str(report_path)]
                )
            self.assertEqual(rc, 0)
            report_text = report_path.read_text(encoding="utf-8")
            self.assertNotIn("test-token", report_text)
            report = json.loads(report_text)
            self.assertEqual(report["schema_version"], 2)


class DriftContractTreeTests(unittest.TestCase):
    def test_no_remediate_path_or_drift_mode(self):
        self.assertFalse((ROOT / "remediate.yml").exists())
        drift = (ROOT / "drift-detect.yml").read_text(encoding="utf-8")
        self.assertNotIn("drift_mode", drift)
        self.assertNotIn("DRIFT_MODE", drift)
        self.assertNotIn("remediate.yml", drift)
        self.assertIn("report-only", drift)
        self.assertIn("drift_compare.py", drift)
        self.assertIn("no_log: true", drift)

    def test_catalog_drift_keys_and_classifications(self):
        keys = generate_resource_catalog.current_drift_keys()
        self.assertEqual(
            keys,
            {
                "aap_organizations",
                "aap_teams",
                "controller_credential_types",
                "controller_projects",
                "controller_inventories",
            },
        )
        schema = yaml.safe_load(
            (ROOT / "schemas/resource-types.yml").read_text(encoding="utf-8")
        )
        defaults = schema["defaults"]
        self.assertEqual(defaults.get("drift_comparison"), "unsupported")
        for key, value in schema["exceptions"].items():
            meta = {**defaults, **(value or {})}
            mode = meta["drift_comparison"]
            self.assertIn(mode, {"identity_presence", "unsupported"}, key)
            self.assertTrue(str(meta.get("drift_evidence", "")).strip(), key)

    def test_credential_types_adapter_omits_managed_filter(self):
        adapter = drift_compare.ADAPTERS["controller_credential_types"]
        self.assertEqual(adapter["desired_fields"], ("name",))
        self.assertNotIn("query_fields", adapter)
        self.assertNotIn("managed", adapter.get("query_map", {}))
        query = drift_compare._query_for_identity(
            "controller_credential_types", {"name": "Machine"}
        )
        self.assertEqual(query, {"name": "Machine"})
        self.assertNotIn("managed", query)


if __name__ == "__main__":
    unittest.main()
