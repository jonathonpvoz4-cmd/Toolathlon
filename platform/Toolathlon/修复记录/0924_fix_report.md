# 流式推理"空回合被误判为任务完成"修复记录

**日期**：2026-09-24
**问题来源**：`/mnt/djc/dumps_public_service/deepseek-v4-flash-0731/run9`（流式，61/108）对比 `run1`（非流式，78/108）
**涉及任务**：`task_files/problem_tasks.txt` 里 21 道"流式错、非流式对"的 task
**改动文件**：`utils/api_model/model_provider.py`（唯一改动文件，`+239 −7`）
**改动后 md5**：`61623498b9abb58c8d8567ac32a17729`
**验证 run**：`/mnt/djc/dumps_public_service/deepseek-v4-flash-0731/run10`（12 道 A 类 task，12 workers）

---

## 一、问题现象

0923 修复了 `reasoning.effort` 的校验错误之后，流式路径能跑了、思考链也保住了，但**在 108 道全量评测上明显掉分**：

| run | 配置 | 成功率 |
|---|---|---|
| run1 | 非流式 | 0.722 |
| run2–run8 | 非流式 | 0.667 – 0.722 |
| **run9** | **流式** | **0.565** |

run9 是明显的离群点。逐题比对后，21 道"流式错、非流式对"的 task 分成两类：

- **A 类（11 道空回合 + 1 道答案错，共复验 12 道，见 1.1）**：流式下任务**提前静默终止**，工作没做完
- **B 类（9 道）**：正常跑完，但模型自己算错/漏读

本记录处理 A 类。

### 1.1 A 类 task 清单（12 道）

`add-bibtex`、`apply-phd-email`、`excel-market-research`、`identify-all-songs`、
`inter-final-performance-analysis`、`interview-report`、`k8s-redis-helm-upgrade`、
`llm-training-dataset`、`nhl-b2b-analysis`、`nvidia-stock-analysis`、`profile-update-online`、`paper-checker`

> **计数口径说明**（避免混淆）：
> - **空终结回合共 11 次**，分布在 **11 道题**上（每题恰好一个，都在最后一回合）。
> - **`paper-checker` 是第 12 道**，但它**没有空回合**——它在 run9 里正常收尾，
>   只是答案错（漏改 `\autoref`）。把它并入 A 类复验是因为它也属于"流式错、非流式对"，
>   且需要一并确认修复没有副作用。
> - 因此复验跑的是 **12 道**（A 类 11 道 + `paper-checker`）。

### 1.2 共同形态

每道题的**最后一个 assistant 回合**都长这样：

```json
{"item_type": "message_output_item",
 "raw_content": {"content": [
   {"type": "reasoning_content", "pure_thinking_str": "...[truncated]\n"}
 ], "role": "assistant", "status": "completed", "type": "message"}}
```

**没有 `output_text` 块，后面也没有 tool call。**

而框架的 system prompt 明确写着：

> "If you believe the task is completed, you can either call the `local-claim-done` tool or **respond without calling any tool to indicate completion. This will immediately terminate the task**"

于是这个空回合被当成"模型主动宣布完成"，循环 break，日志还打印
`Agent phase finished with status: success`。**任务静默死亡。**

---

## 二、根因

### 2.1 直接原因：0922 的"补建 message"副作用

0922 的第一轮改动为了解决"思考链丢失"，在 `_attach_reasoning` 里加了补建逻辑：

```python
if message is None:
    # Reasoning-only turn: the upstream handler emits no message item, so
    # create one in the same shape the non-streaming path produces.
    message = ExtendedResponseOutputMessage(
        id=FAKE_RESPONSES_ID, content=[], role="assistant",
        type="message", status="completed",
    )
    outputs.append(message)
```

上游 `chatcmpl_stream_handler.py` 在"只有思考、没有正文"的回合里**不产出 message item**
（见该文件 242 行 `if state.text_content_index_and_output or state.refusal_content_index_and_output:`）。
0922 补建它是为了给思考链找落脚点 —— 这个目的达到了，但副作用是：

> **一个"模型其实还没说完"的回合，被包装成了一个结构完整、`status="completed"` 的终结消息。**

下游 `run_interaction_loop` 只判断"有 message、无 tool call"，于是判定完成。

### 2.2 为什么偏偏流式才会空回合

