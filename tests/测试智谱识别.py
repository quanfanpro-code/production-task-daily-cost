"""缓存、失败处理和当前调用用量，不用真实密钥或生产图片。"""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import 智谱文字识别 as ocr


class 识别复用(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='智谱识别测试_'))
        self.image = self.root/'合成.png'
        self.image.write_bytes(b'fake-image')
        self.cache = self.root/'缓存'
        self.body = {'id': 'test-only', 'model': 'glm-ocr', 'md_results': '日期 1.4',
                     'layout_details': [[{'label':'table','bbox_2d':[1,2,30,40],'content':'日期 1.4'}]], 'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}

    def response(self):
        return io.BytesIO(json.dumps(self.body).encode())

    def test_成功只请求一次且不保存密钥(self):
        with patch.object(ocr, 'api_key', return_value='test-secret'), patch.object(ocr.urllib.request, 'urlopen', side_effect=lambda *a, **k: self.response()) as call:
            first = ocr.recognize(self.image, self.cache)
            second = ocr.recognize(self.image, self.cache)
        self.assertEqual(call.call_count, 1)
        self.assertFalse(first['cache_hit'])
        self.assertTrue(second['cache_hit'])
        self.assertEqual(second['record']['response']['usage']['total_tokens'], 15)
        self.assertNotIn('test-secret', Path(first['cache_file']).read_text())

    def test_图像变化使用新缓存(self):
        with patch.object(ocr, 'api_key', return_value='test-secret'), patch.object(ocr.urllib.request, 'urlopen', side_effect=lambda *a, **k: self.response()) as call:
            first = ocr.recognize(self.image, self.cache)
            changed = self.root/'改变.png'
            changed.write_bytes(b'different-image')
            second = ocr.recognize(changed, self.cache)
        self.assertEqual(call.call_count, 2)
        self.assertNotEqual(first['cache_file'], second['cache_file'])

    def test_失败留存且不原样自动重试(self):
        error = urllib.error.HTTPError(ocr.ENDPOINT, 429, 'limited', {}, io.BytesIO(b'test-secret limited'))
        with patch.object(ocr, 'api_key', return_value='test-secret'), patch.object(ocr.urllib.request, 'urlopen', side_effect=error) as call:
            with self.assertRaisesRegex(RuntimeError, '429'):
                ocr.recognize(self.image, self.cache)
            with self.assertRaisesRegex(RuntimeError, '已有失败记录'):
                ocr.recognize(self.image, self.cache)
        self.assertEqual(call.call_count, 1)
        self.assertNotIn('test-secret', next(self.cache.glob('*.json')).read_text())

    def test_再次运行只计当前请求费用并跳过图纸(self):
        pages = [{'source_file': '合成.pdf', 'page': 1, 'candidate_type': '工序记录', 'image': str(self.image), 'text': '既有印刷工序名','coarse_rows':[{'text':'日期','box':[1,2,3,4]}]},
                 {'source_file': '合成.pdf', 'page': 2, 'candidate_type': '图纸规范', 'image': '不应访问.png'}]
        with patch.object(ocr, 'api_key', return_value='test-secret'), patch.object(ocr.urllib.request, 'urlopen', side_effect=lambda *a, **k: self.response()) as call:
            first = ocr.run(pages, self.root/'首次', self.cache)
            second = ocr.run(pages, self.root/'接续', self.cache)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(first['current_request_usage']['total_tokens'], 15)
        self.assertEqual(second['current_request_usage']['total_tokens'], 0)
        self.assertEqual(second['cache_hits'], 1)
        self.assertEqual(len(second['pages']), 1)
        self.assertNotIn('coarse_text',second['pages'][0])
        self.assertNotIn('coarse_rows',second['pages'][0])
        self.assertEqual(second['pages'][0]['layout'],[{'label':'table','bbox_2d':[1,2,30,40]}])

    def test_Windows密钥保护往返(self):
        plain = b'test-secret-no-production-key'
        protected = ocr.protect(plain)
        self.assertNotIn(plain, protected)
        self.assertEqual(ocr.protect(protected, decrypt=True), plain)


    def test_同一入口完成准备及缓存复用无交接(self):
        from PIL import Image
        image=self.root/'原页.png';Image.new('RGB',(600,400),'white').save(image)
        rows=[[[[150,30],[220,30],[220,50],[150,50]],'加工时间',.9],
              [[[160,80],[200,80],[200,100],[160,100]],'415',.9]]
        index={'pages':[{'source_file':'合成.pdf','page':1,'candidate_type':'工序记录',
                        'image':str(image),'page_cache_key':'合成','ocr':rows}]}
        with patch.object(ocr,'api_key',return_value='test-secret'),patch.object(ocr.urllib.request,'urlopen',side_effect=lambda *a,**k:self.response()) as call:
            first=ocr.run_index(index,self.root/'准备识别',self.cache)
            second=ocr.run_index(index,self.root/'接续识别',self.cache)
        self.assertEqual(call.call_count,1)
        self.assertEqual(second['cache_hits'],1)
        self.assertNotIn('coarse_rows',second['pages'][0])
        self.assertIn('preparation_seconds',first)


if __name__ == '__main__':
    unittest.main()
