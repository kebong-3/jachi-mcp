from __future__ import annotations

import re
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from typing import Any
from zoneinfo import ZoneInfo
from .models import Article, Document, digest
from .normalize import compact, extract_references, paragraphs, article_order

DIMENSIONS = {
    "목적·정의": ["목적", "정의", "뜻한다"],
    "소관사무·책무": ["책무", "구청장", "시장", "군수", "사무"],
    "지원대상·제외": ["지원대상", "지원 대상", "대상자", "제외", "자격"],
    "지원내용·금액": ["지원금", "비용", "지원사업", "지원 사업", "보조금", "지원내용"],
    "신청·선정": ["신청", "선정", "접수", "결정"],
    "지급·중복방지": ["지급", "중복", "유사한 지원"],
    "중지·환수·이의": ["환수", "반환", "중지", "이의신청", "의견제출"],
    "위원회·제척회피": ["위원회", "제척", "기피", "회피", "위촉"],
    "위탁·공유재산": ["위탁", "공유재산", "사용료", "수탁"],
    "개인정보·기록": ["개인정보", "주민등록번호", "보유기간", "파기", "민감정보"],
    "점검·평가·공개": ["성과", "평가", "공개", "점검", "보고", "실태조사"],
    "시행·경과·서식": ["시행", "경과", "적용례", "별표", "별지", "서식"],
}


def features(text: str) -> set[str]:
    return {k for k,v in DIMENSIONS.items() if any(compact(w) in compact(text) for w in v)}


def shingles(text: str) -> set[str]:
    t = compact(text)[:12000]
    return {t[i:i+3] for i in range(max(0,len(t)-2))}


def jaccard(a:set, b:set) -> float:
    return len(a&b)/len(a|b) if a or b else 0.0


def alignment_score(a:Article, b:Article) -> float:
    title = SequenceMatcher(None,compact(a.title),compact(b.title)).ratio() if a.title and b.title else 0
    body = jaccard(shingles(a.text), shingles(b.text))
    function = jaccard(features(a.text),features(b.text))
    return round(.3*title + .55*body + .15*function,4)


def evidence(doc:Document, a:Article) -> dict:
    return {"id":"E-"+digest([doc.identity,a.key])[:20], "document_identity":doc.identity,
            "document_title":doc.title, "document_id":doc.document_id,"version":doc.version,
            "article":a.label,"article_key":a.key,"text":a.text,"source_url":doc.source_url,
            "retrieved_at":doc.retrieved_at,"effective_date":a.effective_date or doc.effective_date,
            "source_state":doc.source_state,"version_scope":doc.version_scope,
            "content_hash":doc.content_hash,"jurisdiction":doc.jurisdiction,"kind":doc.kind}


def evidence_index(docs:list[Document]) -> dict[str,dict]:
    entries = [evidence(d,a) for d in docs for a in d.articles]
    return {x["id"]:x for x in entries}


def temporal_status(doc:Document, as_of:date) -> dict:
    v = re.sub(r"[^0-9]","",doc.effective_date)
    if len(v)!=8:
        state="effective_date_unknown"
    else:
        try:
            effective=datetime.strptime(v,"%Y%m%d").date()
            state="future" if effective>as_of else "effective_on_or_before_as_of"
        except ValueError:
            state="effective_date_invalid"
    return {"state":state,"as_of":as_of.isoformat(),
            "version_current_verified":as_of==datetime.now(ZoneInfo("Asia/Seoul")).date() and doc.version_scope=="current" and doc.source_state in {"live","cache"} and state=="effective_on_or_before_as_of",
            "caveat":"시행일이 과거라는 사실만으로 해당 기준일의 현행본임이 증명되지 않습니다. 후속개정·조문별 시행일·부칙을 확인하세요."}


def semantic_flags(before:str, after:str) -> list[str]:
    flags=[]
    nums=lambda t: set(re.findall(r"\d[\d,]*(?:\.\d+)?\s*(?:원|만원|퍼센트|%|명|개월|년|일|세|회)",t))
    if nums(before)!=nums(after): flags.append("금액·연령·기간·인원 등 수치 차이")
    modal=lambda t: {w for w in ["할수있다","하여야한다","해야한다","해서는아니된다","할수없다"] if w in compact(t)}
    if modal(before)!=modal(after): flags.append("재량·의무·금지 표현 차이")
    neg=lambda t: {w for w in ["제외","포함","아니","금지","이상","이하","초과","미만"] if w in t}
    if neg(before)!=neg(after): flags.append("범위·예외·상하한 표현 차이")
    return flags


