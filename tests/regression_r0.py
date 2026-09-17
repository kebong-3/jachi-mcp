# -*- coding: utf-8 -*-
"""
R0 증거 무결성 회귀시험 — 「지방의회MCP 고도화 개발·검수 종합보고서」의 재현사례를 고정.
실행: python tests/regression_r0.py      (인터넷·인증키 불필요)
※ 모든 회의록은 가상의 이름·발언으로 만든 합성자료입니다.
"""
import asyncio, os, re, sys
os.environ.setdefault("CLIK_API_KEY", "OFFLINE-TEST")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import uijeong_mcp as U

FAILS, CALLS = [], []


def check(tid, name, cond):
    print(("  [통과] " if cond else "  [실패] ") + f"{tid} {name}")
    if not cond:
        FAILS.append(f"{tid} {name}")


# ── 합성 회의록 ────────────────────────────────────────────
T09 = """합성자료
○김가상 위원: 미래전략과 스마트시티 사업의 시비 부담이 얼마입니까?
○미래전략과장 이가상: 시비는 2억원입니다. 세부 내역은 10월 5일까지 제출하겠습니다.
○박가상 위원: 추진 일정도 알려주십시오.
○미래전략과장 이가상: 내년 상반기 착수 예정입니다."""
T10 = T09.replace(":", "  ")          # 같은 내용, 두 칸 공백 표기
T12 = """○위원장 최가상  다음은 미래전략과 소관 업무에 대한 질의를 하겠습니다. 질의하실 위원님 질의해 주시기 바랍니다.
○김가상 위원  스마트시티 공모의 시비 부담은 얼마입니까?
○미래전략과장 이가상  시비 2억원입니다. 세부자료는 10월 5일까지 제출하겠습니다.
○위원장 최가상  더 질의하실 위원님 안 계시면 미래전략과 소관 질의를 종결하겠습니다. 다음은 관광과 소관 업무보고를 받도록 하겠습니다.
○관광과장 정가상  관광과 소관 주요업무를 보고드리겠습니다. 축제 예산은 5억원으로 추진하겠습니다.
○전문위원 한가상  검토보고 드리겠습니다. 본 사업은 적정하다고 판단되며 추진하겠습니다."""
T13 = """○김가상 위원  계획을 말씀해 주십시오.
○기획과장 이가상  그 사업은 추진하지 않겠습니다. 작년에 검토하겠다고 했었습니다. 국비가 확보되면 내년에 추진하겠습니다. 오늘 회의에서 현황을 보고드리겠습니다. 관련 자료는 10월 5일까지 제출하겠습니다."""
T20 = """○위원장 최가상  의석을 정돈해 주십시오. 성원이 되었으므로 개의하겠습니다.
○위원장 최가상  미래전략과장님, 스마트시티 실증사업 예산이 줄어든 이유가 무엇입니까?
○미래전략과장 이가상  국비 공모 결과에 따라 조정되었습니다.
○위원장 최가상  그렇다면 부족분은 어떻게 보완하실 계획입니까?
○미래전략팀장 박가상  과장님을 대신해 답변드리면, 추경에 반영될 수 있도록 검토하겠습니다."""
LONG = "".join(f"<b>○{'김가상 위원' if i % 2 == 0 else '기획과장 이가상'}</b> 발언 {i}번 내용입니다. 스마트시티 관련 확인입니다.<br>"
               for i in range(120))

