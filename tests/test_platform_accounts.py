from utils.mcp.workspace_credentials import WorkspaceClient


def test_automatic_claim_uses_hub_selected_account(monkeypatch):
    client = WorkspaceClient('http://hub', 'group-key', 'toolathlon', '')
    calls = []

    def request(method, path, data=None):
        calls.append((method, path, data))
        return {'workspace': 'account-b', 'id': 'run-1', 'token': 'capability'}

    monkeypatch.setattr(client, 'request', request)
    client.start()
    assert calls == [('POST', '/api/workspace-runs', {'catalog': 'toolathlon'})]
    assert client.workspace == 'account-b'
    assert client.id == 'run-1'


def test_explicit_account_is_preserved(monkeypatch):
    client = WorkspaceClient('http://hub', 'group-key', 'toolathlon', 'account-a')
    calls = []

    def request(method, path, data=None):
        calls.append(data)
        return {'id': 'run-1', 'token': 'capability'}

    monkeypatch.setattr(client, 'request', request)
    client.start()
    assert calls[0]['workspace'] == client.workspace == 'account-a'
