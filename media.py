from io import BytesIO
from pathlib import Path
from uuid import uuid4

from flask import current_app, request
from PIL import Image, UnidentifiedImageError

MAX_IMAGE_BYTES = 10 * 1024 * 1024


def image_path(name):
    if not name or Path(name).name != name:
        raise ValueError('Некорректное имя изображения')
    return Path(current_app.instance_path) / 'uploads' / name


def save_image(existing=None, text=None):
    upload = request.files.get('image')
    if text is None:
        text = request.form.get('message', request.form.get('reply_message', '')).strip()
    keeping = existing and request.form.get('remove_image') != 'on'
    if (keeping or (upload and upload.filename)) and len(text) > 1024:
        raise ValueError('Подпись к изображению не должна превышать 1024 символа')
    if not upload or not upload.filename:
        result = existing if keeping else None
        if not text and not result:
            raise ValueError('Укажите текст или изображение')
        return result
    return store_image(upload)[0]


def store_image(upload):
    import hashlib
    data = upload.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError('Размер изображения не должен превышать 10 МБ')
    try:
        with Image.open(BytesIO(data)) as picture:
            if picture.format not in ('JPEG', 'PNG', 'WEBP'):
                raise ValueError('Допустимы изображения JPEG, PNG и WebP')
            if picture.width * picture.height > 20_000_000:
                raise ValueError('Изображение должно содержать не более 20 млн пикселей')
            picture.load()
            normalized = BytesIO()
            picture.convert('RGB').save(normalized, 'JPEG', quality=92)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError('Не удалось прочитать изображение') from exc
    name = uuid4().hex + '.jpg'
    path = image_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(normalized.getvalue())
    return name, hashlib.sha256(normalized.getvalue()).hexdigest()


async def send_content(client, target, message, image=None, app=None, **kwargs):
    if image:
        with (app or current_app).app_context():
            path = image_path(image)
        return await client.send_file(target, str(path), caption=message, **kwargs)
    return await client.send_message(target, message, **kwargs)
