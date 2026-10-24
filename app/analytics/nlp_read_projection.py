"""Bounded findings from one immutable published generation, without inference."""
from collections import Counter, defaultdict
from contextlib import ExitStack
from datetime import datetime, timezone
import copy
import hashlib
import json
import unicodedata

from app.analytics.errors import (
    AnalyticsError,
    InvalidAnalyticsRequest,
    ProjectionUnavailable,
)
from app.analytics.evidence import EvidenceUnavailable
from app.analytics.evidence_contracts import EvidenceLocation
from app.analytics.nlp_findings_store import digest, effective_config, encoded
from app.analytics.query_contracts import QuestionEvidence, SourceSpan
from app.analytics.query_execution import (
    QuestionBudget,
    QuestionLimits,
    QuestionLimitExceeded,
)
from app.models.analytics import AnalyticsWindow, WindowScope
from app.models.insights import FindingsRequest, FindingsResponse, SubsetFindingsResponse, FilteredFindingsResponse
from app.nlp.contracts import SourceBinding
from app.nlp.profiles import read_profile

SOURCE_PAGE_BYTES = 65_536
SOURCE_PAGE_ROWS = 64
SOURCE_STORAGE_BYTES = 64 * 1024 * 1024


def pack_source_pages(rows, *, check=lambda: None):
    pages, page, total = [], [], 0
    for row in rows:
        check()
        candidate = encoded(page + [row])
        if page and (
            len(page) == SOURCE_PAGE_ROWS or len(candidate) > SOURCE_PAGE_BYTES
        ):
            raw = encoded(page)
            pages.append(raw)
            total += len(raw)
            page = []
        page.append(row)
        if (
            len(encoded(page)) > SOURCE_PAGE_BYTES
            or total + len(encoded(page)) > SOURCE_STORAGE_BYTES
        ):
            return None, []
    if page:
        pages.append(encoded(page))
    sha = hashlib.sha256(b"nlp-source-status-pages.v1\0")
    for raw in pages:
        check()
        sha.update(len(raw).to_bytes(8, "big"))
        sha.update(raw)
    return {
        "source_count": len(rows),
        "source_page_count": len(pages),
        "source_bytes": sum(map(len, pages)),
        "source_pages_sha256": sha.hexdigest(),
        "source_page_hashes": [hashlib.sha256(raw).hexdigest() for raw in pages],
        "source_page_sizes": list(map(len, pages)),
    }, pages


def authenticate_source_page(header, ordinal, raw):
    """Authenticate one bounded page for reads or restartable scoped erasure."""
    if (
        not 0 <= ordinal < header["source_page_count"]
        or not 0 < len(raw) <= SOURCE_PAGE_BYTES
        or len(raw) != header["source_page_sizes"][ordinal]
        or hashlib.sha256(raw).hexdigest() != header["source_page_hashes"][ordinal]
    ):
        raise ValueError("nlp_read_page_invalid")
    page = json.loads(raw)
    if (
        type(page) is not list
        or not 1 <= len(page) <= SOURCE_PAGE_ROWS
        or encoded(page) != raw
    ):
        raise ValueError("nlp_read_page_invalid")
    return page


def validate_source_header(header):
    """Validate bounded page descriptors before an incremental read."""
    if (
        type(header["source_count"]) is not int
        or not 0 <= header["source_count"] <= 100_000
        or type(header["source_page_count"]) is not int
        or not 0 <= header["source_page_count"] <= 100_000
        or type(header["source_bytes"]) is not int
        or not 0 <= header["source_bytes"] <= SOURCE_STORAGE_BYTES
    ):
        raise ValueError("nlp_read_inventory_invalid")
    if (
        len(header["source_page_hashes"]) != header["source_page_count"]
        or len(header["source_page_sizes"]) != header["source_page_count"]
        or any(
            type(size) is not int or not 0 < size <= SOURCE_PAGE_BYTES
            for size in header["source_page_sizes"]
        )
        or sum(header["source_page_sizes"]) != header["source_bytes"]
    ):
        raise ValueError("nlp_read_inventory_invalid")


