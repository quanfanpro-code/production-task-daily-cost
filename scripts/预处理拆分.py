"""本地转正与一次粗识别共用缓存；按已核定方案保全每一页。"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re

from 文件保护 import digest, safe_child, separate_output, write_bytes, write_json

VERSION='2026-10-15.3'
PAGE_VERSION='2026-10-15.1'  # 保持已有页缓存身份，范围升级不重做成功识别。


def prepare(source, output, limit=None, no_ocr=False, exclude=None,
            ocr_scope='header', retry_failed=False, page_list=None):
    import pymupdf
    from PIL import Image
    from 方向识别 import 识别器
    source=Path(source).resolve()
    if not source.exists():raise FileNotFoundError('原件路径不存在：'+str(source))
    if source.is_file():
        if source.suffix.lower()!='.pdf':raise ValueError('单文件输入必须是PDF：'+str(source))
        paths=[source]
        source,output=separate_output(source.parent,output)
    else:
        source,output=separate_output(source,output)
        paths=sorted(source.rglob('*.pdf'))
    excluded=[safe_child(source,p) for p in (exclude or [])]
    paths=[p for p in paths if not p.is_relative_to(output) and not any(p.is_relative_to(e) for e in excluded)]
    if not paths:raise ValueError('指定范围内没有可处理的PDF，请检查原件路径与排除目录')
    model=Path(__file__).parent/'models/rapid_orientation.onnx'
    model_problem=''
    try:
        model_hash=digest(model)
    except Exception as exc:
        model_hash='unavailable';model_problem=f'方向模型不可用，保留原方向供agent查看：{exc}'
    detector=None;detector_attempted=False
    if ocr_scope not in ('header','full'):raise ValueError('识别范围必须为header或full')
    selected=None if page_list is None else {(p['source_file'],p['page']) for p in page_list}
    if selected is not None and any(type(n) is not int or n<1 for _,n in selected):
        raise ValueError('指定原页必须为正整数')
    seen=set()
    reader=None
    files={};pages=[];file_errors=[]
    for path in paths:
        rel=path.relative_to(source).as_posix()
        try:
            sha=digest(path);doc=pymupdf.open(path)
            if doc.needs_pass:
                doc.close();raise ValueError('加密文件，需要可读取副本')
        except Exception as exc:
            file_errors.append({'source_file':rel,'error':str(exc)});continue
        with doc:
            files[rel]={'pages':len(doc),'sha256':sha}
            for n,page in enumerate(doc,1):
                if limit and len(pages)>=limit:break
                seen.add((rel,n))
                key=hashlib.sha256(f'{PAGE_VERSION}|{sha}|{n}|1800|True|{model_hash}'.encode()).hexdigest()
                cache=output/'缓存'/f'{key}.json'
                legacy_key=hashlib.sha256(f'{PAGE_VERSION}|{sha}|{n}|1800|False|{model_hash}'.encode()).hexdigest()
                prior=cache if cache.exists() else output/'缓存'/f'{legacy_key}.json'
                cache_problem='';item={}
                if prior.exists():
                    try:
                        item=json.loads(prior.read_text(encoding='utf-8'))
                        if not isinstance(item,dict) or item.get('sha256')!=sha:raise ValueError('缓存来源不符')
                    except (ValueError,OSError):
                        item={};cache_problem='缓存损坏，已重新生成；'
                try:native=page.get_text().strip()
                except Exception:native=''
                regions=item.get('ocr_regions',{})
                if 'ocr_regions' not in item and item and len(native)<30 and prior==cache:
                    if '粗识别失败' in item.get('problem',''):
                        regions={'header':{'status':'failed','rows':[],'error':item['problem']}}
                    elif item.get('ocr') or not item.get('problem'):
                        regions={'header':{'status':'ok','rows':item.get('ocr',[]),'error':''}}
                wanted=[] if no_ocr or (selected is not None and (rel,n) not in selected) or len(native)>=30 else ['header']
                if wanted and ocr_scope=='full':wanted.append('body')
                pending=[name for name in wanted if name not in regions or
                         (regions[name]['status']=='failed' and retry_failed)]
                if not retry_failed and any(r['status']=='failed' for r in regions.values()):pending=[]
                upright=None
                if not item.get('image') or not Path(item['image']).is_file():
                    try:
                        pix=page.get_pixmap(matrix=pymupdf.Matrix(1800/max(page.rect.width,page.rect.height),1800/max(page.rect.width,page.rect.height)),alpha=False)
                        image=Image.open(io.BytesIO(pix.tobytes('png'))).convert('RGB')
                    except Exception as exc:
                        pages.append({'source_file':rel,'page':n,'sha256':sha,'problem':'页面渲染失败：'+str(exc),'image':None});continue
                    from 方向识别 import 判断
                    if not detector_attempted:
                        detector_attempted=True
                        try:detector=识别器(model)
                        except Exception as exc:model_problem=f'方向模型不可用，保留原方向供agent查看：{exc}'
                    try:direction=detector.predict(image) if detector else 判断(None,0.,model_problem)
                    except Exception as exc:direction=判断(None,0.,'方向判断失败，保留原方向供agent查看：'+str(exc))
                    correction=direction.rotation or 0
                    upright=image.rotate(-correction,expand=True)
                    jpg=output/'页面'/f'{key}.jpg';buf=io.BytesIO();upright.save(buf,format='JPEG',quality=90)
                    write_bytes(jpg,buf.getvalue())
                    item.update(rotation=correction,direction_uncertain=direction.rotation is None,
                                direction_reason=direction.reason,direction_confidence=direction.confidence,image=str(jpg))
                if pending and upright is None:
                    with Image.open(item['image']) as im:upright=im.convert('RGB')
                for name in pending:
                    top=0 if name=='header' else round(upright.height*.45)
                    bottom=round(upright.height*.45) if name=='header' else upright.height
                    try:
                        if reader is None:
                            from rapidocr_onnxruntime import RapidOCR
                            reader=RapidOCR()
                        import numpy as np
                        crop=upright.crop((0,top,upright.width,bottom))
                        rows,_=reader(np.asarray(crop)[:,:,::-1])
                        shifted=[[[[float(x),float(y)+top] for x,y in row[0]],str(row[1]),float(row[2])] for row in (rows or [])]
                        regions[name]={'status':'ok','rows':shifted,'error':''}
                    except Exception as exc:
                        regions[name]={'status':'failed','rows':[],'error':f'{type(exc).__name__}: {exc}'}
                        break
                ocr=[r for name in ('header','body') for r in regions.get(name,{}).get('rows',[])]
                text=native if len(native)>=30 else ('\n'.join(str(r[1]) for r in ocr) or native)
                coverage='full' if len(native)>=30 or all(regions.get(k,{}).get('status')=='ok' for k in ('header','body')) else 'header' if regions.get('header',{}).get('status')=='ok' else 'none'
                problem=cache_problem+'；'.join('识别失败，保留成功结果，诊断后可显式重试：'+r['error'] for r in regions.values() if r['status']=='failed')
                title='生产计划' if ('生产' in text and ('任务' in text or '计划' in text)) else '候选附件'
                numbers=sorted(set(re.findall(r'(?<!\d)\d{8}[-－]?\d{1,6}(?!\d)',text)))
                item.update(source_file=rel,page=n,sha256=sha,candidate_type=title,
                            number_candidates=numbers,text=text,ocr=ocr,problem=problem,
                            ocr_regions=regions,ocr_coverage=coverage,
                            text_source='pdf_text' if len(native)>=30 else 'local_ocr',
                            ocr_engine='RapidOCR 本机默认参数',page_cache_key=key)
                write_json(cache,item);pages.append(item)
        if limit and len(pages)>=limit:break
    if selected is not None and selected-seen:raise ValueError('指定页不在本次原件范围内：'+str(sorted(selected-seen)))
    index={'version':VERSION,'source_root':str(source),'files':files,'pages':pages,
        'partial':bool(limit),'file_errors':file_errors,'model_problem':model_problem,'excluded':exclude or [],
        'notice':'候选不是最终归属；agent核对原图后另写拆分方案。文件错误须单独汇报，不能冒充全批已完成。'}
    write_json(output/'准备索引.json',index)
    # 提取时只读一份精简视图，坐标与区域缓存仍在准备索引中供实际疑点查阅。
    fields=('source_file','page','image','rotation','candidate_type','number_candidates',
            'text','ocr_coverage','direction_uncertain','problem')
    write_json(output/'提取视图.json',{'source_root':str(source),'files':files,
               'pages':[{k:p[k] for k in fields if k in p} for p in pages],
               'file_errors':file_errors,'partial':bool(limit),'model_problem':model_problem})
    return index


def split(plan,output):
    import pymupdf
    source,output=separate_output(plan['source_root'],output)
    files=plan['files'];segments=plan['segments'];coverage={name:set() for name in files}
    normalized=[];ids=set();kept={};filenames=set()
    # 完成所有覆盖与来源校验后才开始写输出。
    for name,meta in files.items():
        path=safe_child(source,name)
        if digest(path)!=meta['sha256']:raise ValueError('来源文件已变化：'+name)
        with pymupdf.open(path) as doc:
            if len(doc)!=meta['pages']:raise ValueError('来源页数变化：'+name)
    for entry in segments:
        s=dict(entry);name=s['source_file'];a=s['start'];b=s['end']
        if name not in files or not isinstance(a,int) or not isinstance(b,int) or not 1<=a<=b<=files[name]['pages']:
            raise ValueError('拆分页码或来源无效')
        chosen=s.get('page_numbers',list(range(a,b+1)))
        if not isinstance(chosen,list) or not chosen or any(type(n) is not int or not a<=n<=b for n in chosen) or chosen!=sorted(set(chosen)):
            raise ValueError('指定页列表须非空、升序、不重复且在起止页内')
        s['page_numbers']=chosen;current=set(chosen)
        if current&coverage[name]:raise ValueError('来源页重复分配：'+name)
        coverage[name]|=current
        s.setdefault('id',name+':'+str(a)+'-'+str(b))
        if s['id'] in ids:raise ValueError('分段标识重复')
        ids.add(s['id'])
        if s['action'] not in ('keep','duplicate','unassigned'):raise ValueError('无效拆分动作')
        if s['action']=='keep':
            if not s.get('task_no'):raise ValueError('保留任务必须有核定编号')
            if s['task_no'] in kept:raise ValueError('同号保留多个来源，先核对身份和完整性')
            kept[s['task_no']]=s['id']
        rotations=s.get('rotations',{})
        if any(str(k) not in {str(i) for i in current} or v not in (0,90,180,270) for k,v in rotations.items()):
            raise ValueError('转正页码或角度无效')
        normalized.append(s)
    for name,meta in files.items():
        if coverage[name]!=set(range(1,meta['pages']+1)):raise ValueError('存在未说明去向的来源页：'+name)
    by_id={s['id']:s for s in normalized}
    for s in normalized:
        if s['action']=='duplicate':
            target=by_id.get(s.get('duplicate_of'))
            if not target or target['action']!='keep' or target.get('task_no')!=s.get('task_no') or not s.get('reason'):
                raise ValueError('重复件必须指向同任务保留件，并记录判断依据')
        else:
            label=s.get('task_no') or '未归属_'+hashlib.sha256(s['id'].encode()).hexdigest()[:10]
            label=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',str(label)).rstrip('. ')
            if not label or label.upper() in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}:label='任务_'+label
            filename=label+'.pdf'
            if filename.casefold() in filenames:raise ValueError('输出文件名碰撞；请明确不同任务身份')
            filenames.add(filename.casefold());s['output_file']=filename
    maps=[]
    for s in sorted(normalized,key=lambda x:(x['source_file'],x['start'])):
        target=by_id[s['duplicate_of']] if s['action']=='duplicate' else s
        if s['action']!='duplicate':
            with pymupdf.open(safe_child(source,s['source_file'])) as doc:
                out=pymupdf.open()
                for n in s['page_numbers']:out.insert_pdf(doc,from_page=n-1,to_page=n-1)
                for k,page in zip(s['page_numbers'],out):
                    page.set_rotation((page.rotation+s.get('rotations',{}).get(str(k),0))%360)
                data=out.tobytes(garbage=4,deflate=True,no_new_id=True);out.close()
                path=write_bytes(safe_child(output,s['output_file']),data)
                with pymupdf.open(path) as check:
                    if len(check)!=len(s['page_numbers']):raise ValueError('拆分回读页数不符')
        for output_page,n in enumerate(s['page_numbers'],1):
            maps.append({'source_file':s['source_file'],'source_page':n,'segment':s['id'],
                         'action':s['action'],'task_no':s.get('task_no',''),'output_file':target['output_file'],
                         'output_page':output_page if s['action']!='duplicate' else None,
                         'reason':s.get('reason',''),'source_sha256':files[s['source_file']]['sha256']})
    result={'version':VERSION,'source_root':str(source),'output_root':str(output),'pages':maps,'segments':normalized,
            'source_pages':len(maps),'kept_pages':sum(len(s['page_numbers']) for s in normalized if s['action']!='duplicate')}
    write_json(output/'拆分清单.json',result)
    return result


if __name__=='__main__':
    ap=argparse.ArgumentParser(description='准备扫描页或执行核定拆分方案')
    ap.add_argument('action',choices=['prepare','split']);ap.add_argument('--source');ap.add_argument('--plan');ap.add_argument('--output',required=True)
    ap.add_argument('--limit-pages',type=int);ap.add_argument('--no-ocr',action='store_true')
    ap.add_argument('--exclude',action='append',default=[],help='排除原件目录下已确认的旧输出子目录，可重复指定')
    ap.add_argument('--ocr-scope',choices=['header','full'],default='header',help='整页只补未覆盖区域，已有识别不重复请求')
    ap.add_argument('--retry-failed',action='store_true',help='诊断后仅补失败区域，默认不原样重试')
    ap.add_argument('--page-list',help='需补读的原件页JSON列表，字段source_file、page；仍复用同一工作目录')
    args=ap.parse_args()
    if args.action=='prepare':
        if not args.source:ap.error('prepare需要--source')
        chosen=json.loads(Path(args.page_list).read_text(encoding='utf-8-sig')) if args.page_list else None
        result=prepare(args.source,args.output,args.limit_pages,args.no_ocr,args.exclude,
                       args.ocr_scope,args.retry_failed,chosen)
    else:
        if not args.plan:ap.error('split需要--plan')
        result=split(json.loads(Path(args.plan).read_text(encoding='utf-8-sig')),args.output)
    summary={'pages':len(result['pages']),'output':args.output}
    if args.action=='prepare':
        summary.update(files=len(result['files']),view=str(Path(args.output).resolve()/'提取视图.json'),
                       file_errors=result['file_errors'],partial=result['partial'],model_problem=result['model_problem'])
    print(json.dumps(summary,ensure_ascii=False))
