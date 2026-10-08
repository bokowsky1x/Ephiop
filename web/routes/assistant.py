from datetime import datetime
import math
import secrets

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError

from ai_engine import redact
from ai_health import checks, telegram_checks
from ai_knowledge import STATUSES, archive, import_site, ingest, source_url_allowed, telegram_source
from models import Account, AIAgent, AIDecision, AIFact, AIFactHistory, AIMessage, AIUserContext, db
from web.routes.profile import run

assistant_bp = Blueprint('assistant', __name__)
MODES = ('OFF', 'OBSERVE', 'ASSIST', 'AUTO')
FLAGS = ('consent', 'community_replies', 'support_router', 'information', 'vision', 'scam_detection', 'allow_delete')
DELETE_FLAGS = ('delete_personal_data', 'delete_payment_data', 'delete_identity_documents')


def csrf():
    token = session.setdefault('assistant_csrf', secrets.token_urlsafe(32))
    if request.method == 'POST' and not secrets.compare_digest(request.form.get('csrf_token', '').encode('utf-8'), token.encode('utf-8')):
        abort(400)
    return token


@assistant_bp.before_request
def protect():
    csrf()


@assistant_bp.after_request
def no_store(response):
    response.headers['Cache-Control'] = 'no-store'
    return response


def fail(exc):
    db.session.rollback()
    flash(str(exc) if isinstance(exc, ValueError) else 'Не удалось выполнить запрос. Действие не повторялось; проверьте журнал.', 'danger')


@assistant_bp.get('/')
def index():
    agents = AIAgent.query.order_by(AIAgent.id).all()
    return render_template('assistant_index.html', agents=agents, csrf_token=csrf(), modes=MODES,
                           accounts=Account.query.filter_by(status='authorized').all(),
                           connected_ids=current_app.telegram_manager.connected_accounts if current_app.telegram_manager else [])


def settings(agent):
    data = request.form
    name, mode = data.get('name', '').strip(), data.get('mode', '')
    if not name or len(name) > 100 or mode not in MODES:
        raise ValueError('Укажите название и режим работы')
    account = db.session.get(Account, int(data.get('account_id', '0')))
    if not account or account.status != 'authorized':
        raise ValueError('Выберите авторизованный аккаунт')
    chat_id = str(int(data.get('chat_id', '0')))
    channel_id = data.get('channel_id', '').strip()
    if int(chat_id) >= 0 or len(chat_id) > 20:
        raise ValueError('Укажите отрицательный числовой ID группы, например -100…')
    if agent.id and agent.chat_id != chat_id:
        raise ValueError('Для другого чата создайте отдельного ассистента')
    if channel_id:
        channel_id = str(int(channel_id))
        if not channel_id.startswith('-100') or len(channel_id) > 20 or channel_id == chat_id:
            raise ValueError('Укажите отдельный официальный канал с ID -100…')
    flags = {flag: data.get(flag) == 'on' for flag in FLAGS}
    flags.update({flag: data.get(flag) == 'on' for flag in DELETE_FLAGS})
    language = data.get('fallback_language', agent.fallback_language or 'AMHARIC')
    if language not in ('AMHARIC', 'AMHARIC_LATIN', 'OROMO', 'ENGLISH'):
        raise ValueError('Выберите язык ответов без подписи')
    if mode == 'AUTO' and data.get('auto_confirm') != 'on':
        raise ValueError('Подтвердите автоматические действия для режима AUTO')
    if mode != 'OFF' and (not flags['consent'] or not current_app.config.get('OPENAI_API_KEY')):
        raise ValueError('Для анализа нужны OPENAI_API_KEY и разрешение передачи данных в OpenAI')
    numbers = {}
    for field in ('reply_confidence', 'moderation_confidence', 'vision_confidence'):
        value = float(data.get(field, '0'))
        if not math.isfinite(value) or not 0.5 <= value <= 1:
            raise ValueError('Порог уверенности должен быть от 0.5 до 1')
        numbers[field] = value
    for field in ('chat_cooldown', 'user_cooldown', 'intent_cooldown'):
        value = int(data.get(field, '0'))
        if not 10 <= value <= 86400:
            raise ValueError('Пауза между ответами: от 10 до 86400 секунд')
        numbers[field] = value
    age = int(data.get('fact_max_age_hours', '72'))
    if not 1 <= age <= 168:
        raise ValueError('Свежесть источника: от 1 до 168 часов')
    if agent.id and (agent.channel_id != channel_id or agent.account_id != account.id or (agent.mode == 'OFF' and mode != 'OFF')):
        for fact in AIFact.query.filter_by(agent_id=agent.id).all():
            archive(fact)
            fact.approved = False
        agent.knowledge_revision += 1
    agent.name, agent.mode, agent.chat_id, agent.channel_id, agent.account_id = name, mode, chat_id, channel_id, account.id
    agent.fallback_language = language
    for key, value in {**flags, **numbers, 'fact_max_age_hours': age}.items():
        setattr(agent, key, value)
    agent.revision = (agent.revision or 0) + 1


