"""Read literal recurrence from an authenticated saved publication."""
from collections import defaultdict

from app.analytics.errors import InvalidAnalyticsRequest, ProjectionUnavailable
from app.analytics.nlp_findings_store import FrozenFindings, digest, encoded
from app.analytics.nlp_read_projection import _effective, _inventory, _select, _summarize
from app.analytics.query_contracts import SourceSpan
from app.analytics.validation_receipt import content_stamp
from app.models.analytics import AnalyticsWindow, WindowScope
from app.models.nlp_literals import LiteralFindingsRequest, LiteralFindingsResponse
from app.nlp.profiles import read_profile


def _page(rows, offset, limit):
    if offset > len(rows):
        raise InvalidAnalyticsRequest('analytics_findings_request_invalid', 'The selected page is unavailable.')
    items = rows[offset:offset + limit]
    return {'offset': offset, 'limit': limit, 'total': len(rows),
            'has_more': offset + len(items) < len(rows), 'items': items}


def _matches(record, category):
    if category is None:
        return True
    if record is None:
        return False
    payload = next(t.result.payload for t in record.tasks if t.result.payload.task == category.task)
    return payload.outcome in ('assessed_findings', 'partial') and any(
        (finding.topic_id if category.task == 'message_topics' else finding.concept_id) == category.category_id
        for finding in payload.findings)


