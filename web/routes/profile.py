import asyncio
from io import BytesIO
import re
import secrets

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, send_file, session, url_for
from PIL import Image, UnidentifiedImageError
from telethon import errors, functions, types, utils

from models import Account, db

profile_bp = Blueprint('profile', __name__)


class RemovePersonalChannelRequest(functions.account.UpdatePersonalChannelRequest):
    async def resolve(self, client, utils):
        # The generated resolver tries to look up InputChannelEmpty as a real peer.
        return


def run(coroutine, timeout=35):
    manager = current_app.telegram_manager
    future = manager.submit(asyncio.wait_for(coroutine, timeout))
    try:
        return future.result(timeout=timeout + 5)
    except Exception:
        future.cancel()
        raise


def connected_client(account_id):
    manager = current_app.telegram_manager
    client = manager.clients.get(account_id) if manager else None
    if client is None or not client.is_connected():
        raise ValueError('Аккаунт не подключён к Telegram')
    return client


def error_message(exc):
    if isinstance(exc, ValueError):
        return str(exc)
    if isinstance(exc, errors.FloodWaitError):
        return f'Telegram ограничил изменения. Повторите через {exc.seconds} с.'
    if isinstance(exc, errors.UsernameOccupiedError):
        return 'Этот @username уже занят'
    if isinstance(exc, errors.UsernameInvalidError):
        return 'Telegram отклонил @username. Проверьте его формат'
    if isinstance(exc, errors.AboutTooLongError):
        return 'Описание превышает допустимую для аккаунта длину'
    if isinstance(exc, errors.FirstNameInvalidError):
        return 'Telegram отклонил имя профиля'
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return 'Telegram не ответил вовремя. Обновите профиль перед повторной попыткой: изменение могло сохраниться.'
    return 'Telegram не выполнил запрос. Обновите страницу и повторите позже.'


async def read_profile(client, flood_sleep_threshold=None):
    options = {} if flood_sleep_threshold is None else {'flood_sleep_threshold': flood_sleep_threshold}
    full = await client(functions.users.GetFullUserRequest(types.InputUserSelf()), **options)
    user = next((user for user in full.users if user.id == full.full_user.id), None)
    if user is None:
        raise ValueError('Telegram не вернул профиль текущего аккаунта. Обновите данные.')
    channel_id = getattr(full.full_user, 'personal_channel_id', None)
    channel = next((chat for chat in getattr(full, 'chats', []) if chat.id == channel_id), None)
    return dict(user_id=user.id, first_name=user.first_name or '', last_name=user.last_name or '',
                username=user.username or '', about=full.full_user.about or '',
                has_photo=bool(user.photo), about_limit=140 if user.premium else 70,
                personal_channel_id=channel_id,
                personal_channel_title=getattr(channel, 'title', '') or '',
                personal_channel_username=getattr(channel, 'username', '') or '')


async def remove_personal_channel(client):
    result = await client(RemovePersonalChannelRequest(types.InputChannelEmpty()),
                          flood_sleep_threshold=0)
    if result is not True:
        raise ValueError('Telegram не подтвердил отвязку канала. Обновите профиль перед повтором')


def prepare_photo(upload):
    if not upload or not upload.filename:
        raise ValueError('Выберите фотографию')
    data = upload.read(10 * 1024 * 1024 + 1)
    if len(data) > 10 * 1024 * 1024:
        raise ValueError('Фотография должна быть не больше 10 МБ')
    try:
        with Image.open(BytesIO(data)) as source:
            if source.format not in ('JPEG', 'PNG', 'WEBP') or source.width * source.height > 20_000_000:
                raise ValueError('Нужна фотография JPEG, PNG или WebP до 20 млн пикселей')
            from PIL import ImageOps
            picture = ImageOps.exif_transpose(source).convert('RGB')
            picture.thumbnail((1600, 1600))
            stream = BytesIO()
            picture.save(stream, 'JPEG', quality=92)
            return stream.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError('Не удалось прочитать фотографию') from exc


