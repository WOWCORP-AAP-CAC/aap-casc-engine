# Nonproduction Integration Validation

Do not use production AAP or production SCM for these tests. Complete all
scenarios before release or demo cutover. Archive pipeline URLs, AAP job IDs,
control revisions, API responses, and negative-test output.

**Release-gate topology:** combined only. One control repository, one platform
desired-state repository, and one desired-state repository per tenant. Custom
combined `repo_name` / `platform_repo` values and `repo_mode=create|existing`
remain in scope. Per-resource layout fields (`platform_repo_pattern`,
`repo_pattern`, `platform_repo_names`, `repo_names`) were removed by
ROADMAP-006 and are rejected by the engine. Do not spend further validation
effort on per-resource scenarios.

## 0. Preconditions

- Engine project synced to the candidate commit.
- Genesis, Bootstrap, Dispatcher, and Drift Job Templates use configured names.
- Dispatcher has `allow_simultaneous=false`.
- GitHub or GitLab protected secrets are configured as documented.
- `casc-platform-control` and platform desired-state repo(s) are disposable.
- `env_branch_map` contains at least two distinct branches.

## 1. Genesis create mode

1. Run Genesis with `repo_mode=create`.
2. Confirm separate control and platform desired-state repositories.
3. Confirm control contains `config.yml` and `tenants.yml` but no active
   `naming-rules.yml`.
4. Confirm caller, examples, and folder scaffold on every mapped branch.
5. Confirm branches were created high-to-low.

Evidence: Genesis job ID, repository URLs, branch SHAs, tree listings.

## 2. Genesis existing mode and custom names

1. Pre-create combined control and platform repos with unrelated files.
2. Use `repo_mode=existing` and customer-selected combined names
   (`control_repo`, `platform_repo`).
3. Include one truly empty pre-created repository for each provider and confirm
   Genesis initializes it with the final README before branch creation. Repeat
   with an empty tenant repository and confirm Bootstrap uses the immutable
   marker as its first commit.
4. Confirm unrelated files and repository permissions remain unchanged on the
   pre-created combined repositories.
5. Negative-test missing mapped branches with branch creation disabled
   (`create_missing_env_branches=false`).

## 3. Greenfield default identity

Add a tenant with `tenant_id: stores`, no `aap_organization`, and a valid
`team_name`.

Confirm:

- effective AAP Organization is exactly `stores`, never `org-stores`;
- tenant routing remains keyed by `stores`;
- only Organization and Team foundation are generated;
- filenames are `stores.yml` in Organizations and Teams locations;
- identical bytes exist on every mapped branch;
- no user, RBAC, Galaxy, or default-EE foundation appears;
- survey fallback does not write a redundant `aap_organization: stores`.

Separately commit a password-free customer user declaration and confirm no
`change_me` default is applied. Confirm Bootstrap does not change individual
GitHub collaborators or GitLab project members.

## 4. Greenfield custom identity and custom repos

Use:

```yaml
tenant_id: stores
aap_organization: "WW Stores: Automation #1"
team_name: Stores Automation
```

Use a custom combined `repo_name`.

Confirm YAML punctuation round-trips unchanged, markers contain the exact
identity and resolved combined repository, Dispatcher and Drift both resolve
that custom name, and bounded onboarding launches `platform` then only
`tenant:stores`.

## 5. Optional naming policy

### Inactive

Run Greenfield Bootstrap with no `naming-rules.yml`, then with an empty file.
Both must succeed and log that naming policy is inactive.

### Active and compliant

Commit a customer Organization + Team naming policy to the control branch.
Bootstrap a matching tenant and confirm preflight success.

### Active and noncompliant

Bootstrap a nonmatching Organization or Team. Confirm failure before any repo,
branch, marker, caller, scaffold, or foundation mutation.

Negative-test malformed YAML, unknown resource type, malformed regex, raw
resource type, and non-scalar identity policy. Each must fail closed.

## 6. Brownfield gradual adoption

Register an existing LDAP/SAML-style AAP Organization with
`onboarding_mode: brownfield`, explicit `aap_organization`, and no `team_name`.

Confirm:

- SCM repositories are scaffolded;
- no Organization or Team foundation is generated;
- no onboarding fan-out occurs;
- existing Teams, users, RBAC, credentials, and other objects are untouched;
- supplying `team_name` or omitting `aap_organization` fails before mutation;
- one separately committed existing object can be adopted without affecting
  undeclared objects.

## 7. Lifecycle immutability

Before any marker:

- correct `tenant_id`, Organization, Team, namespace, and repository inputs;
- remove the pending request;
- confirm both operations are accepted only after previous and proposed repo
  sets are proven marker-free.

After a marker:

- change each immutable identity/topology field and confirm CI rejects before
  Bootstrap launch;
- remove the record and confirm rejection with migration guidance;
- change `status` or `dispatch_enabled` and confirm no Bootstrap rerun;
- onboard one Greenfield tenant with `dispatch_enabled=false`; confirm platform
  Organization/Team foundation applies while tenant-scope dispatch is skipped;
- set inactive and confirm its ID, Organization, and repositories remain reserved.

Create a matching partial scaffold and confirm idempotent resume. Create a
conflicting marker and confirm no managed write occurs.

Exercise survey fallback and inspect the registered records: Greenfield default
omits redundant `aap_organization`, a distinct Greenfield Organization persists,
and Brownfield retains its required explicit Organization with no `team_name`.

