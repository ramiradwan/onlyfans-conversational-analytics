"""Task 6A: clean derived-state builds are deterministic under one context.

The property operates on immutable canonical read models, the same production
read boundary supplied to analytics after canonical persistence.  It does not
exercise incremental maintenance; that is deliberately reserved for Task 6B.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import ast
import json
import os
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from app.analytics.pipeline import AnalyticsPipeline
from app.transport.ingestion import AccountReadModel
from tests.state_models.analytics_oracle import (
    AnalyticsOracleMismatch,
    assert_deterministic_rebuilds,
    context_for,
    expected_active_refs,
    reproduction_payload,
)


EVALUATION_CLOCK = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
CREATOR_ID = "task6a-synthetic-creator"
TEXTS = (
    "hello and thank you",
    "price is $25 #support",
    "can we schedule tomorrow?",
    "https://example.invalid/media @creator",
    "thanks, that works",
)


for profile, examples in (
    ("task6a_determinism_fast", 30),
    ("task6a_determinism_dev", 6),
):
    settings.register_profile(
        profile,
        max_examples=examples,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "task6a_determinism_dev"))


class StaticCanonicalSource:
    """Fresh-source stand-in that exposes one immutable canonical snapshot."""

    def __init__(self, creator_account_id: str, account: AccountReadModel) -> None:
        self.creator_account_id = creator_account_id
        self._account = deepcopy(account)

    def account_exists(self, creator_account_id: str) -> bool:
        return creator_account_id == self.creator_account_id

    def account_read_model(self, creator_account_id: str) -> AccountReadModel:
        assert creator_account_id == self.creator_account_id
        return deepcopy(self._account)

    def account_revisions(self) -> list[tuple[str, int]]:
        return [(self.creator_account_id, self._account.view_revision)]


@st.composite
def canonical_accounts(draw: st.DrawFn) -> AccountReadModel:
    """Canonical finals with order, retention-boundary, and deletion coverage.

    A `deleted` command in the recorded canonical trace is represented by the
    final canonical read model omitting that record, as the production read
    boundary does for tombstoned rows.
    """

    conversation_count = draw(st.integers(min_value=0, max_value=3))
    conversations: dict[str, dict[str, Any]] = {}
    for conversation_index in range(conversation_count):
        message_count = draw(st.integers(min_value=0, max_value=5))
        messages: list[dict[str, Any]] = []
        for ordinal in range(message_count):
            day_offset = draw(st.sampled_from((-91, -90, -89, -2, -1, 0)))
            second_offset = draw(st.integers(min_value=0, max_value=3_600))
            sent_at = EVALUATION_CLOCK + timedelta(days=day_offset, seconds=second_offset)
            messages.append(
                {
                    "message_id": f"c{conversation_index}-m{ordinal}",
                    "source_ordinal": ordinal,
                    "text": draw(st.sampled_from(TEXTS)),
                    "sent_at": sent_at.isoformat(),
                    "direction": draw(st.sampled_from(("inbound", "outbound"))),
                }
            )
        # Deliberately reverse storage order for some examples.  The production
        # pipeline must normalize semantic source order before derivation.
        if draw(st.booleans()):
            messages.reverse()
        conversation_id = f"conversation-{conversation_index}"
        conversations[conversation_id] = {
            "conversation_id": conversation_id,
            "platform_user_id": f"participant-{conversation_index % 2}",
            "display_name": f"Participant {conversation_index % 2}",
            "unread_count": draw(st.integers(min_value=0, max_value=3)),
            "last_message_at": None,
            "messages": messages,
        }
    return AccountReadModel(
        view_revision=draw(st.integers(min_value=1, max_value=15)),
        conversations=conversations,
    )


def _fresh_builds(account: AccountReadModel, *, seed: int | None = 0):
    first_pipeline = AnalyticsPipeline(
        StaticCanonicalSource(CREATOR_ID, account), clock=lambda: EVALUATION_CLOCK
    )
    second_pipeline = AnalyticsPipeline(
        StaticCanonicalSource(CREATOR_ID, account), clock=lambda: EVALUATION_CLOCK
    )
    context = context_for(
        CREATOR_ID,
        account,
        pipeline_revision=first_pipeline.pipeline_revision,
        pipeline_config_digest=first_pipeline.pipeline_config_digest,
        analyzer_provenance=tuple(
            sorted(
                (item.analyzer_name, item.revision, item.config_digest)
                for item in first_pipeline.enrichment.provenance([])
            )
        ),
        evaluation_clock=EVALUATION_CLOCK,
        deterministic_seed=seed,
    )
    first = first_pipeline.rebuild_account(CREATOR_ID).artifact
    second = second_pipeline.rebuild_account(CREATOR_ID).artifact
    return first, second, context


@pytest.mark.stateful_tier_a
class TestAnalyticsDeterminism:
    @given(account=canonical_accounts(), seed=st.integers(min_value=0, max_value=2**32 - 1))
    def test_two_fresh_clean_builds_match(
        self, account: AccountReadModel, seed: int
    ) -> None:
        first, second, context = _fresh_builds(account, seed=seed)
        assert_deterministic_rebuilds(
            first,
            second,
            context,
            expected_refs=expected_active_refs(CREATOR_ID, account, context),
        )
        # A failure can be persisted as this complete JSON-safe record, rather
        # than relying on a Hypothesis example database or process-local state.
        json.dumps(
            reproduction_payload(
                creator_account_id=CREATOR_ID,
                account=account,
                context=context,
                first=first,
                second=second,
            ),
            sort_keys=True,
        )


def _rich_regression_account() -> AccountReadModel:
    """Fixed replay case: repeated entities, two participants, and retention edge."""

    return AccountReadModel(
        view_revision=11,
        conversations={
            "kept": {
                "conversation_id": "kept",
                "platform_user_id": "participant-a",
                "display_name": "A",
                "unread_count": 2,
                "last_message_at": None,
                "messages": [
                    {"message_id": "m1", "source_ordinal": 0, "text": TEXTS[1], "sent_at": "2026-06-03T12:00:01+00:00", "direction": "inbound"},
                    {"message_id": "m2", "source_ordinal": 1, "text": TEXTS[1], "sent_at": "2026-06-03T12:10:00+00:00", "direction": "outbound"},
                    {"message_id": "m3", "source_ordinal": 2, "text": TEXTS[3], "sent_at": "2026-08-31T12:00:00+00:00", "direction": "inbound"},
                ],
            },
            "boundary": {
                "conversation_id": "boundary",
                "platform_user_id": "participant-b",
                "display_name": "B",
                "unread_count": 0,
                "last_message_at": None,
                "messages": [
                    {"message_id": "expired", "source_ordinal": 0, "text": TEXTS[0], "sent_at": "2026-06-03T12:00:00+00:00", "direction": "inbound"},
                ],
            },
            # This is a tombstoned conversation's post-delete canonical shape:
            # no active chat/message reaches the canonical read boundary.
        },
    )


def test_deterministic_regression_preserves_context_and_retention_boundary() -> None:
    account = _rich_regression_account()
    first, second, context = _fresh_builds(account, seed=90210)
    assert_deterministic_rebuilds(
        first,
        second,
        context,
        expected_refs=expected_active_refs(CREATOR_ID, account, context),
    )
    assert {item.message_ref for item in first.projection.message_enrichments}
    assert first.projection.creator_metrics.message_count == 3


def test_oracle_falsifiers_reject_each_6a_family() -> None:
    """Permanent negative controls exercise the same normalization path."""

    first, second, context = _fresh_builds(_rich_regression_account())
    projection = second.projection

    # Message identity changed between clean builds.
    message_identity_broken = second.model_copy(
        update={
            "projection": projection.model_copy(
                update={
                    "message_enrichments": [
                        projection.message_enrichments[0].model_copy(
                            update={"message_ref": "m1:" + "3" * 64}
                        )
                    ]
                    + projection.message_enrichments[1:]
                }
            )
        }
    )
    with pytest.raises(AnalyticsOracleMismatch, match="semantic_projection"):
        assert_deterministic_rebuilds(first, message_identity_broken, context)

    # Graph node identity changed between clean builds.
    identity_broken = second.model_copy(
        update={
            "nodes": [
                second.nodes[0].model_copy(update={"node_id": "g1:" + "1" * 64})
            ]
            + second.nodes[1:]
        }
    )
    with pytest.raises(AnalyticsOracleMismatch, match="referential closure|graph"):
        assert_deterministic_rebuilds(first, identity_broken, context)

    # A semantic metric/enrichment value changed.
    changed_creator = projection.creator_metrics.model_copy(
        update={"message_count": projection.creator_metrics.message_count + 1}
    )
    metric_broken = second.model_copy(
        update={"projection": projection.model_copy(update={"creator_metrics": changed_creator})}
    )
    with pytest.raises(AnalyticsOracleMismatch, match="semantic_projection"):
        assert_deterministic_rebuilds(first, metric_broken, context)

    # A provenance witness changed.
    provenance_broken = second.model_copy(
        update={
            "projection": projection.model_copy(
                update={"canonical_content_digest": "sha256:" + "0" * 64}
            )
        }
    )
    with pytest.raises(AnalyticsOracleMismatch, match="reproducibility context mismatch"):
        assert_deterministic_rebuilds(first, provenance_broken, context)

    # An edge points to a missing node.  Model copies are intentional here so
    # the oracle, rather than Pydantic construction, proves closure detection.
    dangling = second.edges[0].model_copy(update={"target_id": "g1:" + "2" * 64})
    closure_broken = second.model_copy(update={"edges": [dangling] + second.edges[1:]})
    with pytest.raises(AnalyticsOracleMismatch, match="referential closure"):
        assert_deterministic_rebuilds(first, closure_broken, context)


def test_profile_calibration_is_explicit() -> None:
    assert settings.get_profile("task6a_determinism_fast").max_examples == 30
    assert settings.get_profile("task6a_determinism_dev").max_examples == 6


def test_oracle_does_not_import_production_identity_or_digest_helpers() -> None:
    """Keep expected canonical/graph identity calculations independent."""

    oracle = Path(__file__).parents[1] / "state_models" / "analytics_oracle.py"
    tree = ast.parse(oracle.read_text(encoding="utf-8"))
    banned_modules = {
        "app.analytics.identity",
        "app.analytics.opaque_refs",
        "app.analytics.graph_identity",
        "app.analytics.graph_privacy",
    }
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert not imports & banned_modules
