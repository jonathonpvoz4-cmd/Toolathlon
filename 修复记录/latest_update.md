# N 次自动评测编排 —— 进展记录

**日期**：2026-10-21

## 0. 结论摘要

新增了一个「自动跑 N 次评测」的编排器 `scripts/n_run.py`，每次评测前复刻原手工三步流程（部署容器 → 强刷 Notion token → 并行评测），每次结果落到 `result_output_path/model_name/run{i}/`。配套新增一个 n_run 专用的评测入口 `scripts/run_parallel_for_n_runs.sh`，支持动态透传 3 个参数（BASE_URL / API_KEY / CONFIG_FILE），原有的 `scripts/run_parallel.sh` 保持原样供手工流程使用。

- 手动三步流程不变，`n_run.py` 只在其上做「套壳编排」。
- 动态参数改用 **CLI 命名参数** 传递（而非环境变量），原因见 §5。

---

## 1. 目标

原来跑一次完整评测要手工依次执行：

```bash
sudo bash global_preparation/deploy_containers.sh true
bash scripts/bootstrap_notion_official.sh
sudo bash scripts/run_parallel.sh <model> <dump> unified <workers>
```

需要重跑 N 次（N 个 `run1..runN` 结果集）时，纯手工极易遗漏某一步、或 token 过期。目标是把这 N 轮串起来自动跑。

---

## 2. 新增 / 改动文件

| 文件 | 状态 | 作用 |
|---|---|---|
| `scripts/n_run.py` | 新增 | N 次编排器（Python） |
| `scripts/run_parallel_for_n_runs.sh` | 新增 | 评测入口，在 `run_parallel.sh` 基础上增加 3 个命名参数解析 |
| `scripts/run_parallel.sh` | 保持原样 | 手工流程仍用这个 |

---

## 3. `n_run.py` —— N 次编排器

### 3.1 参数

