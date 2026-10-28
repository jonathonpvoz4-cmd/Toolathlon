"""Reset one task page, aborting preparation on any failed step."""
import argparse
import asyncio

from utils.app_specific.notion.notion_task_reset import reset_task_page
from utils.app_specific.notion.notion_remove_page import NotionResetError
from utils.mcp.workspace_credentials import CredentialError


async def main():
    from configs.token_key_session import all_token_key_session as account
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duplicated_page_id_file', required=True)
    parser.add_argument('--needed_subpage_name', required=True)
    args = parser.parse_args()
    await reset_task_page(account.source_notion_page_url, account.eval_notion_page_url,
                          args.needed_subpage_name, account.notion_integration_key,
                          account.notion_integration_key_eval, args.duplicated_page_id_file)
    print('Notion task page reset and access check completed')


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (NotionResetError, CredentialError) as exc:
        raise SystemExit('Notion task reset failed: ' + str(exc)) from None
    except Exception as exc:
        raise SystemExit(f'Notion task reset failed ({type(exc).__name__}); agent must not start. Check authorization, parent pages and template.') from None
