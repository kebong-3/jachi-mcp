from __future__ import annotations

import html
import re
from collections import Counter
from typing import Any
from urllib.parse import urlencode
from .models import Article, Document, UserText, digest


def clean(value: Any) -> str:
    """Preserve lists, paragraph/item content, and line breaks; never stringify dicts."""
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(filter(None, (clean(v) for v in value)))
    if isinstance(value, dict):
        return clean(value.get("#text", value.get("_", "")))
    text = str(value)
    text = re.sub(r"<\s*(?:br\b[^>]*|/p|/div|/li)\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]*>", "", text)
    return re.sub(r"[ \t]+", " ", html.unescape(text)).strip()


def norm_key(key: str) -> str:
    return re.sub(r"[\s_]", "", str(key))


def pick(obj: dict, *keys: str) -> str:
    obj = {norm_key(k): v for k, v in obj.items()}
    for key in keys:
        if norm_key(key) in obj:
            value = clean(obj[norm_key(key)])
            if value:
                return value
    return ""


def nodes(value: Any):
    if isinstance(value, dict):
        yield value
        for v in value.values():
            yield from nodes(v)
    elif isinstance(value, list):
        for v in value:
            yield from nodes(v)


def first_meta(data: Any, kind: str) -> dict:
    names = ("자치법규명",) if kind == "ordinance" else ("법령명한글", "법령명_한글")
    return next((n for n in nodes(data) if pick(n, *names)), {})


def listing_rows(data: Any, kind: str) -> list[dict]:
    result = []
    for n in nodes(data):
        title = pick(n, "자치법규명") if kind == "ordinance" else pick(n, "법령명한글", "법령명_한글")
        doc_id = pick(n, "자치법규ID") if kind == "ordinance" else pick(n, "법령ID")
        mst = pick(n, "자치법규일련번호") if kind == "ordinance" else pick(n, "법령일련번호")
        if title and (doc_id or mst):
            result.append({"kind": kind, "document_id": doc_id, "mst": mst, "title": title,
                           "jurisdiction": pick(n, "지자체기관명"),
                           "effective_date": pick(n, "시행일자"),
                           "promulgation_date": pick(n, "공포일자"),
                           "amendment_type": pick(n, "제개정구분명"),
                           "status_code": pick(n, "현행연혁코드"),
                           "ordinance_type": pick(n, "자치법규종류", "법령구분명"),
                           "source_url": public_url(kind, doc_id, mst),
                           "parent_article": pick(n, "법령조번호"),
                           "ordinance_article": pick(n, "자치법규조번호")})
    return list({(r["document_id"], r["mst"], r["parent_article"], r["ordinance_article"]): r
                 for r in result}.values())


def total_count(data: Any) -> int | None:
    for n in nodes(data):
        v = pick(n, "totalCnt", "totalCount")
        if v.isdigit():
            return int(v)
    return None


def public_url(kind: str, doc_id: str, mst: str = "", effective: str = "") -> str:
    """Public viewer link, never an OC-bearing API URL."""
    if kind == "ordinance":
        values = {"ordinSeq": mst} if mst else {"ordinId": doc_id}
        return "https://www.law.go.kr/자치법규/" if not any(values.values()) else "https://www.law.go.kr/ordinInfoP.do?" + urlencode(values)
    values = {"lsiSeq": mst, "efYd": effective} if mst else {"lsId": doc_id}
    return "https://www.law.go.kr/lsInfoP.do?" + urlencode({k:v for k,v in values.items() if v})


def jo_label(value: str, branch: str = "") -> str:
    value = re.sub(r"\s+", "", value)
    m = re.fullmatch(r"제(\d+)조(?:의(\d+))?", value)
    if m and int(m[1])>0:
        return f"제{int(m[1])}조" + (f"의{int(m[2])}" if m[2] else "")
    if value.isdigit():
        if len(value) == 6:
            number, br = int(value[:4]), int(value[4:])
        elif len(value) == 7: # 법령 조문키 includes trailing entry marker; prefer 조문번호.
            number, br = int(value[:4]), int(value[4:6])
        else:
            number, br = int(value), int(branch or 0)
        if number:
            return f"제{number}조" + (f"의{br}" if br else "")
    return ""


def article_order(label: str) -> tuple[int, int]:
    m = re.fullmatch(r"제(\d+)조(?:의(\d+))?", label)
    return (int(m[1]), int(m[2] or 0)) if m else (10**8, 0)


