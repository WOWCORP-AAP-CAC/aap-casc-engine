# AAP Multi-Tenant CasC Engine - Setup and Operations Guide

This is the canonical guide for installing, configuring, validating, and
operating the engine with GitHub or GitLab. It uses progressive disclosure:

- **Part A:** shortest supported setup.
- **Part B:** complete configuration reference.
- **Part C:** adoption, security, and day-2 operations.
- **Part D:** validation, limitations, and troubleshooting.

OIDC federation and external secret managers are intentionally out of scope.
The baseline uses provider-native protected secrets/variables and AAP bearer
tokens without external dependencies.

## Part A - Recommended setup

### A1. Target topology

| Repository class | Contents |
|---|---|
| Engine | Playbooks, helpers, schemas, templates, reusable pipelines |
| Control | Mandatory `config.yml`, mandatory `tenants.yml`, optional `naming-rules.yml` |
| Platform desired state | Global/shared AAP resource YAML |
| Tenant desired state | One tenant's AAP resource YAML |

Use one control repo, one combined platform repo for the simplest deployment,
and one combined repo per tenant. Per-resource-type layouts remain available
when access boundaries require them.

### A2. AAP objects

Create one AAP Project for this engine and four Job Templates:

| Purpose | Default name | Playbook | Credentials |
|---|---|---|---|
| Genesis | `jt-platform-genesis` | `genesis.yml` | SCM write credential |
| Bootstrap | `jt-platform-bootstrap_tenant` | `bootstrap.yml` | SCM write credential |
| Dispatcher | `jt-platform-casc_dispatcher` | `site.yml` | SCM read + target AAP connection |
| Drift | `jt-platform-drift_detection` | `drift-detect.yml` | SCM read + target AAP connection |

The names are defaults, not requirements. Set `job_templates.*` in control
`config.yml` and the matching Genesis inputs when customers use other names.
Dispatcher must use `allow_simultaneous=false` for the serialized baseline.

Create the engine Project from the `aap-casc-engine` repository, then configure
the Job Templates as follows. Keep privileged SCM write/apply credentials in
AAP; pipelines receive only execute-level launcher tokens.

| Job Template | Fixed extra vars | Launch-time inputs | Required AAP credentials |
|---|---|---|---|
| Genesis | Customer SCM URL/provider defaults | Platform/control namespaces, repo layout/names, branch map, `repo_mode` | SCM credential able to create or update the selected repositories |
| Bootstrap | Control repo coordinates and engine repo | Tenant fields below; `control_revision` is supplied by CI | SCM credential able to scaffold control, platform, and requested tenant repos |
| Dispatcher | Control repo coordinates | `target_env`, `dispatch_scope`, `tenant_id` or `triggered_repo`, `control_revision` | SCM read credential plus target AAP connection credential |
| Drift | Control repo coordinates | `target_env`, `drift_mode`, optional `control_revision` | SCM read credential plus target AAP connection credential |

#### Job Template to control-plane binding

Bootstrap, Dispatcher, and Drift Job Templates are intentionally bound to **one**
trusted control plane through their fixed AAP extra vars
(`control_scm_org`, `control_repo`, `control_branch`, and related coordinates).

This is a trust boundary, not a missing CI feature:

- Caller workflows (including tenant-controlled pipelines) must **not** be able to
  override `control_repo` / control coordinates on privileged Job Template launches.
- CI may supply tenant fields and a pinned `control_revision` for the bound control
  repo; it must not redirect privileged execution to different control content.
- One Bootstrap/Dispatcher/Drift JT set serves one control plane.
- Parallel independent control planes require separately configured JT sets (or an
  intentional operator retarget of the JT fixed vars for a controlled validation
  window), not dynamic forwarding from an editable caller workflow.

Recommended Bootstrap survey schema:

