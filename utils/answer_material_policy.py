"""Exclude known benchmark answer sources from model-facing tool traffic.

This source policy is not a network sandbox: arbitrary code can construct new
URLs, and the task's permission boundary remains responsible for local files.
"""
from __future__ import annotations

import asyncio
import ast
import base64
import binascii
from contextvars import ContextVar
from dataclasses import replace
import html
import json
import logging
import re
from typing import Any
from urllib.parse import unquote, urlsplit

VERSION = 'toolathlon-answer-access-v10'
DENIED = 'Access denied: benchmark answer materials are not available during evaluation.'
LICENSE_TASK = 'finalpool/dataset-license-issue'
# Whole repository IDs only. A bare task name or license is normal evidence.
_LICENSE_COPY = re.compile(
    r'(?<![\w.-])([\w.-]+)/(annoy-datasync(?:\.git)?|annoy-pyedu-rs(?:-raw)?)(?![\w-]|\.[\w.-])',
    re.I,
)
license_task_targets: ContextVar[frozenset[str] | None] = ContextVar(
    'license_task_targets', default=None,
)


def license_targets_from_state(state: dict) -> frozenset[str]:
    """Read only the three identities that trusted preprocessing created."""
    repo = state['github_repo']
    urls = state['hf_datasets']
    targets = {repo.casefold()}
    if not re.fullmatch(r'[\w-]+/annoy-datasync', repo, re.I):
        raise ValueError('Invalid GitHub target in license task state')
    for key, name in [('reasoning', 'annoy-pyedu-rs'), ('raw', 'annoy-pyedu-rs-raw')]:
        match = re.fullmatch(r'https://huggingface\.co/datasets/([\w.-]+/' + name + r')', urls[key], re.I)
        if not match:
            raise ValueError('Invalid Hugging Face target in license task state')
        targets.add(match[1].casefold())
    return frozenset(targets)


def _other_license_copy(text: str, targets: frozenset[str]) -> bool:
    return any(
        match[1].casefold() not in {'repo', 'repos', 'datasets'}
        and match[0].casefold().removesuffix('.git') not in targets
        for match in _LICENSE_COPY.finditer(text)
    )
_REPO_NAME = r'(?:toolathlon|toolathlon[-_]gym|toolathlon-website|toolathlon-traj-website)'
_REPOSITORY = re.compile(r'(?<![\w.-])[\w.-]+/' + _REPO_NAME + r'(?:\.git)?(?![\w.-])', re.I)
_ANSWER_DOMAINS = {'autobenchaudit.com', 'toolathlon-traj.xyz', 'toolathlon.xyz'}
_ANSWER_REPO_IDS = {'1067870538', '1175612051'}
_ANSWER_REPOSITORIES = {
    'hyz17/toolathlon-official',
    'hkustnlp-cmyk/toolathlon-mvp',
    'toolathlon-verified/luffy',
}
_ANSWER_ISSUE_REPOSITORIES = {
    'toolathlon/annoy-datasync', 'lonelybird/annoy-datasync', 'piekeniuszwu/annoy-datasync',
}
_ANSWER_GITHUB_SUBTREES = {'zjunlp/skills': 'skills/skill-collections/toolathlon'}
_GIT_REMOTE = re.compile(
    r'(?:git@github\.com:|(?:ssh|git)://(?:git@)?github\.com/)'
    r'([\w.-]+)/([\w.-]+)', re.I)
