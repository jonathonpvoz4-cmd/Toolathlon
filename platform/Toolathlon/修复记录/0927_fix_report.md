# 流式推理 `length` 截断被放行 / 半截回合静默完成 —— 分析与修复方案

**日期**：2026-09-27
**问题来源（评分对比）**：`/mnt/djc/dumps_public_service/505B-v21-B003-opd-agent-5/run1_stream`（流式）对比 `run3`（非流式）
**截图/抓包依据**：`/mnt/djc/Toolathlon/cc_trace-master/log/cli_api_proxy_messages_2026-09-27_openai-toolathon_1.jsonl`（ab-testing，16 次请求）
**代码位置**：`utils/api_model/model_provider.py`、上游 `agents/models/chatcmpl_stream_handler.py`

> 本文记录分析结论、改动位置与**已落地的修复**（见 §7）。改动文件：`utils/api_model/model_provider.py`。
> 未落地/未定论的项在 §7.3–§7.7 与 §6 中标明。

---

## 0. 背景：评分差异与前置判定

流式 run1 与 非流式 run3 分数差距较大。`task_files/problem_tasks.txt` 列出 15 道"流式失败、非流式成功"的 task。逐题核对轨迹后分为两类：

- **异常原因（非模型能力）** —— 5 道：
  - `canvas-new-students-notification`：工具调用参数 JSON 被截断（`{"account_id": 1, "per_page": ` 断裂），报 `Invalid JSON input` 后整轮 failed。
  - `excel-data-transformation`、`sync-todo-to-readme`：同一段 Python 代码被反复提交 183 次 / 178 次，200 轮打满（`max_turns_reached`）。
  - `woocommerce-new-product`：30 个收件人发出 48 封邮件，200 轮打满。
  - `llm-training-dataset`：最后一条 assistant 消息无工具调用、无 `claim_done`，会话终止，评测读到空 sheet。
- **模型自身能力/判断问题** —— 10 道：`course-schedule`（考场号写成 `3,303`）、`dietary-health`（未产出 analysis.md）、`stock-build-position`（B 列 stock_code 全空）、`ppt-analysis`、`k8s-mysql` 等。

**净结论：15 道中 5 道（33%）由流式推理异常导致，应作为非模型因素剔除。**

---

## 1. 已验证的框架行为（抓包实证）

抓包文件为 SSE 原始字符串（`data: {...}`），需按 SSE 解析后统计。ab-testing 共 16 次请求，全部 `stream: true`、`max_tokens: 128000`。

### 1.1 delta 字段清单

```
delta keys: [('reasoning', 2624), ('reasoning_content', 2624),
             ('content', 97), ('tool_calls', 75), ('role', 16)]
finish reasons: [('tool_calls', 15)]
```

- `reasoning` 与 `reasoning_content` **成对出现、内容一致**（2624 : 2624），与 0922 记录描述吻合。
- 15 条完整响应 finish_reason **全部是 `tool_calls`**，**没有一条 `length`**。

### 1.2 回传侧保留推理

request 侧 assistant 消息带 `reasoning_content`，共 109 条，形如：

```json
{"role": "assistant", "content": "...", "reasoning_content": "...", "tool_calls": [...]}
```

**结论：ab-testing 这道题上，0922 的"推理保留"修复生效，思考链完整回传。**

### 1.3 一个非 bug 的形态观察

同一回合在回传时会被拆成两条消息：一条 `{role, tool_calls}`、一条 `{reasoning_content, role}`。这是 `ConverterWithExplicitReasoningContent` 的正常输出形态，**不是 bug**，但能解释 traj_log 里 `thinking` 字段填充率偏低的现象（大量推理被拆进独立消息）。

---

## 2. 确认的 Bug

### bug 1（核心）：`length` 被当作正常 `finish_reason`，截断守卫失效

**位置**：`model_provider.py:652-655`（`ChatCmplStreamHandlerWithReasoning.handle_stream` 内的 `tee()`）

```python
chunk_finish_reason = getattr(choices[0], "finish_reason", None)
if chunk_finish_reason:
    saw_terminal_finish_reason = True        # ← 任何非空值都置 True
    finish_reasons.append(str(chunk_finish_reason))
```

变量名是 `saw_terminal_finish_reason`，但 `length`（撞满 `max_tokens` 硬停）**也被置为 terminal**。于是 `model_provider.py:693` 的守卫：

```python
if not saw_terminal_finish_reason and not has_payload:
    raise TruncatedStreamError(...)
```

在 `finish_reason='length'` 时**恒为假** —— 不抛异常、不重试。

而 `_classify_stream_end`（`model_provider.py:543`）**已经正确**把 `length` 判为 `TRUNCATED` —— 判定对了，但判定结果**只用于打印日志，不参与控制流**。

