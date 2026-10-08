"""Bounded admission of unchanged, independently verified enrichment bytes."""
from datetime import datetime
import json
import re

BLOCK_RECORDS = 256
BLOCK_BYTES = 1024 * 1024
_ORDINAL = re.compile(rb'"source_ordinal":(0|[1-9][0-9]*)(?=[,}])')


def ordinary_record_fields(raw, ordinal):
    """Retain the original strict ordinal and timestamp-encoding rule."""
    token = b'"source_ordinal":' + str(ordinal).encode('ascii')
    if (b'\\' not in raw and raw.count(b'"source_ordinal"') == 1
            and raw.count(b'"sent_at"') == 1 and b'"sent_at":"' in raw
            and (token+b',' in raw or token+b'}' in raw)):
        return True
    value = json.loads(raw)
    return (type(value['source_ordinal']) is int and value['source_ordinal'] == ordinal
            and isinstance(value['sent_at'], str))


def _ordinary_block(rows, start, check):
    check()
    frame = b'\n'.join(rows)
    count = len(rows)
    if (b'\\' not in frame and frame.count(b'"source_ordinal"') == count
            and frame.count(b'"sent_at"') == count
            and frame.count(b'"sent_at":"') == count):
        seen, ordered = 0, True
        for match in _ORDINAL.finditer(frame):
            ordered = ordered and int(match[1]) == start + seen
            seen += 1
        if seen == count:
            check()
            return ordered
    # Escapes, alternate formatting, duplicate fields or legal legacy encodings
    # use exactly the existing per-record admission, not a broader parser rule.
    for ordinal, row in enumerate(rows, start):
        check()
        if not ordinary_record_fields(row, ordinal):
            return False
    return True


def ordinary_records(rows, *, start=0, check=lambda: None):
    """Check already proved JSON rows with bounded temporary byte storage."""
    pending, size, ordinal = [], 0, start
    for row in rows:
        if pending and (len(pending) >= BLOCK_RECORDS or size+len(row)+1 > BLOCK_BYTES):
            if not _ordinary_block(pending, ordinal-len(pending), check):
                return False
            pending, size = [], 0
        if len(row) > BLOCK_BYTES:
            check()
            if not ordinary_record_fields(row, ordinal):
                return False
        else:
            pending.append(row)
            size += len(row)+1
        ordinal += 1
    return not pending or _ordinary_block(pending, ordinal-len(pending), check)


def match_source(account, raw, previous, rows, check, *, limit):
    """Match a current source to an independently source-bound predecessor.

    Production callers must obtain rows through the live reader's exact
    verified-header/actual-unit binding. A copied header is not authority.
    This extends the existing append rule: the complete old input digest proves
    unchanged source, while actual digest-checked enrichment bytes are reused.
    """
    from app.analytics.conversation_enrichment_units import MAX_ENRICHMENT_UNIT_RECORDS, _canonical
    from app.analytics.opaque_refs import account_ref, conversation_ref, participant_ref, message_ref
    from app.analytics.source_snapshot import conversation_digest
    messages = raw['messages']
    count = previous.message_count
    if (not rows or len(rows) != count or len(messages) != count+1
            or len(messages) > MAX_ENRICHMENT_UNIT_RECORDS or limit <= 0
            or previous.account_ref != account_ref(account)
            or previous.conversation_ref != conversation_ref(account, raw['conversation_id'])
            or previous.metrics.participant_ref != participant_ref(account, raw['platform_user_id'])
            or previous.metrics.unread_count != raw['unread_count']):
        return None
    start = max(0, count-limit)
    if not ordinary_records(rows[:start], check=check):
        return None
    for ordinal, message in enumerate(messages[:start]):
        if ordinal % BLOCK_RECORDS == 0: check()
        if (type(message['source_ordinal']) is not int or message['source_ordinal'] != ordinal
                or not isinstance(message['sent_at'], str)):
            return None
    prior, output = list(messages[:start]), list(rows[:start])
    insertion, cursor, seen = None, start, set()
    for ordinal in range(start, count):
        check()
        value = json.loads(rows[ordinal])
        reference = value['message_ref']
        if (reference in seen or type(value['source_ordinal']) is not int
                or value['source_ordinal'] != ordinal):
            return None
        seen.add(reference)
        selected = messages[cursor]
        selected_ref = message_ref(account, raw['conversation_id'], selected['message_id'])
        if selected_ref != reference:
            if insertion is not None:
                return None
            insertion = cursor
            output.append(None)
            cursor += 1
            selected = messages[cursor]
            selected_ref = message_ref(account, raw['conversation_id'], selected['message_id'])
        if (selected_ref != reference or type(selected['source_ordinal']) is not int
                or selected['source_ordinal'] != cursor
                or value['account_ref'] != previous.account_ref
                or value['conversation_ref'] != previous.conversation_ref
                or value['participant_ref'] != previous.metrics.participant_ref
                or not isinstance(value['sent_at'], str)
                or datetime.fromisoformat(value['sent_at']) != datetime.fromisoformat(selected['sent_at'])
                or value['direction'] != selected['direction']):
            return None
        prior.append(selected if cursor == ordinal else dict(selected, source_ordinal=ordinal))
        output.append(rows[ordinal] if cursor == ordinal else _canonical(dict(value, source_ordinal=cursor)))
        cursor += 1
    if insertion is None or cursor != len(messages) or len(messages)-insertion-1 > limit:
        return None
    added = messages[insertion]
    if (type(added['source_ordinal']) is not int or added['source_ordinal'] != insertion
            or message_ref(account, raw['conversation_id'], added['message_id']) in seen
            or datetime.fromisoformat(added['sent_at']) != datetime.fromisoformat(messages[insertion+1]['sent_at'])):
        return None
    # The full source digest below binds every unchanged identity and field.
    # Also retain explicit rejection of a duplicated added source identity.
    for index, message in enumerate(prior):
        if index % BLOCK_RECORDS == 0: check()
        if message['message_id'] == added['message_id']:
            return None
    old_raw = dict(raw, messages=prior, last_message_at=prior[-1]['sent_at'])
    check()
    if conversation_digest(old_raw) != previous.input_digest:
        return None
    check()
    return insertion, old_raw, output, None
