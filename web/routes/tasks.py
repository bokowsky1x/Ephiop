from datetime import datetime, timezone
import re
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, session, jsonify
from apscheduler.triggers.cron import CronTrigger

from models import db, Account, ScheduledTask, TaskImage
from media import save_image, store_image, image_path
from captions import CaptionError, LANGUAGES, generate_caption

tasks_bp = Blueprint('tasks', __name__)


def caption_fields(form):
    mode = form.get('caption_mode', 'fixed')
    if mode not in ('fixed', 'ai'):
        raise ValueError('Неизвестный режим подписи')
    language = form.get('caption_language', 'ru').strip()
    if language == 'other':
        language = form.get('caption_custom_language', '').strip()
    if not language or len(language) > 80:
        raise ValueError('Укажите язык подписи (до 80 символов)')
    limit = int(form.get('caption_max_chars', '300'))
    if not 80 <= limit <= 1000:
        raise ValueError('Длина подписи: от 80 до 1000 символов')
    if mode == 'ai':
        if not current_app.config.get('OPENAI_API_KEY'):
            raise ValueError('На сервере не настроен OPENAI_API_KEY')
        if not form.get('message', '').strip() or len(form.get('message', '')) > 4000:
            raise ValueError('Укажите основу текста для AI (до 4000 символов)')
    return mode, language, limit, form.get('caption_use_image') == 'on'


def update_fields(task, new=False):
    created_files = []
    try:
        form = request.form
        account_id = int(form.get('account_id', ''))
        account = db.session.get(Account, account_id)
        if not account or account.status != 'authorized':
            raise ValueError('Выберите авторизованный аккаунт')
        group_id = form.get('group_id', '').strip()
        if not group_id or not group_id.lstrip('-').isdigit():
            raise ValueError('Укажите числовой ID получателя')
        task_type = form.get('task_type', 'interval')
        if task_type not in ('interval', 'cron', 'once'):
            raise ValueError('Неизвестный тип задачи')
        interval = None
        cron = None
        once_at = None
        if task_type == 'interval':
            interval = int(form.get('interval_minutes', '0'))
            if interval <= 0:
                raise ValueError('Интервал должен быть больше нуля')
        elif task_type == 'cron':
            cron = form.get('cron_expression', '').strip()
            try:
                CronTrigger.from_crontab(cron, timezone='UTC')
            except ValueError:
                raise ValueError('Некорректное выражение Cron')
        else:
            value = form.get('once_at', '').strip()
            if value:
                try:
                    once_at = datetime.fromisoformat(value)
                    if once_at.tzinfo:
                        once_at = once_at.astimezone(timezone.utc).replace(tzinfo=None)
                except ValueError:
                    raise ValueError('Некорректная дата разовой отправки')
                if once_at < datetime.utcnow() and (new or once_at != task.once_at):
                    raise ValueError('Укажите будущее время UTC или оставьте поле пустым для отправки сейчас')
            else:
                once_at = task.once_at if not new and task.task_type == 'once' else datetime.utcnow()
        low = int(form.get('random_delay_min', '0'))
        high = int(form.get('random_delay_max', '0'))
        if not 0 <= low <= high <= 86400:
            raise ValueError('Задержка: от 0 до 86400 секунд, минимум не больше максимума')
        topic = form.get('topic_id', '').strip()
        topic_id = int(topic) if topic else None
        if topic_id is not None and topic_id <= 0:
            raise ValueError('ID темы должен быть положительным')
        text = form.get('message', '').strip()
        caption_options = caption_fields(form)
        ai = caption_options[0] == 'ai'
        uploads = [upload for upload in request.files.getlist('pool_images') if upload.filename]
        folder = [upload for upload in request.files.getlist('pool_folder') if upload.filename]
        folder = [upload for upload in folder if upload.filename.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))]
        folder.sort(key=lambda upload: tuple((0, int(part)) if part.isdigit() else (1, part.casefold())
                                            for part in re.split(r'(\d+)', upload.filename)))
        uploads.extend(folder)
        if len(uploads) > 50:
            raise ValueError('Можно добавить не более 50 изображений за один раз')
        if uploads and request.files.get('image'):
            raise ValueError('Выберите одно вложение или пул скриншотов')
        remove_ids = set(form.getlist('remove_pool_ids'))
        remaining = [item for item in task.images if item.state == 'pending' and str(item.id) not in remove_ids]
        pool_mode = bool(task.images or uploads)
        if pool_mode and not ai and len(text) > 1024:
            raise ValueError('Подпись к изображению не должна превышать 1024 символа')
        if pool_mode and request.files.get('image'):
            raise ValueError('В этой задаче используется пул скриншотов')
        image = None if pool_mode else save_image(task.image, text='AI' if ai else text)
        known = {item.digest for item in task.images}
        additions = []
        for upload in uploads:
            filename, digest = store_image(upload)
            created_files.append(filename)
            if digest in known:
                image_path(filename).unlink(missing_ok=True)
                created_files.remove(filename)
                continue
            known.add(digest)
            additions.append(TaskImage(filename=filename, digest=digest, state='pending'))
        if new and pool_mode and not additions:
            raise ValueError('Добавьте хотя бы одно изображение')
        if not pool_mode and not text and not image:
            raise ValueError('Укажите текст или изображение')
        old_schedule = (task.task_type, task.interval_minutes, task.cron_expression, task.once_at)
        new_schedule = (task_type, interval, cron, once_at)
        task.account_id, task.group_id = account_id, group_id
        task.group_name, task.topic_id = form.get('group_name', '').strip(), topic_id
        old_message = task.message
        task.message, task.image = text, image
        old_caption_options = (task.caption_mode, task.caption_language, task.caption_max_chars, task.caption_use_image)
        caption_changed = old_caption_options != caption_options or old_message != text
        task.caption_mode, task.caption_language, task.caption_max_chars, task.caption_use_image = caption_options
        if caption_changed:
            for item in task.images:
                if item.state == 'pending':
                    item.caption = None
        task.task_type, task.interval_minutes, task.cron_expression, task.once_at = new_schedule
        task.random_delay_min, task.random_delay_max = low, high
        task.send_immediately = form.get('send_immediately') == 'on'
        task.revision = (task.revision or 0) + 1
        if new or old_schedule != new_schedule:
            task.next_run_at = None
        for item in task.images:
            if item.state == 'pending' and str(item.id) in remove_ids:
                item.state = 'skipped'
        task.images.extend(additions)
        if not new and task.completed_at and additions and task_type != 'once':
            task.completed_at = None
        if task.is_active and pool_mode and not remaining and not additions:
            task.is_active = False
        return created_files
    except Exception:
        for name in created_files:
            image_path(name).unlink(missing_ok=True)
        raise


