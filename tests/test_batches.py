"""Bounded batches share budgets, checkpoint progress, and retry unfinished work."""
import json
import subprocess

import pytest
from test_xim import descriptor

from xpkgindex_update import cli


@pytest.fixture
def scan(tmp_path, monkeypatch):
    for name in 'abcde':
        path = tmp_path / f'pkgs/{name}/{name}.lua'
        path.parent.mkdir(parents=True)
        path.write_text(descriptor().replace('name = "foo"', f'name = "{name}"'))
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    subprocess.run(['git', '-C', str(tmp_path), 'add', 'pkgs'], check=True)
    subprocess.run(['git', '-C', str(tmp_path), '-c', 'user.name=Test',
                    '-c', 'user.email=test@example.org', 'commit', '-qm', 'fixture'], check=True)
    config = tmp_path / '.github/xpkgindex-update.yml'
    config.parent.mkdir()
    config.write_text('adapter: xim-pkgindex\nmax_packages: 2\nrequest_budget: 100\ndownload_bytes: 100\n')
    seen = []
    def check(info, client):
        seen.append(info['id'])
        client.requests += 1
        client.downloaded += 1
        return {'id': info['id'], 'path': info['path'], 'status': 'up-to-date'}
    monkeypatch.setattr(cli.get_adapter('xim-pkgindex'), 'check_package', check)
    output, state = tmp_path / 'plan.json', tmp_path / 'state.json'
    def run(*args):
        code = cli.main(['--workspace', str(tmp_path), 'check', '--output', str(output),
                         '--state-file', str(state), *args])
        return code, json.loads(output.read_text())
    return run, seen, state, config


def test_multiple_batches_cover_inventory_once(scan):
    run, seen, state, _ = scan
    code, report = run('--max-batches', '3')
    assert code == 0
    assert seen == ['xim:' + n for n in 'abcde']
    assert report['summary'] == {'up-to-date': 5}
    assert report['scan'] == {'batch_size': 2, 'max_batches': 3, 'batches': 3,
                              'processed': 5, 'budget_exhausted': None}
    assert json.loads(state.read_text())['cursor'] == 'xim:e'


def test_default_single_batch_rotates_without_starvation(scan):
    run, seen, state, _ = scan
    _, report = run()
    assert report['summary']['budget-deferred'] == 3
    assert report['packages'][2]['reason'] == 'scan batch limit exhausted'
    assert json.loads(state.read_text())['cursor'] == 'xim:b'
    run()
    run()
    assert seen[:5] == ['xim:' + n for n in 'abcde']


@pytest.mark.parametrize('budget,reason', [
    ('request_budget', 'run request budget exhausted'),
    ('download_bytes', 'run download budget exhausted'),
])
def test_network_budget_is_shared_across_batches(scan, budget, reason):
    run, seen, state, config = scan
    config.write_text(config.read_text().replace(f'{budget}: 100', f'{budget}: 3'))
    _, report = run('--max-batches', '3')
    assert len(seen) == 3
    assert report['summary'] == {'up-to-date': 3, 'budget-deferred': 2}
    assert report['scan']['budget_exhausted'] == reason
    assert json.loads(state.read_text())['cursor'] == 'xim:c'
    run('--max-batches', '3')
    assert seen[3:5] == ['xim:d', 'xim:e']


def test_checkpoint_survives_interruption_after_complete_batch(scan, monkeypatch):
    run, _, state, _ = scan
    def interrupted(info, client):
        if info['id'] == 'xim:c':
            raise KeyboardInterrupt
        return {'id': info['id'], 'status': 'up-to-date'}
    monkeypatch.setattr(cli.get_adapter('xim-pkgindex'), 'check_package', interrupted)
    with pytest.raises(KeyboardInterrupt):
        run('--max-batches', '3')
    assert json.loads(state.read_text())['cursor'] == 'xim:b'


def test_resource_budget_failure_does_not_advance_past_unchecked_package(scan, monkeypatch):
    run, _, state, _ = scan
    state.write_text(json.dumps({'cursor': 'xim:a', 'waiting': {}}))
    def exhausted(info, client):
        return {'id': info['id'], 'status': 'budget-deferred', 'reason': 'run request budget exhausted'}
    monkeypatch.setattr(cli.get_adapter('xim-pkgindex'), 'check_package', exhausted)
    _, report = run('--max-batches', '3')
    assert report['summary'] == {'budget-deferred': 5}
    assert json.loads(state.read_text())['cursor'] == 'xim:a'


@pytest.mark.parametrize('value', ['0', '21'])
def test_batch_count_is_bounded(scan, value):
    run, _, _, _ = scan
    with pytest.raises(SystemExit):
        run('--max-batches', value)


def test_twenty_proposals_is_independent_of_scan_batch_size(scan, monkeypatch, tmp_path):
    _, _, _, config = scan
    config.write_text(config.read_text() + 'max_prs: 20\n')
    plan = tmp_path / 'ready.json'
    plan.write_text(json.dumps({'format': 2, 'adapter': 'xim-pkgindex', 'packages': [
        {'id': f'xim:pkg{i}', 'status': 'update-ready'} for i in range(25)]}))
    created = []
    def propose(root, plan, github, **kwargs):
        record = plan['packages'][0]
        created.append(record['id'])
        return [{'id': record['id'], 'status': 'created'}]
    monkeypatch.setattr(cli, 'propose', propose)
    output = tmp_path / 'proposals.json'
    assert cli.main(['--workspace', str(tmp_path), 'propose', '--plan', str(plan),
                     '--output', str(output)]) == 0
    assert len(created) == 20
    assert len(json.loads(output.read_text())['packages']) == 20


def test_one_request_heavy_package_cannot_starve_the_inventory(scan, monkeypatch):
    run, seen, state, config = scan
    config.write_text(config.read_text() + 'package_requests: 2\n')
    from xpkgindex_update.github import BudgetExceeded
    def check(info, client):
        seen.append(info['id'])
        try:
            for _ in range(3 if info['id'] == 'xim:a' else 1):
                client.charge_request()
        except BudgetExceeded as exc:
            return {'id': info['id'], 'status': 'budget-deferred', 'reason': str(exc)}
        return {'id': info['id'], 'status': 'up-to-date'}
    monkeypatch.setattr(cli.get_adapter('xim-pkgindex'), 'check_package', check)
    code, report = run('--max-batches', '3')
    assert code == 0
    assert report['summary'] == {'package-deferred': 1, 'up-to-date': 4}
    assert seen == ['xim:' + n for n in 'abcde']
    assert json.loads(state.read_text())['cursor'] == 'xim:e'
