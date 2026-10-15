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
        self.assertEqual(view['pages'][0]['text'],'工序日期文字')
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
