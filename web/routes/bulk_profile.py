import asyncio
import secrets
import threading
import time
from urllib.parse import urlsplit

from flask import Blueprint, abort, current_app, jsonify, render_template, request, session
from telethon import errors, functions

from models import Account, db
from profile_generation import generate_profiles, text_units, validate_changes
from web.routes.profile import connected_client, error_message, read_profile, remove_personal_channel, run

bulk_profile_bp = Blueprint('bulk_profile', __name__)
PLAN_TTL = 15 * 60
MAX_ACCOUNTS = 50


def init_bulk_profiles(app):
    app.extensions['bulk_profiles'] = dict(lock=threading.Lock(), plans={}, active=set())


def selection(values):
    if not isinstance(values, list) or not values or len(values) > MAX_ACCOUNTS:
        raise ValueError(f'Выберите от 1 до {MAX_ACCOUNTS} аккаунтов')
    if any(isinstance(value, bool) or not str(value).isdigit() for value in values):
        raise ValueError('Некорректный список аккаунтов')
    ids = list(dict.fromkeys(int(value) for value in values))
    accounts = {account.id: account for account in Account.query.filter(Account.id.in_(ids)).all()}
    if len(accounts) != len(ids):
        raise ValueError('Один из выбранных аккаунтов удалён. Обновите список')
    return [accounts[account_id] for account_id in ids]


def settings(data):
    result = {}
    for field in ('name_mode', 'about_mode', 'name_prompt', 'about_prompt', 'about_text', 'about_url'):
        value = data.get(field, '')
        if not isinstance(value, str) or len(value) > 4000:
            raise ValueError('Промпт и текст: не более 4000 символов')
        result[field] = value.strip()
    if result['name_mode'] not in ('keep', 'ai') or result['about_mode'] not in ('keep', 'clear', 'text', 'ai'):
        raise ValueError('Выберите действие для имени и описания')
    result['channel_mode'] = data.get('channel_mode', 'keep')
    if result['channel_mode'] not in ('keep', 'remove'):
        raise ValueError('Выберите действие для канала профиля')
    if result['name_mode'] == 'keep' and result['about_mode'] == 'keep' and result['channel_mode'] == 'keep':
        raise ValueError('Выберите хотя бы одно изменение')
    if result['name_mode'] == 'ai' and not result['name_prompt']:
        raise ValueError('Укажите промпт для имён')
    if result['about_mode'] == 'ai' and not result['about_prompt']:
        raise ValueError('Укажите промпт для описаний')
    link = result['about_url']
    if result['about_mode'] in ('text', 'ai') and link:
        parsed = urlsplit(link)
        if parsed.scheme not in ('https', 'http') or not parsed.hostname or any(char.isspace() for char in link):
            raise ValueError('Укажите полную ссылку, начиная с https:// или http://')
    if result['about_mode'] == 'text' and not (result['about_text'] or link):
        raise ValueError('Введите описание или ссылку; для удаления выберите очистку')
    result['sync_labels'] = data.get('sync_labels') is True
    return result


def post_data():
    data = request.get_json(silent=True)
    token = session.get('bulk_profile_csrf')
    if not isinstance(data, dict) or not token or not isinstance(data.get('csrf_token'), str):
        abort(400)
    if not secrets.compare_digest(data['csrf_token'].encode('utf-8'), token.encode('utf-8')):
        abort(400)
    return data


@bulk_profile_bp.after_request
def no_store(response):
    response.headers['Cache-Control'] = 'no-store'
    return response


@bulk_profile_bp.get('/bulk-profile')
def index():
    session.setdefault('bulk_profile_csrf', secrets.token_urlsafe(32))
    session.setdefault('bulk_profile_owner', secrets.token_urlsafe(32))
    accounts = Account.query.filter_by(status='authorized').order_by(Account.id).all()
    selected = set(request.args.getlist('account_ids', type=int))
    return render_template('bulk_profile.html', accounts=accounts, selected=selected,
                           csrf_token=session['bulk_profile_csrf'])


async def load_profiles(rows, clients):
    semaphore = asyncio.Semaphore(5)

    async def load(row):
        if row['status'] == 'error':
            return
        async with semaphore:
            try:
                row['before'] = await asyncio.wait_for(read_profile(clients[row['id']], 0), 10)
            except Exception as exc:
                row.update(status='error', message=error_message(exc))
    await asyncio.gather(*(load(row) for row in rows))


def public_row(row):
    return {key: row[key] for key in ('id', 'label', 'before', 'changes', 'status', 'message')}


