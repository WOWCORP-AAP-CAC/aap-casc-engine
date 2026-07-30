#!/usr/bin/env python3
"""ROADMAP-004 atomic scaffold commit helper (ordinary Git).

Publishes one coherent commit per (repository, branch) operation, or no commit
when already converged. Credentials via environment + temporary GIT_ASKPASS —
never in URL, argv, .git/config, or logs.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath


POLICIES = frozenset({"converge", "create_only", "immutable"})
PROVIDER_USERNAMES = {
    "github": "x-access-token",
    "gitlab": "oauth2",
}
COMMIT_NAME = "AAP CasC Engine"
COMMIT_EMAIL = "aap-casc-engine@localhost"


class ScaffoldCommitError(Exception):
    """Fail-closed scaffold commit error."""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _require_token() -> str:
    token = _env("SCM_TOKEN")
    if not token:
        raise ScaffoldCommitError("SCM_TOKEN is required")
    return token


def _scrub(text: str) -> str:
    token = _env("SCM_TOKEN") or _env("SCAFFOLD_SCM_TOKEN")
    if token and token in text:
        return text.replace(token, "***")
    return text


def _validate_path(path: str) -> str:
    if not isinstance(path, str) or not path.strip():
        raise ScaffoldCommitError("manifest path must be a non-empty string")
    normalized = path.strip().replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("~"):
        raise ScaffoldCommitError(f"absolute paths are forbidden: {path}")
    parts = PurePosixPath(normalized).parts
    if not parts:
        raise ScaffoldCommitError(f"empty path after normalize: {path}")
    if ".." in parts:
        raise ScaffoldCommitError(f"path traversal is forbidden: {path}")
    if parts[0] == ".git" or ".git" in parts:
        raise ScaffoldCommitError(f".git paths are forbidden: {path}")
    return "/".join(parts)


def load_manifest(path: Path) -> list[dict[str, str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScaffoldCommitError(f"invalid manifest file: {exc}") from exc
    if not isinstance(raw, list):
        raise ScaffoldCommitError("manifest must be a JSON list")
    seen: set[str] = set()
    entries: list[dict[str, str]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ScaffoldCommitError(f"manifest[{index}] must be an object")
        try:
            file_path = _validate_path(item.get("path", ""))
        except ScaffoldCommitError as exc:
            raise ScaffoldCommitError(f"manifest[{index}] {exc}") from exc
        if file_path in seen:
            raise ScaffoldCommitError(f"duplicate manifest path: {file_path}")
        seen.add(file_path)
        policy = str(item.get("policy", "")).strip()
        if policy not in POLICIES:
            raise ScaffoldCommitError(
                f"manifest[{index}] policy must be one of {sorted(POLICIES)}"
            )
        if "content" not in item:
            raise ScaffoldCommitError(f"manifest[{index}] missing content")
        content = item["content"]
        if not isinstance(content, str):
            raise ScaffoldCommitError(f"manifest[{index}] content must be a string")
        entries.append({"path": file_path, "policy": policy, "content": content})
    if not entries:
        raise ScaffoldCommitError("manifest must contain at least one file")
    return entries


def _write_askpass(tmpdir: Path, provider: str) -> Path:
    username = PROVIDER_USERNAMES.get(provider)
    if not username:
        raise ScaffoldCommitError(
            f"unsupported provider '{provider}'; expected one of "
            f"{sorted(PROVIDER_USERNAMES)}"
        )
    script = tmpdir / "askpass.sh"
    # Token is read from SCAFFOLD_SCM_TOKEN at prompt time — never embedded here.
    # Only recognized username / password|passwd|token prompts are answered.
    body = f"""#!/bin/sh
case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
  *username*)
    printf '%s\\n' '{username}'
    ;;
  *password*|*passwd*|*token*)
    printf '%s\\n' "$SCAFFOLD_SCM_TOKEN"
    ;;
  *)
    printf '%s\\n' "unsupported git askpass prompt" >&2
    exit 1
    ;;
