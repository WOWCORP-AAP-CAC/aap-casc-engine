# AAP Multi-Tenant CasC Engine — Setup and Operations Guide

Canonical end-to-end guide for installing, configuring, securing, migrating, and
operating the engine on GitHub or GitLab.

This guide uses progressive disclosure:

- **Part A** — recommended quick start with defaults
- **Part B** — production topology, variables, repository modes, branch model
- **Part C** — brownfield adoption, migration, day-2 operations, security
- **Part D** — optional advanced scoped concurrency (not required for release)

Out of scope for this baseline: OIDC federation and external secret managers.
Production auth uses provider-native SCM secrets/variables plus least-privilege
AAP bearer tokens only.

---

## Part A: Start here and complete the default quick start

### A1. Purpose and outcomes

The engine provides a flat-YAML GitOps consumer interface for AAP desired state.
`infra.aap_configuration` remains the apply backend. Control metadata is
separated from desired state:

| Repository | Contents |
|---|---|
| Engine | Playbooks, schemas, reusable pipelines |
| `casc-platform-control` | `config.yml`, `tenants.yml`, `naming-rules.yml` only |
| Platform desired-state repo(s) | `base/<type>/` and `<env>/<type>/` |
| Tenant desired-state repo(s) | `base/<type>/` and `<env>/<type>/` |

Success looks like: Genesis creates control + platform repos, Bootstrap
onboards one greenfield tenant, a mapped-branch merge applies one YAML object
through Dispatcher, and PR validation never receives AAP credentials.

### A2. Quick-start prerequisites

1. AAP project for this engine with Genesis/Bootstrap/Dispatcher/Drift JTs.
2. SCM write credential attached only to Genesis/Bootstrap.
3. Privileged AAP apply credential attached only to Dispatcher/Drift.
4. Execute-only launcher token for Genesis/Bootstrap (`AAP_ENGINE_TOKEN`).
5. Per-environment execute-only Dispatcher launcher tokens in `AAP_ENV_TARGETS_JSON`.
6. Read-only `ENGINE_REPO_TOKEN` and `CONTROL_REPO_TOKEN`.

### A3. Default quick start

1. Run Genesis with defaults (`repo_mode: create`, combined platform repo,
   `dispatcher_concurrency: serialized`).
2. Confirm two repos exist: `casc-platform-control` and `casc-platform-global`.
3. Configure SCM secrets/variables using the matrix in Section C4.
4. Add one greenfield tenant to `tenants.yml` on `control_branch` and merge.
5. Confirm Bootstrap scaffolds the tenant repo and, when
   `bootstrap_dispatch_fanout: true`, runs bounded onboarding:
   `platform` then only that tenant.
6. Add one object under tenant `base/<type>/` and merge to the lowest mapped
   branch.
7. Confirm Dispatcher applies only that tenant scope.

If `bootstrap_dispatch_fanout: false`, Bootstrap finishes SCM/foundation writes
and leaves onboarding pending. Continue with protected control
`workflow_dispatch` / GitLab web pipeline:

- `operation` / `CASC_OPERATION` = `onboarding_dispatch`
- `tenant_org_id` / `TENANT_ORG_ID` = the tenant

---

## Part B: Build the production deployment

### B1. Architecture choices

| Choice | Recommendation |
|---|---|
| GitHub vs GitLab | Either; behavior is equivalent |
| Combined vs per-resource-type desired state | Combined for most teams |
| `create` vs `existing` | `existing` for pre-created governed repos |
| Greenfield vs brownfield | Greenfield for new orgs; brownfield for adoption |
| Serialized vs scoped concurrency | Serialized is the production default |

### B2. Control configuration (`config.yml`)

Required concepts:

- `control_repo` / `control_branch` — authoritative metadata
- `platform_repo_pattern` / `platform_repo` or `platform_repos`
- optional Genesis `platform_repo_names` overrides for per-resource-type layouts
  (folder→name map or ordered list); seeded into `platform_repos`
- ordered `env_branch_map` (low → high)
- `create_missing_env_branches` (`false` requires every mapped branch already present)
- `bootstrap_dispatch_fanout` (bounded greenfield onboarding only; never `full`)
- `dispatcher_concurrency: serialized`
- `job_templates.*` configurable names

