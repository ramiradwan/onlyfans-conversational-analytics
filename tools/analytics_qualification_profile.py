"""Optional thread-local attribution; instrumented runs never qualify latency."""
from functools import wraps
from pathlib import Path
from threading import Lock, get_ident
import sys
import time

from tools import analytics_qualification as q

# Coarse call boundaries avoid retaining source values or tracing every record.
FUNCTIONS = frozenset({
    'build_candidate', 'publish_candidate', 'stage_artifact', 'stage_built_artifact',
    'scan_identity', '_build_inner', 'assemble', 'enrich_conversation',
    'write_compact_graph', 'write_incremental_graph', 'write_shared_graph',
    'validate', 'recompute_generation', '_validate_generation_links',
    '_read_changed_segment_rows', '_verify_segment_rows', 'verify_shared_graph',
    '_incremental_endpoint_links_valid', 'selected_content_ids',
    '_verify_all_shared_endpoints', '_validate_persisted_generation',
    '_activate_completed_generation', 'collect_garbage',
    'insert_units', 'insert_page_sets', 'resolve_page_sets',
    'load_analyzer_entries', 'verify_generation_units', '_validate_unit',
    'try_append', '_previous_graph', 'create_pages', 'checked_page_sets',
    'create_graph_unit', 'create_enrichment_unit', 'build_conversation_metrics',
    '_canonical_conversations', 'load_enrichment_unit_contents',
    'append_enrichment_unit', 'from_values', '_checked_entries', 'validation_messages',
    '_available_store', '_identity_matches_unlocked', 'store_identity',
    'retain_record', 'prefetch',
    '_quiesce_heartbeat_for_terminal_transition', 'refresh_identity_cache',
})


class ThreadFunctionProfile:
    def __init__(self, root):
        self.root, self.thread = root, get_ident()
        self.active, self.stats = {}, {}

    def observe(self, frame, event, argument):
        if get_ident() != self.thread:
            raise RuntimeError('attribution_thread_changed')
        if event not in ('call', 'return'):
            return
        code = frame.f_code
        if code.co_name not in FUNCTIONS and code is not self.root:
            return
        key = (code.co_filename, code.co_firstlineno, code.co_name)
        identity = id(frame)
        if event == 'call':
            self.active[identity] = (key, time.monotonic())
        else:
            entry = self.active.pop(identity, None)
            if entry is not None:
                previous = self.stats.get(key, (0, 0.0))
                self.stats[key] = (previous[0] + 1,
                    previous[1] + time.monotonic() - entry[1])

    def functions(self):
        return sorted(({'file': file, 'line': line, 'function': name,
            'calls': calls, 'cumulative_seconds': seconds}
            for (file, line, name), (calls, seconds) in self.stats.items()),
            key=lambda row: row['cumulative_seconds'], reverse=True)


def install(work, output: Path):
    lock, sequence = Lock(), [0]
    work.profile_phase = None

    def wrap(method, name):
        @wraps(method)
        def measured(*args, **kwargs):
            phase = work.profile_phase
            if phase is None:
                return method(*args, **kwargs)
            if sys.getprofile() is not None:
                raise RuntimeError('attribution_profiler_already_active')
            with lock:
                sequence[0] += 1
                index = sequence[0]
            profiler = ThreadFunctionProfile(getattr(method, '__code__', None))
            started = time.monotonic()
            sys.setprofile(profiler.observe)
            try:
                return method(*args, **kwargs)
            finally:
                sys.setprofile(None)
                q.write_once(output / 'profiles' / f'{index:04}-{name}.json', {
                    'schema': 'analytics-attribution.v2', 'qualifies_latency': False,
                    'phase': phase, 'method': name,
                    'seconds': time.monotonic() - started,
                    'thread_id': profiler.thread, 'observed': q.stamp(),
                    'functions': profiler.functions()})
        return measured

    for name in ('build_candidate', 'publish_candidate'):
        setattr(work.f.pipeline, name, wrap(getattr(work.f.pipeline, name), name))
