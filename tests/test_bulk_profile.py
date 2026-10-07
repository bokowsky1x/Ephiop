import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telethon import errors, functions

import test_features as fixtures
from test_profile_bio import ProfileClient
from models import Account, db
from profile_generation import generate_profiles
from web.routes import bulk_profile


class BulkProfileTests(unittest.TestCase):
    setUp = fixtures.FeaturesTest.setUp
    tearDown = fixtures.FeaturesTest.tearDown

    def setup_profiles(self):
        second = Account(name='Второй', phone='+20000000000', api_id=1, api_hash='test', status='authorized')
        db.session.add(second)
        db.session.commit()
        self.second_id = second.id
        self.first = ProfileClient(about='Первое описание')
        self.second = ProfileClient(about='Второе описание')
        self.manager.clients.update({self.account_id: self.first, self.second_id: self.second})
        self.runner = patch('web.routes.bulk_profile.run', side_effect=lambda coroutine, **kwargs: asyncio.run(coroutine))
        self.runner.start()
        self.addCleanup(self.runner.stop)
        self.client.get('/accounts/bulk-profile')
        with self.client.session_transaction() as state:
            self.csrf = state['bulk_profile_csrf']

    def preview(self, **changes):
        data = dict(csrf_token=self.csrf, account_ids=[self.account_id, self.second_id],
                    name_mode='keep', about_mode='clear')
        data.update(changes)
        return self.client.post('/accounts/bulk-profile/preview', json=data)

    def apply(self, plan, account_id=None, **changes):
        data = dict(csrf_token=self.csrf)
        data.update(changes)
        return self.client.post(f'/accounts/bulk-profile/{plan["plan_id"]}/{account_id or self.account_id}/apply', json=data)

    def writes(self, client):
        return [req for req in client.requests if isinstance(req, functions.account.UpdateProfileRequest)]

    def test_selection_page_and_accounts_checkboxes(self):
        self.setup_profiles()
        page = self.client.get(f'/accounts/bulk-profile?account_ids={self.account_id}').get_data(as_text=True)
        self.assertIn('Промпт для имён', page)
        self.assertIn('Удалить описание', page)
        self.assertIn(f'value="{self.account_id}" checked', page)
        listing = self.client.get('/accounts/').get_data(as_text=True)
        self.assertIn('form="bulk-account-selection"', listing)
        self.assertIn('Выбрать все аккаунты', listing)

    def test_preview_reads_existing_bios_without_writing(self):
        self.setup_profiles()
        response = self.preview()
        self.assertEqual(response.status_code, 200)
        rows = response.json['rows']
        self.assertEqual([row['before']['about'] for row in rows], ['Первое описание', 'Второе описание'])
        self.assertEqual(rows[0]['changes'], {'about': ''})
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertFalse(self.writes(self.first))
        self.assertFalse(self.writes(self.second))

    def test_clear_only_selected_and_duplicate_apply_does_not_repeat(self):
        self.setup_profiles()
        plan = self.preview(account_ids=[self.account_id]).json
        first = self.apply(plan)
        self.assertEqual(first.json['status'], 'success')
        self.assertEqual(self.apply(plan).json['status'], 'success')
        self.assertEqual(self.first.about, '')
        self.assertEqual(self.second.about, 'Второе описание')
        self.assertEqual(len(self.writes(self.first)), 1)
        self.assertIsNone(self.writes(self.first)[0].first_name)
        self.assertEqual(self.apply(plan, self.second_id).status_code, 404)

    def test_exact_text_and_link_are_not_sent_to_ai(self):
        self.setup_profiles()
        with patch('web.routes.bulk_profile.generate_profiles', new_callable=AsyncMock) as ai:
            plan = self.preview(about_mode='text', about_text='Наш чат', about_url='https://t.me/test').json
            ai.assert_not_awaited()
        self.apply(plan)
        self.apply(plan, self.second_id)
        self.assertEqual(self.first.about, 'Наш чат\nhttps://t.me/test')
        self.assertEqual(self.first.about, self.second.about)
        self.assertEqual(self.first.first_name, 'Original')

    def test_ai_names_preserve_bio_and_labels_unless_requested(self):
        self.setup_profiles()
        suggestion = dict(first_name='Abebe', last_name='Kebede', about='')
        with patch('web.routes.bulk_profile.generate_profiles', new=AsyncMock(return_value=[suggestion])) as ai:
            plan = self.preview(account_ids=[self.account_id], name_mode='ai', name_prompt='Typical Ethiopian names', about_mode='keep').json
            self.assertEqual(ai.call_args.args[2], 'Typical Ethiopian names')
            self.assertEqual(ai.call_args.args[3], '')
        self.apply(plan)
        self.assertEqual(self.first.first_name, 'Abebe')
        self.assertEqual(self.first.about, 'Первое описание')
        self.assertEqual(db.session.get(Account, self.account_id).name, 'Тест')
        with patch('web.routes.bulk_profile.generate_profiles', new=AsyncMock(return_value=[dict(first_name='Hana', last_name='', about='')])):
            plan = self.preview(account_ids=[self.account_id], name_mode='ai', name_prompt='Names', about_mode='keep', sync_labels=True).json
        self.apply(plan)
        self.assertEqual(db.session.get(Account, self.account_id).name, 'Hana')

    def test_ai_bio_appends_exact_link_and_reserves_space(self):
        self.setup_profiles()
        link = 'https://t.me/test'
        with patch('web.routes.bulk_profile.generate_profiles', new=AsyncMock(return_value=[dict(first_name='', last_name='', about='Привет')])) as ai:
            plan = self.preview(account_ids=[self.account_id], about_mode='ai', about_prompt='Short community description', about_url=link).json
            self.assertEqual(ai.call_args.args[4], 70 - len(link) - 1)
        self.apply(plan)
        self.assertEqual(self.first.about, 'Привет\n' + link)

    def test_limits_are_per_account_and_count_utf16(self):
        self.setup_profiles()
        self.second.premium = True
        plan = self.preview(about_mode='text', about_text='😀' * 40).json
        self.assertEqual([row['status'] for row in plan['rows']], ['error', 'pending'])
        self.assertEqual(self.apply(plan).json['status'], 'error')
        self.assertFalse(self.writes(self.first))
        self.assertEqual(self.apply(plan, self.second_id).json['status'], 'success')

    def test_noop_is_not_written(self):
        self.setup_profiles()
        self.first.about = ''
        plan = self.preview(account_ids=[self.account_id]).json
        self.assertEqual(plan['rows'][0]['status'], 'unchanged')
        self.apply(plan)
        self.assertFalse(self.writes(self.first))

    def test_current_fields_are_checked_before_apply(self):
        self.setup_profiles()
        plan = self.preview().json
        self.first.about = 'Edited in Telegram'
        self.assertEqual(self.apply(plan).json['status'], 'error')
        self.assertFalse(self.writes(self.first))
        self.assertEqual(self.first.about, 'Edited in Telegram')
        self.assertEqual(self.apply(plan, self.second_id).json['status'], 'success')

    def test_sync_existing_name_changes_only_the_panel_label(self):
        self.setup_profiles()
        suggestion = dict(first_name='Original', last_name='Name', about='')
        with patch('web.routes.bulk_profile.generate_profiles', new=AsyncMock(return_value=[suggestion])):
            plan = self.preview(account_ids=[self.account_id], name_mode='ai', name_prompt='Names', about_mode='keep', sync_labels=True).json
        self.assertEqual(plan['rows'][0]['status'], 'pending')
        self.assertEqual(self.apply(plan).json['status'], 'success')
        self.assertEqual(db.session.get(Account, self.account_id).name, 'Original Name')
        self.assertFalse(self.writes(self.first))

    def test_client_cannot_replace_previewed_changes(self):
        self.setup_profiles()
        plan = self.preview(account_ids=[self.account_id]).json
        self.apply(plan, changes={'first_name': 'Unreviewed', 'about': 'Other text'})
        self.assertEqual(self.first.about, '')
        self.assertEqual(self.first.first_name, 'Original')

    def test_panel_save_error_does_not_repeat_telegram_write(self):
        self.setup_profiles()
        with patch('web.routes.bulk_profile.generate_profiles', new=AsyncMock(return_value=[dict(first_name='New', last_name='', about='')])):
            plan = self.preview(account_ids=[self.account_id], name_mode='ai', name_prompt='Names', about_mode='keep', sync_labels=True).json
        with patch.object(db.session, 'commit', side_effect=RuntimeError('test database failure')):
            response = self.apply(plan)
        self.assertEqual(response.json['status'], 'success')
        self.assertIn('не обновилось', response.json['message'])
        self.assertEqual(self.apply(plan).json['status'], 'success')
        self.assertEqual(len(self.writes(self.first)), 1)

    def test_replaced_account_is_not_overwritten(self):
        self.setup_profiles()
        plan = self.preview(account_ids=[self.account_id]).json
        account = db.session.get(Account, self.account_id)
        account.phone = '+30000000000'
        db.session.commit()
        self.assertEqual(self.apply(plan).json['status'], 'error')
        self.assertFalse(self.writes(self.first))

    def test_offline_is_individual_error(self):
        self.setup_profiles()
        self.manager.clients.pop(self.account_id)
        plan = self.preview().json
        self.assertEqual([row['status'] for row in plan['rows']], ['error', 'pending'])
        self.assertEqual(self.apply(plan, self.second_id).json['status'], 'success')
        self.app.telegram_manager = None
        plan = self.preview().json
        self.assertTrue(all(row['status'] == 'error' for row in plan['rows']))

    def test_timeout_after_write_is_uncertain_and_never_retried(self):
        self.setup_profiles()
        plan = self.preview(account_ids=[self.account_id]).json
        with patch('web.routes.bulk_profile.apply_changes', new=AsyncMock(side_effect=TimeoutError)) as update:
            row = self.app.extensions['bulk_profiles']['plans'][plan['plan_id']]['rows'][self.account_id]
            async def ambiguous(*args):
                row['write_started'] = True
                raise TimeoutError()
            update.side_effect = ambiguous
            self.assertEqual(self.apply(plan).json['status'], 'uncertain')
            self.assertEqual(self.apply(plan).json['status'], 'uncertain')
            self.assertEqual(update.await_count, 1)

    def test_flood_wait_is_not_slept_or_retried(self):
        self.setup_profiles()
        plan = self.preview(account_ids=[self.account_id]).json
        with patch('web.routes.bulk_profile.apply_changes', new=AsyncMock(side_effect=errors.FloodWaitError(request=None, capture=90))) as update:
            response = self.apply(plan)
            self.assertEqual(response.json['status'], 'error')
            self.assertIn('90', response.json['message'])
            self.apply(plan)
            self.assertEqual(update.await_count, 1)

    def test_expiry_cross_session_and_csrf(self):
        self.setup_profiles()
        plan = self.preview().json
        outsider = self.app.test_client()
        outsider.get('/accounts/bulk-profile')
        with outsider.session_transaction() as state:
            token = state['bulk_profile_csrf']
        self.assertEqual(outsider.post(f'/accounts/bulk-profile/{plan["plan_id"]}/{self.account_id}/apply', json={'csrf_token': token}).status_code, 404)
        self.assertEqual(self.apply(plan, csrf_token='wrong').status_code, 400)
        self.assertEqual(self.apply(plan, csrf_token='неверно').status_code, 400)
        self.app.extensions['bulk_profiles']['plans'][plan['plan_id']]['expires'] = 0
        self.assertEqual(self.apply(plan).status_code, 400)
        self.assertFalse(self.writes(self.first))

    def test_bad_inputs_do_not_write(self):
        self.setup_profiles()
        invalid = [dict(account_ids=[]), dict(account_ids=[99999]), dict(account_ids=[True]),
                   dict(account_ids=[self.account_id] * 51), dict(name_mode='bad'),
                   dict(about_mode='keep'), dict(name_mode='ai'), dict(about_mode='ai'),
                   dict(about_mode='text', about_text=''), dict(about_mode='text', about_url='javascript:alert(1)'),
                   dict(name_prompt='a' * 4001)]
        for data in invalid:
            self.assertEqual(self.preview(**data).status_code, 400, data)
        self.assertFalse(self.writes(self.first))

    def test_account_deleted_or_busy_after_preview(self):
        self.setup_profiles()
        plan = self.preview().json
        store = self.app.extensions['bulk_profiles']
        store['active'].add(self.account_id)
        self.assertEqual(self.apply(plan).status_code, 409)
        store['active'].clear()
        db.session.delete(db.session.get(Account, self.account_id))
        db.session.commit()
        self.assertEqual(self.apply(plan).json['status'], 'error')
        self.assertFalse(self.writes(self.first))


