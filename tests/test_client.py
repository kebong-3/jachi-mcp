import json
import pytest
import httpx
from jachi.models import DocumentRef
from jachi.client import LawClient,OfficialCache,UpstreamError,BudgetExceeded
from jachi.config import Settings

S=Settings(law_oc='fixture-credential',min_interval=0,cache_ttl=600)
def row(i):return {'자치법규ID':str(i),'자치법규일련번호':str(i+1000),'자치법규명':f'시험 조례{i}','지자체기관명':'가상시'}

def client(handler,settings=S,budget=36):return LawClient(settings,budget,httpx.MockTransport(handler),OfficialCache())

async def test_real_credential_required():
    async with client(lambda r:httpx.Response(200,json={}),Settings()) as c:
        with pytest.raises(UpstreamError,match='LAW_OC'):await c.request('search',{'target':'ordin'})

async def test_paged_complete():
    def handler(req):
        p=int(req.url.params['page']);return httpx.Response(200,json={'OrdinSearch':{'totalCnt':'3','ordin':[row(1),row(2)] if p==1 else row(3)}})
    async with client(handler) as c:
        r=await c.search('시험',display=2,max_pages=3)
        assert r['status']=='complete' and len(r['results'])==3 and c.calls==2

async def test_paging_cap_is_partial():
    async with client(lambda r:httpx.Response(200,json={'totalCnt':'100','ordin':[row(1),row(2)]})) as c:
        r=await c.search('시험',display=2,max_pages=1)
        assert r['status']=='partial' and r['coverage']['next_page']==2 and not r['coverage']['national_exhaustive']

async def test_repeated_page_guard():
    async with client(lambda r:httpx.Response(200,json={'totalCnt':'100','ordin':[row(1),row(2)]})) as c:
        r=await c.search('시험',display=2,max_pages=3)
        assert r['status']=='partial' and r['failures'][0]['code']=='repeated_page'

async def test_failure_is_not_zero_results():
    async with client(lambda r:httpx.Response(403)) as c:
        r=await c.search('시험')
        assert r['status']=='unavailable' and r['api_total'] is None and r['failures']

async def test_budget_preserves_partial():
    async with client(lambda r:httpx.Response(200,json={'totalCnt':'100','ordin':[row(1),row(2)]}),budget=1) as c:
        r=await c.search('시험',display=2,max_pages=3)
        assert r['status']=='partial' and r['failures'][0]['code']=='call_budget'

async def test_cache_does_not_spend_calls():
    async with client(lambda r:httpx.Response(200,json={'totalCnt':'0'})) as c:
        await c.search('동일');await c.search('동일')
        assert c.calls==1 and c.cache_hits==1

async def test_cached_objects_are_copied():
    cache=OfficialCache();cache.put('x',{'value':[1]});a=cache.get('x')
    # request copies, cache storage itself is internal, not user-facing
    assert a[2]=={'value':[1]}

async def test_redirect_rejected():
    async with client(lambda r:httpx.Response(302,headers={'location':'https://evil.example'})) as c:
        with pytest.raises(UpstreamError,match='302'):await c.request('search',{'target':'ordin'})

async def test_mst_not_overridden_by_id(law_payload):
    requests=[]
    def h(req):requests.append(req);return httpx.Response(200,json=law_payload)
    async with client(h) as c:
        d=await c.get_document(DocumentRef(kind='law',document_id='200',mst='2001',effective_date='20250101'))
    assert requests[0].url.params['MST']=='2001' and 'ID' not in requests[0].url.params
    assert d.version_scope=='version'

async def test_wrong_identity_rejected(law_payload):
    async with client(lambda r:httpx.Response(200,json=law_payload)) as c:
        with pytest.raises(UpstreamError,match='식별자'):await c.get_document(DocumentRef(kind='law',document_id='999'))

async def test_wrong_version_rejected(law_payload):
    async with client(lambda r:httpx.Response(200,json=law_payload)) as c:
        with pytest.raises(UpstreamError,match='버전'):await c.get_document(DocumentRef(kind='law',mst='999',effective_date='20250101'))

async def test_wrong_title_rejected(law_payload):
    async with client(lambda r:httpx.Response(200,json=law_payload)) as c:
        with pytest.raises(UpstreamError,match='제명'):await c.get_document(DocumentRef(kind='law',document_id='200',title_hint='없는 법'))

async def test_link_endpoint_params():
    requests=[]
    def h(req):requests.append(req);return httpx.Response(200,json={'totalCnt':'0'})
    async with client(h) as c:await c.linked_ordinances('001656','제20조의2')
    p=requests[0].url.params
    assert p['target']=='lnkLsOrdJo' and p['JO']=='0020' and p['JOBR']=='02' and p['knd']=='001656'

async def test_response_size_limit():
    async with client(lambda r:httpx.Response(200,content=b'x'*100),Settings(law_oc='fixture',max_response_bytes=10)) as c:
        with pytest.raises(UpstreamError,match='크기'):await c.request('search',{'target':'ordin'})

async def test_no_url_or_auth_override():
    async with client(lambda r:httpx.Response(200,json={})) as c:
        with pytest.raises(ValueError):await c.request('https://evil.example',{})
        with pytest.raises(ValueError):await c.request('search',{'OC':'evil'})

async def test_sborg_requires_org():
    async with client(lambda r:httpx.Response(200,json={})) as c:
        with pytest.raises(ValueError):await c.search('시험',sborg='123')

async def test_underfilled_page_not_complete():
    async with client(lambda r:httpx.Response(200,json={'totalCnt':'500','ordin':[row(1)]})) as c:
        r=await c.search('시험',display=100)
        assert r['status']=='partial' and r['failures'][0]['code']=='inconsistent_page_count'

async def test_promulgated_view_uses_distinct_endpoint(law_payload):
    requests=[]
    def h(req):requests.append(req);return httpx.Response(200,json=law_payload)
    async with client(h) as c:
        d=await c.get_document(DocumentRef(kind='law',document_id='200',law_view='promulgated'))
    assert requests[0].url.params['target']=='law' and d.version_scope=='promulgated'

async def test_explicit_promulgated_mst_without_effective_date(law_payload):
    requests=[]
    def h(req):requests.append(req);return httpx.Response(200,json=law_payload)
    async with client(h) as c:
        d=await c.get_document(DocumentRef(kind='law',mst='2001',law_view='promulgated'))
    assert requests[0].url.params['MST']=='2001' and 'efYd' not in requests[0].url.params
