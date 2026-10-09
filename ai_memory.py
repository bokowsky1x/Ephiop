import asyncio
from datetime import datetime, timedelta
import hashlib
import math
from types import SimpleNamespace

from openai import AsyncOpenAI

from ai_engine import AnalysisError, redact
from ai_examples import example_payload, example_rows, word_rank
from models import AIAgent, AIDecision, AIExample, db

MODEL = 'text-embedding-3-small'
DIMENSIONS = 256
TEXT_LIMIT = 800


def memory_text(text):
    text = redact(text, TEXT_LIMIT).strip()
    if text in ('[PRIVATE MESSAGE]', '[PRIVATE]') or not any(char.isalpha() for char in text.replace('[PRIVATE]', '')):
        return ''
    return text


def content_hash(text):
    return hashlib.sha256(f'{MODEL}:{DIMENSIONS}:{text}'.encode('utf-8')).hexdigest()


def vector(value):
    if not isinstance(value, list) or len(value) != DIMENSIONS or any(
            type(item) not in (int, float) or abs(item) > 1 or not math.isfinite(item) for item in value):
        return None
    norm = math.sqrt(sum(item * item for item in value))
    return [item / norm for item in value] if norm > 0 else None


def cached(row, text):
    return vector(row.embedding) if text and row.embedding_hash == content_hash(text) else None


def similarity(left, right):
    return sum(a * b for a, b in zip(left, right))


def memory_snapshot(agent_id, exclude_id=None):
    return [dict(id=row.id, agent_id=row.agent_id, created_at=row.created_at, text=row.text,
                 reply_context=row.reply_context, expected=dict(row.expected), corrected_reply=row.corrected_reply,
                 guidance=row.guidance, image=bool(row.image), embedding=row.embedding, embedding_hash=row.embedding_hash)
            for row in example_rows(agent_id, exclude_id)]


async def embed(app, texts):
    if not app.config.get('OPENAI_API_KEY'):
        raise AnalysisError('Для смысловой памяти не настроен OPENAI_API_KEY')
    async with AsyncOpenAI(api_key=app.config['OPENAI_API_KEY'], timeout=8, max_retries=0) as client:
        response = await client.embeddings.create(model=MODEL, input=texts, dimensions=DIMENSIONS, encoding_format='float')
    if response.model != MODEL or len(response.data) != len(texts):
        raise AnalysisError('Неверный формат смысловой памяти')
    results = {}
    for item in response.data:
        value = vector(item.embedding)
        if type(item.index) is not int or item.index not in range(len(texts)) or item.index in results or value is None:
            raise AnalysisError('Неверный формат смысловой памяти')
        results[item.index] = value
    return [results[index] for index in range(len(texts))]


def current_agent(agent_id, created_at, revision):
    db.session.expire_all()
    return AIAgent.query.filter_by(id=agent_id, created_at=created_at, revision=revision,
                                  consent=True, semantic_memory=True).first()


async def select_memory(app, agent, message, decision=None, exclude_id=None, snapshot=None):
    # Copies keep replay inputs and ranking stable while an API request is in flight.
    rows = [SimpleNamespace(**item) for item in (snapshot if snapshot is not None else memory_snapshot(agent.id, exclude_id))]
    rows = [row for row in rows if row.agent_id == agent.id and row.id != exclude_id][:50]
    fallback = word_rank(rows, message)
    result = dict(examples=example_payload(fallback), ids=[row.id for row in fallback], method='words')
    if not agent.semantic_memory or not agent.consent:
        return result
    agent_id, created_at, revision = agent.id, agent.created_at, agent.revision
    text = memory_text(message)
    if not text or (agent.memory_retry_at and agent.memory_retry_at > datetime.utcnow()):
        return result
    query = cached(decision, text) if decision else None
    save_query = bool(decision and not decision.has_image and not decision.has_reply)
    query_stamp = (decision.id, decision.created_at, decision.fingerprint, decision.analysis_started_at) if save_query else None
    eligible = [row for row in rows if memory_text(row.text)]
    if not eligible and not save_query:
        return result
    known, missing = {}, {}
    for row in eligible:
        body = memory_text(row.text)
        key = content_hash(body)
        value = cached(row, body)
        if value is not None:
            known[key] = value
        else:
            missing[key] = body
    query_key = content_hash(text)
    if query is not None:
        known[query_key] = query
    elif query_key not in known:
        missing[query_key] = text
    missing = {key: body for key, body in missing.items() if key not in known}
    try:
        if not current_agent(agent_id, created_at, revision):
            return result
        if missing:
            values = await asyncio.wait_for(embed(app, list(missing.values())), 10)
            values = [vector(value) for value in values]
            if len(values) != len(missing) or any(value is None for value in values):
                raise AnalysisError('Неверный формат смысловой памяти')
            known.update(zip(missing, values))
        if not current_agent(agent_id, created_at, revision):
            return result
        # The write claim also serializes settings changes with cache persistence.
        claimed = AIAgent.query.filter_by(id=agent_id, created_at=created_at, revision=revision,
            consent=True, semantic_memory=True).update({'memory_error': '', 'memory_retry_at': None}, synchronize_session='fetch')
        if not claimed:
            db.session.rollback()
            return result
        for row in eligible:
            key = content_hash(memory_text(row.text))
            if cached(row, memory_text(row.text)) is None:
                AIExample.query.filter_by(id=row.id, agent_id=agent_id, created_at=row.created_at,
                    approved=True, text=row.text).update({'embedding': known[key], 'embedding_hash': key}, synchronize_session=False)
        if save_query:
            decision_id, decision_created_at, fingerprint, started = query_stamp
            AIDecision.query.filter_by(id=decision_id, agent_id=agent_id, created_at=decision_created_at,
                fingerprint=fingerprint, agent_revision=revision, state='ANALYZING', analysis_started_at=started).update(
                    {'embedding': known[query_key], 'embedding_hash': query_key}, synchronize_session='fetch')
        db.session.commit()
        query = known[query_key]
        ranked = sorted(eligible, key=lambda row: (similarity(query, known[content_hash(memory_text(row.text))]), row.id), reverse=True)[:5]
        # Similarity only retrieves context; it is never an action or confidence score.
        return dict(examples=example_payload(ranked), ids=[row.id for row in ranked], method='semantic')
    except Exception as exc:
        db.session.rollback()
        code = getattr(exc, 'code', None)
        reason = {'insufficient_quota': 'Баланс или лимит OpenAI API', 'invalid_api_key': 'Недействительный API-ключ',
                  'model_not_found': 'Нет доступа к модели embeddings', 'rate_limit_exceeded': 'Лимит запросов OpenAI API'}.get(
                      code if isinstance(code, str) else '', 'Ошибка соединения или формата embeddings')
        AIAgent.query.filter_by(id=agent_id, created_at=created_at, revision=revision, semantic_memory=True).update(
            {'memory_error': reason + '. Используется поиск по словам; следующая попытка не раньше чем через 10 минут.',
             'memory_retry_at': datetime.utcnow() + timedelta(minutes=10)}, synchronize_session='fetch')
        db.session.commit()
        return result
