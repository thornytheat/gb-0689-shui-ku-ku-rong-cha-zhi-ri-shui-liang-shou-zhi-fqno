import pytest
from decimal import Decimal as D
from app.errors import DomainError
from app.modules.m6_review.revision_rules import (
    parse_revision, base_rows, merge, to_text, affected_days, day_changes,
    conflicts_from_quarantine, line_ref)

BASIC_HEADER='id,station,time,level,inflow,outflow,flow_unit,version\n'
def base_text(*rows):
    return BASIC_HEADER+'\n'.join(rows)+('\n' if rows else '')

def test_higher_version_supersedes_base():
    base=base_rows(base_text('r1,S,2026-11-16T00:00:00+08:00,101.0,2.0,1.5,m3/s,1'))
    rev=parse_revision(base_text('r1,S,2026-11-16T00:00:00+08:00,101.0,4.0,1.5,m3/s,2'))['raw']
    m=merge(base,rev)
    assert len(m['rows'])==1 and m['rows'][0]['inflow']=='4.0' and m['rows'][0]['_src']=='rev'
    assert m['superseded'][0]['code']=='SUPERSEDE' and m['superseded'][0]['line']=='base:2'
    assert not m['stale'] and not m['conflicts']

def test_late_old_revision_does_not_overwrite():
    # 已采信 v2，迟到 v1（即使数值更激进）必须记 STALE 且赢家仍是 v2
    base=base_rows(base_text('r1,S,2026-11-16T00:00:00+08:00,101.0,5.0,1.5,m3/s,2'))
    rev=parse_revision(base_text('r1,S,2026-11-16T00:00:00+08:00,109.0,9.0,1.5,m3/s,1'))['raw']
    m=merge(base,rev)
    assert m['rows'][0]['inflow']=='5.0' and m['rows'][0]['_src']=='base'
    assert m['stale'][0]['code']=='STALE_REVISION' and m['stale'][0]['line']=='rev:2'
    # 迟到行不进重算文件，避免越界旧值污染管线
    assert '109.0' not in to_text(m['rows'])

def test_same_version_conflict_goes_to_adjudication():
    base=base_rows(base_text('r1,S,2026-11-16T00:00:00+08:00,101.0,2.0,1.5,m3/s,1'))
    rev=parse_revision(base_text('r1,S,2026-11-16T00:00:00+08:00,102.0,2.0,1.5,m3/s,1'))['raw']
    m=merge(base,rev)
    assert len(m['conflicts'])==1
    refs={o['ref'] for o in m['conflicts'][0]['options']}
    assert refs=={'base:2','rev:2'}
    # 冲突整组进入重算文件，交由 M2 隔离互证
    assert len(m['rows'])==2 and {line_ref(r) for r in m['rows']}==refs

def test_adjudication_drop_refs_resolves_conflict():
    base=base_rows(base_text('r1,S,2026-11-16T00:00:00+08:00,101.0,2.0,1.5,m3/s,1'))
    rev=parse_revision(base_text('r1,S,2026-11-16T00:00:00+08:00,102.0,2.0,1.5,m3/s,1'))['raw']
    drop={('S','2026-11-15T16:00:00+00:00'):{'base:2'}}
    m=merge(base,rev,drop)
    assert not m['conflicts'] and len(m['rows'])==1 and m['rows'][0]['level']=='102.0'

def test_duplicate_same_value_revision_wins_and_marks_duplicate():
    base=base_rows(base_text('r1,S,2026-11-16T00:00:00+08:00,101.0,2.0,1.5,m3/s,1'))
    rev=parse_revision(base_text('r1,S,2026-11-16T00:00:00+08:00,101.0,2.0,1.5,m3/s,1'))['raw']
    m=merge(base,rev)
    assert m['rows'][0]['_src']=='rev'  # 同版同值补传优先
    assert m['duplicates'][0]['line']=='base:2'

def test_affected_days_cover_neighbour_segments():
    base=base_rows(base_text(
        'r1,S,2026-11-16T00:00:00+08:00,101.0,2.0,1.5,m3/s,1',
        'r2,S,2026-11-16T01:00:00+08:00,101.1,2.0,1.5,m3/s,1',
        'r3,S,2026-11-17T01:00:00+08:00,101.2,2.0,1.5,m3/s,1'))
    # 受影响键为 r2 时刻（+08 11-16 01:00 = UTC 11-15 17:00）；相邻积分段含次日 r3，故跨两天
    from app.normalize import instant
    affected={('S',instant('2026-11-16T01:00:00+08:00'))}
    days=affected_days(base,affected)
    assert {d for _,d in days}=={'2026-11-16','2026-11-17'}

def test_day_changes_detect_numeric_and_state():
    old=[{'station':'S','date':'2026-11-16','in_m3':'100','out_m3':'50','storage_change':'10','residual':'-40','coverage_hours':'24','state':'完整'}]
    new=[{'station':'S','date':'2026-11-16','in_m3':'120','out_m3':'50','storage_change':'10','residual':'-60','coverage_hours':'23','state':'缺测'}]
    changes=day_changes(old,new)
    assert len(changes)==1 and set(changes[0]['fields'])=={'in_m3','residual','coverage_hours','state'}

def test_day_changes_numeric_string_equivalence():
    old=[{'station':'S','date':'d','in_m3':'100.0','out_m3':'0','storage_change':'0','residual':'0','coverage_hours':'24','state':'完整'}]
    new=[{'station':'S','date':'d','in_m3':'100','out_m3':'0','storage_change':'0','residual':'0','coverage_hours':'24','state':'完整'}]
    assert day_changes(old,new)==[]  # 100.0 与 100 数值相等，不算变化

def test_day_changes_add_and_remove():
    old=[{'station':'S','date':'d1','in_m3':'1','out_m3':'0','storage_change':'0','residual':'0','coverage_hours':'24','state':'完整'}]
    new=[{'station':'S','date':'d2','in_m3':'1','out_m3':'0','storage_change':'0','residual':'0','coverage_hours':'24','state':'完整'}]
    changes=day_changes(old,new)
    assert {c['change'] for c in changes}=={'新增','消除'}

def test_parse_bad_header_rejects_without_partial():
    with pytest.raises(DomainError) as exc: parse_revision('a,b\n1,2')
    assert exc.value.code=='HEADER'

def test_conflicts_from_quarantine_groups_options():
    q=[{'line':4,'key':'r1','code':'CONFLICT','raw':{'station':'S','time':'t','id':'r1'}},
       {'line':5,'key':'r1','code':'CONFLICT','raw':{'station':'S','time':'t','id':'r1'}},
       {'line':6,'key':'r2','code':'RANGE','raw':{}}]
    out=conflicts_from_quarantine(q)
    assert len(out)==1 and len(out[0]['options'])==2
