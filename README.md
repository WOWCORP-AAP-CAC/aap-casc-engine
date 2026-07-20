# aap-casc-engine

**AAP Multi-Tenant CasC Engine** provides a simple flat-YAML interface for
multi-team, multi-environment AAP Configuration-as-Code. It keeps
`infra.aap_configuration` as the supported apply backend while centralizing the
repository scaffolding, validation, environment overlays, dispatch, onboarding,
and drift workflows that customers would otherwise build themselves.

## What the engine provides

- One central pipeline service for GitHub Actions and GitLab CI.
- Separate control, platform desired-state, and tenant desired-state repositories.
- Multiple YAML files per resource type, merged by the engine before dispatch.
- `base/` plus environment-overlay folders driven by `env_branch_map`.
- Repository creation or governed pre-created repository scaffolding.
- Combined or per-resource-type repository layouts with customer-selected names.
- Greenfield and Brownfield tenant onboarding.
- Optional, customer-owned naming policy.
- Scoped platform or tenant dispatch through `infra.aap_configuration.dispatch`.
- Report-mode drift detection and optional remediation.

## Architecture

| Repository | Purpose |
|---|---|
| `aap-casc-engine` | Playbooks, schemas, reusable pipelines, and templates |
| `casc-platform-control` | Mandatory `config.yml` and `tenants.yml`; optional `naming-rules.yml` |
| Platform desired-state repo(s) | Shared Organization, Team, RBAC, settings, and other platform YAML |
| Tenant desired-state repo(s) | Tenant projects, inventories, credentials, templates, workflows, schedules, and notifications |

The control repository never contains AAP desired-state YAML. Normal platform
and tenant pushes dispatch only their own scope; a tenant dispatch does not
reapply platform desired state.

## Consumer interface

A tenant can keep each object in a separate file:

```text
casc-tenant-stores/
├── base/
│   ├── projects/project-deploy.yml
│   ├── inventories/inventory-dev.yml
│   └── templates/job-template-deploy.yml
├── dev/inventories/inventory-dev.yml
├── prd/inventories/inventory-dev.yml
└── .github/workflows/casc.yml
```

Each file uses an `infra.aap_configuration` variable key:

```yaml
---
controller_projects:
  - name: Stores Deployment
    organization: WW Stores Automation
    scm_type: git
    scm_url: https://github.example/ww/stores-automation.git
    scm_branch: main
```

Filenames are organizational only. Optional naming policy validates resource
identities inside YAML, not filenames.

## Quick start

### Prerequisites

- Red Hat Ansible Automation Platform with an execution environment containing
  `infra.aap_configuration >=4.0.0,<5.0.0`.
- GitHub or GitLab API access.
- AAP Job Templates for Genesis, Bootstrap, Dispatcher, and Drift Detection.
- SCM and AAP credentials described in the
  [Setup and Operations Guide](docs/ENGINE_SETUP_AND_OPERATIONS_GUIDE.md).

### 1. Run Genesis

Genesis creates repositories when `repo_mode=create`, or scaffolds repositories
that already exist when `repo_mode=existing`. Pre-created repositories may be
empty when branch creation is enabled; the engine initializes them with final
managed content before creating the high-to-low environment branch topology.

```bash
export SCM_TOKEN='<scm-api-token>'
export SCM_BASE_URL='https://github.com'

ansible-playbook genesis.yml \
  -e platform_scm_org=ww-platform \
  -e control_scm_org=ww-platform \
  -e control_repo=casc-platform-control \
  -e platform_repo=casc-platform-global \
  -e platform_repo_pattern=combined \
  -e repo_mode=existing
```

Genesis seeds `config.yml` and `tenants.yml` in the control repository. It does
not activate a naming policy by default.

### 2. Register a Greenfield tenant

```yaml
---
tenants:
  - tenant_id: stores
    aap_organization: WW Stores Automation  # optional; defaults to tenant_id
    team_name: Stores Automation
    tenant_scm_org: ww-tenants
    repo_pattern: combined
    repo_name: stores-aap-casc               # optional
    repo_mode: existing
    onboarding_mode: greenfield
    status: active
```

Greenfield Bootstrap scaffolds tenant repositories and writes only two platform
foundation declarations on every mapped branch:

