# -*- coding: utf-8 -*-
"""
의정소통 MCP (uijeong-mcp)
─────────────────────────────────────────────────────────────
국회도서관 지방의정포털(CLIK) Open API 기반 지방의회 회의록·의안 MCP 서버.
집행부 공무원이 의회 논의 이력을 근거 기반으로 파악해 성실한 답변과
약속 이행을 준비하도록 돕는 도구입니다.

[설계 원칙 — 의회관계 보호]
  1) 의원 개인 단위 성향·통계·순위 기능은 만들지 않는다. 집계는 의회·주제·연도·회의유형 단위만.
  2) 발언은 공개 회의록 원문에서만 가져오고 문서ID(docid)를 항상 붙인다.
  3) 근거가 없으면 '자료 없음'으로 답한다. 추정 답변 금지.
  4) 용어는 '제안·요구사항 / 답변 준비 / 관심사항'으로 쓴다.

[데이터 원천]
  - 지방의회 회의록  : https://clik.nanet.go.kr/openapi/minutes.do
  - 지방의회 의안정보: https://clik.nanet.go.kr/openapi/bill.do
  - 지방정책정보     : https://clik.nanet.go.kr/openapi/policyinfoList.do / policyinfoDetail.do
  ※ 호출 제한: 1회 100건, 인증키당 1일 1,000회 → 캐시와 호출 예산 관리 내장

[실행]
  연결 점검  : CLIK_API_KEY=발급키 python uijeong_mcp.py --check
  로컬(stdio) : CLIK_API_KEY=발급키 python uijeong_mcp.py
  원격(HTTP)  : CLIK_API_KEY=발급키 python uijeong_mcp.py --http   (엔드포인트 /mcp)
"""

from __future__ import annotations

import asyncio
import contextvars
import datetime as dt
import html as htmllib
import os
import re
import sys
import time
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

# ════════════════════════════════════════════════════════════
# 1. 설정
# ════════════════════════════════════════════════════════════
SERVER_VERSION = "1.1.0 (R0 증거 무결성: 무손실 커서·이어읽기·날짜검증·출처종류·안건경계·발언행위·약속분류)"
BASE = "https://clik.nanet.go.kr/openapi"
API_KEY = os.environ.get("CLIK_API_KEY", "").strip()
VERIFY_SSL = os.environ.get("CLIK_VERIFY_SSL", "1") != "0"
DAILY_LIMIT = int(os.environ.get("CLIK_DAILY_LIMIT", "1000"))
DAILY_RESERVE = 50            # 한도 근처에서 무거운 도구를 막기 위한 여유분
CACHE_TTL = 6 * 3600          # 6시간
CACHE_MAX = 800
MAX_DETAIL_DOCS = 10          # 한 번의 도구 호출에서 열어볼 회의록 최대 수

CLIK_ERRORS = {
    "ERROR01": "인증키가 유효하지 않습니다. CLIK_API_KEY 환경변수를 확인하세요.",
    "ERROR02": "응답결과타입(type) 값이 유효하지 않습니다.",
    "ERROR03": "요청구분(displayType) 값이 유효하지 않습니다.",
    "ERROR04": "요청시작위치(startCount) 값이 유효하지 않습니다.",
    "ERROR05": "리스트개수(listCount) 값이 유효하지 않습니다(최대 100).",
    "ERROR06": "검색타입(searchType) 값이 유효하지 않습니다.",
    "ERROR07": "검색어(searchKeyword) 값이 유효하지 않습니다.",
    "ERROR08": "문서ID(docid) 값이 유효하지 않습니다.",
    "ERROR09": "오늘 API 허용 호출량(1,000회)을 초과했습니다. 내일 다시 시도하거나 기관별 인증키를 쓰세요.",
    "ERROR10": "'사용불가' 상태 인증키입니다. 지방의정포털 마이페이지에서 키 상태를 확인하세요.",
    "ERROR11": "CLIK 서버 호출 중 오류가 발생했습니다. 잠시 후 다시 시도하세요.",
}

# ════════════════════════════════════════════════════════════
# 2. 지방의회 ID 표 (CLIK 리소스센터 공개 코드표 기준)
# ════════════════════════════════════════════════════════════
_COUNCIL_TABLE = """
002001 서울특별시의회|065001 전남광주통합특별시의회|062001 광주광역시의회(통합 전)|061001 전라남도의회(통합 전)
051001 부산광역시의회|053001 대구광역시의회|032001 인천광역시의회|042001 대전광역시의회|052001 울산광역시의회
044001 세종특별자치시의회|031001 경기도의회|033001 강원특별자치도의회|043001 충청북도의회|041001 충청남도의회
063001 전북특별자치도의회|054001 경상북도의회|055001 경상남도의회|064001 제주특별자치도의회
002002 서울특별시 강남구의회|002003 서울특별시 강동구의회|002004 서울특별시 강북구의회|002005 서울특별시 강서구의회
002006 서울특별시 관악구의회|002007 서울특별시 광진구의회|002008 서울특별시 구로구의회|002009 서울특별시 금천구의회
002010 서울특별시 노원구의회|002011 서울특별시 도봉구의회|002012 서울특별시 동대문구의회|002013 서울특별시 동작구의회
002014 서울특별시 마포구의회|002015 서울특별시 서대문구의회|002016 서울특별시 서초구의회|002017 서울특별시 성동구의회
002018 서울특별시 성북구의회|002019 서울특별시 송파구의회|002020 서울특별시 양천구의회|002021 서울특별시 영등포구의회
002022 서울특별시 용산구의회|002023 서울특별시 은평구의회|002024 서울특별시 종로구의회|002025 서울특별시 중구의회
002026 서울특별시 중랑구의회
061002 전남광주통합특별시 강진군의회|061003 전남광주통합특별시 고흥군의회|061004 전남광주통합특별시 곡성군의회
062002 전남광주통합특별시 광산구의회|061005 전남광주통합특별시 광양시의회|061006 전남광주통합특별시 구례군의회
061007 전남광주통합특별시 나주시의회|062003 전남광주통합특별시 남구의회|061008 전남광주통합특별시 담양군의회
062004 전남광주통합특별시 동구의회|061009 전남광주통합특별시 목포시의회|061010 전남광주통합특별시 무안군의회
061011 전남광주통합특별시 보성군의회|062005 전남광주통합특별시 북구의회|062006 전남광주통합특별시 서구의회
061012 전남광주통합특별시 순천시의회|061013 전남광주통합특별시 신안군의회|061014 전남광주통합특별시 여수시의회
061015 전남광주통합특별시 영광군의회|061016 전남광주통합특별시 영암군의회|061017 전남광주통합특별시 완도군의회
061018 전남광주통합특별시 장성군의회|061019 전남광주통합특별시 장흥군의회|061020 전남광주통합특별시 진도군의회
061021 전남광주통합특별시 함평군의회|061022 전남광주통합특별시 해남군의회|061023 전남광주통합특별시 화순군의회
051002 부산광역시 강서구의회|051003 부산광역시 금정구의회|051004 부산광역시 기장군의회|051005 부산광역시 남구의회
051006 부산광역시 동구의회|051007 부산광역시 동래구의회|051008 부산광역시 부산진구의회|051009 부산광역시 북구의회
051010 부산광역시 사상구의회|051011 부산광역시 사하구의회|051012 부산광역시 서구의회|051013 부산광역시 수영구의회
051014 부산광역시 연제구의회|051015 부산광역시 영도구의회|051016 부산광역시 중구의회|051017 부산광역시 해운대구의회
053010 대구광역시 군위군의회|053002 대구광역시 남구의회|053003 대구광역시 달서구의회|053004 대구광역시 달성군의회
053005 대구광역시 동구의회|053006 대구광역시 북구의회|053007 대구광역시 서구의회|053008 대구광역시 수성구의회
053009 대구광역시 중구의회
032002 인천광역시 강화군의회|032015 인천광역시 검단구의회|032003 인천광역시 계양구의회|032005 인천광역시 남동구의회
032004 인천광역시 미추홀구의회|032007 인천광역시 부평구의회|032008 인천광역시 서해구의회|032009 인천광역시 연수구의회
032013 인천광역시 영종구의회|032010 인천광역시 옹진군의회|032012 인천광역시 제물포구의회
042002 대전광역시 대덕구의회|042003 대전광역시 동구의회|042004 대전광역시 서구의회|042005 대전광역시 유성구의회
042006 대전광역시 중구의회
052002 울산광역시 남구의회|052003 울산광역시 동구의회|052004 울산광역시 북구의회|052005 울산광역시 울주군의회
052006 울산광역시 중구의회
031002 경기도 가평군의회|031003 경기도 고양시의회|031004 경기도 과천시의회|031005 경기도 광명시의회
031006 경기도 광주시의회|031007 경기도 구리시의회|031008 경기도 군포시의회|031009 경기도 김포시의회
031010 경기도 남양주시의회|031011 경기도 동두천시의회|031012 경기도 부천시의회|031013 경기도 성남시의회
031014 경기도 수원시의회|031015 경기도 시흥시의회|031016 경기도 안산시의회|031017 경기도 안성시의회
031018 경기도 안양시의회|031019 경기도 양주시의회|031020 경기도 양평군의회|031021 경기도 여주시의회
031022 경기도 연천군의회|031023 경기도 오산시의회|031024 경기도 용인시의회|031025 경기도 의왕시의회
031026 경기도 의정부시의회|031027 경기도 이천시의회|031028 경기도 파주시의회|031029 경기도 평택시의회
031030 경기도 포천시의회|031031 경기도 하남시의회|031032 경기도 화성시의회
033002 강원특별자치도 강릉시의회|033003 강원특별자치도 고성군의회|033004 강원특별자치도 동해시의회
033005 강원특별자치도 삼척시의회|033006 강원특별자치도 속초시의회|033007 강원특별자치도 양구군의회
033008 강원특별자치도 양양군의회|033009 강원특별자치도 영월군의회|033010 강원특별자치도 원주시의회
033011 강원특별자치도 인제군의회|033012 강원특별자치도 정선군의회|033013 강원특별자치도 철원군의회
033014 강원특별자치도 춘천시의회|033015 강원특별자치도 태백시의회|033016 강원특별자치도 평창군의회
033017 강원특별자치도 홍천군의회|033018 강원특별자치도 화천군의회|033019 강원특별자치도 횡성군의회
043002 충청북도 괴산군의회|043003 충청북도 단양군의회|043004 충청북도 보은군의회|043005 충청북도 영동군의회
043006 충청북도 옥천군의회|043007 충청북도 음성군의회|043008 충청북도 제천시의회|043009 충청북도 증평군의회
043010 충청북도 진천군의회|043012 충청북도 청주시의회|043013 충청북도 충주시의회
041002 충청남도 계룡시의회|041003 충청남도 공주시의회|041004 충청남도 금산군의회|041005 충청남도 논산시의회
041006 충청남도 당진시의회|041007 충청남도 보령시의회|041008 충청남도 부여군의회|041009 충청남도 서산시의회
041010 충청남도 서천군의회|041011 충청남도 아산시의회|041012 충청남도 예산군의회|041013 충청남도 천안시의회
041014 충청남도 청양군의회|041015 충청남도 태안군의회|041016 충청남도 홍성군의회
063002 전북특별자치도 고창군의회|063003 전북특별자치도 군산시의회|063004 전북특별자치도 김제시의회
063005 전북특별자치도 남원시의회|063006 전북특별자치도 무주군의회|063007 전북특별자치도 부안군의회
063008 전북특별자치도 순창군의회|063009 전북특별자치도 완주군의회|063010 전북특별자치도 익산시의회
063011 전북특별자치도 임실군의회|063012 전북특별자치도 장수군의회|063013 전북특별자치도 전주시의회
063014 전북특별자치도 정읍시의회|063015 전북특별자치도 진안군의회
054002 경상북도 경산시의회|054003 경상북도 경주시의회|054004 경상북도 고령군의회|054005 경상북도 구미시의회
054007 경상북도 김천시의회|054008 경상북도 문경시의회|054009 경상북도 봉화군의회|054010 경상북도 상주시의회
054011 경상북도 성주군의회|054012 경상북도 안동시의회|054013 경상북도 영덕군의회|054014 경상북도 영양군의회
054015 경상북도 영주시의회|054016 경상북도 영천시의회|054017 경상북도 예천군의회|054018 경상북도 울릉군의회
054019 경상북도 울진군의회|054020 경상북도 의성군의회|054021 경상북도 청도군의회|054022 경상북도 청송군의회
054023 경상북도 칠곡군의회|054024 경상북도 포항시의회
055002 경상남도 거제시의회|055003 경상남도 거창군의회|055004 경상남도 고성군의회|055005 경상남도 김해시의회
055006 경상남도 남해군의회|055007 경상남도 밀양시의회|055008 경상남도 사천시의회|055009 경상남도 산청군의회
055010 경상남도 양산시의회|055011 경상남도 의령군의회|055012 경상남도 진주시의회|055013 경상남도 창녕군의회
055019 경상남도 창원시의회|055014 경상남도 통영시의회|055015 경상남도 하동군의회|055016 경상남도 함안군의회
055017 경상남도 함양군의회|055018 경상남도 합천군의회
"""

COUNCILS: dict[str, str] = {}
for _chunk in _COUNCIL_TABLE.replace("\n", "|").split("|"):
    _chunk = _chunk.strip()
    if _chunk:
        _id, _nm = _chunk.split(" ", 1)
        COUNCILS[_id] = _nm

# 사용자가 흔히 쓰는 옛 이름 → 현 명칭 치환
_REGION_ALIASES = [
    ("광주광역시", "전남광주통합특별시"), ("전라남도", "전남광주통합특별시"),
    ("광주시", "전남광주통합특별시"), ("광주", "전남광주통합특별시"), ("전남", "전남광주통합특별시"),
    ("서울시", "서울특별시"), ("서울", "서울특별시"), ("부산시", "부산광역시"), ("부산", "부산광역시"),
    ("대구시", "대구광역시"), ("대구", "대구광역시"), ("인천시", "인천광역시"), ("인천", "인천광역시"),
    ("대전시", "대전광역시"), ("대전", "대전광역시"), ("울산시", "울산광역시"), ("울산", "울산광역시"),
    ("경기", "경기도"), ("강원도", "강원특별자치도"), ("강원", "강원특별자치도"), ("충북", "충청북도"),
    ("충남", "충청남도"), ("전라북도", "전북특별자치도"), ("전북", "전북특별자치도"), ("경북", "경상북도"),
    ("경남", "경상남도"), ("제주도", "제주특별자치도"), ("제주", "제주특별자치도"), ("세종시", "세종특별자치시"),
]


def _norm(s: str) -> str:
    return re.sub(r"[\s()·]", "", s or "")


def resolve_council(query: str) -> list[tuple[str, str]]:
    """의회명(또는 ID)을 받아 (ID, 정식명) 후보 목록을 돌려준다."""
    q = (query or "").strip()
    if not q:
        return []
    if re.fullmatch(r"\d{6}", q):
        return [(q, COUNCILS.get(q, "(코드표에 없는 ID)"))]
    # 1) 입력 그대로 부분일치
    nq = _norm(q).replace("의회", "")
    hits = [(i, n) for i, n in COUNCILS.items() if nq and nq in _norm(n).replace("의회", "")]
    if hits:
        return _prefer_current(hits)
    # 2) 옛 명칭 치환 후 재시도(첫 토큰만 치환)
    parts = q.split()
    for old, new in _REGION_ALIASES:
        if parts and (parts[0] == old or (len(parts) == 1 and q.startswith(old))):
            rest = " ".join(parts[1:]) if parts[0] == old else q[len(old):]
            cand = _norm(new + rest).replace("의회", "")
            hits = [(i, n) for i, n in COUNCILS.items() if cand in _norm(n).replace("의회", "")]
            if hits:
                return _prefer_current(hits)
    # 3) 마지막 토큰(예: '서구')만으로 후보 제시
    tail = _norm(parts[-1]).replace("의회", "") if parts else ""
    if len(tail) >= 2:
        return [(i, n) for i, n in COUNCILS.items() if tail in _norm(n)][:15]
    return []


def _prefer_current(hits: list[tuple[str, str]]) -> list[tuple[str, str]]:
    # '(통합 전)' 명칭은 뒤로
    return sorted(hits, key=lambda x: ("통합 전" in x[1], len(x[1])))