| Field | Survey required? | Runtime rule |
|---|---:|---|
| `tenant_id` | Yes | Lowercase safe engine key, maximum 64 characters |
| `onboarding_mode` | Yes | `greenfield` or `brownfield` |
| `aap_organization` | No | Greenfield defaults to `tenant_id`; Brownfield requires it |
| `team_name` | No | Greenfield requires it; Brownfield rejects it |
| `tenant_scm_org` | Yes for an unregistered request | Registered Git record is authoritative |
| `repo_pattern`, `repo_mode`, `repo_visibility` | No | Shared runtime defaults apply when omitted |
| `repo_name`, `repo_names` | No | Mutually exclusive layout-specific overrides |
| `tenant_scm_namespace_id` | No | Required only for GitLab project creation |

For a tenant already registered in control `tenants.yml`, CI should launch
Bootstrap with `tenant_id` and pinned `control_revision`; conflicting survey
values fail rather than overriding Git.

Bootstrap survey fields `aap_organization` and `team_name` must be optional at
survey-schema level. Shared runtime validation enforces the conditional rules.
Do not configure team-lead, user-password, or individual SCM collaborator
questions; those identities and access grants are outside Bootstrap.

### A3. Configure protected secrets

#### GitHub

| Secret/variable | Repositories | Minimum purpose |
|---|---|---|
| `CONTROL_REPO_TOKEN` | Control, platform, tenant | Read control metadata and lifecycle markers |
| `ENGINE_REPO_TOKEN` | Control, platform, tenant | Read private engine assets when required |
| `AAP_ENV_TARGETS_JSON` | Control, platform, tenant | Execute-only Dispatcher token per AAP environment |
| `AAP_ENGINE_TOKEN` | Control only | Execute Bootstrap JT |
| `AAP_ENGINE_HOST` variable | Control only | AAP API host for Bootstrap launch |

`AAP_ENV_TARGETS_JSON` format:

```json
{"dev":{"host":"https://aap-dev.example","token":"..."},"prd":{"host":"https://aap-prd.example","token":"..."}}
```

#### GitLab

Use protected/masked CI/CD variables with the same names. `SCM_TOKEN` is
injected into Genesis/Bootstrap AAP jobs by an AAP credential. GitLab namespace
creation also requires the appropriate numeric namespace ID.

Use separate least-privilege tokens where practical. Do not expose deploy
secrets to pull/merge-request validation. The supplied pipelines scope secrets
to deployment steps and mask parsed AAP tokens.

### A4. Run Genesis

Recommended starting inputs:

```yaml
scm_base_url: https://github.com
platform_scm_org: ww-platform
control_scm_org: ww-platform
control_repo: casc-platform-control
control_branch: main
platform_repo_pattern: combined
platform_repo: casc-platform-global
repo_mode: existing
repo_visibility: private
env_branch_map:
  dev: develop
  tst: release/tst
  prd: main
```

Use `repo_mode=existing` when customer governance pre-creates repositories.
The repositories may be empty when `create_missing_env_branches=true`; the engine
initializes them with its final README or immutable tenant marker before adding
the high-to-low branch topology. Genesis preserves unrelated files and converges
only engine-managed control, caller, folder, and example scaffold. Use
`repo_mode=create` when the supplied SCM token is allowed to create repositories.

### A5. Bootstrap one tenant

Greenfield request:

```yaml
---
tenants:
  - tenant_id: stores
    aap_organization: WW Stores Automation
    team_name: Stores Automation
    tenant_scm_org: ww-tenants
    repo_pattern: combined
    repo_name: stores-aap-casc
    repo_mode: existing
    onboarding_mode: greenfield
    status: active
```

Bootstrap writes the Organization and Team foundation to every mapped platform
branch and scaffolds every mapped tenant branch. It does not create users, RBAC
assignments, Galaxy associations, execution-environment associations, or SCM
memberships.

