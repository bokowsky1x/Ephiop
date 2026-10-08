import asyncio
from collections import defaultdict, deque
from datetime import datetime, timedelta
import re
import time
import logging

from telethon import errors

from ai_engine import AnalysisError, analyze, failure_message, fingerprint, image_content, redact
from ai_knowledge import ingest, telegram_source, verified_facts, archive
from ai_locales import LANGUAGE_LABELS, NOTICE_LANGUAGES, custom_notice, has_language_hint, locale_payload
from ai_moderation import matched_rules, moderation_rules
from models import Account, AIAgent, AIDecision, AIFact, AIMessage, AIUserContext, db

INFO_INTENTS = {'INFORMATION', 'POST_DISCUSSION', 'PROMO_INFO_REQUEST', 'BONUS_INFO_REQUEST',
                'FREE_SPIN_INFO_REQUEST', 'WAGERING_INFO_REQUEST', 'PROMO_CODE_INFO', 'CONTEST_INFO_REQUEST', 'RESULT_INFO_REQUEST'}
SUPPORT_INTENTS = {'DEPOSIT_PROBLEM', 'WITHDRAWAL_PROBLEM', 'BALANCE_PROBLEM', 'ACCOUNT_PROBLEM', 'SUPPORT_REQUEST', 'TECHNICAL_PROBLEM'}
SCAM_INTENTS = {'SCAM', 'FAKE_AGENT', 'FAKE_SUPPORT'}
SENSITIVE = {'PERSONAL_DATA', 'PAYMENT_DATA', 'IDENTITY_DOCUMENT'}
FINAL_STATES = {'SENT', 'DELETED', 'BANNED', 'UNCERTAIN', 'RUNNING'}
logger = logging.getLogger(__name__)
DELETE_FLAGS = {'PERSONAL_DATA': 'delete_personal_data', 'PAYMENT_DATA': 'delete_payment_data',
                'IDENTITY_DOCUMENT': 'delete_identity_documents'}


def _legacy_support_reply(language):
    texts = {
        'AMHARIC': 'የክፍያ ወይም የአካውንት ሁኔታን ማረጋገጥ አልችልም። በኦፊሴላዊው ድረ ገጽ ያለውን ድጋፍ ያነጋግሩ፦ support@betjam.com',
        'OROMO': 'Haala kaffaltii yookaan akkaawuntii keessanii mirkaneessuu hin danda\'u. Deeggarsa marsariitii rasmii qunnamaa: support@betjam.com',
        'AMHARIC_LATIN': 'Ye payment weyim account status maregageT alchilm. Be official website yalew support anegagru: support@betjam.com',
    }
    return texts.get(language, 'I cannot check payment or account status. Please contact support through the official website or support@betjam.com.')


def _legacy_privacy_warning(language, payment=False, support=True):
    warning = {'AMHARIC': 'እባክዎ የግል ወይም የክፍያ መረጃ ያለባቸውን ስክሪንሾቶች በዚህ ቻት አይላኩ።',
            'OROMO': 'Odeeffannoo dhuunfaa yookaan kaffaltii chaatii uummataa keessatti hin qoodinaa.',
            'AMHARIC_LATIN': 'Ye gil weyim ye payment mereja be public chat wist ayasayU.'}.get(
                language, 'Do not publish personal or payment details in the public chat. Never send money or credentials to people claiming to be support.')
    if payment and support:
        contact = {'AMHARIC': 'በገንዘብ ማስገባት ላይ ችግር ካለብዎ፣ ወደ support@betjam.com ይጻፉ።',
                   'OROMO': 'Rakkoo maallaqa galchuu yoo qabaattan, support@betjam.com irratti barreessaa.',
                   'AMHARIC_LATIN': 'Birr masgebat lay chigir kale, wede support@betjam.com tsafu.'}.get(
                       language, 'For a deposit problem, email support@betjam.com.')
        warning += ' ' + contact
    return warning


def support_reply(language, agent=None):
    custom = custom_notice(agent, language, 'support_text')
    contact = agent.support_contact if agent else 'support@betjam.com'
    if custom:
        return custom + (' ' + contact if contact else '')
    if language not in NOTICE_LANGUAGES:
        return ''
    if language == 'RUSSIAN':
        text = 'Я не могу проверить статус платежа или аккаунта. Обратитесь в официальную поддержку.'
        return text + (' ' + contact if contact else '')
    if language == 'ENGLISH' and agent and agent.language_profile == 'general':
        return 'I cannot check payment or account status. Please contact official support.' + (' ' + contact if contact else '')
    return _legacy_support_reply(language).replace('support@betjam.com', contact or '').rstrip(': .') + '.'


