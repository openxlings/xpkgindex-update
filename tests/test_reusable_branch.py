"""Real Git origin/worktrees prove PR refreshes are ordinary fast-forward pushes."""
import json
import subprocess
from pathlib import Path

import pytest
from test_update import FakeGitHub, descriptor

from xpkgindex_update import pull_request as mod
from xpkgindex_update.lua_edit import Invalid


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / 'index'
    root.mkdir()
    path = root / 'pkgs/f/foo.lua'
    path.parent.mkdir(parents=True)
    path.write_text(descriptor())
    def git(*args, cwd=root, env=None):
        return subprocess.check_output(['git', *args], cwd=cwd, env=env, text=True, stderr=subprocess.PIPE).strip()
    git('init', '-b', 'main')
    git('config', 'core.autocrlf', 'false')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.org')
    git('add', 'pkgs')
    git('commit', '-m', 'fixture')
    origin = tmp_path / 'origin.git'
    git('init', '--bare', str(origin))
    git('remote', 'add', 'origin', str(origin))
    git('push', 'origin', 'main')
    client = FakeGitHub()
    client.token = 'fixture-token-never-sent'
    adapter = mod.get_adapter('mcpp-index')
    def plan(version='1.1.0'):
        client.releases = [{'tag_name': 'v' + version}]
        record = adapter.check_package(adapter.package_info(path, root), client)
        assert record['status'] == 'update-ready'
        return {'base_commit': git('rev-parse', 'HEAD'), 'adapter': 'mcpp-index', 'packages': [record]}
    context = {'root': root, 'origin': origin, 'path': path, 'git': git, 'client': client,
               'plan': plan, 'prs': [], 'events': [], 'pushes': []}
    real_run = mod.run
    def run(*args, cwd, env=None):
        if args[:3] == ('gh', 'repo', 'view'):
            return json.dumps({'nameWithOwner': 'acme/index', 'defaultBranchRef': {'name': 'main'}})
        if args[:3] == ('gh', 'pr', 'list'):
            return json.dumps(context['prs'] if 'open' in args else [])
        if args[:3] == ('gh', 'pr', 'create'):
            context['events'].append('create')
            pr = {'headRefName': args[args.index('--head') + 1], 'state': 'OPEN',
                  'url': 'https://github.com/acme/index/pull/1', 'files': [{'path': 'pkgs/f/foo.lua'}],
                  'author': {'login': 'github-actions[bot]'}, 'isCrossRepository': False,
                  'body': Path(args[args.index('--body-file') + 1]).read_text()}
            context['prs'].append(pr)
            return pr['url']
        if args[:2] == ('gh', 'api'):
            context['events'].append('edit')
            payload = json.loads(Path(args[args.index('--input') + 1]).read_text())
            context['prs'][0].update(payload)
            return '{}'
        if args[:3] == ('gh', 'pr', 'close'):
            context['events'].append('close')
            context['prs'][:] = [p for p in context['prs'] if p['url'] != args[3]]
            return ''
        if args[0] == 'git' and 'push' in args:
            context['pushes'].append(args)
            assert not any(a.startswith('--force') or a.startswith('+') for a in args)
        return real_run(*args, cwd=cwd, env=env)
    monkeypatch.setattr(mod, 'run', run)
    context['run'] = run
    return context


def tip(repo, branch='auto/bump-compat-foo'):
    return repo['git']('ls-remote', 'origin', branch).split()[0]


def test_new_release_reuses_one_pr_and_replaces_only_unpublished_candidate(repo):
    first = mod.propose(repo['root'], repo['plan'](), repo['client'])
    old = tip(repo)
    second = mod.propose(repo['root'], repo['plan']('1.2.0'), repo['client'])
    new = tip(repo)
    assert first[0]['status'] == 'created' and second[0]['status'] == 'updated'
    assert first[0]['url'] == second[0]['url']
    assert repo['prs'][0]['headRefName'] == 'auto/bump-compat-foo'
    assert old != new
    assert repo['git']('merge-base', old, new) == old
    text = repo['git']('show', f'{new}:pkgs/f/foo.lua')
    assert '["1.2.0"]' in text and '["1.1.0"]' not in text and '["1.0.0"]' in text
    assert '1.2.0' in repo['prs'][0]['title']
    assert len(repo['prs']) == 1 and len(repo['pushes']) == 2
    # Unchanged candidates reuse the head without another push or CI-worthy commit.
    third = mod.propose(repo['root'], repo['plan']('1.2.0'), repo['client'])
    assert third[0]['status'] == 'pending' and tip(repo) == new and len(repo['pushes']) == 2