def aggregate(value, records, generation, snapshot, request, budget, source_rows, *, connection):
    from app.analytics.nlp_literals import LiteralUnavailable, checked_publication

    profile = read_profile(encoded(value['snapshot']['profile']))
    inventory, labels, sources = _inventory(value, records, profile, budget, source_rows)
    selected = _select(sources, request.start_date, request.end_date, request.direction,
                       conversation=request.conversation_ref)
    tasks, _ = _summarize(profile, inventory, labels, selected, records, budget, with_evidence=False)
    if request.category_filter is not None and not any(
            task['task'] == request.category_filter.task and any(
                row['category_id'] == request.category_filter.category_id for row in task['rows'])
            for task in tasks):
        raise InvalidAnalyticsRequest('analytics_findings_request_invalid',
                                      'The selected category is unavailable for this analysis profile.')
    extraction = next((task for task in tasks if task['task'] == 'concept_mentions'), None)
    common = {public: generation[stored] for public, stored in (
        ('account_ref', 'creator_account_id'), ('source_revision', 'canonical_revision'),
        ('projection_digest', 'projection_digest'), ('canonical_content_digest', 'canonical_content_digest'),
        ('graph_digest', 'graph_digest'), ('pipeline_revision', 'pipeline_revision'),
        ('pipeline_config_digest', 'pipeline_config_digest'), ('pipeline_identity_digest', 'pipeline_identity_digest'))}
    common.update(projection_generation=snapshot.projection_generation,
        snapshot_sha256='sha256:' + digest(value), profile_sha256='sha256:' + profile.identity_digest(),
        presentation=profile.presentation,
        requested_window=AnalyticsWindow(scope=WindowScope.REQUESTED, start=request.start_date, end=request.end_date),
        effective_window=_effective(selected), direction=request.direction,
        conversation_ref=request.conversation_ref, category_filter=request.category_filter, node_id=request.node_id,
        extraction=extraction)

    def unavailable(reason):
        if request.expected_normalization_sha256 is not None:
            raise ProjectionUnavailable(availability='building')
        return LiteralFindingsResponse(**common, availability='unavailable', unavailable_reason=reason,
            normalization_sha256=None, normalization_policy=None, normalization_coverage=None,
            row_page=_page([], request.row_offset, request.row_limit),
            evidence_page=_page([], request.evidence_offset, request.evidence_limit)), []

    if extraction is None:
        return unavailable('task_not_declared')
    binding = FrozenFindings(snapshot.account_ref, digest(value))
    try:
        data = checked_publication(connection, binding, check=budget.check)
    except LiteralUnavailable as error:
        return unavailable(getattr(error, 'reason', 'not_prepared'))
    publication_id = 'sha256:' + data.publication_id
    if request.expected_normalization_sha256 not in (None, publication_id):
        raise ProjectionUnavailable(availability='building')
    nodes = {}
    for node in data.nodes:
        budget.check()
        if node.node_id in nodes:
            raise ValueError('nlp_literal_node_duplicate')
        nodes[node.node_id] = node
    if request.node_id is not None and request.node_id.removeprefix('sha256:') not in nodes:
        raise InvalidAnalyticsRequest('analytics_findings_request_invalid',
                                      'The selected literal value is unavailable for this saved analysis.')
    record_states = {}
    for state in data.records:
        budget.check()
        key = state.record_sha256
        if key in record_states or key not in records or records[key].source != state.source:
            raise ValueError('nlp_literal_source_invalid')
        record_states[key] = state
    if record_states.keys() != records.keys():
        raise ValueError('nlp_literal_records_invalid')
    population = {}
    matching = set()
    for item, reference, location in selected:
        budget.check()
        key = item['record_digest']
        if key is None:
            continue
        population[item['source'].source_ref] = (item, reference, location)
        if _matches(records[key], request.category_filter):
            matching.add(item['source'].source_ref)
    coverage = {'eligible_occurrences': 0, 'mapped_occurrences': 0,
                'unrepresentable_occurrences': 0, 'supporting_messages': 0}
    for source, (item, _, _) in population.items():
        budget.check()
        state = record_states[item['record_digest']]
        coverage['eligible_occurrences'] += state.occurrences
        coverage['mapped_occurrences'] += state.mapped
        coverage['unrepresentable_occurrences'] += state.unrepresentable
        coverage['supporting_messages'] += int(state.occurrences > 0)
    grouped = defaultdict(lambda: {'messages': set(), 'conversations': set(), 'complete': set(),
                                   'partial': set(), 'occurrences': set()})
    evidence, seen = [], set()
    for mapping in data.mappings:
        budget.check()
        anchor = mapping.occurrence
        if anchor.source.source_ref not in matching:
            continue
        item, reference, location = population[anchor.source.source_ref]
        if anchor.source != item['source'] or mapping.extraction.extraction_record_sha256 != item['record_digest']:
            raise ValueError('nlp_literal_source_invalid')
        state = record_states[item['record_digest']]
        if state.outcome not in ('assessed_findings', 'partial'):
            raise ValueError('nlp_literal_outcome_invalid')
        occurrence = (anchor.source.source_ref, anchor.source.source_version, anchor.category_id,
                      anchor.span.start, anchor.span.end)
        if occurrence in seen:
            raise ValueError('nlp_literal_occurrence_duplicate')
        seen.add(occurrence)
        node = nodes.get(mapping.node_id)
        if node is None or node.type_id != mapping.type_id:
            raise ValueError('nlp_literal_node_invalid')
        assessment = 'partial' if state.outcome == 'partial' else 'complete'
        row = grouped[mapping.node_id]
        row['messages'].add(reference.message_ref)
        row['conversations'].add(reference.conversation_ref)
        row[assessment].add(reference.message_ref)
        row['occurrences'].add(occurrence)
        if 'category_id' in row and row['category_id'] != anchor.category_id:
            raise ValueError('nlp_literal_type_invalid')
        row['category_id'] = anchor.category_id
        if request.node_id in (None, 'sha256:' + mapping.node_id):
            exact = reference.model_copy(update={'span': SourceSpan(start=anchor.span.start, end=anchor.span.end)})
            evidence.append((mapping.node_id, anchor.category_id, assessment, exact, location))
    rows = []
    for node_id, counts in grouped.items():
        budget.check()
        node = nodes[node_id]
        label = labels[('concept_mentions.v1', counts['category_id'])]
        rows.append({'node_id': 'sha256:' + node_id, 'type_id': node.type_id,
            **{key: label[key] for key in ('category_id', 'display_label', 'label_status', 'label_identity_sha256')},
            'literal_key': node.literal_key, 'supporting_conversations': len(counts['conversations']),
            'supporting_messages': len(counts['messages']), 'occurrences': len(counts['occurrences']),
            'complete_supporting_messages': len(counts['complete']), 'partial_supporting_messages': len(counts['partial'])})
    rows.sort(key=lambda row: (-row['supporting_conversations'], -row['supporting_messages'], -row['occurrences'], row['node_id']))
    evidence.sort(key=lambda entry: (entry[3].message_ref, entry[0], entry[1], entry[3].span.start, entry[3].span.end))
    evidence_page = _page(evidence, request.evidence_offset, request.evidence_limit)
    page = evidence_page['items']
    budget.check()
    return LiteralFindingsResponse(**common, availability='available', unavailable_reason=None,
        normalization_sha256=publication_id, normalization_policy=data.envelope.publication.normalization_policy,
        normalization_coverage=coverage, row_page=_page(rows, request.row_offset, request.row_limit),
        evidence_page={'offset': request.evidence_offset, 'limit': request.evidence_limit, 'total': len(evidence),
            'has_more': request.evidence_offset + len(page) < len(evidence),
            'items': [{'node_id': 'sha256:' + node, 'category_id': category,
                       'assessment': assessment, 'reference': reference} for node, category, assessment, reference, _ in page]}), [
                (reference, location) for _, _, _, reference, location in page]


def _assert_current(connection, response, budget):
    if response.normalization_sha256 is not None:
        from app.analytics.nlp_literals import assert_publication
        assert_publication(connection, FrozenFindings(response.account_ref, response.snapshot_sha256.removeprefix('sha256:')),
                           response.normalization_sha256.removeprefix('sha256:'), check=budget.check)


def read(resources, policy, request, *, cancellation_check=None):
    from app.analytics.nlp_read_projection import read as read_saved
    stamp = None

    def project(*args, connection):
        nonlocal stamp
        stamp = content_stamp(connection)
        if stamp is None:
            raise ProjectionUnavailable(availability='error', reason_code='analytics_literals_invalid')
        return aggregate(*args, connection=connection)

    def recheck(connection, response, budget):
        _assert_current(connection, response, budget)
        if content_stamp(connection) != stamp:
            raise ProjectionUnavailable(availability='building')

    return read_saved(resources, policy, request, cancellation_check=cancellation_check,
        request_model=LiteralFindingsRequest, projector=project, response_check=recheck)