@assistant_bp.post('/add')
def add():
    agent = AIAgent()
    try:
        settings(agent)
        db.session.add(agent)
        db.session.commit()
        flash('Ассистент назначен. Подключение аккаунта и членство в чате не менялись.', 'success')
        return redirect(url_for('assistant.edit', agent_id=agent.id))
    except IntegrityError:
        fail(ValueError('Для этого чата ассистент уже назначен'))
    except Exception as exc:
        fail(exc)
    return redirect(url_for('assistant.index'))


@assistant_bp.route('/<int:agent_id>', methods=['GET', 'POST'])
def edit(agent_id):
    agent = AIAgent.query.get_or_404(agent_id)
    if request.method == 'POST':
        try:
            if AIDecision.query.filter_by(agent_id=agent.id, state='RUNNING').first():
                raise ValueError('Дождитесь завершения текущего действия')
            settings(agent)
            AIDecision.query.filter_by(agent_id=agent.id).filter(AIDecision.state.in_(('READY', 'REVIEW'))).update({'state': 'STALE'}, synchronize_session='fetch')
            db.session.commit()
            flash('Настройки сохранены', 'success')
            return redirect(url_for('assistant.edit', agent_id=agent.id))
        except Exception as exc:
            fail(exc)
    tab = request.args.get('tab', 'journal')
    if tab not in ('journal', 'review', 'knowledge', 'deleted', 'scam'):
        tab = 'journal'
    query = AIDecision.query.filter_by(agent_id=agent.id)
    if tab == 'review':
        query = query.filter_by(state='REVIEW')
    elif tab == 'deleted':
        query = query.filter_by(state='DELETED')
    elif tab == 'scam':
        query = query.filter(AIDecision.intent.in_(('SCAM', 'FAKE_AGENT', 'FAKE_SUPPORT')))
    stats = db.session.query(AIDecision.intent, db.func.count(AIDecision.id)).filter_by(agent_id=agent.id).group_by(AIDecision.intent).all()
    return render_template('assistant_edit.html', agent=agent, modes=MODES, csrf_token=csrf(), tab=tab,
        accounts=Account.query.filter_by(status='authorized').all(),
        decisions=query.order_by(AIDecision.id.desc()).limit(100).all(),
        facts=AIFact.query.filter_by(agent_id=agent.id).order_by(AIFact.id.desc()).limit(100).all(),
        stats=stats, connected=bool(current_app.telegram_manager and agent.account_id in current_app.telegram_manager.connected_accounts),
        health=checks(current_app, agent, session.get('assistant_checks', {}).get(str(agent.id))))


@assistant_bp.post('/<int:agent_id>/stop')
def stop(agent_id):
    agent = AIAgent.query.get_or_404(agent_id)
    agent.mode, agent.revision = 'OFF', agent.revision + 1
    db.session.commit()
    flash('Ассистент выключен. Уже отправленный запрос Telegram нельзя отменить.', 'warning')
    return redirect(url_for('assistant.edit', agent_id=agent.id))


