# Toolathlon 接入 MCPHub v2

## 2026-10-28：仅接入层的兼容修复

- Hub 任务适配器为 MCP 1.9 的 STDIO reader 合并完整行，避免大结果反复拼接。
  不升级或编辑原 SDK，不裁剪 Sheets 内容，不修改工具参数、schema 或原客户端超时。
- Hub 运行包装器仅在本轮独立配置中补全 `ports_config.yaml` 已声明的本地邮件端口映射。
  外部邮件服务器、任务正文、答案、评分和仓库原任务文件保持不变。
- 取消任务时等待子进程和输出管道退出，未评分任务标记 cancelled，不改已生成的评分文件。
- `arxiv_local`、`arxiv-latex` 在任务 MCP 启动前领取该注册的 Relay 配置；常驻 scholarly 包增加可选 arXiv HTTP 适配器。
  仅 arXiv 域名经现有 `/v1/fetch`，论文和源码仍保留原任务位置，Google Scholar 分支不增加未经证实的限速。
  Relay 参数来自 Hub，启用后初始化失败不退回直连；没有启用时保留原路径。
  现有共享 Relay 的 arXiv 策略为 3000ms、并发 1，与 Atlas 共用同一调度入口。

更新 Toolathlon 接入代码后需退出旧账号终端、重新进入工作区，才能复制到新的运行目录。
新 scholarly 包位于分发目录；当前 3003 已覆盖导入并重载该服务。
Hub 镜像、Relay 服务和 3002 不需要因这些 Python 接入层修复重启。
上述验证不等于证明不同模型运行的总分必然相同；正式等价验收仍需同账号/环境、同模型配置与官方任务语义下的整轮对照。

此分支保留原 direct 模式。导入包和完整安装说明位于 `/home/lny/mcp-buddle/toolathlon`；可重新生成：

```bash
python3 scripts/mcphub/build_bundles.py --output /home/lny/mcp-buddle/toolathlon
uv run python scripts/mcphub/export_registry.py --output /home/lny/mcp-buddle/toolathlon
```

构建依赖 npm、uv、Git 和 Python 3.12。工作目录默认位于 `~/.local/share/toolathlon-buddle-build`，不进入仓库，也不作为运行依赖。MCPB 包包含独立运行文件，不依赖 Toolathlon、Atlas 或构建目录。Python 包包含 Linux x86_64 Python；执行服务容器需提供 Node.js 和兼容的 Linux 系统库。

fetch 由任务容器启动并通过 Hub 任务连接调用，保留任务本地 HTTP 访问；`fetch.mcpb` 仅用于另行部署通用公网抓取，不作为 Toolathlon 常驻资源。

在网页将 7 个常驻 MCPB 导入 `toolathlon` 空间；将 `任务连接.json` 作为 JSON 导入。`可选任务连接.json` 包含当前 finalpool 未引用的 `notion_official` 和 `time`，按需导入。勿将 Toolathlon 的 fetch、google_map、notion 替换成 Atlas 的同类包；二者实现和工具定义可能不同。

创建调用分组，授权 `toolathlon` 空间中要用的服务器及默认任务连接；同时授权公共 `serper`。复制组 key 到本地 `.env.mcphub` 的 `MCPHUB_TOKEN`。公共 Serper 已配置的上游 key 保留在 Hub；此处只填 Hub 调用 key。

```bash
cp env.mcphub.example .env.mcphub
# 编辑 .env.mcphub 后
set -a
source .env.mcphub
set +a
# 通过工作区包装器运行原命令；先在 Hub 配置 account-a
python3 scripts/mcphub/run_with_workspace.py -- python3 scripts/n_run.py --N 1 --model_name MODEL --result_output_path /absolute/results --max_workers 10
```

两个容器启动脚本均透传这些变量；命令输出只展示变量名。原工具重试机制、并发设置和 YAML 超时保留。`SERPER_API_URL` 是完整 `/search` 地址，代码不会再次拼接路径。没有它时保留原 Serper 直连配置。

