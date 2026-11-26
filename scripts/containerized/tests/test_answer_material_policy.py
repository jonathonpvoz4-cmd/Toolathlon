import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from utils.answer_material_policy import (
    DENIED, filtered_output, protected_source, protect_local_tools,
)


class AnswerSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_license_task_filters_foreign_copies_and_keeps_its_prepared_resources(self):
        from utils.answer_material_policy import license_targets_from_state, license_task_targets

        state = {
            'github_repo': 'o1zys777/Annoy-DataSync',
            'hf_datasets': {
                'reasoning': 'https://huggingface.co/datasets/ll3213/Annoy-PyEdu-Rs',
                'raw': 'https://huggingface.co/datasets/ll3213/Annoy-PyEdu-Rs-Raw',
            },
        }
        foreign = 'new-eval-account/Annoy-PyEdu-Rs-Raw'
        self.assertFalse(protected_source({'repo_id': foreign}))
        token = license_task_targets.set(license_targets_from_state(state))
        try:
            self.assertTrue(protected_source({'owner': 'new-eval-account', 'repo': 'Annoy-DataSync'}))
            self.assertTrue(protected_source({'repo_id': foreign}))
            self.assertTrue(protected_source(f'https://huggingface.co/datasets/{foreign}/commit/abc'))
            self.assertFalse(protected_source({'owner': 'o1zys777', 'repo': 'Annoy-DataSync', 'issue_number': 1}))
            self.assertFalse(protected_source({'repo_ids': list(state['hf_datasets'].values())}))
            own = {'id': 'll3213/Annoy-PyEdu-Rs-Raw', 'description': 'This run’s dataset'}
            upstream = {'id': 'HuggingFaceTB/smollm-corpus', 'description': 'Source data license'}
            mixed = [own, {'id': foreign, 'description': 'A previous evaluation answer'}, upstream]
            self.assertEqual(json.loads(await filtered_output(json.dumps(mixed), 'fetch')), [own, upstream])
        finally:
            license_task_targets.reset(token)
        self.assertFalse(protected_source({'repo_id': foreign}))

    def test_shared_database_query_history_is_protected(self):
        from utils.answer_material_policy import request_denied
        for query in [
            'SELECT query FROM `region-us.INFORMATION_SCHEMA.JOBS_BY_PROJECT`',
            'select * from `project.region-us.INFORMATION_SCHEMA.JOBS`',
            'SELECT * FROM INFORMATION_SCHEMA./* inspection */JOBS_BY_USER',
            'SELECT * FROM `region-us`.`INFORMATION_SCHEMA`.`JOBS_TIMELINE_BY_PROJECT`',
            'SELECT * FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY())',
            'SELECT query_text FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY',
        ]:
            self.assertTrue(request_denied({'query': query}, 'google_cloud_bigquery_run_query'), query)
        for code in [
            "from google.cloud import bigquery; client = bigquery.Client(); client.list_jobs()",
            "build('bigquery', 'v2').jobs().list(projectId=p).execute()",
            "client.query('SELECT * FROM `region-us.INFORMATION_' + 'SCHEMA.JOBS_BY_PROJECT`')",
        ]:
            self.assertTrue(request_denied({'code': code}, 'local_python_execute'), code)

    def test_normal_database_queries_and_schema_metadata_remain_available(self):
        from utils.answer_material_policy import request_denied
        for query in [
            'SELECT AVG(score) FROM academic_warning.scores_2501',
            'SELECT * FROM academic_warning.INFORMATION_SCHEMA.TABLES',
            'SELECT * FROM academic_warning.INFORMATION_SCHEMA.COLUMNS',
            'SELECT * FROM company.jobs',
            'SELECT * FROM company.query_history',
            'SELECT * FROM information_schema.tables',
            "from google.cloud import bigquery; bigquery.Client().query('SELECT 1').result()",
            "build('bigquery', 'v2').jobs().insert(projectId=p, body=query).execute()",
        ]:
            self.assertFalse(request_denied({'query': query}, 'query'), query)

    async def test_gcp_resource_paths_pass_through_without_answer_filtering(self):
        from mcp.types import TextContent
        from utils.answer_material_policy import request_denied
        from utils.openai_agents_monkey_patch.custom_mcp_util import MCPUtil
        requests = [
            ('logging_write_log', {'log_name': 'projects/toolathlon-eval-1790756214/logs/exam_log',
                                   'message': 'student warning', 'severity': 'WARNING'}),
            ('logging_read_logs', {'log_filter': 'logName="projects/toolathlon-eval-1790756214/logs/exam_log"'}),
            ('logging_create_log_bucket', {'bucket_id': 'projects/toolathlon-eval-1790756214/locations/global/buckets/exam_log_bucket'}),
        ]
        for name, arguments in requests:
            with self.subTest(tool=name):
                self.assertFalse(request_denied(arguments, 'google_cloud_' + name))
                response = json.dumps({'resource': arguments, 'success': True})
                server = SimpleNamespace(name='google_cloud', call_tool=AsyncMock(
                    return_value=SimpleNamespace(content=[TextContent(type='text', text=response)])))
                tool = SimpleNamespace(name=name, inputSchema={'type': 'object'})
                result = await MCPUtil.invoke_mcp_tool(server, tool, None, json.dumps(arguments))
                server.call_tool.assert_awaited_once()
                self.assertIn('student warning' if name == 'logging_write_log' else 'toolathlon-eval-1790756214', result)
                self.assertNotIn(DENIED, result)
                self.assertNotIn('Benchmark answer sources were filtered', result)

    async def test_history_query_is_denied_before_mcp_and_local_execution(self):
        from utils.openai_agents_monkey_patch.custom_mcp_util import MCPUtil
        from agents.tool import FunctionTool
        query = 'SELECT query FROM `region-us.INFORMATION_SCHEMA.JOBS_BY_PROJECT`'
        server = SimpleNamespace(name='google_cloud', call_tool=AsyncMock())
        tool = SimpleNamespace(name='bigquery_run_query', inputSchema={'type': 'object'})
        self.assertEqual(await MCPUtil.invoke_mcp_tool(server, tool, None,
            json.dumps({'query': query})), DENIED)
        server.call_tool.assert_not_awaited()
        callback = AsyncMock()
        local = FunctionTool(name='python_execute', description='test',
            params_json_schema={'type': 'object', 'properties': {}}, on_invoke_tool=callback)
        self.assertEqual(await protect_local_tools([local])[0].on_invoke_tool(None,
            json.dumps({'code': f'client.query({query!r})'})), DENIED)
        callback.assert_not_awaited()

    def test_known_repository_variants_and_split_github_arguments(self):
        for source in [
            'https://raw.githubusercontent.com/hkust-nlp/Toolathlon/main/tasks/demo/evaluation/main.py',
            'https://github.com/fork/Toolathlon',
            'https://github.com/hkust-nlp/Toolathlon.git',
            'git clone hkust-nlp/Toolathlon.git',
            'https://huggingface.co/datasets/hkust-nlp/Toolathlon-Trajectories/resolve/main/data.parquet',
            'https://huggingface.co/api/datasets/hkust-nlp/Toolathlon-Trajectories',
            'hkust-nlp%252FToolathlon',
            'https://ghproxy.example/https://github.com/hkust-nlp/Toolathlon/main/tasks/demo',
            {'owner': 'hkust-nlp', 'repo': 'Toolathlon'},
        ]:
            self.assertTrue(protected_source(source), source)
        for source in ['arxiv research Toolathlon', 'org/normal-project',
                       'https://api.github.com/repos/toolathlon/BenchTasksCollv3/git/trees/main',
                       'https://github.com/toolathlon/BenchTasksCollv3',
                       {'owner': 'toolathlon', 'repo': 'BenchTasksCollv3'},
                       '/benchmark/toolathlon/task/demo']:
            self.assertFalse(protected_source(source), source)

    def test_known_repository_names_and_completed_license_copies(self):
        for value in [
            {'owner': 'WaitHZ', 'repo': 'toolathlon-traj-website', 'path': 'trajs/demo.json'},
            'https://huggingface.co/datasets/other/Annoy-PyEdu-Rs-Raw-with-license/raw/main/README.md',
            'https://huggingface.co/datasets/toolevalxm/Annoy-PyEdu-Rs/raw/main/README.md',
        ]:
            self.assertTrue(protected_source(value), value)
        for value in [
            'https://raw.githubusercontent.com/org/my-toolathlon-copy/main/readme.json',
            'https://github.com/org/Toolathlon-tools',
            'https://huggingface.co/datasets/org/my-toolathlon-data',
            'https://github.com/toolathlon/Annoy-DataSync/blob/main/README.md',
            'https://api.github.com/repos/toolathlon/llm-adaptive-learning/contents/README.md',
            'https://github.com/toolathlon/BenchTasksCollv3',
            'https://huggingface.co/datasets/myaccount/Annoy-PyEdu-Rs-Raw/raw/main/README.md',
        ]:
            self.assertFalse(protected_source(value), value)

    async def test_confirmed_github_answer_sources_are_blocked(self):
        from utils.answer_material_policy import request_denied
        identities = [('HYZ17', 'Toolathlon-Official'), ('hkustnlp-cmyk', 'Toolathlon-mvp'),
                      ('toolathlon-verified', 'LUFFY')]
        for owner, repo in identities:
            for source in [
                f'https://github.com/{owner}/{repo}/blob/main/evaluation/main.py',
                f'https://raw.githubusercontent.com/{owner}/{repo}/main/evaluation/main.py',
                f'https://api.github.com/repos/{owner}/{repo}/contents/evaluation/main.py',
                f'git clone https://github.com/{owner}/{repo}.git',
                f'git clone git@github.com:{owner}/{repo}.git',
                f'git clone ssh://git@github.com/{owner}/{repo}.git',
                f'git clone git://github.com/{owner}/{repo}.git',
                {'owner': owner, 'repo': repo, 'path': 'evaluation/main.py'},
            ]:
                with self.subTest(source=source):
                    self.assertTrue(request_denied(source, 'get_file_contents'))
            result = await filtered_output(json.dumps({'results': [
                {'url': f'https://github.com/{owner}/{repo}', 'snippet': 'grading answer'},
                {'url': 'https://cloud.google.com/logging', 'snippet': 'business documentation'},
            ]}), 'search')
            self.assertNotIn('grading answer', result)
            self.assertIn('business documentation', result)

    def test_precise_blacklist_preserves_business_paths_and_unlisted_repositories(self):
        for source in [
            'projects/toolathlon-eval-1790756214/logs/exam_log',
            'https://logging.googleapis.com/v2/projects/toolathlon-eval-1790756214/logs/exam_log',
            'gs://toolathlon-results/task-output.csv',
            'https://storage.googleapis.com/toolathlon-results/task-output.csv',
            'https://example.com/projects/toolathlon-eval-1790756214',
            '/workspace/dumps/workspace/toolathlon-results/output.json',
            'https://github.com/Toolathlon-Archive/LUFFY',
            'https://github.com/toolathlon-verified/another-business-repo',
            'https://github.com/another-owner/Toolathlon-mvp',
            {'owner': 'other-owner', 'repo': 'myTOOLATHLONcopy'},
            'https://github.com/Toolathlon-Archive/SimpleShopping',
            'https://github.com/toolathlon/BenchTasksCollv3',
            'https://api.github.com/repos/o1zys777/Annoy-DataSync/issues/1',
            'https://github.com/org/normal-repo/blob/main/toolathlon-notes.md',
            {'owner': 'toolathlon', 'repo': 'Annoy-DataSync'},
            'https://huggingface.co/datasets/org/my-toolathlon-data',
        ]:
            with self.subTest(source=source):
                self.assertFalse(protected_source(source))

    def test_python_variable_url_is_resolved_without_executing_code(self):
        from utils.answer_material_policy import request_denied
        for code in [
            'name = "Tool" + "athlon"\nbase = f"https://api.github.com/repos/hkust-nlp/{name}"\nget(base)',
            'repo = "toolathlon-traj-website"\nurl = f"https://raw.githubusercontent.com/WaitHZ/{repo}/main/trajs/x.json"\nget(url)',
            'repo = "Annoy-PyEdu-Rs-Raw-with-license"\nurl = f"https://huggingface.co/datasets/other/{repo}/raw/main/README.md"\nget(url)',
            'owners = ["toolevalxm", "ll3213"]\nfor owner in owners:\n for ds in ["Annoy-PyEdu-Rs-Raw", "Annoy-PyEdu-Rs"]:\n  get(f"https://huggingface.co/datasets/{owner}/{ds}/raw/main/README.md")',
        ]:
            self.assertTrue(request_denied({'code': code}, 'local_python_execute'), code)
        self.assertFalse(request_denied({'code':
            'org = "toolathlon"\nrepo = "Annoy-DataSync"\nget(f"https://api.github.com/repos/{org}/{repo}")'},
            'local_python_execute'))

    async def test_mixed_search_keeps_clean_hit_without_orphan_answer(self):
        value = json.dumps({'results': [
            {'url': 'https://github.com/hkust-nlp/Toolathlon', 'snippet': 'secret answer'},
            {'url': 'https://arxiv.org/abs/123', 'snippet': 'normal evidence'},
        ]})
        safe = await filtered_output(value, 'search')
        self.assertIn('normal evidence', safe)
        self.assertNotIn('secret answer', safe)
        self.assertEqual(json.loads(safe), {'results': [{'url': 'https://arxiv.org/abs/123', 'snippet': 'normal evidence'}]})
        self.assertEqual(await filtered_output('hkust-nlp/Toolathlon answer', 'fetch'), DENIED)

    async def test_request_blocked_before_local_callback(self):
        from agents.tool import FunctionTool
        callback = AsyncMock(return_value='must not execute')
        tool = FunctionTool(name='python_execute', description='test',
                            params_json_schema={'type': 'object', 'properties': {}}, on_invoke_tool=callback)
        wrapped = protect_local_tools([tool])[0]
        self.assertEqual(await wrapped.on_invoke_tool(None, '{"code":"fetch hkust-nlp/Toolathlon"}'), DENIED)
        callback.assert_not_awaited()
        self.assertEqual(await wrapped.on_invoke_tool(None, '{}'), 'must not execute')
        callback.assert_awaited_once()
        self.assertIs(tool.on_invoke_tool, callback)

    async def test_large_clean_output_unchanged(self):
        value = 'normal tool output ' * 20000
        self.assertEqual(await filtered_output(value, 'query'), value)

    async def test_mcp_request_is_blocked_before_backend_call(self):
        from utils.openai_agents_monkey_patch.custom_mcp_util import MCPUtil
        server = SimpleNamespace(name='github', call_tool=AsyncMock())
        tool = SimpleNamespace(name='get_file_contents', inputSchema={'type': 'object'})
        result = await MCPUtil.invoke_mcp_tool(server, tool, None,
            json.dumps({'owner': 'hkust-nlp', 'repo': 'Toolathlon', 'path': 'tasks/demo/evaluation/main.py'}))
        self.assertEqual(result, DENIED)
        server.call_tool.assert_not_awaited()

    async def test_structured_mcp_content_drops_identified_whole_block(self):
        value = json.dumps({'type': 'text', 'text': json.dumps([
            {'url': 'https://github.com/hkust-nlp/Toolathlon', 'text': 'answer'},
            {'url': 'https://example.com', 'text': 'evidence'},
        ])})
        result = await filtered_output(value, 'search')
        self.assertIn('evidence', result)
        self.assertNotIn('"answer"', result)

    def test_observed_audit_trajectory_forks_and_dataset_answers_are_blocked(self):
        for value in [
            'https://data.autobenchaudit.com/benchmarks/toolathlon/configs/demo.json',
            'https://autobenchaudit.com/benchmark/toolathlon/task/hk-top-conf',
            'https://toolathlon-traj.xyz/api/trajectory?id=deepseek_demo.json',
            'https://toolathlon.xyz/docs/tasks/campus/190',
            'https://github.com/eigent-ai/toolathlon_gym',
            'https://github.com/EnvCommons/toolathlon-gym',
            'https://raw.githubusercontent.com/WaitHZ/toolathlon-website/main/docs/tasks/aca/3/gpt-5.json',
            'https://api.github.com/repositories/1067870538/contents/docs/tasks/aca/3/gpt-5.json',
            'https://huggingface.co/datasets/hkust-nlp/Toolathlon-Verified_Trajectories',
            'https://huggingface.co/datasets/chengze79/Annoy-PyEdu-Rs-Raw-license',
            {'owner': 'WaitHZ', 'repo': 'toolathlon-website', 'path': '/docs/tasks'},
        ]:
            self.assertTrue(protected_source(value), value)
        for value in [
            'https://github.com/toolathlon/Annoy-DataSync/blob/main/README.md',
            'https://github.com/toolathlon/BenchTasksCollv3',
            'https://github.com/hkust-nlp/simpleRL-reason',
            'https://huggingface.co/datasets/hkust-nlp/CodeIO-PyEdu-Reasoning-Raw',
            'https://huggingface.co/datasets/my-account/Annoy-PyEdu-Rs-Raw',
            'https://arxiv.org/abs/2412.17256',
            'https://docs.google.com/forms/d/e/normal/formResponse',
            'https://not-autobenchaudit.com/example',
        ]:
            self.assertFalse(protected_source(value), value)

    async def test_encoded_or_split_answer_url_is_denied_before_execution(self):
        import base64
        from utils.answer_material_policy import request_denied
        url = 'https://data.autobenchaudit.com/benchmarks/toolathlon/configs/llm-training-dataset.json'
        encoded = base64.b64encode(url.encode()).decode()
        for code in [
            f"const u = atob('{encoded}'); await fetch(u); return 'saved';",
            f"url = base64.b64decode('{encoded}'); download(url); print('saved length 4094')",
            "fetch('https://data.auto' + 'benchaudit.com/benchmarks/' + 'toolathlon/configs/demo.json')",
            "url='https://toolathlon-' + 'traj.xyz/api/trajectory'; get(url)",
            r"fetch('https://data.\u0061utobenchaudit.com/index.json')",
        ]:
            self.assertTrue(request_denied({'code': code}, 'python_execute'), code)
        callback = AsyncMock(return_value='saved length 4094')
        from agents.tool import FunctionTool
        tool = FunctionTool(name='python_execute', description='test',
            params_json_schema={'type': 'object', 'properties': {}}, on_invoke_tool=callback)
        wrapped = protect_local_tools([tool])[0]
        self.assertEqual(await wrapped.on_invoke_tool(None, json.dumps({'code': f"atob('{encoded}')"})), DENIED)
        callback.assert_not_awaited()

    async def test_allowed_issue_html_keeps_body_and_removes_answer_link_block(self):
        value = json.dumps({'type': 'text', 'text': '''<html><head><title>Issue</title></head><body>
<article><p>Could you provide license information for this dataset?</p>
<p>Benchmark solution: secret answer <a href="https://github.com/hkust-nlp/Toolathlon">source</a></p>
<p>Normal maintainer reply remains available.</p></article></body></html>'''})
        result = await filtered_output(value, 'fetch_html', {'url': 'https://github.com/o1zys777/Annoy-DataSync/issues/1'})
        self.assertIn('Could you provide license information', result)
        self.assertIn('Normal maintainer reply', result)
        self.assertNotIn('secret answer', result)
        self.assertNotIn('Benchmark answer sources were filtered', result)
        # Without a known allowed requested document, do not sanitize an
        # unidentified answer page into apparently normal evidence.
        self.assertNotIn('Normal maintainer reply', await filtered_output(value, 'browser_evaluate'))

    async def test_mcp_text_array_preserves_unrelated_blocks_and_allowed_document(self):
        result = await filtered_output(json.dumps([
            {'type': 'text', 'text': 'Normal business data'},
            {'type': 'text', 'text': 'https://toolathlon-traj.xyz/api/trajectory secret answer'},
        ]), 'query')
        self.assertIn('Normal business data', result)
        self.assertNotIn('secret answer', result)

    async def test_answer_page_is_denied_even_if_html_also_contains_normal_data(self):
        from utils.answer_material_policy import request_denied
        self.assertTrue(request_denied({'url': 'https://autobenchaudit.com/benchmark/toolathlon/task/demo'}, 'fetch'))
        self.assertEqual(await filtered_output(
            '<html><p>https://autobenchaudit.com/benchmark/toolathlon/task/demo secret answer</p></html>',
            'fetch', {'url': 'https://autobenchaudit.com/benchmark/toolathlon/task/demo'}), DENIED)

    def test_public_answer_threads_blocked_but_task_issue_and_source_readme_remain_available(self):
        for owner in ['toolathlon', 'piekeniuszwu', 'lonelybird']:
            for value in [
                {'owner': owner, 'repo': 'Annoy-DataSync', 'issue_number': 1},
                f'https://github.com/{owner}/Annoy-DataSync/issues/1',
                f'https://api.github.com/repos/{owner}/Annoy-DataSync/issues/31/comments?page=15',
                f'https://api.github.com/repos/{owner}/Annoy-DataSync/issues/comments/1234',
                f'license repo:{owner}/Annoy-DataSync',
            ]:
                with self.subTest(value=value):
                    self.assertTrue(protected_source(value))
        for value in [
            {'owner': 'o1zys777', 'repo': 'Annoy-DataSync', 'issue_number': 1},
            {'owner': 'shdlah', 'repo': 'Annoy-DataSync', 'issue_number': 1},
            'https://github.com/toolathlon/Annoy-DataSync/blob/main/README.md',
            'https://huggingface.co/datasets/HuggingFaceTB/smollm-corpus',
            'https://github.com/deepseek-ai/DeepSeek-V2.5/blob/main/LICENSE-MODEL',
            'https://github.com/lonelybird/another-business-repo/issues/1',
        ]:
            with self.subTest(value=value):
                self.assertFalse(protected_source(value))

    def test_known_huggingface_products_cover_repo_ids_fs_and_http_without_blocking_upstream(self):
        for identity in ['dongbobo/unified-toolcalls-canonical', 'dusersad12/unified-tool-call-corpus-29',
                         'dusersad12/unified-tool-call-merge', 'dusersad12/unified-tool-calls']:
            for value in [
                {'repo_id': identity}, {'repo_ids': [identity]},
                {'operations': [{'cmd': 'cat', 'args': [f'hf://datasets/{identity}/data/unified_tool_call.jsonl']}]},
                f'https://huggingface.co/datasets/{identity}/resolve/main/data/unified_tool_call.jsonl',
                f'https://hf.co/api/datasets/{identity}',
                f'https://datasets-server.huggingface.co/rows?dataset={identity}&split=train',
                f'https://hf-mirror.com/datasets/{identity}/resolve/main/README.md',
            ]:
                with self.subTest(value=value):
                    self.assertTrue(protected_source(value))
        for value in [
            {'repo_id': 'lockon/ToolACE'},
            {'repo_ids': ['lockon/glaive_toolcall_en', 'lockon/xlam-function-calling-60k']},
            'hf://datasets/dongbobo/unrelated-business-data/train.jsonl',
            'https://huggingface.co/datasets/another-owner/unified-toolcalls-canonical',
            'https://huggingface.co/datasets/dusersad12/unified-tool-calls-business',
        ]:
            with self.subTest(value=value):
                self.assertFalse(protected_source(value))

    def test_task_solution_skill_subtree_is_precise_and_relative_path_is_not_a_repository(self):
        for value in [
            'https://raw.githubusercontent.com/zjunlp/Skills/main/skills/skill-collections/toolathlon/youtube-playlist-tech-extractor/SKILL.md',
            'https://github.com/zjunlp/Skills/tree/main/skills/skill-collections/toolathlon',
            'https://api.github.com/repos/zjunlp/Skills/contents/skills/skill-collections/toolathlon/youtube-playlist-tech-extractor',
            {'owner': 'zjunlp', 'repo': 'Skills', 'path': 'skills/skill-collections/toolathlon/youtube-playlist-tech-extractor/SKILL.md'},
        ]:
            self.assertTrue(protected_source(value), value)
        for value in [
            'https://github.com/zjunlp/Skills/blob/main/README.md',
            'https://raw.githubusercontent.com/zjunlp/Skills/main/skills/general-search/SKILL.md',
            'skills/skill-collections/toolathlon/youtube-playlist-tech-extractor/SKILL.md',
            'projects/toolathlon/logs/business',
        ]:
            self.assertFalse(protected_source(value), value)

    async def test_repr_history_filters_only_unsafe_record_without_exempting_history_tools(self):
        from utils.answer_material_policy import request_denied
        history = {'status': 'success', 'results': [
            {'turn': 0, 'messages': [{'content': 'Please analyze the playlist with 52 videos.'}]},
            {'turn': 20, 'messages': [{'type': 'tool_output', 'preview':
                'https://github.com/hkust-nlp/Toolathlon answer material'}]},
            {'turn': 21, 'messages': [{'content': 'Continue browsing the playlist.'}]},
        ]}
        for value in [history, str(history)]:
            result = await filtered_output(value, 'local_browse_history')
            self.assertIn('52 videos', result)
            self.assertIn('Continue browsing', result)
            self.assertNotIn('answer material', result)
            self.assertNotIn('Benchmark answer sources were filtered', result)
            self.assertNotEqual(result, DENIED)
        self.assertTrue(request_denied({'url': 'https://github.com/hkust-nlp/Toolathlon'}, 'local_browse_history'))
        self.assertEqual(await filtered_output('https://github.com/hkust-nlp/Toolathlon answer',
                                              'local_browse_history'), DENIED)

    async def test_public_comment_search_removes_complete_answer_comment(self):
        response = json.dumps([
            {'body': 'Thanks for your interest! Both licenses are odc-by.',
             'issue_url': 'https://api.github.com/repos/toolathlon/Annoy-DataSync/issues/1'},
            {'body': 'The question belongs to this task.',
             'issue_url': 'https://api.github.com/repos/o1zys777/Annoy-DataSync/issues/1'},
        ])
        result = await filtered_output(response, 'github_search_issues')
        self.assertNotIn('Both licenses', result)
        self.assertIn('The question belongs', result)

    async def test_huggingface_search_retains_unrelated_dataset_cards(self):
        output = ('Found 2 repositories\n\n'
                  '### dusersad12/unified-tool-call-merge\nCompleted answer material\n\n---\n\n'
                  '### lockon/ToolACE\nOriginal task data source\n')
        result = await filtered_output(output, 'huggingface_hub_repo_search')
        self.assertNotIn('Completed answer', result)
        self.assertIn('Original task data source', result)
        self.assertNotEqual(result, DENIED)
        structured = json.dumps([
            {'repo_id': 'dusersad12/unified-tool-call-merge', 'description': 'Completed answer material'},
            {'repo_id': 'lockon/ToolACE', 'description': 'Original task data source'},
        ])
        result = await filtered_output(structured, 'huggingface_hub_repo_details')
        self.assertNotIn('Completed answer', result)
        self.assertIn('Original task data source', result)