def load_source_pages(db, account, snapshot_id, value, budget):
    """Authenticate the exact compact sequence before interpreting its rows."""
    from app.analytics.opaque_refs import account_ref, conversation_ref, message_ref

    header = value["read_inventory"]
    validate_source_header(header)
    rows, total, ordinal = [], 0, 0
    sha = hashlib.sha256(b"nlp-source-status-pages.v1\0")
    for row in db.execute(
        "SELECT ordinal,document FROM nlp_snapshot_source_pages "
        "WHERE account_ref=? AND snapshot_id=? ORDER BY ordinal",
        (account_ref(account), snapshot_id),
    ):
        budget.check()
        raw = bytes(row["document"])
        if row["ordinal"] != ordinal or not 0 < len(raw) <= SOURCE_PAGE_BYTES:
            raise ValueError("nlp_read_page_invalid")
        ordinal += 1
        total += len(raw)
        if ordinal > header["source_page_count"] or total > header["source_bytes"]:
            raise ValueError("nlp_read_page_invalid")
        sha.update(len(raw).to_bytes(8, "big"))
        sha.update(raw)
        page = authenticate_source_page(header, row["ordinal"], raw)
        rows.extend(page)
        if len(rows) > header["source_count"]:
            raise ValueError("nlp_read_page_invalid")
    if (
        ordinal != header["source_page_count"]
        or total != header["source_bytes"]
        or len(rows) != header["source_count"]
        or sha.hexdigest() != header["source_pages_sha256"]
    ):
        raise ValueError("nlp_read_page_invalid")
    sources, last = [], ""
    keys = value["records"]
    for row in rows:
        budget.check()
        if type(row) is not list or len(row) != 8:
            raise ValueError("nlp_read_source_invalid")
        chat, message, sent_at, direction, version, speaker, record_index, status = row
        location = EvidenceLocation(conversation_id=chat, message_id=message)
        source = SourceBinding(
            account_scope=account_ref(account),
            source_ref=message_ref(account, chat, message),
            source_version=version,
            speaker_ref=speaker,
        )
        if (
            source.source_ref <= last
            or type(record_index) is not int
            or not -1 <= record_index < len(keys)
        ):
            raise ValueError("nlp_read_source_invalid")
        last = source.source_ref
        reference = QuestionEvidence(
            account_ref=source.account_scope,
            conversation_ref=conversation_ref(account, chat),
            message_ref=source.source_ref,
            source_revision=value["snapshot"]["canonical_revision"],
            source_version_digest="sha256:" + version,
            sent_at=sent_at,
        )
        sources.append(
            {
                "reference": reference,
                "location": location,
                "source": source,
                "direction": direction,
                "record_digest": keys[record_index] if record_index >= 0 else None,
                "status": status,
            }
        )
    return sources


def frozen_labels(profile, resolver=None):
    """Names are display metadata; category and taxonomy identities remain authority.

    A production resolver reads hash-verified configuration data. Callbacks never
    arrive from distribution documents or execute downloaded code.
    """
    labels = []
    for binding in profile.bindings:
        descriptor = next(
            t
            for t in profile.component(binding.component_id).tasks
            if t.task == binding.task
        )
        for category in descriptor.enabled_ids:
            identity = {
                "presentation": profile.presentation.model_dump(mode="json"),
                "task_contract": descriptor.task_contract,
                "taxonomy": descriptor.taxonomy.model_dump(mode="json"),
                "category_id": category,
            }
            answer = resolver(copy.deepcopy(identity)) if resolver is not None else None
            if answer is None:
                label, status = category, "id_only"
            else:
                if (
                    type(answer) is not dict
                    or set(answer) != set(identity) | {"display_label"}
                    or any(answer[k] != value for k, value in identity.items())
                ):
                    raise ValueError("nlp_display_identity_invalid")
                label, status = answer["display_label"], "resolved"
            if (
                type(label) is not str
                or not 1 <= len(label) <= 256
                or any(unicodedata.category(c).startswith("C") for c in label)
            ):
                raise ValueError("nlp_display_label_invalid")
            label.encode("utf-8", errors="strict")
            item = {**identity, "display_label": label, "label_status": status}
            labels.append({**item, "label_identity_sha256": "sha256:" + digest(item)})
    return labels


