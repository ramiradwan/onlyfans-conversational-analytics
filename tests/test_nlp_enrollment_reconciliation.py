"""Canonical selection revisions applied to real disposable stores."""
from uuid import uuid4
from pathlib import Path

import pytest

from app.analytics.nlp_findings_store import FindingsStore
from app.models.text_analysis_enrollment import CurrentEnrollment
from app.nlp.profiles import read_profile
from tests.continuous_analytics_fixture import ACCOUNT, make_fixture, cleanup

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]


@pytest.fixture
def selection(tmp_path):
    f=make_fixture(tmp_path,conversations=1,messages=1)
    profile=read_profile((Path(__file__).resolve().parents[1]/'shared/nlp/v2/fixtures/joint-profile.json').read_bytes())
    current=CurrentEnrollment(installation_id='installation',creator_account_id=ACCOUNT,principal_id='principal',
        mode_version=1,enrollment_id=str(uuid4()),state='enabled',capsule_available=True)
    yield f,FindingsStore(f.stores.database),profile,current
    cleanup(f)


def reconcile(store,current,profile,*,identity='canonical'):
    return store.reconcile_enrollment(ACCOUNT,canonical_store_id=identity,current=current,profile=profile)


def snapshot(store):
    with store.database.read() as db:
        return {table:[tuple(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY 1')]
                for table in ('nlp_accounts','nlp_jobs','nlp_enrollment_reconciliation','nlp_epoch_ledger')}


def test_exact_reconciliation_is_noop_and_mode_change_preserves_selection(selection):
    f,store,profile,current=selection
    assert reconcile(store,current,profile)
    first=snapshot(store)
    assert not reconcile(store,current,profile)
    assert snapshot(store) == first
    paused=current.model_copy(update={'mode_version':2,'state':'paused'})
    assert reconcile(store,paused,profile)
    state=store.state(ACCOUNT)
    assert state['mode'] == 'paused'
    assert state['selection_epoch'] == first['nlp_accounts'][0][2]
    epoch=state['selection_epoch']
    assert reconcile(store,paused.model_copy(update={'mode_version':3,'state':'enabled'}),profile)
    assert store.state(ACCOUNT)['selection_epoch'] == epoch


@pytest.mark.parametrize('change', ['older','store','equal_binding','same_parent_profile'])
def test_stale_or_changed_binding_refuses_without_mutation(selection,change):
    _,store,profile,current=selection
    reconcile(store,current,profile)
    identity='canonical'
    changed=current
    other=profile
    if change == 'older':
        changed=current.model_copy(update={'mode_version':0})
    elif change == 'store':
        identity='other-store'
    elif change == 'equal_binding':
        changed=current.model_copy(update={'principal_id':'other'})
    else:
        other=read_profile((Path(__file__).resolve().parents[1]/'shared/nlp/v2/fixtures/split-profile.json').read_bytes())
        changed=current.model_copy(update={'mode_version':2})
    before=snapshot(store)
    with pytest.raises(ValueError):
        reconcile(store,changed,other,identity=identity)
    assert snapshot(store) == before


def test_replacement_selects_once_and_retirement_preserves_retained_selection(selection):
    _,store,profile,current=selection
    reconcile(store,current,profile)
    epoch=store.state(ACCOUNT)['selection_epoch']
    replacement=current.model_copy(update={'enrollment_id':str(uuid4()),'mode_version':2})
    reconcile(store,replacement,profile)
    assert store.state(ACCOUNT)['selection_epoch'] == epoch+1
    retired=replacement.model_copy(update={'enrollment_id':None,'mode_version':3,'state':'retired','capsule_available':False})
    reconcile(store,retired,None)
    state=store.state(ACCOUNT)
    assert state['mode'] == 'paused' and state['selection_epoch'] == epoch+1
    assert read_profile(state['profile_json']) == profile
    assert not reconcile(store,retired,None)


def test_null_owner_without_selection_never_fabricates_profile(selection):
    _,store,_,current=selection
    retired=current.model_copy(update={'enrollment_id':None,'state':'retired','capsule_available':False})
    reconcile(store,retired,None)
    with pytest.raises(ValueError,match='account_missing'):
        store.state(ACCOUNT)


def test_projection_and_marker_rollback_together(selection,monkeypatch):
    _,store,profile,current=selection
    original=store._select_in
    def interrupted(*args,**kwargs):
        original(*args,**kwargs)
        raise RuntimeError('after selection')
    before=snapshot(store)
    monkeypatch.setattr(store,'_select_in',interrupted)
    with pytest.raises(RuntimeError,match='after selection'):
        reconcile(store,current,profile)
    assert snapshot(store) == before
