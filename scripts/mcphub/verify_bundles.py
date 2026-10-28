#!/usr/bin/env python3
"""Verify standalone package startup and original schemas; no tool executions. Startup may fetch public metadata.

Run with Toolathlon's Python dependencies. --image additionally verifies the
packages inside the given Hub image, without restarting the running Hub.
"""
import argparse
import asyncio
import json
import os
import tempfile
import zipfile
from pathlib import Path
from agents.mcp import MCPServerStdio


async def verify(args):
    results = {}
    for package in sorted(args.directory.glob('*.mcpb')):
        with tempfile.TemporaryDirectory(prefix='toolathlon-bundle-verify-') as temporary:
            folder = Path(temporary)
            with zipfile.ZipFile(package) as archive:
                archive.extractall(folder)
            manifest = json.loads((folder/'manifest.json').read_text())
            environment = {'GOOGLE_MAPS_API_KEY':'package-startup-test', 'YOUTUBE_API_KEY':'package-startup-test'}
            params = {'command':'node','args':[str(folder/'launch.cjs')], 'env': environment}
            if args.image:
                params = {'command':'docker','args':['run','--rm','-i','--network','host','--user',f'{os.getuid()}:{os.getgid()}','-v',f'{folder}:/bundle',
                         '-e','GOOGLE_MAPS_API_KEY=package-startup-test','-e','YOUTUBE_API_KEY=package-startup-test',
                         '--entrypoint','node',args.image,'/bundle/launch.cjs']}
            try:
                async with MCPServerStdio(params=params, client_session_timeout_seconds=120) as server:
                    tools = await server.list_tools()
                results[package.stem] = {'status':'ok','version':manifest['version'],'toolCount':len(tools),
                    'tools':[t.model_dump(by_alias=True,exclude_none=True) for t in tools]}
            except Exception as exc:
                results[package.stem] = {'status':'failed','error':str(exc)}
            print(package.stem, results[package.stem]['status'], results[package.stem].get('toolCount'), flush=True)
    (args.directory/'核对记录').mkdir(exist_ok=True)
    destination = args.directory/'核对记录'/('容器启动验证.json' if args.image else '启动与工具定义验证.json')
    destination.write_text(json.dumps({'scope':'startup and schema inventory only; no real provider credentials or task state',
                                      'image':args.image,'results':results},ensure_ascii=False,indent=2)+'\n')
    if any(item['status'] != 'ok' for item in results.values()):
        raise SystemExit(1)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,default=Path('/home/lny/mcp-buddle/toolathlon'))
    parser.add_argument('--image',default='')
    asyncio.run(verify(parser.parse_args()))
