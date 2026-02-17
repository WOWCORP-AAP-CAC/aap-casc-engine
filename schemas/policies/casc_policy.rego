# OPA Policy for AAP CasC Governance
#
# This policy enforces organizational governance rules on CasC JSON configs.
# Extend with additional rules as needed for your organization.

package casc

# Deny organizations without a description
deny[msg] {
    input.aap_organizations[i]
    not input.aap_organizations[i].description
    msg := sprintf("Organization '%s' must have a description", [input.aap_organizations[i].name])
}

# Deny projects without SCM URL
deny[msg] {
    input.controller_projects[i]
    input.controller_projects[i].scm_type == "git"
    not input.controller_projects[i].scm_url
    msg := sprintf("Git project '%s' must have scm_url", [input.controller_projects[i].name])
}

# Deny job templates without an inventory
deny[msg] {
    input.controller_templates[i]
    not input.controller_templates[i].inventory
    not input.controller_templates[i].ask_inventory_on_launch
    msg := sprintf("Job template '%s' must have an inventory or enable ask_inventory_on_launch", [input.controller_templates[i].name])
}

# Deny credentials without organization
deny[msg] {
    input.controller_credentials[i]
    not input.controller_credentials[i].organization
    msg := sprintf("Credential '%s' must have an organization", [input.controller_credentials[i].name])
}

# Warn on job templates with verbosity > 2 (noisy in production)
warn[msg] {
    input.controller_templates[i]
    input.controller_templates[i].verbosity > 2
    msg := sprintf("Job template '%s' has verbosity %d (consider reducing for production)", [input.controller_templates[i].name, input.controller_templates[i].verbosity])
}
