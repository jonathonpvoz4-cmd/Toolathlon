from utils.mcp.tool_servers import MCPServerManager, call_tool_with_retry
import asyncio
import os
import json
from utils.general.helper import print_color

async def test_huggingface_server():
    """Test the Hugging Face MCP server connectivity and basic functionality."""

    # Create a test directory
    test_workspace = "./dumps_for_mcp_check/huggingface_test"
    os.makedirs(test_workspace, exist_ok=True)

    # Initialize MCP Server Manager
    print_color("Initializing MCP Server Manager...", "cyan")
    mcp_manager = MCPServerManager(agent_workspace=test_workspace)

    server_name = "huggingface"

    if server_name not in mcp_manager.servers:
        print_color(f"Error: Server '{server_name}' not found in available servers", "red")
        print_color(f"Available servers: {list(mcp_manager.servers.keys())}", "yellow")
        return False

    server = mcp_manager.servers[server_name]
    max_retries = 4

    for attempt in range(1, max_retries + 1):
        try:
            print_color(f"Testing Hugging Face server (attempt {attempt}/{max_retries})...", "yellow")

            async with server as active_server:
                # List available tools
                print_color("Fetching available tools...", "cyan")
                tools = await active_server.list_tools()

                print_color(f"✓ Successfully connected to Hugging Face server", "green")
                print_color(f"  Available tools: {len(tools)}", "green")

                # Display tool names
                if tools:
                    print_color("\nAvailable Hugging Face tools:", "cyan")
                    for tool in tools:
                        tool_name = tool.name if hasattr(tool, 'name') else str(tool)
                        print_color(f"  - {tool_name}", "white")

                # Save test results
                result = {
                    "server_name": server_name,
                    "status": "success",
                    "tools_count": len(tools),
                    "tools": [tool.name if hasattr(tool, 'name') else str(tool) for tool in tools],
                    "attempt": attempt
                }

                with open(f"{test_workspace}/test_result.json", "w") as f:
                    json.dump(result, f, indent=2)

                print_color(f"\n✓ Hugging Face server test completed successfully", "green")
                print_color(f"  Results saved to: {test_workspace}/test_result.json", "cyan")
                return True

        except Exception as e:
            if attempt < max_retries:
                print_color(f"✗ Test failed (attempt {attempt}/{max_retries}): {e}", "red")
                print_color(f"  Retrying in 0.8 seconds...", "yellow")
                await asyncio.sleep(0.8)
                continue
            else:
                print_color(f"✗ Hugging Face server test failed after {max_retries} attempts", "red")
                print_color(f"  Error: {e}", "red")

                # Save error results
                result = {
                    "server_name": server_name,
                    "status": "failed",
                    "error": str(e),
                    "attempts": max_retries
                }

                with open(f"{test_workspace}/test_result.json", "w") as f:
                    json.dump(result, f, indent=2)

                return False

    return False


async def main():
    print_color("=" * 60, "cyan")
    print_color("Hugging Face MCP Server Test", "cyan")
    print_color("=" * 60, "cyan")
    print()

    success = await test_huggingface_server()

    print()
    print_color("=" * 60, "cyan")
    if success:
        print_color("Test Status: PASSED", "green")
    else:
        print_color("Test Status: FAILED", "red")
    print_color("=" * 60, "cyan")

    return 0 if success else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
