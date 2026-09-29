import asyncio
from io import BytesIO
from pathlib import Path, PurePosixPath
import re
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZipFile

from telethon.sessions import StringSession


def extract_tdata(data, directory):
    root = Path(directory).resolve()
    try:
        archive = ZipFile(BytesIO(data))
    except BadZipFile as exc:
        raise ValueError('Загрузите ZIP-архив папки tdata') from exc
    with archive:
        entries = archive.infolist()
        if len(entries) > 5000 or sum(item.file_size for item in entries) > 128 * 1024 * 1024:
            raise ValueError('Архив слишком большой: удалите кэш из копии tdata')
        for item in entries:
            name = item.filename.replace('\\', '/')
            parts = PurePosixPath(name).parts
            if name.startswith('/') or '..' in parts or any(':' in part for part in parts):
                raise ValueError('Недопустимый путь в архиве')
            if (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Ссылки в архиве не поддерживаются')
        # Only session files are needed; cached media never reaches the disk.
        for item in entries:
            name = item.filename.replace('\\', '/')
            basename = PurePosixPath(name).name
            if item.is_dir() or not re.fullmatch(r'(?:key_data[012s]?|[A-Fa-f0-9]{16}[012s]?|map[012s]?)', basename):
                continue
            target = root.joinpath(*PurePosixPath(name).parts)
            if not target.resolve().is_relative_to(root):
                raise ValueError('Недопустимый путь в архиве')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(item))
    candidates = {p.parent for p in root.rglob('key_data*') if p.is_file()}
    if len(candidates) != 1:
        raise ValueError('Архив должен содержать одну папку tdata с файлом key_data')
    return candidates.pop()


async def import_tdata(app, data, passcode='', name=''):
    from opentele2.api import API, UseCurrentSession
    from opentele2.td import TDesktop
    from opentele2.tl import TelegramClient
    from models import Account, db

    async def convert():
        with TemporaryDirectory(prefix='ephiop-tdata-') as directory:
            path = extract_tdata(data, directory)
            desktop = TDesktop(str(path), passcode=passcode)
            if not desktop.isLoaded() or not desktop.accounts:
                raise ValueError('В tdata нет авторизованных аккаунтов')
            imported = []
            for account in desktop.accounts:
                client = await TelegramClient.FromTDesktop(
                    account, session=StringSession(), flag=UseCurrentSession,
                    api=API.TelegramDesktop,
                )
                try:
                    await client.connect()
                    if not await client.is_user_authorized():
                        raise ValueError('Сессия tdata больше не авторизована')
                    user = await client.get_me()
                    if not user or not user.phone:
                        raise ValueError('Не удалось получить номер аккаунта')
                    imported.append(dict(
                        name=name or user.first_name or user.phone,
                        phone='+' + user.phone.lstrip('+'),
                        api_id=API.TelegramDesktop.api_id,
                        api_hash=API.TelegramDesktop.api_hash,
                        session_string=StringSession.save(client.session),
                        session_kind='tdesktop', status='authorized',
                    ))
                finally:
                    await client.disconnect()
            return imported

    records = await asyncio.wait_for(convert(), timeout=90)
    with app.app_context():
        ids = []
        skipped = 0
        try:
            for record in records:
                phone = record['phone']
                if Account.query.filter(Account.phone.in_([phone, phone.lstrip('+')])).first():
                    skipped += 1
                    continue
                account = Account(**record)
                db.session.add(account)
                db.session.flush()
                ids.append(account.id)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
    for account_id in ids:
        await app.telegram_manager.reload_account(account_id)
    return len(ids), skipped
