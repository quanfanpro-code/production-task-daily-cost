"""必要区域交给GLM-OCR识别一次，缓存供拆分、提取和日期推算复用。"""
import argparse
import base64
import ctypes
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

from 文件保护 import write_bytes, write_json

ENDPOINT = 'https://open.bigmodel.cn/api/paas/v4/layout_parsing'
PARAMS = {'model': 'glm-ocr', 'return_crop_images': False, 'need_layout_visualization': False}
KEY_FILE = Path.home()/'.config'/'production-task-daily-cost'/'glm-ocr.key'


def protect(data, decrypt=False):
    """使用Windows当前用户保护密钥，不在项目内保存凭据。"""
    class Blob(ctypes.Structure):
        _fields_ = [('size', ctypes.c_ulong), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buf = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    fn = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OSError('Windows密钥保护操作失败')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)


def api_key():
    key = os.environ.get('ZHIPU_API_KEY', '').strip()
    if not key and KEY_FILE.exists():
        key = protect(KEY_FILE.read_bytes(), decrypt=True).decode('utf-8').strip()
    if not key:
        raise ValueError('尚未配置智谱密钥：设置ZHIPU_API_KEY或使用本机用户密钥文件')
    return key


def recognize(image, cache, retry_failed=False):
    image = Path(image)
    raw = image.read_bytes()
    suffix = image.suffix.lower()
    if suffix not in ('.jpg', '.jpeg', '.png') or len(raw) > 10*1024*1024:
        raise ValueError('识别输入须为不超过10MB的JPG或PNG图片')
    identity = {'endpoint': ENDPOINT, 'parameters': PARAMS, 'image_sha256': hashlib.sha256(raw).hexdigest()}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    path = Path(cache)/(key+'.json')
    if path.exists():
        record = json.loads(path.read_text(encoding='utf-8-sig'))
        if record['status'] == 'ok':
            return {'cache_hit': True, 'request_seconds': 0, 'cache_file': str(path), 'record': record}
        if not retry_failed:
            raise RuntimeError('已有失败记录，本次不重复请求：'+record['error'])
    secret = api_key()
    mime = 'image/png' if suffix == '.png' else 'image/jpeg'
    payload = dict(PARAMS, file='data:'+mime+';base64,'+base64.b64encode(raw).decode('ascii'))
    request = urllib.request.Request(ENDPOINT, json.dumps(payload).encode('utf-8'),
                                     {'Authorization': 'Bearer '+secret, 'Content-Type': 'application/json'})
    started = time.perf_counter()
    record = dict(identity, created=datetime.now().isoformat(timespec='seconds'))
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            body = json.load(response)
        if not isinstance(body, dict) or 'error' in body or not isinstance(body.get('md_results'), str):
            raise ValueError('服务未返回有效识别结果：'+json.dumps(body, ensure_ascii=False)[:500])
        record.update(status='ok', response=body)
    except Exception as exc:
        if isinstance(exc, urllib.error.HTTPError):
            detail = exc.read(4096).decode('utf-8', errors='replace')
            message = f'服务错误{exc.code}：{detail}'
        else:
            message = type(exc).__name__+'：'+str(exc)
        message = message.replace(secret, '[密钥已隐藏]')
        record.update(status='failed', error=message)
    record['request_seconds'] = round(time.perf_counter()-started, 3)
    write_json(path, record)
    if record['status'] != 'ok':
        raise RuntimeError(record['error'])
    return {'cache_hit': False, 'request_seconds': record['request_seconds'], 'cache_file': str(path), 'record': record}


def run(pages, output, cache, retry_failed=False):
    output = Path(output)
    result = {'model': PARAMS['model'], 'pages': [], 'failed': [], 'network_requests': 0, 'cache_hits': 0}
    started = time.perf_counter()
    usage = {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}
    usage_missing = False
    for page in pages:
        if page.get('candidate_type') == '图纸规范':
            continue
        try:
            hit = recognize(page['image'], cache, retry_failed=retry_failed)
        except Exception as exc:
            result['failed'].append({'source_file': page['source_file'], 'page': page['page'], 'error': str(exc)})
            break
        body = hit['record']['response']
        item = {k: page.get(k) for k in ('source_file', 'page', 'candidate_type', 'image', 'crop_boxes', 'region_problem')}
        item.update(text=body['md_results'],
                    layout=[{k:v for k,v in block.items() if k!='content'} for group in body.get('layout_details',[])
                            for block in (group if isinstance(group,list) else [group]) if isinstance(block,dict)],
                    cache_file=hit['cache_file'], cache_hit=hit['cache_hit'],
                    request_seconds=hit['request_seconds'], usage=body.get('usage'))
        result['pages'].append(item)
        result['cache_hits' if hit['cache_hit'] else 'network_requests'] += 1
        if not hit['cache_hit']:
            values = body.get('usage') or {}
            for k in usage:
                if not isinstance(values.get(k), (int, float)):
                    usage_missing = True
                else:
                    usage[k] += values[k]
    result.update(elapsed_seconds=round(time.perf_counter()-started, 3),
                  current_request_usage=None if usage_missing else usage)
    write_json(output/'专业识别结果.json', result)
    return result


def run_index(index, output, cache, retry_failed=False):
    """同一程序从已有本地索引裁图、复用专业缓存并返回文字，不经代理交接。"""
    from 区域裁剪 import cost_regions,page_view
    started=time.perf_counter()
    pages=[]
    for original in index['pages']:
        if original.get('candidate_type')=='图纸规范':continue
        page=dict(original)
        page['cost_region']=cost_regions(page,Path(output)/'竖切')
        pages.append(page_view(page))
    preparation_seconds=round(time.perf_counter()-started,3)
    write_json(Path(output)/'提取视图.json',{'pages':pages})
    result=run(pages,output,cache,retry_failed)
    result.update(preparation_seconds=preparation_seconds,pipeline_seconds=round(time.perf_counter()-started,3))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    source=parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--pages',help='已有提取视图，image为必要区域')
    source.add_argument('--index',help='已有本地准备索引，直接裁图并识别，不重做本地OCR')
    parser.add_argument('--output', required=True)
    parser.add_argument('--cache', required=True, help='所有批次共用的成功识别缓存目录')
    parser.add_argument('--retry-failed', action='store_true', help='诊断失败原因后才使用')
    args = parser.parse_args()
    value = json.loads(Path(args.index or args.pages).read_text(encoding='utf-8-sig'))
    result = (run_index(value,args.output,args.cache,args.retry_failed) if args.index else
              run(value['pages'] if isinstance(value,dict) else value,args.output,args.cache,args.retry_failed))
    print(json.dumps({k: v for k, v in result.items() if k != 'pages'}, ensure_ascii=False))
    if result['failed']:
        raise SystemExit(1)
