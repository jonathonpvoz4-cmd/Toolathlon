# tool 定义出现 null —— `required` 缺失导致整请求被网关拒绝

**日期**：2026-09-28
**问题现象**：推理时报 `400 Invalid schema for function '<name>': null is not of type "array"`，工具定义被网关判为 null，**整次请求在模型看到之前就被拒**（一个坏 tool 拖垮全部 tool）。
**改动文件**：
- `utils/openai_agents_monkey_patch/`（整目录替换）
- `utils/roles/task_agent.py`（2 处改动）

---

## 0. 结论摘要

- 真正的**内容修复**是 `custom_mcp_util.py` 里新增的 `_ensure_object_required()`：给每个 object 子 schema 补上 `"required": []`。
- `task_agent.py` 新增对该 helper 的调用，把它从"MCP 工具专用"扩展到**所有**模型可见工具（含本地 `FunctionTool`），收敛到唯一的 `parameters` 构建点。
- **⚠️ 目录替换这一步是空操作（内容层面）**：见 §4，两份目录逐字节相同，diff 只有文件权限位。

---

## 1. 根因

多家 provider（含本项目的 eval 网关）对 `parameters` 做严格 JSON Schema 校验，**把缺失的 `required` 读作 null**，于是报：

```
400 Invalid schema for function '<name>': null is not of type "array"
```

MCP server 对无参工具（如 `terminal` 的 `show_security_rules`）按规范**可以省略 `required`**，OpenAI 侧不接受。2026-09-13 实测对照：

| `parameters` | 结果 |
|---|---|
| `{"type":"object","properties":{}}` | HTTP 400 |
| `{"type":"object"}` | HTTP 400 |
| `{"type":"object","properties":{},"required":[]}` | HTTP 200 |

**拒绝粒度是"每个请求"**：只要有一个工具 schema 缺 `required`，整个请求失败，模型一个工具都看不到。曾导致 94/108 个 task 首轮即死（全部挂了 `terminal` server 的 task）。

---

## 2. 改动一：`utils/openai_agents_monkey_patch/custom_mcp_util.py`

### 2.1 新增 `_ensure_object_required()`（第 123–160 行）

递归、原地、幂等地为每个 object 子 schema 补 `required`：

```python
def _ensure_object_required(schema: Any) -> None:
    if isinstance(schema, list):
        for item in schema:
            _ensure_object_required(item)
        return
    if not isinstance(schema, dict):
        return

    if schema.get("type") == "object" and "required" not in schema:
        schema["required"] = []

    for key in ("properties", "patternProperties", "definitions", "$defs"):
        child = schema.get(key)
        if isinstance(child, dict):
            for sub in child.values():
                _ensure_object_required(sub)
    for key in ("items", "additionalProperties", "not", "contains",
                "propertyNames", "if", "then", "else"):
        _ensure_object_required(schema.get(key))
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        _ensure_object_required(schema.get(key))
    return
```

设计要点：

- **递归到嵌套层** —— 嵌套 object 会被同样拒绝，只修根节点会漏掉 array-of-objects 这类参数。
- **幂等** —— 已带 `required` 的 schema 原样不动（空数组和有内容都合法），可重复调用。
- **原地修改** —— 无返回值，调用方直接用改过的对象。

### 2.2 `my_to_function_tool()` 内调用（第 189 行）

在 `"properties" not in schema → schema["properties"] = {}` 之后、`ensure_strict_json_schema` 之前插入 `_ensure_object_required(schema)`。此处只覆盖 **MCP 工具**，这正是 §3 还要在 `task_agent.py` 补一刀的原因。

---

## 3. 改动二：`utils/roles/task_agent.py`

### 3.1 新增 import（第 63 行）

```python
from utils.openai_agents_monkey_patch.custom_mcp_util import _ensure_object_required
```

### 3.2 `_build_tools()` 循环内调用（第 561 行）

