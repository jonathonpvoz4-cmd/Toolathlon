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


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_first', [False, True])
async def test_concurrent_cleanup_has_one_pipe_reader(cancel_first):
    from types import SimpleNamespace
    source = Path(__file__).parents[1]/'run_parallel.py'
    nodes = [node for node in ast.parse(source.read_text()).body
             if isinstance(node, ast.AsyncFunctionDef)
             and node.name in {'cleanup_single_process', '_cleanup_process_group'}]
    started = asyncio.Event()
    calls = []
    class Process:
        pid = 123
        returncode = None
        reads = 0
        @property
        def stdout(self):
            return self
        async def read(self, size):
            self.reads += 1
            started.set()
            await asyncio.sleep(.05)
            return b''
        async def wait(self):
            self.returncode = 0
            return 0
    process = Process()
    ns = {'asyncio': asyncio, 'os': SimpleNamespace(killpg=lambda *args: calls.append(args)), 'signal': signal}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), ns)
    first = asyncio.create_task(ns['cleanup_single_process'](process))
    await started.wait()
    second = asyncio.create_task(ns['cleanup_single_process'](process))
    if cancel_first:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    else:
        await first
    await second
    assert process.reads == 1
    assert calls == [(123, signal.SIGTERM)]


def test_platform_intermediate_process_waits_for_child_cleanup_before_exiting(tmp_path):
    import importlib.util
    import subprocess
    import sys

    root = Path(__file__).parents[1]
    child = tmp_path / 'child.py'
    marker = tmp_path / 'cleaned'
    child.write_text('''import signal,time,sys
from pathlib import Path
def stop(signum, frame):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(.3)
    Path(sys.argv[1]).write_text('cleanup finished')
    raise SystemExit(143)
signal.signal(signal.SIGTERM, stop)
Path(sys.argv[1]+'.ready').touch()
print('ready', flush=True)
while True: time.sleep(.1)
''')
    parent = tmp_path / 'parent.py'
    parent.write_text('''import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from scripts.platform import single_run
single_run.ROOT=Path(sys.argv[2])
raise SystemExit(single_run.run_step([sys.executable,sys.argv[3],sys.argv[4]],Path(sys.argv[2])/'step.log',os.environ.copy()))
''')
    spec = importlib.util.spec_from_file_location('wrapper_stop', root/'scripts/mcphub/run_with_workspace.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    process = subprocess.Popen([sys.executable, str(parent), str(root), str(tmp_path), str(child), str(marker)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        # Bound readiness and cancellation together, including subprocess pipes.
        import time
        deadline = time.monotonic() + 5
        while not Path(str(marker)+'.ready').exists():
            assert time.monotonic() < deadline, 'child did not start'
            time.sleep(.01)
        module.terminate(process)
        assert process.returncode == 143
        assert marker.read_text() == 'cleanup finished'
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        process.stdout.close()
        process.stderr.close()


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
