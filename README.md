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

### 2. Configure Connection

**Via AAP Job Templates (production):** AAP credentials inject `CONTROLLER_HOST`, `CONTROLLER_USERNAME`, `CONTROLLER_PASSWORD`, and `SCM_TOKEN` automatically. No manual configuration needed -- see [AAP Credential Types](#aap-credential-types) below.

**Via CLI (local testing):** Set environment variables directly. The playbooks read these from play-level `vars:`, which take precedence over inventory files:

```bash
export CONTROLLER_HOST="aap-controller.dev.example.com"
export CONTROLLER_USERNAME="admin"
export CONTROLLER_PASSWORD="<your-password>"
export SCM_BASE_URL="https://gitlab.example.com/casc"
export SCM_TOKEN="<your-scm-token>"
```

> **Inventory files:** `inventory/{dev,tst,npr,prd}.yml` set `target_env` and provide per-environment host fallbacks (`CONTROLLER_DEV_HOST`, etc.) for advanced multi-env CLI workflows. Note that playbook play-level `vars:` have higher Ansible precedence than inventory group vars, so `CONTROLLER_HOST` from the environment (read by play vars) is used unless overridden with `-e`.

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
  - project: '<platform-group>/aap-casc-engine'
    ref: 'main'
    file: '/pipeline-templates/gitlab/.gitlab-ci-template.yml'
```

> **Note:** Replace `<platform-group>` with the full path to the engine project in your GitLab instance. Override the `ENGINE_PROJECT_PATH` CI/CD variable if the engine project lives at a non-default path.

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
| `AAP_USERNAME` | AAP username (least-privilege: execute-only on dispatcher JT) |
| `AAP_PASSWORD` | AAP password |

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

## AAP Credential Types

All sensitive connection values are injected at runtime via AAP credentials attached to Job Templates. Playbooks read exclusively from environment variables -- no plaintext secrets in `extra_vars`.

### Built-in: Red Hat Ansible Automation Platform

Injects `CONTROLLER_HOST`, `CONTROLLER_USERNAME`, `CONTROLLER_PASSWORD`, `CONTROLLER_VERIFY_SSL` as environment variables. All three playbooks (`site.yml`, `drift-detect.yml`, `bootstrap.yml`) read these via `lookup('env', ...)`.

- Credential name (demo): `cred-platform-aap-connection`
- Attach to all 3 Job Templates

### Custom: CasC SCM Token

The built-in GitHub/GitLab PAT credential types have empty injectors -- they cannot pass tokens to playbooks. Create a custom credential type:

- **Name:** `CasC SCM Token`
- **Input:** `scm_token` (secret string)
- **Injector:** `env: { SCM_TOKEN: "{{ '{{' }}scm_token{{ '}}' }}" }`

Both `scm_token` (git clone) and `scm_api_token` (bootstrap SCM API) resolve from the single `SCM_TOKEN` environment variable.

- Credential name (demo): `cred-platform-scm-token`
- Attach to all 3 Job Templates

### What Stays in extra_vars

Non-sensitive configuration values remain in JT `extra_vars`:
- `target_env`, `scm_base_url`, `registry_repo`, `registry_repo_org`, `platform_org`, `orgs_repo`, etc.

## SCM Token Requirements

The `SCM_TOKEN` credential should belong to a **dedicated service account** (machine user), not a personal user. Required permissions:

| Playbook | Access Needed |
|----------|--------------|
| `site.yml` (dispatcher) | **Read** across all CasC repos (platform + tenant orgs) |
| `drift-detect.yml` | **Read** across all CasC repos (platform + tenant orgs) |
| `bootstrap.yml` | **Read-write** in the target tenant SCM org + platform registry repo |

A single token with broad access is the simplest model but has a wider blast radius if compromised. For production hardening options (GitHub App, per-org credentials, fine-grained PATs, GitLab Group Access Tokens), see the master design document.

## Tenant Onboarding Workflow

Bootstrap follows a 4-phase handshake between the platform and tenant teams:

1. **Intake** -- Tenant provides `org_id`, `team_name`, `team_lead`, `tenant_scm_org`, preferred `repo_pattern`
2. **SCM + CI/CD prerequisite** -- Platform team adds service account to tenant's SCM org and configures CI/CD secrets
3. **Bootstrap execution** -- Platform team runs `jt-platform-bootstrap-tenant` with tenant survey inputs
4. **Handover** -- Platform team provides tenant with repo URLs, pipeline usage guide, and first-action path

See the platform team user guide for a detailed checklist.

## CI/CD Secret Requirements

The CI/CD pipeline trigger stage authenticates with AAP using secrets stored in the CI/CD platform. These are separate from AAP credential types.

**GitHub Actions** (per tenant org):

| Secret | Required | Description |
|--------|----------|-------------|
| `AAP_HOST` | Basic auth | AAP controller hostname |
| `AAP_USERNAME` | Basic auth | AAP username with execute permission on dispatcher JT |
| `AAP_PASSWORD` | Basic auth | AAP password |
| `AAP_DEV_HOST` / `AAP_DEV_TOKEN` | Bearer auth | Per-environment AAP endpoints and OAuth tokens |
| `ENGINE_REPO_TOKEN` | If engine is private | PAT with read access to the engine repo |

**GitLab CI** (per tenant group):

| Variable | Required | Description |
|----------|----------|-------------|
| `AAP_HOST` | Basic auth | AAP controller hostname |
| `AAP_USERNAME` | Basic auth | AAP username |
| `AAP_PASSWORD` | Basic auth | AAP password |
| Engine `CI_JOB_TOKEN` allowlist | Always | Add tenant groups to the engine project's Token Access settings |

The AAP user/token used by CI/CD should have **only Execute permission** on the dispatcher JT. Rotate tokens on a schedule and enable AAP activity stream monitoring.

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
