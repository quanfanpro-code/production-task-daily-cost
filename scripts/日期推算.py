"""固定业务规则：日期展开、证据归集和183天整任务分流。"""
from collections import defaultdict
from copy import deepcopy
from datetime import date,timedelta
import itertools
import json
import re

VERSION='2026-10-15.2'
ORDER=['精磨','研磨','双轴','环抛','环磨','离子束','成品检验']
ALIASES={'研磨':'精磨','成品检':'成品检验','出货检验':'成品检验'}


def day(value):
    if isinstance(value,date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def order(proc):
    return (ORDER.index(proc) if proc in ORDER else len(ORDER),proc)


def stable(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,default=str)


def number_date(no):
    m=re.search(r'(\d{4})(\d{2})(\d{2})',no)
    return day('-'.join(m.groups())) if m else None


def choose_candidate(candidates):
    """候选须提供相互比较胜出关系；平局返回空供独立兜底，绝不按输入顺序选择。"""
    values={str(c['value']) for c in candidates}
    if len(values)==1:
        return next(iter(values))
    wins=defaultdict(set)
    for c in candidates:
        wins[str(c['value'])].update(str(v) for v in c.get('beats',[]))
    winners=[v for v in sorted(values) if values-{v}<=wins[v]
             and not any(v in wins[o] for o in values-{v})]
    return winners[0] if len(winners)==1 else None


def build(tasks):
    result={'rule_version':VERSION,'normal':[],'abnormal':[],'trace':[],'tasks':[],'warnings':[]}
    if not isinstance(tasks,list):
        raise ValueError('tasks必须为任务清单')
    ids=[str(t.get('task_no','')).strip() for t in tasks]
    if any(not no for no in ids) or len(ids)!=len(set(ids)):
        raise ValueError('任务编号为空或重复；先确认身份并完成去重')
    for original in sorted(tasks,key=lambda t:t['task_no']):
        original=deepcopy(original)
        for field in ('records','plan'):
            if isinstance(original.get(field),list):
                original[field]=sorted(original[field],key=stable)
        t=deepcopy(original);no=t['task_no']
        warnings=list(t.get('warnings',[]))
        records=[]
        for source in t.get('records',[]):
            r=deepcopy(source)
            if r.get('kind') not in ('point','range','inspection','note','blank'):
                raise ValueError(no+'：记录必须明确日期角色kind')
            r['process']=ALIASES.get(r.get('process',''),r.get('process',''))
            if r['kind'] not in ('note','blank') and not r['process']:
                raise ValueError(no+'：有记录必须确定大工序，依据写入留痕')
            if r['process']=='成品检验' and r['kind'] not in ('blank','note'):
                r['kind']='inspection'
            r.setdefault('reason','')
            for k in ['date','end']:
                if r.get(k) and day(r[k]) is None:
                    warnings.append('无效日期按缺失补齐：'+str(r[k]))
                    r['reason']+='；原日期无效：'+str(r[k])
            if r.get('candidates'):
                winner=choose_candidate(r['candidates'])
                r['date']=winner
                r['reason']+='；候选唯一胜出' if winner else '；候选无唯一胜出，另按统一依据推算'
                r['estimated']=True
            records.append(r)
        records=sorted({stable(r):r for r in records}.values(),key=stable)
        plans=deepcopy(t.get('plan',[]))
        if not isinstance(plans,list):
            raise ValueError(no+'：plan应为已识别的工序计划列表')
        for p in plans:
            p['process']=ALIASES.get(p.get('process',''),p.get('process',''))
        active=[r for r in records if r['kind'] in ('point','range')]
        explicit=[day(r.get('date')) for r in active if r['kind']=='range' and day(r.get('date'))]
        actual=[day(r.get(k)) for r in active for k in ('date','end') if day(r.get(k))]
        manual=t.get('start') or {}
        if isinstance(manual,str): manual={'date':manual,'basis':'给定起点'}
        incoming=t.get('incoming_dates',[])
        first_plan=min([day(p.get('start')) for p in plans if day(p.get('start'))],default=None)
        candidates=[(day(manual.get('date')),manual.get('basis','已核定起点')),
                    (day(t.get('material_date')),'生产计划领料/下料'),
                    (min([day(d) for d in incoming if day(d)],default=None),'来料检验'),
                    (first_plan,'计划第一工序开始'),(min(explicit,default=None),'明确实际开始'),
                    (number_date(no),'任务编号日期估算'),
                    (min(actual,default=None),'最早加工日期估算'),
                    (min([day(p.get('end')) for p in plans if day(p.get('end'))],default=None),'计划完成日期兜底估算'),
                    (min([day(r.get('date')) for r in records if r['kind']=='inspection' and day(r.get('date'))],default=None),'检验日期兜底估算'),
                    (day(t.get('delivery_date')),'交付日期兜底估算'),
                    (day(t.get('batch_date')),'批次参考日期兜底估算')]
        start,basis=next(((d,b) for d,b in candidates if d),(None,'无日期且无记录'))
        if start and actual and start>min(actual):
            warnings.append(f'起点{start}晚于加工{min(actual)}，按实际前移')
            start=min(actual);basis+='（实际校正）'
        if not start and (active or plans or any(r['kind']=='inspection' for r in records)):
            raise ValueError(no+'：请agent从上下文补入batch_date及依据，再生成；不是空白任务')
        events=[];days={}

        def put(proc,d,r,nature,reason):
            key=(proc,d)
            item=days.setdefault(key,{'task_no':no,'product_name':t.get('product_name') or '',
                 'spec':t.get('spec') or '', 'material':t.get('material') or '',
                 'process':proc,'date':d.isoformat(),'evidence':[]})
            evidence={'nature':nature,'reason':reason,'record':r}
            if evidence not in item['evidence']: item['evidence'].append(evidence)

        def span(proc,a,b,r,nature,reason):
            for n in range((b-a).days+1):
                put(proc,a+timedelta(days=n),r,nature,reason)

        # 无日期的有内容记录始终保留，并用同工序计划或相邻有效日期补齐。
        for r in active:
            a=day(r.get('date'));b=day(r.get('end'))
            if not a and b:
                a=b;r['kind']='point'
            if not a:
                same_plan=[day(p.get('end') or p.get('start')) for p in plans if p['process']==r['process']]
                same=[day(x.get('date') or x.get('end')) for x in active if x['process']==r['process'] and x is not r]
                a=min([d for d in same_plan if d],default=None) or min([d for d in same if d],default=None)
                a=a or day(t.get('delivery_date')) or min(actual,default=None) or start
                r['estimated']=True;r['reason']+='；有内容无日期，按同工序计划/相邻日期/交付/起点补齐'
                r['kind']='point'
            r['date']=a.isoformat()
            if r['kind']=='range':
                b=b or a
                if b<a:
                    a,b=b,a;r['estimated']=True;r['reason']+='；起止倒序，按可见端点升序估算'
                    warnings.append(r['process']+'起止倒序已估算，原值保留')
                nature='日期修正区间推算' if r.get('estimated') else '实际明确区间'
                span(r['process'],a,b,r,nature,r['reason'] or '原件分别记录开始与完成')
                events.extend([(a,0,r['process'],r),(b,0,r['process'],r)])
            else:
                events.append((a,1,r['process'],r))
        actual_procs={r['process'] for r in active}
        for p in plans:
            if not p.get('process') or p['process'] in actual_procs: continue
            if p['process']=='成品检验':
                if not any(r['kind']=='inspection' for r in records):
                    records.append({'kind':'inspection','process':'成品检验','date':p.get('end') or p.get('start'),
                                    'estimated':True,'reason':'无实际检验记录，采用计划检验完成日','source':p.get('source',{}),'raw':p.get('raw','')})
                continue
            a=day(p.get('start'));b=day(p.get('end'))
            if not a and not b:
                a=day(t.get('delivery_date')) or max(actual,default=None) or start
                warnings.append(p['process']+'计划有内容但日期缺失，按交付/相邻实际/起点估算')
            r={'kind':'range' if a and b else 'point','process':p['process'],
               'date':(a or b).isoformat(),'end':b.isoformat() if b else None,
               'source':p.get('source',{}),'raw':p.get('raw',''),'planned':True,'reason':'无实际日期，按生产计划补齐'}
            if a and b:
                if b<a:a,b=b,a;r['reason']+='；计划起止倒序校正'
                span(p['process'],a,b,r,'计划补齐',r['reason'])
                events.extend([(a,0,p['process'],r),(b,0,p['process'],r)])
            else:events.append((a or b,1,p['process'],r))
        events.sort(key=lambda e:(e[0],e[1],order(e[2]),stable(e[3])))
        if events and start>events[0][0]:
            warnings.append('补齐后的有效加工日期早于起点，已校正起点')
            start=events[0][0];basis+='（补齐后校正）'
        prev=start;prev_proc=None
        for d,group in itertools.groupby(events,key=lambda e:e[0]):
            group=list(group)
            for _,kind,proc,r in group:
                if kind==0:
                    prev=d;prev_proc=proc
            pts=[e for e in group if e[1]==1]
            for index,(_,_,proc,r) in enumerate(pts):
                a=(prev+timedelta(days=1) if prev_proc==proc else prev) if prev else d
                if index>0 or a>d:a=d
                base='计划补齐' if r.get('planned') else '必要日期推算' if r.get('estimated') else '实际时点连续推算'
                span(proc,a,d,r,base,r.get('reason') or '相关加工时点升序连续推算；跨工序共享交接当天')
                if not r.get('estimated') and not r.get('planned'):
                    put(proc,d,r,'实际完成时点','原件记录该完成日期')
                prev=d;prev_proc=proc
        last=max([d for p,d in days],default=None)
        for r in [x for x in records if x['kind']=='inspection']:
            d=day(r.get('date'))
            if not d:
                d=day(t.get('delivery_date')) or last or start
                if last and d<last:d=last
                r['estimated']=True;r['reason']+='；有检验内容无日期，使用交付/最后加工/起点并顺延'
            if r.get('estimated') and last and d<last:
                r['reason']+=f'；原推定检验日{d}早于最后加工{last}，顺延至最后加工日'
                d=last
            put('成品检验',d,r,'检验日期推算' if r.get('estimated') else '实际检验日期',r.get('reason') or '检验按记录当天计一日')
        span_days=(last-start).days+1 if last and start else 1 if days else 0
        abnormal=span_days>=183
        result['tasks'].append({'task_no':no,'start':start.isoformat() if start else None,
             'last_process_date':last.isoformat() if last else None,'span_days':span_days,'abnormal':abnormal,
             'start_basis':basis,'row_count':len(days),'warnings':sorted(set(map(str,warnings)))})
        priority={'实际明确区间':0,'实际完成时点':1,'实际检验日期':1,'实际时点连续推算':2,'计划补齐':3}
        for item in sorted(days.values(),key=lambda r:(r['date'],order(r['process']))):
            es=sorted(item.pop('evidence'),key=lambda e:(priority.get(e['nature'],4),stable(e)))
            result['abnormal' if abnormal else 'normal'].append(item)
            result['trace'].append(dict(task_no=no,process=item['process'],date=item['date'],
                nature=es[0]['nature'],reason=es[0]['reason'],evidence=es,start=start.isoformat() if start else '',
                start_basis=basis,span_days=span_days,abnormal=abnormal))
        # 原始记录独立保留，包含未参与计算的空白、备注和全部推断前字段。
        result['trace'].append({'task_no':no,'process':'','date':'','nature':'任务与原始记录',
            'reason':'无逐日记录，保留资料事实' if not days else '保留完整原始提取与采用起点',
            'evidence':original,'start':start.isoformat() if start else '', 'start_basis':basis,
            'span_days':span_days,'abnormal':abnormal})
        result['warnings'].extend({'task_no':no,'message':str(w)} for w in sorted(set(map(str,warnings))))
    return result
