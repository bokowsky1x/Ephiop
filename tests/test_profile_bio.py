import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from telethon import functions, types

import test_features as fixtures
from web.routes.profile import read_profile, update


class ProfileClient:
    def __init__(self, about='Existing biography', premium=False):
        self.about = about
        self.premium = premium
        self.first_name = 'Original'
        self.last_name = 'Name'
        self.username = 'originalname'
        self.requests = []

    def is_connected(self):
        return True

    async def __call__(self, request, **kwargs):
        self.requests.append(request)
        user = types.User(id=888, is_self=True, first_name=self.first_name,
                          last_name=self.last_name, username=self.username, premium=self.premium)
        if isinstance(request, functions.users.GetFullUserRequest):
            return SimpleNamespace(full_user=SimpleNamespace(id=888, about=self.about), users=[user])
        if isinstance(request, functions.account.UpdateProfileRequest):
            for field in ('first_name', 'last_name', 'about'):
                value = getattr(request, field)
                if value is not None:
                    setattr(self, field, value)
            return user
        raise AssertionError('Unexpected Telegram request')


class ProfileBioTests(unittest.TestCase):
    setUp = fixtures.FeaturesTest.setUp
    tearDown = fixtures.FeaturesTest.tearDown

    def install_profile(self, **kwargs):
        client = ProfileClient(**kwargs)
        self.manager.clients[self.account_id] = client
        return client

    def url(self):
        return f'/accounts/{self.account_id}/profile'

    def token(self):
        with self.client.session_transaction() as state:
            return state['profile_csrf']

    def test_read_full_bio_for_current_session(self):
        client = ProfileClient(about='Описание\nብር & <text>', premium=True)
        profile = asyncio.run(read_profile(client))
        self.assertEqual(profile['about'], client.about)
        self.assertEqual(profile['about_limit'], 140)
        self.assertIsInstance(client.requests[-1].id, types.InputUserSelf)

    def test_existing_bio_renders_and_refresh_reads_new_value(self):
        client = self.install_profile(about='Описание\nብር & <text>')
        with patch('web.routes.profile.run', side_effect=asyncio.run):
            response = self.client.get(self.url())
            page = response.get_data(as_text=True)
            self.assertIn('Описание\nብር &amp; &lt;text&gt;</textarea>', page)
            self.assertIn('Сохранить описание', page)
            self.assertIn('Удалить описание', page)
            self.assertIn('Обновить из Telegram', page)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            client.about = 'Changed outside the panel'
            page = self.client.get(self.url()).get_data(as_text=True)
            self.assertIn(client.about + '</textarea>', page)
            self.assertEqual(len(client.requests), 2)

    def test_name_changes_do_not_clear_bio(self):
        client = ProfileClient()
        asyncio.run(update(client, 'details', dict(first_name='New', last_name='')))
        request = client.requests[-1]
        self.assertEqual((request.first_name, request.last_name, request.about), ('New', '', None))
        self.assertEqual(client.about, 'Existing biography')

    def test_replace_and_delete_bio_leave_other_fields_unchanged(self):
        client = self.install_profile()
        with patch('web.routes.profile.run', side_effect=asyncio.run):
            self.client.get(self.url())
            for action, text in (('about', 'New biography'), ('delete_about', '')):
                with self.subTest(action=action):
                    response = self.client.post(self.url(), data=dict(
                        action=action, about=text, csrf_token=self.token()), follow_redirects=True)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(client.about, text)
                    self.assertEqual((client.first_name, client.last_name, client.username),
                                     ('Original', 'Name', 'originalname'))
                    request = next(request for request in reversed(client.requests)
                                   if isinstance(request, functions.account.UpdateProfileRequest))
                    self.assertIsNone(request.first_name)
                    self.assertIsNone(request.last_name)
                    self.assertEqual(request.about, text)
            self.assertNotIn('Удалить описание', response.get_data(as_text=True))

    def test_empty_missing_bio_and_read_failure_are_distinct(self):
        client = self.install_profile(about=None)
        with patch('web.routes.profile.run', side_effect=asyncio.run):
            page = self.client.get(self.url()).get_data(as_text=True)
            self.assertIn('placeholder="Описание не задано"></textarea>', page)
            self.assertNotIn('Удалить описание', page)
        with patch('web.routes.profile.run', side_effect=lambda coroutine: self.fail_read(coroutine)):
            page = self.client.get(self.url()).get_data(as_text=True)
            self.assertIn('Профиль недоступен', page)
            self.assertNotIn('name="about"', page)
        self.assertIsNone(client.about)

    @staticmethod
    def fail_read(coroutine):
        coroutine.close()
        raise ConnectionError('Test connection unavailable')

    def test_bio_length_limits_include_utf16_units(self):
        for premium, limit in ((False, 70), (True, 140)):
            client = ProfileClient(premium=premium)
            asyncio.run(update(client, 'about', dict(about='x' * limit)))
            self.assertEqual(client.about, 'x' * limit)
            for text in ('x' * (limit + 1), '\U0001f600' * (limit // 2 + 1)):
                with self.subTest(premium=premium, length=len(text)):
                    with self.assertRaises(ValueError):
                        asyncio.run(update(client, 'about', dict(about=text)))
                    self.assertEqual(client.about, 'x' * limit)

    def test_bio_error_preserves_draft_and_csrf_protects_deletion(self):
        client = self.install_profile()
        with patch('web.routes.profile.run', side_effect=asyncio.run):
            self.client.get(self.url())
            count = len(client.requests)
            self.assertEqual(self.client.post(self.url(), data=dict(action='delete_about')).status_code, 400)
            self.assertEqual(len(client.requests), count)
            response = self.client.post(self.url(), data=dict(
                action='about', about='x' * 71, csrf_token=self.token()))
            self.assertEqual(response.status_code, 200)
            self.assertIn('x' * 71 + '</textarea>', response.get_data(as_text=True))
            self.assertEqual(client.about, 'Existing biography')

    def test_missing_full_user_never_becomes_blank_bio(self):
        async def client(request):
            return SimpleNamespace(full_user=SimpleNamespace(id=888, about='Bio'),
                                   users=[types.User(id=999)])
        with self.assertRaisesRegex(ValueError, 'Telegram'):
            asyncio.run(read_profile(client))
