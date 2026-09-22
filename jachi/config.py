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
                   public_read_only=enabled("MCP_PUBLIC_READ_ONLY"))

    def check_http(self) -> None:
        if not self.allowed_hosts or "*" in self.allowed_hosts or "*" in self.allowed_origins:
            raise ValueError("ALLOWED_HOSTS/ALLOWED_ORIGINS에 전체 허용 '*'를 사용하지 마세요.")
        if self.host not in {"127.0.0.1", "localhost", "::1"}:
            if not self.api_token and not self.public_read_only:
                raise ValueError("외부 HTTP는 MCP_API_TOKEN 또는 명시적인 MCP_PUBLIC_READ_ONLY=true가 필요합니다.")
            if self.api_token and len(self.api_token) < 32:
                raise ValueError("MCP_API_TOKEN은 32자 이상의 무작위 비밀값이어야 합니다.")
        if self.public_read_only and self.allow_llm:
            raise ValueError("인증 없는 공개 모드에서는 유료 LLM 실행을 허용하지 않습니다.")
