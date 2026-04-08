# SAGA-MEM

RisuAI용 리버스 프록시 + LangMem 기반 장기 기억 엔진.

RisuAI의 SupaMemory/HypaMemory를 대체하여 에피소드 메모리 추출, 벡터 검색, 프롬프트 주입을 수행한다.

## Architecture

```
RisuAI → POST /v1/chat/completions → SAGA-MEM (:8000) → upstream LLM
                                        |
                                   [동기] 에피소드 검색 → 메모리 주입 → SSE 패스쓰루
                                   [비동기] Gemini Flash로 에피소드 추출 → 벡터 저장
```

## Quick Start

```bash
# 1. 의존성 설치
uv sync

# 2. 환경변수 설정
cp .env.example .env
# .env 편집: SAGA_OPENAI_API_KEY, SAGA_GOOGLE_API_KEY 입력

# 3. 서버 실행
uv run uvicorn app.main:app --port 8000 --reload

# 4. RisuAI 설정
#    API URL → http://localhost:8000
#    SupaMemory/HypaMemory → OFF
```

## Tech Stack

| 역할 | 기술 |
|------|------|
| 프레임워크 | FastAPI + httpx (리버스 프록시, SSE 패스쓰루) |
| 메모리 | LangMem (create_memory_store_manager + ReflectionExecutor) |
| 스토어 | LangGraph InMemoryStore (Phase 4: AsyncPostgresStore) |
| 추출 모델 | Gemini 2.5 Flash (무료) |
| 임베딩 | OpenAI text-embedding-3-small |

## Cost (100 turns)

| 작업 | 비용 |
|------|------|
| 메인 RP 응답 | 유저 부담 (RisuAI 패스쓰루) |
| 에피소드 추출 | $0 (Gemini 무료) |
| 임베딩 | ~$0.02 |

## Roadmap

- [x] **Phase 1** — 리버스 프록시 + 에피소드 메모리
- [ ] **Phase 2** — 압축 + 4-stage RRF 검색 + 캐릭터 상태
- [ ] **Phase 3** — 큐레이터 (NPC 레지스트리, 모순 검사, 복선 추적)
- [ ] **Phase 4** — SystemStabilizer + AsyncPostgresStore + 비용 추적
