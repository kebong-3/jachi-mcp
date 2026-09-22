"""Eight-role staged workflow: A1 -> A2 -> (A3,A4,A5,A6) -> A7 -> A8.
Default roles are deterministic functions; optional Gemini augments each role once.
No claim of eight independent external specialists or legally binding consensus.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any
from .models import ReviewInput, Document, DocumentRef, utcnow, digest
from .config import Settings
from .client import LawClient, UpstreamError
from .normalize import parse_user_text, compact, region_match, extract_references
from .analysis import (evidence_index, review_rules, verify_references, compare_documents,
                       amendment_impact, procedure_checklist, temporal_status)
from .drafting import draft_outline
from .audit import quality_gate, markdown_report
from .llm import GeminiRoles
from . import __version__

ROLES=[
 ('A1','사업·입법기획','사업 목적·대상·수단을 구조화하고 제정/개정/기존 근거 활용의 대안을 설정한다.'),
 ('A2','공식자료·버전관리','공식 식별자·기관·시행일·수집범위와 원문 근거를 확보한다.'),
 ('A3','상위법·권한검토','소관사무·법률유보·위임·권한분장·법령 인용을 확인한다.'),
 ('A4','지자체 비교·차이분석','기능별 조문을 대응시키고 도입 후보와 적용 불가 조건을 구별한다.'),
 ('A5','개정영향·연계정비','구·신 상위법 변경이 조례·부칙·별표·규칙·업무에 미치는 영향을 검토한다.'),
 ('A6','재정·절차·집행가능성','예산·인력·사전협의·평가·입법절차·시스템 정비를 확인한다.'),
 ('A7','입안·개정안작성','선행 검토를 토대로 검토용 조문 골격·제안이유·신구대비 후보를 작성한다.'),
 ('A8','반대검토·근거감사','미확인 출처·시점·반대근거·오류를 모아 자동 확정 없이 사람 검토 대상으로 넘긴다.')]

@dataclass
class Work:
    request:ReviewInput
    docs:list[Document]=field(default_factory=list)
    parents:list[Document]=field(default_factory=list)
    comparisons:list[Document]=field(default_factory=list)
    baseline:Document|None=None
    old:Document|None=None
    new:Document|None=None
    searches:list[dict]=field(default_factory=list)
    selections:list[dict]=field(default_factory=list)
    errors:list[dict]=field(default_factory=list)
    registry:dict=field(default_factory=dict)
    agents:dict[str,dict]=field(default_factory=dict)

    def add(self,doc:Document):
        if len(doc.articles)>800 or sum(len(a.text) for a in doc.articles)>1200000:
            raise ValueError('문서 처리 한도 초과(800개 조문/120만자). 범위를 나누어 검토하세요.')
        if not any(d.identity==doc.identity for d in self.docs): self.docs.append(doc)
        return doc


def unique_exact(rows:list[dict],title:str,jurisdiction:str='') -> list[dict]:
    matched=[r for r in rows if compact(r['title'])==compact(title)
             and (not jurisdiction or region_match(r['jurisdiction'],jurisdiction))]
    return list({r['document_id'] or r['mst']:r for r in matched}.values())

async def gather_sources(w:Work,client:LawClient) -> dict:
    req=w.request
    async def fetch(ref:DocumentRef,role:str):
        try:
            doc=await client.get_document(ref)
            w.add(doc)
            w.selections.append({'role':role,'document_identity':doc.identity,'selection':'explicit_identifier'})
            return doc
        except (UpstreamError,ValueError) as e:
            w.errors.append({'phase':'collection','role':role,'code':getattr(e,'code','parse_or_identity_error'),
                             'message':str(e)[:600]})
            return None
    if req.baseline:
        w.baseline=await fetch(req.baseline,'baseline')
        if w.baseline and not region_match(w.baseline.jurisdiction,req.jurisdiction):
            w.errors.append({'phase':'collection','code':'baseline_region_mismatch',
                             'message':'기준 조례의 기관명이 요청 기관과 정확히 일치하지 않아 기준 선택을 해제했습니다.'})
            w.baseline=None
    for ref in req.comparisons:
        d=await fetch(ref,'comparison')
        if d: w.comparisons.append(d)
    for ref in req.parents:
        d=await fetch(ref,'parent')
        if d: w.parents.append(d)
    if req.old_parent:
        w.old=await fetch(req.old_parent,'old_parent')
        w.new=await fetch(req.new_parent,'new_parent')
        if w.new and not any(d.identity==w.new.identity for d in w.parents):w.parents.append(w.new)
    for item in req.provided_documents:
        try:
            d=w.add(parse_user_text(item))
            if d.kind=='law': w.parents.append(d)
            elif not w.baseline and region_match(d.jurisdiction,req.jurisdiction):
                w.baseline=d
                w.selections.append({'role':'baseline','document_identity':d.identity,
                    'selection':'user_provided_first_same_jurisdiction',
                    'warning':'사용자 제공 순서 기준: 원문 공식성·대상 적합성 미확인'})
            else: w.comparisons.append(d)
        except ValueError as e:
            w.errors.append({'phase':'collection','code':'invalid_user_text','message':str(e)[:600]})
    if req.auto_search:
        terms=list(dict.fromkeys(req.search_terms or ([req.topic] if req.topic else [])))[:5]
        if not terms:
            w.errors.append({'phase':'collection','code':'search_terms_missing',
                'message':'사업 설명만으로 임의 검색어를 만들지 않았습니다. topic 또는 search_terms를 지정하세요.'})
        discovered=[]
        for term in terms:
            result=await client.search(term,max_pages=2)
            w.searches.append(result);discovered.extend(result['results'])
            if result['status']!='complete':
                w.errors.append({'phase':'search','code':'incomplete_coverage','message':f'{term}: {result["status"]}'})
        # No generic first hit baseline. Only a unique exact official title+jurisdiction can be selected.
        expected=req.topic if compact(req.topic).startswith(compact(req.jurisdiction)) else req.jurisdiction+' '+req.topic
        if not w.baseline and req.topic and w.searches and all(s["status"]=="complete" for s in w.searches):
            matches=unique_exact(discovered,expected,req.jurisdiction)
            if len(matches)==1 and matches[0]['document_id']:
                row=matches[0]
                w.baseline=await fetch(DocumentRef(document_id=row['document_id'],title_hint=row['title']),'baseline_exact_title')
        if not req.comparisons:
            # Exploratory, bounded, jurisdiction-diverse sample. Not 'best ordinances' or exhaustive.
            seen={d.jurisdiction for d in w.comparisons};count=0
            for row in discovered:
                if count>=4:break
                org=row['jurisdiction']
                if not org or region_match(org,req.jurisdiction) or org in seen or not row['document_id']: continue
                seen.add(org)
                d=await fetch(DocumentRef(document_id=row['document_id'],title_hint=row['title']),'exploratory_peer_sample')
                if d:
                    count+=1;w.comparisons.append(d)
                    w.selections[-1]['warning']='검색순·기관 다양성에 따른 최대 4곳 표본. 우수사례 선정 아님'
        if w.baseline:
            names=list(dict.fromkeys(r['law_title'] for a in w.baseline.articles for r in extract_references(a.text)))[:4]
            known={compact(d.title) for d in w.parents}
            for name in names:
                if compact(name) in known:continue
                found=await client.search(name,kind='law',max_pages=1)
                w.searches.append(found)
                exact=unique_exact(found['results'],name)
                if found['status']=='complete' and len(exact)==1 and exact[0]['document_id']:
                    d=await fetch(DocumentRef(kind='law',document_id=exact[0]['document_id'],title_hint=name),'referenced_parent_exact')
                    if d:w.parents.append(d);known.add(compact(name))
        if w.new and w.new.document_id.isdigit():
            linked=await client.linked_ordinances(w.new.document_id,max_pages=2)
            w.searches.append(linked)
    w.comparisons=list({d.identity:d for d in w.comparisons if not w.baseline or d.identity!=w.baseline.identity}.values())
    w.parents=list({d.identity:d for d in w.parents}.values())
    w.registry=evidence_index(w.docs)
    return {'documents':[d.summary() for d in w.docs],'baseline':w.baseline.summary() if w.baseline else None,
            'selection_log':w.selections,'searches':w.searches,'errors':w.errors.copy(),
            'evidence_count':len(w.registry),'national_exhaustive':False,
            'version_checks':[{'document_identity':d.identity,**temporal_status(d,req.as_of)} for d in w.docs]}

async def run_review(request:ReviewInput, settings:Settings|None=None, client:LawClient|None=None,
                     model:GeminiRoles|None=None, include_markdown:bool=True) -> dict:
    settings=settings or Settings.from_env()
    if client is None:
        async with LawClient(settings,budget=request.budget_calls) as owned:
            return await run_review(request,settings,owned,model,include_markdown)
    w=Work(request)
    provider=model or GeminiRoles(settings)
    async def role(index:int,fn):
        rid,name,goal=ROLES[index];start=perf_counter()
        try:
            output=fn()
            if hasattr(output,'__await__'):output=await output
            status='completed'
        except (UpstreamError,ValueError,KeyError,TypeError) as e:
            output={'error':'해당 역할 분석 미완료','code':getattr(e,'code','analysis_error')}
            w.errors.append({'phase':rid,'code':output['code'],'message':str(e)[:400]});status='failed'
        llm={'status':'not_requested'}
        if request.reasoning=='gemini':
            # validated consent is mandatory even if this function is called directly.
            if not request.allow_external_llm:
                llm={'status':'blocked','reason':'자료 외부 전송 동의 없음'}
            else:
                context=output
                if index>=6:
                    context={"own_role_output":output,"team_inputs":[
                        {"id":a['id'],"name":a['name'],"status":a['status'],
                         "accepted_but_not_legally_verified_claims":a['model'].get('verified_claims',{}).get('accepted',[]),
                         "dissent":a['model'].get('dissent',[]),"questions":a['model'].get('questions',[])}
                        for a in w.agents.values()]}
                llm=await provider.augment(name,goal,request.project,context,w.registry,request.as_of)
            if llm['status']!='completed':status='partial' if status=='completed' else status
        record={'id':rid,'name':name,'goal':goal,'method':'rules+gemini' if request.reasoning=='gemini' else 'deterministic_rules',
                'status':status,'duration_seconds':round(perf_counter()-start,4),'result':output,'model':llm}
        w.agents[rid]=record
        return output
    await role(0,lambda:{'project':request.project,'jurisdiction':request.jurisdiction,'mode':request.mode,
        'as_of':request.as_of.isoformat(),'intake_questions':['소관사무와 사업 권한은 무엇인가?',
            '사업 대상·지원 방법·예산·성과지표가 정해졌는가?','기존 조례·규칙·지침으로 집행 가능한가?'],
        'collection_plan':{'terms':request.search_terms or [request.topic],
                           'baseline_explicit':bool(request.baseline),'api_budget':min(request.budget_calls,settings.max_calls)}})
    await role(1,lambda:gather_sources(w,client))
    # A2 could fail mid-collection; preserve verified collected material, never report a clean empty review.
    w.registry=evidence_index(w.docs)
    def legal():
        out=review_rules(([w.baseline] if w.baseline else [])+w.comparisons,request.project)
        out['reference_checks']=verify_references(out['explicit_references'],w.parents,request.as_of)
        return out
    def compare():
        return compare_documents(w.baseline,w.comparisons,request.as_of) if w.baseline else {
            'status':'baseline_missing','comparisons':[], 'dimension_matrix':[],'warning':'기준 조례 선택 전 부족사항을 확정하지 않습니다.'}
    def impact():
        return amendment_impact(w.old,w.new,[d for d in w.docs if d.kind in {'ordinance','draft'}],request.as_of) if w.old and w.new else {
            'status':'version_pair_missing','impacts':[],'warning':'구·신 상위법의 식별자·시행일이 필요합니다.'}
    legal_out,comparison,impact_out,feasibility=await asyncio.gather(
        role(2,legal),role(3,compare),role(4,impact),role(5,lambda:procedure_checklist(request.project)))
    def draft_with_team():
        outline=draft_outline(request.project,request.jurisdiction,w.baseline,comparison,request.mode,request.topic)
        outline['constraints_from_team']={
            'authority_and_rights_checks':legal_out.get('findings',[]),
            'citations_to_verify':legal_out.get('reference_checks',[]),
            'amendment_impact':impact_out,
            'procedural_dependencies':feasibility.get('steps',[]),
            'source_errors':w.errors.copy(),
            'draft_block_policy':'미확인 위임·법적 근거·기한은 확정 조문으로 자동 삽입하지 않음'}
        return outline
    draft=await role(6,draft_with_team)
    draft['model_proposed_clauses']=w.agents['A7']['model'].get('proposed_clauses',{'accepted':[],'rejected':[]})
    before_gate=lambda:quality_gate(w.docs,w.errors,request.as_of,w.baseline,w.parents,[a['model'] for a in w.agents.values()])
    await role(7,before_gate)
    # Recompute AFTER A8 so its own rejected claims/failure cannot bypass the final gate.
    final_gate=before_gate()
    report={'schema_version':'2.0','version':__version__,'report_id':'R-'+digest([request.model_dump(mode='json'),utcnow()])[:20],
        'generated_at':utcnow(),'project':request.project,'jurisdiction':request.jurisdiction,
        'as_of':request.as_of.isoformat(),'reasoning':request.reasoning,
        'documents':[d.summary() for d in w.docs],'searches':w.searches,'selection_log':w.selections,
        'legal_review':legal_out,'comparison':comparison,'impact':impact_out,'feasibility':feasibility,
        'draft':draft,'quality_gate':final_gate,'errors':w.errors,
        'agents':[w.agents[r[0]] for r in ROLES],'evidence':w.registry,
        'usage':{'official_api_calls':client.calls,'official_cache_hits':client.cache_hits,'llm_calls':provider.calls},
        'coverage':{'national_exhaustive':False,'baseline_confirmed':w.baseline is not None,
                    'source_documents':len(w.docs),'modeled_indirect_impacts_exhaustive':False},
        'privacy':{'user_documents_persisted_by_server':False,'external_llm_requested':request.reasoning=='gemini'},
        'warning':'역할별 보조 검토이며 법률전문가 8명의 독립 심사·완벽한 검증을 뜻하지 않습니다.'}
    if include_markdown:report['markdown']=markdown_report(report)
    return report
