sudo -v

sudo rm -rf /mnt/djc/Toolathlon/configs/.mcp-auth

# dengjiecheng
bash scripts/bootstrap_notion_official.sh

uv run -m global_preparation.special_setup_notion_official

nohup sudo bash global_preparation/deploy_containers.sh true > run.log 2>&1 &

sudo bash global_preparation/check_installation_containerized.sh

bash scripts/list_subfolders.sh /mnt/djc/Toolathlon/tasks/finalpool task_files/all_tasks.txt


跑之前先过以下步骤：
run_parallel.sh
1. TOOLATHLON_OPENAI_API_KEY - pangu_api_key or "sk-xxx"
2. TASKS_FOLDER
3. TASK_LIST

# 待测：pangu92b-0831-yibu-iter43，qwen3.5-35b-a3b-jiecheng-non-fast-sft-ep5，qwen3.5-35b-a3b-jiecheng-non-fast-sft-ep3，505B-v21-stage1-17817-agent3, pangu92b-0831-yibu-iter73
nohup sudo bash scripts/run_parallel.sh 505B-v121_50_claweval /mnt/djc/dumps_public_service/505B-v121_50_claweval/run1 unified 32 > run_parallel.log 2>&1 &


# 在/mnt/djc/Toolathlon这个目录下执行

# tmux new -s hub
# tmux attach -t hub
# 退出 先ctrl + B 再按D 


# 停止所有以alpha开头的容器
sudo bash -c 'docker stop $(docker ps -a --filter "name=^/alpha-" -q)'

# 删除所有已停止的容器
sudo bash -c 'docker container prune -f'

# 检查某个程序的pid
sudo ps aux | grep api_proxy_v2.py

# 后台运行
nohup python api_proxy_v2.py > run.log 2>&1 &


tar -czvf run1.tar.gz -C /mnt/djc/dumps_public_service/92B-v21-B002-stage2-15406-agent1 run1


tar -czvf Toolathlon.tar.gz \
    --exclude='node_modules' \
    --exclude='.git' \
    --exclude='cc_trace-master' \
    --exclude='.venv' \
    /mnt/djc/Toolathlon



推荐操作顺序

cd /mnt/djc/Toolathlon

# 1) 抓主进程 PID（避免手抄）
PY=$(pgrep -f "\.venv/bin/python3 run_parallel\.py" | head -n1)
echo "python pid = $PY"

# 2) 只用 TERM，让 handler 走 killpg + 容器 trap cleanup
sudo kill -TERM "$PY"

# 3) 等 15 秒，看它有没有自己收干净
sleep 15
ps -ef | grep -E "run_parallel|run_single_containerized" | grep -v grep
sudo docker ps --filter "name=alpha-toolathlon" --format '{{.Names}}\t{{.Status}}'
如果上面没收干净（兜底）

# 4) 先整组 TERM，再 KILL 剩余进程组
sudo pkill -TERM -f "run_single_containerized\.sh" ; sleep 3
sudo pkill -KILL  -f "run_single_containerized\.sh"

# 5) 主进程链还在就补刀（TERM 优先，别一上来 KILL）
sudo kill -TERM 1011499 2>/dev/null
sleep 3
sudo pkill -KILL -f "run_parallel\.py"
sudo pkill -KILL -f "run_parallel\.sh"

# 6) 最后强制清容器（这一步保证前面 fallback 也能收干净）
sudo docker ps -a --filter "name=alpha-toolathlon" --format '{{.ID}} {{.Names}}' \
  | awk '{print $1}' | xargs -r sudo docker rm -f

# 7) 确认
sudo docker ps -a --filter "name=alpha-toolathlon"
ps -ef | grep -E "run_parallel|run_single|container_agent" | grep -v grep

# 服务器互传
rsync -avz --progress \
  -e 'ssh -J admin@101.245.71.190' \
  "/mnt/djc/generated_data_to_export/task_outline/toolathlon_variant_cards_deepseek-v4.1-flash-1017.jsonl" \
  zhuqi@190.92.207.12:"/mnt/djc/generated_data_to_export/task_outline/10月"


# 传回本地
scp tiaoban-djc:/mnt/djc/generated_data_to_export/train_format_to_export/2026_09_12月/20261004-1007/train_data_1004-1007_part2.jsonl D:\工作相关\蓝区传输

