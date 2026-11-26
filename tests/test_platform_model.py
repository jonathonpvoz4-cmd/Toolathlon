import json

import httpx
import pytest
from agents import ModelSettings
from openai import AsyncOpenAI

from utils.api_model.model_provider import OpenAIChatCompletionsModelWithRetry


@pytest.mark.asyncio
async def test_model_request_carries_proxy_identity_and_generation(monkeypatch):
    monkeypatch.setenv('TOOLATHLON_PLATFORM_RUN_ID', 'job/run')
    monkeypatch.setenv('TOOLATHLON_PLATFORM_TASK_ID', 'task-1')
    monkeypatch.setenv('TOOLATHLON_REAL_BASE_URL', 'https://model.example/v1')
    monkeypatch.setenv('TOOLATHLON_REAL_API_KEY', 'test-model-key')
    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(200, json={'id': 'test', 'object': 'chat.completion', 'created': 1,
                                        'model': 'test-model', 'choices': [{'index': 0, 'finish_reason': 'stop',
                                        'message': {'role': 'assistant', 'content': 'ok'}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = AsyncOpenAI(api_key='test-model-key', base_url='http://proxy.example/v1', http_client=http)
        model = OpenAIChatCompletionsModelWithRetry('test-model', client, debug=False, short_model_name='test-model')
        from types import SimpleNamespace
        result = await model._fetch_response(None, 'hello',
                                            ModelSettings(reasoning={'effort': 'max'}, extra_body={'thinking': {'type': 'enabled'}}),
                                            [], None, [], None, SimpleNamespace(include_data=lambda: False))
        assert result.choices[0].message.content == 'ok'
    request = requests[0]
    assert str(request.url) == 'http://proxy.example/v1/chat/completions'
    assert request.headers['X-Run-ID'] == 'job/run'
    assert request.headers['X-Task-ID'] == 'task-1'
    assert request.headers['X-Real-Base-URL'] == 'https://model.example/v1'
    assert request.headers['X-Real-API-Key'] == 'test-model-key'
    body = json.loads(request.content)
    assert body['reasoning_effort'] == 'max'
    assert body['thinking'] == {'type': 'enabled'}