def privacy_warning(language, payment=False, support=True, agent=None):
    custom = custom_notice(agent, language, 'privacy_text')
    if custom:
        warning = custom
    elif language == 'RUSSIAN':
        warning = 'Не публикуйте персональные или платёжные данные в общем чате. Никому не передавайте пароли и коды.'
    elif language in NOTICE_LANGUAGES:
        # Preserve existing notices for already configured Ethiopian communities.
        if not agent or (agent.language_profile == 'ethiopia' and agent.support_contact == 'support@betjam.com' and not agent.support_text):
            return _legacy_privacy_warning(language, payment, support)
        warning = _legacy_privacy_warning(language, False, False)
    else:
        return ''
    if payment and support:
        contact = support_reply(language, agent)
        if contact:
            warning += ' ' + contact
    return warning


def policy(agent, decision, facts, rules=None):
    rules = moderation_rules(getattr(agent, 'id', None)) if rules is None else rules
    community_violation = decision.classification == 'RULE_VIOLATION' and decision.intent == 'COMMUNITY_RULE_VIOLATION'
    sensitive = decision.classification in SENSITIVE
    if sensitive:
        decision.intent = decision.classification
        decision.reply = privacy_warning(decision.language, payment=decision.classification == 'PAYMENT_DATA', support=agent.support_router, agent=agent)
        if decision.has_image and not agent.vision:
            return 'REVIEW', 'Изображение требует проверки: Vision отключён'
        if decision.action == 'DELETE' and (not agent.allow_delete or not getattr(agent, DELETE_FLAGS[decision.classification])):
            decision.action = 'WARN'
    if decision.intent in {'HOW_TO_REGISTER', 'GAMBLING_INSTRUCTIONS', 'UNKNOWN'}:
        return 'REVIEW', 'Требуется ответ человека'
    if decision.action in {'HUMAN_REVIEW', 'ESCALATE', 'MODERATE'}:
        return 'REVIEW', 'Проверка модератором'
    if decision.action == 'IGNORE' or decision.intent in {'CONTEST_ANSWER', 'SIMPLE_REACTION'}:
        return 'IGNORED', 'Ответ не нужен'
    if decision.action == 'BAN':
        if not community_violation or not matched_rules(decision, rules, 'BAN') or not agent.allow_ban:
            return 'REVIEW', 'Бан не разрешён или нет подтверждённого правила бана'
        if decision.has_image and not agent.vision:
            return 'REVIEW', 'Изображение требует проверки: Vision отключён'
        threshold = max(agent.ban_confidence, agent.vision_confidence) if decision.has_image else agent.ban_confidence
        if decision.confidence < threshold:
            return 'REVIEW', 'Недостаточная уверенность для бана'
        return 'READY', 'Рекомендован бан по правилу сообщества; права и автор проверяются перед действием'
    if decision.action == 'DELETE':
        scam = decision.intent in SCAM_INTENTS and agent.scam_detection
        community_delete = community_violation and matched_rules(decision, rules, 'DELETE')
        threshold = agent.vision_confidence if decision.has_image else agent.moderation_confidence
        if not (sensitive or scam or community_delete) or not agent.allow_delete or (decision.has_image and not agent.vision):
            return 'REVIEW', 'Автоудаление не разрешено для этой категории'
        if decision.confidence < threshold:
            return 'REVIEW', 'Недостаточная уверенность для удаления'
        return 'READY', 'Рекомендовано удаление'
    if decision.action not in {'REPLY', 'WARN'} or decision.confidence < agent.reply_confidence:
        return 'REVIEW', 'Недостаточная уверенность для ответа'
    if sensitive:
        if not decision.reply:
            return 'REVIEW', 'Нет проверенного предупреждения на этом языке'
        return 'READY', 'Предупреждение о персональных данных подготовлено'
    if decision.intent in SUPPORT_INTENTS:
        if not agent.support_router or decision.confidence < 0.75:
            return 'REVIEW', 'Проверка обращения в поддержку'
        decision.reply = support_reply(decision.language, agent)
        if not decision.reply:
            return 'REVIEW', 'Нет проверенного ответа поддержки на этом языке'
    else:
        if not decision.reply.strip() or redact(decision.reply, 1000) != decision.reply or re.search(r'https?://|www\.|t\.me/', decision.reply, re.I):
            return 'REVIEW', 'Нужно проверить текст ответа'
        if decision.intent in INFO_INTENTS:
            available = {fact['id']: fact for fact in facts}
            if not agent.information or not decision.fact_ids or any(value not in available for value in decision.fact_ids):
                return 'REVIEW', 'Нет подтверждённого свежего источника'
        elif decision.intent in SCAM_INTENTS:
            if not agent.scam_detection:
                return 'REVIEW', 'Защита от мошенничества отключена'
        elif decision.intent in SENSITIVE:
            decision.reply = privacy_warning(decision.language, agent=agent)
            if not decision.reply:
                return 'REVIEW', 'Нет проверенного предупреждения на этом языке'
        elif decision.intent in {'GREETING', 'CASUAL_CHAT', 'FOOTBALL_DISCUSSION'}:
            if not agent.community_replies:
                return 'IGNORED', 'Ответы на беседу отключены'
        else:
            return 'REVIEW', 'Категория требует проверки'
    return 'READY', 'Ответ подготовлен'


