"""Bounded conversation context for an already registered saved finding."""
from app.analytics.evidence import EvidenceUnavailable
from app.analytics.evidence_contracts import EvidenceContextRequest
from app.analytics.nlp_read_projection import read
from app.analytics.query_execution import QuestionBudget, QuestionLimits


def read_context(resources, policy, request, *, cancellation_check=None):
    context_budget, anchor_lease = None, None

    def project(value, records, generation, snapshot, plan, budget, source_rows, *, connection):
        nonlocal context_budget, anchor_lease
        reference = plan.reference.model_copy(update={'span': None})
        present = False
        for item in source_rows:
            budget.check()
            if item['reference'] == reference:
                present = True
                break
        if not present:
            raise EvidenceUnavailable()
        context_budget = QuestionBudget(QuestionLimits(max_records=128,
            wall_clock_ms=max(1, min(5000, int(budget.remaining_seconds() * 1000)))),
            cancellation_check=lambda: budget.check() or False)
        response, anchor_lease = resources.evidence.resolve_context(policy, plan, context_budget)
        return response, []

    def recheck(connection, response, budget):
        budget.check()
        resources.evidence.check_context(policy, response, context_budget, anchor_lease)

    return read(resources, policy, request, cancellation_check=cancellation_check,
                request_model=EvidenceContextRequest, projector=project, response_check=recheck)
