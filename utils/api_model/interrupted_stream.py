"""Preserve partial model output without executing or regenerating it."""
import json
import time
from contextvars import ContextVar
from pathlib import Path
from uuid import uuid4


stream_output_dir = ContextVar('toolathlon_stream_output_dir', default=None)
agent_budget_state = ContextVar('toolathlon_agent_budget_state', default=None)


class ModelOutputInterrupted(Exception):
    def __init__(self, reason, *, reasoning='', content='', tool_calls=False, missing_finish_reason=False):
        super().__init__(reason)
        self.missing_finish_reason = missing_finish_reason
        self.partial_output = {
            'reasoning_content': reasoning, 'content': content,
            'incomplete_tool_calls': tool_calls, 'interrupted': True,
        }


class StreamJournal:
    def __init__(self):
        self.file = None
        self.path = None
        self.last_flush = time.monotonic()
        root = stream_output_dir.get()
        if root:
            directory = Path(root) / 'model_streams'
            directory.mkdir(parents=True, exist_ok=True)
            self.path = directory / (uuid4().hex + '.partial.jsonl')
            self.file = self.path.open('w', buffering=65536)

    def write(self, delta):
        if self.file:
            self.file.write(json.dumps(delta, ensure_ascii=False) + '\n')
            # Keep interrupted output available even before the model returns.
            if time.monotonic() - self.last_flush >= 1:
                self.file.flush()
                self.last_flush = time.monotonic()

    def close(self, completed=False):
        if self.file:
            self.file.close()
            if completed:
                self.path.unlink(missing_ok=True)
