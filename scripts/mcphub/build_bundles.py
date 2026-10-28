#!/usr/bin/env python3
"""Build independent, pinned Linux x86_64 MCPB packages; never include credentials.

Build scratch data lives outside the repository. No Toolathlon source path is used
at runtime. Task-bound servers are registered as callbacks, not shared processes.
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import tomllib
import zipfile
import yaml
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NODE = {
    'rail_12306': ('12306-mcp@0.3.9', '12306-mcp'),
    'fetch': ('@tokenizin/mcp-npx-fetch@1.0.0', '@tokenizin/mcp-npx-fetch'),
    'google_map': ('@modelcontextprotocol/server-google-maps@0.6.2', '@modelcontextprotocol/server-google-maps'),
    'howtocook': ('git+https://github.com/lockon-n/HowToCook-mcp.git#11510d36bcb55f6c84eaac3f85f2b051a1c70b5b', 'howtocook-mcp'),
    'youtube': ('git+https://github.com/lockon-n/youtube-mcp-server.git#b202e00e9014bf74b9f5188b623cad16f13c01c4', 'zubeid-youtube-mcp-server'),
}
PYTHON = {
    'scholarly': ('git+https://github.com/lockon-n/mcp-scholarly@82a6ca268ae0d2e10664be396e1a0ea7aba23229', 'mcp-scholarly'),
    'youtube-transcript': ('git+https://github.com/jkawamoto/mcp-youtube-transcript@28081729905a48bef533d864efbd867a2bfd14cd', 'mcp-youtube-transcript'),
    'yahoo-finance': ('git+https://github.com/lockon-n/yahoo-finance-mcp@469103ba1464486cb7b8bd2c1f6355f42ca64a5b', 'server.py'),
}
# Read connection budgets from the current benchmark config, not a second table.
TIMEOUTS = {
    config['name']: config.get('client_session_timeout_seconds', 5)
    for path in (ROOT/'configs/mcp_servers').glob('*.yaml')
    if (config := yaml.safe_load(path.read_text()))
}

ENV = {'google_map': {'GOOGLE_MAPS_API_KEY': ''}, 'youtube': {'YOUTUBE_API_KEY': ''}}


def run(*cmd, **kwargs):
    subprocess.run(cmd, check=True, **kwargs)


def copy_tree(src, dst):
    shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('/home/lny/mcp-buddle/toolathlon'))
    parser.add_argument('--work', type=Path, default=Path.home()/'.local/share/toolathlon-buddle-build')
    parser.add_argument('--servers', default=','.join([*NODE, *PYTHON]))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    args.work.mkdir(parents=True, exist_ok=True)
    for name in args.servers.split(','):
        work = args.work / name
        work.mkdir(exist_ok=True)
        bundle = work / 'bundle'
        if bundle.exists():
            shutil.rmtree(bundle)
        bundle.mkdir()
        env = ENV.get(name, {})
        if name in NODE:
            source, package = NODE[name]
            install = work / 'install'
            install.mkdir(exist_ok=True)
            if not (install/'node_modules'/package/'package.json').exists():
                (install/'package.json').write_text(json.dumps({'name': 'toolathlon-bundle-build', 'private': True}))
                run('npm', 'install', '--no-audit', '--no-fund', source, cwd=install)
            modules = install/'node_modules'
            if name == 'youtube' and not (modules/package/'dist/index.js').exists():
                shutil.rmtree(modules/package)
                run('git', 'clone', '--quiet', 'https://github.com/lockon-n/youtube-mcp-server', str(modules/package))
                run('git', 'checkout', '--quiet', 'b202e00e9014bf74b9f5188b623cad16f13c01c4', cwd=modules/package)
                run('npm', 'install', '--include=dev', '--no-audit', '--no-fund', cwd=modules/package)
                run('npm', 'run', 'build', cwd=modules/package)
            meta = json.loads((modules/package/'package.json').read_text())
            bins = meta.get('bin')
            entry = next(iter(bins.values())) if isinstance(bins, dict) else bins
            if name == 'youtube':
                entry = 'dist/index.js'  # Toolathlon launches index.js, not the CLI bin.
            if not entry:
                entry = 'dist/index.js'
            if not (modules/package/entry).exists():
                run('npm', 'run', 'build', cwd=modules/package)
            # Copy installed resolution in full for this isolated single-root install.
            copy_tree(modules, bundle/'server/node_modules')
            command = "process.execPath"
            command_args = [f'server/node_modules/{package}/{entry}']
            version = meta['version']
            launch = f"const path=require('node:path');const {{spawn}}=require('node:child_process');const args={json.dumps(command_args)}.map(x=>path.join(__dirname,x));const p=spawn({command},args,{{stdio:'inherit',env:process.env,cwd:__dirname}});for(const s of ['SIGTERM','SIGINT'])process.on(s,()=>p.kill(s));p.on('error',()=>process.exit(1));p.on('exit',(c,s)=>process.exit(c??1));\n"
            provenance = {'source': source, 'package': package, 'version': version}
        elif name in PYTHON:
            source, entry = PYTHON[name]
            venv = work/'venv'
            if not venv.exists():
                run('uv', 'venv', '--python', '3.12.11', str(venv))
                # Toolathlon's locked core avoids silently upgrading MCP protocol behavior.
                run('uv', 'pip', 'install', '--python', str(venv/'bin/python'), source, 'mcp==1.9.0', 'pydantic==2.11.3', 'httpx==0.27.0')
            # Reuse the benchmark lock for overlapping runtime dependencies.
            # Tool-specific git revisions remain those used by the Dockerfile.
            locked = tomllib.loads((ROOT/'uv.lock').read_text())['package']
            installed = set(json.loads(subprocess.check_output([str(venv/'bin/python'), '-c', "import importlib.metadata as m,json;print(json.dumps([d.metadata['Name'].lower().replace('_','-') for d in m.distributions()]))"], text=True)))
            constraints = work/'constraints.txt'
            constraints.write_text(''.join(f"{p['name']}=={p['version']}\n" for p in locked if p['name'] in installed and p.get('source',{}).get('registry')))
            run('uv', 'pip', 'install', '--python', str(venv/'bin/python'), '--constraint', str(constraints), source)
            python = venv/'bin/python'
            info = json.loads(subprocess.check_output([str(python), '-c', "import sys,json,importlib.metadata as m;print(json.dumps({'prefix':sys.base_prefix,'version':f'{sys.version_info.major}.{sys.version_info.minor}','packages':{d.metadata['Name']:d.version for d in m.distributions()}}))"], text=True))
            version = next((v for k,v in info['packages'].items() if k.lower().replace('_','-') == ('mcp-scholarly' if name=='scholarly' else 'mcp-youtube-transcript' if name=='youtube-transcript' else 'yahoo-finance-mcp')), '0.0.0')
            base = Path(info['prefix'])
            (bundle/'python/bin').mkdir(parents=True)
            shutil.copy2(base/'bin'/f"python{info['version']}", bundle/'python/bin/python')
            copy_tree(base/'lib', bundle/'python/lib')
            copy_tree(venv/'lib'/f"python{info['version']}"/'site-packages', bundle/'venv')
            if name == 'yahoo-finance':
                checkout = work/'source'
                if not checkout.exists():
                    run('git','clone','--quiet','https://github.com/lockon-n/yahoo-finance-mcp',str(checkout))
                    run('git','checkout','--quiet','469103ba1464486cb7b8bd2c1f6355f42ca64a5b',cwd=checkout)
                shutil.copy2(checkout/'server.py', bundle/'server.py')
                entry_path = 'server.py'
            else:
                (bundle/'bin').mkdir()
                shutil.copy2(venv/'bin'/entry, bundle/'bin'/entry)
                entry_path = 'bin/'+entry
            if name == 'scholarly':
                for file in (ROOT/'utils/mcp/relay_adapter').glob('*.py'):
                    shutil.copy2(file, bundle/'venv'/file.name)
                env = {'MCPHUB_RELAY_ADAPTER': 'fetch-v1'}
            launch = """const path=require('node:path'),fs=require('node:fs'),{spawn}=require('node:child_process');