非流式路径走 `ConverterWithExplicitReasoningContent.message_to_output_items`，
`message.content` 为空时 `items` 为空，不会伪造出"完整但不含任何输出"的消息。

流式路径则是：**流在思考阶段就结束了**，而这一轮既没吐正文、也没吐 tool call。

### 2.3 为什么流会在思考阶段结束

**上游 handler 从不检查 `finish_reason`。** 看
`.venv/.../agents/models/chatcmpl_stream_handler.py` 的主循环（51–63 行）：

```python
async for chunk in stream:
    ...
    usage = chunk.usage if hasattr(chunk, "usage") else None
    if not chunk.choices or not chunk.choices[0].delta:
        continue
    delta = chunk.choices[0].delta
    # 只累积 delta.content / delta.tool_calls，从不看 finish_reason
```

循环结束后**无条件**发 `response.completed`。所以：

- 一条**正常结束**的流 → `response.completed`
- 一条**被中途掐断**的流 → 同样 `response.completed`

**两者在返回值上完全无法区分。** 这正是"静默死亡"的土壤。

### 2.4 实测：后端本身是正常的

直接请求 `http://115.120.87.159:8082/v1/chat/completions`（`deepseek-v4-flash-0731`），
正常流式返回的顺序是：

```
data: {"choices":[{"delta":{"reasoning_content":"..."},"finish_reason":null}]}   ← 思考
data: {"choices":[{"delta":{"content":"Hello,"},"finish_reason":null}]}          ← 正文
data: {"choices":[{"delta":{},"finish_reason":"stop"}]}                          ← 终结信号
data: {"choices":[],"usage":{...}}                                               ← usage
data: [DONE]
```

被 `max_tokens` 截断时 `finish_reason` 为 `"length"`。**后端该给的信号都给了**,
是我们的 handler 没去读。

### 2.5 证据汇总

| 指标 | run1 | run9 |
|---|---|---|
| 空终结回合数（21 题范围） | **0** | **11**（分布在 11 道题上） |
| `max_sequence_output_tokens` | 619–1826 | **5**（k8s / nhl / llm-training / add-bibtex / identify-all-songs 等） |
| 是否触及 200 turn 上限 | 否 | 否（最大 63/200） |
| 是否调用 `claim_done` | 部分 | 基本没有 |

`max_sequence_output_tokens : 5` 是最直观的指纹：**峰值输出序列只有 5 个 token**,
即整轮里出现过一次"输出几乎为空"的请求。

**最干净的标本 `excel-market-research`**：只跑了 **3 个回合**，最后回合思考 **888 秒**、
生成 **8240 token 全是思考**，然后断在一句话中间：

```
"...Let me use Python with openpyxl to examine the file, and compute the results.
 ... Either works. Let me first "        ← 硬停在这里
```

`content_len=0`、无 tool call。既没超限、没循环、没崩 —— **就是流断了。**

### 2.6 关于 `...[truncated]` 的澄清（重要，避免误诊）

排查中一度以为 `...[truncated]` 是元凶。**它不是。**

- 全仓库该字符串**只出现于一处**：`utils/aux_tools/overlong_tool_manager.py:196` 的搜索工具输出，
  与推理路径无关；也没有任何代码为 reasoning 写这个串。
- **非流式的 run4–run8 同样大量出现**（run4: 1323 次、run5: 1216 次…），
  但成功率保持 0.67–0.72，与 run1 同级。
- 它更接近"长思考回合"的落盘表现，**不是**流式独有的回归，也不是失败原因。

真正区分 run9 的，是**流式独有的 11 个空终结回合**（分布在 11 道题上，每题一个，都在最后一回合）。

---

## 三、改动内容

**唯一改动文件：`utils/api_model/model_provider.py`**（`+239 −7`）

分两层防护：第一层识别"流异常结束"，第二层兜底拦截"空回合"。

### 改动 1：新增 `TruncatedStreamError` —— 第 479 行

```python
class TruncatedStreamError(Exception):
    """The SSE stream ended without the provider ever signalling a clean stop.

    Raised so the retry loop in `get_response` re-issues the request instead of
    handing back a half-finished turn. See `ChatCmplStreamHandlerWithReasoning`.
    """
```

### 改动 2：`ChatCmplStreamHandlerWithReasoning.handle_stream` 追踪 `finish_reason` —— 第 492–552 行

