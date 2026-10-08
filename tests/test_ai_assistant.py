import asyncio
from contextlib import closing
import json
from datetime import datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image

import test_features as fixtures
from ai_assistant import AIAssistant, policy
from ai_engine import AnalysisError, analysis_resources, analyze, ask, failure_message, fingerprint, image_content, redact, schema
from ai_health import checks, telegram_checks
from ai_knowledge import ingest, source_url_allowed, verified_facts, VisibleText
from models import Account, AIAgent, AIDecision, AIFact, AIFactHistory, AIMessage, AIUserContext, db


def message(message_id=1, text='Hi', **kwargs):
    data = dict(id=message_id, raw_text=text, text=text, out=False, photo=None, document=None, reply_to_msg_id=None, mentioned=False)
    data.update(kwargs)
    return SimpleNamespace(**data)


def decision_result(**kwargs):
    data = dict(language='ENGLISH', intent='GREETING', confidence=0.99, action='REPLY', classification='SAFE',
                reply='Hello!', reason='Initial greeting', fact_ids=[])
    data.update(kwargs)
    return data


class AgentClient:
    def __init__(self):
        self._self_id = 777
        self.messages = {}
        self.sent = []
        self.deleted = []
        self.can_delete = True

    def is_connected(self):
        return True

    async def get_messages(self, chat, ids=None, limit=None):
        if ids is not None:
            return self.messages.get(ids)
        return list(self.messages.values())[:limit]

    async def send_message(self, chat, text, **kwargs):
        self.sent.append((chat, text, kwargs))
        return message(1000 + len(self.sent), text, out=True)

    async def get_permissions(self, chat, user):
        return SimpleNamespace(delete_messages=self.can_delete)

    async def delete_messages(self, chat, ids, **kwargs):
        self.deleted.extend(ids)
        return []


