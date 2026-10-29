from __future__ import annotations

import collections
import copy
import ctypes
import datetime as dt
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import tempfile
import time
import unicodedata
import uuid
import zipfile
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image
import pypdfium2 as pdfium
import pypdfium2.raw as raw
import pdfplumber
from pypdf import PdfReader, PdfWriter
from pypdf.generic import RectangleObject

ENGINE_VERSION = "0.2.0"
CSS = """body { margin: 0 2%; line-height: 1.45; font-family: serif; }
p { margin: 0 0 .65em; text-align: left; orphans: 2; widows: 2; }
h1,h2,h3 { text-align: left; line-height: 1.2; margin: 1.4em 0 .7em; break-after: avoid; }
h1 { font-size: 1.65em; } h2 { font-size: 1.3em; } h3 { font-size: 1.12em; }
blockquote { margin: .8em 1em; } figure { margin: 1em 0; text-align: center; }
img { max-width: 100%; height: auto; } figcaption { font-size: .85em; margin-top: .4em; }
.note { font-size: .85em; } .page-anchor { font-size: 0; } .source-link { font-size: .75em; }
.source-reference { font-size: .85em; margin-top: 2em; } sup,sub { font-size: .7em; line-height: 0; }
.cover { text-align: center; } a { color: inherit; } .list-item { margin-left: 1em; }
"""


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.'+uuid.uuid4().hex[:8]+".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(12):
        try:
            os.replace(temp, path)
            break
        except PermissionError:
            if attempt==11: raise
            time.sleep(.03*(attempt+1))


def clean_text(s):
    s = unicodedata.normalize("NFC", s).replace("\ufffe", "\u00ad")
    s = s.replace("\ufb01", "fi").replace("\ufb02", "fl").replace("\ufb00", "ff")
    s = s.replace("\ufb03", "ffi").replace("\ufb04", "ffl")
    s = "".join(c for c in s if c in "\t\n\u00ad" or (ord(c) >= 32 and not 0xD800 <= ord(c) <= 0xDFFF and ord(c) not in (0xFFFE, 0xFFFF)))
    return re.sub(r"[ \t\u00a0]+", " ", s).strip()


def canonical(s):
    return "".join(c.casefold() for c in unicodedata.normalize("NFKC", clean_text(s)) if c.isalnum())


def union(boxes):
    boxes = [b for b in boxes if b and b[2] > b[0] and b[3] > b[1]]
    if not boxes:
        return None
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def inside_center(box, region):
    x, y = (box[0]+box[2])/2, (box[1]+box[3])/2
    return region[0] <= x < region[2] and region[1] <= y < region[3]


def intersection(a, b):
    r = [max(a[0],b[0]),max(a[1],b[1]),min(a[2],b[2]),min(a[3],b[3])]
    return r if r[2] > r[0] and r[3] > r[1] else None


def render_page(page, scale=1.0):
    bitmap = page.render(scale=scale)
    try:
        return bitmap.to_pil().convert("RGB").copy()
    finally:
        bitmap.close()


def visual_bounds(im, region, scale, scan=False):
    """Ink bounds in visible-page coordinates. Scan borders are ignored, content is padded later."""
    x0,y0,x1,y1 = [int(v*scale) for v in region]
    a = np.asarray(im.convert("L"))[y0:y1,x0:x1]
    if not a.size:
        return list(region)
    mask = a < 190
    if scan:
        # Page-edge shadows must not make a scan look like edge-to-edge content.
        by, bx = max(1,int(a.shape[0]*.018)), max(1,int(a.shape[1]*.018))
        mask[:by,:] = False; mask[-by:,:] = False
        mask[:,:bx] = False; mask[:,-bx:] = False
        dense_rows = mask.mean(axis=1) > .65
        dense_cols = mask.mean(axis=0) > .65
        mask[dense_rows,:] = False; mask[:,dense_cols] = False
    ys,xs = np.nonzero(mask)
    if not len(xs):
        return list(region)
    return [(x0+int(xs.min()))/scale,(y0+int(ys.min()))/scale,(x0+int(xs.max())+1)/scale,(y0+int(ys.max())+1)/scale]


def font_style(tp, i, cache):
    buf = ctypes.create_string_buffer(160)
    flags = ctypes.c_int()
    n = raw.FPDFText_GetFontInfo(tp, i, buf, len(buf), ctypes.byref(flags))
    name = buf.value.decode("utf-8", "replace") if n else ""
    if name not in cache:
        cache[name] = (bool(flags.value & 64) or bool(re.search("Italic|Oblique",name,re.I)), bool(re.search("Bold|Demi|Black",name,re.I)))
    return cache[name]


def visible_box(box, page):
    l,b,r,t=box; ox,oy,ex,ey=page.get_bbox()
    rotation=page.get_rotation()
    if rotation==90: return [b-oy,l-ox,t-oy,r-ox]
    if rotation==180: return [ex-r,b-oy,ex-l,t-oy]
    if rotation==270: return [ey-t,ex-r,ey-b,ex-l]
    return [l-ox,ey-t,r-ox,ey-b]


