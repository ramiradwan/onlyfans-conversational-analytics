"""Store bounded conversation pages inside witnessed analytics generations."""

import hashlib

from app.analytics.cancellation import check_cancelled
from app.analytics.conversation_pages import (ConversationPageHeader, ConversationPage,
    PagedConversation, ConversationPageReference, PAGE_BYTES, MAX_PAGES)


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
    return PagedConversation(header, tuple(pages), generation_id)


def insert_page_sets(connection, generation_id, page_sets, *, check):
    if not connection.in_transaction:
        raise ValueError('conversation_pages_require_staging_transaction')
    shared = shared_pages_supported(connection)
    for packed in page_sets:
        check(); h = packed.header; data = h.model_dump_json()
        connection.execute('''INSERT INTO conversation_page_sets
            (generation_id,creator_account_id,conversation_ref,input_digest,config_digest,header_json,header_digest)
            VALUES (?,?,?,?,?,?,?)''',
            (generation_id, h.account_ref, h.conversation_ref, h.input_digest,
             h.config_digest, data, hashlib.sha256(data.encode()).hexdigest()))
        existing = existing_page_content(connection, h.account_ref, packed.pages, check) if shared else None
        for index, page in enumerate(packed.pages):
            check()
            if existing is not None:
                insert_shared_page(connection, generation_id, h, index, page, existing=existing, check=check)
                continue
            connection.execute('''INSERT INTO conversation_pages
                (generation_id,creator_account_id,conversation_ref,ordinal,kind,data)
                VALUES (?,?,?,?,?,?)''',
                (generation_id, h.account_ref, h.conversation_ref, index, page.kind, page.data))


def shared_pages_supported(connection):
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_page_refs'").fetchone() is not None


def page_content_id(page):
    return hashlib.sha256(b'conversation-page-content.v1\0' + page.kind.encode('ascii')
                          + b'\0' + page.data).hexdigest()


def insert_shared_page(connection, generation_id, header, ordinal, page, *, existing, check):
    """Write membership after checking existing bytes in the same transaction."""

    signature = page_content_id(page)
    check()
    if signature not in existing:
        connection.execute('INSERT INTO conversation_page_content(creator_account_id,content_id,kind,data) VALUES(?,?,?,?)',
                           (header.account_ref, signature, page.kind, page.data))
        existing.add(signature)
    connection.execute("""INSERT INTO conversation_page_refs
        (generation_id,creator_account_id,conversation_ref,ordinal,content_id) VALUES(?,?,?,?,?)""",
        (generation_id, header.account_ref, header.conversation_ref, ordinal, signature))


def existing_page_content(connection, account, pages, check):
    """Compare actual content once; damaged shared pages require an owned replacement."""

    existing = set()
    for page in pages:
        check()
        signature = page_content_id(page)
        row = connection.execute(
            'SELECT kind,data FROM conversation_page_content WHERE creator_account_id=? AND content_id=?',
            (account, signature)).fetchone()
        if row is not None:
            if row['kind'] != page.kind or row['data'] != page.data:
                return None
            existing.add(signature)
    return existing


def resolve_page_sets(connection, store, account_id, page_sets, *, check):
    """Resolve previously read page sets again in the staging transaction."""

    from app.analytics.opaque_refs import account_ref

    account = account_ref(account_id)
    for value in page_sets:
        check()
        if not isinstance(value, ConversationPageReference):
            yield value
            continue
        header = value.header
        generation = connection.execute(
            "SELECT * FROM projection_generations WHERE generation_id=? AND creator_account_id=? AND status='active' AND activated_at IS NOT NULL",
            (value.generation_id, account)).fetchone()
        witness = None if generation is None else store.activation.get(value.generation_id)
        if (header.account_ref != account
                or not store._intent_matches(generation, witness, require_completed=True)
                or witness.creator_account_id != account_id):
            raise ValueError('conversation_page_reference_unavailable')
        packed = load_pages(connection, value.generation_id, account, header.conversation_ref,
                            header.input_digest, header.config_digest, cancellation_check=check)
        if packed is None or packed.header != header:
            raise ValueError('conversation_page_reference_changed')
        yield packed
