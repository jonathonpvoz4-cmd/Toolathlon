"""Infrastructure failures that must not be returned as model tool errors."""

FAILURE_MARKER = 'HUB_TASK_TRANSPORT_FAILURE'


class HubTaskTransportError(RuntimeError):
    pass