def test_refresh_includes_new_base_without_a_rebase_or_force_push(repo):
    mod.propose(repo['root'], repo['plan'](), repo['client'])
    old = tip(repo)
    (repo['root'] / 'README.md').write_text('an independent reviewed change\n')
    repo['git']('add', 'README.md')
    repo['git']('commit', '-m', 'advance main')
    repo['git']('push', 'origin', 'main')
    base = repo['git']('rev-parse', 'HEAD')
    mod.propose(repo['root'], repo['plan']('1.2.0'), repo['client'])
    new = tip(repo)
    assert repo['git']('merge-base', old, new) == old
    assert repo['git']('merge-base', base, new) == base
    assert repo['git']('diff', '--name-only', base, new) == 'pkgs/f/foo.lua'


def test_human_edit_is_preserved_even_when_pr_author_is_the_bot(repo):
    mod.propose(repo['root'], repo['plan'](), repo['client'])
    old = tip(repo)
    tree = repo['git']('rev-parse', old + '^{tree}')
    human = repo['git']('commit-tree', tree, '-p', old, '-m', 'human review adjustment')
    repo['git']('push', 'origin', human + ':refs/heads/auto/bump-compat-foo')
    with pytest.raises(Invalid, match='modified outside the updater'):
        mod.propose(repo['root'], repo['plan']('1.2.0'), repo['client'])
    assert tip(repo) == human


def test_concurrent_branch_update_rejects_push_without_overwriting(repo, monkeypatch):
    mod.propose(repo['root'], repo['plan'](), repo['client'])
    old = tip(repo)
    tree = repo['git']('rev-parse', old + '^{tree}')
    concurrent = repo['git']('commit-tree', tree, '-p', old, '-m', 'concurrent adjustment')
    repo['git']('push', 'origin', concurrent + ':refs/heads/concurrent')
    def run(*args, cwd, env=None):
        if args[0] == 'git' and 'push' in args:
            repo['git']('--git-dir', str(repo['origin']), 'update-ref', 'refs/heads/auto/bump-compat-foo', concurrent)
        return repo['run'](*args, cwd=cwd, env=env)
    monkeypatch.setattr(mod, 'run', run)
    with pytest.raises(Invalid, match='rejected'):
        mod.propose(repo['root'], repo['plan']('1.2.0'), repo['client'])
    assert tip(repo) == concurrent


def test_bot_legacy_pr_is_closed_only_after_stable_pr_creation(repo):
    mod.propose(repo['root'], repo['plan'](), repo['client'])
    old = tip(repo)
    repo['git']('--git-dir', str(repo['origin']), 'update-ref', 'refs/heads/auto/bump-compat-foo-1.1.0', old)
    repo['git']('--git-dir', str(repo['origin']), 'update-ref', '-d', 'refs/heads/auto/bump-compat-foo')
    repo['prs'][0].update(headRefName='auto/bump-compat-foo-1.1.0', url='https://github.com/acme/index/pull/old')
    repo['events'].clear()
    mod.propose(repo['root'], repo['plan']('1.2.0'), repo['client'])
    assert repo['events'] == ['create', 'close']
    assert len(repo['prs']) == 1 and repo['prs'][0]['headRefName'] == 'auto/bump-compat-foo'


def test_moved_production_base_requires_a_new_plan(repo):
    plan = repo['plan']()
    old = repo['git']('rev-parse', 'HEAD')
    tree = repo['git']('rev-parse', old + '^{tree}')
    new = repo['git']('commit-tree', tree, '-p', old, '-m', 'concurrent main')
    repo['git']('push', 'origin', new + ':refs/heads/main')
    with pytest.raises(Invalid, match='base advanced'):
        mod.propose(repo['root'], plan, repo['client'])
    assert not repo['pushes']


def test_failed_migration_keeps_the_legacy_pr_open(repo, monkeypatch):
    mod.propose(repo['root'], repo['plan'](), repo['client'])
    old = tip(repo)
    repo['git']('--git-dir', str(repo['origin']), 'update-ref', 'refs/heads/auto/bump-compat-foo-1.1.0', old)
    repo['git']('--git-dir', str(repo['origin']), 'update-ref', '-d', 'refs/heads/auto/bump-compat-foo')
    repo['prs'][0].update(headRefName='auto/bump-compat-foo-1.1.0', url='https://github.com/acme/index/pull/old')
    def run(*args, cwd, env=None):
        if args[:3] == ('gh', 'pr', 'create'):
            raise Invalid('API temporarily unavailable')
        return repo['run'](*args, cwd=cwd, env=env)
    monkeypatch.setattr(mod, 'run', run)
    with pytest.raises(Invalid, match='temporarily unavailable'):
        mod.propose(repo['root'], repo['plan']('1.2.0'), repo['client'])
    assert len(repo['prs']) == 1 and repo['prs'][0]['headRefName'].endswith('-1.1.0')
    assert 'close' not in repo['events']
    assert tip(repo)  # Pushed replacement can be recovered on the next invocation.
