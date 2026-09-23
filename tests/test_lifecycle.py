import json
import os
import subprocess
import threading
import time
import tempfile
import shutil
import signal
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from host.agent import AgentError, HostAgent, Server, Handler, run_process

@pytest.fixture
def setup(tmp_path, monkeypatch):
    runtime=Path(tempfile.mkdtemp(prefix='ekh-',dir='/private/tmp'));vault=tmp_path/'vault';vault.mkdir()
    repo=tmp_path/'Demo Project';repo.mkdir();(repo/'main.py').write_text('def hello(): return "world"\n')
    subprocess.run(['git','init',str(repo)],check=True,capture_output=True)
    fake=tmp_path/'graphify';fake.write_text('#!/usr/bin/env python3\nimport json, pathlib\np=pathlib.Path("graphify-out");p.mkdir(exist_ok=True)\n(p/"graph.json").write_text(json.dumps({"nodes":[{"id":"hello","label":"hello","source_file":"main.py"}],"links":[]}))\n(p/"GRAPH_REPORT.md").write_text("# Demo architecture\\nhello")\n(p/"graph.html").write_text("<h1>Graph</h1>")\n')
    fake.chmod(0o755)
    monkeypatch.setenv('PATH',str(tmp_path)+os.pathsep+os.environ['PATH'])
    agent=HostAgent(runtime,Path(__file__).resolve().parents[1],reconcile=False)
    agent.dispatch('set_vault',{'path':str(vault)})
    sock=agent.agent_dir/'control.sock'
    server=Server(str(sock),Handler);server.agent=agent
    threading.Thread(target=server.serve_forever,daemon=True).start()
    monkeypatch.setenv('HUB_RUNTIME',str(runtime));monkeypatch.setenv('HUB_PROJECTS',str(agent.registry_path));monkeypatch.setenv('HUB_VAULT',str(vault));monkeypatch.setenv('HUB_GRAPHS',str(tmp_path/'graphs'));monkeypatch.setenv('HUB_AGENT_SOCKET',str(sock));monkeypatch.setenv('HUB_AGENT_TOKEN',str(agent.agent_dir/'agent.token'));monkeypatch.setenv('HUB_WEB_TOKEN',str(agent.agent_dir/'web.token'));monkeypatch.setenv('HUB_INDEX_DB',str(tmp_path/'index.db'));monkeypatch.setenv('HUB_TESTING','1');monkeypatch.setenv('HUB_STATIC',str(Path(__file__).resolve().parents[1]/'frontend'/'dist'))
    from backend import config
    config.settings=config.Settings()
    from backend import app as module
    module.settings=config.settings
    from backend import index
    index.settings=config.settings
    module.knowledge=module.KnowledgeIndex(vault, refresh_seconds=0)
    module.search_index=module.SearchIndex(module.knowledge)
    from backend import control
    control.settings=config.settings
    client=TestClient(module.app)
    key=(agent.agent_dir/'web.token').read_text()
    login=client.post('/api/login',json={'token':key})
    assert login.status_code==200
    csrf=login.json()['csrf']
    yield agent,client,csrf,repo,vault,tmp_path
    server.shutdown();server.server_close();shutil.rmtree(runtime)

def post(client,path,csrf,data):
    return client.post('/api'+path,json=data,headers={'x-hub-csrf':csrf})

def await_op(client,op_id):
    for _ in range(100):
        result=client.get('/api/operations/'+op_id).json()
        if result['status']!='running':return result
        time.sleep(.05)
    raise AssertionError('operation did not finish')

def test_full_lifecycle(setup):
    agent,client,csrf,repo,vault,tmp=setup
    preview=post(client,'/preview',csrf,{'path':str(repo)})
    assert preview.status_code==200 and not preview.json()['graph_ready']
    add=post(client,'/projects',csrf,{'path':str(repo),'name':'Demo Project','graphify':True,'generate_graph':True,'knowledge':True})
    result=await_op(client,add.json()['operation_id'])
    assert result['status']=='complete',result
    pid=result['events'][-1]['detail']['project_id']
    assert (repo/'graphify-out'/'graph.json').exists()
    (tmp/'graphs').mkdir()
    (tmp/'graphs'/pid).symlink_to(repo/'graphify-out')
    assert (vault/'Projects'/'Demo Project'/'Overview.md').exists()
    assert client.get('/api/projects').json()[0]['id']==pid
    assert client.get('/api/knowledge/notes?project_id='+pid).json()
    assert client.get('/api/search?q=hello').json()
    assert client.get('/api/search?q=Purpose').json()
    graph=post(client,f'/projects/{pid}/graph',csrf,{})
    assert await_op(client,graph.json()['operation_id'])['status']=='complete'
    remove=post(client,f'/projects/{pid}/unregister',csrf,{'confirm':'Demo Project'})
    assert await_op(client,remove.json()['operation_id'])['status']=='complete'
    assert client.get('/api/projects').json()==[]
    assert (repo/'main.py').exists() and (repo/'graphify-out'/'graph.json').exists()
    assert (vault/'Projects'/'Demo Project'/'Overview.md').exists()
    preview2=post(client,'/preview',csrf,{'path':str(repo)}).json()
    assert preview2['graph_ready'] and preview2['existing_knowledge']
    add2=post(client,'/projects',csrf,{'path':str(repo),'name':'Demo Project','graphify':True,'generate_graph':False,'knowledge':True})
    assert await_op(client,add2.json()['operation_id'])['status']=='complete'
    assert len(client.get('/api/projects').json())==1

