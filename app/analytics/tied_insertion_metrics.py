"""Reuse metric aggregates only for an append-equivalent terminal tie."""
from app.analytics.conversation_pages import PAGE_RECORDS


def tied_suffix_metrics(previous, suffix, added, check):
    """Return exact metrics or None for the ordinary full-calculation fallback.

    A same-direction terminal run at one timestamp preserves every response
    sample, turn boundary and silence gap when one message joins that run.
    Requiring the same score throughout the shifted suffix also preserves the
    floating-point summation order: insertion and append yield identical score
    sequences. The existing append calculation refuses ambiguous rounding.

    The caller must first prove that the prefix is unchanged and the suffix
    differs only by this insertion and its required ordinal shifts. This is a
    metric calculation, not source, generation or publication authority.
    """
    from app.analytics.metrics import append_conversation_metrics

    if (not suffix or len(suffix) > PAGE_RECORDS or previous.message_count <= 0
            or added.source_ordinal != previous.message_count-len(suffix)
            or added.sent_at != previous.ended_at
            or added.account_ref != previous.account_ref
            or added.conversation_ref != previous.conversation_ref
            or added.participant_ref != previous.participant_ref):
        return None
    for offset, value in enumerate(suffix, start=added.source_ordinal+1):
        check()
        if (value.source_ordinal != offset or value.sent_at != added.sent_at
                or value.direction != added.direction
                or value.sentiment.score != added.sentiment.score
                or value.account_ref != previous.account_ref
                or value.conversation_ref != previous.conversation_ref
                or value.participant_ref != previous.participant_ref):
            return None
    check()
    # No sum callback: uncertainty must fall back to the original insertion
    # order, rather than recomputing an appended sum in a different order.
    return append_conversation_metrics(previous, suffix[-1], added,
                                       previous.unread_count)
