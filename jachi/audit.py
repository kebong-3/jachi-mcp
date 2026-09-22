from __future__ import annotations
import html
import json
from datetime import date
from .analysis import temporal_status
from .models import Document


def verify_claims(claims:list[dict], registry:dict, as_of:date) -> dict:
    accepted=[];rejected=[]
    for claim in claims:
        errors=[]
        refs=claim.get('references',[])
        if claim.get('category') in {'observation','legal_candidate'} and not refs:
            errors.append('근거 없는 사실 또는 법적 검토 주장')
        provenance=[]
        for ref in refs:
            entry=registry.get(ref.get('evidence_id'))
            if not entry:
                errors.append('미등록 근거 ID');continue
            quote=ref.get('quote','')
            if not quote or quote not in entry['text']:
                errors.append('인용문 원문 불일치')
            provenance.append({'evidence_id':entry['id'],'source_state':entry['source_state'],
                               'effective_date':entry['effective_date']})
        item={**claim,'verification':{'id_and_literal_quote_match':not errors,
                    'semantic_entailment_verified':False,'legal_validity_verified':False,
                    'source_provenance':provenance}}
        if errors: rejected.append({**item,'errors':errors})
        else: accepted.append(item)
    return {'accepted':accepted,'rejected':rejected,
            'warning':'원문에 같은 문장이 있다는 사실은 주장 전체의 타당성이나 법적 적합성을 보증하지 않습니다.'}


def quality_gate(docs:list[Document], errors:list[dict], as_of:date,
                 baseline:Document|None, parents:list[Document], model_outputs:list[dict]) -> dict:
    blockers=[]
    if not baseline: blockers.append('기준 조례 미확정: 검색 후보 중 정식 제명·기관·버전을 선택해야 합니다.')
    if not parents: blockers.append('상위법 원문 미확보: 권한·위임·최신성 검토가 완료되지 않았습니다.')
    if errors: blockers.append('수집 또는 역할 실행 실패가 있어 검토 범위가 불완전합니다.')
    for doc in docs:
        if doc.source_state not in {'live','cache'}:
            blockers.append(f'{doc.title}: 공식 최신 원문 확인 필요({doc.source_state}).')
        t=temporal_status(doc,as_of)
        if not t['version_current_verified']:
            blockers.append(f'{doc.title}: 해당 기준일 적용 버전·조문별 시행일 확인 필요({t["state"]}).')
        if doc.warnings:
            blockers.extend(f'{doc.title}: {w}' for w in doc.warnings)
    rejected=sum(len(o.get('verified_claims',{}).get('rejected',[]))+len(o.get('proposed_clauses',{}).get('rejected',[])) for o in model_outputs)
    if rejected: blockers.append(f'모델 제안 중 근거 검사를 통과하지 못한 주장 {rejected}건을 채택하지 않았습니다.')
    if any(o.get('status') not in {'completed','not_requested'} for o in model_outputs):
        blockers.append('외부 모델 호출 일부가 미완료 또는 차단되었습니다.')
    blockers.append('법무·사업·예산 등 담당자의 최종 법적 판단 및 입법절차 확인이 필요합니다.')
    return {'status':'human_review_required','legal_approval':False,'submission_ready':False,
            'blocking_or_pending_items':list(dict.fromkeys(blockers)),
            'rejected_model_claims':rejected,'no_majority_vote_on_law':True,
            'decision_policy':'근거 없는 주장·상충 근거·미확인 적용시점은 다수결로 통과시키지 않습니다.'}


def safe(text) -> str:
    return html.escape(str(text),quote=True).replace('|','&#124;').replace('\r','').replace('\n','<br>')