const root=__dirname,python=path.join(root,'python/bin/python');fs.chmodSync(python,0o755);
const env={...process.env,PYTHONDONTWRITEBYTECODE:'1',PYTHONHOME:path.join(root,'python'),PYTHONPATH:path.join(root,'venv'),LD_LIBRARY_PATH:path.join(root,'python/lib')+(process.env.LD_LIBRARY_PATH?':'+process.env.LD_LIBRARY_PATH:'')};
const p=spawn(python,[path.join(root,ENTRY)],{stdio:'inherit',env,cwd:root});
for(const s of ['SIGTERM','SIGINT'])process.on(s,()=>p.kill(s));p.on('error',()=>process.exit(1));p.on('exit',c=>process.exit(c??1));
""".replace('ENTRY', json.dumps(entry_path))
            provenance = {'source': source, 'version': version, 'pythonPackages': info['packages']}
        else:
            raise ValueError(name)
        (bundle/'launch.cjs').write_text(launch)
        manifest = {'manifest_version':'0.3','name':name,'version':version,'description':f'Toolathlon pinned {name}; independent Linux x86_64 package. Credentials are not included.', 'author':{'name':'Toolathlon'},'server':{'type':'node','entry_point':'launch.cjs','mcp_config':{'command':'node','args':['${__dirname}/launch.cjs'],'env':env,'options':{'timeout':TIMEOUTS[name]*1000,'resetTimeoutOnProgress':False,'maxTotalTimeout':TIMEOUTS[name]*1000}}}}
        (bundle/'manifest.json').write_text(json.dumps(manifest,indent=2))
        (bundle/'provenance.json').write_text(json.dumps(provenance,indent=2))
        output = args.output/(name+'.mcpb')
        with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
            for file in sorted(bundle.rglob('*')):
                if file.is_file():
                    archive.write(file,file.relative_to(bundle))
        print(f'{name}: {output.stat().st_size} bytes sha256={hashlib.sha256(output.read_bytes()).hexdigest()}',flush=True)


if __name__ == '__main__':
    main()