class AssistantTests(unittest.TestCase):
    setUp = fixtures.FeaturesTest.setUp
    tearDown = fixtures.FeaturesTest.tearDown

    def setup_agent(self, **kwargs):
        self.app.config['OPENAI_API_KEY'] = 'test-only-no-network'
        self.telegram = AgentClient()
        self.manager.clients[self.account_id] = self.telegram
        self.assistant = AIAssistant(self.manager)
        self.manager.ai_assistant = self.assistant
        data = dict(account_id=self.account_id, name='Test assistant', chat_id='-100123', channel_id='-100456',
                    mode='OBSERVE', consent=True, community_replies=True)
        data.update(kwargs)
        self.agent = AIAgent(**data)
        db.session.add(self.agent)
        db.session.commit()
        self.agent_created_at = self.agent.created_at.isoformat()
        self.client.get('/assistant/')
        with self.client.session_transaction() as state:
            self.csrf = state['assistant_csrf']

    def event(self, msg=None, user_id=22, chat_id=-100123):
        msg = msg or message()
        self.telegram.messages[msg.id] = msg
        return SimpleNamespace(message=msg, sender_id=user_id, chat_id=chat_id, is_private=False,
                               get_reply_message=AsyncMock(return_value=message(99, 'Official post context')))

    def handle(self, event=None, result=None):
        with patch('ai_assistant.analyze', new=AsyncMock(return_value=result or decision_result())) as ai:
            handled = asyncio.run(self.assistant.handle_message(self.account_id, self.telegram, event or self.event()))
        db.session.expire_all()
        return handled, ai

    def latest(self):
        return AIDecision.query.order_by(AIDecision.id.desc()).first()

    def fact(self, **kwargs):
        data = dict(agent_id=self.agent.id, source_key='test', source_url='https://betjam.com/aa/rules',
                    title='Official reference', summary='The published event has finished.', status='FINISHED',
                    approved=True, verified_at=datetime.utcnow())
        data.update(kwargs)
        fact = AIFact(**data)
        db.session.add(fact)
        db.session.commit()
        return fact

    def form(self, **kwargs):
        data = dict(csrf_token=self.csrf, name='Test assistant', account_id=str(self.account_id), chat_id='-100123',
                    channel_id='-100456', mode='OBSERVE', consent='on', community_replies='on', support_router='on',
                    information='on', scam_detection='on', reply_confidence='0.7', moderation_confidence='0.9',
                    vision_confidence='0.93', chat_cooldown='45', user_cooldown='120', intent_cooldown='300', fact_max_age_hours='72')
        data.update(delete_personal_data='on', delete_payment_data='on', delete_identity_documents='on')
        data.update(kwargs)
        return data

    def test_missing_analysis_resource_reports_filename_before_api(self):
        with patch('ai_engine.Path.read_text', side_effect=FileNotFoundError('private server path')):
            with self.assertRaisesRegex(AnalysisError, 'prompts/assistant_system.txt') as failure:
                analysis_resources()
            self.assertNotIn('private server path', str(failure.exception))
        with patch('ai_engine.Path.read_text', side_effect=['instructions', FileNotFoundError()]):
            with self.assertRaisesRegex(AnalysisError, 'data/ethiopia_slang.json'):
                analysis_resources()

    def test_invalid_analysis_resources_fail_closed(self):
        for values in (['', '{}'], ['instructions', 'bad json'], ['instructions', '[]'], ['instructions', '{"word": 1}']):
            with patch('ai_engine.Path.read_text', side_effect=values):
                with self.assertRaises(AnalysisError):
                    analysis_resources()

    def test_docker_includes_only_required_dictionary(self):
        from pathlib import Path
        rules = (Path(__file__).parents[1] / '.dockerignore').read_text().splitlines()
        self.assertNotIn('data/', rules)
        self.assertIn('data/*', rules)
        self.assertGreater(rules.index('!data/ethiopia_slang.json'), rules.index('data/*'))

    def test_delete_requires_csrf_confirmation_and_off(self):
        self.setup_agent()
        endpoint = f'/assistant/{self.agent.id}/delete'
        self.assertEqual(self.client.post(endpoint, data={'confirm_delete': 'on'}).status_code, 400)
        self.client.post(endpoint, data={'csrf_token': self.csrf})
        self.assertEqual(AIAgent.query.count(), 1)
        self.client.post(endpoint, data={'csrf_token': self.csrf, 'confirm_delete': 'on'})
        self.assertEqual(AIAgent.query.count(), 1)
        self.assertEqual(self.client.get(endpoint).status_code, 405)

    def test_delete_cleans_dependencies_and_allows_new_assignment(self):
        self.setup_agent(mode='OFF')
        agent_id = self.agent.id
        fact = self.fact()
        closing = self.fact(source_key='closing', related_id=fact.id)
        db.session.add(AIFactHistory(fact_id=fact.id, snapshot={'status': 'ACTIVE'}))
        db.session.add(AIFactHistory(fact_id=closing.id, snapshot={'status': 'FINISHED'}))
        db.session.add(AIMessage(agent_id=agent_id, message_id=1, fingerprint='abc'))
        db.session.add(AIUserContext(agent_id=agent_id, user_id='22'))
        db.session.add(AIDecision(agent_id=agent_id, message_id=1, fingerprint='abc', agent_revision=0, knowledge_revision=0, state='REVIEW'))
        db.session.commit()
        # Production can enforce foreign keys; deletion order must work there too.
        db.session.execute(db.text('PRAGMA foreign_keys=ON'))
        self.client.post(f'/assistant/{agent_id}/delete', data={'csrf_token': self.csrf, 'confirm_delete': 'on', 'agent_created_at': self.agent_created_at})
        db.session.expire_all()
        for model in (AIAgent, AIDecision, AIMessage, AIUserContext, AIFact, AIFactHistory):
            self.assertEqual(model.query.count(), 0, model.__name__)
        self.assertIsNotNone(db.session.get(Account, self.account_id))
        self.assertFalse(self.telegram.sent)
        self.assertFalse(self.telegram.deleted)
        self.client.post('/assistant/add', data=self.form(mode='OFF'))
        self.assertEqual(AIAgent.query.count(), 1)

    def test_delete_blocks_inflight_analysis_and_actions(self):
        self.setup_agent(mode='OFF')
        decision = AIDecision(agent_id=self.agent.id, message_id=1, fingerprint='abc', agent_revision=0, knowledge_revision=0)
        db.session.add(decision)
        db.session.commit()
        for state in ('ANALYZING', 'RUNNING'):
            decision.state = state
            db.session.commit()
            self.client.post(f'/assistant/{self.agent.id}/delete', data={'csrf_token': self.csrf, 'confirm_delete': 'on', 'agent_created_at': self.agent_created_at})
            self.assertEqual(AIAgent.query.count(), 1)
            self.assertEqual(AIDecision.query.count(), 1)

    def test_delete_uses_manager_lock(self):
        self.setup_agent(mode='OFF')
        self.manager.loop = object()
        try:
            with patch('web.routes.assistant.run', side_effect=lambda coroutine, **kwargs: asyncio.run(coroutine)):
                response = self.client.post(f'/assistant/{self.agent.id}/delete', data={'csrf_token': self.csrf, 'confirm_delete': 'on', 'agent_created_at': self.agent_created_at})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(AIAgent.query.count(), 0)
        finally:
            self.manager.loop = None

    def test_stale_delete_form_cannot_remove_new_assignment(self):
        self.setup_agent(mode='OFF')
        self.client.post(f'/assistant/{self.agent.id}/delete', data={
            'csrf_token': self.csrf, 'confirm_delete': 'on', 'agent_created_at': 'old assignment'})
        self.assertEqual(AIAgent.query.count(), 1)

    def test_stale_review_form_cannot_execute_recreated_decision(self):
        self.setup_agent(mode='ASSIST')
        self.handle()
        decision = self.latest()
        self.client.post(f'/assistant/decisions/{decision.id}', data={
            'csrf_token': self.csrf, 'action': 'REPLY', 'confirm': 'on', 'decision_created_at': 'old decision'})
        self.assertFalse(self.telegram.sent)
        with self.assertRaisesRegex(AnalysisError, 'Решение изменилось'):
            asyncio.run(self.assistant.execute(decision.id, 'REPLY', created_at='old decision'))
        self.assertFalse(self.telegram.sent)

    def test_queued_event_cannot_run_under_recreated_assignment(self):
        self.setup_agent(mode='AUTO')
        from web.routes.assistant import delete_assignment
        agent_id = self.agent.id
        async def recreate():
            lock = self.assistant.locks[agent_id]
            await lock.acquire()
            pending = asyncio.create_task(self.assistant.handle_message(self.account_id, self.telegram, self.event()))
            await asyncio.sleep(0)
            self.agent.mode = 'OFF'
            db.session.commit()
            delete_assignment(agent_id)
            db.session.add(AIAgent(id=agent_id, account_id=self.account_id, chat_id='-100123', name='New agent',
                                   mode='AUTO', consent=True, community_replies=True))
            db.session.commit()
            lock.release()
            await pending
        with patch('ai_assistant.analyze', new=AsyncMock()) as ai:
            asyncio.run(recreate())
        ai.assert_not_awaited()
        self.assertEqual(AIDecision.query.count(), 0)

    def test_existing_database_gets_category_defaults_without_enabling_delete(self):
        self.setup_agent(mode='OFF', allow_delete=False)
        from pathlib import Path
        import sqlite3
        from config import Config
        from web.app import create_app
        agent_id = self.agent.id
        with db.engine.begin() as connection:
            for name in ('delete_personal_data', 'delete_payment_data', 'delete_identity_documents'):
                connection.execute(db.text(f'ALTER TABLE ai_agents DROP COLUMN {name}'))
        legacy = Path(self.temp.name) / 'legacy.sqlite'
        with db.engine.connect() as connection, closing(sqlite3.connect(legacy)) as destination:
            connection.connection.driver_connection.backup(destination)
        with patch.object(Config, 'DATABASE_URL', 'sqlite:///' + legacy.as_posix()):
            migrated_app = create_app()
        with migrated_app.app_context():
            agent = db.session.get(AIAgent, agent_id)
            self.assertTrue(agent.delete_personal_data)
            self.assertTrue(agent.delete_payment_data)
            self.assertTrue(agent.delete_identity_documents)
            self.assertFalse(agent.allow_delete)
            db.session.remove()
            db.engine.dispose()

    def test_unselected_sensitive_categories_warn_instead_of_delete(self):
        self.setup_agent(mode='AUTO', allow_delete=True)
        for classification, flag in [('PERSONAL_DATA', 'delete_personal_data'), ('PAYMENT_DATA', 'delete_payment_data'), ('IDENTITY_DOCUMENT', 'delete_identity_documents')]:
            setattr(self.agent, flag, False)
            db.session.commit()
            self.handle(self.event(message(AIDecision.query.count() + 1)), decision_result(classification=classification, action='DELETE'))
            self.assertEqual(self.latest().action, 'WARN')
            self.assertEqual(self.latest().state, 'SENT')
            self.assertFalse(self.telegram.deleted)
            self.agent.last_reply_at = None
            AIUserContext.query.delete()
            setattr(self.agent, flag, True)
            db.session.commit()

    def test_category_settings_save_and_render(self):
        self.setup_agent()
        form = self.form()
        form.pop('delete_payment_data')
        self.client.post(f'/assistant/{self.agent.id}', data=form)
        db.session.expire_all()
        self.assertFalse(self.agent.delete_payment_data)
        self.assertTrue(self.agent.delete_personal_data)
        page = self.client.get(f'/assistant/{self.agent.id}').get_data(as_text=True)
        self.assertIn('Диагностика', page)
        self.assertIn('Категории автоудаления', page)
        self.assertIn('Удалить назначение ассистента', page)
        self.assertNotIn('test-only-no-network', page)

    def test_health_reports_resources_offline_and_last_failure(self):
        self.setup_agent()
        self.manager.clients.clear()
        self.agent.last_error = 'Test failure'
        with patch('ai_health.analysis_resources', side_effect=AnalysisError('Missing dictionary')):
            result = checks(self.app, self.agent)
        rows = {row['key']: row for row in result['rows']}
        self.assertEqual(rows['connection']['state'], 'error')
        self.assertEqual(rows['resources']['detail'], 'Missing dictionary')
        self.assertEqual(rows['last_error']['detail'], 'Test failure')
        self.assertEqual(rows['key']['state'], 'warning')

    def test_telegram_diagnostics_are_read_only_and_expire(self):
        self.setup_agent(allow_delete=True)
        self.telegram.can_delete = False
        with patch('web.routes.assistant.run', side_effect=lambda coroutine, **kwargs: asyncio.run(coroutine)):
            response = self.client.post(f'/assistant/{self.agent.id}/check', data={'csrf_token': self.csrf})
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as state:
            saved = state['assistant_checks'][str(self.agent.id)]
        result = checks(self.app, self.agent, saved)
        rows = {row['key']: row for row in result['rows']}
        self.assertEqual(rows['membership']['state'], 'ok')
        self.assertEqual(rows['delete_rights']['state'], 'error')
        self.assertFalse(self.telegram.sent)
        self.assertFalse(self.telegram.deleted)
        saved['at'] = (datetime.utcnow() - timedelta(minutes=6)).isoformat()
        self.assertIsNone(checks(self.app, self.agent, saved)['checked_at'])
        saved['at'] = datetime.utcnow().isoformat()
        saved['revision'] += 1
        self.assertIsNone(checks(self.app, self.agent, saved)['checked_at'])

    def test_telegram_diagnostics_never_show_raw_error_data(self):
        client = SimpleNamespace(get_permissions=AsyncMock(side_effect=RuntimeError('secret payment details')), get_messages=AsyncMock())
        rows = asyncio.run(telegram_checks(client, '-100123', '', True))
        self.assertEqual(rows[0]['state'], 'error')
        self.assertNotIn('secret', rows[0]['detail'])

    def test_off_and_unassigned_chats_do_not_call_ai(self):
        self.setup_agent(mode='OFF')
        handled, ai = self.handle()
        self.assertFalse(handled)
        ai.assert_not_awaited()
        self.assertEqual(AIDecision.query.count(), 0)

    def test_observe_analyzes_reply_context_without_actions(self):
        self.setup_agent()
        event = self.event(message(reply_to_msg_id=99))
        handled, ai = self.handle(event)
        self.assertTrue(handled)
        self.assertEqual(self.latest().state, 'OBSERVED')
        self.assertEqual(ai.call_args.args[1]['reply_to']['text'], 'Official post context')
        self.assertEqual(self.telegram.sent, [])
        self.assertEqual(self.telegram.deleted, [])

    def test_assist_queues_and_requires_confirmation(self):
        self.setup_agent(mode='ASSIST')
        self.handle()
        decision = self.latest()
        self.assertEqual(decision.state, 'REVIEW')
        response = self.client.post(f'/assistant/decisions/{decision.id}', data={'csrf_token': self.csrf, 'action': 'REPLY', 'decision_created_at': decision.created_at.isoformat()})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.telegram.sent)
        with patch('web.routes.assistant.run', side_effect=lambda coroutine, **kwargs: asyncio.run(coroutine)):
            self.client.post(f'/assistant/decisions/{decision.id}', data={'csrf_token': self.csrf, 'action': 'REPLY', 'confirm': 'on', 'decision_created_at': decision.created_at.isoformat()})
        db.session.expire_all()
        self.assertEqual(self.latest().state, 'SENT')
        self.assertEqual(len(self.telegram.sent), 1)

    def test_auto_greeting_only_once_and_duplicate_event_is_not_reanalyzed(self):
        self.setup_agent(mode='AUTO', chat_cooldown=10, user_cooldown=10, intent_cooldown=10)
        event = self.event()
        self.handle(event)
        self.assertEqual(self.latest().state, 'SENT')
        self.assertEqual(AIUserContext.query.one().greeted, True)
        _, ai = self.handle(event)
        ai.assert_not_awaited()
        self.handle(self.event(message(2)))
        self.assertEqual(self.latest().state, 'IGNORED')
        self.assertEqual(len(self.telegram.sent), 1)

    def test_support_never_invents_transaction_status(self):
        self.setup_agent(mode='AUTO')
        self.handle(result=decision_result(intent='DEPOSIT_PROBLEM', reply='Your money will arrive tomorrow.'))
        text = self.telegram.sent[0][1]
        self.assertIn('support@betjam.com', text)
        self.assertNotIn('tomorrow', text)

    def test_information_requires_verified_fresh_facts_and_appends_source(self):
        self.setup_agent(mode='AUTO')
        self.handle(result=decision_result(intent='PROMO_INFO_REQUEST', fact_ids=[999], reply='This event has finished.'))
        self.assertEqual(self.latest().state, 'REVIEW')
        fact = self.fact()
        self.handle(self.event(message(2, 'Is it active?')), decision_result(intent='PROMO_INFO_REQUEST', fact_ids=[fact.id], reply='This event has finished.'))
        self.assertEqual(self.latest().state, 'SENT')
        self.assertIn(fact.source_url, self.telegram.sent[0][1])
        fact.verified_at = datetime.utcnow() - timedelta(days=4)
        db.session.commit()
        self.assertEqual(verified_facts(self.agent), [])

    def test_practical_gambling_instructions_go_to_review(self):
        self.setup_agent(mode='AUTO')
        self.handle(result=decision_result(intent='GAMBLING_INSTRUCTIONS'))
        self.assertEqual(self.latest().state, 'REVIEW')
        self.assertFalse(self.telegram.sent)

    def test_safe_image_is_not_deleted(self):
        self.setup_agent(mode='AUTO', vision=True, allow_delete=True)
        with patch('ai_assistant.image_content', new=AsyncMock(return_value={'type': 'input_image'})):
            self.handle(self.event(message(photo=SimpleNamespace(id=1))), decision_result(action='DELETE', classification='SAFE'))
        self.assertEqual(self.latest().state, 'REVIEW')
        self.assertFalse(self.telegram.deleted)

    def test_sensitive_image_deletes_only_with_threshold_permission_and_warning(self):
        self.setup_agent(mode='AUTO', vision=True, allow_delete=True)
        with patch('ai_assistant.image_content', new=AsyncMock(return_value={'type': 'input_image'})):
            self.handle(self.event(message(text='', photo=SimpleNamespace(id=1))),
                        decision_result(intent='PAYMENT_DATA', action='DELETE', classification='PAYMENT_DATA', confidence=0.98))
        self.assertEqual(self.telegram.deleted, [1])
        self.assertEqual(self.latest().state, 'DELETED')
        self.assertEqual(AIMessage.query.filter_by(message_id=1).one().text, '[PRIVATE MESSAGE]')
        self.assertEqual(len(self.telegram.sent), 1)
        self.assertIn('support@betjam.com', self.telegram.sent[0][1])
        self.assertEqual(self.latest().language, 'AMHARIC')

    def test_receipt_without_caption_warns_in_amharic_even_without_delete_permission(self):
        self.setup_agent(mode='AUTO', vision=True, allow_delete=False)
        with patch('ai_assistant.image_content', new=AsyncMock(return_value={'type': 'input_image'})):
            self.handle(self.event(message(text='', photo=SimpleNamespace(id=1))), decision_result(
                intent='DEPOSIT_PROBLEM', action='DELETE', classification='PAYMENT_DATA', reply='A transfer was credited.'))
        self.assertEqual(self.latest().state, 'SENT')
        self.assertEqual(self.latest().intent, 'PAYMENT_DATA')
        self.assertEqual(self.latest().language, 'AMHARIC')
        self.assertEqual(self.latest().action, 'WARN')
        self.assertFalse(self.telegram.deleted)
        reply = self.telegram.sent[0][1]
        self.assertIn('እባክዎ', reply)
        self.assertIn('support@betjam.com', reply)
        self.assertNotIn('credited', reply)
        self.assertNotIn('[PRIVATE]', reply)

    def test_sensitive_warn_does_not_need_generated_reply(self):
        self.setup_agent(mode='AUTO', vision=True)
        with patch('ai_assistant.image_content', new=AsyncMock(return_value={'type': 'input_image'})):
            self.handle(self.event(message(text='', photo=SimpleNamespace(id=1))), decision_result(
                intent='PAYMENT_DATA', action='WARN', classification='PAYMENT_DATA', reply=''))
        self.assertEqual(self.latest().state, 'SENT')
        self.assertIn('support@betjam.com', self.telegram.sent[0][1])

    def test_image_caption_language_is_preserved_and_support_toggle_is_honored(self):
        self.setup_agent(mode='AUTO', vision=True, support_router=False)
        with patch('ai_assistant.image_content', new=AsyncMock(return_value={'type': 'input_image'})):
            self.handle(self.event(message(text='Please help', photo=SimpleNamespace(id=1))), decision_result(
                intent='PAYMENT_DATA', action='WARN', classification='PAYMENT_DATA'))
        self.assertEqual(self.latest().language, 'ENGLISH')
        self.assertNotIn('support@', self.telegram.sent[0][1])
        self.assertIn('Do not publish', self.telegram.sent[0][1])

    def test_plain_receipt_without_private_data_is_not_deleted(self):
        self.setup_agent(mode='AUTO', vision=True, allow_delete=True)
        with patch('ai_assistant.image_content', new=AsyncMock(return_value={'type': 'input_image'})):
            self.handle(self.event(message(text='', photo=SimpleNamespace(id=1))), decision_result(
                intent='SIMPLE_REACTION', action='IGNORE', classification='PAYMENT_SCREENSHOT'))
        self.assertEqual(self.latest().state, 'IGNORED')
        self.assertFalse(self.telegram.deleted)
        self.assertFalse(self.telegram.sent)

    def test_image_failure_reports_stage_without_echoing_private_error(self):
        self.setup_agent(mode='AUTO', vision=True)
        with patch('ai_assistant.image_content', new=AsyncMock(side_effect=RuntimeError('PRIVATE_RECEIPT_SECRET'))):
            _, ai = self.handle(self.event(message(text='', photo=SimpleNamespace(id=1))))
        ai.assert_not_awaited()
        self.assertIn('Получение изображения', self.latest().result)
        self.assertIn('RuntimeError', self.latest().result)
        self.assertNotIn('PRIVATE_RECEIPT_SECRET', self.latest().result)
        self.assertFalse(self.telegram.sent)
        self.assertFalse(self.telegram.deleted)

    def test_language_setting_is_validated_and_saved(self):
        self.setup_agent()
        self.client.post(f'/assistant/{self.agent.id}', data=self.form(fallback_language='UNKNOWN'))
        db.session.expire_all()
        self.assertEqual(self.agent.fallback_language, 'AMHARIC')
        self.client.post(f'/assistant/{self.agent.id}', data=self.form(fallback_language='OROMO'))
        db.session.expire_all()
        self.assertEqual(self.agent.fallback_language, 'OROMO')

    def test_image_pipeline_with_fake_responses_api_sends_privacy_notice(self):
        self.setup_agent(mode='AUTO', vision=True)
        self.app.config['OPENAI_MODEL'] = 'gpt-6-luna'
        image = BytesIO()
        Image.new('RGB', (20, 20), 'red').save(image, 'PNG')
        async def download(msg, file, progress_callback):
            file.write(image.getvalue())
            progress_callback(len(image.getvalue()), len(image.getvalue()))
        self.telegram.download_media = download
        response = SimpleNamespace(status='completed', output_text=json.dumps(decision_result(
            intent='PAYMENT_DATA', classification='PAYMENT_DATA', action='WARN', reply='')))
        api, context = AssistantEngineTests().api_client(response)
        with patch('ai_engine.AsyncOpenAI', return_value=context):
            asyncio.run(self.assistant.handle_message(self.account_id, self.telegram, self.event(message(text='', photo=SimpleNamespace(id=1)))))
        db.session.expire_all()
        self.assertEqual(self.latest().state, 'SENT')
        self.assertEqual(self.latest().language, 'AMHARIC')
        self.assertIn('support@betjam.com', self.telegram.sent[0][1])
        self.assertFalse(self.telegram.deleted)
        request = api.responses.create.call_args.kwargs
        self.assertEqual(request['input'][0]['content'][1]['detail'], 'high')

    def test_low_confidence_or_no_permissions_does_not_delete(self):
        self.setup_agent(mode='AUTO', allow_delete=True)
        self.handle(result=decision_result(intent='PERSONAL_DATA', action='DELETE', classification='PERSONAL_DATA', confidence=0.8))
        self.assertEqual(self.latest().state, 'REVIEW')
        self.telegram.can_delete = False
        self.handle(self.event(message(2)), decision_result(intent='PERSONAL_DATA', action='DELETE', classification='PERSONAL_DATA'))
        self.assertEqual(self.latest().state, 'REVIEW')
        self.assertFalse(self.telegram.deleted)

    def test_api_or_vision_failure_cannot_send_or_delete(self):
        self.setup_agent(mode='AUTO', allow_delete=True, vision=True)
        with patch('ai_assistant.analyze', new=AsyncMock(side_effect=ValueError('API offline'))):
            asyncio.run(self.assistant.handle_message(self.account_id, self.telegram, self.event()))
        with patch('ai_assistant.image_content', new=AsyncMock(side_effect=ValueError('Vision offline'))):
            self.handle(self.event(message(2, photo=SimpleNamespace(id=1))))
        self.assertFalse(self.telegram.deleted)
        self.assertFalse(self.telegram.sent)

    def test_uncertain_delivery_is_not_retried(self):
        self.setup_agent(mode='AUTO')
        with patch.object(self.telegram, 'send_message', new=AsyncMock(side_effect=TimeoutError)) as send:
            self.handle()
            self.assertEqual(self.latest().state, 'UNCERTAIN')
            decision_id = self.latest().id
            self.assertEqual(asyncio.run(self.assistant.execute(decision_id, 'REPLY')), 'UNCERTAIN')
            send.assert_awaited_once()

    def test_edit_or_settings_change_invalidates_pending_action(self):
        self.setup_agent(mode='ASSIST')
        self.handle()
        decision = self.latest()
        self.telegram.messages[1].raw_text = 'Edited outside'
        self.assertEqual(asyncio.run(self.assistant.execute(decision.id, 'REPLY')), 'STALE')
        self.assertFalse(self.telegram.sent)
        self.handle(self.event(message(2)))
        self.agent.revision += 1
        db.session.commit()
        self.assertEqual(asyncio.run(self.assistant.execute(self.latest().id, 'REPLY')), 'STALE')

    def test_edited_channel_with_failed_parse_invalidates_approved_fact(self):
        self.setup_agent()
        fact = self.fact(source_key='tg:9', source_message_id=9, fingerprint='old')
        with patch('ai_knowledge.parse_source', new=AsyncMock(side_effect=ValueError('offline'))):
            with self.assertRaises(ValueError):
                asyncio.run(ingest(self.app, self.agent, 'Edited source', 'tg:9', 'https://t.me/c/456/9', 9))
        db.session.expire_all()
        self.assertFalse(db.session.get(AIFact, fact.id).approved)
        self.assertEqual(len(fact.history), 1)

    def test_deleted_channel_sources_cannot_be_used(self):
        self.setup_agent()
        fact = self.fact(source_message_id=9)
        asyncio.run(self.assistant.handle_deleted(self.account_id, SimpleNamespace(chat_id=-100456, deleted_ids=[9])))
        db.session.expire_all()
        self.assertTrue(db.session.get(AIFact, fact.id).deleted)
        self.assertFalse(verified_facts(self.agent))

    def test_own_messages_and_wrong_chats_are_not_analyzed(self):
        self.setup_agent()
        handled, ai = self.handle(self.event(message(out=True)))
        ai.assert_not_awaited()
        handled, ai = self.handle(self.event(chat_id=-100999))
        self.assertFalse(handled)
        ai.assert_not_awaited()

    def test_keyword_rules_remain_independent_outside_assigned_chat(self):
        self.setup_agent()
        self.manager._trigger_modes[self.account_id] = {'all_messages'}
        with patch('ai_assistant.analyze', new=AsyncMock(return_value=decision_result())), patch('message_handler.check_keywords', new=AsyncMock()) as keywords:
            asyncio.run(self.manager._handle_incoming_message(self.account_id, self.telegram, self.event()))
            keywords.assert_not_awaited()
            asyncio.run(self.manager._handle_incoming_message(self.account_id, self.telegram, self.event(message(2), chat_id=-100999)))
            keywords.assert_awaited_once()

    def test_changed_knowledge_blocks_pending_information_reply(self):
        self.setup_agent(mode='ASSIST')
        fact = self.fact()
        self.handle(result=decision_result(intent='INFORMATION', fact_ids=[fact.id], reply='The event has finished.'))
        decision_id = self.latest().id
        self.agent.knowledge_revision += 1
        db.session.commit()
        with self.assertRaises(ValueError):
            asyncio.run(self.assistant.execute(decision_id, 'REPLY'))
        self.assertFalse(self.telegram.sent)

    def test_user_and_chat_cooldowns_do_not_remove_sensitive_messages_policy(self):
        self.setup_agent(mode='AUTO')
        self.handle(result=decision_result(intent='FOOTBALL_DISCUSSION'))
        self.handle(self.event(message(2, 'Question')), decision_result(intent='CASUAL_CHAT'))
        self.assertEqual(self.latest().state, 'IGNORED')
        self.assertEqual(len(self.telegram.sent), 1)

    def test_deleted_closing_post_invalidates_linked_source(self):
        self.setup_agent()
        root = self.fact()
        child = self.fact(source_key='end', source_message_id=55, related_id=root.id)
        asyncio.run(self.assistant.handle_deleted(self.account_id, SimpleNamespace(chat_id=-100456, deleted_ids=[55])))
        db.session.expire_all()
        self.assertFalse(root.approved)
        self.assertFalse(verified_facts(self.agent))

    def test_channel_new_and_edit_keep_one_record_unapproved(self):
        self.setup_agent()
        parsed = dict(title='Reference', summary='Official reference summary', code='', status='UNKNOWN', end_at=None)
        event = self.event(message(20, 'Official published text'), chat_id=-100456)
        with patch('ai_knowledge.parse_source', new=AsyncMock(return_value=parsed)) as parse:
            asyncio.run(self.assistant.handle_channel_post(self.account_id, self.telegram, event))
            asyncio.run(self.assistant.handle_channel_post(self.account_id, self.telegram, event))
            self.assertEqual(parse.await_count, 1)
            event.message.raw_text = 'Edited published text'
            asyncio.run(self.assistant.handle_channel_post(self.account_id, self.telegram, event))
        db.session.expire_all()
        self.assertEqual(AIFact.query.count(), 1)
        self.assertFalse(AIFact.query.one().approved)
        self.assertEqual(len(AIFact.query.one().history), 1)

    def test_account_removal_disables_agent_without_dropping_journal(self):
        self.setup_agent()
        self.handle()
        self.app.telegram_manager = None
        self.client.post(f'/accounts/{self.account_id}/delete')
        db.session.expire_all()
        self.assertEqual(self.agent.mode, 'OFF')
        self.assertIsNone(self.agent.account_id)
        self.assertEqual(AIDecision.query.count(), 1)

    def test_stale_and_expired_facts(self):
        self.setup_agent()
        fact = self.fact(status='ACTIVE', end_at=datetime.utcnow() - timedelta(minutes=1))
        self.assertEqual(verified_facts(self.agent)[0]['status'], 'EXPIRED')
        fact.approved = False
        db.session.commit()
        self.assertEqual(verified_facts(self.agent), [])

    def test_modes_consent_thresholds_and_unique_chat_validation(self):
        self.setup_agent(mode='OFF')
        self.assertEqual(self.client.post('/assistant/add', data=self.form(csrf_token='bad')).status_code, 400)
        for changes in (dict(mode='AUTO'), dict(consent=''), dict(reply_confidence='nan'), dict(chat_id='123'), dict(channel_id='-100123')):
            self.client.post(f'/assistant/{self.agent.id}', data=self.form(**changes))
            db.session.expire_all()
            self.assertEqual(self.agent.mode, 'OFF')
        self.client.post('/assistant/add', data=self.form(mode='OFF'))
        self.assertEqual(AIAgent.query.count(), 1)

    def test_manual_fact_confirmation_edit_history_and_closing_link(self):
        self.setup_agent()
        source = dict(csrf_token=self.csrf, title='Official status', summary='The event is active.', status='ACTIVE',
                      source_url='https://betjam.com/aa/info', verified='on')
        self.client.post(f'/assistant/{self.agent.id}/facts', data=source)
        root = AIFact.query.one()
        self.assertTrue(root.approved)
        closing = self.fact(source_key='closing', title='Ended', summary='The event has finished.', status='FINISHED')
        self.client.post(f'/assistant/facts/{closing.id}', data=dict(csrf_token=self.csrf, action='save', title='Ended',
            summary='The event has finished.', code='', status='FINISHED', verified='on', related_id=str(root.id)))
        db.session.expire_all()
        self.assertEqual(root.status, 'FINISHED')
        self.assertEqual(closing.related_id, root.id)
        facts = verified_facts(self.agent)
        self.assertEqual(len(facts), 1)
        self.assertIn(closing.source_url, facts[0]['related_sources'])
        self.assertTrue(root.history)

    def test_all_pages_escape_generated_text(self):
        self.setup_agent()
        self.fact(title='<script>alert(1)</script>')
        for tab in ('journal', 'review', 'knowledge', 'deleted', 'scam'):
            response = self.client.get(f'/assistant/{self.agent.id}?tab={tab}')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
        page = self.client.get(f'/assistant/{self.agent.id}?tab=knowledge').get_data(as_text=True)
        self.assertIn('&lt;script&gt;', page)
        self.assertNotIn('<script>alert', page)
        self.assertEqual(self.client.get('/assistant/').status_code, 200)

    def test_detaching_closing_record_invalidates_previous_parent(self):
        self.setup_agent()
        root = self.fact()
        closing = self.fact(source_key='closing', related_id=root.id)
        self.client.post(f'/assistant/facts/{closing.id}', data=dict(csrf_token=self.csrf, action='save',
            title='Closing reference', summary='Ended.', code='', status='FINISHED', verified='on', related_id=''))
        db.session.expire_all()
        self.assertIsNone(closing.related_id)
        self.assertFalse(root.approved)
        self.assertTrue(root.history)

    def test_revoking_linked_confirmation_invalidates_parent(self):
        self.setup_agent()
        root = self.fact()
        closing = self.fact(source_key='closing', related_id=root.id)
        self.client.post(f'/assistant/facts/{closing.id}', data=dict(csrf_token=self.csrf, action='unapprove'))
        db.session.expire_all()
        self.assertFalse(root.approved)
        self.assertFalse(closing.approved)

    def test_settings_change_during_source_parse_discards_result(self):
        self.setup_agent()
        async def parse(app, text):
            self.agent.revision += 1
            self.agent.mode = 'OFF'
            db.session.commit()
            return dict(title='Old source', summary='Old summary', code='', status='UNKNOWN', end_at=None)
        with patch('ai_knowledge.parse_source', new=parse):
            with self.assertRaises(ValueError):
                asyncio.run(ingest(self.app, self.agent, 'Source text', 'test', 'https://betjam.com/aa/info'))
        self.assertEqual(AIFact.query.count(), 0)

    def test_dismiss_during_message_recheck_cannot_send(self):
        self.setup_agent(mode='ASSIST')
        self.handle()
        decision_id = self.latest().id
        async def fetch(chat, ids):
            db.session.get(AIDecision, decision_id).state = 'DISMISSED'
            db.session.commit()
            return self.telegram.messages[ids]
        with patch.object(self.telegram, 'get_messages', new=fetch):
            with self.assertRaises(ValueError):
                asyncio.run(self.assistant.execute(decision_id, 'REPLY'))
        self.assertFalse(self.telegram.sent)

    def test_revoking_consent_during_image_download_skips_openai(self):
        self.setup_agent(mode='AUTO', vision=True)
        async def download(client, msg):
            current_agent = db.session.get(AIAgent, self.agent.id)
            current_agent.consent = False
            current_agent.revision += 1
            db.session.commit()
            return {'type': 'input_image'}
        with patch('ai_assistant.image_content', new=download):
            _, ai = self.handle(self.event(message(photo=SimpleNamespace(id=1))))
        ai.assert_not_awaited()
        self.assertEqual(self.latest().state, 'STALE')
        self.assertFalse(self.telegram.sent)

    def test_startup_recovers_interrupted_actions_without_retry(self):
        from pathlib import Path
        from sqlalchemy import inspect, text
        from config import Config
        from web.app import create_app
        with patch.object(Config, 'DATABASE_URL', 'sqlite:///' + (Path(self.temp.name) / 'recovery.db').as_posix()):
            previous = create_app()
            with previous.app_context():
                agent = AIAgent(name='Recovery', chat_id='-100123')
                db.session.add(agent)
                db.session.flush()
                for i, state in enumerate(('RUNNING', 'ANALYZING'), 1):
                    db.session.add(AIDecision(agent_id=agent.id, message_id=i, fingerprint=str(i),
                        agent_revision=0, knowledge_revision=0, state=state))
                db.session.commit()
                db.session.execute(text('ALTER TABLE ai_agents DROP COLUMN fallback_language'))
                db.session.commit()
                db.session.remove()
                db.engine.dispose()
            for _ in range(2):
                recovered = create_app()
                with recovered.app_context():
                    self.assertEqual([row.state for row in AIDecision.query.order_by(AIDecision.id)], ['UNCERTAIN', 'REVIEW'])
                    self.assertIn('related_id', {row['name'] for row in inspect(db.engine).get_columns('ai_facts')})
                    self.assertEqual(AIAgent.query.one().fallback_language, 'AMHARIC')
                    db.session.remove()
                    db.engine.dispose()


