from datetime import datetime, timedelta
import hashlib
import json
import re
from types import SimpleNamespace

from ai_engine import (ACTIONS, CLASSIFICATIONS, INTENTS, LANGUAGES, AnalysisError,
                       analysis_resources, analyze, encoded_image, failure_message, redact)
from ai_knowledge import verified_facts
from ai_locales import has_language_hint, locale_payload
from ai_moderation import moderation_rules
from models import AIAgent, AIDecision, AIExample, AIReplayRun, db

EXPECTED_VALUES = dict(language=LANGUAGES, intent=INTENTS, classification=CLASSIFICATIONS,
                       action=ACTIONS, policy_state=('READY', 'REVIEW', 'IGNORED'))


def approved_examples(agent_id, exclude_id=None, message=None):
    query = AIExample.query.filter_by(agent_id=agent_id, approved=True)
    if exclude_id is not None:
        query = query.filter(AIExample.id != exclude_id)
    rows = query.order_by(AIExample.id.desc()).limit(50).all()
    terms = set(re.findall(r'[^\W\d_]{2,}', (message or '').replace('[PRIVATE]', '').casefold()))
    if terms:
        def relevance(row):
            words = set(re.findall(r'[^\W\d_]{2,}', row.text.replace('[PRIVATE]', '').casefold()))
            return len(terms & words), row.id
        rows.sort(key=relevance, reverse=True)
    rows = rows[:5]
    return [dict(message=row.text[:800], reply_context=row.reply_context[:500], expected=row.expected,
                 reply=row.corrected_reply[:500], moderator_guidance=row.guidance, has_image=bool(row.image)) for row in reversed(rows)]


def validate_approval(example):
    labels = example.expected
    if not all(labels.get(key) in EXPECTED_VALUES[key] for key in ('language', 'intent', 'classification', 'action')):
        raise ValueError('Для исправления задайте язык, тему, категорию и действие')
    if labels['intent'] in ('GAMBLING_INSTRUCTIONS', 'HOW_TO_REGISTER', 'UNKNOWN') and labels['action'] != 'HUMAN_REVIEW':
        raise ValueError('Эта тема требует проверки человеком')
    if labels['action'] == 'DELETE' and labels['classification'] not in ('PERSONAL_DATA', 'PAYMENT_DATA', 'IDENTITY_DOCUMENT', 'SCAM'):
        raise ValueError('Безопасные сообщения не являются примером удаления')
    if labels['action'] == 'BAN':
        raise ValueError('Примеры банов добавляются в отдельной вкладке «Модерация»')
    if redact(example.corrected_reply, 801) != example.corrected_reply:
        raise ValueError('Удалите личные данные и контакты из исправленного ответа')
    if len(example.corrected_reply.encode('utf-16-le')) // 2 > 800:
        raise ValueError('Исправленный ответ: максимум 800 символов UTF-16')
    if re.search(r'https?://|www\.|t\.me/', example.corrected_reply, re.I):
        raise ValueError('Контакты задаются в настройках, а ссылки — в официальных источниках')
    if labels['action'] in ('REPLY', 'WARN') and not example.corrected_reply.strip():
        raise ValueError('Укажите проверенный текст ответа для этого исправления')
    if not example.text.strip():
        raise ValueError('Для примера с изображением добавьте обезличенное описание ситуации')


def invalidate_decisions(agent):
    agent.revision += 1
    AIDecision.query.filter_by(agent_id=agent.id).filter(AIDecision.state.in_(('READY', 'REVIEW', 'OBSERVED'))).update(
        {'state': 'STALE', 'result': 'Подтверждённые примеры изменились. Нужен новый анализ'}, synchronize_session='fetch')


