import asyncio
from datetime import datetime, timedelta

from telethon import errors

from ai_engine import AnalysisError, analysis_resources
from models import AIDecision


def check_row(key, title, state, detail):
    return dict(key=key, title=title, state=state, detail=detail)


def checks(app, agent, telegram=None):
    manager = app.telegram_manager
    connected = bool(manager and agent.account_id in manager.connected_accounts)
    account = agent.account
    ready = bool(account and account.is_active and account.status == 'authorized')
    rows = [check_row('mode', 'Режим', 'warning' if agent.mode == 'OFF' else 'ok', {
        'OFF': 'Выключен: новые сообщения не анализируются',
        'OBSERVE': 'Наблюдение: анализ без ответов и удалений',
        'ASSIST': 'Ответы и удаления только после подтверждения модератором',
        'AUTO': 'Автоматические действия в пределах разрешений',
    }[agent.mode]),
        check_row('account', 'Аккаунт', 'ok' if ready else 'error',
                  'Авторизован и включён' if ready else 'Аккаунт удалён, выключен или не авторизован'),
        check_row('connection', 'Telegram', 'ok' if connected else 'error',
                  'Подключён; членство и права проверяются отдельно' if connected else 'Нет подключения аккаунта'),
        check_row('consent', 'Передача данных в AI', 'ok' if agent.consent else 'error',
                  'Разрешена' if agent.consent else 'Не разрешена: анализ заблокирован'),
        check_row('key', 'OpenAI API', 'warning' if app.config.get('OPENAI_API_KEY') else 'error',
                  'Ключ настроен; доступ, баланс и модель этой проверкой не проверяются'
                  if app.config.get('OPENAI_API_KEY') else 'На сервере отсутствует OPENAI_API_KEY')]
    try:
        analysis_resources()
        rows.append(check_row('resources', 'Файлы анализа', 'ok', 'Инструкции и словарь доступны и корректны'))
    except AnalysisError as exc:
        rows.append(check_row('resources', 'Файлы анализа', 'error', str(exc)))
    rows.append(check_row('vision', 'Изображения', 'ok' if agent.vision else 'warning',
                          'Анализ разрешён' if agent.vision else 'Vision выключен: изображения отправляются на ручную проверку'))
    categories = [label for flag, label in (
        ('delete_personal_data', 'персональные данные'), ('delete_payment_data', 'платёжные данные'),
        ('delete_identity_documents', 'документы'), ('scam_detection', 'мошенничество')) if getattr(agent, flag)]
    detail = 'Удаление выключено в настройках'
    if agent.allow_delete:
        detail = 'Категории: ' + (', '.join(categories) or 'не выбраны')
        detail += '. ' + ('Автоудаление; права Telegram обязательны' if agent.mode == 'AUTO' else 'Автоудаление не выполняется в этом режиме')
        detail += f'. Пороги: текст {agent.moderation_confidence:.2f}, изображение {agent.vision_confidence:.2f}'
    rows.append(check_row('deletion', 'Удаление сообщений', 'ok' if agent.allow_delete and agent.mode == 'AUTO' else 'warning', detail))
    fresh = bool(telegram and telegram.get('revision') == agent.revision and
                 telegram.get('account_id') == agent.account_id and
                 telegram.get('created_at') == agent.created_at.isoformat() and
                 datetime.utcnow() - datetime.fromisoformat(telegram['at']) < timedelta(minutes=5))
    if fresh:
        rows.extend(telegram['rows'])
    else:
        rows.append(check_row('membership', 'Чат и права Telegram', 'warning', 'Не проверены. Запустите проверку Telegram'))
    if agent.last_error:
        rows.append(check_row('last_error', 'Последний сбой', 'error', agent.last_error))
    latest = AIDecision.query.filter_by(agent_id=agent.id).order_by(AIDecision.id.desc()).first()
    return dict(rows=rows, status='error' if any(row['state'] == 'error' for row in rows) else 'warning',
                checked_at=telegram['at'] if fresh else None, latest=latest)


def telegram_error(exc):
    if isinstance(exc, errors.FloodWaitError):
        return f'Telegram ограничил запросы. Повторите через {exc.seconds} с'
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return 'Telegram не ответил вовремя'
    if isinstance(exc, (errors.UserNotParticipantError, errors.ChannelPrivateError)):
        return 'Нет членства или доступа. Вступите в чат через раздел аккаунтов'
    return f'Доступ не подтверждён ({type(exc).__name__}). Проверьте ID и членство аккаунта'


async def telegram_checks(client, chat_id, channel_id, allow_delete):
    rows = []
    try:
        permissions = await asyncio.wait_for(client.get_permissions(int(chat_id), 'me'), 10)
        participant = getattr(permissions, 'participant', None)
        banned = getattr(participant, 'banned_rights', None)
        if not permissions or getattr(permissions, 'has_left', False) or getattr(banned, 'view_messages', False):
            raise AnalysisError('Аккаунт не состоит в чате или доступ к сообщениям запрещён')
        rows.append(check_row('membership', 'Членство в чате', 'ok', 'Подтверждено Telegram'))
        can_delete = bool(getattr(permissions, 'delete_messages', False))
        rows.append(check_row('delete_rights', 'Право удалять в Telegram', 'ok' if can_delete else 'error' if allow_delete else 'warning',
                              'Есть право удалять чужие сообщения' if can_delete else 'Нет права удалять чужие сообщения: нужен администратор с этим правом'))
    except Exception as exc:
        rows.append(check_row('membership', 'Членство в чате', 'error', str(exc) if isinstance(exc, AnalysisError) else telegram_error(exc)))
    if channel_id:
        try:
            await asyncio.wait_for(client.get_messages(int(channel_id), limit=1), 10)
            rows.append(check_row('channel', 'Официальный канал', 'ok', 'Чтение доступно; вступление не выполнялось'))
        except Exception as exc:
            rows.append(check_row('channel', 'Официальный канал', 'error', telegram_error(exc)))
    return rows
