from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Article(Strict):
    key: str
    label: str
    title: str = ""
    text: str
    deleted: bool = False
    effective_date: str = ""


class Document(Strict):
    """Internal model. Public tools never accept source_state/verified from callers."""
    kind: Literal["ordinance", "law", "draft"]
    document_id: str
    title: str
    jurisdiction: str = ""
    version: str = ""
    effective_date: str = ""
    promulgation_date: str = ""
    source_url: str = ""
    retrieved_at: str = Field(default_factory=utcnow)
    source_state: Literal["live", "cache", "stale", "user_provided", "fixture"] = "user_provided"
    version_scope: Literal["current", "version", "promulgated", "unknown"] = "unknown"
    articles: list[Article] = Field(default_factory=list)
    supplementary: list[str] = Field(default_factory=list)
    annexes: list[dict[str, str]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    department: str = ""
    phone: str = ""
    amendment_reason: str = ""
    upstream_hash: str = ""

    @property
    def content_hash(self) -> str:
        return digest({"title": self.title, "jurisdiction": self.jurisdiction,
                       "effective": self.effective_date,
                       "articles": [a.model_dump() for a in self.articles],
                       "supplementary": self.supplementary, "annexes": self.annexes})

    @property
    def identity(self) -> str:
        return f"{self.kind}:{self.document_id}:{self.version}:{self.version_scope}:{self.content_hash[:16]}"

    def summary(self) -> dict[str, Any]:
        return {**self.model_dump(exclude={"articles", "supplementary", "annexes", "amendment_reason"}),
                "identity": self.identity, "content_hash": self.content_hash,
                "article_count": len(self.articles), "supplement_count": len(self.supplementary),
                "annex_count": len(self.annexes)}


class DocumentRef(Strict):
    kind: Literal["ordinance", "law"] = "ordinance"
    document_id: str = Field(default="", pattern=r"^\d*$", max_length=24)
    mst: str = Field(default="", pattern=r"^\d*$", max_length=24)
    effective_date: str = ""
    title_hint: str = Field(default="", max_length=300)
    law_view: Literal["effective", "promulgated"] = "effective"

    @model_validator(mode="after")
    def needs_identifier(self):
        if not self.document_id and not self.mst:
            raise ValueError("검색에서 얻은 document_id 또는 mst가 필요합니다.")
        if self.kind == "ordinance" and self.law_view != "effective":
            raise ValueError("law_view=promulgated는 법령에만 사용할 수 있습니다.")
        if self.kind == "law" and self.law_view == "effective" and self.mst and not self.effective_date:
            raise ValueError("상위법 특정 버전 조회에는 mst와 시행일 effective_date를 함께 지정하세요.")
        return self

    @field_validator("effective_date")
    @classmethod
    def valid_date(cls, value):
        if value:
            value = value.replace("-", "")
            if len(value) != 8 or not value.isdigit():
                raise ValueError("날짜는 YYYYMMDD 또는 YYYY-MM-DD 형식입니다.")
            datetime.strptime(value, "%Y%m%d")
        return value


class UserText(Strict):
    title: str = Field(min_length=1, max_length=300)
    text: str = Field(min_length=1, max_length=160000)
    jurisdiction: str = Field(default="", max_length=120)
    kind: Literal["ordinance", "law", "draft"] = "draft"
    source_url: str = Field(default="", max_length=2000)


class ReviewInput(Strict):
    project: str = Field(min_length=2, max_length=16000,
                         description="공개 가능한 사업 목적·대상·지원방법. 개인정보·비공개자료 금지")
    jurisdiction: str = Field(min_length=2, max_length=120)
    as_of: date = Field(default_factory=lambda: datetime.now(ZoneInfo("Asia/Seoul")).date())
    topic: str = Field(default="", max_length=150)
    search_terms: list[str] = Field(default_factory=list, max_length=5)
    baseline: DocumentRef | None = None
    comparisons: list[DocumentRef] = Field(default_factory=list, max_length=12)
    parents: list[DocumentRef] = Field(default_factory=list, max_length=10)
    old_parent: DocumentRef | None = None
    new_parent: DocumentRef | None = None
    provided_documents: list[UserText] = Field(default_factory=list, max_length=12)
    mode: Literal["review", "compare", "enact", "amend", "impact"] = "review"
    auto_search: bool = True
    reasoning: Literal["rules", "gemini"] = "rules"
    allow_external_llm: bool = False
    budget_calls: int = Field(default=36, ge=1, le=80)

    @model_validator(mode="after")
    def paired_versions(self):
        if bool(self.old_parent) != bool(self.new_parent):
            raise ValueError("상위법 변경 비교는 old_parent와 new_parent를 함께 지정해야 합니다.")
        if self.reasoning == "gemini" and not self.allow_external_llm:
            raise ValueError("Gemini로 자료를 전송하려면 allow_external_llm=true 동의가 필요합니다.")
        if self.baseline and self.baseline.kind != "ordinance":
            raise ValueError("baseline은 ordinance여야 합니다.")
        if any(r.kind != "ordinance" for r in self.comparisons):
            raise ValueError("comparisons에는 ordinance만 지정하세요.")
        if any(r.kind != "law" for r in self.parents):
            raise ValueError("parents에는 law만 지정하세요.")
        if self.old_parent and (self.old_parent.kind != "law" or self.new_parent.kind != "law"):
            raise ValueError("old_parent/new_parent는 law여야 합니다.")
        return self


class ChangeOperation(Strict):
    operation: Literal["replace_article", "delete_article", "insert_article"]
    article: str = Field(min_length=2, max_length=40)
    expected_text: str = Field(default="", max_length=30000)
    new_text: str = Field(default="", max_length=30000)
    reason: str = Field(min_length=2, max_length=3000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
