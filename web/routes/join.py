import re
import secrets
from urllib.parse import urlsplit

from flask import Blueprint, abort, current_app, jsonify, render_template, request, session
from telethon import errors, functions, types, utils

from models import Account
from web.routes.profile import connected_client, run
from web.routes.targets import upsert_target

join_bp = Blueprint('join', __name__)


def parse_invite(value):
    value = value.strip()
    if value.startswith('@'):
        value = value[1:]
    elif '/' in value:
        parsed = urlsplit(value if '://' in value else 'https://' + value)
        if parsed.scheme not in ('http', 'https') or parsed.netloc.lower() not in ('t.me', 'telegram.me', 'www.t.me'):
            raise ValueError('Укажите ссылку t.me или @username чата')
        value = parsed.path.strip('/')
        if value.startswith('+') or value.startswith('joinchat/'):
            code = value[1:] if value.startswith('+') else value[len('joinchat/'):]
            if re.fullmatch(r'[A-Za-z0-9_-]+', code):
                return 'invite', code
            raise ValueError('Некорректная ссылка-приглашение')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}', value):
        raise ValueError('Нужна ссылка на чат или @username. Числовой ID и ссылки на сообщения не подходят')
    return 'public', value


def chat_result(chat, status):
    return dict(status=status, message='Уже участник' if status == 'already' else 'Вступил',
                chat_id=str(utils.get_peer_id(chat)), title=chat.title,
                kind='supergroup' if getattr(chat, 'megagroup', False) else ('channel' if isinstance(chat, types.Channel) else 'group'))


async def join_chat(client, kind, target):
    try:
        if kind == 'invite':
            invite = await client(functions.messages.CheckChatInviteRequest(target), flood_sleep_threshold=0)
            if isinstance(invite, types.ChatInviteAlready):
                return chat_result(invite.chat, 'already')
            result = await client(functions.messages.ImportChatInviteRequest(target), flood_sleep_threshold=0)
            chats = getattr(result, 'chats', [])
            if not chats:
                return dict(status='unknown', message='Запрос выполнен. Проверьте членство в списке чатов')
            return chat_result(chats[0], 'joined')
        chat = await client.get_entity(target)
        if not isinstance(chat, types.Channel):
            raise ValueError('Ссылка ведёт на пользователя или бота, а не на чат')
        if not chat.left:
            return chat_result(chat, 'already')
        await client(functions.channels.JoinChannelRequest(chat), flood_sleep_threshold=0)
        return chat_result(chat, 'joined')
    except errors.InviteRequestSentError:
        return dict(status='pending', message='Заявка отправлена, ожидает одобрения администратора')
    except errors.UserAlreadyParticipantError:
        return dict(status='already', message='Уже участник')


def join_error(exc):
    if isinstance(exc, ValueError):
        return str(exc)
    if isinstance(exc, errors.FloodWaitError):
        return f'Лимит Telegram. Повторите через {exc.seconds} с.'
    if isinstance(exc, (errors.InviteHashExpiredError, errors.InviteHashInvalidError)):
        return 'Приглашение недействительно или срок его действия истёк'
    if isinstance(exc, (errors.ChannelPrivateError, errors.UserBannedInChannelError)):
        return 'Нет доступа к чату или аккаунт заблокирован в нём'
    if isinstance(exc, errors.ChannelsTooMuchError):
        return 'Аккаунт достиг лимита групп и каналов'
    if isinstance(exc, TimeoutError):
        return 'Время ожидания истекло. Проверьте членство перед повторной попыткой'
    return 'Telegram не выполнил запрос. Проверьте ссылку и подключение'


@join_bp.get('/join')
def index():
    token = session.setdefault('join_csrf', secrets.token_urlsafe(32))
    accounts = Account.query.order_by(Account.id).all()
    return render_template('join_chat.html', accounts=accounts, csrf_token=token)


@join_bp.post('/<int:account_id>/join')
def submit(account_id):
    data = request.get_json(silent=True) or {}
    token = session.get('join_csrf', '')
    if not token or not isinstance(data.get('csrf_token'), str) or not secrets.compare_digest(data['csrf_token'], token):
        abort(400)
    account = Account.query.get_or_404(account_id)
    try:
        if not account.is_active or account.status != 'authorized':
            raise ValueError('Аккаунт отключён или не авторизован')
        link = data.get('link', '')
        if not isinstance(link, str) or len(link) > 500:
            raise ValueError('Некорректная ссылка')
        kind, target = parse_invite(link)
        result = run(join_chat(connected_client(account_id), kind, target))
        if result.get('chat_id'):
            try:
                upsert_target(result['chat_id'], result['title'], result['kind'])
            except Exception:
                from models import db
                db.session.rollback()
                result['message'] += '. Не удалось сохранить получателя; используйте ID чата'
        return jsonify(result)
    except Exception as exc:
        return jsonify(status='error', message=join_error(exc))
