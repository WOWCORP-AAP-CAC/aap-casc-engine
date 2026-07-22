# Pipeline Trigger Logic Reference

Authoritative behavior for the GitHub reusable workflow, GitHub standalone
workflow, and GitLab template.

## User action matrix

| User action | Pipeline path | AAP effect |
|---|---|---|
| Push to an environment-mapped platform branch | `validate -> trigger` | Applies platform scope to that environment |
| Push to an environment-mapped tenant branch | `validate -> trigger` | Resolves repository to `tenant_id` and applies only that tenant scope |
| Push to an unmapped feature branch | `validate` | None |
| Pull request / merge request to any target branch | `validate` | None; deploy credentials are not exposed |
| Control-branch push adding/correcting active Greenfield tenant | `validate -> bootstrap -> fanout` | SCM scaffold, two-file foundation, then bounded platform + changed tenant when enabled |
| Control-branch push adding Brownfield tenant | `validate -> bootstrap` | SCM scaffold only; no foundation and no onboarding dispatch |
| Control change to mutable `status` / `dispatch_enabled` only | `validate` | No Bootstrap action |
| Protected control `onboarding_dispatch` | `validate -> onboarding_dispatch` | Preflight, then bounded platform + one Greenfield tenant |
| Push containing `[skip dispatch]` | `validate` | None |
| Manual platform/tenant run on a mapped branch | `validate -> trigger` | Reapplies caller scope to mapped environment |

## Jobs

| Job | Gate | Responsibility |
|---|---|---|
| `validate` | Every supported event | Structural YAML, control registry, optional naming policy, and OPA checks |
| `bootstrap` | Control caller, control branch, exact `tenants.yml` change, actionable lifecycle diff | Launches Bootstrap JT sequentially for actionable tenants |
| `fanout` | Successful actionable Greenfield Bootstrap and fan-out enabled | Runs `platform`, then only changed `tenant:<tenant_id>` in every environment; never `full` |
| `onboarding_dispatch` | Protected manual control operation | Resumes one pending Greenfield tenant after complete marker/foundation preflight |
| `trigger` | Mapped platform/tenant push or manual run | Launches one scoped Dispatcher and polls to terminal |

## Tenant lifecycle diff

The three pipeline implementations use `scripts/pipeline/casc_runtime.py` for the
same behavior:

- Validate all tenant IDs, exact AAP Organization bindings, and repository ownership.
- Resolve the scalar combined tenant `repository` from the tenant record (`repo_name` override or default).
- Inspect markers across every mapped branch.
- Allow identity/topology corrections or removal before any marker exists.
- Reject identity/topology changes or removal after any marker exists.
- Do not rerun Bootstrap for `status` or `dispatch_enabled` changes alone.
- During Greenfield onboarding, `dispatch_enabled=false` still allows the
  platform-owned Organization/Team foundation apply but suppresses tenant scope.
- Greenfield requires `team_name`; Brownfield requires `aap_organization` and rejects `team_name`.

## Optional naming policy

Control `config.yml` and `tenants.yml` are mandatory. Root
`naming-rules.yml` is optional:

- missing or empty: naming policy inactive;
- present: validate its schema, then enforce only listed resource types;
- no engine policy is copied as a fallback;
- `naming-rules.yml` itself is excluded from desired-state scanning.

Bootstrap validates the exact rendered Greenfield Organization and Team before
SCM mutation. Dispatcher and Drift have no naming-policy runtime dependency.

## Credentials

| Secret/variable | Use |
|---|---|
| `AAP_ENV_TARGETS_JSON` | Environment -> `{host, token}` execute-only Dispatcher access |
| `AAP_ENGINE_TOKEN` and engine host | Control-only Bootstrap JT launch |
| `ENGINE_REPO_TOKEN` | Private engine workflow/helper access |
| `CONTROL_REPO_TOKEN` | Pinned control metadata and marker reads |

Runtime deployment credentials are bearer-token only. GitHub checkout uses
`persist-credentials: false`, parsed tokens are masked, and pull/merge-request
validation does not receive AAP deployment credentials.

## Control revision

Validation resolves an explicit `control_revision` or control-branch HEAD. The
same revision is forwarded to Bootstrap, bounded onboarding, Dispatcher, and
Drift. Missing required control metadata or a pin mismatch fails closed.

## Branch behavior

- `env_branch_map` values are unique and may use any valid branch names.
- Generated callers validate pull/merge requests to every target branch.
- Feature-branch pushes validate only because the branch does not map to an environment.
- Mapped-branch pushes dispatch only the caller's platform or tenant scope.
- Genesis and Bootstrap converge callers and required scaffold on every mapped branch.

## Serialized baseline

The production baseline requires Dispatcher `allow_simultaneous=false`.
Launches are polled to terminal and timeout is failure. GitHub normal trigger
uses workflow concurrency; GitLab normal trigger uses `resource_group`.
Tenant-scoped concurrent dispatch remains a separate roadmap enhancement.

## GitLab parity

GitLab uses `rules:changes` plus an internal `CI_COMMIT_BEFORE_SHA` exact diff
guard for `tenants.yml`. Dotenv artifacts carry actionable tenant IDs and
trigger suppression. Scope, lifecycle, optional naming, token, polling, and
protected onboarding behavior match GitHub.
