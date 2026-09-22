from __future__ import annotations

import asyncio
import copy
import json
import time
from collections import OrderedDict
from typing import Any

import httpx
from defusedxml import ElementTree as ET
from .config import Settings
from .models import DocumentRef, digest, utcnow
from .normalize import listing_rows, total_count, parse_document, region_match, compact

SEARCH_URL = "https://www.law.go.kr/DRF/lawSearch.do"
SERVICE_URL = "https://www.law.go.kr/DRF/lawService.do"


class UpstreamError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class BudgetExceeded(UpstreamError):
    def __init__(self):
        super().__init__("call_budget", "요청별 API 호출 한도에 도달했습니다. 수집 범위는 부분 완료입니다.")


def xml_value(el):
    children = list(el)
    if not children:
        return el.text or ""
    out = dict(el.attrib)
    for child in children:
        key = child.tag.split("}")[-1]
        val = xml_value(child)
        if key in out:
            if not isinstance(out[key], list):
                out[key] = [out[key]]
            out[key].append(val)
        else:
            out[key] = val
    return out


def parse_payload(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8-sig", errors="strict").strip()
    except UnicodeDecodeError:
        raise UpstreamError("invalid_encoding", "UTF-8 응답 해석 실패") from None
    if not text:
        raise UpstreamError("empty_response", "법제처가 빈 응답을 반환했습니다.")
    try:
        if text.startswith(("{", "[")):
            result = json.loads(text)
        elif text.startswith("<") and not text.lower().startswith(("<!doctype html", "<html")):
            root = ET.fromstring(text)
            result = {root.tag: xml_value(root)}
        else:
            raise UpstreamError("non_data_response", "JSON/XML 법령 응답이 아닙니다. 인증값·등록 IP·서비스 상태를 확인하세요.")
    except UpstreamError:
        raise
    except Exception:
        raise UpstreamError("malformed_response", "응답을 안전하게 해석하지 못했습니다. 원문 오류내용은 노출하지 않습니다.") from None
    if not isinstance(result, (dict, list)):
        raise UpstreamError("schema_error", "예상하지 않은 응답 형식입니다.")
    # API errors can be HTTP 200; never normalize an error as an empty search.
    def error_nodes(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k.lower() in {"error", "errormsg", "errormessage", "errorcode"} and v:
                    return True
                if k in {"resultCode", "RESULT_CODE"} and str(v).upper() not in {"0", "00", "200", "SUCCESS", "INFO-000"}:
                    return True
                if error_nodes(v):
                    return True
        if isinstance(x, list):
            return any(error_nodes(v) for v in x)
        return False
    if error_nodes(result):
        raise UpstreamError("upstream_reported_error", "API가 오류를 보고했습니다. LAW_OC와 요청 IP 등록을 확인하세요.")
    return result


class OfficialCache:
    """Bounded in-process cache for public upstream payloads only, separated by credential hash."""
    def __init__(self, capacity: int = 128):
        self.capacity = capacity
        self.data: OrderedDict[str, tuple[float, str, Any]] = OrderedDict()

    def get(self, key: str):
        if key not in self.data:
            return None
        self.data.move_to_end(key)
        return self.data[key]

    def put(self, key: str, value: Any):
        self.data[key] = (time.monotonic(), utcnow(), copy.deepcopy(value))
        self.data.move_to_end(key)
        while len(self.data) > self.capacity:
            self.data.popitem(last=False)


CACHE = OfficialCache()


class LawClient:
    def __init__(self, settings: Settings | None = None, budget: int | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, cache: OfficialCache | None = None):
        self.settings = settings or Settings.from_env()
        self.budget = min(budget or self.settings.max_calls, self.settings.max_calls)
        self.calls = 0
        self.cache_hits = 0
        self.cache = cache if cache is not None else CACHE
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.settings.timeout),
                                         follow_redirects=False, transport=transport,
                                         limits=httpx.Limits(max_connections=self.settings.concurrency),
                                         headers={"User-Agent": "Jachi-MCP/2.0 ordinance-research"})
        self._sem = asyncio.Semaphore(self.settings.concurrency)
        self._rate_lock = asyncio.Lock()
        self._last = 0.0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self._client.aclose()

    async def request(self, endpoint: str, params: dict, allow_stale: bool = False) -> tuple[Any, dict]:
        if not self.settings.law_oc or self.settings.law_oc == "test":
            raise UpstreamError("missing_credential", "LAW_OC를 본인 신청 API 인증값으로 설정하세요. 'test' 기본키는 사용하지 않습니다.")
        if endpoint not in {"search", "service"}:
            raise ValueError("지원하지 않는 endpoint")
        clean_params = {k:v for k,v in params.items() if v not in ("", None)}
        if "OC" in clean_params or "type" in clean_params:
            raise ValueError("인증값과 응답형식은 클라이언트 설정으로만 관리합니다.")
        key = digest([digest(self.settings.law_oc), endpoint, clean_params])
        cached = self.cache.get(key)
        if cached and time.monotonic()-cached[0] < self.settings.cache_ttl:
            self.cache_hits += 1
            return copy.deepcopy(cached[2]), {"source_state": "cache", "retrieved_at": cached[1]}
        url = SEARCH_URL if endpoint == "search" else SERVICE_URL
        error = None
        for attempt in range(3):
            if self.calls >= self.budget:
                raise BudgetExceeded()
            self.calls += 1
            async with self._sem:
                async with self._rate_lock:
                    await asyncio.sleep(max(0.0, self.settings.min_interval-(time.monotonic()-self._last)))
                    self._last = time.monotonic()
                try:
                    async with self._client.stream("GET", url, params={**clean_params,"OC":self.settings.law_oc,"type":"JSON"}) as resp:
                        if resp.status_code in {429, 500, 502, 503, 504}:
                            retry_after = resp.headers.get("Retry-After", "")
                            delay = min(10, float(retry_after)) if retry_after.isdigit() else 0.5*(2**attempt)
                            error = UpstreamError("retryable_http", "법제처가 일시적 제한/장애를 반환했습니다.")
                            if attempt < 2:
                                await asyncio.sleep(delay)
                                continue
                            break
                        if not 200 <= resp.status_code < 300:
                            raise UpstreamError("http_error", f"법제처 응답 HTTP {resp.status_code}. 인증/IP/요청값을 확인하세요.")
                        pieces, size = [], 0
                        async for chunk in resp.aiter_bytes():
                            size += len(chunk)
                            if size > self.settings.max_response_bytes:
                                raise UpstreamError("response_too_large", "응답 크기 한도를 초과했습니다. 범위를 줄이세요.")
                            pieces.append(chunk)
                    data = parse_payload(b"".join(pieces))
                    self.cache.put(key, data)
                    return data, {"source_state":"live","retrieved_at":utcnow()}
                except httpx.TransportError:
                    error = UpstreamError("network_error", "공식 API 연결에 실패했습니다. 조회 실패는 검색 0건이 아닙니다.")
                    if attempt < 2:
                        await asyncio.sleep(.5*(2**attempt))
                        continue
        if allow_stale and cached:
            return copy.deepcopy(cached[2]), {"source_state":"stale","retrieved_at":cached[1],"warning":"연결 실패: 이전 수집자료이며 최신성 미확인"}
        raise error or UpstreamError("unknown_error", "자료 조회에 실패했습니다.")

    async def search(self, query: str, kind: str = "ordinance", jurisdiction: str = "", *,
                     body: bool = False, max_pages: int = 3, page: int = 1, display: int = 100,
                     org: str = "", sborg: str = "", history: bool = False,
                     target: str = "", extra: dict | None = None) -> dict:
        if page < 1:
            raise ValueError("page는 1 이상입니다.")
        if len(query) > 200 or not query.strip():
            raise ValueError("검색어는 1~200자입니다.")
        if sborg and not org:
            raise ValueError("sborg를 사용하려면 org도 지정해야 합니다.")
        max_pages, display = max(1,min(max_pages,12)), max(1,min(display,100))
        results, pages, failures, total, raw_count = [], [], [], None, 0
        next_page, exhausted = page, False
        seen_pages = set()
        params = {"target": target or ("ordin" if kind=="ordinance" else "eflaw"),
                  "query":query,"display":display,"search":2 if body else 1}
        if not target and kind == "ordinance":
            params.update({"nw":2 if history else 1,"org":org,"sborg":sborg,"knd":"30001"})
        params.update(extra or {})
        for p in range(page, page+max_pages):
            try:
                data, meta = await self.request("search", {**params, "page":p})
                rows, count = listing_rows(data, kind), total_count(data)
                if count is not None:
                    total = count
                # Without rows AND known zero total, schema/permission errors are not an empty search.
                if not rows and count is None:
                    raise UpstreamError("unknown_search_schema", "검색 결과 구조 또는 총건수 확인 실패")
                signature = digest(rows)
                if rows and signature in seen_pages:
                    failures.append({"page":p,"code":"repeated_page","message":"API가 이전 페이지를 반복하여 수집을 중단했습니다."})
                    break
                seen_pages.add(signature)
                pages.append({"page":p,"received":len(rows),**meta})
                raw_count += len(rows)
                results.extend(rows)
                next_page = p+1
                observed_end=(page-1)*display+raw_count
                if total is not None and observed_end < total and len(rows) < display:
                    failures.append({"page":p,"code":"inconsistent_page_count","message":"총건수에 비해 반환 행이 부족하여 완전 수집으로 표시하지 않습니다."})
                    break
                if total is not None and observed_end > total:
                    failures.append({"page":p,"code":"inconsistent_total","message":"총건수와 반환 행 수가 일치하지 않습니다."})
                    break
                if not rows or len(rows) < display or (total is not None and observed_end >= total):
                    exhausted = True
                    break
            except UpstreamError as e:
                failures.append({"page":p,"code":e.code,"message":str(e)})
                next_page = p
                break
        dedup = list({(r["document_id"],r["mst"],r["parent_article"],r["ordinance_article"]):r for r in results}.values())
        orgs = sorted({x["jurisdiction"] for x in dedup if x["jurisdiction"]})
        selected = [r for r in dedup if region_match(r["jurisdiction"], jurisdiction)] if jurisdiction else dedup
        status = "complete" if exhausted and not failures else ("partial" if pages else "unavailable")
        return {"query":query,"kind":kind,"status":status,"api_total":total,
                "scanned_rows":raw_count,"matched_in_scanned_rows":len(selected),"results":selected,
                "coverage":{"pages":pages,"next_page":None if exhausted else next_page,
                            "query_exhausted":exhausted,"query_fully_scanned":exhausted and page==1 and not failures,
                            "first_scanned_page":page,"national_exhaustive":False,
                            "scope":"이 검색어·필터에 한정한 결과이며 전국 모든 관련 법규의 완전성을 보증하지 않음"},
                "observed_jurisdictions":orgs,"failures":failures,
                "warning":"검색 0건을 미제정/법적 근거 없음으로 확정하지 마세요. 정식 제명·본문·동의어·기관코드로 재확인하세요."}

    async def get_document(self, ref: DocumentRef, allow_stale: bool = False):
        # An explicit version always wins. Never send both ID and MST: ID can silently force current.
        params = {"target":"ordin" if ref.kind=="ordinance" else ("law" if ref.law_view=="promulgated" else "eflaw")}
        if ref.mst:
            params.update({"MST":ref.mst})
            if ref.kind=="law" and ref.law_view=="effective":
                params["efYd"] = ref.effective_date
        else:
            params["ID"] = ref.document_id
        data, meta = await self.request("service", params, allow_stale=allow_stale)
        doc = parse_document(data,ref.kind,ref.document_id,ref.mst,ref.effective_date,
                             source_state=meta["source_state"],version_scope="promulgated" if ref.law_view=="promulgated" else ("version" if ref.mst else "current"))
        doc.retrieved_at = meta["retrieved_at"]
        if ref.document_id and doc.document_id != ref.document_id:
            raise UpstreamError("identity_mismatch", "요청한 식별자와 반환된 법령 식별자가 다릅니다.")
        if ref.mst and doc.version and ref.mst != doc.version:
            raise UpstreamError("version_mismatch", "요청한 버전과 반환된 법령 버전이 다릅니다.")
        if ref.kind=="law" and ref.law_view=="effective" and ref.mst and compact(doc.effective_date).replace("-","") != ref.effective_date:
            doc.warnings.append("요청 시행일과 반환 시행일 불일치: 조문별 시행일과 부칙을 확인하세요.")
        if ref.title_hint and compact(doc.title)!=compact(ref.title_hint):
            raise UpstreamError("title_mismatch", "지정한 정식 제명과 반환된 제명이 다릅니다. 제명변경 여부를 재확인하세요.")
        return doc

    async def linked_ordinances(self, law_id: str, article: str = "", max_pages: int = 3):
        from .normalize import article_order
        if not law_id.isdigit():
            raise ValueError("상위법 법령ID가 필요합니다.")
        extra = {"knd":law_id}
        target = "lnkLsOrd"
        if article:
            n,b = article_order(article)
            if not (1 <= n <= 9999 and 0 <= b <= 99):
                raise ValueError("조문은 제20조 또는 제20조의2 형식입니다.")
            extra.update({"JO":f"{n:04d}","JOBR":f"{b:02d}"})
            target = "lnkLsOrdJo"
        return await self.search("*", target=target, extra=extra, max_pages=max_pages)
