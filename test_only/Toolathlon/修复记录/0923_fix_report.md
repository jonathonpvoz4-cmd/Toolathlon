# 流式推理 `reasoning_effort=max` 报错修复记录

**日期**：2026-09-23
**问题来源**：`/mnt/djc/dumps_public_service/deepseek-v4-flash-0731/test_run2`（流式，报错）对比 `test_run1`（非流式，正常）
**涉及任务**：`finalpool/ab-testing`（复现与验证均用此 task）
**改动文件**：`utils/api_model/model_provider.py`（唯一改动文件，删除 1 行、新增 7 行注释）

---

## 一、问题现象

0922 的修复让流式推理跑通了。换上 `deepseek-v4-flash-0731` 模型、并在配置里带上 `reasoning_effort` 之后，**流式路径每一轮都失败并重试 10 次**，最终整个 task 挂掉。

配置（`scripts/temp_configs/deepseek_v4_flash_thinking.json`）：

```json
"generation": {
    "max_tokens": 128000,
    "reasoning": { "effort": "max" },
    "extra_body": { "thinking": { "type": "enabled" } },
    "stream": true
}
```

`run.log` 里的报错：

```
Error in get_response: 1 validation error for Response
reasoning.effort
  Input should be 'low', 'medium' or 'high' [type=literal_error, input_value='max', input_type=str]
  For further information visit https://errors.pydantic.dev/2.11/v/literal_error, retry 1/10, waiting 10.0 seconds...
```

完整调用栈：

```
task_agent.py:1022  run
  └─ task_agent.py:728  run_interaction_loop
      └─ context_managed_runner.py:134  run
          └─ context_managed_runner.py:306  _run_single_turn
              └─ model_provider.py:1134  get_response
                  └─ model_provider.py:949   raw_get_response
                      └─ model_provider.py:876   _fetch_response
                          └─ model_provider.py:843   Response(...)      ← 在这里炸
                              └─ pydantic ValidationError: reasoning.effort
```

**关键对照**：**同一份配置，把 `stream` 关掉（test_run1）就完全正常。** 所以问题只出在流式路径。

---

## 二、根因

**不是流式解析代码写错了，而是流式路径比非流式路径多走了一步 `Response` 构造，而那一步会校验 `reasoning_effort` 的取值。**

### 2.1 两条路径的分叉点

`_fetch_response`（`model_provider.py`）里请求发出后有一处分叉：

```python
ret = await self._get_client().chat.completions.create(**base_params)

if isinstance(ret, ChatCompletion):   # 非流式：直接返回，不做任何校验
    return ret

response = Response(...)              # 流式：构造 Response 模板
    reasoning=model_settings.reasoning,   # ← 把原始 dict {"effort": "max"} 原样塞进去
return response, ret
```

| 路径 | `stream` | 返回值 | 是否构造 `Response` |
|---|---|---|---|
| 非流式 | `False` | `ChatCompletion` | **否**（命中早返回） |
| 流式 | `True` | `AsyncStream` | **是** |

- **非流式**：`reasoning_effort` 只是作为请求参数原样发给服务端。服务端认 `max`，所以 test_run1 正常。
- **流式**：`AsyncStream` 不是 `ChatCompletion`，落到下面构造 `Response`。`reasoning=model_settings.reasoning` 把 `{"effort": "max"}` 原样传入，被 openai 客户端的类型校验拦下。

### 2.2 为什么 `max` 过不了校验

`Response.reasoning` 字段的类型是 `openai.types.shared.reasoning.Reasoning`，其中：

```python
ReasoningEffort: TypeAlias = Optional[Literal["low", "medium", "high"]]
# 文件：.venv/lib/python3.12/site-packages/openai/types/shared/reasoning_effort.py
```

**`max` 不在其中** —— 这是 OpenAI 上游客户端类型的定义（新版本才加了 `minimal`），和你的服务端支持什么无关。服务端接受 `max`，但客户端类型不认识它。

本机复现（项目 venv，openai 1.76.0）：

```
$ .venv/bin/python -c "from openai.types.responses import Response; Response(..., reasoning={'effort':'max'})"
FAIL: ValidationError 1 validation error for Response
reasoning.effort
  Input should be 'low', 'medium' or 'high' [type=literal_error, input_value='max']
```

逐个试 `Reasoning(effort=...)`：

```
low        OK
medium     OK
high       OK
max        FAIL
minimal    FAIL
```

> 说明：`Response` 这段构造本身是从 `agents` SDK 上游
> `.venv/.../agents/models/openai_chatcompletions.py` 照搬过来的，**上游同样有这个缺陷** ——
> 只要 `reasoning.effort` 不是那三个值就会炸。我们不是引入了新 bug，而是踩到了上游的坑。

