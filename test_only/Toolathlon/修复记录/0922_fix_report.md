# 流式推理对话中断问题修复记录

**日期**：2026-09-22
**问题来源**：`/mnt/djc/dumps_public_service/505B_AgentRL_iter30_0920/run2`（流式）对比 `run1`（非流式）
**涉及任务**：finalpool 108 个 task
**改动文件**：`utils/api_model/model_provider.py`（唯一改动文件）

---

## 一、问题现象

将推理从非流式改为流式后，大量对话异常中断，但 `status` 仍被标记为 `success`，表面看不出报错。

对比 run1（非流式，正常）与 run2（流式，中断）：

| 指标 | run1（非流式） | run2（流式） |
|---|---|---|
| `extra_request_params` | `{}` | `{'stream': True}` |
| 对话历史里带 `reasoning_content` 的 task | **108 / 108** | **0 / 108** |
| 单次响应 output ≥ 128000 token 的 task | 0 | **12** |
| 单条 assistant 消息 > 10 万字符（畸形超长） | 0 | **12** |
| `ab-testing` agent_llm_requests | 23 | 10 |
| finalpool 完整产出 traj_log.json | 107 / 108 | 103 / 108 |

典型例子 `ab-testing`：run1 有 58 条消息、23 次请求，正常收尾并调用 `claim_done`；run2 只有 23 条消息、10 次请求，在 turn 9 停在 "Let me examine the raw data more carefully..." 就没了下文，既没有总结也没有调用 `claim_done`。

典型例子 `nvidia-stock-analysis`：单条 assistant 消息 **443426 字符**，其中 8-gram `'me try to get the historical stock prices'` 重复出现 **986 次**，输出烧到 128000 token 上限。

---

## 二、根因

**流式路径把模型的思考链（reasoning_content）丢掉了，非流式路径保留。**

### 2.1 后端返回的字段形态（实测）

直接请求后端 `http://192.168.0.22:3000/v1/chat/completions`，模型 `505B_AgentRL_iter30_0920`（底层 `pangu_ultra_moe`）：

**流式**：思考内容走独立字段，**不在 `content` 里**

```
data: {"choices":[{"delta":{"reasoning":"We","reasoning_content":"We"},"finish_reason":null}]}
data: {"choices":[{"delta":{"reasoning":" are","reasoning_content":" are"},"finish_reason":null}]}
...（实测 87 个 chunk 中，85 个携带 reasoning，只有 1 个携带 content）
```

**非流式**：思考内容在 message 的 `reasoning` / `reasoning_content` 字段

```json
{"content": "OK", "reasoning": "We are given a conversation ...", "reasoning_content": "..."}
```

### 2.2 代码上的差异

- 非流式路径 [model_provider.py:124-193](utils/api_model/model_provider.py) 的 `ConverterWithExplicitReasoningContent.message_to_output_items`
  显式读取 `reasoning_content`，构造 `ResponseOutputReasoningContent` 塞进 message —— **思考链保留**

- 流式路径原先走库里的 `ChatCmplStreamHandler.handle_stream`
  （`.venv/.../agents/models/chatcmpl_stream_handler.py`），它**只累积 `delta.content` 和 `delta.tool_calls`**，
  `delta.reasoning_content` 直接丢弃 —— **思考链丢失**

### 2.3 为什么会导致中断

思考链丢失后，每一轮请求回传给模型的上下文里都没有它自己上一轮的推理过程。模型只能从零重新推导，在这条端点上退化成反复自我重复，一路把 `max_tokens: 128000` 的输出预算烧穿；回复在 token 上限处硬停，此时既没有正文收尾、也没有 tool call。框架看到一个"无工具调用的最终输出"就终止循环并标 `success`。所以表象是"对话突然中断"，实质是输出预算被畸形重复耗尽。

---

## 三、改动内容

**唯一改动文件：`utils/api_model/model_provider.py`**

- 改动前：`git HEAD = 9be8d8fe`，工作区已有其他人的未提交改动
- 改动后：`+136 −6` 行，`md5 = c9bfacd3cce006ae60eb3ad982b66eae`

