import argparse
import json
from pathlib import Path
import traceback
from .engine import convert, atomic_json


def main():
    parser=argparse.ArgumentParser(description='Convert a local PDF into a reflowable EPUB and cropped PDF.')
    parser.add_argument('source')
    parser.add_argument('--output',required=True)
    parser.add_argument('--title',default='')
    parser.add_argument('--author',default='')
    parser.add_argument('--options',help='JSON file of conversion settings')
    parser.add_argument('--pages',help='Comma-separated 1-based PDF page numbers for a sample')
    parser.add_argument('--status',help='Write progress to this JSON file')
    args=parser.parse_args()
    options=json.loads(Path(args.options).read_text(encoding='utf-8')) if args.options else {}
    if args.pages: options['pages']=[int(x) for x in args.pages.split(',')]
    def progress(stage,current,total):
        state={'status':'complete' if stage=='Complete' else 'running','stage':stage,'current':current,'total':total}
        if args.status: atomic_json(args.status,state)
        if current in (0,1,total) or current%25==0: print(json.dumps(state),flush=True)
    try:
        report=convert(args.source,args.output,args.title,args.author,options,progress)
        if not report['epub_validation']['passed'] or not report['text_coverage_passed']:
            raise RuntimeError('Output failed validation. Inspect report.json before using it.')
    except Exception as e:
        if args.status: atomic_json(args.status,{'status':'failed','error':str(e)})
        traceback.print_exc()
        raise SystemExit(1)


if __name__=='__main__': main()
