# Toolathlon：从 MCPHub 工作区取得账号

本功能在 `mcphub-v2` 和 Toolathlon 中实现。agent-atom-test 暂不改动。
直连模式原有的配置入口不变。Hub 模式必须使用下述启动包装器，避免只有 MCP 走 Hub、预处理仍读取另一套账号。

## 1. 在 Hub 中配置

新版优先在「服务器 → 对应 MCP → 编辑 → 启动账号」配置。点击新增账号，名称如 `account-a`；后续同空间 MCP 会自动显示此账号。连接方式仍为按任务，账号只是启动凭据的选择。

下述「凭据 → 工作区账号配置」仍作为高级配置入口保留。
选择允许获取这套凭据的调用分组。该组还需要原有的 Toolathlon MCP 调用权限，以及公共 Serper 权限。
分组的「允许使用账号」权限包含读取这套凭据用于预处理和评分；不要把这种组 key 当成只可调用工具的无敏感权限 key。

任务连接 JSON 声明字段、说明和公开默认值，网页自动生成表单。新增 account-b 复用相同字段，不复制 account-a 的密钥；无需重复导入 MCP、账号模板或凭据文件。

只有使用高级凭据入口时才需导入 scripts/mcphub/workspace-template.json。Google Maps、YouTube 常驻 MCP 的环境变量绑定随预设创建：

| MCP | MCP 环境变量 | 工作区变量 |
| --- | --- | --- |
| google_map | GOOGLE_MAPS_API_KEY | google_cloud_console_api_key |
| youtube | YOUTUBE_API_KEY | google_cloud_console_api_key |

在对应 MCP 的「按任务连接 → 使用启动账号」中直接填写。复杂授权可展开「预览 / 编辑完整配置」粘贴已有 JSON；普通表单和完整配置编辑的是同一份内容。

| 任务 MCP | 需要填写 | 固定或自动处理 |
| --- | --- | --- |
| huggingface | Hugging Face Token | 任务参数由原脚本设置 |
| github | GitHub Token | 仓库范围、只读标志按任务设置 |
| wandb | W&B API Key | 原有项目/任务逻辑不变 |
| google_calendar / google_sheet / google_forms | Client ID、Client Secret、Refresh Token；Access Token 可选 | 三者共用同账号 Google 授权；scopes、token_uri 已预置，Calendar 客户端文件自动生成 |
| google-cloud | 项目 ID 和服务账号各字段，或粘贴服务账号 JSON | type、auth_uri、token_uri 等公开默认值已预置 |
| snowflake | 账号、用户名、角色、Warehouse、完整私钥 | 角色和 Warehouse 提供原配置默认值，可修改 |
| notion | 两个 Integration Key、两个父页面 URL；登录邮箱及所用邮箱的授权凭据 | OAuth Token、客户端和浏览器状态均有预置 JSON 结构，可编辑；首次授权仍使用原有授权脚本 |
| canvas / emails / k8s / woocommerce | 无需填固定上游 Key | 任务配置与原脚本提供账号/域名/邮箱配置/kubeconfig，继续按任务注入 |
| arxiv-latex / arxiv_local / excel / filesystem / git / memory / pdf-tools / playwright_with_chunk / pptx / terminal / word | 无账号字段 | 使用原任务目录和状态 |

首次 Google OAuth 授权仍需原项目工具完成，将产生的 client_id/client_secret/refresh_token 填入表单，或在 JSON 预览粘贴完整授权。修改 scopes 字符串不会自动扩大真实授权。
Notion Integration Key 不等于 MCP OAuth Token。已有 OAuth 状态可以在预览中粘贴；原脚本登录后更新的状态仍自动回存。
保存后管理员重新打开同一账号，可查看和编辑已保存内容。密钥默认隐藏，可用眼睛按钮查看；完整配置预览包含密钥，不要截图或分享。
未填写的默认授权模板不会写成空 Token 文件。使用中的账号仍禁止修改；保存采用版本校验，防止覆盖并发更新。
Hub 不替你申请第三方账号、补齐缺失的服务端代码，或修复上游 API 版本兼容问题。

## 2. 启动

先导出 `env.mcphub.example` 对应配置，至少包括：

```bash
export MCP_TOOL_TRANSPORT=hub
export MCPHUB_URL=http://192.168.0.22:3003
export MCPHUB_CATALOG=toolathlon
export MCPHUB_TARGET=account-a
# MCPHUB_TOKEN 用允许使用 account-a 的组 key；不要写上游 key。
```

在 Toolathlon 根目录，用包装器运行原有相对路径命令，例如：

```bash
python3 scripts/mcphub/run_with_workspace.py -- python3 scripts/n_run.py --N 1 --model_name MODEL --result_output_path /absolute/path/to/results --max_workers 10
```

也可用包装器执行原来的单题启动脚本。命令相对本次独立目录运行；不要使用指回原仓库的绝对脚本路径。
模型 API 配置仍按原有方式提供。

