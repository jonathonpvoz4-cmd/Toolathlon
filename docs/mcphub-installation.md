> 新版账号入口：在 Hub 凭据页导入 `工作区模板.json`，按 [工作区账号说明](mcphub-workspaces.md) 配置并运行包装器。下文涉及本地凭据文件的步骤说明的是上游文件格式/首次授权，Hub 模式会将工作区文件写到独立运行目录，不要求在原仓库维护另一套 key。

# Toolathlon MCP 导入与启动说明

## 本目录怎么用

- 8 个 `.mcpb`：导入到 Hub 的 `toolathlon` 空间，名称保持包名；这些包由独立的 `mcphub-v2-toolathlon` 执行容器运行，不依赖 Atlas 或 Toolathlon 源码目录。环境变量里的 key 留空，由你导入后填写。
- `任务连接.json`：包含 24 个当前任务使用的 MCP 资源，用网页“JSON 导入”，同样放在 `toolathlon` 空间。服务和凭据仍在每个 Toolathlon 任务容器中，由适配代码自动注册到 Hub；不要给这些资源另填固定启动命令或全局账号。
- `可选任务连接.json`：另含当前 finalpool 未引用的 `notion_official`、`time`。需要才导入，不与前一份重复。
- `核对记录/来源与策略.json`：逐个记录原 YAML、启动配置和超时，不是导入文件。
- `env.mcphub.example`：复制成 Toolathlon 根目录 `.env.mcphub`，填 Hub 组 key 后导出环境变量。
- `核对记录` 内的验证 JSON：仅证明包能启动并列出工具，不代表真实账号和全部任务已验收。

需要使用本次修改后的 Hub v2：它支持任务 JSON 无启动命令导入、任务请求预算配置及正确的续期计时。旧 Hub 会丢弃部分任务选项；更新后再导入。Atlas 现有配置、全局 IP 限速及 Relay 配置不作变更。

## 启用与回退

代码位于 Toolathlon 的 `mcphub-v2` 分支，保留 direct；普通和 decoupled 任务入口均已适配。首次配置：

```bash
cd /home/lny/Toolathlon
cp /home/lny/mcp-buddle/toolathlon/env.mcphub.example .env.mcphub
# 编辑 .env.mcphub，填写 Hub 调用组 key
set -a
source .env.mcphub
set +a
# 接着运行你原来的单任务或并行评测命令
```

`MCP_TOOL_TRANSPORT=hub` 启用；`MCP_TOOL_TRANSPORT=direct` 恢复 MCP 直连。Serper 独立由 URL 决定，恢复它的直连还需 `unset SERPER_API_URL SERPER_API_KEY`。预处理、数据复制、评判保持原调用路径；模型工具请求才经过 Hub。任务 JSON 的超时选项需要本次修改后的 Hub，先部署更新再导入，不要在旧版本上测长调用。

完整架构说明保存在 Toolathlon `docs/mcphub-v2.md`。分发目录不包含构建脚本；要重打包时使用 Toolathlon `scripts/mcphub/build_bundles.py`、`export_registry.py`。

## 导入后要填什么

常驻包中的 `google_map` 填 `GOOGLE_MAPS_API_KEY`，`youtube` 填 `YOUTUBE_API_KEY`；其他 6 个常驻包不需要 key。这里保留 Toolathlon 原实现：Google Maps 是旧版 MCP 0.6.2，没有套 Atlas 的新 Maps API 兼容层；是否能用取决于账号对应 API 权限，不因打包而自动升级。

为 Toolathlon 建调用组，授权常驻 MCP、24 个按任务资源的 `task/default`，并授权公共空间的 `serper`。同一个组 key 可以覆盖两类空间；也可以单独设置 `SERPER_API_KEY` 为 Serper 组的 Hub key。不要在 Toolathlon 填 Hub 上游 Serper 密钥。

`env.mcphub.example` 的 Serper 地址是完整 `/http/serper/search`，与一些 benchmark 填基础地址后自行拼 `/search` 不同。Hub 根地址为 `http://192.168.0.22:3003`；换机器改环境变量即可。

## 仍要保留的准备工作

