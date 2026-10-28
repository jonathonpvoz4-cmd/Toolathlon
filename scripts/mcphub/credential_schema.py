"""Declarative account forms. Public defaults only; never load operator credentials."""
import hashlib
import json


def field(name, label, *, secret=True, description=None, default=None, multiline=False):
    result = {'name': name, 'label': label, 'secret': secret}
    if description:
        result['description'] = description
    if default is not None:
        if secret:
            raise ValueError('Secret defaults must remain empty')
        result['default'] = default
    if multiline:
        result['multiline'] = True
    return result


def document(path, label, fields=(), *, template=None, format='json', description=None):
    return {'path': path, 'label': label, 'format': format, 'fields': list(fields),
            **({'template': json.dumps(template, ensure_ascii=False, indent=2)} if template is not None else {}),
            **({'description': description} if description else {})}


# Same scopes as global_preparation/simple_google_auth.py; they describe an
# already granted authorization, not a new OAuth grant created by this form.
GOOGLE_SCOPES = [f'https://www.googleapis.com/auth/{name}' for name in
                 ['drive', 'gmail.modify']] + ['https://mail.google.com/'] + [
                 f'https://www.googleapis.com/auth/{name}' for name in
                 ['calendar', 'youtube', 'documents', 'spreadsheets', 'forms']]
GOOGLE_OAUTH = document('configs/google_credentials.json', 'Google 用户授权', [
    field('client_id', 'OAuth Client ID', secret=False),
    field('client_secret', 'OAuth Client Secret'),
    field('refresh_token', 'Refresh Token', description='填写已经完成 Google 授权得到的 refresh token；表单不会代替首次授权。'),
    field('token', 'Access Token（可选）', description='可留空，由现有 Google 客户端通过 refresh token 更新。'),
], template={'token': '', 'refresh_token': '', 'client_id': '', 'client_secret': '',
             'token_uri': 'https://oauth2.googleapis.com/token', 'scopes': GOOGLE_SCOPES},
    description='Calendar、Sheets、Forms 共用同账号的 Google 授权。固定 scopes 和 token_uri 已预置，可在 JSON 预览中调整；修改 scopes 不会自动扩大授权。')
GOOGLE_SERVICE_ACCOUNT = document('configs/gcp-service_account.keys.json', 'Google Cloud 服务账号', [
    field('project_id', '服务账号 Project ID', secret=False),
    field('private_key_id', 'Private Key ID', secret=False),
    field('private_key', 'Private Key（PEM）', multiline=True),
    field('client_email', 'Client Email', secret=False),
    field('client_id', 'Client ID', secret=False),
    field('client_x509_cert_url', 'Client Certificate URL', secret=False),
], template={'type': 'service_account', 'project_id': '', 'private_key_id': '', 'private_key': '',
             'client_email': '', 'client_id': '', 'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
             'token_uri': 'https://oauth2.googleapis.com/token',
             'auth_provider_x509_cert_url': 'https://www.googleapis.com/oauth2/v1/certs',
             'client_x509_cert_url': '', 'universe_domain': 'googleapis.com'},
    description='可逐项填写，也可展开 JSON 预览粘贴完整服务账号 JSON；无需上传文件。')