@assistant_bp.post('/<int:agent_id>/check')
def check(agent_id):
    agent = AIAgent.query.get_or_404(agent_id)
    try:
        manager = current_app.telegram_manager
        client = manager.clients.get(agent.account_id) if manager else None
        if not client or not manager.client_connected(client):
            raise ValueError('Аккаунт не подключён к Telegram')
        revision, account_id = agent.revision, agent.account_id
        rows = run(telegram_checks(client, agent.chat_id, agent.channel_id, agent.allow_delete), timeout=25)
        db.session.expire_all()
        if agent.revision != revision or agent.account_id != account_id:
            raise ValueError('Настройки изменились во время проверки. Запустите проверку снова')
        saved = dict(session.get('assistant_checks', {}))
        saved[str(agent.id)] = dict(revision=revision, account_id=account_id, created_at=agent.created_at.isoformat(),
                                   at=datetime.utcnow().isoformat(), rows=rows)
        session['assistant_checks'] = dict(list(saved.items())[-3:])
        flash('Проверка Telegram завершена. Сообщения не отправлялись и не удалялись.', 'info')
    except Exception as exc:
        saved = dict(session.get('assistant_checks', {}))
        saved.pop(str(agent_id), None)
        session['assistant_checks'] = saved
        fail(exc)
    return redirect(url_for('assistant.edit', agent_id=agent_id))


def delete_assignment(agent_id, created_at=None):
    agent = db.session.get(AIAgent, agent_id)
    if not agent:
        raise ValueError('Назначение уже удалено')
    if created_at is not None and agent.created_at.isoformat() != created_at:
        raise ValueError('Назначение изменилось. Обновите страницу')
    if agent.mode != 'OFF':
        raise ValueError('Сначала выключите ассистента')
    if AIDecision.query.filter_by(agent_id=agent_id).filter(AIDecision.state.in_(('ANALYZING', 'RUNNING'))).first():
        raise ValueError('Дождитесь завершения текущего анализа или действия')
    revision = agent.revision
    fact_ids = db.session.query(AIFact.id).filter_by(agent_id=agent_id)
    AIFactHistory.query.filter(AIFactHistory.fact_id.in_(fact_ids)).delete(synchronize_session='fetch')
    AIFact.query.filter_by(agent_id=agent_id).update({'related_id': None}, synchronize_session='fetch')
    for model in (AIDecision, AIMessage, AIUserContext, AIFact):
        model.query.filter_by(agent_id=agent_id).delete(synchronize_session='fetch')
    if not AIAgent.query.filter_by(id=agent_id, revision=revision, mode='OFF').delete(synchronize_session='fetch'):
        raise ValueError('Настройки изменились. Удаление отменено')
    db.session.commit()


@assistant_bp.post('/<int:agent_id>/delete')
def delete(agent_id):
    agent = AIAgent.query.get_or_404(agent_id)
    try:
        if request.form.get('confirm_delete') != 'on':
            raise ValueError('Подтвердите удаление назначения и его данных')
        created_at = request.form.get('agent_created_at')
        if created_at != agent.created_at.isoformat():
            raise ValueError('Назначение изменилось. Обновите страницу')
        manager = current_app.telegram_manager
        if manager and manager.loop is not None:
            app = current_app._get_current_object()
            async def remove():
                async with manager.assistant().locks[agent_id]:
                    with app.app_context():
                        try:
                            delete_assignment(agent_id, created_at)
                        except Exception:
                            db.session.rollback()
                            raise
            run(remove())
        else:
            delete_assignment(agent_id, created_at)
        saved = dict(session.get('assistant_checks', {}))
        saved.pop(str(agent_id), None)
        session['assistant_checks'] = saved
        flash('Назначение ассистента удалено. Telegram-аккаунт и сообщения в чате сохранены.', 'success')
        return redirect(url_for('assistant.index'))
    except Exception as exc:
        fail(exc)
    return redirect(url_for('assistant.edit', agent_id=agent_id))


