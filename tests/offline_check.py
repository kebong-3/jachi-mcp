# -*- coding: utf-8 -*-
"""
인터넷·인증키 없이 파서와 도구 흐름을 점검하는 오프라인 검사.
실행: python tests/offline_check.py
※ 아래 회의록은 형식 점검용 가상 예시이며 실제 발언이 아닙니다.
"""
import asyncio, os, sys, re
os.environ.setdefault("CLIK_API_KEY", "OFFLINE-TEST")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import uijeong_mcp as U

U.SITE_DELAY = 0
FAILS = []


def check(name, cond):
    print(("  [통과] " if cond else "  [실패] ") + name)
    if not cond:
        FAILS.append(name)


# ── 서구의회 화면 형식(목록·상세) 예시
def row(no, s, o, n, d, k):
    return (f'<tr><td>{no}</td><td>10대</td><td>제{s}회</td><td>{o}차</td>'
            f'<td><a href="/record/recordView.do?key={k}">{n}</a></td><td>{d} 월요일</td></tr>')


LIST = ("<table><tr><th>번호</th><th>대수</th><th>회수</th><th>차수</th><th>회의명</th><th>일자</th></tr>"
        + row(9002, 900, 3, "의회운영위원회", "2026.09.03", "aaa111")
        + row(9001, 900, 6, "사회도시위원회", "2026.07.27", "bbb222") + "</table>")

SITE_AGENDA = """제900회 가상시 서구의회(폐회중)
의회운영위원회 회의록
제3호
일   시  2026년 9월 3일(목) 10시
○위원장 <a href="#">가나다</a><br>
  성원이 되었으므로 회의를 개회하겠습니다.<br>
1. 제901회 임시회 의사일정 협의의 건 ○위원장 <a href="#">가나다</a><br>
  의사일정 제1항을 상정합니다.<br>
○출석위원(6인)<br>가나다 라마바"""

SITE_QA = """제900회 가상시 서구의회(임시회)
사회도시위원회 회의록
제6호
일   시  2026년 7월 27일(월) 10시
○위원장 <a href="#">사아자</a><br>질의하실 위원님 질의해 주십시오.<br>
○<a href="#">차카타</a> 위원<br>경로당 냉방기 교체 예산이 부족하다는 민원이 많습니다. 경로당 주차 공간도 부족합니다. 대책이 있습니까?<br>
○노인복지과장 <a href="#">파하가</a><br>위원님 말씀에 공감합니다. 수요조사를 거쳐 추경에 반영될 수 있도록 검토하겠습니다.<br>
○<a href="#">거너더</a> 위원<br>경로당 프로그램 운영비도 함께 살펴봐 주십시오.<br>
○노인복지과장 <a href="#">파하가</a><br>프로그램 운영비는 동 행정복지센터와 협의하겠습니다.<br>
○출석위원(6인)<br>사아자 차카타"""

PLENARY = """<b>○의장 가가가</b> 다음은 5분 자유발언을 듣도록 하겠습니다. 나나나 의원 발언해 주시기 바랍니다.<br>
<b>○나나나 의원</b> 경로당 냉방비 현실화를 제안드립니다. """ + "어르신들의 여름나기가 힘듭니다. " * 15 + """<br>
<b>○의장 가가가</b> 다음은 구정질문ㆍ답변을 진행하겠습니다.<br>
<b>○다다다 의원</b> 경로당 냉방비 추가 지원 계획을 묻습니다.<br>
<b>○구청장 라라라</b> 경로당 냉방비는 내년 본예산에 반영하겠습니다.<br>
<b>○출석의원</b> 가가가"""

CLIK_LIST = [{"RESULT_CODE": "SUCCESS", "TOTAL_COUNT": 1, "LIST": [{"ROW": {
    "DOCID": "DOC1", "RASMBLY_ID": "062006", "MTG_DE": "20250710", "RASMBLY_NM": "가상 서구의회",
    "RASMBLY_NUMPR": "9", "RASMBLY_SESN": "800", "MINTS_ODR": "2", "MTGNM": "본회의"}}]}]
CLIK_DET = [{"RESULT_CODE": "SUCCESS", "DOCID": "DOC1", "MINTS_HTML": PLENARY, "MTG_DE": "20250710",
             "RASMBLY_NM": "가상 서구의회", "RASMBLY_NUMPR": "9", "RASMBLY_SESN": "800", "MINTS_ODR": "2", "MTGNM": "본회의"}]


async def fake_site(url):
    if url.endswith("robots.txt"):
        return "User-agent: *\nAllow: /\n"
    if "late.do" in url:
        return LIST if "pageIndex" not in url else "<table></table>"
    return "<ul><li>메뉴</li></ul><div>" + (SITE_AGENDA if "aaa111" in url else SITE_QA) + "</div><p>Copyright</p>"


async def fake_clik(path, params):
    if path == "bill.do":
        return [{"RESULT_CODE": "SUCCESS", "TOTAL_COUNT": 0, "LIST": []}]
    return CLIK_DET if params.get("displayType") == "detail" else CLIK_LIST


async def main():
    U.site._fetch_text = fake_site
    U.clik._raw_get = fake_clik

    print("1. 의회 이름 해석")
    check("'광주 서구' → 062006", U.pick_council("광주 서구")[0] == "062006")
    check("'서구의회'는 여러 곳이라 되묻기", U.pick_council("서구의회")[0] is None)

    print("2. 회의록 파서")
    t = U.parse_turns(SITE_AGENDA)
    check("직함+링크 이름 표기 인식(위원장 2회)", [x["label"] for x in t] == ["위원장 가나다", "위원장 가나다"])
    check("출석위원 명단에서 발언 종료", all("출석" not in x["label"] for x in t))
    check("발언 끝 안건 제목 제거", not t[0]["text"].endswith("협의의 건"))
    q = U.parse_turns(SITE_QA)
    check("이름 뒤 '위원' 표기 → 의원", q[1]["role"] == "member")
    check("과장 → 집행부", q[2]["role"] == "executive")
    p = U.parse_turns(PLENARY)
    check("5분자유발언 판별", U.speech_context(p, 1) == "5분자유발언")
    check("구정질문 판별", U.speech_context(p, 3) == "구·시정질문")

    print("3. 도구 흐름")
    r = await U.seogu_council_search("냉방기", mode="약속", answerer="노인복지과장")
    check("서구 홈페이지 약속 추출 + 부서 필터", "추경에 반영" in r and "원문:" in r)
    r = await U.council_evidence_search("경로당", depth="빠르게")
    check("통합검색: 홈페이지+CLIK 두 출처", "① 서구의회 홈페이지" in r and "② CLIK" in r and "근거·한계" in r)
    r = await U.council_issue_radar(max_meetings=5)
    check("관심현안 레이더: 위원회 단위 키워드", "경로당" in r and "사회도시위원회" in r)
    check("레이더에 의원 이름 미포함", "차카타" not in r and "거너더" not in r)
    r = await U.council_open_record("https://www.gjsc.or.kr/record/recordView.do?key=bbb222", "경로당")
    check("주소로 회의록 열기", "제안·질의 ↔ 답변" in r)
    r = await U.council_open_record("DOC1")
    check("docid로 회의록 열기(5분발언 포함)", "5분자유발언" in r)
    r = await U.council_analyze_text(SITE_QA, "냉방기")
    check("붙여넣기 분석 회의명 해석", "사회도시위원회" in r and "2026.07.27" in r)

    print("\n결과: " + ("모두 통과" if not FAILS else f"실패 {len(FAILS)}건 → {FAILS}"))
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
