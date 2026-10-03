"""Protocol and fail-closed tests; these are not performance measurements."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import light_first_update_benchmark as runner

import pytest

pytestmark = [pytest.mark.ci_tier("fast")]


class FirstUpdateProtocolTests(unittest.TestCase):
    def test_cold_prefix_keeps_qualification_order_and_mutation(self):
        calls = []
        async def direct(work, journal, resources, name):
            calls.extend([name, 'independent-cold-verification'])
            return {'cold': True}
        async def start(*, recover):
            calls.append(('scheduler-start', recover))
        async def scheduled(work, journal, resources, scheduler, name, mutation, *, case):
            calls.append((name, case))
            mutation()
            calls.append('independent-update-verification')
            return {'probe': True}
        work = SimpleNamespace(add=lambda *args: calls.append(('mutation', *args)))
        resources = SimpleNamespace(start=lambda: calls.append('resources-start'))
        helpers = SimpleNamespace(direct=direct, scheduled=scheduled)
        with patch.dict('sys.modules', {'tools.analytics_qualification_workloads': helpers}):
            answer = asyncio.run(runner.cold_prefix(work, None, resources,
                                                   SimpleNamespace(start=start), 'small'))
        self.assertEqual(answer, ({'cold': True}, {'probe': True}))
        self.assertEqual(calls, ['cold', 'independent-cold-verification',
            'resources-start', ('scheduler-start', True),
            ('one_committed_message', 'ordinary/small'),
            ('mutation', 1, 'visibility-ordinary-small'),
            'independent-update-verification'])

    def probe(self):
        return dict(case='ordinary/small', valid_current_result=True, cleanup_complete=True,
            stale_reference_rejected=True, backlog_before=0, backlog_after=0,
            clocks=dict(operation_started=1, durable_canonical_commit=2,
                first_valid_visible_result=9, required_cleanup_complete=14,
                backlog_drained=15), publication_events=[
                    dict(stage='built', at=5), dict(stage='validated', at=7),
                    dict(stage='activated', at=8)])

    def test_cleanup_and_backlog_are_inside_gate(self):
        result = runner.summarize_probe(self.probe())
        self.assertEqual(result['total'], 13)
        self.assertFalse(result['gate'])
        self.assertEqual(result['commit_to_built'] + result['built_to_validated']
                         + result['validated_to_done'], result['total'])

    def test_missing_clock_cannot_pass(self):
        for key in ('first_valid_visible_result', 'required_cleanup_complete', 'backlog_drained'):
            probe = self.probe()
            probe['clocks'][key] = None
            with self.assertRaisesRegex(ValueError, 'probe_clock_missing'):
                runner.summarize_probe(probe)

    def test_failed_safety_cannot_pass(self):
        for key, value in [('valid_current_result', False), ('cleanup_complete', False),
                ('stale_reference_rejected', False), ('backlog_before', 1), ('backlog_after', 1)]:
            probe = self.probe()
            probe[key] = value
            with self.assertRaisesRegex(ValueError, 'probe_safety_incomplete'):
                runner.summarize_probe(probe)

    def test_cold_cli_rejects_seed_and_idle(self):
        base = ['runner', '--source-root', 'source', '--expected-sha', 'sha',
                '--output', 'output', '--owner-lock', 'lock', '--preparation', 'cold']
        for extra in (['--seed', 'seed'], ['--idle-seconds', '61']):
            with patch('sys.argv', base + extra), self.assertRaises(SystemExit) as error:
                runner.options()
            self.assertEqual(error.exception.code, 2)

    def test_cold_defaults_to_100k(self):
        with patch('sys.argv', ['runner', '--source-root', 'source', '--expected-sha', 'sha',
                '--output', 'output', '--owner-lock', 'lock', '--preparation', 'cold']):
            args = runner.options()
        self.assertEqual(args.messages, 100000)
        self.assertIsNone(args.seed)
        self.assertEqual(args.idle_seconds, 0)


class RepeatOnePrefixTests(unittest.TestCase):
    def arguments(self, *extra):
        return ['runner', '--source-root', 'source', '--expected-sha', 'sha',
                '--output', 'output', '--owner-lock', 'lock',
                '--preparation', 'repeat1-prefix', *extra]

    def test_fresh_prefix_defaults_and_unprofiled_mode(self):
        with patch('sys.argv', self.arguments('--trace-mode', 'none')):
            args = runner.options()
        self.assertEqual(args.messages, 100000)
        self.assertEqual(args.timeout_seconds, 9000)
        self.assertEqual(args.trace_mode, 'none')
        self.assertIsNone(args.seed)
        self.assertEqual(args.idle_seconds, 0)

    def test_prefix_rejects_seed_extra_idle_and_v6_operation_binding(self):
        for extra in (['--seed', 'seed'], ['--idle-seconds', '61'],
                      ['--baseline-operation', 'old-operation.json']):
            with patch('sys.argv', self.arguments(*extra)), self.assertRaises(SystemExit):
                runner.options()

    def test_prefix_uses_worker_order_mutations_verification_and_original_idle(self):
        from tests.test_qualification_process_improvements import Scenario, MANIFEST, q
        scenario = Scenario()
        with scenario.installed() as sleep:
            report = asyncio.run(runner.repeat1_prefix(
                scenario.work, scenario.journal, scenario.config, 'prefix-parent'))
        sleep.assert_awaited_once_with(61)
        self.assertEqual([p['case'] for p in report['probes']], runner.PREFIX_CASES)
        mutations = [x for x in scenario.calls if isinstance(x, tuple) and x[0] == 'mutate']
        self.assertEqual(mutations, [('mutate', (0, 'visibility-ordinary-dominant')),
            ('mutate', (1, 'visibility-rebuilt-small')), ('mutate', (0, 'visibility-idle-dominant'))])
        self.assertEqual(scenario.calls.count('cold-or-rebuild-verified'), 2)
        self.assertEqual(scenario.calls.count('independent-verification-complete'), 3)
        self.assertEqual(scenario.calls.count('scheduler-joined'), 1)
        self.assertNotIn('restart-child', scenario.calls)
        self.assertIn('visibility_case_set_mismatch', q.check_visibility(MANIFEST, scenario.config['job'], report))

    def test_verified_idle_failure_stops_and_joins_without_restart(self):
        from tests.test_qualification_process_improvements import Scenario
        scenario = Scenario(miss='idle/dominant')
        with scenario.installed():
            report = asyncio.run(runner.repeat1_prefix(
                scenario.work, scenario.journal, scenario.config, 'prefix-parent'))
        self.assertFalse(report['complete'])
        self.assertTrue(report['stopped_after_verified_failure']['verification_completed'])
        self.assertTrue(report['scheduler_closed'])
        self.assertEqual(report['unexecuted_cases'], ['restarted/small'])
        self.assertNotIn('restart-child', scenario.calls)

    def test_earlier_failure_never_runs_later_prefix_steps(self):
        from tests.test_qualification_process_improvements import Scenario
        scenario = Scenario(miss='ordinary/dominant')
        with scenario.installed() as sleep:
            report = asyncio.run(runner.repeat1_prefix(
                scenario.work, scenario.journal, scenario.config, 'prefix-parent'))
        sleep.assert_not_awaited()
        self.assertEqual(len(report['probes']), 1)
        self.assertNotIn('unchanged_rebuild', scenario.calls)
        self.assertTrue(report['scheduler_closed'])

    def test_outcome_does_not_conflate_nonreproduction_smoke_or_earlier_failure(self):
        result = dict(complete=True, summaries=[dict(case=c, gate=True) for c in runner.PREFIX_CASES])
        self.assertEqual(runner.prefix_outcome(result, 100000), 'NOT_REPRODUCED')
        self.assertEqual(runner.prefix_outcome(result, 1000), 'SMOKE_ONLY')
        result['summaries'][-1]['gate'] = False
        self.assertEqual(runner.prefix_outcome(result, 100000), 'LATENCY_MISS_REPRODUCED')
        result['summaries'] = result['summaries'][:1]
        self.assertEqual(runner.prefix_outcome(result, 100000), 'EARLIER_PREFIX_GATE_FAILED')
        result['complete'] = False
        self.assertEqual(runner.prefix_outcome(result, 100000), 'INCOMPLETE')


if __name__ == '__main__':
    unittest.main()
