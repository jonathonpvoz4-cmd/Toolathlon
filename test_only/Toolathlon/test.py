import json
import os
from openai import OpenAI

def get_task_name(path, output_file):
    task_name = os.listdir(path)
    with open(output_file, 'w', encoding='utf-8') as f:
        for tn in task_name:
            f.write(tn + '\n')

def find_problem_task(finalpool_path):

    problem_task = set()
    for task_folder in os.listdir(finalpool_path):
        if not os.path.exists(os.path.join(finalpool_path, task_folder, 'eval_res.json')):
            problem_task.add(task_folder)
            continue
        
        # with open(os.path.join(finalpool_path, task_folder, 'eval_res.json'), 'r', encoding='utf-8') as f:
        #     data = json.load(f)

        #     if 'pass' in data and data['pass'] == None:    
        #         if 'max_turns_reached' in data['details']:
        #             continue
        #         problem_task.add(task_folder)
    
    print(len(problem_task))


    with open('/mnt/djc/Toolathlon/task_files/problem_tasks.txt', 'w', encoding='utf-8') as f:
        for pt in problem_task:
            f.write(pt + '\n')

def find_fail_web_sum_task(finalpool_path):

    problem_task = []
    for task_folder in os.listdir(finalpool_path):
        traj_log_path = os.path.join(finalpool_path, task_folder, 'traj_log.json')
        eval_res_path = os.path.join(finalpool_path, task_folder, 'eval_res.json')
        
        if not os.path.exists(eval_res_path) or not os.path.exists(traj_log_path):
            continue
        
        with open(eval_res_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

            if not data['pass']:    
                with open(traj_log_path, 'r', encoding='utf-8') as ff:
                    traj_log_data = json.load(ff)
                    print(traj_log_data['config']['needed_local_tools'])
                    if 'ai_webpage_summary' in traj_log_data['config']['needed_local_tools']:
                        problem_task.append(task_folder)

                ff.close()
        f.close()

    print(len(problem_task))
    with open('/mnt/djc/Toolathlon/task_files/all_tasks.txt', 'w', encoding='utf-8') as f:
        for pt in problem_task:
            f.write(pt + '\n')


def find_web_search_tool_failed_task(finalpool_path):

    problem_task = []
    for task_folder in os.listdir(finalpool_path):
        traj_log_path = os.path.join(finalpool_path, task_folder, 'traj_log.json')

        if not os.path.exists(traj_log_path):
            continue

        with open(traj_log_path, 'r', encoding='utf-8') as ff:
            traj_log_data = json.load(ff)

            for message in traj_log_data['messages']:
                if message.get('role') != 'assistant':
                    continue
                for tool_call in message.get('tool_calls') or []:
                    if tool_call['function']['name'] == 'local_web_search':
                        problem_task.append(task_folder)
                        break
                else:
                    continue
                break

    print(len(problem_task))
    with open('/mnt/djc/Toolathlon/task_files/all_tasks.txt', 'w', encoding='utf-8') as f:
        for pt in problem_task:
            f.write(pt + '\n')

# with open('/mnt/djc/env.json', 'r', encoding='utf-8') as f:
#     env_data = json.load(f)

# # claude-opus-4-8
client = OpenAI(
    # api_key='sk-MKbJwRceK2W9sHhrluWN5CfiZuFKO9KgMFOvcFFnJFDTGIbn', #sk-pDcSg5BW1xVwwPibAnFW7HcHk67U6xvkDutk9tFCyyzqdkPD, pangu_api_key
    # base_url='http://192.168.0.22:3000/v1'
    api_key='sk-HHt8slIowGuv9KDMlJF1W5sqb3Qj87WVsCPgornaI5V2VGMD',
    base_url='http://115.120.87.159:8082/v1',

    # api_key='sk-jRujm6QYmbTZ2x5uuXcVFeNe1eQQKaZg4H900wl6DLzAFOtJ',
    # base_url='http://101.245.71.190:3000/v1'
)
response = client.chat.completions.create(
            messages=[{'role': 'user', 'content': 'who are you?'}],
            reasoning_effort="max",
            extra_body={
                'thinking':{'type': 'enabled'}
            },
            model='deepseek-v4-flash')


print(response)


# get_task_name('/mnt/djc/Toolathlon/tasks/finalpool','/mnt/djc/Toolathlon/task_files/all_tasks.txt')


# find_problem_task('/mnt/djc/dumps_public_service/92B-v22-B001-17678-agent2/run1/finalpool')

# try:
#     from utils.app_specific.googlesheet.drive_helper import (
#         get_google_service, find_folder_by_name, create_folder, 
#         clear_folder, copy_sheet_to_folder
#     )
    
#     GOOGLESHEET_URLS = [
#         "https://docs.google.com/spreadsheets/d/1S9BFFHU262CjU87DnGFfP_LMChhAT4lx7uNvwY-7HoI",
#     ]
#     FOLDER_NAME = "update-material-inventory"
    
#     drive_service, sheets_service = get_google_service()
    
#     folder_id = find_folder_by_name(drive_service, FOLDER_NAME)
#     if not folder_id:
#         folder_id = create_folder(drive_service, FOLDER_NAME)

#     clear_folder(drive_service, folder_id)

#     for sheet_url in GOOGLESHEET_URLS:
#         copy_sheet_to_folder(drive_service, sheet_url, folder_id)

#     # Use folder_id as spreadsheet_id reference for config
#     spreadsheet_id = folder_id
    
# except Exception as e:
#     spreadsheet_id = None

