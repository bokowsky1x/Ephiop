from flask import current_app, flash, redirect, request, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer

from ai_engine import redact
from ai_examples import EXPECTED_VALUES, invalidate_decisions, validate_approval
from ai_training import FINISHED, question_groups
from models import AIAgent, AIDecision, AIExample, AIMessage, AIModerationRule, db
from web.routes.assistant import assistant_bp, fail
from web.routes.assistant_examples import ensure_agent


def training_context(agent, tab):
    pending = AIDecision.query.filter_by(agent_id=agent.id, training_needed=True, training_status='OPEN').filter(
        ~AIDecision.state.in_(FINISHED)).order_by(AIDecision.id.desc()).limit(100).all()
    message_ids = [row.message_id for row in pending]
    messages = AIMessage.query.filter_by(agent_id=agent.id).filter(AIMessage.message_id.in_(message_ids)).all() if message_ids else []
    groups = question_groups(pending, {row.message_id: row for row in messages}, agent.semantic_memory)
    tokens = {group[0].id: group_signer().dumps(dict(agent=[agent.id, agent.created_at.isoformat(), agent.revision],
        members=[decision_stamp(row) for row in group])) for group in groups if len(group) > 1}
    return dict(training_queue=pending, training_groups=groups, training_group_tokens=tokens,
                training_messages={row.message_id: row.text for row in messages},
                trained_examples=AIExample.query.filter_by(agent_id=agent.id, kind='training').order_by(AIExample.id.desc()).limit(50).all(),
                moderation_rules=AIModerationRule.query.filter_by(agent_id=agent.id).order_by(AIModerationRule.id.desc()).all())


def group_signer():
    return URLSafeTimedSerializer(current_app.secret_key, salt='assistant-training-group')


def decision_stamp(row):
    return [row.id, row.created_at.isoformat(), row.fingerprint, row.state]


def selected_questions(agent, decision):
    ids = request.form.getlist('group_members')
    if not ids:
        return [decision]
    if len(ids) > 99 or len(set(ids)) != len(ids) or any(not value.isdigit() for value in ids):
        raise ValueError('Некорректный выбор вопросов')
    try:
        signed = group_signer().loads(request.form.get('group_token', ''), max_age=900)
        if signed['agent'] != [agent.id, agent.created_at.isoformat(), agent.revision] or signed['members'][0] != decision_stamp(decision):
            raise ValueError
        stamps = {item[0]: item for item in signed['members'][1:]}
        rows = [decision]
        for value in ids:
            row = AIDecision.query.filter_by(id=int(value), agent_id=agent.id, training_needed=True, training_status='OPEN').first()
            if not row or stamps.get(row.id) != decision_stamp(row) or row.state in FINISHED:
                raise ValueError
            rows.append(row)
        return rows
    except (BadSignature, ValueError, KeyError, TypeError, IndexError):
        raise ValueError('Группа вопросов изменилась или устарела. Обновите страницу') from None


def ensure_editable(agent):
    ensure_agent(agent)
    if AIDecision.query.filter_by(agent_id=agent.id, state='RUNNING').first():
        raise ValueError('Дождитесь завершения текущего действия в Telegram')


def field(name, limit, required=True):
    value = request.form.get(name, '').strip()
    if (required and not value) or len(value) > limit:
        raise ValueError(f'{name}: укажите текст длиной до {limit} символов')
    return redact(value, limit)


