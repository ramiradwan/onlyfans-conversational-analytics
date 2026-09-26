"""Deleted-node checks select exact edge versions without scanning unrelated edges."""

import pytest
from sqlcipher3 import dbapi2 as sqlite3

from app.analytics import shared_graph as shared
from app.analytics import graph_membership_pages as pages
from app.analytics.graph_store import GraphReferentialIntegrityError
from tests.continuous_analytics_fixture import ACCOUNT
from tests.test_shared_graph import fixture


class ObservedConnection:
    def __init__(self, connection):
        self.connection, self.queries = connection, []

    def execute(self, statement, parameters=()):
        if 'FROM graph_edge_content e' in statement and 'AND EXISTS (' in statement:
            self.queries.append((statement, parameters))
        return self.connection.execute(statement, parameters)

    def __getattr__(self, name):
        return getattr(self.connection, name)


def validation_for(fixture, db, removed):
    generation = db.execute("SELECT * FROM projection_generations WHERE status='active'").fetchone()
    proof = fixture.stores.projections._trusted_graph_segment_proof(db, generation)
    assert proof is not None
    plans = tuple(shared.SegmentValidation(x.kind, x.bucket, x.segment_id,
                  x.digest, x.count, True) for x in proof.segments)
    value = shared.SharedGraphValidation(generation['graph_digest'], plans, proof, tuple(removed))
    return generation, value


@pytest.mark.parametrize('page_layout', [False, True])
def test_removed_node_queries_are_indexed_scoped_and_batched(fixture, monkeypatch, page_layout):
    fixture.pipeline.project_account(ACCOUNT)
    monkeypatch.setattr(pages, 'supported', lambda db: page_layout)
    with fixture.stores.database.read() as db:
        removed = ['g1:' + format(i, '064x') for i in range(260)]
        generation, validation = validation_for(fixture, db, removed)
        observer = ObservedConnection(db)
        assert shared._incremental_endpoint_links_valid(observer,
            generation['generation_id'], generation['creator_account_id'], validation, lambda: None)
        assert len(observer.queries) == 6
        assert [len(args)-2 for _, args in observer.queries] == [128, 128, 128, 128, 4, 4]
        for query, args in observer.queries:
            plan = [row[3] for row in db.execute('EXPLAIN QUERY PLAN ' + query, args)]
            side = 'source' if 'e.source_id IN' in query else 'target'
            assert any('graph_edge_content_by_' + side in row for row in plan)
            assert any('generation_id=?' in row and 'bucket=?' in row for row in plan)
            if page_layout:
                assert any('SEARCH r ' in row and 'page_id=?' in row for row in plan)
            assert args[0] == generation['creator_account_id']
            assert args[-1] == generation['generation_id']


def isolated_membership_db():
    db = sqlite3.connect(':memory:')
    db.executescript('''
        CREATE TABLE graph_edge_content(creator_account_id,content_id,edge_id,source_id,target_id,
            PRIMARY KEY(creator_account_id,content_id)) WITHOUT ROWID;
        CREATE INDEX graph_edge_content_by_source ON graph_edge_content(creator_account_id,source_id);
        CREATE INDEX graph_edge_content_by_target ON graph_edge_content(creator_account_id,target_id);
        CREATE TABLE generation_graph_segments(generation_id,creator_account_id,kind,bucket,segment_id,
            PRIMARY KEY(generation_id,creator_account_id,kind,bucket)) WITHOUT ROWID;
        CREATE TABLE graph_segment_edges(creator_account_id,segment_id,edge_id,content_id,
            PRIMARY KEY(creator_account_id,segment_id,edge_id)) WITHOUT ROWID;
        CREATE TABLE graph_segment_membership_pages(creator_account_id,segment_id,kind,bucket,page_id,
            PRIMARY KEY(creator_account_id,segment_id,bucket)) WITHOUT ROWID;
        CREATE TABLE graph_membership_edges(creator_account_id,page_id,edge_id,content_id,
            PRIMARY KEY(creator_account_id,page_id,edge_id)) WITHOUT ROWID;
    ''')
    return db