GitLab note: when `control_scm_org` is a different group from `platform_scm_org`,
set `CONTROL_NAMESPACE_ID` (and `PLATFORM_NAMESPACE_ID`) to the matching numeric
group IDs.

### B3. Tenant registry (`tenants.yml`)

Fields:

- `org_id`, `team_name`, `team_lead`, `tenant_scm_org`
- `repo_pattern`, optional `repo_name` / `repo_names`
- `repo_mode: create|existing`
- `onboarding_mode: greenfield|brownfield`
- optional `dispatch_enabled` (tenant-scope pause only)
- optional `aap_organization`

After scaffolding starts, identity/repository fields are immutable through normal
Bootstrap. Use the tenant identity migration procedure instead.

### B4. Branch model

- Create missing desired-state branches high-to-low.
- Promote changes low-to-high.
- Control repo does not receive env branches from `env_branch_map`.
- Feature branches and PR/MR paths are validation-only.

### B5. Caller roles

| Role | Bootstrap | Normal dispatch |
|---|---|---|
| `control` | Yes on `tenants.yml` change | Never |
| `platform` | Never | Platform scope |
| `tenant` | Never | Tenant scope when enabled |

---

## Part C: Adopt existing AAP and operate safely

### C1. Brownfield adoption

1. Bootstrap with `onboarding_mode: brownfield` (SCM only; no foundation YAML; no fan-out).
2. Baseline one existing object on a feature branch.
3. Optional: run Drift Detection report mode as a protected operator action.
4. Merge into the lowest mapped branch to adopt only that declared object.
5. Promote through higher branches for higher environments.
6. Undeclared AAP objects remain ClickOps-managed.

### C2. Deletion safety

Absence from SCM is never deletion. Explicit deletion is accepted only when
`schemas/resource-types.yml` marks `deletion_supported: true` for that key.
See `docs/resource-deletion-capabilities.md`.

### C3. Migration

Use `scripts/migration/migrate_control_plane.py`:

```bash
# Legacy colocated platform-home -> dedicated control workspace
python3 scripts/migration/migrate_control_plane.py legacy-split \
  --source-repo /path/to/legacy-home \
  --output-dir /tmp/casc-migration \
  --platform-scm-org my-org

# Tenant identity/repository migration plan
python3 scripts/migration/migrate_control_plane.py tenant-identity \
  --tenants-file /path/to/tenants.yml \
  --from-org-id stores \
  --to-repo-name stores-aap-casc \
  --output-dir /tmp/tenant-migration
```

Migration is one-way. There is no steady-state colocated fallback.

### C4. Security and secrets

| Caller | Secrets/vars |
|---|---|
| Control | `AAP_ENGINE_HOST` (non-secret), `AAP_ENGINE_TOKEN`, `AAP_ENV_TARGETS_JSON`, `ENGINE_REPO_TOKEN`, `CONTROL_REPO_TOKEN` |
| Platform/Tenant | `AAP_ENV_TARGETS_JSON`, `ENGINE_REPO_TOKEN`, `CONTROL_REPO_TOKEN` |

Rules:

- Bearer tokens only.
- PR/MR validation receives no AAP launcher credentials and never dispatches.
- Assigned SCM secrets do **not** provide cryptographic caller identity.
- Native assigned-secret workflow authors are trusted launchers; protect
  workflow files and branches accordingly.

### C5. Day-2 operations

- Topology reconcile: change `env_branch_map` on control branch (adds only).
- Pause one tenant: `dispatch_enabled: false`.
- Continue pending greenfield onboarding: protected `onboarding_dispatch`.
- Drift report mode compares declared objects only; remediation uses Dispatcher
  `full` scope intentionally and exclusively.

---

## Part D: Advanced scale (optional)

Scoped concurrency (`dispatcher_concurrency: scoped`) is **not** required for
the production release. Enable only after Phases 1-3 pass and load tests prove
FIFO same-tenant / exclusive platform-full-onboarding behavior with
`allow_simultaneous=true`. Keep serialized rollback documented and tested.

---

## Reference appendices

### Secret / variable matrix

See Section C4 and `docs/pipeline-trigger-logic.md`.

### Nonproduction validation

See `docs/NONPRODUCTION_VALIDATION.md`.

### Related docs

- `docs/pipeline-trigger-logic.md`
- `docs/resource-deletion-capabilities.md`
- `README.md`
