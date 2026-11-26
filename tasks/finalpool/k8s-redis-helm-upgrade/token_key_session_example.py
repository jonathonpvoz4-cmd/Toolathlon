from utils.general.task_resources import task_kubeconfig_path
from addict import Dict
import os
# I am gradually modifying the tokens to the pseudo account in this project

file_path = os.path.abspath(__file__)

kubeconfig_path = task_kubeconfig_path(os.path.dirname(file_path), 'cluster-redis-helm')

all_token_key_session = Dict(
    # k8s
    kubeconfig_path = kubeconfig_path,
)