def replay_snapshot(app, agent, example):
    instructions, slang = analysis_resources(agent.language_profile)
    locale = locale_payload(agent)
    version = hashlib.sha256(json.dumps(dict(instructions=instructions, slang=slang, locale=locale),
                                       sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    agent_values = {column.name: getattr(agent, column.name) for column in AIAgent.__table__.columns}
    facts = verified_facts(agent)
    rules = moderation_rules(agent.id)
    hint = has_language_hint(example.text)
    payload = dict(message=example.text,
                   reply_to=dict(text=example.reply_context, is_ai=False) if example.reply_context else None,
                   preferred_language=agent.fallback_language, has_user_language_hint=hint,
                   permissions=dict(allow_delete=agent.allow_delete, allow_ban=agent.allow_ban, support_router=agent.support_router),
                   recent_messages=[], user_history=[], verified_facts=facts, now=datetime.utcnow().isoformat(),
                   **locale, moderation_rules=rules, approved_examples=approved_examples(agent.id, exclude_id=example.id, message=example.text))
    return dict(agent=agent_values, payload=payload, facts=facts, rules=rules, expected=dict(example.expected),
                image=example.image, hint=hint, version=version)


async def evaluate_replay(app, run_id, created_at, snapshot):
    # This path intentionally has no Telegram client or live decision record.
    from ai_assistant import policy
    try:
        agent = SimpleNamespace(**snapshot['agent'])
        db.session.expire_all()
        current = db.session.get(AIAgent, agent.id)
        if not current or not current.consent or current.created_at != agent.created_at or current.revision != agent.revision:
            raise AnalysisError('Настройки или разрешение передачи данных изменились. Тест отменён')
        if snapshot['image'] and not agent.vision:
            raise AnalysisError('Включите Vision для проверки тестового изображения')
        image = encoded_image(snapshot['image']) if snapshot['image'] else None
        result = await analyze(app, snapshot['payload'], image)
        decision = SimpleNamespace(**result, has_image=bool(image))
        if decision.language in ('UNKNOWN', 'MIXED') or (image and not snapshot['hint']):
            decision.language = agent.fallback_language
        policy_state, reason = policy(agent, decision, snapshot['facts'], snapshot['rules'])
        outcome = {key: getattr(decision, key) for key in EXPECTED_VALUES if key != 'policy_state'}
        outcome.update(policy_state=policy_state, confidence=decision.confidence, reply=redact(decision.reply, 1000),
                       reason=redact(decision.reason, 500), policy_reason=reason,
                       mode=agent.mode, would_execute=agent.mode == 'AUTO' and policy_state == 'READY',
                       telegram_checked=False)
        mismatches = {key: dict(expected=value, actual=outcome[key]) for key, value in snapshot['expected'].items()
                      if value != outcome[key]}
        outcome['mismatches'] = mismatches
        state = 'FAIL' if mismatches else 'PASS'
        summary = 'Ожидаемые метки совпали' if state == 'PASS' else 'Есть расхождения с ожидаемыми метками'
    except Exception as exc:
        state, summary, outcome = 'ERROR', failure_message(exc, 'Тестовый анализ'), {}
    db.session.expire_all()
    run = db.session.get(AIReplayRun, run_id)
    if not run or run.created_at.isoformat() != created_at or run.state != 'RUNNING':
        return
    example = db.session.get(AIExample, run.example_id)
    current = db.session.get(AIAgent, example.agent_id) if example else None
    if not current or current.revision != run.agent_revision or current.knowledge_revision != run.knowledge_revision:
        state, summary = 'STALE', 'Настройки или источники изменились. Результат неактуален'
    run.state, run.result, run.outcome, run.completed_at = state, summary, outcome, datetime.utcnow()
    if example:
        example.running = False
    db.session.commit()


def check_replay_limit(agent_id):
    count = AIReplayRun.query.join(AIExample).filter(AIExample.agent_id == agent_id,
        AIReplayRun.created_at >= datetime.utcnow() - timedelta(minutes=1)).count()
    if count >= 12:
        raise ValueError('Не больше 12 тестовых запросов в минуту на ассистента')
