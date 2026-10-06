"""Preparation reuse and input isolation controls; no qualification credit."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from tools import analytics_qualification as q
from tools import analytics_qualification_baselines as b
from tools import analytics_qualification_bundle as bundle


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.config = {"subject_sha256": "source-one", "manifest": {"fixture": {"seed": 7}},
                       "profile": "constrained-windows-8g", "messages": 200,
                       "case": "populated", "state": "fresh", "known_kinds": True}
        self.runtime = {"python": "test-runtime"}
        self.key = b.binding(self.config, self.runtime)
        self.producer = {"instance": "test-process", "runtime": self.runtime, "subject_sha256": self.config["subject_sha256"], "prepared_profile": self.config["profile"]}
        self.builds = 0

    def build(self, directory):
        self.builds += 1
        for name in b.DATABASES:
            (directory / name).write_bytes((name + str(self.builds)).encode())
        expected = {key: "sha256:" + "a"*64 for key in
                    ("canonical_content_digest", "projection_digest", "graph_digest")}
        return {"independent_rebuild_equal": True, "persisted_content_revalidated": True,
                "expected": expected, "actual": expected}, {"messages": 200, "revision": 1}

    def prepare(self, key=None):
        return b.prepare(self.root / "baselines", key or self.key, self.producer, self.build)

    def test_only_two_variants_total_for_twenty_four_jobs(self):
        keys = set()
        for profile in ("constrained-windows-8g", "reference-windows-16g"):
            for case in ("populated", "empty", "generation_bound_pagination", "tied_time"):
                for state in ("fresh", "idle", "mutated"):
                    key = b.binding(dict(self.config, profile=profile, case=case, state=state), self.runtime)
                    keys.add(q.digest(key)); self.prepare(key)
        self.assertEqual(len(keys), 2)
        self.assertEqual(self.builds, 2)

    def test_reuse_does_not_call_builder(self):
        first, record, created = self.prepare()
        second, again, reused_created = b.prepare(self.root/'baselines', self.key, self.producer,
                                                lambda _: self.fail("unnecessary rebuild"))
        self.assertTrue(created); self.assertFalse(reused_created)
        self.assertEqual(first, second); self.assertEqual(record, again)

    def test_subject_runtime_manifest_size_and_kind_are_bound(self):
        original = q.digest(self.key)
        for field, value in (("subject_sha256", "other"),
                             ("messages", 10000), ("known_kinds", False),
                             ("manifest", {"fixture": {"seed": 8}})):
            self.assertNotEqual(q.digest(b.binding(dict(self.config, **{field: value}), self.runtime)), original)
        self.assertNotEqual(q.digest(b.binding(self.config, {"python": "other"})), original)
        self.assertEqual(q.digest(b.binding(dict(self.config, profile="reference-windows-16g"), self.runtime)), original)
        with self.assertRaises(ValueError): b.variant("unsupported")

    def test_private_copies_cannot_mutate_baseline_or_next_job(self):
        path, record, _ = self.prepare()
        first, second = self.root/'job-one', self.root/'job-two'
        b.clone(path, record, first)
        (first/'canonical.sqlite3').write_bytes(b"mutated job data")
        b.clone(path, record, second)
        self.assertEqual(b.file_record(second/'canonical.sqlite3'), record['files']['canonical.sqlite3'])
        self.assertNotEqual((first/'canonical.sqlite3').read_bytes(), (second/'canonical.sqlite3').read_bytes())
        self.assertFalse(os.path.samefile(path/'canonical.sqlite3', second/'canonical.sqlite3'))

    def test_corrupted_database_refuses_without_rebuild(self):
        path, record, _ = self.prepare()
        file = path/'analytics.sqlite3'; file.chmod(stat.S_IWRITE | stat.S_IREAD)
        file.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, 'working_copy_hash_mismatch'):
            b.clone(path, record, self.root/'job')
        self.assertEqual(self.builds, 1)

    def test_changed_manifest_and_unknown_files_refuse(self):
        path, record, _ = self.prepare()
        manifest = path/'baseline.json'; manifest.chmod(stat.S_IWRITE | stat.S_IREAD)
        changed = copy.deepcopy(record); changed['binding']['profile'] = 'wrong'
        manifest.write_bytes(q.encoded(changed))
        with self.assertRaisesRegex(ValueError, 'binding_mismatch'): self.prepare()
        manifest.write_bytes(q.encoded(record))
        (path/'unexpected').write_bytes(b'x')
        with self.assertRaisesRegex(ValueError, 'unexpected_baseline_files'): self.prepare()

    def test_failed_preparation_is_not_a_published_baseline(self):
        def fail(path):
            (path/'canonical.sqlite3').write_bytes(b'partial')
            raise ValueError('injected build failure')
        with self.assertRaisesRegex(ValueError, 'injected build failure'):
            b.prepare(self.root/'baselines', self.key, self.producer, fail)
        self.assertFalse((self.root/'baselines'/q.digest(self.key)).exists())
        self.assertEqual(len(list((self.root/'baselines').glob('*.preparing-*'))), 1)

    def test_live_sidecars_prevent_publication(self):
        def unclosed(path):
            answer = self.build(path)
            (path/'analytics.sqlite3-wal').write_bytes(b'not checkpointed')
            return answer
        with self.assertRaisesRegex(ValueError, 'unclosed_sidecars'):
            b.prepare(self.root/'baselines', self.key, self.producer, unclosed)

    def test_busy_preparation_is_not_stolen(self):
        root = self.root/'baselines'; root.mkdir()
        lock = root/(q.digest(self.key)+'.lock'); lock.mkdir()
        with self.assertRaisesRegex(ValueError, 'preparation_busy'): self.prepare()
        self.assertTrue(lock.exists()); self.assertEqual(self.builds, 0)

    def test_existing_destination_and_overlaps_refused(self):
        path, record, _ = self.prepare()
        dest = self.root/'job'; dest.mkdir()
        with self.assertRaisesRegex(ValueError, 'already_exists'): b.clone(path, record, dest)
        with self.assertRaisesRegex(ValueError, 'overlaps'): b.clone(path, record, path/'job')

    def test_hardlinked_input_is_rejected(self):
        path, record, _ = self.prepare()
        os.link(path/'canonical.sqlite3', self.root/'alias')
        with self.assertRaisesRegex(ValueError, 'baseline_link'): b.clone(path, record, self.root/'job')

    def test_cleanup_requires_joined_success_and_never_removes_baseline(self):
        path, record, _ = self.prepare(); dest = self.root/'job'; copied = b.clone(path, record, dest)
        cfg = dict(self.config, data=str(dest), question_baselines=str(path.parent))
        report = dict(complete=True, child_exit_code=0, scheduler_closed=True, detached_workers=0, backlog=0,
                      prepared_input={"copy": copied, "baseline_manifest_sha256": q.digest(record)})
        for field, value in (("complete", False), ("child_exit_code", 1), ("scheduler_closed", False),
                             ("detached_workers", 1), ("backlog", 1)):
            with self.assertRaises(ValueError): b.cleanup_working_copy(cfg, dict(report, **{field: value}))
            self.assertTrue(dest.exists())
        b.cleanup_working_copy(cfg, report)
        self.assertFalse(dest.exists()); self.assertTrue(path.exists())


    def test_public_receipt_rejects_missing_proof_wrong_binding_and_cached_pass(self):
        self.config["manifest"]["questions"] = {"messages": 200}
        self.key = b.binding(self.config, self.runtime)
        path, record, _ = self.prepare()
        copied = b.clone(path, record, self.root/"job")
        receipt = {"protocol": b.PROTOCOL, "baseline_id": q.digest(self.key), "created": True,
                   "baseline_manifest_sha256": q.digest(record), "baseline": record, "copy": copied,
                   "initial_builds_this_job": 1, "prior_results_reused": False}
        data = {"subject_sha256": self.config["subject_sha256"], "collector_process": self.producer,
                "process_instance": "new-query-process", "fixture_mode": "known_synthetic_kinds",
                "prepared_input": receipt, "working_copy_cleanup": "removed_after_joined_success", "state": "fresh",
                "verification": dict(record["verification"], reference_mode="verified_baseline",
                                     canonical_rescanned=True, baseline_manifest_sha256=q.digest(record))}
        check = lambda value: b.check_input_receipt(value, self.config["manifest"], self.config["profile"], "populated")
        self.assertEqual(check(data), [])
        for fault in ("missing", "extra", "source", "cached_pass", "same_process", "copied_hashes",
                      "cleanup", "reference", "actual", "producer", "build_count"):
            with self.subTest(fault=fault):
                bad = copy.deepcopy(data)
                if fault == "missing": del bad["prepared_input"]
                elif fault == "extra": bad["prepared_input"]["unreviewed"] = True
                elif fault == "source": bad["subject_sha256"] = "other"
                elif fault == "cached_pass": bad["prepared_input"]["prior_results_reused"] = True
                elif fault == "same_process": bad["process_instance"] = self.producer["instance"]
                elif fault == "copied_hashes": bad["prepared_input"]["copy"]["all_input_hashes_verified"] = False
                elif fault == "cleanup": bad["working_copy_cleanup"] = "pending"
                elif fault == "reference": bad["verification"]["canonical_rescanned"] = False
                elif fault == "actual": bad["verification"]["actual"] = {}
                elif fault == "producer": bad["prepared_input"]["baseline"]["producer"]["subject_sha256"] = "other"
                else: bad["prepared_input"]["initial_builds_this_job"] = True
                self.assertTrue(check(bad))
        changed = copy.deepcopy(data); changed["state"] = "mutated"
        self.assertTrue(check(changed))
        changed["verification"]["reference_mode"] = "independent_rebuild"
        self.assertTrue(check(changed))  # A mutation cannot reuse the old canonical reference.


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.closure = self.root/'closure'; self.closure.mkdir()
        self.store = self.root/'bundle'

    def test_second_export_copies_only_new_content(self):
        (self.closure/'baseline.db').write_bytes(b'x'*100000)
        (self.closure/'result-one.json').write_bytes(b'{"passed":true}')
        one = bundle.export(self.closure, self.store)
        (self.closure/'result-two.json').write_bytes(b'{"new":true}')
        two = bundle.export(self.closure, self.store, one['manifest_sha256'])
        self.assertEqual(one['new_objects'], 2)
        self.assertEqual(two['new_objects'], 1)
        self.assertEqual(two['new_object_bytes'], len(b'{"new":true}'))
        restored = self.root/'restored'
        bundle.restore(self.store, two['manifest_sha256'], restored)
        for path in self.closure.iterdir(): self.assertEqual(path.read_bytes(), (restored/path.name).read_bytes())

    def test_changed_or_removed_prior_evidence_is_rejected(self):
        p=self.closure/'result.json'; p.write_bytes(b'first'); one=bundle.export(self.closure,self.store)
        p.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'prior_evidence_changed'):
            bundle.export(self.closure,self.store,one['manifest_sha256'])
        p.unlink()
        with self.assertRaisesRegex(ValueError,'prior_evidence_changed'):
            bundle.export(self.closure,self.store,one['manifest_sha256'])

    def test_corrupt_object_and_manifest_are_rejected(self):
        (self.closure/'data').write_bytes(b'original'); first=bundle.export(self.closure,self.store)
        record=bundle.load_manifest(self.store,first['manifest_sha256'])
        obj=bundle.object_path(self.store,record['files']['data']['sha256'])
        obj.chmod(stat.S_IWRITE|stat.S_IREAD); obj.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'corrupt'): bundle.restore(self.store,first['manifest_sha256'],self.root/'out')
        manifest=Path(first['manifest']);manifest.write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError,'manifest_hash'):bundle.load_manifest(self.store,first['manifest_sha256'])

    def test_unsafe_paths_overlaps_and_busy_export_refuse(self):
        for name in ('../escape','C:/escape','/absolute','bad\\path','a/../b','a//b','a./b'):
            with self.assertRaises(ValueError):bundle.relative(name)
        with self.assertRaises(ValueError):bundle.export(self.closure,self.closure/'bundle')
        self.store.mkdir();(self.store/'export.lock').mkdir()
        with self.assertRaisesRegex(ValueError,'busy'):bundle.export(self.closure,self.store)


if __name__ == '__main__':
    unittest.main()