普通 agent 和 decoupled gateway 均采用相同接入层；预处理和评判的内部 MCP 连接保持原样，不经 Hub。基础账号凭据由 Hub 工作区提供，任务准备产生的页面 ID、资源范围和文件仍由原任务配置保存。每次 manager 创建独立运行标识，每个 MCP 使用独立回调 token 和租约，同一题同时运行也不会共用任务连接。

任务服务先由 Toolathlon 按原配置启动，再将工具请求经 Hub 返回该任务服务。导入 JSON 仅建立可授权的任务资源，不会在 Hub 启动相同进程，也不替代原镜像、部署服务或启动脚本。任务结束撤销租约、关闭回调并回收本地服务；Hub 模式失败会报错，不会悄悄回退直连。

任务导入配置使用 `timeout: 0`，不另加 Hub 150 秒截止；Hub 外层 HTTP 与代理读等待使用包含排队余量的有限预算，同时受任务取消和整题总时限约束，不复用 direct 模式的短读超时。任务容器内的本地 MCP 客户端仍保留 YAML 配置的执行期限，详见下文；`maxBufferSize: 0` 不加额外响应大小限制。租约仍每 120 秒续期，有效期 300 秒，撤销、过期或客户端断开均取消转发。Hub 未修改的 Atlas 资源仍使用原默认策略。Hub 通用 JSON 请求体限制仍适用（默认 1 MiB，可在 Hub 路由配置调整）；超大工具入参需单独核对，不宣称任意负载完全等同直连。

恢复 direct：`export MCP_TOOL_TRANSPORT=direct`。Serper 路由单独控制；需要恢复它的直连时再 `unset SERPER_API_URL SERPER_API_KEY`。

完整账号、Notion OAuth、应用部署和 Relay 候选清单见分发目录 `安装说明.md`。首次接入先做单任务验收，再扩大并发；协议测试不能代替使用真实账号的完整评测。

网络要求：两个运行脚本保留原 `--network host`。Hub 必须能访问任务机器自动选择的随机回调端口；跨机器部署时可用 `MCPHUB_CALLBACK_HOST` 指定可达的 LAN 地址，并配置相应的内网访问规则。每个任务使用独立端口与 token，不通过公共 Serper 地址回调。

`n_run.py` 启动时验证 Notion 授权能刷新并确认回存 Hub，部署一次原应用；随后共用一个账号租约、一个总并发池执行 N 次评测。同题依次执行，每次仍在 preprocess 中重置题目数据，不同题可交错推进。缺少或明确失效的授权才重新登录，网络错误保留授权并停止启动。单题脚本不会替你执行全部部署或整轮授权预检。适配已让 n_run 自己调用 sudo 时保留 Hub/Serper 变量名，密钥值不进入命令日志。若手动在 shell 前加 sudo，也须显式保留相应路由变量，否则 sudo 会清空它们、回到 direct。

空间服务已拆分：公共 Serper 留在 Hub；7 个常驻包上传到 `toolathlon` 空间后由 `mcphub-v2-toolathlon` 执行；Atlas 常驻 MCP 由 `mcphub-v2-atlas` 执行。Hub 仍是唯一客户端入口，并集中管理 key、目标权限、工具限速和日志。按任务连接仍返回原任务容器，K8s/Canvas 等原部署流程不变。MCPB 不需要因本次拆容器重新打包。

账号统一管理与原脚本的启动方式见 [工作区账号配置](mcphub-workspaces.md)。Hub 模式运行预处理、模型和评分时必须来自同一次工作区运行。


SMTP 就绪检查会等待服务端欢迎消息再发送 NOOP/QUIT，避免触发 Poste 的 early_talker 保护。
部署时将邮件容器的实际网桥网关精确加入 Poste guard 白名单，并清除该网关的自动封禁计数，
避免重复健康检查被计为无效邮件会话；其他来源的防护规则保持不变。
Poste 部署脚本保留原私网白名单，并自动加入邮件容器实际网络的网关地址，
适配不同 Docker/Podman 网桥；任务容器与 Poste 同机时无需填写额外地址，与 Hub 所在机器无关。
若平台将任务与 Poste 分置不同机器，可在部署邮件服务前设置
`POSTE_SMTP_TRUSTED_SOURCES="203.0.113.7/32"`（示例地址须替换成邮件服务实际看到的任务来源 IP）；
多项以逗号或空白分隔，仅填写可信来源，不接受全网放行。此设置只控制邮件服务免认证发送，
不改变 Hub 的访问权限。平台跨机器运行仍需自行配置可达的 SMTP 地址；本设置不会修改连接地址。
基础服务已启动而仅需检查时，可在同一账号运行终端执行：

