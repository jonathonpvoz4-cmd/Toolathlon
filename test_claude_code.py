from claude_agent_sdk import ThinkingBlock, ToolResultBlock, ToolUseBlock, ClaudeAgentOptions, ClaudeSDKClient, AssistantMessage, UserMessage, TextBlock
import os, datetime, asyncio, uuid
ENV = {
        "ANTHROPIC_AUTH_TOKEN": 'sk-agV4SlbBbrxak3DvBGwr15ayJGz83fv4dCLfNBLCMcPiz4H8',
        # 强制使用本地代理地址
        "ANTHROPIC_BASE_URL": "http://115.120.120.64:8082",
        "ANTHROPIC_MODEL": 'deepseek-v4-pro',
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-v4-pro",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-v4-pro",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-v4-flash",
        "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-v4-flash",
        "NODE_TLS_REJECT_UNAUTHORIZED": "0",
        #"CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
        "CLAUDE_CONFIG_DIR": '/mnt/djc/Toolathlon/.claude'
    }

async def test_claude():

    options = ClaudeAgentOptions(
                system_prompt=f'',
                env=ENV,
                permission_mode='bypassPermissions',
                cwd='',
                max_turns=100,
                thinking={
                    "type": "enabled",
                    "budget_tokens": 64000
                },
                effort="max",
            )
    
    client = ClaudeSDKClient(options=options)
    await client.connect()

    user_prompt = 'solve equation 3x + 5 = 17'

    await client.query(user_prompt)
    async for message in client.receive_response():
            
        msg_type = type(message).__name__
        if msg_type == 'AssistantMessage' or msg_type == 'UserMessage':
            for block in message.content:
                print(block)

    

asyncio.run(test_claude())


    