> **行号说明**：以下行号为最终版本的当前行号。第一轮改动（改动 1–3）插入位置在文件中部，
> 第二轮改动（改动 4–6）在文件头部新增了 2 行 import 和 2 个函数，使改动 1–3 的行号整体下移 29 行。
> 如果只看第一轮改动的原始位置，请减去 29。

### 改动 1：新增 `_extract_chunk_reasoning(delta)` —— 第 460 行

从流式 delta 里取思考内容。后端把同一段文本同时放在 `reasoning` 和 `reasoning_content` 两个键下（实测 85 个 chunk 全部成对出现、内容完全一致），优先取 `reasoning_content`，与非流式返回值对齐，保证两条路径归一化到同一个字段名。找不到时返回空串。

### 改动 2：新增 `ChatCmplStreamHandlerWithReasoning` 类 —— 第 479 行

继承库里的 `ChatCmplStreamHandler`，包含两个方法：

| 位置 | 方法 | 作用 |
|---|---|---|
| 第 493 行 | `handle_stream` | 用内部 `tee()` 生成器在父类消费流之前先遍历一遍原始 chunk，把 reasoning 累积进 `reasoning_parts`，再在 `response.completed` 事件上调用 `_attach_reasoning` |
| 第 525 行 | `_attach_reasoning` | 把累积的思考文本构造成 `ResponseOutputReasoningContent`，挂到 assistant message 的 content 上；若该轮没有 message item（纯思考、无正文无工具调用），则补建一个 `ExtendedResponseOutputMessage`，形状与非流式路径一致 |

设计要点：

1. **用 tee 而不是重写整个 handler** —— 父类的 tool_calls 累积、usage 解析、事件顺序全部原样保留，只增补 reasoning 这一件事。
2. **双层容错** —— `tee()` 内部读取异常和 `_attach_reasoning` 的异常都被捕获并降级返回原事件（`logger.warning` 记录）。丢思考链是功能退步，但绝不能因此把整轮请求打挂。
3. **补齐纯思考轮次** —— 父类在没有正文也没有工具调用时不产出 message item，这里补建，避免 reasoning 无处安放。

### 改动 3：接入调用点 —— 第 686 行

`OpenAIChatCompletionsModelWithRetry._collect_streamed_response`（第 680 行起）内部的调用：

```python
# 改前
async for event in ChatCmplStreamHandler.handle_stream(response, stream):
# 改后
async for event in ChatCmplStreamHandlerWithReasoning.handle_stream(response, stream):
```

---

## 三之二、第二轮改动：消除 pydantic 序列化警告

第一轮改动上线后跑 run3 复验，功能正常，但日志里出现了 **17 条 pydantic 序列化警告**（run1/run2 都是 0 条）——这是第一轮改动引入的副作用：

```
PydanticSerializationUnexpectedValue(Expected `ResponseOutputText` -
  [input_value=ResponseOutputReasoningContent(...)])
```

### 成因

`_attach_reasoning` 往 message 上挂的 `ResponseOutputReasoningContent`，不在 openai 类型里 `Response.output` 那个联合类型的成员中。pydantic 在 dump 含这种 message 的 `Response` 时会警告类型不匹配。

**这只是警告，不是错误** —— 实测载荷完全正确（`payload identical: True`），reasoning 内容原样保留，run3 正是靠它才跑通的。

### 尝试过但无效的方案（记录备查，避免重复踩坑）

在 subclass 上换各种 content 注解都无法消除，因为该联合的序列化器在 import 时一次性构建、按具体类型匹配：

| 方案 | 结果 |
|---|---|
| `content: List[Union[ResponseOutputText, ResponseOutputRefusal, ResponseOutputReasoningContent]]`（去掉 Annotated） | 仍警告 |
| `content: List[Any]` | 仍警告 |
| `content: List[object]` | 仍警告 |
| `SerializeAsAny` | 仍警告 |
| 自定义 `__get_pydantic_core_schema__` | 仍警告 |
| `model_construct()` 绕过校验 | 直接报错（`MockValSer` 无法转 `SchemaSerializer`） |
| 构造 `Response(output=[msg])` 替代 `model_copy` | 仍警告 |

