# Nonproduction Integration Validation

Run these checks against a nonproduction AAP and SCM org/group before any
production cutover. Do **not** use production launcher tokens or production
desired-state repositories.

## 0. Preconditions

- Engine commit under test is available to AAP project sync.
- Nonprod GitHub or GitLab org/group with ability to create private repos.
- Nonprod AAP with Genesis/Bootstrap/Dispatcher/Drift JTs.
- Dedicated execute-only launcher identities:
  - control launcher → Genesis/Bootstrap only
  - per-env dispatcher launcher → Dispatcher only
- Read-only `ENGINE_REPO_TOKEN` / `CONTROL_REPO_TOKEN`.
- Serialized Dispatcher JT: `allow_simultaneous=false`.

Record evidence for each step: pipeline URL, AAP job ID, and outcome.

## 1. SCM API scaffolding (create mode)

1. Run Genesis with `repo_mode: create`.
2. Expect dedicated `casc-platform-control` plus separate platform desired-state repo(s).
3. Confirm control repo has only `config.yml`, `tenants.yml`, `naming-rules.yml`,
   caller, and README.
4. Confirm platform desired-state repo has no control files.

## 2. Existing-repository mode

1. Pre-create empty control and platform repos.
2. Run Genesis with `repo_mode: existing`.
3. Confirm scaffold files are added without changing visibility/permissions.
4. Re-run Genesis and confirm existing customer files are preserved.

## 3. Greenfield bootstrap + bounded onboarding

1. Merge an active greenfield tenant into control `tenants.yml`.
2. Confirm Bootstrap scaffolds tenant repo(s) and writes foundation YAML to every
   mapped platform branch.
3. With `bootstrap_dispatch_fanout: true`, confirm Dispatcher launches:
   - `dispatch_scope=platform`
   - then `dispatch_scope=tenant` for only that `tenant_org_id`
   - never `full`
4. Confirm each launched job is polled to terminal success/failure.

## 4. Disabled fan-out continuation

1. Set `bootstrap_dispatch_fanout: false`.
2. Onboard another greenfield tenant.
3. Confirm Bootstrap succeeds and pipeline reports onboarding pending.
4. Confirm normal trigger does **not** run for that control event.
5. Run protected control manual operation:
   - GitHub: `workflow_dispatch` with `operation=onboarding_dispatch` and
     `tenant_org_id`
   - GitLab: web pipeline with `CASC_OPERATION=onboarding_dispatch` and
     `TENANT_ORG_ID`
6. Confirm bounded platform-then-tenant dispatch for that tenant only.

## 5. Brownfield bootstrap

1. Bootstrap a tenant with `onboarding_mode: brownfield`.
2. Confirm no foundation YAML and no Dispatcher/fan-out launch.
3. Merge one declared object into the lowest mapped branch.
4. Confirm only that tenant scope is applied.
5. Confirm undeclared AAP objects remain untouched.

## 6. Control bootstrap authorization

1. Attempt Bootstrap launch credentials that lack Execute on Bootstrap JT → fail.
2. Confirm platform/tenant callers do not receive `AAP_ENGINE_TOKEN`.
3. Confirm PR/MR pipelines validate only and make no AAP API calls.

## 7. Dispatcher scope and polling

1. Push a platform desired-state change → `dispatch_scope=platform`.
2. Push a tenant desired-state change → `dispatch_scope=tenant` for that tenant.
3. Force a long-running job or lower poll timeout and confirm timeout fails the
   pipeline (nonzero), not warn-and-succeed.
4. Confirm supplied `control_revision` is honored by Dispatcher/Drift.

## 8. Least-privilege launcher RBAC

1. Control launcher cannot edit JTs, read credentials, or launch Dispatcher.
2. Env dispatcher launcher cannot edit JTs or read attached credentials.
3. Privileged SCM-write and AAP-apply credentials exist only on AAP JTs, not in
   SCM secret stores.

## 9. Evidence package

Before production approval, archive:

- Genesis/Bootstrap/Dispatcher/Drift job IDs
- GitHub/GitLab pipeline URLs for create, existing, greenfield, brownfield,
  pending onboarding continuation, PR validation, and timeout failure
- Security preflight notes (no secret values)
- Confirmation that Phase 4 scoped concurrency was **not** enabled
