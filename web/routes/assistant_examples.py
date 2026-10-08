import asyncio
import secrets

from flask import abort, current_app, flash, redirect, request, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import HTTPException

from ai_engine import normalize_image, redact
from ai_examples import (EXPECTED_VALUES, check_replay_limit, evaluate_replay, invalidate_decisions,
                         replay_snapshot, validate_approval)
from ai_locales import LANGUAGE_LABELS
from models import AIAgent, AIDecision, AIExample, AIMessage, AIReplayRun, db
from web.routes.assistant import assistant_bp, fail


def signer():
    return URLSafeTimedSerializer(current_app.secret_key, salt='assistant-replay')


@assistant_bp.context_processor
def language_context():
    return dict(language_labels=LANGUAGE_LABELS, expected_values=EXPECTED_VALUES)


def example_context(agent):
    examples = AIExample.query.filter_by(agent_id=agent.id).order_by(AIExample.id.desc()).limit(50).all()
    prefill = dict(title='', text='', reply_context='', expected={}, corrected_reply='')
    source = None
    if request.args.get('correct', '').isdigit():
        source = AIDecision.query.filter_by(id=int(request.args['correct']), agent_id=agent.id).first()
        if source:
            record = AIMessage.query.filter_by(agent_id=agent.id, message_id=source.message_id).first()
            prefill.update(title=f'Исправление #{source.message_id}',
                           text=record.text if record and record.text != '[PRIVATE MESSAGE]' else '',
                           expected={key: getattr(source, key) for key in ('language', 'intent', 'classification', 'action')},
                           corrected_reply=redact(source.reply, 800))
    tokens = {row.id: signer().dumps([row.id, row.created_at.isoformat(), secrets.token_hex(16)]) for row in examples}
    return dict(examples=examples, prefill=prefill, correction_source=source, replay_tokens=tokens)


def ensure_agent(agent):
    if request.form.get('agent_created_at') != agent.created_at.isoformat():
        raise ValueError('Назначение изменилось. Обновите страницу')


def target(agent_id, example_id):
    agent = AIAgent.query.get_or_404(agent_id)
    example = AIExample.query.filter_by(id=example_id, agent_id=agent_id).first_or_404()
    ensure_agent(agent)
    if request.form.get('example_created_at') != example.created_at.isoformat():
        raise ValueError('Пример изменился. Обновите страницу')
    return agent, example


def back(agent_id):
    return redirect(url_for('assistant.edit', agent_id=agent_id, tab='examples'))


@assistant_bp.post('/<int:agent_id>/examples/add')
def add_example(agent_id):
    agent = AIAgent.query.get_or_404(agent_id)
    try:
        ensure_agent(agent)
        if request.form.get('anonymized') != 'on':
            raise ValueError('Подтвердите обезличивание текста и изображения')
        if AIExample.query.filter_by(agent_id=agent_id).count() >= 50:
            raise ValueError('Не больше 50 примеров на ассистента. Удалите ненужные')
        values = {}
        for field, limit in (('title', 100), ('text', 2000), ('reply_context', 1500), ('corrected_reply', 800), ('guidance', 600)):
            value = request.form.get(field, '').strip()
            if len(value) > limit:
                raise ValueError(f'{field}: максимум {limit} символов')
            values[field] = redact(value, limit)
        if not values['title']:
            raise ValueError('Укажите название примера')
        expected = {}
        for key, choices in EXPECTED_VALUES.items():
            value = request.form.get('expected_' + key, '')
            if value:
                if value not in choices:
                    raise ValueError('Неизвестная ожидаемая метка')
                expected[key] = value
        if not expected:
            raise ValueError('Задайте хотя бы одну ожидаемую метку')
        image = None
        upload = request.files.get('image')
        if upload and upload.filename:
            image = normalize_image(upload.stream.read(10 * 1024 * 1024 + 1))
            if len(image) > 2 * 1024 * 1024:
                raise ValueError('После обработки изображение больше 2 МБ. Уменьшите его')
        if not image and not values['text']:
            raise ValueError('Добавьте текст или обезличенное изображение')
        source_id = None
        if request.form.get('source_decision_id'):
            source = AIDecision.query.filter_by(id=int(request.form['source_decision_id']), agent_id=agent_id).first()
            if not source or source.created_at.isoformat() != request.form.get('source_created_at'):
                raise ValueError('Исходное решение изменилось. Обновите страницу')
            source_id = source.id
        example = AIExample(agent_id=agent_id, image=image, expected=expected, source_decision_id=source_id, **values)
        if request.form.get('approved') == 'on':
            validate_approval(example)
            example.approved = True
            invalidate_decisions(agent)
        db.session.add(example)
        db.session.commit()
        flash('Пример сохранён. Действия в Telegram не выполнялись.', 'success')
    except Exception as exc:
        fail(exc)
    return back(agent_id)


