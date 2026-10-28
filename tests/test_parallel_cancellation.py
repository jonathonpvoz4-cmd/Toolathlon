"""Exercise the real entrypoint signal handler without running benchmark tasks."""
import ast
import asyncio
import os
from pathlib import Path
import signal

import pytest


@pytest.mark.parametrize('cancel', [False, True])
def test_parallel_entrypoint_waits_for_cleanup_and_preserves_cancel_code(cancel, monkeypatch):
    monkeypatch.delenv('TOOLATHLON_HUB_SNAPSHOT', raising=False)
    source=Path(__file__).parents[1]/'run_parallel.py'
    tree=ast.parse(source.read_text())
    entry=next(node for node in tree.body if isinstance(node,ast.AsyncFunctionDef) and node.name=='main_with_signal_handling')
    events=[]
    async def main():
        events.append('started')
        if cancel:
            asyncio.get_running_loop().call_soon(os.kill,os.getpid(),signal.SIGINT)
            await asyncio.sleep(30)
        events.append('completed')
    async def cleanup():
        events.append('cleanup_started')
        if cancel:
            os.kill(os.getpid(),signal.SIGINT)
        await asyncio.sleep(.05)
        events.append('cleanup_finished')
    ns={'asyncio':asyncio,'os':os,'signal':signal,'main':main,'async_cleanup_processes':cleanup}
    exec(compile(ast.Module(body=[entry],type_ignores=[]),str(source),'exec'),ns)
    assert asyncio.run(ns['main_with_signal_handling']()) == (130 if cancel else 0)
    assert events[-2:] == ['cleanup_started','cleanup_finished']
    assert ('completed' in events) is not cancel


def test_cancel_finishes_when_console_pipe_has_already_closed(tmp_path):
    import subprocess
    import sys
    import textwrap
    source=Path(__file__).parents[1]/'run_parallel.py'
    marker=tmp_path/'cleaned'
    code=textwrap.dedent('''
        import ast,asyncio,os,signal,sys
        from pathlib import Path
        source=Path(sys.argv[1]);marker=Path(sys.argv[2])
        tree=ast.parse(source.read_text())
        entry=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='main_with_signal_handling')
        async def main():
            asyncio.get_running_loop().call_later(.2,os.kill,os.getpid(),signal.SIGINT)
            await asyncio.sleep(30)
        async def cleanup():
            marker.write_text('done')
            print('cleanup completed',flush=True)
        ns={'asyncio':asyncio,'os':os,'signal':signal,'main':main,'async_cleanup_processes':cleanup}
        exec(compile(ast.Module(body=[entry],type_ignores=[]),str(source),'exec'),ns)
        sys.exit(asyncio.run(ns['main_with_signal_handling']()))
    ''')
    process=subprocess.Popen([sys.executable,'-B','-c',code,str(source),str(marker)],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,env={k:v for k,v in os.environ.items() if k!='TOOLATHLON_HUB_SNAPSHOT'})
    process.stdout.close()
    try:
        assert process.wait(timeout=5)==130
        assert marker.read_text()=='done'
    finally:
        if process.poll() is None: process.kill();process.wait()
        process.stderr.close()
