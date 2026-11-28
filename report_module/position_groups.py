"""Shared position families for player comparisons."""

POSITION_GROUPS = {
    'central_midfield': ('CDM', 'CM', 'CAM'),
    'full_back': ('LB', 'RB'),
    'centre_back': ('CB',),
    'goalkeeper': ('GK',),
    'centre_forward': ('CF',),
    'wide': ('LM', 'RM', 'LW', 'RW'),
}
ROLE_GROUP = {role: group for group, roles in POSITION_GROUPS.items() for role in roles}


def comparison_groups(roles):
    """Map already eligible top-two roles to unique groups, in usage order.

    Eligibility is decided BEFORE grouping. Two roles in one family cannot
    promote an otherwise ineligible secondary role. Unspecified roles retain
    their own benchmark rather than silently joining an unrelated family.
    """
    groups = {}
    for role, share in roles.items():
        group = ROLE_GROUP.get(role, role)
        entry = groups.setdefault(group, {'roles': list(POSITION_GROUPS.get(group, (role,))), 'playerRoles': {}})
        entry['playerRoles'][role] = share
    return groups

def comparison_role_groups(groups):
    return [list(POSITION_GROUPS.get(group, (group,))) for group in groups]
