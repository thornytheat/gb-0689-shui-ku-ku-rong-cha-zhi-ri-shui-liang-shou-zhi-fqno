import csv, io
from decimal import Decimal, InvalidOperation
from datetime import timedelta, timezone
from collections import defaultdict
from app.normalize import rows_from, instant

HEADER=['id','station','time','level','inflow','outflow','flow_unit','version']
ZONE=timezone(timedelta(hours=8))
SIG=lambda x:(x.get('level'),x.get('inflow'),x.get('outflow'))
NUMERIC={'in_m3','out_m3','storage_change','residual','coverage_hours'}

def parse_revision(raw):
    """复用 M1 契约解析补传 CSV；结构性错误直接抛 DomainError，不建任务。"""
    if isinstance(raw,str): raw=raw.encode('utf-8')
    parsed=rows_from(raw,HEADER)
    for row in parsed['raw']: row['_src']='rev'
    return parsed

def base_rows(text):
    parsed=rows_from(text.encode('utf-8'),HEADER)
    for row in parsed['raw']: row['_src']='base'
    return parsed['raw']

def line_ref(row):
    """带文件来源的行号引用，避免原批次与补传行号撞车。"""
    return f"{row['_src']}:{row.get('_line','')}"

def _iso(value):
    return value.isoformat() if hasattr(value,'isoformat') else str(value)

def _key_of(row):
    # 同站同刻；时间解析失败的行留给 M2 管线隔离，不进入版本裁决
    try: return row['station'],instant(row['time'])
    except Exception: return row['station'],row['time']

def _ver(x):
    try: return int(str(x['version']).strip())
    except (TypeError,ValueError): return -1

def merge(base_rows,rev_rows,drop_refs=None):
    """同站同刻按测次版本裁决。返回合并行（保留全部行，隔离交给 M2）与留痕分类。
    规则：最高版本且同值时补传行优先（新到达）；补传旧版输给已采信新版记迟到，不倒写。
    drop_refs: 已人工裁决剔除的 {(station,iso_time):{ref,...}}，这些行不参与裁决与重算。"""
    drop_refs=drop_refs or {}
    groups=defaultdict(list)
    for row in base_rows: groups[_key_of(row)].append(dict(row))
    for row in rev_rows: groups[_key_of(row)].append(dict(row))
    merged=[];conflicts=[];stale=[];duplicates=[];superseded=[];affected=set()
    for key,group in sorted(groups.items(),key=lambda kv:str(kv[0])):
        removed=drop_refs.get((key[0],_iso(key[1])),set())
        group=[x for x in group if line_ref(x) not in removed]
        if not group: continue
        latest=max(_ver(x) for x in group)
        top=[x for x in group if _ver(x)==latest]
        distinct={SIG(x) for x in top}
        if len(distinct)>1:
            # 同版本（最高版本）水文值矛盾：整组留待裁决，全部进入重算由 M2 隔离互证，不倒写现行值
            conflicts.append({'station':key[0],'time':key[1],'code':'CONFLICT',
                              'options':[{'ref':line_ref(x),'line':x.get('_line'),'src':x['_src'],'raw':{k:v for k,v in x.items() if not k.startswith('_')}} for x in group]})
            merged.extend(group);affected.add(key);continue
        # 最高版本胜出；同版同值时补传行（新到达）优先于原批次行，再按原始行号稳定排序
        winner=min(group,key=lambda x:(-_ver(x),0 if x['_src']=='rev' else 1,x.get('_line',0)))
        for x in group:
            if x is winner: continue
            if x['_src']=='rev' and _ver(x)<latest:
                stale.append({'station':key[0],'time':key[1],'line':line_ref(x),'key':x.get('id'),
                              'code':'STALE_REVISION','detail':'迟到旧修订版本低于已采信版本，不倒写现行值',
                              'raw':{k:v for k,v in x.items() if not k.startswith('_')}})
            elif SIG(x)==SIG(winner):
                duplicates.append({'station':key[0],'time':key[1],'line':line_ref(x),'key':x.get('id'),
                                   'code':'DUPLICATE','detail':'同版同值重复测次',
                                   'raw':{k:v for k,v in x.items() if not k.startswith('_')}})
            else:
                superseded.append({'station':key[0],'time':key[1],'line':line_ref(x),'key':x.get('id'),
                                   'code':'SUPERSEDE','detail':'同刻旧测次被高版本替代',
                                   'raw':{k:v for k,v in x.items() if not k.startswith('_')}})
        # 重算文件每键只放赢家；迟到旧修订/重复/被替代行仅留痕，不进入管线（不倒写现行值）
        merged.append(winner)
        if any(x['_src']=='rev' for x in group): affected.add(key)
    merged.sort(key=lambda x:(x['station'],str(_key_of(x)[1]),x.get('_line',0)))
    return {'rows':merged,'conflicts':conflicts,'stale':stale,'duplicates':duplicates,'superseded':superseded,'affected_keys':affected}