def markdown_report(report:dict) -> str:
    rows=['# 조례 제·개정 종합 검토보고서','',
          '> 검토 보조자료입니다. 적법성 확정·법무심사·의회 의결을 대신하지 않습니다.','',
          f'검토 기준일: {safe(report["as_of"])} / 대상 기관: {safe(report["jurisdiction"])}',
          f'\n사업: {safe(report["project"])}',
          f'\n실행 방식: {safe(report["reasoning"])} / 역할 수: {len(report["agents"])}',
          '\n## 1. 입법 방식 검토']
    draft=report.get('draft',{})
    options=draft.get('legislative_options',{})
    rows.append(safe(options.get('provisional_path','자료 부족: 판단 유보')))
    for opt in options.get('options',[]): rows.append(f'\n**{safe(opt["path"])}** — {safe(opt["condition"])}')
    rows+=['\n## 2. 자료 확보 및 한계','|문서|버전|시행일|자료 상태|조문 수|','|---|---|---|---|---|']
    for doc in report.get('documents',[]):
        rows.append('|'+ '|'.join(safe(doc.get(k,'')) for k in ['title','version','effective_date','source_state','article_count'])+'|')
    for s in report.get('searches',[]):
        rows.append(f'\n검색 `{safe(s["query"])}`: {s["status"]}, 확인된 후보 {s["matched_in_scanned_rows"]}건. 검색 범위 밖 미제정 여부는 미판정.')
    rows+=['\n## 3. 상위법·행정 검토사항']
    for f in report.get('legal_review',{}).get('findings',[]):
        rows.append(f'\n**{safe(f["topic"])}** — {safe(f["action"])}\n근거 단서: {safe(", ".join(f["trigger_evidence_ids"]) or "사업 설명")}.')
    rows+=['\n## 4. 타 지자체 기능 비교']
    comp=report.get('comparison',{}) or {}
    for c in comp.get('comparisons',[]):
        rows.append(f'\n### {safe(c["peer_document"]["title"])}')
        for a in c['alignments']:
            rows.append(f'\n{safe(a["peer"]["article"])}: {safe(a["category"])} — 근거 {safe(a["peer"]["id"])}')
            for b in a['baseline_candidates'][:1]:
                rows.append('대응 후보: '+safe(b['evidence']['article'])+' / '+safe('; '.join(b['semantic_flags']) or '추가 의미 검토 필요'))
    rows+=['\n## 5. 상위법 개정 영향']
    impact=report.get('impact',{}) or {}
    if impact.get('parent_diff'):
        for ch in impact['parent_diff']['changes']:
            rows.append(f'\n{safe(ch["article"])}: {safe(ch["kind"])} — {safe("; ".join(ch["flags"]))}')
        for i in impact['impacts']:
            rows.append(f'\n정비 검토 후보: {safe(i["ordinance"]["title"])} {safe(i["ordinance_evidence"]["article"])} / {safe(i["disposition"])}')
    else: rows.append('구·신 상위법 버전이 없거나 조회되지 않아 영향 비교를 완료하지 못했습니다.')
    rows+=['\n## 6. 제정·개정 초안']
    rows.append(safe(draft.get('enactment_outline','초안 미작성')))
    for clause in draft.get('model_proposed_clauses',{}).get('accepted',[]):
        rows.append('\n**모델 제안 조문(권한·위임 적합성 미확정)**\n\n'+safe(clause['text'])+'\n\n'+safe(clause['reason']))
    rows.append('\n'+safe(draft.get('amendment_draft_status','')))
    rows+=['\n## 7. 절차 및 협의']
    for step in report.get('feasibility',{}).get('steps',[]):
        rows.append(f'\n**{safe(step["name"])}** ({safe(step["owner_role"])}) — {safe(step["check"])}. 적용 여부: {safe(step["applicability"])}.')
    rows+=['\n## 8. 최종 확인이 필요한 사항']
    for p in report.get('quality_gate',{}).get('blocking_or_pending_items',[]): rows.append('\n- '+safe(p))
    rows+=['\n## 9. 역할별 실행 기록','|역할|방식|상태|','|---|---|---|']
    for a in report['agents']: rows.append(f'|{safe(a["name"])}|{safe(a["method"])}|{safe(a["status"])}|')
    rows+=['\n## 10. 근거 원장']
    for eid,e in report.get('evidence',{}).items():
        rows.append(f'\n**{safe(eid)}** — {safe(e["document_title"])} {safe(e["article"])} / {safe(e["source_state"])}\n\n'+safe(e['text'])+'\n\n출처: '+safe(e.get('source_url','') or '사용자 제공 문서(공식성 미확인)'))
    rows+=['\n---','이 보고서는 요청 시점에 수집한 범위에 한정됩니다. 전국 전체 조례 및 모든 간접 영향을 전수 검토했다는 뜻이 아닙니다.']
    return '\n'.join(rows)+'\n'