NOTION_HASH = hashlib.md5(b'https://mcp.notion.com/mcp').hexdigest()
NOTION_PREFIX = f'configs/.mcp-auth/mcp-remote-0.1.16/{NOTION_HASH}'
NOTION_FILES = [
    document('configs/.mcp-auth/notion.env', 'Notion 登录账号', [
        field('NOTION_ACCOUNT_EMAIL', 'Notion 登录邮箱', secret=False),
        field('MAILBOX_PROVIDER', '验证码邮箱类型（outlook / 163）', secret=False, default='outlook'),
        field('OUTLOOK_ACCOUNT_PASSWORD', 'Outlook 密码（使用 Outlook 时填写）'),
        field('OUTLOOK_RECOVERY_EMAIL', 'Outlook 恢复邮箱（使用 Outlook 时填写）', secret=False),
        field('NETEASE_163_ADDRESS', '163 邮箱（使用 163 时填写）', secret=False),
        field('NETEASE_163_AUTH_CODE', '163 授权码（使用 163 时填写）'),
    ], format='env', description='供 Toolathlon 原有自动授权脚本读取。两种邮箱只填实际使用的一种。'),
    document(NOTION_PREFIX + '_tokens.json', 'Notion MCP OAuth Token', [
        field('access_token', 'Access Token'), field('refresh_token', 'Refresh Token'),
    ], template={'access_token': '', 'refresh_token': '', 'token_type': 'Bearer'},
        description='已有授权可直接填写；首次使用也可由原有授权脚本生成并回存，不要把 Notion Integration Key 填在这里。'),
    document(NOTION_PREFIX + '_client_info.json', 'Notion OAuth 客户端', [
        field('client_id', 'OAuth Client ID', secret=False),
    ], template={'client_id': '', 'redirect_uris': ['http://localhost:9553/oauth/callback'],
                 'token_endpoint_auth_method': 'none', 'grant_types': ['authorization_code', 'refresh_token'],
                 'response_types': ['code'], 'client_name': 'MCP CLI Client', 'client_uri': 'https://github.com/geelen/mcp-remote'},
        description='OAuth 动态注册结果；通常由授权脚本生成。可用 JSON 预览粘贴已有客户端状态。'),
    document('configs/.mcp-auth/notion_login_state.json', 'Notion 浏览器登录状态',
             template={'cookies': [], 'origins': []}, description='可选。已有浏览器状态可通过 JSON 预览粘贴；否则由原有登录脚本生成。'),
    {'path': 'configs/.mcp-auth', 'label': '其他已保存授权状态', 'directory': True,
     'description': '保留并可编辑授权脚本生成的其他状态；无需手动创建文件或填写路径。'},
]
SCHEMAS = {
    'huggingface': {'values': [field('huggingface_token', 'Hugging Face Token', description='hf_ 开头的访问令牌；不是 Hub 调用 Key。')]},
    'github': {'values': [field('github_token', 'GitHub Token', description='仓库访问范围仍由 Toolathlon 按任务设置。')]},
    'wandb': {'values': [field('wandb_api_key', 'Weights & Biases API Key')]},
    'notion': {'values': [field('notion_integration_key', 'Notion 预处理 Key'),
                          field('notion_integration_key_eval', 'Notion 工具调用 Key'),
                          field('source_notion_page_url', '模板父页面 URL', secret=False),
                          field('eval_notion_page_url', '评测父页面 URL', secret=False)],
               'files': NOTION_FILES},
    'notion_official': {'files': NOTION_FILES},
    'google_calendar': {'files': [GOOGLE_OAUTH]},
    'google_sheet': {'files': [GOOGLE_OAUTH]},
    'google_forms': {'files': [GOOGLE_OAUTH]},
    'google-cloud': {'values': [field('gcp_project_id', 'Google Cloud 项目 ID', secret=False)],
                     'files': [GOOGLE_SERVICE_ACCOUNT]},
    'snowflake': {'values': [field('snowflake_account', 'Snowflake 账号', secret=False),
                             field('snowflake_user', 'Snowflake 用户名', secret=False),
                             field('snowflake_role', 'Snowflake 角色', secret=False, default='ACCOUNTADMIN'),
                             field('snowflake_warehouse', 'Snowflake Warehouse', secret=False, default='COMPUTE_WH')],
                  'files': [document('configs/snowflake_rsa_key.p8', 'Snowflake 私钥（PKCS#8 PEM）', format='text',
                                     description='粘贴完整私钥，包含 BEGIN / END 行；无需上传文件。')]},
}

# These registrations intentionally have no operator account form. Their source
# YAML/task preparation owns these inputs; adding a dummy key would change behavior.
TASK_GENERATED = {'canvas': 'Canvas token/domain', 'emails': '任务邮箱连接配置',
                  'k8s': '任务 kubeconfig', 'woocommerce': '任务商店 URL/key/secret'}


def managed(name, source, description, default=None):
    return {'name': name, 'source': source, 'description': description,
            **({'default': default} if default is not None else {})}


