"""Read acquisition evidence and bound historical coverage to a question cutoff."""

from app.analytics.query_contracts import utc_instant


class AcquisitionCoverage:
    def __init__(self, connection, account, *, consume=lambda: None):
        self.connection, self.account, self.consume = connection, account, consume
        consume()
        row = connection.execute("""SELECT g.generation_id,g.state,g.as_of,
                g.inventory_ended_at,g.closed_at
            FROM account_coverage_heads h JOIN coverage_generations g
              ON g.creator_account_id=h.creator_account_id
             AND g.generation_id=h.active_generation_id
            WHERE h.creator_account_id=?""", (account,)).fetchone()
        self.generation = None if row is None else dict(zip(
            ("generation_id", "state", "as_of", "inventory_ended_at", "closed_at"), row))

    def conversation(self, conversation):
        if self.generation is None:
            return None
        self.consume()
        row = self.connection.execute("""SELECT history_started_at,head_reconciled_through
            FROM coverage_members
            WHERE creator_account_id=? AND generation_id=? AND conversation_id=?""",
            (self.account, self.generation["generation_id"], conversation)).fetchone()
        return {**self.generation,
                "history_started_at": None if row is None else row[0],
                "head_reconciled_through": None if row is None else row[1]}


def question_coverage(evidence, question):
    if evidence is None:
        return "unknown"
    if (evidence["state"] != "complete"
            or any(evidence[key] is None for key in (
                "inventory_ended_at", "closed_at", "history_started_at", "head_reconciled_through"))
            or question.selection_clipped_by_retention):
        return "partial"
    as_of = utc_instant(evidence["as_of"])
    if (utc_instant(evidence["head_reconciled_through"]) < as_of
            or question.cutoff > as_of):
        return "partial"
    return "complete"
