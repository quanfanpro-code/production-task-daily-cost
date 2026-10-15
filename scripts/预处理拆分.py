"""本地转正与一次粗识别共用缓存；按已核定方案保全每一页。"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re

from 文件保护 import digest, safe_child, separate_output, write_bytes, write_json

VERSION='2026-10-15.1'


def prepare(source, output, limit=None, no_ocr=False, exclude=None):
    import pymupdf
    from PIL import Image
    from 方向识别 import 识别器
    source,output=separate_output(source,output)
    excluded=[safe_child(source,p) for p in (exclude or [])]
    model=Path(__file__).parent/'models/rapid_orientation.onnx'
    model_problem=''
    try:
        detector=识别器(model);model_hash=digest(model)
    except Exception as exc:
        detector=None;model_hash='unavailable';model_problem=f'方向模型不可用，保留原方向供agent查看：{exc}'
    reader=None
    files={};pages=[];file_errors=[]
    for path in sorted(source.rglob('*.pdf')):
        if path.is_relative_to(output) or any(path.is_relative_to(p) for p in excluded):continue
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
                key=hashlib.sha256(f'{VERSION}|{sha}|{n}|1800|{not no_ocr}|{model_hash}'.encode()).hexdigest()
                cache=output/'缓存'/f'{key}.json'
                cache_problem=''
                if cache.exists():
                    try:item=json.loads(cache.read_text(encoding='utf-8'))
                    except (ValueError,OSError):
                        item={};cache_problem='缓存损坏，已重新生成；'
                    # 同内容不同文件共用识别，归属仍逐页登记。
                    item.update(source_file=rel,page=n)
                    if item.get('image') and Path(item['image']).is_file():
                        pages.append(item);continue
                try:
                    pix=page.get_pixmap(matrix=pymupdf.Matrix(1800/max(page.rect.width,page.rect.height),1800/max(page.rect.width,page.rect.height)),alpha=False)
                    image=Image.open(io.BytesIO(pix.tobytes('png'))).convert('RGB')
                except Exception as exc:
                    pages.append({'source_file':rel,'page':n,'sha256':sha,'problem':'页面渲染失败：'+str(exc),'image':None});continue
                from 方向识别 import 判断
                try:direction=detector.predict(image) if detector else 判断(None,0.,model_problem)
                except Exception as exc:direction=判断(None,0.,'方向判断失败，保留原方向供agent查看：'+str(exc))
                correction=direction.rotation or 0
                upright=image.rotate(-correction,expand=True)
                jpg=output/'页面'/f'{key}.jpg';buf=io.BytesIO();upright.save(buf,format='JPEG',quality=90)
                write_bytes(jpg,buf.getvalue())
                try:text=page.get_text().strip()
                except Exception:text=''
                ocr=[];problem=cache_problem
                if len(text)<30 and not no_ocr:
                    try:
                        if reader is None:
                            from rapidocr_onnxruntime import RapidOCR
                            reader=RapidOCR()
                        import numpy as np
                        # 页面顶部用于找任务边界；保留框位置，不把局部文字当整页正文。
                        header=upright.crop((0,0,upright.width,round(upright.height*.45)))
                        rows,_=reader(np.asarray(header)[:,:,::-1])
                        ocr=rows or [];text='\n'.join(str(r[1]) for r in ocr)
                    except Exception as exc:
                        problem=f'粗识别失败，需agent看图继续：{type(exc).__name__}: {exc}'
                title='生产计划' if ('生产' in text and ('任务' in text or '计划' in text)) else '候选附件'
                numbers=sorted(set(re.findall(r'(?<!\d)\d{8}[-－]?\d{1,6}(?!\d)',text)))
                item={'source_file':rel,'page':n,'sha256':sha,'rotation':correction,
                      'direction_uncertain':direction.rotation is None,'direction_reason':direction.reason,
                      'direction_confidence':direction.confidence,'image':str(jpg),
                      'candidate_type':title,'number_candidates':numbers,'text':text,'ocr':ocr,'problem':problem}
                write_json(cache,item);pages.append(item)
        if limit and len(pages)>=limit:break
    index={'version':VERSION,'source_root':str(source),'files':files,'pages':pages,
        'partial':bool(limit),'file_errors':file_errors,'model_problem':model_problem,'excluded':exclude or [],
        'notice':'候选不是最终归属；agent核对原图后另写拆分方案。文件错误须单独汇报，不能冒充全批已完成。'}
    write_json(output/'准备索引.json',index)
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
    args=ap.parse_args()
    if args.action=='prepare':
        if not args.source:ap.error('prepare需要--source')
        result=prepare(args.source,args.output,args.limit_pages,args.no_ocr,args.exclude)
    else:
        if not args.plan:ap.error('split需要--plan')
        result=split(json.loads(Path(args.plan).read_text(encoding='utf-8-sig')),args.output)
    print(json.dumps({'pages':len(result['pages']),'output':args.output},ensure_ascii=False))
