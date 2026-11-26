from utils.general.task_resources import task_woocommerce_url
from addict import Dict
import os
# I am gradually modifying the tokens to the pseudo account in this project
all_token_key_session = Dict(
    woocommerce_api_key = "ck_woocommerce_token_emma_206rnIn",
    woocommerce_api_secret = "cs_woocommerce_token_emma_206rnIn",
    woocommerce_site_url = task_woocommerce_url('/store81'),
    woocommerce_config_file = os.path.join(os.path.dirname(__file__), "woocommerce_config.json"),
)