SCHEMAS['github']['managed'] = [
    managed('github_allowed_repos', 'task', '由任务配置限定可访问仓库，覆盖基础配置；无需手填。', 'null'),
    managed('github_read_only', 'task', '任务可覆盖为允许写入；保持原任务权限，不在账号表单修改。', '1'),
]
SCHEMAS['github']['http'] = [
    {'id': 'api', 'url': 'https://api.github.com',
     'credential': {'value': 'github_token', 'header': 'Authorization', 'prefix': 'Bearer '},
     'headers': {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}},
    {'id': 'git', 'url': 'https://github.com', 'streaming': True,
     'credential': {'value': 'github_token', 'header': 'Authorization', 'username': 'x-access-token'}},
]
SCHEMAS['notion']['http'] = [
    {'id': binding, 'url': 'https://api.notion.com',
     'credential': {'value': name, 'header': 'Authorization', 'prefix': 'Bearer '}}
    for binding, name in [('prepare', 'notion_integration_key'), ('evaluate', 'notion_integration_key_eval')]
]
SCHEMAS['huggingface']['http'] = [
    {'id': 'api', 'url': 'https://huggingface.co',
     'credential': {'value': 'huggingface_token', 'header': 'Authorization', 'prefix': 'Bearer '}}]
SCHEMAS['wandb']['http'] = [
    {'id': 'api', 'url': 'https://api.wandb.ai',
     'credential': {'value': 'wandb_api_key', 'header': 'Authorization', 'username': 'api'}}]


def sdk_binding(identifier, url, file, secret_field=None, *, protocol='http'):
    return {'id': identifier, 'url': url, 'protocol': protocol,
            'credential': {'file': file, **({'field': secret_field} if secret_field else {}),
                           'forward': True, 'header': 'Authorization'}}


for name, urls in {
    'google_calendar': ['https://www.googleapis.com/calendar', 'https://calendar.googleapis.com', 'https://oauth2.googleapis.com/token'],
    'google_forms': ['https://forms.googleapis.com', 'https://www.googleapis.com/forms', 'https://oauth2.googleapis.com/token'],
    'google_sheet': ['https://sheets.googleapis.com', 'https://www.googleapis.com/drive', 'https://www.googleapis.com/upload/drive', 'https://oauth2.googleapis.com/token'],
}.items():
    SCHEMAS[name]['http'] = [sdk_binding('sdk-'+str(index), url, 'configs/google_credentials.json', 'refresh_token')
                             for index, url in enumerate(urls)]
SCHEMAS['google-cloud']['http'] = [
    sdk_binding('sdk-'+str(index), url, 'configs/gcp-service_account.keys.json', 'private_key')
    for index, url in enumerate(['https://storage.googleapis.com', 'https://www.googleapis.com/storage',
        'https://www.googleapis.com/upload/storage', 'https://bigquery.googleapis.com',
        'https://logging.googleapis.com', 'https://compute.googleapis.com', 'https://oauth2.googleapis.com/token'])]
SCHEMAS['google-cloud']['http'].append(sdk_binding('grpc-logging', 'https://logging.googleapis.com',
    'configs/gcp-service_account.keys.json', 'private_key', protocol='grpc'))
SCHEMAS['snowflake']['http'] = [sdk_binding('sdk', 'https://${snowflake_account}.snowflakecomputing.com',
    'configs/snowflake_rsa_key.p8')]
SCHEMAS['snowflake']['http'][0]['responseError'] = {'field': 'success', 'mode': 'false'}
SCHEMAS['wandb']['http'][0]['responseError'] = {'field': 'errors', 'mode': 'nonempty-array'}
SCHEMAS['notion']['managed'] = [
    managed('notion_allowed_page_ids', 'task', '页面预处理生成本题可访问的页面范围；无需手填。'),
]
for name in ['google_calendar', 'google_sheet', 'google_forms']:
    SCHEMAS[name]['managed'] = [
        managed('google_client_id', 'generated', '从此账号 Google 授权 JSON 的 client_id 读取，无需再填一遍。'),
        managed('google_client_secret', 'generated', '从此账号 Google 授权 JSON 的 client_secret 读取。'),
        managed('google_refresh_token', 'generated', '从此账号 Google 授权 JSON 的 refresh_token 读取；已有客户端负责刷新授权。'),
    ]