class ProfileGenerationTests(unittest.TestCase):
    def generate(self, profiles, status='completed', **kwargs):
        app = SimpleNamespace(config={'OPENAI_API_KEY': 'test-only', 'OPENAI_MODEL': 'gpt-4.1-mini'})
        client = AsyncMock()
        client.responses.create.return_value = SimpleNamespace(status=status, output_text=json.dumps({'profiles': profiles}))
        factory = patch('profile_generation.AsyncOpenAI')
        with factory as constructor:
            constructor.return_value.__aenter__ = AsyncMock(return_value=client)
            constructor.return_value.__aexit__ = AsyncMock(return_value=False)
            result = asyncio.run(generate_profiles(app, kwargs.pop('count', 1), **kwargs))
        return result, client.responses.create.call_args.kwargs

    def test_schema_and_payload_only_contain_prompts(self):
        result, request = self.generate([dict(first_name='Abebe', last_name='Kebede', about='')], name_prompt='Country-specific fictional names')
        self.assertEqual(result[0]['first_name'], 'Abebe')
        self.assertFalse(request['store'])
        self.assertTrue(request['text']['format']['strict'])
        self.assertEqual(set(json.loads(request['input'])), {'count', 'name_prompt', 'about_prompt'})
        self.assertNotIn('about', json.loads(request['input']))

    def test_incomplete_invalid_or_repeated_suggestions_are_rejected(self):
        good = dict(first_name='A', last_name='B', about='')
        cases = [(dict(profiles=[good], status='incomplete'), 'name_prompt'),
                 (dict(profiles=[]), 'name_prompt'),
                 (dict(profiles=[{'first_name': 'A'}]), 'name_prompt'),
                 (dict(profiles=[dict(good, first_name='')]), 'name_prompt'),
                 (dict(profiles=[dict(good, first_name='x' * 65)]), 'name_prompt'),
                 (dict(profiles=[good, good], count=2), 'name_prompt'),
                 (dict(profiles=[dict(good, about='x' * 71)]), 'about_prompt')]
        for data, prompt in cases:
            with self.assertRaises(ValueError):
                self.generate(**data, **{prompt: 'Test prompt'})

    def test_key_is_required(self):
        with self.assertRaisesRegex(ValueError, 'OPENAI_API_KEY'):
            asyncio.run(generate_profiles(SimpleNamespace(config={}), 1, name_prompt='Names'))
