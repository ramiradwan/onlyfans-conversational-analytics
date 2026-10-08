"""Bounded canonical identity checks for saved questions."""

from app.analytics.source_snapshot import scan_identity


def canonical_question_identity(db, account, revision, budget):
    identity, _, _ = scan_identity(db, account, revision,
        check=budget.check, consume=budget.consume, max_text=65_536)
    return identity
