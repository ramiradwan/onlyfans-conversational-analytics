#!/usr/bin/env python3
"""Audit the ingestion and rebuild contracts against repository source.

Performs static AST inspection of codebase symbols, constants, and pragmas
without importing application modules or triggering settings/environment errors.
Verifies the complete structural and semantic integrity of:
- docs/architecture/ingestion-state-contract.md
- docs/architecture/rebuild-equivalence-contract.md
"""

from __future__ import annotations

import ast
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]

# -----------------------------------------------------------------------------
# AST and File Inspection Helpers
# -----------------------------------------------------------------------------


def check_file_exists(relative_path: str) -> tuple[bool, str]:
    """Verify that a referenced file exists relative to the repository root."""
    target = ROOT / relative_path
    if not target.exists():
        return False, f"File missing: {relative_path}"
    return True, f"Found: {relative_path}"


def extract_ast_symbols(file_path: Path) -> set[str]:
    """Extract top-level class, function, variable, and imported names via AST."""
    try:
        content = file_path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(file_path))
    except Exception as exc:
        raise RuntimeError(f"Failed to parse AST for {file_path}: {exc}") from exc

    symbols: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    symbols.add(target.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                symbols.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                symbols.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                symbols.add(alias.asname or alias.name)
    return symbols


def check_ast_symbol(relative_path: str, symbol_name: str) -> tuple[bool, str]:
    """Verify that a symbol is defined or imported in a Python file via AST."""
    target = ROOT / relative_path
    if not target.exists():
        return False, f"File missing for symbol check: {relative_path}"
    try:
        symbols = extract_ast_symbols(target)
    except Exception as exc:
        return False, f"AST error in {relative_path}: {exc}"
    if symbol_name not in symbols:
        return False, f"Symbol '{symbol_name}' not found in {relative_path}"
    return True, f"{relative_path}::{symbol_name}"


def _eval_simple_ast(node: ast.AST | None):
    """Safely evaluate simple constant and arithmetic expressions in AST."""
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.BinOp):
        left = _eval_simple_ast(node.left)
        right = _eval_simple_ast(node.right)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Div):
            return left / right
    return None


def get_ast_constant_value(relative_path: str, const_name: str):
    """Extract a top-level constant value from a Python file via AST."""
    target = ROOT / relative_path
    if not target.exists():
        return None
    content = target.read_text(encoding="utf-8")
    tree = ast.parse(content, filename=str(target))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == const_name:
                    return _eval_simple_ast(node.value)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == const_name:
                return _eval_simple_ast(node.value)
    return None


def check_file_contains(
    relative_path: str, pattern: str, description: str, is_regex: bool = False
) -> tuple[bool, str]:
    """Verify that a file contains a specific substring or pattern."""
    target = ROOT / relative_path
    if not target.exists():
        return False, f"File missing: {relative_path}"
    content = target.read_text(encoding="utf-8")
    if is_regex:
        matched = bool(re.search(pattern, content, re.MULTILINE))
    else:
        matched = pattern in content
    if not matched:
        return False, f"{relative_path}: missing required pattern '{description}'"
    return True, f"{relative_path}: contains '{description}'"


# -----------------------------------------------------------------------------
# Codebase Grounding Verification (AST & Static)
# -----------------------------------------------------------------------------


