from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from models import db, Account, ScheduledTask
from media import save_image

tasks_bp = Blueprint('tasks', __name__)


@tasks_bp.route('/')
def index():
    tasks = ScheduledTask.query.order_by(ScheduledTask.created_at.desc()).all()
    accounts = Account.query.filter_by(status='authorized').all()
    return render_template('tasks.html', tasks=tasks, accounts=accounts)


@tasks_bp.route('/add', methods=['POST'])
def add():
    account_id = request.form.get('account_id', '').strip()
    group_id = request.form.get('group_id', '').strip()
    group_name = request.form.get('group_name', '').strip()
    topic_id_raw = request.form.get('topic_id', '').strip()
    topic_id = int(topic_id_raw) if topic_id_raw.lstrip('-').isdigit() and topic_id_raw.lstrip('-') else None
    message = request.form.get('message', '').strip()
    task_type = request.form.get('task_type', 'interval')
    interval_minutes = request.form.get('interval_minutes', '').strip()
    cron_expression = request.form.get('cron_expression', '').strip()
    random_delay_min = request.form.get('random_delay_min', '0').strip()
    random_delay_max = request.form.get('random_delay_max', '0').strip()

    if not all([account_id, group_id]) or not (message or request.files.get('image')):
        flash('Укажите аккаунт, ID получателя и текст или изображение', 'danger')
        return redirect(url_for('tasks.index'))

    try:
        account_id = int(account_id)
    except ValueError:
        flash('Некорректный аккаунт', 'danger')
        return redirect(url_for('tasks.index'))

    task = ScheduledTask(
        account_id=account_id,
        group_id=group_id,
        group_name=group_name,
        topic_id=topic_id,
        message=message,
        task_type=task_type,
        random_delay_min=int(random_delay_min) if random_delay_min.isdigit() else 0,
        random_delay_max=int(random_delay_max) if random_delay_max.isdigit() else 0,
    )

    if task_type == 'interval':
        if not interval_minutes or not interval_minutes.isdigit():
            flash('Укажите положительный интервал в минутах', 'danger')
            return redirect(url_for('tasks.index'))
        task.interval_minutes = int(interval_minutes)
    elif task_type == 'cron':
        if not cron_expression or len(cron_expression.split()) != 5:
            flash('Некорректное выражение Cron: нужно 5 полей, например "0 9 * * *"', 'danger')
            return redirect(url_for('tasks.index'))
        task.cron_expression = cron_expression

    try:
        task.image = save_image()
    except ValueError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('tasks.index'))
    db.session.add(task)
    db.session.commit()

    # 自动同步目标到目标列表
    from web.routes.targets import upsert_target
    upsert_target(group_id, group_name)

    # 通知 Telegram 管理器重新加载任务
    manager = current_app.telegram_manager
    if manager:
        manager.submit(manager._reload_scheduled_tasks())

    flash('Задача добавлена', 'success')
    return redirect(url_for('tasks.index'))


@tasks_bp.route('/<int:task_id>/edit', methods=['GET', 'POST'])
def edit(task_id):
    task = ScheduledTask.query.get_or_404(task_id)
    accounts = Account.query.filter_by(status='authorized').all()

    if request.method == 'POST':
        account_id = request.form.get('account_id', '').strip()
        group_id = request.form.get('group_id', '').strip()
        message = request.form.get('message', '').strip()

        if not all([account_id, group_id]) or not (message or request.files.get('image') or (task.image and request.form.get('remove_image') != 'on')):
            flash('Укажите аккаунт, ID получателя и текст или изображение', 'danger')
            return render_template('task_edit.html', task=task, accounts=accounts)

        try:
            task.account_id = int(account_id)
        except ValueError:
            flash('Некорректный аккаунт', 'danger')
            return render_template('task_edit.html', task=task, accounts=accounts)

        task.group_id = group_id
        task.group_name = request.form.get('group_name', '').strip()
        topic_id_raw = request.form.get('topic_id', '').strip()
        task.topic_id = int(topic_id_raw) if topic_id_raw.lstrip('-').isdigit() and topic_id_raw.lstrip('-') else None
        task.message = message
        task.task_type = request.form.get('task_type', 'interval')
        random_delay_min = request.form.get('random_delay_min', '0').strip()
        random_delay_max = request.form.get('random_delay_max', '0').strip()
        task.random_delay_min = int(random_delay_min) if random_delay_min.isdigit() else 0
        task.random_delay_max = int(random_delay_max) if random_delay_max.isdigit() else 0

        if task.task_type == 'interval':
            interval_minutes = request.form.get('interval_minutes', '').strip()
            if not interval_minutes or not interval_minutes.isdigit():
                flash('Укажите положительный интервал в минутах', 'danger')
                return render_template('task_edit.html', task=task, accounts=accounts)
            task.interval_minutes = int(interval_minutes)
        elif task.task_type == 'cron':
            cron_expression = request.form.get('cron_expression', '').strip()
            if not cron_expression or len(cron_expression.split()) != 5:
                flash('Некорректное выражение Cron: нужно 5 полей, например "0 9 * * *"', 'danger')
                return render_template('task_edit.html', task=task, accounts=accounts)
            task.cron_expression = cron_expression

        try:
            task.image = save_image(task.image)
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
            return render_template('task_edit.html', task=task, accounts=accounts)
        db.session.commit()

        # 自动同步目标到目标列表
        from web.routes.targets import upsert_target
        upsert_target(group_id, task.group_name)

        # 通知 Telegram 管理器重新加载任务
        manager = current_app.telegram_manager
        if manager:
            manager.submit(manager._reload_scheduled_tasks())

        flash('Задача обновлена', 'success')
        return redirect(url_for('tasks.index'))

    return render_template('task_edit.html', task=task, accounts=accounts)


@tasks_bp.route('/<int:task_id>/toggle', methods=['POST'])
def toggle(task_id):
    task = ScheduledTask.query.get_or_404(task_id)
    manager = current_app.telegram_manager
    task.is_active = not task.is_active
    db.session.commit()

    if manager:
        if task.is_active:
            manager.submit(manager._reload_scheduled_tasks())
        else:
            manager.submit(manager.remove_task_job(task.id))

    status = 'Включено' if task.is_active else 'Отключено'
    flash(f'Задача: {status}', 'success')
    return redirect(url_for('tasks.index'))


@tasks_bp.route('/<int:task_id>/delete', methods=['POST'])
def delete(task_id):
    task = ScheduledTask.query.get_or_404(task_id)
    manager = current_app.telegram_manager
    if manager:
        manager.submit(manager.remove_task_job(task.id))
    db.session.delete(task)
    db.session.commit()
    flash('Задача удалена', 'success')
    return redirect(url_for('tasks.index'))