def compare_documents(baseline:Document, others:list[Document], as_of:date, max_pairs:int=24000) -> dict:
    comparisons=[]
    pairs_used=0
    unprocessed=0
    baseline_active=[a for a in baseline.articles if not a.deleted]
    for doc in others:
        candidates=[]
        for their in doc.articles:
            if their.deleted: continue
            if pairs_used+len(baseline_active)>max_pairs:
                unprocessed+=1
                candidates.append({"category":"not_processed_budget","peer":evidence(doc,their),
                                   "baseline_candidates":[],"finding":"계산 한도: 범위를 나누어 재검토"})
                continue
            pairs_used+=len(baseline_active)
            ranked=sorted(((alignment_score(our,their),our) for our in baseline.articles if not our.deleted),key=lambda x:x[0],reverse=True)
            top=ranked[:2]
            best=top[0][0] if top else 0.0
            category="correspondence_candidate" if best>=.52 else "possible_gap"
            candidates.append({"category":category,"peer":evidence(doc,their),
                               "baseline_candidates":[{"retrieval_score":score,"evidence":evidence(baseline,art),
                                    "semantic_flags":semantic_flags(art.text,their.text),
                                    "paragraphs_only_in_peer_candidate":[p for p in paragraphs(their.text)
                                          if max((jaccard(shingles(p),shingles(q)) for q in paragraphs(art.text)),default=0)<.35]}
                                    for score,art in top],
                               "finding":"기능·본문 대응 후보"})
        comparisons.append({"peer_document":doc.summary(),"temporal":temporal_status(doc,as_of),
                            "same_jurisdiction_level":"미판정: 권한 수준 확인 필요", "alignments":candidates})
    # Matrix is observed coverage, not a legal quality score or a mandatory clause list.
    matrix=[]
    for dim in DIMENSIONS:
        cells=[]
        for doc in [baseline]+others:
            hits=[evidence(doc,a)["id"] for a in doc.articles if dim in features(a.text)]
            cells.append({"document_identity":doc.identity,"observed":bool(hits),"evidence_ids":hits})
        matrix.append({"dimension":dim,"cells":cells})
    # User-facing Korean labels kept explicit, separate from language-independent status codes.
    for comp in comparisons:
        comp["same_jurisdiction_level"]="미판정: 광역·기초·교육기관의 권한 차이를 별도 확인"
        for a in comp["alignments"]:
            a["finding"]="문자·기능 단서에 따른 대응 후보이며 법적 동등성 판단 아님"
    return {"baseline":baseline.summary(),"comparisons":comparisons,"dimension_matrix":matrix,
            "method":"제목 0.30 + 본문 3글자 집합 0.55 + 기능단서 0.15. 법적 적합성 점수 아님",
            "coverage":{"pairs_compared":pairs_used,"unprocessed_articles":unprocessed,"comparison_complete":unprocessed==0},
            "guardrails":["조항 수가 많거나 여러 지자체가 채택했다고 우수·적법한 것은 아닙니다.",
                          "possible_gap은 기능이 다른 조문·다른 조례·상위법에 있을 수 있는 검토 후보입니다.",
                          "대상·사무·재정·광역/기초 권한·시행시점을 확인한 후 적용하세요."]}


RULES = [
    ("R01","권리제한·의무부과",r"의무|금지|제한|허가|신고하여야","소관사무 및 법률 유보·위임 범위를 확인"),
    ("R02","제재·과태료",r"과태료|벌금|벌칙|가산금","개별법·지방자치법 등 근거, 상한 및 절차를 확인; 조례만으로 모든 제재를 만들 수 있다고 단정 금지"),
    ("R03","지원금·보조금",r"보조금|지원금|경비.{0,12}지원|예산의 범위","지방재정·지방보조금 법령, 공익성·지원근거·중복지원·집행절차 확인"),
    ("R04","사회보장 신설·변경",r"출산|양육|난임|복지|돌봄|수당|취약계층","사회보장제도 신설·변경 협의 대상 여부 및 협의 진행상태 확인"),
    ("R05","민간위탁",r"위탁|수탁","자치사무 여부·위탁범위·수탁자 선정·의회 동의·성과관리 관련 개별법과 우리 조례 확인"),
    ("R06","공유재산·사용료",r"공유재산|사용료|임대료|무상|감면|대부","재산 유형과 사용·대부·감면 권한, 공유재산 법령 및 우리 조례 확인"),
    ("R07","개인정보·민감정보",r"개인정보|주민등록번호|민감정보|건강정보|명단","수집 필요성·처리 근거·민감/고유식별정보 특칙·보유기간·파기·위탁 확인"),
    ("R08","위원회·이해충돌",r"위원회|위원장|위촉","기존 위원회 활용 가능성·구성·임기·제척기피회피·수당·권한 침해 확인"),
    ("R09","환수·중지·불복",r"환수|반환|취소|지급.{0,4}중지","처분 근거·사전통지·의견제출·불복 절차 및 상위법과의 관계 확인"),
    ("R10","재위임·백지위임",r"구청장이.{0,20}정한다|시장이.{0,20}정한다|규칙으로 정한다","조례에 정해야 할 본질적 사항의 과도한 위임 여부 확인"),
    ("R11","중복·충돌",r"이 조례|다른 조례","우리 자치법규·상위법의 정의·적용대상·위원회·지원기준 중복 확인"),
    ("R12","계획·평가",r"계획|실태조사|성과|평가","주기·주체·결과활용·공개범위·추가 행정부담 확인"),
    ("R13","자격·형평",r"거주|연령|연속|주소|소득|재산|국적","합리적 대상 설정·차별·과잉 제한·확인자료 최소화 확인"),
    ("R14","재정·조직",r"센터|기금|출연|공단|재단|전담|인력","조직·정원·기금·출자출연 관련 별도 근거와 중장기 재정 영향 확인"),
    ("R15","정치적 중립·선거",r"포인트|상품권|기념품|격려금|홍보물","수혜 방식과 시기에 따라 공직선거법 등 관련 검토·유권해석 필요성 확인"),
    ("R16","부칙·경과조치",r"시행|경과|적용례|종전","시행예정일·상위법 시행일·기존 신청/처분 보호·다른 조례 개정 연계 확인"),
]
GUIDE="https://opinion.lawmaking.go.kr/lmKnlg/loPlanGud"


