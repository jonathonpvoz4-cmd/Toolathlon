"""Google SDK entry points; task data and credentials keep their original layout."""
from utils.mcp.workspace_http import hub_enabled
from utils.mcp.hub_sdk_http import install_google_http

install_google_http()

from google.cloud import storage, bigquery, logging, logging_v2
from googleapiclient.discovery import build
from google.cloud.logging_v2.services import config_service_v2
from google.cloud.logging_v2.services.config_service_v2.transports.grpc import ConfigServiceV2GrpcTransport
from google.cloud.logging_v2.services.logging_service_v2.transports.grpc import LoggingServiceV2GrpcTransport


def install_channel(transport):
    original = transport.create_channel
    def create_channel(cls, host='logging.googleapis.com', credentials=None, **kwargs):
        if not hub_enabled(): return original(host, credentials=credentials, **kwargs)
        if host not in {'logging.googleapis.com', 'logging.googleapis.com:443'}:
            raise ValueError('The selected Hub binding does not declare this Google gRPC host')
        import google.auth
        from google.auth.credentials import with_scopes_if_required
        from utils.mcp.hub_grpc import Channel
        if credentials is None: credentials, _ = google.auth.default(scopes=transport.AUTH_SCOPES)
        credentials = with_scopes_if_required(credentials, kwargs.get('scopes') or transport.AUTH_SCOPES)
        return Channel(credentials)
    transport.create_channel = classmethod(create_channel)


if hub_enabled():
    install_channel(ConfigServiceV2GrpcTransport)
    install_channel(LoggingServiceV2GrpcTransport)

ConfigServiceV2Client = config_service_v2.ConfigServiceV2Client
