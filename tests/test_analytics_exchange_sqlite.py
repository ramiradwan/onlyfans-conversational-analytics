"""Exercise active SQLite publication with transported synthetic question facts."""

from datetime import timedelta
import sqlite3

import pytest

from app.analytics.exchange_codec import ExchangeInvalid, decode_exchange
from app.analytics.query_execution import QuestionResultInvalid
from tools.analytics_exchange_fixtures import SyntheticSource, load_cases
from tools.analytics_exchange_local import SQLiteExchangeTarget, execute_question

pytestmark = [pytest.mark.ci_tier("integration"), pytest.mark.windows_compat]


@pytest.mark.parametrize("case", load_cases("no-later-reply") + load_cases("pricing-discussions"),
                         ids=lambda case: case["id"])
def test_fresh_sqlite_roundtrip_matches_literal_answers(tmp_path, case):
    first = SQLiteExchangeTarget(tmp_path / "first", SyntheticSource(case))
    second = SQLiteExchangeTarget(tmp_path / "second", SyntheticSource(case))
    try:
        original = first.build()
        raw = first.export(original)
        before = execute_question(first, first.source)
        first.source.assert_expected(before.page)
        imported = second.import_bytes(raw)
        assert second.import_bytes(raw) == imported
        assert second.export(imported) == raw
        after = execute_question(second, second.source)
        second.source.assert_expected(after.page)
        assert after.page == before.page
        assert imported.generation_id != original.generation_id
        first.sidecar.execute("DELETE FROM exchanges")
        first.sidecar.commit()
        assert execute_question(second, second.source).page == after.page
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("checkpoint", ["staged", "projection_published"])
@pytest.mark.parametrize("reopen", [False, True])
def test_interrupted_import_has_no_published_sidecar(tmp_path, checkpoint, reopen):
    case = load_cases("no-later-reply")[0]
    first = SQLiteExchangeTarget(tmp_path / "first", SyntheticSource(case))
    second = SQLiteExchangeTarget(tmp_path / "second", SyntheticSource(case))
    try:
        raw = first.export(first.build())
        def fault(stage):
            if stage == checkpoint:
                raise RuntimeError("synthetic interruption")
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            second.import_bytes(raw, fault=fault)
        assert second.sidecar.execute("SELECT count(*) FROM exchanges WHERE state='ready'").fetchone()[0] == 0
        if reopen:
            second.close()
            second = SQLiteExchangeTarget(tmp_path / "second", SyntheticSource(case))
        receipt = second.import_bytes(raw)
        assert second.export(receipt) == raw
    finally:
        first.close()
        second.close()


def test_cold_reopen_uses_persisted_facts_and_canonical_witness(tmp_path):
    case = load_cases("no-later-reply")[0]
    target = SQLiteExchangeTarget(tmp_path, SyntheticSource(case))
    receipt = target.build()
    raw = target.export(receipt)
    target.close()
    reopened = SQLiteExchangeTarget(tmp_path, SyntheticSource(case))
    try:
        assert reopened.export(receipt) == raw
        reopened.source.assert_expected(execute_question(reopened, reopened.source).page)
    finally:
        reopened.close()


def test_import_cannot_resurrect_deleted_or_expired_source(tmp_path):
    case = load_cases("no-later-reply")[0]
    target = SQLiteExchangeTarget(tmp_path, SyntheticSource(case))
    try:
        receipt = target.build()
        raw = target.export(receipt)
        target.source.case["messages"][0]["deleted"] = True
        with pytest.raises(ExchangeInvalid, match="source_changed"):
            target.export(receipt)
        with pytest.raises(ExchangeInvalid, match="source_changed"):
            target.import_bytes(raw)
        target.source.case["messages"][0]["deleted"] = False
        target.source.now = decode_exchange(raw).snapshot.retention_due_at
        with pytest.raises(ExchangeInvalid):
            target.import_bytes(raw)
    finally:
        target.close()


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("change", ["document", "reinsert"])
def test_prepared_facts_reject_sidecar_mutations(tmp_path, external, change):
    target = SQLiteExchangeTarget(tmp_path, SyntheticSource(load_cases("no-later-reply")[0]))
    try:
        receipt = target.build()
        connection = sqlite3.connect(target.sidecar_path) if external else target.sidecar
        try:
            with connection:
                if change == "document":
                    connection.execute("UPDATE exchanges SET document=? WHERE digest=?", (b"{}", receipt.content_digest))
                else:
                    row = connection.execute("SELECT * FROM exchanges WHERE digest=?", (receipt.content_digest,)).fetchone()
                    connection.execute("DELETE FROM exchanges WHERE digest=?", (receipt.content_digest,))
                    connection.execute("INSERT INTO exchanges VALUES (?,?,?,?)", row)
        finally:
            if external:
                connection.close()
        with pytest.raises(QuestionResultInvalid):
            execute_question(target, target.source)
    finally:
        target.close()
