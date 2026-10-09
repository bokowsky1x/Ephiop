from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import math
import random

from openai import APIConnectionError, APIStatusError, APITimeoutError
from telethon import errors

from models import AIDecision, db

MAX_ATTEMPTS = 3
MAX_AGE = timedelta(minutes=10)


def server_delay(response):
    raw = response.headers.get('Retry-After')
    if not raw:
        return 0
    try:
        delay = float(raw)
    except ValueError:
        try:
            delay = (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 0
    return delay if math.isfinite(delay) and delay > 0 else 0


def backoff(attempt, minimum=0):
    if minimum >= MAX_AGE.total_seconds():
        return None
    return math.ceil(max(10 * 2 ** max(0, attempt - 1), minimum)) + random.randint(0, 3)


def retry_delay(exc, attempt):
    """Only retry temporary read/analysis failures, never Telegram mutations."""
    current, seen = exc, set()
    while current and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, errors.FloodWaitError):
            return max(1, current.seconds)
        if isinstance(current, (TimeoutError, ConnectionError, APIConnectionError, APITimeoutError)):
            return backoff(attempt)
        if isinstance(current, (errors.ServerError, errors.TimedOutError, errors.RpcCallFailError)):
            return backoff(attempt)
        if isinstance(current, APIStatusError):
            if (current.status_code == 429 and current.code in ('rate_limit_exceeded', 'slow_down')) or current.status_code in (408, 409) or current.status_code >= 500:
                return backoff(attempt, server_delay(current.response))
        current = current.__cause__
    return None


def recover_analysis():
    now = datetime.utcnow()
    AIDecision.query.filter_by(state='ANALYZING').update({
        'state': 'QUEUED', 'next_analysis_at': now,
        'result': 'Анализ прерван перезапуском; ожидает повторной проверки сообщения'}, synchronize_session=False)


def queue_status(agent_id):
    queued = AIDecision.query.filter_by(agent_id=agent_id, state='QUEUED')
    return dict(waiting=queued.count(), analyzing=AIDecision.query.filter_by(agent_id=agent_id, state='ANALYZING').count(),
                retrying=queued.filter(AIDecision.analysis_attempts > 0).count(),
                next_at=db.session.query(db.func.min(AIDecision.next_analysis_at)).filter(
                    AIDecision.agent_id == agent_id, AIDecision.state == 'QUEUED').scalar())
