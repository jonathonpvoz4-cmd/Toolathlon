"""Canvas preprocessing must inject the evidence required by its evaluator."""
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('declared_mapping', [False, True])
@pytest.mark.parametrize('imap_port,smtp_port', [(2143, 3525), (22143, 23525)])
def test_clean_run_uses_deployed_mail_ports(tmp_path, monkeypatch, declared_mapping, imap_port, smtp_port):
    monkeypatch.syspath_prepend(str(ROOT))
    from utils.mcp.hub_instance import reconcile_mail_ports

    deployment = tmp_path/'deployment/poste/scripts/setup.sh'
    deployment.parent.mkdir(parents=True)
    source = (ROOT/'deployment/poste/scripts/setup.sh').read_text()
    deployment.write_text(source.replace('IMAP_PORT=2143', f'IMAP_PORT={imap_port}')
                         .replace('SMTP_PORT=3525', f'SMTP_PORT={smtp_port}'))
    if declared_mapping:
        config = tmp_path/'configs/ports_config.yaml'
        config.parent.mkdir()
        config.write_text('port_mappings:\n  1143: 2143\n')
    path = tmp_path/'tasks/finalpool/canvas-arrange-exam/files/email_config.json'
    path.parent.mkdir(parents=True)
    source = json.loads((ROOT/path.relative_to(tmp_path)).read_text())['server_config']
    path.write_text(json.dumps({'server_config': source,
                               'remote': {'imap_server': 'mail.example.org', 'imap_port': 1143}}))
    assert reconcile_mail_ports(tmp_path) == 2
    actual = json.loads(path.read_text())
    assert actual['server_config']['imap_port'] == imap_port
    assert actual['server_config']['smtp_port'] == smtp_port
    assert actual['remote']['imap_port'] == 1143
    assert reconcile_mail_ports(tmp_path) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('injected', [False, True])
async def test_email_injection_is_required_for_preprocess_success(monkeypatch, injected):
    calls = []

    async def setup(**kwargs):
        calls.append(kwargs)

    setup_module = ModuleType('setup_courses_with_mcp')
    setup_module.main = setup
    mail_module = ModuleType('send_exam_notification_smtp')
    mail_module.inject_exam_emails_from_config = lambda *args, **kwargs: injected
    monkeypatch.setitem(sys.modules, setup_module.__name__, setup_module)
    monkeypatch.setitem(sys.modules, mail_module.__name__, mail_module)
    monkeypatch.setattr(sys, 'path', sys.path.copy())
    spec = importlib.util.spec_from_file_location(
        'canvas_preprocess', ROOT/'tasks/finalpool/canvas-arrange-exam/preprocess/main.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if injected:
        await module.main(agent_workspace='/unused')
    else:
        with pytest.raises(SystemExit) as error:
            await module.main(agent_workspace='/unused')
        assert error.value.code == 1
    assert len(calls) == 2