```bash
sudo -E env "PATH=$PATH" bash global_preparation/deploy_containers.sh --check-only
```

此模式不会拉镜像、清理、重建服务或重置数据；成功后再运行测评。
# GitHub 预处理与判分请求接入

Hub 模式下，GitHub 公共辅助函数、git-milestone 判分查询、task-tracker 判分查询
通过本轮账号的 Hub HTTP 入口请求 GitHub API。镜像 clone/push 使用 Git smart HTTP
经过同一个入口，Hub 使用所选账号的 GitHub token；Hub key 和运行凭证不会写入 Git URL
或命令参数。直接模式保持原调用路径。

启用前需要部署支持 `workspaceSchema.http` 的 Hub，并更新 GitHub 注册中的声明：
`scripts/mcphub/credential_schema.py` 和重新导出的 `任务连接.json` 已包含 `api`、`git`
两个固定上游绑定。无需重打包 MCPB。更新现有注册时只更新该声明，保留原启动配置、
账号数据和限速设置；不要删除已填写的账号。

这些请求使用该 MCP 原有的共享调度器，是否限速及其数值仍由 Hub 服务器设置决定。
客户端仅对 GitHub 明确的 429、secondary rate limit 或 API rate limit exceeded 响应
最多尝试三次，遵守 Retry-After/额度重置时间；普通权限 403 不重试，Hub 失败不回退直连。
网页日志按 GitHub 注册、账号、任务记录请求及状态，凭据记录为指纹。

Git pack、SDK 上传下载采用流式转发，不整包缓冲。Hub 不自动跟随重定向；
SDK 按原始服务地址处理，并重新选择授权的 Hub 入口，Hub 凭证不会传到上游。

## Notion 预处理和判分

Notion API 的请求辅助函数和 Notion Python SDK 调用通过 Hub 转发，分别使用
`prepare`（notion_integration_key）和 `evaluate`（notion_integration_key_eval）绑定。
适配器用本轮独立目录的账号数据确定凭据角色，不读取源码仓库里的旧账号；不属于
当前账号的 key 会被拒绝。Hub 根据本轮账号注入实际 key，保留 API 状态和正文。
页面复制、重置仍由原预处理控制，模型使用的任务 MCP 仍在原任务容器，不重复启动。
原来的 notion_official OAuth 自动授权流程不在本次修改范围内。


## 其他第三方 SDK 出口

Hugging Face、W&B、Google（Calendar/Forms/Sheets/Cloud）和 Snowflake 的预处理、
判分 SDK 网络请求也接入本轮 Hub HTTP 入口。保留原 SDK 的请求签名、OAuth 刷新、
结果解析和重试；Cloud Logging 保留原生同步 unary gRPC，不替换成另一套 REST 工具。
声明允许的 CDN/对象存储请求同样经过 Hub，不向存储域名注入账号 key。

Hugging Face 的原生 MCP 会话也使用本轮账号的 Hub HTTP 入口，保留会话恢复和执行期限。其 API 与下载绑定使用 Hub 配置的 IP 池；API 绑定启用流式转发以保留 SSE 响应。Hub 需支持 `egress: ip-pool` 并配置可用代理，已有部署需更新 `workspaceSchema.http` 后重新进入工作区。
未声明的认证服务地址会报错，不能悄悄退回直连；自定义服务地址需先扩展注册声明。

此改动保留 7 个常驻 MCP 和原按任务 MCP 的架构，任务回调、原服务部署、重置和
本地 Canvas/Poste/Woo/K8s 测试数据不迁移。任务 MCP 仍临时领取本轮凭据；
并非把所有 task MCP 内部网络请求再次经过此 HTTP 网关。原 Notion 自动授权脚本不改。
账号存储和临时目录隔离沿用现有实现，不新增一个 MCP 容器。