```bash
python3 scripts/n_run.py N model_name [result_output_path] [max_workers] \
    [--base-url URL] [--api-key KEY] [--config-file PATH]
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `N` | 必填 | 跑几轮 |
| `model_name` | 必填 | 被测模型名 |
| `result_output_path` | `/mnt/djc/dumps_public_service` | 结果根目录 |
| `max_workers` | `10` | 每轮最大并发 |
| `--base-url` | 不传→用脚本内置默认 | 覆盖 `TOOLATHLON_OPENAI_BASE_URL` |
| `--api-key` | 不传→用脚本内置默认 | 覆盖 `TOOLATHLON_OPENAI_API_KEY` |
| `--config-file` | 不传→用脚本内置默认 | 覆盖 `CONFIG_FILE_ARG` → `CONFIG_FILE` |

另有 `--dry-run`：只打印计划、解析出的 run 目录，不执行任何命令。

### 3.2 每轮流程（严格复刻手工三步）

1. `sudo bash global_preparation/deploy_containers.sh true`
2. `bash scripts/bootstrap_notion_official.sh`（**不带 sudo**，与手工一致）
3. `sudo bash scripts/run_parallel_for_n_runs.sh <model> <run_dir> unified <workers> [三个 --flag]`

### 3.3 结果目录

`N=3` 时生成：

```
<result_output_path>/<model_name>/run1/
<result_output_path>/<model_name>/run2/
<result_output_path>/<model_name>/run3/
```

每轮 run 目录内除评测产物外，额外落 3 个步骤日志：

- `deploy.log` —— 步骤 1 输出
- `bootstrap_notion.log` —— 步骤 2 输出
- `run_parallel.log` —— 步骤 3 输出（实时进度镜像；`run_parallel_for_n_runs.sh` 自己还 tee 一份到 `stdout.log`）

### 3.4 失败策略

- 步骤 1（部署）或步骤 2（Notion）**非零退出 → 中止整个 N 轮**（后面的轮次都依赖基础设施，继续没意义）。
- 步骤 3（评测）退出码非零 → **记录并继续下一轮**：因为 `run_parallel_for_n_runs.sh` 里 `$?` 抓的是 `tee` 的退出码（脚本无 `pipefail`），且单任务结果已落盘，停下来没必要。
- 每轮都做步骤 2 的强刷，避免 N 轮串行跨过 token 的 8h 有效期而翻车。

---

## 4. `run_parallel_for_n_runs.sh` —— 动态参数透传

脚本顶部加了一段解析循环，先把这 3 个命名参数从 `$@` 里摘掉、赋给对应变量，其余位置参数 `$1..$9` 含义不变：

| 命名参数 | 赋给 | 之后 |
|---|---|---|
| `--base-url URL` | `TOOLATHLON_OPENAI_BASE_URL` | 照旧 `export` |
| `--api-key KEY` | `TOOLATHLON_OPENAI_API_KEY` | 照旧 `export` |
| `--config-file PATH` | `CONFIG_FILE_ARG` → `CONFIG_FILE` | 照旧 |

优先级：

- URL / KEY：**命令行 flag > 环境变量 > 内置默认值**（`"${VAR:-默认}"`）。
- 配置：`CONFIG_FILE_ARG` = **flag / 环境变量 > 第 6 个位置参数 > 空**，再 `CONFIG_FILE = "${CONFIG_FILE_ARG:-scripts/temp_configs/pangu_92B.json}"`。

不传任何 flag 时，行为与原来完全一致。

---

## 5. 关键设计决策

### 5.1 为什么用 CLI 参数而不是环境变量

`sudo` 默认会重置环境，直接用 `n_run.py` 的子进程环境把它们传给 `sudo bash` 会被丢掉。本想用 `sudo env K=v bash ...` 注入，但**实测 `/home/djc/.local/bin/env` 是个非标准 shell 脚本，遮蔽了系统的 `/usr/bin/env`**，`env K=v cmd` 会静默失败（退出码 0、无任何输出）。于是改成 CLI 命名参数——参数天然能穿透 `sudo`，不依赖 `env` 二进制，最稳。

### 5.2 API key 脱敏

`--api-key=xxx` 会以 argv 形式存在，`n_run.py` 写日志时把命令头里的 `--api-key=...` 替换成 `--api-key=***`。但评测运行期间进程列表（`ps`）里仍可见明文 key——暴露程度与原脚本把 key 明文写在文件里相当；若介意可改为从文件读取，尚待确认。

### 5.3 sudo 处理

- 部署 + 评测用 `sudo`（与手工一致）；Notion 强刷**不用** `sudo`（与手工一致）。
- 脚本会自动检测 `geteuid()==0`，已是 root 时省略 `sudo`。
- 要求 `sudo` 免密，或该进程有 TTY 能交互输入密码。

---

## 6. 验证情况

- `bash -n scripts/run_parallel_for_n_runs.sh` 通过。
- `python3 -m py_compile scripts/n_run.py` 通过。
- `python3 scripts/n_run.py --dry-run ...` 计划打印正常，run 目录解析正确（含带 `/` 的 model_name、缺省 result_output_path / max_workers 的情况）。
- 用复制出来的解析逻辑单独测过：无 flag 落到内置默认；传 flag 正确覆盖；只传 `--config-file` 只覆盖 config、URL/KEY 走默认；位置参数 `$1..$4` 均保留正确。
- 命令头脱敏验证：`--api-key=sk-SECRET` 在日志/控制台输出为 `--api-key=***`。

> 尚未做「真实跑一轮完整评测」的 E2E 验证（需 `sudo` 免密 + 真实网关），留待第一次正式开跑确认。

---

## 7. 使用方式

```bash
cd /mnt/djc/Toolathlon

# 最常用：跑 3 轮，其余全默认
python3 scripts/n_run.py 3 <model_name>

# 指定结果目录与并发
python3 scripts/n_run.py 3 <model_name> /mnt/djc/dumps_public_service 8

# 动态覆盖网关/密钥/配置
python3 scripts/n_run.py 3 <model_name> \
    --base-url http://...:3000/v1 \
    --api-key sk-xxxx \
    --config-file scripts/temp_configs/xxx.json

# 先 dry-run 看计划，不执行
python3 scripts/n_run.py --dry-run 3 <model_name>
```

---

## 8. 遗留 / 待办

- [ ] **真实 E2E 验证**：首轮正式开跑后确认部署/Notion/评测三步在自动串接下都正常（尤其 `sudo` 免密是否就绪）。
- [ ] **API key 的 `ps` 可见性**：如介意，可改为 n_run.py 把 key 写到临时文件、脚本内 `source` 读取。
- [ ] **陈旧 config 文件**：`run_parallel_for_n_runs.sh` 里 `CONFIG_FILE` 默认仍是 `scripts/temp_configs/pangu_92B.json`（已存在，不会被按新模型名重新生成）。已核对：`run_parallel.py` 会用 `--model_short_name`（= `model_name`）覆盖 config 里的 `agent.model.short_name`，所以真正跑的模型是对的；但该文件里残留的 `max_turns:200`、`stream:false` 等字段仍会生效，需要时再处理成按模型干净重生成。
- [ ] **VIRTUAL_ENV 环境不一致（非阻断）**：见 `notion_auto_auth_update_1014.md` §4，`uv run` 会有 warning，不影响结果。