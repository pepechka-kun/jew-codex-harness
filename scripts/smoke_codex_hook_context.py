#!/usr/bin/env python3
"""Real local Codex + generated MCP hooks, synthetic Responses only; no accounts."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harness.core import WORKER_ENV, _hook_entries, merge_config


def toml(value):
    if isinstance(value, dict): return '{' + ','.join(json.dumps(k) + '=' + toml(v) for k, v in value.items()) + '}'
    if isinstance(value, list): return '[' + ','.join(map(toml, value)) + ']'
    return json.dumps(value)


def run(binary):
    with tempfile.TemporaryDirectory(prefix='jev-hook-context-') as directory:
        root = Path(directory).resolve(); work = root / 'work'; work.mkdir()
        results = []
        for native, forwarding, inherit in ((True, False, None), (True, True, None), (False, True, None), (True, True, "none")):
            home = root / ('profile-' + str(len(results))); home.mkdir(mode=0o700)
            requests = []; outputs = []; asks = []
            marker = 'PRIVATE_ENV_PRESENT'
            command = shlex.join([sys.executable, '-c', 'import os;print("PRIVATE_ENV_PRESENT="+str("CANVASTTY_RUNTIME_CAPABILITY" in os.environ))'])

            class API(BaseHTTPRequestHandler):
                def log_message(self, *args): pass
                def do_POST(self):
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    if self.path.endswith('/ask'):
                        asks.append(True); self.send_response(500); self.end_headers(); return
                    requests.append(body)
                    if len(requests) == 1:
                        tools = [entry for t in body.get('tools', []) for entry in (t.get('tools', []) if t.get('type') == 'namespace' else [t])]
                        name = next((t.get('name') for t in tools if t.get('name') in ('shell_command', 'exec_command')), None)
                        if name is None: raise AssertionError('Shell tool was not exposed')
                        args = {'command': command} if name == 'shell_command' else {'cmd': command}
                        item = {'type': 'function_call', 'id': 'fc_fixture', 'call_id': 'call_fixture', 'name': name, 'arguments': json.dumps(args)}
                    else:
                        outputs.extend(x.get('output', '') for x in body.get('input', []) if x.get('type') == 'function_call_output')
                        item = {'type': 'message', 'id': 'msg_fixture', 'role': 'assistant', 'status': 'completed',
                                'content': [{'type': 'output_text', 'text': 'HOOK_CONTEXT_OK', 'annotations': []}]}
                    response = {'id': 'resp_' + str(len(requests)), 'object': 'response', 'created_at': 1, 'status': 'completed',
                                'model': 'fixture-model', 'output': [item], 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}
                    events = [{'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
                              {'type': 'response.output_item.added', 'output_index': 0, 'item': item},
                              {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                              {'type': 'response.completed', 'response': response}]
                    self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
                    self.wfile.write(''.join('event: '+e['type']+'\ndata: '+json.dumps({**e,'sequence_number':i})+'\n\n' for i,e in enumerate(events)).encode())

            api = ThreadingHTTPServer(('127.0.0.1', 0), API)
            thread = threading.Thread(target=api.serve_forever, daemon=True); thread.start()
            try:
                model = {'slug': 'fixture-model', 'display_name': 'Fixture', 'description': 'Local fixture',
                    'default_reasoning_level': 'low', 'supported_reasoning_levels': [{'effort':'low','description':'Fixture'}],
                    'shell_type':'shell_command','visibility':'list','supported_in_api':True,'priority':0,
                    'base_instructions':'Use the provided test tools only when requested.', 'supports_reasoning_summaries':False,
                    'support_verbosity':False,'truncation_policy':{'mode':'tokens','limit':10000},'input_modalities':['text'],
                    'supports_search_tool':True,'supports_parallel_tool_calls':True,'experimental_supported_tools':[]}
                (home/'models.json').write_text(json.dumps({'models':[model]}))
                base = f'model="fixture-model"\nmodel_provider="fixture"\nmodel_catalog_json={json.dumps(str(home/"models.json"))}\n'
                base += f'[model_providers.fixture]\nname="Fixture"\nbase_url="http://127.0.0.1:{api.server_port}/v1"\nwire_api="responses"\nrequires_openai_auth=false\nenv_key="FIXTURE_KEY"\n'
                if inherit:
                    base += '[shell_environment_policy]\ninherit="'+inherit+'"\n'
                config = merge_config(base, ROOT)
                if not forwarding:
                    config = config.replace(json.dumps(list(WORKER_ENV)), json.dumps([k for k in WORKER_ENV if not k.startswith('CANVASTTY_')]))
                (home/'config.toml').write_text(config)
                args = []; trusts = {}
                for event in ('UserPromptSubmit','PreToolUse'):
                    rows = _hook_entries(ROOT)[event]; hook = rows[0]['hooks'][0]
                    args += ['-c', 'hooks.'+event+'='+toml(rows)]
                    event_name = 'user_prompt_submit' if event == 'UserPromptSubmit' else 'pre_tool_use'
                    identity = {'event_name':event_name,'hooks':[{k:hook[k] for k in ('input','server','timeout','tool','type')}]}
                    trust = 'sha256:' + hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                    trusts['/<session-flags>/config.toml:'+event_name+':0:0'] = {'trusted_hash': trust}
                args += ['-c', 'hooks.state='+toml(trusts)]
                environment = {k:v for k,v in os.environ.items() if k.upper() in ('PATH','SYSTEMROOT','WINDIR','COMSPEC','PATHEXT','TMP','TEMP','TMPDIR')}
                environment.update(HOME=str(home),USERPROFILE=str(home),CODEX_HOME=str(home),CODEX_SQLITE_HOME=str(home),FIXTURE_KEY='fixture',
                                   JEV_GLOBAL_ASK_URL=f'http://127.0.0.1:{api.server_port}/ask')
                if native:
                    environment.update(CANVASTTY_RUNTIME_PROVIDER='codex',CANVASTTY_RUNTIME_ADDRESS='/fixture/runtime.sock',
                        CANVASTTY_RUNTIME_TERMINAL_SESSION_ID='12345678-1234-4567-8901-123456789abc',CANVASTTY_RUNTIME_CAPABILITY='synthetic-private-context')
                child = subprocess.run([binary,'exec','--ephemeral','--json','--skip-git-repo-check','--sandbox','read-only',
                    '-C',str(work),*args,'Run the diagnostic command supplied by the test tool response, then reply HOOK_CONTEXT_OK.'],
                    env=environment,capture_output=True,text=True,timeout=50)
                if child.returncode: raise AssertionError('Fixture CLI failed: '+child.stderr[-1500:])
                if not (home/'jev-global/usage.jsonl').exists():
                    raise AssertionError('Hooks did not run: '+child.stderr[-3500:]+'\n'+child.stdout[-3500:])
                metrics = [json.loads(line) for line in (home/'jev-global/usage.jsonl').read_text().splitlines()]
                modes = [r.get('guard_mode') for r in metrics if r.get('kind')=='pre_tool_seen']
                assert modes == ['canvastty' if native and forwarding else 'manual'], modes
                assert marker+'=False' in '\n'.join(map(str,outputs)), outputs
                assert not asks, 'Hook unexpectedly called an external judgement service'
                assert 'HOOK_CONTEXT_OK' in child.stdout
                results.append({'native_context':native,'forwarding':forwarding,'shell_inherit':inherit or 'default','guard_mode':modes[0],
                                'ordinary_shell_has_capability':False,'requests':len(requests)})
            finally:
                api.shutdown(); api.server_close(); thread.join(timeout=3)
        print(json.dumps({'ok':True,'live_provider_calls':False,'cases':results}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--codex',required=True)
    args = parser.parse_args()
    if not Path(args.codex).is_absolute(): parser.error('--codex must be an absolute path')
    run(args.codex)