部署：重建 Hub v2 和 Toolathlon 运行镜像；将重新生成的任务连接 JSON 中
`workspaceSchema.http` 合并到现有同名服务器，保留原账号、命令、目标和限速。
无需重新打包或导入 7 个常驻 MCPB。修改后应先做真实账号单任务验收，再跑整轮；
本地协议/SDK测试不能证明远端账号授权和服务额度都正常。
当前 GitHub 注册若未设置调用限速，新请求也不会自动产生一个限速值；
HTTP 请求与工具调用共享该 MCP 已配置的调度策略，不解决跨注册同 key 的统一额度。


工作区配置文件解析支持普通 Python 包和 namespace 包（configs 没有 __init__.py 时
__file__ 为 None）。仅在当前 configs 包的唯一配置目录读取本轮账号/路由文件；
多个候选账号目录会拒绝调用，不回退到源码账号。Notion 的八个页面预处理入口会
检查重置子进程退出码，重置失败即停止准备，不再掩盖为缺少页面 ID 文件。
旧 Hub 账号中的 PEM 转义换行由新版 Hub 在读取时规范化，无需更换 key 或重新导包。
这类修复需要重新部署 Hub、退出旧工作区并重新进入；不修改已运行任务的快照。


Snowflake HTTP SDK 路由按标准 origin 比较：主机名不区分大小写，HTTPS 显式
:443 与省略端口视为相同来源；其他端口、其他主机和带用户信息的地址仍不匹配。
路径与查询参数不改变。此项只需更新 Toolathlon 代码并重新进入工作区，
无需重新部署 Hub 或导入 MCPB。

## GitHub 共享请求调度

Hub 模式下，预处理、评分与任务内 GitHub MCP 的上游 HTTP 请求统一经过
MCPHub v2 的 GitHub 请求队列。任务和账号选择方式保持不变，不需要账号池；
原 GitHub MCP 版本、工具定义及仓库权限限制保持不变。

任务内仅增加一个监听 `127.0.0.1` 的轻量转发器，使用原二进制支持的
`GITHUB_HOST` 和代理配置；真实 GitHub token 由 Hub 注入上游请求，不放进转发器。
Hub 中 GitHub 注册需声明 `api`、`git`、`raw`、`uploads` 四个 HTTPS 绑定，
`scripts/mcphub/export_registry.py` 生成的配置已包含这些声明。

同一 GitHub 用户的不同 token、不同任务共享请求队列，写请求至少间隔一秒。
上游限流后，Hub 根据 `Retry-After`、`X-RateLimit-*` 统一暂停并有界重试；
客户端不再额外叠加限流重试。网络异常、权限错误与已发送的 Git 流不自动重放。
客户端原有超时仍生效，排队超过预算会取消。Hub 重启保留已保存的退避截止时间。

调用日志详情可查看上游接口、账号 ID、阶段、尝试次数与等待时间。其他机器
使用同账号直接访问 GitHub 时，不在此队列控制范围内。

升级后应重新启动 workspace 终端，确保 run 快照包含新适配代码。
Google Maps 与 YouTube 的打包声明同时支持按账号保存 API Key 和启动配置。


## Hub 模式的等待与取消

基础设施部署会验证 WooCommerce 插件及网络激活状态、配置商店数量和 `wc/v3` REST 命名空间。插件安装最多尝试三次；仍失败时返回部署失败，由外层部署流程处理，不进入任务执行。

续跑保留已有评分的模型时限/步数耗尽结果。步数耗尽但没有有效评分检查点时，按环境或评分未完成收集，在 `runN_retry` 中执行一次独立重试，原始目录保留；不把日志中的步数耗尽标志代替评分文件。评分产物保护器的目录检查使用系统 Python 标准库，不触发任务虚拟环境依赖同步。

Direct 模式沿用各 SDK/MCP 原有配置。Hub 模式区分以下阶段：