def extract_glyphs(page):
    """Use PDFium's generated spaces; retain geometry and font cues for reconstruction."""
    tp = page.get_textpage()
    origin = page.get_bbox()
    glyphs = []
    font_cache = {}
    try:
        for i in range(tp.count_chars()):
            u = raw.FPDFText_GetUnicode(tp, i)
            if not u or u > 0x10ffff:
                continue
            c = chr(u)
            # PDFium exposes discretionary line-end hyphens as U+0002 internally.
            # Its text-range API instead maps these to U+FFFE. Preserve the join cue.
            if u==2 and raw.FPDFText_IsHyphen(tp,i)>0: c='\u00ad'
            if c in "\r\n":
                glyphs.append({"c":c, "br":True})
                continue
            l,b,r,t = tp.get_charbox(i)
            box = visible_box((l,b,r,t),page)
            size = raw.FPDFText_GetFontSize(tp,i) or max(1,t-b)
            matrix=raw.FS_MATRIX()
            if raw.FPDFText_GetMatrix(tp,i,ctypes.byref(matrix)):
                size*=math.hypot(matrix.c,matrix.d) or 1
            italic,bold = font_style(tp,i,font_cache) if not c.isspace() else (False,False)
            glyphs.append({"c":c,"b":box,"s":float(size),"i":italic,"bold":bold})
        original = clean_text(tp.get_text_range())
    finally:
        tp.close()
    return glyphs, original


def line_from_glyphs(glyphs):
    while glyphs and glyphs[0]['c'].isspace(): glyphs = glyphs[1:]
    while glyphs and glyphs[-1]['c'].isspace(): glyphs = glyphs[:-1]
    actual = [g for g in glyphs if not g['c'].isspace() and 'b' in g]
    if not actual: return None
    text = clean_text(''.join(g['c'] for g in glyphs))
    if not text: return None
    size = statistics.median(g['s'] for g in actual)
    bottom = statistics.median(g['b'][3] for g in actual if g['s'] >= size*.9)
    runs = []
    for g in glyphs:
        c = clean_text(g['c']) if not g['c'].isspace() else ' '
        small = g['s'] < size*.83
        sup = small and g['b'][3] < bottom-size*.15
        sub = small and g['b'][3] > bottom+size*.05
        key = (g['i'],g['bold'],sup,sub)
        if runs and runs[-1]['key'] == key: runs[-1]['text'] += c
        else: runs.append({'key':key,'text':c})
    marked = ''
    for run in runs:
        value = html.escape(run['text'])
        italic,bold,sup,sub = run['key']
        if italic: value = '<em>'+value+'</em>'
        if bold: value = '<strong>'+value+'</strong>'
        if sup: value = '<sup>'+value+'</sup>'
        if sub: value = '<sub>'+value+'</sub>'
        marked += value
    return {'text':text,'html':marked,'b':union([g['b'] for g in actual]),'size':size,
            'bold':sum(g['bold'] for g in actual)/len(actual)>.7,
            'italic':sum(g['i'] for g in actual)/len(actual)>.7}


def region_lines(glyphs, region):
    lines, current = [], []
    def flush():
        if current:
            line = line_from_glyphs(current)
            if line: lines.append(line)
            current.clear()
    for g in glyphs:
        if g.get('br'): flush(); continue
        if not inside_center(g['b'],region): continue
        # Some PDFs omit explicit newlines between independently positioned text.
        if current and not g['c'].isspace():
            prev = next((x for x in reversed(current) if not x['c'].isspace()),None)
            if prev and abs(g['b'][3]-prev['b'][3]) > max(g['s'],prev['s'])*.85:
                flush()
        current.append(g)
    flush()
    # Preserve visual order within a physical page, after splitting scanned spreads.
    lines.sort(key=lambda l:(round(l['b'][1]/2)*2,l['b'][0]))
    return lines


def scan_region_lines(plumber_page, page, region, native_glyphs):
    """An independent word extractor avoids extra spaces inserted into older OCR layers."""
    words=plumber_page.extract_words(x_tolerance=2.4,y_tolerance=3,return_chars=True)
    hyphens=[g['b'] for g in native_glyphs if g.get('c')=='\u00ad']
    converted=[]
    for word in words:
        glyphs=[]
        for ch in word['chars']:
            box=visible_box((ch['x0'],plumber_page.height-ch['bottom'],ch['x1'],plumber_page.height-ch['top']),page)
            if not inside_center(box,region): continue
            text=ch['text']
            if text=='-' and any(abs(box[0]-h[0])<2 and abs(box[1]-h[1])<3 for h in hyphens): text='\u00ad'
            font=ch.get('fontname','')
            for c in text:
                glyphs.append({'c':c,'b':box,'s':ch.get('size',10),'i':bool(re.search('Italic|Oblique',font,re.I)),'bold':bool(re.search('Bold|Demi',font,re.I))})
        if glyphs:
            converted.append({'glyphs':glyphs,'b':union([g['b'] for g in glyphs]),'size':statistics.median(g['s'] for g in glyphs)})
    converted.sort(key=lambda w:(w['b'][1],w['b'][0]))
    groups=[]
    for word in converted:
        center=(word['b'][1]+word['b'][3])/2
        matches=[g for g in groups[-3:] if abs(center-g['center'])<max(3,word['size']*.55)]
        if matches:
            group=min(matches,key=lambda g:abs(center-g['center']))
            group['words'].append(word)
            group['center']=statistics.median((w['b'][1]+w['b'][3])/2 for w in group['words'])
        else: groups.append({'center':center,'words':[word]})
    result=[]
    for group in sorted(groups,key=lambda g:g['center']):
        glyphs=[]
        for word in sorted(group['words'],key=lambda w:w['b'][0]):
            if glyphs:
                last=glyphs[-1]
                glyphs.append({'c':' ','b':[last['b'][2],last['b'][1],last['b'][2],last['b'][3]],'s':last['s'],'i':False,'bold':False})
            glyphs.extend(word['glyphs'])
        line=line_from_glyphs(glyphs)
        if line:result.append(line)
    return result


