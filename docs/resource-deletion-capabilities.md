# Resource deletion capabilities

Desired-state absence from SCM is **never** interpreted as deletion. Explicit
deletion requires:

1. `deletion_supported: true` for the resource key in the catalog, and
2. an object-level deletion marker (default field `state`, value `absent`).

Catalog sources:

- default: `schemas/resource-types.yml` → `defaults.deletion_supported`
- per-key overrides: `schemas/resource-types.yml` → `exceptions.<key>.deletion_supported`

CI (`validate-deletions`) and Dispatcher (`process_casc_config`) both enforce
this contract fail-closed.

Atomic path replace (ROADMAP-001) replaces file contributions during merge; it
does **not** delete live AAP objects.

## Seeded inventory audit (initial release)

All currently supported keys default to **`deletion_supported: false`** until an
operator-reviewed collection-role audit records evidence for a specific key.

Representative foundation / day-2 keys:

| Key | deletion_supported | Notes |
|---|---|---|
| `aap_organizations` | false | Foundation object; no implicit delete |
| `aap_teams` | false | Foundation object; atomic overlay |
| `aap_user_accounts` | false | Not audited for safe delete |
| `controller_projects` | false | Atomic overlay; not audited for safe delete |
| `controller_settings` | false | Raw settings; update-oriented |
| `gateway_role_user_assignments` | false | Atomic overlay; not audited for safe delete |

Action keys (`controller_launch_jobs`, `controller_bulk_hosts`,
`controller_workflow_launch_jobs`, `hub_ee_repository_sync`) remain unsupported
and cannot carry deletion metadata.

## Enabling deletion for a key

1. Confirm the `infra.aap_configuration` role/module supports an explicit absent/state field.
2. Record the accepted object-level field/value in `schemas/resource-types.yml`.
3. Record non-empty `deletion_evidence` for the audited collection role/module.
4. Set `deletion_supported: true` for that key only.
5. Update this matrix and contract tests.
6. Require reviewed commits for any deletion declaration.