@assistant_bp.post('/<int:agent_id>/training/<int:decision_id>')
def teach(agent_id, decision_id):
    agent = AIAgent.query.get_or_404(agent_id)
    try:
        ensure_editable(agent)
        decision = AIDecision.query.filter_by(id=decision_id, agent_id=agent.id).first_or_404()
        if request.form.get('decision_created_at') != decision.created_at.isoformat():
            raise ValueError('Исходное сообщение изменилось. Обновите страницу')
        action = request.form.get('training_action')
        if action not in ('teach', 'dismiss'):
            raise ValueError('Выберите сохранение пояснения или пропуск')
        if decision.state in FINISHED:
            raise ValueError('Обработка этого сообщения уже завершена')
        selected = selected_questions(agent, decision)
        artifact = None
        is_rule = False
        if action == 'teach':
            if request.form.get('anonymized') != 'on':
                raise ValueError('Подтвердите обезличивание сообщения и пояснения')
            expected = {}
            for key in ('language', 'intent', 'classification', 'action'):
                value = request.form.get('expected_' + key, '')
                if value not in EXPECTED_VALUES[key]:
                    raise ValueError('Укажите язык, тему, категорию и действие')
                expected[key] = value
            is_rule = expected['action'] in ('BAN', 'DELETE_BAN') or (expected['action'] == 'DELETE' and expected['classification'] == 'RULE_VIOLATION')
            if is_rule:
                if expected['classification'] != 'RULE_VIOLATION' or expected['intent'] not in ('SPAM', 'COMMUNITY_RULE_VIOLATION'):
                    raise ValueError('Для правила удаления/бана выберите нарушение правила сообщества и ситуацию «Спам» или «Нарушение правила сообщества»')
                if request.form.get('confirm_rule') != 'on':
                    raise ValueError('Подтвердите сохранение этого примера как правила модерации')
                if AIModerationRule.query.filter_by(agent_id=agent.id).count() >= 20:
                    raise ValueError('Не больше 20 правил на чат')
                artifact = AIModerationRule(agent_id=agent.id, title=f'Нарушение из сообщения #{decision.message_id}',
                    example=field('text', 800), guidance=field('guidance', 600), action=expected['action'],
                    enabled=request.form.get('rule_enabled') == 'on')
            else:
                if AIExample.query.filter_by(agent_id=agent.id).count() >= 50:
                    raise ValueError('Не больше 50 примеров. Удалите ненужные')
                artifact = AIExample(agent_id=agent.id, title=f'Тренировка #{decision.message_id}' + (f' ({len(selected)} вопросов)' if len(selected) > 1 else ''), kind='training',
                    text=field('text', 2000), guidance=field('guidance', 600),
                    corrected_reply=field('corrected_reply', 800, False), expected=expected,
                    source_decision_id=decision.id, approved=True)
                validate_approval(artifact)
        for row in selected:
            claimed = AIDecision.query.filter_by(id=row.id, agent_id=agent.id, created_at=row.created_at,
                fingerprint=row.fingerprint, state=row.state, training_status='OPEN', training_needed=True).filter(
                ~AIDecision.state.in_(FINISHED)).update(
                    {'training_status': 'DONE' if artifact else 'DISMISSED'}, synchronize_session='fetch')
            if not claimed:
                raise ValueError('Этот вопрос уже обработан или заменён новым сообщением')
        if artifact:
            invalidate_decisions(agent)
            db.session.add(artifact)
        db.session.commit()
        flash(('Правило сохранено во вкладке «Модерация». Разрешения удаления и бана не изменялись. '
               if is_rule else 'Пояснение сохранено для этого чата. ') +
              f'Обработано вопросов: {len(selected)}. Никаких действий в Telegram не выполнялось.'
              if artifact else f'Пропущено вопросов: {len(selected)}', 'success')
    except Exception as exc:
        fail(exc)
    return redirect(url_for('assistant.edit', agent_id=agent_id, tab='training'))


@assistant_bp.post('/<int:agent_id>/moderation/add')
@assistant_bp.post('/<int:agent_id>/moderation/<int:rule_id>')
def save_rule(agent_id, rule_id=None):
    agent = AIAgent.query.get_or_404(agent_id)
    try:
        ensure_editable(agent)
        rule = AIModerationRule.query.filter_by(id=rule_id, agent_id=agent.id).first_or_404() if rule_id else None
        if rule and request.form.get('rule_created_at') != rule.created_at.isoformat():
            raise ValueError('Правило изменилось. Обновите страницу')
        if request.form.get('operation') == 'delete':
            if not rule or request.form.get('confirm_delete') != 'on':
                raise ValueError('Подтвердите удаление правила')
            db.session.delete(rule)
        else:
            if request.form.get('anonymized') != 'on':
                raise ValueError('Подтвердите обезличивание примера')
            action = request.form.get('action', '')
            if action not in ('DELETE', 'BAN', 'DELETE_BAN'):
                raise ValueError('Действие правила: удаление, бан или удаление и бан')
            if not rule:
                if AIModerationRule.query.filter_by(agent_id=agent.id).count() >= 20:
                    raise ValueError('Не больше 20 правил на чат')
                rule = AIModerationRule(agent_id=agent.id)
                db.session.add(rule)
            rule.title, rule.example, rule.guidance = field('title', 100), field('example', 800), field('guidance', 600)
            rule.action, rule.enabled = action, request.form.get('enabled') == 'on'
        invalidate_decisions(agent)
        db.session.commit()
        flash('Правила обновлены. Разрешения удаления и бана не изменялись.', 'success')
    except Exception as exc:
        fail(exc)
    return redirect(url_for('assistant.edit', agent_id=agent_id, tab='moderation'))
