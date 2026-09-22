# 인증 없는 공개 운영 — 2.0.0-rc.2-public

이 문서는 rc.1의 토큰 필수·공개 읽기 전용 안내를 대체합니다. 이용자 로그인 없이 16개 조례 검토 도구를 제공하는 변경입니다. 법적 결론의 정확성을 추가로 보증하는 변경은 아닙니다.

## Render

기존 LAW_OC 인증값은 유지하고 아래 설정을 사용합니다.

```text
MCP_AUTH_MODE=none
MCP_PUBLIC_READ_ONLY=false
MCP_HOST=0.0.0.0
ALLOW_EXTERNAL_LLM=false
ALLOW_PUBLIC_LLM=false
```

none 모드에서는 기존 MCP_API_TOKEN이 남아 있어도 무시합니다. 인증과 도구 프로필은 별개입니다. MCP_PUBLIC_READ_ONLY=true는 12개 도구 제한 프로필이므로 전체 기능 공개에는 사용하지 않습니다.

시작 명령은 `python jachibeopgyu_mcp.py --http`입니다. MCP 연결 URL은 `https://jachi-mcp.onrender.com/mcp`, 상태 확인은 `/health`입니다. ChatGPT 연결에서 인증 없음 / No Authentication을 선택하고 도구 목록을 새로고침합니다. MCP 서버 공개와 모든 이용자의 ChatGPT 계정에 자동 설치하는 것은 별개입니다.

상태 응답의 authentication은 none, tool_profile은 full, tool_count는 16이어야 합니다. 이는 예상 설정이며 실제 운영 서버의 검증 결과는 배포 후 따로 확인해야 합니다. health 성공만으로 법제처 API 조회 성공이나 ChatGPT 연결 성공을 판단하지 마세요.

## 유지하는 보호 조치

Host·Origin 검사, 요청 본문 1,800,000바이트 제한, 프로세스 전체 분당 60개 요청 제한과 동시접속 제한을 유지합니다. 인증 없음은 무제한 호출이 아닙니다. ALLOWED_HOSTS/ALLOWED_ORIGINS에 *를 사용하지 마세요. 별도 브라우저 클라이언트의 Origin은 운영자가 명시적으로 허용해야 합니다.

법제처 인증값과 Gemini 키를 이용자에게 배포하지 않습니다. 공개 가능한 조례·사업 설명만 입력하고 개인정보·비공개 내부자료·인증키는 입력하지 마세요. 키를 공개하지 않아도 외부인의 호출은 운영자 서버 자원과 상위 API 한도를 사용합니다.

기본 8역할은 규칙 기반이며 서버가 Gemini를 호출하지 않습니다. ChatGPT가 MCP 결과를 해석하는 것과 서버 내부 Gemini 호출은 별개입니다. 공개 서버의 외부 모델 호출에는 운영자의 ALLOW_PUBLIC_LLM 추가 허용과 기존 요청별 전송 동의가 필요하며, 기본값은 비활성화입니다.

## 검증 범위와 복구

제작 중 로컬 핵심·경계 시험 145개가 통과했고 실제 MCP SDK가 없는 환경에서 SDK 모듈 1개는 건너뛰었습니다. HTTPGuard 경계 시험은 MCP 핸드셰이크 시험이 아닙니다. 이 기록 자체는 운영 배포나 실제 법령 검색 성공을 뜻하지 않습니다. 공개 접근 회귀 시험과 실제 SDK 통합시험은 저장소 테스트에 추가했습니다. GitHub Actions 실행 결과는 별도 확인해야 합니다.

고정 Bearer 인증으로 되돌리려면 MCP_AUTH_MODE=bearer와 32자 이상의 무작위 MCP_API_TOKEN을 설정합니다. 이는 OAuth 서버가 아닙니다. 코드에서 직접 Settings()를 만드는 기존 호출자는 auth_mode=auto 호환 동작을 유지하고, 환경변수 진입점 Settings.from_env()는 none을 기본값으로 사용합니다.

기존 docs/qa 및 rc.1 안내는 이전 버전 기록입니다. 기존 MANIFEST.sha256은 rc.1 배포파일 목록이며 이번 공개 접근 변경 전체를 검증하는 증명으로 사용하지 마세요.

공식 참고:
- https://developers.openai.com/api/docs/guides/developer-mode
- https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
- https://render.com/docs/configure-environment-variables