def pick_council(query: Optional[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(id, name, error_message). 후보가 여러 개면 정확일치를 고르고, 없으면 되묻는다."""
    if not query:
        return None, None, None
    c = resolve_council(query)
    if not c:
        return None, None, f"'{query}'에 해당하는 지방의회를 찾지 못했습니다. council_find_council 도구로 먼저 확인하세요."
    if len(c) == 1:
        return c[0][0], c[0][1], None
    nq = _norm(query).replace("의회", "")
    exact = [x for x in c if _norm(x[1]).replace("의회", "") == nq]
    if len(exact) == 1:
        return exact[0][0], exact[0][1], None
    cands = ", ".join(f"{n}({i})" for i, n in c[:8])
    return None, None, f"'{query}'에 해당하는 의회가 여러 곳입니다: {cands}. 시·도명을 붙이거나 ID로 다시 지정하세요."


# ════════════════════════════════════════════════════════════
# 3. CLIK API 클라이언트 (캐시 + 일일 호출 예산)
# ════════════════════════════════════════════════════════════
class ClikError(Exception):
    pass


class ClikClient:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, Any]] = {}
        self._day = dt.date.today()
        self.calls_today = 0
        self._client: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()

    def _roll_day(self) -> None:
        if dt.date.today() != self._day:
            self._day = dt.date.today()
            self.calls_today = 0

    def budget_left(self) -> int:
        self._roll_day()
        return DAILY_LIMIT - self.calls_today

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(25.0, connect=10.0),
                verify=VERIFY_SSL,
                headers={"User-Agent": "uijeong-mcp/1.0 (+local government council research)"},
                follow_redirects=True,
            )
        return self._client

    async def _raw_get(self, path: str, params: dict) -> Any:
        client = await self._http()
        r = await client.get(f"{BASE}/{path}", params=params)
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            raise ClikError("CLIK 응답이 JSON이 아닙니다(점검 중이거나 키 오류일 수 있음). 원문 앞부분: " + r.text[:200])

    async def get(self, path: str, **params: Any) -> dict:
        if not API_KEY:
            raise ClikError(
                "CLIK_API_KEY가 설정되지 않았습니다. 국회도서관 지방의정포털(clik.nanet.go.kr) 로그인 → "
                "Open API → 인증키 신청 후, 서버 환경변수 CLIK_API_KEY에 넣어주세요."
            )
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        ck = path + "?" + "&".join(f"{k}={clean[k]}" for k in sorted(clean))
        hit = self._cache.get(ck)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
        self._roll_day()
        if self.calls_today >= DAILY_LIMIT:
            raise ClikError(CLIK_ERRORS["ERROR09"])
        async with self._lock:
            self.calls_today += 1
        try:
            data = await self._raw_get(path, {"key": API_KEY, "type": "json", **clean})
        except httpx.HTTPError as e:
            raise ClikError(f"CLIK 서버 연결 실패: {type(e).__name__}. 잠시 후 다시 시도하세요.")
        obj = _unwrap(data)
        code = str(obj.get("RESULT_CODE", "SUCCESS"))
        if code != "SUCCESS":
            raise ClikError(CLIK_ERRORS.get(code, f"CLIK 오류 {code}: {obj.get('RESULT_MESSAGE', '')}"))
        if len(self._cache) >= CACHE_MAX:
            for k in sorted(self._cache, key=lambda k: self._cache[k][0])[: CACHE_MAX // 4]:
                self._cache.pop(k, None)
        self._cache[ck] = (time.time(), obj)
        return obj


def _unwrap(data: Any) -> dict:
    if isinstance(data, list):
        data = data[0] if data else {}
    return data if isinstance(data, dict) else {}


def _rows(obj: dict) -> list[dict]:
    rows = obj.get("LIST") or []
    out = []
    for r in rows:
        if isinstance(r, dict) and "ROW" in r and isinstance(r["ROW"], dict):
            out.append(r["ROW"])
        elif isinstance(r, dict):
            out.append(r)
    return out


clik = ClikClient()

# ════════════════════════════════════════════════════════════
# 4. 회의록 파싱: HTML → 발언(turn) 목록
# ════════════════════════════════════════════════════════════
_STAFF = ("전문위원", "사무국장", "사무과장", "의사팀장", "의사담당", "의정팀장", "속기", "입법조사", "의회사무국")
_CHAIR = ("의장", "부의장", "위원장", "부위원장", "임시의장", "임시위원장", "의장직무대리", "위원장직무대리")
_EXEC = (
    "구청장", "시장", "군수", "도지사", "지사", "교육감", "교육장", "부구청장", "부시장", "부군수", "부지사",
    "국장", "실장", "과장", "소장", "단장", "관장", "담당관", "팀장", "동장", "읍장", "면장", "원장",
    "본부장", "사장", "이사장", "센터장", "감사관", "주무관", "계장", "사무관", "대표이사", "처장", "관리관",
)
_END_LABELS = ("출석", "출사무국", "참석", "불출석", "결석", "배석", "회의록서명", "서명의원", "청가", "출장", "속기사")


def _classify(label: str) -> str:
    lab = label.replace(" ", "")
    if any(lab.startswith(e) for e in _END_LABELS):
        return "end"
    if any(s in lab for s in _STAFF):
        return "staff"
    head = label.split()[0] if label.split() else lab
    if any(head == c or head.endswith(c) and not head.endswith("의원") for c in _CHAIR) and not any(
        e in head for e in ("청장", "시장", "군수", "지사")
    ):
        return "chair"
    if "의원" in lab or re.search(r"(^|[^전문])위원($|\s)", label):
        return "member"
    if any(e in lab for e in _EXEC):
        return "executive"
    return "other"


ROLE_KO = {"chair": "의장·위원장", "member": "의원", "executive": "집행부", "staff": "의회사무국", "other": "기타"}


def _html_to_marked_text(src: str) -> str:
    s = src or ""
    s = re.sub(r"(?is)<script.*?</script>|<style.*?</style>|<!--.*?-->", " ", s)
    s = re.sub(r"(?i)<\s*b\b[^>]*>|<\s*strong\b[^>]*>", "\x01", s)
    s = re.sub(r"(?i)<\s*/\s*b\s*>|<\s*/\s*strong\s*>", "\x02", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|<p\b[^>]*>|</div>|</tr>|<hr[^>]*>|</?spk[^>]*>|</td>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = htmllib.unescape(s).replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return s


_NAMEISH = re.compile(r"^[가-힣]{2,4}$")
_SPEECH_WORDS = {"다음은", "그러면", "이어서", "먼저", "네", "예", "감사합니다", "존경하는", "의사일정", "좌석을", "의석을", "성원이"}


def parse_turns(minutes_html: str) -> list[dict]:
    """회의록 HTML을 발언 단위로 분해. 반환: [{idx, label, role, text}]"""
    s = _html_to_marked_text(minutes_html)
    # 콜론 표기 정규화: "○김철수 위원:" "○미래전략과장 홍길동 :" "<b>○위원장 이동식</b>:" → 콜론 제거
    s = re.sub(r"(○\s*[^\s○:：\x01\x02]{1,20}(?:[ \t]+[^\s○:：\x01\x02]{1,12})?)[ \t]*[:：]", r"\1 ", s)
    s = re.sub(r"\x02[ \t]*[:：]", "\x02 ", s)
    if "○" not in s:
        # ○ 기호가 전혀 없는 회의록: 줄머리 "이름 위원:" / "직함 이름:" 표기를 ○ 표기로 바꿔 같은 규칙으로 처리
        s = re.sub(r"(?m)^[ \t]*([가-힣]{2,4}[ \t]*(?:의원|위원)|[가-힣]{1,14}(?:위원장|의장|청장|시장|군수|국장|과장|팀장|실장|소장|단장|전문위원|담당관)[ \t]+[가-힣]{2,4})[ \t]*[:：]",
                   r"○\1 ", s)
    marks = list(re.finditer(r"\x01\s*○\s*([^\x01\x02]{1,40}?)\s*\x02", s))
    segments: list[tuple[str, int, int]] = []
    if marks:
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(s)
            segments.append((m.group(1), m.end(), end))
    else:
        # 굵은 글씨 표식이 없는 회의록: 줄머리 ○ 기준 분해
        s2 = s.replace("\x01", "").replace("\x02", "")
        lines = list(re.finditer(r"(?:(?<=\s)|(?<=^)|(?<=[.?!」)]))○\s*(\S+)(?:\s+(\S+))?", s2))
        s = s2
        for i, m in enumerate(lines):
            first, second = m.group(1), m.group(2) or ""
            if second and ((_NAMEISH.match(second) and second not in _SPEECH_WORDS) or second in ("의원", "위원")):
                label, start = f"{first} {second}", m.end()
            else:
                label, start = first, m.end(1)
            end = lines[i + 1].start() if i + 1 < len(lines) else len(s)
            segments.append((label, start, end))

    turns: list[dict] = []
    agenda = 0
    for label, a, b in segments:
        label = re.sub(r"\s+", " ", label.replace("\x01", "").replace("\x02", "")).strip()
        text0 = s[a:b].replace("\x01", "").replace("\x02", "").lstrip()
        if len(label.split()) == 1 and not _NAMEISH.match(label):
            mname = re.match(r"([가-힣]{2,4})\s", text0)
            if mname and mname.group(1) not in _SPEECH_WORDS:
                label = f"{label} {mname.group(1)}"
                a = s.find(mname.group(1), a) + len(mname.group(1))
        role = _classify(label)
        if role == "end":
            break
        text = s[a:b].replace("\x01", "").replace("\x02", "")
        text = re.sub(r"\n\s*\n+", "\n", text)
        text = re.sub(r"[ \t]*\n[ \t]*", " ", text).strip()
        # 안건 머리글·맨위로 등 잡음 제거
        text = re.sub(r"\s*맨위로\s*", " ", text).strip()
        header = re.search(r"\s*\d+\.\s*[^.。]{2,150}?(의\s*건|조례안|규칙안|승인안|동의안|결의안|건의안|계획안|예산안|업무보고|소관)\s*$", text)
        if header:
            text = text[: header.start()].strip()
        if not text:
            if header:
                agenda += 1
            continue
        if role == "chair" and _AGENDA_START.search(text):
            agenda += 1
        act = classify_act(role, label, text)
        turns.append({"idx": len(turns), "label": label, "role": role, "text": text, "agenda": agenda, "act": act})
        if (role == "chair" and _AGENDA_CLOSE.search(text)) or header:
            agenda += 1
    return turns


# 안건 시작·종료 신호(사회자 발언)
_AGENDA_START = re.compile(r"(상정합니다|상정하겠습니다|상정하도록|안건을\s*상정|의사일정\s*제\s*\d+\s*항|다음은\s*[^.?!]{0,30}(소관|업무보고|안건|순서|질의|제안설명|보고))")
_AGENDA_CLOSE = re.compile(r"(질의\s*(답변)?\s*(을|를)?\s*(모두\s*)?(종결|마치)|가결되었음을\s*선포|의결되었음을\s*선포|산회를\s*선포|폐회를\s*선포|이상으로\s*[^.?!]{0,30}(마치|마치겠))")
_PROCEDURAL = re.compile(r"(성원|개의|개회|산회|정회|속개|상정|선포|의석|질의하실|질의해\s*주|발언하여\s*주|발언해\s*주|의견이?\s*(있|없)|이의\s*(가|는)?\s*없|계십니까|없습니까|다음은|의결|종결|수고하셨|순서|진행하겠|마치겠|배부해\s*드린)")
_QMARK = re.compile(r"(\?|습니까|십니까|나요|는지요|궁금|설명해\s*주시|답변해\s*주시|말씀해\s*주시|어떻게\s*(되|생각|하실)|왜\s)")
_REVIEW_LABEL = ("전문위원", "입법조사")
_REPORT_OPEN = re.compile(r"^[^.?!]{0,40}(업무보고|제안설명|보고를?\s*드리|설명을?\s*드리|보고드리도록|보고드리겠습니다|설명드리겠습니다)")


def classify_act(role: str, label: str, text: str) -> str:
    """발언자 역할과 별도로 발언행위를 분류: question·procedural·answer_candidate·report·review·other"""
    if role == "staff" and any(x in label for x in _REVIEW_LABEL):
        return "review"
    if role == "chair":
        for sent in split_sentences(text):
            if _QMARK.search(sent) and not _PROCEDURAL.search(sent) and len(sent) >= 12:
                return "question"
        return "procedural"
    if role == "member":
        if len(text) < 12 and not _QMARK.search(text):
            return "other"
        return "question"
    if role in ("executive", "staff"):
        return "report" if _REPORT_OPEN.search(text) else "answer_candidate"
    return "other"


_FIVE_MIN = re.compile(r"5\s*분\s*자유\s*발언")
_QUESTION_TIME = re.compile(r"(구정|시정|군정|도정|교육행정)\s*질문")


def speech_context(turns: list[dict], i: int) -> str:
    """해당 발언 앞의 사회자 발언을 거슬러 보며 발언유형(5분자유발언·구정질문) 추정."""
    for j in range(i - 1, max(-1, i - 15), -1):
        t = turns[j]
        if t["role"] != "chair":
            continue
        if _FIVE_MIN.search(t["text"]):
            return "5분자유발언"
        if _QUESTION_TIME.search(t["text"]):
            return "구·시정질문"
        if re.search(r"(의사일정\s*제\s*\d+\s*항|상정합니다|안건을\s*상정)", t["text"]):
            return ""
    return ""


def meeting_kind(mtgnm: str) -> str:
    m = mtgnm or ""
    if "감사" in m:
        return "행정사무감사·조사"
    if "예산" in m or "결산" in m:
        return "예산·결산"
    if "본회의" in m:
        return "본회의"
    if "특별" in m:
        return "특별위원회"
    if "위원회" in m:
        return "상임위원회"
    return "기타"


_COMMIT = re.compile(
    r"(검토하겠습니다|검토토록\s*하겠습니다|검토해\s*보겠습니다|추진하겠습니다|추진토록\s*하겠습니다|반영하겠습니다|"
    r"반영토록\s*하겠습니다|반영될\s*수\s*있도록|조치하겠습니다|조치토록\s*하겠습니다|개선하겠습니다|"
    r"보고드리겠습니다|보고\s*드리겠습니다|협의하겠습니다|마련하겠습니다|확대하겠습니다|점검하겠습니다|"
    r"시정하겠습니다|노력하겠습니다|제출하겠습니다|자료를\s*드리겠습니다|챙기겠습니다|해결하겠습니다)"
)


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[다요까죠])[.?!]\s+|(?<=습니다)\s+(?=[가-힣A-Za-z0-9“\"(])", text)
    return [p.strip() for p in parts if p and p.strip()]


def snippet(text: str, kw: Optional[str], width: int = 180) -> str:
    if not text:
        return ""
    if not kw:
        return text[: width * 2] + ("…" if len(text) > width * 2 else "")
    pos = text.find(kw)
    if pos < 0:
        return text[: width * 2] + ("…" if len(text) > width * 2 else "")
    a, b = max(0, pos - width), min(len(text), pos + len(kw) + width)
    return ("…" if a else "") + text[a:b] + ("…" if b < len(text) else "")


ANSWER_ROLES = ("executive", "staff")
_ANSWER_CUE = re.compile(r"(답변|말씀|설명|대답)")


def build_qa_pairs(turns: list[dict], kw: Optional[str] = None) -> list[dict]:
    """질의 발언 → 같은 안건 안에서 이어지는 집행부 답변을 묶는다.
    · 안건이 바뀌거나 사회자가 안건을 넘기면 연결을 끊는다(다른 부서 보고가 붙지 않도록)
    · 전문위원 검토보고, 사회자 안내 뒤 새로 시작된 업무보고·제안설명은 답변으로 보지 않는다
    · 위원장도 실질 질문이면 질의로 보존한다"""
    pairs = []
    n = len(turns)
    for i, t in enumerate(turns):
        if t.get("act") != "question":
            continue
        answers = []
        j = i + 1
        while j < n:
            u = turns[j]
            if u.get("agenda", 0) != t.get("agenda", 0):
                break
            if u["act"] == "question" and u["role"] in ("member", "chair"):
                break
            if u["role"] == "chair":
                if _AGENDA_START.search(u["text"]) or _AGENDA_CLOSE.search(u["text"]):
                    break
                if _ANSWER_CUE.search(u["text"]) or len(u["text"]) < 60:
                    j += 1
                    continue
                break
            if u["role"] in ANSWER_ROLES:
                if u["act"] == "review":
                    j += 1
                    continue
                prev = turns[j - 1]
                if u["act"] == "report" and prev["role"] == "chair" and not _ANSWER_CUE.search(prev["text"]):
                    break
                answers.append(u)
            j += 1
        kind = speech_context(turns, i)
        blob = t["text"] + " ".join(a["text"] for a in answers)
        if (not kw or kw in blob) and (answers or kind == "5분자유발언" or len(t["text"]) >= 300):
            pairs.append({"q": t, "answers": answers, "kind": kind, "agenda": t.get("agenda", 0)})
    return pairs


# ───────── 후속조치 후보 분류 (E03·E04·E05) ─────────
_COMMIT_VERB = re.compile(
    r"(검토|추진|반영|조치|개선|보고|협의|마련|확대|점검|시정|노력|제출|드리|챙기|해결|시행|설치|정비|확보|파악|살펴|추가|보완|강화|진행)"
    r"[가-힣\s]{0,8}(하겠습니다|겠습니다|토록\s*하겠습니다|도록\s*하겠습니다|해\s*보겠습니다|될\s*수\s*있도록)"
)
_NEG = re.compile(r"(않겠습니다|않도록\s*하겠습니다\s*$|어렵습니다|곤란합니다|불가합니다|하지\s*않|안\s*하겠습니다)")
_PAST = re.compile(r"(하겠다고\s*(했|말씀|답변|약속)|했었습니다|하였었습니다|겠다고\s*하셨)")
_NOW_REPORT = re.compile(r"(업무보고|보고|설명|제안설명|답변)\s*(을|를)?\s*(드리|올리)(겠|도록)")
_LATER = re.compile(r"(추후|향후|별도로|서면|나중에|다음\s*회기|까지|정리해서|정리하여|확인해서|확인하여|파악해서|파악하여|결과를|결과는|자료를|자료로)")
_COND = re.compile(r"(되면|된다면|될\s*경우|경우에는|경우에|한다면|전제로|여건이\s*되|가능하다면|가능하면|확보되|허락한다면|통과되면)")
_DEADLINE = re.compile(r"(\d{1,2}\s*월\s*\d{1,2}\s*일|\d{1,2}\s*월\s*(초|중순|말)?|연내|올해\s*안|연말|상반기|하반기|내년(도)?(\s*\d{1,2}\s*월)?|다음\s*(달|주|회기)|추경|추가경정|본예산)\s*(까지|중에?|안에|내에|에)?")


def classify_commitment(sentence: str) -> Optional[dict]:
    """집행부 답변 문장에서 후속조치 '후보'를 분류. 약속이 아니면 None.
    반환: {type: 자료제출·검토의사·조건부 추진·시행의사, condition, deadline}
    ※ 기한의 연도는 원문에 없으면 붙이지 않는다. 이행 여부는 판정하지 않는다."""
    x = sentence.strip()
    if not _COMMIT_VERB.search(x):
        return None
    if _NEG.search(x) or _PAST.search(x):
        return None
    if _NOW_REPORT.search(x) and not _LATER.search(x):
        return None                                   # 지금 하는 보고·설명은 약속이 아님
    cond = _COND.search(x)
    dl = _DEADLINE.search(x)
    if re.search(r"(자료|제출|서면)", x):
        typ = "자료제출"
    elif cond:
        typ = "조건부 추진"
    elif re.search(r"(검토|협의|살펴|파악)", x):
        typ = "검토의사"
    else:
        typ = "시행의사"
    return {"type": typ, "condition": x[max(0, cond.start() - 20): cond.end() + 1].strip() if cond else None,
            "deadline": dl.group(0).strip() if dl else None}


def commitment_tag(info: dict) -> str:
    tag = info["type"]
    if info.get("condition"):
        tag += f"·조건 '{info['condition']}'"
    tag += f"·기한 {info['deadline']}" if info.get("deadline") else "·기한 미상"
    return f"[{tag}]"


_COMMIT = type("_CommitCompat", (), {"search": staticmethod(classify_commitment)})()


_DEPT_SUFFIX = re.compile(r"(과장|팀장|국장|실장|소장|단장|센터장|담당관|원장|과|팀|국|실|소|단|센터)$")


def dept_match(answerer: Optional[str], label: str) -> bool:
    """'미래전략과장' 필터가 '미래전략팀장'(대리답변)·'미래전략과장'을 함께 찾도록 부서 어간으로도 비교."""
    if not answerer:
        return True
    a = answerer.replace(" ", "")
    lab = label.replace(" ", "")
    if a in lab:
        return True
    stem = _DEPT_SUFFIX.sub("", a)
    return len(stem) >= 2 and stem in lab


# ════════════════════════════════════════════════════════════
# 5. 공통 조회 헬퍼
# ════════════════════════════════════════════════════════════
MINUTES_SEARCH = {"전체": "ALL", "내용": "MINTS_HTML", "안건": "MTR_SJ", "위원회": "PRMPST_CMIT_NM", "의회명": "RASMBLY_NM"}
BILL_SEARCH = {"전체": "ALL", "제목": "BI_SJ", "제안자": "PROPSR", "요지": "BI_OUTLINE"}
BILL_STATUS = {
    "접수": "BIR101", "원안가결": "BIR201", "수정가결": "BIR202", "가결(대안)": "BIR203", "의결(원안)": "BIR204",
    "의결(수정)": "BIR205", "본회의직상정": "BIR206", "미상정": "BIR207", "승인": "BIR208", "동의": "BIR209",
    "채택": "BIR210", "부결(폐기)": "BIR211", "폐기": "BIR212", "철회": "BIR213", "기타(처리)": "BIR214",
    "보류": "BIR215", "재회부": "BIR216", "미처리(계류)": "BIR301", "기타": "BIR999",
}


def _ymd(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    d = re.sub(r"\D", "", s)
    if len(d) == 6:
        d += "01"
    return d if len(d) == 8 else None


class InvalidInput(Exception):
    pass


def check_dates(date_from: Optional[str], date_to: Optional[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """형식·실존 날짜·역전 기간을 조회 전에 검사. 반환 (시작, 종료, 오류문구)"""
    vals: list[Optional[str]] = []
    for label, v in (("시작일", date_from), ("종료일", date_to)):
        if not v:
            vals.append(None)
            continue
        d = re.sub(r"\D", "", str(v))
        if len(d) == 6:
            d += "01"
        if len(d) != 8:
            return None, None, f"상태: INVALID_INPUT\n{label} '{v}' 형식을 읽을 수 없습니다. YYYY-MM-DD로 입력하세요."
        try:
            dt.datetime.strptime(d, "%Y%m%d")
        except ValueError:
            return None, None, f"상태: INVALID_INPUT\n{label} '{v}'는 존재하지 않는 날짜입니다."
        vals.append(d)
    if vals[0] and vals[1] and vals[0] > vals[1]:
        return None, None, (f"상태: INVALID_INPUT\n날짜 범위 오류: 시작일({_fmt_date(vals[0])})이 종료일({_fmt_date(vals[1])})보다 늦습니다. "
                            "'자료 없음'이 아니라 입력 오류이므로 조회하지 않았습니다.")
    return vals[0], vals[1], None


def _fmt_date(s: str) -> str:
    return f"{s[:4]}.{s[4:6]}.{s[6:8]}." if s and len(s) >= 8 and s[:4] != "1970" else "-"


# 요청(작업)별로 분리되는 상태: 동시 사용자끼리 섞이지 않도록 ContextVar 사용
_LIST_STATE: contextvars.ContextVar[dict] = contextvars.ContextVar("list_state")
_EXAMINED: contextvars.ContextVar[list] = contextvars.ContextVar("examined")


def list_state() -> dict:
    return _LIST_STATE.get({"next_pos": None, "scanned": 0, "exhausted": False})


def examined_docs() -> list:
    return _EXAMINED.get([])


async def list_minutes(
    keyword: Optional[str], council_id: Optional[str], search_type: str = "ALL",
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    kind_filter: Optional[str] = None, want: int = 20, offset: int = 0, sort: str = "MTG_DE/DESC",
    max_pages: int = 3,
) -> tuple[list[dict], int]:
    """회의록 목록. 날짜·회의유형은 API가 지원하지 않아 받아온 뒤 거른다.
    반환 (rows, 상류 전체건수). 마지막으로 소비한 상류 위치는 list_state()로 확인(무손실 이어보기용)."""
    out: list[dict] = []
    total = 0
    start = offset
    df, dt_, derr = check_dates(date_from, date_to)
    if derr:
        raise InvalidInput(derr)
    st = {"next_pos": None, "scanned": 0, "exhausted": False}
    _LIST_STATE.set(st)
    for _ in range(max_pages):
        obj = await clik.get(
            "minutes.do", displayType="list", startCount=start, listCount=100,
            searchType=search_type, searchKeyword=keyword, rasmblyId=council_id, sort=sort,
        )
        rows = _rows(obj)
        total = int(obj.get("TOTAL_COUNT") or 0)
        stop = False
        for ri, r in enumerate(rows):
            d = r.get("MTG_DE", "")
            if dt_ and d > dt_:
                continue
            if df and d < df:
                if sort == "MTG_DE/DESC":
                    stop = True
                continue
            if kind_filter and meeting_kind(r.get("MTGNM", "")) != kind_filter:
                continue
            out.append(r)
            if len(out) >= want:
                st.update(next_pos=start + ri + 1, scanned=st["scanned"] + ri + 1)
                return out, total
        st["scanned"] += len(rows)
        start += len(rows)
        if stop or len(rows) < 100 or start >= total:
            st.update(next_pos=None, exhausted=True)
            break
        st["next_pos"] = start
    return out, total


async def minutes_detail(docid: str) -> dict:
    return await clik.get("minutes.do", displayType="detail", docid=docid)


def _meta_line(r: dict) -> str:
    return (
        f"{r.get('RASMBLY_NM', '')} 제{r.get('RASMBLY_NUMPR', '?')}대 제{r.get('RASMBLY_SESN', '?')}회 "
        f"{r.get('MTGNM', '')} 제{r.get('MINTS_ODR', '?')}차 ({_fmt_date(r.get('MTG_DE', ''))})"
    )


def _footer() -> str:
    return f"\n\n—\n출처: 국회도서관 지방의정포털(CLIK) Open API · 오늘 API 잔여 약 {clik.budget_left()}회"


def _guard_budget(cost: int) -> Optional[str]:
    if clik.budget_left() - cost < DAILY_RESERVE:
        return (
            f"오늘 남은 API 호출량(약 {clik.budget_left()}회)이 부족해 이 작업(예상 {cost}회)을 실행하지 않았습니다. "
            "열어볼 회의록 수(max_docs)를 줄이거나 내일 다시 시도하세요."
        )
    return None


async def _gather_details(docids: list[str]) -> list[tuple[str, Optional[dict], Optional[str]]]:
    sem = asyncio.Semaphore(4)

    async def one(d: str):
        async with sem:
            try:
                return d, await minutes_detail(d), None
            except (ClikError, InvalidInput) as e:
                return d, None, str(e)

    return await asyncio.gather(*(one(d) for d in docids))


# ════════════════════════════════════════════════════════════
# 6. MCP 서버와 도구
# ════════════════════════════════════════════════════════════
INSTRUCTIONS = """의정소통 MCP — 전국 지방의회 회의록·의안을 근거 기반으로 조회합니다.
사용 원칙:
- 결과를 요약할 때 의원 개인에 대한 성향 평가, 순위, 점수화, '공격적' 같은 인상 표현을 하지 마세요.
- '지적/공격/방어' 대신 '제안·요구사항/관심사항/답변 준비'라는 표현을 쓰세요.
- 발언을 인용할 때는 반드시 회의 정보(의회·회기·회의명·일자)와 docid를 함께 제시하세요.
- 도구가 '자료 없음'을 반환하면 추정으로 채우지 말고 그대로 알리세요.
- 결과 첫 줄 '상태'를 따르세요: COMPLETE(확인 범위 완료) · PARTIAL(일부 확인, cursor/start_turn으로 이어보기) ·
  EMPTY(확인한 범위에 없음 — 전체 부재 아님) · INVALID_INPUT(입력 오류) · ERROR(조회 실패 — 자료 없음과 다름).
- '답변하지 않았다', '약속 미이행', '모두 없음'이라고 쓰지 마세요. 대신 '이번 확인 구간에서 연결 미확인', '후속 증빙 미확인'으로 쓰세요.
- 후속조치는 '후보'입니다. 조건·기한을 그대로 옮기고, 원문에 없는 연도나 마감일을 붙이지 마세요.
- 붙여넣은 글·합성자료를 공식 회의록이라고 소개하지 마세요(출처 종류 표시를 그대로 전달).
광주 서구의회 질문은 seogu_council_search(최근 회의록 자동 조회)를 먼저 쓰고, 오래된 회의는 CLIK 도구(council_*)로 보완하세요.
자동 조회가 막히면 council_analyze_text(붙여넣은 원문) 또는 council_search_local(보관함)을 쓰세요.
- 모든 답변에는 도구가 준 원문 주소나 docid를 붙이고, 도구 결과에 없는 발언·수치는 만들지 마세요.
가장 먼저 council_evidence_search(통합 검색)를 쓰세요. 연결이 이상하면 council_status.
권장 흐름: council_find_council → council_topic_history(현안 흐름) → council_find_qa(발언·답변 사례)
→ council_read_minutes(원문 확인) → council_prepare_briefing(대응자료 뼈대)."""

_http_mode = "--http" in sys.argv
mcp = FastMCP(
    "uijeong_mcp",
    instructions=INSTRUCTIONS,
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

PROFILE = os.environ.get("UIJEONG_PROFILE", "full").strip().lower()
LITE_TOOLS = {
    "council_find_council", "council_evidence_search", "council_open_record", "council_issue_radar",
    "council_find_commitments", "council_prepare_briefing", "council_analyze_text", "council_status",
}


def tool(name: str, annotations: ToolAnnotations):
    """UIJEONG_PROFILE=lite 이면 핵심 8개 도구만 노출(ChatGPT 등 도구 선택이 약한 환경용)."""
    def deco(fn):
        if PROFILE != "lite" or name in LITE_TOOLS:
            return mcp.tool(name=name, annotations=annotations)(fn)
        return fn
    return deco


RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
RO_LOCAL = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


@tool(name="council_find_council", annotations=RO_LOCAL)
async def council_find_council(query: str) -> str:
    """지방의회 이름으로 CLIK 의회 ID를 찾습니다(API 호출 없음).
    예: '광주 서구', '광주광역시 서구의회', '수원시의회', '경기도의회', '062006'.
    광주·전남 기관은 현 명칭 '전남광주통합특별시 ○○의회'로 등록돼 있고,
    통합 전 광역의회 회의록은 '광주광역시의회(통합 전) 062001', '전라남도의회(통합 전) 061001'로 조회합니다."""
    c = resolve_council(query)
    if not c:
        return f"'{query}'와 일치하는 지방의회가 없습니다. 시·도명과 시·군·구명을 함께 적어보세요(예: '경기 성남시')."
    lines = [f"- {n} (ID: {i})" for i, n in c[:20]]
    return f"'{query}' 검색 결과 {len(c)}건\n" + "\n".join(lines)


def _status(code: str) -> str:
    return f"상태: {code}"


def _dup_note(rows: list[dict]) -> dict[str, str]:
    """같은 의회·날짜·회의명·차수인데 docid가 다른 경우 판본 중복 후보로 표시(삭제하지 않음)."""
    seen: dict[tuple, str] = {}
    notes: dict[str, str] = {}
    for r in rows:
        k = (r.get("RASMBLY_ID"), r.get("MTG_DE"), re.sub(r"\s", "", r.get("MTGNM", "")), r.get("MINTS_ODR"))
        if k in seen and seen[k] != r.get("DOCID"):
            notes[r.get("DOCID")] = f"판본 중복 후보(docid={seen[k]}와 같은 회의)"
            notes.setdefault(seen[k], f"판본 중복 후보(docid={r.get('DOCID')}와 같은 회의)")
        else:
            seen[k] = r.get("DOCID")
    return notes


def _encode_cursor(pos: int) -> str:
    return f"m{pos}"


def _decode_cursor(cursor: Optional[str], offset: int) -> int:
    if cursor:
        m = re.fullmatch(r"m(\d+)", cursor.strip())
        if m:
            return int(m.group(1))
    return max(0, int(offset or 0))


@tool(name="council_search_minutes", annotations=RO)
async def council_search_minutes(
    keyword: str,
    council: Optional[str] = None,
    search_in: str = "전체",
    meeting_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    sort: str = "최신순",
    limit: int = 20,
    cursor: Optional[str] = None,
    offset: int = 0,
) -> str:
    """지방의회 회의록을 키워드로 검색해 목록(회의 단위)을 돌려줍니다.
    Args:
      keyword: 검색어(예: '주민참여예산', '빈집', '반도체')
      council: 의회명 또는 ID. 비우면 전국 검색
      search_in: '전체' | '내용' | '안건' | '위원회' | '의회명'
      meeting_type: '본회의' | '상임위원회' | '행정사무감사·조사' | '예산·결산' | '특별위원회' (선택)
      date_from/date_to: 'YYYY-MM-DD' (선택, 시작일이 종료일보다 늦으면 입력 오류)
      sort: '최신순' | '과거순' | '정확도순'
      limit: 1~50
      cursor: 이전 결과의 '다음 cursor' 값(권장). 표시하지 않은 결과를 건너뛰지 않고 이어서 보여줍니다
      offset: 상류 결과 위치(구버전 호환용)
    반환: 회의 정보와 docid 목록. 내용 확인은 council_read_minutes(docid)."""
    cid, cname, err = pick_council(council)
    if err:
        return _status("INVALID_INPUT") + "\n" + err
    st = MINUTES_SEARCH.get(search_in, "ALL")
    so = {"최신순": "MTG_DE/DESC", "과거순": "MTG_DE/ASC", "정확도순": "WEIGHT/DESC"}.get(sort, "MTG_DE/DESC")
    limit = max(1, min(50, limit))
    start = _decode_cursor(cursor, offset)
    try:
        rows, total = await list_minutes(keyword, cid, st, date_from, date_to, meeting_type, limit, start, so)
    except (ClikError, InvalidInput) as e:
        return (str(e) if isinstance(e, InvalidInput) else _status("ERROR") + "\n" + str(e))
    stt = list_state()
    scope = cname or "전국"
    period = f"{_fmt_date(_ymd(date_from) or '')}~{_fmt_date(_ymd(date_to) or '')}" if (date_from or date_to) else "기간 제한 없음"
    head = [f"{scope} 회의록 '{keyword}' 검색 · 기간 {period}",
            f"상류(CLIK) 검색건수 {total:,}건 — 검색어·의회 기준 건수이며 기간·회의유형 조건 충족 건수나 질의 건수가 아닙니다.",
            f"이번 조회: 상류 위치 {start}부터 {stt['scanned']}건 검토 → 조건 충족 {len(rows)}건 표시"]
    if not rows:
        if stt["next_pos"]:
            tail = f"확인한 범위에서는 조건에 맞는 회의록이 없습니다. cursor={_encode_cursor(stt['next_pos'])}로 이어서 확인하세요."
            code = "PARTIAL"
        elif start > 0:
            tail = "더 이어지는 결과가 없습니다. 이전 페이지가 조건에 맞는 마지막 결과였습니다."
            code = "COMPLETE"
        else:
            tail = "조건에 맞는 회의록이 없습니다(상류 검색 결과를 끝까지 확인)."
            code = "EMPTY"
        return "\n".join([_status(code)] + head + [tail]) + _footer()
    dup = _dup_note(rows)
    lines = [_status("PARTIAL" if stt["next_pos"] else "COMPLETE")] + head
    for r in rows:
        note = f" ※{dup[r.get('DOCID')]}" if r.get("DOCID") in dup else ""
        lines.append(f"- {_meta_line(r)} [{meeting_kind(r.get('MTGNM', ''))}] docid={r.get('DOCID')}{note}")
    if stt["next_pos"]:
        lines.append(f"\n다음 결과: cursor={_encode_cursor(stt['next_pos'])} (방금 표시한 결과 바로 다음부터 이어집니다)")
    else:
        lines.append("\n마지막 결과까지 확인했습니다.")
    return "\n".join(lines) + _footer()


@tool(name="council_read_minutes", annotations=RO)
async def council_read_minutes(
    docid: str,
    keyword: Optional[str] = None,
    speaker_role: Optional[str] = None,
    speaker_name: Optional[str] = None,
    context_turns: int = 1,
    whole_agenda: bool = False,
    start_turn: int = 0,
    max_turns: int = 30,
    full_text: bool = False,
) -> str:
    """회의록 1건을 열어 발언 단위로 보여줍니다(원문 확인·끝까지 읽기).
    Args:
      docid: council_search_minutes 등에서 받은 회의록 ID
      keyword: 지정하면 해당 단어가 들어간 발언 주변만 표시
      speaker_role: '의원' | '집행부' | '의장·위원장' | '의회사무국' (선택)
      speaker_name: 발언자 표기에 포함된 이름(원문 검색 필터로만 사용, 선택)
      context_turns: 키워드 발언 앞뒤로 함께 보여줄 발언 수(0~3)
      whole_agenda: True면 키워드가 나온 안건 구간을 처음부터 끝까지 표시(부서 구간 완독용)
      start_turn: 이어읽기 시작 발언 번호(이전 결과의 '다음 start_turn')
      max_turns: 한 번에 표시할 발언 수(1~80)
      full_text: True면 긴 발언도 자르지 않고 표시(발언당 최대 6,000자)"""
    try:
        d = await minutes_detail(docid.strip())
    except (ClikError, InvalidInput) as e:
        return _status("ERROR") + "\n" + str(e)
    turns = parse_turns(d.get("MINTS_HTML", ""))
    head = _meta_line(d)
    agenda = re.sub(r"\s+", " ", d.get("MTR_SJ", "") or "").strip()
    orig = d.get("ORGINL_FILE_URL") or ""
    if not turns:
        return (_status("EMPTY") + f"\n{head}\n안건: {agenda or '-'}\n이 회의록은 발언 단위로 분해할 수 있는 본문이 없습니다."
                + (f"\n원본파일: {orig}" if orig else "") + _footer())
    role_key = {v: k for k, v in ROLE_KO.items()}.get(speaker_role or "", None)
    ctx = max(0, min(3, context_turns))
    filtered = bool(keyword or role_key or speaker_name)
    keep: set[int] = set()
    hit_agendas: set[int] = set()
    for t in turns:
        ok = (not keyword or keyword in t["text"]) and (not role_key or t["role"] == role_key) \
             and (not speaker_name or speaker_name in t["label"])
        if ok and filtered:
            hit_agendas.add(t["agenda"])
            for k in range(t["idx"] - (ctx if keyword else 0), t["idx"] + (ctx if keyword else 0) + 1):
                if 0 <= k < len(turns):
                    keep.add(k)
    if not filtered:
        keep = set(range(len(turns)))
    if whole_agenda and hit_agendas:
        keep |= {t["idx"] for t in turns if t["agenda"] in hit_agendas}
    ordered = sorted(keep)
    start = max(0, int(start_turn or 0))
    remaining = [k for k in ordered if k >= start]
    page = remaining[: max(1, min(80, max_turns))]
    nxt = remaining[len(page)] if len(remaining) > len(page) else None
    limit_chars = 6000 if full_text else 1200
    out = [_status("PARTIAL" if nxt is not None else ("COMPLETE" if page else "EMPTY")),
           head, f"docid={docid}", f"안건: {agenda or '-'}",
           f"전체 발언 {len(turns)}개 · 조건 해당 {len(ordered)}개 · 이번 표시 {len(page)}개"
           + (f"(#{page[0]}~#{page[-1]})" if page else "")]
    if keyword and not whole_agenda:
        out.append("※ 키워드 주변 보기입니다. 부서·안건 구간 전체는 whole_agenda=True로 확인하세요.")
    out.append("")
    if not page:
        out.append("조건에 맞는 발언이 없습니다.")
    cut = 0
    prev = None
    for k in page:
        t = turns[k]
        if prev is not None and k != prev + 1:
            out.append(f"   … 발언 #{prev + 1}~#{k - 1} 생략(조건 밖)")
        prev = k
        kind = speech_context(turns, t["idx"]) if t["role"] == "member" else ""
        act = {"question": "질의", "procedural": "진행", "report": "보고", "review": "검토보고"}.get(t["act"], "")
        tag = f"[{ROLE_KO[t['role']]}{'·' + act if act else ''}{'·' + kind if kind else ''}·안건{t['agenda']}]"
        if keyword and not full_text:
            body = snippet(t["text"], keyword, 400)
        else:
            body = t["text"][:limit_chars]
        if len(body.replace("…", "")) < len(t["text"]):
            cut += 1
            body += f" (발언 일부 표시: 원문 {len(t['text']):,}자)"
        out.append(f"#{t['idx']} {tag} ○{t['label']}: {body}")
    out.append("")
    if nxt is not None:
        out.append(f"다음 이어읽기: start_turn={nxt} (남은 발언 {len(remaining) - len(page)}개)")
    else:
        out.append("조건 해당 발언을 끝까지 표시했습니다.")
    if cut:
        out.append(f"잘린 발언 {cut}개 — 전문은 full_text=True로 확인하세요.")
    if orig:
        out.append(f"원본파일: {orig}")
    return "\n".join(out) + _footer()


async def _collect_pairs(keyword: str, council_ids: list[Optional[str]], meeting_type: Optional[str],
                         date_from: Optional[str], max_docs: int, date_to: Optional[str] = None) -> tuple[list[dict], list[str], int]:
    docs: list[dict] = []
    per = max(1, max_docs // max(1, len(council_ids)))
    for cid in council_ids:
        rows, _ = await list_minutes(keyword, cid, "MINTS_HTML", date_from, date_to, meeting_type, per, 0, "MTG_DE/DESC", 2)
        docs.extend(rows[:per])
    docs = docs[:max_docs]
    _EXAMINED.set([f"{_fmt_date(r.get('MTG_DE', ''))} {r.get('MTGNM', '')}({r['DOCID']})" for r in docs])
    details = await _gather_details([r["DOCID"] for r in docs])
    meta = {r["DOCID"]: r for r in docs}
    results, errors = [], []
    for docid, d, err in details:
        if err or not d:
            errors.append(f"{docid}: {err}")
            continue
        turns = parse_turns(d.get("MINTS_HTML", ""))
        for p in build_qa_pairs(turns, keyword):
            results.append({"docid": docid, "meta": meta.get(docid, d), "pair": p})
    return results, errors, len(docs)


@tool(name="council_find_qa", annotations=RO)
async def council_find_qa(
    keyword: str,
    councils: Optional[list[str]] = None,
    meeting_type: Optional[str] = None,
    date_from: Optional[str] = None,
    max_docs: int = 5,
    max_pairs: int = 12,
    answerer: Optional[str] = None,
    date_to: Optional[str] = None,
) -> str:
    """주제어가 담긴 '의원 제안·질의 ↔ 집행부 답변' 묶음을 찾아줍니다.
    같은 주제를 다른 지자체 집행부는 어떻게 답했는지 비교할 때 councils에 여러 의회를 넣으세요.
    Args:
      keyword: 주제어(예: '경로당 냉방비', '청년 월세')
      councils: 의회명 목록(예: ['광주 서구','광주 북구','수원시']). 비우면 전국
      meeting_type: '본회의' | '상임위원회' | '행정사무감사·조사' | '예산·결산' (선택)
      date_from: 이 날짜 이후 회의만(선택)
      max_docs: 열어볼 회의록 수(1~10, 회의록 1건 = API 1회)
      max_pairs: 최대 표시 묶음 수
      answerer: 답변자 직함·부서(선택, 예: '미래전략과장') — 같은 부서 팀장 대리답변도 함께 찾음
      date_to: 이 날짜 이전 회의만(선택)
    ※ '찾지 못함'은 확인한 회의록 범위의 결과이지 전체 부재가 아닙니다.
    ※ 요약 시 의원 개인 평가·순위화 금지. 회의 정보와 docid를 함께 인용하세요."""
    ids: list[Optional[str]] = []
    names: list[str] = []
    for c in (councils or [None]):
        cid, cname, err = pick_council(c) if c else (None, "전국", None)
        if err:
            return err
        ids.append(cid)
        names.append(cname or "전국")
    max_docs = max(1, min(MAX_DETAIL_DOCS, max_docs))
    g = _guard_budget(max_docs + 2 * len(ids))
    if g:
        return g
    try:
        results, errors, ndocs = await _collect_pairs(keyword, ids, meeting_type, date_from, max_docs, date_to)
    except (ClikError, InvalidInput) as e:
        return str(e) if isinstance(e, InvalidInput) else _status("ERROR") + "\n" + str(e)
    seen_docs = examined_docs()
    if answerer:
        results = [r for r in results if any(dept_match(answerer, a["label"]) for a in r["pair"]["answers"])]
    exam = "확인한 회의록: " + ("; ".join(seen_docs) if seen_docs else "없음")
    if not results:
        return (_status("EMPTY") + f"\n{', '.join(names)} — '{keyword}'" + (f" · 답변자 '{answerer}'" if answerer else "")
                + f"\n확인한 회의록 {ndocs}건 범위에서 관련 질의·답변 묶음을 찾지 못했습니다. 전체 회의록에 없다는 뜻은 아닙니다.\n"
                + exam + "\n범위를 넓히려면 max_docs를 늘리거나 기간을 지정하고, 알려진 회의록은 council_open_record로 직접 열어 확인하세요."
                + _footer())
    out = [_status("PARTIAL"), f"'{keyword}' 질의·답변 사례 — 대상: {', '.join(names)} · 회의록 {ndocs}건 확인 · 묶음 {len(results)}개",
           "연결 기준: 같은 안건 안에서 질의 직후 이어진 답변(안건 전환·업무보고·검토보고는 연결하지 않음)", exam]
    for k, r in enumerate(results[:max_pairs], 1):
        p, m = r["pair"], r["meta"]
        kind = p["kind"] or meeting_kind(m.get("MTGNM", ""))
        out.append(f"\n[{k}] {_meta_line(m)} · {kind} · docid={r['docid']}")
        out.append(f"  ▶ 제안·질의(○{p['q']['label']}): {snippet(p['q']['text'], keyword, 220)}")
        shown = [a for a in p["answers"] if dept_match(answerer, a["label"])] or p["answers"]
        if shown:
            for a in shown[:2]:
                out.append(f"  ◀ 답변(○{a['label']}): {snippet(a['text'], keyword, 220)}")
        else:
            out.append("  ◀ 같은 회의 안의 집행부 답변 없음(5분자유발언 등은 서면·후속 조치로 답하는 경우가 많음)")
    if errors:
        out.append("\n일부 회의록 조회 실패: " + "; ".join(errors[:3]))
    return "\n".join(out) + _footer()


@tool(name="council_topic_history", annotations=RO)
async def council_topic_history(
    keyword: str,
    councils: Optional[list[str]] = None,
    date_from: Optional[str] = None,
    include_bills: bool = True,
    date_to: Optional[str] = None,
) -> str:
    """현안(주제어)이 의회에서 언제·어떤 회의에서 다뤄졌는지 연도별 흐름을 보여줍니다.
    집계 단위는 의회·연도·회의유형뿐이며 의원 개인 단위 집계는 하지 않습니다.
    Args:
      keyword: 현안 주제어(예: '복합쇼핑몰', '생활인구')
      councils: 의회명 목록(비우면 전국). 의회당 API 약 1~3회
      date_from: 시작일(선택, 기본 최근 4년)
      include_bills: 관련 의안(조례안·건의안 등)도 함께 볼지
      date_to: 종료일(선택)"""
    df, dto, derr = check_dates(date_from, date_to)
    if derr:
        return derr
    df = df or f"{dt.date.today().year - 4}0101"
    if dto and df > dto:
        return _status("INVALID_INPUT") + "\n기본 시작일(최근 4년)이 종료일보다 늦습니다. date_from을 함께 지정하세요."
    targets: list[tuple[Optional[str], str]] = []
    for c in (councils or [None]):
        cid, cname, err = pick_council(c) if c else (None, "전국", None)
        if err:
            return err
        targets.append((cid, cname or "전국"))
    g = _guard_budget(len(targets) * 4)
    if g:
        return g
    out = [_status("PARTIAL"), f"'{keyword}' 의회 논의 흐름 ({_fmt_date(df)}~{_fmt_date(dto) if dto else '현재'}, 회의록 본문 기준)",
           "※ 회의 수는 검색어가 본문에 나온 회의 건수이며 질의 건수가 아닙니다. 조회 상한(의회당 최근 300건)을 넘는 부분은 포함되지 않을 수 있습니다."]
    try:
        for cid, cname in targets:
            rows, total = await list_minutes(keyword, cid, "MINTS_HTML", df, dto, None, 300, 0, "MTG_DE/DESC", 3)
            if not rows:
                out.append(f"\n■ {cname}: [자료 없음]")
                continue
            by_year: dict[str, dict[str, int]] = {}
            for r in rows:
                y = r.get("MTG_DE", "")[:4]
                by_year.setdefault(y, {})
                k = meeting_kind(r.get("MTGNM", ""))
                by_year[y][k] = by_year[y].get(k, 0) + 1
            scope = f"(전국 최근 {len(rows)}건 표본)" if cid is None else f"(해당 기간 {len(rows)}건)"
            out.append(f"\n■ {cname} {scope}")
            for y in sorted(by_year, reverse=True):
                parts = ", ".join(f"{k} {v}" for k, v in sorted(by_year[y].items(), key=lambda x: -x[1]))
                out.append(f"  - {y}년: {parts}")
            recent = rows[:3]
            out.append("  최근 회의: " + " / ".join(f"{_fmt_date(r['MTG_DE'])} {r.get('MTGNM', '')}(docid={r['DOCID']})" for r in recent))
            if cid is None:
                cnt: dict[str, int] = {}
                for r in rows:
                    cnt[r.get("RASMBLY_NM", "")] = cnt.get(r.get("RASMBLY_NM", ""), 0) + 1
                top = sorted(cnt.items(), key=lambda x: -x[1])[:8]
                out.append("  논의가 많았던 의회(표본 기준): " + ", ".join(f"{n} {v}" for n, v in top))
            if include_bills:
                b = await clik.get("bill.do", displayType="list", startCount=0, listCount=20, searchType="ALL",
                                   searchKeyword=keyword, rasmblyId=cid, sort="ITNC_DE/DESC")
                brows = [x for x in _rows(b) if (x.get("ITNC_DE") or "0") >= df]
                if brows:
                    out.append(f"  관련 의안 {len(brows)}건(최근순):")
                    for x in brows[:6]:
                        out.append(f"   · {_fmt_date(x.get('ITNC_DE', ''))} [{x.get('BI_KND_NM', '')}·{x.get('CL_STD_NM', '')}] "
                                   f"{x.get('BI_SJ', '')} ({x.get('RASMBLY_NM', '')}, docid={x.get('DOCID')})")
    except (ClikError, InvalidInput) as e:
        out.append(f"\n조회 중단: {e}")
    return "\n".join(out) + _footer()


@tool(name="council_search_bills", annotations=RO)
async def council_search_bills(
    keyword: str,
    council: Optional[str] = None,
    search_in: str = "전체",
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> str:
    """지방의회 의안(조례안·예산안·건의안·결의안·청원 등)을 검색합니다.
    Args:
      keyword: 검색어
      council: 의회명 또는 ID(선택)
      search_in: '전체' | '제목' | '제안자' | '요지'
      status: '원안가결' | '수정가결' | '부결(폐기)' | '미처리(계류)' | '철회' 등(선택)
      date_from/date_to: 제안일 범위(선택)
      limit: 1~100"""
    cid, cname, err = pick_council(council)
    if err:
        return err
    _, _, derr = check_dates(date_from, date_to)
    if derr:
        return derr
    try:
        obj = await clik.get(
            "bill.do", displayType="list", startCount=offset, listCount=max(1, min(100, limit)),
            searchType=BILL_SEARCH.get(search_in, "ALL"), searchKeyword=keyword, rasmblyId=cid,
            clStdCd=BILL_STATUS.get(status or "", None), itncStartDt=_ymd(date_from), itncEndDt=_ymd(date_to),
            sort="ITNC_DE/DESC",
        )
    except (ClikError, InvalidInput) as e:
        return str(e)
    rows = _rows(obj)
    total = int(obj.get("TOTAL_COUNT") or 0)
    if not rows:
        return f"[자료 없음] {cname or '전국'} 의안에서 '{keyword}' 결과가 없습니다." + _footer()
    out = [f"{cname or '전국'} 의안 '{keyword}': 전체 {total:,}건 중 {len(rows)}건"]
    for x in rows:
        out.append(f"- {_fmt_date(x.get('ITNC_DE', ''))} [{x.get('BI_KND_NM', '')}·{x.get('CL_STD_NM', '')}] {x.get('BI_SJ', '')} "
                   f"— {x.get('RASMBLY_NM', '')} (의안번호 {x.get('BI_NO', '-')}, docid={x.get('DOCID')})")
    return "\n".join(out) + _footer()


@tool(name="council_get_bill", annotations=RO)
async def council_get_bill(docid: str) -> str:
    """의안 1건의 처리경과(위원회·본회의 일자와 결과, 이송·공포)와 의안요지를 보여줍니다."""
    try:
        d = await clik.get("bill.do", displayType="detail", docid=docid.strip())
    except (ClikError, InvalidInput) as e:
        return str(e)
    f = _fmt_date
    lines = [
        f"{d.get('BI_SJ', '')}",
        f"의회: {d.get('RASMBLY_NM', COUNCILS.get(d.get('RASMBLY_ID', ''), ''))} 제{d.get('RASMBLY_NUMPR', '?')}대 · 의안번호 {d.get('BI_NO', '-')} · 종류 {d.get('BI_KND_NM', '-')}",
        f"제안: {f(d.get('ITNC_DE', ''))} · 제안자 {d.get('PROPSR', '-')}",
        f"위원회: 회부 {f(d.get('CMIT_REPORT_DE', ''))} / 상정 {f(d.get('CMIT_SBMISN_DE', ''))} / 처리 {f(d.get('CMIT_PROCESS_DE', ''))} · 결과 {d.get('CMIT_RESULT', '-')}",
        f"본회의: 보고 {f(d.get('PLNMT_REPORT_DE', ''))} / 상정 {f(d.get('PLNMT_SBMISN_DE', ''))} / 의결 {f(d.get('PLNMT_PROCESS_DE', ''))} · 결과 {d.get('PLNMT_RESULT_NM') or d.get('PLNMT_RESULT', '-')}",
        f"집행기관 이송: {f(d.get('TRNSF_DE', ''))} · 공포 {d.get('PRMLGT_NO') or '-'} ({f(d.get('PRMLGT_DE', ''))})",
    ]
    outline = re.sub(r"\s+", " ", _html_to_marked_text(d.get("BI_OUTLINE", "") or "").replace("\x01", "").replace("\x02", "")).strip()
    if outline:
        lines.append("의안요지: " + outline[:1500] + ("…" if len(outline) > 1500 else ""))
    if d.get("BI_FILE_NM"):
        lines.append("첨부: " + " / ".join(x.strip() for x in str(d["BI_FILE_NM"]).splitlines() if x.strip()))
    lines.append(f"docid={docid}")
    return "\n".join(lines) + _footer()


@tool(name="council_find_commitments", annotations=RO)
async def council_find_commitments(
    council: str,
    keyword: Optional[str] = None,
    meeting_type: Optional[str] = None,
    date_from: Optional[str] = None,
    max_docs: int = 6,
    date_to: Optional[str] = None,
    answerer: Optional[str] = None,
) -> str:
    """질의에 대한 집행부 답변 중 후속조치 '후보'(자료제출·검토의사·조건부 추진·시행의사)를 기한과 함께 뽑습니다.
    현재 보고·전문위원 검토보고·부정·과거 인용은 제외하며, 이행 여부는 판정하지 않습니다.
    Args:
      council: 의회명(필수)
      keyword: 주제어(선택, 비우면 최근 회의 전반)
      meeting_type: '행정사무감사·조사' 등(선택)
      date_from/date_to: 기간(선택)
      max_docs: 열어볼 회의록 수(1~10)
      answerer: 답변자 직함·부서(선택, 같은 부서 팀장 대리답변 포함)"""
    cid, cname, err = pick_council(council)
    if err:
        return err
    max_docs = max(1, min(MAX_DETAIL_DOCS, max_docs))
    g = _guard_budget(max_docs + 2)
    if g:
        return g
    try:
        rows, _ = await list_minutes(keyword, cid, "MINTS_HTML" if keyword else "ALL", date_from, date_to,
                                     meeting_type, max_docs, 0, "MTG_DE/DESC", 2)
    except (ClikError, InvalidInput) as e:
        return str(e) if isinstance(e, InvalidInput) else _status("ERROR") + "\n" + str(e)
    if not rows:
        return _status("EMPTY") + f"\n{cname} 조건에 맞는 회의록이 없습니다." + _footer()
    details = await _gather_details([r["DOCID"] for r in rows])
    meta = {r["DOCID"]: r for r in rows}
    items = []
    for docid, d, e in details:
        if not d:
            continue
        turns = parse_turns(d.get("MINTS_HTML", ""))
        for pr in build_qa_pairs(turns, keyword):
            for a in pr["answers"]:
                if not dept_match(answerer, a["label"]):
                    continue
                for sent in split_sentences(a["text"]):
                    info = classify_commitment(sent)
                    if info:
                        items.append({"docid": docid, "meta": meta[docid], "who": a["label"], "sent": sent,
                                      "q": pr["q"]["text"], "info": info})
    exam = "확인한 회의록: " + "; ".join(f"{_fmt_date(r.get('MTG_DE', ''))} {r.get('MTGNM', '')}({r['DOCID']})" for r in rows)
    if not items:
        return (_status("EMPTY") + f"\n{cname} 확인한 회의록 {len(rows)}건 범위에서 후속조치 후보를 찾지 못했습니다.\n" + exam + _footer())
    out = [_status("PARTIAL"), exam, f"{cname} 질의에 대한 답변 중 후속조치 후보 {len(items)}건 (회의록 {len(rows)}건 확인)",
           "※ 현재 보고·검토보고·부정·과거 인용 문장은 제외했습니다. 이행 여부는 판정하지 않으며 '후속 증빙 미확인' 상태입니다."]
    for k, it in enumerate(items[:30], 1):
        m = it["meta"]
        out.append(f"\n[{k}] {_fmt_date(m.get('MTG_DE', ''))} {m.get('MTGNM', '')} · docid={it['docid']}")
        out.append(f"  후보 {commitment_tag(it['info'])}: {it['sent'][:260]} (○{it['who']})")
        if it["q"]:
            out.append(f"  관련 제안·질의 요지: {snippet(it['q'], keyword, 120)}")
    return "\n".join(out) + _footer()


@tool(name="council_search_policy", annotations=RO)
async def council_search_policy(keyword: str, region: Optional[str] = None, limit: int = 10, docid: Optional[str] = None) -> str:
    """타 지자체·공공기관 정책자료(지방정책정보: 연구용역·출장보고·감사자료 등)를 검색합니다.
    검토결과 작성 시 벤치마킹 근거로 활용하세요. docid를 주면 해당 자료 본문을 엽니다.
    Args:
      keyword: 검색어
      region: 광역 지역명(예: '서울','경기','광주') — 선택
      limit: 1~50
      docid: 상세 조회할 자료 ID(선택)"""
    try:
        if docid:
            d = await clik.get("policyinfoDetail.do", docid=docid.strip())
            body = re.sub(r"\s+", " ", _html_to_marked_text(d.get("EXTRACTHTML", "") or "").replace("\x01", "").replace("\x02", "")).strip()
            return (f"{d.get('TITLE', '')}\n{d.get('SITENM', '')} · {d.get('SEEDNM', '')} · {_fmt_date(d.get('CDATE', ''))} · 작성 {d.get('WRITER', '-')}\n"
                    f"원문: {d.get('URL', '-')}\n\n{body[:2500]}" + _footer())
        reg = None
        if region:
            rmap = {"서울": "002", "부산": "051", "대구": "053", "인천": "032", "광주": "062", "대전": "042", "울산": "052",
                    "세종": "044", "경기": "031", "강원": "033", "충북": "043", "충남": "041", "전북": "063", "전남": "061",
                    "경북": "054", "경남": "055", "제주": "064", "전남광주": "065"}
            reg = next((v for k, v in rmap.items() if region.replace(" ", "").startswith(k)), None)
        obj = await clik.get("policyinfoList.do", startCount=0, listCount=max(1, min(50, limit)),
                             searchType="ALL", searchKeyword=keyword, region=reg)
    except (ClikError, InvalidInput) as e:
        return str(e)
    rows = _rows(obj)
    if not rows:
        return f"[자료 없음] 지방정책정보에서 '{keyword}' 결과가 없습니다." + _footer()
    out = [f"지방정책정보 '{keyword}': 전체 {int(obj.get('TOTAL_COUNT') or 0):,}건 중 {len(rows)}건"]
    for x in rows:
        out.append(f"- {_fmt_date(x.get('CDATE', ''))} [{x.get('SEEDNM', '')}] {x.get('TITLE', '')} — {x.get('SITENM', '')} (docid={x.get('DOCID')})")
    return "\n".join(out) + _footer()


BRIEFING_TEMPLATES = {
    "5분자유발언": "1. 개    요(발언의원·주요내용)\n2. 현 실태\n3. 검토결과\n4. 행정사항",
    "행정사무감사": "1. 감사 개요(위원회·일시·소관부서)\n2. 최근 의회 관심사항(주제별)\n3. 예상 요구사항별 답변 방향\n4. 지난 답변 약속 이행상황\n5. 제출자료 준비 목록",
    "구정질문": "1. 질문 요지\n2. 추진 현황\n3. 답변 요지(수용·검토·곤란 구분)\n4. 향후 계획",
    "업무보고": "1. 보고 개요\n2. 주요 현안별 의회 논의 이력\n3. 예상 질의 주제와 답변 방향\n4. 참고자료",
}

TONE_GUIDE = (
    "답변 작성 원칙: ① 제안 취지에 대한 공감·경청을 먼저 밝힌다 ② 사실관계는 수치·근거로 담백하게 ③ 즉시 수용/중장기 검토/"
    "법령·예산상 곤란을 구분하되 곤란 사유는 대안과 함께 ④ '지적'이 아닌 '제안·말씀'으로 표현 ⑤ 후속조치 약속은 이행 가능한 범위로"
)


@tool(name="council_prepare_briefing", annotations=RO)
async def council_prepare_briefing(
    council: str,
    keyword: str,
    purpose: str = "5분자유발언",
    compare_councils: Optional[list[str]] = None,
    max_docs: int = 5,
) -> str:
    """의회 대응자료의 '근거 묶음 + 서식 뼈대'를 한 번에 만듭니다.
    우리 의회의 논의 이력·질의답변 사례·후속조치 약속, 타 의회 답변 사례, 관련 의안을 모아
    선택한 서식(5분자유발언/행정사무감사/구정질문/업무보고) 뼈대와 답변 톤 원칙을 함께 돌려줍니다.
    Args:
      council: 우리 의회(예: '광주 서구')
      keyword: 현안 주제어
      purpose: '5분자유발언' | '행정사무감사' | '구정질문' | '업무보고'
      compare_councils: 비교할 타 의회 목록(선택, 최대 3)
      max_docs: 우리 의회에서 열어볼 회의록 수(1~8)
    ※ 이 결과를 바탕으로 초안을 쓸 때 의원 개인 평가 표현을 넣지 마세요."""
    cid, cname, err = pick_council(council)
    if err:
        return err
    comps = (compare_councils or [])[:3]
    max_docs = max(1, min(8, max_docs))
    g = _guard_budget(max_docs + 4 + len(comps) * 4)
    if g:
        return g
    out = [f"■ 대응자료 근거 묶음 — {cname} · '{keyword}' · 용도: {purpose}"]
    try:
        rows, total = await list_minutes(keyword, cid, "MINTS_HTML", f"{dt.date.today().year - 4}0101", None, None, 200, 0, "MTG_DE/DESC", 2)
        out.append(f"\n1) 논의 이력: 최근 4년 {len(rows)}건 회의에서 언급")
        yc: dict[str, int] = {}
        for r in rows:
            yc[r["MTG_DE"][:4]] = yc.get(r["MTG_DE"][:4], 0) + 1
        if yc:
            out.append("   연도별: " + ", ".join(f"{y}년 {c}건" for y, c in sorted(yc.items(), reverse=True)))
        results, errors, nd = await _collect_pairs(keyword, [cid], None, None, max_docs)
        out.append(f"\n2) 우리 의회 질의·답변 사례 (회의록 {nd}건 확인)")
        if not results:
            out.append("   [자료 없음]")
        for k, r in enumerate(results[:6], 1):
            p, m = r["pair"], r["meta"]
            out.append(f"   [{k}] {_fmt_date(m.get('MTG_DE', ''))} {m.get('MTGNM', '')} {p['kind']} docid={r['docid']}")
            out.append(f"     ▶ {snippet(p['q']['text'], keyword, 160)}")
            for a in p["answers"][:1]:
                out.append(f"     ◀ (○{a['label']}) {snippet(a['text'], keyword, 160)}")
        out.append("\n3) 과거 답변 중 후속조치 약속")
        found = 0
        for r in results:
            for a in r["pair"]["answers"]:
                for s in split_sentences(a["text"]):
                    info = classify_commitment(s)
                    if info and found < 6:
                        found += 1
                        out.append(f"   - {_fmt_date(r['meta'].get('MTG_DE', ''))} {commitment_tag(info)} {s[:200]} (docid={r['docid']})")
        if not found:
            out.append("   [자료 없음]")
        if comps:
            out.append("\n4) 타 의회 답변 사례")
            for c in comps:
                ccid, ccname, cerr = pick_council(c)
                if cerr:
                    out.append(f"   - {c}: {cerr}")
                    continue
                cres, _, cnd = await _collect_pairs(keyword, [ccid], None, None, 2)
                if not cres:
                    out.append(f"   - {ccname}: [자료 없음] (회의록 {cnd}건 확인)")
                    continue
                r = next((x for x in cres if x["pair"]["answers"]), cres[0])
                out.append(f"   - {ccname} {_fmt_date(r['meta'].get('MTG_DE', ''))} docid={r['docid']}")
                if r["pair"]["answers"]:
                    out.append(f"     ◀ {snippet(r['pair']['answers'][0]['text'], keyword, 180)}")
        b = await clik.get("bill.do", displayType="list", startCount=0, listCount=10, searchType="ALL",
                           searchKeyword=keyword, rasmblyId=cid, sort="ITNC_DE/DESC")
        brows = _rows(b)
        out.append("\n5) 관련 의안")
        out.append("   [자료 없음]" if not brows else "\n".join(
            f"   - {_fmt_date(x.get('ITNC_DE', ''))} [{x.get('BI_KND_NM', '')}·{x.get('CL_STD_NM', '')}] {x.get('BI_SJ', '')} (docid={x.get('DOCID')})"
            for x in brows[:5]))
    except (ClikError, InvalidInput) as e:
        out.append(f"\n조회 중단: {e}")
    tpl_key = next((k for k in BRIEFING_TEMPLATES if k.replace(" ", "") in purpose.replace(" ", "")), "5분자유발언")
    out.append(f"\n■ 서식 뼈대({tpl_key})\n{BRIEFING_TEMPLATES[tpl_key]}")
    out.append(f"\n■ {TONE_GUIDE}")
    out.append("※ 조례 제정·개정 요구가 포함되면 자치법규 MCP(jachi_find_gaps 등)로 타 지자체 조문을 함께 확인하세요.")
    return "\n".join(out) + _footer()



# ════════════════════════════════════════════════════════════
# 6-2. 원문 직접 분석 · 로컬 회의록 보관함
#   의회 홈페이지(예: 서구의회 gjsc.or.kr)는 robots.txt로 자동수집을 막고 있어
#   이 서버는 홈페이지를 긁지 않는다. 대신
#   ① 담당자가 화면에서 복사·저장한 회의록 원문을 분석하거나
#   ② 의회사무국 협조로 받은 회의록 파일을 보관함 폴더에 두고 검색한다.
# ════════════════════════════════════════════════════════════
LOCAL_DIR = os.environ.get("COUNCIL_LOCAL_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "minutes_archive"))

_TITLE_RE = re.compile(r"제\s*(\d+)\s*회\s*([^\n()]*?)\s*제\s*(\d+)\s*차\s*\(?\s*(\d{4})\s*[.\-]\s*(\d{1,2})\s*[.\-]\s*(\d{1,2})")
_DATE_RE = re.compile(r"(\d{4})\s*[.\-년]\s*(\d{1,2})\s*[.\-월]\s*(\d{1,2})")


def parse_title_meta(text: str, fallback_name: str = "") -> dict:
    head = (text or "")[:600]
    m = _TITLE_RE.search(head) or _TITLE_RE.search(fallback_name)
    if m:
        return {"sesn": m.group(1), "mtgnm": m.group(2).strip() or "회의", "odr": m.group(3),
                "date": f"{m.group(4)}{int(m.group(5)):02d}{int(m.group(6)):02d}"}
    m2 = re.search(r"제\s*(\d+)\s*회[^\n]*\n\s*([^\n]*?(?:본회의|위원회))\s*회의록\s*\n?\s*제\s*(\d+)\s*호", head)
    d = _DATE_RE.search(fallback_name) or _DATE_RE.search(head)
    if m2:
        return {"sesn": m2.group(1), "mtgnm": m2.group(2).strip(), "odr": m2.group(3),
                "date": f"{d.group(1)}{int(d.group(2)):02d}{int(d.group(3)):02d}" if d else ""}
    name = re.sub(r"\(.*?\)|\.(html?|txt)$", "", fallback_name).strip()
    return {"sesn": "?", "mtgnm": name or "회의", "odr": "?",
            "date": f"{d.group(1)}{int(d.group(2)):02d}{int(d.group(3)):02d}" if d else ""}


def _meta_text(meta: dict) -> str:
    return f"제{meta['sesn']}회 {meta['mtgnm']} 제{meta['odr']}차 ({_fmt_date(meta['date'])})"


def analyze_turns_report(turns: list[dict], keyword: Optional[str], label: str, ref: str) -> list[str]:
    out = [label, f"발언 {len(turns)}개 분해 (의원 {sum(t['role']=='member' for t in turns)} · 집행부 "
           f"{sum(t['role']=='executive' for t in turns)} · 의장·위원장 {sum(t['role']=='chair' for t in turns)})"]
    five = [t for t in turns if t["role"] == "member" and speech_context(turns, t["idx"]) == "5분자유발언"]
    if five:
        out.append("\n[5분자유발언]")
        for t in five:
            out.append(f"  - ○{t['label']}: {snippet(t['text'], keyword, 200)}")
    pairs = [p for p in build_qa_pairs(turns, keyword) if p["kind"] != "5분자유발언"]
    out.append(f"\n[제안·질의 ↔ 답변] {len(pairs)}묶음")
    for k, p in enumerate(pairs[:15], 1):
        out.append(f"  [{k}] ▶ ○{p['q']['label']}: {snippet(p['q']['text'], keyword, 180)}")
        for a in p["answers"][:2]:
            out.append(f"      ◀ ○{a['label']}: {snippet(a['text'], keyword, 180)}")
    commits = []
    for p in build_qa_pairs(turns, keyword):
        for a in p["answers"]:
            for x in split_sentences(a["text"]):
                info = classify_commitment(x)
                if info:
                    commits.append((a["label"], x, info))
    out.append(f"\n[후속조치 후보] {len(commits)}건 (질의에 대한 답변만 · 현재 보고·검토보고 제외)")
    for who, x, info in commits[:15]:
        out.append(f"  - {commitment_tag(info)} {x[:220]} (○{who})")
    out.append("\n" + (ref if ref.startswith("출처 종류") else f"근거: {ref}"))
    return out


@tool(name="council_analyze_text", annotations=RO_LOCAL)
async def council_analyze_text(minutes_text: str, keyword: Optional[str] = None, title: Optional[str] = None,
                               source_kind: str = "사용자 제공") -> str:
    """의회 홈페이지에서 담당자가 직접 복사한 회의록 원문(텍스트 또는 HTML)을 분석합니다(API 호출 없음).
    최신 회의록이 CLIK에 아직 올라오지 않았거나, 특정 회의를 바로 정리하고 싶을 때 씁니다.
    발언자 분해 → 5분자유발언 · 제안·질의↔답변 묶음 · 후속조치 약속 문장을 뽑습니다.
    Args:
      minutes_text: 회의록 본문(예: 서구의회 '제342회 의회운영위원회 제3차(2026.09.03.)' 화면 전체 복사)
      keyword: 특정 주제만 보고 싶을 때(선택)
      title: 회의명(선택, 본문 첫머리에 없을 때)
      source_kind: '사용자 제공'(기본) | '합성·시험'. 서버는 붙여넣은 글이 공식 원문인지 검증할 수 없으므로
                   어떤 경우에도 '공식 회의록'으로 표시하지 않습니다.
    ※ 의원 개인 평가 없이, 회의 정보와 함께 인용하세요."""
    if not minutes_text or len(minutes_text.strip()) < 30:
        return "회의록 본문이 비어 있거나 너무 짧습니다. 회의록 화면의 본문 전체를 붙여넣어 주세요."
    turns = parse_turns(minutes_text)
    meta = parse_title_meta(minutes_text, title or "")
    if not turns:
        return ("[분해 실패] '○발언자 이름' 표기를 찾지 못했습니다. 화면에서 회의록 본문(○의장 ○○○ … 부분)까지 "
                "포함해 복사했는지 확인해 주세요.")
    head = (minutes_text[:400] + " " + (title or ""))
    synthetic = "합성" in source_kind or "시험" in source_kind or re.search(r"(합성|가상|테스트|시험용|예시\s*자료)", head)
    if synthetic:
        ref = "출처 종류: SYNTHETIC(합성·시험자료) — 실제 의회·인물의 발언이 아니며 공식 회의록 근거로 쓸 수 없습니다."
    else:
        ref = ("출처 종류: USER_PROVIDED(사용자 제공 텍스트) — 서버가 공식 원문과 대조하지 않았습니다. "
               "인용 전 의회 홈페이지나 CLIK 원문으로 확인하세요.")
    return _status("COMPLETE") + "\n" + "\n".join(analyze_turns_report(turns, keyword, "■ " + _meta_text(meta), ref))


_local_index: dict[str, Any] = {"mtime": 0.0, "docs": []}


def _load_local_archive() -> list[dict]:
    if not os.path.isdir(LOCAL_DIR):
        return []
    files = []
    for root, _, names in os.walk(LOCAL_DIR):
        for n in names:
            if n.lower().endswith((".html", ".htm", ".txt")):
                files.append(os.path.join(root, n))
    latest = max((os.path.getmtime(f) for f in files), default=0.0)
    if _local_index["docs"] and latest <= _local_index["mtime"] and len(files) == len(_local_index["docs"]):
        return _local_index["docs"]
    docs = []
    for f in sorted(files):
        raw = open(f, "rb").read()
        for enc in ("utf-8", "cp949", "euc-kr"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                text = raw.decode("utf-8", "ignore")
        turns = parse_turns(text)
        plain = re.sub(r"\s+", " ", _html_to_marked_text(text).replace("\x01", "").replace("\x02", ""))
        meta = parse_title_meta(plain, os.path.basename(f))
        docs.append({"path": os.path.relpath(f, LOCAL_DIR), "meta": meta, "turns": turns,
                     "kind": meeting_kind(meta["mtgnm"])})
    _local_index.update(mtime=latest, docs=docs)
    return docs


@tool(name="council_search_local", annotations=RO_LOCAL)
async def council_search_local(
    keyword: str,
    meeting_type: Optional[str] = None,
    committee: Optional[str] = None,
    date_from: Optional[str] = None,
    mode: str = "질의답변",
    limit: int = 12,
    date_to: Optional[str] = None,
) -> str:
    """로컬 회의록 보관함(minutes_archive 폴더 또는 COUNCIL_LOCAL_DIR)에서 검색합니다(API 호출 없음).
    의회사무국에서 제공받거나 담당자가 저장한 회의록(.html/.txt)을 대상으로 합니다.
    Args:
      keyword: 주제어
      meeting_type: '본회의' | '상임위원회' | '행정사무감사·조사' | '예산·결산' | '특별위원회' (선택)
      committee: 위원회명 일부(예: '사회도시', '기획총무', '의회운영') (선택)
      date_from: 이 날짜 이후(선택)
      mode: '질의답변' | '5분자유발언' | '약속' | '발언'
      limit: 최대 표시 건수"""
    docs = _load_local_archive()
    if not docs:
        return (f"[보관함 비어 있음] {LOCAL_DIR} 폴더에 회의록(.html/.txt)이 없습니다. 의회사무국 협조로 받은 파일이나 "
                "직접 저장한 회의록을 넣으면 검색됩니다. 한 건만 볼 때는 council_analyze_text에 본문을 붙여넣으세요.")
    df, dto, derr = check_dates(date_from, date_to)
    if derr:
        return derr
    hits = []
    for d in docs:
        m = d["meta"]
        if meeting_type and d["kind"] != meeting_type:
            continue
        if committee and committee not in m["mtgnm"]:
            continue
        if df and m["date"] and m["date"] < df:
            continue
        if dto and m["date"] and m["date"] > dto:
            continue
        turns = d["turns"]
        if mode == "5분자유발언":
            for t in turns:
                if t["role"] == "member" and speech_context(turns, t["idx"]) == "5분자유발언" and keyword in t["text"]:
                    hits.append((m["date"], d, f"○{t['label']}: {snippet(t['text'], keyword, 220)}"))
        elif mode == "약속":
            for p in build_qa_pairs(turns, keyword):
                for a in p["answers"]:
                    for x in split_sentences(a["text"]):
                        info = classify_commitment(x)
                        if info:
                            hits.append((m["date"], d, f"후보 {commitment_tag(info)}: {x[:220]} (○{a['label']})"))
        elif mode == "발언":
            for t in turns:
                if keyword in t["text"]:
                    hits.append((m["date"], d, f"[{ROLE_KO[t['role']]}] ○{t['label']}: {snippet(t['text'], keyword, 200)}"))
        else:
            for p in build_qa_pairs(turns, keyword):
                if p["kind"] == "5분자유발언":
                    continue
                line = f"▶ ○{p['q']['label']}: {snippet(p['q']['text'], keyword, 180)}"
                for a in p["answers"][:1]:
                    line += f"\n      ◀ ○{a['label']}: {snippet(a['text'], keyword, 180)}"
                hits.append((m["date"], d, line))
    if not hits:
        return f"[자료 없음] 보관함 회의록 {len(docs)}건에서 '{keyword}'({mode}) 결과가 없습니다."
    hits.sort(key=lambda x: x[0], reverse=True)
    out = [f"보관함 {len(docs)}건 중 '{keyword}' {mode} {len(hits)}건 (최신순)"]
    for date, d, line in hits[:max(1, min(50, limit))]:
        out.append(f"\n- {_meta_text(d['meta'])} · 파일 {d['path']}\n  {line}")
    return "\n".join(out)


# ════════════════════════════════════════════════════════════
# 6-3. 서구의회 홈페이지 자동 연동 (최근 회의록)
#   목록: /kr/assembly/late.do  →  상세: /record/recordView.do?key=…
#   · 실행 시점마다 robots.txt를 확인해 허용될 때만 수집한다(우회 옵션 없음)
#   · 요청 간격 1.5초, 목록 30분·상세 7일 캐시 → 의회 서버 부담 최소화
#   · 과거 회의록 전문검색은 CLIK, 최근 회의(CLIK 반영 전)는 이 연동이 담당
# ════════════════════════════════════════════════════════════
import urllib.robotparser

SITE_BASE = os.environ.get("SEOGU_COUNCIL_BASE", "https://www.gjsc.or.kr").rstrip("/")
SITE_LIST_PATH = os.environ.get("SEOGU_LIST_PATH", "/kr/assembly/late.do")
SITE_PAGE_PARAM = os.environ.get("SEOGU_PAGE_PARAM", "pageIndex")
SITE_UA = os.environ.get("SEOGU_USER_AGENT", "uijeong-mcp/1.0 (Gwangju Seo-gu office; council minutes research)")
SITE_DELAY = float(os.environ.get("SEOGU_DELAY_SEC", "1.5"))
SITE_MAX_PAGES = 10
SITE_MAX_MEETINGS = 40


class SiteBlocked(Exception):
    pass


class CouncilSite:
    def __init__(self) -> None:
        self._robots: Optional[urllib.robotparser.RobotFileParser] = None
        self._robots_at = 0.0
        self._last = 0.0
        self._lock = asyncio.Lock()
        self._list_cache: dict[int, tuple[float, list[dict]]] = {}
        self._detail_cache: dict[str, tuple[float, dict]] = {}
        self._client: Optional[httpx.AsyncClient] = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0), verify=VERIFY_SSL,
                                             headers={"User-Agent": SITE_UA}, follow_redirects=True)
        return self._client

    async def _fetch_text(self, url: str) -> str:
        client = await self._http()
        async with self._lock:
            wait = SITE_DELAY - (time.time() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            r = await client.get(url)
            self._last = time.time()
        r.raise_for_status()
        return r.text

    async def _allowed(self, url: str) -> bool:
        if self._robots is None or time.time() - self._robots_at > 6 * 3600:
            rp = urllib.robotparser.RobotFileParser()
            try:
                txt = await self._fetch_text(SITE_BASE + "/robots.txt")
                rp.parse(txt.splitlines())
            except httpx.HTTPStatusError as e:
                rp.parse([] if e.response.status_code in (404, 410) else ["User-agent: *", "Disallow: /"])
            except httpx.HTTPError:
                rp.parse(["User-agent: *", "Disallow: /"])
            self._robots, self._robots_at = rp, time.time()
        return self._robots.can_fetch(SITE_UA, url)

    async def get(self, url: str) -> str:
        if not await self._allowed(url):
            raise SiteBlocked(
                "서구의회 홈페이지의 robots.txt가 이 서버의 자동 접근을 허용하지 않아 수집하지 않았습니다. "
                "의회사무국에 이 MCP의 User-Agent(" + SITE_UA + ") 허용을 요청하거나, CLIK 검색·붙여넣기 분석을 이용하세요."
            )
        return await self._fetch_text(url)

    async def list_page(self, page: int) -> list[dict]:
        hit = self._list_cache.get(page)
        if hit and time.time() - hit[0] < 1800:
            return hit[1]
        url = f"{SITE_BASE}{SITE_LIST_PATH}" + (f"?{SITE_PAGE_PARAM}={page}" if page > 1 else "")
        rows = parse_site_list(await self.get(url))
        self._list_cache[page] = (time.time(), rows)
        return rows

    async def detail(self, key: str) -> dict:
        hit = self._detail_cache.get(key)
        if hit and time.time() - hit[0] < 7 * 86400:
            return hit[1]
        url = f"{SITE_BASE}/record/recordView.do?key={key}"
        raw = await self.get(url)
        body = _site_body(raw)
        plain = re.sub(r"[ \t]+", " ", _html_to_marked_text(body).replace("\x01", "").replace("\x02", ""))
        doc = {"key": key, "url": url, "turns": parse_turns(body), "meta": parse_title_meta(plain), "plain_head": plain[:400]}
        self._detail_cache[key] = (time.time(), doc)
        return doc


def _cells(tr: str) -> list[str]:
    tds = re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", tr)
    return [re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", c))).strip() for c in tds]


def parse_site_list(page_html: str) -> list[dict]:
    """최근회의록 표(번호·대수·회수·차수·회의명·일자) 해석."""
    rows = []
    for tr in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", page_html or ""):
        m = re.search(r"recordView\.do\?key=([0-9A-Za-z]+)", tr)
        if not m:
            continue
        c = _cells(tr)
        date = ""
        dm = re.search(r"(\d{4})[.\-](\d{2})[.\-](\d{2})", " ".join(c))
        if dm:
            date = "".join(dm.groups())
        numpr = next((re.sub(r"\D", "", x) for x in c if re.fullmatch(r"\d+\s*대", x)), "")
        sesn = next((re.sub(r"\D", "", x) for x in c if re.fullmatch(r"제?\s*\d+\s*회", x)), "")
        odr = next((re.sub(r"\D", "", x) for x in c if re.fullmatch(r"\d+\s*차", x)), "")
        name = next((x for x in c if re.search(r"(본회의|위원회)", x)), "")
        rows.append({"key": m.group(1), "no": c[0] if c else "", "numpr": numpr, "sesn": sesn, "odr": odr,
                     "mtgnm": name, "date": date})
    return rows


def _site_body(page_html: str) -> str:
    """상세 화면에서 메뉴·꼬리말을 걷어내고 회의록 본문만 남긴다."""
    h = page_html or ""
    start = h.find("의사일정")
    first_spk = h.find("○")
    a = min(x for x in (start, first_spk) if x >= 0) if max(start, first_spk) >= 0 else 0
    a = max(0, h.rfind("회의록", 0, a) - 200) if a else 0
    end = h.find("Copyright", a)
    return h[a: end if end > 0 else len(h)]


site = CouncilSite()


def _site_meta_line(r: dict) -> str:
    return f"제{r.get('numpr') or '?'}대 제{r.get('sesn') or '?'}회 {r.get('mtgnm') or '회의'} 제{r.get('odr') or '?'}차 ({_fmt_date(r.get('date', ''))})"


async def _site_rows(pages: int, committee: Optional[str], date_from: Optional[str],
                     date_to: Optional[str] = None) -> list[dict]:
    rows: list[dict] = []
    df, dto, derr = check_dates(date_from, date_to)
    if derr:
        raise InvalidInput(derr)
    for p in range(1, max(1, min(SITE_MAX_PAGES, pages)) + 1):
        got = await site.list_page(p)
        if not got:
            break
        for r in got:
            if committee and committee not in r["mtgnm"]:
                continue
            if df and r["date"] and r["date"] < df:
                continue
            if dto and r["date"] and r["date"] > dto:
                continue
            rows.append(r)
        if df and got[-1]["date"] and got[-1]["date"] < df:
            break
    return rows


@tool(name="seogu_council_recent", annotations=RO)
async def seogu_council_recent(pages: int = 1, committee: Optional[str] = None, date_from: Optional[str] = None,
                               date_to: Optional[str] = None) -> str:
    """서구의회 홈페이지 '최근회의록' 목록을 가져옵니다(한 페이지 10건, 최신순).
    Args:
      pages: 가져올 페이지 수(1~10)
      committee: '본회의' | '의회운영' | '기획총무' | '사회도시' | '예산결산' 등 회의명 일부(선택)
      date_from: 이 날짜 이후만(선택)"""
    try:
        rows = await _site_rows(pages, committee, date_from, date_to)
    except InvalidInput as e:
        return str(e)
    except SiteBlocked as e:
        return str(e)
    except httpx.HTTPError as e:
        return f"서구의회 홈페이지 연결 실패({type(e).__name__}). 잠시 후 다시 시도하세요."
    if not rows:
        return "[자료 없음] 조건에 맞는 최근 회의록이 없습니다(목록 형식이 바뀌었을 수도 있습니다)."
    out = [f"서구의회 최근회의록 {len(rows)}건"]
    for r in rows:
        out.append(f"- {_site_meta_line(r)} · key={r['key']}")
    out.append(f"\n출처: {SITE_BASE}{SITE_LIST_PATH}")
    return "\n".join(out)


async def _site_hits(keyword: str, mode: str, committee: Optional[str], date_from: Optional[str],
                     pages: int, max_meetings: int, answerer: Optional[str] = None,
                     date_to: Optional[str] = None) -> tuple[list[dict], int, int]:
    """서구의회 최근 회의록에서 구조화된 결과를 뽑는다. 반환: (hits, 확인 회의수, 실패수)"""
    rows = (await _site_rows(pages, committee, date_from, date_to))[: max(1, min(SITE_MAX_MEETINGS, max_meetings))]
    hits, failed = [], 0
    for r in rows:
        try:
            d = await site.detail(r["key"])
        except (SiteBlocked, httpx.HTTPError):
            failed += 1
            continue
        for h in _hits_from_turns(d["turns"], keyword, mode, answerer):
            h.update(src="서구의회 홈페이지", date=r["date"], meeting=_site_meta_line(r), url=d["url"], mtgnm=r["mtgnm"])
            hits.append(h)
    return hits, len(rows), failed


def _hits_from_turns(turns: list[dict], keyword: str, mode: str, answerer: Optional[str] = None) -> list[dict]:
    out: list[dict] = []
    if mode == "5분자유발언":
        for t in turns:
            if t["role"] == "member" and speech_context(turns, t["idx"]) == "5분자유발언" and keyword in t["text"]:
                out.append({"kind": "5분자유발언", "text": f"○{t['label']}: {snippet(t['text'], keyword, 220)}"})
        return out
    if mode == "발언":
        return [{"kind": "발언", "text": f"[{ROLE_KO[t['role']]}] ○{t['label']}: {snippet(t['text'], keyword, 200)}"}
                for t in turns if keyword in t["text"] and dept_match(answerer, t["label"])]
    for p in build_qa_pairs(turns, keyword):
        if p["kind"] == "5분자유발언":
            continue
        answers = [a for a in p["answers"] if dept_match(answerer, a["label"])]
        if answerer and not answers:
            continue
        if mode == "약속":
            for a in answers:
                for x in split_sentences(a["text"]):
                    info = classify_commitment(x)
                    if info:
                        out.append({"kind": "약속", "text": f"후보 {commitment_tag(info)}: {x[:220]} (○{a['label']})\n  관련 제안·질의: {snippet(p['q']['text'], keyword, 100)}"})
        else:
            line = f"▶ ○{p['q']['label']}: {snippet(p['q']['text'], keyword, 180)}"
            for a in answers[:1]:
                line += f"\n    ◀ ○{a['label']}: {snippet(a['text'], keyword, 180)}"
            out.append({"kind": p["kind"] or "질의답변", "text": line})
    return out


@tool(name="seogu_council_search", annotations=RO)
async def seogu_council_search(
    keyword: str,
    mode: str = "질의답변",
    committee: Optional[str] = None,
    answerer: Optional[str] = None,
    date_from: Optional[str] = None,
    pages: int = 2,
    max_meetings: int = 15,
    date_to: Optional[str] = None,
) -> str:
    """서구의회 홈페이지 최근 회의록을 자동으로 열어 주제어를 찾습니다(CLIK 반영 전 최신 회의 대응).
    Args:
      keyword: 주제어(예: '결산', '경로당', '주차')
      mode: '질의답변' | '5분자유발언' | '약속' | '발언'
      committee: 회의명 일부(선택, 예: '사회도시')
      answerer: 답변자 직함 일부(선택, 예: '노인복지과장', '복지국장') — 우리 부서 답변 이력만 볼 때
      date_from: 이 날짜 이후(선택)
      pages: 목록 페이지 수(1~10, 페이지당 10회의)
      max_meetings: 열어볼 회의록 수(1~40). 첫 조회는 회의록당 약 1.5초, 이후 7일 캐시
    반환 결과마다 회의록 원문 주소(recordView)가 붙습니다. 원문에 없는 내용은 답하지 마세요."""
    try:
        hits, n, failed = await _site_hits(keyword, mode, committee, date_from, pages, max_meetings, answerer, date_to)
    except InvalidInput as e:
        return str(e)
    except SiteBlocked as e:
        return str(e)
    except httpx.HTTPError as e:
        return f"서구의회 홈페이지 연결 실패({type(e).__name__})."
    head = f"서구의회 최근 회의록 {n}건 확인" + (f"(조회 실패 {failed}건)" if failed else "")
    if not hits:
        return f"[자료 없음] {head} — '{keyword}'({mode}) 결과가 없습니다. 기간을 넓히려면 pages를 늘리거나 CLIK 도구를 쓰세요."
    out = [f"{head} — '{keyword}' {mode} {len(hits)}건"]
    for h in hits[:30]:
        out.append(f"\n- {h['meeting']}\n  {h['text']}\n  원문: {h['url']}")
    return "\n".join(out)


@tool(name="seogu_council_read", annotations=RO)
async def seogu_council_read(key: str, keyword: Optional[str] = None) -> str:
    """서구의회 회의록 1건(key)을 열어 발언 분해·질의답변·약속 문장을 정리합니다.
    Args:
      key: seogu_council_recent 결과의 key 또는 recordView 주소 전체
      keyword: 특정 주제만 볼 때(선택)"""
    k = re.search(r"key=([0-9A-Za-z]+)", key)
    k = k.group(1) if k else key.strip()
    try:
        d = await site.detail(k)
    except SiteBlocked as e:
        return str(e)
    except httpx.HTTPError as e:
        return f"회의록을 열지 못했습니다({type(e).__name__}). key를 확인하세요."
    if not d["turns"]:
        return f"[분해 실패] 발언자 표기를 찾지 못했습니다. 원문: {d['url']}\n머리글: {d['plain_head'][:200]}"
    return "\n".join(analyze_turns_report(d["turns"], keyword, "■ 서구의회 " + _meta_text(d["meta"]), d["url"]))


# ════════════════════════════════════════════════════════════
# 6-4. 통합·심화 기능
#   · council_evidence_search : 홈페이지(최근)+CLIK(과거)+타 의회를 한 번에 — 근거·한계 명시
#   · council_open_record     : docid든 서구의회 주소든 하나로 열기
#   · council_issue_radar     : 위원회 단위 관심 현안 레이더(의원 개인 집계 없음)
#   · council_status          : 키·호출량·홈페이지 연동 상태 진단
# ════════════════════════════════════════════════════════════
SEOGU_ID = "062006"


def _is_seogu(cid: Optional[str]) -> bool:
    return cid == SEOGU_ID


def _evidence_block(status: str, sources: list[str], limits: list[str]) -> str:
    lines = ["\n━━ 근거·한계 ━━", f"자료 상태: {status}"]
    if sources:
        lines.append("확인한 출처: " + " / ".join(sources))
    for x in limits:
        lines.append(f"· {x}")
    lines.append("· 위 결과에 없는 발언·수치·약속은 존재 여부를 단정하지 마세요.")
    return "\n".join(lines)


@tool(name="council_evidence_search", annotations=RO)
async def council_evidence_search(
    keyword: str,
    council: str = "광주 서구",
    mode: str = "질의답변",
    answerer: Optional[str] = None,
    committee: Optional[str] = None,
    date_from: Optional[str] = None,
    compare_councils: Optional[list[str]] = None,
    depth: str = "보통",
    date_to: Optional[str] = None,
) -> str:
    """【가장 먼저 쓰는 통합 검색】 의회에서 어떤 주제가 어떻게 논의됐는지 근거와 함께 찾습니다.
    서구의회는 홈페이지 최근 회의록 → CLIK 과거 회의록 순으로 자동 조회하고, 타 의회 비교도 한 번에 합니다.
    Args:
      keyword: 주제어(예: '경로당', '결산', '불법주정차')
      council: 대상 의회(기본 '광주 서구')
      mode: '질의답변' | '5분자유발언' | '약속' | '발언'
      answerer: 답변자 직함 일부(선택, 예: '노인복지과장') — 우리 부서 답변 이력
      committee: 위원회명 일부(선택, 예: '사회도시')
      date_from: 이 날짜 이후(선택)
      compare_councils: 비교할 타 의회(선택, 최대 3, 예: ['광주 북구','수원시'])
      depth: '빠르게'(회의록 6건) | '보통'(15건) | '깊게'(30건)
      date_to: 이 날짜 이전(선택)
    결과 끝의 '근거·한계'를 반드시 함께 전달하세요."""
    cid, cname, err = pick_council(council)
    if err:
        return _status("INVALID_INPUT") + "\n" + err
    _, _, derr = check_dates(date_from, date_to)
    if derr:
        return derr
    n = {"빠르게": 6, "보통": 15, "깊게": 30}.get(depth, 15)
    out = [f"■ '{keyword}' 의회 논의 근거 — {cname} · {mode}" + (f" · 답변자 '{answerer}'" if answerer else "")]
    sources, limits, total = [], [], 0
    seen: set[tuple[str, str]] = set()

    if _is_seogu(cid):
        try:
            hits, nm, failed = await _site_hits(keyword, mode, committee, date_from, max(2, n // 8), n, answerer, date_to)
            sources.append(f"서구의회 홈페이지 최근 회의록 {nm}건")
            if failed:
                limits.append(f"홈페이지 회의록 {failed}건은 열지 못함")
            out.append(f"\n① 서구의회 홈페이지(최근) — {len(hits)}건")
            for h in hits[:12]:
                seen.add((h["date"], re.sub(r"\s", "", h["mtgnm"])))
                out.append(f"- {h['meeting']}\n  {h['text']}\n  원문: {h['url']}")
            if not hits:
                out.append("  [자료 없음]")
            total += len(hits)
        except SiteBlocked as e:
            limits.append("홈페이지 자동 연동 불가: " + str(e)[:80] + "…")
        except httpx.HTTPError as e:
            limits.append(f"홈페이지 연결 실패({type(e).__name__})")

    if clik.budget_left() - (n // 2 + 3) >= DAILY_RESERVE and API_KEY:
        try:
            results, errors, nd = await _collect_pairs(keyword, [cid], "행정사무감사·조사" if committee and "감사" in committee else None,
                                                       date_from, max(3, n // 2), date_to)
            if committee:
                results = [r for r in results if committee in r["meta"].get("MTGNM", "")]
            if answerer:
                results = [r for r in results if any(dept_match(answerer, a["label"]) for a in r["pair"]["answers"])]
            if mode == "5분자유발언":
                results = [r for r in results if r["pair"]["kind"] == "5분자유발언"]
            fresh = [r for r in results if (r["meta"].get("MTG_DE", ""), re.sub(r"\s", "", r["meta"].get("MTGNM", ""))) not in seen]
            sources.append(f"CLIK 회의록 {nd}건")
            out.append(f"\n② CLIK 회의록(과거·전문검색) — {len(fresh)}건")
            for r in fresh[:10]:
                p, m = r["pair"], r["meta"]
                if mode == "약속":
                    for a in p["answers"]:
                        for x in split_sentences(a["text"]):
                            info = classify_commitment(x)
                            if info:
                                out.append(f"- {_meta_line(m)} · docid={r['docid']}\n  후보 {commitment_tag(info)}: {x[:220]} (○{a['label']})")
                    continue
                line = f"- {_meta_line(m)} · docid={r['docid']}\n  ▶ ○{p['q']['label']}: {snippet(p['q']['text'], keyword, 170)}"
                for a in [a for a in p["answers"] if dept_match(answerer, a["label"])][:1]:
                    line += f"\n  ◀ ○{a['label']}: {snippet(a['text'], keyword, 170)}"
                out.append(line)
            if not fresh:
                out.append("  [자료 없음]")
            total += len(fresh)
            if errors:
                limits.append(f"CLIK 회의록 {len(errors)}건 조회 실패")
        except (ClikError, InvalidInput) as e:
            limits.append(f"CLIK 조회 불가: {e}")
    else:
        limits.append("CLIK 인증키 미설정 또는 오늘 호출량 부족으로 과거 회의록은 확인하지 않음")

    for c in (compare_councils or [])[:3]:
        ccid, ccname, cerr = pick_council(c)
        if cerr:
            limits.append(cerr)
            continue
        try:
            cres, _, cnd = await _collect_pairs(keyword, [ccid], None, date_from, 3, date_to)
        except (ClikError, InvalidInput) as e:
            limits.append(f"{ccname} 조회 불가: {e}")
            continue
        sources.append(f"{ccname} CLIK {cnd}건")
        withans = [r for r in cres if r["pair"]["answers"]]
        out.append(f"\n③ 비교: {ccname} — 답변 있는 묶음 {len(withans)}건")
        for r in withans[:3]:
            a = r["pair"]["answers"][0]
            out.append(f"- {_meta_line(r['meta'])} · docid={r['docid']}\n  ◀ ○{a['label']}: {snippet(a['text'], keyword, 170)}")
        total += len(withans)

    limits.append("질의·답변 묶음과 약속 문장은 회의록 표기(○직함 이름)를 규칙으로 해석한 결과이므로 인용 전 원문 확인 권장")
    status = "확인됨" if total else "자료 없음"
    return "\n".join(out) + _evidence_block(status, sources, limits) + _footer()


@tool(name="council_open_record", annotations=RO)
async def council_open_record(ref: str, keyword: Optional[str] = None) -> str:
    """회의록 1건을 열어 발언 분해·질의답변·5분자유발언·약속 문장을 정리합니다.
    Args:
      ref: CLIK docid 또는 서구의회 회의록 주소(recordView.do?key=…)나 key
      keyword: 특정 주제만 볼 때(선택)"""
    ref = ref.strip()
    m = re.search(r"key=([0-9A-Za-z]+)", ref)
    if m or re.fullmatch(r"[0-9a-f]{40,}", ref):
        return await seogu_council_read(m.group(1) if m else ref, keyword)
    try:
        d = await minutes_detail(ref)
    except (ClikError, InvalidInput) as e:
        return str(e)
    turns = parse_turns(d.get("MINTS_HTML", ""))
    if not turns:
        return f"[분해 실패] {_meta_line(d)} · docid={ref} — 발언 단위 본문이 없습니다." + _footer()
    return "\n".join(analyze_turns_report(turns, keyword, "■ " + _meta_line(d), f"CLIK docid={ref}")) + _footer()


_JOSA = sorted(["으로부터", "에서부터", "이라든지", "에서는", "으로는", "에게는", "까지는", "부터는", "이라는", "라는", "에서",
                "으로", "에게", "께서", "까지", "부터", "보다", "처럼", "마다", "이나", "이랑", "하고", "과는", "와는", "에는",
                "에도", "만큼", "은", "는", "이", "가", "을", "를", "에", "의", "로", "와", "과", "도", "만", "들"], key=len, reverse=True)
_STOP = set("""의원 위원 위원님 의원님 위원장 위원장님 의장 의장님 구청장 구청장님 과장 과장님 국장 국장님 실장 팀장 동장 집행부
말씀 부분 생각 저희 우리 지금 관련 그런 이런 저런 그거 이거 저거 그래서 그리고 그러면 그런데 하지만 그러니까 때문 정도
경우 문제 내용 사항 사업 계획 추진 진행 현재 올해 작년 내년 확인 필요 검토 답변 질의 질문 요청 부탁 감사 수고 여러분
어떻게 어떤 무엇 얼마 이렇게 그렇게 저렇게 다시 계속 조금 많이 너무 아주 정말 혹시 일단 먼저 다음 이상 이하 대해 대한
통해 위해 따라 관해 등등 함께 모두 각각 전체 일부 역시 또한 특히 바로 가장 매우 여기 거기 저기 자료 보고 설명 방안 부서 담당 행정 서구 전남광주통합특별시 광주광역시 광주 제가
했는데 하는데 있는데 없는데 거든요 있습니다 없습니다 합니다 됩니다 같습니다 드립니다 바랍니다 하겠습니다 주시기 해주시기
그게 이게 저게 것이 것을 것은 거는 거를 건데 뭐냐 그러 이제 좀 네 예 아니 아니요 위원회 회의 안건 의사일정""".split())
_VERB_END = re.compile(r"(습니다|십니까|니까|니다|는데|세요|어요|아요|해서|하고|했고|하며|하면|하는|했던|되는|되어|된다|한다|했다|이다|였다|지만|는지|거든|잖아|네요|시오|십시오|주고|봐|줘|죠|까|요|다|게|며|고|서|면|던|인|한|된|할|될|있는|없는|같은)$")


def extract_terms(text: str) -> list[str]:
    out = []
    for tok in re.findall(r"[가-힣A-Za-z0-9]{2,}", text):
        if tok.isdigit() or (_VERB_END.search(tok) and len(tok) >= 3):
            continue
        for _ in range(2):
            for j in _JOSA:
                if tok.endswith(j) and len(tok) - len(j) >= 2:
                    tok = tok[: -len(j)]
                    break
        if _VERB_END.search(tok) and len(tok) >= 3:
            continue
        if len(tok) < 2 or tok in _STOP or re.fullmatch(r"\d+[가-힣]?", tok):
            continue
        out.append(tok)
    return out


def radar_from_docs(docs: list[tuple[str, str, list[dict]]], top: int = 12) -> dict[str, list[tuple[str, int, list[str]]]]:
    """docs: [(위원회·회의명, 참조, turns)] → 회의명 그룹별 [(용어, 등장 회의수, 참조)] — 회의수, 총 등장횟수 순"""
    group: dict[str, dict[str, set[str]]] = {}
    freq: dict[str, dict[str, int]] = {}
    for mtgnm, ref, turns in docs:
        g = re.sub(r"\s", "", mtgnm) or "기타"
        bag = group.setdefault(g, {})
        fq = freq.setdefault(g, {})
        for t in turns:
            if t["role"] == "member" or (t["role"] == "chair" and len(t["text"]) >= 150):
                for term in extract_terms(t["text"]):
                    bag.setdefault(term, set()).add(ref)
                    fq[term] = fq.get(term, 0) + 1
    result = {}
    for g, bag in group.items():
        ranked = sorted(bag.items(), key=lambda x: (-len(x[1]), -freq[g][x[0]], x[0]))
        result[g] = [(term, len(refs), sorted(refs)[:2]) for term, refs in ranked][:top]
    return result


@tool(name="council_issue_radar", annotations=RO)
async def council_issue_radar(
    council: str = "광주 서구",
    committee: Optional[str] = None,
    date_from: Optional[str] = None,
    max_meetings: int = 15,
    top: int = 12,
    date_to: Optional[str] = None,
) -> str:
    """위원회별로 최근 의원 제안·질의에서 반복해서 등장한 주제어를 모아 '관심 현안 레이더'를 만듭니다.
    집계 단위는 위원회(회의명)이며 의원 개인별 집계·순위는 하지 않습니다.
    Args:
      council: 의회(기본 '광주 서구' — 홈페이지 최근 회의록 사용, 그 외는 CLIK 최근 회의록)
      committee: 위원회명 일부(선택, 예: '사회도시')
      date_from: 이 날짜 이후(선택)
      max_meetings: 분석할 회의록 수(3~30)
      top: 위원회별 표시 주제어 수
    ※ 자동 추출 키워드이므로 해석 전에 council_evidence_search로 실제 발언을 확인하세요."""
    cid, cname, err = pick_council(council)
    if err:
        return err
    _, _, derr = check_dates(date_from, date_to)
    if derr:
        return derr
    k = max(3, min(30, max_meetings))
    docs: list[tuple[str, str, list[dict]]] = []
    sources, limits = [], []
    if _is_seogu(cid):
        try:
            rows = (await _site_rows(max(1, k // 10 + 1), committee, date_from, date_to))[:k]
            for r in rows:
                if "본회의" in r["mtgnm"] and committee:
                    continue
                try:
                    d = await site.detail(r["key"])
                    docs.append((r["mtgnm"], f"{_fmt_date(r['date'])} {r['mtgnm']} 제{r['odr']}차", d["turns"]))
                except (SiteBlocked, httpx.HTTPError):
                    pass
            sources.append(f"서구의회 홈페이지 최근 회의록 {len(docs)}건")
        except SiteBlocked as e:
            limits.append("홈페이지 자동 연동 불가 → CLIK으로 대체")
        except httpx.HTTPError:
            limits.append("홈페이지 연결 실패 → CLIK으로 대체")
    if not docs:
        g = _guard_budget(k + 2)
        if g:
            return g
        try:
            rows, _ = await list_minutes(None, cid, "ALL", date_from, date_to, None, k * 2, 0, "MTG_DE/DESC", 1)
        except (ClikError, InvalidInput) as e:
            return str(e)
        rows = [r for r in rows if not committee or committee in r.get("MTGNM", "")][:k]
        for docid, d, e in await _gather_details([r["DOCID"] for r in rows]):
            if d:
                docs.append((d.get("MTGNM", ""), f"{_fmt_date(d.get('MTG_DE', ''))} {d.get('MTGNM', '')} docid={docid}",
                             parse_turns(d.get("MINTS_HTML", ""))))
        sources.append(f"CLIK 최근 회의록 {len(docs)}건")
    if not docs:
        return f"[자료 없음] {cname} 분석할 회의록이 없습니다." + _evidence_block("자료 없음", sources, limits)
    radar = radar_from_docs(docs, max(3, min(30, top)))
    out = [f"■ {cname} 위원회별 관심 현안 레이더 (회의록 {len(docs)}건, 의원 제안·질의 발언 기준)"]
    for g, items in sorted(radar.items(), key=lambda x: -sum(c for _, c, _ in x[1])):
        if not items:
            continue
        out.append(f"\n[{g}]")
        out.append("  " + ", ".join(f"{t}({c}회의)" for t, c, _ in items))
        ex = items[0]
        out.append(f"  예: '{ex[0]}' → {' / '.join(ex[2])}")
    limits += ["형태소 분석기 없이 조사·어미를 규칙으로 걷어낸 키워드라 일부 잡음이 섞일 수 있음",
               "'N회의'는 해당 단어가 의원 발언에 나온 회의 수이며 중요도·찬반을 뜻하지 않음",
               "의원 개인 단위 집계는 설계상 제공하지 않음"]
    return "\n".join(out) + _evidence_block("확인됨", sources, limits)


@tool(name="council_status", annotations=RO)
async def council_status(test_council: str = "광주 서구") -> str:
    """서버 연결 상태를 진단합니다: CLIK 인증키 작동, 오늘 남은 호출량, 대상 의회 자료 수, 서구의회 홈페이지 연동 가능 여부.
    처음 연결했을 때나 결과가 이상할 때 먼저 실행하세요."""
    lines = ["■ 의정소통 MCP 상태 점검", f"서버 버전: {SERVER_VERSION}", f"도구 프로필: {PROFILE} · CLIK 인증키: {'설정됨' if API_KEY else '미설정'} · 오늘 잔여 호출 약 {clik.budget_left()}회"]
    cid, cname, err = pick_council(test_council)
    if API_KEY and cid:
        ids = [(cid, cname)] + ([("065001", COUNCILS["065001"]), ("062001", COUNCILS["062001"])] if _is_seogu(cid) else [])
        for i, nm in ids:
            try:
                obj = await clik.get("minutes.do", displayType="list", startCount=0, listCount=1, searchType="ALL",
                                     rasmblyId=i, sort="MTG_DE/DESC")
                rows = _rows(obj)
                latest = _meta_line(rows[0]) if rows else "-"
                lines.append(f"- CLIK {nm}({i}): 회의록 {int(obj.get('TOTAL_COUNT') or 0):,}건 · 최신 {latest}")
            except (ClikError, InvalidInput) as e:
                lines.append(f"- CLIK {nm}({i}): 오류 — {e}")
                break
    elif err:
        lines.append(err)
    try:
        url = f"{SITE_BASE}{SITE_LIST_PATH}"
        ok = await site._allowed(url)
        lines.append(f"- 서구의회 홈페이지 자동 연동: {'허용(robots.txt 확인)' if ok else '불허(robots.txt) — 사무국 협조 필요'}")
        if ok:
            rows = await site.list_page(1)
            lines.append(f"  최근회의록 목록 해석: {len(rows)}건" + (f" · 최신 {_site_meta_line(rows[0])}" if rows else " (형식 변경 가능성 점검 필요)"))
    except httpx.HTTPError as e:
        lines.append(f"- 서구의회 홈페이지: 연결 실패({type(e).__name__})")
    lines.append(f"- 로컬 보관함: {len(_load_local_archive())}건 ({LOCAL_DIR})")
    return "\n".join(lines)


# ── 사용 흐름 프롬프트(Claude 등 프롬프트 지원 클라이언트에서 메뉴로 노출)
@mcp.prompt(name="5분발언_대응자료", description="5분 자유발언 대응 회의자료 초안 만들기")
def prompt_five_minute(주제: str, 의회: str = "광주 서구") -> str:
    return (f"{의회} 의회의 '{주제}' 관련 5분 자유발언 대응자료를 만들어줘.\n"
            f"1) council_evidence_search(keyword='{주제}', council='{의회}', mode='5분자유발언')로 발언 원문을 찾고\n"
            f"2) council_prepare_briefing(council='{의회}', keyword='{주제}', purpose='5분자유발언')으로 근거 묶음을 만든 뒤\n"
            "3) '1. 개요 / 2. 현 실태 / 3. 검토결과 / 4. 행정사항' 서식으로 정리해줘.\n"
            "원문 주소·docid를 붙이고, 도구 결과에 없는 사실은 [확인 필요]로 표시해줘. 의원 개인 평가 표현은 쓰지 마.")


@mcp.prompt(name="회기전_약속점검", description="정례회·행정사무감사 전에 우리 부서가 의회에 약속한 사항 점검")
def prompt_commitments(부서직함: str, 주제: str = "", 의회: str = "광주 서구") -> str:
    return (f"{의회} 의회에서 '{부서직함}'이(가) 답변하며 약속한 사항을 점검해줘.\n"
            f"council_evidence_search(keyword='{주제 or 부서직함}', council='{의회}', mode='약속', answerer='{부서직함}', depth='깊게')를 쓰고,\n"
            "약속 문장·회의·원문 주소를 표로 정리한 뒤 '이행 확인 필요' 칸을 비워둬. 이행 여부는 추정하지 마.")


@mcp.prompt(name="위원회_관심현안", description="위원회별 최근 관심 현안 파악(업무보고·회기 준비)")
def prompt_radar(위원회: str = "", 의회: str = "광주 서구") -> str:
    return (f"council_issue_radar(council='{의회}', committee='{위원회}')로 관심 현안을 뽑고, 상위 3개 주제는 "
            f"council_evidence_search로 실제 발언을 확인해서 '주제 · 논의 요지 · 우리 부서 준비사항'으로 정리해줘. "
            "의원 개인별로 나누지 말고 위원회 기준으로만 정리해줘.")

# ════════════════════════════════════════════════════════════
# 7. 실행
# ════════════════════════════════════════════════════════════
if __name__ == "__main__":
    if "--check" in sys.argv:
        print(asyncio.run(council_status()))
        sys.exit(0)
    if _http_mode:
        import uvicorn

        app = mcp.streamable_http_app()
        uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
    else:
        mcp.run()
