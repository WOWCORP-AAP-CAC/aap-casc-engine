# aap-casc-engine

Hybrid AAP Configuration-as-Code engine combining **JSON-as-Interface** for tenant self-service with **`infra.aap_configuration`** as the platform backend. A reusable Red Hat Professional Services offering for governed, multi-tenant AAP management at enterprise scale.

## Overview

The `aap-casc-engine` is the core deliverable of the **Hybrid AAP Configuration-as-Code Framework**. It provides:

- **Dispatcher playbook** — Clones CasC repos from Git, processes JSON (env filter, merge, scope suffix), and applies configuration to AAP via the `infra.aap_configuration.dispatch` role
- **Drift detection** — Compares Git desired state vs AAP live state, generates drift reports, and optionally auto-remediates
- **Bootstrap automation** — Automated tenant onboarding (org creation, RBAC, repo scaffolding)
- **Pipeline-as-a-Service** — Shared CI/CD templates (GitLab CI + GitHub Actions) for validation and deployment
- **JSON schema contracts** — Validation schemas for all AAP resource types
- **Governance policies** — OPA policies and naming convention enforcement

## Two-Persona Architecture

| Persona | Responsibility | Interface |
|---------|---------------|-----------|
| **Platform Team** | Manage engine, governance, shared resources, onboarding | This repo + platform CasC repos |
| **Tenant Teams** | Define their AAP resources (projects, credentials, templates, etc.) | Flat JSON files — no Ansible knowledge required |

Tenant teams commit JSON files to their dedicated repos. The platform-managed pipeline validates and triggers the dispatcher, which applies configuration to AAP. Tenants never interact with Ansible, the dispatch role, or this engine directly.

## Repository Structure

```
aap-casc-engine/
├── site.yml                          # Dispatcher playbook (main entry point)
├── drift-detect.yml                  # Drift detection playbook
├── remediate.yml                     # Drift remediation tasks
├── bootstrap.yml                     # Tenant onboarding playbook
├── repos-manifest.yml                # Registry of all CasC repos + env-branch map
├── ansible.cfg                       # Ansible configuration
├── inventory/
│   ├── dev.yml                       # Dev AAP environment
│   ├── tst.yml                       # Test AAP environment
│   ├── npr.yml                       # Pre-production AAP environment
│   └── prd.yml                       # Production AAP environment
├── roles/
│   ├── git_clone_repos/              # Clone CasC repos from Git
│   └── process_casc_json/            # Env filter + merge + scope suffix
├── schemas/
│   ├── *.schema.json                 # JSON Schema contracts per resource type
│   ├── validate_naming.py            # Naming convention validator
│   ├── naming-rules.yml              # Naming convention rules
│   └── policies/                     # OPA governance policies
├── pipeline-templates/
│   ├── gitlab/                       # GitLab CI shared template
│   └── github/                       # GitHub Actions reusable workflow
├── templates/                        # Jinja2 templates for bootstrap
├── collections/
│   └── requirements.yml              # Ansible collection dependencies
├── execution-environment/
│   └── execution-environment.yml     # EE build definition
└── examples/
    ├── platform/                     # Example platform JSON configs
    └── tenant/                       # Example tenant JSON configs
```

## Quick Start

### Prerequisites

- **AAP 2.5+** with Gateway, Controller, Hub, and (optionally) EDA
- **Git SCM** (GitLab, GitHub, or compatible) with API access
- **CI/CD platform** (GitLab CI, GitHub Actions, or compatible)
- **`infra.aap_configuration`** collection v2.9.0+ installed in the Execution Environment
- Python 3.9+ (for local validation)

### 1. Configure the Repos Manifest

Edit `repos-manifest.yml` to register your platform and tenant CasC repositories:

```yaml
env_branch_map:
  dev: develop
  tst: develop
  npr: release/npr
  prd: main

platform_repos:
  - name: aap-organizations-global
    scope: platform
  # ... add your platform repos

tenant_repos:
  - name: controller-projects-myorg01
    scope: myorg01
  # ... add your tenant repos
```

### 2. Configure Inventory

Update `inventory/<env>.yml` with your AAP environment connection details, or set environment variables:

```bash
export AAP_HOSTNAME="aap-controller.dev.example.com"
export AAP_USERNAME="admin"
export AAP_PASSWORD="<your-password>"
export SCM_BASE_URL="https://gitlab.example.com/casc"
export SCM_TOKEN="<your-scm-token>"
```

### 3. Run the Dispatcher

**Full apply** (all repos — scheduled reconciliation):

```bash
ansible-playbook site.yml -e target_env=dev
```

**Targeted apply** (single tenant + platform repos — CI/CD triggered):

```bash
ansible-playbook site.yml \
  -e target_env=dev \
  -e triggered_repo=controller-projects-myorg01 \
  -e trigger_source=ci-cd-pipeline
```

### 4. Run Drift Detection

```bash
# Report mode (detect only)
ansible-playbook drift-detect.yml -e target_env=dev -e drift_mode=report

# Remediate mode (detect and auto-fix)
ansible-playbook drift-detect.yml -e target_env=prd -e drift_mode=remediate
```

### 5. Bootstrap a New Tenant

```bash
ansible-playbook bootstrap.yml \
  -e org_id=newteam01 \
  -e team_name="New Team" \
  -e team_lead=newteam_lead \
  -e team_group=newteam_developers
```

## How It Works

