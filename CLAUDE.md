# SAGA-MEM — RP Memory Engine

RisuAI 뒤에서 동작하는 LangMem 기반 장기 기억 + 서사 관리 리버스 프록시 엔진.
RisuAI의 SupaMemory/HypaMemory를 OFF하고, 이 엔진이 메모리/압축/큐레이션을 전부 담당한다.

## 핵심 원칙

- **RisuAI를 대체하지 않는다** — 프론트엔드는 RisuAI, 우리는 미들웨어
- **리버스 프록시 패턴** — RisuAI 프롬프트를 그대로 LLM에 전달, ⑥번 메모리 슬롯에만 주입
- **LLM 1회 호출** — 메인 응답은 동기 1회, 나머지(에피소드 추출, 큐레이션)는 비동기

## 아키텍처

```
RisuAI → POST /v1/chat/completions → 우리 엔진 (:8000)
  [동기]  메모리 검색 + ⑥슬롯 주입 + httpx SSE 패스쓰루
  [비동기] 에피소드 추출 (LangMem) + 큐레이터 (10턴마다)
```

## 기술 스택

- FastAPI + httpx (리버스 프록시, SSE 패스쓰루)
- LangMem (create_memory_store_manager + ReflectionExecutor)
- LangGraph Store (InMemoryStore → AsyncPostgresStore)
- SAGA 패턴 참고 (MessageCompressor, WindowRecovery, Curator)

## 문서 구조

| 문서 | 용도 |
|------|------|
| **PLAN.md** | 구현 계획 (Phase 1~4, 파일 구조, 비용) |
| **TECHNICAL_REFERENCE.md** | 상세 기술 사양 (프롬프트 구조, 검색 전략, 큐레이터, LangMem API, charx 분석) |

## 참조 문서 (같은 디렉토리)

| 문서 | 내용 |
|------|------|
| RisuAI_제작_가이드.md | RisuAI 전체 시스템 (프롬프트 조립, Regex, 변수, 메모리, 모듈) |
| 캐릭터_설계_방법론.md | Layered Persona 5계층 + 실제 charx 분석 (위지소연, Fate HGW) |
| 시나리오_제작_총정리.md | 장르별 설계 패턴, 메모리 선택 가이드 |

## 참조 문서 (외부)

| 문서 | 위치 | 내용 |
|------|------|------|
| SAGA README | /Users/yanghyeon-u/Desktop/RISU_ENE/README.md | 캐싱 3모듈, 3-Agent 파이프라인, 벤치마크 |
| AGENTS.md | /Users/yanghyeon-u/Desktop/LLM-FastApi/AGENTS.md | 코드 스타일 규칙 |

## charx 샘플 및 에셋

> **주의:** `캐릭터카드-에셋모음/` 폴더는 대용량(이미지 수백장 포함). 컨텍스트에 통째로 읽지 말 것.
> 필요할 때 **특정 파일만** 읽을 것 (예: `card.json`, `module.risum`).

**charx 파일 (압축 상태):**

| 파일 | 특징 |
|------|------|
| 현대 던전 시뮬 (1).charx | 15+ 캐릭터, 키워드 트리거, 관계 정의 |
| 위지소연 (1).charx | 14MB, 142이미지, 이미지 커맨드 시스템 |
| Fate HGW V1.charx | 80MB, 767 로어북, 변수 시스템, 풀 게임 |

**압축 해제된 에셋 (분석용):**

```
캐릭터카드-에셋모음/
├── 위지소연/          ← card.json + 이미지 142장
├── 현대던전시뮬/       ← card.json + module.risum
├── Fate HGW V1/      ← card.json (767 로어북) + 이미지 772장
└── 던전 보스가 되었다/  ← 추가 샘플
```

**분석할 때:** `card.json`의 `character_book.entries`가 로어북, `data.description`이 캐릭터 설정.
charx 구조 상세는 TECHNICAL_REFERENCE.md 섹션 14 참조.

## 코드 스타일 (AGENTS.md 준수)

- import는 파일 최상단
- structlog 사용, 이벤트명 lowercase_underscore, f-string 금지
- tenacity로 retry (exponential backoff)
- Pydantic 모델 필수
- slowapi rate limiting
- async def + 타입 힌트
- 에러는 early return, logger.exception()

## 현재 Phase

Phase 1 구현 준비 완료. 순수 프록시 먼저 → 에피소드 메모리 추가.

```
app/
├── main.py       ← FastAPI 앱
├── config.py     ← 설정
├── schemas.py    ← RPEpisode, OpenAI 모델
├── proxy.py      ← /v1/chat/completions (핵심)
├── memory.py     ← Store 초기화, 검색
└── extractor.py  ← ReflectionExecutor
```
