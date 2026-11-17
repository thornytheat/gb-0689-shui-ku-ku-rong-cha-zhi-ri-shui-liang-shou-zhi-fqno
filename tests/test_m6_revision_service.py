import pytest
from app.pipeline import run
from app import store
from app.errors import DomainError
from app.modules.m6_review import revision_service as svc

HEADER='id,station,time,level,inflow,outflow,flow_unit,version\n'
def csv_text(*rows): return HEADER+'\n'.join(rows)+'\n'

@pytest.fixture
def base(isolate):
    text=csv_text(
        'r1,S,2026-11-16T00:00:00+08:00,101.0,2.0,1.5,m3/s,1',
        'r2,S,2026-11-16T01:00:00+08:00,101.1,2.0,1.5,m3/s,1',
        'r3,S,2026-11-16T02:00:00+08:00,101.2,2.0,1.5,m3/s,1',
        'r4,S,2026-11-16T03:00:00+08:00,101.3,2.0,1.5,m3/s,1')
    return run(text)

def revision(rid,body,**kw): return svc.submit_revision(rid,csv_text(*body),**kw)

def test_revision_recomputes_and_lists_day_changes(base):
    job=revision(base['manifest']['run_id'],['r2,S,2026-11-16T01:00:00+08:00,101.1,4.0,1.5,m3/s,2'])
    assert job['status']=='候命切换' and job['checkpoint']==6
    assert job['affected_days']==[('S','2026-11-16')]
    fields=set()
    for c in job['day_changes']: fields.update(c['fields'])
    assert {'in_m3','residual'}<=fields
    candidate=store.fetch(job['candidate_run_id'])
    assert candidate['manifest']['run_id']!=base['manifest']['run_id']  # 派生替代批次而非覆盖

def test_resubmit_same_revision_is_idempotent(base):
    body=['r2,S,2026-11-16T01:00:00+08:00,101.1,4.0,1.5,m3/s,2']
    a=revision(base['manifest']['run_id'],body);b=revision(base['manifest']['run_id'],body)
    assert a['id']==b['id'] and a['candidate_run_id']==b['candidate_run_id']
    assert store.get_revision(a['candidate_run_id'])['digest']==a['digest']

def test_late_old_revision_kept_out_of_current(base):
    # 先建立 v2 采信，再迟到 v1
    j=revision(base['manifest']['run_id'],['r3,S,2026-11-16T02:00:00+08:00,101.2,5.0,1.5,m3/s,2'])
    base2=store.fetch(j['candidate_run_id'])
    stale=revision(base2['manifest']['run_id'],['r3,S,2026-11-16T02:00:00+08:00,109.0,9.0,1.5,m3/s,1'])
    assert stale['merge']['stale'][0]['code']=='STALE_REVISION'
    cand=store.fetch(stale['candidate_run_id'])
    r3=next(r for r in cand['records'] if r['id']=='r3')
    assert str(r3['inflow'])=='5.0' and str(r3['level'])=='101.2'  # 现行值不倒写
    assert stale['day_changes']==[]  # 无实际日账变化

def test_resume_from_confirmed_checkpoint(base):
    body=['r2,S,2026-11-16T01:00:00+08:00,101.1,4.0,1.5,m3/s,2']
    job=revision(base['manifest']['run_id'],body)
    expected=job['candidate_run_id']
    # 模拟落库中断：回退检查点并清除后续产物
    job['checkpoint']=3;job['step']='affected';job['status']='进行中'
    for k in ('candidate_run_id','open_conflicts','day_changes','revision_evidence','candidate_version','candidate_review'):
        job.pop(k,None)
    store.upsert_job(job)
    resumed=revision(base['manifest']['run_id'],body)  # 同修订命中任务续跑
    assert resumed['checkpoint']==6 and resumed['candidate_run_id']==expected
    assert resumed['status']=='候命切换'

def test_conflict_blocks_switch_until_adjudicated(base):
    job=revision(base['manifest']['run_id'],['r4,S,2026-11-16T03:00:00+08:00,102.0,2.0,1.5,m3/s,1'])
    assert job['status']=='进行中' and job['open_conflicts'] and 'day_changes' not in job
    with pytest.raises(DomainError) as exc: svc.switch(job['id'])
    assert exc.value.code=='REVIEW'
    conf=job['open_conflicts'][0];keep=next(o['ref'] for o in conf['options'] if o['src']=='rev')
    done=svc.adjudicate(job['id'],conf['station'],conf['time'],keep)
    assert done['status']=='候命切换' and not done['open_conflicts']
    r4=next(r for r in store.fetch(done['candidate_run_id'])['records'] if r['id']=='r4')
    assert str(r4['level'])=='102.0'

def test_switch_requires_review_and_keeps_old_on_failure(base):
    job=revision(base['manifest']['run_id'],['r2,S,2026-11-16T01:00:00+08:00,101.1,4.0,1.5,m3/s,2'])
    with pytest.raises(DomainError): svc.switch(job['id'])  # 未复核通过
    svc.review_candidate(job['id'],job['candidate_version'],'通过')
    _,previous=svc.switch(job['id'])
    assert previous is None and store.active()==job['candidate_run_id']
    # 第二个替代批次切换，乐观锁不匹配时保留旧版
    j2=revision(base['manifest']['run_id'],['r3,S,2026-11-16T02:00:00+08:00,101.2,3.0,1.5,m3/s,2'])
    svc.review_candidate(j2['id'],j2['candidate_version'],'通过')
    with pytest.raises(DomainError) as exc: store.switch_active(j2['candidate_run_id'],expected='wrong')
    assert exc.value.status==409 and store.active()==job['candidate_run_id']  # 旧版保留

def test_original_batch_immutable_and_evidence_fixed(base):
    job=revision(base['manifest']['run_id'],['r2,S,2026-11-16T01:00:00+08:00,101.1,4.0,1.5,m3/s,2'])
    svc.review_candidate(job['id'],job['candidate_version'],'通过');svc.switch(job['id'])
    original=store.fetch(base['manifest']['run_id'])
    assert 'revision' not in original and original['review']=='待复核'  # 原批次原貌
    candidate=store.fetch(job['candidate_run_id'])
    assert candidate['revision']['job_id']==job['id']  # 替代批次带固定留痕

def test_bad_revision_raises_before_job_created(base,isolate):
    before=len(store.list_jobs())
    with pytest.raises(DomainError) as exc: svc.submit_revision(base['manifest']['run_id'],'a,b\n1,2')
    assert exc.value.code=='HEADER' and len(store.list_jobs())==before

def test_revision_requires_retained_source():
    # 未通过完整管线持久化的批次没有原始凭证
    ghost=run(csv_text('r1,S,2026-11-16T00:00:00+08:00,101.0,2.0,1.5,m3/s,1'),persist=False)
    with pytest.raises(DomainError) as exc:
        svc.submit_revision(ghost['manifest']['run_id'],csv_text('r1,S,2026-11-16T00:00:00+08:00,101.0,3.0,1.5,m3/s,2'))
    assert exc.value.code=='NOT_FOUND'
