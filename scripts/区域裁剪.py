"""用已有文字坐标裁出成本字段；原页和坐标留在准备索引。"""
import io
import re
from pathlib import Path

from 文件保护 import write_bytes


def _box(row):
    points=row[0]
    return (min(p[0] for p in points),min(p[1] for p in points),
            max(p[0] for p in points),max(p[1] for p in points))


def page_view(item):
    """只输出页面与裁剪定位；本地识别文字不进入具体字段提取。"""
    region=item.get('cost_region') or {}
    view={k:item[k] for k in ('source_file','page','rotation','candidate_type',
          'number_candidates','ocr_coverage','direction_uncertain','problem') if k in item}
    boxes=region.get('crop_boxes',[])
    view.update(image=region.get('image',item.get('image')),crop_boxes=boxes,
                region_problem=region.get('problem',''))
    return view


def cost_regions(item,output):
    """每页保留左侧一整块，已有裁图直接使用。"""
    from PIL import Image
    if item.get('candidate_type')=='图纸规范':return None
    saved=item.get('cost_region') or {}
    if saved.get('image') and saved.get('crop_boxes') and Path(saved['image']).is_file():return saved
    if not item.get('image'):return {'problem':'没有可用页图','image':None}
    with Image.open(item['image']) as im:image=im.convert('RGB')
    width,height=image.size
    rows=item.get('ocr',[]);kind=item.get('candidate_type','')
    dates=[_box(r) for r in rows if any(v in r[1] for v in
        ('加工时间','加工节点','开始时间','开始时润','结束时间','结术时间','完成日期','生产日期','检验日期','检验口期','来料日期','领料时间'))]
    if kind=='生产计划':
        # 计划属性和备用日期分散，保留足够宽度，仍只生成一张图。
        right=round(width*.96) if rows else width
    elif dates:
        right=min(width,round(max(b[2] for b in dates)+max(80,width*.08)))
    else:return {'image':item['image'],'crop_boxes':[],'problem':'未定位的版式，保留原页'}
    target=Path(output)/(str(item['page_cache_key'])+'_左侧整块.png')
    buf=io.BytesIO();image.crop((0,0,right,height)).save(buf,format='PNG');write_bytes(target,buf.getvalue())
    return {'image':str(target),'crop_boxes':[[0,0,right,height]],'original_pixels':width*height,
            'input_pixels':right*height,'problem':''}