def review_rules(docs:list[Document], project:str) -> dict:
    findings=[]
    for rid,title,pattern,action in RULES:
        refs=[evidence(d,a)["id"] for d in docs for a in d.articles if re.search(pattern,a.text)]
        in_project=bool(re.search(pattern,project))
        if refs or in_project:
            findings.append({"id":rid,"topic":title,"status":"review_required","priority":"high" if rid in {"R01","R02","R07"} else "normal",
                             "trigger_evidence_ids":refs,"triggered_by_project":in_project,
                             "action":action,"rule_basis_type":"screening_checklist_not_legal_determination",
                             "method_reference":GUIDE})
    texts="\n".join(a.text for d in docs for a in d.articles)
    refs=extract_references(texts)
    unresolved=bool(re.search(r"(?:이 법|같은 법|법)\s*제\d+조",texts))
    return {"findings":findings,"explicit_references":refs,"unresolved_short_references":unresolved,
            "legal_conclusion":"자동 적법/위법 판정 없음. 규칙 탐지 여부와 법적 결론을 구분하세요.",
            "negative_result_warning":"단서가 발견되지 않아도 검토 항목이 불필요하거나 적법하다는 뜻이 아닙니다."}


def verify_references(refs:list[dict], parents:list[Document], as_of:date) -> list[dict]:
    out=[]
    for ref in refs:
        candidates=[d for d in parents if compact(d.title)==compact(ref["law_title"])]
        if not candidates:
            out.append({**ref,"status":"source_missing","message":"공식 원문 미확보. 해당 법령을 검색·조회하세요."}); continue
        versions=[]
        for doc in candidates:
            art=next((a for a in doc.articles if a.label==ref["article"]),None)
            status="title_only" if not ref["article"] else ("article_present" if art and not art.deleted else "article_missing_or_deleted_in_supplied_version")
            versions.append({"document_identity":doc.identity,"status":status,
                             "evidence_id":evidence(doc,art)["id"] if art else None,
                             "temporal":temporal_status(doc,as_of),"source_state":doc.source_state,
                             "subdivision_status":"not_verified" if ref.get("paragraph") or ref.get("item") else "not_requested"})
        out.append({**ref,"versions":versions,"warning":"조문 존재 확인은 인용 취지의 정확성·사업 근거·위임 적합성 검증과 다릅니다."})
    return out


def diff_documents(old:Document,new:Document) -> dict:
    if old.kind!=new.kind:
        raise ValueError("같은 종류의 문서를 비교하세요.")
    if old.document_id!=new.document_id and old.source_state not in {"user_provided","fixture"} and new.source_state not in {"user_provided","fixture"}:
        raise ValueError("동일 법령/조례 ID가 아닙니다. 제명변경·통합·분리인지 별도 확인하세요.")
    left,right={a.key:a for a in old.articles},{a.key:a for a in new.articles}
    changes=[]
    for key in sorted(left.keys()|right.keys(),key=lambda k:article_order(k.removeprefix("main:"))):
        a,b=left.get(key),right.get(key)
        if a and b and a.text==b.text: continue
        typ="added" if a is None else "removed" if b is None else "changed"
        changes.append({"kind":typ,"article":(b or a).label,
                        "before":evidence(old,a) if a else None,"after":evidence(new,b) if b else None,
                        "flags":semantic_flags(a.text if a else "",b.text if b else "")})
    moved=[]
    for r in [x for x in changes if x["kind"]=="removed"]:
        for a in [x for x in changes if x["kind"]=="added"]:
            similarity=jaccard(shingles(r["before"]["text"]),shingles(a["after"]["text"]))
            if similarity>=.55:
                moved.append({"old_article":r["article"],"new_article":a["article"],"retrieval_score":round(similarity,3),
                              "status":"renumbering_candidate_human_confirmation_required"})
    return {"old":old.summary(),"new":new.summary(),"changes":changes,"renumbering_candidates":moved,
            "supplementary_changed":old.supplementary!=new.supplementary,
            "annexes_changed":old.annexes!=new.annexes,
            "metadata_changed":{k:{"before":getattr(old,k),"after":getattr(new,k)} for k in ["title","effective_date","promulgation_date"] if getattr(old,k)!=getattr(new,k)},
            "warning":"문자 비교는 법적 효력 변화의 확정판단이 아닙니다. 부칙/별표·표현 변화의 취지를 확인하세요."}


