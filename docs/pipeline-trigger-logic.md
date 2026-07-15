# Pipeline Trigger Logic Reference

> Authoritative reference for the CI/CD trigger paths in `casc-validate-and-trigger.yml`.
> Source of truth: [`../.github/workflows/casc-validate-and-trigger.yml`](../.github/workflows/casc-validate-and-trigger.yml)

## Caller Trigger Events

The generated thin-caller workflow ([`templates/github-workflow-caller.yml.j2`](../templates/github-workflow-caller.yml.j2)) fires the reusable workflow on three events:

| Event | Scope | Notes |
|---|---|---|
| `push` | Any branch | Every push invokes the reusable workflow — required for multi-env branch mapping |
| `pull_request` | Any target branch | PRs invoke validation only. No AAP dispatch occurs on PR events. |
| `workflow_dispatch` | Manual | Allows manual trigger from the GitHub Actions UI |

GitLab parity: `pipeline-templates/gitlab/.gitlab-ci-template.yml` validates `merge_request_event` pipelines without a target-branch restriction. Bootstrap/fan-out remain push-only, and trigger does not dispatch from merge request pipelines.

## Reusable Workflow Jobs

The reusable workflow has **4 jobs**. The conditions prevent duplicate dispatcher launches — only one dispatch path executes per push:

| Job | Runs On Events | Gate Conditions | What It Does | AAP JT Triggered |
|---|---|---|---|---|
| **validate** | All (`push`, `pull_request`, `workflow_dispatch`) | Always runs | YAML structural validation, control file validation (`config.yml`, `tenants.yml`), naming convention checks, OPA policy compliance. Also detects if `tenants.yml` changed (push events only). | None |
| **bootstrap** | `push` only | All must be true: validate passed, repo is the platform home repo (`platform_scm_org/platform_home_repo`), `tenants.yml` was modified, commit message does NOT contain `[skip dispatch]` | Diffs old vs new `tenants.yml`, identifies added/modified active tenants, launches bootstrap JT for each tenant sequentially, polls each to completion | `jt-platform-bootstrap_tenant` (once per new/modified tenant) |
| **fanout** | `push` only (runs after bootstrap) | `bootstrap.result == 'success'` AND `has_tenants_change == 'true'` | Reads `config.yml` `env_branch_map`, checks `bootstrap_dispatch_fanout` flag. If flag is `true`, triggers dispatcher for **every** environment sequentially. If flag is `false`, exits successfully without dispatching. | `jt-platform-casc_dispatcher` (once per env in `env_branch_map`), or none if fanout disabled |
| **trigger** | `push`, `workflow_dispatch` | All must be true: validate passed, bootstrap was skipped OR bootstrap succeeded with no actionable tenants, fanout did NOT succeed, commit message does NOT contain `[skip dispatch]` | Fetches `config.yml`, maps current branch to env via `env_branch_map`, resolves per-env credentials, triggers dispatcher for that **single** env. If the branch doesn't match any `env_branch_map` entry, dispatch is skipped with a logged message. | `jt-platform-casc_dispatcher` (single env) |

## User GitOps Actions and Pipeline Paths

| User GitOps Action | Pipeline Path Triggered | What Happens | Gap / Note |
|---|---|---|---|
| Open/update PR to any branch | `validate` only | Runs YAML validation, control-file checks, naming checks, OPA checks | PR validation is intentionally broad; deploy dispatch remains controlled by `env_branch_map` on push/dispatch events. |
| Push to tenant repo branch mapped in `env_branch_map` | `validate` → `trigger` | Validates repo, resolves branch to target env, launches dispatcher once for that env | Standard Day-1+ GitOps path. |
| Push to tenant repo branch not in `env_branch_map` | `validate` → `trigger` resolves no env | Validation runs, dispatcher is skipped (logged, not silent) | Expected behavior. Could be confusing if user expected deploy. |
| Push to platform home repo without `tenants.yml` change | `validate` → `trigger` | Applies platform/global config for the env mapped to the branch | Platform repo changes follow the same branch-to-env model. |
| Push to platform home repo with `tenants.yml` new/modified active tenant (`bootstrap_dispatch_fanout=true`) | `validate` → `bootstrap` → `fanout` | Bootstrap scaffolds each tenant, then dispatcher fan-out runs for every env in `env_branch_map` | Day-0 tenant onboarding across all envs. Default behavior. |
| Push to platform home repo with `tenants.yml` new/modified active tenant (`bootstrap_dispatch_fanout=false`) | `validate` → `bootstrap` → `fanout` (no-op exit) | Bootstrap scaffolds tenants. Fanout exits successfully without dispatching. Because `trigger` suppresses when `fanout.result == 'success'`, **no dispatch occurs at all** after bootstrap. | **Known edge case.** Newly bootstrapped tenant repos get no initial config applied. Requires a separate manual dispatch or subsequent push to trigger. Design decision: is this intentional? |
| Push to platform home repo with `tenants.yml` changed but no actionable tenant (e.g., `inactive` status changes only) | `validate` → `bootstrap` → `trigger` | Bootstrap finds no action; normal single-env dispatcher runs | Avoids blocking normal config changes. |
| PR merged to a mapped branch | `push` event fires → `validate` → `trigger` | Merge creates a push event. Validates, resolves branch to env, dispatches. | Standard GitOps deploy path. PR validation ran on the PR event; the merge push re-validates and dispatches. |
| Manual `workflow_dispatch` on selected branch | `validate` → `trigger` | Resolves selected branch to env and dispatches that env | For manual retry if branch maps to env. |
| Push with `[skip dispatch]` in commit message | `validate` only | Validation runs; bootstrap and trigger both skip | For engine/scaffold commits that should not deploy. |
| PR to branch not in `env_branch_map` | `validate` only | Runs validation, no dispatch | Intentional. PR validation should catch YAML/policy issues before merge, even if the branch is not a deploy target. |

