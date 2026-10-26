"""Bounded exact UTC sampling from the live evidence predicate."""
from datetime import timedelta, timezone
import hashlib
import heapq

from app.analytics.canonical_source import HistoryAnalyticsSource, read_transaction_evidence
from app.analytics.nlp_findings_store import source_binding
from app.analytics.source_snapshot import scan_identity
from app.analytics.query_sql import bounded_sql
from app.models.knowledge_contributions import InputIdentity, SourceMemberSpec, inventory_digest
from app.models.text_analysis_preview_execution import PreviewScope, PREVIEW_MESSAGE_CAP


def freeze_preview_scope(repository, source, account, cutoff, budget):
    """Freeze metadata only; a failed complete scan never returns a smaller sample."""
    if not isinstance(source, HistoryAnalyticsSource) or source.history.database is not repository.database:
        raise ValueError('preview_source_owner_invalid')
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError('preview_cutoff_invalid')
    cutoff = cutoff.astimezone(timezone.utc)
    lower = cutoff - timedelta(days=90)
    heap = []
    with repository.database.read() as db, bounded_sql(db, budget):
        db.execute('BEGIN')
        head = db.execute('SELECT canonical_revision FROM account_heads WHERE creator_account_id=?', (account,)).fetchone()
        if head is None:
            raise ValueError('preview_account_unavailable')
        revision = int(head[0])
        identity, _, _ = scan_identity(db, account, revision, check=budget.check, consume=budget.consume)
        scoped = HistoryAnalyticsSource(source.history, connection=db)
        after = ('', '')
        while True:
            budget.check()
            rows = scoped.nlp_location_page(account, after, limit=64)
            if not rows:
                break
            for row in rows:
                budget.consume()
                after = (row.conversation_id, row.message_id)
                evidence = read_transaction_evidence(db, account,
                    row.model_dump(mode='python'), budget)
                if evidence is None or not lower < evidence.sent_at <= cutoff:
                    continue
                key = (evidence.sent_at, evidence.location.message_id)
                member = SourceMemberSpec(ordinal=0, location=evidence.location.model_dump(),
                    source=source_binding(evidence), source_revision=revision,
                    text_sha256=hashlib.sha256(evidence.text.encode('utf-8')).hexdigest())
                if len(heap) < PREVIEW_MESSAGE_CAP:
                    heapq.heappush(heap, (key, member))
                elif key > heap[0][0]:
                    heapq.heapreplace(heap, (key, member))
        budget.check()
    ordered = sorted(heap, key=lambda entry: entry[0])
    members = tuple(member.model_copy(update={'ordinal': ordinal}) for ordinal, (_, member) in enumerate(ordered))
    digest = inventory_digest(members)
    scope = PreviewScope(cutoff=cutoff, lower_exclusive=lower, source_count=len(members),
        earliest=ordered[0][0][0] if ordered else None, latest=ordered[-1][0][0] if ordered else None,
        inventory_sha256=digest)
    return scope, InputIdentity(canonical_revision=revision,
        canonical_content_sha256=identity.content_digest.removeprefix('sha256:'),
        inventory_sha256=digest, source_count=len(members)), members