### 最终方案：在 dump 调用点做窄域抑制

新增两个函数（文件头部）：

| 行号 | 函数 | 作用 |
|---|---|---|
| 第 6 行 | `from contextlib import contextmanager` | 新增 import |
| 第 5 行 | `import warnings` | 新增 import |
| 第 95 行 | `_suppress_pydantic_serializer_warnings()` | 上下文管理器，用 `warnings.catch_warnings()` 只屏蔽 `"Pydantic serializer warnings"` 和 `"PydanticSerializationUnexpectedValue"` 两类消息 |
| 第 115 行 | `_dump_model(obj)` | 在抑制上下文中执行 `obj.model_dump()` |

应用点（第 902、906 行的两处 dump）：

```python
# 改前
json.dumps(final_response.model_dump(), indent=2, ensure_ascii=False),
span_generation.span_data.output = [final_response.model_dump()]
# 改后
json.dumps(_dump_model(final_response), indent=2, ensure_ascii=False),
span_generation.span_data.output = [_dump_model(final_response)]
```

**为什么选择抑制而不是"修根因"**：根因在 openai 类型定义的联合序列化器里，属于第三方库内部结构；上游代码本身是欢迎 `reasoning_content` 这种扩展内容的（非流式路径也用同一个类），我们的载荷是正确的。在 dump 这一层做窄域抑制，比动第三方类型定义更安全，也比全局 `filterwarnings` 更克制 —— 只影响这两个调用点周围的 dump，且只屏蔽这两类消息。

### ⚠️ 修正：上面这个方案没解决问题（第二轮返工）

按上述方案改完后又跑了一遍 ab-testing，**警告依然存在，38 条**（19 轮 × 2）。说明我定位错了地方。

**返工过程**：把 `warnings.showwarning` 替换成会打印调用栈的版本，再通过 `ContextManagedRunner.run` 复现，抓到真实出处：

```
>>> WARNING RAISED AT: pydantic/main.py 463
>>> CALL CHAIN:
      context_managed_runner.py, line 403, in _run_single_turn
      context_managed_runner.py, line 690, in _save_items_to_history
      pydantic/main.py, line 463, in model_dump
```

**真正的来源是 `utils/roles/context_managed_runner.py` 第 690 行的历史落盘**，不是 `model_provider.py`：

```python
"raw_content": item.raw_item.model_dump() ...
```

每一轮把 assistant message 写进 `conversation_history/*.jsonl` 时都会 dump 一次，而那个 message 带着 `reasoning_content`，于是每轮报一次。

**为什么之前没找到**：我一开始只在 `model_provider.py` 里搜 dump 调用点，而这条路径在另一个文件；并且单独 dump 这个 message 对象**不会**报警告（pydantic 只在特定嵌套/上下文下才判定不匹配），所以隔离测试一直是干净的，误导了判断。教训是：**先抓调用栈，再改代码**。

### 修正后的方案

改动文件增加到两个：

| 文件 | 行号 | 改动 |
|---|---|---|
| `utils/api_model/model_provider.py` | 第 5–6 行 | `import warnings` + `from contextlib import contextmanager` |
| 同上 | 第 95 行 | `_suppress_pydantic_serializer_warnings()` 上下文管理器 |
| 同上 | 第 115 行 | `_dump_model(obj)` 辅助函数 |
| 同上 | 第 891、895 行 | 流式分支的调试日志与 tracing span 改用 `_dump_model` |
| `utils/roles/context_managed_runner.py` | 第 22 行 | import 复用 `_suppress_pydantic_serializer_warnings` |
| 同上 | 第 688 行 | **真正生效的一处**：`_save_items_to_history` 内 dump 前套上抑制上下文 |

改动后的落盘逻辑：

```python
# 改前（context_managed_runner.py:690）
"raw_content": item.raw_item.model_dump() if hasattr(item.raw_item, 'model_dump') else item.raw_item
# 改后（第 688 行处）
with _suppress_pydantic_serializer_warnings():
    raw_content = (
        item.raw_item.model_dump()
        if hasattr(item.raw_item, 'model_dump')
        else item.raw_item
    )
...
"raw_content": raw_content
```