def _inventory(value, records, profile, budget, source_rows):
    inventory = value["read_inventory"]
    if (
        set(inventory)
        != {
            "schema_version",
            "processing_mode",
            "scan_complete",
            "labels",
            "source_count",
            "source_page_count",
            "source_bytes",
            "source_pages_sha256",
            "source_page_hashes",
            "source_page_sizes",
        }
        or inventory["schema_version"] != "nlp-source-status.v1"
        or inventory["processing_mode"] not in ("enabled", "paused")
        or type(inventory["scan_complete"]) is not bool
        or len(source_rows) != inventory["source_count"]
    ):
        raise ValueError("nlp_read_inventory_invalid")
    expected = {
        (x["task_contract"], x["category_id"]): x for x in frozen_labels(profile)
    }
    labels = {}
    for item in inventory["labels"]:
        budget.check()
        key = (item["task_contract"], item["category_id"])
        template = expected.get(key)
        body = {k: v for k, v in item.items() if k != "label_identity_sha256"}
        label = item["display_label"]
        if (
            template is None
            or key in labels
            or set(item) != set(template)
            or any(
                item[k] != template[k]
                for k in ("presentation", "task_contract", "taxonomy", "category_id")
            )
            or item["label_status"] not in ("id_only", "resolved")
            or type(label) is not str
            or not 1 <= len(label) <= 256
            or any(unicodedata.category(c).startswith("C") for c in label)
            or (item["label_status"] == "id_only" and label != item["category_id"])
            or item["label_identity_sha256"] != "sha256:" + digest(body)
        ):
            raise ValueError("nlp_display_identity_invalid")
        labels[key] = item
    if labels.keys() != expected.keys():
        raise ValueError("nlp_display_identity_invalid")
    seen, completed, sources = set(), set(), []
    # The source bound counts unique immutable inventory entries. Existing
    # canonical framing/evidence budgets remain independent of this count.
    if len(source_rows) > budget.limits.max_records:
        raise QuestionLimitExceeded()
    for item in source_rows:
        budget.check()
        if set(item) != {
            "reference",
            "direction",
            "location",
            "source",
            "record_digest",
            "status",
        }:
            raise ValueError("nlp_read_source_invalid")
        ref = item["reference"]
        source = item["source"]
        location = item["location"]
        key = item["record_digest"]
        if (
            source.source_ref in seen
            or source.account_scope != ref.account_ref
            or source.source_ref != ref.message_ref
            or "sha256:" + source.source_version != ref.source_version_digest
            or ref.span is not None
            or ref.source_revision != value["snapshot"]["canonical_revision"]
            or item["direction"] not in ("inbound", "outbound")
            or item["status"] not in ("record", "pending", "failed", "unsupported")
            or (item["status"] == "record") != (key is not None)
        ):
            raise ValueError("nlp_read_source_invalid")
        if key is not None:
            if key not in records or key in completed or records[key].source != source:
                raise ValueError("nlp_read_record_invalid")
            completed.add(key)
        seen.add(source.source_ref)
        sources.append((item, ref, location))
    if completed != records.keys():
        raise ValueError("nlp_read_record_invalid")
    return inventory, labels, sources


