import pytest
from app.config import SAMPLES

HEADER='id,station,time,level,inflow,outflow,flow_unit,version\n'
def csv_text(*rows): return HEADER+'\n'.join(rows)+'\n'

@pytest.fixture
def base_id(client):
    text=csv_text(
        'r1,S,2026-11-16T00:00:00+08:00,101.0,2.0,1.5,m3/s,1',
        'r2,S,2026-11-16T01:00:00+08:00,101.1,2.0,1.5,m3/s,1',
        'r3,S,2026-11-16T02:00:00+08:00,101.2,2.0,1.5,m3/s,1')
    return client.post('/api/runs',json={'text':text}).json['manifest']['run_id']

def test_revision_http_full_lifecycle(client,base_id):
    rev=csv_text('r2,S,2026-11-16T01:00:00+08:00,101.1,4.0,1.5,m3/s,2')
    job=client.post(f'/api/revision/{base_id}',json={'text':rev}).json
    assert job['status']=='候命切换' and job['checkpoint']==6
    jid=job['id']
    assert client.get('/api/revision/jobs').status_code==200
    assert client.get(f'/api/revision/job/{jid}').status_code==200
    # 复核乐观锁
    assert client.post(f'/api/revision/job/{jid}/review',json={'version':99,'decision':'通过'}).status_code==409
    client.post(f'/api/revision/job/{jid}/review',json={'version':job['candidate_version'],'decision':'通过'})
    # 切换现行
    sw=client.post(f'/api/revision/job/{jid}/switch',json={})
    assert sw.status_code==200 and sw.json['active_run_id']==job['candidate_run_id']
    assert client.get('/api/active').json['active_run_id']==job['candidate_run_id']
    # M6 页面渲染补传面板
    assert '补传修订办理' in client.get(f'/m/6?run={base_id}').text

def test_evidence_fixed_per_version(client,base_id):
    rev=csv_text('r2,S,2026-11-16T01:00:00+08:00,101.1,4.0,1.5,m3/s,2')
    job=client.post(f'/api/revision/{base_id}',json={'text':rev}).json
    base_ev=client.get(f'/api/evidence.csv?run={base_id}').text
    cand_ev=client.get(f'/api/evidence.csv?run={job["candidate_run_id"]}').text
    assert '补传任务' not in base_ev          # 原凭证原貌
    assert '补传任务' in cand_ev and '日变化' in cand_ev  # 替代批次随版本固定留痕

def test_conflict_adjudication_then_switch_http(client,base_id):
    rev=csv_text('r3,S,2026-11-16T02:00:00+08:00,102.0,2.0,1.5,m3/s,1')
    job=client.post(f'/api/revision/{base_id}',json={'text':rev}).json
    assert job['open_conflicts'] and job['status']=='进行中'
    conf=job['open_conflicts'][0]
    keep=next(o['ref'] for o in conf['options'] if o['src']=='rev')
    adj=client.post(f'/api/revision/job/{job["id"]}/adjudicate',
                    json={'station':conf['station'],'time':conf['time'],'ref':keep})
    assert adj.status_code==200 and adj.json['status']=='候命切换'
    # 未复核不得切换
    assert client.post(f'/api/revision/job/{job["id"]}/switch',json={}).status_code==422
    client.post(f'/api/revision/job/{job["id"]}/review',
                json={'version':adj.json['candidate_version'],'decision':'通过'})
    assert client.post(f'/api/revision/job/{job["id"]}/switch',json={}).status_code==200

def test_stale_switch_keeps_previous_active(client,base_id):
    rev=csv_text('r2,S,2026-11-16T01:00:00+08:00,101.1,4.0,1.5,m3/s,2')
    job=client.post(f'/api/revision/{base_id}',json={'text':rev}).json
    client.post(f'/api/revision/job/{job["id"]}/review',json={'version':1,'decision':'通过'})
    client.post(f'/api/revision/job/{job["id"]}/switch',json={})
    r=client.post(f'/api/revision/job/{job["id"]}/switch',json={'expected_active':'mismatch'})
    assert r.status_code==409 and client.get('/api/active').json['active_run_id']==job['candidate_run_id']

def test_invalid_revision_http(client,base_id):
    r=client.post(f'/api/revision/{base_id}',json={'text':'bad,header\n1,2'})
    assert r.status_code==422 and r.json['code']=='HEADER'
    assert client.get('/api/revision/jobs').json==[]
