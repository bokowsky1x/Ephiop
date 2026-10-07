import base64
from io import BytesIO
import json
import math
from pathlib import Path
import re

from openai import APIError, APIConnectionError, APITimeoutError, AsyncOpenAI
from PIL import Image, ImageOps, UnidentifiedImageError

LANGUAGES = ('AMHARIC', 'AMHARIC_LATIN', 'OROMO', 'ENGLISH', 'MIXED', 'UNKNOWN')
INTENTS = ('GREETING', 'CASUAL_CHAT', 'FOOTBALL_DISCUSSION', 'POST_DISCUSSION', 'INFORMATION',
           'PROMO_INFO_REQUEST', 'BONUS_INFO_REQUEST', 'FREE_SPIN_INFO_REQUEST', 'WAGERING_INFO_REQUEST',
           'PROMO_CODE_INFO', 'CONTEST_INFO_REQUEST', 'RESULT_INFO_REQUEST', 'HOW_TO_REGISTER',
           'GAMBLING_INSTRUCTIONS', 'DEPOSIT_PROBLEM', 'WITHDRAWAL_PROBLEM', 'BALANCE_PROBLEM',
           'ACCOUNT_PROBLEM', 'TECHNICAL_PROBLEM', 'SUPPORT_REQUEST', 'COMPLAINT', 'PERSONAL_DATA',
           'PAYMENT_DATA', 'IDENTITY_DOCUMENT', 'SCAM', 'FAKE_AGENT', 'FAKE_SUPPORT', 'SPAM',
           'CONTEST_ANSWER', 'SIMPLE_REACTION', 'UNKNOWN')
ACTIONS = ('REPLY', 'IGNORE', 'DELETE', 'WARN', 'MODERATE', 'ESCALATE', 'HUMAN_REVIEW')
CLASSIFICATIONS = ('SAFE', 'PAYMENT_SCREENSHOT', 'PAYMENT_DATA', 'PERSONAL_DATA', 'IDENTITY_DOCUMENT', 'SCAM', 'UNKNOWN')
_PRIVATE = re.compile(
    r'\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b|(?<!\w)\+?\d[\d ()-]{7,}\d(?!\w)|'
    r'(?i:\b(?:otp|transaction\s*(?:id)?|account\s*number|card\s*number|password|pin)\s*[:=#]?\s*[\w-]{4,})'
)


class AnalysisError(ValueError):
    """A safe diagnostic that never contains raw API responses or user data."""


def failure_message(exc, stage):
    if isinstance(exc, AnalysisError):
        detail = str(exc)
    elif isinstance(exc, TimeoutError):
        detail = 'Истекло время ожидания. Проверьте соединение и доступ аккаунта'
    else:
        detail = 'Ошибка ' + type(exc).__name__ + '. Нужна проверка серверных логов'
    return f'{stage}: {detail}. Действия не выполнялись'


def redact(text, limit=1500):
    return _PRIVATE.sub('[PRIVATE]', str(text or ''))[:limit]


def fingerprint(message):
    import hashlib
    data = dict(id=message.id, text=getattr(message, 'raw_text', None) or getattr(message, 'text', '') or '',
                photo=getattr(getattr(message, 'photo', None), 'id', None),
                document=getattr(getattr(message, 'document', None), 'id', None))
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode('utf-8')).hexdigest()


async def image_content(client, message):
    document = getattr(message, 'document', None)
    photo = getattr(message, 'photo', None)
    if not photo and not (document and (document.mime_type or '').startswith('image/')):
        return None
    if document and document.size > 10 * 1024 * 1024:
        raise AnalysisError('Изображение больше 10 МБ: требуется ручная проверка')
    stream = BytesIO()
    def progress(received, total):
        if received > 10 * 1024 * 1024:
            raise AnalysisError('Изображение больше 10 МБ: требуется ручная проверка')
    await client.download_media(message, file=stream, progress_callback=progress)
    if stream.tell() > 10 * 1024 * 1024:
        raise AnalysisError('Изображение больше 10 МБ: требуется ручная проверка')
    stream.seek(0)
    try:
        with Image.open(stream) as source:
            if source.format not in ('JPEG', 'PNG', 'WEBP') or source.width * source.height > 20_000_000:
                raise AnalysisError('Неподдерживаемое изображение: требуется ручная проверка')
            image = ImageOps.exif_transpose(source).convert('RGB')
            image.thumbnail((1600, 1600))
            output = BytesIO()
            image.save(output, 'JPEG', quality=90)
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise AnalysisError('Не удалось прочитать изображение: требуется ручная проверка') from exc
    return dict(type='input_image', detail='high', image_url='data:image/jpeg;base64,' + base64.b64encode(output.getvalue()).decode('ascii'))


