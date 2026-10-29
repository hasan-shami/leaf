from __future__ import annotations

import argparse
import concurrent.futures
import io
import json
import mimetypes
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

import pypdfium2 as pdfium
from .engine import atomic_json, render_page, block_html

ROOT=Path(__file__).resolve().parent.parent
DATA=ROOT/'data'
LIBRARY=DATA/'library.json'
LOCK=threading.RLock()
PDF_LOCK=threading.Lock()
WORKERS={}
JOBS={}
QUEUE=concurrent.futures.ThreadPoolExecutor(max_workers=2)
PAGE_CACHE={}


def read_library():
    if not LIBRARY.exists(): return []
    return json.loads(LIBRARY.read_text(encoding='utf-8'))


def change_book(book_id,updates):
    with LOCK:
        books=read_library()
        book=next(b for b in books if b['id']==book_id)
        book.update(updates)
        atomic_json(LIBRARY,books)
        return book


def get_book(book_id):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}',book_id): raise ValueError('Invalid book')
    return next((b for b in read_library() if b['id']==book_id),None)


def job_path(book):
    return DATA/'books'/book['id']/book.get('run','pending')


def public_book(book):
    result={k:v for k,v in book.items() if k not in ('source',)}
    result['source_name']=Path(book['source']).name
    result['available']=Path(book['source']).is_file()
    result['status']={'status':'new'}
    if book.get('run'):
        root=job_path(book)
        try: result['status']=json.loads((root/'status.json').read_text(encoding='utf-8'))
        except (OSError,ValueError): result['status']={'status':'new'}
        worker=WORKERS.get(book['id'])
        if result['status'].get('status')=='running' and worker and worker.poll() is not None:
            result['status']={'status':'interrupted','error':'The previous conversion stopped. Convert again to resume.'}
        if (root/'report.json').exists():
            report=json.loads((root/'report.json').read_text(encoding='utf-8'))
            result['report']={k:v for k,v in report.items() if k not in ('source','pages')}
    return result


