"""Bounded full-update attribution for the existing light diagnostic.

No runtime source edits, verification queries, checkpoint changes or row tracing.
"""
from contextlib import contextmanager
from functools import wraps
import hashlib
import inspect
import re
import threading
import time

from tools.analytics_insertion_diagnostic import Attribution

MAX_SPANS = 32768
MAX_SQL_GROUPS = 512


def graph_counts(graph):
    if graph is None:
        return {}
    segments = getattr(graph, 'segments', ())
    changed = [s for s in segments if not s.reused]
    return dict(nodes=len(graph.nodes), edges=len(graph.edges), segments=len(segments),
                changed_segments=len(changed), changed_segment_records=sum(s.count for s in changed),
                changed_keys=sum(len(s.changed_keys) for s in changed),
                reused_segments=len(segments)-len(changed))


def stored_unit_counts(unit):
    if unit is None:
        return {}
    h = unit.header
    return dict(nodes=h.node_count, edges=h.edge_count)


class UpdateAttribution(Attribution):
    def __init__(self, *, enabled=True):
        super().__init__(enabled=enabled, max_events=MAX_SPANS)
        self.sql_groups = {}
        self.missing_attributes = []
        self.extra_patches = []

    def patch(self, owner, name, before=None, after=None):
        if inspect.isclass(owner) and name not in vars(owner):
            self.missing_attributes.append((owner, name))
        return super().patch(owner, name, before, after)

    def context(self, owner, name):
        """Measure context acquisition and release, never charge its body twice."""
        original = getattr(owner, name)
        @contextmanager
        @wraps(original)
        def measured(obj, *args, **kwargs):
            cm = original(obj, *args, **kwargs)
            with self.span(owner.__name__+'.'+name+'.enter'):
                value = cm.__enter__()
            try:
                yield value
            except BaseException as error:
                with self.span(owner.__name__+'.'+name+'.exit'):
                    suppress = cm.__exit__(type(error), error, error.__traceback__)
                if not suppress:
                    raise
            else:
                with self.span(owner.__name__+'.'+name+'.exit'):
                    cm.__exit__(None, None, None)
        self.extra_patches.append((owner, name, original))
        setattr(owner, name, measured)

    def sql(self, owner, name):
        original = getattr(owner, name)
        inherited = name not in vars(owner)
        @wraps(original)
        def measured(connection, statement, *args, **kwargs):
            if not self.enabled:
                return original(connection, statement, *args, **kwargs)
            # Retain a hash of the parameterized statement, never values, SQL,
            # encryption keys, or caller arguments. Key/cipher PRAGMAs excluded.
            text = statement.strip()
            if text.upper().startswith(('PRAGMA KEY', 'PRAGMA CIPHER')):
                return original(connection, statement, *args, **kwargs)
            normalized = re.sub(r'\?(?:,\?)+', '?...', text)
            fingerprint = hashlib.sha256(normalized.encode()).hexdigest()
            stack = getattr(self.local, 'stack', [])
            parent = stack[-1]['name'] if stack else 'unscoped'
            store = getattr(connection, '_tracked_path', None)
            store = store.name if store else 'untracked'
            key = (self.phase, parent, store, name, fingerprint)
            start, cpu = time.monotonic(), time.thread_time()
            error_name = None
            try:
                return original(connection, statement, *args, **kwargs)
            except BaseException as error:
                error_name = type(error).__name__
                raise
            finally:
                elapsed, used = time.monotonic()-start, time.thread_time()-cpu
                with self.lock:
                    if key not in self.sql_groups:
                        if len(self.sql_groups) >= MAX_SQL_GROUPS:
                            raise RuntimeError('full_update_sql_group_capacity_exceeded')
                        self.sql_groups[key] = dict(phase=key[0], parent=parent, store=store,
                            method=name, statement_sha256=fingerprint, calls=0, seconds=0.0,
                            thread_cpu_seconds=0.0, maximum_seconds=0.0, errors=0)
                    group = self.sql_groups[key]
                    group['calls'] += 1
                    group['seconds'] += elapsed
                    group['thread_cpu_seconds'] += used
                    group['maximum_seconds'] = max(group['maximum_seconds'], elapsed)
                    group['errors'] += int(error_name is not None)
        self.extra_patches.append((owner, name, original))
        if inherited:
            self.missing_attributes.append((owner, name))
        setattr(owner, name, measured)

    def install(self, *, pipeline=True):
        super().install(pipeline=True)
        from app.analytics import sqlite_projection_store as projection
        from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore as Store
        from app.analytics.sqlite_graph_store import SQLiteGraphGenerationWriter as Writer
        from app.analytics.pipeline import AnalyticsPipeline
        from app.analytics import incremental_graph as incremental
        from app.analytics import conversation_reuse, shared_graph, conversation_page_sql
        from app.analytics import conversation_graph_unit_sql, conversation_enrichment_unit_sql
        from app.analytics import conversation_membership_validation, conversation_integrity_store
        from app.analytics import graph_membership_pages, conversation_graph_insertion
        from app.analytics import projection_verification, projection_encoding
        from app.analytics import source_tokens
        from app.persistence.database import LocalSQLite, _TrackedConnection
        from app.persistence.projection_activation import ProjectionActivationRepository

        self.patch(AnalyticsPipeline, '_build_inner')
        self.patch(AnalyticsPipeline, '_capture_source')
        self.context(AnalyticsPipeline, '_account_lock')
        self.patch(conversation_reuse, 'assemble')
        self.patch(incremental, 'build_incremental_graph', after=graph_counts)
        self.patch(incremental, '_records_from_verified_chunk',
                   lambda a,k: dict(chunk_bytes=len(a[1])), lambda r: dict(records=len(r[0])))
        self.patch(incremental, '_segment_value', lambda a,k: dict(records=len(a[2])))
        self.patch(incremental, 'write_incremental_graph',
                   lambda a,k: graph_counts(a[1]), after=None)
        self.patch(projection, 'write_compact_graph', lambda a,k: graph_counts(a[1]))
        self.patch(shared_graph, 'write_shared_graph')
        self.patch(shared_graph, 'selected_content_ids',
                   lambda a,k: dict(requested_keys=len(a[4])), lambda r: dict(returned_keys=len(r)))
        self.patch(shared_graph, 'verified_segment_chunk', after=lambda r:
                   {} if r is None else dict(records=r[0].count, chunk_bytes=len(r[1])))
        self.patch(shared_graph, '_read_changed_segment_rows', lambda a,k:
                   dict(planned_records=sum(x.count for x in a[2])), lambda r:
                   {} if r is None else dict(read_records=sum(len(x) for x in r.values())))
        self.patch(shared_graph, '_verified_changed_segment_chunks', lambda a,k:
                   {} if a[2] is None else dict(plans=len(a[2].plans),
                       changed_plans=sum(not x.reused for x in a[2].plans),
                       changed_records=sum(x.count for x in a[2].plans if not x.reused),
                       changed_keys=sum(len(x.changed_keys) for x in a[2].plans if not x.reused)))
        for name in ('verify_shared_graph', '_incremental_endpoint_links_valid',
                     '_verify_all_shared_endpoints', 'verify_segment_links'):
            self.patch(shared_graph, name)
        self.patch(conversation_integrity_store, 'verify_generation_integrity')
        self.patch(conversation_membership_validation, 'changed_predecessor_members')
        self.patch(conversation_membership_validation, 'predecessor_manifest_matches')
        self.patch(conversation_membership_validation, 'proven_unit_is_unchanged')
        self.patch(conversation_graph_unit_sql, 'load_unit', after=stored_unit_counts)
        self.patch(conversation_graph_unit_sql, 'insert_units', lambda a,k:
                   dict(units=len(a[2])) if hasattr(a[2], '__len__') else {})
        self.patch(conversation_enrichment_unit_sql, 'insert_units', lambda a,k:
                   dict(units=len(a[2])) if hasattr(a[2], '__len__') else {})
        self.patch(conversation_enrichment_unit_sql, 'verify_generation_units', lambda a,k:
                   dict(proof_argument_present=a[3] is not None, materialize=k.get('materialize',False)))
        self.patch(conversation_page_sql, 'insert_page_sets')
        # resolve_page_sets is lazy; its work is inside insert_page_sets.
        self.patch(conversation_page_sql, 'load_pages', after=lambda r:
                   {} if r is None else dict(pages=len(r.pages)))
        self.patch(graph_membership_pages, 'prepare_pages', lambda a,k:
                   dict(records=len(a[4])), lambda r: dict(pages=len(r),
                       reused_pages=sum(p.reused for p in r.values())))
        self.patch(graph_membership_pages, 'write_members', lambda a,k:
                   dict(members=len(a[2])) if hasattr(a[2], '__len__') else {})
        self.patch(projection_verification, 'verify_projection_document')
        self.patch(projection, 'projection_storage_document')
        self.patch(projection, 'recompute_generation', lambda a,k: dict(
                   materialize_projection=k.get('materialize_projection',True),
                   graph_proof=k.get('graph_validation') is not None,
                   enrichment_proof=k.get('enrichment_validation') is not None,
                   conversation_proof=k.get('conversation_validation') is not None))
        self.patch(projection, '_validate_generation_links')
        for name in ('stage_artifact', 'publish_generation', '_activate_completed_generation',
                     'collect_garbage', 'prepare_current_verification_envelope',
                     '_remember_conversation_graph_proof','_remember_conversation_enrichment_proof'):
            self.patch(Store, name)
        for name in ('write_stats', 'validate', '_quiesce_heartbeat_for_terminal_transition'):
            self.patch(Writer, name)
        self.context(Writer, 'lease_session')
        self.context(Writer, '_owned_transaction')
        for name in ('get', 'complete', 'reserve'):
            if hasattr(ProjectionActivationRepository,name):
                self.patch(ProjectionActivationRepository,name)
        self.patch(source_tokens.SourceIdentityCache,'get')
        self.patch(source_tokens.SourceIdentityCache,'preparation_due')
        self.patch(LocalSQLite,'connect')
        self.context(LocalSQLite,'read')
        self.context(LocalSQLite,'transaction')
        self.patch(_TrackedConnection,'commit')
        self.patch(_TrackedConnection,'rollback')
        self.patch(_TrackedConnection,'close')
        self.patch(_TrackedConnection,'_close_native')
        self.sql(_TrackedConnection,'execute')
        self.sql(_TrackedConnection,'executemany')

    def snapshot(self):
        with self.lock:
            return dict(schema='a07-full-update-attribution.v1',
                        events=sorted(self.events,key=lambda e:e['id']),
                        sql_groups=list(self.sql_groups.values()),
                        limits=dict(spans=MAX_SPANS,sql_groups=MAX_SQL_GROUPS),
                        sql_scope='execute/executemany only; fetch and iteration remain in enclosing bulk spans',
                        self_time_note='Self time subtracts nested synchronous spans on the same thread only; SQL groups overlap spans.')

    def restore(self):
        for owner,name,original in reversed(self.extra_patches):
            setattr(owner,name,original)
        self.extra_patches.clear()
        super().restore()
        for owner,name in reversed(self.missing_attributes):
            if name in vars(owner):
                delattr(owner,name)
        self.missing_attributes.clear()