`model_provider.py` 里那两处虽然经确认不是警告来源（单独 dump 不触发），但保留作为防御 —— 它们保护的是同一个类的同一种风险。

**入参**：`_suppress_pydantic_serializer_warnings` 是从 `model_provider` 导出的，`context_managed_runner` 原本就已经 import 了该模块的 `ContextTooLongError`，因此复用不引入循环依赖（已验证 import 正常）。

### 修正后的验证

| 测试 | 方法 | 结果 |
|---|---|---|
| 复现（改前） | 走完整 `ContextManagedRunner.run` + 流式 | 每轮 1 条警告 |
| 修复后 | 同一路径 | **0 条警告** |
| 历史完整性 | 检查落盘 jsonl 是否仍带 reasoning | `history records=1, with reasoning_content=1` ✅ |
| 循环依赖 | 直接 import 两个模块 | 正常 |
| 编译 | `py_compile` 两个文件 | 通过 |
| 回归 | 重跑合成 chunk 单测 + 往返测试 | 全部通过 |

**当前改动汇总**：

| 文件 | 改动 | md5 |
|---|---|---|
| `utils/api_model/model_provider.py` | 见下方「真正的根因」一节 | — |
| `utils/roles/context_managed_runner.py` | `+35 −4` | `0090ac9740a67170e172f3260186c984` |

### ⚠️⚠️ 再次修正：抑制仍然没解决问题（第三轮，真正的根因）

按上面的抑制方案改完后再跑一次 ab-testing，**警告依然是 23 条**（23 轮 × 1）。两次都没修到根上。

#### 真正的根因

问题在我自己的 `_attach_reasoning` 里。库里的流式 handler 在**有正文**的轮次构造的是**普通** `ResponseOutputMessage`：

```python
assistant_msg = ResponseOutputMessage(   # 注意：不是 Extended 子类
    id=FAKE_RESPONSES_ID, content=[], role="assistant",
    type="message", status="completed",
)
```

而 `_attach_reasoning` 拿到这个对象后，直接把 `reasoning_content` 块追加进去：

```python
message.content = list(message.content) + [ResponseOutputReasoningContent(...)]
```

普通类的 `content` 字段类型是基础联合 `output_text | refusal`，**不含** `reasoning_content`。往里塞一个不属于该联合的块，之后每次 dump 都会触发 pydantic 警告。

实测三种轮次形态（`utils/api_model/model_provider.py` 修复前）：

| 轮次形态 | 产出类 | content 类型 | dump 警告 |
|---|---|---|---|
| 思考 + 工具调用（无正文） | `ExtendedResponseOutputMessage` | `['reasoning_content']` | 0 |
| **思考 + 正文** | **`ResponseOutputMessage`** | `['output_text','reasoning_content']` | **1** |
| 仅思考 | `ExtendedResponseOutputMessage` | `['reasoning_content']` | 0 |

只有「思考 + 正文」这一种形态会报警 —— 而实际任务里绝大多数轮次都有正文，所以每轮一条，23 轮 23 条，完全对得上。

**为什么之前两次都没找到**：我只顾着在 dump 调用点外面贴抑制，没去想"这个对象本身是否合法"。隔离测试时我手工构造的是 `ExtendedResponseOutputMessage`，自然不报警；而真实路径里库构造的是普通类，我没察觉两者差异。

#### 真正的修复：把普通 message 提升为 Extended 子类

`utils/api_model/model_provider.py` 第 549–560 行，`_attach_reasoning` 内：

```python
elif not isinstance(message, ExtendedResponseOutputMessage):
    # 库在"有正文"的轮次构造的是普通 ResponseOutputMessage，其 content
    # 字段类型不含 reasoning_content。原地追加会让之后每次 dump 都告警。
    # 这里把它提升为 content 类型兼容的 Extended 子类。
    outputs[outputs.index(message)] = message = ExtendedResponseOutputMessage(
        **message.model_dump()
    )
```

