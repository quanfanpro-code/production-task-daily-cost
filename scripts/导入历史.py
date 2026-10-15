"""迁移旧提取记录用于回归；保留旧值与语义假设，不替代看原图复核。"""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
from 日期推算 import day,number_date,stable
from 文件保护 import write_json

PROCS=['精磨','研磨','双轴','环抛','环磨','离子束','成品检验','成品检']


def dates(text,year):
    value=str(text or '')
    matches=list(re.finditer(r'(?<!\d)(?:(20\d{2})[-./年])?(\d{1,2})[./月-](\d{1,2})(?!\d)',value))
    out=[];current_year=year;prev=None
    for m in matches:
        y=int(m[1]) if m[1] else current_year
        d=day(f'{y:04}-{int(m[2]):02}-{int(m[3]):02}')
        if d and prev and not m[1] and prev.month>=10 and d.month<=3:
            y+=1;d=day(f'{y:04}-{int(m[2]):02}-{int(m[3]):02}')
        if d:out.append(d.isoformat());prev=d;current_year=y
    return out


def clean(value):
    return re.split(r'[（(]',str(value or ''),maxsplit=1)[0].strip()


def migrate(folder,batch_date):
    if not day(batch_date):raise ValueError('批次参考日期必须有效')
    grouped=defaultdict(list)
    for path in sorted(Path(folder).glob('batch*.json')):
        data=json.loads(path.read_text(encoding='utf-8-sig'))
        for f in data.get('files',[]):
            for old in f.get('tasks',[]):grouped[old['task_no']].append((path.name,f,old))
    tasks=[]
    for no,entries in sorted(grouped.items()):
        # 历史迁移选最完整单份来源，全部候选照录；新批次应先完成图像层归属。
        entries.sort(key=lambda e:(-int(e[1].get('pages_total',0)),-len(e[2].get('process_records',[])),e[0],e[1]['file']))
        batch,f,old=entries[0];plan=old.get('plan') or {};anchor=number_date(no) or day(batch_date)
        t={'task_no':no,'product_name':clean(old.get('product_name')),'spec':clean(old.get('spec')),
           'material':clean(old.get('material')),'batch_date':batch_date,'material_date':plan.get('material_date'),
           'delivery_date':plan.get('delivery_date'),'plan':[],'records':[],'incoming_dates':[],
           'warnings':['历史迁移回归：日期角色按旧字段推定，未据此宣称重新识别全部原图。'],
           'legacy_sources':[{'batch':b,'file':x['file'],'task':o,'page_index':x.get('page_index',[]),'file_anomalies':x.get('file_anomalies',[])} for b,x,o in entries]}
        if len(entries)>1:t['warnings'].append('同任务多份旧记录，保留页数最多单份；同页数按记录完整度与固定文件顺序选择，全部原值留痕')
        t['warnings'] += [stable(a) for a in old.get('anomalies',[])]
        incoming=old.get('incoming_inspection') or []
        for x in incoming if isinstance(incoming,list) else [incoming]:
            if isinstance(x,dict) and day(x.get('date')):t['incoming_dates'].append(x['date'])
        for r in old.get('process_records',[]):
            proc=r.get('process','');raw=stable(r);source={'file':f['file'],'page':r.get('page'),'location':r.get('location',''),'batch':batch}
            kind='range' if day(r.get('date')) and day(r.get('date_end')) else 'point'
            reason='历史双日期字段按起止区间迁移（含同日）；单日期按完成时点迁移'
            if '精改' in proc:proc='精磨';reason+='；手写精改按上下文估为精磨'
            if proc.startswith('Λ'):
                proc='双轴';reason+='；手写工序名不清，按相邻加工记录估为双轴';t['warnings'].append(reason)
            if '整行划改作废' in raw:kind='note';reason='原提取标记整行作废，仅留痕'
            elif proc in ('领料检查','来料检验'):
                if day(r.get('date')):t['incoming_dates'].append(r['date'])
                kind='note'
            elif proc not in PROCS:kind='note';reason='备注/交接非已确定独立大工序；原记录完整保留，回归不冒充最终重识别'
            if proc in ('成品检','成品检验'):kind='inspection';proc='成品检验'
            destination=t.setdefault('notes',[]) if kind=='note' else t['records']
            destination.append({'process':proc,'kind':kind,'date':r.get('date'),'end':r.get('date_end'),
                                 'source':source,'raw':r,'reason':reason,'estimated':bool('辨' in proc or '估为' in reason)})
        for name,v in (plan.get('process_plan_dates') or {}).items():
            if any(w in name for w in ['签署','审核','批准','标准化','检图','会签']):continue
            proc=next((p for p in PROCS if p in name),None)
            if not proc:continue
            if not v or str(v).strip() in ('/','／'):continue
            text=stable(v) if isinstance(v,dict) else str(v)
            if isinstance(v,dict):ds=[d for d in [v.get('date'),v.get('date_end')] if day(d)];parts=[ds]
            else:
                # 多段分开，括号解释保留在raw，不重复读成新日期。
                short=re.split(r'[（(]',text,maxsplit=1)[0]
                parts=[dates(s,anchor.year) for s in re.split(r'、| 与 |；',short)]
            for ds in parts:
                p={'process':proc,'start':ds[0] if len(ds)>1 else None,'end':ds[-1] if ds else None,
                   'raw':{name:v},'source':{'file':f['file'],'page':old.get('page_main',1),'location':name}}
                t['plan'].append(p)
        ins=old.get('final_inspection') or []
        for r in ins if isinstance(ins,list) else [ins]:
            if not isinstance(r,dict) or not r.get('has_content'):continue
            ds=[r['date']] if day(r.get('date')) else []
            # 原字段明确列出多个复检日期时逐一保留。
            if any(x in str(r.get('date_raw','')) for x in ['、',',','，']):
                ds=dates(r['date_raw'],day(r.get('date')).year if day(r.get('date')) else anchor.year) or ds
            for d in sorted(set(ds)) or [None]:
                t['records'].append({'kind':'inspection','process':'成品检验','date':d,'raw':r,
                                     'source':{'file':f['file'],'page':r.get('page'),'location':'成品检验'},'reason':'旧提取检验记录迁移'})
        tasks.append(t)
    return {'tasks':tasks,'migration_only':True,'batch_date':batch_date}


if __name__=='__main__':
    ap=argparse.ArgumentParser(description='旧记录迁移，仅用于回归核对')
    ap.add_argument('--source',required=True);ap.add_argument('--output',required=True);ap.add_argument('--batch-date',required=True)
    args=ap.parse_args();value=migrate(args.source,args.batch_date);write_json(args.output,value)
    print('已迁移任务数：'+str(len(value['tasks'])))
