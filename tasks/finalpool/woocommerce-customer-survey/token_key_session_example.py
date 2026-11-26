from utils.general.task_resources import task_woocommerce_url
from addict import Dict
import os

all_token_key_session = Dict(
    woocommerce_api_key = "ck_woocommerce_token_Jcruz821xB00",
    woocommerce_api_secret = "cs_woocommerce_token_Jcruz821xB00",
    woocommerce_site_url = task_woocommerce_url('/store87'),

    emails_config_file = os.path.join(os.path.dirname(__file__),"email_config.json"),
)