def object_boxes(page):
    origin = page.get_bbox()
    w,h = page.get_size()
    images, paths = [], []
    for ob in page.get_objects(filter=[raw.FPDF_PAGEOBJ_IMAGE,raw.FPDF_PAGEOBJ_PATH]):
        try:
            l,b,r,t=ob.get_bounds()
            box=intersection(visible_box((l,b,r,t),page),[0,0,w,h])
            if not box: continue
            if ob.type == raw.FPDF_PAGEOBJ_IMAGE: images.append(box)
            elif (box[2]-box[0]>10 or box[3]-box[1]>10): paths.append(box)
        except Exception:
            continue
    return images,paths


def detect_regions(w,h,images,lines,mode):
    if mode == 'single': return [[0,0,w,h]]
    spread = mode == 'split'
    if mode == 'auto' and w/h > 1.16:
        left=sum(1 for l in lines if len(l['text'])>50 and l['b'][2]-l['b'][0]>w*.20 and (l['b'][0]+l['b'][2])/2 < w*.48)
        right=sum(1 for l in lines if len(l['text'])>50 and l['b'][2]-l['b'][0]>w*.20 and (l['b'][0]+l['b'][2])/2 > w*.52)
        scan=any((b[2]-b[0])*(b[3]-b[1])>w*h*.35 for b in images)
        spread=scan and left>=8 and right>=8
    if not spread: return [[0,0,w,h]]
    # Scan spreads may have an off-center gutter. Prefer the gap between text columns.
    candidates=[]
    for l in lines:
        if l['b'][2] < w*.57 and l['b'][0] < w*.25: candidates.append(('left',l['b'][2]))
        if l['b'][0] > w*.43 and l['b'][2] > w*.75: candidates.append(('right',l['b'][0]))
    ls=[v for k,v in candidates if k=='left']; rs=[v for k,v in candidates if k=='right']
    gutter=w/2
    if ls and rs:
        le=float(np.percentile(ls,90)); ri=float(np.percentile(rs,10))
        if le<ri and w*.42<(le+ri)/2<w*.58: gutter=(le+ri)/2
    return [[0,0,gutter,h],[gutter,0,w,h]]


def book_spread_profile(doc):
    if len(doc)<6:return False
    samples=sorted(set(int((len(doc)-1)*q) for q in [.1,.2,.3,.4,.5,.6,.7,.8,.9]))
    landscape=[]
    for idx in samples:
        page=doc[idx];w,h=page.get_size();page.close()
        if w/h>1.16:landscape.append(idx)
    if len(landscape)<len(samples)*.7:return False
    paired=0
    for idx in landscape[:5]:
        page=doc[idx];w,h=page.get_size()
        glyphs,_=extract_glyphs(page);images,_=object_boxes(page)
        if len(detect_regions(w,h,images,region_lines(glyphs,[0,0,w,h]),'auto'))==2:paired+=1
        page.close()
    return paired>=min(3,len(landscape))


def furniture_key(s):
    s = re.sub(r'\d+','',s.casefold())
    return re.sub(r'[^\w]+','',s)


def remove_furniture(pages):
    occurrences=collections.defaultdict(set)
    for p in pages:
        r=p['region']; height=r[3]-r[1]
        for l in p['lines']:
            top=l['b'][1]-r[1]; bottom=l['b'][3]-r[1]
            if top<height*.14 or bottom>height*.88:
                key=furniture_key(l['text'])
                if key: occurrences[key].add(p['source_page'])
    repeated={k for k,v in occurrences.items() if len(v)>=3}
    for p in pages:
        kept=[]; removed=[]
        r=p['region']; height=r[3]-r[1]
        lines=p['lines']
        for idx,l in enumerate(lines):
            key=furniture_key(l['text'])
            edge=l['b'][1]-r[1]<height*.14 or l['b'][3]-r[1]>height*.88
            numeric=bool(re.fullmatch(r'[\[\]()\s\divxlcIVXLC.·•–—-]+',l['text']))
            # Only isolated outer lines are candidates; titles are not deleted just for repeating.
            isolated=(idx==0 and len(lines)>1 and lines[1]['b'][1]-l['b'][3]>4) or (idx==len(lines)-1 and idx>0 and l['b'][1]-lines[idx-1]['b'][3]>4)
            if edge and isolated and (numeric or key in repeated) and len(l['text'])<150:
                removed.append(l)
            else: kept.append(l)
        p['lines']=kept
        p['removed_furniture']=[l['text'] for l in removed]


def repair_known_hyphens(pages):
    """Use the book's own vocabulary to distinguish split words from real compounds."""
    word_pattern=r'[^\W\d_]+(?:-[^\W\d_]+)*'
    vocabulary=collections.Counter(w.casefold() for p in pages for l in p['lines'] for w in re.findall(word_pattern,l['text'],re.UNICODE))
    for page_index,p in enumerate(pages):
        repairs=0
        for line_index,line in enumerate(p['lines']):
            if line_index+1<len(p['lines']): nxt=p['lines'][line_index+1]['text']
            elif page_index+1<len(pages) and pages[page_index+1]['lines']: nxt=pages[page_index+1]['lines'][0]['text']
            else: continue
            left=re.search('('+word_pattern+')[\u00ad-]$',line['text'],re.UNICODE)
            right=re.match('('+word_pattern+')',nxt,re.UNICODE)
            if not left or not right or not right[1][:1].islower():continue
            compound=(left[1]+'-'+right[1]).casefold()
            joined=(left[1]+right[1]).casefold()
            old=line['text'][-1]
            new=('-' if compound in vocabulary else '\u00ad') if old=='\u00ad' else ('\u00ad' if joined in vocabulary and compound not in vocabulary else '-')
            if old!=new:
                line['text']=line['text'][:-1]+new
                line['html']=re.sub(re.escape(old)+r'(?=(?:</[^>]+>)*$)',lambda _:new,line['html'])
                repairs+=1
        p['hyphen_repairs']=repairs


