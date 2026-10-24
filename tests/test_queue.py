"""Across invocations, queued updates survive scan and proposal budgets."""
import json

import pytest
from test_batches import scan as scan_fixture

from xpkgindex_update import cli
from xpkgindex_update.manifest import descriptor_digest
from xpkgindex_update.state import eligible, enqueue, load_state, save_state, settle

scan = scan_fixture


def record(identity, candidate='2.0'):
    return {'id': identity, 'candidate': candidate, 'status': 'update-ready', 'path': 'pkgs/f/foo.lua'}


def test_newer_candidate_keeps_its_fifo_position_and_resets_backoff():
    state = {}
    enqueue(state, [record('xim:a'), record('xim:b')], 100)
    state['queue'][0].update(attempts=3, retry_at=999)
    enqueue(state, [record('xim:a', '3.0')], 200)
    assert [r['id'] for r in eligible(state, 200)] == ['xim:a', 'xim:b']
    assert state['queue'][0]['first_seen'] == 100
    assert state['queue'][0]['attempts'] == 0
    assert state['queue'][0]['record']['candidate'] == '3.0'


def test_failure_yields_to_others_and_waits_before_retry(tmp_path):
    state, path = {}, tmp_path / 'state.json'
    a, b = record('xim:a'), record('xim:b')
    enqueue(state, [a, b], 100)
    settle(state, a, {'status': 'error', 'reason': 'temporary outage'}, 100)
    save_state(path, state)
    restored = load_state(path)
    assert [r['id'] for r in eligible(restored, 101)] == ['xim:b']
    assert [r['id'] for r in eligible(restored, 3700)] == ['xim:b', 'xim:a']
    settle(restored, b, {'status': 'updated'}, 101)
    assert len(restored['queue']) == 1


def test_two_runs_drain_more_than_twenty_candidates(scan, monkeypatch, tmp_path):
    _, _, state, config = scan
    config.write_text(config.read_text() + 'max_prs: 20\n')
    records = [record(f'xim:pkg{i}') for i in range(25)]
    plan = tmp_path / 'ready.json'
    plan.write_text(json.dumps({'format': 2, 'adapter': 'xim-pkgindex', 'packages': records}))
    called = []
    def propose(root, plan, github, **kwargs):
        item = plan['packages'][0]
        called.append(item['id'])
        return [{'id': item['id'], 'status': 'created'}]
    monkeypatch.setattr(cli, 'propose', propose)
    args = ['--workspace', str(tmp_path), 'propose', '--plan', str(plan), '--state-file', str(state),
            '--output', str(tmp_path / 'proposals.json')]
    assert cli.main(args) == 0
    assert called == [r['id'] for r in records[:20]]
    assert len(load_state(state)['queue']) == 5
    # A later plan need not rediscover the outstanding candidates.
    plan.write_text(json.dumps({'format': 2, 'adapter': 'xim-pkgindex', 'packages': []}))
    assert cli.main(args) == 0
    assert called == [r['id'] for r in records]
    assert load_state(state)['queue'] == []


def test_scan_retains_unvisited_candidates_but_invalidates_changed_recipe(scan, monkeypatch, tmp_path):
    run, _, state, _ = scan
    def ready(info, client):
        return {**record(info['id']), 'path': info['path'], 'descriptor_sha256': descriptor_digest(info['text'])}
    monkeypatch.setattr(cli.get_adapter('xim-pkgindex'), 'check_package', ready)
    run()
    assert [q['record']['id'] for q in load_state(state)['queue']] == ['xim:a', 'xim:b']
    # Next batch scans c/d; a/b remain available in the plan.
    _, plan = run()
    assert [r['id'] for r in plan['candidates']] == ['xim:a', 'xim:b', 'xim:c', 'xim:d']
    path = tmp_path / 'pkgs/a/a.lua'
    path.write_text(path.read_text() + '\n-- reviewed recipe change\n')
    _, plan = run()
    # a is rescanned in this wrapping batch, and re-enters after older candidates.
    assert [r['id'] for r in plan['candidates']] == ['xim:b', 'xim:c', 'xim:d', 'xim:e', 'xim:a']


def test_failed_candidate_does_not_hold_the_twenty_attempt_limit(scan, monkeypatch, tmp_path):
    _, _, state, config = scan
    config.write_text(config.read_text() + 'max_prs: 1\n')
    plan = tmp_path / 'ready.json'
    plan.write_text(json.dumps({'format': 2, 'adapter': 'xim-pkgindex',
                               'packages': [record('xim:a'), record('xim:b')]}))
    calls = []
    def propose(root, plan, github, **kwargs):
        identity = plan['packages'][0]['id']
        calls.append(identity)
        return [{'id': identity, 'status': 'error' if identity == 'xim:a' else 'created'}]
    monkeypatch.setattr(cli, 'propose', propose)
    args = ['--workspace', str(tmp_path), 'propose', '--plan', str(plan), '--state-file', str(state),
            '--output', str(tmp_path / 'proposals.json')]
    assert cli.main(args) == 1
    assert cli.main(args) == 0
    assert calls == ['xim:a', 'xim:b']


def test_corrupt_state_is_reported_instead_of_discarding_the_queue(tmp_path):
    path = tmp_path / 'state.json'
    path.write_text('{broken')
    with pytest.raises(cli.Invalid, match='refusing to discard'):
        load_state(path)
