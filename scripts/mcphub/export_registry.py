#!/usr/bin/env python3
"""Export credential-free task registrations and an exact source-config inventory."""
import argparse
import json
from pathlib import Path
import yaml
from credential_schema import SCHEMAS

ROOT = Path(__file__).resolve().parents[2]
SHARED = {'rail_12306','fetch','google_map','howtocook','scholarly','yahoo-finance','youtube','youtube-transcript'}
OPTIONAL = {'notion_official', 'time'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('/home/lny/mcp-buddle/toolathlon'))
    args = parser.parse_args()
    inventory, tasks, optional = {}, {}, {}
    for path in sorted((ROOT/'configs/mcp_servers').glob('*.yaml')):
        source = yaml.safe_load(path.read_text())
        name = source['name']
        inventory[name] = {'mode': 'shared' if name in SHARED else 'task',
                           'source': str(path.relative_to(ROOT)),
                           'timeoutSeconds': source.get('client_session_timeout_seconds', 5),
                           'sourceConnection': source}
        if name not in SHARED:
            config = {'type':'stdio','enabled':True,'description':f'Toolathlon task-owned {name}; backend is registered by the task worker.',
                      'targets':[{'path':'task/default','kind':'task','enabled':True,
                                  'config':{'options':{'timeout':0,'maxBufferSize':0}}}]}
            if name in {'arxiv_local', 'arxiv-latex'}:
                config['targets'][0]['config']['env'] = {'MCPHUB_RELAY_ADAPTER': 'fetch-v1'}
            if name in SCHEMAS:
                config['workspaceSchema'] = SCHEMAS[name]
            (optional if name in OPTIONAL else tasks)['toolathlon::'+name] = config
    args.output.mkdir(parents=True, exist_ok=True)
    for filename, servers in [('任务连接.json',tasks),('可选任务连接.json',optional)]:
        (args.output/filename).write_text(json.dumps({'mcpServers':servers},ensure_ascii=False,indent=2)+'\n')
    (args.output/'核对记录').mkdir(exist_ok=True)
    (args.output/'核对记录'/'来源与策略.json').write_text(json.dumps(inventory,ensure_ascii=False,indent=2)+'\n')


if __name__ == '__main__':
    main()
