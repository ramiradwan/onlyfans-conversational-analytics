"""Report actual CI runner occupancy and latency, never release qualification.

Inputs: complete GitHub run.json and filter=all jobs.json per run. Keep failed,
retried and successful runs separate. The result describes only supplied runs,
not account-wide runner availability or a population percentile.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import statistics

try:
    from tools import browser_ci_metrics as common
except ModuleNotFoundError:
    import browser_ci_metrics as common


def summarize(run, document):
    # The existing validator checks source, run, attempts, pagination and aliases.
    # No dependencies are guessed from job display names.
    base = common.summarize(run, document, dependencies={})
    latest, actual = common.actual_job_history(document['jobs'], run_id=run['id'], source=run['head_sha'])
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
    return dict(schema='product-ci-timing/v1', run_id=run['id'], source=run['head_sha'], workflow=run.get('name'), event=run.get('event'), branch=run.get('head_branch'), attempt=run['run_attempt'], complete=complete, conclusion=run.get('conclusion'), observed_span_seconds=round(terminal-created, 3), completed_runner_minutes=base['all_attempts_workflow_runner_minutes'], runner_minutes_by_os={k: round(v/60, 3) for k,v in sorted(by_os.items())}, actual_job_outcomes=dict(counts), latest_job_outcomes=dict(Counter(v.get('conclusion') or v['status'] for v in latest.values())), incomplete_job_ids=incomplete, intervals=sorted(intervals, key=lambda v:(v['start'],v['id'])), qualification_claim=False)


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


def observations(runs):
    groups = defaultdict(list)
    for run in runs:
        if run['complete']:
            groups[(run['workflow'], run['event'], 'main' if run['branch'] == 'main' else 'non-main', run['conclusion'], 'first' if run['attempt']==1 else 'rerun')].append(run)
    return [dict(workflow=k[0], event=k[1], branch_class=k[2], conclusion=k[3], attempt_class=k[4], observations=len(v), median_span_seconds=statistics.median(r['observed_span_seconds'] for r in v), slowest_span_seconds=max(r['observed_span_seconds'] for r in v), median_runner_minutes=statistics.median(r['completed_runner_minutes'] for r in v)) for k,v in sorted(groups.items(), key=lambda x:str(x[0]))]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directories', type=Path, nargs='+')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        runs = [summarize(json.loads((d/'run.json').read_text(encoding='utf-8-sig')), json.loads((d/'jobs.json').read_text(encoding='utf-8-sig'))) for d in args.directories]
        output = dict(runs=runs, overlap=overlap(runs), observations=observations(runs), qualification_claim=False)
        encoded = json.dumps(output, indent=2, allow_nan=False)+'\n'
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding='utf-8')
        else:
            print(encoded, end='')
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f'product_ci_timing_invalid_evidence: {error}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
