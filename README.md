# aap-casc-engine

Hybrid AAP Configuration-as-Code engine combining **YAML-as-Interface** for tenant self-service with **`infra.aap_configuration`** as the platform backend. A reusable Red Hat Professional Services offering for governed, multi-tenant AAP management at enterprise scale.

## Overview

The `aap-casc-engine` is the core deliverable of the **Hybrid AAP Configuration-as-Code Framework**. It provides:

- **Dispatcher playbook** — Clones the platform repo to read `config.yml` + `tenants.yml`, clones CasC repos, processes YAML (folder-based environment layering with base/env merge), and applies configuration to AAP via the `infra.aap_configuration.dispatch` role
- **Drift detection** — Compares Git desired state vs AAP live state, generates drift reports (persisted as AAP job artifacts via `set_stats`), and optionally auto-remediates
- **Platform genesis** — Automated one-time setup of platform CasC repos (combined or per-resource-type), CI/CD seeding, and `config.yml` + `tenants.yml` generation
- **Bootstrap automation** — SCM-only tenant onboarding (repo creation, RBAC YAML push, environment branch creation, `tenants.yml` registration). Bootstrap runs as an AAP JT but uses only SCM credentials — the dispatcher is the sole mechanism for applying desired state to AAP
- **Pipeline-as-a-Service** — Shared CI/CD templates (GitLab CI + GitHub Actions) for validation and deployment
- **Governance policies** — OPA policies and naming convention enforcement
- **Multi-environment model** — `env_branch_map` defines a strict 1:1 mapping from environment to Git branch. Platform repos are read from `platform_branch`; tenant repos from the env-specific branch. CI/CD fan-out dispatches across all environments after bootstrap (default enabled via `bootstrap_dispatch_fanout: true`)
- **GitOps lifecycle** — Genesis is imperative (Day 0); all subsequent operations are commit-driven (Day 1+)

## Two-Persona Architecture

| Persona | Responsibility | Interface |
|---------|---------------|-----------|
| **Platform Team** | Manage engine, governance, shared resources, onboarding | This repo + platform CasC repos |
| **Tenant Teams** | Define their AAP resources (projects, credentials, templates, etc.) | Declarative YAML files — engine and collection complexity abstracted |

Tenant teams commit YAML files to their dedicated repos using a folder-based structure (`base/` + `<env>/` directories). The platform-managed pipeline validates and triggers the dispatcher, which applies configuration to AAP. Tenants never interact with the dispatch role, collection internals, or this engine directly.

## Repository Structure

```
aap-casc-engine/
├── site.yml                          # Dispatcher playbook (main entry point)
├── drift-detect.yml                  # Drift detection playbook
├── remediate.yml                     # Drift remediation tasks
├── genesis.yml                       # Platform genesis playbook
├── bootstrap.yml                     # Tenant onboarding playbook
├── ansible.cfg                       # Ansible configuration
├── inventory/
│   ├── dev.yml                       # Dev AAP environment
│   ├── tst.yml                       # Test AAP environment
│   ├── npr.yml                       # Pre-production AAP environment
│   └── prd.yml                       # Production AAP environment
├── roles/
│   ├── git_clone_repos/              # Clone CasC repos from Git
│   └── process_casc_config/          # Folder-based YAML processing + env merge
├── schemas/
│   ├── resource-types.yml            # Per-resource-type validation & merge config
│   ├── validate_naming.py            # Naming convention validator (YAML)
│   ├── naming-rules.yml              # Naming convention rules
│   └── policies/                     # OPA governance policies
├── pipeline-templates/
│   ├── gitlab/                       # GitLab CI shared template
│   └── github/                       # GitHub Actions standalone workflow
├── templates/                        # Jinja2 templates (YAML seeds, bootstrap resources)
├── collections/
│   └── requirements.yml              # Ansible collection dependencies
└── examples/
    └── v2/                           # Example folder-based YAML configs
        ├── platform/                 # Platform repo example
        └── tenant/                   # Tenant repo example (base + env overrides)
```

## Tenant Repo Structure

Tenants use a folder-based structure with `base/` for all-environment configs and optional `<env>/` directories for overrides:

```
casc-tenant-myorg01/
  base/                          # applies to ALL environments
    projects/
      prj-myorg01-db_patching.yml
    templates/
      jt-myorg01-db_patching.yml
    credentials/
      crd-myorg01-machine_demo.yml
    inventories/
      inv-myorg01-db_servers.yml
  dev/                           # env-specific overrides
    inventories/
      inv-myorg01-db_servers.yml
  prd/
    inventories/
      inv-myorg01-db_servers.yml
  .github/workflows/casc.yml    # CI/CD thin caller
```

Each YAML file uses a single top-level dispatch variable key:

```yaml
controller_projects:
  - name: prj-myorg01-db_patching
    description: Database patching automation
    scm_type: git
    scm_url: https://github.com/example/repo.git
    organization: org-myorg01
```

## Quick Start

### Prerequisites

- **AAP 2.5+** with Gateway, Controller, Hub, and (optionally) EDA
- **Git SCM** (GitLab, GitHub, or compatible) with API access
- **CI/CD platform** (GitLab CI, GitHub Actions, or compatible)
- **`infra.aap_configuration`** collection v4.x (>=4.0.0, <5.0.0) installed in the Execution Environment
- Python 3.9+ (for local validation)

### 1. Run Platform Genesis

Genesis creates the platform CasC repository (or repositories) and initializes `config.yml` + `tenants.yml`:

```bash
export SCM_TOKEN="<your-scm-token>"
ansible-playbook genesis.yml \
  -e platform_scm_org=<your-platform-org> \
  -e scm_base_url=https://github.com \
  -e engine_repo=aap-casc-engine \
  -e default_organization=MyOrg \
  -e repo_pattern=combined
```

