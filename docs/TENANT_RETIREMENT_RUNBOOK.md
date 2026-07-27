# Tenant Retirement Runbook (Manual, Fail-Safe)

Use this checklist when a scaffolded tenant must leave the CasC estate. The engine **rejects in-place removal** of tenants that already have scaffold markers. There is **no automated tenant deletion**.

Retirement is deliberate, audited, and reversible until the final AAP cleanup step.

## Preconditions

- Operator has admin access to the control repository, platform desired-state repository, tenant SCM org/group, and AAP.
- Confirm the tenant `tenant_id`, AAP organization name, SCM org, and repository from `tenants.yml` and `.aap-casc-engine/tenant-scaffold.yml`.
- Pause desired-state apply first: set `dispatch_enabled: false` for the tenant, merge to the control branch, and wait for pipelines to finish.

## Phase 1 — Stop CasC management (non-destructive)

1. In the **control** repo `tenants.yml`, set the tenant `status: inactive` and `dispatch_enabled: false` (if not already).
2. Merge and confirm control pipelines succeed with **no** onboarding fan-out for this tenant (inactive/Brownfield-equivalent pause).
3. Do **not** delete the registry entry yet.

## Phase 2 — Quiesce desired-state content

Inactive / `dispatch_enabled=false` tenants are **not** resolvable by Dispatcher
tenant scope (`site.yml` fails closed). Any push that would run `trigger` must
therefore skip deploy.

1. In the **tenant** combined repository, move day-2 resource YAML **out of**
   scanned paths (`base/` and every `env_branch_map` environment directory).
   Prefer a non-scanned tree such as `_retirement_archive/` at the repo root
   (not under `base/` or a mapped env folder). Do **not** leave archived YAML
   inside scanned directories.
2. Commit and push those moves with **`[skip dispatch]`** in the commit message
   on every mapped branch you touch.
3. In the **platform** combined repository, remove Greenfield foundation files
   for this tenant if they exist:
   - `base/organizations/<tenant_id>.yml`
   - `base/teams/<tenant_id>.yml`
4. Commit and push platform removals with **`[skip dispatch]`** as well when
   those commits would otherwise trigger Dispatcher.
5. Confirm Dispatcher/Drift behavior matches your ownership policy (absence does
   **not** delete live AAP objects unless `deletion_supported` and explicit
   `state: absent` apply — see `resource-deletion-capabilities.md`).

## Phase 3 — Remove markers (required before registry removal)

Archived repositories remain readable to the control token, so markers in an archived repo continue to block tenant-registry removal. Markers must be deleted while the repository is still live.

1. On **every** mapped branch in the tenant combined repository (all `env_branch_map` values), delete `.aap-casc-engine/tenant-scaffold.yml`.
2. Commit and push those marker deletions with **`[skip dispatch]`** in the
   commit message (required — the inactive/disabled tenant cannot resolve in
   Dispatcher).
3. Confirm control lifecycle validation can no longer observe a marker for this
   tenant (or confirm via explicit marker reads that every mapped branch returns
   404 for the marker path).
4. Keep the inactive registry entry until that confirmation is complete.
5. Do **not** archive or delete the tenant repository before this phase finishes.

## Phase 4 — Remove registry entry

1. Delete the tenant block from control `tenants.yml`.
2. Merge. Validation must succeed now that markers are gone on every mapped branch.
3. If validation still rejects removal, stop: re-check marker reads on every mapped branch before retrying.

## Phase 5 — Archive the SCM repository

1. Archive (preferred) or delete the tenant combined repository in the tenant SCM org/group **after** registry removal succeeds.
2. Do **not** leave a live repo whose marker no longer matches any registry identity.

## Phase 6 — AAP cleanup (explicit, out-of-band)

1. Inventory live objects in the tenant AAP organization (Gateway org, team, users, RBAC, controller resources).
2. Delete or re-home those objects using approved AAP admin procedures.
3. Remove the Gateway/Controller organization only when empty and approved.

## Phase 7 — Verification

- [ ] Tenant cleanup and marker-removal commits used `[skip dispatch]`
- [ ] Archived YAML is outside `base/` and mapped environment directories
- [ ] Markers removed from every mapped branch before registry deletion
- [ ] Tenant absent from `tenants.yml`
- [ ] Tenant repo archived (or deleted) only after registry removal
- [ ] Platform foundation YAML for the tenant removed
- [ ] No Dispatcher scope still targeting the tenant
- [ ] AAP organization retired or explicitly retained under a new owner
- [ ] Change record / ticket closed with the seven phases above

## Recovery

If retirement is aborted mid-flight, restore the previous control `tenants.yml` commit and re-enable `dispatch_enabled` only after markers and foundation files are consistent again. Prefer Git revert of control/platform/tenant commits over ad-hoc API edits.