_ANSWER_DATASETS = {
    'chengze79/annoy-pyedu-rs-raw-license', 'chengze79/annoy-pyedu-rs-license',
    'asfafaf4546/annoy-datasync-license-response',
    'dongbobo/annoy-datasync-license-report', 'dongbobo/annoy-datasync-license-snapshot',
    'dongbobo/annoy-license-metadata', 'dongbobo/annoy-pyedu-rs-raw',
    'asd12dsa21dsa21dsa/annoy-pyedu-rs', 'asd12dsa21dsa21dsa/annoy-pyedu-rs-raw',
    'asd21dwsa/annoy-pyedu-rs', 'dsadczc12/annoy-pyedu-rs-raw',
    'lovebird-ll/annoy-pyedu-rs', 'naive-ai-eval/annoy-pyedu-rs',
    'sad12dxcas/annoy-pyedu-rs', 'sddsdf44/annoy-pyedu-rs',
    'dongbobo/unified-toolcalls-canonical',
    'dusersad12/unified-tool-call-corpus-29', 'dusersad12/unified-tool-calls',
    'dusersad12/unified-tool-call-merge', 'dusersad12/glaive_toolcall_en',
    'dusersad12/toolace-function-calls', 'dusersad12/xlam-function-calling-60k',
    *(f'{owner}/{dataset}' for owner in (
        'toolathlon-2556', 'toolevalxm', 'll3213', 'cassieyu74', 'justin7219', 'cskin666',
    ) for dataset in ('annoy-pyedu-rs-raw', 'annoy-pyedu-rs')),
}
_ENCODED_LITERAL = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{24,16384}={0,2}(?![A-Za-z0-9+/_=-])")
_NON_REPO_PREFIXES = {'repos', 'users', 'orgs', 'organizations', 'workspace', 'tmp', 'home',
                      'benchmark', 'skill-collections', 'projects', 'buckets'}
_URL = re.compile(r'(?:https?|hf)://[^\s"\x27<>\\]+', re.I)
_KNOWN_DATASET = re.compile(r'(?<![\w.-])(?:' + '|'.join(
    re.escape(value) for value in sorted(_ANSWER_DATASETS)) + r')(?![\w.-])', re.I)
_ISSUE_SEARCH = re.compile(r'\brepo:(' + '|'.join(
    re.escape(value) for value in sorted(_ANSWER_ISSUE_REPOSITORIES)) + r')(?![\w.-])', re.I)
_SEARCH_ITEMS = re.compile(r'(?m)(?=^Title:\s|^\d+\.\s+(?:\*\*|\[)|^###\s+|^## Operation \d+\b)')
logger = logging.getLogger(__name__)


def _github_answer_repository(owner: str, repo: str) -> bool:
    if not re.fullmatch(r'[\w.-]+', owner) or not re.fullmatch(r'[\w.-]+', repo):
        return False
    identity = owner.casefold() + '/' + repo.casefold().removesuffix('.git')
    return bool(re.fullmatch(_REPO_NAME, repo.casefold().removesuffix('.git'))
                or identity in _ANSWER_REPOSITORIES)


def _github_answer_path(owner: str, repo: str, path: str) -> bool:
    identity = owner.casefold() + '/' + repo.casefold().removesuffix('.git')
    path = path.strip('/').casefold()
    if (identity in _ANSWER_ISSUE_REPOSITORIES and identity not in (license_task_targets.get() or ())
            and path.split('/')[0] in {'issues', 'discussions'}):
        return True
    subtree = _ANSWER_GITHUB_SUBTREES.get(identity)
    return bool(subtree and (path == subtree or path.startswith(subtree + '/')
                            or '/' + subtree + '/' in '/' + path + '/'))


def _shared_query_history(text: str) -> bool:
    # Match system history views, not business tables or TABLES/COLUMNS metadata.
    sql = re.sub(r'/\*.*?\*/|--[^\r\n]*', '', text, flags=re.S)
    sql = re.sub(r'[`"\'\\]+', '', sql).casefold()
    sql = re.sub(r'\s*\.\s*', '.', sql)
    if re.search(r'information_schema\.jobs(?:_[a-z0-9_]+)?\b', sql):
        return True
    if re.search(r'(?:information_schema|snowflake\.account_usage)\.'
                 r'query_history(?:_[a-z0-9_]+)?\b', sql):
        return True
    # Also cover the BigQuery SDK/discovery API when invoked from local code.
    compact = re.sub(r'\s+', '', sql)
    return bool('bigquery' in compact and re.search(
        r'\.list_jobs\(|\.jobs\(\)\.list\(|\.jobs\.list\(', compact))