接入 Hub 不会代替应用后端、任务初始化或评判器。以下脚本不因接入而删除，也不自动执行；其中部分会创建账号/容器、修改远端数据，按原部署流程确认环境后执行。

| 项目 | 原位置 | 怎么处理 |
|---|---|---|
| 通用配置 | `configs/global_configs.py`、`configs/token_key_session.py` | 从原 example 准备，继续用于预处理/评判与任务凭据。不能因为走 Hub 就全部清空。 |
| Google OAuth / GCP | `global_preparation/automated_google_setup.sh` | 原流程准备 OAuth、服务账号和 `configs` 里的 JSON 文件；Calendar、Sheets、Forms、GCP 任务仍使用这些文件/任务限定权限。 |
| Gmail/邮箱后端 | `deployment/poste/scripts/setup.sh`、`create_users.sh` | 保留原邮箱部署与账号初始化；emails 的附件及导出路径属于任务 workspace。不要把附件路径改为 Hub 目录。 |
| Canvas | `deployment/canvas/scripts/setup.sh`、`create_admin_accounts.py`、`create_canvas_user.py` | 后端服务仍在原部署位置；任务预处理创建/选择账号，MCP 在任务容器启动，再经 Hub 回调。不是把 Canvas 数据搬进 Hub。 |
| WooCommerce | `deployment/woocommerce/scripts/setup.sh`、`fix_permissions.sh` | 保留 WordPress/数据库、账号和任务初始化；站点地址及消费者 key 从任务配置传入。 |
| 部署总入口 | `global_preparation/deploy_containers.sh` | 这是原部署入口，会影响应用容器和缓存；已有可用部署不必为 Hub 重跑。 |
| GitHub | `local_binary/github-mcp-server`、任务 token 配置 | 保留项目自己的 fork 二进制、allowed repos、只读权限；不能替换成市场原生 GitHub MCP。 |
| HuggingFace / W&B / Snowflake | `configs/token_key_session.py`、Snowflake 私钥文件 | 保留任务 token、数据库/项目限制与凭据文件挂载，不填成 Hub 全局共享凭据。 |
| Kubernetes | 原任务镜像中的 Kind/kubectl、Docker/Podman socket 与 `deployment/k8s` | 继续由任务创建/管理环境；Hub 不需要 Docker socket。 |
| Office / PDF / 浏览器 / arXiv | 原任务镜像及 task preprocess | 文件路径、下载输出、浏览器状态均留任务容器；Hub 只转发调用。不得用 Atlas arxiv/pubmed 包顶替 Toolathlon 实现。 |
| 本地工具 | web_search、claim_done 等 | 保留原本地工具；web_search 改用完整 Serper URL，其余本地工具不注册为 MCP。 |

已有评测镜像时，两个运行脚本会复制当前分支的 `utils`、`scripts` 等代码进入任务容器，不要求仅为这次 Python 适配重建完整 Toolathlon 镜像。但镜像仍必须具有原 MCP、浏览器、二进制及其依赖；上传包不会给任务镜像补这些依赖。

### Notion 两种情况

1. `notion`：使用 Toolathlon 自己的 fork MCP 和 `--page-id` 限制。继续配置原 `notion_integration_key`、`notion_integration_key_eval`、source/eval 页面。原任务预处理会复制页面并生成 `notion_allowed_page_ids`。它仍在任务容器启动，不能用固定公共页面列表替代；无需在 Hub 再粘贴同一份 Notion key。
2. `notion_official`：使用 `mcp-remote https://mcp.notion.com/mcp` 和 OAuth 缓存。需要时先准备 `configs/.mcp-auth/notion.env`。脚本支持 163/Outlook，具体邮箱和首次浏览器会话按原 OAuth 工具准备；不要仅看旧 README 就认定只有一种邮箱可用。
3. 先检查：`bash scripts/bootstrap_notion_official.sh --dry-run`。确实需要刷新时，再运行 `bash scripts/bootstrap_notion_official.sh`；它会强制完整授权往返，不是每题都要执行。任务脚本仍挂载 `configs/.mcp-auth`；普通 `notion` 不因接 Hub 而强制执行官方 OAuth。
4. `configs/notion-mcp-patches/notion-openapi.json` 的原挂载保持不变，不要手动把它复制到 Hub 常驻包。Notion 页面准备、任务运行、评判仍按原顺序进行。