def paragraph_blocks(lines, region, body_size=None):
    if not lines: return []
    body_size=body_size or statistics.median(l['size'] for l in lines)
    left=float(np.percentile([l['b'][0] for l in lines],15))
    right=float(np.percentile([l['b'][2] for l in lines],85))
    body_width=max(30,right-left)
    heights=[lines[i+1]['b'][1]-l['b'][1] for i,l in enumerate(lines[:-1]) if 3<lines[i+1]['b'][1]-l['b'][1]<body_size*2.5]
    leading=statistics.median(heights) if heights else body_size*1.25
    blocks=[]; current=None
    def finish():
        nonlocal current
        if current:
            current['b']=union(current.pop('boxes'))
            current['join_next']=current['text'].endswith('\u00ad')
            current['html']=current['html'].replace('\u00ad','')
            current['text']=current['text'].replace('\u00ad','')
            blocks.append(current); current=None
    previous=None
    for l in lines:
        txt=l['text']
        short=len(txt)<100 and l['b'][2]-l['b'][0]<body_width*.92
        heading=(l['size']>body_size*1.16 and len(txt)<180) or (short and (l['bold'] or (txt.isupper() and len(txt)>3)))
        list_item=bool(re.match(r'^(?:[•●▪]|\d{1,2}[.)]|[a-z][.)])\s+',txt))
        kind='heading' if heading else ('list' if list_item else 'paragraph')
        gap=(l['b'][1]-previous['b'][1]) if previous else 999
        indent=l['b'][0]-left
        prev_indent=(previous['b'][0]-left) if previous else 0
        prev_short=previous and previous['b'][2]<right-body_width*.13
        new=(not current or kind!='paragraph' or current['kind']=='heading' or gap>leading*1.5 or
             (current['kind']=='paragraph' and indent>body_size*.65 and prev_indent<body_size*.4) or
             (prev_short and previous['text'][-1:] in '.!?;:»”\"'))
        if new:
            finish()
            current={'kind':kind,'text':txt,'html':l['html'],'boxes':[l['b']], 'size':l['size'],
                     'indent':indent,'note':l['size']<body_size*.84}
        else:
            separator=' '
            if current['text'].endswith('\u00ad'):
                current['text']=current['text'][:-1]
                current['html']=re.sub('\u00ad(?=(?:</[^>]+>)*$)','',current['html'])
                separator=''
            # Explicit hyphens are retained: avoid changing meaningful compound words.
            elif current['text'].endswith('-') and txt[:1].islower(): separator=''
            current['text']+=separator+txt
            current['html']+=separator+l['html']
            current['boxes'].append(l['b'])
        previous=l
    finish()
    return blocks


def save_region_image(page, box, path, scale=2.0):
    im=render_page(page,scale)
    b=[max(0,int(v*scale)) for v in box]
    cropped=im.crop(tuple(b))
    if cropped.width>1800:
        cropped.thumbnail((1800,2600))
    cropped.save(path,'JPEG',quality=88,optimize=True)


def tesseract_lines(page, region, tempdir):
    exe=shutil.which('tesseract')
    if not exe: return None
    image_path=Path(tempdir)/'ocr-input.png'
    scale=3
    im=render_page(page,scale).crop(tuple(int(v*scale) for v in region))
    im.save(image_path)
    res=subprocess.run([exe,str(image_path),'stdout','-l','eng','--psm','3','tsv'],capture_output=True,text=True,encoding='utf-8',timeout=180)
    if res.returncode: raise RuntimeError('Local OCR failed: '+res.stderr[-500:])
    import csv,io
    groups=collections.defaultdict(list)
    confidences=[]
    for row in csv.DictReader(io.StringIO(res.stdout),delimiter='\t'):
        if row.get('text','').strip():
            key=(row['block_num'],row['par_num'],row['line_num'])
            groups[key].append(row)
            confidences.append(float(row['conf']))
    lines=[]
    for rows in groups.values():
        text=' '.join(r['text'] for r in rows)
        boxes=[[region[0]+int(r['left'])/scale,region[1]+int(r['top'])/scale,region[0]+(int(r['left'])+int(r['width']))/scale,region[1]+(int(r['top'])+int(r['height']))/scale] for r in rows]
        b=union(boxes)
        lines.append({'text':text,'html':html.escape(text),'b':b,'size':statistics.median(int(r['height'])/scale for r in rows),'bold':False,'italic':False})
    return lines, statistics.mean(confidences) if confidences else 0


def make_cropped_pdf(source, pages, target, padding=8, manual=None):
    reader=PdfReader(source)
    writer=PdfWriter()
    clipped=[]
    for p in pages:
        src=reader.pages[p['source_page']-1]
        dst=writer.add_page(src)
        # Make page dictionaries independent for two halves of the same source page.
        dst.cropbox=RectangleObject(list(dst.cropbox))
        b=p['crop']
        ox,oy,ex,ey=p['pdf_bbox']
        rotation=p.get('rotation',0)
        if rotation==90: rect=[ox+b[1],oy+b[0],ox+b[3],oy+b[2]]
        elif rotation==180: rect=[ex-b[2],oy+b[1],ex-b[0],oy+b[3]]
        elif rotation==270: rect=[ex-b[3],ey-b[2],ex-b[1],ey-b[0]]
        else: rect=[ox+b[0],ey-b[3],ox+b[2],ey-b[1]]
        dst.cropbox=RectangleObject(rect)
    writer.add_metadata({'/Title':Path(source).stem+' — reading copy','/Producer':'Leaf local converter'})
    with open(target,'wb') as f: writer.write(f)
    return clipped