def test_search_keeps_knowledge_when_code_has_many_matches(setup):
    agent,client,csrf,repo,vault,tmp=setup
    add=post(client,'/projects',csrf,{'path':str(repo),'name':'Demo Project','graphify':True,'generate_graph':True,'knowledge':True})
    result=await_op(client,add.json()['operation_id'])
    assert result['status']=='complete',result
    pid=result['events'][-1]['detail']['project_id']
    (tmp/'graphs').mkdir()
    (tmp/'graphs'/pid).symlink_to(repo/'graphify-out')
    nodes=[{'id':str(i),'label':'Purpose '+str(i),'source_file':'main.py'} for i in range(80)]
    (repo/'graphify-out'/'graph.json').write_text(json.dumps({'nodes':nodes,'links':[]}))
    hits=client.get('/api/search?q=Purpose&project='+pid).json()
    assert len([hit for hit in hits if hit['kind']=='code'])==50
    assert any(hit['kind']=='knowledge' and hit['path'].endswith('Overview.md') for hit in hits)

def test_security(setup):
    agent,client,csrf,repo,vault,tmp=setup
    assert client.get('/api/projects').status_code==200
    assert client.post('/api/preview',json={'path':str(repo)}).status_code==403
    assert client.post('/api/login',json={'token':'wrong'}).status_code==401
    assert client.get('/api/projects/unknown/graph/../../etc/passwd').status_code in (400,404)
    with pytest.raises(Exception):agent.dispatch('shell',{'command':'rm -rf /'})
    bad=post(client,'/preview',csrf,{'path':str(tmp/'missing')})
    assert bad.status_code==503
    link=tmp/'linked';link.symlink_to(repo)
    assert post(client,'/preview',csrf,{'path':str(link)}).status_code==503
    poison=post(client,'/projects',csrf,{'path':str(repo),'name':'<script>alert(1)</script>','graphify':False,'generate_graph':False,'knowledge':False})
    assert await_op(client,poison.json()['operation_id'])['status']=='failed'

def test_markdown_sanitized(setup):
    agent,client,csrf,repo,vault,tmp=setup
    note=vault/'Threat.md';note.write_text('# Threat\n\n<script>alert(1)</script>\n\n[click](javascript:alert(1))\n')
    data=client.get('/api/knowledge/note/Threat.md').json()
    assert '<script>' not in data['html'] and 'javascript:' not in data['html']

def test_browser_boundaries(setup, monkeypatch):
    agent,client,csrf,repo,vault,tmp=setup
    monkeypatch.delenv('HUB_TESTING')
    assert client.get('/api/projects',headers={'Host':'attacker.example'}).status_code==403
    assert post(client,'/preview',csrf,{'path':str(repo)}).status_code==403
    base={'Host':'127.0.0.1:8765','Origin':'http://127.0.0.1:8765','x-hub-csrf':csrf}
    assert client.post('/api/preview',json={'path':str(repo)},headers=base).status_code==200
    assert client.post('/api/preview',json={'path':str(repo)},headers={**base,'Origin':'https://attacker.example'}).status_code==403
    assert client.get('/internal/agent/next',headers={'Host':'127.0.0.1:8765'}).status_code==403
    assert client.get('/api/projects',headers={'Host':'127.0.0.1:8765'}).status_code==200

