import json
import unicodedata

from openai import APIError, AsyncOpenAI, AuthenticationError, RateLimitError


def text_units(text):
    return len(text.encode('utf-16-le')) // 2


def validate_changes(changes, about_limit=70):
    if 'first_name' in changes:
        if not changes['first_name'].strip() or text_units(changes['first_name']) > 64:
            raise ValueError('Имя должно содержать от 1 до 64 символов')
        if text_units(changes['last_name']) > 64:
            raise ValueError('Фамилия должна быть не длиннее 64 символов')
    if 'about' in changes and text_units(changes['about']) > about_limit:
        raise ValueError(f'Описание вместе со ссылкой должно быть не длиннее {about_limit} символов')


async def generate_profiles(app, count, name_prompt='', about_prompt='', about_limit=70):
    if not app.config.get('OPENAI_API_KEY'):
        raise ValueError('На сервере не настроен OPENAI_API_KEY')
    schema = {
        'type': 'object', 'additionalProperties': False, 'required': ['profiles'],
        'properties': {'profiles': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['first_name', 'last_name', 'about'],
            'properties': {field: {'type': 'string'} for field in ('first_name', 'last_name', 'about')},
        }}},
    }
    instructions = (
        'Generate exactly the requested number of Telegram profile suggestions. '
        'For names, use distinct fictional names representative of the requested language, '
        'country or community. Do not impersonate specific real people. '
        'First and last names must each be at most 64 UTF-16 code units. '
        'For bios, follow the requested style without inventing personal facts. '
        'Do not add links: any requested link is appended separately by the application. '
        'Fields whose prompt is empty must be empty strings. '
        f'Each bio must fit within {about_limit} UTF-16 code units.'
    )
    try:
        async with AsyncOpenAI(api_key=app.config['OPENAI_API_KEY'], timeout=50, max_retries=0) as client:
            options = {}
            if app.config['OPENAI_MODEL'].startswith('gpt-6'):
                options['reasoning'] = {'effort': 'none'}
            response = await client.responses.create(
                model=app.config['OPENAI_MODEL'], instructions=instructions,
                input=json.dumps(dict(count=count, name_prompt=name_prompt, about_prompt=about_prompt), ensure_ascii=False),
                text={'format': {'type': 'json_schema', 'name': 'profile_suggestions', 'strict': True, 'schema': schema}},
                max_output_tokens=max(1000, count * 220), store=False, **options,
            )
            if response.status != 'completed':
                raise ValueError('AI не завершил генерацию. Уменьшите выборку или измените промпт')
            result = json.loads(response.output_text)
            profiles = result['profiles']
            if not isinstance(profiles, list) or len(profiles) != count:
                raise ValueError('AI вернул неверное количество профилей')
            names = set()
            for profile in profiles:
                if not isinstance(profile, dict) or set(profile) != {'first_name', 'last_name', 'about'}:
                    raise ValueError('AI вернул неверный формат профиля')
                if not all(isinstance(value, str) for value in profile.values()):
                    raise ValueError('AI вернул неверный формат текста')
                profile.update({key: value.strip() for key, value in profile.items()})
                changes = {}
                if name_prompt:
                    changes.update(first_name=profile['first_name'], last_name=profile['last_name'])
                    key = unicodedata.normalize('NFKC', ' '.join((profile['first_name'], profile['last_name']))).casefold()
                    if key in names:
                        raise ValueError('AI повторил имя. Измените промпт и повторите предпросмотр')
                    names.add(key)
                if about_prompt:
                    if not profile['about']:
                        raise ValueError('AI вернул пустое описание')
                    changes['about'] = profile['about']
                validate_changes(changes, about_limit)
            return profiles
    except AuthenticationError as exc:
        raise ValueError('OpenAI отклонил ключ API. Проверьте OPENAI_API_KEY') from exc
    except RateLimitError as exc:
        raise ValueError('OpenAI: исчерпан лимит или баланс API') from exc
    except APIError as exc:
        raise ValueError('OpenAI не ответил. Проверьте соединение и OPENAI_MODEL') from exc
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('AI вернул неверный формат. Измените промпт и повторите предпросмотр') from exc
