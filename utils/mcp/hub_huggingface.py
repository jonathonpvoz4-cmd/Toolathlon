"""Use the pinned Hugging Face SDK's supported HTTP backend extension."""
import os
import requests
from utils.mcp.workspace_http import AccountHTTPAdapter, AssetHTTPAdapter, hub_enabled

if hub_enabled():
    # Native download engines would bypass the SDK HTTP backend.
    os.environ['HF_HUB_DISABLE_XET'] = '1'
    os.environ['HF_HUB_ENABLE_HF_TRANSFER'] = '0'

import huggingface_hub

if hub_enabled():
    from huggingface_hub import constants
    if constants.ENDPOINT.rstrip('/') != 'https://huggingface.co':
        raise ValueError('Configure the Hugging Face Hub upstream before using a custom SDK endpoint')
    constants.HF_HUB_DISABLE_XET = True
    constants.HF_HUB_ENABLE_HF_TRANSFER = False
    def backend():
        session = requests.Session()
        session.mount('https://', AssetHTTPAdapter('huggingface'))
        session.mount('https://huggingface.co/', AccountHTTPAdapter('huggingface', 'api'))
        return session
    huggingface_hub.configure_http_backend(backend)

HfApi = huggingface_hub.HfApi
whoami = huggingface_hub.whoami
snapshot_download = huggingface_hub.snapshot_download
