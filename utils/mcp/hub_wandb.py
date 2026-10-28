"""Route the pinned W&B SDK GraphQL transports, including login verification."""
from utils.mcp.workspace_http import AccountHTTPAdapter, AssetHTTPAdapter, hub_enabled
from urllib.parse import urlsplit

import wandb


class _SdkRequests:
    def __init__(self, original): self.original = original

    def __getattr__(self, name):
        if name in {'get', 'post', 'put', 'patch', 'delete', 'head', 'options', 'request'}:
            def request(*args, **kwargs):
                if not hub_enabled(): return getattr(self.original, name)(*args, **kwargs)
                import requests
                with requests.Session() as session:
                    session.mount('https://', AssetHTTPAdapter('wandb'))
                    session.mount('https://api.wandb.ai/', AccountHTTPAdapter('wandb', 'api'))
                    return getattr(session, name)(*args, **kwargs)
            return request
        return getattr(self.original, name)


def Api(*args, **kwargs):
    if hub_enabled():
        import importlib
        importlib.import_module('wandb.util')
        if not isinstance(wandb.util.requests, _SdkRequests):
            wandb.util.requests = _SdkRequests(wandb.util.requests)
        from wandb.sdk.lib.gql_request import GraphQLSession
        from wandb.sdk.internal.internal_api import Api as InternalApi
        class HubGraphQLSession(GraphQLSession):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                if not hub_enabled(): return
                url = urlsplit(self.url)
                if url.scheme != 'https' or url.hostname != 'api.wandb.ai' or url.port not in {None, 443}:
                    raise ValueError('Configure the W&B Hub upstream before using a custom SDK endpoint')
                self.session.mount('https://', AssetHTTPAdapter('wandb'))
                self.session.mount('https://api.wandb.ai/', AccountHTTPAdapter('wandb', 'api'))
        # Both clients construct their sessions before verification requests begin.
        wandb.Api.__init__.__globals__['GraphQLSession'] = HubGraphQLSession
        InternalApi.__init__.__globals__['GraphQLSession'] = HubGraphQLSession
    return wandb.Api(*args, **kwargs)
