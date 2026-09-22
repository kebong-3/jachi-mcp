from __future__ import annotations
import argparse
import asyncio
import importlib.metadata
import json
import os
from pathlib import Path
from . import __version__
from .models import ReviewInput,DocumentRef
from .config import Settings
from .client import LawClient
from .agents import run_review
from .monitor import SnapshotStore,run_watch


def atomic_write(path:Path,text:str):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.tmp-'+str(os.getpid()))
    try:
        tmp.write_text(text,encoding='utf-8')
        os.replace(tmp,path)
    finally:
        if tmp.exists():tmp.unlink()


def doctor(settings:Settings)->dict:
    versions={}
    for name in ['httpx','pydantic','defusedxml','mcp','uvicorn']:
        try:versions[name]=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:versions[name]='not_installed'
    try:settings.check_http();http='configuration_valid'
    except ValueError as e:http=str(e)
    return {'version':__version__,'packages':versions,'law_oc_configured':bool(settings.law_oc and settings.law_oc!='test'),
            'gemini_configured':bool(settings.gemini_api_key and settings.gemini_model and settings.allow_llm),
            'http_configuration':http,'live_api_tested':False,'secrets_printed':False}

async def async_main(args):
    settings=Settings.from_env()
    if args.command=='doctor':
        print(json.dumps(doctor(settings),ensure_ascii=False,indent=2));return 0
    if args.command in {'demo','review'}:
        source=Path(__file__).resolve().parents[1]/'examples'/'demo_review.json' if args.command=='demo' else Path(args.input)
        request=ReviewInput.model_validate_json(source.read_text(encoding='utf-8'))
        report=await run_review(request,settings)
        out=Path(args.out)
        markdown=report.pop('markdown')
        atomic_write(out.with_suffix('.json'),json.dumps(report,ensure_ascii=False,indent=2))
        atomic_write(out.with_suffix('.md'),markdown)
        print(json.dumps({'status':'completed_with_human_review_pending','json':str(out.with_suffix('.json')),
                         'markdown':str(out.with_suffix('.md')),'agents':len(report['agents']),
                         'usage':report['usage'],'submission_ready':False},ensure_ascii=False))
        return 0 if not report['errors'] else 2
    if args.command=='watch':
        payload=json.loads(Path(args.config).read_text(encoding='utf-8'))
        refs=[DocumentRef.model_validate(r) for r in payload.get('watch',[])]
        ordinances=[DocumentRef.model_validate(r) for r in payload.get('ordinances',[])]
        async with LawClient(settings) as c:
            with SnapshotStore(args.db) as store:result=await run_watch(refs,c,store,ordinances)
        atomic_write(Path(args.out),json.dumps(result,ensure_ascii=False,indent=2))
        print(json.dumps({'status':'one_shot_finished','out':args.out,'scheduler_installed':False},ensure_ascii=False))
        return 2 if result['errors'] or any(x['status']=='unavailable' for x in result['results']) else 0


def main():
    parser=argparse.ArgumentParser(description='조례 검토 MCP / 오프라인 데모 / 일회 변경 점검')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('doctor')
    demo=sub.add_parser('demo');demo.add_argument('--out',default='output/demo_report')
    review=sub.add_parser('review');review.add_argument('--input',required=True);review.add_argument('--out',default='output/review')
    watch=sub.add_parser('watch');watch.add_argument('--config',required=True);watch.add_argument('--db',default='data/snapshots.sqlite')
    watch.add_argument('--out',default='output/watch_result.json')
    args=parser.parse_args()
    try:raise SystemExit(asyncio.run(async_main(args)))
    except (ValueError,OSError) as e:
        parser.exit(2,'실행 실패: '+str(e)[:600]+'\n')

if __name__=='__main__':main()
