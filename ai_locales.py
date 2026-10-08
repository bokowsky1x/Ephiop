import re
from urllib.parse import urlsplit


LANGUAGE_LABELS = {
    'AMHARIC': 'Амхарский', 'AMHARIC_LATIN': 'Амхарский латиницей', 'OROMO': 'Оромо',
    'ENGLISH': 'Английский', 'RUSSIAN': 'Русский', 'UKRAINIAN': 'Украинский',
    'SPANISH': 'Испанский', 'PORTUGUESE': 'Португальский', 'FRENCH': 'Французский',
    'ARABIC': 'Арабский', 'HINDI': 'Хинди', 'CHINESE': 'Китайский', 'TURKISH': 'Турецкий',
    'INDONESIAN': 'Индонезийский', 'CUSTOM': 'Другой язык',
}
NOTICE_LANGUAGES = {'AMHARIC', 'AMHARIC_LATIN', 'OROMO', 'ENGLISH', 'RUSSIAN'}


def has_language_hint(text):
    return any(char.isalpha() for char in str(text or '').replace('[PRIVATE]', ''))


def parse_glossary(text):
    if len(text) > 4000:
        raise ValueError('Словарь: максимум 4000 символов')
    result = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        term, separator, meaning = line.partition('=')
        term, meaning = term.strip(), meaning.strip()
        if not separator or not term or not meaning or len(term) > 80 or len(meaning) > 200:
            raise ValueError('Словарь: одна строка «выражение = значение», до 80 / 200 символов')
        result[term] = meaning
    if len(result) > 30:
        raise ValueError('Словарь: максимум 30 выражений')
    return result


def valid_contact(value):
    if not value:
        return True
    if re.fullmatch(r'[A-Za-z0-9.+_-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', value):
        return True
    parsed = urlsplit(value)
    return bool(parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password
                and not re.search(r'\s|[<>]', value))


def locale_payload(agent):
    return dict(language_profile=agent.language_profile, custom_language=agent.custom_language,
                language_instructions=agent.language_instructions, glossary=parse_glossary(agent.glossary or ''))


def custom_notice(agent, language, field):
    if agent and language == agent.fallback_language:
        return getattr(agent, field, '') or ''
    return ''
