"""Hub integration fixes preserve tool payloads and benchmark-owned resources."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.mcp.hub_instance import reconcile_mail_ports


def test_only_declared_local_mail_ports_change_in_run_copy(tmp_path):
    (tmp_path/'configs').mkdir()
    (tmp_path/'configs/ports_config.yaml').write_text('port_mappings:\n  1143: 2143\n  1587: 2587\n')
    directory = tmp_path/'tasks/finalpool/example/files'
    directory.mkdir(parents=True)
    original = {'server_config': {'imap_server': 'localhost', 'imap_port': 1143,
                                  'smtp_server': '127.0.0.1', 'smtp_port': 1587},
                'remote': {'imap_server': 'mail.example.org', 'imap_port': 1143},
                'question': {'number': 1143, 'expected': 'unchanged'}}
    path=directory/'email_config.json';path.write_text(json.dumps(original))
    assert reconcile_mail_ports(tmp_path) == 2
    actual=json.loads(path.read_text())
    assert actual['server_config']['imap_port'] == 2143
    assert actual['server_config']['smtp_port'] == 2587
    assert actual['remote'] == original['remote']
    assert actual['question'] == original['question']
    assert reconcile_mail_ports(tmp_path) == 0


@pytest.mark.asyncio
async def test_large_stdio_result_is_complete_without_upstream_sdk_changes():
    from datetime import timedelta
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from utils.mcp.hub_stdio import install_linear_stdio_reader
    install_linear_stdio_reader()
    code='''import sys,json
for line in sys.stdin:
    request=json.loads(line)
    if 'id' not in request:continue
    if request['method']=='initialize':
        result={'protocolVersion':'2024-11-05','capabilities':{},'serverInfo':{'name':'test','version':'1'}}
    else:
        result={'content':[{'type':'text','text':'数据\\n' * 2000000}],'isError':False}
    sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result},ensure_ascii=False)+'\\n')
    sys.stdout.flush()
'''
    params=StdioServerParameters(command=sys.executable,args=['-B','-c',code])
    async with stdio_client(params) as (rd,wr):
        async with ClientSession(rd,wr,read_timeout_seconds=timedelta(seconds=10)) as session:
            await session.initialize()
            result=await session.call_tool('original_name',{})
    expected=('数据\n'*2000000).encode()
    assert not result.isError
    assert hashlib.sha256(result.content[0].text.encode()).digest()==hashlib.sha256(expected).digest()


@pytest.mark.asyncio
@pytest.mark.parametrize('grandchild', [False, True])
async def test_cancel_drains_real_subprocess_pipes_before_loop_shutdown(tmp_path, grandchild):
    import run_parallel as runner
    log=tmp_path/'task.log'
    code='import time;print("ready",flush=True);time.sleep(60)'
    if grandchild:
        child='import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print("child-ready",flush=True);time.sleep(60)'
        code='import subprocess,sys,time;subprocess.Popen([sys.executable,"-u","-c",'+repr(child)+']);time.sleep(60)'
    marker='\nchild-ready\n' if grandchild else '\nready\n'
    task=asyncio.create_task(runner.run_command_async(
        'exec '+shlex.quote(sys.executable)+' -u -c '+shlex.quote(code),str(log)))
    for _ in range(200):
        if log.exists() and marker in log.read_text() and runner.active_processes:break
        await asyncio.sleep(.01)
    else:raise AssertionError('Diagnostic child did not start')
    process=next(iter(runner.active_processes))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert not runner.active_processes
    assert process.returncode is not None
    assert process.stdout.at_eof()


@pytest.mark.asyncio
@pytest.mark.parametrize('scored', [False,True])
async def test_cancel_updates_execution_state_without_changing_grade(tmp_path,monkeypatch,scored):
    import run_parallel as runner
    directory=tmp_path/'finalpool/example';directory.mkdir(parents=True)
    status={'preprocess':'done','running':'running','evaluation':None}
    (directory/'status.json').write_text(json.dumps(status))
    if scored:(directory/'eval_res.json').write_text('{"pass":true}')
    ready=asyncio.Event()
    async def command(*args,**kwargs):
        ready.set();await asyncio.sleep(60)
    monkeypatch.setattr(runner,'run_command_async',command)
    scheduler=runner.AsyncTaskScheduler([],1)
    monkeypatch.setattr(scheduler,'_archive_previous_results',lambda *args:None)
    task=asyncio.create_task(scheduler._execute_task('finalpool/example','full','model','unified','200',60,False,dump_path=str(tmp_path)))
    await ready.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    actual=json.loads((directory/'status.json').read_text())
    assert actual['running']==('running' if scored else 'cancelled')
    assert actual['evaluation'] is None
    if scored:assert (directory/'eval_res.json').read_text()=='{"pass":true}'


def test_arxiv_transport_preserves_query_body_and_response(monkeypatch):
    import requests
    from utils.mcp.relay_adapter import arxiv_mcp_compat as adapter
    from utils.mcp.relay_adapter.egress_relay_client import RelayResponse
    received=[]
    monkeypatch.setattr(adapter,'enabled',lambda:True)
    def direct(*args,**kwargs):
        raise AssertionError('arXiv must not bypass the configured Relay')
    monkeypatch.setattr(requests.Session,'request',direct)
    def fetch(method,url,**kwargs):
        received.append((method,url,kwargs))
        return RelayResponse(200,{'Content-Type':'application/atom+xml'},b'<feed>original</feed>',url)
    monkeypatch.setattr(adapter,'fetch',fetch)
    adapter.install_requests_relay()
    response=requests.Session().get('https://export.arxiv.org/api/query',params={'search_query':'ti:A B','start':2},headers={'User-Agent':'original-agent'})
    assert received[0][1].endswith('search_query=ti%3AA+B&start=2')
    assert received[0][2]['headers']['User-Agent']=='original-agent'
    assert response.content==b'<feed>original</feed>'
    assert response.status_code==200
    assert len(received)==1


@pytest.mark.asyncio
async def test_task_relay_is_fetched_before_launch_and_preserves_original_command(monkeypatch):
    import httpx
    from types import SimpleNamespace
    from utils.mcp.hub_transport import HubSettings, HubTaskServer
    from mcp.types import Tool
    settings=HubSettings('http://hub.test','group-key','toolathlon',frozenset(),'account-a','-')
    events=[]
    class Backend:
        params=SimpleNamespace(env={'PYTHONPATH':'/workspace','ORIGINAL':'unchanged'},command='original-command',args=['original-arg'])
        async def __aenter__(self):events.append('started');return self
        async def __aexit__(self,*args):pass
        async def list_tools(self):return [Tool(name='search_papers',inputSchema={'type':'object'})]
    class Bridge:
        run_id='task-id'
        async def bind(self,*args):return 'binding','callback-token','http://task.test/bridge'
        async def unbind(self,*args):pass
    async def dispatch(request):
        if request.url.path.endswith('/runtime'):
            events.append('runtime')
            return httpx.Response(200,json={'data':{'env':{'MCP_RELAY_URL':'http://relay.test','MCP_RELAY_TOKEN':'relay-token','UNRELATED':'must-not-propagate'}}})
        if request.method=='POST':
            return httpx.Response(200,json={'success':True,'data':{'id':'lease','token':'lease-token','path':'/gateway/v1/tasks/lease'}})
        return httpx.Response(200,json={'success':True})
    real_client=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:real_client(transport=httpx.MockTransport(dispatch),**kwargs))
    bridge=Bridge();bridge.settings=settings
    server=HubTaskServer('arxiv_local',Backend(),bridge,{'client_session_timeout_seconds':300})
    try:
        await server.connect()
        assert events==['runtime','started']
        assert server.backend.params.env['PYTHONPATH'].endswith('relay_adapter:/workspace')
        assert server.backend.params.env['MCP_RELAY_TOKEN']=='relay-token'
        assert 'UNRELATED' not in server.backend.params.env
        assert server.backend.params.command=='original-command'
        assert server.backend.params.args==['original-arg']
    finally:await server.cleanup()