def amendment_impact(old:Document,new:Document,ordinances:list[Document],as_of:date) -> dict:
    delta=diff_documents(old,new)
    changed={c["article"]:c for c in delta["changes"]}
    impacts=[]
    for doc in ordinances:
        for art in doc.articles:
            refs=extract_references(art.text)
            for ref in refs:
                if compact(ref["law_title"]) not in {compact(old.title),compact(new.title)}: continue
                if ref["article"] in changed:
                    impacts.append({"ordinance":doc.summary(),"ordinance_evidence":evidence(doc,art),
                                    "parent_change":changed[ref["article"]],"link_type":"explicit_article_reference",
                                    "priority":"high","disposition":"개정 필요성 검토: 조번호·대상·기준·권한을 대조; 자동 치환 금지"})
                elif not ref["article"] or delta["supplementary_changed"] or delta["annexes_changed"]:
                    impacts.append({"ordinance":doc.summary(),"ordinance_evidence":evidence(doc,art),
                                    "link_type":"law_or_supplement_reference","priority":"normal",
                                    "disposition":"법령 전체 인용 또는 부칙·별표 변경 영향 확인"})
    return {"parent_diff":delta,"new_temporal":temporal_status(new,as_of),"impacts":impacts,
            "scope":{"ordinances_checked":len(ordinances),"national_exhaustive":False},
            "unresolved":["직접 인용 없는 간접 영향(지원대상·업무권한·정의 변화)은 별도 검토",
                          "조례와 함께 시행규칙·서식·업무지침·시스템 항목의 정비 필요성 확인",
                          "상위법 부칙에 명시된 유예기간·경과조치와 내부 입법일정을 확인; 임의 법정기한 생성 금지"]}


PROCEDURES=[
    ("소관사무·입법형식","항상","사업부서+법무부서","조례가 필요한지, 기존 조례·규칙·지침·예산 집행으로 가능한지 비교"),
    ("입법예고","적용·예외 확인","입안부서","행정절차법·지방자치법·우리 입법절차 조례상 기간/예외·제출주체 확인"),
    ("비용추계·예산협의","재정수반 여부 확인","예산부서","우리 비용추계 조례의 대상·기준금액·면제사유; 재원·연도별 수요 확인"),
    ("규제심사","규제 해당 여부 확인","규제업무부서","권리제한·의무 신설/강화 여부와 심사 절차 확인"),
    ("성별영향평가","대상·제외 확인","성별영향평가 담당","성별영향평가법과 해당 기관 운영기준 확인"),
    ("부패영향평가","대상·운영기준 확인","감사부서","특혜·재량·대상선정·이해충돌 기준 확인"),
    ("사회보장 협의","복지성 제도 여부 확인","복지부서","사회보장기본법상 신설·변경 협의 대상 확인"),
    ("개인정보 검토","개인정보 처리 여부 확인","개인정보보호 담당","수집항목·법적근거·위탁·보유기간·시스템 영향 확인"),
    ("조례규칙심의회·의회","제출주체별 확인","법무부서+의회협력 담당","단체장 제출/의원 발의/주민청구에 맞는 심의·의결 경로 확인"),
    ("공포·시행 준비","의결 후 확인","입안부서+법무부서","공포·시행일, 경과조치, 서식·홈페이지·시스템·직원교육 정비")]


def procedure_checklist(project:str) -> dict:
    return {"steps":[{"name":a,"applicability":b,"owner_role":c,"check":d,"completed":False} for a,b,c,d in PROCEDURES],
            "budget_template":{"beneficiaries":None,"unit_cost":None,"frequency":None,"operating_cost":None,
                               "formula":"지원대상 수 × 단가 × 지원횟수 + 운영비 (사업에 맞게 수정)",
                               "amounts_are_estimates":True},
            "warning":"법정 기간·임계금액·부서명·연락처는 추정하지 않습니다. 실제 법령/우리 조례/조직도에서 확인하세요."}
