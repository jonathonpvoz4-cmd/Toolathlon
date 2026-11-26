"""Run the model phase without evaluator or container-manager write privileges."""
from __future__ import annotations

import ctypes
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

AGENT_UID = 65532
AGENT_GID = 65532


def stage_runtime_files(bundle: dict, home: Path, source_root: Path = Path('/workspace/tasks')) -> None:
    """Preserve only file resources explicitly referenced by this task's MCPs."""
    import yaml
    config = bundle['resolved_task_config']
    tokens = config.get('local_token_key_session') or {}
    needed = set(config.get('needed_mcp_servers', []))
    config_dir = Path(bundle['eval_config']['mcp']['server_config_path'])
    keys = set()
    for path in config_dir.glob('*.yaml'):
        server = yaml.safe_load(path.read_text()) or {}
        if server.get('name', path.stem) in needed:
            keys.update(re.findall(r'\$\{token\.([A-Za-z0-9_]+)\}', str(server.get('params', {}))))
    for key in sorted(keys):
        value = tokens.get(key)
        if not isinstance(value, str):
            continue
        path = Path(value).absolute()
        if not path.is_relative_to(source_root.absolute()):
            continue
        if not path.is_file():
            raise RuntimeError(f'Missing task MCP runtime file: {key}')
        destination = home / 'runtime' / key / path.name
        destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        destination.chmod(0o644)
        tokens[key] = str(destination)


def _own_tree(root: Path) -> None:
    for directory, names, files in os.walk(root, followlinks=False):
        for path in [Path(directory), *(Path(directory) / name for name in names + files)]:
            os.chown(path, AGENT_UID, AGENT_GID, follow_symlinks=False)


def stage_home_resources(home: Path, original_home: Path) -> None:
    """Retain the runtime caches prepared by run_single_containerized.sh."""
    for name in ['.gmail-mcp', '.calendar-mcp', '.mcp-auth']:
        source = original_home / name
        if source.is_dir() and not source.is_symlink():
            destination = home / name
            shutil.copytree(source, destination, symlinks=True)
            _own_tree(destination)
    source = original_home / '.gitconfig'
    if source.is_file() and not source.is_symlink():
        shutil.copyfile(source, home / '.gitconfig')
        os.chown(home / '.gitconfig', AGENT_UID, AGENT_GID)
    parameters = os.environ.get('TOOLATHLON_MODEL_PARAMS_FILE')
    if parameters and Path(parameters).is_file():
        destination = home / 'model-params.json'
        shutil.copyfile(parameters, destination)
        destination.chmod(0o644)
        os.environ['TOOLATHLON_MODEL_PARAMS_FILE'] = str(destination)


