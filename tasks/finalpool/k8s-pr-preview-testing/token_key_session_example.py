from utils.general.task_resources import task_kubeconfig_path
from addict import Dict
import os
# I am gradually modifying the tokens to the pseudo account in this project

file_path = os.path.abspath(__file__)

kubeconfig_path = task_kubeconfig_path(os.path.dirname(file_path), 'cluster-pr-preview')

all_token_key_session = Dict(
    # k8s
    kubeconfig_path = kubeconfig_path,
    github_allowed_repos = "SimpleShopping", # only allowed to operate this repo
    github_read_only = "0", # if your task does not require write access to the repos, please set to 1, otherwise set to 0
)
