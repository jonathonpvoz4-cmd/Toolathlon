import json
import sys
import asyncio
from utils.aux_tools.ai_webpage_summary import on_ai_webpage_summary_tool_invoke

async def run_test():
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
    
    try:
        # 3. 调用待测函数
        result = await on_ai_webpage_summary_tool_invoke(
            context=mock_context, 
            params_str=test_params
        )
        
        print("✅ 调用成功!")
        print(f"📝 摘要结果:\n{result}")
        
    except Exception as e:
        print(f"❌ 测试失败: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

# 运行异步测试
asyncio.run(run_test())