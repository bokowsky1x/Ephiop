import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import text

import test_ai_assistant as helpers
import test_features as fixtures
from ai_assistant import policy
from ai_examples import approved_examples, replay_snapshot, evaluate_replay
from ai_moderation import moderation_rules
from models import Account, AIAgent, AIDecision, AIExample, AIModerationRule, AIReplayRun, db


class BanClient(helpers.AgentClient):
    def __init__(self):
        super().__init__()
        self.banned, self.permissions_calls = [], []
        self.can_ban = True
        self.target_admin, self.target_creator = False, False
        self.ban_error = None

    async def get_permissions(self, chat, user):
        self.permissions_calls.append(user)
        return SimpleNamespace(delete_messages=self.can_delete, ban_users=self.can_ban,
            is_admin=self.target_admin if user != 'me' else True,
            is_creator=self.target_creator if user != 'me' else False)

    async def edit_permissions(self, chat, user, **kwargs):
        self.banned.append((chat, user, kwargs))
        if self.ban_error:
            raise self.ban_error
        return SimpleNamespace(ok=True)


class TrainingTests(unittest.TestCase):
    tearDown = fixtures.FeaturesTest.tearDown
    form = helpers.AssistantTests.form
    event = helpers.AssistantTests.event
    handle = helpers.AssistantTests.handle
    latest = helpers.AssistantTests.latest

    def setUp(self):
        fixtures.FeaturesTest.setUp(self)
        helpers.AssistantTests.setup_agent(self, mode='OBSERVE', language_profile='general',
                                           fallback_language='RUSSIAN', support_contact='')
        self.telegram = BanClient()
        self.manager.clients[self.account_id] = self.telegram

    def unclear(self):
        self.handle(self.event(helpers.message(text='Непонятное выражение')), helpers.decision_result(
            language='RUSSIAN', intent='UNKNOWN', action='HUMAN_REVIEW', reply='', reason='Нужно пояснить смысл'))
        return self.latest()

    def training_data(self, decision, **kwargs):
        data = dict(csrf_token=self.csrf, agent_created_at=self.agent.created_at.isoformat(),
            decision_created_at=decision.created_at.isoformat(), training_action='teach', anonymized='on',
            text='Непонятное выражение', guidance='Это согласие с собеседником, ответ не нужен.',
            expected_language='RUSSIAN', expected_intent='CASUAL_CHAT', expected_classification='SAFE',
            expected_action='IGNORE', corrected_reply='')
        data.update(kwargs)
        return data

    def rule_data(self, **kwargs):
        data = dict(csrf_token=self.csrf, agent_created_at=self.agent.created_at.isoformat(),
            title='Спам', example='Рекламное сообщение, повторённое много раз',
            guidance='Удалять рекламу, не удалять цитаты из жалобы на спам.', action='DELETE', enabled='on', anonymized='on')
        data.update(kwargs)
        return data

    def rule(self, action='BAN', **kwargs):
        data = dict(agent_id=self.agent.id, title='Rule', example='Explicit spam', guidance='Actual violations only.',
                    action=action, enabled=True)
        data.update(kwargs)
        row = AIModerationRule(**data)
        db.session.add(row)
        db.session.commit()
        return row

    def violation(self, rule, **kwargs):
        data = dict(language='RUSSIAN', intent='COMMUNITY_RULE_VIOLATION', classification='RULE_VIOLATION',
                    action=rule.action, confidence=0.99, rule_ids=[rule.id], reply='')
        data.update(kwargs)
        return helpers.decision_result(**data)

    def active(self):
        self.agent.mode, self.agent.allow_ban, self.agent.allow_delete = 'AUTO', True, True
        db.session.commit()

    def test_unclear_message_enters_training_but_assist_confirmation_does_not(self):
        row = self.unclear()
        self.assertTrue(row.training_needed)
        page = self.client.get(f'/assistant/{self.agent.id}?tab=training').get_data(as_text=True)
        self.assertIn('Непонятное выражение', page)
        self.assertIn('Что делать с сообщением', page)
        self.agent.mode = 'ASSIST'
        db.session.commit()
        self.handle(self.event(helpers.message(2, 'Hi')), helpers.decision_result())
        self.assertFalse(self.latest().training_needed)

    def test_api_error_does_not_become_language_training(self):
        with patch('ai_assistant.analyze', new=AsyncMock(side_effect=RuntimeError('SECRET'))):
            asyncio.run(self.assistant.handle_message(self.account_id, self.telegram, self.event()))
        self.assertFalse(self.latest().training_needed)
        page = self.client.get(f'/assistant/{self.agent.id}?tab=training').get_data(as_text=True)
        self.assertIn('Новых вопросов для тренировки нет', page)

    def test_teaching_stores_guidance_without_telegram_actions_and_deduplicates(self):
        decision = self.unclear()
        data = self.training_data(decision)
        for _ in range(2):
            self.client.post(f'/assistant/{self.agent.id}/training/{decision.id}', data=data)
        db.session.expire_all()
        self.assertEqual(AIExample.query.count(), 1)
        example = AIExample.query.one()
        self.assertTrue(example.approved)
        self.assertEqual(example.kind, 'training')
        self.assertEqual(decision.training_status, 'DONE')
        self.assertEqual(approved_examples(self.agent.id)[0]['moderator_guidance'], data['guidance'])
        self.assertFalse(self.telegram.sent or self.telegram.deleted or self.telegram.banned)
        self.assertIn('Сохранённые пояснения', self.client.get(f'/assistant/{self.agent.id}?tab=training').get_data(as_text=True))

    def test_training_requires_guidance_anonymization_and_correct_source_stamp(self):
        decision = self.unclear()
        for extra in ({'guidance': ''}, {'anonymized': ''}, {'decision_created_at': 'old'}):
            self.client.post(f'/assistant/{self.agent.id}/training/{decision.id}', data=self.training_data(decision, **extra))
        self.assertEqual(AIExample.query.count(), 0)
        self.assertEqual(decision.training_status, 'OPEN')

    def test_skip_removes_question_without_punishing_or_training(self):
        decision = self.unclear()
        self.client.post(f'/assistant/{self.agent.id}/training/{decision.id}',
                         data=self.training_data(decision, training_action='dismiss'))
        self.assertEqual(decision.training_status, 'DISMISSED')
        self.assertEqual(AIExample.query.count(), 0)
        self.assertFalse(self.telegram.banned or self.telegram.sent or self.telegram.deleted)

    def test_edited_or_deleted_messages_supersede_old_training_questions(self):
        old = self.unclear()
        self.handle(self.event(helpers.message(1, 'Now clear')), helpers.decision_result())
        self.assertEqual(old.training_status, 'SUPERSEDED')
        self.client.post(f'/assistant/{self.agent.id}/training/{old.id}', data=self.training_data(old))
        self.assertEqual(AIExample.query.count(), 0)

    def test_teaching_is_scoped_and_csrf_protected(self):
        decision = self.unclear()
        other = AIAgent(name='Other', chat_id='-100999')
        db.session.add(other)
        db.session.commit()
        self.client.post(f'/assistant/{other.id}/training/{decision.id}', data=self.training_data(decision,
                         agent_created_at=other.created_at.isoformat()))
        self.assertEqual(AIExample.query.count(), 0)
        self.assertEqual(self.client.post(f'/assistant/{self.agent.id}/training/{decision.id}', data={}).status_code, 400)

    def test_older_relevant_training_is_selected_before_recent_unrelated_examples(self):
        db.session.add(AIExample(agent_id=self.agent.id, title='Relevant', text='вывод завис', expected={},
                                 guidance='Это вопрос о выводе.', approved=True))
        for index in range(7):
            db.session.add(AIExample(agent_id=self.agent.id, title=str(index), text='Совсем другая ситуация', expected={}, approved=True))
        db.session.commit()
        selected = approved_examples(self.agent.id, message='У меня вывод завис')
        self.assertEqual(len(selected), 5)
        self.assertIn('вывод завис', str(selected))

    def test_rule_crud_does_not_enable_delete_or_ban_and_invalidates_decisions(self):
        decision = self.unclear()
        self.client.post(f'/assistant/{self.agent.id}/moderation/add', data=self.rule_data())
        rule = AIModerationRule.query.one()
        self.assertTrue(rule.enabled)
        self.assertFalse(self.agent.allow_ban or self.agent.allow_delete)
        self.assertEqual(decision.state, 'STALE')
        self.client.post(f'/assistant/{self.agent.id}/moderation/{rule.id}', data=self.rule_data(
            rule_created_at=rule.created_at.isoformat(), enabled='', guidance='New explanation'))
        self.assertEqual(moderation_rules(self.agent.id), [])
        self.assertEqual(rule.guidance, 'New explanation')
        self.client.post(f'/assistant/{self.agent.id}/moderation/{rule.id}', data=self.rule_data(
            rule_created_at=rule.created_at.isoformat(), operation='delete', confirm_delete='on'))
        self.assertEqual(AIModerationRule.query.count(), 0)

    def test_rules_require_consent_valid_action_and_scoped_stamp(self):
        for extra in ({'anonymized': ''}, {'action': 'IGNORE'}, {'agent_created_at': 'old'}, {'guidance': ''}):
            self.client.post(f'/assistant/{self.agent.id}/moderation/add', data=self.rule_data(**extra))
        self.assertEqual(AIModerationRule.query.count(), 0)
        self.assertEqual(self.client.post(f'/assistant/{self.agent.id}/moderation/add', data={}).status_code, 400)

    def test_rule_limit_is_twenty(self):
        for _ in range(20):
            self.rule('DELETE')
        self.client.post(f'/assistant/{self.agent.id}/moderation/add', data=self.rule_data())
        self.assertEqual(AIModerationRule.query.count(), 20)

    def test_rules_of_other_agents_never_authorize_action(self):
        other = AIAgent(name='Other', chat_id='-100999')
        db.session.add(other)
        db.session.commit()
        rule = self.rule(agent_id=other.id)
        self.active()
        self.handle(self.event(helpers.message(sender_id=22)), self.violation(rule))
        self.assertEqual(self.latest().state, 'REVIEW')
        self.assertFalse(self.telegram.banned)

    def test_delete_rule_authorizes_only_matching_enabled_rule(self):
        rule = self.rule('DELETE')
        self.active()
        self.handle(self.event(helpers.message(sender_id=22)), self.violation(rule))
        self.assertEqual(self.latest().state, 'DELETED')
        self.assertEqual(self.telegram.deleted, [1])
        self.assertFalse(self.telegram.sent)

    def test_disabled_rule_or_wrong_id_cannot_delete_or_ban(self):
        self.active()
        for index, action in enumerate(('DELETE', 'BAN'), 1):
            rule = self.rule(action, enabled=False)
            self.handle(self.event(helpers.message(index, sender_id=22)), self.violation(rule))
            self.assertEqual(self.latest().state, 'REVIEW')
        self.assertFalse(self.telegram.banned or self.telegram.deleted)

    def test_ban_is_opt_in_confidence_gated_and_bound_to_rule_action(self):
        rule = self.rule('BAN')
        for index, (allowed, confidence) in enumerate(((False, 0.99), (True, 0.96)), 1):
            self.agent.allow_ban = allowed
            db.session.commit()
            decision = SimpleNamespace(**self.violation(rule, confidence=confidence), has_image=False)
            self.assertEqual(policy(self.agent, decision, [])[0], 'REVIEW')
        decision = SimpleNamespace(**self.violation(rule, action='DELETE'), has_image=False)
        self.assertEqual(policy(self.agent, decision, [])[0], 'REVIEW')

    def test_auto_ban_calls_telegram_once_no_message_deletion_and_no_repeat(self):
        rule = self.rule()
        self.active()
        event = self.event(helpers.message(sender_id=22))
        self.handle(event, self.violation(rule))
        decision = self.latest()
        self.assertEqual(decision.state, 'BANNED')
        self.assertEqual(self.telegram.banned, [(-100123, 22, {'view_messages': False})])
        self.assertFalse(self.telegram.deleted)
        asyncio.run(self.assistant.execute(decision.id, 'BAN', decision.created_at.isoformat()))
        self.handle(event, self.violation(rule))
        self.assertEqual(len(self.telegram.banned), 1)

    def test_ban_requires_rights_and_never_targets_admin_creator_or_unknown_role(self):
        rule = self.rule()
        self.active()
        for index, (can_ban, admin, creator) in enumerate(((False, False, False), (True, True, False), (True, False, True), (True, None, False)), 1):
            self.telegram.can_ban, self.telegram.target_admin, self.telegram.target_creator = can_ban, admin, creator
            self.handle(self.event(helpers.message(index, sender_id=22)), self.violation(rule))
            self.assertEqual(self.latest().state, 'REVIEW')
        self.assertFalse(self.telegram.banned)

    def test_ban_never_targets_channel_wrong_sender_or_own_account(self):
        rule = self.rule()
        self.active()
        for index, sender in enumerate((-10099, 33, 777, None), 1):
            self.handle(self.event(helpers.message(index, sender_id=sender)), self.violation(rule))
            self.assertEqual(self.latest().state, 'REVIEW')
        self.assertFalse(self.telegram.banned)

    def test_ban_cannot_punish_payment_data_or_ignore_disabled_vision(self):
        rule = self.rule()
        self.active()
        for decision in (SimpleNamespace(**self.violation(rule, classification='PAYMENT_DATA'), has_image=False),
                         SimpleNamespace(**self.violation(rule), has_image=True)):
            self.assertEqual(policy(self.agent, decision, [])[0], 'REVIEW')

    def test_assist_requires_confirmation_then_bans_via_same_checks(self):
        rule = self.rule()
        self.active()
        self.agent.mode = 'ASSIST'
        db.session.commit()
        self.handle(self.event(helpers.message(sender_id=22)), self.violation(rule))
        decision = self.latest()
        self.assertEqual(decision.state, 'REVIEW')
        self.assertFalse(self.telegram.banned)
        data = dict(csrf_token=self.csrf, decision_created_at=decision.created_at.isoformat(), action='BAN')
        with patch('web.routes.assistant.run', side_effect=lambda coro, timeout: asyncio.run(coro)) as execute:
            response = self.client.post(f'/assistant/decisions/{decision.id}', data=data)
            self.assertEqual(response.status_code, 302)
            execute.assert_not_called()
            self.assertFalse(self.telegram.banned)
            data['confirm'] = 'on'
            self.client.post(f'/assistant/decisions/{decision.id}', data=data)
        db.session.expire_all()
        self.assertEqual(decision.state, 'BANNED')

    def test_safe_or_unrelated_classification_can_never_use_ban_rule(self):
        rule = self.rule()
        self.active()
        decision = SimpleNamespace(**self.violation(rule, classification='SAFE', intent='GREETING'), has_image=False)
        self.assertEqual(policy(self.agent, decision, [])[0], 'REVIEW')

    def test_settings_validate_high_ban_threshold_and_keep_opt_in_explicit(self):
        self.client.post(f'/assistant/{self.agent.id}', data=self.form(allow_ban='on', ban_confidence='0.5'))
        self.assertFalse(self.agent.allow_ban)
        self.client.post(f'/assistant/{self.agent.id}', data=self.form(allow_ban='on', ban_confidence='0.99'))
        db.session.expire_all()
        self.assertTrue(self.agent.allow_ban)
        self.assertEqual(self.agent.ban_confidence, 0.99)

    def test_invalid_rule_identifier_in_structured_result_is_rejected(self):
        from ai_engine import AnalysisError, analyze
        result = helpers.decision_result(rule_ids=['not an integer'])
        with patch('ai_engine.ask', new=AsyncMock(return_value=result)):
            with self.assertRaises(AnalysisError):
                asyncio.run(analyze(self.app, {'language_profile': 'general'}))

    def test_unknown_ban_result_is_not_retried_and_does_not_leak_error(self):
        rule = self.rule()
        self.active()
        self.telegram.ban_error = RuntimeError('PRIVATE_SECRET')
        self.handle(self.event(helpers.message(sender_id=22)), self.violation(rule))
        decision = self.latest()
        self.assertEqual(decision.state, 'UNCERTAIN')
        self.assertNotIn('PRIVATE_SECRET', decision.result)
        asyncio.run(self.assistant.execute(decision.id, 'BAN', decision.created_at.isoformat()))
        self.assertEqual(len(self.telegram.banned), 1)

    def test_rule_change_makes_prepared_ban_stale(self):
        rule = self.rule()
        self.active()
        self.agent.mode = 'ASSIST'
        db.session.commit()
        self.handle(self.event(helpers.message(sender_id=22)), self.violation(rule))
        decision = self.latest()
        self.client.post(f'/assistant/{self.agent.id}/moderation/{rule.id}', data=self.rule_data(
            action='BAN', rule_created_at=rule.created_at.isoformat(), enabled=''))
        self.assertEqual(asyncio.run(self.assistant.execute(decision.id, 'BAN', decision.created_at.isoformat())), 'STALE')
        self.assertFalse(self.telegram.banned)

    def test_replay_can_simulate_ban_without_telegram_calls(self):
        rule = self.rule()
        self.active()
        example = AIExample(agent_id=self.agent.id, title='Ban case', text='Explicit spam', expected={'action': 'BAN'})
        db.session.add(example)
        db.session.commit()
        snapshot = replay_snapshot(self.app, self.agent, example)
        run = AIReplayRun(example_id=example.id, request_token='mock', agent_revision=self.agent.revision,
            knowledge_revision=0, prompt_version=snapshot['version'], model='mock')
        db.session.add(run)
        db.session.commit()
        with patch('ai_engine.ask', new=AsyncMock(return_value=self.violation(rule))):
            asyncio.run(evaluate_replay(self.app, run.id, run.created_at.isoformat(), snapshot))
        self.assertEqual(run.state, 'PASS')
        self.assertTrue(run.outcome['would_execute'])
        self.assertFalse(self.telegram.banned or self.telegram.deleted or self.telegram.sent)

    def test_deleting_agent_also_deletes_rules(self):
        self.rule()
        self.agent.mode = 'OFF'
        db.session.commit()
        self.client.post(f'/assistant/{self.agent.id}/delete', data=dict(csrf_token=self.csrf,
            agent_created_at=self.agent.created_at.isoformat(), confirm_delete='on'))
        self.assertEqual(AIModerationRule.query.count(), 0)
        self.assertIsNotNone(db.session.get(Account, self.account_id))

    def test_legacy_database_migrates_without_enabling_bans(self):
        from config import Config
        from web.app import create_app
        with patch.object(Config, 'DATABASE_URL', 'sqlite:///' + (Path(self.temp.name) / 'training-migration.db').as_posix()):
            old = create_app()
            with old.app_context():
                agent = AIAgent(name='Legacy', chat_id='-1001')
                db.session.add(agent)
                db.session.flush()
                db.session.add(AIDecision(agent_id=agent.id, message_id=1, fingerprint='x', agent_revision=0,
                                         knowledge_revision=0, state='REVIEW', confidence=0.4))
                db.session.add(AIExample(agent_id=agent.id, title='Legacy'))
                db.session.commit()
                for table, columns in {'ai_agents': ('allow_ban', 'ban_confidence'),
                    'ai_decisions': ('rule_ids', 'training_needed', 'training_status'),
                    'ai_examples': ('guidance', 'kind')}.items():
                    for column in columns:
                        db.session.execute(text(f'ALTER TABLE {table} DROP COLUMN {column}'))
                db.session.commit()
                db.session.remove()
                db.engine.dispose()
            recovered = create_app()
            with recovered.app_context():
                self.assertFalse(AIAgent.query.one().allow_ban)
                self.assertEqual(AIAgent.query.one().ban_confidence, 0.98)
                self.assertTrue(AIDecision.query.one().training_needed)
                self.assertEqual(AIDecision.query.one().rule_ids, [])
                self.assertEqual(AIExample.query.one().guidance, '')
                db.session.remove()
                db.engine.dispose()