**后果**：一个被 `length` 截断的半截回合，若无工具调用，被当作"模型主动说完"交给 agent loop，任务静默结束并记 `success`，实际工作没做完。

**run1 实证**（`run.log` 中的 `[STREAM_DIAG]` 行）：

```
verdict=TRUNCATED finish_reason=['length'] content_chunks=32411 content_chars=448732
             reasoning_chunks=0 tool_call_chunks=0        ← llm-training-dataset
verdict=TRUNCATED finish_reason=['length'] content_chunks=15892 content_chars=236926
             reasoning_chunks=16411 reasoning_chars=256917 tool_call_chunks=0   ← dietary-health
```

全库共 35 条诊断：`TEXT_FLOOD` 24、`TRUNCATED` 9、`EMPTY_TURN` 2。

### bug 2：被 `length` 截断的正文仍被算作 payload

**位置**：`model_provider.py:1225`（`get_response` 内）

```python
if not _model_response_has_payload(model_response):
    raise TruncatedStreamError(...)
```

`_response_has_payload` / `_model_response_has_payload`（`model_provider.py:760-776` / `779-793`）只看"message 里有没有 `output_text`"，**不看这段正文是否被 `length` 截断**。因此撞满 `max_tokens` 后半截正文照样通过检查，直接 return，不进重试。

> ⚠️ **注意**：`get_response` 是**流式与非流式共用**的方法体。本 bug 的修复**不应**在 1225 行加判断，而应由 bug 1 的修复在流式侧拦下（见 §3 方案），从而绕开该共用方法。

### bug 3（已撤销的误判，记录备查）

先前基于"同一段代码重复 183 次"推断存在"流式回放导致的重复提交"，**该判断错误，已撤销**。核查 `tool_call_id`：**195 个调用对应 195 个唯一 id**，且重复段落的**执行结果逐条变化**（msg31/33 报 `SyntaxError`，msg35 起才出结果）。

**结论**：这些重复是模型在不同轮次**各自独立生成**了几乎相同的代码 —— 属"推理/上下文退化"的**表现**，不是独立的框架 bug。**上游 `chatcmpl_stream_handler.py:154` 的累积逻辑无需改动。**

### bug 4：`_extract_chunk_reasoning` 取键过窄 —— ab-testing 上**不成立**，其他任务待验

`model_provider.py:486` 从 `delta.reasoning_content` / `delta.reasoning` 取推理。ab-testing 抓包证明**两个键都命中**，取键策略正确充分，**485–494 行无需修改**。

但 run1 中另有任务报 `reasoning_chunks=0`（如 `course-schedule` 的 `content_chunks=2382 reasoning_chunks=0`），且 traj_log 中 `thinking` 字段填充率极低（excel 8/205、sync-todo 4/204）。**这些是不同任务、不同后端实例**（抓包的 `model` 字段为 `pangu_ultra_moe`），ab-testing 不能替其作证。

**状态：待你抓取一道真正报了 `reasoning_chunks=0` 的任务（建议 llm-training-dataset）后再定论。** 若那些轮次确实不流式返回推理，则属模型端行为，代码层面无法修复。

---

## 3. 待实施的改动位置（纯流式侧，不碰非流式）

**改动边界依据**：流式与非流式**共用同一个类** `OpenAIChatCompletionsModelWithRetry`（`model_provider.py:803`），靠运行时 `stream` 参数在 `model_provider.py:1066` 分流：

```python
if isinstance(ret, ChatCompletion):   # 非流式：早返回
    return ret
response = Response(...)              # 流式：往下走到 handle_stream
return response, ret
```

因此**只要不动 1066 行以上的构造部分、以及 1093-1166 的非流式分支，就不会影响非流式路径**。所有改动集中在以下位置：

| 位置 | 行号 | 内容 | 说明 |
|---|---|---|---|
| **位置 1** | `model_provider.py:652-655` | `saw_terminal_finish_reason` 的置位条件 | 改为仅当 `chunk_finish_reason in _CLEAN_FINISH_REASONS` 时置 True（常量已在 `:503` 定义）。**核心改动** |
| **位置 2** | `model_provider.py:693-701` | 截断守卫 | 配合位置 1，使 `length` 截断落入 `raise TruncatedStreamError` → 触发 `get_response` 重试 |
| **位置 3** | `model_provider.py:1220-1229` | 共用方法内的 payload 检查 | **不加逻辑**。由位置 1+2 在流式侧解决，绕开共用方法体 |
| **位置 4** | `model_provider.py:642-648` | 诊断计数器（可选） | `_TEXT_FLOOD_CHARS` 只统计 `delta.content`，**不统计 tool_call 参数体积** —— 这是 excel/sync-todo 那种"循环烧满 200 轮"在 run.log 里一条诊断都没有的原因。纯观测，不改行为 |
| 不动 | `:522 _stream_diag`、`:543 _classify_stream_end`、`:52 _build_llm_timeout` | 保持原样 | 它们只负责"看"，不负责"改"。bug 1 的修法是把它们**已算对的结论**接进控制流，而非重写 |