def _summarize(
    profile, inventory, labels, selected, records, budget, *, with_evidence=True
):
    tasks, evidence = [], []
    for binding in profile.bindings:
        component = profile.component(binding.component_id)
        descriptor = next(t for t in component.tasks if t.task == binding.task)
        counts = Counter()
        totals = defaultdict(
            lambda: {
                "messages": set(),
                "conversations": set(),
                "complete": set(),
                "partial": set(),
                "occurrences": set(),
            }
        )
        for item, ref, location in selected:
            budget.check()
            record = records.get(item["record_digest"])
            payload = (
                next(
                    t.result.payload
                    for t in record.tasks
                    if t.result.payload.task == binding.task
                )
                if record
                else None
            )
            state = item["status"] if payload is None else payload.outcome
            state = (
                "complete"
                if state.startswith("assessed_")
                else "pending" if state == "unprocessed" else state
            )
            counts[state] += 1
            if payload is None or state not in ("complete", "partial"):
                continue
            for finding in payload.findings:
                budget.check()
                category = (
                    finding.topic_id
                    if binding.task == "message_topics"
                    else finding.concept_id
                )
                row = totals[category]
                span = (
                    None
                    if binding.task == "message_topics"
                    else SourceSpan(start=finding.span.start, end=finding.span.end)
                )
                occurrence = (
                    ref.message_ref,
                    ref.source_version_digest,
                    category,
                    None if span is None else (span.start, span.end),
                )
                if occurrence in row["occurrences"]:
                    continue
                row["occurrences"].add(occurrence)
                row["messages"].add(ref.message_ref)
                row["conversations"].add(ref.conversation_ref)
                row[state].add(ref.message_ref)
                if with_evidence:
                    evidence.append(
                        (
                            binding.task,
                            category,
                            ref.message_ref,
                            -1 if span is None else span.start,
                            -1 if span is None else span.end,
                            state,
                            ref.model_copy(update={"span": span}),
                            location,
                        )
                    )
        rows = []
        for category in descriptor.enabled_ids:
            row, label = totals[category], labels[(descriptor.task_contract, category)]
            rows.append(
                {
                    **{
                        k: label[k]
                        for k in (
                            "category_id",
                            "display_label",
                            "label_status",
                            "label_identity_sha256",
                        )
                    },
                    "supporting_messages": len(row["messages"]),
                    "supporting_conversations": len(row["conversations"]),
                    "occurrences": len(row["occurrences"]),
                    "complete_supporting_messages": len(row["complete"]),
                    "partial_supporting_messages": len(row["partial"]),
                    "share_of_complete_messages": (
                        len(row["complete"]) / counts["complete"]
                        if counts["complete"]
                        else None
                    ),
                }
            )
        tasks.append(
            {
                "task": binding.task,
                "task_contract": descriptor.task_contract,
                "component_id": component.component_id,
                "component_sha256": "sha256:" + component.identity_digest(),
                "taxonomy": descriptor.taxonomy,
                "availability": (
                    "pending"
                    if counts["pending"] or not inventory["scan_complete"]
                    else "partial" if len(selected) != counts["complete"] else "current"
                ),
                "eligible_messages": len(selected),
                "complete_messages": counts["complete"],
                "partial_messages": counts["partial"],
                "pending_messages": counts["pending"],
                "failed_messages": counts["failed"],
                "unsupported_messages": counts["unsupported"],
                "uncertain_messages": counts["uncertain"],
                "scan_complete": inventory["scan_complete"],
                "rows": rows,
            }
        )
    return tasks, evidence


def _effective(selected):
    times = [ref.sent_at for _, ref, _ in selected]
    return AnalyticsWindow(
        scope=WindowScope.EFFECTIVE,
        start=min(times) if times else None,
        end=max(times) if times else None,
    )


def _select(sources, start, end, direction, *, end_exclusive=False, conversation=None):
    return [
        (item, ref, location)
        for item, ref, location in sources
        if (start is None or start <= ref.sent_at)
        and (
            end is None or (ref.sent_at < end if end_exclusive else ref.sent_at <= end)
        )
        and direction in ("all", item["direction"])
        and conversation in (None, ref.conversation_ref)
    ]


