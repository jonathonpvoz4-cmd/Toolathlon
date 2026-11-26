from utils.general.task_resources import task_kubeconfig_path
from addict import Dict
import os

file_path = os.path.abspath(__file__)

folder_id_file = os.path.join(os.path.dirname(file_path), "files", "folder_id.txt")
with open(folder_id_file, "r") as f:
    folder_id = f.read().strip()

kubeconfig_path = task_kubeconfig_path(os.path.dirname(file_path), 'cluster-safety-audit')

all_token_key_session = Dict(
    google_sheets_folder_id = folder_id,
    # k8s
    kubeconfig_path = kubeconfig_path,
)
