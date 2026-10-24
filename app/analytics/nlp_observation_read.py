"""Bounded source-incidence links from immutable saved findings."""
from app.analytics.errors import InvalidAnalyticsRequest, ProjectionUnavailable
from app.analytics.nlp_findings_store import FrozenFindings, digest, encoded
from app.analytics.nlp_read_projection import _effective, _inventory, _select, _summarize
from app.analytics.query_contracts import SourceSpan
from app.analytics.validation_receipt import content_stamp
from app.models.analytics import AnalyticsWindow, WindowScope
from app.models.nlp_observations import (
    ObservationContext, ObservationLinksRequest, ObservationLinksResponse, TopicProvenance, view_identity,
)
from app.nlp.normalization import literal_type_id
from app.nlp.profiles import read_profile


def _invalid():
    raise InvalidAnalyticsRequest('analytics_findings_request_invalid',
                                  'The selected observation is unavailable for this saved analysis.')


def _coverage():
    return dict(eligible_occurrences=0, mapped_occurrences=0, unrepresentable_occurrences=0, supporting_messages=0)


def _target(task, label):
    return {key: label[key] for key in ('category_id', 'display_label', 'label_status', 'label_identity_sha256')} | {
        'task': task['task'], 'task_contract': task['task_contract'], 'taxonomy': task['taxonomy']}


def _finish(common, tasks, inventory, request, links, normalization_coverage, focus_coverage, budget):
    messages, conversations, complete, partial = set(), set(), set(), set()
    for link, _ in links:
        budget.check()
        reference = link['reference']
        messages.add(reference.message_ref)
        conversations.add(reference.conversation_ref)
        (partial if link['assessment'] == 'partial' else complete).add(reference.message_ref)
    links.sort(key=lambda entry: (entry[0]['reference'].message_ref,
        entry[0]['target'].get('node_id', entry[0]['target']['label_identity_sha256']),
        entry[0]['target']['category_id'],
        -1 if entry[0]['reference'].span is None else entry[0]['reference'].span.start,
        -1 if entry[0]['reference'].span is None else entry[0]['reference'].span.end))
    budget.check()
    offset, limit = request.evidence_offset, request.evidence_limit
    if offset > len(links):
        _invalid()
    page = links[offset:offset + limit]
    context = ObservationContext(**common)
    identity = view_identity(context)
    if request.expected_view_sha256 not in (None, identity):
        raise ProjectionUnavailable(availability='building')
    response = ObservationLinksResponse(**context.model_dump(), view_sha256=identity,
        processing_mode=inventory['processing_mode'], tasks=tasks,
        task_availability=[{'task': task, 'availability': 'declared' if any(t['task'] == task for t in tasks) else 'not_declared'}
                           for task in ('message_topics', 'concept_mentions')],
        normalization_coverage=normalization_coverage, focus_coverage=focus_coverage,
        support=dict(supporting_conversations=len(conversations), supporting_messages=len(messages), occurrences=len(links),
                     complete_supporting_messages=len(complete), partial_supporting_messages=len(partial)),
        link_page=dict(offset=offset, limit=limit, total=len(links), has_more=offset + len(page) < len(links),
                       items=[link for link, _ in page]))
    budget.check()
    return response, [(link['reference'], location) for link, location in page]


