import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import text
from telethon import errors

import test_ai_assistant as helpers
import test_ai_training as fixtures
from ai_assistant import policy
from ai_engine import analyze
from ai_examples import evaluate_replay, replay_snapshot
from models import AIAgent, AIDecision, AIExample, AIMessage, AIModerationRule, AIReplayRun, db


class CombinedClient(fixtures.BanClient):
    def __init__(self):
        super().__init__()
        self.operations, self.after_ban, self.delete_error = [], None, None

    async def edit_permissions(self, chat, user, **kwargs):
        self.operations.append('ban')
        result = await super().edit_permissions(chat, user, **kwargs)
        if self.after_ban:
            self.after_ban()
        return result

    async def delete_messages(self, chat, ids, **kwargs):
        self.operations.append('delete')
        if self.delete_error:
            raise self.delete_error
        result = await super().delete_messages(chat, ids, **kwargs)
        for value in ids:
            self.messages.pop(value, None)
        return result

    async def send_message(self, chat, body, **kwargs):
        self.operations.append('reply')
        if kwargs.get('reply_to') not in self.messages:
            raise errors.ReplyMessageIdInvalidError(None)
        return await super().send_message(chat, body, **kwargs)


class CombinedTests(unittest.TestCase):
    tearDown = fixtures.TrainingTests.tearDown
    event = fixtures.TrainingTests.event
    handle = fixtures.TrainingTests.handle
    latest = fixtures.TrainingTests.latest
    rule = fixtures.TrainingTests.rule
    rule_data = fixtures.TrainingTests.rule_data
    violation = fixtures.TrainingTests.violation
    active = fixtures.TrainingTests.active
    unclear = fixtures.TrainingTests.unclear
    training_data = fixtures.TrainingTests.training_data

    def setUp(self):
        fixtures.TrainingTests.setUp(self)
        self.telegram = CombinedClient()
        self.manager.clients[self.account_id] = self.telegram

    def prepare(self, mode='AUTO'):
        self.active()
        self.agent.mode = mode
        db.session.commit()
        return self.rule('DELETE_BAN')

    def combined(self, rule, num=1, **kwargs):
        return self.handle(self.event(helpers.message(num, 'Explicit rule violation', sender_id=22)), self.violation(rule, **kwargs))

    def test_combined_action_bans_then_deletes_exact_message_once(self):
        rule = self.prepare()
        event = self.event(helpers.message(sender_id=22))
        self.handle(event, self.violation(rule))
        row = self.latest()
        self.assertEqual(row.state, 'DELETED_BANNED')
        self.assertEqual((row.ban_status, row.delete_status), ('DONE', 'DONE'))
        self.assertEqual(self.telegram.operations, ['ban', 'delete'])
        self.assertEqual(self.telegram.deleted, [1])
        self.assertEqual(self.telegram.banned[0][1], 22)
        self.handle(event, self.violation(rule))
        asyncio.run(self.assistant.execute(row.id, 'DELETE_BAN', row.created_at.isoformat()))
        self.assertEqual(self.telegram.operations, ['ban', 'delete'])
        page = self.client.get(f'/assistant/{self.agent.id}?tab=deleted').get_data(as_text=True)
        self.assertIn('Удалено + бан', page)
        self.assertIn('Бан: Подтверждено', page)

    def test_both_opt_in_permissions_and_exact_matching_rule_are_required(self):
        rule = self.prepare()
        for delete, ban in ((False, True), (True, False)):
            self.agent.allow_delete, self.agent.allow_ban = delete, ban
            decision = SimpleNamespace(**self.violation(rule), has_image=False)
            self.assertEqual(policy(self.agent, decision, [])[0], 'REVIEW')
        self.agent.allow_delete, self.agent.allow_ban = True, True
        for action in ('DELETE', 'BAN'):
            decision = SimpleNamespace(**self.violation(rule, action=action), has_image=False)
            self.assertEqual(policy(self.agent, decision, [])[0], 'REVIEW')
        self.assertFalse(self.telegram.operations)

    def test_high_confidence_vision_and_non_sensitive_category_are_required(self):
        rule = self.prepare()
        for extra, has_image in (({'confidence': 0.96}, False), ({'classification': 'PAYMENT_DATA'}, False), ({}, True)):
            decision = SimpleNamespace(**self.violation(rule, **extra), has_image=has_image)
            self.assertEqual(policy(self.agent, decision, [])[0], 'REVIEW')
        self.agent.vision = True
        decision = SimpleNamespace(**self.violation(rule), has_image=True)
        self.assertEqual(policy(self.agent, decision, [])[0], 'READY')

    def test_missing_actor_rights_or_target_admin_block_both_mutations(self):
        rule = self.prepare()
        for num, (delete, ban, admin) in enumerate(((False, True, False), (True, False, False), (True, True, True)), 1):
            self.telegram.can_delete, self.telegram.can_ban, self.telegram.target_admin = delete, ban, admin
            self.combined(rule, num)
            self.assertEqual(self.latest().state, 'REVIEW')
        self.assertFalse(self.telegram.operations)

    def test_unknown_ban_result_never_deletes_or_retries(self):
        rule = self.prepare()
        self.telegram.ban_error = TimeoutError('SECRET')
        self.combined(rule)
        row = self.latest()
        self.assertEqual(row.state, 'UNCERTAIN')
        self.assertEqual((row.ban_status, row.delete_status), ('UNCERTAIN', 'SKIPPED'))
        asyncio.run(self.assistant.execute(row.id, 'DELETE_BAN'))
        self.assertEqual(self.telegram.operations, ['ban'])
        self.assertNotIn('SECRET', row.result)

    def test_rejected_ban_never_deletes(self):
        rule = self.prepare()
        self.telegram.ban_error = errors.ChatAdminRequiredError(None)
        self.combined(rule)
        self.assertEqual(self.latest().state, 'ERROR')
        self.assertEqual((self.latest().ban_status, self.latest().delete_status), ('ERROR', 'SKIPPED'))
        self.assertEqual(self.telegram.operations, ['ban'])

    def test_deletion_failure_after_ban_is_visible_partial_and_never_repeated(self):
        rule = self.prepare()
        for num, error in enumerate((TimeoutError('SECRET'), errors.ChatAdminRequiredError(None)), 1):
            self.telegram.delete_error = error
            self.combined(rule, num)
            row = self.latest()
            self.assertEqual(row.state, 'PARTIAL')
            self.assertEqual(row.ban_status, 'DONE')
            self.assertEqual(row.delete_status, 'UNCERTAIN' if num == 1 else 'ERROR')
            self.assertNotIn('SECRET', row.result)
            asyncio.run(self.assistant.execute(row.id, 'DELETE_BAN'))
        self.assertEqual(self.telegram.operations, ['ban', 'delete', 'ban', 'delete'])
        page = self.client.get(f'/assistant/{self.agent.id}').get_data(as_text=True)
        self.assertIn('Частично', page)
        self.assertIn('Отказ Telegram', page)

    def test_stop_after_ban_cancels_deletion(self):
        rule = self.prepare()
        def stop():
            self.agent.mode = 'OFF'
            self.agent.revision += 1
            db.session.commit()
        self.telegram.after_ban = stop
        self.combined(rule)
        self.assertEqual(self.latest().state, 'PARTIAL')
        self.assertEqual(self.latest().delete_status, 'SKIPPED')
        self.assertEqual(self.telegram.operations, ['ban'])

    def test_edit_after_ban_cancels_deletion_and_preserves_progress(self):
        rule = self.prepare()
        def edit():
            self.telegram.messages[1].raw_text = 'Edited message'
        self.telegram.after_ban = edit
        self.combined(rule)
        self.assertEqual(self.latest().state, 'PARTIAL')
        self.assertEqual(self.latest().delete_status, 'SKIPPED')
        self.assertEqual(self.telegram.operations, ['ban'])

    def test_role_or_source_rechecks_do_not_escalate_ban_only_decisions(self):
        rule = self.rule('BAN')
        self.active()
        self.agent.mode = 'ASSIST'
        db.session.commit()
        self.handle(self.event(helpers.message(sender_id=22)), self.violation(rule))
        with self.assertRaises(ValueError):
            asyncio.run(self.assistant.execute(self.latest().id, 'DELETE_BAN'))
        self.assertFalse(self.telegram.operations)

    def test_cancel_during_second_step_records_partial_without_retry(self):
        rule = self.prepare()
        self.telegram.delete_error = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            self.combined(rule)
        self.assertEqual(self.latest().state, 'PARTIAL')
        self.assertEqual((self.latest().ban_status, self.latest().delete_status), ('DONE', 'UNCERTAIN'))
        asyncio.run(self.assistant.execute(self.latest().id, 'DELETE_BAN'))
        self.assertEqual(self.telegram.operations, ['ban', 'delete'])

    def test_assist_confirmation_executes_combined_via_same_guards(self):
        rule = self.prepare('ASSIST')
        self.combined(rule)
        row = self.latest()
        page = self.client.get(f'/assistant/{self.agent.id}?tab=review').get_data(as_text=True)
        self.assertIn('Удалить и заблокировать', page)
        data = dict(csrf_token=self.csrf, decision_created_at=row.created_at.isoformat(), action='DELETE_BAN')
        with patch('web.routes.assistant.run', side_effect=lambda coro, **kwargs: asyncio.run(coro)) as run:
            self.client.post(f'/assistant/decisions/{row.id}', data=data)
            run.assert_not_called()
            self.client.post(f'/assistant/decisions/{row.id}', data={**data, 'confirm': 'on'})
        db.session.expire_all()
        self.assertEqual(row.state, 'DELETED_BANNED')

    def test_training_violation_deletion_ban_and_combination_create_rules_not_unsafe_examples(self):
        for num, action in enumerate(('DELETE', 'BAN', 'DELETE_BAN'), 1):
            self.handle(self.event(helpers.message(num)), helpers.decision_result(intent='UNKNOWN', action='HUMAN_REVIEW', reply=''))
            row = self.latest()
            data = self.training_data(row, expected_action=action, expected_intent='SPAM', expected_classification='RULE_VIOLATION',
                confirm_rule='on', rule_enabled='on', text='Реклама стороннего сервиса', guidance='Настоящую рекламу удалять; цитату в жалобе не считать рекламой.')
            self.client.post(f'/assistant/{self.agent.id}/training/{row.id}', data=data)
            self.assertEqual(row.training_status, 'DONE')
            self.assertEqual(AIModerationRule.query.order_by(AIModerationRule.id.desc()).first().action, action)
        self.assertEqual(AIExample.query.count(), 0)
        self.assertEqual(AIModerationRule.query.count(), 3)
        self.assertFalse(self.agent.allow_delete or self.agent.allow_ban or self.telegram.operations)

    def test_training_rule_needs_explicit_confirmation_valid_category_and_anonymization(self):
        row = self.unclear()
        valid = self.training_data(row, expected_action='DELETE_BAN', expected_intent='SPAM', expected_classification='RULE_VIOLATION', confirm_rule='on')
        for extra in ({'confirm_rule': ''}, {'anonymized': ''}, {'expected_classification': 'PAYMENT_DATA'}, {'expected_intent': 'UNKNOWN'}):
            self.client.post(f'/assistant/{self.agent.id}/training/{row.id}', data={**valid, **extra})
            self.assertEqual(AIModerationRule.query.count(), 0)
            self.assertEqual(row.training_status, 'OPEN')
        self.client.post(f'/assistant/{self.agent.id}/training/{row.id}', data=valid)
        self.assertFalse(AIModerationRule.query.one().enabled)

    def test_moderation_crud_accepts_combined_action_and_never_enables_permissions(self):
        self.client.post(f'/assistant/{self.agent.id}/moderation/add', data=self.rule_data(action='DELETE_BAN'))
        self.assertEqual(AIModerationRule.query.one().action, 'DELETE_BAN')
        self.assertFalse(self.agent.allow_ban or self.agent.allow_delete)
        page = self.client.get(f'/assistant/{self.agent.id}?tab=moderation').get_data(as_text=True)
        self.assertIn('Удалить и заблокировать', page)

    def test_all_normal_replies_target_the_trigger_not_the_quoted_post(self):
        self.active()
        self.handle(self.event(helpers.message(sender_id=22, reply_to_msg_id=99)))
        self.assertEqual(self.telegram.sent[0][2]['reply_to'], 1)
        self.assertEqual(self.telegram.operations, ['reply'])

    def test_privacy_warning_replies_before_deletion_and_preserves_cooldown(self):
        self.active()
        self.handle(self.event(helpers.message(sender_id=22)), helpers.decision_result(
            intent='PAYMENT_DATA', classification='PAYMENT_DATA', action='DELETE'))
        self.assertEqual(self.telegram.operations, ['reply', 'delete'])
        self.assertEqual(self.telegram.sent[0][2]['reply_to'], 1)
        self.assertEqual(self.latest().state, 'DELETED')
        self.assertIsNotNone(self.agent.last_reply_at)

    def test_edit_during_warning_cancels_delete_without_sending_unthreaded_fallback(self):
        self.active()
        original = self.telegram.send_message
        async def changed(*args, **kwargs):
            result = await original(*args, **kwargs)
            self.telegram.messages[1].raw_text = 'Edited text'
            return result
        with patch.object(self.telegram, 'send_message', side_effect=changed):
            self.handle(self.event(helpers.message(sender_id=22)), helpers.decision_result(
                intent='PAYMENT_DATA', classification='PAYMENT_DATA', action='DELETE'))
        self.assertEqual(self.latest().state, 'PARTIAL')
        self.assertEqual(self.telegram.operations, ['reply'])

    def test_unknown_warning_still_deletes_once_and_never_replies_without_parent(self):
        self.active()
        with patch.object(self.telegram, 'send_message', new=AsyncMock(side_effect=TimeoutError())) as send:
            self.handle(self.event(helpers.message(sender_id=22)), helpers.decision_result(
                intent='PAYMENT_DATA', classification='PAYMENT_DATA', action='DELETE'))
        self.assertEqual(self.latest().state, 'DELETED')
        self.assertIn('не подтверждено', self.latest().result)
        send.assert_awaited_once()
        self.assertEqual(send.call_args.kwargs['reply_to'], 1)
        self.assertEqual(self.telegram.operations, ['delete'])

    def test_structured_analysis_and_replay_accept_combined_without_telegram(self):
        rule = self.prepare()
        result = self.violation(rule)
        with patch('ai_engine.ask', new=AsyncMock(return_value=result)):
            self.assertEqual(asyncio.run(analyze(self.app, {'language_profile': 'general'}))['action'], 'DELETE_BAN')
        sample = AIExample(agent_id=self.agent.id, title='Combined test', text='Explicit spam', expected={'action': 'DELETE_BAN'})
        db.session.add(sample)
        db.session.commit()
        snapshot = replay_snapshot(self.app, self.agent, sample)
        row = AIReplayRun(example_id=sample.id, request_token='mock', agent_revision=self.agent.revision,
            knowledge_revision=0, prompt_version=snapshot['version'], model='mock')
        db.session.add(row)
        db.session.commit()
        with patch('ai_examples.analyze', new=AsyncMock(return_value=result)):
            asyncio.run(evaluate_replay(self.app, row.id, row.created_at.isoformat(), snapshot))
        self.assertEqual(row.state, 'PASS')
        self.assertFalse(self.telegram.operations)

    def test_legacy_migration_and_restart_preserve_known_subaction_without_resuming(self):
        from config import Config
        from web.app import create_app
        with patch.object(Config, 'DATABASE_URL', 'sqlite:///' + (Path(self.temp.name) / 'combined-migration.db').as_posix()):
            old = create_app()
            with old.app_context():
                agent = AIAgent(name='Old', chat_id='-1001')
                db.session.add(agent)
                db.session.flush()
                db.session.add(AIDecision(agent_id=agent.id, message_id=1, fingerprint='x', agent_revision=0,
                    knowledge_revision=0, state='RUNNING', action='DELETE_BAN', ban_status='DONE', delete_status='RUNNING'))
                db.session.commit()
                db.session.remove()
                db.engine.dispose()
            recovered = create_app()
            with recovered.app_context():
                self.assertEqual(AIDecision.query.one().state, 'UNCERTAIN')
                self.assertEqual((AIDecision.query.one().ban_status, AIDecision.query.one().delete_status), ('DONE', 'UNCERTAIN'))
                for column in ('ban_status', 'delete_status'):
                    db.session.execute(text(f'ALTER TABLE ai_decisions DROP COLUMN {column}'))
                db.session.commit()
                db.session.remove()
                db.engine.dispose()
            migrated = create_app()
            with migrated.app_context():
                self.assertEqual(AIDecision.query.one().ban_status, '')
                self.assertEqual(AIDecision.query.one().delete_status, '')
                self.assertFalse(AIAgent.query.one().allow_ban)
                db.session.remove()
                db.engine.dispose()