在原有的 tee 循环里，除了累积 reasoning，**额外记录是否收到过终结性 `finish_reason`**：

```python
reasoning_parts: list[str] = []
saw_terminal_finish_reason = False

async def tee() -> AsyncIterator[ChatCompletionChunk]:
    nonlocal saw_terminal_finish_reason
    async for chunk in stream:
        try:
            choices = getattr(chunk, "choices", None)
            if choices:
                delta = choices[0].delta
                if delta:
                    reasoning = _extract_chunk_reasoning(delta)
                    if reasoning:
                        reasoning_parts.append(reasoning)
                # A provider that completes normally sends at least one
                # chunk carrying a non-null finish_reason (e.g. "stop",
                # "tool_calls", "length").
                if getattr(choices[0], "finish_reason", None):
                    saw_terminal_finish_reason = True
        except Exception:
            pass
        yield chunk
```

然后在 `response.completed` 处加判断：

```python
async for event in super().handle_stream(response, tee()):
    if event.type == "response.completed":
        if not saw_terminal_finish_reason and not _response_has_payload(event.response):
            # The stream ended mid-generation and produced nothing usable.
            # Surface it as an error so `get_response` retries rather than
            # returning an empty turn that reads as "task finished".
            raise TruncatedStreamError(
                "stream ended without a finish_reason and produced no "
                "text or tool call (reasoning chars: "
                f"{sum(len(p) for p in reasoning_parts)})"
            )
        try:
            event = cls._attach_reasoning(event, "".join(reasoning_parts))
        except Exception as e:
            logger.warning(f"Failed to attach streamed reasoning content: {e}")
    yield event
```

> **为什么是"两个条件同时满足"才抛错**：只看 `finish_reason` 会误伤那些不发
> `finish_reason` 的兼容后端；只看内容为空又会误伤"模型确实没话说"的极端情况。
> 两者同时成立（**没终结信号 且 没产出任何可用内容**）才是真正的截断。

### 改动 3：新增 `_response_has_payload` / `_model_response_has_payload` —— 第 610、629 行

```python
def _response_has_payload(response: Response) -> bool:
    """True if the response carries a real assistant output.

    A "payload" is visible text or a tool call — the two things the agent loop
    acts on. A message that holds only a reasoning block is *not* a payload:
    the model was still thinking when the turn ended, and the caller must not
    mistake that for a deliberate finish.
    """
    for item in getattr(response, "output", None) or []:
        item_type = getattr(item, "type", None)
        if item_type == "function_call":
            return True
        if item_type == "message":
            for part in getattr(item, "content", None) or []:
                if getattr(part, "type", None) in ("output_text", "refusal"):
                    return True
    return False
```

`_model_response_has_payload` 是同名逻辑的 `ModelResponse` 版本（`get_response` 里用）。

### 改动 4：`get_response` 空回合护栏 —— 第 1071 行

```python
                # A turn with neither text nor a tool call is not a legitimate
                # end-of-task signal — the model was still thinking when the
                # stream stopped. Returning it would make the agent loop exit
                # and mark the task `success` with the work undone, so treat it
                # as a failed attempt and let the retry loop re-issue.
                if not _model_response_has_payload(model_response):
                    raise TruncatedStreamError(
                        "model returned a turn with no text and no tool call "
                        f"(items: {len(output_items)})"
                    )

                return model_response
```

**这一层不依赖 `finish_reason`**，所以即使将来换到行为不同的后端、或上游 handler
再引入别的变化，只要"空回合被当成完成"这个模式还在，护栏就能拦住。

`get_response` 本身已带 10 次重试（`retry_times=10`，间隔 10s），空回合会自动重发。

### 一处顺带改动

`_RESERVED_EXTRA_REQUEST_PARAM_KEYS` 中删掉了 `"stream"`（第 50 行附近）：

```python
     "parallel_tool_calls",
-    "stream",
     "stream_options",
```

这是让配置里的 `"stream": true` 能透传的前提（此前会被过滤掉并打印 warning）。

---

## 四、验证

### 4.1 单元测试：两个 helper 的 6 种形态

| 形态 | `_response_has_payload` | `_model_response_has_payload` | 期望 |
|---|---|---|---|
| 只有 reasoning（**bug 形态**） | False | False | False ✅ |
| 有 output_text | True | True | True ✅ |
| 有 refusal | True | True | True ✅ |
| 有 tool call | True | True | True ✅ |
| 空 | False | False | False ✅ |
| reasoning + tool call | True | True | True ✅ |

