"""Hub-owned account material and isolated Toolathlon run snapshots.

This module uses the standard library so deployment/bootstrap scripts can use it
before the benchmark environment has been installed. It never logs secret data.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class CredentialError(RuntimeError):
    def __init__(self, message, *, code='sync_failed'):
        super().__init__(message)
        self.code = code


class WorkspaceClient:
    def __init__(self, root: str, key: str, catalog: str, workspace: str):
        url = urlsplit(root)
        if (url.scheme not in {"http", "https"} or not url.hostname or
                url.username or url.password or url.query or url.fragment):
            raise CredentialError("Invalid MCPHUB_URL")
        if not key or any(c.isspace() for c in key):
            raise CredentialError("MCPHUB_TOKEN is required")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", catalog):
            raise CredentialError("Invalid MCPHUB_CATALOG")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}", workspace):
            raise CredentialError("Select a workspace with MCPHUB_TARGET")
        self.root, self.key, self.catalog, self.workspace = root.rstrip('/'), key, catalog, workspace
        self.id = ""
        self.token = ""

    @classmethod
    def from_env(cls):
        client = cls(os.getenv("MCPHUB_URL", ""), os.getenv("MCPHUB_TOKEN", ""),
                     os.getenv("MCPHUB_CATALOG", "toolathlon"), os.getenv("MCPHUB_TARGET", ""))
        client.id = os.getenv("MCPHUB_RUN_ID", "")
        client.token = os.getenv("MCPHUB_RUN_TOKEN", "")
        return client

    def request(self, method, path, data=None):
        headers = {"Authorization": "Bearer " + self.key, "Content-Type": "application/json"}
        if self.token:
            headers["X-MCPHub-Run-Token"] = self.token
        request = Request(self.root + path, headers=headers, method=method,
                          data=None if data is None else json.dumps(data).encode())
        try:
            with urlopen(request, timeout=30) as response:
                result = json.load(response)
        except HTTPError as exc:
            if exc.code == 409 and method == 'POST' and path == '/api/workspace-runs':
                raise CredentialError('Workspace account is occupied by a previous run. Exit its workspace terminal; if it has stopped, review and release it in the Hub dashboard.') from None
            # Never render request bodies, tokens, response bodies or authorization URLs.
            raise CredentialError(f"Hub workspace request failed (HTTP {exc.code})") from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise CredentialError("Hub workspace connection failed", code="connection_failed") from None
        if result.get("success") is not True:
            raise CredentialError("Hub workspace request rejected")
        return result.get("data")

    def start(self):
        if self.id or self.token:
            raise CredentialError("A new run cannot inherit another run's capability")
        data = self.request("POST", "/api/workspace-runs", {"catalog": self.catalog, "workspace": self.workspace})
        self.id, self.token = data["id"], data["token"]

    @property
    def path(self):
        if not re.fullmatch(r"[a-zA-Z0-9-]+", self.id) or not self.token:
            raise CredentialError("No active Hub workspace run")
        return "/api/workspace-runs/" + self.id

    def credentials(self):
        data = self.request("GET", self.path)
        if data["catalog"] != self.catalog or data["workspace"] != self.workspace:
            raise CredentialError("Hub workspace identity mismatch")
        return data

    def renew(self):
        self.request("PUT", self.path, {})

    def report(self, *, synced=False, error=None):
        payload = {'syncComplete': synced}
        if error: payload['error'] = error
        self.request('PUT', self.path, payload)

    def finish(self, reason='completed'):
        self.request("DELETE", self.path, {'exitReason': reason})


def safe_file(name: str) -> bool:
    return (len(name) <= 240 and name.startswith("configs/") and
            all(re.fullmatch(r"[a-zA-Z0-9_.-]+", part) and part not in {".", ".."}
                for part in name.split('/')) and
            re.search(r"\.(json|env|yaml|yml|pem|p8|key|txt)$", name) is not None)


def write_private(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".workspace-tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def materialize(root: Path, credentials: dict):
    """Keep the original import contract, including task-local resource overrides."""
    values, files = credentials['values'], credentials['files']
    if not isinstance(values, dict) or not isinstance(files, dict):
        raise CredentialError("Invalid credential bundle")
    for name, content in files.items():
        if not safe_file(name) or not isinstance(content, str):
            raise CredentialError("Invalid credential file")
        destination = root / name
        if destination.is_symlink() or not destination.resolve().is_relative_to(root.resolve()):
            raise CredentialError("Credential destination escapes run snapshot")
        write_private(destination, content)
    # Calendar consumes the OAuth client document as well as user credentials.
    # Derive its identity from the same declared account fields, preserving public
    # client metadata when an older account already supplied that document.
    google = root/'configs/google_credentials.json'
    if google.is_file():
        oauth = json.loads(google.read_text())
        client_path = root/'configs/gcp-oauth.keys.json'
        client = json.loads(client_path.read_text()) if client_path.is_file() else {'web': {}}
        kind = 'installed' if 'installed' in client else 'web'
        details = client.setdefault(kind, {})
        details.update({key: oauth.get(key, '') for key in ('client_id', 'client_secret')})
        details.setdefault('auth_uri', 'https://accounts.google.com/o/oauth2/auth')
        details['token_uri'] = oauth.get('token_uri', 'https://oauth2.googleapis.com/token')
        details.setdefault('redirect_uris', ['http://localhost:3000/oauth2callback'])
        write_private(client_path, json.dumps(client))
    # Keep mutable Google authorization shared with this run's task containers.
    for name in ['configs/google_credentials.json', 'configs/credentials.json']:
        path = root/name
        if path.is_file():
            shared = root/'configs/.hub-oauth'/path.name
            write_private(shared, path.read_text())
            path.unlink()
            path.symlink_to(Path('.hub-oauth')/path.name)
    write_private(root/'configs/hub-account-values.json', json.dumps(values))
    write_private(root/'configs/hub-http-routes.json', json.dumps(credentials.get('http', [])))
    write_private(root/'configs/token_key_session.py', '''# Generated inside one private run snapshot; never copied into the source repository.
import json
from pathlib import Path
from configs.token_key_session_example import all_token_key_session
all_token_key_session.update(json.loads(Path(__file__).with_name("hub-account-values.json").read_text()))
for field in ("google_oauth2_credentials_path", "google_oauth2_token_path"):
    if Path("configs/.hub-oauth/google_credentials.json").exists():
        all_token_key_session[field] = "configs/.hub-oauth/google_credentials.json"

''')


def snapshot(source: Path, destination: Path):
    """Copy source and task inputs, linking only installed dependencies.

    Configs/deployment/task source are private copies because preprocessing writes
    them. Never bind two account runs to the original configs/.mcp-auth directory.
    """
    tracked = subprocess.check_output(['git', '-C', str(source), 'ls-files', '-z',
                                      '--cached', '--others', '--exclude-standard'])
    for raw in tracked.split(b'\0'):
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        if relative.parts[0] in {'node_modules', '.venv', 'local_servers', 'local_binary'}:
            continue
        original = source/relative
        if not original.is_file() or original.is_symlink():
            continue
        target = destination/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, target)
    # Task-local modules also contain resource bindings (page IDs, store paths,
    # kubeconfig paths). They are intentionally ignored by Git, but preprocess
    # and MCP startup must see the same private copy inside the run snapshot.
    for original in (source/'tasks').glob('*/*/token_key_session.py'):
        if (not original.is_file() or original.is_symlink() or
                not original.resolve().is_relative_to(source.resolve())):
            continue
        target = destination/original.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        write_private(target, original.read_text())
    # These ignored files are non-secret instance settings. Hub workspace files
    # can override them later; dropping them silently changes ports and cluster IDs.
    for relative in ('configs/ports_config.yaml', 'configs/port_changes.json'):
        original = source/relative
        if original.is_file() and not original.is_symlink():
            write_private(destination/relative, original.read_text())
    # This operator configuration is ignored by git but contains benchmark settings.
    global_config = source/'configs/global_configs.py'
    if global_config.is_file():
        shutil.copy2(global_config, destination/'configs/global_configs.py')
    elif (destination/'configs/global_configs_example.py').is_file():
        shutil.copy2(destination/'configs/global_configs_example.py', destination/'configs/global_configs.py')
    for name in ['node_modules', '.venv', 'local_servers', 'local_binary']:
        original = source/name
        if original.exists():
            (destination/name).symlink_to(original.resolve(), target_is_directory=True)


class OAuthSync:
    """Persist changes from existing OAuth clients; do not invent a second login flow.

    Existing Notion file locks serialize refresh/use across task containers. The
    run snapshot's auth directory is shared only by tasks in this same run.
    """
    def __init__(self, client: WorkspaceClient, root: Path, files: dict[str, str]):
        self.client, self.root = client, root
        self.hashes = {name: hashlib.sha256(content.encode()).hexdigest() for name, content in files.items()}

    def sync(self, final=False):
        auth = self.root/'configs/.mcp-auth'
        auth.mkdir(parents=True, exist_ok=True)
        with (auth/'notion_official_refresh.lock').open('a+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            candidates = list(auth.rglob('*'))
            candidates += [self.root/'configs/google_credentials.json', self.root/'configs/credentials.json']
            changes = []
            for path in candidates:
                if not path.is_file() or not path.resolve().is_relative_to(self.root.resolve()):
                    continue
                name = path.relative_to(self.root).as_posix()
                if not safe_file(name) or path.suffix not in {'.json', '.env'}:
                    continue
                try:
                    content = path.read_text()
                    if path.suffix == '.json':
                        json.loads(content)  # Do not persist a partial SDK write.
                except (ValueError, OSError, UnicodeError):
                    if final:
                        raise CredentialError("OAuth state is incomplete; retain the run directory") from None
                    continue
                digest = hashlib.sha256(content.encode()).hexdigest()
                if digest != self.hashes.get(name):
                    changes.append({'path': name, 'content': content, 'expectedHash': self.hashes.get(name)})
            if changes:
                self.client.request('PUT', self.client.path+'/files', {'updates': changes})
                for change in changes:
                    self.hashes[change['path']] = hashlib.sha256(change['content'].encode()).hexdigest()
            return True