def validate_epub(path):
    errors=[]
    with zipfile.ZipFile(path) as z:
        names=z.namelist()
        if not names or names[0]!='mimetype' or z.read('mimetype')!=b'application/epub+zip' or z.getinfo('mimetype').compress_type!=zipfile.ZIP_STORED:
            errors.append('Invalid EPUB mimetype entry')
        parsed={}
        for name in names:
            if name.endswith(('.xhtml','.opf','.ncx','.xml')):
                try: parsed[name]=ET.fromstring(z.read(name))
                except ET.ParseError as e: errors.append(f'{name}: {e}')
        ns={'o':'http://www.idpf.org/2007/opf'}
        opf=parsed.get('OEBPS/package.opf')
        if opf is None: errors.append('Missing package document')
        else:
            manifest={i.attrib['id']:i.attrib for i in opf.findall('o:manifest/o:item',ns)}
            for i in manifest.values():
                if 'OEBPS/'+i['href'] not in names: errors.append('Missing manifest asset '+i['href'])
            for ref in opf.findall('o:spine/o:itemref',ns):
                if ref.attrib['idref'] not in manifest: errors.append('Invalid spine reference')
            if not any('nav' in i.get('properties','').split() for i in manifest.values()): errors.append('Missing navigation document')
        import posixpath
        for name,tree in parsed.items():
            if not name.endswith('.xhtml'): continue
            for el in tree.iter():
                ref=el.attrib.get('href') or el.attrib.get('src')
                if not ref or re.match(r'\w+:',ref): continue
                dest,_,anchor=ref.partition('#')
                full=posixpath.normpath(posixpath.join(posixpath.dirname(name),dest)) if dest else name
                if full not in names: errors.append(f'{name}: broken link {ref}')
                elif anchor and full in parsed and not any(e.attrib.get('id')==anchor for e in parsed[full].iter()): errors.append(f'{name}: missing anchor {ref}')
    return {'passed':not errors,'errors':errors,'validator':'Built-in ZIP/XML, manifest, spine and internal-link checks; not EPUBCheck.'}


def xhtml(title, content):
    return '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en" xml:lang="en"><head><title>'+html.escape(title)+'</title><link rel="stylesheet" type="text/css" href="style.css"/></head><body>'+content+'</body></html>'


def block_html(b):
    if b['kind']=='image':
        return '<figure><img src="'+html.escape(b['asset'])+'" alt="'+html.escape(b.get('alt','Original page detail'))+'"/></figure>'
    if b['kind']=='heading': return '<h2>'+b['html']+'</h2>'
    cls='note' if b.get('note') else ('list-item' if b['kind']=='list' else '')
    return '<p'+(' class="'+cls+'"' if cls else '')+'>'+b['html']+'</p>'