## Credential Cascades

### Dispatcher Credentials (`trigger` and `fanout` jobs) — 5 methods, tried in order:

| Priority | Method | Auth Type |
|---|---|---|
| 1 | `AAP_ENV_TARGETS_JSON[env].host` + `.token` | Bearer |
| 2 | `AAP_ENV_TARGETS_JSON[env].host` + `.username/.password` | Basic |
| 3 | `AAP_<ENV>_HOST` + `AAP_<ENV>_TOKEN` | Bearer |
| 4 | `AAP_<ENV>_HOST` + `AAP_<ENV>_USERNAME/PASSWORD` | Basic |
| 5 | `AAP_HOST` + `AAP_USERNAME/PASSWORD` (only if `AAP_FANOUT_ALLOW_BASIC=true`) | Basic (legacy) |

### Bootstrap Credentials (`bootstrap` job) — 3 methods, tried in order:

| Priority | Method | Auth Type |
|---|---|---|
| 1 | `AAP_ENGINE_HOST` + `AAP_ENGINE_TOKEN` | Bearer |
| 2 | `AAP_ENGINE_HOST` + `AAP_ENGINE_USERNAME/PASSWORD` | Basic |
| 3 | `AAP_HOST` + `AAP_USERNAME/PASSWORD` | Basic (legacy) |

## Safeguards

- **Concurrency group**: The `trigger` job uses `concurrency: casc-dispatcher-trigger` with `cancel-in-progress: false` to serialize dispatches and prevent overlapping applies.
- **Busy-wait**: Both `trigger` and `fanout` check for running/pending dispatcher jobs before launching (2 attempts, 15s wait between checks).
- **`allow_simultaneous` guard**: Both `trigger` and `fanout` verify the dispatcher JT has `allow_simultaneous=false` and abort if not, preventing parallel apply conflicts inside AAP.
- **`[skip dispatch]`**: Commit message flag to suppress all dispatch paths. Scoped to `push` events only — `workflow_dispatch` cannot be skipped this way (by design: manual triggers should always dispatch).

## Known Gaps and Design Decisions

### 1. PR validation scope (RESOLVED)

The generated caller no longer sets a `pull_request.branches` filter. PRs to any target branch trigger validation.

This avoids stale generated callers when `env_branch_map` changes later. Dispatch is still gated at runtime by branch-to-env resolution in the reusable workflow.

### 2. No dispatch after bootstrap when `bootstrap_dispatch_fanout=false`

When `bootstrap_dispatch_fanout=false` and bootstrap has actionable tenants, fanout exits as `success` (no-op). The `trigger` job suppresses when `fanout.result == 'success'`, so **no dispatcher runs at all**. Newly bootstrapped tenant repos get no initial config applied.

**Decision required:** Is this intentional (operator expects to manually dispatch afterward), or should `trigger` fall through when fanout is a no-op?

### 3. Pushes to unmapped branches still run full CI

The `push` trigger has no branch filter (required for multi-env support). Pushes to feature branches that aren't in `env_branch_map` still run the full validate + trigger pipeline. Trigger resolves no env and skips, but this creates CI noise.

**Mitigation:** This is by design — validation still provides value even on unmapped branches. Suppress noise by documenting expected behavior.