async def update(client, action, data, photo=None):
    if action in ('details', 'about'):
        changes = {}
        if action == 'details':
            first_name = data.get('first_name', '').strip()
            last_name = data.get('last_name', '').strip()
            if not first_name or len(first_name) > 64 or len(last_name) > 64:
                raise ValueError('Укажите имя. Имя и фамилия должны быть не длиннее 64 символов')
            changes.update(first_name=first_name, last_name=last_name)
        # Legacy forms may still submit the bio together with the name.
        if action == 'about' or 'about' in data:
            about = data.get('about', '').strip()
            profile = await read_profile(client)
            if len(about.encode('utf-16-le')) // 2 > profile['about_limit']:
                raise ValueError(f'Описание должно быть не длиннее {profile["about_limit"]} символов')
            changes['about'] = about
        await client(functions.account.UpdateProfileRequest(**changes))
    elif action == 'delete_about':
        await client(functions.account.UpdateProfileRequest(about=''))
    elif action == 'remove_channel':
        if data.get('confirm_remove_channel') != 'on':
            raise ValueError('Подтвердите удаление канала из профиля')
        profile = await read_profile(client, 0)
        if str(profile['user_id']) != data.get('channel_user_id'):
            raise ValueError('Сессия аккаунта изменилась. Обновите профиль')
        if profile['personal_channel_id'] is None:
            return
        if str(profile['personal_channel_id']) != data.get('personal_channel_id'):
            raise ValueError('Канал профиля изменился. Обновите профиль перед удалением')
        await remove_personal_channel(client)
    elif action == 'username':
        username = data.get('username', '').strip().removeprefix('@')
        profile = await read_profile(client)
        if username.lower() == profile['username'].lower():
            return
        if username and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,30}[A-Za-z0-9]', username):
            raise ValueError('@username: 5–32 латинских символа, цифры и подчёркивание; первая буква латинская')
        await client(functions.account.UpdateUsernameRequest(username))
    elif action == 'photo':
        stream = BytesIO(photo)
        stream.name = 'profile.jpg'
        uploaded = await client.upload_file(stream)
        await client(functions.photos.UploadProfilePhotoRequest(file=uploaded))
    elif action == 'delete_photo':
        photos = await client.get_profile_photos('me', limit=1)
        if photos:
            await client(functions.photos.DeletePhotosRequest([utils.get_input_photo(photos[0])]))
    else:
        raise ValueError('Неизвестное действие')


@profile_bp.route('/<int:account_id>/profile', methods=['GET', 'POST'])
def edit(account_id):
    account = Account.query.get_or_404(account_id)
    token = session.setdefault('profile_csrf', secrets.token_urlsafe(32))
    if request.method == 'POST':
        if not secrets.compare_digest(request.form.get('csrf_token', ''), token):
            abort(400)
        try:
            action = request.form.get('action', '')
            if action == 'label':
                name = request.form.get('name', '').strip()
                if not name or len(name) > 100:
                    raise ValueError('Название должно содержать от 1 до 100 символов')
                account.name = name
                db.session.commit()
            else:
                client = connected_client(account_id)
                photo = prepare_photo(request.files.get('photo')) if action == 'photo' else None
                run(update(client, action, request.form.to_dict(), photo))
            flash({'about': 'Описание сохранено', 'delete_about': 'Описание удалено',
                   'remove_channel': 'Канал убран из профиля. Сам канал не удалён.'}.get(action, 'Изменения сохранены'), 'success')
            return redirect(url_for('profile.edit', account_id=account_id))
        except Exception as exc:
            db.session.rollback()
            flash(error_message(exc), 'danger')
    profile = None
    try:
        profile = run(read_profile(connected_client(account_id)))
    except Exception as exc:
        if request.method == 'GET':
            flash(error_message(exc), 'warning')
    return render_template('account_profile.html', account=account, profile=profile, csrf_token=token), 200, {'Cache-Control': 'no-store'}


@profile_bp.get('/<int:account_id>/profile/photo')
def photo(account_id):
    Account.query.get_or_404(account_id)
    async def download(client):
        return await client.download_profile_photo('me', file=bytes)
    try:
        data = run(download(connected_client(account_id)))
    except Exception:
        abort(404)
    if not data:
        abort(404)
    response = send_file(BytesIO(data), mimetype='image/jpeg')
    response.headers['Cache-Control'] = 'no-store'
    return response
