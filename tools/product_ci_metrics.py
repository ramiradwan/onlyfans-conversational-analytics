"""Report actual CI runner occupancy and latency, never release qualification.

Inputs: complete GitHub run.json and filter=all jobs.json per run. Keep failed,
retried and successful runs separate. The result describes only supplied runs,
not account-wide runner availability or a population percentile.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import statistics
import subprocess
import re
from itertools import product

try:
    from tools import browser_ci_metrics as common
except ModuleNotFoundError:
    import browser_ci_metrics as common


def workflow_graph(workflow, *, latest=None):
    """Expand the static matrices used by these workflows, refusing ambiguity."""
    jobs = workflow['jobs']
    expanded = {}
    for key, job in jobs.items():
        matrix = job.get('strategy', {}).get('matrix', {})
        if not isinstance(matrix, dict) or not all(isinstance(v, list) and v for v in matrix.values()) or {'include', 'exclude'} & matrix.keys():
            raise common.TimingError('unsupported_workflow_matrix')
        template = job.get('name', key)
        names = []
        for values in product(*matrix.values()):
            name = template
            for field, value in zip(matrix, values):
                name = re.sub(r'\$\{\{\s*matrix\.' + re.escape(field) + r'\s*\}\}', str(value), name)
            if '${{' in name:
                raise common.TimingError('unsupported_workflow_job_name')
            names.append(name)
        # GitHub represents an entirely skipped matrix with its unexpanded
        # display name. Accept only the exact source template, administrative
        # skip, and absence of every expanded job; never guess missing work.
        if (matrix and latest is not None and template in latest
                and common.administrative_skip(latest[template])
                and not set(names) & latest.keys()):
            names = [template]
        if len(names) != len(set(names)):
            raise common.TimingError('ambiguous_workflow_job_name')
        expanded[key] = names
    graph = {}
    for key, job in jobs.items():
        needs = job.get('needs', [])
        if isinstance(needs, str):
            needs = [needs]
        if not isinstance(needs, list) or any(n not in expanded for n in needs):
            raise common.TimingError('unknown_workflow_dependency')
        deps = tuple(name for n in needs for name in expanded[n])
        for name in expanded[key]:
            if name in graph:
                raise common.TimingError('ambiguous_workflow_job_name')
            graph[name] = deps
    return graph


def source_workflow(run):
    import yaml
    path = run.get('path', '').split('@', 1)[0]
    source = run.get('head_sha', '')
    if not re.fullmatch(r'\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml', path) or not re.fullmatch(r'[0-9a-f]{40}', source):
        raise common.TimingError('invalid_workflow_source_coordinate')
    raw = subprocess.check_output(['git', 'show', f'{source}:{path}'])
    return yaml.safe_load(raw)


def source_graph(run):
    return workflow_graph(source_workflow(run))


def scheduling(intervals, graph, created, skipped):
    # In a partial run, an unseen dependency is not a skipped dependency.
    # Report unknown rather than assigning missing predecessor time to the queue.
    rows = []
    for row in intervals:
        deps = graph.get(row['job'])
        ready = created if deps is not None else None
        predecessors = []
        for dep in deps or ():
            available = [v for v in intervals if v['job']==dep and v['end'] <= row['start'] and v['attempt'] <= row['attempt']]
            if available:
                predecessors.append(max(available, key=lambda v:(v['attempt'],v['end'])))
            elif dep not in skipped:
                ready = None
        if ready is not None:
            ready = max([ready, *(v['end'] for v in predecessors)])
        rows.append(dict(job=row['job'], attempt=row['attempt'], prerequisite_seconds=None if ready is None else round(ready-created,3), queue_after_prerequisites_seconds=None if ready is None else round(row['start']-ready,3)))
    return rows


def summarize(run, document, *, graph=None, workflow=None, policy=None):
    # The existing validator checks source, run, attempts, pagination and aliases.
    # No dependencies are guessed from job display names.
    base = common.summarize(run, document, dependencies={})
    latest, actual = common.actual_job_history(document['jobs'], run_id=run['id'], source=run['head_sha'])
    if workflow is not None:
        if graph is not None:
            raise common.TimingError('ambiguous_source_graph')
        graph = workflow_graph(workflow, latest=latest)
    intervals = []
    by_os = defaultdict(float)
    counts = Counter()
    incomplete = []
    for job in actual:
        counts[job.get('conclusion') or job['status']] += 1
        if common.administrative_skip(job):
            continue
        if job['status'] != 'completed':
            incomplete.append(job['id'])
            continue
        start = common.timestamp(job['started_at'])
        end = common.timestamp(job['completed_at'])
        labels = job.get('labels', [])
        if not isinstance(labels, list) or not all(isinstance(s, str) for s in labels):
            raise common.TimingError('invalid_runner_labels')
        platforms = {p for p in ('windows', 'ubuntu', 'macos') if any(s.lower().startswith(p + '-') for s in labels)}
        os_name = next(iter(platforms)) if len(platforms) == 1 else 'unknown'
        by_os[os_name] += end - start
        intervals.append(dict(id=job['id'], job=job['name'], attempt=job['run_attempt'], start=start, end=end, os=os_name, conclusion=job['conclusion']))
    created = common.timestamp(run['created_at'])
    complete = run.get('status') == 'completed' and not incomplete
    if complete and (run.get('conclusion') not in common.CONCLUSIONS or not run.get('conclusion')):
        raise common.TimingError('invalid_run_conclusion')
    terminal = max((v['end'] for v in intervals), default=created)
    schedule = None if graph is None else scheduling(intervals, graph, created, {j['name'] for j in latest.values() if common.administrative_skip(j)})
    return dict(schema='product-ci-timing/v1', created_at=run['created_at'], policy=policy, scheduling=schedule, critical_path=critical_path(latest, intervals, graph) if complete and graph is not None else None, first_attempt_success=first_attempt_success(run, actual), hardware_normalized_seconds=None, scheduling_note='Source-defined dependencies; rerun queue includes rerun request delay; unavailable prerequisites are null.', run_id=run['id'], source=run['head_sha'], workflow=run.get('name'), event=run.get('event'), branch=run.get('head_branch'), attempt=run['run_attempt'], complete=complete, conclusion=run.get('conclusion'), observed_span_seconds=round(terminal-created, 3), completed_runner_minutes=base['all_attempts_workflow_runner_minutes'], runner_minutes_by_os={k: round(v/60, 3) for k,v in sorted(by_os.items())}, actual_job_outcomes=dict(counts), latest_job_outcomes=dict(Counter(v.get('conclusion') or v['status'] for v in latest.values())), incomplete_job_ids=incomplete, intervals=sorted(intervals, key=lambda v:(v['start'],v['id'])), qualification_claim=False)


def overlap(runs):
    events = []
    seen = set()
    for run in runs:
        if run['run_id'] in seen:
            raise common.TimingError('duplicate_run')
        seen.add(run['run_id'])
        for row in run['intervals']:
            if row['end'] > row['start']:
                events.extend([(row['start'], 1, run['run_id']), (row['end'], -1, run['run_id'])])
    active = Counter()
    peak = shared_peak = 0
    shared_seconds = 0.0
    previous = None
    # End events precede starts at identical timestamps; zero-duration jobs
    # contribute nothing. Occupancy uses the actual execution intervals only.
    for time, delta, run_id in sorted(events):
        if previous is not None and len(active) > 1:
            shared_seconds += time - previous
        active[run_id] += delta
        if not active[run_id]:
            del active[run_id]
        peak = max(peak, sum(active.values()))
        if len(active) > 1:
            shared_peak = max(shared_peak, sum(active.values()))
        previous = time
    return dict(observed_peak_concurrent_jobs=peak, peak_across_overlapping_runs=shared_peak, overlapping_run_seconds=round(shared_seconds,3), scope='supplied completed job intervals only; excludes other workflows and accounts')


def critical_path(latest, intervals, graph):
    """Longest source-declared path of actual job durations, excluding queue."""
    if set(latest) != set(graph) or any(row['attempt'] != 1 for row in intervals):
        return None
    durations = {row['job']: row['end'] - row['start'] for row in intervals}
    if not durations or any(name not in durations and not common.administrative_skip(job) for name, job in latest.items()):
        return None
    paths, visiting = {}, set()

    def visit(name):
        if name in paths:
            return paths[name]
        if name in visiting:
            raise common.TimingError('cyclic_source_graph')
        visiting.add(name)
        predecessor = max((visit(dep) for dep in graph[name]), default=(0, ()))
        value = (predecessor[0] + durations.get(name, 0), predecessor[1] + ((name,) if name in durations else ()))
        visiting.remove(name)
        paths[name] = value
        return value

    seconds, names = max(visit(name) for name in graph)
    return dict(seconds=round(seconds, 3), jobs=list(names), scope='Source-declared dependency path; excludes queue.')


def first_attempt_success(run, actual):
    if run.get('status') != 'completed':
        return None
    if run['run_attempt'] == 1:
        return run.get('conclusion') == 'success'
    first = [job for job in actual if job['run_attempt'] == 1 and not common.administrative_skip(job)]
    if any(job.get('conclusion') in {'failure', 'cancelled', 'timed_out', 'action_required', 'stale'} for job in first):
        return False
    return None


def observations(runs, *, as_of=None):
    if not runs:
        return []
    end = common.timestamp(as_of) if as_of else max(common.timestamp(r['created_at']) for r in runs)
    groups = defaultdict(list)
    for run in runs:
        if run['complete'] and end - 30*86400 <= common.timestamp(run['created_at']) <= end:
            groups[(run['workflow'], run['event'], 'main' if run['branch']=='main' else 'non-main', run['conclusion'], 'first' if run['attempt']==1 else 'rerun', run.get('policy'))].append(run)
    rows = []
    for k, values in sorted(groups.items(), key=lambda x:str(x[0])):
        v = sorted(values, key=lambda r:(common.timestamp(r['created_at']),r['run_id']))
        spans = sorted(r['observed_span_seconds'] for r in v)
        paths = [r['critical_path']['seconds'] for r in v if r.get('critical_path')]
        rows.append(dict(workflow=k[0], event=k[1], branch_class=k[2], conclusion=k[3], attempt_class=k[4], policy=k[5], observations=len(v), window_days=30, run_ids=[r['run_id'] for r in v], median_span_seconds=statistics.median(spans), slowest_span_seconds=max(spans), empirical_p95_span_seconds=spans[math.ceil(.95*len(spans))-1], p95_provisional=len(v)<20 or k[5] is None, p95_note='Nearest-rank empirical p95; fewer than 20 observations or unknown policy is provisional, not a population guarantee.', median_runner_minutes=statistics.median(r['completed_runner_minutes'] for r in v), critical_path_observations=len(paths), median_critical_path_seconds=statistics.median(paths) if paths else None, hardware_normalized_observations=0, hardware_normalized_median_seconds=None))
    return rows


def first_attempt_rates(runs, *, as_of):
    end = common.timestamp(as_of)
    groups = defaultdict(list)
    for r in runs:
        if r['complete'] and end - 30*86400 <= common.timestamp(r['created_at']) <= end:
            groups[(r['workflow'],r['event'],'main' if r['branch']=='main' else 'non-main',r.get('policy'))].append(r)
    rows = []
    for k, v in sorted(groups.items(), key=lambda x:str(x[0])):
        passes = sum(r.get('first_attempt_success') is True for r in v)
        failures = sum(r.get('first_attempt_success') is False for r in v)
        rows.append(dict(workflow=k[0],event=k[1],branch_class=k[2],policy=k[3],observations=len(v),first_attempt_successes=passes,first_attempt_non_successes=failures,unknown_first_attempt_outcomes=len(v)-passes-failures,cancelled=sum(r['conclusion']=='cancelled' for r in v),known_first_attempt_success_rate=passes/(passes+failures) if passes+failures else None))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directories', type=Path, nargs='+')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--as-of', help='UTC timestamp ending the 30-day window; default: newest supplied run creation')
    parser.add_argument('--source-graph', action='store_true', help='Resolve exact workflow YAML from each source commit in the local Git repository')
    args = parser.parse_args()
    try:
        runs = []
        for d in args.directories:
            run = json.loads((d/'run.json').read_text(encoding='utf-8-sig'))
            jobs = json.loads((d/'jobs.json').read_text(encoding='utf-8-sig'))
            workflow = source_workflow(run) if args.source_graph else None
            policy = None if workflow is None else workflow.get('env', {}).get('CI_POLICY_VERSION') or workflow.get('env', {}).get('VISUAL_CI_POLICY_VERSION')
            runs.append(summarize(run, jobs, workflow=workflow, policy=policy))
        as_of = args.as_of or max((r['created_at'] for r in runs), key=common.timestamp)
        output = dict(runs=runs, overlap=overlap(runs), window_end=as_of, window_days=30, observations=observations(runs, as_of=as_of), first_attempt_rates=first_attempt_rates(runs, as_of=as_of), hardware_normalization_note='No calibrated phase-specific host controls in the supplied evidence; report raw durations without CPU scaling.', qualification_claim=False)
        encoded = json.dumps(output, indent=2, allow_nan=False)+'\n'
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding='utf-8')
        else:
            print(encoded, end='')
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        print(f'product_ci_timing_invalid_evidence: {error}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
