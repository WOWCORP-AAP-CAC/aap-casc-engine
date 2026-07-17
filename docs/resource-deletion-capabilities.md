# Resource Deletion Capabilities

Authoritative matrix for whether the engine accepts explicit reviewed deletion
declarations. Absence of a YAML object is **never** treated as deletion.

Source of truth:

- default: `schemas/resource-types.yml` → `defaults.deletion_supported`
- per-key overrides: `schemas/resource-types.yml` → `exceptions.<key>.deletion_supported`
- allowed keys inventory: `roles/process_casc_config/defaults/main.yml`

## Contract

| Condition | Engine behavior |
|---|---|
| Object undeclared in SCM | Unmanaged / ClickOps-safe; not a deletion candidate |
| Object declared in SCM | Git authoritative for that object |
| Explicit deletion syntax for `deletion_supported: false` | Validation fails before dispatch |
| Explicit deletion syntax for `deletion_supported: true` | Allowed only with documented schema field/value |

## Seeded inventory audit (initial release)

All seeded keys currently default to **`deletion_supported: false`** until an
operator-reviewed collection-role audit records evidence for a specific key.

### Platform / global keys

| Key | deletion_supported | Notes |
|---|---|---|
| `aap_organizations` | false | Foundation object; no implicit delete |
| `aap_teams` | false | Foundation object; no implicit delete |
| `aap_user_accounts` | false | Foundation object; no implicit delete |
| `gateway_role_definitions` | false | Not audited for safe delete |
| `gateway_role_user_assignments` | false | Not audited for safe delete |
| `gateway_role_team_assignments` | false | Not audited for safe delete |
| `controller_credential_types` | false | Not audited for safe delete |
| `controller_execution_environments` | false | Not audited for safe delete |
| `controller_settings` | false | Settings are update-oriented |
| `controller_schedules` | false | Shared key; not audited for safe delete |

### Tenant keys

| Key | deletion_supported | Notes |
|---|---|---|
| `controller_projects` | false | Not audited for safe delete |
| `controller_credentials` | false | Not audited for safe delete |
| `controller_inventories` | false | Not audited for safe delete |
| `controller_templates` | false | Not audited for safe delete |
| `controller_workflows` | false | Not audited for safe delete |
| `controller_schedules` | false | Not audited for safe delete |
| `controller_notifications` | false | Not audited for safe delete |

## Enabling deletion for a key

1. Confirm the `infra.aap_configuration` role/module supports an explicit absent/state field.
2. Record the accepted object-level field/value in `schemas/resource-types.yml`.
3. Set `deletion_supported: true` for that key only.
4. Update this matrix and contract tests.
5. Require reviewed commits for any deletion declaration.

Do not infer support from a collection-wide default.
