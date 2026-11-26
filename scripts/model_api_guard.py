"""Stop a batch only on explicit failures from the model API error response."""
import asyncio
import json
import os
from pathlib import Path
import re

MARKER = 'fatal_model_api.json'


class FatalModelAPIError(RuntimeError):
    pass


def classify(error):
    status = getattr(error, 'status_code', None)
    if not isinstance(status, int):
        return None
    body = getattr(error, 'body', None)

    def fields(value):
        if isinstance(value, dict):
            return {key: fields(item) for key, item in value.items()
                    if key in {'error', 'code', 'type', 'message', 'error_description', 'status'}}
        if isinstance(value, str):
            try:
                return fields(json.loads(value))
            except ValueError:
                return value
        return value if isinstance(value, (int, float, bool)) else None

    text = json.dumps(fields(body), ensure_ascii=False).lower()
    def has_code(codes):
        return any(re.search(rf'(?<![a-z0-9_]){re.escape(code)}(?![a-z0-9_])', text) for code in codes)

    if status == 401 or has_code(('invalid_api_key', 'authentication_error', 'invalid_token')) or re.search(
            r'\binvalid (?:api )?key\b|\bauthentication failed\b|\bapi key (?:has )?expired\b', text):
        return 'authentication_failed'
    codes = ('insufficient_quota', 'insufficient_balance', 'insufficient_credit', 'insufficient_funds',
             'billing_hard_limit_reached', 'credit_balance_too_low', 'out_of_credits',
             'payment_required', 'pre_consume_token_quota_failed', 'quota_exhausted')
    if status == 402 or has_code(codes) or re.search(
            r'\binsufficient (?:account )?(?:balance|credit|funds|quota)\b|\bcredit balance is too low\b|'
            r'\btoken quota is not enough\b|\bpayment required\b|余额不足|额度不足|积分不足|配额已用尽|欠费', text):
        return 'billing_exhausted'
    if status == 503 and (re.search(r'\bservice\s+[^\s"}]+\s+is unavailable with status:\s*stopped\b', text)
                          or re.search(r'"status"\s*:\s*"stopped"[^\n]{0,500}\b(?:service|deployment)\b', text)):
        return 'service_stopped'
    return None


def raise_if_fatal(error):
    reason = classify(error)
    if reason is None:
        return
    payload = {'reason': reason, 'status_code': error.status_code}
    path = os.getenv('TOOLATHLON_FATAL_API_FILE')
    if path:
        target = Path(path)
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload))
        temporary.replace(target)
    raise FatalModelAPIError(f'Model API circuit breaker: {reason} (HTTP {error.status_code}); restore service then resume')


async def gather_or_cancel(tasks):
    tasks = [asyncio.ensure_future(task) for task in tasks]
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