1. **Tenant teams** commit flat JSON files (e.g., `controller_projects`, `controller_credentials`) to their dedicated Git repos using standard `infra.aap_configuration` variable names
2. **The shared CI/CD pipeline** validates JSON files (schema, naming, policy) and triggers the dispatcher on the correct AAP environment via a single API call
3. **The dispatcher** (`site.yml`) clones all CasC repos from Git, filters by environment, merges JSON files, adds scope suffixes (`_platform`, `_<org_id>`), and applies via `infra.aap_configuration.dispatch` with `dispatch_include_wildcard_vars: true`
4. **Weekly scheduled reconciliation** runs drift detection to catch manual changes and ensure continuous compliance

## Key Design Principles

- **JSON-as-Interface** — All AAP resources are defined as flat JSON files with standard variable names. No Ansible knowledge required for tenants.
- **Scope suffixing** — The `process_casc_json` role adds suffixes (e.g., `controller_projects_myorg01`). The `dispatch` role's wildcard merging combines them automatically.
- **Dual-mode apply** — Targeted apply for CI/CD triggers (fast, single tenant); full apply for scheduled reconciliation (comprehensive).
- **Git as single source of truth** — No artifact repositories. The dispatcher clones repos directly from Git.
- **Vault-free secrets** — AAP Custom Credential Types with external secrets manager integration (HashiCorp Vault, CyberArk, Azure Key Vault).

## CI/CD Pipeline Setup

### GitLab CI

Tenant repos include the shared pipeline template:

```yaml
# .gitlab-ci.yml in tenant repo
include:
  - project: 'platform-team/aap-casc-engine'
    ref: 'main'
    file: '/pipeline-templates/gitlab/.gitlab-ci-template.yml'
```

### GitHub Actions

Tenant repos reference the reusable workflow:

```yaml
# .github/workflows/casc.yml in tenant repo
name: CasC Pipeline
on:
  push:
  pull_request:
    branches: [main]
jobs:
  casc:
    uses: <org>/aap-casc-engine/.github/workflows/casc-validate-and-trigger.yml@main
    with:
      dispatcher_jt_name: jt-platform-casc-dispatcher
    secrets: inherit
```

The workflow supports **dual authentication**:

- **Bearer token** (production) — per-environment secrets with branch-to-env routing
- **Basic auth** (demo/sandbox) — single-host secrets for quick validation setups

If per-env token secrets are set, Bearer token auth with branch routing is used. Otherwise, if `AAP_HOST` + `AAP_USERNAME` + `AAP_PASSWORD` are set, basic auth against a single AAP host is used. If neither is configured, the trigger stage is skipped (validate-only mode).

### Required CI/CD Variables

**Production mode (Bearer token — per-environment):**

| Variable | Description |
|----------|-------------|
| `AAP_DEV_HOST` / `AAP_TST_HOST` / `AAP_NPR_HOST` / `AAP_PRD_HOST` | AAP API endpoints per environment |
| `AAP_DEV_TOKEN` / `AAP_TST_TOKEN` / `AAP_NPR_TOKEN` / `AAP_PRD_TOKEN` | Per-environment AAP API tokens |

**Demo/sandbox mode (basic auth — single host):**

| Variable | Description |
|----------|-------------|
| `AAP_HOST` | AAP controller hostname |
| `AAP_USERNAME` | AAP admin username |
| `AAP_PASSWORD` | AAP admin password |

**Workflow inputs:**

| Input | Default | Description |
|-------|---------|-------------|
| `dispatcher_jt_name` | `jt-platform-casc-dispatcher` | Name of the dispatcher Job Template to trigger |

**Optional secrets:**

| Secret | Description |
|--------|-------------|
| `ENGINE_REPO_TOKEN` | GitHub PAT for accessing the engine repo when it is private (defaults to `github.token` for public repos) |

## AAP Job Templates

Create these Job Templates in each AAP environment:

| Job Template | Playbook | Purpose |
|-------------|----------|---------|
| `jt-platform-casc-dispatcher` | `site.yml` | Main dispatcher — apply CasC configuration |
| `jt-platform-drift-detection` | `drift-detect.yml` | Drift detection and reconciliation |
| `jt-platform-bootstrap-tenant` | `bootstrap.yml` | Onboard new tenant organizations |

## Local Validation

Validate JSON files locally before committing:

```bash
# Install validation tools
pip install check-jsonschema pyyaml

# Validate a JSON file against its schema
resource_type=$(python3 -c "import json,sys; print(list(json.load(open(sys.argv[1])).keys())[0])" your-file.json)
check-jsonschema --schemafile schemas/${resource_type}.schema.json your-file.json

# Validate naming conventions
python3 schemas/validate_naming.py --config-dir . --rules schemas/naming-rules.yml
```

## Dependencies

- [infra.aap_configuration](https://github.com/redhat-cop/infra.aap_configuration) >= 2.9.0 — Red Hat Communities of Practice collection for AAP management
- Red Hat Ansible Automation Platform 2.5+
- Python 3.9+

## License

[GPL-3.0](LICENSE) — consistent with `infra.aap_configuration` and other Red Hat Communities of Practice Ansible projects.

## Contributing

This project is part of the [Red Hat Communities of Practice](https://github.com/redhat-cop). Contributions are welcome via pull requests.

## Related Resources

- [infra.aap_configuration documentation](https://github.com/redhat-cop/infra.aap_configuration)
- [Ansible Automation Platform documentation](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/)
- [Red Hat Communities of Practice](https://github.com/redhat-cop)
