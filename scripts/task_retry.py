"""Bounded task retries and selection of one result per task/repetition."""
import json
import os
import tempfile
from pathlib import Path
import re


def retry_marker(directory):
    directory = Path(directory)
    return directory.parent / '.api-retries' / (directory.name + '.json')


def _api_failure(directory):
    directory = Path(directory)
    try:
        status = json.loads((directory / 'status.json').read_text())
        score = json.loads((directory / 'eval_res.json').read_text())
        if (status.get('preprocess') != 'done' or status.get('running') != 'fail'
                or score.get('pass', False) is not None):
            return False
        return bool(re.search(
            r'^Error when running agent - Failed to get response after \d+ retries, error:',
            (directory / 'run.log').read_text(errors='replace'), re.M))
    except (OSError, ValueError, AttributeError):
        return False


def can_retry_api_failure(directory):
    directory = Path(directory)
    return (not (directory / 'fatal_model_api.json').exists()
            and not retry_marker(directory).exists() and _api_failure(directory))


def claim_api_retry(directory):
    marker = retry_marker(directory)
    marker.parent.mkdir(parents=True, exist_ok=True)
    with marker.open('x') as stream:
        json.dump({'additional_attempts': 1, 'reason': 'model_api_retries_exhausted'}, stream)


def selected_result(directory):
    """A deferred retry supersedes its original attempt, even while incomplete."""
    directory = Path(directory)
    marker = retry_marker(directory)
    if marker.exists():
        data = json.loads(marker.read_text())
        if data.get('deferred') is True:
            round_dir = directory.parent.parent
            return round_dir.with_name(round_dir.name + '_retry') / directory.parent.name / directory.name
    return directory


def claim_deferred_retry(directory, reason):
    marker = retry_marker(directory)
    marker.parent.mkdir(parents=True, exist_ok=True)
    # Publish a whole claim atomically without replacing a prior retry budget.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=marker.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({'additional_attempts': 1, 'reason': reason, 'deferred': True}, stream)
        os.link(temporary, marker)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def has_terminal_result(directory, *, retry_exhausted=False):
    directory = Path(directory)
    if (directory / 'fatal_model_api.json').exists():
        return False
    try:
        status = json.loads((directory / 'status.json').read_text())
        score = json.loads((directory / 'eval_res.json').read_text())
        if status.get('preprocess') != 'done' or not isinstance(score, dict) or 'pass' not in score:
            return False
        state = status.get('running')
        if state == 'done':
            return isinstance(status.get('evaluation'), bool) and isinstance(score['pass'], bool)
        if state in ('timeout', 'max_turn_exceeded'):
            return score['pass'] is None or isinstance(score['pass'], bool)
        if state == 'fail' and score['pass'] is None:
            if tool_transport_failure(directory):
                return False
            if _api_failure(directory):
                return retry_exhausted or retry_marker(directory).exists()
            trajectory = json.loads((directory / 'traj_log.json').read_text())
            messages = trajectory.get('messages')
            return (trajectory.get('status') == 'failed'
                    and isinstance(messages, list) and bool(messages))
    except (OSError, ValueError, AttributeError):
        pass
    return False


def tool_transport_failure(directory):
    """A lost tool session is not a completed model answer, even with a partial trace."""
    directory = Path(directory)
    try:
        status = json.loads((directory / 'status.json').read_text())
        score = json.loads((directory / 'eval_res.json').read_text())
        return (status.get('preprocess') == 'done' and status.get('running') == 'fail'
                and score.get('pass', False) is None
                and re.search(r'^(?:HUGGINGFACE_MCP|HUB_TASK)_TRANSPORT_FAILURE\s*$',
                              (directory / 'run.log').read_text(errors='replace'), re.M) is not None)
    except (OSError, ValueError, AttributeError):
        return False


def deferred_retry_reason(directory):
    """Retry unfinished environment/evaluation attempts and exhausted API calls."""
    directory = Path(directory)
    if (directory / 'fatal_model_api.json').exists() or retry_marker(directory).exists():
        return None
    if can_retry_api_failure(directory):
        return 'model_api_retries_exhausted'
    if has_terminal_result(directory):
        return None
    try:
        status = json.loads((directory / 'status.json').read_text())
    except (OSError, ValueError):
        status = {}
    if (status.get('preprocess') == 'cancelled'
            or status.get('running') in {'cancelled', 'timeout'}):
        return None
    return 'environment_or_evaluation_incomplete'
