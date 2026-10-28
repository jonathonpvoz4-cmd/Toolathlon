"""Real namespace package + local Hub HTTP, without replacing configs in sys.modules."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import types

import pytest


def test_real_namespace_package_routes_notion_and_google_http(tmp_path):
    config=tmp_path/'configs';config.mkdir()
    (config/'hub-account-values.json').write_text(json.dumps({'notion_integration_key':'source-fixture'}))
    (config/'hub-http-routes.json').write_text(json.dumps([
        {'server':'google_sheet','id':'sheets','url':'https://sheets.googleapis.com/'}]))
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_GET(self):
            requests.append((self.path,dict(self.headers)))
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
            self.wfile.write(b'{"ok":true}')
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    code='''
import configs
assert configs.__file__ is None
from utils.mcp.workspace_http import notion_requests
from utils.mcp.hub_sdk_http import http_request
assert notion_requests.get('https://api.notion.com/v1/pages/task',headers={'Authorization':'Bearer source-fixture'}).json()['ok']
assert http_request('GET','https://sheets.googleapis.com/v4/spreadsheets/task',headers={'Authorization':'Bearer temporary-google'}).json()['ok']
'''
    root=Path(__file__).parents[1]
    env={**os.environ,'PYTHONPATH':str(root),'MCP_TOOL_TRANSPORT':'hub',
        'MCPHUB_URL':f'http://127.0.0.1:{server.server_port}','MCPHUB_TOKEN':'hub-fixture',
        'MCPHUB_RUN_ID':'run-fixture','MCPHUB_RUN_TOKEN':'run-capability-fixture',
        'MCPHUB_CATALOG':'toolathlon','MCPHUB_TARGET':'account-a'}
    try:
        result=subprocess.run([sys.executable,'-B','-c',code],cwd=tmp_path,env=env,capture_output=True,text=True,timeout=15)
        assert result.returncode==0,result.stderr
        assert len(requests)==2
        assert requests[0][0].endswith('/http/notion/prepare/v1/pages/task')
        assert requests[1][0].endswith('/http/google_sheet/sheets/v4/spreadsheets/task')
        assert all(headers['Authorization']=='Bearer hub-fixture' for _,headers in requests)
        assert all('source-fixture' not in str(headers) for _,headers in requests)
    finally:
        server.shutdown();server.server_close();thread.join()


def test_multiple_account_config_roots_fail_closed(tmp_path,monkeypatch):
    from utils.mcp.run_config import run_config_file
    from utils.mcp.workspace_credentials import CredentialError
    roots=[tmp_path/'a',tmp_path/'b']
    for root in roots:
        root.mkdir();(root/'hub-account-values.json').write_text('{}')
    monkeypatch.setitem(sys.modules,'configs',types.SimpleNamespace(__file__=None,__path__=[str(p) for p in roots]))
    with pytest.raises(CredentialError,match='ambiguous'):
        run_config_file('hub-account-values.json')