@assistant_bp.post('/decisions/<int:decision_id>')
def review(decision_id):
    decision = AIDecision.query.get_or_404(decision_id)
    agent_id = decision.agent_id
    action = request.form.get('action')
    try:
        created_at = request.form.get('decision_created_at')
        if created_at != decision.created_at.isoformat():
            raise ValueError('Решение изменилось. Обновите страницу')
        if action == 'dismiss':
            AIDecision.query.filter_by(id=decision.id).filter(AIDecision.state.in_(('REVIEW', 'OBSERVED', 'READY'))).update(
                {'state': 'DISMISSED', 'result': 'Отклонено модератором'}, synchronize_session='fetch')
            db.session.commit()
        elif action in ('REPLY', 'DELETE'):
            if request.form.get('confirm') != 'on':
                raise ValueError('Подтвердите действие в Telegram')
            manager = current_app.telegram_manager
            if not manager:
                raise ValueError('Telegram не запущен')
            # Release the route's snapshot before the Telegram loop writes the decision.
            db.session.commit()
            state = run(manager.assistant().execute(decision.id, action, created_at=created_at), timeout=65)
            flash('Результат: ' + state, 'success' if state in ('SENT', 'DELETED') else 'warning')
        else:
            raise ValueError('Неизвестное действие')
    except Exception as exc:
        fail(exc)
    return redirect(url_for('assistant.edit', agent_id=agent_id, tab='review'))


@assistant_bp.post('/<int:agent_id>/site')
def site(agent_id):
    agent = AIAgent.query.get_or_404(agent_id)
    try:
        if not agent.consent:
            raise ValueError('Разрешите передачу данных в OpenAI')
        url = request.form.get('url', '').strip()
        db.session.commit()
        # Capture the app outside the coroutine: Flask globals are thread-local.
        app = current_app._get_current_object()
        manager = current_app.telegram_manager
        if not manager:
            raise ValueError('Telegram не запущен')
        async def perform():
            async with manager.assistant().locks[agent_id]:
                with app.app_context():
                    return (await import_site(app, db.session.get(AIAgent, agent_id), url)).id
        run(perform(), timeout=65)
        flash('Страница разобрана. Проверьте запись перед подтверждением.', 'success')
    except Exception as exc:
        fail(exc)
    return redirect(url_for('assistant.edit', agent_id=agent_id, tab='knowledge'))


@assistant_bp.post('/<int:agent_id>/channel')
def channel(agent_id):
    agent = AIAgent.query.get_or_404(agent_id)
    app, manager = current_app._get_current_object(), current_app.telegram_manager
    try:
        if not manager or not agent.channel_id or not agent.consent:
            raise ValueError('Назначьте канал и разрешите передачу данных в OpenAI')
        channel_id, account_id = agent.channel_id, agent.account_id
        db.session.commit()
        async def perform():
            async with manager.assistant().locks[agent_id]:
                with app.app_context():
                    current = db.session.get(AIAgent, agent_id)
                    if current.account_id != account_id or current.channel_id != channel_id or not current.consent:
                        raise ValueError('Настройки изменились. Импорт отменён')
                    client = manager.clients.get(account_id)
                    if not client or not manager.client_connected(client):
                        raise ValueError('Аккаунт не подключён')
                    messages = await client.get_messages(int(channel_id), limit=3)
                    db.session.expire_all()
                    if current.account_id != account_id or current.channel_id != channel_id or not current.consent:
                        raise ValueError('Настройки изменились. Импорт отменён')
                    count = 0
                    for message in reversed(messages):
                        if message.raw_text:
                            await ingest(app, current, message.raw_text,
                                         'tg:' + str(message.id), telegram_source(channel_id, message.id), message.id)
                            count += 1
                    return count
        count = run(perform(), timeout=110)
        flash(f'Проверено постов: {count}. Подтвердите справочные записи.', 'success')
    except Exception as exc:
        fail(exc)
    return redirect(url_for('assistant.edit', agent_id=agent_id, tab='knowledge'))


