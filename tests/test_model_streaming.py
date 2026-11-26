"""Offline SSE checks using the benchmark's pinned OpenAI/Agents packages."""
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if not (ROOT / 'configs/global_configs.py').exists():
    config = ModuleType('configs.global_configs')
    config.global_configs = SimpleNamespace()
    sys.modules.setdefault('configs.global_configs', config)

import httpx
from agents import ModelSettings
from openai import AsyncOpenAI
from utils.api_model.model_provider import (
    OpenAIChatCompletionsModelWithRetry, TruncatedStreamError,
    ConverterWithExplicitReasoningContent, _dump_model,
)
from utils.api_model.interrupted_stream import ModelOutputInterrupted, stream_output_dir, agent_budget_state


class ModelStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, deltas, finish='stop', usage=False, read_error=False):
        def chunk(delta, reason=None):
            return {'id': 'test', 'object': 'chat.completion.chunk', 'created': 1,
                    'model': 'test', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': reason}]}
        records = [chunk({'role': 'assistant'})] + [chunk(delta) for delta in deltas]
        if finish is not None:
            records.append(chunk({}, finish))
        if usage:
            records.append({'id': 'test', 'object': 'chat.completion.chunk', 'created': 1,
                            'model': 'test', 'choices': [],
                            'usage': {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30}})
        wire = ''.join('data: ' + json.dumps(x, ensure_ascii=False) + '\n\n' for x in records)
        wire += 'data: [DONE]\n\n'
        requests = []
        async def handle(request):
            requests.append(json.loads(request.content))
            if read_error:
                class BrokenStream(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        yield wire.replace('data: [DONE]\n\n', '').encode()
                        raise httpx.ReadTimeout('stream interrupted')
                return httpx.Response(200, stream=BrokenStream(), headers={'content-type': 'text/event-stream'})
            return httpx.Response(200, content=wire.encode(), headers={'content-type': 'text/event-stream'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            model = OpenAIChatCompletionsModelWithRetry(
                'test', AsyncOpenAI(api_key='test', base_url='https://mock.invalid/v1', http_client=http),
                debug=False, short_model_name='test',
            )
            # This is the same override supplied by agent.generation in a platform run.
            model.extra_request_params = {'stream': True}
            settings = ModelSettings(include_usage=True)
            response = await model._fetch_response(None, 'hello', settings, [], None, [], None,
                                                   SimpleNamespace(include_data=lambda: False), stream=False)
            result = await model._collect_streamed_response(*response)
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0]['stream'])
        self.assertTrue(requests[0]['stream_options']['include_usage'])
        return result

    async def test_reasoning_aliases_and_unicode_are_preserved_without_duplicates(self):
        for field in ['reasoning', 'reasoning_content']:
            with self.subTest(field=field):
                response = await self.collect([{field: '推理'}, {field: '继续'}, {'content': '答'}, {'content': '案'}])
                parts = response.output[0].content
                self.assertEqual(parts[0].text, '答案')
                self.assertEqual(parts[-1].pure_thinking_str, '推理继续')
                messages = ConverterWithExplicitReasoningContent.items_to_messages([_dump_model(x) for x in response.output])
                self.assertEqual(messages[0]['reasoning_content'], '推理继续')
        response = await self.collect([{'reasoning': 'duplicate', 'reasoning_content': 'preferred'}, {'content': 'ok'}])
        self.assertEqual(response.output[0].content[-1].pure_thinking_str, 'preferred')

    async def test_parallel_tool_arguments_are_joined_by_index(self):
        response = await self.collect([
            {'reasoning': 'choose tools'},
            {'tool_calls': [{'index': 0, 'id': 'a', 'type': 'function', 'function': {'name': 'first', 'arguments': '{"x":'}},
                            {'index': 1, 'id': 'b', 'type': 'function', 'function': {'name': 'second', 'arguments': '{"y":'}}]},
            {'tool_calls': [{'index': 1, 'function': {'arguments': '2}'}},
                            {'index': 0, 'function': {'arguments': '1}'}}]},
        ], finish='tool_calls')
        calls = {x.name: json.loads(x.arguments) for x in response.output if x.type == 'function_call'}
        self.assertEqual(calls, {'first': {'x': 1}, 'second': {'y': 2}})

    async def test_tool_continuation_preserves_reasoning_presence_on_wire(self):
        cases = [
            ({'reasoning_content': ''}, '', None),
            ({'reasoning': ''}, '', None),
            ({'reasoning_content': 'think'}, 'think', None),
            ({'reasoning': 'think'}, 'think', None),
            ({'reasoning_content': '', 'reasoning': 'alias'}, 'alias', None),
            ({'reasoning_content': 'canonical', 'reasoning': 'alias'}, 'canonical', None),
            ({'reasoning_content': None}, None, None),
            ({}, None, None),
            ({'reasoning_content': ''}, '', 'lookup now'),
            ({'reasoning_content': 'think'}, 'think', 'lookup now'),
        ]
        for reasoning_delta, expected, content in cases:
            with self.subTest(delta=reasoning_delta, content=content):
                deltas = [reasoning_delta]
                if content is not None:
                    deltas.append({'content': content})
                deltas.append({'tool_calls': [{
                    'index': 0, 'id': 'call-one', 'type': 'function',
                    'function': {'name': 'lookup', 'arguments': '{"x":1}'},
                }]})
                response = await self.collect(deltas, finish='tool_calls')
                history = [_dump_model(x) for x in response.output] + [{
                    'type': 'function_call_output', 'call_id': 'call-one', 'output': 'found',
                }]
                requests = []

                async def handle(request):
                    requests.append(json.loads(request.content))
                    return httpx.Response(200, json={
                        'id': 'next', 'object': 'chat.completion', 'created': 1, 'model': 'test',
                        'choices': [{'index': 0, 'finish_reason': 'stop',
                                     'message': {'role': 'assistant', 'content': 'done'}}],
                    })

                async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                    model = OpenAIChatCompletionsModelWithRetry(
                        'test', AsyncOpenAI(api_key='test', base_url='https://mock.invalid/v1', http_client=http),
                        debug=False, short_model_name='test',
                    )
                    await model._fetch_response(None, history, ModelSettings(), [], None, [], None,
                                                SimpleNamespace(include_data=lambda: False), stream=False)
                messages = requests[0]['messages']
                self.assertEqual([m['role'] for m in messages], ['assistant', 'tool'])
                assistant = messages[0]
                self.assertEqual('reasoning_content' in assistant, expected is not None)
                if expected is not None:
                    self.assertEqual(assistant['reasoning_content'], expected)
                self.assertEqual(assistant.get('content'), content)
                self.assertEqual(assistant['tool_calls'][0]['function'],
                                 {'name': 'lookup', 'arguments': '{"x":1}'})
                self.assertEqual(messages[1]['tool_call_id'], 'call-one')

    async def test_reasoning_only_output_is_preserved(self):
        response = await self.collect([{'reasoning': 'still thinking'}])
        self.assertEqual(response.output[0].content[-1].pure_thinking_str, 'still thinking')

    async def test_normal_provider_endings_preserve_partial_output(self):
        for finish in ['length', 'content_filter']:
            with self.subTest(finish=finish):
                response = await self.collect([{'content': 'unfinished'}], finish=finish)
                self.assertEqual(response.output[0].content[0].text, 'unfinished')

    async def test_stream_without_finish_marker_is_incomplete(self):
        with self.assertRaises(ModelOutputInterrupted) as captured:
            await self.collect([{'content': 'unfinished'}], finish=None)
        self.assertEqual(captured.exception.partial_output['content'], 'unfinished')

    async def test_empty_stream_without_finish_marker_remains_retryable(self):
        with self.assertRaises(TruncatedStreamError):
            await self.collect([], finish=None)

    async def test_read_timeout_after_reasoning_preserves_output_without_retry(self):
        with self.assertRaises(ModelOutputInterrupted) as captured:
            await self.collect([{'reasoning_content': 'unfinished thinking'}], finish=None, read_error=True)
        self.assertEqual(captured.exception.partial_output['reasoning_content'], 'unfinished thinking')

    async def test_interrupted_reasoning_is_journaled_and_not_retried(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            token = stream_output_dir.set(directory)
            try:
                with self.assertRaises(ModelOutputInterrupted) as captured:
                    await self.collect([{'reasoning': '思考中'}, {'reasoning_content': '继续'}], finish=None)
                self.assertEqual(captured.exception.partial_output['reasoning_content'], '思考中继续')
                files = list(Path(directory).glob('model_streams/*.partial.jsonl'))
                self.assertEqual(len(files), 1)
                self.assertIn('思考中', files[0].read_text())
            finally:
                stream_output_dir.reset(token)

    async def test_partial_tool_arguments_are_not_returned_for_execution(self):
        with self.assertRaises(ModelOutputInterrupted) as captured:
            await self.collect([{'reasoning': 'use tool'}, {'tool_calls': [
                {'index': 0, 'id': 'a', 'type': 'function',
                 'function': {'name': 'write', 'arguments': '{"x":'}}]}], finish=None)
        self.assertTrue(captured.exception.partial_output['incomplete_tool_calls'])

    async def test_model_retry_loop_does_not_regenerate_interrupted_output(self):
        model = OpenAIChatCompletionsModelWithRetry(
            'test', AsyncOpenAI(api_key='test'), debug=False, retry_times=2,
            retry_delay=0, short_model_name='test',
        )
        model.raw_get_response = AsyncMock(side_effect=ModelOutputInterrupted(
            'Model output ended without a finish_reason', reasoning='thinking',
        ))
        with self.assertRaises(ModelOutputInterrupted):
            await model.get_response()
        self.assertEqual(model.raw_get_response.await_count, 1)

    async def test_model_retry_loop_still_retries_empty_stream(self):
        model = OpenAIChatCompletionsModelWithRetry(
            'test', AsyncOpenAI(api_key='test'), debug=False, retry_times=2,
            retry_delay=0, short_model_name='test',
        )
        model.raw_get_response = AsyncMock(side_effect=TruncatedStreamError('stream ended before a finish_reason'))
        with self.assertRaisesRegex(Exception, 'Failed to get response after 2 retries'):
            await model.get_response()
        self.assertEqual(model.raw_get_response.await_count, 2)

    async def test_short_incomplete_stream_retries_and_recovers(self):
        model = OpenAIChatCompletionsModelWithRetry(
            'test', AsyncOpenAI(api_key='test'), debug=False, retry_delay=0,
            short_model_name='test',
        )
        error = ModelOutputInterrupted('missing finish', reasoning='partial', missing_finish_reason=True)
        response = SimpleNamespace(output=[])
        model.raw_get_response = AsyncMock(side_effect=[error, response])
        with patch('utils.api_model.model_provider.monotonic', side_effect=[0, 2999, 3000]):
            self.assertIs(await model.get_response(), response)
        self.assertEqual(model.raw_get_response.await_count, 2)

    async def test_short_incomplete_stream_exhausts_existing_ten_attempts(self):
        model = OpenAIChatCompletionsModelWithRetry(
            'test', AsyncOpenAI(api_key='test'), debug=False, retry_delay=0,
            short_model_name='test',
        )
        error = ModelOutputInterrupted('missing finish', content='partial', missing_finish_reason=True)
        model.raw_get_response = AsyncMock(side_effect=error)
        with self.assertRaisesRegex(Exception, 'Failed to get response after 10 retries'):
            await model.get_response()
        self.assertEqual(model.raw_get_response.await_count, 10)

    async def test_fifty_minute_incomplete_stream_does_not_retry(self):
        for error in [
            ModelOutputInterrupted('missing finish', reasoning='partial', missing_finish_reason=True),
            TruncatedStreamError('stream ended before a finish_reason'),
        ]:
            model = OpenAIChatCompletionsModelWithRetry(
                'test', AsyncOpenAI(api_key='test'), debug=False, retry_delay=0,
                short_model_name='test',
            )
            model.raw_get_response = AsyncMock(side_effect=error)
            with patch('utils.api_model.model_provider.monotonic', side_effect=[0, 3000]):
                with self.assertRaises(type(error)):
                    await model.get_response()
            self.assertEqual(model.raw_get_response.await_count, 1)

    async def test_expired_agent_budget_prevents_incomplete_stream_retry(self):
        model = OpenAIChatCompletionsModelWithRetry(
            'test', AsyncOpenAI(api_key='test'), debug=False, retry_delay=0,
            short_model_name='test',
        )
        error = ModelOutputInterrupted('missing finish', reasoning='partial', missing_finish_reason=True)
        model.raw_get_response = AsyncMock(side_effect=error)
        token = agent_budget_state.set({'expired': True})
        try:
            with self.assertRaises(ModelOutputInterrupted):
                await model.get_response()
        finally:
            agent_budget_state.reset(token)
        self.assertEqual(model.raw_get_response.await_count, 1)

    async def test_cancellation_is_not_retried(self):
        import asyncio
        model = OpenAIChatCompletionsModelWithRetry(
            'test', AsyncOpenAI(api_key='test'), debug=False, retry_delay=0,
            short_model_name='test',
        )
        model.raw_get_response = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await model.get_response()
        self.assertEqual(model.raw_get_response.await_count, 1)

    async def test_usage_only_terminal_chunk_is_retained(self):
        response = await self.collect([{'reasoning': 'think'}, {'content': 'ok'}], usage=True)
        self.assertEqual(response.usage.total_tokens, 30)


if __name__ == '__main__':
    unittest.main()
