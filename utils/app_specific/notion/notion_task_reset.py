"""Checked reset of one named task page, never the template or entire account."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import sys
import time
from utils.mcp.workspace_http import notion_requests as requests

from utils.app_specific.notion.notion_remove_page import (
    NotionResetError, delete_pages_by_title, get_child_pages,
)


def page_id(value):
    slug = value.split('?', 1)[0].split('#', 1)[0].rstrip('/').rsplit('/', 1)[-1]
    match = re.search(r'([a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}|[a-fA-F0-9]{32})$', slug)
    if not match:
        raise NotionResetError('Invalid Notion page ID')
    return match.group(1).replace('-', '').lower()


def headers(token):
    return {'Authorization': 'Bearer ' + token, 'Notion-Version': '2022-06-28', 'Content-Type': 'application/json'}


def verify_new_page(identifier, parent, title, token):
    response = requests.get('https://api.notion.com/v1/pages/' + identifier,
                            headers=headers(token), timeout=30)
    if response.status_code != 200:
        raise NotionResetError(f'New task page is inaccessible (HTTP {response.status_code})')
    page = response.json()
    if page.get('archived') or page.get('in_trash'):
        raise NotionResetError('New task page is archived')
    if page_id((page.get('parent') or {}).get('page_id', '')) != parent:
        raise NotionResetError('New task page is outside the evaluation parent')
    matching = [child['id'] for child in get_child_pages(parent, headers(token))
                if child.get('type') == 'child_page' and child.get('child_page', {}).get('title') == title]
    if len(matching) != 1 or page_id(matching[0]) != identifier:
        raise NotionResetError('Expected exactly one newly copied task page')


async def settle_new_page_title(identifier, parent, title, token):
    """The hosted MCP copy can overwrite a successful rename while it finishes."""
    deadline = time.monotonic() + 60
    stable_since = None
    last_state = 'page has not been read'
    transient_statuses = {429, 500, 502, 503, 504}
    while time.monotonic() < deadline:
        try:
            response = requests.get('https://api.notion.com/v1/pages/' + identifier,
                                    headers=headers(token), timeout=30)
        except requests.RequestException:
            stable_since = None
            last_state = 'temporary page read connection failure'
            await asyncio.sleep(2)
            continue
        if response.status_code in transient_statuses:
            stable_since = None
            last_state = f'page read HTTP {response.status_code}'
            await asyncio.sleep(2)
            continue
        if response.status_code != 200:
            raise NotionResetError(f'New task page is inaccessible (HTTP {response.status_code})')
        page = response.json()
        if (page.get('archived') or page.get('in_trash') or
                page_id((page.get('parent') or {}).get('page_id', '')) != parent):
            raise NotionResetError('New task page is not active under the evaluation parent')
        current = ''.join(item.get('plain_text', item.get('text', {}).get('content', ''))
                          for prop in page.get('properties', {}).values()
                          if prop.get('type') == 'title' for item in prop.get('title', []))
        last_state = f'expected={title[:160]!r}, observed={current[:160]!r}'
        if current == title:
            if stable_since is None:
                stable_since = time.monotonic()
            if time.monotonic() - stable_since >= 10:
                verify_new_page(identifier, parent, title, token)
                return
        else:
            stable_since = None
            if not re.fullmatch(re.escape(title) + r'(?: \(\d+\))+', current):
                # Copy metadata can be incomplete while Notion finishes. Only
                # wait here; never rename a page with an unrelated title.
                await asyncio.sleep(2)
                continue
            try:
                renamed = requests.patch('https://api.notion.com/v1/pages/' + identifier,
                                         headers=headers(token), timeout=30,
                                         json={'properties': {'title': {'title': [{'text': {'content': title}}]}}})
            except requests.RequestException:
                last_state += '; rename connection failure'
                await asyncio.sleep(2)
                continue
            if renamed.status_code in transient_statuses:
                last_state += f'; rename HTTP {renamed.status_code}'
                # Re-read before retrying: the upstream may have applied the
                # rename even when the response was lost.
                await asyncio.sleep(2)
                continue
            if renamed.status_code != 200:
                raise NotionResetError(f'Unable to rename new task page (HTTP {renamed.status_code})')
        await asyncio.sleep(2)
    raise NotionResetError(f'New task page title did not stabilize after copying: page={identifier}; {last_state}')


async def reset_task_page(source_url, target_url, title, source_key, evaluation_key, output_file):
    output = Path(output_file)
    output.unlink(missing_ok=True)
    source, target = page_id(source_url), page_id(target_url)
    if source == target:
        raise NotionResetError('Template and evaluation parent must be different; reset refused')
    # Check both account bindings before deleting any old evaluation data.
    source_children = get_child_pages(source, headers(source_key))
    if not any(child.get('type') == 'child_page' and child.get('child_page', {}).get('title') == title for child in source_children):
        raise NotionResetError('Template task page not found; reset refused')
    delete_pages_by_title(target, title, headers(evaluation_key), confirm=False, include_copies=True)
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-m', 'utils.app_specific.notion.notion_page_duplicator',
        '--source-parent', source_url, '--child-name', title, '--target-parent', target_url,
        '--output-file', str(output), env={**os.environ, 'NOTION_TOKEN': source_key},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        await process.communicate()
        if process.returncode != 0 or not output.is_file():
            raise NotionResetError('Task page duplication failed; agent must not start')
        identifier = page_id(output.read_text().strip())
        if identifier in {source, target}:
            raise NotionResetError('Duplicator returned a parent page instead of a task page')
        await settle_new_page_title(identifier, target, title, evaluation_key)
    except BaseException:
        output.unlink(missing_ok=True)
        if process.returncode is None:
            process.terminate()
            await process.wait()
        raise


async def run_reset_command(command):
    """Do not let callers treat a failed reset as a missing output file later."""
    from utils.general.helper import run_command
    stdout, stderr, code = await run_command(command, debug=False, show_output=True)
    if code:
        # Only the reset entrypoint's bounded diagnosis is safe to show. Never
        # dump third-party tracebacks, HTTP bodies or arbitrary child stderr.
        summary = next((line for line in stderr.splitlines()
                        if line.startswith('Notion task reset failed')), '')
        if summary:
            print(summary, file=sys.stderr)
        raise NotionResetError(f'Notion reset command failed (exit {code}); task preparation stopped')
    return stdout, stderr, code