def prepare_excel_log(path: Path) -> None:
    """Grant the agent one empty runtime log, never writes to installed code."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError('Excel runtime log must be a single regular file')
        os.ftruncate(descriptor, 0)
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, AGENT_UID, AGENT_GID)
    finally:
        os.close(descriptor)


def restrict_agent(bundle: dict) -> None:
    """Called after consuming the private bundle, before any tools are started.

    This entry point is only for the root-owned phased task container. The
    trusted preprocessing and evaluation processes retain their original UID.
    """
    if os.geteuid() != 0:
        raise RuntimeError('The phased agent requires a root-owned task container')
    config = bundle['resolved_task_config']
    output = Path(config['task_root']).resolve(strict=True)
    workspace = Path(config['agent_workspace']).resolve(strict=True)
    if not output.is_relative_to('/workspace/dumps') or workspace != output / 'workspace':
        raise RuntimeError('Agent output paths must be under /workspace/dumps')
    if any(Path(p).is_symlink() for p in ['/workspace', '/workspace/dumps/workspace']):
        raise RuntimeError('Agent workspace must not be a symbolic link')

    # Model tools must not inherit the socket's group. Do not chmod a bind
    # mounted socket: in standalone mode it can belong to the real host daemon.
    sockets = [Path('/var/run/docker.sock'), Path('/run/podman/podman.sock'),
               *Path('/run/user').glob('*/podman/podman.sock')]
    for path in sockets:
        if path.exists():
            info = path.stat()
            if (info.st_uid == AGENT_UID or info.st_mode & stat.S_IWOTH
                    or (info.st_gid == AGENT_GID and info.st_mode & stat.S_IWGRP)):
                raise RuntimeError(f'Container-manager socket is accessible to the agent: {path}')

    home = Path('/tmp/toolathlon-agent')
    home.mkdir(mode=0o700, exist_ok=False)
    os.chown(home, AGENT_UID, AGENT_GID)
    from scripts.containerized.kubernetes_access import prepare_kubernetes_access
    prepare_kubernetes_access(bundle, home)
    stage_runtime_files(bundle, home)
    if (home / 'runtime').is_dir():
        _own_tree(home / 'runtime')
    stage_home_resources(home, Path.home())
    # Inputs have already been copied into workspace; the original task source
    # can also contain setup data and unlisted reference copies. The resolved
    # bundle means the prepared agent no longer needs to open that source tree.
    tasks = Path('/workspace/tasks')
    if tasks.is_dir():
        if tasks.is_symlink() or tasks.stat().st_uid != 0:
            raise RuntimeError('Task sources must be root-owned')
        tasks.chmod(0o700)
    _own_tree(workspace)
    os.chown(output, 0, AGENT_GID)
    output.chmod(0o1770)
    for path in output.iterdir():
        if path.is_file() and not path.is_symlink():
            # Preparatory stdout can contain generated answers. Keep existing
            # runtime logs root-only and sticky-directory protected.
            if path.name in {'status.json', 'traj_log.json'}:
                os.chown(path, AGENT_UID, AGENT_GID)
            else:
                os.chown(path, 0, 0)
                path.chmod(0o600)
    for name in ['configs/.mcp-auth', 'configs/.hub-oauth']:
        path = Path('/workspace') / name
        if path.is_dir():
            _own_tree(path)
    for path in Path('/workspace/configs').rglob('*'):
        if not path.is_symlink():
            path.chmod(path.stat().st_mode | (0o005 if path.is_dir() else stat.S_IROTH))

    # The pinned image's Python and installed MCP tools live below /root.
    # Permit traversal, not listing of /root or writes to its dependencies.
    os.chmod('/root', 0o711)
    # A writable package overlay lets model code install dependencies without
    # modifying the Python environment subsequently used by the evaluator.
    env_dir = home / 'venv'
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(env_dir)], check=True)
    site = next((env_dir / 'lib').glob('python*/site-packages'))
    base_site = next(Path('/workspace/.venv/lib').glob('python*/site-packages'))
    (site / 'toolathlon-runtime.pth').write_text(str(base_site) + '\n')
    if 'excel' in config.get('needed_mcp_servers', []):
        prepare_excel_log(base_site.parent / 'excel-mcp.log')
    _own_tree(env_dir)
    os.environ.update(HOME=str(home), UV_CACHE_DIR=str(home / 'uv-cache'),
                      UV_NO_SYNC='1', VIRTUAL_ENV=str(env_dir),
                      TOOLATHLON_AGENT_PYTHON=str(env_dir / 'bin/python'),
                      PATH=str(env_dir / 'bin') + os.pathsep + os.environ['PATH'])
    if Path('/root/.cache/ms-playwright').is_dir():
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = '/root/.cache/ms-playwright'
    # Suppress setuid/file-capability elevation by any model-created child.
    if ctypes.CDLL(None, use_errno=True).prctl(38, 1, 0, 0, 0):
        raise OSError(ctypes.get_errno(), 'Cannot enable no_new_privs')
    os.setgroups([])
    os.setresgid(AGENT_GID, AGENT_GID, AGENT_GID)
    os.setresuid(AGENT_UID, AGENT_UID, AGENT_UID)


def seal_output(output: Path) -> None:
    """Protect grading checkpoints while leaving task background services alive."""
    if os.geteuid() != 0 or output.is_symlink() or not output.resolve().is_relative_to('/workspace/dumps'):
        raise RuntimeError('Cannot seal untrusted output directory')
    os.chown(output, 0, 0)
    output.chmod(0o755)
    workspace = output / 'workspace'
    if workspace.is_symlink():
        logging.warning('answer_material_blocked phase=grading artifact=workspace reason=symbolic-link')
        workspace.unlink()
        workspace.mkdir()
    if workspace.is_dir():
        protected = Path('/workspace/tasks')
        for directory, names, files in os.walk(workspace, followlinks=False):
            for name in names + files:
                path = Path(directory) / name
                if not path.is_symlink():
                    continue
                try:
                    target = path.resolve()
                except (OSError, RuntimeError):
                    target = protected
                if target.is_relative_to(protected) or protected.is_relative_to(target):
                    # Do not let the root grader follow a model-created alias
                    # into its own answers. Normal workspace links are retained;
                    # the native evaluator judges the remaining actual outputs.
                    logging.warning('answer_material_blocked phase=grading artifact=%s reason=private-source-link',
                                    path.relative_to(workspace))
                    path.unlink()
    for name in ['traj_log.json', 'status.json']:
        path = output / name
        if not path.exists() and not path.is_symlink():
            continue
        if not stat.S_ISREG(path.lstat().st_mode):
            raise RuntimeError(f'Invalid grading checkpoint: {name}')
        # Replace the inode too: a model-created background process may retain
        # an already open writable descriptor to the old checkpoint.
        with tempfile.NamedTemporaryFile(dir=output, delete=False) as temporary:
            temporary.write(path.read_bytes())
            replacement = Path(temporary.name)
        replacement.chmod(0o644)
        replacement.replace(path)


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != 'seal':
        raise SystemExit('Usage: agent_permissions seal OUTPUT')
    seal_output(Path(sys.argv[2]))
