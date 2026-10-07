import asyncio
from datetime import datetime
from types import SimpleNamespace
import unittest
from pathlib import Path
import re
import shutil
import subprocess
from unittest.mock import AsyncMock, patch

from telethon import types, utils

import test_features as fixtures
from message_handler import check_keywords
from models import Account, Keyword, PendingReply, ScheduledTask, TaskImage, MessageLog, Whitelist, db
from task_scheduler import execute_task
from telegram_manager import TelegramManager


class RegressionTests(unittest.TestCase):
    setUp = fixtures.FeaturesTest.setUp
    tearDown = fixtures.FeaturesTest.tearDown
    task = fixtures.FeaturesTest.task
    start_test_scheduler = fixtures.FeaturesTest.start_test_scheduler

    def extra_account(self, phone='+20000000000', **options):
        account = Account(name='Second', phone=phone, api_id=1, api_hash='test', status='authorized', **options)
        db.session.add(account)
        db.session.commit()
        return account

    def connect(self, account_id, connected=True):
        client = SimpleNamespace(send_message=AsyncMock(), send_file=AsyncMock(),
                                 is_connected=lambda: connected)
        self.manager.clients[account_id] = client
        return client

    def test_selected_pool_rotates_and_persists_position(self):
        second = self.extra_account()
        self.task(account_mode='selected', account_ids=[str(second.id), str(self.account_id)],
                  pool_images=[fixtures.picture(color) for color in ('red', 'blue', 'green')])
        task = ScheduledTask.query.one()
        task_id, revision = task.id, task.revision
        first_client, second_client = self.connect(self.account_id), self.connect(second.id)
        with patch('task_scheduler.send_content', new_callable=AsyncMock) as send:
            asyncio.run(execute_task(self.manager, task_id, revision))
            db.session.expire_all()
            self.assertEqual(db.session.get(ScheduledTask, task_id).last_account_id, self.account_id)
            restarted = TelegramManager()
            restarted.app, restarted.clients = self.app, self.manager.clients
            self.manager = restarted
            asyncio.run(execute_task(self.manager, task_id, revision))
            asyncio.run(execute_task(self.manager, task_id, revision))
            self.assertEqual([call.args[0] for call in send.await_args_list], [first_client, second_client, first_client])
            self.assertEqual(len({call.args[3] for call in send.await_args_list}), 3)
        db.session.expire_all()
        row = db.session.get(ScheduledTask, task_id)
        self.assertFalse(row.is_active)
        self.assertEqual([item.sent_by_account_id for item in row.images], [self.account_id, second.id, self.account_id])

    def test_all_accounts_and_offline_skip(self):
        second = self.extra_account()
        self.extra_account('+30000000000', is_active=False)
        self.task(account_mode='all', pool_images=[fixtures.picture('red'), fixtures.picture('blue')])
        row = ScheduledTask.query.one()
        self.assertEqual([account.id for account in row.sending_accounts], [self.account_id, second.id])
        self.connect(self.account_id, connected=False)
        second_client = self.connect(second.id)
        with patch('task_scheduler.send_content', new_callable=AsyncMock) as send:
            asyncio.run(execute_task(self.manager, row.id, row.revision))
            self.assertIs(send.await_args.args[0], second_client)

    def test_legacy_task_and_empty_selection(self):
        row = ScheduledTask(account_id=self.account_id, group_id='123', message='old', interval_minutes=5)
        db.session.add(row)
        db.session.commit()
        self.assertEqual([account.id for account in row.sending_accounts], [self.account_id])
        self.task(account_mode='selected', account_ids=[])
        self.assertEqual(ScheduledTask.query.count(), 1)
        self.assertEqual(self.client.get(f'/tasks/{row.id}/edit').status_code, 200)

    def test_delete_account_removes_owned_data_and_preserves_shared_pool(self):
        second = self.extra_account()
        self.task(account_mode='selected', account_ids=[str(self.account_id), str(second.id)], pool_images=[fixtures.picture()])
        shared_id = ScheduledTask.query.one().id
        self.task()
        rule = Keyword(account_id=self.account_id, keyword='test', reply_message='reply')
        db.session.add(rule)
        db.session.flush()
        db.session.add(PendingReply(account_id=self.account_id, keyword_id=rule.id, group_id='123', message='reply', scheduled_at=datetime.utcnow()))
        db.session.add(Whitelist(account_id=self.account_id, entity_id='999'))
        db.session.add(MessageLog(account_id=self.account_id, content='history'))
        db.session.commit()
        self.assertEqual(self.client.post(f'/accounts/{self.account_id}/delete').status_code, 302)
        db.session.expire_all()
        self.assertIsNone(db.session.get(Account, self.account_id))
        self.assertEqual(ScheduledTask.query.count(), 1)
        shared = db.session.get(ScheduledTask, shared_id)
        self.assertEqual(shared.account_id, second.id)
        self.assertEqual([account.id for account in shared.sending_accounts], [second.id])
        self.assertEqual(len(shared.images), 1)
        self.assertEqual((Keyword.query.count(), PendingReply.query.count(), Whitelist.query.count()), (0, 0, 0))
        self.assertIsNone(MessageLog.query.one().account_id)

    def test_delete_during_delivery_is_blocked(self):
        self.task()
        row = ScheduledTask.query.one()
        row.delivery_state = 'sending'
        db.session.commit()
        self.client.post(f'/accounts/{self.account_id}/delete')
        self.assertIsNotNone(db.session.get(Account, self.account_id))

    def test_auto_reply_keeps_channel_id(self):
        chat = types.Channel(id=123, title='Test', photo=types.ChatPhotoEmpty(), date=datetime.now(), megagroup=True)
        client = self.connect(self.account_id)
        db.session.add(Keyword(keyword='hello', reply_message='reply', trigger_mode='all_messages'))
        db.session.commit()
        event = SimpleNamespace(get_chat=AsyncMock(return_value=chat))
        asyncio.run(check_keywords(self.manager, self.account_id, client, event, SimpleNamespace(text='hello', sender_id=987), None))
        self.assertEqual(client.send_message.await_args.args[0], utils.get_peer_id(chat))

    def test_skip_cancels_delayed_execution_without_consuming_image(self):
        self.task(random_delay_min='1', random_delay_max='1', pool_images=[fixtures.picture()])
        row = ScheduledTask.query.one()
        task_id, revision = row.id, row.revision
        self.start_test_scheduler()
        self.app.telegram_manager = self.manager
        async def run():
            await self.manager._reload_scheduled_tasks()
            entered, release = asyncio.Event(), asyncio.Event()
            async def delay(seconds):
                entered.set()
                await release.wait()
            with patch('task_scheduler.asyncio.sleep', side_effect=delay), patch('task_scheduler.send_content', new_callable=AsyncMock) as send:
                worker = asyncio.create_task(execute_task(self.manager, task_id, revision))
                await entered.wait()
                response = self.client.post('/queue/api/delete', json={'items':[{'type':'scheduled_task', 'id':task_id}]})
                self.assertEqual(response.json['deleted'], 1)
                release.set()
                await worker
                send.assert_not_awaited()
        asyncio.run(run())
        db.session.expire_all()
        self.assertEqual(db.session.get(ScheduledTask, task_id).images[0].state, 'pending')
        job = self.manager.scheduler.get_job(f'task_{task_id}')
        self.assertEqual(job.args[2], db.session.get(ScheduledTask, task_id).revision)

    def test_working_hours_hold_tasks_and_pending_replies(self):
        account = db.session.get(Account, self.account_id)
        account.schedule_enabled = True
        db.session.commit()
        self.task()
        row = ScheduledTask.query.one()
        client = self.connect(self.account_id)
        db.session.add(PendingReply(account_id=self.account_id, group_id='123', message='reply', scheduled_at=datetime.utcnow()))
        db.session.commit()
        with patch('message_handler._is_in_schedule', return_value=False), patch('task_scheduler.send_content', new_callable=AsyncMock) as send:
            asyncio.run(execute_task(self.manager, row.id, row.revision))
            asyncio.run(self.manager._check_pending_replies())
            send.assert_not_awaited()
            client.send_message.assert_not_awaited()

    def test_connection_status_and_failed_reload(self):
        old = self.connect(self.account_id, connected=False)
        old.disconnect = AsyncMock()
        self.assertNotIn(self.account_id, self.manager.connected_accounts)
        client = AsyncMock()
        client.connect.side_effect = RuntimeError('offline')
        with patch('telegram_manager.TelegramClient', return_value=client):
            asyncio.run(self.manager._start_client(self.account_id, '+100', 1, 'test', ''))
        self.assertNotIn(self.account_id, self.manager.clients)
        client.disconnect.assert_awaited_once()
        old.disconnect.assert_awaited_once()

    @unittest.skipUnless(shutil.which('node'), 'Node is needed for the HTML rendering regression')
    def test_queue_escapes_untrusted_text_and_attributes(self):
        templates = Path(__file__).resolve().parents[1] / 'web' / 'templates'
        base = (templates / 'base.html').read_text(encoding='utf-8')
        queue = (templates / 'queue.html').read_text(encoding='utf-8')
        script = re.search(r'function escapeHtml\(value\) \{.*?\n\}', base, re.S).group()
        for name in ('fmtTime', 'urgencyClass', 'barColor', 'buildRow'):
            script += '\n' + re.search(r'function ' + name + r'\([^)]*\) \{.*?\n\}', queue, re.S).group()
        script += '''
const maxSeconds = {};
const attack = '<img src=x onerror="alert(1)">';
const row = buildRow({type:'scheduled_task', id:1, remaining_seconds:60, account:attack,
 group_name:attack, group_id:'123', message:attack, triggered_by:attack, scheduled_at:attack});
if (row.includes('<img') || row.includes('title="<')) process.exit(1);
if (!row.includes('&lt;img') || !row.includes('&quot;')) process.exit(2);
'''
        subprocess.run([shutil.which('node'), '-e', script], check=True, capture_output=True, timeout=10)