@pytest.mark.parametrize('page_layout', [False, True])
@pytest.mark.parametrize('side', ['source', 'target'])
def test_removed_node_matches_only_the_selected_account_generation_and_version(
    fixture, monkeypatch, page_layout, side
):
    fixture.pipeline.project_account(ACCOUNT)
    monkeypatch.setattr(pages, 'supported', lambda db: page_layout)
    with fixture.stores.database.read() as db:
        generation, validation = validation_for(fixture, db, ['g1:' + '0' * 64])
        observer = ObservedConnection(db)
        assert shared._incremental_endpoint_links_valid(observer, generation['generation_id'],
            generation['creator_account_id'], validation, lambda: None)
        query = next(sql for sql, _ in observer.queries if 'e.' + side + '_id IN' in sql)
    db = isolated_membership_db()
    edge = 'g1:abc' + '0' * 61
    try:
        for account in ('a', 'other'):
            for content, endpoint in (('old', 'removed'), ('new', 'kept')):
                db.execute('INSERT INTO graph_edge_content VALUES (?,?,?,?,?)',
                    (account, content, edge, endpoint, endpoint))
            db.execute('INSERT INTO generation_graph_segments VALUES (?,?,?,?,?)',
                ('selected', account, 'edge', 'ab', 'current'))
            db.execute('INSERT INTO graph_segment_membership_pages VALUES (?,?,?,?,?)',
                (account, 'current', 'edge', 'abc', 'page'))
            for table, owner in (('graph_segment_edges', 'current'), ('graph_membership_edges', 'page')):
                db.execute('INSERT INTO ' + table + ' VALUES (?,?,?,?)',
                    (account, owner, edge, 'new' if account == 'a' else 'old'))
        db.execute("INSERT INTO generation_graph_segments VALUES ('retained','a','edge','ab','old-segment')")
        db.execute("INSERT INTO graph_segment_membership_pages VALUES ('a','old-segment','edge','abc','old-page')")
        db.execute('INSERT INTO graph_segment_edges VALUES (?,?,?,?)', ('a', 'old-segment', edge, 'old'))
        db.execute('INSERT INTO graph_membership_edges VALUES (?,?,?,?)', ('a', 'old-page', edge, 'old'))
        args = ('a', 'removed', 'selected')
        assert db.execute(query, args).fetchone() is None
        assert db.execute(query, ('a', 'removed', 'retained')).fetchone() == (1,)
        assert db.execute(query, ('other', 'removed', 'selected')).fetchone() == (1,)
        assert db.execute(query, ('missing', 'removed', 'selected')).fetchone() is None
        assert db.execute(query, ('a', 'removed', 'missing')).fetchone() is None
        def instruction_count():
            steps = 0
            def progress():
                nonlocal steps
                steps += 1
                return 0
            db.set_progress_handler(progress, 1)
            try:
                assert db.execute(query, args).fetchone() is None
                return steps
            finally:
                db.set_progress_handler(None, 0)
        before = instruction_count()
        db.executemany('INSERT INTO graph_edge_content VALUES (?,?,?,?,?)',
            [('a', 'extra-' + str(i), 'g1:' + format(i, '064x'), 'unrelated', 'unrelated')
             for i in range(4096)])
        assert instruction_count() <= before + 30
        table = 'graph_membership_edges' if page_layout else 'graph_segment_edges'
        owner_field = 'page_id' if page_layout else 'segment_id'
        owner = 'page' if page_layout else 'current'
        db.execute('UPDATE ' + table + " SET content_id='old' WHERE creator_account_id=? AND "
                   + owner_field + '=?', ('a', owner))
        assert db.execute(query, args).fetchone() == (1,)
        db.execute('UPDATE ' + table + " SET edge_id='different' WHERE creator_account_id=? AND "
                   + owner_field + '=?', ('a', owner))
        assert db.execute(query, args).fetchone() is None
    finally:
        db.close()


@pytest.mark.parametrize('side', ['source', 'target'])
def test_selected_dangling_endpoint_is_still_rejected(fixture, side):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.read() as db:
        endpoint = db.execute(f'SELECT {side}_id FROM graph_edges LIMIT 1').fetchone()[0]
        generation, validation = validation_for(fixture, db, [endpoint])
        with pytest.raises(GraphReferentialIntegrityError, match='graph_endpoint_absent'):
            shared._incremental_endpoint_links_valid(db, generation['generation_id'],
                generation['creator_account_id'], validation, lambda: None)


def test_removal_batches_keep_cancellation(fixture):
    from app.analytics.errors import ProjectionBuildCancelled

    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.read() as db:
        removed = ['g1:' + format(i, '064x') for i in range(260)]
        generation, validation = validation_for(fixture, db, removed)
        observer = ObservedConnection(db)
        def cancel():
            if len(observer.queries) == 2:
                raise ProjectionBuildCancelled()
        with pytest.raises(ProjectionBuildCancelled):
            shared._incremental_endpoint_links_valid(observer, generation['generation_id'],
                generation['creator_account_id'], validation, cancel)
        assert len(observer.queries) == 2