### 4.2 端到端：护栏确实会重试并恢复

构造一个会**前两次强行返回"只有思考"**的假 `raw_get_response`，跑真实 `get_response`：

```
   [sim] attempt 1: forced reasoning-only
   [sim] attempt 2: forced reasoning-only
   [sim] attempt 3: real response
  attempts: 3 payload: True
  RESULT: PASS - guard retried and recovered
```

即：**连续两次空回合都被识别、重试，第三次拿到真实响应后正常返回。**
验证了"重试恢复"这条路径，而不仅是"会抛错"。

### 4.3 后端行为确认

直接 curl 实测（见 2.4）：`finish_reason` 在 `stop` / `length` 两种情况下都会正常下发，
usage 与 `[DONE]` 也齐全。改动依赖的信号真实存在。

### 4.4 全量 A 类复验（run10）

- **路径**：`/mnt/djc/dumps_public_service/deepseek-v4-flash-0731/run10`
- **任务**：A 类 12 道（见 1.1）
- **worker**：12
- **命令**：
  ```bash
  nohup sudo bash scripts/run_parallel.sh deepseek-v4-flash-0731 \
      /mnt/djc/dumps_public_service/deepseek-v4-flash-0731/run10 unified 12 \
      > run_parallel.log 2>&1 &
  ```
#### 结果：12 道里 **9 道转绿**（run9 为 0/12）

| task | run9 | **run10** |
|---|---|---|
| add-bibtex | ✗ | ✗ |
| apply-phd-email | ✗ | **✓** |
| excel-market-research | ✗ | **✓** |
| identify-all-songs | ✗ | **✓** |
| inter-final-performance-analysis | ✗ | ✗ |
| interview-report | ✗ | **✓** |
| k8s-redis-helm-upgrade | ✗ | **✓** |
| llm-training-dataset | ✗ | **✓** |
| nhl-b2b-analysis | ✗ | **✓** |
| nvidia-stock-analysis | ✗ | **✓** |
| profile-update-online | ✗ | **✓** |
| paper-checker | ✗ | ✗ |

#### 核心指标对照

| 检查项 | run1 | run9 | **run10** |
|---|---|---|---|
| 空终结回合数（21 题范围） | 0 | **11** | **0** |
| `max_sequence_output_tokens = 5` 的 task 数 | 0 | **6** | **0** |
| 卡在 200 turn 上限 | 否 | 否 | 否 |
| 平均回合数 | — | 22.9 | **45.8** |
| 平均 tool calls | — | 44.1 | **90.6** |
| 12 道通过率 | — | **0/12** | **9/12** |

**逐题的回合数变化最说明问题**（run9 普遍偏少，run10 恢复到与 run1 同级）：

| task | run9 回合 | run10 回合 |
|---|---|---|
| add-bibtex | 15 | 51 |
| identify-all-songs | 11 | 47 |
| nvidia-stock-analysis | 21 | 47 |
| inter-final-performance-analysis | 25 | 52 |
| interview-report | 80 | 127 |
| k8s-redis-helm-upgrade | 63 | 86 |

#### 产出文件：全部补齐

| task | run9 workspace | run10 workspace |
|---|---|---|
| identify-all-songs | 只有 `format.md` | `format.md` + **`songs.md`** |
| excel-market-research | 只有 `Market_Data.xlsx` | + **`segment_growth_rates.xlsx`** |
| nvidia-stock-analysis | 只有 `results_template.xlsx` | **`results.xlsx`** |
| inter-final-performance-analysis | 空 | + **`sheet_url.txt`** + 4 个数据 json |
| add-bibtex | `ref.bib` **53** 条 | `ref.bib` **58** 条（与 run1 一致） |

#### 最终消息形态：全部正常

run10 每一题的最后一个 assistant 回合都带 `output_text`，例如：

- `identify-all-songs`：`"The task is complete. I successfully: 1. Found the video: ..."`
- `excel-market-research`：`"The task is complete. I've created the segment_growth_rates.xlsx..."`
- `add-bibtex`：`"All 5 articles have been successfully added to ...ref.bib..."`

**空回合形态完全消失。**

#### 重要说明：本次 run10 里护栏并未触发

精确检索 run10 全部日志：