每次启动会在 `~/.local/share/toolathlon-hub-runs/` 创建权限为 0700 的独立目录，终端会显示位置。
此目录含运行配置、账号凭据和可能的结果，不应提交仓库或提供给模型作为工具工作目录。
依赖目录复用已安装依赖，configs、deployment、任务源码为独立副本。
原有脚本继续执行部署、预处理、模型交互、评分和清理；不在 Hub 中执行 Toolathlon 脚本。

## 3. 页面 ID 与任务目录

原有预处理返回的页面 ID、仓库范围和目录继续保存在原任务配置/可信 bundle 中。
模型 MCP 后端仍在任务容器中读取这些配置，通过带 capability 的回调注册给 Hub。
注册同时绑定本轮 Hub run，Hub 检查它属于同一个空间；run 结束/过期后旧任务连接不能继续调用。
`task/default` 是任务注册模板，不是账号名称，不应替换成工作区名。

## 4. 授权更新与结束

现有 OAuth 客户端负责刷新 access token。Notion 授权目录由同一轮任务共享，沿用原有进程文件锁。
包装器定时续租，并把 Google 授权文件、Notion 授权文件的变化自动写回 Hub。
每个文件更新都携带原内容哈希，Hub 拒绝覆盖更新过的授权。JSON 写入未完成时不会上传。
正常结束和中断退出都会尝试最终同步和释放工作区。进程失联导致租约过期时不会自动复用账号，需要管理员在确认旧任务已停止并找回最新授权后释放。同步失败返回非零状态并保留本地目录，不能当作完全正常结束。
凭据撤销或授权失效仍可能要求人工重新授权；API key 没有通用自动刷新机制。

同一工作区不能被两个包装器同时占用。不同工作区并不自动隔离本地 Canvas/Poste/Woo/Kubernetes 的端口和数据；
原部署脚本必须使用各自配置的独立环境。Kubernetes setup、arxiv_local/pdf-tools wrapper 当前已补齐。
Hub 包装器禁止原部署脚本执行全宿主机 Docker prune，避免清理别的项目资源。

## 5. 当前验证范围

离线测试覆盖凭据加密/权限、工作区占用、OAuth 旧值冲突、独立源代码副本、原命令执行、任务 ID 保存与授权写回。
还没有使用真实账号执行完整的部署、Notion 复制、Google 授权刷新和全量测评。
Hub 工作区功能已部署在 3003。字段表单与完整配置编辑需要本次 Hub 新版本；Toolathlon 适配源码也需同步，用于从 Google 用户授权生成 Calendar 客户端文件。尚未使用真实账号验证完整授权流程。

## 当前 configs 的使用方式

- 原 `configs/token_key_session.py` 不会复制到 Hub 运行目录，也不会把其中的 key 自动上传。运行时从所选工作区生成同名配置。
- `configs/global_configs.py` 保留原有模型/运行设置；不要求把模型 API key 混入工具账号工作区。
- 原目录的 `ports_config.yaml` 和 `port_changes.json` 会复制为本轮独立实例配置；工作区上传的同路径文件可以覆盖它们。不会因 Git 忽略了这些文件而退回默认端口。
- 任务内页面 ID、仓库范围、Canvas/邮件/商店账号绑定和 Kubernetes 路径仍沿用预处理与任务配置，不需要手动放进工作区模板。
- Hub 模式未显式填写 `SERPER_API_URL` 时，会从 `MCPHUB_URL` 推导公共 `/http/serper/search`，使用 Hub 调用 key；不回退使用本地 Serper 上游 key。若公共 Serper 注册名称不同，显式填写完整 URL。
- `ai_webpage_summary` 已在源码标记弃用，当前 finalpool 没有任务启用它；未填写摘要模型凭据不会阻碍其他任务导入。以后真要启用，工作区可添加 `web_summary_tool_api_key` 与 `web_summary_tool_base_url`，不要求当前额外申请。

原项目仍读取 `configs/google_credentials.json`、`gcp-service_account.keys.json`、Snowflake 私钥及 Notion 授权状态。
这些文件由网页表单保存的内容在本轮独立运行目录中生成，用户不用手动命名或上传。
本地服务实例配置 `ports_config.yaml`、`port_changes.json` 继续由原配置复制；高级凭据页面仍可用于特殊文件覆盖。

## MCP 编辑页的启动账号

任务连接 JSON 的 `workspaceSchema` 声明具体字段、公开默认值和所需文档结构。
Hugging Face、GitHub、W&B、Notion、Google Cloud/Calendar/Forms/Sheets、Snowflake 会显示账号表单。
不需要账号的文件工具等不会显示无关字段。Google Calendar/Forms/Sheets 共用同一账号的 Google 授权文件。