def to_text(rows):
    stream=io.StringIO(newline='');writer=csv.writer(stream);writer.writerow(HEADER)
    for row in rows: writer.writerow([row.get(k,'') for k in HEADER])
    return stream.getvalue()

def affected_days(merged_rows,affected_keys):
    """受影响观测时刻及其相邻积分段所落的 +08 日期。"""
    by_station=defaultdict(list)
    for row in merged_rows:
        key=_key_of(row)
        if not hasattr(key[1],'date'): continue
        by_station[key[0]].append(key[1])
    days=set()
    for station,t in affected_keys:
        if not hasattr(t,'date'): continue
        seq=sorted(by_station.get(station,[]))
        idx=seq.index(t) if t in seq else -1
        instants=[t]
        if idx>0: instants.append(seq[idx-1])
        if 0<=idx<len(seq)-1: instants.append(seq[idx+1])
        for moment in instants: days.add((station,moment.astimezone(ZONE).date().isoformat()))
    return sorted(days)

def _same(field,a,b):
    if field in NUMERIC:
        try: return Decimal(str(a))==Decimal(str(b))
        except (InvalidOperation,TypeError): return str(a)==str(b)
    return str(a)==str(b)

def day_changes(base_budgets,new_budgets):
    """按日列出新旧日账变化（数值按 Decimal 比较）；未变化日不列入。"""
    fields=['in_m3','out_m3','storage_change','residual','coverage_hours','state']
    old={(b['station'],b['date']):b for b in base_budgets}
    new={(b['station'],b['date']):b for b in new_budgets}
    changes=[]
    for key in sorted(set(old)|set(new)):
        a,b=old.get(key),new.get(key)
        if a is None:
            changes.append({'station':key[0],'date':key[1],'change':'新增','old':None,'new':{f:b.get(f) for f in fields}});continue
        if b is None:
            changes.append({'station':key[0],'date':key[1],'change':'消除','old':{f:a.get(f) for f in fields},'new':None});continue
        diffs={f:{'old':a.get(f),'new':b.get(f)} for f in fields if not _same(f,a.get(f),b.get(f))}
        if diffs: changes.append({'station':key[0],'date':key[1],'change':'重算','old':{f:a.get(f) for f in fields},'new':{f:b.get(f) for f in fields},'fields':diffs})
    return changes

def conflicts_from_quarantine(quarantine):
    """从替代批次隔离行提取同版冲突，供待裁决栏展示与人工选择。"""
    grouped=defaultdict(list)
    for row in quarantine:
        if row.get('code')!='CONFLICT': continue
        raw=row.get('raw',{})
        grouped[(raw.get('station'),raw.get('time'))].append({'line':row.get('line'),'ref':f"q:{row.get('line')}",'key':raw.get('id'),'raw':raw})
    return [{'station':k[0],'time':k[1],'code':'CONFLICT','options':v} for k,v in sorted(grouped.items())]
