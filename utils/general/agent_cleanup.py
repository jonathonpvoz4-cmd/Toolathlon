"""Bound isolated agent shutdown after its final trajectory has been saved."""
from contextvars import ContextVar
import os
import sys
from threading import Timer


cleanup_watchdog = ContextVar('cleanup_watchdog', default=None)


class AgentCleanupWatchdog:
    def __init__(self, timeout=60):
        self.timeout = timeout
        self.timer = None

    def arm(self, exit_code, manager):
        if self.timer is not None:
            return

        def expired():
            try:
                pending = [name for name, task in list(manager._server_tasks.items())
                           if not task.done()] if manager else []
                print(f'[agent-cleanup] exceeded {self.timeout}s after saving trajectory; '
                      f'pending MCP servers: {", ".join(pending) or "none"}; '
                      'exiting isolated agent so evaluation can continue',
                      file=sys.stderr, flush=True)
            finally:
                os._exit(exit_code)

        self.timer = Timer(self.timeout, expired)
        self.timer.daemon = True
        self.timer.start()

    def cancel(self):
        if self.timer is not None:
            self.timer.cancel()