追加 reasoning 块的动作移到这个提升之后，因此无论哪种轮次形态，最终对象都是 `ExtendedResponseOutputMessage`。

**验证（修复后）**：

| 轮次形态 | 产出类 | dump 警告 |
|---|---|---|
| 思考 + 工具调用（无正文） | `ExtendedResponseOutputMessage` | 0 |
| **思考 + 正文** | **`ExtendedResponseOutputMessage`** | **0** |
| 仅思考 | `ExtendedResponseOutputMessage` | 0 |

**端到端验证**（走完整 `ContextManagedRunner.run`，带工具调用，流式）：

```
Pydantic warnings: 0
history records=4, with reasoning=2      # 思考链依然完整保留
```

#### 关于前两轮的抑制改动如何处置

保留，但定位改变 —— 它们从"修复手段"降级为"防御性兜底"。经确认它们不是警告来源（单独 dump 不触发），但保护的是同一类风险，删掉也无妨。**真正的修复是上面提升类别的这一处。**

#### 三轮返工的教训

1. **第一轮**：只在一个文件里搜 dump 调用点，漏掉了另一个文件。
2. **第二轮**：找到了 `context_managed_runner.py` 那条路径，但**只贴抑制，没问对象本身是否合法** —— 这是典型的治标。警告的成因是"对象类型和内容不匹配"，却在"哪里 dump"上打转。
3. **第三轮**：换思路对比了"三种轮次形态产出的对象类型"，一眼看出差异。

**下次遇到同类问题的顺序**：先确认**对象本身**的类型/结构是否合法 → 再查调用栈 → 最后才考虑抑制。抑制永远应该是最后一档，而不是第一档。

**最终改动汇总**：

| 文件 | 改动 | md5 |
|---|---|---|
| `utils/api_model/model_provider.py` | `+147 −6` | `a0c3c1eba3f81006e1a8a17d47f81fd0` |
| `utils/roles/context_managed_runner.py` | `+35 −4` | `0090ac9740a67170e172f3260186c984` |

---

## 四、验证

### 第一轮改动（reasoning 保留）

| 测试 | 方法 | 结果 |
|---|---|---|
| 合成 chunk 单测 | 构造 3 种流：思考+正文 / 纯思考 / 思考+工具调用 | 三种情况 reasoning 均正确挂到 message 上 |
| 下一轮请求回传 | 用 `ConverterWithExplicitReasoningContent.items_to_messages` 反向序列化 | `{"role":"assistant","reasoning_content":"Let me read the file.","content":"Reading it now.","tool_calls":[...]}` —— 思考链完整存活 |
| 真实后端流式调用 | 直接打 `192.168.0.22:3000` | `REASONING len: 95`（修复前该路径恒为 0） |
| 流式 vs 非流式对照 | 同一请求分别走两条路径，比较下一轮回传字段 | 两者均为 `['reasoning_content']`，达成一致 |
| 语法/编译 | `py_compile` | 通过 |

**保留说明**：那 12 个 task 烧穿 128000 token 的重复循环，未在本机完整复现验证。完整复现需跑一整轮真实任务（起 docker + 全部 MCP server），成本较高；已做的是一次含 13 轮真实历史的等价上下文重放，**未**触发循环。即单请求内的循环未必只由 reasoning 缺失触发。

### 第二轮改动（消除警告）的验证

| 测试 | 方法 | 结果 |
|---|---|---|
| 复现警告 | 构造 `text + reasoning` 的 message 放进 `Response` 后 `model_dump()` | 旧路径 **1 条警告** |
| 应用修复 | 同一对象走 `_dump_model()` | **0 条警告** |
| 载荷一致性 | 逐字节比对新旧 dump 结果 | **`payload identical: True`** |
| reasoning 保留 | 检查 dump 后 content 的 type 判别符 | `['output_text', 'reasoning_content']`，未丢失 |
| 真实后端全流程 | 打 `192.168.0.22:3000` 走完整流式调用 | pydantic 警告 **0 条**，reasoning 保留 |
| 回归 | 重跑第一轮的合成 chunk 单测 + 往返测试 | 全部通过 |

---

