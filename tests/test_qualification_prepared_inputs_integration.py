"""Real encrypted-database controls for reusable synthetic input; not qualification."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

from tools import analytics_qualification as q
from tools import analytics_qualification_baselines as b
from tools.analytics_qualification_fixture import Workload
from tools.analytics_qualification_progress import CollectorProgress


pytestmark = [pytest.mark.ci_tier("integration"), pytest.mark.windows_compat]

@pytest.fixture(scope="module")
def prepared(tmp_path_factory):
    root = tmp_path_factory.mktemp("prepared-real-db")
    manifest = deepcopy(q.read_json(Path(__file__).resolve().parents[1]/"docs/analytics/acceptance-manifest.json"))
    manifest["questions"]["messages"] = 400
    config = {"subject_sha256": "diagnostic-input-only", "manifest": manifest,
              "profile": "constrained-windows-8g", "messages": 400, "case": "populated",
              "state": "fresh", "known_kinds": True}
    runtime = {"python": "real-SQLCipher-control"}
    producer = {"instance": "test-producer", "runtime": runtime,
                "subject_sha256": config["subject_sha256"]}
    progress = CollectorProgress(root, "test-producer")
    path, record, created = b.prepare(root/"baselines", b.binding(config, runtime), producer,
                                     lambda destination: b.build_input(config, destination, progress))
    assert created
    return root, path, record, manifest


@pytest.fixture
def working(prepared, tmp_path):
    _, baseline, record, manifest = prepared
    destination = tmp_path/"job"
    b.clone(baseline, record, destination)
    work = Workload(destination, manifest, 400, reopen=True, question_case="populated", known_kinds=True)
    try:
        work.f.pipeline.ensure_projection_storage()  # Normal startup storage recovery, not a rebuild.
        yield work, record, destination
    finally:
        work.close()


def test_sealed_pair_has_no_live_journal_and_copies_match(prepared, working):
    _, baseline, record, _ = prepared
    work, _, destination = working
    assert {p.name for p in baseline.iterdir()} == {*b.DATABASES, "baseline.json"}
    assert work.counts() == {"messages": 400, "revision": 1}
    for name in b.DATABASES:
        assert b.file_record(baseline/name) == record["files"][name]


def test_unchanged_verification_does_not_rebuild_oracle(working):
    work, baseline, _ = working
    with patch("tools.qualify_continuous_analytics.reference_artifact", side_effect=AssertionError("unnecessary oracle rebuild")):
        result = work.verify_prepared_reference(baseline)
    assert result["canonical_rescanned"]
    assert result["expected"] == result["actual"] == baseline["verification"]["expected"]
    assert result["reference_mode"] == "verified_baseline"


def test_source_tampering_without_revision_change_is_rejected(working):
    work, baseline, _ = working
    with work.f.repositories.database.transaction() as db:
        db.execute("UPDATE account_messages SET text='tampered' WHERE message_id='matrix-input-0'")
    with pytest.raises(ValueError, match="prepared_reference_source_changed"):
        work.verify_prepared_reference(baseline)


def test_stored_output_tampering_is_rejected(working):
    work, baseline, _ = working
    store = getattr(work.f.stores.projections, "_store", None) or work.f.stores.projections
    from app.persistence.database import sqlite3
    with pytest.raises(sqlite3.IntegrityError, match="projection_generation_identity_immutable"):
        with store.database.transaction() as db:
            db.execute("UPDATE projection_generations SET projection_digest=? WHERE status='active'", ("sha256:"+"0"*64,))
    # Deliberately corrupt ONLY this disposable test copy past the normal write
    # guard, so the independent prepared-reference validation is also exercised.
    with store.database.transaction() as db:
        triggers = db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='projection_generations' AND sql LIKE '%projection_generation_identity_immutable%'").fetchall()
        assert triggers
        for row in triggers:
            assert all(c.isalnum() or c == '_' for c in row["name"])
            db.execute('DROP TRIGGER "' + row["name"] + '"')
        db.execute("UPDATE projection_generations SET projection_digest=? WHERE status='active'", ("sha256:"+"0"*64,))
    with pytest.raises(Exception):
        work.verify_prepared_reference(baseline)


def test_actual_mutation_requires_new_oracle_and_does_not_change_template(prepared, working):
    _, baseline_path, baseline, _ = prepared
    work, _, _ = working
    work.edit()
    with pytest.raises(ValueError, match="prepared_reference_source_changed"):
        work.verify_prepared_reference(baseline)
    candidate = work.f.pipeline.build_candidate(work.account, force=False)
    work.f.pipeline.publish_candidate(candidate)
    result = work.verify()
    assert result["reference_mode"] == "independent_rebuild"
    assert result["expected"] == result["actual"]
    assert result["expected"]["canonical_content_digest"] != baseline["verification"]["expected"]["canonical_content_digest"]
    assert b.file_record(baseline_path/"canonical.sqlite3") == baseline["files"]["canonical.sqlite3"]


def test_snapshot_refuses_open_connection(working):
    work, _, _ = working
    with work.f.repositories.database.read():
        with pytest.raises(Exception, match="closed connections"):
            work.close_for_snapshot()



def test_adoption_revalidates_real_database_without_building_and_retains_donor(prepared,tmp_path,monkeypatch):
    import shutil
    from app.analytics.pipeline import AnalyticsPipeline
    from tools import qualify_continuous_analytics as reference
    root,original,record,manifest=prepared
    # Source witnesses are test doubles; no synthetic witness is ever written to
    # a real campaign. The copied SQLCipher data and independent oracle are real.
    old={'revision':'test-old','signature_valid':True,'working_tree':'','files':{}}
    new={'revision':'test-new','signature_valid':True,'working_tree':'','files':{}}
    old_root=tmp_path/'old-source';new_root=tmp_path/'new-source';old_root.mkdir();new_root.mkdir()
    runtime=record['producer']['runtime']
    config={'subject_sha256':q.digest(old),'manifest':manifest,'profile':'constrained-windows-8g',
            'messages':400,'case':'populated','state':'fresh','known_kinds':True}
    donor_producer=dict(record['producer'],subject_sha256=q.digest(old))
    def copy_original(destination):
        for name in b.DATABASES:shutil.copyfile(original/name,destination/name)
        return record['verification'],record['source_counts']
    donor,donor_record,_=b.prepare(tmp_path/'donor',b.binding(config,runtime),donor_producer,copy_original)
    before={name:b.file_record(donor/name) for name in b.DATABASES}
    target=dict(config,subject_sha256=q.digest(new),subject_directory=str(new_root),question_baselines=str(tmp_path/'target'/'question-baselines'))
    monkeypatch.setattr(q,'source_context',lambda root:old if Path(root)==old_root else new)
    monkeypatch.setattr(AnalyticsPipeline,'build_candidate',lambda *a,**k:(_ for _ in ()).throw(AssertionError('input adoption must not build product analytics')))
    real=reference.reference_artifact;calls=[]
    def counted(*args,**kwargs):calls.append(True);return real(*args,**kwargs)
    monkeypatch.setattr(reference,'reference_artifact',counted)
    (tmp_path/'trace').mkdir()
    progress=CollectorProgress(tmp_path/'trace','input-adoption-test')
    path,adopted,result=b.adopt_verified_input(target,dict(donor_producer,instance='new-verifier',subject_sha256=q.digest(new)),donor,old_root,progress)
    assert result['status']=='PASS' and result['qualification_credit']==0 and result['initial_analytics_builds']==0
    assert result['old_pass_results_reused'] is False and len(calls)==1
    assert result['independent_verification']['reference_mode']=='independent_rebuild'
    assert adopted['producer']['donor_baseline_sha256']==q.digest(donor_record)
    assert adopted['verification']['expected']==donor_record['verification']['expected']
    assert {name:b.file_record(donor/name) for name in b.DATABASES}==before
    assert path.is_dir() and (path/'baseline.json').is_file()
