"""Local collection UI opens directly; external API requests still have independent access."""
import pytest
from fastapi.testclient import TestClient

from collection_context.application.contracts import ContextError
from collection_context.interfaces.access import AccessRegistry
from collection_context.interfaces.http import create_app
from collection_context.interfaces.security import AccessPolicy
from collection_context.library.store import LibraryStore


def test_local_page_opens_without_owner_key_and_saves_settings(tmp_path):
    store = LibraryStore.initialize(tmp_path / 'original-local-fixture')
    registry = AccessRegistry(store)
    policy = AccessPolicy('http://127.0.0.1:8790', [], local_ui=True)
    with TestClient(create_app(store.files.root, policy, refresh=lambda: registry.refresh(policy)),
        base_url=policy.origin) as client:
        response = client.get('/v1/session')
        assert response.status_code == 200
        session = response.json()['data']
        assert session['local_ui'] and 'ui:manage' in session['permissions']
        assert 'httponly' in response.headers['set-cookie'].lower()
        headers = {'X-CSRF-Token': session['csrf_token']}
        assert client.post('/v1/management/preferences-save', json={'download_media': False}, headers=headers).status_code == 200
        assert client.post('/v1/management/preferences', json={}, headers=headers).json()['data']['download_media'] is False
        assert client.post('/v1/management/preferences', json={}).status_code == 403
        assert client.get('/v1/session').json()['data']['csrf_token'] == session['csrf_token']
        client.cookies.clear()
        assert client.get('/v1/collections/overview').status_code == 401
        assert client.get('/v1/session', headers={'Origin': 'https://other.invalid'}).status_code == 403
        assert client.get('/v1/session', headers={'Authorization': 'Bearer invalid'}).status_code == 401
        assert not registry.records()
    store.close()


def test_remote_page_does_not_inherit_local_owner_access():
    with pytest.raises(ContextError):
        AccessPolicy('https://fixture.invalid', [], remote=True, local_ui=True)


def test_launcher_never_presents_or_creates_owner_key(tmp_path, monkeypatch):
    from io import StringIO
    import socket
    from types import SimpleNamespace
    from collection_context.launcher import launch
    captured = {}
    def fake_app(workspace, policy, **options):
        captured['policy'] = policy
        return SimpleNamespace(state=SimpleNamespace())
    class FakeServer:
        def __init__(self, config):
            self.should_exit = False
            self.started = True
        def run(self): pass
    monkeypatch.setattr('collection_context.launcher.create_app', fake_app)
    monkeypatch.setattr('collection_context.launcher._wait_for_ready', lambda *a, **kw: None)
    monkeypatch.setattr('uvicorn.Server', FakeServer)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    output = StringIO()
    assert launch(tmp_path / 'local-ui', port=port, initialize_empty=True, no_browser=True, output=output,
        owner_presenter=lambda _: pytest.fail('no startup password popup')) == 0
    assert captured['policy'].local_ui
    store = LibraryStore(tmp_path / 'local-ui')
    assert not AccessRegistry(store).records()
    assert 'scc_' not in output.getvalue()
    store.close()