def aggregate(value, records, generation, snapshot, request, budget, source_rows):
    """Return counts and one exact evidence page, plus private locator bindings."""
    profile = read_profile(encoded(value["snapshot"]["profile"]))
    category = request.category_filter
    if category is not None and not any(
        binding.task == category.task and any(
            task.task == category.task and category.category_id in task.enabled_ids
            for task in profile.component(binding.component_id).tasks
        ) for binding in profile.bindings
    ):
        raise InvalidAnalyticsRequest(
            "analytics_findings_request_invalid", "The selected category is unavailable for this analysis profile."
        )
    inventory, labels, sources = _inventory(
        value, records, profile, budget, source_rows
    )
    population = _select(
        sources, request.start_date, request.end_date, request.direction
    )
    selected = [
        entry
        for entry in population
        if request.conversation_ref in (None, entry[1].conversation_ref)
    ]
    tasks, evidence = _summarize(profile, inventory, labels, selected, records, budget)
    conversations = Counter(ref.conversation_ref for _, ref, _ in population)
    if category is not None:
        matching_conversations = set()
        for item, ref, _ in population:
            budget.check()
            record = records.get(item["record_digest"])
            if record is None:
                continue
            payload = next(task.result.payload for task in record.tasks
                           if task.result.payload.task == category.task)
            if payload.outcome not in ("assessed_findings", "partial"):
                continue
            for finding in payload.findings:
                budget.check()
                identifier = finding.topic_id if category.task == "message_topics" else finding.concept_id
                if identifier == category.category_id:
                    matching_conversations.add(ref.conversation_ref)
                    break
        matching_counts = Counter()
        for key, count in conversations.items():
            budget.check()
            if key in matching_conversations:
                matching_counts[key] = count
        conversations = matching_counts
        matching_evidence = []
        for entry in evidence:
            budget.check()
            if entry[0] == category.task and entry[1] == category.category_id:
                matching_evidence.append(entry)
        evidence = matching_evidence
    conversation_rows = sorted(conversations)
    conversation_page = conversation_rows[
        request.conversation_offset : request.conversation_offset
        + request.conversation_limit
    ]
    reason = (
        "unbounded_window"
        if request.start_date is None or request.end_date is None
        else "zero_width_window"
    )
    comparison = {
        "availability": "unavailable",
        "unavailable_reason": reason,
        "requested_window": None,
        "effective_window": None,
        "tasks": [],
    }
    if (
        request.start_date is not None
        and request.end_date is not None
        and request.start_date < request.end_date
    ):
        try:
            previous_start = request.start_date - (
                request.end_date - request.start_date
            )
        except OverflowError:
            comparison["unavailable_reason"] = "window_out_of_range"
        else:
            previous = _select(
                sources,
                previous_start,
                request.start_date,
                request.direction,
                end_exclusive=True,
                conversation=request.conversation_ref,
            )
            previous_tasks, _ = _summarize(
                profile,
                inventory,
                labels,
                previous,
                records,
                budget,
                with_evidence=False,
            )
            comparison = {
                "availability": "available",
                "unavailable_reason": None,
                "requested_window": AnalyticsWindow(
                    scope=WindowScope.REQUESTED,
                    start=previous_start,
                    end=request.start_date,
                ),
                "effective_window": _effective(previous),
                "tasks": previous_tasks,
            }
    evidence.sort(key=lambda e: e[:5])
    page = evidence[
        request.evidence_offset : request.evidence_offset + request.evidence_limit
    ]
    response_model = (FilteredFindingsResponse if category is not None else
                      SubsetFindingsResponse if profile.schema_version == 'nlp-profile.v3' else FindingsResponse)
    availability = ({'task_availability': [
        {'task': task, 'availability': 'declared' if task in {b.task for b in profile.bindings} else 'not_declared'}
        for task in ('message_topics', 'concept_mentions')]} if category is not None or profile.schema_version == 'nlp-profile.v3' else {})
    response = response_model(
        **({'category_filter': category} if category is not None else {}),
        **availability,
        **{
            key: generation[column]
            for key, column in (
                ("account_ref", "creator_account_id"),
                ("source_revision", "canonical_revision"),
                ("projection_digest", "projection_digest"),
                ("canonical_content_digest", "canonical_content_digest"),
                ("graph_digest", "graph_digest"),
                ("pipeline_revision", "pipeline_revision"),
                ("pipeline_config_digest", "pipeline_config_digest"),
                ("pipeline_identity_digest", "pipeline_identity_digest"),
            )
        },
        projection_generation=snapshot.projection_generation,
        snapshot_sha256="sha256:" + digest(value),
        profile_sha256="sha256:" + profile.identity_digest(),
        presentation=profile.presentation,
        requested_window=AnalyticsWindow(
            scope=WindowScope.REQUESTED, start=request.start_date, end=request.end_date
        ),
        effective_window=_effective(selected),
        direction=request.direction,
        conversation_ref=request.conversation_ref,
        conversation_page={
            "offset": request.conversation_offset,
            "limit": request.conversation_limit,
            "total": len(conversations),
            "has_more": request.conversation_offset + len(conversation_page)
            < len(conversations),
            "items": [
                {"conversation_ref": ref, "eligible_messages": conversations[ref]}
                for ref in conversation_page
            ],
        },
        comparison=comparison,
        processing_mode=inventory["processing_mode"],
        tasks=tasks,
        evidence_page={
            "offset": request.evidence_offset,
            "limit": request.evidence_limit,
            "total": len(evidence),
            "has_more": request.evidence_offset + len(page) < len(evidence),
            "items": [
                {
                    "task": e[0],
                    "category_id": e[1],
                    "assessment": e[5],
                    "reference": e[6],
                }
                for e in page
            ],
        },
    )
    budget.check()
    return response, [(e[6], e[7]) for e in page]