```python
available_tools = await self.agent.get_all_tools()
validate_model_tool_names(available_tools)
for tool in available_tools:
    _ensure_object_required(tool.params_json_schema)   # ← 新增
    self.all_tools.append({
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.params_json_schema
        }
    })
```

**放在这里的原因**：这是 `parameters` payload **唯一的构建点**，一处同时覆盖本地工具和 MCP 工具。MCP 工具已在 §2.2 归一化过，这里重复调用无副作用（幂等）；本地 `FunctionTool` 此前完全没有归一化，正是漏网之鱼。

### 3.3 ⚠️ 同文件另有一处未在本次说明中提及的改动

`git diff` 显示 `task_agent.py` 今天还有一处改动，**不属于本次 null 问题**，一并记下备查：

```diff
@@ class TaskAgent.__init__
-        self.termination_checker = termination_checker or self._default_termination_checker
+        self.termination_checker = self._default_termination_checker

@@ _default_termination_checker
         if check_target == 'user':
             return '#### STOP' in content
+        if check_target == 'agent':
+            for tool_call in recent_tools:
+                if 'claim_done' in tool_call['function']['name']:
+                    return True
         return False
```

注意这**忽略了传入的 `termination_checker` 参数**，即外部自定义的终止检查器会被覆盖。若调用方依赖注入，需要复查。

---

## 4. ⚠️ 目录替换的实际效果：只有权限位变了

按要求把 `utils/openai_agents_monkey_patch/` 替换为
`/mnt/djc/Toolathlon_for_unitool/Toolathlon_old_version/utils/openai_agents_monkey_patch`。
比对结果：

| 文件 | 两侧 md5 | `git diff` |
|---|---|---|
| `custom_mcp_util.py` | `4d70b772…` **相同** | 内容有 diff（vs HEAD）|
| `custom_run_impl.py` | `8934da99…` **相同** | 0 行 |
| `test_tool_adapters.py` | `e0b5faba…` **相同** | 0 行 |
| `tool_name_aliases.py` | `c25ce830…` **相同** | 0 行 |

- 四个文件**逐字节相同**，`diff -ru` 只报 `Only in ...: __pycache__`（源目录多一个 pycache，未复制）。
- 本次仓库里 `custom_mcp_util.py` 的 diff 内容（新增 helper + 调用）**替换前就已存在**——是更早一次改动留下的未提交内容，不是这次拷进来的。
- **唯一的实际变化是权限位 644 → 755**（`custom_run_impl.py` / `test_tool_adapters.py` / `tool_name_aliases.py` 的 `git diff --stat` 为 0 行即此意）。

**影响**：如果哪天需要回退，回退目录 = 回退内容这条路走不通；真正要回退的是 `custom_mcp_util.py` 里新增的那 56 行，以及 `task_agent.py` 的两处改动。

---

## 5. 待验证项

1. **重跑确认 400 消失** —— `run.log` 中不应再出现 `null is not of type "array"`。
2. **首轮即死的 task 应复活** —— 原 94/108 个挂 `terminal` server 的 task 应能进入正常工具调用。
3. **本地工具路径** —— 确认 `task_agent.py` 的调用确实覆盖到了 MCP 之外的 `FunctionTool`（此前未归一化的那一类）。
4. **§3.3 的 termination_checker 改动** —— 确认没有调用方依赖注入自定义 checker。

---

## 附：本报告的核查方法

```bash
# 目录内容比对（结论：逐字节相同）
diff -ru /mnt/djc/Toolathlon/utils/openai_agents_monkey_patch \
         /mnt/djc/Toolathlon_for_unitool/Toolathlon_old_version/utils/openai_agents_monkey_patch
md5sum /mnt/djc/Toolathlon/utils/openai_agents_monkey_patch/*.py

# 实际改动（vs HEAD）
git diff utils/openai_agents_monkey_patch/custom_mcp_util.py
git diff utils/roles/task_agent.py
git diff --stat utils/openai_agents_monkey_patch/   # 0 行者 = 仅权限位变化

# helper 全部引用点
grep -rn "_ensure_object_required" --include=*.py .
```
