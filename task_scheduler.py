import asyncio
from datetime import datetime, timedelta, timezone
import random

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from media import send_content
from captions import CaptionError, generate_caption
from models import Account, MessageLog, ScheduledTask, TaskImage, db


def utc_naive(value):
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def account_ready(manager, account):
    from message_handler import _is_in_schedule
    return bool(account and account.is_active and account.status == 'authorized'
                and manager.client_connected(manager.clients.get(account.id))
                and (not account.schedule_enabled or _is_in_schedule(account.schedule_start, account.schedule_end)))


def next_account(manager, task):
    accounts = [account for account in task.sending_accounts if account_ready(manager, account)]
    if not accounts:
        return None
    return next((account for account in accounts if account.id > (task.last_account_id or 0)), accounts[0])


async def sync_tasks(manager):
    if not manager.scheduler:
        return
    running = getattr(manager, '_running_tasks', set())
    with manager.app.app_context():
        tasks = ScheduledTask.query.filter_by(is_active=True).all()
        wanted = set()
        now = datetime.utcnow()
        for task in tasks:
            job_id = f'task_{task.id}'
            if task.delivery_state != 'ready' and task.id not in running:
                if task.delivery_state == 'generating':
                    task.is_active, task.delivery_state, task.next_run_at = False, 'ready', None
                    task.last_error = 'Генерация подписи была прервана. Скриншот остался в очереди; включите задачу снова.'
                    continue
                task.is_active = False
                task.delivery_state = 'uncertain'
                task.last_error = 'Отправка была прервана. Проверьте чат: автоматический повтор отключён.'
                for item in task.images:
                    if item.state == 'sending':
                        item.state = 'uncertain'
                continue
            if task.id in running:
                wanted.add(job_id)
                continue
            if next_account(manager, task) is None:
                continue
            wanted.add(job_id)
            existing = manager.scheduler.get_job(job_id)
            args = [manager, task.id, task.revision]
            if existing and existing.args[2] == task.revision:
                task.next_run_at = utc_naive(existing.next_run_time) if existing.next_run_time else None
                continue
            if task.task_type == 'interval':
                trigger = IntervalTrigger(minutes=task.interval_minutes, timezone='UTC')
                next_run = task.next_run_at
                if not next_run:
                    next_run = now if task.send_immediately and not task.last_run_at else now + timedelta(minutes=task.interval_minutes)
                # At most one catch-up execution after downtime.
                next_run = max(next_run, now)
            elif task.task_type == 'cron':
                trigger = CronTrigger.from_crontab(task.cron_expression, timezone='UTC')
                next_run = trigger.get_next_fire_time(None, now.replace(tzinfo=timezone.utc))
            elif task.task_type == 'once':
                next_run = max(task.once_at or now, now)
                trigger = DateTrigger(run_date=next_run, timezone='UTC')
            else:
                continue
            job = manager.scheduler.add_job(execute_task, trigger, id=job_id, args=args,
                                            next_run_time=next_run, replace_existing=True,
                                            max_instances=1, coalesce=True, misfire_grace_time=None)
            task.next_run_at = utc_naive(job.next_run_time)
        for job in manager.scheduler.get_jobs():
            if job.id.startswith('task_') and job.id not in wanted:
                manager.scheduler.remove_job(job.id)
        db.session.commit()
    manager._refresh_trigger_modes()


