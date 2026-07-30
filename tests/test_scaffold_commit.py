"""Bare-repo integration tests for ROADMAP-004 scaffold_commit helper."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "pipeline"))

import scaffold_commit  # noqa: E402


def _git(args: list[str], *, cwd: Path | None = None, env: dict | None = None) -> str:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    merged.setdefault("GIT_AUTHOR_NAME", "Test")
    merged.setdefault("GIT_AUTHOR_EMAIL", "test@example.com")
    merged.setdefault("GIT_COMMITTER_NAME", "Test")
    merged.setdefault("GIT_COMMITTER_EMAIL", "test@example.com")
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        env=merged,
        text=True,
        capture_output=True,
        check=True,
    )
    return (result.stdout or "").strip()


class ScaffoldCommitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="scaffold-test-"))
        self.bare = self.tmp / "remote.git"
        _git(["init", "--bare", "-b", "main", str(self.bare)])
        self.repo_url = str(self.bare)
        self.env = {"SCM_TOKEN": "test-token-not-for-url"}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _manifest(self, entries: list[dict], name: str = "manifest.json") -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(entries), encoding="utf-8")
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return path

    def _run(self, *, branch: str, entries: list[dict], message: str = "scaffold [skip ci]") -> str:
        manifest = self._manifest(entries)
        with mock.patch.dict(os.environ, self.env, clear=False):
            return scaffold_commit.scaffold_commit(
                repo_url=self.repo_url,
                branch=branch,
                provider="github",
                manifest_path=manifest,
                message=message,
            )

    def _clone_head(self, branch: str) -> Path:
        work = self.tmp / f"inspect-{branch}"
        if work.exists():
            shutil.rmtree(work)
        _git(["clone", "--branch", branch, str(self.bare), str(work)])
        return work

    def _log_count(self, branch: str) -> int:
        work = self._clone_head(branch)
        out = _git(["rev-list", "--count", "HEAD"], cwd=work)
        return int(out)

    def test_one_commit_contains_all_operation_files(self):
        entries = [
            {"path": ".aap-casc-engine/tenant-scaffold.yml", "policy": "immutable", "content": "marker: v1\n"},
            {"path": ".github/workflows/casc.yml", "policy": "converge", "content": "name: caller\n"},
            {"path": "base/projects/example.yml.sample", "policy": "create_only", "content": "sample: true\n"},
            {"path": "dev/projects/.gitkeep", "policy": "create_only", "content": ""},
        ]
        result = self._run(branch="main", entries=entries)
        self.assertEqual(result, "committed")
        self.assertEqual(self._log_count("main"), 1)
        work = self._clone_head("main")
        for entry in entries:
            self.assertEqual(
                (work / entry["path"]).read_text(encoding="utf-8"),
                entry["content"],
            )

    def test_converge_overwrites_differing_caller(self):
        first = [
            {"path": ".github/workflows/casc.yml", "policy": "converge", "content": "name: old\n"},
            {"path": "README.md", "policy": "create_only", "content": "hello\n"},
        ]
        self.assertEqual(self._run(branch="main", entries=first), "committed")
        second = [
            {"path": ".github/workflows/casc.yml", "policy": "converge", "content": "name: new\n"},
            {"path": "README.md", "policy": "create_only", "content": "engine readme\n"},
        ]
        self.assertEqual(self._run(branch="main", entries=second), "committed")
        self.assertEqual(self._log_count("main"), 2)
        work = self._clone_head("main")
        self.assertEqual((work / ".github/workflows/casc.yml").read_text(encoding="utf-8"), "name: new\n")
        # create_only preserves customer README
        self.assertEqual((work / "README.md").read_text(encoding="utf-8"), "hello\n")

    def test_create_only_and_unmanaged_files_preserved(self):
        first = [
            {"path": "README.md", "policy": "create_only", "content": "engine\n"},
            {"path": "base/example.yml.sample", "policy": "create_only", "content": "sample\n"},
        ]
        self._run(branch="main", entries=first)
        work = self._clone_head("main")
        (work / "customer.txt").write_text("keep me\n", encoding="utf-8")
        (work / "README.md").write_text("customer readme\n", encoding="utf-8")
        _git(["add", "-A"], cwd=work)
        _git(["commit", "-m", "customer edit"], cwd=work)
        _git(["push"], cwd=work)

        second = [
            {"path": "README.md", "policy": "create_only", "content": "engine\n"},
            {"path": "base/example.yml.sample", "policy": "create_only", "content": "sample\n"},
            {"path": ".github/workflows/casc.yml", "policy": "converge", "content": "name: caller\n"},
        ]
        self.assertEqual(self._run(branch="main", entries=second), "committed")
        work2 = self._clone_head("main")
        self.assertEqual((work2 / "README.md").read_text(encoding="utf-8"), "customer readme\n")
        self.assertEqual((work2 / "customer.txt").read_text(encoding="utf-8"), "keep me\n")
        self.assertEqual(
            (work2 / ".github/workflows/casc.yml").read_text(encoding="utf-8"),
            "name: caller\n",
        )

    def test_immutable_divergence_fails_before_commit(self):
        first = [
            {"path": ".aap-casc-engine/tenant-scaffold.yml", "policy": "immutable", "content": "marker: a\n"},
        ]
        self._run(branch="main", entries=first)
        work = self._clone_head("main")
        (work / ".aap-casc-engine/tenant-scaffold.yml").write_text("marker: tampered\n", encoding="utf-8")
        _git(["add", "-A"], cwd=work)
        _git(["commit", "-m", "tamper"], cwd=work)
        _git(["push"], cwd=work)
        before = self._log_count("main")
        with self.assertRaisesRegex(scaffold_commit.ScaffoldCommitError, "immutable path diverges"):
            self._run(
                branch="main",
                entries=[
                    {
                        "path": ".aap-casc-engine/tenant-scaffold.yml",
                        "policy": "immutable",
                        "content": "marker: a\n",
                    }
                ],
            )
        self.assertEqual(self._log_count("main"), before)

    def test_unsafe_paths_rejected(self):
        with self.assertRaisesRegex(scaffold_commit.ScaffoldCommitError, "absolute"):
            scaffold_commit.load_manifest(
                self._manifest([{"path": "/etc/passwd", "policy": "create_only", "content": "x"}])
            )
        with self.assertRaisesRegex(scaffold_commit.ScaffoldCommitError, "traversal"):
            scaffold_commit.load_manifest(
                self._manifest([{"path": "foo/../../etc", "policy": "create_only", "content": "x"}])
            )
        with self.assertRaisesRegex(scaffold_commit.ScaffoldCommitError, r"\.git"):
            scaffold_commit.load_manifest(
                self._manifest([{"path": ".git/config", "policy": "create_only", "content": "x"}])
            )
        with self.assertRaisesRegex(scaffold_commit.ScaffoldCommitError, "duplicate"):
            scaffold_commit.load_manifest(
                self._manifest(
                    [
                        {"path": "a.yml", "policy": "create_only", "content": "1"},
                        {"path": "a.yml", "policy": "create_only", "content": "2"},
                    ]
                )
            )
        with self.assertRaisesRegex(scaffold_commit.ScaffoldCommitError, "non-empty string"):
            scaffold_commit.load_manifest(
                self._manifest([{"path": 123, "policy": "create_only", "content": "x"}])
            )

    def test_symlink_traversal_rejected(self):
        # Seed a normal commit first so clone succeeds.
        self._run(
            branch="main",
            entries=[{"path": "README.md", "policy": "create_only", "content": "ok\n"}],
        )
        work = self._clone_head("main")
        outside = self.tmp / "outside.txt"
        outside.write_text("secret\n", encoding="utf-8")
        link = work / "linkdir"
        link.symlink_to(self.tmp)
        _git(["add", "-A"], cwd=work)
        # git may refuse to commit symlink depending on config; plant symlink after clone via patch
        _git(["commit", "-m", "with link", "--allow-empty"], cwd=work)
        _git(["push"], cwd=work)

        real_clone = scaffold_commit._run_git

        def clone_then_plant(args, **kwargs):
            result = real_clone(args, **kwargs)
            if args and args[0] == "clone" and result.returncode == 0:
                dest = Path(args[-1])
                planted = dest / "trap"
                planted.symlink_to(self.tmp)
            return result

        with mock.patch.object(scaffold_commit, "_run_git", side_effect=clone_then_plant):
            with mock.patch.dict(os.environ, self.env, clear=False):
                with self.assertRaisesRegex(
                    scaffold_commit.ScaffoldCommitError, "symlink traversal"
                ):
                    scaffold_commit.scaffold_commit(
                        repo_url=self.repo_url,
                        branch="main",
                        provider="github",
                        manifest_path=self._manifest(
                            [
                                {
                                    "path": "trap/outside.txt",
                                    "policy": "create_only",
                                    "content": "nope\n",
                                }
                            ]
                        ),
                        message="bad [skip ci]",
                    )

    def test_broken_symlink_rejected_before_external_write(self):
        """Broken symlink must not create the absent external target."""
        self._run(
            branch="main",
            entries=[{"path": "README.md", "policy": "create_only", "content": "ok\n"}],
        )
        external = self.tmp / "absent-external.txt"
        self.assertFalse(external.exists())

        real_clone = scaffold_commit._run_git

        def clone_then_plant_broken(args, **kwargs):
            result = real_clone(args, **kwargs)
            if args and args[0] == "clone" and result.returncode == 0:
                dest = Path(args[-1])
                (dest / "leaky.yml").symlink_to(external)
                self.assertTrue((dest / "leaky.yml").is_symlink())
                self.assertFalse((dest / "leaky.yml").exists())
            return result

        with mock.patch.object(scaffold_commit, "_run_git", side_effect=clone_then_plant_broken):
            with mock.patch.dict(os.environ, self.env, clear=False):
                with self.assertRaisesRegex(
                    scaffold_commit.ScaffoldCommitError, "symlink traversal"
                ):
                    scaffold_commit.scaffold_commit(
                        repo_url=self.repo_url,
                        branch="main",
                        provider="github",
                        manifest_path=self._manifest(
                            [
                                {
                                    "path": "leaky.yml",
                                    "policy": "create_only",
                                    "content": "pwned\n",
                                }
                            ]
                        ),
                        message="broken-symlink [skip ci]",
                    )
        self.assertFalse(external.exists())

    def test_askpass_answers_only_recognized_prompts(self):
        tmp = self.tmp / "askpass-home"
        tmp.mkdir()
        for provider, username in (
            ("github", "x-access-token"),
            ("gitlab", "oauth2"),
        ):
            script = scaffold_commit._write_askpass(tmp, provider)
            env = {**os.environ, "SCAFFOLD_SCM_TOKEN": "secret-token"}
            user = subprocess.run(
                [str(script), "Username for 'https://example.com':"],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(user.stdout.strip(), username)
            password = subprocess.run(
                [str(script), "Password for 'https://example.com':"],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(password.stdout.strip(), "secret-token")
            unexpected = subprocess.run(
                [str(script), "Enter passphrase for key:"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(unexpected.returncode, 0)
            self.assertNotIn("secret-token", unexpected.stdout)
            self.assertNotIn("secret-token", unexpected.stderr)

    def test_non_fast_forward_fails_closed(self):
        entries = [
            {"path": "README.md", "policy": "create_only", "content": "v1\n"},
            {"path": ".github/workflows/casc.yml", "policy": "converge", "content": "name: a\n"},
        ]
        self._run(branch="main", entries=entries)

        stale = self.tmp / "stale"
        _git(["clone", "--branch", "main", str(self.bare), str(stale)])

        # Advance remote concurrently.
        tip = self._clone_head("main")
        (tip / "other.txt").write_text("concurrent\n", encoding="utf-8")
        _git(["add", "other.txt"], cwd=tip)
        _git(["commit", "-m", "concurrent"], cwd=tip)
        _git(["push"], cwd=tip)

        real_run = scaffold_commit._run_git

        def fake_run(args, **kwargs):
            if args and args[0] == "clone":
                dest = Path(args[-1])
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(stale, dest)
                # Drop the copied .git remote tracking freshness by resetting remote URL
                _git(["remote", "set-url", "origin", str(self.bare)], cwd=dest)
                return subprocess.CompletedProcess(args, 0, "", "")
            return real_run(args, **kwargs)

        with mock.patch.object(scaffold_commit, "_run_git", side_effect=fake_run):
            with mock.patch.dict(os.environ, self.env, clear=False):
                with self.assertRaisesRegex(
                    scaffold_commit.ScaffoldCommitError, "non-fast-forward"
                ):
                    scaffold_commit.scaffold_commit(
                        repo_url=self.repo_url,
                        branch="main",
                        provider="github",
                        manifest_path=self._manifest(
                            [
                                {
                                    "path": "README.md",
                                    "policy": "create_only",
                                    "content": "v1\n",
                                },
                                {
                                    "path": ".github/workflows/casc.yml",
                                    "policy": "converge",
                                    "content": "name: b\n",
                                },
                            ]
                        ),
                        message="race [skip ci]",
                    )

    def test_already_converged_is_zero_commits(self):
        entries = [
            {"path": "README.md", "policy": "create_only", "content": "ok\n"},
            {"path": ".github/workflows/casc.yml", "policy": "converge", "content": "name: c\n"},
        ]
        self.assertEqual(self._run(branch="main", entries=entries), "committed")
        self.assertEqual(self._run(branch="main", entries=entries), "already_converged")
        self.assertEqual(self._log_count("main"), 1)

    def test_credentials_in_url_rejected(self):
        with mock.patch.dict(os.environ, self.env, clear=False):
            with self.assertRaisesRegex(scaffold_commit.ScaffoldCommitError, "credentials"):
                scaffold_commit.scaffold_commit(
                    repo_url="https://x-access-token:secret@github.com/org/repo.git",
                    branch="main",
                    provider="github",
                    manifest_path=self._manifest(
                        [{"path": "a.yml", "policy": "create_only", "content": "x"}]
                    ),
                    message="nope",
                )

    def test_verify_policy_semantics_match_plan(self):
        """Genesis verify parity contract encoded beside helper policies."""
        # converge/immutable → exact; create_only → existence only
        work = self.tmp / "verify-tree"
        work.mkdir()
        (work / "caller.yml").write_text("exact\n", encoding="utf-8")
        (work / "marker.yml").write_text("exact\n", encoding="utf-8")
        (work / "readme.md").write_text("customer\n", encoding="utf-8")
        entries = [
            {"path": "caller.yml", "policy": "converge", "content": "exact\n"},
            {"path": "marker.yml", "policy": "immutable", "content": "exact\n"},
            {"path": "readme.md", "policy": "create_only", "content": "engine\n"},
        ]
        scaffold_commit._revalidate(work, entries)
        # create_only mismatch is OK at verify time (existence only) — helper
        # revalidate for create_only only requires presence after apply.
        with self.assertRaises(scaffold_commit.ScaffoldCommitError):
            scaffold_commit._revalidate(
                work,
                [{"path": "caller.yml", "policy": "converge", "content": "other\n"}],
            )


class TopologyScaffoldContractTests(unittest.TestCase):
    """String/contract checks that survey tenants.yml stays outside scaffold batch."""

    def test_survey_update_remains_outside_helper_scope(self):
        helper = (ROOT / "scripts" / "pipeline" / "scaffold_commit.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("tenants.yml", helper)
        self.assertNotIn("survey", helper.lower())


if __name__ == "__main__":
    unittest.main()