If `bootstrap_dispatch_fanout=true`, onboarding dispatches platform scope first,
then only the new tenant in each environment. It never launches `full`. If
false, complete the pending onboarding through protected control
`onboarding_dispatch` for that `tenant_id`.

When `dispatch_enabled=false`, Bootstrap still scaffolds the tenant and applies
the platform-owned Organization/Team foundation, but skips tenant-scope apply.
Re-enable it and use a later mapped-branch merge or protected manual dispatch to
apply tenant desired state.

## Part B - Configuration reference

### B1. Genesis inputs

| Input | Default | Meaning |
|---|---|---|
| `scm_base_url` | `https://github.com` | SCM base URL |
| `scm_provider` / `SCM_PROVIDER` | auto-detected | `github` or `gitlab` |
| `platform_scm_org` | required | Platform desired-state namespace |
| `control_scm_org` | platform namespace | Control namespace |
| `control_repo` | `casc-platform-control` | Control repository name |
| `control_branch` | `main` | Control branch |
| `platform_repo_pattern` | `combined` | `combined` or `per-resource-type` |
| `platform_repo` | `casc-platform-global` | Combined platform repo name |
| `platform_repo_names` | `{}` | Per-resource folder-to-repo overrides |
| `repo_mode` | `create` | `create` or `existing` |
| `repo_visibility` | `private` | `private` or `public` |
| `create_missing_env_branches` | `true` | Create missing mapped branches; otherwise require all |
| `bootstrap_dispatch_fanout` | `true` | Enable bounded Greenfield onboarding dispatch |
| `env_branch_map` | deployment input | Ordered low-to-high environment/branch map |
| `*_jt_name` | documented defaults | Customer Job Template names |
| `platform_namespace_id` | GitLab create mode | Numeric platform group ID |
| `control_namespace_id` | platform group ID | Numeric control group ID for control repo creation when split |

`platform_repo_names` accepts only a mapping. Unknown folders, blank names, and
duplicate effective repository names fail before mutation.

### B2. Control `config.yml`

Genesis seeds the authoritative runtime configuration:

```yaml
---
scm_provider: github
control_scm_org: ww-platform
control_repo: casc-platform-control
control_branch: main
platform_scm_org: ww-platform
platform_repo_pattern: combined
platform_repo: casc-platform-global
repo_mode: existing
create_missing_env_branches: true
bootstrap_dispatch_fanout: true
dispatcher_concurrency: serialized
job_templates:
  genesis: jt-platform-genesis
  bootstrap: jt-platform-bootstrap_tenant
  dispatcher: jt-platform-casc_dispatcher
  drift_detection: jt-platform-drift_detection
env_branch_map:
  dev: develop
  tst: release/tst
  prd: main
```

For per-resource-type platform layout, `platform_repos` is a list of explicit
`resource_type`/`name` records. Bootstrap fails closed if the required
Organizations or Teams repository is missing; it never guesses a hardcoded name.

### B3. Tenant record

| Field | Greenfield | Brownfield | Default/notes |
|---|---|---|---|
| `tenant_id` | required | required | Safe stable engine key, max 64 characters |
| `aap_organization` | optional | required | Exact AAP Organization; Greenfield defaults to `tenant_id` |
| `team_name` | required | forbidden | Exact Team generated only for Greenfield |
| `tenant_scm_org` | required | required | Tenant SCM organization/group |
| `tenant_scm_namespace_id` | GitLab when needed | GitLab when needed | Numeric group ID for project creation |
| `repo_pattern` | optional | optional | `combined` default or `per-resource-type` |
| `repo_name` | combined only | combined only | Optional combined repo override |
| `repo_names` | per-resource only | per-resource only | Optional partial folder-to-repo mapping |
| `repo_mode` | optional | optional | `create` default or `existing` |
| `repo_visibility` | optional | optional | `private` default |
| `onboarding_mode` | required intent | required intent | `greenfield` or `brownfield` |
| `status` | optional | optional | `active` default or `inactive` |
| `dispatch_enabled` | optional | optional | `true` default |