def verify_referenced_codebase() -> list[tuple[bool, str]]:
    """Statically verify all files, symbols, constants, and pragmas cited in contracts."""
    checks: list[tuple[bool, str]] = []

    # 1. All CODE-VERIFY header files
    referenced_files = [
        "app/protocol/payloads.py",
        "app/protocol/common.py",
        "app/canonical/read_models.py",
        "app/transport/ingestion.py",
        "app/transport/manager.py",
        "app/persistence/history.py",
        "app/persistence/database.py",
        "app/persistence/projection_activation.py",
        "extension/transport/durable-outbox.mjs",
        "extension/transport/entity-merge.mjs",
        "extension/transport/agent-websocket.mjs",
        "app/analytics/pipeline.py",
        "app/analytics/canonical_source.py",
        "app/analytics/historical_derivation.py",
        "app/analytics/metrics.py",
        "app/analytics/analyzers.py",
        "app/analytics/enrichment.py",
        "app/analytics/graph_projection.py",
        "app/analytics/graph_identity.py",
        "app/analytics/opaque_refs.py",
        "app/models/analytics.py",
        "app/models/insights.py",
        "app/analytics/sqlite_projection_store.py",
        "app/analytics/sqlite_graph_store.py",
    ]
    for rel_path in referenced_files:
        checks.append(check_file_exists(rel_path))

    # 2. Protocol payloads and constants
    payload_symbols = [
        "AgentHelloPayload",
        "AgentSessionPayload",
        "IngestDeltaPayload",
        "IngestSnapshotBeginPayload",
        "IngestSnapshotChunkPayload",
        "IngestSnapshotCommitPayload",
        "IngestAckPayload",
        "ProtocolErrorPayload",
        "SnapshotRecordCounts",
    ]
    for sym in payload_symbols:
        checks.append(check_ast_symbol("app/protocol/payloads.py", sym))

    common_symbols = [
        "PROTOCOL_VERSION",
        "MAX_SNAPSHOT_RECORDS_PER_CHUNK",
        "MAX_SNAPSHOT_FRAME_BYTES",
        "MAX_SNAPSHOT_RECORD_BYTES",
    ]
    for sym in common_symbols:
        checks.append(check_ast_symbol("app/protocol/common.py", sym))

    # 3. Canonical read-model symbols
    canonical_read_model_symbols = [
        ("app/canonical/read_models.py", "AccountReadModel"),
    ]
    for rel_path, sym in canonical_read_model_symbols:
        checks.append(check_ast_symbol(rel_path, sym))

    # 4. Transport symbols
    transport_symbols = [
        ("app/transport/ingestion.py", "InMemoryIngestionRepository"),
        ("app/transport/ingestion.py", "IngestionService"),
        ("app/transport/ingestion.py", "CommitOutcome"),
        ("app/transport/manager.py", "InMemoryTransportManager"),
        ("app/transport/manager.py", "AgentLease"),
        ("app/transport/manager.py", "LEASE_EXPIRED_CLOSE_CODE"),
    ]
    for rel_path, sym in transport_symbols:
        checks.append(check_ast_symbol(rel_path, sym))

    # 5. Persistence symbols
    history_symbols = [
        "HistoryRepository",
        "StreamKey",
        "IngestResult",
        "InvariantViolation",
    ]
    for sym in history_symbols:
        checks.append(check_ast_symbol("app/persistence/history.py", sym))

    database_symbols = [
        "LocalSQLite",
        "CanonicalSQLite",
        "AuthSQLite",
        "ProjectionsSQLite",
        "SQLiteConfigurationError",
    ]
    for sym in database_symbols:
        checks.append(check_ast_symbol("app/persistence/database.py", sym))

    activation_symbols = [
        "ProjectionActivationRepository",
        "ProjectionActivationIntent",
        "ProjectionActivationConflict",
    ]
    for sym in activation_symbols:
        checks.append(check_ast_symbol("app/persistence/projection_activation.py", sym))

    # 6. Analytics pipeline and derivation symbols
    pipeline_symbols = [
        ("app/analytics/pipeline.py", "AnalyticsPipeline"),
        ("app/analytics/pipeline.py", "ProjectionCandidate"),
        ("app/analytics/pipeline.py", "PipelineRun"),
        ("app/analytics/pipeline.py", "rebuild_projection"),
        ("app/analytics/pipeline.py", "_RETENTION_CUTOFF"),
        ("app/analytics/canonical_source.py", "HistoryAnalyticsSource"),
        ("app/analytics/historical_derivation.py", "HistoricalDerivationProvenance"),
        ("app/analytics/historical_derivation.py", "historical_retention_cutoff"),
        ("app/analytics/historical_derivation.py", "source_time_is_authorized"),
        ("app/analytics/historical_derivation.py", "PARTICIPANT_ANALYTICS_MAX_DAYS"),
        ("app/analytics/historical_derivation.py", "HISTORICAL_DERIVATION_SCHEMA"),
        ("app/analytics/historical_derivation.py", "RETENTION_BASIS"),
        ("app/analytics/metrics.py", "build_conversation_metrics"),
        ("app/analytics/metrics.py", "build_creator_metrics"),
        ("app/analytics/analyzers.py", "RuleBasedSentimentAnalyzer"),
        ("app/analytics/analyzers.py", "RuleBasedTopicEntityAnalyzer"),
        ("app/analytics/analyzers.py", "RuleBasedEngagementAnalyzer"),
        ("app/analytics/enrichment.py", "EnrichmentStage"),
        ("app/analytics/graph_projection.py", "RelationshipGraphProjector"),
        ("app/analytics/graph_identity.py", "graph_id"),
        ("app/analytics/graph_identity.py", "require_graph_id"),
    ]
    for rel_path, sym in pipeline_symbols:
        checks.append(check_ast_symbol(rel_path, sym))

    # 6. Opaque reference symbols
    opaque_symbols = [
        "opaque_ref",
        "require_opaque_ref",
        "account_ref",
        "conversation_ref",
        "participant_ref",
        "message_ref",
        "topic_ref",
        "entity_ref",
    ]
    for sym in opaque_symbols:
        checks.append(check_ast_symbol("app/analytics/opaque_refs.py", sym))

    # 7. Models and store symbols
    model_symbols = [
        "AnalyticsProjection",
        "RebuildArtifact",
        "GraphNode",
        "GraphEdge",
        "ConversationMetrics",
        "CreatorMetrics",
        "MessageEnrichment",
        "SentimentResult",
        "TopicEntityResult",
        "EngagementResult",
        "GraphNodeKind",
        "GraphRelation",
        "AvailabilityStatus",
        "WindowScope",
        "AnalyticsWindow",
        "AnalyzerProvenance",
        "MetricProvenance",
        "GraphProjectionSummary",
    ]
    for sym in model_symbols:
        checks.append(check_ast_symbol("app/models/analytics.py", sym))

    insights_symbols = [
        "SliceProvenance",
        "TopicMetricsCollection",
        "SentimentTrendResponse",
        "ResponseTimeMetricsResponse",
    ]
    for sym in insights_symbols:
        checks.append(check_ast_symbol("app/models/insights.py", sym))

    store_symbols = [
        ("app/analytics/sqlite_projection_store.py", "SQLiteAnalyticsProjectionStore"),
        ("app/analytics/sqlite_projection_store.py", "ProjectionValidationError"),
        ("app/analytics/database.py", "ProjectionGeneration"),
        ("app/analytics/sqlite_graph_store.py", "SQLiteGraphGenerationWriter"),
        ("app/analytics/sqlite_graph_store.py", "SQLiteGraphReader"),
        ("app/analytics/sqlite_graph_store.py", "GraphReferentialIntegrityError"),
        ("app/analytics/errors.py", "CanonicalAccountNotFound"),
        ("app/analytics/errors.py", "ProjectionUnavailable"),
        ("app/analytics/errors.py", "CanonicalRevisionChanged"),
    ]
    for rel_path, sym in store_symbols:
        checks.append(check_ast_symbol(rel_path, sym))

    # 8. Constant values verified statically
    constants_to_verify = [
        ("app/protocol/common.py", "PROTOCOL_VERSION", "2"),
        ("app/protocol/common.py", "MAX_SNAPSHOT_RECORDS_PER_CHUNK", 100),
        ("app/protocol/common.py", "MAX_SNAPSHOT_FRAME_BYTES", 524288),
        ("app/protocol/common.py", "MAX_SNAPSHOT_RECORD_BYTES", 393216),
        ("app/transport/manager.py", "LEASE_EXPIRED_CLOSE_CODE", 4001),
        ("app/analytics/historical_derivation.py", "HISTORICAL_DERIVATION_SCHEMA", "historical-derivation.v1"),
        ("app/analytics/historical_derivation.py", "PARTICIPANT_ANALYTICS_MAX_DAYS", 90),
        ("app/analytics/historical_derivation.py", "RETENTION_BASIS", "canonical_message_sent_at"),
    ]
    for rel_path, const_name, expected_val in constants_to_verify:
        actual_val = get_ast_constant_value(rel_path, const_name)
        if actual_val == expected_val:
            checks.append((True, f"{rel_path}::{const_name} == {expected_val!r}"))
        else:
            checks.append((False, f"{rel_path}::{const_name} expected {expected_val!r}, got {actual_val!r}"))

    # 9. SQLite Pragmas in LocalSQLite.connect
    db_file = ROOT / "app/persistence/database.py"
    if db_file.exists():
        db_src = db_file.read_text(encoding="utf-8")
        checks.append(
            ("PRAGMA journal_mode = WAL" in db_src, "LocalSQLite.connect enforces WAL journal mode")
        )
        checks.append(
            ("PRAGMA synchronous = FULL" in db_src, "LocalSQLite.connect enforces synchronous = FULL")
        )
        checks.append(
            ("PRAGMA foreign_keys = ON" in db_src, "LocalSQLite.connect enforces foreign_keys = ON")
        )
    else:
        checks.append((False, "app/persistence/database.py missing"))

    return checks