在 Hugging Face 中创建 account-a 并填 token，点击「保存账号」即保存到账户存储；随后去 Notion 选择 account-a 填它的 key。
各 MCP 不需要重复创建同名账号。新增 account-b 时同样填写 B 的值，原 A 的值和其他 MCP 的值不变。
保存账号与保存服务器配置是独立按钮，避免只填 token 却重新加载 MCP。账号列表中选中的值只是编辑对象；测评启动仍明确设置 MCPHUB_TARGET。

Atlas 的原 MCPB 环境变量、多目标配置和 arxiv/pubmed 任务路由不受这项可选声明影响。
新版本需要部署 Hub 后才支持该声明；已经导入的 MCP 可在升级时只补声明，不必删除原注册、组权限或 key。


## 网页中的自动配置与默认值

任务连接显示「任务自动提供的配置（无需手填）」，逐项标出由任务提供/覆盖的资源范围和凭据，以及自动生成的文件路径。
WooCommerce 的店铺 Key/Secret 来自对应任务的预置配置，配合原部署脚本初始化，不是让 Hub 自动创建云端账号。
Google Cloud 的 auth_uri/token_uri 等公开默认值在服务账号区直接显示；完整配置中修改过的值以保存内容为准。
Snowflake 角色/Warehouse 等输入旁显示模板默认值；database/schema 的基础默认值和任务覆盖关系另有说明。
这些是说明元数据，不会更改任务变量、账号凭据或限速策略。


## Notion 授权预检与任务数据重置

完整运行入口仍使用 `run_with_workspace.py -- python3 scripts/n_run.py --N ...`。
每轮顺序：检查并刷新所选账号授权 → MCP 连接检查 → 确认授权回存 Hub → 原应用部署/初始化 → 各题 preprocess → 模型运行与评分。
正常刷新不删除旧授权；缺少授权或明确的 invalid_grant 才触发首次/恢复登录。网络超时、403、429、5xx 不伪装成授权失效。
刷新返回的新 Token 先原子写入独立目录。启动检查即使后续失败，也会请求父包装器保存已轮换的授权；回存失败时停止并保留运行目录/账号占用，待恢复后再用。
只有父包装器负责写回 Hub，因此不会因启动检查和定时同步并发而互相覆盖。

`bash scripts/bootstrap_notion_official.sh` 默认验证刷新；`--force-round-trip` 仅用于明确要求重新授权。
`--dry-run` 只核对本地前置文件，不代表 OAuth 或服务已验证。
单题启动若没有经过 n_run，需在同一包装器运行目录中先执行 bootstrap，再运行单题命令；不要在原仓库目录直接初始化另一套账号。

任务数据重置不因复用授权而跳过：仍只删除评测父页面下本题同名旧页面，然后从模板父页面复制新页。
复制前检查模板存在、模板父页与评测父页不同；删除 API 失败或旧页仍存在会停止，不再当成空目录。
复制退出非零、没有合法新页面 ID、页面不属于评测父页、评测 Key 不能访问或出现重复同名页，都不保留可用 ID 文件，阻止任务模型阶段启动。
这不是全账号清库，也不清空模板父页。原 Canvas/邮件/WooCommerce/K8s 部署和各题初始化逻辑保持不变。

本次验证为模拟授权/页面 API、包装器回存握手和真实只读公开 OAuth 元数据查询；未对真实账号执行登录、刷新或页面删除/复制。


SMTP 就绪检查会等待服务端欢迎消息再发送 NOOP/QUIT，避免触发 Poste 的 early_talker 保护。
基础服务已启动而仅需检查时，可在同一账号运行终端执行：

```bash
sudo -E env "PATH=$PATH" bash global_preparation/deploy_containers.sh --check-only
```

此模式不会拉镜像、清理、重建服务或重置数据；成功后再运行测评。


## 取消、完成与账号释放

工作区租约属于 `run_with_workspace.py` 启动的整条命令。若启动的是交互终端，
在终端内结束或取消测评不会关闭终端，账号继续保留；输入 `exit` 才会回存并释放。
若包装器直接运行测评命令，命令完成或 Ctrl+C 后会进入清理、回存和释放流程。
清理期间重复 Ctrl+C 不再中断授权回存。同步临时失败后最终重试成功，可以正常释放；
仍失败则保留目录和账号，网页展示恢复状态，不自动丢弃授权。

每个新启动的任务容器带 `toolathlon.hub-run` 标签。包装器退出时只检查、停止本轮
标签对应的任务容器；无法确认停止时保留占用。Canvas/Poste/Woo/K8s 基础应用不在
这项清理范围内。旧版本启动的容器没有此标签，需要原来的退出清理或人工核对。
并行脚本保留评测程序实际退出码，不再用 tee 的成功状态覆盖评测失败。

网页启动账号下可查看开始、心跳、授权同步成功时间和上一轮退出原因。
管理员可点击“检查并释放占用”，确认旧测评、容器和终端已停且授权已核对后恢复。
该按钮不代替停止任务，也不会自动找回未回存 token。当前运行目录的
`workspace-status.json` 只记录运行 ID、状态和时间，不含凭据或运行 capability。