- 本地任务 MCP：保留 YAML 的 `client_session_timeout_seconds`，Hub 调用回调之后才开始执行计时。
- 本地列工具：沿用该 MCP 的配置期限，不统一压成 15 秒。注册、续租等短控制请求仍为 15 秒。
- 任务连接续租：临时网络错误、429 和 5xx 在最后一次确认的有效租约内退避重试，恢复期间保持回调可用；明确撤销或租约过期时结束为环境故障，进入原有的集中补跑。不会自动重放业务工具调用；日志保留服务名、连接 ID、状态码或异常类型、耗时及剩余租约，不记录密钥。
- 共享 MCP：实际执行仍由 Hub 服务器配置限制；客户端另给原期限加 600 秒排队余量和 15 秒传输余量，作为有限兜底。
- 准备/评分 SDK：保留调用方有限的读取期限，以 `X-MCPHub-Upstream-Timeout-Ms` 发给 Hub。Hub 在出队后应用，内部请求头不传给供应商。流式正文有进度时允许继续，空闲超时则关闭上游。gRPC unary 使用执行总期限。
- 未设置有限读取期限的 SDK 请求：默认 300 秒上游读取兜底，可通过 `MCPHUB_UPSTREAM_TIMEOUT_SECONDS` 调整。Hub 对没有预算头且未配置有限期限的工作区请求，使用 `MCPHUB_WORKSPACE_HTTP_TIMEOUT_MS`（默认 300000 毫秒）。管理员已有更严格期限仍生效。
- 外层 HTTP：保留 15 秒连接/连接池期限；读取和写入等待为上游预算加排队余量及传输余量。`MCPHUB_QUEUE_WAIT_SECONDS` 可调整默认 600 秒余量，不改变 Hub 的共享排队、限速或退避策略。
- arXiv/Scholarly Relay：透传调用方有效读取期限，作为出队后上游网络期限；Relay 自己的分类网络/整次请求上限仍生效。未给有限期限时沿用 Relay 配置。Relay 内部既有重试不变，适配层不额外重放。

共享 Scholarly 的 Relay 适配打包在 MCPB 内，需重新构建并导入 `+timeout.1` 版本才能应用其请求预算透传；本地任务 MCP 的执行期限调整不依赖重新导入 MCPB。

这些是 Hub 增加调度后的时间策略，不承诺与直连总耗时相同。任何写操作超时后都不自动重试，避免重复执行。整题原有总时限不变。

### 任务 MCP 执行期限

任务连接的外层 HTTP 等待可包含 Hub 排队；任务容器里的本地 MCP 客户端保留
`client_session_timeout_seconds`。排队完成后才发送本地 MCP 请求，因此不会
消耗其执行期限。实际执行期间的等待（包括 MCP 内部经 Hub 排队的 HTTP 请求）
计入这个期限。超时按原生 SDK 语义返回错误，不自动重试调用，
不重启整个 MCP 或篡改工具实现。当前固定版本的 SDK 不保证在超时时发送取消通知；
客户端返回超时并不保证任意 shell 命令已终止，任务结束时仍应清理
任务容器。

调整执行期限不需要重新导入 MCPB 或修改 K8s 工具 schema。

Hugging Face SDK 在 Hub 模式下的 GET / HEAD 读取使用至少 120 秒上游读取期限（调用方更长的期限保留），覆盖仓库信息、下载元数据、站内重定向、CDN 下载和 Range 续传。连接/读取异常及 502、503、504 在响应交付前最多尝试 3 次；每次仍经 Hub 和所选账号，不回退直连。401/403/404/429 不新增重试，POST/PUT/PATCH/DELETE 不扩大期限或自动重放。响应正文中断后将截断异常交给 SDK 原有的 Range 续传，不能重头向已写文件重复拼接。此行为由 Toolathlon 客户端镜像提供，Hub 无需重启；已落盘的失败评分不会因此自动重做。

## 模型阶段的评分材料隔离

默认分阶段容器流程中，预处理和评分保留原权限，模型阶段固定使用独立的非 root 身份，并禁止通过 setuid 或文件 capabilities 提权。任务输入仍位于工作区，模型可正常读写、使用 MCP 和在自己的 Python 环境安装依赖；原始题目源目录、预处理日志不可读，评测框架和评测 Python 环境不可修改。模型容器不挂载 Docker/Podman socket，启动前校验挂载源、目标及读写权限。需要 Kind 的题目通过同一 Run 私有 Docker 内的临时 setup 容器完成预处理；setup 持有 socket，结束后删除。任务目录通过独立卷共享，模型和评分继续通过 kubeconfig 访问原集群，应用端口转发保留在模型容器内。取消时一并清理 setup 容器与任务卷。

