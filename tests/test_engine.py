import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from reportlab.pdfgen import canvas
from pypdf import PdfReader
import pypdfium2 as pdfium
from leaf.engine import convert, paragraph_blocks, validate_epub, canonical, clean_text, visible_box, extract_glyphs, region_lines, repair_known_hyphens, scan_region_lines, detect_regions, object_boxes

TEST_ROOT=Path(__file__).resolve().parents[3]/'work'/'test-fixtures'
TEST_ROOT.mkdir(parents=True,exist_ok=True)


def line(text,y,x=50,right=350):
    return {'text':text,'html':text,'b':[x,y,right,y+10],'size':10,'bold':False,'italic':False}


class ReadingCopyTests(unittest.TestCase):
    def test_discretionary_hyphen_and_compound(self):
        result=paragraph_blocks([line('They reassure them\u00ad',50),line('selves during face-to-',63),line('face encounters.',76)], [0,0,400,600])
        self.assertEqual(result[0]['text'],'They reassure themselves during face-to-face encounters.')

    def test_unicode_and_ligatures(self):
        self.assertEqual(clean_text('ijtihād ﬁeld'), 'ijtihād field')
        self.assertEqual(canonical('the\u00adory'),canonical('theory'))

    def test_numbered_list_continuation_is_one_paragraph(self):
        blocks=paragraph_blocks([line('2. A list item reaches its log\u00ad',50),line('ical conclusion on another line.',63)], [0,0,400,600])
        self.assertEqual(len(blocks),1)
        self.assertIn('logical conclusion',blocks[0]['text'])

    def test_book_vocabulary_repairs_splits_and_preserves_compounds(self):
        pages=[{'lines':[line('The tradi-',50),line('tions continue.',63),line('These traditions endure in face-to-face meetings.',90),line('More face-to\u00ad',120),line('face meetings follow.',133)]}]
        repair_known_hyphens(pages)
        text=' '.join(b['text'] for b in paragraph_blocks(pages[0]['lines'],[0,0,400,600]))
        self.assertIn('traditions continue',text)
        self.assertIn('face-to-face meetings follow',text)

    def test_halm_diagram_is_not_a_spread_and_diacritics_survive(self):
        source=next((Path.home()/'Downloads').glob('Heinz Halm - Shi*ism (2022*.pdf'),None)
        if source is None:self.skipTest('User sample not present')
        import pdfplumber
        doc=pdfium.PdfDocument(source)
        diagram=doc[27];glyphs,_=extract_glyphs(diagram);w,h=diagram.get_size()
        lines=region_lines(glyphs,[0,0,w,h]);images,_=object_boxes(diagram)
        self.assertEqual(len(detect_regions(w,h,images,lines,'auto')),1)
        page=doc[73];glyphs,_=extract_glyphs(page)
        with pdfplumber.open(source) as pl:
            lines=scan_region_lines(pl.pages[73],page,[0,0,*page.get_size()],glyphs)
        self.assertIn('singly (āḥād)', ' '.join(l['text'] for l in lines))
        diagram.close();page.close();doc.close()

    def test_pdf_roundtrip_crop_and_links(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as td:
            root=Path(td); src=root/'fixture.pdf'
            c=canvas.Canvas(str(src),pagesize=(500,700))
            for i in range(4):
                c.setFont('Times-Roman',9); c.drawString(90,660,'Repeated book header '+str(i+1))
                c.setFont('Times-Roman',12)
                c.drawString(90,600,f'Unique page {i+1} starts here with ordinary readable words.')
                c.drawString(90,585,'This second line continues the paragraph without lost text.')
                c.drawString(90,100,'A footnote near the lower edge must remain visible.')
                c.showPage()
            c.save()
            r=convert(src,root/'out','Fixture','Test Author')
            self.assertTrue(r['epub_validation']['passed'])
            self.assertTrue(r['text_coverage_passed'])
            pages=json.loads((root/'out/pages.json').read_text())
            for p in pages:
                self.assertTrue(p['removed_furniture'])
                self.assertNotIn('Repeated book header',' '.join(b.get('text','') for b in p['blocks']))
                self.assertIn('footnote',' '.join(b.get('text','') for b in p['blocks']))
            cropped=PdfReader(root/'out/cropped.pdf')
            self.assertEqual(len(cropped.pages),4)
            self.assertLess(float(cropped.pages[0].cropbox.width),500)
            self.assertLessEqual(float(cropped.pages[0].cropbox.bottom),100)
            with zipfile.ZipFile(root/'out/book.epub') as z:
                content=''.join(z.read(n).decode() for n in z.namelist() if n.startswith('OEBPS/chapter-'))
                for i in range(1,5): self.assertEqual(content.count(f'Unique page {i}'),1)

    def test_spread_order_and_independent_crops(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as td:
            root=Path(td); src=root/'spread.pdf'
            c=canvas.Canvas(str(src),pagesize=(800,600));c.setFont('Times-Roman',12)
            c.drawString(50,530,'LEFT FIRST paragraph from the left book page.')
            c.drawString(50,510,'LEFT SECOND continues the left page.')
            c.drawString(450,530,'RIGHT FIRST paragraph from the right book page.')
            c.drawString(450,510,'RIGHT SECOND continues the right page.')
            c.save()
            r=convert(src,root/'out','Spread','Test',{'spread':'split'})
            self.assertEqual(r['logical_pages'],2)
            pages=json.loads((root/'out/pages.json').read_text())
            self.assertIn('LEFT FIRST',pages[0]['original_text'])
            self.assertNotIn('RIGHT',pages[0]['original_text'])
            self.assertIn('RIGHT FIRST',pages[1]['original_text'])
            cropped=PdfReader(root/'out/cropped.pdf')
            self.assertLess(float(cropped.pages[0].cropbox.right),float(cropped.pages[1].cropbox.left))
            doc=pdfium.PdfDocument(root/'out/cropped.pdf')
            self.assertLess(doc[0].get_width(),400);self.assertLess(doc[1].get_width(),400)
            doc.close()

    def test_rotated_crop_matches_visual_region(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as td:
            root=Path(td); src=root/'plain.pdf'
            c=canvas.Canvas(str(src),pagesize=(500,700));c.setFont('Times-Roman',12)
            c.drawString(110,500,'Rotated text is preserved in the image.');c.save()
            from pypdf import PdfWriter
            reader=PdfReader(src);writer=PdfWriter();p=writer.add_page(reader.pages[0]);p.rotate(90)
            rotated=root/'rotated.pdf'
            with rotated.open('wb') as f: writer.write(f)
            r=convert(rotated,root/'out','Rotated','Test')
            self.assertTrue(r['epub_validation']['passed'])
            self.assertIn('image-page',r['pages'][0]['flags'])
            out=pdfium.PdfDocument(root/'out/cropped.pdf')
            self.assertLess(out[0].get_width(),700)
            self.assertLess(out[0].get_height(),500)
            out.close()

    def test_collins_actual_font_matrix_and_word_joins(self):
        source=Path.home()/'Downloads'/'Randall Collins - Violence_ A Micro-sociological Theory (2008, Princeton University Press) - libgen.lc.pdf'
        if not source.exists(): self.skipTest('User sample not present')
        doc=pdfium.PdfDocument(source);page=doc[191]
        glyphs,_=extract_glyphs(page)
        lines=region_lines(glyphs,[0,0,*page.get_size()])
        blocks=paragraph_blocks(lines,[0,0,*page.get_size()])
        text=' '.join(b['text'] for b in blocks)
        self.assertIn('muggers reassure themselves',text)
        self.assertIn('higher degree of confrontation',text)
        self.assertLess(len(blocks),15)
        page.close();doc.close()


if __name__=='__main__': unittest.main()
