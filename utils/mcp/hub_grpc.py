"""Synchronous unary gRPC channel over the authenticated Hub HTTP gateway.

The original generated Google transport still owns protobufs, pagination, error
mapping and retry policies. No REST reimplementation or second MCP is introduced.
"""
import base64
import gzip
import struct
import time
from urllib.parse import unquote

import grpc
import requests
from google.auth.transport.requests import Request

from utils.mcp.hub_sdk_http import mapping


class RpcError(grpc.RpcError, grpc.Call, grpc.Future):
    def __init__(self, status, message, metadata=()):
        self.status, self.message, self.metadata = status, message, metadata
    def code(self): return next((code for code in grpc.StatusCode if code.value[0] == self.status), grpc.StatusCode.UNKNOWN)
    def details(self): return self.message
    def initial_metadata(self): return ()
    def trailing_metadata(self): return self.metadata
    def cancel(self): return False
    def is_active(self): return False
    def time_remaining(self): return None
    def add_callback(self, callback): return False
    def result(self, timeout=None): raise self
    def exception(self, timeout=None): return self
    def traceback(self, timeout=None): return None
    def running(self): return False
    def done(self): return True
    def cancelled(self): return self.status == 1
    def add_done_callback(self, callback): callback(self)


class Unary(grpc.UnaryUnaryMultiCallable):
    def __init__(self, channel, method, serialize, deserialize):
        self.channel, self.method = channel, method
        self.serialize, self.deserialize = serialize, deserialize
    def __call__(self, request, timeout=None, metadata=(), credentials=None, wait_for_ready=None, compression=None):
        if self.channel.closed:
            raise RpcError(14, 'Hub gRPC channel is closed')
        headers = {name: value for name, value in (metadata or ()) if isinstance(value, str)}
        url = 'https://logging.googleapis.com' + self.method
        started = time.monotonic()
        auth_request = Request()
        def refresh_request(*args, **kwargs):
            if timeout is not None:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0: raise RpcError(4, 'Hub gRPC deadline exceeded')
                kwargs['timeout'] = min(kwargs.get('timeout') or 120, remaining)
            return auth_request(*args, **kwargs)
        self.channel.credentials.before_request(refresh_request, 'POST', url, headers)
        if timeout is not None:
            timeout -= time.monotonic() - started
            if timeout <= 0: raise RpcError(4, 'Hub gRPC deadline exceeded')
        data = self.serialize(request) if self.serialize else request
        frame = b'\0' + struct.pack('>I', len(data)) + data
        target, headers = mapping(url, headers, protocol='grpc')
        headers['Content-Type'] = 'application/grpc'
        try:
            response = requests.post(target, headers=headers, data=frame, timeout=timeout, allow_redirects=False)
        except requests.Timeout:
            raise RpcError(4, 'Hub gRPC deadline exceeded') from None
        except requests.RequestException:
            raise RpcError(14, 'Hub gRPC connection unavailable') from None
        if response.status_code != 200:
            raise RpcError({401: 16, 403: 7, 429: 8, 504: 4}.get(response.status_code, 14),
                           f'Hub gRPC request failed (HTTP {response.status_code})')
        status = int(response.headers.get('grpc-status', '2'))
        if status:
            trailers = []
            if response.headers.get('grpc-status-details-bin'):
                trailers.append(('grpc-status-details-bin', base64.b64decode(response.headers['grpc-status-details-bin'])))
            raise RpcError(status, unquote(response.headers.get('grpc-message', 'Upstream gRPC request failed')), trailers)
        content = response.content
        if len(content) < 5 or len(content) != 5 + struct.unpack('>I', content[1:5])[0]:
            raise RpcError(13, 'Invalid unary gRPC response')
        payload = content[5:]
        if content[0] == 1 and response.headers.get('grpc-encoding') == 'gzip': payload = gzip.decompress(payload)
        elif content[0] != 0: raise RpcError(13, 'Unsupported gRPC response encoding')
        return self.deserialize(payload) if self.deserialize else payload
    def with_call(self, *args, **kwargs):
        result = self(*args, **kwargs)
        return result, RpcError(0, '')
    def future(self, *args, **kwargs):
        raise NotImplementedError('This benchmark uses synchronous unary gRPC calls')


class Unsupported:
    def __call__(self, *args, **kwargs): raise RpcError(12, 'Streaming gRPC is not configured')


class Channel(grpc.Channel):
    def __init__(self, credentials):
        self.credentials, self.closed = credentials, False
    def unary_unary(self, method, request_serializer=None, response_deserializer=None, _registered_method=False, **kwargs):
        return Unary(self, method.decode() if isinstance(method, bytes) else method, request_serializer, response_deserializer)
    def unary_stream(self, *args, **kwargs): return Unsupported()
    def stream_unary(self, *args, **kwargs): return Unsupported()
    def stream_stream(self, *args, **kwargs): return Unsupported()
    def subscribe(self, callback, try_to_connect=False): callback(grpc.ChannelConnectivity.READY)
    def unsubscribe(self, callback): pass
    def close(self): self.closed = True
