import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_features as fixtures
from captions import CaptionError, generate_caption
from models import ScheduledTask, db
from task_scheduler import execute_task


class CaptionInstructionsTests(unittest.TestCase):
    setUp = fixtures.FeaturesTest.setUp
    tearDown = fixtures.FeaturesTest.tearDown
    task = fixtures.FeaturesTest.task
    start_test_scheduler = fixtures.FeaturesTest.start_test_scheduler

    def preview_token(self):
        self.client.get('/tasks/')
        with self.client.session_transaction() as state:
            return state['caption_csrf']

    def test_save_edit_escape_and_reset_only_pending_captions(self):
        self.app.config['OPENAI_API_KEY'] = 'fake-test-key'
        rules = 'Разговорный стиль; сумму упоминать редко.'
        self.task(caption_mode='ai', caption_instructions='  ' + rules + '  ',
                  pool_images=[fixtures.picture(), fixtures.picture('blue')])
        row = ScheduledTask.query.one()
        self.assertEqual(row.caption_instructions, rules)
        row.images[0].state, row.images[0].caption = 'sent', 'Sent caption'
        row.images[1].caption = 'Pending caption'
        db.session.commit()
        revision = row.revision
        new_rules = '</textarea><script>alert(1)</script> New style'
        response = self.client.post(f'/tasks/{row.id}/edit', data=dict(
            account_id=str(self.account_id), group_id='123', message='Подпись',
            task_type='interval', interval_minutes='5', caption_mode='ai',
            caption_instructions=new_rules))
        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        self.assertEqual(row.caption_instructions, new_rules)
        self.assertGreater(row.revision, revision)
        self.assertEqual(row.images[0].caption, 'Sent caption')
        self.assertEqual(row.images[0].state, 'sent')
        self.assertIsNone(row.images[1].caption)
        self.assertEqual(row.images[1].state, 'pending')
        page = self.client.get(f'/tasks/{row.id}/edit').get_data(as_text=True)
        self.assertIn('name="caption_instructions"', page)
        self.assertIn('&lt;/textarea&gt;&lt;script&gt;', page)
        self.assertNotIn(new_rules, page)

    def test_empty_instructions_preserve_legacy_behavior(self):
        self.task()
        self.assertEqual(ScheduledTask.query.one().caption_instructions, '')
        self.assertEqual(self.client.get('/tasks/').status_code, 200)
        self.app.config['OPENAI_API_KEY'] = 'fake-test-key'
        api = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(
            return_value=SimpleNamespace(output_text='Caption', status='completed'))))
        context = AsyncMock()
        context.__aenter__.return_value = api
        with patch('captions.AsyncOpenAI', return_value=context):
            asyncio.run(generate_caption(self.app, 'Source', 'en'))
        self.assertNotIn('Task-specific instructions', api.responses.create.await_args.kwargs['instructions'])

    def test_validation_in_forms_preview_and_generator(self):
        self.app.config['OPENAI_API_KEY'] = 'fake-test-key'
        self.task(caption_mode='ai', caption_instructions='x' * 4001)
        self.assertEqual(ScheduledTask.query.count(), 0)
        self.task(caption_mode='ai', caption_instructions='x' * 4000)
        row = ScheduledTask.query.one()
        self.assertEqual(len(row.caption_instructions), 4000)
        response = self.client.post(f'/tasks/{row.id}/edit', data=dict(
            account_id=str(self.account_id), group_id='123', message='Подпись',
            task_type='interval', interval_minutes='5', caption_mode='ai',
            caption_instructions='x' * 4001))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(row.caption_instructions), 4000)
        token = self.preview_token()
        with patch('web.routes.tasks.generate_caption', new_callable=AsyncMock) as generate:
            for value in ('x' * 4001, ['not text'], 42, None):
                with self.subTest(value_type=type(value).__name__):
                    response = self.client.post('/tasks/caption-preview', json=dict(
                        message='Source', csrf_token=token, caption_instructions=value))
                    self.assertEqual(response.status_code, 400)
            generate.assert_not_awaited()
        with patch('captions.AsyncOpenAI') as api:
            with self.assertRaises(CaptionError):
                asyncio.run(generate_caption(self.app, 'Source', 'en', task_instructions='x' * 4001))
            api.assert_not_called()

    def test_instructions_separate_from_source_on_every_retry(self):
        self.app.config['OPENAI_API_KEY'] = 'fake-test-key'
        rules = 'Use natural Amharic. Mention amounts only occasionally.'
        api = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(side_effect=[
            SimpleNamespace(output_text='Same', status='completed'),
            SimpleNamespace(output_text='Different caption', status='completed'),
        ])))
        context = AsyncMock()
        context.__aenter__.return_value = api
        with patch('captions.AsyncOpenAI', return_value=context):
            result = asyncio.run(generate_caption(self.app, 'Reaction', 'am', previous=['Same'],
                                                  task_instructions=rules))
        self.assertEqual(result, 'Different caption')
        self.assertEqual(api.responses.create.await_count, 2)
        for call in api.responses.create.await_args_list:
            payload = call.kwargs
            self.assertIn(rules, payload['instructions'])
            self.assertIn('Never invent names, amounts', payload['instructions'])
            self.assertIn('factual accuracy rules above', payload['instructions'])
            self.assertEqual(json.loads(payload['input'][0]['content'][0]['text'])['source'], 'Reaction')
            self.assertNotIn(rules, payload['input'][0]['content'][0]['text'])
            self.assertFalse(payload['store'])

    def test_scheduled_caption_uses_saved_instructions(self):
        self.app.config['OPENAI_API_KEY'] = 'fake-test-key'
        rules = 'Сумму упоминать изредка.'
        self.task(caption_mode='ai', caption_instructions=rules, pool_images=[fixtures.picture()])
        row = ScheduledTask.query.one()
        self.start_test_scheduler()
        with patch('task_scheduler.generate_caption', new_callable=AsyncMock, return_value='Caption') as generate:
            with patch('task_scheduler.send_content', new_callable=AsyncMock) as send:
                asyncio.run(execute_task(self.manager, row.id, row.revision))
                self.assertEqual(generate.await_args.kwargs['task_instructions'], rules)
                self.assertEqual(send.await_args.args[2], 'Caption')
        db.session.expire_all()
        self.assertEqual(row.images[0].caption, 'Caption')
        self.assertEqual(row.images[0].state, 'sent')

    def test_preview_uses_unsaved_instructions_without_sending(self):
        self.app.config['OPENAI_API_KEY'] = 'fake-test-key'
        rules = 'Natural conversational style, mostly without amounts.'
        token = self.preview_token()
        with patch('web.routes.tasks.generate_caption', new_callable=AsyncMock, return_value='Preview') as generate:
            with patch('telegram_manager.send_content', new_callable=AsyncMock) as send:
                response = self.client.post('/tasks/caption-preview', json=dict(
                    message='Reaction', caption_language='am', caption_instructions=rules,
                    csrf_token=token))
                self.assertEqual(response.json['caption'], 'Preview')
                self.assertEqual(generate.await_args.kwargs['task_instructions'], rules)
                send.assert_not_awaited()
        self.assertEqual(ScheduledTask.query.count(), 0)
