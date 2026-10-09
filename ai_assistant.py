import asyncio
from collections import defaultdict, deque
from datetime import datetime, timedelta
import re
import time
import logging
from types import SimpleNamespace

from telethon import errors

from ai_engine import AnalysisError, analyze, failure_message, fingerprint, image_content, redact
from ai_knowledge import ingest, telegram_source, verified_facts, archive
from ai_locales import LANGUAGE_LABELS, NOTICE_LANGUAGES, custom_notice, has_language_hint, locale_payload
from ai_moderation import matched_rules, moderation_rules
from ai_queue import MAX_AGE, MAX_ATTEMPTS, retry_delay
from models import Account, AIAgent, AIDecision, AIFact, AIMessage, AIUserContext, db

INFO_INTENTS = {'INFORMATION', 'POST_DISCUSSION', 'PROMO_INFO_REQUEST', 'BONUS_INFO_REQUEST',
                'FREE_SPIN_INFO_REQUEST', 'WAGERING_INFO_REQUEST', 'PROMO_CODE_INFO', 'CONTEST_INFO_REQUEST', 'RESULT_INFO_REQUEST'}
SUPPORT_INTENTS = {'DEPOSIT_PROBLEM', 'WITHDRAWAL_PROBLEM', 'BALANCE_PROBLEM', 'ACCOUNT_PROBLEM', 'SUPPORT_REQUEST', 'TECHNICAL_PROBLEM'}
SCAM_INTENTS = {'SCAM', 'FAKE_AGENT', 'FAKE_SUPPORT'}
SENSITIVE = {'PERSONAL_DATA', 'PAYMENT_DATA', 'IDENTITY_DOCUMENT'}
FINAL_STATES = {'SENT', 'DELETED', 'BANNED', 'DELETED_BANNED', 'PARTIAL', 'UNCERTAIN', 'RUNNING'}
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
    community_violation = decision.classification == 'RULE_VIOLATION' and decision.intent in ('COMMUNITY_RULE_VIOLATION', 'SPAM')
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
    if decision.action in ('BAN', 'DELETE_BAN'):
        if not community_violation or not matched_rules(decision, rules, decision.action) or not agent.allow_ban:
            return 'REVIEW', 'Бан не разрешён или нет подтверждённого правила бана'
        if decision.action == 'DELETE_BAN' and not agent.allow_delete:
            return 'REVIEW', 'Для удаления и бана нужны оба разрешения'
        if decision.has_image and not agent.vision:
            return 'REVIEW', 'Изображение требует проверки: Vision отключён'
        threshold = max(agent.ban_confidence, agent.vision_confidence) if decision.has_image else agent.ban_confidence
        if decision.action == 'DELETE_BAN':
            threshold = max(threshold, agent.moderation_confidence)
        if decision.confidence < threshold:
            return 'REVIEW', 'Недостаточная уверенность для бана'
        return 'READY', 'Рекомендованы удаление и бан по правилу сообщества' if decision.action == 'DELETE_BAN' else 'Рекомендован бан по правилу сообщества; права и автор проверяются перед действием'
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
            if not agent or agent.mode == 'OFF' or not agent.consent:
                return False
            if getattr(event.message, 'out', False):
                return True
            owned_ids = {getattr(value, '_self_id', None) for value in self.manager.clients.values()}
            if getattr(event, 'sender_id', None) in owned_ids - {None}:
                return True
            agent_id = agent.id
            created_at = agent.created_at
            decision = self._queue_message(agent, event)
            if not decision:
                return True
            decision_id = decision.id
        if self.locks[agent_id].locked():
            return True
        async with self.locks[agent_id]:
            with self.manager.app.app_context():
                agent = db.session.get(AIAgent, agent_id)
                if not agent or agent.created_at != created_at or not self.account_ready(agent) or agent.account_id != account_id or agent.chat_id != str(event.chat_id):
                    return True
                decision = db.session.get(AIDecision, decision_id)
                if decision:
                    await self._process_analysis(agent, client, decision, event)
        return True

    def _queue_message(self, agent, event):
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
                              has_image=has_image, has_reply=bool(getattr(message, 'reply_to_msg_id', None)),
                              model=self.manager.app.config.get('OPENAI_MODEL', ''),
                              state='QUEUED', next_analysis_at=datetime.utcnow(), result='Ожидает анализа')
        db.session.add(decision)
        db.session.flush()
        AIDecision.query.filter_by(agent_id=agent.id, message_id=message.id).filter(
            AIDecision.id != decision.id,
            AIDecision.state.in_(('READY', 'REVIEW', 'OBSERVED', 'QUEUED', 'ANALYZING'))).update({'state': 'STALE', 'training_status': 'SUPERSEDED'}, synchronize_session='fetch')
        AIMessage.query.filter_by(agent_id=agent.id).filter(AIMessage.created_at < datetime.utcnow() - timedelta(days=7)).delete(synchronize_session='fetch')
        AIDecision.query.filter_by(agent_id=agent.id).filter(AIDecision.created_at < datetime.utcnow() - timedelta(days=30),
            AIDecision.state != 'RUNNING').delete(synchronize_session='fetch')
        db.session.commit()
        return decision

    async def process_queue(self):
        with self.manager.app.app_context():
            now = datetime.utcnow()
            AIDecision.query.filter_by(state='QUEUED').filter(AIDecision.created_at < now - MAX_AGE).update(
                {'state': 'STALE', 'result': 'Ожидание превысило 10 минут; старое сообщение не обрабатывается автоматически'}, synchronize_session=False)
            AIDecision.query.filter_by(state='ANALYZING').filter(AIDecision.analysis_started_at < now - timedelta(minutes=3)).update(
                {'state': 'QUEUED', 'next_analysis_at': now, 'result': 'Анализ не завершился вовремя; ожидает повторной проверки сообщения'}, synchronize_session=False)
            db.session.commit()
            candidates = db.session.query(AIDecision.agent_id).filter(
                AIDecision.state == 'QUEUED', AIDecision.next_analysis_at <= now).group_by(
                    AIDecision.agent_id).order_by(db.func.min(AIDecision.next_analysis_at)).all()
        available = [value for (value,) in candidates if not self.locks[value].locked()][:4]
        results = await asyncio.gather(*(self._drain_agent(value) for value in available), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                logger.warning('AI queue failure: error_type=%s', type(result).__name__)

    async def _drain_agent(self, agent_id):
        async with self.locks[agent_id]:
            with self.manager.app.app_context():
                agent = db.session.get(AIAgent, agent_id)
                decision = AIDecision.query.filter_by(agent_id=agent_id, state='QUEUED').filter(
                    AIDecision.next_analysis_at <= datetime.utcnow()).order_by(AIDecision.id).first()
                if agent and decision:
                    await self._process_analysis(agent, self.manager.clients.get(agent.account_id), decision)

    async def _process_analysis(self, agent, client, decision, event=None):
        now = datetime.utcnow()
        if decision.state != 'QUEUED' or (decision.next_analysis_at and decision.next_analysis_at > now):
            return
        if agent.revision != decision.agent_revision or agent.mode == 'OFF' or not agent.consent or now - decision.created_at > MAX_AGE:
            decision.state, decision.result = 'STALE', 'Настройки изменились или сообщение устарело; анализ отменён'
            db.session.commit()
            return
        if decision.analysis_attempts >= MAX_ATTEMPTS:
            decision.state, decision.result = 'REVIEW', 'Попытки анализа исчерпаны. Требуется проверка администратора'
            db.session.commit()
            return
        if not self.account_ready(agent):
            decision.next_analysis_at = now + timedelta(seconds=10)
            decision.result = 'Ожидает подключения активного авторизованного аккаунта'
            db.session.commit()
            return
        clock = time.monotonic()
        if agent.id not in self.analysis_times:
            recent = AIDecision.query.filter_by(agent_id=agent.id).filter(
                AIDecision.analysis_started_at >= now - timedelta(seconds=60)).order_by(AIDecision.analysis_started_at).all()
            for row in recent:
                self.analysis_times[agent.id].extend([clock - max(0, (now - row.analysis_started_at).total_seconds())] * min(3, row.analysis_attempts))
        window = self.analysis_times[agent.id]
        while window and window[0] <= clock - 60:
            window.popleft()
        if len(window) >= 12:
            decision.next_analysis_at = now + timedelta(seconds=max(1, 60 - (clock - window[0])))
            decision.result = 'Лимит 12 анализов в минуту; сообщение остаётся в очереди'
            db.session.commit()
            return
        started = now
        claimed = AIDecision.query.filter_by(id=decision.id, state='QUEUED', agent_revision=agent.revision).filter(
            AIDecision.agent.has(db.and_(AIAgent.revision == decision.agent_revision, AIAgent.consent == True, AIAgent.mode != 'OFF'))).update(
                {'state': 'ANALYZING', 'analysis_attempts': AIDecision.analysis_attempts + 1,
                 'knowledge_revision': agent.knowledge_revision,
                 'analysis_started_at': started, 'next_analysis_at': None, 'result': 'Анализируется'}, synchronize_session='fetch')
        if not claimed:
            db.session.rollback()
            return
        window.append(clock)
        decision.model = self.manager.app.config.get('OPENAI_MODEL', '')
        db.session.commit()
        stage = 'Подготовка анализа'
        try:
            if event is None:
                stage = 'Повторное чтение сообщения из Telegram'
                message = await asyncio.wait_for(client.get_messages(int(agent.chat_id), ids=decision.message_id), 10)
                owned_ids = {getattr(value, '_self_id', None) for value in self.manager.clients.values()}
                if not message or fingerprint(message) != decision.fingerprint or getattr(message, 'out', False) or str(getattr(message, 'sender_id', '') or '') != decision.user_id or getattr(message, 'sender_id', None) in owned_ids - {None}:
                    decision.state, decision.result = 'STALE', 'Сообщение изменено, удалено или автор не подтверждён'
                    db.session.commit()
                    return
                async def get_reply():
                    return await client.get_messages(int(agent.chat_id), ids=message.reply_to_msg_id)
                event = SimpleNamespace(message=message, get_reply_message=get_reply)
            message = event.message
            text = redact(getattr(message, 'raw_text', None) or getattr(message, 'text', ''), 4000)
            has_image, user_id = decision.has_image, decision.user_id
            record = AIMessage.query.filter_by(agent_id=agent.id, message_id=message.id).first()
            if not record or record.fingerprint != decision.fingerprint:
                decision.state, decision.result = 'STALE', 'Исходное сообщение заменено или удалено'
                db.session.commit()
                return
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
            if decision.state != 'ANALYZING' or decision.analysis_started_at != started:
                return
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
            stage = 'Поиск пояснений'
            from ai_memory import select_memory
            selected = await select_memory(self.manager.app, agent, text, decision=decision)
            db.session.expire_all()
            if decision.state != 'ANALYZING' or decision.analysis_started_at != started:
                return
            if agent.revision != decision.agent_revision or agent.mode == 'OFF' or not agent.consent:
                decision.state, decision.result = 'STALE', 'Настройки изменились. Анализ отменён'
                db.session.commit()
                return
            decision.memory_method, decision.memory_example_ids = selected['method'], selected['ids']
            stage = 'Анализ OpenAI'
            result = await asyncio.wait_for(analyze(self.manager.app, dict(message=text, reply_to=reply,
                preferred_language=preferred_language, has_user_language_hint=language_hint,
                **locale_payload(agent), approved_examples=selected['examples'],
                moderation_rules=moderation_rules(agent.id),
                permissions=dict(allow_delete=agent.allow_delete, allow_ban=agent.allow_ban, support_router=agent.support_router),
                recent_messages=[dict(text=row.text, is_ai=row.is_ai) for row in reversed(history)],
                user_history=[row.text for row in reversed(user_history)], verified_facts=facts,
                now=datetime.utcnow().isoformat()), image), 90)
            db.session.expire_all()
            if decision.state != 'ANALYZING' or decision.analysis_started_at != started:
                return
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
                decision.embedding, decision.embedding_hash = None, ''
            if datetime.utcnow() - decision.created_at > MAX_AGE:
                decision.state, decision.result = 'STALE', 'Сообщение устарело во время анализа; автоматическое действие отменено'
            elif agent.revision != decision.agent_revision or agent.mode == 'OFF' or not agent.consent:
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
        except asyncio.CancelledError:
            db.session.rollback()
            current = db.session.get(AIDecision, decision.id)
            if current and current.state == 'ANALYZING' and current.analysis_started_at == started:
                current.state, current.next_analysis_at = 'QUEUED', datetime.utcnow() + timedelta(seconds=10)
                current.result = 'Анализ отменён; ожидает повторной проверки сообщения'
                db.session.commit()
            raise
        except Exception as exc:
            db.session.rollback()
            decision = db.session.get(AIDecision, decision.id)
            error = failure_message(exc, stage)
            logger.warning('AI failure: stage=%s error_type=%s', stage, type(exc).__name__)
            if not decision or (stage != 'Проверка перед действием в Telegram' and (
                    decision.state != 'ANALYZING' or decision.analysis_started_at != started)):
                return
            if agent.revision != decision.agent_revision or agent.mode == 'OFF' or not agent.consent:
                if decision.state not in FINAL_STATES:
                    decision.state, decision.result = 'STALE', 'Настройки изменились; повтор анализа отменён'
                    db.session.commit()
                return
            if decision.state == 'ANALYZING':
                delay = retry_delay(exc, decision.analysis_attempts)
                if delay is not None and decision.analysis_attempts < MAX_ATTEMPTS and datetime.utcnow() + timedelta(seconds=delay) - decision.created_at < MAX_AGE:
                    decision.state, decision.next_analysis_at = 'QUEUED', datetime.utcnow() + timedelta(seconds=delay)
                    decision.result = error + f' Повтор анализа через {delay} с (попытка {decision.analysis_attempts}/{MAX_ATTEMPTS})'
                    agent.last_error = error
                    db.session.commit()
                    return
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
            AIDecision.state.in_(('SENT', 'DELETED', 'BANNED', 'DELETED_BANNED', 'PARTIAL')), AIDecision.completed_at >= now - timedelta(seconds=agent.intent_cooldown),
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
        max_age = timedelta(minutes=15) if manual else MAX_AGE
        if agent.revision != decision.agent_revision or datetime.utcnow() - decision.created_at > max_age:
            decision.state, decision.result = 'STALE', 'Решение устарело'
            db.session.commit()
            return decision.state
        candidate = action if manual else decision.action
        if candidate not in ('REPLY', 'DELETE', 'BAN', 'DELETE_BAN', 'WARN'):
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
        elif candidate in ('BAN', 'DELETE_BAN'):
            if decision.action != candidate:
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
        if candidate not in ('DELETE', 'BAN', 'DELETE_BAN'):
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
        if candidate in ('DELETE', 'DELETE_BAN'):
            permissions = await asyncio.wait_for(client.get_permissions(int(agent.chat_id), 'me'), 10)
            if not permissions or not permissions.delete_messages:
                raise AnalysisError('У аккаунта нет права удалять сообщения в этом чате')
        if candidate in ('BAN', 'DELETE_BAN'):
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
        if datetime.utcnow() - decision.created_at > max_age:
            decision.state, decision.result = 'STALE', 'Решение устарело во время проверки Telegram'
            db.session.commit()
            return decision.state
        if candidate not in ('DELETE', 'BAN', 'DELETE_BAN') and decision.intent in INFO_INTENTS:
            fresh_ids = {fact['id'] for fact in verified_facts(agent)}
            if decision.knowledge_revision != agent.knowledge_revision or any(value not in fresh_ids for value in decision.fact_ids):
                raise AnalysisError('Официальные источники устарели или изменились')
        gate = [AIAgent.revision == decision.agent_revision, AIAgent.consent == True,
                AIAgent.mode.in_(('ASSIST', 'AUTO'))]
        if candidate in ('BAN', 'DELETE_BAN'):
            gate.append(AIAgent.allow_ban == True)
        if candidate in ('DELETE', 'DELETE_BAN'):
            gate.append(AIAgent.allow_delete == True)
        if candidate not in ('DELETE', 'BAN', 'DELETE_BAN') and decision.intent in INFO_INTENTS:
            gate.append(AIAgent.knowledge_revision == decision.knowledge_revision)
        claimed = AIDecision.query.filter_by(id=decision.id).filter(
            AIDecision.state.in_(('READY', 'REVIEW')), AIDecision.agent.has(db.and_(*gate))
        ).update({'action': candidate, 'state': 'RUNNING', 'result': 'Выполнение'}, synchronize_session='fetch')
        if not claimed:
            db.session.rollback()
            raise AnalysisError('Решение отменено или настройки изменились')
        db.session.commit()
        try:
            if candidate == 'DELETE_BAN':
                return await self._delete_and_ban(client, agent, decision, max_age)
            if candidate == 'BAN':
                await asyncio.wait_for(client.edit_permissions(int(agent.chat_id), int(decision.user_id), view_messages=False), 20)
                decision.state, decision.result = 'BANNED', 'Участник заблокирован по правилу сообщества. Сообщение отдельно не удалялось'
            elif candidate == 'DELETE':
                warning_result = ''
                if (decision.classification in SENSITIVE or decision.intent in SCAM_INTENTS) and decision.confidence >= agent.moderation_confidence and not self.cooldown(agent, decision, memory):
                    try:
                        warning = privacy_warning(decision.language, payment=decision.classification == 'PAYMENT_DATA', support=agent.support_router, agent=agent)
                        if not warning:
                            raise AnalysisError('Нет проверенного предупреждения на этом языке')
                        await asyncio.wait_for(client.send_message(int(agent.chat_id), warning, reply_to=decision.message_id,
                                                                  parse_mode=None, link_preview=False), 15)
                        agent.last_reply_at = memory.last_reply_at = datetime.utcnow()
                        memory.language, memory.last_intent = decision.language, decision.intent
                        warning_result = '; предупреждение отправлено ответом на исходное сообщение'
                    except Exception:
                        warning_result = '; предупреждение не подтверждено'
                    db.session.commit()
                    current = await asyncio.wait_for(client.get_messages(int(agent.chat_id), ids=decision.message_id), 10)
                    db.session.expire_all()
                    if not current or fingerprint(current) != decision.fingerprint or not self.account_ready(agent) or agent.revision != decision.agent_revision or datetime.utcnow() - decision.created_at > max_age:
                        decision.state, decision.result = 'PARTIAL', 'Предупреждение обработано; удаление отменено: сообщение или настройки изменились'
                        decision.completed_at = datetime.utcnow()
                        agent.last_error = decision.result
                        db.session.commit()
                        return decision.state
                await asyncio.wait_for(client.delete_messages(int(agent.chat_id), [decision.message_id], revoke=True), 20)
                decision.state, decision.result = 'DELETED', 'Сообщение удалено' + warning_result
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
        except asyncio.CancelledError:
            db.session.rollback()
            decision = db.session.get(AIDecision, decision_id)
            if decision and decision.state == 'RUNNING':
                decision.state, decision.result = 'UNCERTAIN', 'Выполнение прервано. Проверьте Telegram; автоматического повтора нет'
                db.session.commit()
            raise
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

    async def _delete_and_ban(self, client, agent, decision, max_age):
        decision_id = decision.id
        ban_confirmed, delete_confirmed, in_flight = False, False, None

        def finish_error(exc):
            db.session.rollback()
            row = db.session.get(AIDecision, decision_id)
            rejected = isinstance(exc, errors.RPCError)
            def status(operation, confirmed):
                if confirmed:
                    return 'DONE'
                return ('ERROR' if rejected else 'UNCERTAIN') if in_flight == operation else 'SKIPPED'
            row.ban_status, row.delete_status = status('ban', ban_confirmed), status('delete', delete_confirmed)
            if ban_confirmed and delete_confirmed:
                row.state, row.result = 'DELETED_BANNED', 'Бан и удаление подтверждены'
            elif ban_confirmed:
                row.state = 'PARTIAL'
                row.result = 'Бан подтверждён; ' + ('удаление отклонено Telegram' if row.delete_status == 'ERROR' else
                    'результат удаления не подтверждён' if row.delete_status == 'UNCERTAIN' else 'удаление не запускалось')
                if isinstance(exc, AnalysisError):
                    row.result += ': ' + str(exc)
            else:
                row.state = 'ERROR' if rejected or in_flight is None else 'UNCERTAIN'
                row.result = 'Бан отклонён; удаление не запускалось' if rejected else (
                    'Результат бана не подтверждён; удаление не запускалось' if in_flight else 'Бан и удаление не запускались')
            row.result += '. Автоматического повтора нет'
            row.completed_at = datetime.utcnow()
            agent.last_error = row.result if row.state != 'DELETED_BANNED' else ''
            logger.warning('AI combined action stopped: phase=%s error_type=%s', in_flight, type(exc).__name__)
            db.session.commit()
            return row.state

        try:
            decision.ban_status, decision.delete_status = 'RUNNING', 'PENDING'
            db.session.commit()
            in_flight = 'ban'
            await asyncio.wait_for(client.edit_permissions(int(agent.chat_id), int(decision.user_id), view_messages=False), 20)
            ban_confirmed, in_flight = True, None
            decision.ban_status, decision.result = 'DONE', 'Бан подтверждён; проверяется удаление'
            db.session.commit()
            # Telegram has no atomic ban+delete; recheck before the second mutation.
            current = await asyncio.wait_for(client.get_messages(int(agent.chat_id), ids=decision.message_id), 10)
            permissions = await asyncio.wait_for(client.get_permissions(int(agent.chat_id), 'me'), 10)
            db.session.expire_all()
            record = AIMessage.query.filter_by(agent_id=agent.id, message_id=decision.message_id).first()
            if not self.account_ready(agent) or agent.revision != decision.agent_revision or datetime.utcnow() - decision.created_at > max_age:
                raise AnalysisError('Настройки изменились или решение устарело')
            if not record or record.fingerprint != decision.fingerprint or not current or fingerprint(current) != decision.fingerprint or getattr(current, 'out', False) or str(getattr(current, 'sender_id', '') or '') != decision.user_id:
                raise AnalysisError('Исходное сообщение изменено, удалено или автор не подтверждён')
            if not permissions or not getattr(permissions, 'delete_messages', False) or not agent.allow_delete:
                raise AnalysisError('Нет разрешения или права удаления')
            decision.delete_status = 'RUNNING'
            db.session.commit()
            in_flight = 'delete'
            await asyncio.wait_for(client.delete_messages(int(agent.chat_id), [decision.message_id], revoke=True), 20)
            delete_confirmed, in_flight = True, None
            decision.delete_status = 'DONE'
            decision.state, decision.result = 'DELETED_BANNED', 'Сообщение удалено; автор заблокирован'
            decision.completed_at = datetime.utcnow()
            db.session.commit()
            return decision.state
        except asyncio.CancelledError as exc:
            finish_error(exc)
            raise
        except Exception as exc:
            return finish_error(exc)

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
                            AIDecision.state.in_(('READY', 'REVIEW', 'OBSERVED', 'QUEUED', 'ANALYZING'))).update({'state': 'STALE', 'training_status': 'SUPERSEDED'}, synchronize_session='fetch')
                    db.session.commit()
