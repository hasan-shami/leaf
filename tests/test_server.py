import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from reportlab.pdfgen import canvas
from leaf import server
from leaf.engine import atomic_json,convert

TEST_ROOT=Path(__file__).resolve().parents[3]/'work'/'test-fixtures'
TEST_ROOT.mkdir(parents=True,exist_ok=True)


class LocalAPITests(unittest.TestCase):
    def handler(self,path,body=None):
        h=object.__new__(server.Handler)
        h.path=path
        h.server=SimpleNamespace(server_port=8765)
        encoded=json.dumps(body or {}).encode()
        h.headers={'Host':'127.0.0.1:8765','X-Leaf-Local':'1','Content-Length':str(len(encoded))}
        h.rfile=io.BytesIO(encoded)
        h.send=lambda data,status=200,kind=None,extra=None:(data,status,kind,extra)
        return h

    def test_cross_origin_writes_are_rejected(self):
        h=self.handler('/api/books')
        self.assertTrue(h.allowed(write=True))
        h.headers['Origin']='https://another-site.example'
        self.assertFalse(h.allowed(write=True))
        del h.headers['Origin'];del h.headers['X-Leaf-Local']
        self.assertFalse(h.allowed(write=True))

    def test_library_preview_settings_and_download(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as td:
            data=Path(td)
            source=data/'source.pdf'
            c=canvas.Canvas(str(source),pagesize=(500,700));c.setFont('Times-Roman',12)
            c.drawString(90,600,'A readable paragraph for checking the local preview API.')
            c.drawString(90,585,'Another line that belongs to the same source page.');c.save()
            result=data/'books'/'sample'/'run-test'
            convert(source,result,'Test book','Test author')
            atomic_json(result/'status.json',{'status':'complete'})
            book={'id':'sample','title':'Test book','author':'Test author','source':str(source),'run':'run-test','options':{}}
            atomic_json(data/'library.json',[book])
            with patch.object(server,'DATA',data),patch.object(server,'LIBRARY',data/'library.json'):
                server.PAGE_CACHE.clear()
                listed=self.handler('/api/books').get_route()[0]
                self.assertNotIn('source',listed[0])
                self.assertTrue(listed[0]['report']['epub_validation']['passed'])
                pages=self.handler('/api/books/sample/pages').get_route()[0]
                details=self.handler('/api/books/sample/page?id='+pages[0]['id']).get_route()[0]
                self.assertIn('readable paragraph',details['html'])
                self.assertTrue(details['text_fidelity'])
                download=self.handler('/api/books/sample/download?kind=epub').get_route()
                self.assertTrue(download[0].startswith(b'PK'))
                saved=self.handler('/api/books/sample/settings',{'title':'Updated title','options':{'padding':12}}).post_route()[0]
                self.assertEqual(saved['title'],'Updated title')
                self.assertEqual(saved['options']['padding'],12)
                self.assertTrue(self.handler('/api/books/sample/image?page=1').get_route()[0].startswith(b'\xff\xd8'))
                with self.assertRaises(ValueError):
                    self.handler('/api/books/sample/asset/assets/../../../library.json').get_route()
                server.PAGE_CACHE.clear()


if __name__=='__main__':unittest.main()