async def execute_task(manager, task_id, revision):
    if not hasattr(manager, '_running_tasks'):
        manager._running_tasks = set()
    if task_id in manager._running_tasks:
        return
    manager._running_tasks.add(task_id)
    reserved = False
    image_id = None
    generating = False
    try:
        with manager.app.app_context():
            task = db.session.get(ScheduledTask, task_id)
            if not task or not task.is_active or task.revision != revision:
                return
            low, high = task.random_delay_min or 0, task.random_delay_max or 0
        if high:
            await asyncio.sleep(random.randint(low, high))
        with manager.app.app_context():
            task = db.session.get(ScheduledTask, task_id)
            if not task or not task.is_active or task.revision != revision or task.delivery_state != 'ready':
                return
            account = next_account(manager, task)
            if account is None:
                return
            client = manager.clients[account.id]
            image = task.image
            if task.images:
                item = TaskImage.query.filter_by(task_id=task.id, state='pending').order_by(TaskImage.id).first()
                if item is None:
                    task.is_active = False
                    task.completed_at = datetime.utcnow()
                    task.next_run_at = None
                    db.session.commit()
                    return
                image, image_id = item.filename, item.id
            account_id, group_id, message, topic_id = account.id, task.group_id, task.message, task.topic_id
            ai = task.caption_mode == 'ai'
            cached_caption = item.caption if image_id else None
            if ai:
                language, limit = task.caption_language, task.caption_max_chars
                source_image = image if task.caption_use_image else None
                previous = [entry.caption for entry in task.images if entry.state == 'sent' and entry.caption]
                task.delivery_state = 'generating'
                db.session.commit()
                generating = True
        if ai:
            message = cached_caption or await asyncio.wait_for(generate_caption(
                manager.app, message, language, limit, image=source_image, previous=previous), timeout=80)
        with manager.app.app_context():
            task = db.session.get(ScheduledTask, task_id)
            if not task:
                return
            if not task.is_active or task.revision != revision:
                if generating and task.delivery_state == 'generating':
                    task.delivery_state = 'ready'
                    db.session.commit()
                generating = False
                return
            if not account_ready(manager, db.session.get(Account, account_id)) or manager.clients.get(account_id) is not client:
                if generating:
                    task.delivery_state = 'ready'
                    db.session.commit()
                generating = False
                return
            if image_id:
                item = db.session.get(TaskImage, image_id)
                if item.state != 'pending':
                    raise CaptionError('Состояние скриншота изменилось. Проверьте очередь задачи')
                if ai:
                    item.caption = message
                item.state = 'sending'
            # Persist the reservation before the network call; an interrupted delivery
            # must never silently reuse an image or a one-time message on restart.
            task.delivery_state = 'sending'
            db.session.commit()
            reserved = True
            generating = False
        kwargs = {'reply_to': topic_id} if topic_id else {}
        await send_content(client, int(group_id), message, image, app=manager.app, **kwargs)
        with manager.app.app_context():
            task = db.session.get(ScheduledTask, task_id)
            if not task:
                return
            now = datetime.utcnow()
            if image_id:
                item = db.session.get(TaskImage, image_id)
                if item:
                    item.state, item.sent_at = 'sent', now
                    item.sent_by_account_id = account_id
            task.last_account_id = account_id
            task.last_run_at, task.delivery_state, task.last_error = now, 'ready', ''
            exhausted = bool(task.images) and not TaskImage.query.filter_by(task_id=task.id, state='pending').first()
            if task.task_type == 'once' or exhausted:
                task.is_active, task.completed_at, task.next_run_at = False, now, None
            else:
                job = manager.scheduler.get_job(f'task_{task.id}') if manager.scheduler else None
                task.next_run_at = utc_naive(job.next_run_time) if job and job.next_run_time else None
            db.session.add(MessageLog(account_id=account_id, group_id=group_id,
                                      log_type='scheduled_sent', content=message or '[Изображение]'))
            db.session.commit()
            reserved = False
    except BaseException as exc:
        if generating and not reserved:
            with manager.app.app_context():
                db.session.rollback()
                task = db.session.get(ScheduledTask, task_id)
                if task:
                    task.is_active, task.delivery_state, task.next_run_at = False, 'ready', None
                    reason = str(exc) if isinstance(exc, CaptionError) else 'Генерация подписи не завершилась'
                    task.last_error = reason + '. Скриншот не отправлен и остаётся в очереди.'
                    db.session.add(MessageLog(account_id=account_id, group_id=task.group_id,
                                              log_type='error', content=task.last_error))
                    db.session.commit()
        if reserved:
            with manager.app.app_context():
                db.session.rollback()
                task = db.session.get(ScheduledTask, task_id)
                if task:
                    task.is_active, task.delivery_state, task.next_run_at = False, 'uncertain', None
                    task.last_error = 'Отправка не подтверждена. Проверьте чат; повтор автоматически не выполняется.'
                    if image_id:
                        item = db.session.get(TaskImage, image_id)
                        if item:
                            item.state = 'uncertain'
                    db.session.add(MessageLog(account_id=account_id, group_id=task.group_id,
                                              log_type='error', content=task.last_error))
                    db.session.commit()
        raise
    finally:
        manager._running_tasks.discard(task_id)
        await sync_tasks(manager)
