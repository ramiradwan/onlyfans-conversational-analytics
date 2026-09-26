"""Membership admission stays exact and avoids unrelated segment scans."""

from pathlib import Path
import re

import pytest
from sqlcipher3 import dbapi2 as sqlite3


@pytest.mark.parametrize('kind', ['node', 'edge'])
def test_admission_query_is_bounded_by_the_exact_page(kind):
    source = (Path(__file__).parents[1] /
              'app/analytics/sql/0020_shared_graph_membership_pages.sql').read_text()
    body = re.search(r'CREATE TRIGGER graph_membership_' + kind +
                     r's_insert\b.*?END;', source, re.S).group()
    query = body.split('WHEN NOT EXISTS(', 1)[1].split(')\n OR EXISTS', 1)[0]
    query = query.replace('NEW.creator_account_id', ':account')
    query = query.replace('NEW.page_id', ':page').replace('NEW.' + kind + '_id', ':identity')
    assert query.count('CROSS JOIN') == 4
    original = query.replace('CROSS JOIN', 'JOIN')
    db = sqlite3.connect(':memory:')
    db.executescript('''
        CREATE TABLE graph_membership_pages (
            creator_account_id, page_id, kind, bucket, sealed,
            PRIMARY KEY(creator_account_id,page_id)) WITHOUT ROWID;
        CREATE TABLE graph_segment_membership_pages (
            creator_account_id, segment_id, page_id,
            PRIMARY KEY(creator_account_id,segment_id,page_id)) WITHOUT ROWID;
        CREATE INDEX graph_membership_page_references ON
            graph_segment_membership_pages(creator_account_id,page_id);
        CREATE TABLE graph_segments (creator_account_id,segment_id,sealed,
            PRIMARY KEY(creator_account_id,segment_id)) WITHOUT ROWID;
        CREATE TABLE generation_graph_segments (generation_id,creator_account_id,segment_id,
            PRIMARY KEY(generation_id,creator_account_id,segment_id)) WITHOUT ROWID;
        CREATE INDEX generation_graph_segments_by_segment ON
            generation_graph_segments(creator_account_id,segment_id);
        CREATE TABLE projection_generations (generation_id,creator_account_id,status,
            PRIMARY KEY(generation_id)) WITHOUT ROWID;
        INSERT INTO graph_segments VALUES ('a','zz-target',0);
        INSERT INTO graph_segment_membership_pages VALUES ('a','zz-target','page');
        INSERT INTO generation_graph_segments VALUES ('target','a','zz-target');
        INSERT INTO projection_generations VALUES ('target','a','building');
    ''')
    db.execute('INSERT INTO graph_membership_pages VALUES (?,?,?,?,0)',
               ('a', 'page', kind, 'abc'))
    params = {'account': 'a', 'page': 'page', 'identity': 'x1:abc' + '0' * 61}
    def observed(sql):
        steps = 0
        def progress():
            nonlocal steps
            steps += 1
            return 0
        db.set_progress_handler(progress, 1)
        try:
            return db.execute(sql, params).fetchall(), steps
        finally:
            db.set_progress_handler(None, 0)
    try:
        before, short_steps = observed(query)
        assert before == [(1,)]
        db.executemany('INSERT INTO generation_graph_segments VALUES (?,?,?)',
                       [(f'g{i:04}', 'a', f's{i:04}') for i in range(1024)])
        db.executemany('INSERT INTO projection_generations VALUES (?,?,?)',
                       [(f'g{i:04}', 'a', 'building') for i in range(1024)])
        after, large_steps = observed(query)
        assert before == after
        assert large_steps <= short_steps + 10
        assert large_steps < 200
        assert observed(original)[0] == after
        plan = [row[3] for row in db.execute('EXPLAIN QUERY PLAN ' + query, params)]
        assert any('SEARCH m USING' in step and 'segment_id=?' in step for step in plan)
        db.commit()
        for sql in (
            "UPDATE graph_membership_pages SET sealed=1",
            "UPDATE graph_membership_pages SET bucket='def'",
            "UPDATE graph_membership_pages SET kind='other'",
            "UPDATE graph_segments SET sealed=1",
            "UPDATE projection_generations SET status='retired'",
            "DELETE FROM graph_segment_membership_pages",
        ):
            db.execute('BEGIN')
            db.execute(sql)
            assert observed(query)[0] == observed(original)[0] == []
            db.rollback()
        for key in ('account', 'page', 'identity'):
            bad = dict(params, **{key: 'absent'})
            assert db.execute(query, bad).fetchall() == db.execute(original, bad).fetchall() == []
    finally:
        db.close()


@pytest.mark.parametrize('trigger,table,alias', [
    ('graph_membership_pages_insert', 'graph_segments', 's'),
    ('graph_membership_pages_immutable', 'graph_segment_membership_pages', 'p'),
    ('graph_membership_selection_insert', 'graph_segments', 's'),
])
def test_page_metadata_guards_follow_the_selected_object(tmp_path, trigger, table, alias):
    from app.analytics.database import ProjectionsDatabase

    database = ProjectionsDatabase(tmp_path / 'metadata.sqlite3')
    with database.read() as db:
        body = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                          (trigger,)).fetchone()[0]
        start = body.index('SELECT 1 FROM ' + table + ' ' + alias)
        last = body.index("g.status='building'", start) + len("g.status='building'")
        query = body[start:last]
        for field, parameter in (('creator_account_id', 'account'), ('page_id', 'page'),
                                 ('segment_id', 'segment'), ('kind', 'kind'), ('bucket', 'bucket')):
            query = query.replace('NEW.' + field, ':' + parameter)
            query = query.replace('OLD.' + field, ':' + parameter)
        assert query.count('CROSS JOIN') == 2
        parameters = {'account': 'a', 'page': 'p', 'segment': 's', 'kind': 'node', 'bucket': 'abc'}
        plan = [row[3] for row in db.execute('EXPLAIN QUERY PLAN ' + query, parameters)]
        assert plan[0].startswith('SEARCH ' + alias + ' USING ')
        assert any('SEARCH m USING ' in step and 'segment_id=?' in step for step in plan)
        assert not db.execute(query, parameters).fetchall()
