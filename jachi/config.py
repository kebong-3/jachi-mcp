from __future__ import annotations
import os
from dataclasses import dataclass


def enabled(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class Settings:
    law_oc: str = ""
    timeout: float = 20.0
    concurrency: int = 3
    cache_ttl: int = 600
    min_interval: float = 0.3
    max_response_bytes: int = 6000000
    max_calls: int = 36
    gemini_api_key: str = ""
    gemini_model: str = ""
    allow_llm: bool = False
    llm_max_input_chars: int = 50000
    llm_max_output_tokens: int = 3000
    host: str = "127.0.0.1"
    port: int = 8000
    allowed_hosts: tuple[str, ...] = ("localhost:*", "127.0.0.1:*")
    allowed_origins: tuple[str, ...] = ("http://localhost:*", "http://127.0.0.1:*")
    api_token: str = ""
    public_read_only: bool = False
    # Direct Settings() callers retain the legacy auto policy; the public
    # distribution's environment entry point explicitly defaults to "none".
    auth_mode: str = "auto"
    allow_public_llm: bool = False

    @property
    def requires_auth(self) -> bool:
        return self.auth_mode == "bearer" or (self.auth_mode == "auto" and bool(self.api_token))

    @property
    def authentication(self) -> str:
        return "bearer" if self.requires_auth else "none"

    @property
    def tool_profile(self) -> str:
        return "read_only" if self.public_read_only else "full"

    @classmethod
    def from_env(cls) -> "Settings":
        render = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
        allowed = tuple(x.strip() for x in os.getenv("ALLOWED_HOSTS", render or "localhost:*,127.0.0.1:*").split(",") if x.strip())
        origins = tuple(x.strip() for x in os.getenv("ALLOWED_ORIGINS", "https://"+render if render else "http://localhost:*,http://127.0.0.1:*").split(",") if x.strip())
        return cls(law_oc=os.getenv("LAW_OC", "").strip(),
                   timeout=max(1, min(60, float(os.getenv("LAW_TIMEOUT", "20")))),
                   concurrency=max(1, min(6, int(os.getenv("LAW_CONCURRENCY", "3")))),
                   cache_ttl=max(0, min(3600, int(os.getenv("CACHE_TTL", "600")))),
                   min_interval=max(.1, float(os.getenv("LAW_MIN_INTERVAL", ".3"))),
                   max_calls=max(1, min(80, int(os.getenv("LAW_MAX_CALLS", "36")))),
                   gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
                   gemini_model=os.getenv("GEMINI_MODEL", ""),
                   allow_llm=enabled("ALLOW_EXTERNAL_LLM"),
                   host=os.getenv("MCP_HOST", "0.0.0.0" if render else "127.0.0.1"),
                   port=int(os.getenv("PORT", "8000")), allowed_hosts=allowed,
                   allowed_origins=origins, api_token=os.getenv("MCP_API_TOKEN", ""),
                   public_read_only=enabled("MCP_PUBLIC_READ_ONLY"),
                   auth_mode=os.getenv("MCP_AUTH_MODE", "none").strip().lower(),
                   allow_public_llm=enabled("ALLOW_PUBLIC_LLM"))

    def check_http(self) -> None:
        if self.auth_mode not in {"none", "bearer", "auto"}:
            raise ValueError("MCP_AUTH_MODE는 none, bearer, auto 중 하나여야 합니다.")
        if not self.allowed_hosts or "*" in self.allowed_hosts or "*" in self.allowed_origins:
            raise ValueError("ALLOWED_HOSTS/ALLOWED_ORIGINS에 전체 허용 '*'를 사용하지 마세요.")
        if self.requires_auth and len(self.api_token) < 32:
            raise ValueError("Bearer 모드의 MCP_API_TOKEN은 32자 이상의 무작위 비밀값이어야 합니다.")
        if self.host not in {"127.0.0.1", "localhost", "::1"}:
            if self.auth_mode == "auto" and not self.api_token and not self.public_read_only:
                raise ValueError("공개 운영은 MCP_AUTH_MODE=none, 인증 운영은 MCP_AUTH_MODE=bearer와 MCP_API_TOKEN을 설정하세요.")
        if self.public_read_only and self.allow_llm:
            raise ValueError("읽기 전용 프로필에서는 외부 LLM 실행을 허용하지 않습니다.")
        if not self.requires_auth and self.allow_llm and not self.allow_public_llm:
            raise ValueError("인증 없는 서버의 외부 LLM은 기본 차단됩니다. ALLOW_EXTERNAL_LLM=false를 사용하거나 운영자가 비용·전송 위험을 검토한 후 ALLOW_PUBLIC_LLM=true를 명시하세요.")
