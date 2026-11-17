import hashlib,json
from app import store
from app.config import RULE_VERSION
from app.errors import DomainError
from app.pipeline import run
from . import revision_rules as rr

STEPS=['parsed','merged','affected','recomputed','diffed','ready']
STEP_TITLES={'parsed':'解析补传','merged':'版本合并','affected':'定位影响日','recomputed':'重算替代日账','diffed':'按日变化','ready':'候命切换'}

def _job_id(base_run_id,digest):
    return hashlib.sha256((base_run_id+digest+RULE_VERSION).encode()).hexdigest()[:20]

def _jsonable(value):
    return json.loads(json.dumps(value,ensure_ascii=False,default=str))

def _iso(value):
    return value.isoformat() if hasattr(value,'isoformat') else str(value)

def submit_revision(base_run_id,raw,params=None):
    """办理补传：同内容重办复用任务并从已确认进度续跑。"""
    base=store.fetch(base_run_id)
    raw_bytes=raw.encode('utf-8') if isinstance(raw,str) else raw
    digest=hashlib.sha256(raw_bytes).hexdigest()
    job=store.find_job(base_run_id,digest)
    if job is None:
        # 解析失败直接抛 DomainError，不产生半成品任务
        parsed=rr.parse_revision(raw_bytes)
        job={'id':_job_id(base_run_id,digest),'base_run_id':base_run_id,'digest':digest,
             'status':'进行中','step':None,'checkpoint':0,'steps':STEPS,'step_titles':STEP_TITLES,
             'input_count':parsed['input_count'],'quarantine':parsed['quarantine'],
             'revision_rows':parsed['raw'],'resolved':[],
             'candidate_run_id':None,'open_conflicts':[],'error':None,
             'override_params':params}
        job=store.upsert_job(job)
    return _advance(job,base)

def _drop_map(job):
    # resolved 经 JSON 往返后 time 为 ISO 文本；统一用 isoformat(T 分隔) 作为键
    norm=lambda t:t.isoformat() if hasattr(t,'isoformat') else str(t).replace(' ','T')
    return {(r['station'],norm(r['time'])):set(map(str,r['drop_refs'])) for r in job['resolved']}

def _merged_rows(job):
    merged=rr.merge(rr.base_rows(store.source(job['base_run_id'])),job['revision_rows'],_drop_map(job))
    return merged['rows'],merged

def _advance(job,base):
    """按检查点逐步推进；每步完成立即落盘，中断后重入从最后确认点继续。"""
    while job['checkpoint']<len(STEPS):
        step=STEPS[job['checkpoint']]
        if step=='parsed':
            pass  # 解析明细已随任务留存
        elif step=='merged':
            rows,merged=_merged_rows(job)
            job['merge']={'conflicts':_jsonable(merged['conflicts']),'stale':merged['stale'],
                          'duplicates':merged['duplicates'],'superseded':merged['superseded'],
                          'merged_text':rr.to_text(rows)}
            job['affected_keys']=[[s,_iso(t)] for s,t in sorted(merged['affected_keys'],key=str)]
        elif step=='affected':
            rows,_=_merged_rows(job)
            wanted={(s,t) for s,t in (rr._key_of(r) for r in rows) if [s,_iso(t)] in job['affected_keys']}
            job['affected_days']=rr.affected_days(rows,wanted)
        elif step=='recomputed':
            use_params=job.get('override_params') or base['manifest']['params']
            candidate=run(job['merge']['merged_text'],use_params)
            job['candidate_run_id']=candidate['manifest']['run_id']
            job['candidate_version']=candidate['version'];job['candidate_review']=candidate['review']
            # 待裁决栏以版本合并阶段的同版冲突为准（含来源行号），替代批次隔离行互为印证
            job['open_conflicts']=job['merge']['conflicts']
        elif step=='diffed':
            candidate=store.fetch(job['candidate_run_id'])
            job['day_changes']=rr.day_changes(base['budgets'],candidate['budgets'])
        elif step=='ready':
            candidate=store.fetch(job['candidate_run_id'])
            job['revision_evidence']={'job_id':job['id'],'base_run_id':job['base_run_id'],
                                      'digest':job['digest'],'rules':RULE_VERSION,
                                      'affected_days':job['affected_days'],'day_changes':job['day_changes'],
                                      'stale_revisions':job['merge']['stale'],
                                      'superseded':job['merge']['superseded'],
                                      'duplicates':job['merge']['duplicates'],
                                      'open_conflicts':job['open_conflicts'],'resolved':job['resolved']}
            # 留痕独立成表随批次固定；原批次 payload 不被改写，重办同修订内容一致
            store.save_revision(job['candidate_run_id'],job['revision_evidence'])
            job['status']='待裁决' if job['open_conflicts'] else '候命切换'
        job['step']=step;job['checkpoint']+=1
        job=store.upsert_job(job)  # 检查点确认落盘
        if step=='recomputed' and job['open_conflicts']:
            return job  # 同版冲突留在待裁决栏，不进入按日变化与切换
    return job

def adjudicate(job_id,station,time,ref):
    """待裁决栏人工选定保留行（来源行号 ref，如 rev:6）；裁决后从合并检查点重算，解析进度保留。"""
    job=store.get_job(job_id)
    if not job.get('open_conflicts'): raise DomainError('REVIEW','该补传没有待裁决冲突')
    target=next((c for c in job['open_conflicts'] if c['station']==station and c['time']==time),None)
    if target is None: raise DomainError('NOT_FOUND','冲突时刻不存在',404)
    if not any(str(o['ref'])==str(ref) for o in target['options']):
        raise DomainError('REVIEW','请选择保留的测次行号')
    dropped=[o['ref'] for o in target['options'] if str(o['ref'])!=str(ref)]
    job['resolved']=[r for r in job['resolved'] if not (r['station']==station and r['time']==time)]
    job['resolved'].append({'station':station,'time':time,'keep_ref':str(ref),'drop_refs':dropped})
    # 仅合并之后的步骤受裁决影响：检查点回退到 merged，parsed 已确认不重跑
    job['checkpoint']=1;job['step']='parsed';job['status']='进行中'
    for key in ('merge','affected_keys','affected_days','day_changes','revision_evidence','candidate_run_id','open_conflicts'):
        job.pop(key,None)
    job=store.upsert_job(job)
    return _advance(job,store.fetch(job['base_run_id']))

def review_candidate(job_id,version,decision):
    """对替代日账办理复核，沿用整数版本乐观锁，冲突返回 409。"""
    job=store.get_job(job_id)
    if not job.get('candidate_run_id'): raise DomainError('INCOMPLETE','替代日账尚未派生')
    result=store.review(job['candidate_run_id'],version,decision)
    job['candidate_version']=result['version'];job['candidate_review']=result['review']
    return result,store.upsert_job(job)

def switch(job_id,expected_active=None):
    """复核完整后才切换现行日账；单事务切换，失败保留旧版。"""
    job=store.get_job(job_id)
    if job.get('open_conflicts'): raise DomainError('REVIEW','仍有待裁决冲突，复核不完整')
    if not job.get('candidate_run_id'): raise DomainError('INCOMPLETE','替代日账尚未派生')
    candidate=store.fetch(job['candidate_run_id'])
    if candidate['review']!='通过': raise DomainError('REVIEW','替代日账未复核通过，不得切换现行')
    previous=store.switch_active(candidate['manifest']['run_id'],expected=expected_active)
    job['status']='现行';job['active_run_id']=candidate['manifest']['run_id']
    return store.upsert_job(job),previous