Do not store derived `repositories`, `repo_by_folder`, or `tenant_repos` fields.
The shared resolver derives them consistently in Bootstrap, Dispatcher, Drift,
and CI.

### B4. Repository-name overrides

Combined tenant:

```yaml
repo_pattern: combined
repo_name: stores-aap-casc
```

Per-resource tenant:

```yaml
repo_pattern: per-resource-type
repo_names:
  projects: stores-projects-casc
  inventories: stores-inventories-casc
```

Partial maps retain deterministic defaults for omitted folders. Ordered lists
are rejected to prevent positional misrouting.

### B5. Branch and pipeline model

`env_branch_map` is ordered low to high. Missing branches are created from the
highest branch toward lower branches; changes are promoted from low to high.
The caller exists on every mapped branch, including pre-existing branches.

| User action | Result |
|---|---|
| Push to mapped desired-state branch | Validate and dispatch only caller scope to mapped environment |
| Push to feature branch | Validate only |
| Pull/merge request to any branch | Validate only; no deploy credentials |
| Control push changing `tenants.yml` | Validate, lifecycle diff, sequential Bootstrap, bounded fan-out when enabled |
| Protected `onboarding_dispatch` | Resume one pending Greenfield tenant only |
| `[skip dispatch]` commit | Validation only |

See [Pipeline Trigger Logic](pipeline-trigger-logic.md).

### B6. Dispatcher and Drift inputs

| Playbook | Important inputs |
|---|---|
| `site.yml` | `target_env`, `dispatch_scope=platform|tenant|full`, optional `tenant_id`, full `triggered_repo` SCM path, `control_revision` |
| `drift-detect.yml` | `target_env`, `drift_mode=report|remediate`, optional `control_revision` |

Normal platform and tenant pipelines never request `full`. Scheduled protected
operations may use it. Tenant repository mapping remains independent of the AAP
Organization display name.

## Part C - Adoption and operations

### C1. Brownfield gradual adoption

Brownfield enables CasC to coexist with ClickOps:

1. Register the exact existing `aap_organization` with no `team_name`.
2. Bootstrap SCM only. No AAP foundation or onboarding fan-out occurs.
3. Baseline one existing object into YAML on a feature branch.
4. Validate in a pull/merge request.
5. Merge to the lowest mapped branch, then promote upward.
6. Repeat object by object.

Only declared objects become managed through CasC. Unknown existing objects are
not deleted by absence. Naming can remain unchanged: omit rules for those
resource types or supply a policy that accepts existing LDAP/SAML identities.

### C2. Optional naming policy

The only active policy path is control-root `naming-rules.yml` at the pinned
control revision.

| File state | Behavior |
|---|---|
| Missing | Naming policy inactive |
| Empty YAML | Naming policy inactive |
| Contains resource rules | Enforce only those resource types |

Day-0 activation:

1. Start with `examples/naming-rules.yml.sample` or the explicitly optional
   type-prefixed sample.
2. Commit it as control-root `naming-rules.yml`.
3. Bootstrap with Organization and Team values that satisfy the policy.
4. Bootstrap validates the exact rendered bytes before any SCM mutation.

Policy format:

```yaml
---
aap_organizations:
  pattern: '^WW .+ Automation$'
  example: WW Stores Automation
aap_teams:
  pattern: '^.+ Automation$'
  example: Stores Automation
```

Rules use the `identity_field` from `schemas/resource-types.yml`. Raw resource
values and non-scalar identities cannot have naming rules. A policy never
renames an object and never validates filenames.

Local validation example:

```bash
python3 schemas/validate_naming.py \
  --config-dir /path/to/desired-state \
  --rules /path/to/casc-platform-control/naming-rules.yml \
  --resource-types schemas/resource-types.yml \
  --allowed-keys roles/process_casc_config/defaults/main.yml
```

