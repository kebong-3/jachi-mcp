"""Explicit operator-triggered official API connectivity test. No key values printed."""
import asyncio
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from jachi.client import LawClient
from jachi.config import Settings
from jachi.models import DocumentRef
from jachi.agents import unique_exact

async def main():
    async with LawClient(Settings.from_env(),budget=6) as c:
        result=await c.search('지방자치법',kind='law',max_pages=1)
        exact=unique_exact(result['results'],'지방자치법')
        doc=None
        if result['status']=='complete' and len(exact)==1:
            ref=DocumentRef(kind='law',document_id=exact[0]['document_id'],title_hint='지방자치법')
            doc=(await c.get_document(ref)).summary()
        print(json.dumps({'search_status':result['status'],'exact_matches':len(exact),'document':doc,
                          'calls':c.calls,'failures':result['failures']},ensure_ascii=False,indent=2))
        if doc is None:raise SystemExit(2)

if __name__=='__main__':asyncio.run(main())
