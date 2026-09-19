"""Store bounded conversation pages inside witnessed analytics generations."""

import hashlib

from app.analytics.cancellation import check_cancelled
from app.analytics.conversation_pages import (ConversationPageHeader, ConversationPage,
    PagedConversation, PAGE_BYTES, MAX_PAGES)


def supported(connection):
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_page_sets'").fetchone() is not None


def load_pages(connection, generation_id, account, conversation, input_digest,
               config_digest, *, cancellation_check=None):
    check_cancelled(cancellation_check)
    row = connection.execute('''SELECT header_json,header_digest FROM conversation_page_sets
        WHERE generation_id=? AND creator_account_id=? AND conversation_ref=?
          AND input_digest=? AND config_digest=? AND length(CAST(header_json AS BLOB))<=65536''',
        (generation_id, account, conversation, input_digest, config_digest)).fetchone()
    if row is None or not isinstance(row['header_json'], str):
        return None
    data = row['header_json'].encode('utf-8')
    if hashlib.sha256(data).hexdigest() != row['header_digest']:
        return None
    try:
        header = ConversationPageHeader.model_validate_json(data)
    except ValueError:
        return None
    if (header.account_ref != account or header.conversation_ref != conversation
            or header.input_digest != input_digest or header.config_digest != config_digest):
        return None
    pages, used = [], 0
    rows = connection.execute('''SELECT ordinal,kind,data FROM conversation_pages
        WHERE generation_id=? AND creator_account_id=? AND conversation_ref=?
          AND length(data)<=? ORDER BY ordinal LIMIT ?''',
        (generation_id, account, conversation, PAGE_BYTES, MAX_PAGES + 1))
    for row in rows:
        check_cancelled(cancellation_check)
        if row['ordinal'] != len(pages) or len(pages) >= header.page_count:
            return None
        if not isinstance(row['data'], bytes):
            return None
        raw = row['data']; used += len(raw)
        if used > header.byte_count:
            return None
        pages.append(ConversationPage(row['kind'], raw))
    if len(pages) != header.page_count or used != header.byte_count:
        return None
    return PagedConversation(header, tuple(pages))


def insert_page_sets(connection, generation_id, page_sets, *, check):
    for packed in page_sets:
        check(); h = packed.header; data = h.model_dump_json()
        connection.execute('''INSERT INTO conversation_page_sets
            (generation_id,creator_account_id,conversation_ref,input_digest,config_digest,header_json,header_digest)
            VALUES (?,?,?,?,?,?,?)''',
            (generation_id, h.account_ref, h.conversation_ref, h.input_digest,
             h.config_digest, data, hashlib.sha256(data.encode()).hexdigest()))
        for index, page in enumerate(packed.pages):
            check()
            connection.execute('''INSERT INTO conversation_pages
                (generation_id,creator_account_id,conversation_ref,ordinal,kind,data)
                VALUES (?,?,?,?,?,?)''',
                (generation_id, h.account_ref, h.conversation_ref, index, page.kind, page.data))