def notify_scheduler():
    manager = current_app.telegram_manager
    if manager:
        manager.submit(manager._reload_scheduled_tasks())


@tasks_bp.route('/')
def index():
    tasks = ScheduledTask.query.order_by(ScheduledTask.created_at.desc()).all()
    accounts = Account.query.filter_by(status='authorized').all()
    return render_template('tasks.html', tasks=tasks, accounts=accounts)


@tasks_bp.route('/add', methods=['POST'])
def add():
    task = ScheduledTask(is_active=True)
    try:
        update_fields(task, new=True)
        db.session.add(task)
        db.session.commit()
    except (ValueError, TypeError) as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
        return redirect(url_for('tasks.index'))
    from web.routes.targets import upsert_target
    upsert_target(task.group_id, task.group_name)
    notify_scheduler()
    flash('Задача добавлена', 'success')
    return redirect(url_for('tasks.index'))


@tasks_bp.route('/<int:task_id>/edit', methods=['GET', 'POST'])
def edit(task_id):
    task = ScheduledTask.query.get_or_404(task_id)
    accounts = Account.query.filter_by(status='authorized').all()
    if request.method == 'POST':
        try:
            if task.delivery_state in ('sending', 'generating'):
                raise ValueError('Сейчас готовится или отправляется сообщение. Дождитесь завершения')
            update_fields(task)
            db.session.commit()
        except (ValueError, TypeError) as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
            return render_template('task_edit.html', task=task, accounts=accounts)
        from web.routes.targets import upsert_target
        upsert_target(task.group_id, task.group_name)
        notify_scheduler()
        flash('Задача обновлена', 'success')
        return redirect(url_for('tasks.index'))
    return render_template('task_edit.html', task=task, accounts=accounts)


@tasks_bp.route('/<int:task_id>/toggle', methods=['POST'])
def toggle(task_id):
    task = ScheduledTask.query.get_or_404(task_id)
    if not task.is_active:
        if task.task_type == 'once' and (task.completed_at or task.delivery_state != 'ready'):
            flash('Эта разовая отправка уже выполнена или не подтверждена. Для новой отправки создайте новую задачу', 'warning')
            return redirect(url_for('tasks.index'))
        if task.images and not any(item.state == 'pending' for item in task.images):
            flash('Пул закончился. Добавьте новые изображения в редакторе', 'warning')
            return redirect(url_for('tasks.index'))
        task.delivery_state = 'ready'
        task.completed_at = None
        task.last_error = ''
        task.next_run_at = None
    task.is_active = not task.is_active
    task.revision += 1
    db.session.commit()
    notify_scheduler()
    flash('Задача включена' if task.is_active else 'Задача остановлена', 'success')
    return redirect(url_for('tasks.index'))


@tasks_bp.route('/<int:task_id>/delete', methods=['POST'])
def delete(task_id):
    task = ScheduledTask.query.get_or_404(task_id)
    if task.delivery_state in ('sending', 'generating'):
        flash('Дождитесь завершения подготовки и отправки', 'warning')
        return redirect(url_for('tasks.index'))
    db.session.delete(task)
    db.session.commit()
    notify_scheduler()
    flash('Задача удалена', 'success')
    return redirect(url_for('tasks.index'))


@tasks_bp.route('/caption-preview', methods=['POST'])
def caption_preview():
    import asyncio
    import secrets
    data = request.get_json(silent=True) or {}
    if not session.get('caption_csrf') or not secrets.compare_digest(str(data.get('csrf_token', '')), session['caption_csrf']):
        return jsonify(error='Обновите страницу и повторите попытку'), 400
    try:
        fields = {**data, 'caption_mode': 'ai'}
        _, language, limit, _ = caption_fields(fields)
        text = asyncio.run(asyncio.wait_for(generate_caption(
            current_app._get_current_object(), data.get('message', ''), language, limit), timeout=80))
        return jsonify(caption=text)
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
    except Exception:
        return jsonify(error='Не удалось получить пример подписи. Повторите попытку позже'), 503
