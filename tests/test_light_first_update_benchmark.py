"""Protocol and fail-closed tests; these are not performance measurements."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import light_first_update_benchmark as runner


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


if __name__ == '__main__':
    unittest.main()