## 超时、重试、限速

原 YAML 超时逐项见下表；常驻包 manifest 已带相同毫秒预算。按任务资源设置 `timeout: 0`，由 Toolathlon 原 SDK 超时控制，不额外叠加 Hub 150 秒；其租约仍 300 秒、每 120 秒续期。Hub 到任务端的隐藏 HTTP 300 秒等待上限也已移除，避免截断 howtocook 等长调用。

没有给 MCP 增加无依据的并发20、固定间隔等限制；原 MCP 内部、任务调度与 web_search 的并发/重试保留。Hub 服务共享后，上游账号/IP 的总调用会汇合，这一点本身与各任务独立进程不同，后续只按实际额度配置共享限速。

Hub 默认 JSON 请求体上限仍存在（1 MiB，可在路由配置修改）。任务响应本次配置不再加额外的 4 MiB 截断；要处理超大入参或返回图片的题，仍需专项验收。不能仅凭列出工具就说所有题完全等价。

## Relay 候选名单（本版没有自动启用）

| MCP | 建议 | 接入前要确认 |
|---|---|---|
| scholarly | 优先评估 | Google Scholar / arXiv 外网请求；可能涉及 cookie、验证码、代理，不是填一个 Relay URL 就已适配。 |
| youtube-transcript | 优先评估 | 字幕网页请求可能遇 IP 限制；需包装其实际 HTTP 客户端，保留原工具 schema。 |
| fetch | 可选 | 通用网页访问可经 Relay，但需保留方法/编码/重定向及安全规则；此包是 Node fetch，不能直接复用 Atlas Python fetch 补丁。 |
| yahoo-finance | 可选，需专项验证 | yfinance/curl_cffi、cookie/crumb 需要会话与出口一致；简单逐请求换 IP 可能反而失败。 |
| arxiv_local、arxiv-latex | 可选 | 服务仍在任务侧；只代理 arXiv 下载/API，文件继续落原任务目录。它们不是 Atlas 的 arxiv 包。 |
| rail_12306 | 暂不默认接 | 存在启动时获取网页及上游出口/会话行为，需先验证，再决定是否代理。 |
| howtocook | 通常不必 | 启动远端数据失败有本地备份；是否接取决于部署网络。 |
| google_map、youtube、公共 Serper | 不以 IP 池解决额度 | API key/账号额度仍由上游规则决定；Serper 已由公共 HTTP 转发管理。 |
| Canvas、WooCommerce、邮箱、k8s、本地文件工具 | 不建议 | 本地/内网后端、任务状态与路径必须直达任务环境，不能把整个回调绕进公网 IP 池。 |
| Notion、GitHub、Google OAuth、Snowflake、W&B、HF | 暂不默认接 | 带账号权限或会话的 API；先判断确有网络需求，再逐客户端适配。 |

这些是基于当前实现的候选，不是官方额度表。当前包没有声明 Relay adapter，所以不会把“可考虑”伪装成“已走 Relay”。现有 Atlas Relay 服务不变。

## 第一次测试

先完成 Hub 更新、导入、组授权与 `.env.mcphub`，再沿用原来的单任务命令试跑，确认 Hub 有按任务注册/调用日志，任务结束租约消失，且本地文件落在原 workspace。随后分别选 Notion、Canvas、文件/Office、Google OAuth 任务验收。真实账号和完整后端尚未在本机配置，不能把协议测试当成所有服务都跑通。

## 34 个 MCP 的连接与原始超时

