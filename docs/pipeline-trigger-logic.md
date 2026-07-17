# Pipeline Trigger Logic Reference

> Authoritative CI/CD behavior for the GitHub reusable workflow, GitHub standalone
> template, and GitLab template. The control repository owns onboarding metadata;
> desired-state repositories own normal environment deployments.

## Caller Events

| Event | Result |
|---|---|
| Push to an environment-mapped branch | Validate, then dispatch that repository's scope to its mapped environment. |
| Push to an unmapped feature branch | Validate only; no AAP dispatch. |
| Pull request / merge request to any branch | Validate only. No AAP launcher credentials and no dispatch. |
| Manual dispatch on an environment-mapped branch | Validate, then dispatch the caller scope to its mapped environment. |
| Protected control manual `onboarding_dispatch` | Validate onboarding preflight, then bounded platform + that tenant dispatch. |
| Push with `[skip dispatch]` | Validate only. |

## Jobs and Ownership

| Job | Runs when | Outcome |
|---|---|---|
| `validate` | Every supported event | Validates YAML, control files, naming rules from the control revision, and policy. On a push, also detects an exact `tenants.yml` change. |
| `bootstrap` | Control-repo push to `control_branch` that changes `tenants.yml` | Launches the Bootstrap JT sequentially for added or changed active tenants, pinning `control_revision`. |
| `fanout` | Bootstrap found actionable greenfield tenants and `bootstrap_dispatch_fanout: true` | Launches dispatcher work in each configured environment: `platform` first, then only each newly onboarded `tenant:<org_id>`. Never `full`. |
| `onboarding_dispatch` | Protected control manual operation only | Continues a pending greenfield onboarding for one `tenant_org_id` with the same bounded platform-then-tenant sequence. |
| `trigger` | Normal mapped-branch push or manual run from a platform or tenant desired-state repo | Launches a single scoped dispatcher run and fails closed if polling times out. Control-repo pushes never take this path. |

## Onboarding Continuation Contract

Greenfield onboarding is intentionally bounded:

1. Bootstrap scaffolds tenant repos and writes foundation files on every mapped branch.
2. When `bootstrap_dispatch_fanout: true`, CI launches `platform` then only that `tenant:<org_id>` in each configured environment.
3. When `bootstrap_dispatch_fanout: false`, Bootstrap still completes SCM/foundation work, fan-out records that initial reconciliation remains pending, and the normal `trigger` path stays suppressed for that control event.
4. The only continuation for pending onboarding is protected control manual operation:
   - GitHub: `workflow_dispatch` with `operation=onboarding_dispatch` and `tenant_org_id`
   - GitLab: web pipeline with `CASC_OPERATION=onboarding_dispatch` and `TENANT_ORG_ID`
5. Preflight requires: control caller/repo/branch, explicit manual operation, active registered greenfield tenant, complete scaffold marker, foundation files on every mapped branch, current control revision, and all target credentials.
6. This path never accepts `dispatch_scope=full`.

Brownfield Bootstrap is SCM-only and never auto-fanouts.

## GitOps Action Matrix

| User action | Pipeline path | AAP effect |
|---|---|---|
| Push a desired-state change to a branch mapped in `env_branch_map` | `validate -> trigger` | Applies only that platform or tenant caller scope to its mapped AAP environment. |
| Push a desired-state change to a feature branch | `validate` | No apply. Merge through an environment branch to deploy. |
| Open a PR/MR | `validate` | No apply and no AAP deploy credentials. |
| Add or change an active greenfield tenant in control `tenants.yml` | `validate -> bootstrap -> fanout` | Bootstrap scaffolds the tenant. Fan-out applies platform then that tenant in every enabled environment when fan-out is enabled. |
| Add an active greenfield tenant with fan-out disabled | `validate -> bootstrap` + pending notice | No automatic dispatcher launch. Continue with protected `onboarding_dispatch`. |
| Change only an inactive tenant or make a non-actionable control edit | `validate -> bootstrap` | No tenant bootstrap action. The control repository itself never performs normal desired-state dispatch. |
| Run protected control `onboarding_dispatch` | `validate -> onboarding_dispatch` | Bounded platform then that tenant only. |
| Run manually on an environment branch from platform/tenant repo | `validate -> trigger` | Re-applies the selected caller scope to the branch-mapped environment. |

## Credentials

| Purpose | Required secret | Use |
|---|---|---|
| Per-environment dispatcher access | `AAP_ENV_TARGETS_JSON` | JSON mapping of environment name to `{host, token}`. This is the sole dispatcher credential model. |
| Bootstrap JT launcher access | `AAP_ENGINE_TOKEN` plus `aap_engine_host` / `AAP_ENGINE_HOST` | Execute-only bearer token for the engine AAP host. Passed only by the control caller. |
| Engine workflow/schema access | `ENGINE_REPO_TOKEN` | Allows a caller to fetch reusable workflow or validation assets when cross-repository access requires it. |
| Control metadata access | `CONTROL_REPO_TOKEN` | Read access to the control repository for `config.yml`, `tenants.yml`, and naming rules at the resolved control revision. |

No basic-auth, per-environment `AAP_<ENV>_*`, or global fallback paths are accepted at runtime. Parsed launcher tokens are masked in CI logs. Checkout steps use `persist-credentials: false` on GitHub. Secrets are step-scoped: PR/MR validation never receives deployment credentials.

## Control Revision Contract

- Pipelines resolve an authoritative `control_revision` (explicit pin or control-branch HEAD SHA).
- Bootstrap, Dispatcher, Drift, GitHub, and GitLab all consume the same revision semantics.
- Validation fetches `config.yml`, `tenants.yml`, and `naming-rules.yml` from that revision and fails closed if unavailable.
- Job Template names are resolved from control `config.yml` `job_templates.*` when present.

## Serialized Release Safeguards

- The Phase 1-3 baseline requires `allow_simultaneous=false` on every dispatcher JT.
- CI/CD launches are followed by job-completion polling where the caller needs completion semantics; timeout is a nonzero failure.
- There is no pre-launch global busy probe.
- GitHub serializes normal trigger launches with its workflow concurrency group. GitLab uses `resource_group` for the trigger path.
- Tenant-scoped concurrent dispatcher execution is a Phase 4 enhancement, not a baseline requirement.

## GitLab Parity

GitLab uses `rules:changes` plus an internal `CI_COMMIT_BEFORE_SHA` diff guard for the control-repo bootstrap path. It consumes the bounded tenant identities through the bootstrap dotenv artifact and applies the same token-only credential, control-revision, naming-rules, timeout-fail, and scope contract as GitHub.