class AssistantEngineTests(unittest.TestCase):
    def test_source_allowlist_and_html_script_exclusion(self):
        self.assertTrue(source_url_allowed('https://betjam.com/aa/rules'))
        self.assertTrue(source_url_allowed('https://t.me/c/456/1', '-100456'))
        for url in ('http://betjam.com/aa/', 'https://betjam.com.evil/aa/', 'https://user@betjam.com/aa/', 'http://127.0.0.1/', 'https://t.me/c/777/1'):
            self.assertFalse(source_url_allowed(url, '-100456'))
        parser = VisibleText()
        parser.feed('<h1>Official text</h1><script>bad()</script><style>hidden</style><p>Details</p>')
        self.assertEqual(parser.parts, ['Official text', 'Details'])

    def test_redaction_does_not_store_contact_or_payment_identifiers(self):
        text = redact('Call +251912345678 or x@example.com; OTP: 123456; card number: 12345678')
        self.assertNotIn('+251912345678', text)
        self.assertNotIn('x@example.com', text)
        self.assertNotIn('123456', text)

    def test_structured_output_and_invalid_response_fail_closed(self):
        app = SimpleNamespace(config={'OPENAI_API_KEY': 'test', 'OPENAI_MODEL': 'gpt-4.1-mini'})
        with patch('ai_engine.ask', new=AsyncMock(return_value=decision_result(confidence=2))):
            with self.assertRaises(ValueError):
                asyncio.run(analyze(app, dict(message='Hello')))
        with patch('ai_engine.ask', new=AsyncMock(return_value=decision_result())) as ask:
            result = asyncio.run(analyze(app, dict(message='Hello')))
            self.assertEqual(result['intent'], 'GREETING')
            self.assertTrue(ask.call_args.args[4]['properties']['fact_ids'])
            self.assertIn('awo', ask.call_args.args[3]['slang'])

    def test_image_download_is_in_memory_and_metadata_is_not_transmitted(self):
        stream = BytesIO()
        Image.new('RGB', (20, 20), 'red').save(stream, 'PNG')
        async def download(msg, file, progress_callback):
            file.write(stream.getvalue())
            progress_callback(len(stream.getvalue()), len(stream.getvalue()))
        client = SimpleNamespace(download_media=download)
        result = asyncio.run(image_content(client, message(photo=SimpleNamespace(id=1))))
        self.assertTrue(result['image_url'].startswith('data:image/jpeg;base64,'))
        self.assertEqual(result['detail'], 'high')
        with self.assertRaises(ValueError):
            asyncio.run(image_content(client, message(document=SimpleNamespace(id=2, mime_type='image/png', size=11 * 1024 * 1024))))

    def api_client(self, response=None, error=None):
        api = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=response, side_effect=error)))
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=api)
        context.__aexit__ = AsyncMock(return_value=False)
        return api, context

    def test_responses_request_contains_image_and_model_compatible_reasoning(self):
        response = SimpleNamespace(status='completed', output_text=json.dumps(decision_result()))
        for model, effort in (('gpt-6-luna', 'none'), ('gpt-6-astra', 'low'), ('gpt-6.1-sol', 'low')):
            api, context = self.api_client(response)
            app = SimpleNamespace(config={'OPENAI_API_KEY': 'test-no-network', 'OPENAI_MODEL': model})
            with patch('ai_engine.AsyncOpenAI', return_value=context):
                result = asyncio.run(analyze(app, {'preferred_language': 'AMHARIC'}, {'type': 'input_image', 'detail': 'high', 'image_url': 'data:image/jpeg;base64,test'}))
            self.assertEqual(result['intent'], 'GREETING')
            request = api.responses.create.call_args.kwargs
            self.assertEqual(request['reasoning'], {'effort': effort})
            self.assertFalse(request['store'])
            self.assertEqual(request['input'][0]['content'][1]['type'], 'input_image')
            self.assertIn('AMHARIC', request['input'][0]['content'][0]['text'])

    def test_api_errors_have_safe_diagnostics_not_raw_body(self):
        import httpx
        from openai import BadRequestError
        response = httpx.Response(400, request=httpx.Request('POST', 'https://api.openai.com/v1/responses'))
        error = BadRequestError('PRIVATE_RECEIPT_SECRET', response=response, body={'code': 'invalid_image', 'message': 'PRIVATE_RECEIPT_SECRET'})
        _, context = self.api_client(error=error)
        app = SimpleNamespace(config={'OPENAI_API_KEY': 'test-no-network', 'OPENAI_MODEL': 'gpt-6-luna'})
        with patch('ai_engine.AsyncOpenAI', return_value=context):
            with self.assertRaises(AnalysisError) as caught:
                asyncio.run(ask(app, 'test', 'Test', {}, schema({})))
        result = failure_message(caught.exception, 'Анализ OpenAI')
        self.assertIn('HTTP 400', result)
        self.assertIn('изображение', result)
        self.assertNotIn('PRIVATE_RECEIPT_SECRET', result)

    def test_incomplete_response_reports_token_limit(self):
        response = SimpleNamespace(status='incomplete', output_text='', incomplete_details=SimpleNamespace(reason='max_output_tokens'))
        _, context = self.api_client(response)
        app = SimpleNamespace(config={'OPENAI_API_KEY': 'test-no-network', 'OPENAI_MODEL': 'gpt-6-luna'})
        with patch('ai_engine.AsyncOpenAI', return_value=context):
            with self.assertRaisesRegex(AnalysisError, 'лимит ответа'):
                asyncio.run(ask(app, 'test', 'Test', {}, schema({})))