# ── 가짜 CLIK: 250건 목록(날짜 내림차순, 일부 기간 밖 섞음) ──────────
ROWS = []
for i in range(250):
    y = 2026 - (i // 60)
    mm = 12 - (i % 12)
    ROWS.append({"DOCID": f"D{i:03d}", "RASMBLY_ID": "061007", "RASMBLY_NM": "가상시의회", "RASMBLY_NUMPR": "9",
                 "RASMBLY_SESN": str(300 - i // 10), "MINTS_ODR": "1", "MTGNM": "본회의" if i % 3 else "기획위원회",
                 "MTG_DE": f"{y}{mm:02d}{(i % 27) + 1:02d}"})
ROWS.sort(key=lambda r: r["MTG_DE"], reverse=True)
ROWS[4] = dict(ROWS[3], DOCID="DUP")      # 같은 회의 다른 docid(판본 중복 후보)
DET = {"LONG": LONG, "T12": T12, "T20": T20}


async def fake_clik(path, params):
    CALLS.append((path, dict(params)))
    if path == "bill.do":
        return [{"RESULT_CODE": "SUCCESS", "TOTAL_COUNT": 0, "LIST": []}]
    if params.get("displayType") == "detail":
        d = params["docid"]
        return [{"RESULT_CODE": "SUCCESS", "DOCID": d, "MINTS_HTML": DET.get(d, T12), "MTG_DE": "20250619",
                 "RASMBLY_NM": "가상시의회", "RASMBLY_NUMPR": "9", "RASMBLY_SESN": "270", "MINTS_ODR": "1", "MTGNM": "기획위원회"}]
    start = int(params.get("startCount", 0))
    rows = ROWS[start:start + int(params.get("listCount", 100))]
    return [{"RESULT_CODE": "SUCCESS", "TOTAL_COUNT": len(ROWS), "LIST": [{"ROW": r} for r in rows]}]


async def main():
    U.clik._raw_get = fake_clik

    print("■ 조사범위·입력 검증")
    CALLS.clear()
    r = await U.council_search_minutes("미래전략과", council="061007", date_from="2026-09-17", date_to="2023-09-17", limit=1)
    check("T18/Q003", "역전 기간은 INVALID_INPUT, 조회 호출 0회", "INVALID_INPUT" in r and not CALLS)
    r = await U.council_find_qa("스마트시티", councils=["061007"], date_from="2025-02-29")
    check("Q004", "존재하지 않는 날짜 거부", "INVALID_INPUT" in r and "존재하지 않는" in r)
    r = await U.council_search_minutes("미래전략과", council="존재하지않는검증전용의회_20260917")
    check("T19/Q007", "없는 의회를 다른 지역으로 대체하지 않음", "찾지 못했습니다" in r and "docid=" not in r)

    print("■ 무손실 페이지 이동")
    want = [x["DOCID"] for x in ROWS if "20240101" <= x["MTG_DE"] <= "20261231"]
    got, cursor, guard = [], None, 0
    while guard < 80:
        guard += 1
        r = await U.council_search_minutes("미래전략과", council="061007", date_from="2024-01-01",
                                           date_to="2026-12-31", limit=5, cursor=cursor)
        got += re.findall(r"docid=(\w+)", r.split("다음 결과")[0])
        m = re.search(r"cursor=(m\d+)", r)
        if not m:
            break
        cursor = m.group(1)
    got = [g for g in got if g in want or g == "DUP"]
    check("T02~T04/Q009", "5건씩 넘겨도 누락·중복 없이 기간 내 전체 순회", sorted(set(got)) == sorted(set(want)) and len(got) == len(set(got)))
    r = await U.council_search_minutes("미래전략과", council="061007", limit=10)
    check("B04", "상류 건수를 조건 충족 건수로 표현하지 않음", "기간·회의유형 조건 충족 건수나 질의 건수가 아닙니다" in r)
    check("B06/Q014", "같은 회의 다른 docid에 판본 중복 후보 표시", "판본 중복 후보" in r)

    print("■ 발언 표기·안건 경계·발언행위")
    a, b = U.parse_turns(T09), U.parse_turns(T10)
    check("T09/Q018", "콜론 표기에서 의원·집행부 인식", [t["role"] for t in a] == ["member", "executive", "member", "executive"])
    check("D01", "콜론·공백 표기 결과 동일", [(t["label"], t["role"]) for t in a] == [(t["label"], t["role"]) for t in b])
    check("T09", "콜론 표기 질의답변 2묶음", len(U.build_qa_pairs(a)) == 2)
    t12 = U.parse_turns(T12)
    pairs = U.build_qa_pairs(t12)
    labels = [x["label"] for x in pairs[0]["answers"]] if pairs else []
    check("T12/Q021", "미래전략과 질의에 관광과 보고가 붙지 않음", len(pairs) == 1 and labels == ["미래전략과장 이가상"])
    check("D04", "전문위원 검토보고는 review로 분리", any(t["act"] == "review" for t in t12))
    commits = [U.classify_commitment(x) for p in pairs for ans in p["answers"] for x in U.split_sentences(ans["text"])]
    commits = [c for c in commits if c]
    check("T12/E03", "후속조치 후보는 미래 제출 1건만", len(commits) == 1 and commits[0]["type"] == "자료제출")
    t20 = U.parse_turns(T20)
    p20 = U.build_qa_pairs(t20)
    check("T20/Q022", "위원장 실질질문 2건 보존, 개의 안내는 제외", len(p20) == 2 and t20[0]["act"] == "procedural")
    check("A06/Q026", "'미래전략과장' 필터가 팀장 대리답변도 찾음", U.dept_match("미래전략과장", "미래전략팀장 박가상"))
    check("A05", "다른 부서는 매칭하지 않음", not U.dept_match("미래전략과장", "관광과장 정가상"))

    print("■ 후속조치 후보 분류")
    sents = U.split_sentences(U.parse_turns(T13)[1]["text"])
    cls = {x: U.classify_commitment(x) for x in sents}
    pick = lambda k: next((v for s_, v in cls.items() if k in s_), "없음")
    check("Q035", "부정 표현 제외", pick("추진하지 않겠습니다") is None)
    check("Q037", "과거 인용 제외", pick("했었습니다") is None)
    check("Q033", "현재 보고 제외", pick("오늘 회의에서") is None)
    c = pick("국비가")
    check("Q036", "조건부 추진 + 조건 보존", bool(c) and c["type"] == "조건부 추진" and "국비" in (c["condition"] or ""))
    c = pick("10월 5일")
    check("Q034", "제출 약속 + 기한(연도 임의 부여 안 함)", bool(c) and c["type"] == "자료제출" and c["deadline"].startswith("10월 5일") and "20" not in c["deadline"])

    print("■ 원문 이어읽기")
    r = await U.council_read_minutes("LONG", max_turns=50)
    ok = "start_turn=50" in r
    r2 = await U.council_read_minutes("LONG", start_turn=100, max_turns=50)
    check("B03/Q012", "120발언 문서를 끝까지 이어읽기", ok and "#119" in r2 and "끝까지 표시" in r2)
    r = await U.council_read_minutes("T12", keyword="스마트시티", whole_agenda=True)
    check("C01", "whole_agenda로 해당 안건 구간 전체 표시(관광과 구간 제외)", "시비 2억원" in r and "관광과장" not in r)

    print("■ 출처 종류")
    r = await U.council_analyze_text(T12, title="합성 시험자료 T12")
    check("F02/Q049", "합성자료는 SYNTHETIC, 공식 회의록 표시 금지", "SYNTHETIC" in r and "공개 회의록" not in r)
    r = await U.council_analyze_text(T20.replace("가상", "명"))
    check("F02", "일반 붙여넣기는 USER_PROVIDED(서버 미검증)", "USER_PROVIDED" in r and "공식 회의록" not in r.split("출처 종류")[0])

    print("■ 표본 한계 표현")
    r = await U.council_find_qa("존재하지않는주제어XYZ", councils=["061007"], max_docs=3)
    check("C05/T15", "찾지 못함은 확인한 회의록 범위로 한정하고 목록 공개", "전체 회의록에 없다는 뜻은 아닙니다" in r and "확인한 회의록:" in r)

    print("\n결과: " + ("모두 통과" if not FAILS else f"실패 {len(FAILS)}건 → {FAILS}"))
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