@assistant_bp.post('/<int:agent_id>/facts')
def manual_fact(agent_id):
    agent = AIAgent.query.get_or_404(agent_id)
    try:
        title, summary = request.form.get('title', '').strip(), request.form.get('summary', '').strip()
        url, status = request.form.get('source_url', '').strip(), request.form.get('status', '')
        code = request.form.get('code', '').strip()
        if not title or len(title) > 200 or not summary or len(summary) > 1500 or len(code) > 100 or len(url) > 500 or status not in STATUSES:
            raise ValueError('Проверьте название, справочный текст и статус')
        if not source_url_allowed(url, agent.channel_id):
            raise ValueError('Нужна ссылка на настроенный официальный канал или амхарскую версию сайта')
        approved = request.form.get('verified') == 'on'
        db.session.add(AIFact(agent_id=agent.id, source_key='manual:' + secrets.token_hex(16),
            source_url=url, title=redact(title, 200), summary=redact(summary), code=code, status=status,
            approved=approved, verified_at=datetime.utcnow() if approved else None))
        agent.knowledge_revision += 1
        db.session.commit()
        flash('Справочная запись сохранена', 'success')
    except Exception as exc:
        fail(exc)
    return redirect(url_for('assistant.edit', agent_id=agent_id, tab='knowledge'))


@assistant_bp.post('/facts/<int:fact_id>')
def verify_fact(fact_id):
    fact = AIFact.query.get_or_404(fact_id)
    agent = AIAgent.query.get_or_404(fact.agent_id)
    try:
        action = request.form.get('action')
        if action not in ('approve', 'unapprove', 'save') or (action == 'approve' and (fact.deleted or request.form.get('verified') != 'on')):
            raise ValueError('Проверьте официальный источник и подтвердите запись')
        archive(fact)
        if action == 'save':
            title, summary, code = (request.form.get(key, '').strip() for key in ('title', 'summary', 'code'))
            status = request.form.get('status')
            if not title or len(title) > 200 or not summary or len(summary) > 1500 or len(code) > 100 or status not in STATUSES or fact.deleted:
                raise ValueError('Проверьте справочные поля')
            fact.title, fact.summary, fact.code, fact.status = redact(title, 200), redact(summary), code, status
            end = request.form.get('end_at', '').strip()
            fact.end_at = datetime.fromisoformat(end) if end else None
        fact.approved = action == 'approve' or (action == 'save' and request.form.get('verified') == 'on')
        old_related = fact.related_id
        related = request.form.get('related_id', str(old_related or '')).strip()
        new_related = int(related) if related else None
        if old_related and old_related != new_related:
            previous_parent = db.session.get(AIFact, old_related)
            if previous_parent:
                archive(previous_parent)
                previous_parent.approved, previous_parent.verified_at = False, None
                previous_parent.revision += 1
        fact.related_id = new_related
        if related:
            parent = db.session.get(AIFact, new_related)
            if not parent or parent.id == fact.id or parent.agent_id != agent.id or parent.related_id or parent.deleted or fact.status not in ('FINISHED', 'EXPIRED'):
                raise ValueError('Для связывания выберите исходную запись и подтверждённый статус FINISHED или EXPIRED')
            fact.related_id = parent.id
            archive(parent)
            parent.status, parent.approved = fact.status, fact.approved
            parent.verified_at = datetime.utcnow() if fact.approved else None
            parent.revision += 1
        fact.verified_at = datetime.utcnow() if fact.approved else None
        fact.revision += 1
        agent.knowledge_revision += 1
        db.session.commit()
        flash('Подтверждение обновлено', 'success')
    except Exception as exc:
        fail(exc)
    return redirect(url_for('assistant.edit', agent_id=agent.id, tab='knowledge'))
