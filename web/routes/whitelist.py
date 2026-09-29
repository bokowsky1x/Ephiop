from flask import Blueprint, render_template, request, redirect, url_for, flash
from models import db, Account, Whitelist

whitelist_bp = Blueprint('whitelist', __name__)


@whitelist_bp.route('/')
def index():
    entries = Whitelist.query.order_by(Whitelist.created_at.desc()).all()
    accounts = Account.query.filter_by(status='authorized').all()
    return render_template('whitelist.html', entries=entries, accounts=accounts)


@whitelist_bp.route('/add', methods=['POST'])
def add():
    account_id = request.form.get('account_id') or None
    entity_id = request.form.get('entity_id', '').strip()
    entity_name = request.form.get('entity_name', '').strip()
    entity_type = request.form.get('entity_type', 'user')
    note = request.form.get('note', '').strip()

    if not entity_id:
        flash('Укажите ID получателя', 'danger')
        return redirect(url_for('whitelist.index'))

    if account_id:
        try:
            account_id = int(account_id)
        except ValueError:
            account_id = None

    # 防止重复添加
    existing = Whitelist.query.filter_by(
        account_id=account_id,
        entity_id=entity_id
    ).first()
    if existing:
        flash(f'ID {entity_id} уже есть в белом списке', 'warning')
        return redirect(url_for('whitelist.index'))

    entry = Whitelist(
        account_id=account_id,
        entity_id=entity_id,
        entity_name=entity_name,
        entity_type=entity_type,
        note=note,
        is_active=True,
    )
    db.session.add(entry)
    db.session.commit()

    # 自动同步到目标列表
    from web.routes.targets import upsert_target
    upsert_target(entity_id, entity_name, entity_type, note)

    flash(f'Добавлено в белый список: {entity_name or entity_id}', 'success')
    return redirect(url_for('whitelist.index'))


@whitelist_bp.route('/<int:entry_id>/toggle', methods=['POST'])
def toggle(entry_id):
    entry = Whitelist.query.get_or_404(entry_id)
    entry.is_active = not entry.is_active
    db.session.commit()
    status = 'Включено' if entry.is_active else 'Отключено'
    flash(f'{entry.entity_name or entry.entity_id}: {status}', 'success')
    return redirect(url_for('whitelist.index'))


@whitelist_bp.route('/<int:entry_id>/delete', methods=['POST'])
def delete(entry_id):
    entry = Whitelist.query.get_or_404(entry_id)
    name = entry.entity_name or entry.entity_id
    db.session.delete(entry)
    db.session.commit()
    flash(f'Удалено из белого списка: {name}', 'success')
    return redirect(url_for('whitelist.index'))
