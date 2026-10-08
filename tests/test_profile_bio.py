import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telethon import functions, types

import test_features as fixtures
from web.routes.profile import read_profile, update, remove_personal_channel, RemovePersonalChannelRequest


class ProfileClient:
    def __init__(self, about='Existing biography', premium=False, channel_id=None):
        self.about = about
        self.premium = premium
        self.first_name = 'Original'
        self.last_name = 'Name'
        self.username = 'originalname'
        self.requests = []
        self.channel_id = channel_id
        self.channel_title = 'Profile channel'

    def is_connected(self):
        return True

    async def __call__(self, request, **kwargs):
        self.requests.append(request)
        user = types.User(id=888, is_self=True, first_name=self.first_name,
                          last_name=self.last_name, username=self.username, premium=self.premium)
        if isinstance(request, functions.users.GetFullUserRequest):
            chats = [SimpleNamespace(id=self.channel_id, title=self.channel_title, username='profile_channel')] if self.channel_id else []
            return SimpleNamespace(full_user=SimpleNamespace(id=888, about=self.about, personal_channel_id=self.channel_id), users=[user], chats=chats)
        if isinstance(request, functions.account.UpdatePersonalChannelRequest):
            if not isinstance(request.channel, types.InputChannelEmpty):
                raise AssertionError('Only detaching the profile channel is allowed')
            self.channel_id = None
            return True
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

    def test_profile_channel_reads_and_renders_from_full_user(self):
        client = self.install_profile(channel_id=123)
        client.channel_title = 'Channel <test>'
        with patch('web.routes.profile.run', side_effect=asyncio.run):
            page = self.client.get(self.url()).get_data(as_text=True)
        self.assertIn('Channel &lt;test&gt;', page)
        self.assertIn('ID: -100123', page)
        self.assertIn('Убрать канал из профиля', page)
        self.assertTrue(all(isinstance(req, functions.users.GetFullUserRequest) for req in client.requests))

    def test_remove_channel_requires_confirmation_and_preserves_profile(self):
        client = self.install_profile(channel_id=123)
        with patch('web.routes.profile.run', side_effect=asyncio.run):
            self.client.get(self.url())
            data = dict(action='remove_channel', personal_channel_id='123', channel_user_id='888', csrf_token=self.token())
            self.client.post(self.url(), data=data)
            self.assertEqual(client.channel_id, 123)
            response = self.client.post(self.url(), data={**data, 'confirm_remove_channel': 'on'}, follow_redirects=True)
        self.assertIsNone(client.channel_id)
        self.assertEqual((client.first_name, client.last_name, client.username, client.about),
                         ('Original', 'Name', 'originalname', 'Existing biography'))
        self.assertIn('Канал не привязан', response.get_data(as_text=True))
        writes = [req for req in client.requests if not isinstance(req, functions.users.GetFullUserRequest)]
        self.assertEqual(len(writes), 1)
        self.assertIsInstance(writes[0], functions.account.UpdatePersonalChannelRequest)

    def test_remove_channel_rejects_stale_channel_or_session(self):
        client = ProfileClient(channel_id=456)
        data = dict(confirm_remove_channel='on', channel_user_id='888', personal_channel_id='123')
        for values in (data, {**data, 'personal_channel_id': '456', 'channel_user_id': '999'}):
            with self.assertRaises(ValueError):
                asyncio.run(update(client, 'remove_channel', values))
        self.assertEqual(client.channel_id, 456)
        self.assertTrue(all(isinstance(req, functions.users.GetFullUserRequest) for req in client.requests))

    def test_remove_channel_noop_and_false_response_are_distinct(self):
        client = ProfileClient()
        asyncio.run(update(client, 'remove_channel', dict(confirm_remove_channel='on', channel_user_id='888', personal_channel_id='123')))
        self.assertEqual(len(client.requests), 1)
        with self.assertRaisesRegex(ValueError, 'не подтвердил'):
            asyncio.run(remove_personal_channel(AsyncMock(return_value=False)))

    def test_empty_channel_request_serializes_without_peer_lookup(self):
        from telethon import utils
        request = RemovePersonalChannelRequest(types.InputChannelEmpty())
        client = SimpleNamespace(get_input_entity=AsyncMock(side_effect=AssertionError('No peer lookup')))
        asyncio.run(request.resolve(client, utils))
        client.get_input_entity.assert_not_awaited()
        self.assertEqual(bytes(request), bytes(functions.account.UpdatePersonalChannelRequest(types.InputChannelEmpty())))