### C3. Scaffold lifecycle

The first matching `.aap-casc-engine/tenant-scaffold.yml` marker is the lifecycle
boundary.

| Field group | Before any marker | After any marker |
|---|---|---|
| `tenant_id` | Request may be corrected or removed | Immutable |
| Effective `aap_organization` | May be corrected | Immutable |
| SCM namespace, repo mode/pattern/names, onboarding mode, visibility | May be corrected | Immutable |
| Greenfield `team_name` | May be corrected | Immutable Bootstrap input |
| `status`, `dispatch_enabled` | Mutable | Mutable |

Inactive records continue to reserve tenant ID, AAP Organization, and repository
ownership. Identity/topology change or retirement requires a deliberate future
migration; Bootstrap does not rename objects in place.

### C4. Repository permissions

Bootstrap does not manage individual SCM collaborators. Grant access through the
customer's normal organization, group, repository, and approval process.

- `repo_mode=create` requires namespace-level project creation permission and a
  normal post-create access process.
- `repo_mode=existing` preserves permissions and unrelated customer files.
- SCM tokens should have only the repositories and API operations required by
  their role.
- AAP launcher tokens should have Execute only on the intended Job Template.

### C5. Users, RBAC, credentials, and execution environments

Generic Bootstrap creates no users and assigns no roles. Platform teams declare
users, IdP mappings, RBAC, Galaxy credentials, and execution-environment
associations in normal platform desired-state YAML. Tenant self-service through
SCM does not depend on a generated AAP user.

Shipped user examples contain no password. Dispatcher and remediation set
`users_default_password: ""` to disable the collection's `change_me` fallback.
A customer-supplied item password can still override that empty default, but
secret material should not be committed to Git.

### C6. Deletion and drift

Absence from YAML is never deletion. The current deletion matrix is fail-closed;
see [Resource Deletion Capabilities](resource-deletion-capabilities.md).

Current drift comparison covers Organizations, credential types, projects, and
job templates. It reports undeclared live objects as `extra_in_live`. During
Brownfield adoption, use report mode and review the report before enabling
remediation. Expanding coverage and aligning unmanaged-object reporting is a
separate roadmap item.

## Part D - Validate and troubleshoot

### D1. Local checks

```bash
python3 -m unittest tests/test_topology_contract.py
ansible-playbook --syntax-check genesis.yml
ansible-playbook --syntax-check bootstrap.yml
python3 -m py_compile scripts/pipeline/*.py schemas/validate_naming.py
```

`site.yml` and Drift syntax checks require `infra.aap_configuration` installed
in the local Ansible collection path.

### D2. Required nonproduction evidence

Run every scenario in [Nonproduction Validation](NONPRODUCTION_VALIDATION.md)
against nonproduction AAP and SCM. Archive AAP job IDs, pipeline URLs, control
revisions, generated marker/foundation bytes, and negative-test output.

### D3. Common failures

| Failure | Check |
|---|---|
| Tenant ID rejected | Lowercase safe key, starts with a letter, max 64 characters |
| Brownfield rejected | Explicit `aap_organization` present and `team_name` absent |
| Foundation collision | Existing engine-owned target path must contain identical bytes |
| Marker conflict | Registry identity/topology changed after scaffolding began |
| Naming failure | Active rule applies to exact Organization or Team identity |
| Missing branch | Enable branch creation or pre-create every mapped branch |
| Dispatch timeout | AAP job state, launcher RBAC, target token, and configured timeout |

### D4. Deliberately deferred capabilities

- **Scoped Dispatcher concurrency:** serialized operation remains the production
  baseline until ownership and locking boundaries are proven.
- **Composite overlay identity:** some role/RBAC/input-source types need a future
  explicit composite key design.
- **Drift redesign:** unmanaged-object semantics and broader resource coverage
  remain separate work.

These limitations must not be presented as completed by this release.