| MCP | 连接方式 | 原始超时（秒） |
|---|---|---:|
| arxiv-latex | 按任务 JSON | 10 |
| arxiv_local | 按任务 JSON | 300 |
| canvas | 按任务 JSON | 10 |
| emails | 按任务 JSON | 120 |
| excel | 按任务 JSON | 100 |
| fetch | 常驻 MCPB | 100 |
| filesystem | 按任务 JSON | 300 |
| git | 按任务 JSON | 10 |
| github | 按任务 JSON | 50 |
| google-cloud | 按任务 JSON | 30 |
| google_calendar | 按任务 JSON | 10 |
| google_forms | 按任务 JSON | 20 |
| google_map | 常驻 MCPB | 10 |
| google_sheet | 按任务 JSON | 60 |
| howtocook | 常驻 MCPB | 1000 |
| huggingface | 按任务 JSON | 120 |
| k8s | 按任务 JSON | 600 |
| memory | 按任务 JSON | 100 |
| notion | 按任务 JSON | 100 |
| notion_official | 按任务（可选） | 100 |
| pdf-tools | 按任务 JSON | 120 |
| playwright_with_chunk | 按任务 JSON | 120 |
| pptx | 按任务 JSON | 10 |
| rail_12306 | 常驻 MCPB | 120 |
| scholarly | 常驻 MCPB | 120 |
| snowflake | 按任务 JSON | 120 |
| terminal | 按任务 JSON | 120 |
| time | 按任务（可选） | 5 |
| wandb | 按任务 JSON | 120 |
| woocommerce | 按任务 JSON | 10 |
| word | 按任务 JSON | 120 |
| yahoo-finance | 常驻 MCPB | 60 |
| youtube | 常驻 MCPB | 20 |
| youtube-transcript | 常驻 MCPB | 10 |

版本差异：rail_12306 按 YAML 的精确 `12306-mcp@0.3.9` 打包；仓库 npm 锁文件另有 fork 0.3.5，两者不可混称。其余 fork 采用 Dockerfile/package-lock 中的提交；Python 重叠依赖优先使用 uv.lock 的锁定版本。每包的 provenance.json 记录实际版本/来源；没有原运行镜像快照，不能证明所有历史浮动依赖完全一致。

网络要求：两个运行脚本保留原 `--network host`。Hub 必须能访问任务机器自动选择的随机回调端口；跨机器部署时可用 `MCPHUB_CALLBACK_HOST` 指定可达的 LAN 地址，并配置相应的内网访问规则。每个任务使用独立端口与 token，不通过公共 Serper 地址回调。

`n_run.py` 每轮仍会自动执行应用部署、Notion 官方 OAuth 强刷和并行评测，接 Hub 没有取消这些步骤；单题脚本不会替你执行全部部署。适配已让 n_run 自己调用 sudo 时保留 Hub/Serper 变量名，密钥值不进入命令日志。若手动在 shell 前加 sudo，也须显式保留相应路由变量，否则 sudo 会清空它们、回到 direct。

## 当前容器布局

| 容器 | 承载内容 |
|---|---|
| `mcphub-v2` | 公共 Serper 等公共资源、入口认证、路由、共享限速、日志统计 |
| `mcphub-v2-atlas` | `atlas` 空间常驻 STDIO MCP |
| `mcphub-v2-toolathlon` | `toolathlon` 空间这 8 个常驻 MCP；导入前进程数为 0 是正常的 |
| Toolathlon 原任务容器 | 24 个任务 MCP（及需要时的 2 个可选 MCP）、任务文件和状态 |
| 原应用/Kind 容器 | Canvas、邮箱、WooCommerce 后端及任务 Kubernetes 集群，继续沿用原部署 |

你仍在同一网页导入、填 key 和重载；Hub 根据空间选择执行容器，不需要手写执行容器 URL。后台运行实例表有“执行位置”。上传后的包由 Hub 分空间存放，再由对应执行容器自动读取并复制为独立运行版本，不需要手动复制文件。多 key、Relay 及共享限速仍由 Hub 配置；本次没有自动给 Toolathlon 启用 Relay，也没有新增无依据的速率参数。当前 3003 是 v2，3002 和 `~/mcphub` 保持原状。

如果以后某个 MCP 有系统依赖冲突，可手动增加执行服务，并按 MCP 或目标路径分配到新服务；目前不做自动冲突识别或自动创建容器。执行容器不可用时会报错，禁止悄悄在 Hub 中启动同一个空间的 MCP。