def flatten_article(raw: dict) -> str:
    parts = []
    for k, v in raw.items():
        if norm_key(k) in {"조내용", "조문내용", "항내용", "호내용", "목내용"}:
            value = clean(v)
            if value:
                parts.append(value)
        elif isinstance(v, (list, dict)) and norm_key(k) in {"항", "호", "목", "항단위", "호단위", "목단위"}:
            seq = v if isinstance(v, list) else [v]
            parts.extend(flatten_article(x) for x in seq if isinstance(x, dict))
    # Do not remove identical legitimate items. Only omit exact nested copy of full previous block.
    out = []
    for p in parts:
        if p and not (out and p == out[-1]):
            out.append(p)
    return "\n".join(out)


def parse_document(data: Any, kind: str, document_id: str = "", mst: str = "",
                   effective: str = "", source_state: str = "live", version_scope: str = "unknown") -> Document:
    meta = first_meta(data, kind)
    if not meta:
        raise ValueError("본문 메타데이터를 식별하지 못했습니다. 검색결과나 인증오류를 본문으로 대체하지 않습니다.")
    actual_id = pick(meta, "자치법규ID" if kind == "ordinance" else "법령ID") or document_id
    actual_mst = pick(meta, "자치법규일련번호" if kind == "ordinance" else "법령일련번호") or mst
    title = pick(meta, "자치법규명", "법령명한글", "법령명_한글")
    arts, supplements, annexes, warnings = [], [], [], []
    if not pick(meta, "자치법규일련번호" if kind == "ordinance" else "법령일련번호"):
        warnings.append("응답 버전 식별자 미확인: 요청 MST 또는 본문 해시를 기록했으나 버전 메타데이터 독립 대조 필요")
    if not pick(meta, "자치법규ID" if kind == "ordinance" else "법령ID"):
        warnings.append("응답 ID 미확인: 요청 식별자 및 정식 제명 대조 필요")
    for n in nodes(data):
        if pick(n, "조문여부") == "N":
            continue
        content = pick(n, "조내용", "조문내용")
        if content:
            label = jo_label(pick(n, "조문번호"), pick(n, "조문가지번호"))
            if not label:
                m = re.match(r"(제\s*\d+\s*조(?:\s*의\s*\d+)?)", content)
                label = jo_label(m[1]) if m else ""
            if not label:
                warnings.append("조문번호 해석 실패: 번호 미확인 내용을 검토에서 누락시킬 수 있음")
                continue
            body = flatten_article(n)
            arts.append(Article(key=f"main:{label}", label=label,
                                title=pick(n, "조제목", "조문제목"), text=body,
                                deleted=bool(re.match(r"^(?:제\d+조(?:의\d+)?\s*(?:\([^)]*\))?\s*)?삭제(?:\s|<|$)", body)),
                                effective_date=pick(n, "조문시행일자")))
        if pick(n, "부칙내용"):
            supplements.append(pick(n, "부칙내용"))
        if pick(n, "별표제목", "별표내용"):
            annexes.append({"title": pick(n, "별표제목"), "text": pick(n, "별표내용"),
                            "file": pick(n, "별표첨부파일명", "별표HWP파일명"), "number": pick(n, "별표번호"),
                            "file_url":pick(n,"별표서식파일링크","별표PDF파일링크","별표HWP파일링크")})
    counts = Counter(a.key for a in arts)
    if any(c > 1 for c in counts.values()):
        raise ValueError("중복 조문키가 반환되었습니다. 법령 버전/본문 구조 수동 확인 필요")
    if not arts:
        raise ValueError("조문을 추출하지 못했습니다. '문제 없음' 또는 '조례 없음'으로 해석하지 마세요.")
    if kind == "ordinance":
        warnings.append("자치법규 별표 API 제공범위 제한: 별표·별지·서식은 소관기관 원문과 별도 대조 필요")
    if annexes and any(not a["text"] for a in annexes):
        warnings.append("별표 첨부파일의 본문 미수집: 메타데이터 비교만 가능하며 원본 표·서식 확인 필요")
    if not supplements:
        warnings.append("부칙 미수집: 시행일 특례·적용례·경과조치 확인 미완료")
    if any("별표" in a.text or "별지" in a.text for a in arts) and not annexes:
        warnings.append("본문에서 별표/별지 참조가 발견되었으나 내용 미수집")
    return Document(kind=kind, document_id=actual_id, version=actual_mst, title=title,
                    jurisdiction=pick(meta, "지자체기관명"),
                    effective_date=pick(meta, "시행일자") or effective,
                    promulgation_date=pick(meta, "공포일자"),
                    source_url=public_url(kind, actual_id, actual_mst, effective),
                    source_state=source_state, version_scope=version_scope, articles=arts,
                    supplementary=supplements, annexes=annexes, warnings=warnings,
                    department=pick(meta, "담당부서명"), phone=pick(meta, "전화번호"),
                    amendment_reason=next((pick(n, "제개정이유내용") for n in nodes(data) if pick(n,"제개정이유내용")), ""),
                    upstream_hash=digest(data))


