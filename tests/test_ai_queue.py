import asyncio
from datetime import datetime, timedelta
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from openai import APIConnectionError, APIStatusError
from telethon import errors

from ai_engine import AnalysisError
from ai_queue import MAX_AGE, queue_status, recover_analysis, retry_delay
from models import AIAgent, AIDecision, AIMessage, db
import test_ai_assistant as fixtures
from test_ai_assistant import decision_result, message


class QueueTests(unittest.TestCase):
    setUp = fixtures.AssistantTests.setUp
    tearDown = fixtures.AssistantTests.tearDown
    setup_agent = fixtures.AssistantTests.setup_agent
    event = fixtures.AssistantTests.event
    handle = fixtures.AssistantTests.handle
    latest = fixtures.AssistantTests.latest

    def enqueue(self, message_id=1, text='Hi'):
        event = self.event(message(message_id, text, sender_id=22))
        return self.assistant._queue_message(self.agent, event)

    def due(self):
        db.session.expire_all()
        self.latest().next_analysis_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()

    def drain(self, result=None, error=None):
        with patch('ai_assistant.analyze', new=AsyncMock(return_value=result or decision_result(), side_effect=error)) as ai:
            asyncio.run(self.assistant.process_queue())
        db.session.expire_all()
        return ai

    def test_rate_limit_persists_message_without_spending_attempt(self):
        self.setup_agent(mode='AUTO')
        self.assistant.analysis_times[self.agent.id].extend([time.monotonic()] * 12)
        handled, ai = self.handle(self.event(message(sender_id=22)))
        self.assertTrue(handled)
        ai.assert_not_awaited()
        row = self.latest()
        self.assertEqual(row.state, 'QUEUED')
        self.assertEqual(row.analysis_attempts, 0)
        self.assertGreater(row.next_analysis_at, datetime.utcnow())
        self.assistant.analysis_times[self.agent.id].clear()
        self.due()
        self.drain().assert_awaited_once()
        self.assertEqual(self.latest().state, 'SENT')
        self.assertEqual(len(self.telegram.sent), 1)

    def test_transient_analysis_retries_same_decision_and_sends_once(self):
        self.setup_agent(mode='AUTO')
        event = self.event(message(sender_id=22))
        with patch('ai_assistant.analyze', new=AsyncMock(side_effect=TimeoutError('private raw details'))):
            asyncio.run(self.assistant.handle_message(self.account_id, self.telegram, event))
        db.session.expire_all()
        row = self.latest()
        self.assertEqual(row.state, 'QUEUED')
        self.assertEqual(row.analysis_attempts, 1)
        self.assertFalse(row.training_needed)
        self.assertNotIn('private raw details', row.result)
        self.assertFalse(self.telegram.sent)
        self.drain().assert_not_awaited()
        self.due()
        self.drain().assert_awaited_once()
        self.assertEqual(AIDecision.query.count(), 1)
        self.assertEqual(self.latest().analysis_attempts, 2)
        self.assertEqual(self.latest().state, 'SENT')
        self.drain().assert_not_awaited()
        self.assertEqual(len(self.telegram.sent), 1)

    def test_three_failures_stop_without_training_or_action(self):
        self.setup_agent(mode='AUTO')
        self.enqueue()
        for _ in range(3):
            self.due()
            self.drain(error=TimeoutError())
        self.assertEqual(self.latest().state, 'REVIEW')
        self.assertEqual(self.latest().analysis_attempts, 3)
        self.assertFalse(self.latest().training_needed)
        self.drain().assert_not_awaited()
        self.assertFalse(self.telegram.sent)

    def test_invalid_analysis_is_not_retried(self):
        self.setup_agent()
        self.enqueue()
        self.drain(error=AnalysisError('Invalid structured result'))
        self.assertEqual(self.latest().state, 'REVIEW')
        self.assertEqual(self.latest().analysis_attempts, 1)
        self.drain().assert_not_awaited()

    def test_offline_account_waits_without_spending_attempt(self):
        self.setup_agent(mode='AUTO')
        self.enqueue()
        self.manager.clients.clear()
        self.drain().assert_not_awaited()
        self.assertEqual(self.latest().state, 'QUEUED')
        self.assertEqual(self.latest().analysis_attempts, 0)
        self.manager.clients[self.account_id] = self.telegram
        self.due()
        self.drain().assert_awaited_once()
        self.assertEqual(self.latest().state, 'SENT')

    def test_expired_message_does_not_call_model_or_telegram(self):
        self.setup_agent(mode='AUTO')
        row = self.enqueue()
        row.created_at = datetime.utcnow() - MAX_AGE - timedelta(seconds=1)
        db.session.commit()
        with patch.object(self.telegram, 'get_messages', new=AsyncMock()) as read:
            self.drain().assert_not_awaited()
        read.assert_not_awaited()
        self.assertEqual(self.latest().state, 'STALE')

    def test_edited_deleted_own_or_different_author_is_not_retried(self):
        self.setup_agent(mode='AUTO')
        for i, replacement in enumerate((None, message(text='Changed', sender_id=22),
                                         message(sender_id=33), message(sender_id=22, out=True)), 1):
            row = self.enqueue(i)
            self.telegram.messages[i] = replacement
            self.drain().assert_not_awaited()
            self.assertEqual(db.session.get(AIDecision, row.id).state, 'STALE')
        self.assertFalse(self.telegram.sent)

    def test_changed_settings_or_revoked_consent_cancel_queue(self):
        self.setup_agent(mode='AUTO')
        for change in ('revision', 'consent', 'mode'):
            row = self.enqueue(AIDecision.query.count() + 1)
            if change == 'revision':
                self.agent.revision += 1
            elif change == 'consent':
                self.agent.consent = False
            else:
                self.agent.mode = 'OFF'
            db.session.commit()
            self.drain().assert_not_awaited()
            self.assertEqual(db.session.get(AIDecision, row.id).state, 'STALE')
            self.agent.consent, self.agent.mode = True, 'AUTO'
            db.session.commit()

    def test_busy_chat_enqueues_before_waiting_for_model(self):
        self.setup_agent()
        async def busy():
            lock = self.assistant.locks[self.agent.id]
            await lock.acquire()
            try:
                await self.assistant.handle_message(self.account_id, self.telegram, self.event(message(sender_id=22)))
                self.assertEqual(self.latest().state, 'QUEUED')
            finally:
                lock.release()
        with patch('ai_assistant.analyze', new=AsyncMock()) as ai:
            asyncio.run(busy())
        ai.assert_not_awaited()
        self.drain().assert_awaited_once()

    def test_duplicate_event_does_not_reset_backoff(self):
        self.setup_agent()
        row = self.enqueue()
        row.next_analysis_at = datetime.utcnow() + timedelta(minutes=1)
        db.session.commit()
        expected = row.next_analysis_at
        self.handle(self.event(message(sender_id=22)))[1].assert_not_awaited()
        self.assertEqual(AIDecision.query.count(), 1)
        self.assertEqual(self.latest().next_analysis_at, expected)

    def test_new_edit_supersedes_pending_analysis(self):
        self.setup_agent()
        old = self.enqueue()
        self.enqueue(text='Edited')
        self.assertEqual(old.state, 'STALE')
        self.assertEqual(old.training_status, 'SUPERSEDED')
        self.assertEqual(self.latest().state, 'QUEUED')

    def test_recovery_only_requeues_analysis_not_mutations(self):
        self.setup_agent(mode='AUTO')
        states = ('ANALYZING', 'RUNNING', 'UNCERTAIN', 'SENT', 'DELETED', 'BANNED', 'READY', 'REVIEW')
        for i, state in enumerate(states, 1):
            row = self.enqueue(i)
            row.state, row.analysis_attempts = state, 1
        db.session.commit()
        recover_analysis()
        db.session.commit()
        db.session.expire_all()
        self.assertEqual([row.state for row in AIDecision.query.order_by(AIDecision.id)], ['QUEUED', *states[1:]])
        self.drain().assert_awaited_once()
        self.assertEqual(len(self.telegram.sent), 1)

    def test_recovered_analysis_has_attempt_limit(self):
        self.setup_agent()
        row = self.enqueue()
        row.state, row.analysis_attempts = 'ANALYZING', 3
        db.session.commit()
        recover_analysis()
        db.session.commit()
        self.drain().assert_not_awaited()
        self.assertEqual(self.latest().state, 'REVIEW')

    def test_cancellation_requeues_only_unfinished_analysis(self):
        self.setup_agent()
        self.enqueue()
        async def cancelled():
            with patch('ai_assistant.analyze', new=AsyncMock(side_effect=asyncio.CancelledError)):
                with self.assertRaises(asyncio.CancelledError):
                    await self.assistant._drain_agent(self.agent.id)
        asyncio.run(cancelled())
        db.session.expire_all()
        self.assertEqual(self.latest().state, 'QUEUED')
        self.assertEqual(self.latest().analysis_attempts, 1)

    def test_analysis_failure_after_stop_does_not_reactivate(self):
        self.setup_agent(mode='AUTO')
        self.enqueue()
        async def fail_after_stop(*args):
            agent = db.session.get(AIAgent, self.agent.id)
            agent.mode, agent.revision = 'OFF', agent.revision + 1
            db.session.commit()
            raise TimeoutError()
        with patch('ai_assistant.analyze', new=AsyncMock(side_effect=fail_after_stop)):
            asyncio.run(self.assistant.process_queue())
        db.session.expire_all()
        self.assertEqual(self.latest().state, 'STALE')
        self.assertFalse(self.telegram.sent)

    def test_unknown_send_result_is_never_reanalyzed_or_resent(self):
        self.setup_agent(mode='AUTO')
        self.enqueue()
        with patch.object(self.telegram, 'send_message', new=AsyncMock(side_effect=TimeoutError())) as send:
            self.drain()
            self.assertEqual(self.latest().state, 'UNCERTAIN')
            self.drain().assert_not_awaited()
        send.assert_awaited_once()

    def test_dashboard_shows_queue_without_action_controls(self):
        self.setup_agent(mode='AUTO')
        row = self.enqueue()
        row.analysis_attempts = 1
        db.session.commit()
        status = queue_status(self.agent.id)
        self.assertEqual((status['waiting'], status['retrying']), (1, 1))
        response = self.client.get(f'/assistant/{self.agent.id}?tab=queue')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('Попыток анализа: 1 / 3', body)
        self.assertNotIn('Подтверждаю выбранное действие в Telegram', body)
        self.assertNotIn('Добавить исправление', body)

    def test_images_are_not_persisted_and_are_refetched(self):
        self.setup_agent(vision=True)
        event = self.event(message(photo=object(), sender_id=22))
        self.assistant._queue_message(self.agent, event)
        with patch('ai_assistant.image_content', new=AsyncMock(return_value={'type': 'input_image'})) as vision:
            self.drain()
        vision.assert_awaited_once()
        self.assertEqual(AIMessage.query.count(), 1)

    def test_reply_context_is_reread_after_restart(self):
        self.setup_agent()
        event = self.event(message(sender_id=22, reply_to_msg_id=99))
        self.assistant._queue_message(self.agent, event)
        self.telegram.messages[99] = message(99, 'Current reply context', sender_id=33)
        ai = self.drain()
        self.assertEqual(ai.call_args.args[1]['reply_to']['text'], 'Current reply context')

    def test_temporary_telegram_read_error_retries_without_model(self):
        self.setup_agent()
        self.enqueue()
        with patch.object(self.telegram, 'get_messages', new=AsyncMock(side_effect=TimeoutError())):
            self.drain().assert_not_awaited()
        self.assertEqual(self.latest().state, 'QUEUED')
        self.assertEqual(self.latest().analysis_attempts, 1)

    def test_expired_analysis_lease_recovers_but_running_action_does_not(self):
        self.setup_agent()
        row = self.enqueue()
        row.state, row.analysis_attempts = 'ANALYZING', 1
        row.analysis_started_at = datetime.utcnow() - timedelta(minutes=4)
        action = self.enqueue(2)
        action.state = 'RUNNING'
        action.analysis_started_at = row.analysis_started_at
        db.session.commit()
        self.drain().assert_awaited_once()
        self.assertEqual(db.session.get(AIDecision, row.id).state, 'OBSERVED')
        self.assertEqual(db.session.get(AIDecision, action.id).state, 'RUNNING')

    def test_successful_old_lease_result_is_ignored(self):
        self.setup_agent(mode='AUTO')
        row = self.enqueue()
        async def supersede(*args):
            current = db.session.get(AIDecision, row.id)
            current.state = 'QUEUED'
            current.next_analysis_at = datetime.utcnow() + timedelta(seconds=10)
            db.session.commit()
            return decision_result()
        with patch('ai_assistant.analyze', new=AsyncMock(side_effect=supersede)):
            asyncio.run(self.assistant.process_queue())
        db.session.expire_all()
        self.assertEqual(self.latest().state, 'QUEUED')
        self.assertFalse(self.telegram.sent)

    def test_newly_connected_owned_account_is_not_analyzed(self):
        self.setup_agent()
        self.enqueue()
        self.telegram._self_id = 22
        self.drain().assert_not_awaited()
        self.assertEqual(self.latest().state, 'STALE')

    def test_restarted_worker_retains_recent_rate_usage(self):
        self.setup_agent()
        for i in range(1, 13):
            row = self.enqueue(i)
            row.state, row.analysis_attempts, row.analysis_started_at = 'OBSERVED', 1, datetime.utcnow()
        db.session.commit()
        self.enqueue(13)
        self.drain().assert_not_awaited()
        self.assertEqual(self.latest().state, 'QUEUED')
        self.assertEqual(self.latest().analysis_attempts, 0)

    def test_scheduler_registers_persistent_queue_worker(self):
        self.manager._load_accounts = AsyncMock()
        self.manager._reload_scheduled_tasks = AsyncMock()
        self.manager._refresh_trigger_modes = MagicMock()
        async def initialize():
            await self.manager._init()
            try:
                job = self.manager.scheduler.get_job('process_ai_analysis_queue')
                self.assertIsNotNone(job)
                self.assertEqual(job.max_instances, 1)
                self.assertTrue(job.coalesce)
                self.assertEqual(job.func.__self__, self.manager.ai_assistant)
            finally:
                self.manager.scheduler.shutdown(wait=False)
                self.manager.scheduler = None
        asyncio.run(initialize())

    def test_large_flood_wait_does_not_retry_before_deadline(self):
        self.setup_agent()
        self.enqueue()
        self.drain(error=errors.FloodWaitError(request=None, capture=700))
        self.assertEqual(self.latest().state, 'REVIEW')
        self.assertFalse(self.telegram.sent)

    def test_changed_model_is_recorded_for_new_attempt(self):
        self.setup_agent()
        self.enqueue()
        self.app.config['OPENAI_MODEL'] = 'test-only-another-model'
        self.drain()
        self.assertEqual(self.latest().model, 'test-only-another-model')

    def test_deadline_during_analysis_blocks_automatic_action(self):
        self.setup_agent(mode='AUTO')
        row = self.enqueue()
        async def too_late(*args):
            current = db.session.get(AIDecision, row.id)
            current.created_at = datetime.utcnow() - MAX_AGE - timedelta(seconds=1)
            db.session.commit()
            return decision_result()
        with patch('ai_assistant.analyze', new=AsyncMock(side_effect=too_late)):
            asyncio.run(self.assistant.process_queue())
        db.session.expire_all()
        self.assertEqual(self.latest().state, 'STALE')
        self.assertFalse(self.telegram.sent)

    def test_parallel_worker_ticks_do_not_claim_same_message(self):
        self.setup_agent(mode='AUTO')
        self.enqueue()
        async def analyze_once(*args):
            await asyncio.sleep(0)
            return decision_result()
        async def workers():
            await asyncio.gather(self.assistant.process_queue(), self.assistant.process_queue())
        with patch('ai_assistant.analyze', new=AsyncMock(side_effect=analyze_once)) as ai:
            asyncio.run(workers())
        ai.assert_awaited_once()
        self.assertEqual(len(self.telegram.sent), 1)

    def test_queued_analysis_uses_current_official_source_revision(self):
        self.setup_agent(mode='AUTO')
        self.enqueue()
        fact = fixtures.AssistantTests.fact(self)
        self.agent.knowledge_revision += 1
        db.session.commit()
        self.drain(result=decision_result(intent='INFORMATION', fact_ids=[fact.id], reply='The published event has finished.'))
        self.assertEqual(self.latest().state, 'SENT')
        self.assertEqual(self.latest().knowledge_revision, self.agent.knowledge_revision)
        self.assertIn(fact.source_url, self.telegram.sent[0][1])

    def test_legacy_database_adds_queue_columns_before_recovery(self):
        from pathlib import Path
        from sqlalchemy import inspect
        from config import Config
        from web.app import create_app
        path = (Path(self.temp.name) / 'queue-migration.db').as_posix()
        with patch.object(Config, 'DATABASE_URL', 'sqlite:///' + path):
            old = create_app()
            with old.app_context():
                db.session.add(AIAgent(id=1, name='Legacy', chat_id='-100555'))
                db.session.flush()
                db.session.add(AIDecision(agent_id=1, message_id=1, fingerprint='old', agent_revision=0, knowledge_revision=0))
                db.session.commit()
                for name in ('analysis_attempts', 'next_analysis_at', 'analysis_started_at'):
                    if name == 'next_analysis_at':
                        db.session.execute(db.text('DROP INDEX IF EXISTS ix_ai_decisions_next_analysis_at'))
                    db.session.execute(db.text(f'ALTER TABLE ai_decisions DROP COLUMN {name}'))
                db.session.commit()
                db.session.remove()
                db.engine.dispose()
            migrated = create_app()
            with migrated.app_context():
                try:
                    row = AIDecision.query.one()
                    self.assertEqual(row.state, 'QUEUED')
                    self.assertEqual(row.analysis_attempts, 0)
                    self.assertIsNotNone(row.next_analysis_at)
                    self.assertIn('analysis_started_at', {value['name'] for value in inspect(db.engine).get_columns('ai_decisions')})
                finally:
                    db.session.remove()
                    db.engine.dispose()


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('ai_queue.random.randint', return_value=0))

    def test_only_transient_failures_are_retryable(self):
        request = httpx.Request('POST', 'https://api.example.test')
        for status, code, expected in ((429, 'rate_limit_exceeded', 10), (503, 'server_error', 10),
                                       (429, 'insufficient_quota', None), (401, 'invalid_api_key', None),
                                       (429, 'project_spend_limit_exceeded', None), (429, 'organization_spend_limit_exceeded', None),
                                       (400, 'invalid_image', None), (404, 'model_not_found', None)):
            exc = APIStatusError('not real', response=httpx.Response(status, request=request), body={'code': code})
            self.assertEqual(retry_delay(exc, 1), expected)
        self.assertEqual(retry_delay(APIConnectionError(request=request), 2), 20)
        self.assertEqual(retry_delay(errors.FloodWaitError(request=None, capture=70), 1), 70)
        self.assertIsNone(retry_delay(ValueError(), 1))
        wrapped = AnalysisError('safe error')
        wrapped.__cause__ = TimeoutError('private')
        self.assertEqual(retry_delay(wrapped, 1), 10)

    def test_server_wait_is_minimum_and_invalid_headers_use_backoff(self):
        request = httpx.Request('POST', 'https://api.example.test')
        for header, expected in (('75', 75), ('1.2', 10), ('bad', 10), ('nan', 10), ('-5', 10), ('700', None), ('1e308', None)):
            exc = APIStatusError('test', response=httpx.Response(429, request=request, headers={'Retry-After': header}),
                                 body={'code': 'slow_down'})
            self.assertEqual(retry_delay(exc, 1), expected)

    def test_telegram_read_server_failure_is_retryable(self):
        self.assertEqual(retry_delay(errors.ServerError(request=None, message='server'), 1), 10)