esac
"""
    script.write_text(body, encoding="utf-8")
    script.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return script


def _run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    merged.setdefault("GIT_TERMINAL_PROMPT", "0")
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        env=merged,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        stderr = _scrub((result.stderr or "").strip())
        raise ScaffoldCommitError(
            f"git {' '.join(args)} failed ({result.returncode}): {stderr}"
        )
    return result


def _reject_symlink_path(workdir: Path, rel: str) -> Path:
    dest = workdir / rel
    # is_symlink() is True for broken symlinks even when exists() is False.
    if dest.is_symlink():
        raise ScaffoldCommitError(f"symlink traversal is forbidden: {rel}")
    for parent in dest.parents:
        try:
            parent.relative_to(workdir)
        except ValueError:
            break
        if parent == workdir:
            break
        if parent.is_symlink():
            raise ScaffoldCommitError(
                f"symlink traversal is forbidden in parents of: {rel}"
            )
    return dest


def _read_file(path: Path) -> str | None:
    if path.is_symlink():
        raise ScaffoldCommitError(f"symlink traversal is forbidden: {path}")
    if not path.exists():
        return None
    if not path.is_file():
        raise ScaffoldCommitError(f"path is not a regular file: {path}")
    return path.read_text(encoding="utf-8")


def _apply_policies(workdir: Path, entries: list[dict[str, str]]) -> list[str]:
    """Return relative paths that must be written under policy rules."""
    to_write: list[str] = []
    for entry in entries:
        rel = entry["path"]
        dest = _reject_symlink_path(workdir, rel)
        existing = _read_file(dest) if dest.exists() else None
        policy = entry["policy"]
        intended = entry["content"]
        if policy == "converge":
            if existing != intended:
                to_write.append(rel)
        elif policy == "create_only":
            if existing is None:
                to_write.append(rel)
        elif policy == "immutable":
            if existing is None:
                to_write.append(rel)
            elif existing != intended:
                raise ScaffoldCommitError(
                    f"immutable path diverges from intended scaffold: {rel}"
                )
    return to_write


def _write_paths(workdir: Path, entries: list[dict[str, str]], paths: list[str]) -> None:
    by_path = {e["path"]: e["content"] for e in entries}
    for rel in paths:
        dest = _reject_symlink_path(workdir, rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(by_path[rel], encoding="utf-8")


def _revalidate(workdir: Path, entries: list[dict[str, str]]) -> None:
    """Re-check every manifest policy against the worktree before commit."""
    for entry in entries:
        rel = entry["path"]
        dest = _reject_symlink_path(workdir, rel)
        existing = _read_file(dest) if dest.exists() else None
        policy = entry["policy"]
        intended = entry["content"]
        if policy == "converge":
            if existing != intended:
                raise ScaffoldCommitError(
                    f"pre-commit revalidation failed for converge path: {rel}"
                )
        elif policy == "create_only":
            if existing is None:
                raise ScaffoldCommitError(
                    f"pre-commit revalidation failed; create_only missing: {rel}"
                )
        elif policy == "immutable":
            if existing != intended:
                raise ScaffoldCommitError(
                    f"pre-commit revalidation failed for immutable path: {rel}"
                )


def _repo_url_has_embedded_credentials(repo_url: str) -> bool:
    if "://" not in repo_url:
        return False
    authority = repo_url.split("://", 1)[1].split("/", 1)[0]
    return "@" in authority


def _remote_is_empty(repo_url: str, git_env: dict[str, str]) -> bool:
    ls = _run_git(["ls-remote", repo_url], env=git_env, check=False)
    if ls.returncode != 0:
        raise ScaffoldCommitError(
            f"unable to probe remote refs: {_scrub((ls.stderr or '').strip())}"
        )
    return not ls.stdout.strip()


def scaffold_commit(
    *,
    repo_url: str,
    branch: str,
    provider: str,
    manifest_path: Path,
    message: str,
) -> str:
    if not repo_url.strip():
        raise ScaffoldCommitError("repo_url is required")
    if not branch.strip():
        raise ScaffoldCommitError("branch is required")
    if not message.strip():
        raise ScaffoldCommitError("commit message is required")
    if _repo_url_has_embedded_credentials(repo_url):
        raise ScaffoldCommitError("credentials in repo_url are forbidden")

    token = _require_token()
    entries = load_manifest(manifest_path)
    tmpdir = Path(tempfile.mkdtemp(prefix="scaffold-commit-"))
    askpass = _write_askpass(tmpdir, provider)
    workdir = tmpdir / "work"
    git_env = {
        "GIT_ASKPASS": str(askpass),
        "GIT_TERMINAL_PROMPT": "0",
        "SCAFFOLD_SCM_TOKEN": token,
    }

    try:
        empty_first_commit = False
        clone = _run_git(
            [
                "clone",
                "--branch",
                branch,
                "--single-branch",
                "--depth",
                "1",
                repo_url,
                str(workdir),
            ],
            env=git_env,
            check=False,
        )
        if clone.returncode != 0:
            stderr = (clone.stderr or "").lower()
            if any(
                marker in stderr
                for marker in ("authentication failed", "could not read username", "403", "401", "denied")
            ):
                raise ScaffoldCommitError(
                    f"clone authentication failed: {_scrub((clone.stderr or '').strip())}"
                )
            if workdir.exists():
                shutil.rmtree(workdir, ignore_errors=True)
            if not _remote_is_empty(repo_url, git_env):
                raise ScaffoldCommitError(
                    f"branch '{branch}' not found on non-empty remote; "
                    "create the branch via REST before scaffolding"
                )
            empty_first_commit = True
            workdir.mkdir(parents=True, exist_ok=True)
            _run_git(["init"], cwd=workdir, env=git_env)
            _run_git(
                ["checkout", "-B", branch],
                cwd=workdir,
                env=git_env,
            )
            _run_git(["remote", "add", "origin", repo_url], cwd=workdir, env=git_env)

        to_write = _apply_policies(workdir, entries)
        if not to_write:
            _revalidate(workdir, entries)
            return "already_converged"

        _write_paths(workdir, entries, to_write)
        _revalidate(workdir, entries)

        _run_git(["add", "--", *to_write], cwd=workdir, env=git_env)
        status = _run_git(["status", "--porcelain"], cwd=workdir, env=git_env)
        if not status.stdout.strip():
            return "already_converged"

        commit_env = {
            **git_env,
            "GIT_AUTHOR_NAME": COMMIT_NAME,
            "GIT_AUTHOR_EMAIL": COMMIT_EMAIL,
            "GIT_COMMITTER_NAME": COMMIT_NAME,
            "GIT_COMMITTER_EMAIL": COMMIT_EMAIL,
        }
        _run_git(["commit", "-m", message], cwd=workdir, env=commit_env)

        push = _run_git(
            ["push", "--set-upstream", "origin", f"HEAD:refs/heads/{branch}"],
            cwd=workdir,
            env=git_env,
            check=False,
        )
        if push.returncode != 0:
            err = (push.stderr or "").lower()
            if (
                "non-fast-forward" in err
                or "fetch first" in err
                or ("rejected" in err and "failed to push" in err)
            ):
                raise ScaffoldCommitError(
                    "non-fast-forward push rejected; remote advanced concurrently"
                )
            raise ScaffoldCommitError(
                f"git push failed: {_scrub((push.stderr or '').strip())}"
            )
        return "committed"
    finally:
        os.environ.pop("SCAFFOLD_SCM_TOKEN", None)
        shutil.rmtree(tmpdir, ignore_errors=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-url", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--provider", required=True, choices=sorted(PROVIDER_USERNAMES))
    parser.add_argument("--manifest", required=True, help="Path to mode-0600 JSON manifest")
    parser.add_argument("--message", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = scaffold_commit(
            repo_url=args.repo_url,
            branch=args.branch,
            provider=args.provider,
            manifest_path=Path(args.manifest),
            message=args.message,
        )
    except ScaffoldCommitError as exc:
        print(f"ERROR: {_scrub(str(exc))}", file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