**推荐方案**：位置 1 + 位置 2 合并（流式侧拦 `length`），可选加位置 4。

**待你决策**：`length` 截断的处理策略——
- **A**：一律抛 `TruncatedStreamError` 重试（简单、不漏，但整轮重发、成本高）
- **B**：仅在"有正文、无工具调用"时重试（精准、省成本，但可能漏掉"截断正文里夹了工具调用"的情况）

---

## 4. `length` 截断的成因（回答提问）

**抓包中 15 条完整响应 finish_reason 全为 `tool_calls`，无一条 `length`，故 ab-testing 未发生截断。**

`length` 仅出现在 run1 的另两道题（llm-training-dataset、dietary-health）。机制为：

1. 某轮推理**未走 `reasoning_content` 通道**，而是落进了 `content` 通道；
2. 模型在正文里退化为一字排开的自我重复（`text_tail` 实录："Let me fetch these pages. OK let me do the fetches. Let me do this. I'll fetch these two pages. Let me proceed..."），永远走不到真正调用工具；
3. 正文越堆越长（44.8 万 / 23.7 万字符），撞穿 `max_tokens=128000` → 服务端返回 `finish_reason='length'` 硬停；
4. 该回合无工具调用 → **bug 1 将其误判为正常收尾，不重试，任务静默"成功"结束**。

**即 `length` 是结果而非原因**——根因是"输出预算被失控正文烧穿"，与 0922 记录中"思考链丢失 → 反复自我重复 → 烧穿 max_tokens"属同一链条；0922 只修了"把 reasoning 挂回去"，**未修 bug 1**，因此截断一旦发生仍会致命。

---

## 5. 附带发现：半截流（模型端掐断）

抓包 line15 只发了 **1 个 chunk** 即中断：

```json
{"choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}
```

无 reasoning、无 content、无 tool_calls、无 finish_reason，无 `[DONE]`，`finished: False`。对照其余 15 条均有完整 `DONE` + `finish_reason=['tool_calls']` + `finished: True`。

**该 case 当前被 `model_provider.py:693` 的守卫覆盖**（`saw_terminal_finish_reason=False` 且 `has_payload=False` → 抛异常 → 重试）。

**但**：若此类半截流**带着正文**被掐断（`has_payload=True`），则与 bug 2 是同一缺口，仍会漏网。建议在位置 1+2 的修复中一并考虑。

---

## 6. 行动项

1. **修 bug 1**（位置 1 + 位置 2，纯流式侧）——~~待你确认策略 A 或 B 后落地~~ → **已落地，见 §7**。
2. **bug 4 挂起**——ab-testing 证明取键正确；需补抓一道报 `reasoning_chunks=0` 的任务（建议 llm-training-dataset）再定论。
3. **观察项**——"带正文的半截流"（§5），若策略选 B 需额外覆盖 → **已随 §7 一并覆盖**。

---

## 7. 改动记录（2026-09-27 实施）

**改动文件**：`utils/api_model/model_provider.py`（唯一文件）。
`utils/api_model/openai_client.py` **未改动** —— 该文件 `chat_completion_stream` 的流式分支自始至终没有读取 `choices[0].finish_reason`，不存在同类缺口。

### 7.1 位置 1 —— `tee()` 内的 terminal 置位条件

`model_provider.py:649-660`：

```python
chunk_finish_reason = getattr(choices[0], "finish_reason", None)
if chunk_finish_reason:
    if str(chunk_finish_reason) in _CLEAN_FINISH_REASONS:   # ← 新增内层判断
        saw_terminal_finish_reason = True
    finish_reasons.append(str(chunk_finish_reason))          # ← 诊断计数照旧，不丢
```

`length` / `content_filter` 仍进 `finish_reasons`（`[STREAM_DIAG]` 行为不变），但不再置 `saw_terminal_finish_reason`。复用 `:503` 已有常量，未新增。

### 7.2 位置 2 —— 截断守卫由 `AND` 改单条件

`model_provider.py:698-719`：

```python
# 改前
if not saw_terminal_finish_reason and not has_payload:
# 改后
if not saw_terminal_finish_reason:
```

`has_payload` 是原守卫里 `AND` 的一环，正是它放行了"带正文的截断/半截流"。移除后，只要没有干净收尾就抛 `TruncatedStreamError` → `get_response` 重试。

