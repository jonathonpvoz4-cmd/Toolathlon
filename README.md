# Toolathlon

## 准备工作

### 第一步：环境安装

#### uv

Make sure you have [uv](https://github.com/astral-sh/uv) installed, otherwise please install it:

```
# this is for macOS and linux command
# by default it will install uv to $HOME/.local/bin
# you probably need to add it to your $PATH
curl -LsSf https://astral.sh/uv/install.sh | sh

# check whether uv can be found
which uv
```

```
bash global_preparation/install_env_minimal.sh [true|false] # `true` if you have sudo.
```

#### Google Cloud
We recommand register a new Google (gmail) account for Toolathlon evaluation. You can use this google account for most of the services below such as wandb, notion, snowflake, etc.

First make sure you have `gcloud` sdk installed, e.g.:
```bash
curl -O https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-sdk-456.0.0-linux-x86_64.tar.gz
tar -xf google-cloud-sdk-*-x86_64.tar.gz
./google-cloud-sdk/install.sh
source ~/.bashrc # or ~/.zshrc
```


### 第二步：账号准备

#### configs/token_key_session.py

##### Google

```python
google_cloud_console_api_key = ""
gcp_project_id = ""
gcp_service_account_path = "configs/gcp-service_account.keys.json"

google_client_id = google_credentials.get("client_id", "")
google_client_secret = google_credentials.get("client_secret", "")
google_refresh_token = google_credentials.get("refresh_token", "")

google_sheets_folder_id = "XX"
google_oauth2_credentials_path = "configs/google_credentials.json"
google_oauth2_token_path = "configs/google_credentials.json"
```

这些参数的值/文件需要跑：
```bash
bash global_preparation/automated_google_setup.sh
```
这个脚本来自动赋值和创建需求的文件到`configs`文件夹内

##### Github

```python
github_token = ""
```

##### Huggingface

```python
huggingface_token = ""
```

##### Wandb

```python
wandb_api_key = ""
```

##### Notion

```python
notion_integration_key = ""
notion_integration_key_eval = ""
source_notion_page_url = ""
eval_notion_page_url = ""
```

##### SnowFlake

```python
snowflake_account = ""
snowflake_warehouse = "COMPUTE_WH"
snowflake_role = "ACCOUNTADMIN"
snowflake_user = ""
snowflake_private_key_path = snowflake_private_key_path
snowflake_database = "SNOWFLAKE"
snowflake_schema = "PUBLIC"
snowflake_op_allowed_databases = "null"
```

snowflake需要提供`snowflake_account`, `snowflake_user` 和 `snowflake_private_key_path`, 其中`snowflake_private_key_path` 是rsa_key，具体参考configs/snowflake_rsa_key.pub, configs/snowflake_rsa_key.p8


##### other keys

```python
serper_api_base_url = ""
serper_api_key = ""

web_summary_tool_base_url = ""
web_summary_tool_api_key = ""
```

这两key用于工具的使用

账号准备的教程详细参考：
https://github.com/hkust-nlp/Toolathlon/blob/main/global_preparation/how2register_accounts.md


### 第三步：Notion账号设定
找到`configs/.mcp-auth/notion.env`，填写以下三项：

```
NOTION_ACCOUNT_EMAIL=
NETEASE_163_ADDRESS=
NETEASE_163_AUTH_CODE=
```

Notion账号必须使用网易163邮箱。必须开启IMAP/SMTP服务以支持Notion自动验证脚本。


## 运行评测
运行:
```
python scripts/n_run.py \
    --N \
    --model_name \
    --result_output_path \
    --base-url \
    --api-key \
    --config-file \
```

参数说明：

- `N`: 跑几轮评测
- `model_name`: 用于测试的模型名称
- `result_output_path`: 存放推理结果的路径
- `max_workers`: 并发数，同时推理多少个任务
- `base-url`: 模型API的url
- `api-key`: 模型API的key
- `config-file`: 模型的推理配置文件，内容可参考：`scripts/temp_configs/pangu_92B.json` 提供的路径必须是相对路径，必须存放于Toolathlon路径里。比如我要使用`scripts/temp_configs/pangu_92B.json`这个配置文件，就提供`scripts/temp_configs/pangu_92B.json`这个相对路径就行

## 推理结果
推理完成后的结果文件夹会存放在`result_output_path`里面，根据跑多少轮，会存放多少个文件夹，以run1, run2, ..., runN命名。

以`run1`为例，其目录结构如下：

```
result_output_path/
└── run1/
    ├── eval_stats.json                            # 本轮评测统计总览
    ├── execution_report_finalpool_<模型名>_full.json  # 本轮总体执行报告
    ├── eval_res_all.jsonl                         # 所有任务的评测结果汇总
    ├── traj_log_all.jsonl                         # 所有任务的推理轨迹汇总
    ├── run_all.log                                # 本轮总运行日志
    ├── stdout.log                                 # 本轮 stdout 输出
    ├── container_all.log                          # 所有任务容器日志汇总
    └── finalpool/                                 # 每个任务一个子文件夹
        └── <任务名>/
            ├── status.json                # 任务状态
            ├── eval_res.json              # 该任务的评测结果
            ├── eval_temp.json             # 评测中间结果（仅部分任务生成）
            ├── traj_log.json              # 该任务的完整推理轨迹
            ├── run.log                    # 该任务的容器化运行日志
            ├── workspace/                 # 任务的工作目录与初始文件
            └── conversation_history/      # 会话历史
```

各文件的含义说明：

**run 顶层的汇总文件**

- `eval_stats.json`：本轮评测的统计总览，包含`total_tasks`（任务总数）、`average_success_rate`（平均成功率）、`average_turns`（平均轮次）、`average_tool_calls`（平均工具调用次数）、`status_breakdown`（preprocess / running / evaluation 三个阶段的 done、fail、timeout、pass、fail 数量及对应的任务名单）、`summary`（有效轮次统计、所用的 config 与任务清单文件）。
- `execution_report_finalpool_<模型名>_full.json`：本轮总体执行报告，包含`execution_time`（执行时间）、`configuration`（tasks_folder、model_short_name、provider、maxstep、workers、timeout 等）、`summary`（passed / failed / not_executed / error 的数量与 pass_rate）、`details`（passed_tasks、failed_tasks 等详细名单）。
- `eval_res_all.jsonl`：所有任务评测结果的汇总，每个任务一行，格式为`{"pass": true}`，失败时附带`"failure"`字段说明失败原因。
- `traj_log_all.jsonl`：所有任务推理轨迹的汇总。
- `run_all.log`：本轮所有任务的总运行日志。
- `stdout.log`：本轮执行过程的 stdout 输出。
- `container_all.log`：所有任务容器运行日志的汇总。

**单个任务的文件夹**（`finalpool/<任务名>/`）

- `status.json`：该任务三个阶段（`preprocess` / `running` / `evaluation`）的状态，例如`{"preprocess": "done", "running": "done", "evaluation": true}`。
- `eval_res.json`：该任务的评测结果，`pass`为 true 或 false，并附`details`/`failure`说明。
- `eval_temp.json`：评测过程中的中间临时结果（仅部分任务会生成）。
- `traj_log.json`：该任务的完整推理轨迹，包含`config`（任务配置）、`initial_run_time`/`completion_time`（起止时间）、`tool_calls`（工具调用记录）、`status`、`messages`（完整消息历史）、`key_stats`、`agent_cost`/`user_cost`（token 消耗）、`session_id` 等。
- `run.log`：该任务的容器化运行日志，记录了启动参数与每一步的执行过程。
- `workspace/`：该任务的工作目录，包含初始文件（如`files/`）及工具产生的临时目录。
- `conversation_history/`：会话历史文件`task_<任务id>_session_history.jsonl`，逐条记录`turn`、`type`、`content`、`timestamp`等。

