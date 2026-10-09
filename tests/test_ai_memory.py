import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import text

import test_ai_assistant as helpers
import test_ai_training as fixtures
from ai_examples import evaluate_replay, replay_snapshot
from ai_memory import DIMENSIONS, MODEL, content_hash, embed, memory_text, select_memory
from ai_training import question_groups
from models import AIAgent, AIDecision, AIExample, AIMessage, AIReplayRun, db
from web.routes.assistant_training import training_context


def unit(first=1, second=0):
    return [first, second] + [0.0] * (DIMENSIONS - 2)


class MemoryTests(unittest.TestCase):
    setUp = fixtures.TrainingTests.setUp
    tearDown = fixtures.TrainingTests.tearDown
    event = helpers.AssistantTests.event
    handle = helpers.AssistantTests.handle
    latest = helpers.AssistantTests.latest
    form = helpers.AssistantTests.form
    training_data = fixtures.TrainingTests.training_data

    def enable(self):
        self.agent.semantic_memory = True
        db.session.commit()

    def sample(self, body='Other wording', **kwargs):
        data = dict(agent_id=self.agent.id, title='Memory', text=body, expected={}, approved=True)
        data.update(kwargs)
        row = AIExample(**data)
        db.session.add(row)
        db.session.commit()
        return row

    def analysis(self, **kwargs):
        data = dict(agent_id=self.agent.id, message_id=1, fingerprint='test', agent_revision=self.agent.revision,
                    knowledge_revision=0, state='ANALYZING', analysis_started_at=datetime.utcnow())
        data.update(kwargs)
        row = AIDecision(**data)
        db.session.add(row)
        db.session.commit()
        return row

    def pending(self, num=1, body='Непонятное выражение', **kwargs):
        msg = helpers.message(num, body)
        self.handle(self.event(msg), helpers.decision_result(language='RUSSIAN', intent='UNKNOWN', action='HUMAN_REVIEW',
            reply='', reason='Нужно пояснение', **kwargs))
        return self.latest()

    def group_data(self, root, selected):
        with self.app.test_request_context():
            context = training_context(self.agent, 'training')
        return self.training_data(root, group_token=context['training_group_tokens'][root.id],
                                  group_members=[str(row.id) for row in selected])

    def test_off_and_no_consent_make_no_embedding_requests(self):
        self.sample()
        with patch('ai_memory.embed', new=AsyncMock()) as call:
            self.assertEqual(asyncio.run(select_memory(self.app, self.agent, 'Question'))['method'], 'words')
            self.enable()
            self.agent.consent = False
            db.session.commit()
            asyncio.run(select_memory(self.app, self.agent, 'Question'))
            call.assert_not_called()

    def test_semantic_search_finds_different_words_and_is_scoped(self):
        self.enable()
        relevant = self.sample('Unavailable withdrawal', embedding=unit(), embedding_hash=content_hash('Unavailable withdrawal'))
        for _ in range(6):
            self.sample('Football discussion', embedding=unit(0, 1), embedding_hash=content_hash('Football discussion'))
        other = AIAgent(name='Other', chat_id='-100999', consent=True)
        db.session.add(other)
        db.session.commit()
        self.sample('SECRET OTHER CHAT', agent_id=other.id)
        self.sample('NOT APPROVED', approved=False)
        with patch('ai_memory.embed', new=AsyncMock(return_value=[unit()])) as call:
            result = asyncio.run(select_memory(self.app, self.agent, 'money stuck'))
        self.assertEqual(call.call_args.args[1], ['money stuck'])
        self.assertEqual(result['method'], 'semantic')
        self.assertEqual(result['ids'][0], relevant.id)
        self.assertEqual(len(result['ids']), 5)
        self.assertNotIn('SECRET', str(result))
        self.assertNotIn('NOT APPROVED', str(result))

    def test_batch_cache_is_reused_by_retry_and_changes_invalidate_content(self):
        self.enable()
        example = self.sample('Repeated question')
        row = self.analysis()
        with patch('ai_memory.embed', new=AsyncMock(side_effect=lambda app, texts: [unit() for _ in texts])) as call:
            asyncio.run(select_memory(self.app, self.agent, 'Question again', decision=row))
            asyncio.run(select_memory(self.app, self.agent, 'Question again', decision=row))
            self.assertEqual(call.await_count, 1)
            self.assertEqual(len(call.call_args.args[1]), 2)
            example.text = 'Updated description'
            db.session.commit()
            asyncio.run(select_memory(self.app, self.agent, 'Question again', decision=row))
            self.assertEqual(call.await_count, 2)
            self.assertEqual(call.call_args.args[1], ['Updated description'])

    def test_duplicate_inputs_are_batched_once_and_requests_are_bounded(self):
        self.enable()
        for _ in range(50):
            self.sample('x' * 2000)
        with patch('ai_memory.embed', new=AsyncMock(side_effect=lambda app, texts: [unit() for _ in texts])) as call:
            asyncio.run(select_memory(self.app, self.agent, 'Query text'))
        bodies = call.call_args.args[1]
        self.assertEqual(len(bodies), 2)
        self.assertTrue(all(len(body) <= 800 for body in bodies))

    def test_failure_falls_back_with_safe_diagnostic_and_backoff(self):
        self.enable()
        self.sample()
        with patch('ai_memory.embed', new=AsyncMock(side_effect=RuntimeError('PRIVATE SECRET'))) as call:
            result = asyncio.run(select_memory(self.app, self.agent, 'Question'))
            asyncio.run(select_memory(self.app, self.agent, 'Question'))
        self.assertEqual(call.await_count, 1)
        self.assertEqual(result['method'], 'words')
        self.assertNotIn('PRIVATE SECRET', self.agent.memory_error)
        self.assertGreater(self.agent.memory_retry_at, datetime.utcnow())
        page = self.client.get(f'/assistant/{self.agent.id}').get_data(as_text=True)
        self.assertIn('Используется поиск по словам', page)

    def test_malformed_vectors_fall_back_without_poisoning_cache(self):
        self.enable()
        example = self.sample()
        for values in ([[1]], [unit(float('nan'))], [unit(True)], [unit(0, 0)], [unit(2)], []):
            self.agent.memory_retry_at = None
            db.session.commit()
            with patch('ai_memory.embed', new=AsyncMock(return_value=values)):
                self.assertEqual(asyncio.run(select_memory(self.app, self.agent, 'Other wording'))['method'], 'words')
            self.assertIsNone(example.embedding)

    def test_memory_masks_identifiers_and_skips_empty_and_private_queries(self):
        self.enable()
        self.sample()
        with patch('ai_memory.embed', new=AsyncMock(side_effect=lambda app, texts: [unit() for _ in texts])) as call:
            for body in ('', '[PRIVATE MESSAGE]', '[PRIVATE]', '123456789'):
                asyncio.run(select_memory(self.app, self.agent, body))
            call.assert_not_called()
            asyncio.run(select_memory(self.app, self.agent, 'contact secret@example.com or +79991234567'))
        bodies = str(call.call_args.args[1])
        self.assertNotIn('secret@example.com', bodies)
        self.assertNotIn('79991234567', bodies)

    def test_settings_change_during_api_does_not_store_cache_or_analyze_afterwards(self):
        self.enable()
        example = self.sample()
        async def change(app, texts):
            self.agent.consent = False
            self.agent.revision += 1
            db.session.commit()
            return [unit() for _ in texts]
        with patch('ai_memory.embed', new=AsyncMock(side_effect=change)), patch('ai_assistant.analyze', new=AsyncMock()) as analyze:
            asyncio.run(self.assistant.handle_message(self.account_id, self.telegram, self.event()))
        self.assertIsNone(example.embedding)
        self.assertIsNone(self.latest().embedding)
        self.assertEqual(self.latest().state, 'STALE')
        analyze.assert_not_called()

    def test_sensitive_results_clear_query_cache_and_image_queries_are_not_cached(self):
        self.enable()
        with patch('ai_memory.embed', new=AsyncMock(side_effect=lambda app, texts: [unit() for _ in texts])):
            self.handle(self.event(helpers.message(text='Payment confirmation')), helpers.decision_result(
                intent='PAYMENT_DATA', classification='PAYMENT_DATA', action='WARN'))
            self.assertIsNone(self.latest().embedding)
            row = self.analysis(message_id=2, fingerprint='image', has_image=True)
            asyncio.run(select_memory(self.app, self.agent, 'Image caption', decision=row))
            self.assertIsNone(row.embedding)

    def test_memory_failure_does_not_fail_normal_analysis(self):
        self.enable()
        with patch('ai_memory.embed', new=AsyncMock(side_effect=TimeoutError())):
            self.handle()
        self.assertEqual(self.latest().state, 'OBSERVED')
        self.assertEqual(self.latest().memory_method, 'words')
        self.assertTrue(self.agent.memory_error)
        self.assertFalse(self.agent.last_error)

    def test_opt_in_is_explicit_and_needs_key_and_consent(self):
        self.client.post(f'/assistant/{self.agent.id}', data=self.form(semantic_memory='on', consent=''))
        self.assertFalse(self.agent.semantic_memory)
        self.client.post(f'/assistant/{self.agent.id}', data=self.form(semantic_memory='on'))
        self.assertTrue(self.agent.semantic_memory)
        self.client.post(f'/assistant/{self.agent.id}', data=self.form())
        self.assertFalse(self.agent.semantic_memory)

    def test_replay_excludes_itself_uses_immutable_examples_and_no_telegram(self):
        self.enable()
        example = self.sample('Test case')
        reference = self.sample('Reference case')
        snapshot = replay_snapshot(self.app, self.agent, example)
        reference.text = 'Changed behind snapshot'
        db.session.commit()
        row = AIReplayRun(example_id=example.id, request_token='mock', agent_revision=self.agent.revision,
                          knowledge_revision=0, prompt_version=snapshot['version'], model='mock')
        db.session.add(row)
        db.session.commit()
        with patch('ai_memory.embed', new=AsyncMock(side_effect=lambda app, texts: [unit() for _ in texts])) as embeddings, \
                patch('ai_examples.analyze', new=AsyncMock(return_value=helpers.decision_result())) as analyze:
            asyncio.run(evaluate_replay(self.app, row.id, row.created_at.isoformat(), snapshot))
        self.assertEqual(embeddings.call_args.args[1], ['Reference case', 'Test case'])
        self.assertEqual(analyze.call_args.args[1]['approved_examples'][0]['message'], 'Reference case')
        self.assertIsNone(reference.embedding)
        self.assertEqual(row.outcome['memory_method'], 'semantic')
        self.assertFalse(self.telegram.sent or self.telegram.deleted or self.telegram.banned)

    def test_identical_questions_group_without_paid_calls_and_unselected_stay_open(self):
        older = self.pending(1)
        newer = self.pending(2)
        with patch('ai_memory.embed', new=AsyncMock()) as call:
            page = self.client.get(f'/assistant/{self.agent.id}?tab=training').get_data(as_text=True)
        self.assertIn('Похожие вопросы', page)
        self.assertNotIn('id="group-member-' + str(older.id) + '" checked', page)
        call.assert_not_called()
        self.client.post(f'/assistant/{self.agent.id}/training/{newer.id}', data=self.training_data(newer))
        self.assertEqual(older.training_status, 'OPEN')
        self.assertEqual(newer.training_status, 'DONE')
        self.assertEqual(AIExample.query.count(), 1)

    def test_group_teaching_is_atomic_and_creates_one_example_without_telegram_actions(self):
        older = self.pending(1)
        newer = self.pending(2)
        data = self.group_data(newer, [older])
        for _ in range(2):
            self.client.post(f'/assistant/{self.agent.id}/training/{newer.id}', data=data)
        self.assertEqual(older.training_status, 'DONE')
        self.assertEqual(newer.training_status, 'DONE')
        self.assertEqual(AIExample.query.count(), 1)
        self.assertFalse(self.telegram.sent or self.telegram.deleted or self.telegram.banned)

    def test_group_token_cannot_be_tampered_cross_scoped_or_reused_after_edit(self):
        older = self.pending(1)
        newer = self.pending(2)
        data = self.group_data(newer, [older])
        bad = dict(data, group_token=data['group_token'] + 'invalid')
        self.client.post(f'/assistant/{self.agent.id}/training/{newer.id}', data=bad)
        self.assertEqual(AIExample.query.count(), 0)
        self.handle(self.event(helpers.message(1, 'Changed text')), helpers.decision_result())
        self.client.post(f'/assistant/{self.agent.id}/training/{newer.id}', data=data)
        self.assertEqual(AIExample.query.count(), 0)
        self.assertEqual(newer.training_status, 'OPEN')

    def test_group_tokens_expire_and_cannot_select_a_foreign_chat(self):
        import time
        older = self.pending(1)
        newer = self.pending(2)
        with patch('itsdangerous.timed.time.time', return_value=time.time() - 1000):
            expired = self.group_data(newer, [older])
        self.client.post(f'/assistant/{self.agent.id}/training/{newer.id}', data=expired)
        self.assertEqual(AIExample.query.count(), 0)
        other = AIAgent(name='Other', chat_id='-100777')
        db.session.add(other)
        db.session.flush()
        foreign = self.analysis(agent_id=other.id, message_id=3, state='REVIEW', training_needed=True)
        data = self.group_data(newer, [foreign])
        self.client.post(f'/assistant/{self.agent.id}/training/{newer.id}', data=data)
        self.assertEqual(AIExample.query.count(), 0)
        self.assertEqual(newer.training_status, 'OPEN')
        self.assertEqual(foreign.training_status, 'OPEN')

    def test_group_claim_race_rolls_back_the_first_question(self):
        older = self.pending(1)
        newer = self.pending(2)
        data = self.group_data(newer, [older])
        from ai_examples import validate_approval
        def changed(example):
            validate_approval(example)
            older.training_status = 'DISMISSED'
            db.session.commit()
        with patch('web.routes.assistant_training.validate_approval', side_effect=changed):
            self.client.post(f'/assistant/{self.agent.id}/training/{newer.id}', data=data)
        self.assertEqual(newer.training_status, 'OPEN')
        self.assertEqual(AIExample.query.count(), 0)

    def test_old_analysis_cannot_write_a_new_lease_query_cache(self):
        self.enable()
        def changed(app, texts):
            row = self.latest()
            from datetime import timedelta
            row.analysis_started_at += timedelta(seconds=1)
            db.session.commit()
            return [unit() for _ in texts]
        with patch('ai_memory.embed', new=AsyncMock(side_effect=changed)), patch('ai_assistant.analyze', new=AsyncMock()) as analyze:
            asyncio.run(self.assistant.handle_message(self.account_id, self.telegram, self.event()))
        self.assertIsNone(self.latest().embedding)
        analyze.assert_not_called()

    def test_unknown_redacted_questions_never_group_as_identical(self):
        a = self.pending(1, 'Contact [PRIVATE] for details')
        b = self.pending(2, 'Contact [PRIVATE] for details')
        records = {row.message_id: row for row in AIMessage.query.all()}
        self.assertEqual(len(question_groups([b, a], records)), 2)

    def test_selected_question_finished_after_page_load_rejects_entire_group(self):
        older = self.pending(1)
        newer = self.pending(2)
        data = self.group_data(newer, [older])
        older.state = 'SENT'
        db.session.commit()
        self.client.post(f'/assistant/{self.agent.id}/training/{newer.id}', data=data)
        self.assertEqual(newer.training_status, 'OPEN')
        self.assertEqual(AIExample.query.count(), 0)

    def test_semantic_groups_need_compatible_labels_valid_vectors_and_explicit_opt_in(self):
        a = self.pending(1, 'Непонятное выражение')
        b = self.pending(2, 'Неизвестная фраза')
        records = {row.message_id: row for row in AIMessage.query.all()}
        for row in (a, b):
            row.embedding, row.embedding_hash = unit(), content_hash(memory_text(records[row.message_id].text))
        db.session.commit()
        self.assertEqual(len(question_groups([b, a], records, True)), 1)
        self.assertEqual(len(question_groups([b, a], records, False)), 2)
        for field, value in (('language', 'ENGLISH'), ('intent', 'SCAM'), ('action', 'DELETE'),
                             ('classification', 'PAYMENT_DATA'), ('has_reply', True), ('has_image', True), ('embedding_hash', 'old')):
            old = getattr(b, field)
            setattr(b, field, value)
            self.assertEqual(len(question_groups([b, a], records, True)), 2, field)
            setattr(b, field, old)

    def test_full_link_groups_do_not_merge_similarity_chains_or_truncated_messages(self):
        rows = [self.pending(num, f'Question wording {num}') for num in range(1, 4)]
        records = {row.message_id: row for row in AIMessage.query.all()}
        import math
        for row, angle in zip(rows, (0, 0.25, 0.5)):
            row.embedding, row.embedding_hash = unit(math.cos(angle), math.sin(angle)), content_hash(memory_text(records[row.message_id].text))
        self.assertEqual(len(question_groups(rows, records, True)), 2)
        for row in records.values():
            row.text = 'Long question ' * 100 + str(row.id)
        self.assertEqual(len(question_groups(rows, records, True)), 3)

    def test_embeddings_sdk_schema_index_order_and_invalid_outputs(self):
        response = SimpleNamespace(model=MODEL, data=[SimpleNamespace(index=1, embedding=unit(0, 1)), SimpleNamespace(index=0, embedding=unit())])
        client = SimpleNamespace(embeddings=SimpleNamespace(create=AsyncMock(return_value=response)))
        manager = AsyncMock()
        manager.__aenter__.return_value = client
        with patch('ai_memory.AsyncOpenAI', return_value=manager):
            values = asyncio.run(embed(self.app, ['one', 'two']))
            self.assertEqual(values, [unit(), unit(0, 1)])
            self.assertEqual(client.embeddings.create.call_args.kwargs['dimensions'], 256)
            for items in ([SimpleNamespace(index=0, embedding=unit())] * 2,
                          [SimpleNamespace(index=True, embedding=unit()), SimpleNamespace(index=0, embedding=unit())]):
                response.data = items
                with self.assertRaises(ValueError):
                    asyncio.run(embed(self.app, ['one', 'two']))

    def test_legacy_migration_keeps_memory_off_and_old_reply_context_unknown(self):
        from config import Config
        from web.app import create_app
        with patch.object(Config, 'DATABASE_URL', 'sqlite:///' + (Path(self.temp.name) / 'memory-migration.db').as_posix()):
            old = create_app()
            with old.app_context():
                agent = AIAgent(name='Old', chat_id='-1001')
                db.session.add(agent)
                db.session.flush()
                db.session.add(AIDecision(agent_id=agent.id, message_id=1, fingerprint='x', agent_revision=0, knowledge_revision=0, state='REVIEW'))
                db.session.add(AIExample(agent_id=agent.id, title='Old'))
                db.session.commit()
                columns = {'ai_agents': ('semantic_memory', 'memory_error', 'memory_retry_at'),
                    'ai_examples': ('embedding', 'embedding_hash'),
                    'ai_decisions': ('has_reply', 'embedding', 'embedding_hash', 'memory_method', 'memory_example_ids')}
                for table, names in columns.items():
                    for name in names:
                        db.session.execute(text(f'ALTER TABLE {table} DROP COLUMN {name}'))
                db.session.commit()
                db.session.remove()
                db.engine.dispose()
            recovered = create_app()
            with recovered.app_context():
                self.assertFalse(AIAgent.query.one().semantic_memory)
                self.assertTrue(AIDecision.query.one().has_reply)
                self.assertIsNone(AIExample.query.one().embedding)
                self.assertEqual(AIDecision.query.one().memory_example_ids, [])
                db.session.remove()
                db.engine.dispose()