```
TruncatedStreamError          -> 0 hits
no text and no tool call      -> 0 hits
Failed to get response after  -> 0 hits
retry N/10, waiting           -> 0 hits
```

**12 条流全部正常结束，护栏一次都没用上。**

这意味着两种可能，**必须如实区分**：

1. **修复确实消除了问题**：改动让流式路径不再产生空回合；
2. **流式本身就有随机性**：run9 的 12 个空回合可能是服务端偶发的截断，本次恰好没复现。

**当前证据支持"两者都有，但不全是修复的功劳"**：

- 支持 (1)：run10 的回合数普遍翻倍（15→51、11→47），达到 run1 同级——
  说明任务确实在正常推进，而不是靠重试兜住的。
- 支持 (2)：**护栏本身没被触发过**，所以无法证明"护栏救回了这些题"。
  run10 的转绿更可能来自"这次流没被截断"，而非"被截断后救回来了"。

**要严格区分这两种可能，需要补一个针对性验证**：强制让流提前中断（或注入一次空回合），
确认护栏触发 → 重试 → 任务仍能完成。本机已做等价的模拟验证（见 4.2：前两次强行空回合、
第三次恢复），但那是在 `get_response` 层面，**没有跑完整 task**。

> **结论口径**：可以说"12 道 A 类题的失败形态（空终结回合）在 run10 中完全消失，
> 9/12 转绿"；**不能**说"护栏救回了 9 道题"——护栏本次未触发。

#### 剩余 3 道失败的原因（均为模型侧，护栏不覆盖）

| task | 失败原因 | 归类 |
|---|---|---|
| `add-bibtex` | 引用 key 生成成 `yuan2023scalingrelationship` / `roziere2024codellama`，与 groundtruth 的 `yuan2023scaling` / `roziere2023code` 不匹配（年份也错成 2024）。**文件已完整写入 58 条**，是命名问题 | 模型侧 |
| `inter-final-performance-analysis` | 数据抓到了、`sheet_url.txt` 也写了，但 `Blocked` 字段算出 `0.0`（应为 `2.0`）——抓取/解析错误 | 模型侧 |
| `paper-checker` | 仍漏改 `5_tradeoff.tex` 的 `\autoref{tab:compute-cost}`（写成 `tab:api-benchmarks`）。与 run9 同一处错误 | 模型侧 |

三道的最终回合都**有完整正文**、都正常收尾，**不属于**本次修复的空回合范畴。
`paper-checker` 与 run9 错在同一处，说明是稳定的模型行为倾向（该 `\autoref` 语义检查它不做）。


---

## 五、排查中发现但未处理的问题

### 5.1 `final_output` 恒为空（独立老问题，非本次引入）

`_attach_reasoning` 把 reasoning 块**追加在最后**，而 SDK 的
`ItemHelpers.extract_last_text`（`agents/items.py:200`）取的是 `message.content[-1]`：

```python
def extract_last_text(cls, message):
    if isinstance(message, ResponseOutputMessage):
        last_content = message.content[-1]
        if isinstance(last_content, ResponseOutputText):
            return last_content.text
    return None
```

于是 `result.final_output` 拿到的是 reasoning 块 → 返回 `None`。

**实测确认这是两种情况都有的老问题**：

```
  stream=False: final_output=''
  stream=True:  final_output=''
```

所以它**不是** run9 掉分的原因（否则 run1 也会挂）。但副作用值得知道：
`conversation_history` 里那些 `content_len=0` 的中间回合，未必代表模型没说话,
只代表正文没被 `final_output` 取到。

同一处还有 `utils/roles/context_managed_runner.py:357` 的 `claim_done` 分支，
用的是同一个 `extract_last_text`。

**建议**：把 reasoning 块插到正文**之前**（`content.insert(0, ...)`），
与非流式顺序保持一致的语义。改动小，但会影响落盘格式，**本次未动**。

### 5.2 B 类 9 道题的原因（模型侧，非管线）

