#!/usr/bin/env python3
"""자치법규 MCP 서버 (jachibeopgyu_mcp)

법제처 국가법령정보 공동활용 OPEN API를 조문 단위로 정제해
AI가 곧바로 쓸 수 있게 노출하는 MCP 서버.

환경변수:
    LAW_OC  법제처 OPEN API 인증키(신청 시 쓴 이메일 ID)

실행:
    python jachibeopgyu_mcp.py            # MCP stdio 서버로 기동
    python jachibeopgyu_mcp.py --selftest # API 응답 구조 점검(키 이름 확인용)
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import re
from difflib import SequenceMatcher
import sys
from enum import Enum
from typing import Any, Dict, List, Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field

try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.types import ToolAnnotations as _Ann
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore
    _Ann = None  # type: ignore

# ---------------------------------------------------------------- 상수

SEARCH_URL = "https://www.law.go.kr/DRF/lawSearch.do"
SERVICE_URL = "https://www.law.go.kr/DRF/lawService.do"
OC = os.environ.get("LAW_OC", "test")
# 원격 모드에서 사용자가 자기 인증키를 들고 올 때 요청 단위로 담아두는 자리
_REQ_OC: "contextvars.ContextVar[str]" = contextvars.ContextVar("req_oc", default="")
TIMEOUT = 20.0
MAX_DISPLAY = 100

KND_CODES = {
    "조례": "30001",
    "규칙": "30002",
    "훈령": "30003",
    "예규": "30004",
    "기타": "30006",
    "고시": "30010",
    "의회규칙": "30011",
}

# ---- 지자체명 별칭 -------------------------------------------------
SIDO_ALIASES: Dict[str, Set[str]] = {}
_SIDO_GROUPS = [
    ["서울", "서울시", "서울특별시"],
    ["부산", "부산시", "부산광역시"],
    ["대구", "대구시", "대구광역시"],
    ["인천", "인천시", "인천광역시"],
    ["광주", "광주시", "광주광역시", "전남광주통합특별시", "광주전남통합특별시"],
    ["대전", "대전시", "대전광역시"],
    ["울산", "울산시", "울산광역시"],
    ["세종", "세종시", "세종특별자치시"],
    ["경기", "경기도"],
    ["강원", "강원도", "강원특별자치도"],
    ["충북", "충청북도"],
    ["충남", "충청남도"],
    ["전북", "전라북도", "전북특별자치도"],
    ["전남", "전라남도", "전남광주통합특별시", "광주전남통합특별시"],
    ["경북", "경상북도"],
    ["경남", "경상남도"],
    ["제주", "제주도", "제주특별자치도"],
]
for g in _SIDO_GROUPS:
    for a in g:
        SIDO_ALIASES.setdefault(a, set()).update(g)


def _sq(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def region_match(기관명: str, 원하는지자체: str) -> bool:
    """'광주광역시 서구'로 '전남광주통합특별시 서구'를 잡아낸다."""
    org, want = _sq(기관명), _sq(원하는지자체)
    if not want:
        return True
    if want in org:
        return True
    toks = [t for t in re.split(r"\s+", 원하는지자체.strip()) if t]
    if not toks:
        return False
    base = toks[-1]                       # 서구 / 수원시 / 해운대구
    if base not in org:
        return False
    if len(toks) == 1:
        return True
    sido = toks[0]
    for alias in SIDO_ALIASES.get(sido, {sido}):
        if _sq(alias) in org:
            return True
    return False


# ---- 조제목 유사도 -------------------------------------------------
_STOP = {"등", "및", "의", "에", "관한", "대한", "위한", "그", "이"}


def _title_tokens(t: str) -> Set[str]:
    t = re.sub(r"[^\w가-힣]+", " ", t or "")
    return {w for w in t.split() if w and w not in _STOP}


def title_sim(a: str, b: str) -> float:
    a2, b2 = _sq(a), _sq(b)
    if not a2 or not b2:
        return 0.0
    if a2 == b2:
        return 1.0
    seq = SequenceMatcher(None, a2, b2).ratio()
    ta, tb = _title_tokens(a), _title_tokens(b)
    jac = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    contain = 1.0 if (a2 in b2 or b2 in a2) else 0.0
    return max(seq, jac, contain * 0.85)


# ---- 항 분해 -------------------------------------------------------
_HANG = r"[\u2460-\u2473]"          # ① ~ ⑳


def split_hang(조내용: str) -> List[str]:
    """조내용을 항 단위로 쪼갠다. 항 표시가 없으면 통째로 1개."""
    body = re.sub(r"^제\d+조(의\d+)?\s*\([^)]*\)\s*", "", (조내용 or "").strip())
    parts = [p.strip() for p in re.split(rf"(?={_HANG})", body) if p.strip()]
    return parts if len(parts) > 1 else ([body] if body else [])


def hang_sim(a: str, b: str) -> float:
    a2 = _sq(re.sub(_HANG, "", a or ""))
    b2 = _sq(re.sub(_HANG, "", b or ""))
    if not a2 or not b2:
        return 0.0
    return SequenceMatcher(None, a2, b2).ratio()


mcp = _Server("jachibeopgyu_mcp")


def _ann(title: str) -> Any:
    """읽기전용 도구 어노테이션. mcp 1.x/2.x 양쪽 형식을 맞춘다."""
    if _Ann is not None:
        return _Ann(
            title=title,
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        )
    return {
        "title": title,
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }


# ---------------------------------------------------------------- 공통 유틸


class OrdinanceKind(str, Enum):
    조례 = "조례"
    규칙 = "규칙"
    훈령 = "훈령"
    예규 = "예규"
    고시 = "고시"
    의회규칙 = "의회규칙"
    전체 = "전체"


async def _get(url: str, params: Dict[str, Any]) -> Any:
    """법제처 API 호출. JSON 파싱까지 담당."""
    params = {k: v for k, v in params.items() if v not in (None, "")}
    params.setdefault("OC", _REQ_OC.get() or OC)
    params.setdefault("type", "JSON")
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        text = resp.text.strip()
    if not text:
        raise ValueError("빈 응답입니다. LAW_OC 인증키가 유효한지 확인하세요.")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        snippet = text[:300]
        raise ValueError(
            "JSON이 아닌 응답을 받았습니다. 인증키(LAW_OC) 또는 요청 IP 등록 문제일 "
            f"가능성이 큽니다. 응답 앞부분: {snippet}"
        )


def _deep_find_list(obj: Any, hint_keys: List[str]) -> List[Dict[str, Any]]:
    """응답 JSON 어디에 리스트가 박혀 있든 찾아낸다.

    법제처 응답은 래퍼 키 이름이 대상마다 달라서 하드코딩하지 않는다.
    """
    found: List[Dict[str, Any]] = []

    def walk(node: Any, key: Optional[str]) -> None:
        nonlocal found
        if found:
            return
        if isinstance(node, list):
            if node and isinstance(node[0], dict):
                if key is None or not hint_keys or any(h in (key or "") for h in hint_keys):
                    found = node
                    return
            for item in node:
                walk(item, key)
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(v, k)

    walk(obj, None)
    if not found:
        # 힌트 무시하고 아무 리스트나 재시도
        def walk2(node: Any) -> None:
            nonlocal found
            if found:
                return
            if isinstance(node, list) and node and isinstance(node[0], dict):
                found = node
                return
            if isinstance(node, dict):
                for v in node.values():
                    walk2(v)
            elif isinstance(node, list):
                for v in node:
                    walk2(v)

        walk2(obj)
    return found


def _flat(v: Any) -> str:
    """법제처는 같은 값을 리스트로 싸서 보내기도 한다."""
    if isinstance(v, list):
        for item in v:
            if item not in (None, ""):
                return str(item).strip()
        return ""
    return str(v).strip() if v not in (None, "") else ""


def _pick(d: Dict[str, Any], *names: str, default: str = "") -> str:
    """키 이름이 흔들려도 값을 집어온다."""
    for n in names:
        if n in d and _flat(d[n]):
            return _flat(d[n])
    for n in names:
        for k, v in d.items():
            if n in str(k) and _flat(v):
                return _flat(v)
    return default


def _jo_label(code: str) -> str:
    """'000100' -> '제1조', '000102' -> '제1조의2', '000000' -> ''."""
    code = (code or "").strip()
    if not code.isdigit() or len(code) < 6:
        return ""
    jo, ga = int(code[:4]), int(code[4:6])
    if jo == 0:
        return ""
    return f"제{jo}조" + (f"의{ga}" if ga else "")


KND_REVERSE = {
    "C0001": "조례", "C0002": "규칙", "C0003": "훈령",
    "C0004": "예규", "C0006": "기타", "C0010": "고시", "C0011": "의회규칙",
}


def _find_dict_with(obj: Any, key: str) -> Dict[str, Any]:
    """특정 키를 품은 딕셔너리를 깊이 찾아 반환."""
    if isinstance(obj, dict):
        if key in obj:
            return obj
        for v in obj.values():
            hit = _find_dict_with(v, key)
            if hit:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = _find_dict_with(v, key)
            if hit:
                return hit
    return {}


def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    return re.sub(r"[ \t]+", " ", s).strip()


def _norm_listing(raw: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "자치법규ID": _pick(raw, "자치법규ID", "법령ID", "ID"),
        "MST": _pick(raw, "자치법규일련번호", "일련번호", "MST"),
        "자치법규명": _strip_tags(_pick(raw, "자치법규명", "법령명")),
        "지자체": _pick(raw, "지자체기관명", "소관부처명", "기관명"),
        "종류": _pick(raw, "자치법규종류", "법령구분명"),
        "제개정구분": _pick(raw, "제개정구분명"),
        "공포일자": _pick(raw, "공포일자"),
        "시행일자": _pick(raw, "시행일자"),
        "분야": _pick(raw, "자치법규분야명"),
        "링크": _pick(raw, "자치법규상세링크", "법령상세링크"),
    }


def _norm_article(raw: Dict[str, Any]) -> Dict[str, Any]:
    code = _pick(raw, "조문번호")
    return {
        "조문": _jo_label(code),
        "조문여부": _pick(raw, "조문여부", default="Y"),
        "조제목": _strip_tags(_pick(raw, "조제목", "조문제목")),
        "조내용": _strip_tags(_pick(raw, "조내용", "조문내용")),
    }


def _err(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"오류: 법제처 API가 {e.response.status_code}를 반환했습니다. 인증키와 요청값을 확인하세요."
    if isinstance(e, httpx.TimeoutException):
        return "오류: 법제처 API 응답이 지연됩니다. 잠시 후 다시 시도하세요."
    return f"오류: {type(e).__name__}: {e}"


async def _fetch_articles(ordinance_id: str = "", mst: str = "") -> Dict[str, Any]:
    """자치법규 본문을 조문 단위로 정규화해서 반환."""
    data = await _get(SERVICE_URL, {"target": "ordin", "ID": ordinance_id, "MST": mst})
    arts = [_norm_article(a) for a in _deep_find_list(data, ["조", "조문"])]
    arts = [a for a in arts if a["조내용"] or a["조제목"]]
    real = [a for a in arts if a["조문여부"] == "Y"]

    meta = _find_dict_with(data, "자치법규명")
    return {
        "자치법규명": _strip_tags(_pick(meta, "자치법규명")),
        "지자체": _pick(meta, "지자체기관명"),
        "담당부서": _pick(meta, "담당부서명"),
        "전화번호": _pick(meta, "전화번호"),
        "종류": KND_REVERSE.get(_pick(meta, "자치법규종류"), _pick(meta, "자치법규종류")),
        "시행일자": _pick(meta, "시행일자"),
        "조문수": len(real),
        "조문": real,
        "편장절": [a["조내용"] for a in arts if a["조문여부"] == "N"],
    }


# ---------------------------------------------------------------- 입력 모델


class SearchInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    질의: str = Field(..., description="검색어. 예: '주민참여예산', '반려동물', '권한의 위임'", min_length=1)
    본문검색: bool = Field(default=False, description="True면 조문 본문까지 검색, False면 자치법규명만 검색")
    지자체: Optional[str] = Field(default=None, description="지자체명 부분일치 필터. 예: '광주광역시 서구', '수원시'")
    종류: OrdinanceKind = Field(default=OrdinanceKind.조례, description="자치법규 종류")
    개수: int = Field(default=20, description="가져올 최대 건수", ge=1, le=300)


class DetailInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    자치법규ID: Optional[str] = Field(default=None, description="검색 결과의 자치법규ID")
    MST: Optional[str] = Field(default=None, description="검색 결과의 MST(일련번호). ID가 없을 때 사용")
    조문키워드: Optional[str] = Field(
        default=None,
        description="지정하면 해당 키워드가 들어간 조문만 추려서 반환. 예: '위임', '보조금', '심의위원회'",
    )


class CompareInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    주제: str = Field(..., description="비교할 조례 주제어. 예: '주민참여예산', '노인 일자리'", min_length=1)
    지자체목록: List[str] = Field(
        ..., description="비교할 지자체명 목록. 예: ['광주광역시 서구', '수원시', '서울특별시 성동구']",
        min_length=2, max_length=6,
    )


class GapInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    주제: str = Field(..., description="분석할 조례 주제어", min_length=1)
    우리지자체: str = Field(..., description="기준이 되는 우리 지자체명. 예: '광주광역시 서구'")
    비교지자체목록: List[str] = Field(
        ..., description="비교 대상 지자체명 목록", min_length=1, max_length=6
    )


# ---------------------------------------------------------------- 도구


@mcp.tool(name="jachi_search_ordinances", annotations=_ann("자치법규 검색"))
async def jachi_search_ordinances(params: SearchInput) -> str:
    """전국 지자체의 조례·규칙을 검색한다.

    법규명 검색이 기본이고, 본문검색=True로 두면 조문 안 문구까지 훑는다.
    지자체를 지정하면 결과에서 해당 지자체만 추려준다.

    Returns:
        str: JSON. {"검색어","총건수","결과":[{자치법규ID,MST,자치법규명,지자체,종류,
             제개정구분,공포일자,시행일자,분야,링크}]}
    """
    try:
        collected: List[Dict[str, Any]] = []
        total = 0
        page = 1
        want = 10 ** 6 if params.지자체 else params.개수
        max_page = 12 if params.지자체 else 3
        while len(collected) < want and page <= max_page:
            data = await _get(
                SEARCH_URL,
                {
                    "target": "ordin",
                    "query": params.질의,
                    "search": 2 if params.본문검색 else 1,
                    "display": MAX_DISPLAY,
                    "page": page,
                    "knd": KND_CODES.get(params.종류.value),
                },
            )
            total = total or int(_pick(_find_dict_with(data, "totalCnt"), "totalCnt", default="0") or 0)
            rows = _deep_find_list(data, ["ordin", "law"])
            if not rows:
                break
            collected.extend(_norm_listing(r) for r in rows)
            if len(rows) < MAX_DISPLAY or len(collected) >= total:
                break
            page += 1

        후보기관명: List[str] = []
        if params.지자체:
            후보기관명 = sorted({c["지자체"] for c in collected})
            collected = [c for c in collected if region_match(c["지자체"], params.지자체)]

        matched = len(collected)
        collected = collected[: params.개수]
        return json.dumps(
            {
                "검색어": params.질의,
                "전국건수": total,
                "조건일치건수": matched,
                "반환건수": len(collected),
                "결과": collected,
                **({} if collected or not params.지자체 else {
                    "안내": f"'{params.지자체}' 표기로는 잡히지 않았습니다. "
                            "통합·개편으로 기관명이 바뀌었을 수 있으니 아래 실제 등재 기관명을 확인하세요.",
                    "검색결과에_등장한_기관명": 후보기관명[:60],
                }),
            },
            ensure_ascii=False, indent=2,
        )
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(name="jachi_get_ordinance", annotations=_ann("자치법규 본문 조회(조문 단위)"))
async def jachi_get_ordinance(params: DetailInput) -> str:
    """자치법규 본문을 조문 단위로 쪼개서 가져온다.

    조문키워드를 주면 그 말이 들어간 조문만 추려 반환하므로,
    '권한의 위임' 조항만 뽑는 식의 작업에 쓴다.

    Returns:
        str: JSON. {"자치법규명","지자체","담당부서","시행일자","조문수",
             "조문":[{조문번호,조문여부,조제목,조내용}]}
    """
    if not params.자치법규ID and not params.MST:
        return "오류: 자치법규ID 또는 MST 중 하나는 반드시 필요합니다. jachi_search_ordinances로 먼저 찾으세요."
    try:
        doc = await _fetch_articles(params.자치법규ID or "", params.MST or "")
        if params.조문키워드:
            kw = params.조문키워드
            doc["조문"] = [a for a in doc["조문"] if kw in a["조제목"] or kw in a["조내용"]]
            doc["조문수"] = len(doc["조문"])
            doc["필터"] = kw
        return json.dumps(doc, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        return _err(e)


async def _first_match(주제: str, 지자체: str) -> Optional[Dict[str, Any]]:
    """특정 지자체에서 주제에 맞는 조례 1건을 찾아 본문까지 가져온다."""
    raw = await jachi_search_ordinances(
        SearchInput(질의=주제, 지자체=지자체, 개수=1)
    )
    try:
        hit = json.loads(raw)["결과"]
    except Exception:  # noqa: BLE001
        return None
    if not hit:
        return None
    top = hit[0]
    doc = await _fetch_articles(top["자치법규ID"], top["MST"])
    doc["지자체"] = doc["지자체"] or top["지자체"]
    doc["링크"] = top["링크"]
    return doc


@mcp.tool(name="jachi_compare_ordinances", annotations=_ann("지자체 간 조례 조문 대비"))
async def jachi_compare_ordinances(params: CompareInput) -> str:
    """같은 주제의 조례를 여러 지자체에서 가져와 조제목 기준으로 나란히 붙인다.

    타 지자체가 같은 제도를 조례에 어떻게 담았는지 한 번에 보기 위한 도구.

    Returns:
        str: JSON. {"주제","대상":[{지자체,자치법규명,시행일자,조문수,링크}],
             "대비표":[{조제목, 지자체별:{지자체명:조내용}}]}
    """
    try:
        docs = await asyncio.gather(
            *[_first_match(params.주제, g) for g in params.지자체목록]
        )
        found = [(g, d) for g, d in zip(params.지자체목록, docs) if d]
        if not found:
            return f"오류: '{params.주제}' 주제로 해당 지자체들에서 조례를 찾지 못했습니다. 주제어를 더 일반적으로 바꿔보세요."

        table: Dict[str, Dict[str, str]] = {}
        for g, d in found:
            for a in d["조문"]:
                title = a["조제목"] or f"제{a['조문번호']}조"
                table.setdefault(title, {})[g] = a["조내용"]

        return json.dumps(
            {
                "주제": params.주제,
                "대상": [
                    {
                        "지자체": g,
                        "자치법규명": d["자치법규명"],
                        "시행일자": d["시행일자"],
                        "조문수": d["조문수"],
                        "링크": d.get("링크", ""),
                    }
                    for g, d in found
                ],
                "찾지못함": [g for g, d in zip(params.지자체목록, docs) if not d],
                "대비표": [{"조제목": k, "지자체별": v} for k, v in table.items()],
            },
            ensure_ascii=False, indent=2,
        )
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(name="jachi_find_gaps", annotations=_ann("우리 지자체에 없는 조항 발굴"))
async def jachi_find_gaps(params: GapInput) -> str:
    """우리 조례에 빠진 내용을 조(條)와 항(項) 두 층위에서 찾아낸다.

    이 서버의 핵심 도구. 결과는 확정된 결론이 아니라 검토 후보이며,
    세 갈래로 나뉘어 나온다.
      - 신규조항_후보: 우리 조례에 대응하는 조가 아예 없는 것
      - 유사조항_확인필요: 조제목이 비슷해 같은 조항일 수 있는 것(사람이 판단)
      - 항누락_후보: 조는 있으나 그 안의 항이 우리에게만 없는 것

    Returns:
        str: JSON. 각 후보에 보유지자체와 조문 원문이 함께 담긴다.
    """
    try:
        mine = await _first_match(params.주제, params.우리지자체)
        if not mine:
            return json.dumps({
                "결과": "우리 조례 없음",
                "안내": f"'{params.우리지자체}'에서 '{params.주제}' 조례를 찾지 못했습니다. "
                        "기관명 표기가 다를 수 있으니 jachi_find_region_name으로 확인하거나, "
                        "실제 미제정이라면 그 자체가 결론입니다.",
            }, ensure_ascii=False, indent=2)

        others = await asyncio.gather(
            *[_first_match(params.주제, g) for g in params.비교지자체목록]
        )
        pairs = [(g, d) for g, d in zip(params.비교지자체목록, others) if d]
        if not pairs:
            return f"오류: 비교 대상 지자체에서 '{params.주제}' 조례를 찾지 못했습니다."

        신규: Dict[str, Dict[str, Any]] = {}
        유사: Dict[str, Dict[str, Any]] = {}

        def _bucket_key(bucket: Dict[str, Dict[str, Any]], t: str) -> str:
            """지자체마다 표기가 조금씩 다른 같은 조제목을 한 칸으로 모은다."""
            for k in bucket:
                if title_sim(k, t) >= 0.72:
                    return k
            return t
        항누락: List[Dict[str, Any]] = []

        for g, d in pairs:
            지자체명 = d.get("지자체") or g
            for a in d["조문"]:
                t = a["조제목"]
                if not t:
                    continue
                best, best_art = 0.0, None
                for mi in mine["조문"]:
                    sim = title_sim(t, mi["조제목"])
                    if sim > best:
                        best, best_art = sim, mi

                if best >= 0.72 and best_art:          # 같은 조항 -> 항 단위로 파고든다
                    ours = split_hang(best_art["조내용"])
                    theirs = split_hang(a["조내용"])
                    if len(theirs) <= 1:
                        continue
                    missing = [h for h in theirs
                               if max([hang_sim(h, o) for o in ours] or [0.0]) < 0.5]
                    if missing:
                        항누락.append({
                            "우리조문": f"{best_art['조문']} {best_art['조제목']}",
                            "우리항수": len(ours),
                            "비교지자체": 지자체명,
                            "비교조문": f"{a['조문']} {a['조제목']}",
                            "비교항수": len(theirs),
                            "우리에게_없는_항": missing,
                        })
                elif best >= 0.30 and best_art:        # 헷갈림 -> 사람이 판단
                    e = 유사.setdefault(_bucket_key(유사, t), {
                        "타지자체_조제목": t,
                        "우리_유사조문": f"{best_art['조문']} {best_art['조제목']}",
                        "유사도": round(best, 2),
                        "보유지자체": [],
                        "조문내용": {},
                    })
                    if 지자체명 not in e["보유지자체"]:
                        e["보유지자체"].append(지자체명)
                    e["조문내용"][지자체명] = a["조내용"]
                else:                                   # 완전히 없음
                    e = 신규.setdefault(_bucket_key(신규, t), {
                        "조제목": t, "보유지자체": [], "조문내용": {},
                    })
                    if 지자체명 not in e["보유지자체"]:
                        e["보유지자체"].append(지자체명)
                    e["조문내용"][지자체명] = a["조내용"]

        return json.dumps({
            "분석방법": "조제목 유사도로 조 단위를 맞춘 뒤, 같은 조로 판정된 것은 항(項) 단위까지 대조했습니다.",
            "주의": "여기 나온 것은 결론이 아니라 검토 후보입니다. 조제목만 다르고 내용은 같은 경우가 "
                    "섞여 있으니 '유사조항_확인필요'는 반드시 원문을 읽고 판단하세요.",
            "주제": params.주제,
            "우리조례": {
                "지자체": mine["지자체"], "법규명": mine["자치법규명"],
                "시행일자": mine["시행일자"], "담당부서": mine["담당부서"], "조문수": mine["조문수"],
            },
            "비교대상": [{"지자체": d["지자체"] or g, "법규명": d["자치법규명"],
                       "시행일자": d["시행일자"], "조문수": d["조문수"]} for g, d in pairs],
            "찾지못한_지자체": [g for g, d in zip(params.비교지자체목록, others) if not d],
            "신규조항_후보": sorted(신규.values(), key=lambda x: -len(x["보유지자체"])),
            "유사조항_확인필요": sorted(유사.values(), key=lambda x: -x["유사도"]),
            "항누락_후보": 항누락,
        }, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(name="jachi_find_region_name", annotations=_ann("지자체 등재 기관명 확인"))
async def jachi_find_region_name(지자체명: str) -> str:
    """자치법규 DB에 실제로 등재된 기관명 표기를 확인한다.

    행정구역 통합·개편으로 기관명이 바뀌면 평소 쓰던 이름으로는 검색이 0건이 된다.
    검색이 비면 먼저 이 도구로 실제 표기를 확인할 것.

    Returns:
        str: JSON. {"질의","일치":[실제 기관명],"참고":[비슷한 기관명]}
    """
    try:
        data = await _get(SEARCH_URL, {"target": "ordin", "query": 지자체명, "display": MAX_DISPLAY})
        orgs = sorted({_pick(r, "지자체기관명") for r in _deep_find_list(data, ["ordin", "law"])})
        orgs = [o for o in orgs if o]
        hit = [o for o in orgs if region_match(o, 지자체명)]
        return json.dumps({
            "질의": 지자체명,
            "일치": hit,
            "참고": [o for o in orgs if o not in hit][:40],
        }, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ---------------------------------------------------------------- 셀프테스트


async def _selftest() -> None:
    print(f"[LAW_OC] {OC}")
    print("\n--- 1) 목록 API 원본 최상위 키 ---")
    data = await _get(SEARCH_URL, {"target": "ordin", "query": "주민참여예산", "display": 3})
    print(json.dumps(data, ensure_ascii=False)[:1200])

    rows = _deep_find_list(data, ["ordin", "law"])
    print(f"\n--- 2) 추출된 행 수: {len(rows)} ---")
    if rows:
        print("행 키:", list(rows[0].keys()))
        norm = _norm_listing(rows[0])
        print("정규화:", json.dumps(norm, ensure_ascii=False))

        print("\n--- 3) 본문 API 원본 ---")
        detail = await _get(
            SERVICE_URL, {"target": "ordin", "ID": norm["자치법규ID"], "MST": norm["MST"]}
        )
        print(json.dumps(detail, ensure_ascii=False)[:1200])
        arts = _deep_find_list(detail, ["조문"])
        print(f"\n조문 수: {len(arts)}")
        if arts:
            print("조문 키:", list(arts[0].keys()))
            print("정규화:", json.dumps(_norm_article(arts[0]), ensure_ascii=False))


class OCFromQuery:
    """원격 모드에서 ?oc=인증키 로 들어온 값을 그 요청에만 적용하는 미들웨어."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        token = None
        if scope.get("type") == "http":
            from urllib.parse import parse_qs

            qs = parse_qs((scope.get("query_string") or b"").decode())
            oc = (qs.get("oc") or qs.get("OC") or [""])[0].strip()
            if oc:
                token = _REQ_OC.set(oc)
        try:
            await self.app(scope, receive, send)
        finally:
            if token is not None:
                _REQ_OC.reset(token)


def _serve_http() -> None:
    import uvicorn

    app = OCFromQuery(mcp.streamable_http_app())
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        asyncio.run(_selftest())
    elif "--http" in sys.argv or os.environ.get("MCP_TRANSPORT") == "http":
        _serve_http()
    else:
        mcp.run()