### 2.3 为什么偏偏是"流式 + max"才会触发

三者必须同时成立：

1. `stream=True` → 才会走 `Response(...)` 构造
2. `reasoning.effort` 不是 low/medium/high → 才会校验失败
3. 不满足前两条中任意一条 → 早返回或校验通过，都不报错

这就解释了为什么 0922 修复后的流式能跑（没配 effort），以及为什么换非流式也能跑（不走那条路）。

### 2.4 这个 `reasoning` 字段没有任何用处

追溯它被构造之后到最终被读取的完整链路：

```
Response(shell) ──► handle_stream() ──► _attach_reasoning()
                                         └─ model_copy(update={"output": ...})
                    _collect_streamed_response()
                         └─ final_response.output  ← 只用这一个字段
                    ModelResponse(output=items, usage=usage, response_id=None)
```

- 这个 `Response` **从不发给服务端**（给服务端的是上面已经发出去的 `base_params`）
- 该字段**从不被读回**：`raw_get_response` 只取 `final_response.output` 去构建 `ModelResponse`，而 `ModelResponse` 根本没有 `reasoning` 字段
- 全局搜索 `Response.reasoning` 的消费方：无。唯一另一处 `reasoning`（第 1330 行）是发请求用的 `_non_null_or_not_given(model_settings.reasoning)`，与这个模板对象是两回事

**它纯粹是给 `handle_stream` 提供结构框架的装饰字段**，删掉对行为零影响。

---

## 三、改动内容

**唯一改动文件：`utils/api_model/model_provider.py`**

删除 `Response(...)` 构造里的 `reasoning=model_settings.reasoning` 一行，替换为说明性注释。

### 改动位置：第 843–864 行（`_fetch_response` 内）

```python
        response = Response(
            id=FAKE_RESPONSES_ID,
            created_at=time.time(),
            model=self.model,
            object="response",
            output=[],
            tool_choice=cast(Literal["auto", "required", "none"], tool_choice)
            if tool_choice != NOT_GIVEN
            else "auto",
            top_p=model_settings.top_p,
            temperature=model_settings.temperature,
            tools=[],
            parallel_tool_calls=parallel_tool_calls or False,
            # No `reasoning=` here on purpose. This object is only a template for
            # the streaming path (it is never sent to the API and nothing reads
            # this field back — `raw_get_response` only consumes `output`), but
            # `Response` validates it against the client-side
            # `ReasoningEffort = Literal["low", "medium", "high"]`. Server-side
            # extensions such as `"max"` therefore raise a pydantic
            # ValidationError and kill the whole turn. The real effort value
            # still travels to the server via `base_params["reasoning_effort"]`.
        )
        return response, ret
```

改动前：

```python
            parallel_tool_calls=parallel_tool_calls or False,
            reasoning=model_settings.reasoning,
        )
        return response, ret
```

**净改动：`+7 −1` 行**（删除 1 行参数，新增 7 行注释）
**改动后 md5**：`0572ed2d079100a9296d1b4c7ef50760`
**改动后行号**：`Response(...)` 起始于第 843 行，`return response, ret` 于第 864 行

### 为什么选"删除"而不是"降级为 high"

排查过程中考虑过两个替代方案，都放弃了：

| 方案 | 问题 |
|---|---|
| 把 `max` 降级成 `high` 再放进 `Response` | 需要额外引入 `Reasoning` 类型转换，且服务端将来再冒出新 effort 值（如 `minimal`）会再次踩坑 |
| 直接改 openai 源码，往 `ReasoningEffort` 里加 `max` | 改第三方库源码，`uv sync --frozen` 重建 venv 时会被覆盖；影响面也更大 |

既然这个字段本就无人消费，"不让它参与校验"比"想方设法让校验通过"更干净、更彻底。

### 未受影响的部分（重要）

真实发往服务端的请求参数**完全没动**：

```python
# 第 812 行，维持原样
'reasoning_effort': self._non_null_or_not_given(reasoning_effort),
```

服务端照旧收到 `"max"`，`extra_body={"thinking": {"type": "enabled"}}` 也照旧透传。修改的只是本地那个不参与请求的 `Response` 模板。

---

## 四、验证

### 4.1 复现与修复（本机）

| 测试 | 方法 | 结果 |
|---|---|---|
| 复现 | 原代码构造 `Response(..., reasoning={'effort':'max'})` | `ValidationError: reasoning.effort` ✅ 复现 |
| 确认类型定义 | 逐个试 `Reasoning(effort=...)` | `max`/`minimal` FAIL，`low`/`medium`/`high` OK |
| 修复后构造 | 复刻改后的 `Response(...)` | 构造成功，`reasoning is None` |
| 语法检查 | `ast.parse` 整个文件 | 通过 |
| 请求路径未受影响 | `grep` 确认第 812 行保持不变 | `base_params['reasoning_effort']` 仍为原值 |