| task | 真实原因 |
|---|---|
| `canvas-list-test` | 收了 10 个 quiz，少 3 个（两边都调了 `claim_done`） |
| `canvas-new-students-notification` | 邮件标题没过评测关键词过滤 |
| `courses-ta-hws` | 选了 78 个文件（应 100 个） |
| `cvpr-research` | 主动排除了正确的三位学者 |
| `paper-checker` | 漏改 `5_tradeoff.tex` 的 `\autoref` |
| `quantitative-financial-analysis` | `end_date` 当开区间，少一天 → 164 行 vs 168 行 |
| `travel-expense-reimbursement` | 漏读客户招待字段，未套 1.5× 例外 |
| `verl-dataset` | 把嵌套 schema 拍平了 |
| `woocommerce-product-recall` | 11 个字段一轮并发发出，prepend 服务端 race 打乱顺序 |

其中 `woocommerce` 与 `canvas-new-students-notification` 都指向同一件事：
**流式下模型更倾向把多个 tool call 攒进一个回合并发发出**，遇到服务端有顺序语义时出错。
若后续要提升流式表现，可考虑对流式配置收窄 `parallel_tool_calls`，或对有顺序要求的
任务禁用并行。**本次未动。**

### 5.3 排查过程中引入并已修复的失误

在第一轮定位时，为缩小验证范围把 `task_files/all_tasks.txt` 覆盖成了 12 行的 A 类清单，
**但覆盖前没备份**，原始 108 行清单丢失（该文件未被 git 跟踪）。

**已恢复**：从 run1 的 `eval_stats.json`（`status_breakdown.preprocess.done_tasks` 107 项
+ `fail_tasks` 1 项 = 108）重建，并与 run1/run9 两个 `finalpool` 目录逐一 diff，
**108 项完全一致**。完整清单另存于
`task_files/all_tasks_full_108.txt`。

**教训**：改这类"输入清单"文件前先 `cp` 一份，不要依赖 git（本目录大量文件未被跟踪）。

---

## 六、相关配置与路径速查

| 项 | 路径 |
|---|---|
| 本次改动文件 | `/mnt/djc/Toolathlon/utils/api_model/model_provider.py` |
| 流式 handler | 同上，`ChatCmplStreamHandlerWithReasoning` 第 487 行 |
| 截断判断 | 同上，第 543 行 |
| 空回合护栏 | 同上，`get_response` 第 1071 行 |
| payload 辅助函数 | 同上，第 610、629 行 |
| 上游 handler（含缺陷） | `/mnt/djc/Toolathlon/.venv/lib/python3.12/site-packages/agents/models/chatcmpl_stream_handler.py` |
| `extract_last_text`（5.1 问题） | `/mnt/djc/Toolathlon/.venv/lib/python3.12/site-packages/agents/items.py:200` |
| 推理配置文件 | `/mnt/djc/Toolathlon/scripts/temp_configs/deepseek_v4_flash_thinking.json` |
| 评测启动脚本 | `/mnt/djc/Toolathlon/scripts/run_parallel.sh` |
| A 类清单 | `/mnt/djc/Toolathlon/task_files/all_tasks.txt`（12 行） |
| 完整清单（108） | `/mnt/djc/Toolathlon/task_files/all_tasks_full_108.txt` |
| 问题 task 清单 | `/mnt/djc/Toolathlon/task_files/problem_tasks.txt`（21 行） |
| 基准 run（非流式） | `/mnt/djc/dumps_public_service/deepseek-v4-flash-0731/run1/finalpool/` |
| 问题 run（流式） | `/mnt/djc/dumps_public_service/deepseek-v4-flash-0731/run9/finalpool/` |
| 复验 run（流式） | `/mnt/djc/dumps_public_service/deepseek-v4-flash-0731/run10/finalpool/` |
| 0922 修复记录 | `/mnt/djc/Toolathlon/修复记录/0922_fix_report.md` |
| 0923 修复记录 | `/mnt/djc/Toolathlon/修复记录/0923_fix_report.md` |

---

## 七、三段修复的关系

| 记录 | 修的问题 | 是否引入新问题 |
|---|---|---|
| 0922 | 流式丢失思考链 → 模型自我重复烧穿 128k | 补建 message 埋下本次的雷 |
| 0923 | 流式构造 `Response` 时校验 `reasoning.effort` 报错 | 无（该字段无消费方，纯删除） |
| **0924（本次）** | 流被截断 → 空回合被误判为任务完成 → 任务静默死亡 | 待 run10 复验确认 |

三次改动都集中在同一个文件、同一条流式路径上。**0922 和 0923 的修复本身都是对的**
（run9 的思考链完整、校验错误归零，均已复核），本次是补上前两轮遗漏的第三种失败模式。