SCHEMAS['google_calendar']['managed'].append(
    managed('gcp-oauth.keys.json', 'generated', '运行时从同账号 Google 用户授权生成 Calendar 客户端文件，无需单独上传。'))
SCHEMAS['google_sheet']['managed'] += [
    managed('google_oauth2_credentials_path', 'generated', '包装器把 Google 授权写入本轮独立目录，并自动设置路径。'),
    managed('google_oauth2_token_path', 'generated', '与本轮可刷新的 Google 授权文件保持一致，无需填写路径。'),
    managed('google_sheets_folder_id', 'task', '由任务预处理确定本题的 Drive 文件夹，覆盖基础配置。'),
]
SCHEMAS['google-cloud']['managed'] = [
    managed('gcp_service_account_path', 'generated', '表单中的服务账号 JSON 会写入独立运行目录，自动设置文件路径。'),
    *[managed(name, 'task', '由任务配置限定资源访问范围，覆盖基础配置；无需手填。', 'null')
      for name in ['google_cloud_allowed_buckets', 'google_cloud_allowed_bigquery_datasets',
                   'google_cloud_allowed_log_buckets', 'google_cloud_allowed_instances']],
]
SCHEMAS['snowflake']['managed'] = [
    managed('snowflake_private_key_path', 'generated', '表单中粘贴私钥内容即可；运行时写入独立目录并自动设置文件路径。'),
    managed('snowflake_database', 'task', '未覆盖时使用基础默认值，任务配置有值时以任务为准。', 'SNOWFLAKE'),
    managed('snowflake_schema', 'task', '未覆盖时使用基础默认值，任务配置有值时以任务为准。', 'PUBLIC'),
    managed('snowflake_op_allowed_databases', 'task', '由任务配置限定可操作数据库，覆盖基础配置；无需手填。', 'null'),
]
SCHEMAS['woocommerce'] = {'managed': [
    managed('woocommerce_site_url', 'task', '店铺路径来自任务 token_key_session.py；域名和端口沿用本轮本地服务实例配置，无需在 Hub 填统一地址。'),
    managed('woocommerce_api_key', 'task', '来自对应任务 token_key_session.py 的测试店铺 Key，配合原部署脚本初始化；不会使用统一的云端账号 Key。'),
    managed('woocommerce_api_secret', 'task', '来自对应任务的测试店铺 Secret，与该任务店铺匹配；无需手填。'),
]}
SCHEMAS['canvas'] = {'managed': [
    managed('canvas_api_token', 'task', '使用任务指定的测试用户 Token，配合原 Canvas 部署与账号初始化；无需在 Hub 填统一 Token。'),
    managed('canvas_domain', 'task', '由任务与本轮本地 Canvas 实例配置提供。'),
]}
SCHEMAS['emails'] = {'managed': [
    managed('emails_config_file', 'task', '由任务配置提供本题邮箱账号的连接文件；附件、导出目录仍属于本题 workspace。'),
]}
SCHEMAS['k8s'] = {'managed': [
    managed('kubeconfig_path', 'generated', '由原任务集群部署和任务配置确定，包含实例隔离信息；无需手填。'),
]}


# Preserve opaque SDK uploads/downloads; assets never receive the account key.
for schema in SCHEMAS.values():
    for binding in schema.get('http', []):
        if binding.get('protocol', 'http') == 'http': binding['streaming'] = True
for name, hosts in {
    'huggingface': ['*.huggingface.co', '*.hf.co', '*.amazonaws.com', '*.cloudfront.net'],
    'wandb': ['*.wandb.ai', '*.amazonaws.com', '*.blob.core.windows.net', 'storage.googleapis.com'],
    'snowflake': ['*.amazonaws.com', '*.blob.core.windows.net', 'storage.googleapis.com'],
    'google-cloud': ['*.googleusercontent.com'],
    'google_calendar': ['*.googleusercontent.com'],
    'google_sheet': ['*.googleusercontent.com'],
    'google_forms': ['*.googleusercontent.com'],
}.items():
    source = SCHEMAS[name]['http'][0]
    SCHEMAS[name]['http'].append({'id': 'assets', 'url': source['url'],
        'streaming': True, 'assetHosts': hosts, 'credential': dict(source['credential'])})