**Repo patterns:**

| Pattern | Repos Created | Best For |
|---------|--------------|----------|
| `combined` (default) | 1 platform repo (`casc-platform-global`) | Most deployments |
| `per-resource-type` | 9 separate repos + home repo | Large teams wanting fine-grained access |

Genesis creates `config.yml` (platform settings) and `tenants.yml` (empty tenant registry) in the platform home repo. After genesis, the platform repo is the operational source of truth.

### 2. Configure Connection

**Via AAP Job Templates (production):** AAP credentials inject `CONTROLLER_HOST`, `CONTROLLER_USERNAME`, `CONTROLLER_PASSWORD`, and `SCM_TOKEN` automatically.

**Via CLI (local testing):**

```bash
export CONTROLLER_HOST="aap-controller.dev.example.com"
export CONTROLLER_USERNAME="admin"
export CONTROLLER_PASSWORD="<your-password>"
export SCM_BASE_URL="https://github.com"
export SCM_TOKEN="<your-scm-token>"
```

### 3. Run the Dispatcher

```bash
# Full apply (all repos from tenants.yml)
ansible-playbook site.yml -e target_env=dev

# Targeted apply (single tenant + platform repos)
ansible-playbook site.yml \
  -e target_env=dev \
  -e triggered_repo=casc-tenant-myorg01
```

### 4. Bootstrap a New Tenant

```bash
ansible-playbook bootstrap.yml \
  -e org_id=newteam01 \
  -e team_name="New Team" \
  -e team_lead=newteam_lead \
  -e tenant_scm_org=aap-casc-tenant-newteam01 \
  -e repo_pattern=combined
```

Bootstrap is SCM-only: it creates the tenant repo, seeds it with CI/CD and example files, pushes RBAC YAML to the platform repo, creates environment branches (per `env_branch_map`), and registers the tenant in `tenants.yml`. AAP resource creation (org, team, user, RBAC) is handled by the dispatcher when it processes the pushed YAML files.

### 5. Run Drift Detection

```bash
# Report mode
ansible-playbook drift-detect.yml -e target_env=dev -e drift_mode=report

# Remediate mode
ansible-playbook drift-detect.yml -e target_env=prd -e drift_mode=remediate
```

## How It Works

1. **Tenant teams** commit YAML files to their repos using the `base/` + `<env>/` folder structure
2. **The shared CI/CD pipeline** validates YAML files (structural, naming, OPA policy) and triggers the dispatcher
3. **The dispatcher** (`site.yml`) clones the platform repo to read `config.yml` + `tenants.yml`, clones all CasC repos, processes YAML (base/env merge, scope suffix), and applies via `infra.aap_configuration.dispatch`
4. **Scheduled reconciliation** runs drift detection to catch manual changes

## Key Design Principles

- **YAML-as-Interface** — All AAP resources are defined as YAML files with standard `infra.aap_configuration` variable names
- **Folder-based environments** — `base/` for universal config, `<env>/` for environment-specific overrides (like Ansible `group_vars` or Kustomize overlays)
- **Engine = pure logic** — The engine repo contains only playbooks, roles, and templates. All deployment state lives in the platform repo
- **GitOps lifecycle** — Genesis is imperative (Day 0); everything after is commit-driven (Day 1+)
- **No JSON schemas** — Structural validation uses `resource-types.yml`; field-level validation deferred to apply-time collection modules

## Platform Control Files

The platform repo contains two control files generated by genesis:

**`config.yml`** — Platform configuration:

```yaml
default_organization: Default
scm_provider: github
platform_scm_org: my-platform-org
repo_pattern: combined
platform_branch: main
create_missing_env_branches: true
bootstrap_dispatch_fanout: true
env_branch_map:
  dev: develop
  tst: release/tst
  npr: release/npr
  prd: main
```

`env_branch_map` enforces a strict 1:1 mapping — each branch value must be unique (one branch per environment). Environment names must match `^[a-z0-9_]+$`. `platform_branch` specifies which branch platform repos are read from. `create_missing_env_branches` controls whether bootstrap creates env branches in new tenant repos. `bootstrap_dispatch_fanout` (default `true`) enables Day-0 multi-environment dispatch after tenant onboarding.

**`tenants.yml`** — Tenant registry:

```yaml
tenants:
  - org_id: myorg01
    team_name: Platform Engineering
    team_lead: jsmith
    tenant_scm_org: aap-casc-tenant-myorg01
    repo_pattern: combined
    status: active
```

## AAP Job Templates

| Job Template | Playbook | Purpose |
|-------------|----------|---------|
| `jt-platform-genesis` | `genesis.yml` | One-time platform repo creation |
| `jt-platform-casc_dispatcher` | `site.yml` | Main dispatcher — apply CasC configuration |
| `jt-platform-drift_detection` | `drift-detect.yml` | Drift detection and reconciliation |
| `jt-platform-bootstrap_tenant` | `bootstrap.yml` | Onboard new tenant organizations |

## Local Validation

```bash
# Install
pip install pyyaml

# Validate naming conventions
python3 schemas/validate_naming.py --config-dir . --rules schemas/naming-rules.yml
```

## Dependencies

- [infra.aap_configuration](https://github.com/redhat-cop/infra.aap_configuration) >= 4.0.0, < 5.0.0
- Red Hat Ansible Automation Platform 2.5+
- Python 3.9+

## License

[GPL-3.0](LICENSE)

## Related Resources

- [infra.aap_configuration documentation](https://github.com/redhat-cop/infra.aap_configuration)
- [Ansible Automation Platform documentation](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/)
- [Red Hat Communities of Practice](https://github.com/redhat-cop)