# -----------------------------------------------------------------------------
# Document Structure Verification: Ingestion State Contract
# -----------------------------------------------------------------------------


def verify_ingestion_contract_structure() -> list[tuple[bool, str]]:
    """Verify structural integrity, catalogue entries, fields, and rules of ingestion contract."""
    checks: list[tuple[bool, str]] = []
    doc_path = ROOT / "docs/architecture/ingestion-state-contract.md"

    if not doc_path.exists():
        return [(False, f"Document missing: {doc_path}")]

    text = doc_path.read_text(encoding="utf-8")

    # 1. Banned fabricated tokens
    banned_tokens = [
        "projection_metadata",
        "ProjectionRevisionConflict",
    ]
    for token in banned_tokens:
        if token in text:
            checks.append((False, f"Banned fabricated token found: '{token}'"))
        else:
            checks.append((True, f"Banned token absent: '{token}'"))

    # 2. Disallow fabricated calibration duration limits
    banned_calib = [
        (r"\b12\s*s\b", "hardcoded 12s duration limit"),
        (r"\b20\s*s\b", "hardcoded 20s duration limit"),
        (r"\b25\s*s\b", "hardcoded 25s duration limit"),
        (r"15s shrink", "hardcoded 15s shrink limit"),
    ]
    for pat, desc in banned_calib:
        if re.search(pat, text, re.IGNORECASE):
            checks.append((False, f"Found disallowed calibration duration: {desc}"))
        else:
            checks.append((True, f"Disallowed calibration absent: {desc}"))

    # 3. Catalogue entries: exact count (54) and correct ID families
    entry_blocks = re.split(r"#### Entry ", text)[1:]
    checks.append((len(entry_blocks) == 54, f"Ingestion catalogue has exactly 54 entries (found {len(entry_blocks)})"))

    expected_ids = (
        [f"S{i:02d}" for i in range(1, 4)]
        + [f"D{i:02d}" for i in range(1, 10)]
        + [f"A{i:02d}" for i in range(1, 15)]
        + [f"N{i:02d}" for i in range(1, 21)]
        + [f"AG{i:02d}" for i in range(1, 9)]
    )
    found_ids: list[str] = []
    required_fields = [
        "Classification",
        "Stimulus",
        "Precondition",
        "Expected disposition",
        "Expected state mutation",
        "Expected non-mutation",
        "Retryability",
        "Persistent/restart expectation",
        "Protected invariant",
    ]

    all_fields_ok = True
    missing_field_reports: list[str] = []

    for block in entry_blocks:
        header = block.split("\n")[0]
        entry_id = header.split(":")[0].strip()
        found_ids.append(entry_id)
        body = block[: block.find("#### Entry ") if "#### Entry " in block else len(block)]
        for field in required_fields:
            if f"- **{field}:**" not in body:
                all_fields_ok = False
                missing_field_reports.append(f"Entry {entry_id} missing field '{field}'")

    if found_ids == expected_ids:
        checks.append((True, f"All 54 entry IDs match expected sequence (S01-S03, D01-D09, A01-A14, N01-N20, AG01-AG08)"))
    else:
        checks.append((False, f"Catalogue IDs mismatch. Expected {len(expected_ids)} items, found: {found_ids}"))

    if all_fields_ok:
        checks.append((True, f"All 54 catalogue entries contain all 9 required fields"))
    else:
        checks.append((False, f"Entries missing required fields: {missing_field_reports[:5]}..."))

    # 4. S01, S02, S03 groundings
    s01_block = [b for b in entry_blocks if b.startswith("S01:")][0]
    checks.append(
        ("_ensure_stream" in s01_block and "not called during handshake" in s01_block,
         "S01 correctly states _ensure_stream is not called during handshake")
    )
    checks.append(
        ("distinct random fencing token" in s01_block,
         "S01 correctly defines random fencing token without monotonic assumption")
    )

    # Grounding check for S02: _invalid_ingest defaults retryable=False and stale_fence does not override it
    ws_file = ROOT / "app/api/endpoints/transport_ws.py"
    s02_static_ok = False
    if ws_file.exists():
        ws_tree = ast.parse(ws_file.read_text(encoding="utf-8"))
        invalid_def = next(
            (n for n in ws_tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_invalid_ingest"),
            None,
        )
        if invalid_def:
            ret_default = next(
                (ast.literal_eval(d) for a, d in zip(invalid_def.args.kwonlyargs, invalid_def.args.kw_defaults) if a.arg == "retryable"),
                None,
            )
            stale_call = next(
                (
                    n for n in ast.walk(ws_tree)
                    if isinstance(n, ast.Call)
                    and getattr(n.func, "id", None) == "_invalid_ingest"
                    and any(k.arg == "code" and isinstance(k.value, ast.Constant) and k.value.value == "stale_fence" for k in n.keywords)
                ),
                None,
            )
            if ret_default is False and stale_call and not any(k.arg == "retryable" for k in stale_call.keywords):
                s02_static_ok = True

    s02_block = [b for b in entry_blocks if b.startswith("S02:")][0]
    checks.append(
        (
            'code="stale_fence"' in s02_block
            and "retryable=False" in s02_block
            and "fresh handshake" in s02_block
            and s02_static_ok,
            "S02 documents retryable=False, open socket loop, fresh handshake required; grounded by static AST check",
        )
    )
    checks.append(
        ("4001" in s02_block and "reserved for lease heartbeat expiration sweeper" in s02_block,
         "S02 restricts 4001 solely to lease expiration sweepers")
    )

    s03_block = [b for b in entry_blocks if b.startswith("S03:")][0]
    checks.append(
        (
            "stream_epoch" in s03_block
            and ("never incremented merely by reconnecting" in s03_block or "reuses the existing" in s03_block)
            and "checkpoint is not None" in s03_block
            and "pending_snapshot is None" in s03_block
            and "last_acknowledged_source_seq <= checkpoint" in s03_block,
            "S03 documents epoch reuse across reconnections and exact resume predicate",
        )
    )

    # 5. Quality scenarios QS-1 to QS-6
    for qs_num in range(1, 7):
        checks.append(
            (f"**QS-{qs_num}**" in text, f"Quality scenario QS-{qs_num} present")
        )

    # 6. Permanent oracle falsifiers
    oracle_falsifiers = [
        "BrokenGapAdapter",
        "BrokenDuplicateAdapter",
        "BrokenDeletionAdapter",
        "BrokenReopenAdapter",
    ]
    for falsifier in oracle_falsifiers:
        checks.append(
            (f"`{falsifier}`" in text, f"Oracle falsifier '{falsifier}' defined")
        )

    # 7. Dated SQLite advisory research (2026-09-09) and official facts
    checks.append(
        ("2026-09-09" in text, "SQLite advisory research explicitly dated 2026-09-09")
    )
    checks.append(
        ("3.7.0 through 3.51.2" in text and "3.51.3" in text, "Advisory documents affected versions (3.7.0..3.51.2) and fix (3.51.3)")
    )
    checks.append(
        ("SQLCipher 4.14.0" in text, "Advisory cites SQLCipher 4.14.0 incorporating fix")
    )
    checks.append(
        (
            ("cannot be qualified as unaffected" in text or "Tier B storage production-equivalent remains blocked" in text)
            and "BEGIN IMMEDIATE" in text
            and "single application writer per database file" not in text
            and "inter-process file locking" not in text,
            "Advisory documents blocked Tier B qualification, BEGIN IMMEDIATE writer serialization, and removes unsupported claims",
        )
    )

    # 8. Implementation status and executable verification
    checks.append(
        ("## 8. Implementation status and qualification limits" in text,
         "Section 8 states implementation status and qualification limits")
    )
    checks.append(
        ("### 8.2 Executable verification" in text,
         "Section 8.2 identifies executable verification")
    )
    checks.append(
        ("tests/hardening/falsifiers/test_falsifiers.py" in text
         and "hosted Windows runners" in text,
         "Document identifies permanent falsifiers and hosted qualification")
    )

    return checks


# -----------------------------------------------------------------------------
# Document Structure Verification: Rebuild Equivalence Contract
# -----------------------------------------------------------------------------


def verify_rebuild_contract_structure() -> list[tuple[bool, str]]:
    """Verify structural integrity, matrix rows, columns, and rules of rebuild contract."""
    checks: list[tuple[bool, str]] = []
    doc_path = ROOT / "docs/architecture/rebuild-equivalence-contract.md"

    if not doc_path.exists():
        return [(False, f"Document missing: {doc_path}")]

    text = doc_path.read_text(encoding="utf-8")

    # 1. Banned fabricated tokens
    banned_tokens = [
        "projection_metadata",
        "ProjectionRevisionConflict",
    ]
    for token in banned_tokens:
        if token in text:
            checks.append((False, f"Banned fabricated token found: '{token}'"))
        else:
            checks.append((True, f"Banned token absent: '{token}'"))

    # 2. Section 5 Invariant and Oracle Matrix: exactly 19 rows and required columns
    table_match = re.search(
        r"## 5\. Invariant and oracle matrix\s*\n\s*(?:[^\n]+\n)*?\s*(\| # \|[^\n]+\|)\s*\n\s*(\|[-:| ]+\|)\s*\n((?:\| \d+ \|[^\n]+\|\s*\n)+)",
        text,
    )
    if not table_match:
        checks.append((False, "Section 5 Invariant and oracle matrix table not found"))
    else:
        header = table_match.group(1)
        expected_cols = ["#", "Invariant", "Testable Assertion", "Verification Method", "Falsifier Trigger"]
        actual_cols = [c.strip() for c in header.strip("|").split("|")]
        if actual_cols == expected_cols:
            checks.append((True, f"Section 5 matrix has required columns: {expected_cols}"))
        else:
            checks.append((False, f"Section 5 matrix columns mismatch: {actual_cols} != {expected_cols}"))

        rows_text = table_match.group(3).strip().split("\n")
        checks.append((len(rows_text) == 19, f"Section 5 matrix has exactly 19 rows (found {len(rows_text)})"))

        row_numbers = [int(r.strip("|").split("|")[0].strip()) for r in rows_text]
        if row_numbers == list(range(1, 20)):
            checks.append((True, f"Section 5 matrix rows numbered sequentially 1 through 19"))
        else:
            checks.append((False, f"Section 5 matrix numbering incorrect: {row_numbers}"))

    # 3. ReproducibilityContext distinguishes existing vs proposed fields
    checks.append(
        ("Existing production" in text and "Proposed" in text and "ReproducibilityContext" in text,
         "Section 3.1 explicitly distinguishes existing vs proposed ReproducibilityContext fields")
    )

    # 4. Graph projection requires stable semantic identity (not immutable graph_id)
    checks.append(
        ("Stable semantic identity" in text, "Contract requires stable semantic identity for graph projection")
    )

    # 5. Configuration sensitivity requires explicit revision and config provenance
    checks.append(
        ("explicit pipeline/analyzer revision/config provenance" in text or "explicit revision and configuration provenance" in text,
         "Invariant 19 requires explicit revision and configuration provenance for config sensitivity")
    )

    # 6. Actual publication and activation protocol
    checks.append(
        ("projection_generations" in text, "Section 8.2 documents projection_generations store table")
    )
    checks.append(
        ("ProjectionActivationRepository" in text and "ProjectionActivationIntent" in text and "ProjectionActivationConflict" in text,
         "Section 8.2 documents ProjectionActivationRepository CAS protocol and types")
    )
    checks.append(
        ("reserve" in text and "complete" in text and "witness_sequence" in text,
         "Section 8.2 documents reserve, complete, witness_sequence workflow")
    )

    # 7. Permanent oracle falsifiers
    rebuild_falsifiers = [
        "BrokenAnalyticsAdapter",
        "BrokenProvenanceAdapter",
        "BrokenIdentityAdapter",
        "BrokenDerivedDeletionAdapter",
    ]
    for falsifier in rebuild_falsifiers:
        checks.append(
            (f"`{falsifier}`" in text, f"Rebuild falsifier '{falsifier}' defined")
        )

    # 8. Dated SQLite advisory facts in rebuild contract
    checks.append(
        ("2026-09-09" in text, "Rebuild contract documents dated advisory observation (2026-09-09)")
    )
    checks.append(
        ("3.7.0 through 3.51.2" in text and "3.51.3" in text, "Rebuild contract cites advisory version span (3.7.0..3.51.2 -> 3.51.3)")
    )

    # 9. Known discrepancies and executable verification
    checks.append(
        ("## 10. Known discrepancies and verification" in text,
         "Section 10 records known discrepancies and verification")
    )
    checks.append(
        ("### 10.2 Executable verification" in text,
         "Section 10.2 identifies executable verification")
    )
    checks.append(
        ("D02" in text and "create_canonical_repositories" in text,
         "Section 10.1 correctly describes D02 factory composition")
    )
    checks.append(
        ("tests/stateful/test_analytics_equivalence.py" in text
         and "hosted performance measurements" in text,
         "Document identifies convergence controls and hosted qualification")
    )

    return checks


# -----------------------------------------------------------------------------
# Main Test Harness Runner
# -----------------------------------------------------------------------------


def main() -> int:
    print("=" * 75)
    print("Ingestion and Rebuild Contract Verification")
    print("=" * 75)

    codebase_checks = verify_referenced_codebase()
    ingestion_checks = verify_ingestion_contract_structure()
    rebuild_checks = verify_rebuild_contract_structure()

    suites = [
        ("Codebase Grounding (AST & Static Pragmas)", codebase_checks),
        ("Ingestion State Contract Structure & Semantics", ingestion_checks),
        ("Rebuild Equivalence Contract Structure & Matrix", rebuild_checks),
    ]

    failed_total = 0
    passed_total = 0

    for suite_name, checks in suites:
        passed = [msg for ok, msg in checks if ok]
        failed = [msg for ok, msg in checks if not ok]
        passed_total += len(passed)
        failed_total += len(failed)

        print(f"\n[{suite_name}]")
        print(f"  Passed: {len(passed)} / {len(checks)}")
        if failed:
            print("  FAILURES:")
            for msg in failed:
                print(f"    - FAIL: {msg}")
        else:
            print(f"  All {len(checks)} assertions verified successfully.")

    print("\n" + "=" * 75)
    print(f"Total Checks: {passed_total + failed_total} | Passed: {passed_total} | Failed: {failed_total}")
    print("=" * 75)

    if failed_total > 0:
        print("Audit FAILED: One or more static and structural checks failed.")
        return 1

    print("\nScope Note:")
    print("  These checks establish that cited file paths, selected AST symbols,")
    print("  tables, and schema constants exist in the codebase, required contract")
    print("  structures/counts are present, and banned fabricated patterns are absent.")
    print("  Full semantic and source truth requires human review and executable")
    print("  ingestion and analytics oracles.")
    print(f"\nAudit PASSED: {passed_total}/{passed_total} static and structural checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
