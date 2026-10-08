import asyncio
from io import BytesIO
from pathlib import Path
import re
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image
from sqlalchemy import text

import test_features as fixtures
import test_ai_assistant as helpers
from ai_assistant import policy, privacy_warning, support_reply
from ai_engine import AnalysisError, analysis_resources, analyze
from ai_examples import approved_examples
from ai_locales import has_language_hint, parse_glossary, valid_contact
from models import AIAgent, AIDecision, AIExample, AIReplayRun, db


class ExampleTests(unittest.TestCase):
    tearDown = fixtures.FeaturesTest.tearDown
    form = helpers.AssistantTests.form
    event = helpers.AssistantTests.event
    handle = helpers.AssistantTests.handle
    latest = helpers.AssistantTests.latest

    def setUp(self):
        fixtures.FeaturesTest.setUp(self)
        helpers.AssistantTests.setup_agent(self, mode='OFF', language_profile='general',
                                           fallback_language='RUSSIAN', support_contact='')

    def add_data(self, **kwargs):
        values = dict(csrf_token=self.csrf, agent_created_at=self.agent.created_at.isoformat(), anonymized='on',
                      title='Greeting', text='Привет', expected_language='RUSSIAN', expected_intent='GREETING',
                      expected_classification='SAFE', expected_action='REPLY', corrected_reply='Привет!')
        values.update(kwargs)
        return values

    def add(self, **kwargs):
        return self.client.post(f'/assistant/{self.agent.id}/examples/add', data=self.add_data(**kwargs))

    def token(self, example):
        page = self.client.get(f'/assistant/{self.agent.id}?tab=examples').get_data(as_text=True)
        return re.search(r'name="request_token" value="([^"]+)"', page)[1]

    def target_data(self, example, **kwargs):
        values = dict(csrf_token=self.csrf, agent_created_at=self.agent.created_at.isoformat(),
                      example_created_at=example.created_at.isoformat())
        values.update(kwargs)
        return values

    def run_case(self, example, result=None, token=None, **kwargs):
        token = token or self.token(example)
        values = self.target_data(example, confirm_ai='on', request_token=token, **kwargs)
        fake = AsyncMock(return_value=result or helpers.decision_result(language='RUSSIAN', reply='Привет!'))
        with patch('ai_engine.ask', fake):
            response = self.client.post(f'/assistant/{self.agent.id}/examples/{example.id}/run', data=values)
        db.session.expire_all()
        return response, fake

    def test_new_ui_defaults_are_general_without_betjam_contact(self):
        page = self.client.get('/assistant/').get_data(as_text=True)
        self.assertIn('value="general" selected', page)
        self.assertIn('value="ENGLISH" selected', page)
        self.assertNotIn('support@betjam.com', page)

    def test_russian_settings_glossary_and_support_are_saved(self):
        self.client.post(f'/assistant/{self.agent.id}', data=self.form(mode='OFF', fallback_language='RUSSIAN',
            language_profile='general', glossary='прив = привет', support_contact='https://example.org/support'))
        db.session.expire_all()
        self.assertEqual(self.agent.fallback_language, 'RUSSIAN')
        self.assertEqual(self.agent.glossary, 'прив = привет')
        self.assertNotIn('betjam', support_reply('RUSSIAN', self.agent))
        self.assertIn('https://example.org/support', support_reply('RUSSIAN', self.agent))

    def test_switching_legacy_profile_drops_inherited_betjam_email(self):
        self.agent.language_profile, self.agent.support_contact = 'ethiopia', 'support@betjam.com'
        db.session.commit()
        self.client.post(f'/assistant/{self.agent.id}', data=self.form(mode='OFF', fallback_language='ENGLISH',
            language_profile='general', support_contact='support@betjam.com'))
        db.session.expire_all()
        self.assertEqual(self.agent.support_contact, '')

    def test_custom_language_requires_name_and_verified_notices(self):
        self.client.post(f'/assistant/{self.agent.id}', data=self.form(fallback_language='CUSTOM', custom_language='Swahili'))
        db.session.expire_all()
        self.assertEqual(self.agent.mode, 'OFF')
        self.client.post(f'/assistant/{self.agent.id}', data=self.form(fallback_language='CUSTOM', custom_language='Swahili',
            privacy_text='Please remove private details.', support_text='Contact official support.', language_profile='general'))
        db.session.expire_all()
        self.assertEqual(self.agent.custom_language, 'Swahili')
        self.assertEqual(self.agent.fallback_language, 'CUSTOM')
        self.assertEqual(privacy_warning('CUSTOM', agent=self.agent), 'Please remove private details.')

    def test_notice_without_translation_is_reviewed_not_sent_in_wrong_language(self):
        from types import SimpleNamespace
        decision = SimpleNamespace(**helpers.decision_result(language='SPANISH', intent='DEPOSIT_PROBLEM'), has_image=False)
        state, _ = policy(self.agent, decision, [])
        self.assertEqual(state, 'REVIEW')
        self.assertEqual(decision.reply, '')

    def test_save_requires_anonymization_and_valid_labels(self):
        self.add(anonymized='')
        self.assertEqual(AIExample.query.count(), 0)
        self.add(expected_language='INVALID')
        self.assertEqual(AIExample.query.count(), 0)
        self.add(text='Call +12345678901 or me@example.org')
        saved = AIExample.query.one()
        self.assertNotIn('12345678901', saved.text)
        self.assertNotIn('me@example.org', saved.text)

    def test_unsafe_delete_cannot_be_approved(self):
        self.add(approved='on', expected_action='DELETE', expected_classification='SAFE')
        self.assertEqual(AIExample.query.count(), 0)

    def test_gambling_feedback_cannot_override_human_review(self):
        self.add(approved='on', expected_intent='GAMBLING_INSTRUCTIONS')
        self.assertEqual(AIExample.query.count(), 0)

    def test_approved_examples_are_bounded_scoped_and_invalidated(self):
        other = AIAgent(name='Other', chat_id='-100999')
        db.session.add(other)
        db.session.flush()
        db.session.add(AIExample(agent_id=other.id, title='Other', text='OTHER CHAT SECRET', approved=True,
                                 expected={}, corrected_reply=''))
        decision = AIDecision(agent_id=self.agent.id, message_id=1, fingerprint='x', agent_revision=self.agent.revision,
                              knowledge_revision=0, state='READY')
        db.session.add(decision)
        db.session.commit()
        for index in range(7):
            self.add(approved='on', title=str(index), text=f'Example {index}')
        values = approved_examples(self.agent.id)
        self.assertEqual(len(values), 5)
        self.assertEqual(values[0]['message'], 'Example 2')
        self.assertNotIn('OTHER CHAT SECRET', str(values))
        self.assertEqual(db.session.get(AIDecision, decision.id).state, 'STALE')

    def test_approval_can_be_revoked_and_example_deleted_with_confirmation(self):
        self.add(approved='on')
        example = AIExample.query.one()
        self.client.post(f'/assistant/{self.agent.id}/examples/{example.id}/approval', data=self.target_data(example))
        db.session.expire_all()
        self.assertFalse(example.approved)
        self.assertEqual(approved_examples(self.agent.id), [])
        self.client.post(f'/assistant/{self.agent.id}/examples/{example.id}/delete', data=self.target_data(example))
        self.assertEqual(AIExample.query.count(), 1)
        self.client.post(f'/assistant/{self.agent.id}/examples/{example.id}/delete', data=self.target_data(example, confirm_delete='on'))
        self.assertEqual(AIExample.query.count(), 0)

    def test_replay_is_isolated_works_off_and_deduplicates_same_request(self):
        self.add(approved='on')
        example = AIExample.query.one()
        token = self.token(example)
        self.app.telegram_manager = None
        _, fake = self.run_case(example, token=token)
        fake.assert_awaited_once()
        self.assertEqual(AIReplayRun.query.one().state, 'PASS')
        self.assertEqual(AIDecision.query.count(), 0)
        self.assertFalse(self.telegram.sent or self.telegram.deleted)
        payload = fake.call_args.args[3]
        self.assertEqual(payload['approved_examples'], [])
        self.assertEqual(payload['slang'], {})
        self.assertTrue(payload['has_user_language_hint'])
        _, duplicate = self.run_case(example, token=token)
        duplicate.assert_not_awaited()
        self.assertEqual(AIReplayRun.query.count(), 1)

    def test_replay_records_differences_and_policy_veto(self):
        self.add(expected_action='DELETE', expected_policy_state='READY')
        example = AIExample.query.one()
        self.run_case(example, result=helpers.decision_result(language='RUSSIAN', action='DELETE'))
        replay = AIReplayRun.query.one()
        self.assertEqual(replay.state, 'FAIL')
        self.assertEqual(replay.outcome['policy_state'], 'REVIEW')
        self.assertIn('policy_state', replay.outcome['mismatches'])
        self.assertFalse(replay.outcome['would_execute'])
        self.assertEqual(len(replay.prompt_version), 64)

    def test_replay_requires_api_confirmation_consent_and_key(self):
        self.add()
        example = AIExample.query.one()
        with patch('ai_engine.ask', new=AsyncMock()) as fake:
            self.client.post(f'/assistant/{self.agent.id}/examples/{example.id}/run', data=self.target_data(example, request_token=self.token(example)))
            self.agent.consent = False
            db.session.commit()
            self.run_case(example)
            fake.assert_not_awaited()
        self.assertEqual(AIReplayRun.query.count(), 0)

    def test_bad_signed_token_and_csrf_are_rejected(self):
        self.add()
        example = AIExample.query.one()
        response, fake = self.run_case(example, token='tampered')
        self.assertEqual(response.status_code, 400)
        fake.assert_not_awaited()
        for suffix in ('run', 'approval', 'delete'):
            response = self.client.post(f'/assistant/{self.agent.id}/examples/{example.id}/{suffix}', data={})
            self.assertEqual(response.status_code, 400)

    def test_missing_key_and_running_example_block_new_calls(self):
        self.add()
        example = AIExample.query.one()
        self.app.config['OPENAI_API_KEY'] = ''
        _, fake = self.run_case(example)
        fake.assert_not_awaited()
        self.app.config['OPENAI_API_KEY'] = 'mock'
        example.running = True
        db.session.commit()
        _, fake = self.run_case(example)
        fake.assert_not_awaited()
        self.assertEqual(AIReplayRun.query.count(), 0)

    def test_running_case_cannot_be_approved_or_deleted(self):
        self.add()
        example = AIExample.query.one()
        example.running = True
        db.session.commit()
        for suffix, extra in (('approval', {'approved': 'on'}), ('delete', {'confirm_delete': 'on'})):
            self.client.post(f'/assistant/{self.agent.id}/examples/{example.id}/{suffix}',
                             data=self.target_data(example, **extra))
        db.session.expire_all()
        self.assertEqual(AIExample.query.count(), 1)
        self.assertFalse(example.approved)
        self.client.post(f'/assistant/{self.agent.id}/delete', data=dict(csrf_token=self.csrf,
            agent_created_at=self.agent.created_at.isoformat(), confirm_delete='on'))
        self.assertIsNotNone(db.session.get(AIAgent, self.agent.id))

    def test_feedback_with_unverified_link_and_empty_reply_is_not_approved(self):
        self.add(approved='on', corrected_reply='Go to https://example.org')
        self.assertEqual(AIExample.query.count(), 0)
        self.add(approved='on', corrected_reply='')
        self.assertEqual(AIExample.query.count(), 0)

    def test_replay_limit_and_stale_creation_stamp_prevent_requests(self):
        self.add()
        example = AIExample.query.one()
        for index in range(12):
            db.session.add(AIReplayRun(example_id=example.id, request_token=str(index), state='PASS',
                agent_revision=0, knowledge_revision=0, prompt_version='test', model='mock'))
        db.session.commit()
        _, fake = self.run_case(example)
        fake.assert_not_awaited()
        with patch('ai_engine.ask', new=AsyncMock()) as fake:
            self.client.post(f'/assistant/{self.agent.id}/examples/{example.id}/run', data=self.target_data(example,
                example_created_at='2000-01-01', request_token=self.token(example), confirm_ai='on'))
            fake.assert_not_awaited()
        self.assertEqual(AIReplayRun.query.count(), 12)

    def test_replay_error_hides_raw_data_and_allows_explicit_retry(self):
        self.add()
        example = AIExample.query.one()
        with patch('ai_engine.ask', new=AsyncMock(side_effect=RuntimeError('PRIVATE_SCREENSHOT_SECRET'))):
            self.client.post(f'/assistant/{self.agent.id}/examples/{example.id}/run', data=self.target_data(example,
                confirm_ai='on', request_token=self.token(example)))
        db.session.expire_all()
        replay = AIReplayRun.query.one()
        self.assertEqual(replay.state, 'ERROR')
        self.assertNotIn('PRIVATE_SCREENSHOT_SECRET', replay.result)
        self.assertFalse(example.running)
        self.run_case(example)
        self.assertEqual(AIReplayRun.query.count(), 2)

    def test_mid_replay_configuration_change_marks_result_stale(self):
        self.add()
        example = AIExample.query.one()
        async def changed(*args, **kwargs):
            self.agent.revision += 1
            db.session.commit()
            return helpers.decision_result(language='RUSSIAN')
        with patch('ai_engine.ask', new=changed):
            self.client.post(f'/assistant/{self.agent.id}/examples/{example.id}/run', data=self.target_data(example,
                confirm_ai='on', request_token=self.token(example)))
        self.assertEqual(AIReplayRun.query.one().state, 'STALE')

    def test_image_is_normalized_private_and_uses_fallback_without_caption(self):
        stream, name = fixtures.picture()
        self.add(text='', image=(stream, name), expected_language='RUSSIAN')
        example = AIExample.query.one()
        with Image.open(BytesIO(example.image)) as image:
            self.assertEqual(image.format, 'JPEG')
            self.assertFalse(image.getexif())
        self.assertEqual(self.client.get('/media/' + name).status_code, 404)
        self.agent.vision = True
        db.session.commit()
        _, fake = self.run_case(example, result=helpers.decision_result(language='ENGLISH'))
        self.assertEqual(AIReplayRun.query.one().outcome['language'], 'RUSSIAN')
        self.assertEqual(fake.call_args.args[-1]['type'], 'input_image')

    def test_replay_without_vision_records_safe_error_not_action(self):
        self.add(image=fixtures.picture())
        _, fake = self.run_case(AIExample.query.one())
        fake.assert_not_awaited()
        self.assertEqual(AIReplayRun.query.one().state, 'ERROR')
        self.assertEqual(AIDecision.query.count(), 0)

    def test_bad_image_and_cross_agent_requests_are_rejected(self):
        self.add(image=(BytesIO(b'not an image'), 'test.png'))
        self.assertEqual(AIExample.query.count(), 0)
        self.add()
        example = AIExample.query.one()
        other = AIAgent(name='Other', chat_id='-100999')
        db.session.add(other)
        db.session.commit()
        response = self.client.post(f'/assistant/{other.id}/examples/{example.id}/delete',
                                    data=self.target_data(example, confirm_delete='on'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(AIExample.query.count(), 1)

    def test_deleting_agent_purges_examples_runs_but_not_account(self):
        self.add()
        self.run_case(AIExample.query.one())
        response = self.client.post(f'/assistant/{self.agent.id}/delete', data=dict(csrf_token=self.csrf,
            agent_created_at=self.agent.created_at.isoformat(), confirm_delete='on'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(AIExample.query.count(), 0)
        self.assertEqual(AIReplayRun.query.count(), 0)
        self.assertEqual(len(self.telegram.deleted), 0)

    def test_live_pipeline_gets_chat_scoped_language_and_feedback(self):
        self.agent.mode = 'OBSERVE'
        self.agent.language_instructions, self.agent.glossary = 'Brief Russian', 'прив = привет'
        db.session.commit()
        self.add(approved='on')
        _, fake = self.handle(self.event(helpers.message(text='Прив')), helpers.decision_result(language='RUSSIAN'))
        payload = fake.call_args.args[1]
        self.assertEqual(payload['language_profile'], 'general')
        self.assertEqual(payload['glossary'], {'прив': 'привет'})
        self.assertEqual(len(payload['approved_examples']), 1)
        self.assertTrue(payload['has_user_language_hint'])

    def test_legacy_migration_and_interrupted_replay_do_not_enable_actions(self):
        from config import Config
        from web.app import create_app
        with patch.object(Config, 'DATABASE_URL', 'sqlite:///' + (Path(self.temp.name) / 'migration.db').as_posix()):
            old = create_app()
            with old.app_context():
                agent = AIAgent(name='Legacy', chat_id='-1001')
                db.session.add(agent)
                db.session.flush()
                example = AIExample(agent_id=agent.id, title='Old', running=True)
                db.session.add(example)
                db.session.flush()
                db.session.add(AIReplayRun(example_id=example.id, request_token='x', agent_revision=0,
                    knowledge_revision=0, prompt_version='old', model='test'))
                db.session.commit()
                for column in ('language_profile', 'custom_language', 'language_instructions', 'glossary',
                               'support_contact', 'support_text', 'privacy_text'):
                    db.session.execute(text(f'ALTER TABLE ai_agents DROP COLUMN {column}'))
                db.session.commit()
                db.session.remove()
                db.engine.dispose()
            for _ in range(2):
                recovered = create_app()
                with recovered.app_context():
                    agent = AIAgent.query.one()
                    self.assertEqual(agent.language_profile, 'ethiopia')
                    self.assertEqual(agent.support_contact, 'support@betjam.com')
                    self.assertFalse(agent.allow_delete)
                    self.assertEqual(AIReplayRun.query.one().state, 'ERROR')
                    self.assertFalse(AIExample.query.one().running)
                    db.session.remove()
                    db.engine.dispose()


class LanguageTests(unittest.TestCase):
    def test_unicode_hint_and_no_ocr_or_private_placeholder_hint(self):
        for sample in ('Привет', 'مرحبا', '你好', 'ሰላም', 'Hi'):
            self.assertTrue(has_language_hint(sample))
        for sample in ('[PRIVATE]', '12345', '?!'):
            self.assertFalse(has_language_hint(sample))

    def test_general_profile_does_not_require_ethiopian_dictionary(self):
        with patch('ai_engine.Path.read_text', return_value='Core policy') as read:
            self.assertEqual(analysis_resources('general'), ('Core policy', {}))
            self.assertEqual(read.call_count, 1)

    def test_structured_schema_accepts_other_languages_and_keeps_core_rules(self):
        from types import SimpleNamespace
        with patch('ai_engine.ask', new=AsyncMock(return_value=helpers.decision_result(language='ARABIC'))) as fake:
            result = asyncio.run(analyze(SimpleNamespace(config={}), dict(language_profile='general', glossary={'term': 'meaning'})))
        self.assertEqual(result['language'], 'ARABIC')
        self.assertIn('Never proactively advertise', fake.call_args.args[2])
        self.assertEqual(fake.call_args.args[3]['slang'], {'term': 'meaning'})

    def test_glossary_limits_and_contact_validation(self):
        self.assertEqual(parse_glossary('прив = привет\n\n'), {'прив': 'привет'})
        with self.assertRaises(ValueError):
            parse_glossary('missing separator')
        with self.assertRaises(ValueError):
            parse_glossary('\n'.join(f'{i} = term' for i in range(31)))
        for value in ('', 'support@example.org', 'https://example.org/support'):
            self.assertTrue(valid_contact(value))
        for value in ('http://example.org', 'javascript:alert(1)', 'https://user:pass@example.org', 'email\n@example.org'):
            self.assertFalse(valid_contact(value))