def start_conversion(book,settings):
    with LOCK:
        worker=WORKERS.get(book['id'])
        if (worker and worker.poll() is None) or (book['id'] in JOBS and not JOBS[book['id']].done()): raise ValueError('This book is already converting.')
        options=dict(book.get('options',{}))
        options.update(settings)
        options.pop('pages',None)
        if options.get('spread','auto') not in ('auto','single','split'): raise ValueError('Invalid spread option')
        options['padding']=min(36,max(0,float(options.get('padding',8))))
        run='run-'+uuid.uuid4().hex[:12]
        root=DATA/'books'/book['id']/run
        root.mkdir(parents=True)
        atomic_json(root/'options.json',options)
        atomic_json(root/'status.json',{'status':'queued','stage':'Waiting for a conversion slot','current':0,'total':1})
        book=change_book(book['id'],{'run':run,'options':options,'reviews':{}})
        args=[sys.executable,'-X','utf8','-m','leaf.cli',book['source'],'--output',str(root),'--title',book['title'],'--author',book.get('author',''),'--options',str(root/'options.json'),'--status',str(root/'status.json')]
        def run():
            try:
                kwargs={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
                with open(root/'conversion.log','w',encoding='utf-8') as log:
                    worker=subprocess.Popen(args,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,**kwargs)
                    WORKERS[book['id']]=worker
                    result=worker.wait()
                if result and json.loads((root/'status.json').read_text(encoding='utf-8')).get('status')!='failed':
                    atomic_json(root/'status.json',{'status':'failed','error':'Conversion stopped. See conversion.log for details.'})
            except Exception as e:
                atomic_json(root/'status.json',{'status':'failed','error':str(e)})
        JOBS[book['id']]=QUEUE.submit(run)
        return public_book(book)


def load_pages(book):
    key=(book['id'],book.get('run'))
    if key not in PAGE_CACHE:
        PAGE_CACHE[key]=json.loads((job_path(book)/'pages.json').read_text(encoding='utf-8'))
        if len(PAGE_CACHE)>12: PAGE_CACHE.pop(next(iter(PAGE_CACHE)))
    return PAGE_CACHE[key]


class Handler(BaseHTTPRequestHandler):
    server_version='Leaf/0.1'

    def log_message(self,format,*args):
        if args and str(args[1] if len(args)>1 else '').startswith('5'): super().log_message(format,*args)

    def allowed(self,write=False):
        host=self.headers.get('Host','')
        if host not in (f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'): return False
        origin=self.headers.get('Origin')
        if origin and origin not in (f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}'): return False
        return not write or self.headers.get('X-Leaf-Local')=='1'

    def send(self,data,status=200,kind='application/json',extra=None):
        if isinstance(data,(dict,list)): data=json.dumps(data,ensure_ascii=False).encode('utf-8')
        elif isinstance(data,str): data=data.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type',kind+('; charset=utf-8' if kind.startswith('text/') or kind=='application/json' else ''))
        self.send_header('Content-Length',str(len(data)))
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Security-Policy',"default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; object-src 'none'; frame-ancestors 'none'")
        for k,v in (extra or {}).items(): self.send_header(k,v)
        self.end_headers()
        try: self.wfile.write(data)
        except (BrokenPipeError,ConnectionResetError): pass

    def json_body(self):
        length=int(self.headers.get('Content-Length','0'))
        if length>2_000_000: raise ValueError('Settings request is too large')
        return json.loads(self.rfile.read(length) or b'{}')

    def do_GET(self):
        if not self.allowed(): return self.send({'error':'Local access only'},403)
        try: self.get_route()
        except (ValueError,KeyError,StopIteration,IndexError) as e: self.send({'error':str(e)},400)
        except FileNotFoundError: self.send({'error':'This result is not available yet.'},404)
        except Exception as e:
            traceback.print_exc(); self.send({'error':str(e)},500)

    def get_route(self):
        url=urlparse(self.path); path=unquote(url.path); query=parse_qs(url.query)
        if path in ('/','/app.js','/style.css'):
            file=ROOT/'web'/('index.html' if path=='/' else path[1:])
            return self.send(file.read_bytes(),kind=mimetypes.guess_type(str(file))[0] or 'text/plain')
        if path=='/api/books': return self.send([public_book(b) for b in read_library()])
        if path=='/api/health': return self.send({'ok':True,'device':'Paperwhite 11','local':True})
        parts=path.strip('/').split('/')
        if len(parts)<3 or parts[:2]!=['api','books']: return self.send({'error':'Not found'},404)
        book=get_book(parts[2])
        if not book: return self.send({'error':'Book not found'},404)
        if len(parts)==3: return self.send(public_book(book))
        action=parts[3]
        if action=='pages':
            pages=load_pages(book)
            return self.send([{k:p[k] for k in ('id','source_page','part','flags','crop','region','text_fidelity')} for p in pages])
        if action=='page':
            page_id=query.get('id',[''])[0]
            p=next(x for x in load_pages(book) if x['id']==page_id)
            result=dict(p)
            result['html']=''.join(block_html(b) for b in p['blocks'])
            result['html']=re.sub(r'src="(assets/[^\"]+)"',lambda m:'src="/api/books/'+book['id']+'/asset/'+m[1]+'"',result['html'])
            result['html']=re.sub(r'href="original-[^\"]+"','href="#" data-original="true"',result['html'])
            return self.send(result)
        if action=='image':
            page_num=int(query.get('page',['1'])[0]); crop_id=query.get('crop',[''])[0]
            cache=job_path(book)/'previews'; cache.mkdir(exist_ok=True)
            key=f'{page_num}-{crop_id or "source"}.jpg'
            if not re.fullmatch(r'[a-z0-9-]+\.jpg',key): raise ValueError('Invalid page')
            file=cache/key
            if not file.exists():
                with PDF_LOCK:
                    doc=pdfium.PdfDocument(book['source'])
                    try:
                        pg=doc[page_num-1]; scale=1.6
                        im=render_page(pg,scale)
                        if crop_id:
                            p=next(x for x in load_pages(book) if x['id']==crop_id)
                            im=im.crop(tuple(round(v*scale) for v in p['crop']))
                        im.save(file,'JPEG',quality=90)
                        pg.close()
                    finally: doc.close()
            return self.send(file.read_bytes(),kind='image/jpeg')
        if action=='asset':
            relative='/'.join(parts[4:]); root=job_path(book).resolve(); target=(root/relative).resolve()
            if not target.is_relative_to(root/'assets') or target.suffix.lower() not in ('.jpg','.png'): raise ValueError('Invalid asset')
            return self.send(target.read_bytes(),kind=mimetypes.guess_type(str(target))[0] or 'application/octet-stream')
        if action=='download':
            kind=query.get('kind',['epub'])[0]
            name={'epub':'book.epub','pdf':'cropped.pdf','report':'report.html','json':'report.json'}.get(kind)
            if not name: raise ValueError('Unknown download')
            file=job_path(book)/name
            suffix=file.suffix
            title=re.sub(r'[^\w .-]','',book['title'],flags=re.ASCII).strip() or 'book'
            return self.send(file.read_bytes(),kind=mimetypes.guess_type(name)[0] or 'application/octet-stream',extra={'Content-Disposition':'attachment; filename="'+title+('-cropped' if kind=='pdf' else '-report' if kind in ('report','json') else '')+suffix+'"'})
        return self.send({'error':'Not found'},404)

    def do_POST(self):
        if not self.allowed(write=True): return self.send({'error':'Local request required'},403)
        try: self.post_route()
        except (ValueError,KeyError,StopIteration) as e: self.send({'error':str(e)},400)
        except Exception as e:
            traceback.print_exc(); self.send({'error':str(e)},500)

    def post_route(self):
        path=urlparse(self.path).path
        if path=='/api/import':
            size=int(self.headers.get('Content-Length','0'))
            if size<5 or size>300*1024*1024: raise ValueError('Choose a PDF smaller than 300 MB')
            name=Path(unquote(self.headers.get('X-Filename','book.pdf'))).name
            if not name.lower().endswith('.pdf'): raise ValueError('Choose a PDF file')
            content=self.rfile.read(size)
            if b'%PDF-' not in content[:1024]: raise ValueError('The selected file is not a PDF')
            book_id=uuid.uuid4().hex[:12]
            source=DATA/'sources'/(book_id+'.pdf'); source.parent.mkdir(parents=True,exist_ok=True)
            source.write_bytes(content)
            with PDF_LOCK:
                doc=pdfium.PdfDocument(source)
                try: count=len(doc)
                finally: doc.close()
            book={'id':book_id,'title':Path(name).stem,'author':'','source':str(source),'page_count':count,'options':{}}
            with LOCK:
                books=read_library(); books.append(book); atomic_json(LIBRARY,books)
            return self.send(public_book(book),201)
        parts=path.strip('/').split('/')
        if len(parts)!=4 or parts[:2]!=['api','books']: return self.send({'error':'Not found'},404)
        book=get_book(parts[2])
        if not book: raise ValueError('Book not found')
        body=self.json_body()
        if parts[3]=='convert': return self.send(start_conversion(book,body))
        if parts[3]=='settings':
            options=dict(book.get('options',{})); options.update(body.get('options',{}))
            book=change_book(book['id'],{'title':str(body.get('title',book['title']))[:250], 'author':str(body.get('author',book.get('author','')))[:250], 'options':options})
            return self.send(public_book(book))
        if parts[3]=='review':
            reviews=dict(book.get('reviews',{})); reviews[str(body['page'])]=bool(body.get('reviewed',True))
            book=change_book(book['id'],{'reviews':reviews})
            return self.send(public_book(book))
        return self.send({'error':'Not found'},404)


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--port',type=int,default=8765); parser.add_argument('--open',action='store_true')
    args=parser.parse_args(); DATA.mkdir(exist_ok=True)
    server=None
    for port in range(args.port,args.port+20):
        try: server=ThreadingHTTPServer(('127.0.0.1',port),Handler); break
        except OSError: continue
    if server is None: raise RuntimeError('No free local port')
    url=f'http://127.0.0.1:{server.server_port}'
    atomic_json(DATA/'server.json',{'url':url,'pid':os.getpid()})
    print('Leaf is running at '+url,flush=True)
    if args.open: webbrowser.open(url)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__=='__main__': main()