def parse_user_text(item: UserText) -> Document:
    text = item.text.replace("\r\n", "\n")
    # Only beginning-of-line headings. Body citations must never become new articles.
    split = re.split(r"(?m)^\s*부\s*칙[^\n]*", text, maxsplit=1)
    main = split[0]
    matches = list(re.finditer(r"(?m)^\s*(제\s*\d+\s*조(?:\s*의\s*\d+)?)\s*(?:\(([^\n)]*)\))?", main))
    arts = []
    for i, m in enumerate(matches):
        label = jo_label(m[1])
        body = main[m.start(): matches[i+1].start() if i+1 < len(matches) else len(main)].strip()
        arts.append(Article(key=f"main:{label}", label=label, title=m[2] or "", text=body,
                            deleted=bool(re.search(r"^제\s*\d+조(?:의\d+)?\s*삭제", body))))
    warnings = ["사용자 제공 문서: 공식 원문·시행일·완전성은 검증되지 않았습니다."]
    if not arts:
        arts = [Article(key="unstructured:1", label="조문번호 미확인", text=text)]
        warnings.append("조문 구조 인식 실패: 행 시작에 제1조(목적) 형식을 사용하세요.")
    if len({a.key for a in arts}) != len(arts):
        warnings.append("중복 조문번호: 일부개정문/신구조문대비표가 아니라 한 버전의 본문만 입력하세요.")
        raise ValueError(warnings[-1])
    return Document(kind=item.kind, document_id="user-"+digest(item.model_dump())[:16],
                    title=item.title, jurisdiction=item.jurisdiction, source_url=item.source_url,
                    source_state="user_provided", articles=arts,
                    supplementary=[split[1].strip()] if len(split)>1 else [], warnings=warnings)


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def region_match(org: str, wanted: str) -> bool:
    """No assumed merger aliases and no substring match for ambiguous '서구'/'광주시'."""
    a, b = compact(org), compact(wanted)
    return bool(a and b and a == b)


def paragraphs(text: str) -> list[str]:
    body = re.sub(r"^제\s*\d+\s*조(?:\s*의\s*\d+)?\s*\([^)]*\)\s*", "", text.strip())
    return [s.strip() for s in re.split(r"(?=[①-⑳㉑-㉟])", body) if s.strip()]


def extract_references(text: str) -> list[dict[str, str]]:
    """Conservative explicit quoted citations; bare '법 제N조' is flagged, not guessed."""
    refs = []
    pat = r"[「『]([^」』]{1,120})[」』]\s*(제\s*\d+\s*조(?:\s*의\s*\d+)?)?(\s*제\s*\d+\s*항)?(\s*제\s*\d+\s*호)?"
    for m in re.finditer(pat, text):
        refs.append({"law_title": m[1], "article": jo_label(m[2] or ""),
                     "paragraph": compact(m[3] or ""), "item": compact(m[4] or ""),
                     "quote": m[0]})
    aliases = dict(re.findall(r"[「『]([^」』]+)[」』]\s*\(이하\s*[\"“]([^\"”]+)[\"”]", text))
    # alias mapping is emitted as such; it is not a separate source.
    for title, alias in aliases.items():
        for m in re.finditer(re.escape(alias)+r"\s*(제\s*\d+\s*조(?:\s*의\s*\d+)?)", text):
            refs.append({"law_title": title, "article": jo_label(m[1]), "paragraph": "", "item": "", "quote": m[0]})
    return list({(x["law_title"],x["article"],x["paragraph"],x["item"]):x for x in refs}.values())