@bulk_profile_bp.post('/bulk-profile/preview')
def preview():
    data = post_data()
    try:
        accounts = selection(data.get('account_ids'))
        options = settings(data)
        rows, clients = [], {}
        for account in accounts:
            row = dict(id=account.id, label=account.name, before={}, changes={}, status='pending', message='Готов',
                       identity=(account.phone, account.created_at))
            rows.append(row)
            try:
                if account.status != 'authorized':
                    raise ValueError('Аккаунт не авторизован')
                clients[account.id] = connected_client(account.id)
            except Exception as exc:
                row.update(status='error', message=error_message(exc))
        if clients:
            run(load_profiles(rows, clients), timeout=120)
        ready = [row for row in rows if row['status'] == 'pending']
        suggestions = [{} for row in ready]
        if ready and (options['name_mode'] == 'ai' or options['about_mode'] == 'ai'):
            limit = min(row['before']['about_limit'] for row in ready)
            if options['about_mode'] == 'ai' and options['about_url']:
                limit -= text_units(options['about_url']) + 1
                if limit < 1:
                    raise ValueError('Ссылка не оставляет места для описания')
            suggestions = run(generate_profiles(
                current_app._get_current_object(), len(ready),
                options['name_prompt'] if options['name_mode'] == 'ai' else '',
                options['about_prompt'] if options['about_mode'] == 'ai' else '', limit,
            ), timeout=60)
        for row, suggestion in zip(ready, suggestions):
            changes = row['changes']
            if options['name_mode'] == 'ai':
                changes.update(first_name=suggestion['first_name'], last_name=suggestion['last_name'])
            mode = options['about_mode']
            if mode != 'keep':
                about = '' if mode == 'clear' else (suggestion['about'] if mode == 'ai' else options['about_text'])
                if mode in ('text', 'ai') and options['about_url']:
                    about = '\n'.join(part for part in (about, options['about_url']) if part)
                changes['about'] = about
            if options['channel_mode'] == 'remove':
                changes['personal_channel_id'] = None
            try:
                validate_changes(changes, row['before']['about_limit'])
                if options['sync_labels'] and 'first_name' in changes:
                    label = ' '.join(part for part in (changes['first_name'], changes['last_name']) if part)
                    if len(label) > 100:
                        raise ValueError('Название в панели должно быть не длиннее 100 символов')
                label_changed = options['sync_labels'] and 'first_name' in changes and label != row['label']
                if all(row['before'][key] == value for key, value in changes.items()) and not label_changed:
                    row.update(status='unchanged', message='Уже установлено')
            except ValueError as exc:
                row.update(status='error', message=str(exc))
        store = current_app.extensions['bulk_profiles']
        with store['lock']:
            now = time.monotonic()
            expired = [key for key, plan in store['plans'].items()
                       if plan['expires'] <= now and not any(row['status'] == 'sending' for row in plan['rows'].values())]
            for key in expired:
                del store['plans'][key]
            if len(store['plans']) >= 100:
                raise ValueError('Слишком много предпросмотров. Повторите через 15 минут')
            plan_id = secrets.token_urlsafe(24)
            store['plans'][plan_id] = dict(owner=session['bulk_profile_owner'], expires=now + PLAN_TTL,
                                          rows={row['id']: row for row in rows}, sync_labels=options['sync_labels'])
        return jsonify(plan_id=plan_id, rows=[public_row(row) for row in rows])
    except Exception as exc:
        return jsonify(error=error_message(exc)), 400


async def apply_changes(client, row):
    current = await read_profile(client, 0)
    if current['user_id'] != row['before']['user_id']:
        raise ValueError('Сессия аккаунта изменилась. Создайте новый предпросмотр')
    if any(current[key] != row['before'][key] for key in row['changes']):
        raise ValueError('Профиль изменился после предпросмотра. Создайте новый предпросмотр')
    validate_changes(row['changes'], current['about_limit'])
    if all(current[key] == value for key, value in row['changes'].items()):
        return
    # Once the write starts, a network failure is ambiguous; never retry it automatically.
    profile_changes = {key: value for key, value in row['changes'].items() if key != 'personal_channel_id'}
    if any(current[key] != value for key, value in profile_changes.items()):
        row['write_started'] = True
        await client(functions.account.UpdateProfileRequest(**profile_changes), flood_sleep_threshold=0)
        row['write_confirmed'] = True
    if 'personal_channel_id' in row['changes'] and current['personal_channel_id'] is not None:
        row['write_started'] = True
        await remove_personal_channel(client)
        row['write_confirmed'] = True


@bulk_profile_bp.post('/bulk-profile/<plan_id>/<int:account_id>/apply')
def apply(plan_id, account_id):
    post_data()
    store = current_app.extensions['bulk_profiles']
    with store['lock']:
        plan = store['plans'].get(plan_id)
        if not plan or plan['owner'] != session.get('bulk_profile_owner'):
            return jsonify(error='Предпросмотр не найден. Создайте новый'), 404
        row = plan['rows'].get(account_id)
        if row is None:
            return jsonify(error='Аккаунта нет в предпросмотре'), 404
        if row['status'] != 'pending':
            return jsonify(public_row(row))
        if plan['expires'] <= time.monotonic():
            return jsonify(error='Предпросмотр устарел. Создайте новый'), 400
        if account_id in store['active']:
            return jsonify(error='Этот аккаунт уже изменяется. Проверьте профиль перед повтором'), 409
        store['active'].add(account_id)
        row.update(status='sending', message='Сохранение', write_started=False, write_confirmed=False)
    try:
        account = db.session.get(Account, account_id)
        if account is None or account.status != 'authorized':
            raise ValueError('Аккаунт удалён или не авторизован')
        if (account.phone, account.created_at) != row['identity']:
            raise ValueError('Аккаунт заменён после предпросмотра. Создайте новый предпросмотр')
        run(apply_changes(connected_client(account_id), row))
        row.update(status='success', message='Сохранено')
        if plan['sync_labels'] and 'first_name' in row['changes']:
            account.name = ' '.join(part for part in (row['changes']['first_name'], row['changes']['last_name']) if part)
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
                row['message'] = 'Профиль сохранён, но название в панели не обновилось'
    except Exception as exc:
        db.session.rollback()
        uncertain = row['write_started'] and not isinstance(exc, (ValueError, errors.RPCError))
        row.update(status='uncertain' if uncertain else 'error', message=error_message(exc))
        if uncertain:
            row['message'] = 'Результат не подтверждён. Проверьте профиль в Telegram перед повтором'
        elif row['write_confirmed']:
            row.update(status='uncertain', message='Часть изменений сохранена. Проверьте профиль в Telegram и создайте новый предпросмотр')
    finally:
        with store['lock']:
            store['active'].discard(account_id)
    return jsonify(public_row(row))