### 三轮实跑结果汇总（ab-testing）

| 指标 | run1（非流式，基准） | run2（流式，修复前） | **run3（流式，修复后）** |
|---|---|---|---|
| agent_llm_requests | 23 | 10 | **20** |
| assistant_turns | 23 | 9 | **20** |
| tool_calls | 34 | 13 | **30** |
| output_tokens | 27384 | 3431 | **20998** |
| **max_output_tokens** | 7388 | **128000** | **3820** |
| 带 reasoning_content 的消息 | 20 条 | **0 条** | **20/20 条** |
| 轨迹形态 | summary + claim_done | 半句截断 | **summary + claim_done** |
| pydantic 警告数 | 0 | 0 | 17 → **修复后 0** |

run3 产出的 `record.csv` 与 run1 逐项一致（21 个 scenario 无一项差异超过 0.02）。

**关于 run3 的 eval 仍然 fail**：三次跑的失败原因一字不差（`storage bucket with prefix 'promo-assets-for-b' was not created`），**与本次修复无关**。真实原因是模型把转化率算反了 —— 题目列名是 `A_clicks`/`A_store_views`，正确口径 `store_views/clicks` 全局应为 A=73.840%、B=74.121%（B 胜、应建 bucket），模型算成了 `clicks/store_views`（135.46% vs 134.94%）判 A 胜。`100/135.46 = 73.8%` 与 groundtruth 吻合，可确认只是分子分母颠倒。run1 日志里它甚至纠结过（`clicks/store_views` 出现 39 次、`store_views/clicks` 出现 32 次）仍选错。**这是模型能力问题，三条 run 表现完全一致。**

**复核建议指标**（在全量重跑时）：
- `max_output_tokens` 是否还有达到 128000 的 task（run2 有 12 个）
- 带 `reasoning_content` 的 task 是否恢复到 108/108（run2 为 0）
- pydantic 序列化警告是否归零（run3 修复前为 17 条）

---

## 五、尚未处理的相关问题

这两个在排查中发现，但**本次未改**：

### 5.1 `max_tokens` 与上下文窗口冲突

`pangu_92B.json` 配置 `max_tokens: 128000`，而模型上下文窗口是 524288。输入一旦超过 `524288 − 128000 = 396288` token，请求直接 400：

```
'max_tokens' is too large: 128000. This model's maximum context length is 524288
tokens and your request has 404729 input tokens
```

`task-tracker` 就是这么死的。run2 中最高输入 343275（`nvidia-market`），`language-school` 已到 264991，逼近阈值。

**建议**：`max_tokens` 降到 32768 一档。

### 5.2 五个 task 被同时 SIGKILL

run2 中 5 个 task 目录没有 `traj_log.json`，`status.json` 停在 `"running": "running"`：

```
k8s-pr-preview-testing、latex-prompt-box、live-transactions、shopping-helper、task-tracker
```

日志均为 `✗ Agent failed, exit code: 137`（SIGKILL），且 5 个的结束时间戳完全一致（`[2026-09-22 20:54:14]`），而配置的 timeout 是 36000s —— 属外部统一杀掉，与流式这条线无关，需单独查调度侧记录。

---

## 六、相关配置与路径速查

| 项 | 路径 |
|---|---|
| 推理配置文件 | `/mnt/djc/Toolathlon/scripts/temp_configs/pangu_92B.json` |
| 评测启动脚本 | `/mnt/djc/Toolathlon/scripts/run_parallel.sh` |
| 并行调度入口 | `/mnt/djc/Toolathlon/run_parallel.py` |
| 本次改动文件 | `/mnt/djc/Toolathlon/utils/api_model/model_provider.py` |
| 库内原始 handler | `/mnt/djc/Toolathlon/.venv/lib/python3.12/site-packages/agents/models/chatcmpl_stream_handler.py` |
| run1 轨迹（非流式） | `/mnt/djc/dumps_public_service/505B_AgentRL_iter30_0920/run1/finalpool/` |
| run2 轨迹（流式） | `/mnt/djc/dumps_public_service/505B_AgentRL_iter30_0920/run2/finalpool/` |