def build_epub(pages, outdir, title, author, toc):
    uid='urn:uuid:'+str(uuid.uuid4())
    chapters=[]; current=None
    toc_by_page={t['page']:t['title'] for t in toc if t.get('level',0)<=1}
    # Bookmarks are preferred. Fallback to section headings and bounded chunks.
    for p in pages:
        heading=next((b['text'] for b in p['blocks'] if b['kind']=='heading' and len(b['text'])<130),None)
        new_title=toc_by_page.get(p['source_page']) if p.get('part',0)==0 else None
        if current is None or (new_title and len(current['pages'])>0) or (not toc_by_page and heading and len(current['pages'])>=3) or len(current['pages'])>=24:
            name=new_title or heading or ('Beginning' if not chapters else f'PDF page {p["source_page"]}')
            current={'title':name,'pages':[],'file':f'chapter-{len(chapters)+1:03}.xhtml'}
            chapters.append(current)
        current['pages'].append(p)
        p['chapter']=current['file']
    files={}; page_links=[]
    for ch in chapters:
        body=''
        previous=None
        for p in ch['pages']:
            anchor='<span epub:type="pagebreak" role="doc-pagebreak" id="'+p['id']+'" aria-label="PDF page '+str(p['source_page'])+'"/>'
            page_links.append((p['source_page'],p['part'],ch['file']+'#'+p['id']))
            for j,b in enumerate(p['blocks']):
                fragment=block_html(b)
                # Join paragraphs across PDF boundaries only with strong continuation evidence.
                continuation=j==0 and previous and previous['kind']=='paragraph' and b['kind']=='paragraph' and not b.get('note') and b['text'][:1].islower() and previous['text'][-1:] not in '.!?;:'
                if continuation and body.endswith('</p>'):
                    fragment=re.sub(r'^<p[^>]*>','',fragment)
                    separator='' if previous['text'].endswith('-') or previous.get('join_next') else ' '
                    body=body[:-4]+separator+anchor+fragment
                else:
                    body+=anchor+fragment if j==0 else fragment
                previous=b
            if not p['blocks']: body+=anchor
        files[ch['file']]=xhtml(title+' — '+ch['title'],body)
    nav='<nav epub:type="toc" id="toc"><h1>Contents</h1><ol>'+''.join('<li><a href="'+c['file']+'">'+html.escape(c['title'])+'</a></li>' for c in chapters)+'</ol></nav>'
    nav+='<nav epub:type="page-list" hidden="hidden"><h2>Source PDF pages</h2><ol>'+''.join('<li><a href="'+link+'">'+str(n)+(' left' if part==0 and any(q[0]==n and q[1]==1 for q in page_links) else ' right' if part else '')+'</a></li>' for n,part,link in page_links)+'</ol></nav>'
    files['nav.xhtml']=xhtml('Contents',nav)
    ncx='<?xml version="1.0" encoding="utf-8"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head><meta name="dtb:uid" content="'+uid+'"/></head><docTitle><text>'+html.escape(title)+'</text></docTitle><navMap>'+''.join('<navPoint id="n'+str(i)+'" playOrder="'+str(i)+'"><navLabel><text>'+html.escape(c['title'])+'</text></navLabel><content src="'+c['file']+'"/></navPoint>' for i,c in enumerate(chapters,1))+'</navMap></ncx>'
    assets=sorted({b['asset'] for p in pages for b in p['blocks'] if b['kind']=='image'} | {p['reference_asset'] for p in pages if p.get('reference_asset')})
    references=[]
    for p in pages:
        if p.get('reference_asset'):
            name='original-'+p['id']+'.xhtml'
            references.append(name)
            files[name]=xhtml('Original layout', '<p><a href="'+p['chapter']+'#'+p['id']+'">Return to reading</a></p><figure><img src="'+p['reference_asset']+'" alt="Original PDF page '+str(p['source_page'])+'"/></figure>')
    manifest='<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="css" href="style.css" media-type="text/css"/><item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
    manifest+=''.join('<item id="c'+str(i)+'" href="'+c['file']+'" media-type="application/xhtml+xml"/>' for i,c in enumerate(chapters))
    manifest+=''.join('<item id="a'+str(i)+'" href="'+a+'" media-type="image/jpeg"/>' for i,a in enumerate(assets))
    manifest+=''.join('<item id="r'+str(i)+'" href="'+name+'" media-type="application/xhtml+xml"/>' for i,name in enumerate(references))
    stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    opf='<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="en"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="bookid">'+uid+'</dc:identifier><dc:title>'+html.escape(title)+'</dc:title><dc:creator>'+html.escape(author)+'</dc:creator><dc:language>en</dc:language><meta property="dcterms:modified">'+stamp+'</meta></metadata><manifest>'+manifest+'</manifest><spine toc="ncx">'+''.join('<itemref idref="c'+str(i)+'"/>' for i in range(len(chapters)))+'</spine></package>'
    target=outdir/'book.epub'
    with zipfile.ZipFile(target,'w',compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr('mimetype','application/epub+zip',compress_type=zipfile.ZIP_STORED)
        z.writestr('META-INF/container.xml','<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr('OEBPS/package.opf',opf); z.writestr('OEBPS/style.css',CSS); z.writestr('OEBPS/toc.ncx',ncx)
        for name,content in files.items(): z.writestr('OEBPS/'+name,content)
        for a in assets: z.write(outdir/a,'OEBPS/'+a)
    return chapters,validate_epub(target)


def convert(source, outdir, title='', author='', options=None, progress=None):
    source=Path(source); outdir=Path(outdir); outdir.mkdir(parents=True,exist_ok=True)
    (outdir/'assets').mkdir(exist_ok=True)
    options=options or {}; title=title or source.stem; author=author or 'Unknown author'
    report={'engine':ENGINE_VERSION,'title':title,'author':author,'source':str(source),'options':options,'device':'Kindle Paperwhite 11th generation','delivery':'Email to Kindle','device_verified':False,'visual_review':{'reviewed':[],'status':'Pending'},'limitations':[
        'PDF reconstruction is heuristic. Review flagged pages against the original.',
        'Existing scan text can contain OCR errors, especially names and diacritics.',
        'Footnotes are retained in reading order; automatic note linking is not yet implemented.',
        'Complex layouts may use original-page images and can still require zooming.',
        'Actual Kindle/email compatibility and reading comfort require device testing.'
    ]}
    def update(stage,current,total):
        if progress: progress(stage,current,total)
    doc=pdfium.PdfDocument(source)
    plumber_doc=None
    pages=[]
    try:
        report['source_pages']=len(doc)
        spread_book=options.get('spread','auto')=='auto' and book_spread_profile(doc)
        report['book_spread_profile']=spread_book
        toc=[]
        try:
            for item in doc.get_toc():
                if item.page_index is not None: toc.append({'page':item.page_index+1,'title':item.title,'level':item.level})
        except Exception: pass
        indices=options.get('pages') or list(range(1,len(doc)+1))
        for count,n in enumerate(indices,1):
            update('Reading pages',count,len(indices))
            page=doc[n-1]; w,h=page.get_size()
            glyphs,original=extract_glyphs(page)
            images,paths=object_boxes(page)
            all_lines=region_lines(glyphs,[0,0,w,h])
            scan=any((b[2]-b[0])*(b[3]-b[1])>w*h*.35 for b in images)
            mode=options.get('spread_overrides',{}).get(str(n),options.get('spread','auto'))
            if mode=='auto' and spread_book and scan and w/h>1.16:mode='split'
            regions=detect_regions(w,h,images,all_lines,mode)
            raster=render_page(page,.75)
            for part,region in enumerate(regions):
                lines=region_lines(glyphs,region)
                flags=[]
                if scan: flags.append('scan-text-review')
                if len(regions)>1: flags.append('split-spread')
                if page.get_rotation(): flags.append('rotated-page-review')
                if len(regions)==1 and canonical(original)!=canonical(' '.join(l['text'] for l in lines)):
                    flags.append('extraction-order-review')
                method='PDFium text with geometric reconstruction'
                if scan and not page.get_rotation() and sum(len(l['text']) for l in lines)>30:
                    if plumber_doc is None: plumber_doc=pdfplumber.open(source)
                    candidate=scan_region_lines(plumber_doc.pages[n-1],page,region,glyphs)
                    # Never select an extractor that silently drops a substantial amount of text.
                    old_count=len(canonical(' '.join(l['text'] for l in lines)))
                    new_count=len(canonical(' '.join(l['text'] for l in candidate)))
                    if candidate and .98*old_count<=new_count<=1.02*old_count:
                        lines=candidate;method='pdfplumber scan text layer; PDFium hyphen cues'
                    else:flags.append('scan-extractor-disagreement')
                if sum(len(l['text']) for l in lines)<30 and scan and options.get('ocr',True):
                    ocr=tesseract_lines(page,region,outdir)
                    if ocr:
                        lines,confidence=ocr; method='Local Tesseract OCR'
                        if confidence<85: flags.append('low-ocr-confidence')
                    else: flags.append('ocr-unavailable-image-preserved')
                image_boxes=[intersection(b,region) for b in images if intersection(b,region)]
                content=visual_bounds(raster,region,.75,scan=scan)
                text_box=union([l['b'] for l in lines])
                if text_box: content=union([content,text_box])
                # On dense scanned prose, scan borders otherwise prevent useful horizontal cropping.
                # Keep generous padding around every text line; diagram pages stay on visual bounds.
                scan_margin_method='visual ink bounds'
                if scan and text_box and sum(len(l['text'])>50 for l in lines)>=12:
                    extra=(region[2]-region[0])*.04
                    content[0]=max(region[0],text_box[0]-extra)
                    content[2]=min(region[2],text_box[2]+extra)
                    scan_margin_method='Dense scan text bounds with 4% extra horizontal padding; review figures near the edges.'
                p={'id':f'p{n:04}-{part}','source_page':n,'part':part,'region':region,'page_size':[w,h],
                   'pdf_bbox':list(page.get_bbox()),'rotation':page.get_rotation(),'lines':lines,
                   'scan':scan,'image_boxes':image_boxes,'paths':[b for b in paths if inside_center(b,region)],
                   'content_box':content,'flags':flags,'method':method,'original_text':'\n'.join(l['text'] for l in lines),
                   '_raw_canonical':canonical(original) if len(regions)==1 else None,'crop_method':scan_margin_method}
                pages.append(p)
            if scan and plumber_doc is not None: plumber_doc.pages[n-1].flush_cache()
            page.close()
        if options.get('remove_headers',True): remove_furniture(pages)
        else:
            for p in pages: p['removed_furniture']=[]
        for p in pages:
            raw_sequence=p.pop('_raw_canonical',None)
            if raw_sequence is not None:
                for furniture in p['removed_furniture']:
                    raw_sequence=raw_sequence.replace(canonical(furniture),'',1)
                p['source_sequence_passed']=raw_sequence==canonical(' '.join(l['text'] for l in p['lines']))
                if p['source_sequence_passed'] and 'extraction-order-review' in p['flags']:
                    p['flags'].remove('extraction-order-review')
            else: p['source_sequence_passed']=None
        repair_known_hyphens(pages)
        # Stable horizontal crop per geometry and side; never shrink inside actual ink bounds.
        groups=collections.defaultdict(list)
        for p in pages:
            r=p['region']; width=r[2]-r[0]
            key=(round(width/20),round((r[3]-r[1])/20),p['part'],p['source_page']%2)
            p['_group']=key
            if sum(len(l['text']) for l in p['lines'])>250:
                b=p['content_box']; groups[key].append([(b[0]-r[0])/width,(b[2]-r[0])/width])
        pad=float(options.get('padding',8))
        for p in pages:
            r=p['region']; b=list(p['content_box']); width=r[2]-r[0]
            group=groups[p.pop('_group')]
            if group:
                b[0]=min(b[0],r[0]+float(np.percentile([g[0] for g in group],10))*width)
                b[2]=max(b[2],r[0]+float(np.percentile([g[1] for g in group],90))*width)
            p['crop']=[max(r[0],b[0]-pad),max(r[1],b[1]-pad),min(r[2],b[2]+pad),min(r[3],b[3]+pad)]
            overrides=options.get('crop_overrides',{})
            if p['id'] in overrides:
                # User-entered bounds are percentages of the physical page region.
                vals=overrides[p['id']]
                custom=[r[0]+width*vals[0]/100,r[1]+(r[3]-r[1])*vals[1]/100,r[0]+width*vals[2]/100,r[1]+(r[3]-r[1])*vals[3]/100]
                if custom[2]>custom[0] and custom[3]>custom[1]: p['crop']=custom; p['flags'].append('manual-crop-review')
        all_sizes=[l['size'] for p in pages for l in p['lines'] if len(l['text'])>40]
        body_size=statistics.median(all_sizes) if all_sizes else 10
        for i,p in enumerate(pages,1):
            update('Rebuilding reading order',i,len(pages))
            p['blocks']=paragraph_blocks(p['lines'],p['region'],body_size)
            p['text_before']=len(canonical('\n'.join(l['text'] for l in p['lines'])))
            p['text_after']=len(canonical(' '.join(b['text'] for b in p['blocks'])))
            p['text_fidelity']=canonical(' '.join(l['text'] for l in p['lines']))==canonical(' '.join(b['text'] for b in p['blocks']))
            if not p['text_fidelity']: p['flags'].append('text-coverage-mismatch')
            longwords=re.findall(r'[^\W\d_]{28,}', ' '.join(b['text'] for b in p['blocks']),re.UNICODE)
            if longwords: p['flags'].append('suspicious-word-spacing')
            if any('\ufffd' in l['text'] for l in p['lines']): p['flags'].append('replacement-character')
            # Preserve complex pages faithfully and provide reflowed text as a companion.
            complex_page=(not p['scan'] and (len(p['paths'])>12 or any(re.match(r'^(Table|Figure|Fig\.)\s*\d',l['text'],re.I) for l in p['lines'])))
            scan_diagram=p['scan'] and len(p['lines'])>5 and sum(len(l['text'])>60 for l in p['lines'])<4
            if scan_diagram: complex_page=True
            if complex_page: p['flags'].append('complex-layout-review')
            image_only=p['text_before']<30
            preserve=complex_page or 'extraction-order-review' in p['flags'] or bool(p['rotation']) or (image_only and (p['image_boxes'] or p['paths'])) or p['id'] in options.get('image_pages',[])
            page=doc[p['source_page']-1]
            if preserve:
                asset='assets/'+p['id']+'-original.jpg'
                save_region_image(page,p['crop'],outdir/asset)
                image_block={'kind':'image','asset':asset,'alt':f'Original layout, PDF page {p["source_page"]}','b':p['crop']}
                if image_only or p['rotation'] or scan_diagram: p['blocks']=[image_block]; p['flags'].append('image-page')
                else:
                    p['reference_asset']=asset
                    # Original layout remains available without doubling all text in the reading flow.
            if not p['scan'] and not preserve:
                for j,b in enumerate(p['image_boxes']):
                    if b[2]-b[0]<24 or b[3]-b[1]<24: continue
                    asset=f'assets/{p["id"]}-figure-{j}.jpg'
                    save_region_image(page,b,outdir/asset)
                    p['blocks'].append({'kind':'image','asset':asset,'alt':f'Figure from PDF page {p["source_page"]}','b':b})
                p['blocks'].sort(key=lambda b:b['b'][1])
            if image_only and not p['blocks']: p['flags'].append('blank-or-sparse-page')
            page.close()
        # Insert explicit links to reference layouts at the end of affected pages.
        for p in pages:
            if p.get('reference_asset'):
                asset=p['reference_asset']
                p['blocks'].append({'kind':'reference','text':'','html':'<a href="original-'+p['id']+'.xhtml">View original layout for PDF page '+str(p['source_page'])+'</a>','b':p['crop'],'note':True})
        # Include linked reference images in EPUB assets without disrupting paragraph flow.
        update('Writing EPUB',0,1)
        chapters,validation=build_epub(pages,outdir,title,author,toc)
        # build_epub adds referenced originals through its asset collector.
        report['epub_validation']=validation
        update('Cropping PDF margins',0,1)
        rotations=make_cropped_pdf(source,pages,outdir/'cropped.pdf',pad)
        report['crop_rotation_fallbacks']=rotations
        report['logical_pages']=len(pages)
        report['split_source_pages']=len({p['source_page'] for p in pages if p['part']==1})
        report['flag_counts']=dict(collections.Counter(f for p in pages for f in p['flags']))
        report['text_coverage_passed']=all(p['text_fidelity'] for p in pages)
        report['image_pages']=sum(bool(p['blocks']) and all(b['kind']=='image' for b in p['blocks']) for p in pages)
        report['all_source_pages_accounted_for']=set(p['source_page'] for p in pages)==set(indices)
        report['hyphen_repairs']=sum(p.get('hyphen_repairs',0) for p in pages)
        report['coverage_scope']='Normalized character-sequence comparison between retained extracted lines and reconstructed paragraphs before image fallback. Image pages retain the original visual content rather than reflowable text. This check does not independently prove OCR accuracy.'
        report['chapter_count']=len(chapters)
        report['files']={'epub':{'name':'book.epub','bytes':(outdir/'book.epub').stat().st_size},'pdf':{'name':'cropped.pdf','bytes':(outdir/'cropped.pdf').stat().st_size}}
        report['status']='Review needed' if report['flag_counts'] else 'Ready for device test'
        report['pages']=[{k:v for k,v in p.items() if k not in ('lines','paths','image_boxes','original_text','blocks')} for p in pages]
        atomic_json(outdir/'pages.json',pages)
        atomic_json(outdir/'report.json',report)
        write_report_html(report,outdir)
        update('Complete',1,1)
        return report
    finally:
        doc.close()
        if plumber_doc is not None:plumber_doc.close()


def write_report_html(report,outdir):
    rows=''.join('<tr><td>'+str(p['source_page'])+(' right' if p['part'] else '')+'</td><td>'+html.escape(', '.join(p['flags']) or 'No automatic flags')+'</td><td>'+('Pass' if p['text_fidelity'] else 'Review')+'</td></tr>' for p in report['pages'])
    content='<h1>'+html.escape(report['title'])+'</h1><p>Conversion report · Leaf '+ENGINE_VERSION+'</p><p>'+str(report['source_pages'])+' source pages · '+str(report['logical_pages'])+' reading pages · '+str(report['chapter_count'])+' sections.</p><p><strong>Device testing: pending.</strong> '+html.escape(report['coverage_scope'])+'</p><h2>Known limitations</h2><ul>'+''.join('<li>'+html.escape(x)+'</li>' for x in report['limitations'])+'</ul><h2>Page checks</h2><table><thead><tr><th>PDF page</th><th>Review flags</th><th>Text coverage</th></tr></thead><tbody>'+rows+'</tbody></table>'
    (Path(outdir)/'report.html').write_text('<!doctype html><html lang="en"><meta charset="utf-8"><title>Conversion report</title><style>body{max-width:980px;margin:40px auto;padding:0 24px;font:16px/1.6 system-ui;color:#1e302b}td,th{text-align:left;padding:8px;border-bottom:1px solid #ddd}table{width:100%;border-collapse:collapse}</style>'+content+'</html>',encoding='utf-8')