**这一行同时收掉 bug 1 与 bug 2**，并覆盖 §5 的"带正文半截流"。
`has_payload` 仍参与 `_classify_stream_end` 与日志输出，未删除。

### 7.3 位置 3（bug 2 的 `:1225`）—— 未改动

按 §3 建议，共用方法体内的 payload 检查保持原样。流式侧已在更早的 `handle_stream` 拦下，走不到 `:1225`；非流式路径零影响。

### 7.4 bug 4 —— 抓包证伪，代码未改

`cli_api_proxy_messages_2026-09-27_openai-toolathon_1.jsonl` 全量解析：2624 个 chunk 中 `reasoning` 与 `reasoning_content` **成对出现、字节相同**（mismatch=0），`only_reasoning` / `only_reasoning_content` **均为 0**，且 `reasoning` 在 JSON 中恒排在 `reasoning_content` 之前。
`_extract_chunk_reasoning`（`:486`）的取键策略正确充分，**485–494 行未改**。
§6 第 2 条"补抓 llm-training-dataset 再定论"的挂起项**仍然成立**。

### 7.5 策略选择：实际落地方案 A

报告 §3 末尾列的 A/B 二选一，本次按 **A（一律重试）** 实施，因为 `has_payload` 已移出守卫、`length` 也不再算 terminal。

未选 B 的理由：B 需区分"截断正文里夹了工具调用"，而 `length` 截断时工具调用参数本身可能就是断的 —— `canvas-new-students-notification` 的 `{"account_id": 1, "per_page":` 正是这类。放行等于把坏 JSON 交给下游（报 `Invalid JSON input`）。一律重发更安全，代价是整轮重发的 token 成本。

### 7.6 验证（项目 venv，隔离 stub，脚本已删）

| 场景 | 结果 |
|---|---|
| `finish_reason=tool_calls`（干净） | 正常完成 ✓ |
| `finish_reason=stop`（干净） | 正常完成 ✓ |
| `length` + 正文、无工具调用 | 抛 `TruncatedStreamError` ✓（bug 2） |
| `length` + 无正文 | 抛异常 ✓ |
| 半截流无 payload（抓包 record 15） | 抛异常 ✓ |
| 半截流**带正文** | 抛异常 ✓（§5） |
| 仅 reasoning + tool_calls | 正常完成 ✓ |

端到端重试：第 1 次返回 `length` 截断 → 抛错 → 第 2 次重发成功，`retries fired: 2`，最终拿到 `function_call`。确认异常能穿透到 `get_response` 的 retry 循环。

### 7.7 重跑 108 tasks 时看什么

改动只影响"截断回合的处理方式"，因此**新 run 的 `run.log` 里 `verdict=TRUNCATED` 行数不应明显下降**（诊断只看不改），变的是这些行**之后**发生的事：

1. **`TruncatedStreamError` 重试日志应出现在 `run.log`** —— 形如 `Error in get_response: stream ended without a clean finish_reason ... retry 1/10`。若 108 tasks 里一条都没有，说明没有截断回合（好事），或改动未生效（需查）。
2. **`llm-training-dataset` / `dietary-health` 两道题应不再"静默 success"** —— 原症状是 `finish_reason=['length']` 后任务记 success 但产出为空（空 sheet / 无 analysis.md）。新 run 里这两道若仍失败，失败形态应变成"重试后仍失败"或"200 轮打满"，**而不该是空产出 + success**。
3. **`problem_tasks.txt` 里那 5 道异常题** —— 其中 `llm-training-dataset` 属本次直接受益；另 4 道（canvas / excel / sync-todo / woocommerce）是"推理退化 + 轮次打满"，本次**不修**，预期无变化。
4. **非流式 run 应完全无差异** —— 若 run3 出现任何新波动，说明改动漏出了流式侧，需回查。

**未修的关联项**（预期这些症状重跑后仍在）：
- 位置 4（诊断计数器不统计 tool_call 参数体积）—— 仍是纯观测项，未动；
- excel / sync-todo 的"同一段代码重复提交 200 轮"—— 依 §2 bug 3 结论属推理退化表现，非框架 bug；
- bug 4 的真机验证仍未做。

---

## 附：本报告的核查方法

- 轨迹：`run1_stream/finalpool/<task>/traj_log.json` 的 `messages`，统计 `role` / `tool_calls` / `finish_reason` / `key_stats`。
- 诊断：`run1_stream/finalpool/<task>/run.log` 中的 `[STREAM_DIAG]` 行。
- 抓包：`cc_trace-master/log/cli_api_proxy_messages_2026-09-27_openai-toolathon_1.jsonl`，`response.chunks` 为 SSE 原始字符串，需按 `data: ` 前缀解析后再统计 delta 字段。
