from pathlib import Path
import tempfile
import os
import unittest
from unittest.mock import patch

from scripts.containerized.agent_permissions import prepare_excel_log, stage_home_resources, stage_runtime_files


class RuntimeResourceTests(unittest.TestCase):
    def test_home_caches_keep_shared_oauth_link_and_custom_model_parameters(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); old = root / 'old'; home = root / 'agent'
            old.mkdir(); home.mkdir(); (old / '.calendar-mcp').mkdir()
            shared = root / 'shared-token.json'; shared.write_text('SHARED TOKEN')
            (old / '.calendar-mcp/credentials.json').symlink_to(shared)
            (old / '.calendar-mcp/gcp-oauth.keys.json').write_text('CLIENT METADATA')
            (old / '.gitconfig').write_text('[user]\nname = Example\n')
            parameters = root / 'model.json'; parameters.write_text('{"reasoning_effort":"max"}')
            with patch('scripts.containerized.agent_permissions.os.chown'), patch.dict('os.environ',
                    {'TOOLATHLON_MODEL_PARAMS_FILE': str(parameters)}):
                stage_home_resources(home, old)
                import os
                self.assertEqual(Path(os.environ['TOOLATHLON_MODEL_PARAMS_FILE']).read_text(), parameters.read_text())
            self.assertTrue((home / '.calendar-mcp/credentials.json').is_symlink())
            self.assertEqual((home / '.calendar-mcp/credentials.json').resolve(), shared)
            self.assertEqual((home / '.calendar-mcp/gcp-oauth.keys.json').read_text(), 'CLIENT METADATA')
            self.assertEqual((home / '.gitconfig').read_text(), (old / '.gitconfig').read_text())
            self.assertEqual(shared.read_text(), 'SHARED TOKEN')

    def test_stdio_explicitly_receives_agent_cache_and_pinned_browser_paths(self):
        from utils.mcp.tool_servers import MCPServerManager
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'memory.yaml').write_text('name: memory\ntype: stdio\nparams:\n  command: node\n  args: []\n  env:\n    MEMORY_FILE_PATH: "${agent_workspace}/memory.json"\n')
            with patch.dict('os.environ', {'TOOLATHLON_AGENT_PYTHON': '/agent/bin/python',
                'UV_CACHE_DIR': '/agent/cache', 'UV_NO_SYNC': '1', 'VIRTUAL_ENV': '/agent',
                'PLAYWRIGHT_BROWSERS_PATH': '/pinned/browsers'}):
                manager = MCPServerManager(agent_workspace=temporary, config_dir=temporary)
            env = manager.servers['memory'].params.env
            self.assertEqual(env['PLAYWRIGHT_BROWSERS_PATH'], '/pinned/browsers')
            self.assertEqual(env['UV_CACHE_DIR'], '/agent/cache')
            self.assertEqual(env['UV_NO_SYNC'], '1')
            self.assertEqual(env['MEMORY_FILE_PATH'], str(root / 'memory.json'))

    def test_only_needed_mcp_runtime_file_is_copied_without_changing_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, configs, home = root / 'tasks', root / 'configs', root / 'home'
            source.mkdir(); configs.mkdir(); home.mkdir()
            email = source / 'emails_config.json'
            email.write_text('{"accounts":[{"name":"test"}]}')
            answer = source / 'expected_results.json'; answer.write_text('PRIVATE ANSWER')
            (configs / 'emails.yaml').write_text('name: emails\nparams:\n  args: ["${token.emails_config_file}"]\n')
            (configs / 'unused.yaml').write_text('name: unused\nparams:\n  args: ["${token.unused_answer_file}"]\n')
            tokens = {'emails_config_file': str(email), 'unused_answer_file': str(answer), 'plain_id': 'abc'}
            bundle = {'resolved_task_config': {'needed_mcp_servers': ['emails'], 'local_token_key_session': tokens},
                      'eval_config': {'mcp': {'server_config_path': str(configs)}}}
            stage_runtime_files(bundle, home, source)
            copy = Path(tokens['emails_config_file'])
            self.assertEqual(copy.read_text(), email.read_text())
            self.assertNotEqual(copy, email)
            self.assertEqual(tokens['unused_answer_file'], str(answer))
            self.assertEqual(list((home / 'runtime').iterdir()), [home / 'runtime/emails_config_file'])
            self.assertEqual(email.read_text(), '{"accounts":[{"name":"test"}]}')

    def test_missing_declared_resource_is_reported_before_model_starts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); configs = root / 'configs'; configs.mkdir()
            (configs / 'emails.yaml').write_text('name: emails\nparams:\n  args: ["${token.emails_config_file}"]\n')
            bundle = {'resolved_task_config': {'needed_mcp_servers': ['emails'],
                       'local_token_key_session': {'emails_config_file': str(root / 'tasks/missing.json')}},
                      'eval_config': {'mcp': {'server_config_path': str(configs)}}}
            with self.assertRaisesRegex(RuntimeError, 'emails_config_file'):
                stage_runtime_files(bundle, root / 'home', root / 'tasks')


class LocalMCPRuntimeTests(unittest.TestCase):
    def test_excel_only_log_is_writable_and_old_private_content_is_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            code = root / 'server.py'; code.write_text('original MCP code')
            log = root / 'excel-mcp.log'; log.write_text('private preprocessing log')
            original_mode = root.stat().st_mode
            with patch('scripts.containerized.agent_permissions.os.fchown') as chown:
                prepare_excel_log(log)
            chown.assert_called_once()
            self.assertEqual(chown.call_args.args[1:], (65532, 65532))
            self.assertEqual(log.read_text(), '')
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)
            self.assertEqual(root.stat().st_mode, original_mode)
            self.assertEqual(code.read_text(), 'original MCP code')

    def test_excel_log_cannot_follow_a_symlink_or_truncate_a_hardlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = root / 'private'; private.write_text('private answer')
            log = root / 'excel-mcp.log'; log.symlink_to(private)
            with self.assertRaises(OSError):
                prepare_excel_log(log)
            log.unlink(); os.link(private, log)
            with self.assertRaises(RuntimeError):
                prepare_excel_log(log)
            self.assertEqual(private.read_text(), 'private answer')

    def test_snowflake_uses_normalized_installed_package_without_uvx_download(self):
        from utils.mcp.tool_servers import MCPServerManager
        pinned = '/root/.local/share/uv/tools/mcp-snowflake-server/bin/mcp_snowflake_server'
        for args in [['mcp_snowflake_server', '--help'],
                     ['--from', 'mcp_snowflake_server', 'mcp_snowflake_server', '--help']]:
            with self.subTest(args=args), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                import yaml
                (root / 'snowflake.yaml').write_text(yaml.safe_dump({
                    'name': 'snowflake', 'params': {'command': 'uvx', 'args': args}}))
                with patch.dict('os.environ', {'TOOLATHLON_AGENT_PYTHON': '/agent/bin/python'}), \
                     patch('pathlib.Path.is_file', lambda path: str(path) == pinned):
                    manager = MCPServerManager(agent_workspace=temporary, config_dir=temporary)
                self.assertEqual(manager.servers['snowflake'].params.command, pinned)
                self.assertEqual(manager.servers['snowflake'].params.args, ['--help'])
