from models import AIModerationRule


def moderation_rules(agent_id):
    if agent_id is None:
        return []
    rows = AIModerationRule.query.filter_by(agent_id=agent_id, enabled=True).order_by(AIModerationRule.id).limit(20).all()
    return [dict(id=row.id, title=row.title, example=row.example, guidance=row.guidance, action=row.action) for row in rows]


def matched_rules(decision, rules, action):
    ids = getattr(decision, 'rule_ids', None) or []
    available = {rule['id']: rule for rule in rules}
    return bool(ids and all(type(value) is int and value in available and available[value]['action'] == action for value in ids))
