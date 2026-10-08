from flask import flash, redirect, request, url_for

from ai_engine import redact
from ai_examples import EXPECTED_VALUES, invalidate_decisions, validate_approval
from models import AIAgent, AIDecision, AIExample, AIMessage, AIModerationRule, db
from web.routes.assistant import assistant_bp, fail
from web.routes.assistant_examples import ensure_agent


def training_context(agent, tab):
    pending = AIDecision.query.filter_by(agent_id=agent.id, training_needed=True, training_status='OPEN').filter(
        ~AIDecision.state.in_(('RUNNING', 'SENT', 'DELETED', 'BANNED', 'UNCERTAIN'))).order_by(AIDecision.id.desc()).limit(100).all()
    message_ids = [row.message_id for row in pending]
    messages = AIMessage.query.filter_by(agent_id=agent.id).filter(AIMessage.message_id.in_(message_ids)).all() if message_ids else []
    return dict(training_queue=pending, training_messages={row.message_id: row.text for row in messages},
                trained_examples=AIExample.query.filter_by(agent_id=agent.id, kind='training').order_by(AIExample.id.desc()).limit(50).all(),
                moderation_rules=AIModerationRule.query.filter_by(agent_id=agent.id).order_by(AIModerationRule.id.desc()).all())


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
        if decision.state in ('RUNNING', 'SENT', 'DELETED', 'BANNED', 'UNCERTAIN'):
            raise ValueError('Обработка этого сообщения уже завершена')
        example = None
        if action == 'teach':
            if request.form.get('anonymized') != 'on':
                raise ValueError('Подтвердите обезличивание сообщения и пояснения')
            if AIExample.query.filter_by(agent_id=agent.id).count() >= 50:
                raise ValueError('Не больше 50 примеров. Удалите ненужные')
            expected = {}
            for key in ('language', 'intent', 'classification', 'action'):
                value = request.form.get('expected_' + key, '')
                if value not in EXPECTED_VALUES[key]:
                    raise ValueError('Укажите язык, тему, категорию и действие')
                expected[key] = value
            example = AIExample(agent_id=agent.id, title=f'Тренировка #{decision.message_id}', kind='training',
                text=field('text', 2000), guidance=field('guidance', 600),
                corrected_reply=field('corrected_reply', 800, False), expected=expected,
                source_decision_id=decision.id, approved=True)
            validate_approval(example)
        claimed = AIDecision.query.filter_by(id=decision.id, agent_id=agent.id, created_at=decision.created_at,
                                            training_status='OPEN', training_needed=True).filter(
            ~AIDecision.state.in_(('RUNNING', 'SENT', 'DELETED', 'BANNED', 'UNCERTAIN'))).update(
                {'training_status': 'DONE' if example else 'DISMISSED'}, synchronize_session='fetch')
        if not claimed:
            raise ValueError('Этот вопрос уже обработан или заменён новым сообщением')
        if example:
            invalidate_decisions(agent)
            db.session.add(example)
        db.session.commit()
        flash('Пояснение сохранено для этого чата. Никаких действий в Telegram не выполнялось.' if example else 'Вопрос пропущен', 'success')
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
            if action not in ('DELETE', 'BAN'):
                raise ValueError('Действие правила: удаление или бан')
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