def _normalized_name(value: str) -> str:
    return re.sub(r'[-_.]+', '-', value.casefold().removesuffix('.git'))


def _code_strings(text: str):
    """Resolve bounded literal assignments and f-strings without executing code."""
    if len(text) > 128 * 1024:
        return
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return
    values = {}
    nodes = list(ast.walk(tree))[:4096]
    literals = {n.value.casefold() for n in nodes
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    if 'huggingface.co' in text:
        for dataset in _ANSWER_DATASETS:
            owner, name = dataset.split('/', 1)
            if owner in literals and name in literals:
                yield f'https://huggingface.co/datasets/{dataset}'

    def resolve(node, depth=0):
        if depth > 20:
            return None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value[:16384]
        if isinstance(node, ast.Name):
            return values.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = resolve(node.left, depth + 1), resolve(node.right, depth + 1)
            return (left + right)[:16384] if left is not None and right is not None else None
        if isinstance(node, ast.JoinedStr):
            parts = [resolve(n.value if isinstance(n, ast.FormattedValue) else n, depth + 1)
                     for n in node.values]
            return ''.join(parts)[:16384] if all(p is not None for p in parts) else None
        return None

    for node in nodes:
        if isinstance(node, ast.Assign):
            value = resolve(node.value)
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values.pop(target.id, None)
                    if value is not None:
                        values[target.id] = value
        if isinstance(node, (ast.JoinedStr, ast.BinOp)):
            value = resolve(node)
            if value:
                yield value


def _join_string_literals(text: str) -> str:
    """Scan each quoted span once, including malformed or escaped input."""
    output = []
    position = 0
    previous_end = 0
    chain_start = chain_end = None
    pieces = []

    def flush():
        nonlocal previous_end
        if len(pieces) > 1:
            try:
                combined = ''.join(ast.literal_eval(piece) for piece in pieces)
            except (ValueError, SyntaxError, RecursionError):
                return
            output.extend((text[previous_end:chain_start], combined))
            previous_end = chain_end

    while position < len(text):
        if text[position] == '\\':
            position = min(position + 2, len(text))
            continue
        if text[position] not in "\"'":
            position += 1
            continue
        start = position
        quote = text[position]
        position += 1
        while position < len(text):
            char = text[position]
            position += 1
            if char == '\\':
                position = min(position + 1, len(text))
            elif char == quote:
                break
        else:
            break
        if chain_end is not None and text[chain_end:start].strip() == '+':
            pieces.append(text[start:position])
        else:
            flush()
            chain_start = start
            pieces = [text[start:position]]
        chain_end = position
    flush()
    output.append(text[previous_end:])
    return ''.join(output)


def _literal_variants(text: str):
    """Inspect bounded literal encodings without executing model code."""
    text = re.sub(r'\\(?:x([0-9a-fA-F]{2})|u([0-9a-fA-F]{4}))',
                  lambda match: chr(int(match.group(1) or match.group(2), 16)), text)
    yield _join_string_literals(text) if '+' in text else text
    yield from _code_strings(text)
    for token in list(_ENCODED_LITERAL.finditer(text))[:128]:
        value = token.group()
        try:
            raw = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
            decoded = raw.decode('utf-8')
        except (ValueError, UnicodeError, binascii.Error):
            continue
        if decoded.isprintable() or decoded.startswith(('http', '{', '[')):
            yield decoded


def protected_source(value: Any, _depth: int = 0) -> bool:
    targets = license_task_targets.get()
    if isinstance(value, dict):
        owner = value.get('owner') or value.get('organization')
        repo = value.get('repo') or value.get('repository')
        if isinstance(owner, str) and isinstance(repo, str) and _github_answer_repository(owner, repo):
            return True
        if isinstance(owner, str) and isinstance(repo, str):
            identity = owner.casefold() + '/' + repo.casefold()
            if targets is not None and _other_license_copy(identity, targets):
                return True
            if identity in _ANSWER_ISSUE_REPOSITORIES and identity not in (targets or ()) and any(
                    key in value for key in ('issue_number', 'issueNumber')):
                return True
            if isinstance(value.get('path'), str) and _github_answer_path(owner, repo, value['path']):
                return True
        return any(protected_source(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(protected_source(item) for item in value)
    if not isinstance(value, str):
        return False
    text = html.unescape(value).replace('\\/', '/')
    for _ in range(3):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    if _shared_query_history(text):
        return True
    if targets is not None and _other_license_copy(text, targets):
        return True
    if any(match[0].casefold() not in (targets or ()) for match in _KNOWN_DATASET.finditer(text)) \
            or any(match[1].casefold() not in (targets or ()) for match in _ISSUE_SEARCH.finditer(text)):
        return True
    if _depth < 2:
        for variant in _literal_variants(text):
            if variant != text and protected_source(variant, _depth + 1):
                return True
    if any(_github_answer_repository(*match.groups()) for match in _GIT_REMOTE.finditer(text)):
        return True
    for match in _URL.finditer(text):
        try:
            url = urlsplit(match.group().rstrip(').,;]}'))
        except ValueError:
            continue
        host = (url.hostname or '').casefold().rstrip('.')
        if any(host == domain or host.endswith('.' + domain) for domain in _ANSWER_DOMAINS):
            return True
        parts = url.path.strip('/').split('/')
        if host in {'github.com', 'www.github.com', 'raw.githubusercontent.com'}:
            if len(parts) >= 2 and _github_answer_repository(parts[0], parts[1]):
                return True
            if len(parts) >= 3 and _github_answer_path(parts[0], parts[1], '/'.join(parts[2:])):
                return True
        elif host == 'api.github.com':
            if len(parts) >= 3 and parts[0] == 'repos' and _github_answer_repository(parts[1], parts[2]):
                return True
            if len(parts) >= 4 and parts[0] == 'repos' and _github_answer_path(
                    parts[1], parts[2], '/'.join(parts[3:]).removeprefix('contents/')):
                return True
            if len(parts) >= 2 and parts[0] == 'repositories' and parts[1] in _ANSWER_REPO_IDS:
                return True
        elif host in {'huggingface.co', 'hf.co', 'hf-mirror.com'} or url.scheme == 'hf' and host == 'datasets':
            dataset = ['datasets', *parts] if url.scheme == 'hf' else parts[1:] if parts[:1] == ['api'] else parts
            if len(dataset) >= 3 and dataset[0] == 'datasets':
                if (_normalized_name(dataset[2]) in {'toolathlon-trajectories', 'toolathlon-verified-trajectories'}
                        or re.fullmatch(r'annoy-pyedu-rs(?:-raw)?-(?:with-)?licenses?',
                                        _normalized_name(dataset[2]))
                        or ('/'.join(dataset[1:3]).casefold() in _ANSWER_DATASETS
                            and '/'.join(dataset[1:3]).casefold() not in (targets or ()))):
                    return True
        elif any(item.group().split('/')[0].casefold() not in _NON_REPO_PREFIXES
                 for item in _REPOSITORY.finditer(url.path)):
            return True
    # Remove complete URLs first: github.com/toolathlon/SomeTaskRepository
    # denotes the legitimate task organization, not an answer repository.
    plain = _URL.sub('', text)
    return any(match.group().split('/')[0].casefold() not in _NON_REPO_PREFIXES
               for match in _REPOSITORY.finditer(plain))


def _clean_html_document(value: str):
    # Only used for an explicitly requested, allowed document. Remove complete
    # linked blocks, never leave an answer snippet with just its URL stripped.
    if not re.search(r'<(?:html|body|div|article|p)\b', value, re.I):
        return None
    safe = re.sub(r'<(head|script|style)\b[^>]*>.*?</\1\s*>', '', value, flags=re.I | re.S)
    for tag in ('tr', 'li', 'p', 'a'):
        pattern = re.compile(r'<' + tag + r'\b[^>]*>.*?</' + tag + r'\s*>', re.I | re.S)
        safe = pattern.sub(lambda match: '' if protected_source(match.group()) else match.group(), safe)
    return safe if safe.strip() and not protected_source(safe) else None


def filter_value(value: Any, *, allowed_document: bool = False) -> Any:
    """Remove entire identified search hits; never keep an orphan answer snippet."""
    if not protected_source(value):
        return value
    if isinstance(value, list):
        return [filtered for item in value
                if (not protected_source(item) or isinstance(item, dict) and item.get('type') == 'text')
                and (filtered := filter_value(item, allowed_document=allowed_document)) is not None]
    if isinstance(value, dict):
        identity = {k: v for k, v in value.items() if k in {
            'url', 'link', 'uri', 'href', 'path', 'repository', 'repo', 'owner',
            'organization', 'full_name', 'source',
            'issue_url',
            'id', 'repo_id', 'repoId', 'dataset', 'dataset_id',
        }}
        if protected_source(identity):
            return None
        return {k: filtered for k, item in value.items()
                if (filtered := filter_value(item, allowed_document=allowed_document)) is not None}
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        parsed = None
        # Some native local tools serialize dictionaries using Python repr.
        # Filter their individual records instead of rejecting a whole history.
        if len(value) <= 128 * 1024 and value.lstrip().startswith(('{', '[')):
            try:
                parsed = ast.literal_eval(value)
            except (ValueError, SyntaxError, RecursionError):
                pass
    if isinstance(parsed, (list, dict)):
        safe = filter_value(parsed, allowed_document=allowed_document)
        return json.dumps(safe, ensure_ascii=False) if safe else None
    parts = _SEARCH_ITEMS.split(value)
    if len(parts) > 1 and not protected_source(parts[0]):
        kept = [part for part in parts[1:] if not protected_source(part)]
        if kept:
            return ''.join(kept)
    return _clean_html_document(value) if allowed_document else None


async def filtered_output(value: Any, tool: str, request: Any = None) -> Any:
    allowed_document = (isinstance(request, dict) and isinstance(request.get('url'), str)
                        and request['url'].startswith(('http://', 'https://'))
                        and not protected_source(request['url']))
    if isinstance(value, str) and len(value) > 128 * 1024:
        safe = await asyncio.to_thread(filter_value, value, allowed_document=allowed_document)
    else:
        safe = filter_value(value, allowed_document=allowed_document)
    if safe != value:
        logger.warning('answer_material_blocked policy=%s phase=response tool=%s', VERSION, tool)
        if isinstance(safe, (dict, list)) and safe:
            safe = json.dumps(safe, ensure_ascii=False)
        return safe if isinstance(safe, str) and safe.strip() else DENIED
    return value


def request_denied(value: Any, tool: str) -> bool:
    if protected_source(value):
        logger.warning('answer_material_blocked policy=%s phase=request tool=%s', VERSION, tool)
        return True
    return False


def protect_local_tools(tools: list) -> list:
    """Wrap only model-facing callbacks; preprocess/evaluation remain native."""
    from agents.tool import FunctionTool
    result = []
    for tool in tools:
        if not isinstance(tool, FunctionTool):
            result.append(tool)
            continue
        callback, name = tool.on_invoke_tool, tool.name
        async def invoke(context, arguments, _callback=callback, _name=name):
            try:
                parsed = json.loads(arguments)
            except (ValueError, TypeError):
                parsed = arguments
            if request_denied(parsed, _name):
                return DENIED
            return await filtered_output(await _callback(context, arguments), _name, parsed)
        result.append(replace(tool, on_invoke_tool=invoke))
    return result