class AIAssistant:
    def __init__(self, manager):
        self.manager = manager
        self.locks = defaultdict(asyncio.Lock)
        self.analysis_times = defaultdict(deque)

    def account_ready(self, agent):
        account = db.session.get(Account, agent.account_id) if agent.account_id else None
        client = self.manager.clients.get(agent.account_id)
        return bool(account and account.is_active and account.status == 'authorized' and client
                    and self.manager.client_connected(client) and agent.consent and agent.mode != 'OFF')

    async def handle_message(self, account_id, client, event):
        if getattr(event, 'is_private', False) or not getattr(event, 'chat_id', None):
            return False
        with self.manager.app.app_context():
            agent = AIAgent.query.filter_by(account_id=account_id, chat_id=str(event.chat_id)).first()
            if not agent or not self.account_ready(agent):
                return False
            if getattr(event.message, 'out', False):
                return True
            owned_ids = {getattr(value, '_self_id', None) for value in self.manager.clients.values()}
            if getattr(event, 'sender_id', None) in owned_ids - {None}:
                return True
            agent_id = agent.id
            created_at = agent.created_at
        async with self.locks[agent_id]:
            with self.manager.app.app_context():
                agent = db.session.get(AIAgent, agent_id)
                if not agent or agent.created_at != created_at or not self.account_ready(agent) or agent.account_id != account_id or agent.chat_id != str(event.chat_id):
                    return True
                await self._analyze_message(agent, client, event)
        return True

    async def _analyze_message(self, agent, client, event):
        message = event.message
        digest = fingerprint(message)
        previous = AIDecision.query.filter_by(agent_id=agent.id, message_id=message.id, fingerprint=digest).first()
        if previous:
            return
        text = redact(getattr(message, 'raw_text', None) or getattr(message, 'text', ''), 4000)
        has_image = bool(getattr(message, 'photo', None) or (
            getattr(message, 'document', None) and (message.document.mime_type or '').startswith('image/')))
        user_id = str(getattr(event, 'sender_id', '') or '')
        record = AIMessage.query.filter_by(agent_id=agent.id, message_id=message.id).first()
        if not record:
            record = AIMessage(agent_id=agent.id, message_id=message.id)
            db.session.add(record)
        record.text, record.fingerprint, record.user_id = text, digest, user_id
        decision = AIDecision(agent_id=agent.id, message_id=message.id, user_id=user_id, fingerprint=digest,
                              agent_revision=agent.revision, knowledge_revision=agent.knowledge_revision,
                              has_image=has_image, model=self.manager.app.config.get('OPENAI_MODEL', ''))
        db.session.add(decision)
        AIDecision.query.filter_by(agent_id=agent.id, message_id=message.id).filter(
            AIDecision.state.in_(('READY', 'REVIEW', 'OBSERVED'))).update({'state': 'STALE', 'training_status': 'SUPERSEDED'}, synchronize_session='fetch')
        AIMessage.query.filter_by(agent_id=agent.id).filter(AIMessage.created_at < datetime.utcnow() - timedelta(days=7)).delete(synchronize_session='fetch')
        AIDecision.query.filter_by(agent_id=agent.id).filter(AIDecision.created_at < datetime.utcnow() - timedelta(days=30),
            AIDecision.state != 'RUNNING').delete(synchronize_session='fetch')
        db.session.commit()
        stage = 'Подготовка анализа'
        try:
            window = self.analysis_times[agent.id]
            now = time.monotonic()
            while window and window[0] < now - 60:
                window.popleft()
            if len(window) >= 12:
                raise AnalysisError('Лимит анализа: 12 сообщений в минуту. Требуется ручная проверка')
            window.append(now)
            image = None
            if has_image:
                stage = 'Получение изображения из Telegram'
                if not agent.vision:
                    raise AnalysisError('Изображения не анализируются: включите Vision или проверьте вручную')
                image = await asyncio.wait_for(image_content(client, message), 20)
            reply = None
            if getattr(message, 'reply_to_msg_id', None):
                stage = 'Чтение сообщения, на которое ответили'
                replied = await asyncio.wait_for(event.get_reply_message(), 10)
                if replied:
                    reply = dict(text=redact(getattr(replied, 'raw_text', None) or getattr(replied, 'text', ''), 3000),
                                 message_id=replied.id, is_ai=bool(getattr(replied, 'out', False)))
            db.session.expire_all()
            if not self.account_ready(agent) or agent.revision != decision.agent_revision:
                decision.state, decision.result = 'STALE', 'Настройки изменились. Анализ отменён'
                db.session.commit()
                return
            facts = verified_facts(agent)
            memory = AIUserContext.query.filter_by(agent_id=agent.id, user_id=user_id).first()
            known_languages = set(LANGUAGE_LABELS)
            preferred_language = memory.language if memory and memory.language in known_languages else agent.fallback_language
            language_hint = has_language_hint(text)
            if has_image and not language_hint:
                preferred_language = agent.fallback_language
            history = AIMessage.query.filter_by(agent_id=agent.id).filter(AIMessage.message_id != message.id).order_by(AIMessage.created_at.desc()).limit(20).all()
            user_history = AIMessage.query.filter_by(agent_id=agent.id, user_id=user_id).filter(AIMessage.message_id != message.id).order_by(AIMessage.created_at.desc()).limit(5).all()
            stage = 'Анализ OpenAI'
            from ai_examples import approved_examples
            result = await analyze(self.manager.app, dict(message=text, reply_to=reply,
                preferred_language=preferred_language, has_user_language_hint=language_hint,
                **locale_payload(agent), approved_examples=approved_examples(agent.id, message=text),
                moderation_rules=moderation_rules(agent.id),
                permissions=dict(allow_delete=agent.allow_delete, allow_ban=agent.allow_ban, support_router=agent.support_router),
                recent_messages=[dict(text=row.text, is_ai=row.is_ai) for row in reversed(history)],
                user_history=[row.text for row in reversed(user_history)], verified_facts=facts,
                now=datetime.utcnow().isoformat()), image)
            db.session.expire_all()
            for key, value in result.items():
                setattr(decision, key, value)
            if decision.language in ('UNKNOWN', 'MIXED') or (has_image and not language_hint):
                decision.language = preferred_language
            stage = 'Проверка результата анализа'
            decision.reason = redact(decision.reason, 500)
            decision.state, decision.result = policy(agent, decision, verified_facts(agent))
            decision.training_needed = decision.state == 'REVIEW'
            decision.reply = redact(decision.reply, 1000) if decision.intent not in SUPPORT_INTENTS | SENSITIVE else decision.reply
            if decision.intent in SENSITIVE or decision.classification in SENSITIVE:
                record.text = '[PRIVATE MESSAGE]'
            if agent.revision != decision.agent_revision or agent.mode == 'OFF' or not agent.consent:
                decision.state, decision.result = 'STALE', 'Настройки изменились во время анализа'
            elif agent.mode == 'OBSERVE':
                decision.state = 'OBSERVED'
            elif agent.mode == 'ASSIST' and decision.state == 'READY':
                decision.state = 'REVIEW'
            agent.last_analysis_at, agent.last_error = datetime.utcnow(), ''
            db.session.commit()
            if decision.state == 'READY' and agent.mode == 'AUTO':
                stage = 'Проверка перед действием в Telegram'
                await self._execute(decision.id)
        except Exception as exc:
            db.session.rollback()
            decision = db.session.get(AIDecision, decision.id)
            error = failure_message(exc, stage)
            logger.warning('AI failure: stage=%s error_type=%s', stage, type(exc).__name__)
            if decision.state not in FINAL_STATES:
                decision.state, decision.result = 'REVIEW', error
            agent.last_error = error
            db.session.commit()

    def cooldown(self, agent, decision, memory):
        now = datetime.utcnow()
        if decision.intent == 'GREETING' and memory.greeted:
            return 'Приветствие уже отправлено'
        if agent.last_reply_at and (now - agent.last_reply_at).total_seconds() < agent.chat_cooldown:
            return 'Пауза между ответами в чате'
        if memory.last_reply_at and (now - memory.last_reply_at).total_seconds() < agent.user_cooldown:
            return 'Пауза между ответами пользователю'
        last = AIDecision.query.filter_by(agent_id=agent.id, intent=decision.intent).filter(
            AIDecision.state.in_(('SENT', 'DELETED', 'BANNED')), AIDecision.completed_at >= now - timedelta(seconds=agent.intent_cooldown),
            AIDecision.id != decision.id
        ).first()
        return 'Пауза между одинаковыми темами' if last else None

    async def execute(self, decision_id, action=None, created_at=None):
        with self.manager.app.app_context():
            decision = db.session.get(AIDecision, decision_id)
            if not decision:
                raise AnalysisError('Решение не найдено')
            agent_id = decision.agent_id
        async with self.locks[agent_id]:
            with self.manager.app.app_context():
                decision = db.session.get(AIDecision, decision_id)
                if not decision or (created_at is not None and decision.created_at.isoformat() != created_at):
                    raise AnalysisError('Решение изменилось. Обновите страницу')
                return await self._execute(decision_id, manual=True, action=action)

    async def _execute(self, decision_id, manual=False, action=None):
        decision = db.session.get(AIDecision, decision_id)
        agent = db.session.get(AIAgent, decision.agent_id)
        if decision.state not in ('READY', 'REVIEW'):
            return decision.state
        if not agent or not self.account_ready(agent) or agent.mode not in ('ASSIST', 'AUTO'):
            raise AnalysisError('Аккаунт не подключён или режим не разрешает действия')
        if agent.revision != decision.agent_revision or datetime.utcnow() - decision.created_at > timedelta(minutes=15):
            decision.state, decision.result = 'STALE', 'Решение устарело'
            db.session.commit()
            return decision.state
        candidate = action if manual else decision.action
        if candidate not in ('REPLY', 'DELETE', 'BAN', 'WARN'):
            raise AnalysisError('Выберите ответ, удаление или бан')
        facts = verified_facts(agent)
        if candidate in ('REPLY', 'WARN'):
            if decision.action not in ('REPLY', 'WARN'):
                raise AnalysisError('Нет подготовленного безопасного ответа')
            if decision.intent in INFO_INTENTS and decision.knowledge_revision != agent.knowledge_revision:
                raise AnalysisError('Официальные источники изменились. Нужен новый анализ')
            state, reason = policy(agent, decision, facts)
            if state != 'READY':
                raise AnalysisError(reason)
        elif candidate == 'BAN':
            if decision.action != 'BAN':
                raise AnalysisError('Нет подготовленного решения о бане')
            state, reason = policy(agent, decision, facts)
            if state != 'READY':
                raise AnalysisError(reason)
        elif not agent.allow_delete:
            raise AnalysisError('Удаление сообщений не разрешено в настройках')
        memory = AIUserContext.query.filter_by(agent_id=agent.id, user_id=decision.user_id).first()
        if not memory:
            memory = AIUserContext(agent_id=agent.id, user_id=decision.user_id)
            db.session.add(memory)
        if candidate not in ('DELETE', 'BAN'):
            pause = self.cooldown(agent, decision, memory)
            if pause:
                decision.state, decision.result = 'IGNORED', pause
                db.session.commit()
                return decision.state
        client = self.manager.clients[agent.account_id]
        current = await asyncio.wait_for(client.get_messages(int(agent.chat_id), ids=decision.message_id), 10)
        if not current or fingerprint(current) != decision.fingerprint or getattr(current, 'out', False):
            decision.state, decision.result = 'STALE', 'Сообщение изменено или удалено'
            db.session.commit()
            return decision.state
        if candidate == 'DELETE':
            permissions = await asyncio.wait_for(client.get_permissions(int(agent.chat_id), 'me'), 10)
            if not permissions or not permissions.delete_messages:
                raise AnalysisError('У аккаунта нет права удалять сообщения в этом чате')
        if candidate == 'BAN':
            if not agent.chat_id.startswith('-100'):
                raise AnalysisError('Бан доступен только в супергруппе Telegram')
            sender = getattr(current, 'sender_id', None)
            owned_ids = {getattr(value, '_self_id', None) for value in self.manager.clients.values()}
            if type(sender) is not int or sender <= 0 or str(sender) != decision.user_id or sender in owned_ids:
                raise AnalysisError('Автор не подтверждён либо это канал или собственный аккаунт')
            permissions = await asyncio.wait_for(client.get_permissions(int(agent.chat_id), 'me'), 10)
            if not permissions or not getattr(permissions, 'ban_users', False):
                raise AnalysisError('У аккаунта нет права блокировать участников')
            target_permissions = await asyncio.wait_for(client.get_permissions(int(agent.chat_id), sender), 10)
            if not target_permissions or getattr(target_permissions, 'is_admin', None) is not False or getattr(target_permissions, 'is_creator', None) is not False:
                raise AnalysisError('Нельзя банить администратора или участника с неподтверждённой ролью')
        db.session.expire_all()
        if not self.account_ready(agent) or agent.revision != decision.agent_revision:
            raise AnalysisError('Настройки изменились. Действие отменено')
        if candidate not in ('DELETE', 'BAN') and decision.intent in INFO_INTENTS:
            fresh_ids = {fact['id'] for fact in verified_facts(agent)}
            if decision.knowledge_revision != agent.knowledge_revision or any(value not in fresh_ids for value in decision.fact_ids):
                raise AnalysisError('Официальные источники устарели или изменились')
        gate = [AIAgent.revision == decision.agent_revision, AIAgent.consent == True,
                AIAgent.mode.in_(('ASSIST', 'AUTO'))]
        if candidate == 'BAN':
            gate.append(AIAgent.allow_ban == True)
        if candidate not in ('DELETE', 'BAN') and decision.intent in INFO_INTENTS:
            gate.append(AIAgent.knowledge_revision == decision.knowledge_revision)
        claimed = AIDecision.query.filter_by(id=decision.id).filter(
            AIDecision.state.in_(('READY', 'REVIEW')), AIDecision.agent.has(db.and_(*gate))
        ).update({'action': candidate, 'state': 'RUNNING', 'result': 'Выполнение'}, synchronize_session='fetch')
        if not claimed:
            db.session.rollback()
            raise AnalysisError('Решение отменено или настройки изменились')
        db.session.commit()
        try:
            if candidate == 'BAN':
                await asyncio.wait_for(client.edit_permissions(int(agent.chat_id), int(decision.user_id), view_messages=False), 20)
                decision.state, decision.result = 'BANNED', 'Участник заблокирован по правилу сообщества. Сообщение отдельно не удалялось'
            elif candidate == 'DELETE':
                await asyncio.wait_for(client.delete_messages(int(agent.chat_id), [decision.message_id], revoke=True), 20)
                decision.state, decision.result = 'DELETED', 'Сообщение удалено'
                decision.completed_at = datetime.utcnow()
                db.session.commit()
                if (decision.classification in SENSITIVE or decision.intent in SCAM_INTENTS) and decision.confidence >= agent.moderation_confidence and not self.cooldown(agent, decision, memory):
                    try:
                        warning = privacy_warning(decision.language, payment=decision.classification == 'PAYMENT_DATA', support=agent.support_router, agent=agent)
                        if not warning:
                            raise AnalysisError('Нет проверенного предупреждения на этом языке')
                        await asyncio.wait_for(client.send_message(int(agent.chat_id), warning, parse_mode=None, link_preview=False), 15)
                        agent.last_reply_at = memory.last_reply_at = datetime.utcnow()
                        memory.language, memory.last_intent = decision.language, decision.intent
                        decision.result = 'Сообщение удалено; предупреждение отправлено'
                    except Exception:
                        decision.result = 'Сообщение удалено; предупреждение не подтверждено'
            else:
                reply = decision.reply
                if decision.intent in INFO_INTENTS:
                    urls = {fact['id']: [fact['source_url'], *fact.get('related_sources', [])] for fact in facts}
                    reply += '\n' + '\n'.join(dict.fromkeys(url for value in decision.fact_ids for url in urls[value]))
                sent = await asyncio.wait_for(client.send_message(int(agent.chat_id), reply, reply_to=decision.message_id,
                                                                parse_mode=None, link_preview=False), 20)
                decision.state, decision.result = 'SENT', 'Ответ отправлен'
                agent.last_reply_at = memory.last_reply_at = datetime.utcnow()
                memory.language, memory.last_intent = decision.language, decision.intent
                if decision.intent == 'GREETING':
                    memory.greeted = True
                db.session.add(AIMessage(agent_id=agent.id, message_id=sent.id, user_id='', text=redact(reply),
                                         fingerprint=fingerprint(sent), is_ai=True))
            decision.completed_at = datetime.utcnow()
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            decision = db.session.get(AIDecision, decision_id)
            if decision.state in ('DELETED', 'BANNED'):
                decision.result = 'Удаление подтверждено, последующие действия не завершены' if decision.state == 'DELETED' else 'Бан подтверждён, последующие действия не завершены'
            else:
                decision.state = 'ERROR' if isinstance(exc, errors.RPCError) else 'UNCERTAIN'
                decision.result = 'Telegram отклонил действие' if decision.state == 'ERROR' else 'Результат не подтверждён. Проверьте Telegram; автоматического повтора нет'
            agent.last_error = decision.result
            db.session.commit()
        return decision.state

    async def handle_channel_post(self, account_id, client, event):
        with self.manager.app.app_context():
            agents = AIAgent.query.filter_by(account_id=account_id, channel_id=str(event.chat_id), consent=True).filter(AIAgent.mode != 'OFF').all()
            targets = [(agent.id, agent.created_at) for agent in agents]
            for agent_id, created_at in targets:
                async with self.locks[agent_id]:
                    try:
                        db.session.expire_all()
                        agent = db.session.get(AIAgent, agent_id)
                        if not agent or agent.created_at != created_at or not self.account_ready(agent) or agent.account_id != account_id or agent.channel_id != str(event.chat_id):
                            continue
                        text = getattr(event.message, 'raw_text', None) or getattr(event.message, 'text', '') or ''
                        if text:
                            await ingest(self.manager.app, agent, text, 'tg:' + str(event.message.id),
                                         telegram_source(agent.channel_id, event.message.id), event.message.id)
                    except Exception:
                        db.session.rollback()
                        agent = db.session.get(AIAgent, agent_id)
                        if agent and agent.created_at == created_at:
                            agent.last_error = 'Официальный пост не разобран. Старое подтверждение при редактировании снято'
                            db.session.commit()
            return bool(agents)

    async def handle_deleted(self, account_id, event):
        if not event.chat_id:
            return
        with self.manager.app.app_context():
            agents = AIAgent.query.filter_by(account_id=account_id).all()
            targets = [(agent.id, agent.created_at) for agent in agents]
            for agent_id, created_at in targets:
                async with self.locks[agent_id]:
                    db.session.expire_all()
                    agent = db.session.get(AIAgent, agent_id)
                    if not agent or agent.created_at != created_at or agent.account_id != account_id:
                        continue
                    if str(event.chat_id) == agent.channel_id:
                        for fact in AIFact.query.filter_by(agent_id=agent.id).filter(AIFact.source_message_id.in_(event.deleted_ids)).all():
                            archive(fact)
                            fact.deleted, fact.approved = True, False
                            if fact.related_id:
                                parent = db.session.get(AIFact, fact.related_id)
                                if parent:
                                    parent.approved = False
                            fact.revision += 1
                            agent.knowledge_revision += 1
                    if str(event.chat_id) == agent.chat_id:
                        AIMessage.query.filter_by(agent_id=agent.id).filter(AIMessage.message_id.in_(event.deleted_ids)).delete(synchronize_session='fetch')
                        AIDecision.query.filter_by(agent_id=agent.id).filter(AIDecision.message_id.in_(event.deleted_ids),
                            AIDecision.state.in_(('READY', 'REVIEW', 'OBSERVED'))).update({'state': 'STALE', 'training_status': 'SUPERSEDED'}, synchronize_session='fetch')
                    db.session.commit()
