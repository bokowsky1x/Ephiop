from ai_memory import TEXT_LIMIT, cached, memory_text, similarity

FINISHED = ('QUEUED', 'ANALYZING', 'RUNNING', 'SENT', 'DELETED', 'BANNED', 'DELETED_BANNED', 'PARTIAL', 'UNCERTAIN')


def groupable(row, record):
    return bool(record and record.fingerprint == row.fingerprint and not row.has_image and not row.has_reply
                and row.classification in ('SAFE', 'UNKNOWN') and row.state not in FINISHED
                and '[PRIVATE' not in record.text and len(record.text.strip()) >= 12)


def alike(left, right, records, semantic):
    a, b = records.get(left.message_id), records.get(right.message_id)
    if not groupable(left, a) or not groupable(right, b) or any(
            getattr(left, key) != getattr(right, key) for key in ('language', 'intent', 'classification', 'action')):
        return False
    if ' '.join(a.text.split()).casefold() == ' '.join(b.text.split()).casefold():
        return True
    if not semantic or len(a.text) > TEXT_LIMIT or len(b.text) > TEXT_LIMIT:
        return False
    av, bv = cached(left, memory_text(a.text)), cached(right, memory_text(b.text))
    return av is not None and bv is not None and similarity(av, bv) >= 0.95


def question_groups(pending, records, semantic=False):
    groups = []
    for row in pending:
        for group in groups:
            # Complete-link grouping prevents chains of progressively unrelated questions.
            if all(alike(row, member, records, semantic) for member in group):
                group.append(row)
                break
        else:
            groups.append([row])
    return groups