- `base/organizations/stores.yml`
- `base/teams/stores.yml`

Users, IdP mappings, RBAC assignments, credentials, Galaxy associations, and
execution-environment associations remain normal customer desired state.

### 3. Register a Brownfield tenant

```yaml
---
tenants:
  - tenant_id: legacy_app
    aap_organization: Existing LDAP/SAML Organization
    tenant_scm_org: ww-tenants
    repo_pattern: combined
    repo_name: legacy-app-aap-casc
    repo_mode: existing
    onboarding_mode: brownfield
    status: active
```

Brownfield Bootstrap is SCM-only. It requires the exact existing AAP
Organization name, rejects `team_name`, writes no foundation YAML, and performs
no onboarding dispatch. Existing AAP objects remain unchanged until the customer
declares them in desired-state YAML.

### 4. Apply one scope

```bash
ansible-playbook site.yml \
  -e target_env=dev \
  -e dispatch_scope=tenant \
  -e tenant_id=stores
```

## Tenant identity

| Field | Meaning |
|---|---|
| `tenant_id` | Required stable engine key. Must match `^[a-z][a-z0-9_]*$`, maximum 64 characters. |
| `aap_organization` | Exact AAP Organization name. Optional for Greenfield; required for Brownfield. |
| `team_name` | Exact Team created by Greenfield Bootstrap. Required for Greenfield; forbidden for Brownfield. |

Repository routing is `repository -> tenants.yml -> tenant_id`. It never infers
the AAP Organization from a repository name.

## Repository layouts and names

| Option | Combined | Per-resource-type |
|---|---|---|
| Platform | `platform_repo` | `platform_repo_names` folder-to-name mapping |
| Tenant | optional `repo_name` | optional partial `repo_names` folder-to-name mapping |

Unknown folders, blank names, duplicate effective names, or collisions with
control/platform ownership fail before SCM mutation. Ordered name lists are not
supported.

## Environment branches

`env_branch_map` is ordered from lowest to highest environment:

```yaml
env_branch_map:
  dev: develop
  tst: release/tst
  prd: main
```

Missing branches are created from high to low so lower environments start from
the approved higher-environment baseline. Changes are promoted low to high.
Feature-branch pushes and pull/merge requests validate only; mapped-branch
pushes dispatch to the corresponding environment.

## Optional naming policy

Naming validation is inactive when control-root `naming-rules.yml` is missing or
empty. To activate it, start from:

- `examples/naming-rules.yml.sample` for a customer policy.
- `examples/naming-rules-type-prefixed.yml.sample` for the optional type-prefixed example.

Commit the policy to the control branch before Bootstrap. The exact rendered
Greenfield Organization and Team are validated before any SCM mutation. A rule
applies only to resource types explicitly present in the policy.

## Safety boundaries

- Scaffold markers make tenant identity and repository topology immutable after
  scaffolding starts; pre-scaffold corrections remain allowed.
- `status` and `dispatch_enabled` are mutable operational controls.
- A paused new Greenfield tenant is still scaffolded and receives its
  platform-owned Organization/Team foundation; tenant desired state waits until
  `dispatch_enabled` is re-enabled.
- Absence from SCM is never interpreted as deletion.
- Generated user examples contain no password, and apply paths disable the
  collection's `change_me` fallback.
- The production baseline is serialized and requires Dispatcher
  `allow_simultaneous=false`.

## Current limitations

- Scoped Dispatcher concurrency is a separate future enhancement.
- Composite overlay identities for selected RBAC/role/input-source resources are
  not yet universally supported.
- Drift comparison currently covers Organizations, credential types, projects,
  and job templates; undeclared live objects can appear as `extra_in_live` in
  reports. See the setup guide before using remediation in Brownfield adoption.

## Documentation

- [Setup and Operations Guide](docs/ENGINE_SETUP_AND_OPERATIONS_GUIDE.md)
- [Pipeline Trigger Logic](docs/pipeline-trigger-logic.md)
- [Nonproduction Validation](docs/NONPRODUCTION_VALIDATION.md)
- [Resource Deletion Capabilities](docs/resource-deletion-capabilities.md)

## License

[GPL-3.0](LICENSE)