def test_duplicate_nested_and_failure(setup):
    agent,client,csrf,repo,vault,tmp=setup
    add=post(client,'/projects',csrf,{'path':str(repo),'name':'Demo Project','graphify':False,'generate_graph':False,'knowledge':False})
    assert await_op(client,add.json()['operation_id'])['status']=='complete'
    assert post(client,'/preview',csrf,{'path':str(repo)}).status_code==503
    nested=repo/'nested';nested.mkdir()
    assert post(client,'/preview',csrf,{'path':str(nested)}).status_code==503
    assert agent.dispatch('status',{})['projects']==1
    assert not agent.audit_path.read_text().find('register_project')==-1
    assert 'web.token' not in agent.audit_path.read_text()
    agent.active_updates.add(agent.registry()[0]['id'])
    with pytest.raises(Exception,match='already running'):
        agent.dispatch('graphify_update',{'project_id':agent.registry()[0]['id']})
    agent.active_updates.clear()

def test_reconcile_rollback(tmp_path):
    runtime=Path(tempfile.mkdtemp(prefix='ekh-rollback-',dir='/private/tmp'))
    repo=tmp_path/'Rollback Project';repo.mkdir()
    agent=HostAgent(runtime,Path(__file__).resolve().parents[1],reconcile=False)
    def fail(): raise RuntimeError('simulated Compose failure')
    agent._reconcile=fail
    op=agent.dispatch('register_project',{'path':str(repo),'name':'Rollback Project','graphify':False,'generate_graph':False,'knowledge':False})['operation_id']
    for _ in range(100):
        result=agent.dispatch('operation',{'operation_id':op})
        if result['status']!='running':break
        time.sleep(.05)
    assert result['status']=='failed'
    assert agent.registry()==[]
    assert repo.exists()
    assert list((runtime/'generated'/'backups').glob('*-projects.json'))
    shutil.rmtree(runtime)

def test_knowledge_parent_symlink_cannot_escape_vault(tmp_path):
    runtime=tmp_path/'runtime';vault=tmp_path/'vault';outside=tmp_path/'outside';repo=tmp_path/'repo'
    for path in (vault,outside,repo):path.mkdir()
    (vault/'Projects').symlink_to(outside,target_is_directory=True)
    agent=HostAgent(runtime,Path(__file__).resolve().parents[1],reconcile=False)
    agent.config_path.write_text(json.dumps({'vault':str(vault)}))
    row={'id':'0123456789abcdef','name':'Escaped','repo':str(repo),'graph':str(repo/'graphify-out'),'obsidian_note':None}
    with pytest.raises(AgentError,match='symlink'):
        agent._knowledge(row)
    assert not (outside/'Escaped'/'Overview.md').exists()

def test_registry_rejects_unsafe_entries_and_recovers_backup(tmp_path):
    runtime=tmp_path/'runtime';repo=tmp_path/'repo';repo.mkdir()
    agent=HostAgent(runtime,Path(__file__).resolve().parents[1],reconcile=False)
    valid=[{'id':'0123456789abcdef','name':'Safe','repo':str(repo),'graph':str(repo/'graphify-out'),'obsidian_note':None}]
    backups=runtime/'generated'/'backups';backups.mkdir(parents=True,exist_ok=True)
    (backups/'20260101T000000-projects.json').write_text(json.dumps(valid))
    agent.registry_path.write_text('{broken')
    assert agent.registry()==valid
    assert json.loads(agent.registry_path.read_text())==valid
    agent.registry_path.write_text(json.dumps([{'id':'../../etc/passwd','name':'Bad','repo':'/etc'}]))
    assert agent.registry()==valid
    assert 'recover_registry' in agent.audit_path.read_text()

def test_registry_without_backup_fails_closed(tmp_path):
    agent=HostAgent(tmp_path/'runtime',Path(__file__).resolve().parents[1],reconcile=False)
    agent.registry_path.write_text('{broken')
    with pytest.raises(AgentError,match='no valid backup'):
        agent.registry()

def test_graphify_rejects_symlink_output_and_uses_literal_arguments(tmp_path):
    runtime=tmp_path/'runtime';repo=tmp_path/'repo; touch PWNED';repo.mkdir()
    outside=tmp_path/'outside.json';outside.write_text('{}')
    fake=tmp_path/'graphify literal'
    fake.write_text('#!/usr/bin/env python3\nimport json, pathlib, sys\np=pathlib.Path("graphify-out");p.mkdir(exist_ok=True)\n(p/"argv.json").write_text(json.dumps(sys.argv))\n(p/"graph.json").symlink_to('+repr(str(outside))+')\n')
    fake.chmod(0o755)
    agent=HostAgent(runtime,Path(__file__).resolve().parents[1],reconcile=False,graphify=str(fake))
    op='0123456789abcdef';agent._event(op,'Queued','running')
    with pytest.raises(AgentError,match='safe graph.json'):
        agent._graphify(repo,False,op)
    assert json.loads((repo/'graphify-out'/'argv.json').read_text())==[str(fake),'.','--code-only']
    assert not (tmp_path/'PWNED').exists()

