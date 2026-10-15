"""只验证识别复用与失败恢复，不请求网络服务。"""
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


class Reuse(unittest.TestCase):
    def setUp(self):
        import pymupdf
        self.base = Path(tempfile.mkdtemp(prefix='识别复用测试_'))
        self.source = self.base / '原件'
        self.source.mkdir()
        self.output = self.base / '工作'
        doc = pymupdf.open()
        doc.new_page()
        doc.save(self.source / '合成.pdf')
        doc.close()
        self.mod = importlib.import_module('预处理拆分')

    def test_不识别与顶部及整页共用缓存且只补未读区域(self):
        calls = []
        def recognize(image):
            calls.append(image.shape[0])
            return [[[[0, 0], [10, 0], [10, 10], [0, 10]], '文字'+str(len(calls)), .9]], None
        with patch('rapidocr_onnxruntime.RapidOCR', return_value=recognize):
            first = self.mod.prepare(self.source, self.output, no_ocr=True)
            header = self.mod.prepare(self.source, self.output)
            full = self.mod.prepare(self.source, self.output, ocr_scope='full')
            again = self.mod.prepare(self.source, self.output, ocr_scope='full')
        self.assertEqual(calls, [810, 990])
        self.assertEqual(first['pages'][0]['image'], full['pages'][0]['image'])
        self.assertEqual(len(list((self.output/'缓存').glob('*.json'))), 1)
        self.assertEqual(full['pages'][0]['text'], '文字1\n文字2')
        self.assertEqual(full['pages'][0]['ocr'][1][0][0][1], 810)
        self.assertEqual(full['pages'][0]['ocr_coverage'], 'full')
        self.assertEqual(again['pages'], full['pages'])

    def test_失败默认不原样重试且显式恢复只补失败区域(self):
        calls = []
        def recognize(image):
            calls.append(image.shape[0])
            if len(calls) == 2:
                raise TimeoutError('合成失败')
            return [[[[0, 0], [10, 0], [10, 10], [0, 10]], '已识别', .9]], None
        with patch('rapidocr_onnxruntime.RapidOCR', return_value=recognize):
            bad = self.mod.prepare(self.source, self.output, ocr_scope='full')
            unchanged = self.mod.prepare(self.source, self.output, ocr_scope='full')
            self.assertEqual(calls, [810, 990])
            fixed = self.mod.prepare(self.source, self.output, ocr_scope='full', retry_failed=True)
        self.assertEqual(calls, [810, 990, 990])
        self.assertIn('合成失败', bad['pages'][0]['problem'])
        self.assertNotEqual(unchanged['pages'][0]['ocr_coverage'], 'full')
        self.assertEqual(fixed['pages'][0]['ocr_coverage'], 'full')
        self.assertEqual(fixed['pages'][0]['problem'], '')

    def test_图片缺失仅重建图片不重识别(self):
        from 文件保护 import write_json
        calls = []
        def recognize(image):
            calls.append(image.shape[0])
            return [], None
        with patch('rapidocr_onnxruntime.RapidOCR', return_value=recognize):
            self.mod.prepare(self.source, self.output)
            path = next((self.output/'缓存').glob('*.json'))
            item = json.loads(path.read_text(encoding='utf-8'))
            item['image'] = str(self.output/'不存在.jpg')
            write_json(path, item)
            value = self.mod.prepare(self.source, self.output)
        self.assertEqual(calls, [810])
        self.assertTrue(Path(value['pages'][0]['image']).is_file())

    def test_单文件只处理本人且目录接续不重识别(self):
        path=self.source/'合成.pdf'
        (self.source/'另册.pdf').write_bytes(path.read_bytes())
        calls=[]
        def recognize(image):
            calls.append(1)
            return [[[[0,0],[10,0],[10,10],[0,10]],'工序日期文字',.9]],None
        with patch('rapidocr_onnxruntime.RapidOCR',return_value=recognize):
            first=self.mod.prepare(path,self.output)
            again=self.mod.prepare(self.source,self.output,exclude=['另册.pdf'])
        self.assertEqual(list(first['files']),['合成.pdf'])
        self.assertEqual(first['source_root'],str(self.source.resolve()))
        self.assertEqual(calls,[1])
        self.assertEqual(first['pages'],again['pages'])
        view=json.loads((self.output/'提取视图.json').read_text(encoding='utf-8'))
        self.assertEqual(view['files'],first['files'])
        self.assertNotIn('text',view['pages'][0])
        self.assertEqual(view['pages'][0]['image'],first['pages'][0]['image'])
        self.assertNotIn('ocr',view['pages'][0])
        self.assertNotIn('ocr_regions',view['pages'][0])

    def test_无效原件不再零页成功(self):
        with self.assertRaises(FileNotFoundError):
            self.mod.prepare(self.base/'不存在.pdf',self.output)
        wrong=self.base/'说明.txt';wrong.write_text('说明',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'PDF'):
            self.mod.prepare(wrong,self.output)
        empty=self.base/'空目录';empty.mkdir()
        with self.assertRaisesRegex(ValueError,'没有可处理'):
            self.mod.prepare(empty,self.output)

    def test_图纸保留原页但不补读正文也不进入提取视图(self):
        calls=[]
        def recognize(image):
            calls.append(image.shape[0])
            return [[[[0,0],[10,0],[10,10],[0,10]],'对零件的要求 光学均匀性 光吸收系数 条纹度',.9]],None
        with patch('rapidocr_onnxruntime.RapidOCR',return_value=recognize):
            result=self.mod.prepare(self.source,self.output,ocr_scope='full')
            self.mod.prepare(self.source,self.output,ocr_scope='full')
        self.assertEqual(calls,[810])
        self.assertEqual(len(result['pages']),1)
        self.assertTrue(Path(result['pages'][0]['image']).is_file())
        view=json.loads((self.output/'提取视图.json').read_text(encoding='utf-8'))
        self.assertEqual(view['pages'],[])
        self.assertEqual(view['excluded_pages'][0]['page'],1)

    def test_页面分类优先保留计划记录检验和未知页(self):
        drawing='对零件的要求 光学均匀性 光吸收系数 条纹度'
        for title,expected in [('生产单编号','生产计划'),('生产记录表','工序记录'),('成品检验记录表','检验记录')]:
            self.assertEqual(self.mod.page_kind(title+drawing),expected)
        self.assertEqual(self.mod.page_kind('技术要求 CAD 会签'),'图纸规范')
        self.assertEqual(self.mod.page_kind('规格 日期不清'),'候选附件')

    def test_工序日期整列保留首尾记录不依赖表头纵坐标(self):
        from PIL import Image,ImageDraw
        from 区域裁剪 import cost_regions
        image=Image.new('RGB',(500,900),'white');draw=ImageDraw.Draw(image)
        draw.rectangle((400,180,445,205),fill='black')
        path=self.base/'竖版记录.jpg';image.save(path)
        def row(text,x,y,w):return [[[x,y],[x+w,y],[x+w,y+20],[x,y+20]],text,.9]
        item={'candidate_type':'工序记录','image':str(path),'page_cache_key':'合成区域',
              'ocr':[row('开始时间',395,400,65),row('研磨记录',40,260,55),row('来料日期',150,100,60)]}
        result=cost_regions(item,self.base/'局部')
        self.assertTrue(any(b[0]<=420<=b[2] and b[1]==0 and b[3]==900 for b in result['crop_boxes']))
        self.assertTrue(Path(result['image']).is_file())
        self.assertEqual(len(result['crop_boxes']),1)
        with Image.open(path) as original,Image.open(result['image']) as cropped:
            box=result['crop_boxes'][0]
            self.assertEqual(box,[0,0,cropped.width,900])
            self.assertEqual(cropped.tobytes(),original.convert('RGB').crop(tuple(box)).tobytes())

    def test_检验只切右边且保留原行位置无坐标时保留原页(self):
        from PIL import Image,ImageDraw
        from 区域裁剪 import cost_regions
        image=Image.new('RGB',(1000,600),'white');draw=ImageDraw.Draw(image)
        for x in [20,150,700,900]:draw.line((x,80,x,520),fill='black',width=2)
        draw.text((35,95),'process',fill='black');draw.text((720,95),'date',fill='black');draw.text((720,160),'2026-05-06',fill='black')
        path=self.base/'检验.jpg';image.save(path)
        item={'candidate_type':'检验记录','image':str(path),'page_cache_key':'检验合成',
              'ocr':[[[[30,90],[130,90],[130,115],[30,115]],'工序名称',.9],
                     [[[715,90],[820,90],[820,115],[715,115]],'检验日期',.9]]}
        result=cost_regions(item,self.base/'局部')
        self.assertLess(result['input_pixels'],result['original_pixels'])
        self.assertEqual(len(result['crop_boxes']),1)
        self.assertEqual(result['crop_boxes'][0][:2],[0,0])
        with Image.open(path) as original,Image.open(result['image']) as cropped:
            self.assertEqual(cropped.tobytes(),original.convert('RGB').crop(tuple(result['crop_boxes'][0])).tobytes())
        self.assertTrue(all(b[1]==0 and b[3]==600 for b in result['crop_boxes']))
        item['ocr']=[]
        fallback=cost_regions(item,self.base/'局部')
        self.assertEqual(fallback['image'],str(path));self.assertTrue(fallback['problem'])

    def test_定位计划字段只补一次下部识别(self):
        calls=[]
        def recognize(image):
            calls.append(image.shape[0])
            text='生产任务单 生产单编号' if len(calls)==1 else '图样名称 材料'
            return [[[[0,0],[10,0],[10,10],[0,10]],text,.9]],None
        with patch('rapidocr_onnxruntime.RapidOCR',return_value=recognize):
            first=self.mod.prepare(self.source,self.output)
            self.mod.prepare(self.source,self.output)
        self.assertEqual(calls,[810,990])
        self.assertEqual(first['pages'][0]['ocr_coverage'],'full')


    def test_本地识别具体读数不进入提取视图(self):
        from 区域裁剪 import page_view
        item={'source_file':'合成.pdf','page':1,'image':'原页.png','candidate_type':'工序记录',
              'text':'本地识别日期8.8',
              'ocr':[[[[10,10],[20,10],[20,20],[10,20]],'8.8',.9]],
              'cost_region':{'image':'整块.png','crop_boxes':[[0,0,100,200]]}}
        view=page_view(item)
        self.assertEqual(view['image'],'整块.png')
        self.assertNotIn('text',view)
        self.assertNotIn('coarse_rows',view)


if __name__ == '__main__':
    unittest.main(verbosity=2)
