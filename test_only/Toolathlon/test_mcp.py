import asyncio
import json
import sys
from configs.token_key_session import all_token_key_session
from utils.app_specific.snowflake.client import execute_query, get_connection, fetch_all_dict
from utils.app_specific.github.api import (
    github_create_user_repo,
    github_get_login,
    github_get_repo,
    github_delete_repo,
)
from utils.aux_tools.web_search import on_web_search_tool_invoke
from utils.aux_tools.ai_webpage_summary import on_ai_webpage_summary_tool_invoke


async def test_webpage_summary():
    # 1. 构造测试参数 (JSON字符串格式)
    test_url = "https://github.com/hkust-nlp/Toolathlon"  # 可以替换为你想测试的实际URL
    test_params = json.dumps({
        "url": test_url,
        "max_tokens": 500
    })
    
    # 2. 模拟 RunContextWrapper
    # 注意: 如果 on_ai_webpage_summary_tool_invoke 内部没有用到 context，
    # 传 None 或一个简单的 Mock 对象通常也是可以的。
    # 这里为了类型安全，我们尝试创建一个最简对象，或者直接传 None 看是否报错。
    mock_context = None 
    
    print(f"🚀 开始测试 AI 网页摘要工具...")
    print(f"🔗 目标URL: {test_url}")
    print("-" * 50)

    # 3. 调用待测函数 (异常直接向上抛出, 由调用方处理)
    result = await on_ai_webpage_summary_tool_invoke(
        context=mock_context,
        params_str=test_params
    )

    print("✅ 调用成功!")
    print(f"📝 摘要结果:\n{result}")

    return result

def test_github():
    GITHUB_REPO_NAME = "Annoy-DataSync"

    github_token = all_token_key_session.github_token
    owner = github_get_login(github_token)

    # Pre-cleanup: if a repo with this name already exists (e.g. left over from
    # a previous failed run), delete it so creation doesn't fail with a 422.
    try:
        github_get_repo(github_token, owner, GITHUB_REPO_NAME)
        github_delete_repo(github_token, owner, GITHUB_REPO_NAME)
    except RuntimeError:
        # 404 (or similar) means the repo doesn't exist yet, which is fine.
        pass

    try:
        github_create_user_repo(github_token, GITHUB_REPO_NAME, private=False)
    finally:
        # Cleanup: delete the repo created above so the test leaves no residue.
        github_delete_repo(github_token, owner, GITHUB_REPO_NAME)

def test_snowflake():
    result = fetch_all_dict("SELECT CURRENT_TIMESTAMP()")
    return result[0]['CURRENT_TIMESTAMP()']



async def main():
    print('Testing github mcp...')
    test_github()
    print('Github mcp test - OK')

    print('Testing snowflake mcp...')
    test_snowflake()
    print('Snowflake mcp test - OK')

    print('Testing serper api...')
    await on_web_search_tool_invoke(None, '{"query": "China", "num_results": 10}')
    print('Serper Api test - OK')

    print('Testing ai webpage summary...')
    await test_webpage_summary()
    print('AI Webpage Summary test - OK')




if __name__ == "__main__":
    asyncio.run(main())