def test_process_timeout_kills_child_process_group(tmp_path):
    script=tmp_path/'hang.py';pid_file=tmp_path/'child.pid'
    script.write_text('import pathlib,subprocess,sys,time\np=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])\npathlib.Path(sys.argv[1]).write_text(str(p.pid))\ntime.sleep(60)\n')
    with pytest.raises(subprocess.TimeoutExpired):
        run_process([os.sys.executable,str(script),str(pid_file)],cwd=tmp_path,timeout=.5)
    child=int(pid_file.read_text())
    for _ in range(20):
        try: os.kill(child,0)
        except ProcessLookupError: break
        time.sleep(.05)
    else:
        pytest.fail('Graphify child process survived timeout cleanup')

def test_markdown_frontmatter_links_and_duplicate_wikilinks_are_safe(setup):
    agent,client,csrf,repo,vault,tmp=setup
    (vault/'Target.md').write_text('# Target\n')
    (vault/'Hostile.md').write_text('---\ntags: !!python/object/apply:os.system ["touch /tmp/nope"]\n---\n# Hostile\n[[Target]] [[Target]]\n<img src=x onerror=alert(1)>\n')
    data=client.get('/api/knowledge/note/Hostile.md').json()
    assert 'onerror' not in data['html'] and '<img' not in data['html']
    assert data['html'].count('href="/knowledge/note/Target.md"')==2
    target=client.get('/api/knowledge/note/Target.md').json()
    assert target['backlinks']==[{'path':'Hostile.md','title':'Hostile'}]

def test_graph_search_ignores_symlinked_graph_files(setup):
    agent,client,csrf,repo,vault,tmp=setup
    add=post(client,'/projects',csrf,{'path':str(repo),'name':'Demo Project','graphify':False,'generate_graph':False,'knowledge':False})
    result=await_op(client,add.json()['operation_id']);pid=result['events'][-1]['detail']['project_id']
    graph_root=tmp/'graphs'/pid;graph_root.mkdir(parents=True)
    secret=tmp/'secret.json';secret.write_text(json.dumps({'nodes':[{'id':'secret','label':'DO_NOT_INDEX'}]}))
    (graph_root/'graph.json').symlink_to(secret)
    assert client.get('/api/search?q=DO_NOT_INDEX').json()==[]

def test_operation_and_project_ids_cannot_be_substituted(setup):
    agent,client,csrf,repo,vault,tmp=setup
    assert client.get('/api/operations/%2e%2e%2f%2e%2e%2fetc%2fpasswd').status_code in (404,422)
    assert post(client,'/projects/0123456789abcdef/graph',csrf,{}).status_code==404
    with pytest.raises(AgentError,match='Invalid project ID'):
        agent.dispatch('open_repository',{'project_id':'../../etc/passwd'})

def test_empty_web_key_never_authenticates(setup,monkeypatch):
    agent,client,csrf,repo,vault,tmp=setup
    from backend import app as module
    missing=tmp/'missing-web-token';monkeypatch.setattr(module.settings,'web_token',missing)
    forged='1.nonce.'+'0'*64
    client.cookies.set('hub_session',forged)
    assert client.get('/api/projects').status_code==401

def test_bridge_request_expires_without_host_agent(tmp_path,monkeypatch):
    from backend import control
    monkeypatch.setattr(control.settings,'runtime',tmp_path)
    result=control._bridge_request('register_project',{'path':'/tmp/example'})
    operation_id=result['operation_id']
    with control.LOCK:
        control.TICKETS[operation_id].expires_at=time.time()-1
    failed=control._bridge_request('operation',{'operation_id':operation_id})
    assert failed['status']=='failed'
    assert failed['events'][0]['step']=='Host agent did not respond'
    with control.LOCK:
        control.TICKETS.pop(operation_id,None)
    while not control.QUEUE.empty():
        control.QUEUE.get_nowait()

def test_expired_external_ticket_never_executes(tmp_path):
    agent=HostAgent(tmp_path/'runtime',Path(__file__).resolve().parents[1],reconcile=False)
    called=[]
    agent._execute=lambda *args:called.append(args)
    agent.run_external('set_vault',{'path':str(tmp_path)},'0123456789abcdef',time.time()-1)
    assert called==[]
    assert agent.dispatch('operation',{'operation_id':'0123456789abcdef'})['status']=='failed'
