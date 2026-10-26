"""Source validation supplied explicitly to dormant canonical result storage."""
from datetime import datetime, timedelta, timezone
import hashlib

from app.analytics.canonical_source import read_transaction_evidence
from app.analytics.historical_derivation import PARTICIPANT_ANALYTICS_MAX_DAYS
from app.analytics.nlp_findings_store import source_binding
from app.analytics.nlp_record_validation import validate_source_bound_record
from app.analytics.nlp_preview import (
    validate_preview_no_output, validate_preview_output, validate_preview_source,
)
from app.analytics.query_execution import QuestionBudget, QuestionLimits
from app.analytics.source_snapshot import scan_identity


class ContributionSourceGuard:
    def budget(self, *, wall_clock_ms=30_000):
        if type(wall_clock_ms) is not int or not 1 <= wall_clock_ms <= 30_000:
            raise ValueError('knowledge_validation_budget_invalid')
        return QuestionBudget(QuestionLimits(max_records=100_000, wall_clock_ms=wall_clock_ms))

    def origin_budget(self):
        return QuestionBudget(QuestionLimits(max_records=8, wall_clock_ms=1000))

    def source(self, db, spec, member, budget, now):
        source = read_transaction_evidence(db, spec.creator_account_id, member.location.model_dump(), budget)
        if (source is None or source_binding(source) != member.source
                or hashlib.sha256(source.text.encode('utf8')).hexdigest() != member.text_sha256):
            raise ValueError('knowledge_source_changed')
        if source.sent_at + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS) <= datetime.fromtimestamp(now, timezone.utc):
            raise ValueError('knowledge_source_expired')
        return source

    def current_input(self, db, spec, budget):
        head = db.execute('SELECT canonical_revision FROM account_heads WHERE creator_account_id=?',
                          (spec.creator_account_id,)).fetchone()
        if head is None or head[0] != spec.input.canonical_revision:
            raise ValueError('knowledge_input_changed')
        identity, _, _ = scan_identity(db, spec.creator_account_id, head[0], check=budget.check, consume=budget.consume)
        if identity.content_digest.removeprefix('sha256:') != spec.input.canonical_content_sha256:
            raise ValueError('knowledge_input_changed')

    @staticmethod
    def record(profile, source, frame):
        return validate_source_bound_record(profile, source, frame)

    @staticmethod
    def preview_output(spec, profile, sources, frame):
        return validate_preview_output(spec, profile, sources, frame)

    @staticmethod
    def preview_source(output, profile, member, source):
        return validate_preview_source(output, profile, member, source)

    @staticmethod
    def preview_no_output(spec, profile, sources, outcome):
        return validate_preview_no_output(spec, profile, sources, outcome)