def schema(properties):
    return dict(type='object', additionalProperties=False, required=list(properties), properties=properties)


def enum(values):
    return dict(type='string', enum=list(values))


async def ask(app, name, instructions, payload, output_schema, image=None):
    if not app.config.get('OPENAI_API_KEY'):
        raise AnalysisError('На сервере не настроен OPENAI_API_KEY')
    content = [dict(type='input_text', text=json.dumps(payload, ensure_ascii=False))]
    if image:
        content.append(image)
    try:
        async with AsyncOpenAI(api_key=app.config['OPENAI_API_KEY'], timeout=25, max_retries=0) as client:
            model = app.config['OPENAI_MODEL']
            options = {}
            if model.startswith('gpt-6'):
                effort = 'none' if model in ('gpt-6-luna', 'gpt-6-sol') else 'low'
                options = {'reasoning': {'effort': effort}}
            response = await client.responses.create(
                model=app.config['OPENAI_MODEL'], instructions=instructions,
                input=[dict(role='user', content=content)], store=False, max_output_tokens=1800,
                text={'format': dict(type='json_schema', name=name, strict=True, schema=output_schema)},
                **options,
            )
        if response.status != 'completed' or not response.output_text:
            reason = getattr(getattr(response, 'incomplete_details', None), 'reason', None)
            detail = 'Достигнут лимит ответа модели' if reason == 'max_output_tokens' else 'AI не завершил анализ'
            raise AnalysisError(detail + '. Требуется ручная проверка')
        result = json.loads(response.output_text)
        if not isinstance(result, dict) or set(result) != set(output_schema['properties']):
            raise AnalysisError('AI вернул неверный формат анализа')
        return result
    except APITimeoutError as exc:
        raise AnalysisError('OpenAI не ответил за отведённое время') from exc
    except APIConnectionError as exc:
        raise AnalysisError('Нет соединения с OpenAI. Проверьте сеть сервера') from exc
    except APIError as exc:
        reasons = {
            'invalid_api_key': 'Проверьте OPENAI_API_KEY на сервере',
            'model_not_found': 'Модель не найдена или недоступна этому API-ключу',
            'insufficient_quota': 'Проверьте баланс и лимиты OpenAI API',
            'rate_limit_exceeded': 'Достигнут лимит запросов OpenAI API',
            'invalid_image': 'OpenAI не принял изображение',
        }
        status = getattr(exc, 'status_code', None)
        code = getattr(exc, 'code', None)
        reason = reasons.get(code, 'Проверьте модель, доступ API и параметры запроса') if isinstance(code, str) else 'Проверьте доступ OpenAI API'
        raise AnalysisError(f'OpenAI HTTP {status}: {reason}' if status else 'OpenAI недоступен: ' + reason) from exc
    except TypeError as exc:
        raise AnalysisError('Неверный формат API или несовместимый OpenAI SDK. Обновите requirements.txt') from exc
    except json.JSONDecodeError as exc:
        raise AnalysisError('AI вернул неверный формат анализа') from exc


async def analyze(app, payload, image=None):
    instructions = (Path(__file__).parent / 'prompts' / 'assistant_system.txt').read_text(encoding='utf-8')
    payload = {**payload, 'slang': json.loads((Path(__file__).parent / 'data' / 'ethiopia_slang.json').read_text(encoding='utf-8'))}
    output_schema = schema(dict(language=enum(LANGUAGES), intent=enum(INTENTS), action=enum(ACTIONS),
                                confidence=dict(type='number'), classification=enum(CLASSIFICATIONS),
                                reply=dict(type='string'), reason=dict(type='string'),
                                fact_ids=dict(type='array', items=dict(type='integer'))))
    result = await ask(app, 'community_decision', instructions, payload, output_schema, image)
    if any(result[key] not in values for key, values in (
        ('language', LANGUAGES), ('intent', INTENTS), ('action', ACTIONS), ('classification', CLASSIFICATIONS))):
        raise AnalysisError('AI вернул неизвестную классификацию')
    confidence = result['confidence']
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise AnalysisError('AI вернул неверную уверенность')
    if not all(isinstance(result[key], str) for key in ('reply', 'reason')):
        raise AnalysisError('AI вернул неверный текст')
    if len(result['reply'].encode('utf-16-le')) // 2 > 800 or len(result['reason']) > 500:
        raise AnalysisError('AI вернул слишком длинный ответ')
    if not isinstance(result['fact_ids'], list) or len(result['fact_ids']) > 3 or any(type(value) is not int for value in result['fact_ids']):
        raise AnalysisError('AI вернул неверные ссылки на источники')
    return result
