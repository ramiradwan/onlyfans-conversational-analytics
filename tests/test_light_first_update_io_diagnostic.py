"""Protocol tests for timing-only cold-prefix I/O instrumentation."""
import unittest
from types import SimpleNamespace
from tools import light_first_update_io_diagnostic as io
from tools import light_first_update_benchmark as base

import pytest

pytestmark = [pytest.mark.ci_tier("fast")]


class TimingTests(unittest.TestCase):
    def trace(self):
        temporary = SimpleNamespace(Trace=base.Trace)
        return io.instrument(temporary)()

    def test_returns_original_value(self):
        trace = self.trace()
        self.assertEqual(trace.call('example', lambda x:x+1, (3,), {}),4)
        self.assertEqual(len(trace.primitives),1)
        self.assertEqual(trace.snapshot_active(),{})
        self.assertIsNone(trace.primitives[0]['error'])

    def test_exception_is_not_hidden(self):
        trace = self.trace()
        def fail():raise ValueError('test')
        with self.assertRaises(ValueError):trace.call('example',fail,(),{})
        self.assertEqual(trace.primitives[0]['error'],'ValueError')
        self.assertEqual(trace.snapshot_active(),{})

    def test_nested_calls_remain_nested(self):
        trace = self.trace()
        def inspect():
            self.assertEqual([x['kind'] for x in next(iter(trace.snapshot_active().values()))],['outer','inner'])
        trace.call('outer',lambda:trace.call('inner',inspect,(),{}),(),{})
        self.assertEqual([x['kind'] for x in trace.primitives],['inner','outer'])
        self.assertEqual(trace.snapshot_active(),{})

    def test_inherited_patch_is_removed(self):
        class Parent:
            def run(self):return 7
        class Child(Parent):pass
        trace=self.trace()
        trace.patch(Child,'run',lambda self:8)
        self.assertEqual(Child().run(),8)
        trace.restore()
        self.assertNotIn('run',vars(Child))
        self.assertEqual(Child().run(),7)

    def test_counter_paths_include_latency_and_paging(self):
        paths=io.counter_paths()
        self.assertEqual(len(paths),33)
        self.assertEqual(paths['c_physical.write_seconds'],r'\PhysicalDisk(0 C:)\Avg. Disk sec/Write')
        self.assertIn('pages_input_sec',paths)

    def test_short_events_filtered_not_errors(self):
        trace=self.trace()
        trace.call('sql',lambda:None,(),{},threshold=1)
        self.assertEqual(trace.primitives,[])
        with self.assertRaises(KeyError):
            trace.call('sql',lambda:{}['missing'],(),{},threshold=1)
        self.assertEqual(len(trace.primitives),1)

    def test_sample_bound_is_explicit(self):
        trace=self.trace(); old=io.MAX_PRIMITIVES
        try:
            io.MAX_PRIMITIVES=1
            trace.call('a',lambda:None,(),{})
            trace.call('b',lambda:None,(),{})
            self.assertEqual(len(trace.primitives),1)
            self.assertEqual(trace.dropped,1)
        finally:io.MAX_PRIMITIVES=old

if __name__=='__main__':unittest.main()
