from flask import Blueprint, render_template, request, redirect, url_for, flash
from models import db, Account, Keyword
from media import save_image

keywords_bp = Blueprint('keywords', __name__)


@keywords_bp.route('/')
def index():
    keywords = Keyword.query.order_by(Keyword.created_at.desc()).all()
    accounts = Account.query.filter_by(status='authorized').all()
    return render_template('keywords.html', keywords=keywords, accounts=accounts)


@keywords_bp.route('/add', methods=['POST'])
def add():
    account_id = request.form.get('account_id') or None
    keyword_items = request.form.getlist('keywords[]')
    keyword_text = '\n'.join(k.strip() for k in keyword_items if k.strip())
    has_time = request.form.get('has_time_requirement') == 'on'
    buffer_secs = request.form.get('time_buffer_seconds', '30').strip()
    buffer_type = request.form.get('buffer_type', 'fixed')
    reply_message = request.form.get('reply_message', '').strip()

    if not keyword_text or not (reply_message or request.files.get('image')):
        flash('Укажите ключевые слова и текст или изображение', 'danger')
        return redirect(url_for('keywords.index'))

    try:
        buffer_secs = int(buffer_secs)
    except ValueError:
        buffer_secs = 30

    try:
        buf_rand_min = int(request.form.get('buffer_random_min', '0'))
    except ValueError:
        buf_rand_min = 0
    try:
        buf_rand_max = int(request.form.get('buffer_random_max', '0'))
    except ValueError:
        buf_rand_max = 0
    if buffer_type != 'random':
        buf_rand_min = buf_rand_max = 0

    if account_id:
        try:
            account_id = int(account_id)
        except ValueError:
            account_id = None

    target_group_id = request.form.get('target_group_id', '').strip() or None
    target_group_name = request.form.get('target_group_name', '').strip()

    use_random = request.form.get('use_random_time') == 'on'
    try:
        rand_min = int(request.form.get('random_min_seconds', '60'))
    except ValueError:
        rand_min = 60
    try:
        rand_max = int(request.form.get('random_max_seconds', '300'))
    except ValueError:
        rand_max = 300

    topic_id_raw = request.form.get('topic_id', '').strip()
    topic_id = int(topic_id_raw) if topic_id_raw.lstrip('-').isdigit() else None

    trigger_mode = request.form.get('trigger_mode', 'reply_to_me')
    if trigger_mode not in ('reply_to_me', 'mention_me', 'all_messages'):
        trigger_mode = 'reply_to_me'

    kw = Keyword(
        account_id=account_id,
        keyword=keyword_text,
        has_time_requirement=has_time,
        time_buffer_seconds=buffer_secs,
        buffer_random_min=buf_rand_min,
        buffer_random_max=buf_rand_max,
        reply_message=reply_message,
        trigger_mode=trigger_mode,
        target_group_id=target_group_id,
        target_group_name=target_group_name,
        topic_id=topic_id,
        use_random_time=use_random,
        random_min_seconds=rand_min,
        random_max_seconds=rand_max,
        is_active=True,
    )
    try:
        kw.image = save_image()
    except ValueError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('keywords.index'))
    db.session.add(kw)
    db.session.commit()

    # 自动同步目标到目标列表（仅在填写了目标 ID 时）
    if target_group_id:
        from web.routes.targets import upsert_target
        upsert_target(target_group_id, target_group_name)

    flash('Правило добавлено', 'success')
    return redirect(url_for('keywords.index'))


@keywords_bp.route('/<int:kw_id>/edit', methods=['GET', 'POST'])
def edit(kw_id):
    kw = Keyword.query.get_or_404(kw_id)
    accounts = Account.query.filter_by(status='authorized').all()

    if request.method == 'POST':
        kw.account_id = request.form.get('account_id') or None
        if kw.account_id:
            kw.account_id = int(kw.account_id)
        keyword_items = request.form.getlist('keywords[]')
        kw.keyword = '\n'.join(k.strip() for k in keyword_items if k.strip())
        kw.has_time_requirement = request.form.get('has_time_requirement') == 'on'
        buf = request.form.get('time_buffer_seconds', '30').strip()
        kw.time_buffer_seconds = int(buf) if buf.isdigit() else 30
        buffer_type = request.form.get('buffer_type', 'fixed')
        try:
            kw.buffer_random_min = int(request.form.get('buffer_random_min', '0'))
        except ValueError:
            kw.buffer_random_min = 0
        try:
            kw.buffer_random_max = int(request.form.get('buffer_random_max', '0'))
        except ValueError:
            kw.buffer_random_max = 0
        if buffer_type != 'random':
            kw.buffer_random_min = kw.buffer_random_max = 0
        kw.reply_message = request.form.get('reply_message', '').strip()
        trigger_mode = request.form.get('trigger_mode', 'reply_to_me')
        kw.trigger_mode = trigger_mode if trigger_mode in ('reply_to_me', 'mention_me', 'all_messages') else 'reply_to_me'
        kw.target_group_id = request.form.get('target_group_id', '').strip() or None
        kw.target_group_name = request.form.get('target_group_name', '').strip()
        topic_id_raw = request.form.get('topic_id', '').strip()
        kw.topic_id = int(topic_id_raw) if topic_id_raw.lstrip('-').isdigit() else None
        kw.use_random_time = request.form.get('use_random_time') == 'on'
        try:
            kw.random_min_seconds = int(request.form.get('random_min_seconds', '60'))
        except ValueError:
            kw.random_min_seconds = 60
        try:
            kw.random_max_seconds = int(request.form.get('random_max_seconds', '300'))
        except ValueError:
            kw.random_max_seconds = 300

        if not kw.keyword or not (kw.reply_message or request.files.get('image') or (kw.image and request.form.get('remove_image') != 'on')):
            flash('Укажите ключевые слова и текст или изображение', 'danger')
            return render_template('keyword_edit.html', kw=kw, accounts=accounts)

        try:
            kw.image = save_image(kw.image)
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
            return render_template('keyword_edit.html', kw=kw, accounts=accounts)
        db.session.commit()
        flash('Правило обновлено', 'success')
        return redirect(url_for('keywords.index'))

    return render_template('keyword_edit.html', kw=kw, accounts=accounts)


@keywords_bp.route('/<int:kw_id>/toggle', methods=['POST'])
def toggle(kw_id):
    kw = Keyword.query.get_or_404(kw_id)
    kw.is_active = not kw.is_active
    db.session.commit()
    status = 'Включено' if kw.is_active else 'Отключено'
    flash(f'Правило: {status}', 'success')
    return redirect(url_for('keywords.index'))


@keywords_bp.route('/<int:kw_id>/delete', methods=['POST'])
def delete(kw_id):
    kw = Keyword.query.get_or_404(kw_id)
    db.session.delete(kw)
    db.session.commit()
    flash('Правило удалено', 'success')
    return redirect(url_for('keywords.index'))