评分产物保护器暂存并恢复评分脚本、参考答案、中文答案目录和含参考输出的单元测试目录。评分前恢复可信文件，并保护轨迹及评分检查点，保留任务需要的后台服务。此流程不改题目内容或评分条件，不需要另建评分镜像，也不修改 MCPHub。

模型阶段不开放公共集群管理凭据。不使用 `k8s` 的题目不提供 kubeconfig；Kubernetes 题目使用本题独立的受限身份，预处理和评分继续使用原管理凭据。安全审计只读已有资源；其他 Kubernetes 题允许操作业务命名空间内的工作负载、日志、端口转发和 Helm 资源，但不允许修改命名空间安全标签、RBAC、系统命名空间或节点代理。业务命名空间对新 Pod 执行 Kubernetes 1.27 baseline 安全准入，禁止特权容器、节点目录挂载和主机命名空间；已有安全审计样本保持原样。PR 预览题所需的 `pr-preview-123` 命名空间由可信准备阶段预先创建。工作区内残留的 kubeconfig 使用受限身份替换，不改任务源文件及评分逻辑。

模型可见的 MCP 与本地工具另有答案来源策略：拒绝仓库名精确匹配 `Toolathlon`、`toolathlon_gym`/`toolathlon-gym`、`toolathlon-website`、`toolathlon-traj-website` 的已知答案来源、已知审计网站、轨迹网站及其他参与者的同题许可证成品；组织名或业务资源路径包含 `toolathlon` 不会单独触发，GCP `projects/toolathlon-eval-…` 路径正常放行。检查常见 Base64、百分号编码、字面量拼接及可静态解析的 Python 变量/f-string 地址；规则位于 `utils/answer_material_policy.py`。搜索结果移除对应的整条命中，避免留下无来源的答案片段；明确请求的合法 HTML 文档可保留正常正文并移除含答案链接的区块。`toolathlon/BenchTasksCollv3`、`toolathlon/Annoy-DataSync` 等题目业务仓库及普通论文、Google 表单不在拒绝范围内。被拒绝的工具请求返回明确错误，整题仍可继续；日志用 `answer_material_blocked` 及策略版本记录拦截事件，不记录密钥或完整请求内容。

模型工具请求禁止读取共享数据库作业历史：BigQuery `INFORMATION_SCHEMA.JOBS*`、Snowflake 系统 `QUERY_HISTORY*`，以及明确使用 BigQuery SDK/发现接口枚举作业的本地代码。正常业务 SQL、`TABLES`/`COLUMNS` 元数据查询不受此规则限制。初始化和评分不经过模型工具过滤。此规则不清空云端历史，也不保证阻止任意动态代码绕过检查直接请求云 API。

公开成品名单还覆盖已确认的 `Annoy-DataSync` 历史答案讨论、指定 HuggingFace 合并成品及配套副本，以及 `zjunlp/Skills` 的 `skills/skill-collections/toolathlon/` 目录。名单和适用范围集中在 `utils/answer_material_policy.py`，不封整个账号或所有同名数据集；本题自己的 Issue、原始数据源和正常业务 SDK 调用仍可使用。过滤包含禁用来源的历史、评论或搜索结果时，保留其他正常记录，不无条件豁免历史工具。

该来源策略不是任意代码的网络沙箱，不能保证拦截未知镜像站、短链接或动态构造的远端答案来源。上述权限隔离适用于默认的 `run_single_containerized.sh` 分阶段流程，不将直接运行 `main.py` 或 legacy 模式声明为同等隔离。修改后需重建并使用新的 Toolathlon 评测镜像；已运行的任务容器不自动更新。

字符串拼接检查按引号及转义顺序扫描，避免长转义文本引起正则反复回溯、占用解释器并阻塞任务连接续租。扫描覆盖整个输入，不通过截断工具正文或跳过长输出降低检查范围。