def aggregate(value, records, generation, snapshot, request, budget, source_rows, *, connection):
    profile = read_profile(encoded(value['snapshot']['profile']))
    profile_digest, snapshot_digest = profile.identity_digest(), digest(value)
    inventory, labels, sources = _inventory(value, records, profile, budget, source_rows)
    selected = _select(sources, request.start_date, request.end_date, request.direction, conversation=request.conversation_ref)
    # Reuse the already authenticated inventory; summary populations precede focus selection.
    tasks, _ = _summarize(profile, inventory, labels, selected, records, budget, with_evidence=False)
    topic = request.focus.kind == 'category' and request.focus.selection.task == 'message_topics'
    task = next((item for item in tasks if item['task'] == ('message_topics' if topic else 'concept_mentions')), None)
    category = request.focus.selection.category_id if request.focus.kind == 'category' else None
    task_labels = {}
    if task is not None:
        for row in task['rows']:
            budget.check()
            task_labels[row['category_id']] = row
        if category is not None and category not in task_labels:
            _invalid()
    common = {public: generation[stored] for public, stored in (
        ('account_ref', 'creator_account_id'), ('source_revision', 'canonical_revision'),
        ('projection_digest', 'projection_digest'), ('canonical_content_digest', 'canonical_content_digest'),
        ('graph_digest', 'graph_digest'), ('pipeline_revision', 'pipeline_revision'),
        ('pipeline_config_digest', 'pipeline_config_digest'), ('pipeline_identity_digest', 'pipeline_identity_digest'))}
    common.update(projection_generation=snapshot.projection_generation,
        snapshot_sha256='sha256:' + snapshot_digest, profile_sha256='sha256:' + profile_digest, presentation=profile.presentation,
        requested_window=AnalyticsWindow(scope=WindowScope.REQUESTED, start=request.start_date, end=request.end_date),
        effective_window=_effective(selected), direction=request.direction, conversation_ref=request.conversation_ref, focus=request.focus)

    def unavailable(reason):
        if request.expected_normalization_sha256 is not None:
            raise ProjectionUnavailable(availability='building')
        return _finish(dict(common, availability='unavailable', unavailable_reason=reason,
            normalization_status='not_required' if topic else 'unavailable', normalization_sha256=None,
            normalization_policy=None), tasks, inventory, request, [], None, None, budget)

    if task is None:
        return unavailable('task_not_declared')
    if topic:
        links, seen = [], set()
        for item, reference, location in selected:
            budget.check()
            record = records.get(item['record_digest'])
            if record is None:
                continue
            payload = next(result.result.payload for result in record.tasks if result.result.payload.task == 'message_topics')
            if payload.outcome not in ('assessed_findings', 'partial'):
                continue
            for finding in payload.findings:
                budget.check()
                key = (reference.message_ref, reference.source_version_digest, finding.topic_id)
                if finding.topic_id != category or key in seen:
                    continue
                seen.add(key)
                provenance = TopicProvenance(extraction_record_sha256=item['record_digest'],
                    extraction_snapshot_sha256=snapshot_digest, profile_sha256=profile_digest,
                    component_id=task['component_id'], component_sha256=task['component_sha256'].removeprefix('sha256:'),
                    taxonomy=task['taxonomy'])
                links.append((dict(kind='message_topic', source=item['source'], target=_target(task, task_labels[category]),
                    assessment='partial' if payload.outcome == 'partial' else 'complete', provenance=provenance, reference=reference), location))
        return _finish(dict(common, availability='available', unavailable_reason=None, normalization_status='not_required',
            normalization_sha256=None, normalization_policy=None), tasks, inventory, request, links, None, None, budget)

    from app.analytics.nlp_literals import LiteralUnavailable, checked_publication
    binding = FrozenFindings(snapshot.account_ref, snapshot_digest)
    try:
        data = checked_publication(connection, binding, check=budget.check)
    except LiteralUnavailable as error:
        return unavailable(error.reason)
    normalization = 'sha256:' + data.publication_id
    if request.expected_normalization_sha256 not in (None, normalization):
        raise ProjectionUnavailable(availability='building')
    nodes, states = {}, {}
    for node in data.nodes:
        budget.check()
        if node.node_id in nodes:
            raise ValueError('observation_duplicate_node')
        nodes[node.node_id] = node
    for state in data.records:
        budget.check()
        if state.record_sha256 in states or state.record_sha256 not in records or records[state.record_sha256].source != state.source:
            raise ValueError('observation_record_invalid')
        states[state.record_sha256] = state
    if states.keys() != records.keys():
        raise ValueError('observation_records_invalid')
    if request.focus.kind == 'literal':
        node_key = request.focus.node_id.removeprefix('sha256:')
        if node_key not in nodes:
            _invalid()
        categories = set()
        for mapping in data.mappings:
            budget.check()
            if mapping.node_id == node_key:
                categories.add(mapping.occurrence.category_id)
        if len(categories) != 1:
            raise ValueError('observation_literal_category_invalid')
        category = categories.pop()
        if category not in task_labels:
            raise ValueError('observation_category_invalid')
    population, gold = {}, {}
    type_ids = {}
    for category_id in task_labels:
        budget.check()
        type_ids[category_id] = literal_type_id(task['task_contract'], task['taxonomy'], category_id)
    whole, focused = _coverage(), _coverage()
    for item, reference, location in selected:
        budget.check()
        if item['record_digest'] is None:
            continue
        state = states[item['record_digest']]
        source_ref = item['source'].source_ref
        population[source_ref] = (item, reference, location)
        whole['eligible_occurrences'] += state.occurrences
        whole['mapped_occurrences'] += state.mapped
        whole['unrepresentable_occurrences'] += state.unrepresentable
        whole['supporting_messages'] += int(state.occurrences > 0)
        payload = next(result.result.payload for result in records[item['record_digest']].tasks
                       if result.result.payload.task == 'concept_mentions')
        gold[source_ref] = set()
        for finding in payload.findings:
            budget.check()
            gold[source_ref].add((finding.concept_id, finding.span.start, finding.span.end))
    links, seen, focus_messages = [], set(), set()
    observed = {'mapped_occurrences': 0, 'unrepresentable_occurrences': 0}

    def consume(entry, mapped):
        budget.check()
        anchor, provenance = entry.occurrence, entry.extraction
        if anchor.source.source_ref not in population:
            return
        item, reference, location = population[anchor.source.source_ref]
        key = (anchor.source.source_ref, anchor.source.source_version, anchor.category_id, anchor.span.start, anchor.span.end)
        if (key in seen or anchor.source != item['source'] or provenance.extraction_record_sha256 != item['record_digest']
                or provenance.extraction_snapshot_sha256 != binding.snapshot_id or provenance.profile_sha256 != profile_digest
                or provenance.component_id != task['component_id'] or 'sha256:' + provenance.component_sha256 != task['component_sha256']
                or provenance.taxonomy != task['taxonomy'] or provenance.task_contract != task['task_contract']
                or (anchor.category_id, anchor.span.start, anchor.span.end) not in gold[anchor.source.source_ref]
                or entry.type_id != type_ids.get(anchor.category_id)):
            raise ValueError('observation_occurrence_invalid')
        seen.add(key)
        observed['mapped_occurrences' if mapped else 'unrepresentable_occurrences'] += 1
        if anchor.category_id == category:
            focused['eligible_occurrences'] += 1
            focused['mapped_occurrences' if mapped else 'unrepresentable_occurrences'] += 1
            focus_messages.add(anchor.source.source_ref)
        if not mapped or anchor.category_id != category:
            return
        node = nodes.get(entry.node_id)
        if node is None or node.type_id != entry.type_id or node.account_namespace != data.envelope.account_namespace:
            raise ValueError('observation_node_invalid')
        if request.focus.kind == 'literal' and request.focus.node_id != 'sha256:' + node.node_id:
            return
        state = states[item['record_digest']]
        if state.outcome not in ('assessed_findings', 'partial'):
            raise ValueError('observation_outcome_invalid')
        target = dict(_target(task, task_labels[category]), node_id='sha256:' + node.node_id,
                      type_id=node.type_id, literal_key=node.literal_key, resolution=node.resolution)
        exact = reference.model_copy(update={'span': SourceSpan(start=anchor.span.start, end=anchor.span.end)})
        links.append((dict(kind='literal_occurrence', source=anchor.source, target=target,
            assessment='partial' if state.outcome == 'partial' else 'complete', provenance=provenance, reference=exact), location))

    for mapping in data.mappings:
        consume(mapping, True)
    for disposition in data.dispositions:
        consume(disposition, False)
    if any(observed[key] != whole[key] for key in observed):
        raise ValueError('observation_normalization_accounting_invalid')
    focused['supporting_messages'] = len(focus_messages)
    return _finish(dict(common, availability='available', unavailable_reason=None, normalization_status='available',
        normalization_sha256=normalization, normalization_policy=data.envelope.publication.normalization_policy),
        tasks, inventory, request, links, whole, focused, budget)


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
        if response.normalization_status == 'available':
            from app.analytics.nlp_literals import assert_publication
            assert_publication(connection, FrozenFindings(response.account_ref, response.snapshot_sha256.removeprefix('sha256:')),
                               response.normalization_sha256.removeprefix('sha256:'), check=budget.check)
        if content_stamp(connection) != stamp:
            raise ProjectionUnavailable(availability='building')

    return read_saved(resources, policy, request, cancellation_check=cancellation_check,
                      request_model=ObservationLinksRequest, projector=project, response_check=recheck)
