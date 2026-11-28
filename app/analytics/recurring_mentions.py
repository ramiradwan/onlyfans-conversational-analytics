"""Read recurring phrases from a saved, authenticated findings publication."""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from app.analytics.errors import InvalidAnalyticsRequest, ProjectionUnavailable
from app.analytics.nlp_findings_store import FrozenFindings, digest, encoded, source_binding
from app.analytics.nlp_literal_read import _assert_current, _page, index_literal_publication
from app.analytics.nlp_read_projection import _effective, _inventory, _summarize
from app.analytics.validation_receipt import content_stamp
from app.models.analytics import AnalyticsWindow, WindowScope
from app.models.question_evidence import SourceSpan
from app.models.recurring_mentions import MentionCoverage, RecurringMentionsReport, RecurringMentionsRequest
from app.nlp.profiles import read_profile


def _incoming(scope, sources, start, cutoff, budget):
    from app.analytics.canonical_source import read_incoming_participant_evidence

    candidates = []
    for item, reference, location in sources:
        budget.check()
        if item["direction"] == "inbound" and start < reference.sent_at <= cutoff:
            candidates.append((item, reference, location))
    selected = []
    for offset in range(0, len(candidates), 64):
        batch = candidates[offset:offset + 64]
        live = read_incoming_participant_evidence(
            scope.connection, scope.account, [location for _, _, location in batch], budget
        )
        for item, reference, location in batch:
            message = live.get((location.conversation_id, location.message_id))
            if message is None:
                continue
            if source_binding(message) != item["source"] or message.sent_at != reference.sent_at:
                raise ProjectionUnavailable(availability="building")
            selected.append((item, reference, location))
    return selected


@dataclass
class _Phrase:
    senders: set = field(default_factory=set)
    messages: set = field(default_factory=set)
    evidence: dict = field(default_factory=dict)


def _recurrences(publication, records, selected, budget):
    nodes, states = index_literal_publication(publication, records, budget)
    population = {row[1].message_ref: row for row in selected if row[0]["record_digest"] is not None}
    unmapped = sum(states[item["record_digest"]].unrepresentable for item, _, _ in population.values())
    groups = defaultdict(_Phrase)
    for mapping in publication.mappings:
        budget.check()
        anchor = mapping.occurrence
        original = population.get(anchor.source.source_ref)
        if original is None:
            continue
        item, reference, location = original
        record_id = item["record_digest"]
        state = states[record_id]
        node = nodes.get(mapping.node_id)
        if (anchor.source != item["source"] or mapping.extraction.extraction_record_sha256 != record_id
                or state.outcome not in ("assessed_findings", "partial")
                or node is None or node.type_id != mapping.type_id):
            raise ValueError("recurring_mention_source_invalid")
        group = groups[node.literal_key]
        event = (reference.message_ref, anchor.span.start, anchor.span.end)
        # One span can be tagged under more than one taxonomy category.
        if event not in group.evidence:
            group.senders.add(anchor.source.speaker_ref)
            group.messages.add(reference.message_ref)
            exact = reference.model_copy(update={"span": SourceSpan(start=anchor.span.start, end=anchor.span.end)})
            assessment = "partial" if state.outcome == "partial" else "complete"
            group.evidence[event] = (assessment, exact, location)
    ranked = [(key, group) for key, group in groups.items() if len(group.senders) >= 2]
    ranked.sort(key=lambda pair: (-len(pair[1].senders), -len(pair[1].messages), pair[0]))
    return ranked, unmapped


def aggregate(value, records, generation, snapshot, request, budget, source_rows, *, connection, canonical_scope):
    from app.analytics.nlp_literals import LiteralUnavailable, checked_publication

    profile = read_profile(encoded(value["snapshot"]["profile"]))
    inventory, labels, sources = _inventory(value, records, profile, budget, source_rows)
    cutoff = snapshot.derived_at
    start = cutoff - timedelta(days=90)
    selected = _incoming(canonical_scope, sources, start, cutoff, budget)
    tasks, _ = _summarize(profile, inventory, labels, selected, records, budget, with_evidence=False)
    extraction = next((task for task in tasks if task["task"] == "concept_mentions"), None)
    binding = FrozenFindings(snapshot.account_ref, digest(value))
    common = dict(account_ref=snapshot.account_ref, source_revision=snapshot.source_revision,
                  projection_generation=snapshot.projection_generation, projection_digest=snapshot.projection_digest,
                  snapshot_sha256="sha256:" + binding.snapshot_id, phrase_id=request.phrase_id,
                  requested_window=AnalyticsWindow(scope=WindowScope.REQUESTED, start=start, end=cutoff),
                  effective_window=_effective(selected))
    try:
        if extraction is None:
            raise LiteralUnavailable("task_not_declared")
        publication = checked_publication(connection, binding, check=budget.check)
    except LiteralUnavailable as error:
        if request.expected_normalization_sha256 is not None:
            raise ProjectionUnavailable(availability="building") from None
        return RecurringMentionsReport(**common, availability="unavailable", unavailable_reason=error.reason,
            coverage=None, normalization_sha256=None, row_page=_page([], 0, request.row_limit),
            evidence_page=_page([], 0, request.evidence_limit)), []

    normalization = "sha256:" + publication.publication_id
    if request.expected_normalization_sha256 not in (None, normalization):
        raise ProjectionUnavailable(availability="building")
    ranked, unmapped = _recurrences(publication, records, selected, budget)
    rows, evidence = [], []
    for literal, group in ranked:
        budget.check()
        phrase_id = "sha256:" + digest(["recurring-mention.v1", snapshot.account_ref, literal])
        rows.append(dict(phrase_id=phrase_id, literal_key=literal, supporting_senders=len(group.senders),
                         supporting_messages=len(group.messages), occurrences=len(group.evidence)))
        if request.phrase_id in (None, phrase_id):
            for occurrence in sorted(group.evidence):
                budget.check()
                assessment, exact, location = group.evidence[occurrence]
                evidence.append((dict(phrase_id=phrase_id, assessment=assessment, reference=exact), location))
    if request.phrase_id is not None and not evidence:
        raise InvalidAnalyticsRequest("analytics_findings_request_invalid", "This phrase is unavailable in this report.")
    page = _page(evidence, request.evidence_offset, request.evidence_limit)
    items = page["items"]
    coverage = {k: extraction[k] for k in MentionCoverage.model_fields if k != "unrepresentable_occurrences"}
    return RecurringMentionsReport(**common, availability="available", unavailable_reason=None,
        normalization_sha256=normalization, coverage={**coverage, "unrepresentable_occurrences": unmapped},
        row_page=_page(rows, request.row_offset, request.row_limit),
        evidence_page={**page, "items": [entry for entry, _ in items]}), [
            (entry["reference"], location) for entry, location in items]


def read(resources, policy, request, *, cancellation_check=None):
    from app.analytics.nlp_read_projection import read as read_saved

    stamp = None

    def project(*args, connection, canonical_scope):
        nonlocal stamp
        stamp = content_stamp(connection)
        if stamp is None:
            raise ProjectionUnavailable(availability="error", reason_code="analytics_literals_invalid")
        return aggregate(*args, connection=connection, canonical_scope=canonical_scope)

    def recheck(connection, response, budget):
        _assert_current(connection, response, budget)
        if content_stamp(connection) != stamp:
            raise ProjectionUnavailable(availability="building")

    return read_saved(resources, policy, request, cancellation_check=cancellation_check,
                      request_model=RecurringMentionsRequest, projector=project, response_check=recheck)