## 8. Greenfield onboarding (automatic bounded dispatch — current behavior)

After a Greenfield tenant is registered in `tenants.yml` and merged to the
control branch, the control pipeline automatically performs **bounded onboarding
dispatch**: platform scope first, then only the new tenant(s) across all
environments.

Confirm:

- SCM/foundation completion with markers on all mapped branches;
- automatic platform-then-tenant Dispatcher launches across all environments;
- if `dispatch_enabled=false`, scaffolding and foundation complete while
  tenant-scope dispatch is skipped;
- Brownfield receives no automatic onboarding dispatch.

### Historical (removed): disabled fan-out continuation

**ROADMAP-010 removed** the optional “defer automatic onboarding” config flag
and the protected manual continuation operation that resumed a single pending
Greenfield tenant. The engine now performs automatic bounded onboarding for all
Greenfield tenants after Bootstrap completes. This validation case is no longer
applicable.

## 9. Pipeline and authorization matrix

For GitHub and GitLab, verify:

- feature push: validate only;
- PR/MR to every mapped branch: validate only and no AAP deploy secrets;
- mapped platform push: platform scope only;
- mapped tenant push: matching tenant scope only;
- control `tenants.yml` push: lifecycle/bootstrap path with automatic bounded
  onboarding for Greenfield only;
- `[skip dispatch]`: skips Bootstrap, fan-out, and trigger;
- forced Dispatcher timeout: nonzero pipeline failure;
- missing `CONTROL_REPO_TOKEN`: fail closed;
- missing Bootstrap Execute permission: fail closed;
- platform/tenant callers do not receive `AAP_ENGINE_TOKEN`;
- launcher tokens cannot execute unrelated Job Templates.

Inspect the deployed AAP Job Templates and surveys: use `tenant_id`, expose
`aap_organization` and `team_name` as optional survey fields whose conditional
rules are enforced at runtime, and contain no legacy team-lead/password or
replacement collaborator input.

## 10. Drift and deletion safety

Drift is **report-only** (`identity_presence`, schema v2). Validate:

1. **Coverage** — compared keys are exactly Organizations, teams, credential
   types, projects, and inventories. Job templates and other catalog keys are
   `unsupported` until a proven adapter exists.
2. **Declared missing** — delete a declared project or inventory in the UI;
   Drift reports it under `missing_in_live` with an identity object (name +
   organization when required).
3. **Undeclared ignored** — platform JTs / engine project / other live objects
   not declared in desired state do **not** appear in the report (no
   `extra_in_live` / `unmanaged_live`).
4. **Exact query** — duplicate names across orgs are distinguished by
   `organization__name` filters; incomplete desired identity fails closed.
5. **API / RBAC** — unexpected HTTP 401/403 fails the job (not treated as
   missing). The Drift credential must read every declared compared object
   across Gateway and Controller for orgs present in desired state.
6. **Apply path** — restore declared state only by launching
   `jt-platform-casc_dispatcher`. Confirm Drift JT `extra_vars` have no
   `drift_mode` / `DRIFT_MODE`.

Confirm absence from YAML does not delete any object and unsupported explicit
deletion declarations fail validation.
Use an object with `state: absent`; verify both CI validation and a direct
Dispatcher run fail before `infra.aap_configuration.dispatch` is invoked.

## 11. Collection pin and declarative catalog

1. Discover the installed collection on the validated nonproduction
   project/EE (not from a developer laptop alone):

   ```bash
   ansible-galaxy collection list infra.aap_configuration
   ```

2. Confirm [`collections/requirements.yml`](../collections/requirements.yml)
   pins that **exact** version (`4.7.0` for the current baseline).
3. Confirm [`schemas/resource-types.yml`](../schemas/resource-types.yml)
   `collection.version` matches the pin.
4. Negative tests (CI structural validation must fail closed):
   - YAML using action/query keys such as `controller_launch_jobs` or
     `controller_bulk_hosts` is rejected as unsupported.
   - Non-mapping list items (bare strings/numbers) are rejected.
   - Duplicate keyed identities across the complete base or env layer are
     rejected (not only within a single file).
   - Atomic same-path files with different resource keys are rejected.
   - Atomic non-mapping list items are rejected by both CI and merge.
   - Unknown resource keys are rejected.
   - Atomic types (teams, projects, RBAC) validate and merge via path replace
     + concat; naming may still target `aap_teams.name`.
5. Confirm `examples/naming-rules.yml.sample` stays inert (fully commented) and
   covers every catalog type with `naming_supported: true`:

   ```bash
   python3 scripts/pipeline/generate_naming_sample.py --check
   ```

## 12. Final evidence package

Archive run-specific evidence **outside** this reusable engine repository
(engagement workspace or project vault). Do not commit customer/demo SCM org
names, AAP URLs, pipeline URLs, or job IDs into product docs.

Record:

- candidate engine SHA;
- pinned `infra.aap_configuration` version evidence (`ansible-galaxy collection list`);
- control revision for every run;
- GitHub and GitLab pipeline URLs;
- AAP job IDs and final statuses;
- generated marker and foundation files from every branch;
- custom repository-resolution proof;
- unsupported-key / action-key negative-test failures;
- naming-sample `--check` result;
- rollback decision and operator approval.