def read(resources, policy, request, *, cancellation_check=None):
    account = resources._remember(policy)
    try:
        plan = FindingsRequest.model_validate(request)
    except (ValueError, TypeError):
        raise InvalidAnalyticsRequest(
            "analytics_findings_request_invalid", "The findings request is invalid."
        ) from None
    budget = QuestionBudget(
        QuestionLimits(max_records=100_000, wall_clock_ms=5000),
        cancellation_check=lambda: resources._cancelled(account, cancellation_check),
    )
    source, store = resources.source, resources.pipeline.projections
    if not callable(getattr(source, "open_question_scope", None)) or not callable(
        getattr(store, "read_findings_snapshot", None)
    ):
        raise ProjectionUnavailable()
    try:
        with ExitStack() as owned:
            canonical_lease, analytics_lease = owned.enter_context(
                resources._connections.borrow(source, store, account, budget)
            )
            owned.enter_context(source.use_question_connection(canonical_lease))
            scope = owned.enter_context(source.open_question_scope(account, budget))
            owned.enter_context(
                store.activation.read_scope(connection=scope.connection)
            )
            publication = owned.enter_context(
                store.question_publications(account, budget, lease=analytics_lease)
            )
            snapshot, revision, config = publication(account, scope.identity, budget)
            if (
                snapshot.projection_generation != plan.expected_generation
                or snapshot.projection_digest != plan.expected_projection_digest
            ):
                raise ProjectionUnavailable(availability="building")
            if (
                snapshot.retention_due_at is not None
                and snapshot.retention_due_at <= resources.clock()
            ):
                raise ProjectionUnavailable(availability="building")
            value, records, generation, source_rows = publication.findings(snapshot)
            if (
                revision != resources.pipeline.pipeline_revision + "+nlp.snapshot.v1"
                or config
                != effective_config(
                    resources.pipeline.pipeline_config_digest, digest(value)
                )
            ):
                raise ProjectionUnavailable(availability="building")
            response, bindings = aggregate(
                value, records, generation, snapshot, plan, budget, source_rows
            )
            for reference, location in bindings:
                budget.check()
                resources.evidence.bind(
                    policy,
                    reference,
                    location,
                    valid_until=snapshot.retention_due_at,
                    cancellation_check=lambda: budget.check() or False,
                )
            scope.check(budget)
            if publication(account, scope.identity, budget) != (
                snapshot,
                revision,
                config,
            ):
                raise ProjectionUnavailable(availability="building")
            scope.check(budget)
        budget.check()
        return response
    except AnalyticsError:
        resources.evidence.clear_account(policy)
        raise
    except Exception:
        resources.evidence.clear_account(policy)
        raise ProjectionUnavailable(
            availability="error", reason_code="analytics_findings_invalid"
        ) from None
    except BaseException:
        resources.evidence.clear_account(policy)
        raise
