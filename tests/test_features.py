import asyncio
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
import re
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zipfile import ZipFile

from PIL import Image
from config import Config
from media import send_content
from models import Account, Keyword, PendingReply, ScheduledTask, TaskImage, db, set_setting
from message_handler import check_keywords, _smart_dedup
from telegram_manager import TelegramManager
from tdata_import import extract_tdata, import_tdata
from time_parser import parse_chinese_time
from web.app import create_app


def picture(color='red'):
    stream = BytesIO()
    Image.new('RGB', (20, 20), color).save(stream, 'PNG')
    stream.seek(0)
    return stream, 'image.png'


def archive(entries):
    stream = BytesIO()
    with ZipFile(stream, 'w') as target:
        for name, content in entries.items():
            target.writestr(name, content)
    return stream.getvalue()


class FeaturesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = patch.object(Config, 'DATABASE_URL', 'sqlite:///:memory:')
        self.config.start()
        self.manager = TelegramManager()
        self.app = create_app(self.manager)
        self.app.instance_path = self.temp.name
        self.app.config['TESTING'] = True
        self.manager.app = self.app
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        account = Account(name='Тест', phone='+10000000000', api_id=1, api_hash='test', status='authorized')
        db.session.add(account)
        db.session.commit()
        self.account_id = account.id

    def tearDown(self):
        if self.manager.scheduler and self.manager.scheduler.running:
            self.manager.scheduler.shutdown(wait=False)
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        self.config.stop()
        self.temp.cleanup()

    def task(self, **overrides):
        data = dict(account_id=str(self.account_id), group_id='123', message='Подпись', task_type='interval', interval_minutes='5')
        data.update(overrides)
        # Routes notify a background manager; these form tests run without one.
        self.app.telegram_manager = None
        return self.client.post('/tasks/add', data=data)

    def test_image_create_edit_remove_and_no_caption(self):
        self.assertEqual(self.task(image=picture(), message='').status_code, 302)
        task = ScheduledTask.query.one()
        image = task.image
        self.assertTrue((Path(self.temp.name) / 'uploads' / image).is_file())
        with self.client.get('/media/' + image) as response:
            self.assertEqual(response.status_code, 200)
        fields = dict(account_id=str(self.account_id), group_id='123', message='Новая подпись', task_type='interval', interval_minutes='5')
        self.client.post(f'/tasks/{task.id}/edit', data=fields)
        self.assertEqual(task.image, image)
        self.client.post(f'/tasks/{task.id}/edit', data={**fields, 'image': picture()})
        self.assertNotEqual(task.image, image)
        self.assertTrue((Path(self.temp.name) / 'uploads' / image).exists())
        self.client.post(f'/tasks/{task.id}/edit', data={**fields, 'remove_image': 'on'})
        self.assertIsNone(task.image)

    def test_invalid_images_and_caption_rejected(self):
        for data in [dict(image=(BytesIO(b'not a picture'), 'fake.jpg')),
                     dict(image=picture(), message='a' * 1025),
                     dict(image=(BytesIO(b'x' * (10 * 1024 * 1024 + 1)), 'huge.jpg'))]:
            self.task(**data)
            self.assertEqual(ScheduledTask.query.count(), 0)

    def test_empty_message_removal_rejected(self):
        self.task(image=picture(), message='')
        task = ScheduledTask.query.one()
        self.client.post(f'/tasks/{task.id}/edit', data=dict(account_id=str(self.account_id), group_id='123', message='', remove_image='on'))
        self.assertIsNotNone(task.image)

    def test_keyword_image_form(self):
        response = self.client.post('/keywords/add', data={'keywords[]': 'привет', 'reply_message': '', 'image': picture()})
        self.assertEqual(response.status_code, 302)
        rule = Keyword.query.one()
        self.assertIsNotNone(rule.image)
        self.client.post(f'/keywords/{rule.id}/edit', data={'keywords[]': 'привет', 'reply_message': 'Ответ', 'remove_image': 'on'})
        self.assertIsNone(rule.image)

    def test_send_image_text_and_topic(self):
        client = SimpleNamespace(send_file=AsyncMock(), send_message=AsyncMock())
        asyncio.run(send_content(client, 123, 'Текст', 'test.jpg', reply_to=7))
        client.send_file.assert_awaited_once_with(123, str(Path(self.temp.name) / 'uploads/test.jpg'), caption='Текст', reply_to=7)
        asyncio.run(send_content(client, 123, 'Текст'))
        client.send_message.assert_awaited_once_with(123, 'Текст')

    def test_immediate_and_delayed_keyword_images(self):
        client = SimpleNamespace(send_file=AsyncMock(), send_message=AsyncMock())
        event = SimpleNamespace(chat_id=123, get_chat=AsyncMock(return_value=SimpleNamespace(id=123, title='Чат')))
        rule = Keyword(keyword='привет', reply_message='Ответ', image='test.jpg', trigger_mode='all_messages', topic_id=7)
        db.session.add(rule)
        db.session.commit()
        message = SimpleNamespace(text='Привет', sender_id=123)
        asyncio.run(check_keywords(self.manager, self.account_id, client, event, message, None))
        client.send_file.assert_awaited_once()
        rule = db.session.get(Keyword, rule.id)
        rule.use_random_time = True
        db.session.commit()
        asyncio.run(check_keywords(self.manager, self.account_id, client, event, message, None))
        pending = PendingReply.query.one()
        self.assertEqual((pending.image, pending.topic_id), ('test.jpg', 7))

    def test_pending_retry_keeps_image(self):
        client = SimpleNamespace(send_file=AsyncMock(side_effect=RuntimeError('offline')), send_message=AsyncMock())
        self.manager.clients[self.account_id] = client
        pending = PendingReply(account_id=self.account_id, group_id='123', message='Текст', image='test.jpg', scheduled_at=datetime.utcnow() - timedelta(seconds=1))
        db.session.add(pending)
        db.session.commit()
        pending_id = pending.id
        asyncio.run(self.manager._check_pending_replies())
        db.session.expire_all()
        pending = db.session.get(PendingReply, pending_id)
        self.assertFalse(pending.is_sent)
        self.assertEqual(pending.image, 'test.jpg')
        self.assertGreater(pending.scheduled_at, datetime.utcnow())
        client.send_file.side_effect = None
        pending.scheduled_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        asyncio.run(self.manager._check_pending_replies())
        db.session.expire_all()
        self.assertTrue(db.session.get(PendingReply, pending_id).is_sent)

    def test_scheduled_image(self):
        client = SimpleNamespace(send_file=AsyncMock(), send_message=AsyncMock())
        self.manager.clients[self.account_id] = client
        asyncio.run(self.manager._send_scheduled_message(self.account_id, '123', 'Подпись', topic_id=9, image='test.jpg'))
        self.assertEqual(client.send_file.await_args.kwargs['reply_to'], 9)

    def start_test_scheduler(self):
        from apscheduler.schedulers.background import BackgroundScheduler
        self.manager.scheduler = BackgroundScheduler(timezone='UTC')
        self.manager.scheduler.start(paused=True)
        self.manager.clients[self.account_id] = SimpleNamespace(send_file=AsyncMock(), send_message=AsyncMock())

    def test_interval_edit_replaces_trigger_and_immediate_start(self):
        self.task(send_immediately='on')
        task = ScheduledTask.query.one()
        self.start_test_scheduler()
        asyncio.run(self.manager._reload_scheduled_tasks())
        job = self.manager.scheduler.get_job(f'task_{task.id}')
        self.assertEqual(job.trigger.interval.total_seconds(), 300)
        self.assertLess(abs(job.next_run_time.timestamp() - datetime.now().timestamp()), 3)
        db.session.expire_all()
        self.client.post(f'/tasks/{task.id}/edit', data=dict(account_id=str(self.account_id), group_id='123', message='test', task_type='interval', interval_minutes='1'))
        asyncio.run(self.manager._reload_scheduled_tasks())
        job = self.manager.scheduler.get_job(f'task_{task.id}')
        self.assertEqual(job.trigger.interval.total_seconds(), 60)
        self.assertGreater(job.next_run_time.timestamp() - datetime.now().timestamp(), 55)

    def test_pool_consumed_once_and_stops(self):
        from task_scheduler import execute_task
        self.task()
        task = ScheduledTask.query.one()
        task.images.extend([TaskImage(filename=name, digest=name) for name in ('a.jpg', 'b.jpg')])
        db.session.commit()
        task_id, revision = task.id, task.revision
        self.start_test_scheduler()
        with patch('task_scheduler.send_content', new_callable=AsyncMock) as send:
            for _ in range(3):
                asyncio.run(execute_task(self.manager, task_id, revision))
            self.assertEqual([call.args[3] for call in send.await_args_list], ['a.jpg', 'b.jpg'])
        db.session.expire_all()
        task = db.session.get(ScheduledTask, task_id)
        self.assertFalse(task.is_active)
        self.assertIsNotNone(task.completed_at)
        self.assertEqual([item.state for item in task.images], ['sent', 'sent'])

    def test_once_delivery_and_uncertain_failure_do_not_repeat(self):
        from task_scheduler import execute_task
        self.task(task_type='once')
        task = ScheduledTask.query.one()
        task_id, revision = task.id, task.revision
        self.start_test_scheduler()
        asyncio.run(self.manager._reload_scheduled_tasks())
        from apscheduler.triggers.date import DateTrigger
        self.assertIsInstance(self.manager.scheduler.get_job(f'task_{task_id}').trigger, DateTrigger)
        with patch('task_scheduler.send_content', new_callable=AsyncMock) as send:
            asyncio.run(execute_task(self.manager, task_id, revision))
            asyncio.run(execute_task(self.manager, task_id, revision))
            send.assert_awaited_once()
        db.session.expire_all()
        self.assertFalse(db.session.get(ScheduledTask, task_id).is_active)
        self.task(task_type='once')
        task = ScheduledTask.query.order_by(ScheduledTask.id.desc()).first()
        task_id, revision = task.id, task.revision
        with patch('task_scheduler.send_content', new_callable=AsyncMock, side_effect=RuntimeError('offline')) as send:
            with self.assertRaises(RuntimeError):
                asyncio.run(execute_task(self.manager, task_id, revision))
            asyncio.run(execute_task(self.manager, task_id, revision))
            send.assert_awaited_once()
        db.session.expire_all()
        self.assertEqual(db.session.get(ScheduledTask, task_id).delivery_state, 'uncertain')

    def test_pool_upload_dedup_and_interrupted_restart(self):
        self.task(pool_images=[picture(), picture()], message='')
        task = ScheduledTask.query.one()
        self.assertEqual(len(task.images), 1)
        self.assertEqual(self.client.get(f'/tasks/{task.id}/edit').status_code, 200)
        task.delivery_state = 'sending'
        task.images[0].state = 'sending'
        db.session.commit()
        self.start_test_scheduler()
        asyncio.run(self.manager._reload_scheduled_tasks())
        db.session.expire_all()
        self.assertFalse(task.is_active)
        self.assertEqual(task.images[0].state, 'uncertain')
        self.assertIsNone(self.manager.scheduler.get_job(f'task_{task.id}'))

    def test_folder_pool_natural_order_and_non_images(self):
        red, _ = picture()
        blue, _ = picture('blue')
        self.task(pool_folder=[(red, 'folder/screen10.png'), (BytesIO(b'text'), 'folder/readme.txt'),
                               (blue, 'folder/screen2.png')])
        task = ScheduledTask.query.one()
        self.assertEqual(len(task.images), 2)
        with Image.open(Path(self.temp.name) / 'uploads' / task.images[0].filename) as first:
            self.assertGreater(first.getpixel((0, 0))[2], 200)

    def test_ai_form_validation_and_long_source(self):
        self.app.config['OPENAI_API_KEY'] = ''
        self.task(caption_mode='ai', image=picture())
        self.assertEqual(ScheduledTask.query.count(), 0)
        self.app.config['OPENAI_API_KEY'] = 'fake-test-key'
        self.task(caption_mode='ai', message='Основа ' * 400, pool_images=[picture()], caption_language='am')
        task = ScheduledTask.query.one()
        self.assertEqual(task.caption_language, 'am')
        self.assertEqual(task.caption_mode, 'ai')
        self.assertEqual(self.client.get(f'/tasks/{task.id}/edit').status_code, 200)
        task.images[0].caption = 'Старый пример'
        db.session.commit()
        self.client.post(f'/tasks/{task.id}/edit', data=dict(account_id=str(self.account_id), group_id='123',
            message='Новая основа', task_type='interval', interval_minutes='120', caption_mode='ai',
            caption_language='other', caption_custom_language='Italian', caption_max_chars='200'))
        self.assertEqual(task.caption_language, 'Italian')
        self.assertIsNone(task.images[0].caption)

    def test_ai_caption_pool_delivery_and_history(self):
        from task_scheduler import execute_task
        self.app.config['OPENAI_API_KEY'] = 'fake-test-key'
        self.task(caption_mode='ai', caption_language='en', caption_use_image='on', interval_minutes='60',
                  pool_images=[picture(), picture('blue')])
        task = ScheduledTask.query.one()
        task_id, revision = task.id, task.revision
        self.start_test_scheduler()
        with patch('task_scheduler.generate_caption', new_callable=AsyncMock, side_effect=['First caption', 'Second caption']) as generate, patch('task_scheduler.send_content', new_callable=AsyncMock) as send:
            asyncio.run(execute_task(self.manager, task_id, revision))
            asyncio.run(execute_task(self.manager, task_id, revision))
            self.assertEqual([call.args[2] for call in send.await_args_list], ['First caption', 'Second caption'])
            self.assertEqual(generate.await_args_list[1].kwargs['previous'], ['First caption'])
            self.assertIsNotNone(generate.await_args_list[0].kwargs['image'])
        db.session.expire_all()
        task = db.session.get(ScheduledTask, task_id)
        self.assertFalse(task.is_active)
        self.assertEqual([item.caption for item in task.images], ['First caption', 'Second caption'])

    def test_ai_failure_and_restart_preserve_pending_image(self):
        from captions import CaptionError
        from task_scheduler import execute_task
        self.app.config['OPENAI_API_KEY'] = 'fake-test-key'
        self.task(caption_mode='ai', pool_images=[picture()])
        task = ScheduledTask.query.one()
        task_id, revision = task.id, task.revision
        self.start_test_scheduler()
        with patch('task_scheduler.generate_caption', new_callable=AsyncMock, side_effect=CaptionError('Нет баланса')), patch('task_scheduler.send_content', new_callable=AsyncMock) as send:
            with self.assertRaises(CaptionError):
                asyncio.run(execute_task(self.manager, task_id, revision))
            send.assert_not_awaited()
        db.session.expire_all()
        task = db.session.get(ScheduledTask, task_id)
        self.assertEqual(task.delivery_state, 'ready')
        self.assertFalse(task.is_active)
        self.assertEqual(task.images[0].state, 'pending')
        task.is_active, task.delivery_state = True, 'generating'
        db.session.commit()
        asyncio.run(self.manager._reload_scheduled_tasks())
        db.session.expire_all()
        self.assertEqual(task.delivery_state, 'ready')
        self.assertFalse(task.is_active)
        self.assertEqual(task.images[0].state, 'pending')

    def test_pausing_during_caption_prevents_delivery(self):
        from task_scheduler import execute_task
        self.app.config['OPENAI_API_KEY'] = 'fake-test-key'
        self.task(caption_mode='ai', pool_images=[picture()])
        task = ScheduledTask.query.one()
        task_id, revision = task.id, task.revision
        self.start_test_scheduler()
        async def pause(*args, **kwargs):
            with self.app.app_context():
                row = db.session.get(ScheduledTask, task_id)
                row.is_active = False
                row.revision += 1
                db.session.commit()
            return 'Generated caption'
        with patch('task_scheduler.generate_caption', side_effect=pause), patch('task_scheduler.send_content', new_callable=AsyncMock) as send:
            asyncio.run(execute_task(self.manager, task_id, revision))
            send.assert_not_awaited()
        db.session.expire_all()
        self.assertEqual(db.session.get(ScheduledTask, task_id).images[0].state, 'pending')

    def test_caption_api_payload_and_duplicate_retries(self):
        from captions import generate_caption
        self.app.config['OPENAI_API_KEY'] = 'fake-test-key'
        self.task(pool_images=[picture()])
        name = ScheduledTask.query.one().images[0].filename
        responses = [SimpleNamespace(output_text=value, status='completed') for value in ('ПРИВЕТ!', 'x' * 301, 'Новая формулировка')]
        api = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(side_effect=responses)))
        context = AsyncMock()
        context.__aenter__.return_value = api
        with patch('captions.AsyncOpenAI', return_value=context):
            result = asyncio.run(generate_caption(self.app, 'Привет', 'en', image=name, previous=['Привет']))
        self.assertEqual(result, 'Новая формулировка')
        self.assertEqual(api.responses.create.await_count, 3)
        payload = api.responses.create.await_args.kwargs
        self.assertFalse(payload['store'])
        self.assertEqual(payload['input'][0]['content'][1]['type'], 'input_image')
        self.assertTrue(payload['input'][0]['content'][1]['image_url'].startswith('data:image/jpeg;base64,'))

    def test_caption_preview_csrf_and_no_telegram_send(self):
        self.app.config['OPENAI_API_KEY'] = 'fake-test-key'
        self.client.get('/tasks/')
        with self.client.session_transaction() as state:
            token = state['caption_csrf']
        self.assertEqual(self.client.post('/tasks/caption-preview', json=dict(message='Основа')).status_code, 400)
        with patch('web.routes.tasks.generate_caption', new_callable=AsyncMock, return_value='Пример'):
            result = self.client.post('/tasks/caption-preview', json=dict(message='Основа', caption_language='am', csrf_token=token))
        self.assertEqual(result.json['caption'], 'Пример')
        self.assertEqual(ScheduledTask.query.count(), 0)

    def test_dedup_preserves_different_images(self):
        set_setting('smart_dedup_enabled', 'true')
        for image in ('a.jpg', 'b.jpg', 'a.jpg'):
            db.session.add(PendingReply(account_id=self.account_id, group_id='123', message='Текст', image=image, scheduled_at=datetime.utcnow()))
        db.session.commit()
        _smart_dedup(self.account_id, '123', 'Текст', None, 'a.jpg')
        self.assertEqual(PendingReply.query.count(), 2)

    def test_archive_validation(self):
        for entries in ({'../key_datas': b'x'}, {'C:/key_datas': b'x'}, {'tdata/file.txt': b'x'}, {'one/key_datas': b'x', 'two/key_datas': b'x'}):
            with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
                extract_tdata(archive(entries), directory)
        with tempfile.TemporaryDirectory() as directory:
            path = extract_tdata(archive({'tdata/key_datas': b'key', 'tdata/cache/photo': b'private'}), directory)
            self.assertEqual((path / 'key_datas').read_bytes(), b'key')
            self.assertFalse((path / 'cache').exists())

    def test_import_multiple_accounts_and_duplicates(self):
        from opentele2.api import API
        clients = [SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock(), is_user_authorized=AsyncMock(return_value=True), get_me=AsyncMock(return_value=SimpleNamespace(phone=phone, first_name='Тест')), session=object()) for phone in ('10000000000', '20000000000')]
        desktop = SimpleNamespace(isLoaded=lambda: True, accounts=[object(), object()])
        self.manager.reload_account = AsyncMock()
        with patch('opentele2.td.TDesktop', return_value=desktop), patch('opentele2.tl.TelegramClient.FromTDesktop', new=AsyncMock(side_effect=clients)), patch('tdata_import.StringSession.save', return_value='session'):
            count, skipped = asyncio.run(import_tdata(self.app, archive({'tdata/key_datas': b'key'})))
        self.assertEqual((count, skipped), (1, 1))
        added = Account.query.filter_by(phone='+20000000000').one()
        self.assertEqual((added.session_kind, added.api_id), ('tdesktop', API.TelegramDesktop.api_id))
        for client in clients:
            client.disconnect.assert_awaited_once()

    def test_pages_render_in_russian(self):
        for path in ('/', '/accounts/', '/keywords/', '/tasks/', '/targets/', '/whitelist/', '/logs/', '/settings/', '/queue/', f'/accounts/{self.account_id}/auth'):
            result = self.client.get(path)
            self.assertEqual(result.status_code, 200, path)
            text = re.sub(r'<!--.*?-->|//[^\n]*', '', result.get_data(as_text=True), flags=re.S)
            self.assertIsNone(re.search(r'[\u3400-\u9fff]', text), path)

    def test_russian_time(self):
        self.assertEqual(parse_chinese_time('через 2 часа 30 минут 10 секунд'), 9010)
        self.assertEqual(parse_chinese_time('1 д 2 ч 3 мин'), 93780)
        self.assertEqual(parse_chinese_time('1小时48秒'), 3648)

    def test_join_links(self):
        from web.routes.join import parse_invite
        for link in ('@testchat', 'https://t.me/testchat', 't.me/testchat'):
            self.assertEqual(parse_invite(link), ('public', 'testchat'))
        for link in ('https://t.me/+Abc_123', 't.me/joinchat/Abc_123'):
            self.assertEqual(parse_invite(link), ('invite', 'Abc_123'))
        for link in ('-100123456', 'https://evil.example/testchat', 'https://t.me/chatname/123', 'https://t.me/+'):
            with self.assertRaises(ValueError):
                parse_invite(link)

    def test_join_public_and_private(self):
        from web.routes.join import join_chat
        from telethon import types, functions
        chat = types.Channel(id=123, title='Чат', photo=types.ChatPhotoEmpty(), date=datetime.now(), megagroup=True, left=True)
        client = AsyncMock()
        client.get_entity.return_value = chat
        result = asyncio.run(join_chat(client, 'public', 'testchat'))
        self.assertEqual(result['chat_id'], '-1000000000123')
        self.assertIsInstance(client.call_args.args[0], functions.channels.JoinChannelRequest)
        chat.left = False
        client.reset_mock()
        self.assertEqual(asyncio.run(join_chat(client, 'public', 'testchat'))['status'], 'already')
        client.assert_not_awaited()
        client.return_value = types.ChatInviteAlready(chat)
        self.assertEqual(asyncio.run(join_chat(client, 'invite', 'code'))['status'], 'already')

    def test_join_pending_and_limits(self):
        from web.routes.join import join_chat, join_error
        from telethon import errors
        client = AsyncMock(side_effect=errors.InviteRequestSentError(request=None))
        self.assertEqual(asyncio.run(join_chat(client, 'invite', 'code'))['status'], 'pending')
        self.assertIn('42', join_error(errors.FloodWaitError(request=None, capture=42)))

    def test_join_route_csrf_offline_and_saved_target(self):
        from models import TargetEntity
        url = f'/accounts/{self.account_id}/join'
        self.assertEqual(self.client.post(url, json={'link':'@testchat'}).status_code, 400)
        self.assertEqual(self.client.get('/accounts/join').status_code, 200)
        with self.client.session_transaction() as state:
            token = state['join_csrf']
        payload = dict(link='@testchat', csrf_token=token)
        self.assertEqual(self.client.post(url, json=payload).json['status'], 'error')
        def execute(coro):
            coro.close()
            return dict(status='joined', message='Вступил', chat_id='-100123', title='Чат', kind='supergroup')
        with patch('web.routes.join.connected_client'), patch('web.routes.join.run', side_effect=execute):
            self.assertEqual(self.client.post(url, json=payload).json['status'], 'joined')
        self.assertEqual(TargetEntity.query.one().entity_id, '-100123')

    def test_profile_updates_and_validation(self):
        from web.routes.profile import update
        from telethon import functions
        full = SimpleNamespace(full_user=SimpleNamespace(id=1, about='Описание'), users=[SimpleNamespace(id=1, first_name='Имя', last_name='', username='oldname', photo=None, premium=False)])
        client = AsyncMock(return_value=full)
        asyncio.run(update(client, 'details', dict(first_name='Новое', last_name='', about='')))
        rpc = client.call_args.args[0]
        self.assertIsInstance(rpc, functions.account.UpdateProfileRequest)
        self.assertEqual((rpc.first_name, rpc.last_name, rpc.about), ('Новое', '', ''))
        asyncio.run(update(client, 'username', dict(username='@newname')))
        self.assertEqual(client.call_args.args[0].username, 'newname')
        asyncio.run(update(client, 'username', dict(username='')))
        self.assertEqual(client.call_args.args[0].username, '')
        for action, data in [('details', dict(first_name='')), ('details', dict(first_name='Имя', about='x'*71)), ('username', dict(username='bad!'))]:
            with self.assertRaises(ValueError):
                asyncio.run(update(client, action, data))

    def test_profile_photo_validation_and_upload(self):
        from web.routes.profile import prepare_photo, update
        from werkzeug.datastructures import FileStorage
        from telethon import functions
        stream, filename = picture()
        data = prepare_photo(FileStorage(stream=stream, filename=filename))
        self.assertTrue(data.startswith(b'\xff\xd8'))
        with self.assertRaises(ValueError):
            prepare_photo(FileStorage(stream=BytesIO(b'bad'), filename='bad.jpg'))
        client = AsyncMock()
        asyncio.run(update(client, 'photo', {}, data))
        client.upload_file.assert_awaited_once()
        self.assertIsInstance(client.call_args.args[0], functions.photos.UploadProfilePhotoRequest)
        client.get_profile_photos.return_value = []
        client.reset_mock()
        asyncio.run(update(client, 'delete_photo', {}))
        client.assert_not_awaited()

    def test_profile_page_offline_label_and_csrf(self):
        response = self.client.get(f'/accounts/{self.account_id}/profile')
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as state:
            token = state['profile_csrf']
        response = self.client.post(f'/accounts/{self.account_id}/profile', data=dict(action='label', name='Новое'))
        self.assertEqual(response.status_code, 400)
        response = self.client.post(f'/accounts/{self.account_id}/profile', data=dict(action='label', name='Новое', csrf_token=token))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.session.get(Account, self.account_id).name, 'Новое')

    def test_profile_online_page_and_occupied_username(self):
        from telethon.errors import UsernameOccupiedError
        from web.routes.profile import error_message
        profile = dict(first_name='Тест', last_name='', username='testname', about='', has_photo=False, about_limit=70)
        def execute(coro):
            coro.close()
            return profile
        with patch('web.routes.profile.connected_client'), patch('web.routes.profile.run', side_effect=execute):
            response = self.client.get(f'/accounts/{self.account_id}/profile')
        self.assertEqual(response.status_code, 200)
        self.assertIn('testname', response.get_data(as_text=True))
        self.assertEqual(error_message(UsernameOccupiedError(request=None)), 'Этот @username уже занят')

    def test_migration_preserves_existing_data_and_is_repeatable(self):
        import sqlalchemy as sa
        database = Path(self.temp.name) / 'legacy.db'
        with patch.object(Config, 'DATABASE_URL', 'sqlite:///' + database.as_posix()):
            legacy = create_app()
            with legacy.app_context():
                db.session.add(Account(name='Существующий', phone='+333', api_id=1, api_hash='hash'))
                db.session.commit()
                for table in ('keywords', 'scheduled_tasks', 'pending_replies'):
                    db.session.execute(sa.text(f'ALTER TABLE {table} DROP COLUMN image'))
                db.session.execute(sa.text('ALTER TABLE accounts DROP COLUMN session_kind'))
                for column in ('once_at', 'send_immediately', 'revision', 'delivery_state', 'completed_at', 'last_error'):
                    db.session.execute(sa.text(f'ALTER TABLE scheduled_tasks DROP COLUMN {column}'))
                for column in ('caption_mode', 'caption_language', 'caption_max_chars', 'caption_use_image', 'account_mode', 'last_account_id'):
                    db.session.execute(sa.text(f'ALTER TABLE scheduled_tasks DROP COLUMN {column}'))
                db.session.execute(sa.text('ALTER TABLE task_images DROP COLUMN caption'))
                db.session.execute(sa.text('ALTER TABLE task_images DROP COLUMN sent_by_account_id'))
                db.session.commit()
                db.engine.dispose()
            for _ in range(2):
                migrated = create_app()
                with migrated.app_context():
                    self.assertEqual(Account.query.one().name, 'Существующий')
                    self.assertEqual(Account.query.one().session_kind, 'telethon')
                    for table in ('keywords', 'scheduled_tasks', 'pending_replies'):
                        self.assertIn('image', {column['name'] for column in sa.inspect(db.engine).get_columns(table)})
                    self.assertTrue({'caption_mode', 'caption_language', 'caption_max_chars', 'caption_use_image', 'account_mode', 'last_account_id'} <=
                                    {column['name'] for column in sa.inspect(db.engine).get_columns('scheduled_tasks')})
                    self.assertIn('caption', {column['name'] for column in sa.inspect(db.engine).get_columns('task_images')})
                    self.assertIn('sent_by_account_id', {column['name'] for column in sa.inspect(db.engine).get_columns('task_images')})
                    db.session.remove()
                    db.engine.dispose()

    def test_import_failure_disconnects_and_does_not_save(self):
        desktop = SimpleNamespace(isLoaded=lambda: True, accounts=[object()])
        client = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock(), is_user_authorized=AsyncMock(return_value=False))
        with patch('opentele2.td.TDesktop', return_value=desktop), patch('opentele2.tl.TelegramClient.FromTDesktop', new=AsyncMock(return_value=client)), self.assertRaises(ValueError):
            asyncio.run(import_tdata(self.app, archive({'tdata/key_datas': b'key'})))
        client.disconnect.assert_awaited_once()
        self.assertEqual(Account.query.count(), 1)


if __name__ == '__main__':
    unittest.main()