### 4.2 端到端实跑（ab-testing）

命令：

```bash
nohup sudo bash scripts/run_parallel.sh deepseek-v4-flash-0731 \
    /mnt/djc/dumps_public_service/deepseek-v4-flash-0731/test_run2 unified 1 > run_parallel.log 2>&1 &
```

| 指标 | test_run1（非流式，基准） | **test_run2（流式，修复前）** | **test_run2（流式，修复后）** |
|---|---|---|---|
| `reasoning.effort` 校验错误 | 0 | **每轮 1 次 × 10 重试** | **0** |
| `agent_llm_requests` | 18 | 0（全轮失败） | **22** |
| `tool_calls` | 24 | 0 | **29** |
| `output_tokens` | 13402 | 0 | **15593** |
| `max_output_tokens` | 1838 | 0 | **1839** |
| 带 reasoning 的 assistant 消息 | — | 0 | **22 / 22** |
| Evaluation | `Pass: True` | `Pass: None`（failed） | **`Pass: True`** |
| 进程退出码 | 0 | 1 | **0** |

修复后 `grep` 整个 `run.log`：`validation error` 出现 **0 次**，无任何重试记录。

### 4.3 流式核心能力未被带偏

这次改动只删了一个无关字段，但既然动了流式路径，还是确认了一件事——**0922 修复的"reasoning 保留"依然生效**：

```
conversation_history/task_finalpool-ab-testing_session_history.jsonl:
  records=81, with_reasoning=22
  其中 assistant 记录 22 条，带 pure_thinking_str 的 22 条  ← 全部保留
```

22 条正好对上这轮的 22 次 `agent_llm_requests`，说明每一轮的思考链都完整落盘、也完整回传给下一轮。

`max_output_tokens` 为 1839（远低于 128000 上限），0922 记录里"思考链丢失→自我重复烧穿预算"的现象**没有复发**。

---

## 五、遗留与提醒

### 5.1 `old_Toolathlon` 目录未同步

本次只改了 `/mnt/djc/Toolathlon/utils/api_model/model_provider.py`。
`/mnt/djc/old_Toolathlon/` 下若存在同一份流式代码，第 856 行的 `reasoning=model_settings.reasoning` 仍在，用它做流式评测会复现同样的报错。**如后续改用它，需打同样的补丁。**

### 5.2 上游 SDK 的同一处缺陷

`.venv/.../agents/models/openai_chatcompletions.py` 的 `_fetch_response` 里有同样的 `reasoning=model_settings.reasoning`。
只要走 `stream=True` 且 effort 非法，**不用我们的代码也会炸**。我们是在自己那份 `_fetch_response` 里绕开了它，不影响上游文件；将来升级 `agents` 包时，这个上游问题大概率依然存在，不必指望它自动修好。

### 5.3 未采用但可行的替代方案（备查）

- **给 `Response` 构造套 try/except 降级**：将来若 `Response` 再引入别的字段触发同类校验，能兜住。本次没做，因为已确认该字段无消费方，删掉比降级更彻底。
- **加 `reasoning_effort` 白名单归一化**（如 `max` → `high`）：会掩盖服务端与客户端的能力差异，且需要额外维护白名单，不推荐。

---

## 六、相关配置与路径速查

| 项 | 路径 |
|---|---|
| 本次改动文件 | `/mnt/djc/Toolathlon/utils/api_model/model_provider.py`（第 843–864 行） |
| 出问题的 `Response` 构造 | 同上，第 843 行 |
| 请求参数设置（未改动） | 同上，第 812 行 `'reasoning_effort': ...` |
| 推理配置文件 | `/mnt/djc/Toolathlon/scripts/temp_configs/deepseek_v4_flash_thinking.json` |
| openai 类型定义 | `/mnt/djc/Toolathlon/.venv/lib/python3.12/site-packages/openai/types/shared/reasoning_effort.py` |
| 上游 SDK 同源代码 | `/mnt/djc/Toolathlon/.venv/lib/python3.12/site-packages/agents/models/openai_chatcompletions.py` |
| 评测启动脚本 | `/mnt/djc/Toolathlon/scripts/run_parallel.sh` |
| 报错 run（流式） | `/mnt/djc/dumps_public_service/deepseek-v4-flash-0731/test_run2/finalpool/ab-testing/run.log` |
| 基准 run（非流式） | `/mnt/djc/dumps_public_service/deepseek-v4-flash-0731/test_run1/finalpool/ab-testing/run.log` |
| 0922 修复记录 | `/mnt/djc/Toolathlon/修复记录/0922_fix_report.md` |