@assistant_bp.post('/<int:agent_id>/examples/<int:example_id>/approval')
def approve_example(agent_id, example_id):
    try:
        agent, example = target(agent_id, example_id)
        if example.running:
            raise ValueError('Дождитесь завершения теста')
        approved = request.form.get('approved') == 'on'
        if approved:
            validate_approval(example)
        if example.approved != approved:
            if not AIExample.query.filter_by(id=example.id, created_at=example.created_at, running=False).update(
                    {'approved': approved}, synchronize_session='fetch'):
                raise ValueError('Пример уже проверяется или удалён')
            invalidate_decisions(agent)
        db.session.commit()
        flash('Подтверждение примера обновлено', 'success')
    except Exception as exc:
        fail(exc)
    return back(agent_id)


@assistant_bp.post('/<int:agent_id>/examples/<int:example_id>/delete')
def delete_example(agent_id, example_id):
    try:
        agent, example = target(agent_id, example_id)
        if request.form.get('confirm_delete') != 'on':
            raise ValueError('Подтвердите удаление примера и его прогонов')
        if example.running:
            raise ValueError('Дождитесь завершения теста')
        if example.approved:
            invalidate_decisions(agent)
        AIReplayRun.query.filter_by(example_id=example.id).delete(synchronize_session='fetch')
        if not AIExample.query.filter_by(id=example.id, created_at=example.created_at, running=False).delete(synchronize_session='fetch'):
            raise ValueError('Пример уже проверяется или удалён')
        db.session.commit()
        flash('Пример и его прогоны удалены', 'success')
    except Exception as exc:
        fail(exc)
    return back(agent_id)


@assistant_bp.post('/<int:agent_id>/examples/<int:example_id>/run')
def run_example(agent_id, example_id):
    try:
        agent, example = target(agent_id, example_id)
        token = request.form.get('request_token', '')
        try:
            signed = signer().loads(token, max_age=900)
            if not isinstance(signed, list) or len(signed) != 3 or signed[:2] != [example.id, example.created_at.isoformat()]:
                raise BadSignature('Invalid example')
        except BadSignature:
            abort(400)
        request_token = signed[2]
        existing = AIReplayRun.query.filter_by(example_id=example_id, request_token=request_token).first()
        if existing:
            flash('Этот запрос уже зарегистрирован. Повторный вызов AI не выполнялся.', 'info')
            return back(agent_id)
        if request.form.get('confirm_ai') != 'on':
            raise ValueError('Подтвердите передачу обезличенного примера в OpenAI и платный запрос')
        if not agent.consent or not current_app.config.get('OPENAI_API_KEY'):
            raise ValueError('Нужны разрешение передачи данных и OPENAI_API_KEY')
        check_replay_limit(agent_id)
        snapshot = replay_snapshot(current_app, agent, example)
        current_agent = db.session.query(AIAgent.id).filter_by(id=agent.id, created_at=agent.created_at,
                            revision=snapshot['agent']['revision'], consent=True)
        claimed = AIExample.query.filter_by(id=example.id, created_at=example.created_at, running=False).filter(
            AIExample.agent_id.in_(current_agent)).update(
            {'running': True}, synchronize_session='fetch')
        if not claimed:
            raise ValueError('Пример уже проверяется или настройки изменились')
        replay = AIReplayRun(example_id=example.id, request_token=request_token, agent_revision=agent.revision,
                             knowledge_revision=agent.knowledge_revision, prompt_version=snapshot['version'],
                             model=current_app.config['OPENAI_MODEL'])
        db.session.add(replay)
        db.session.flush()
        run_id, created_at = replay.id, replay.created_at.isoformat()
        db.session.commit()
        asyncio.run(evaluate_replay(current_app._get_current_object(), run_id, created_at, snapshot))
        flash('Тест завершён. Telegram не использовался; права и паузы отправки не проверялись.', 'info')
    except HTTPException:
        raise
    except IntegrityError:
        db.session.rollback()
        flash('Повторный запуск не выполнен', 'info')
    except Exception as exc:
        fail(exc)
    return back(agent_id)
