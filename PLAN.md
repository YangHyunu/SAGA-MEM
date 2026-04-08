# RP Memory Engine — 구현 계획서

> RisuAI 뒤에서 동작하는 LangMem 기반 장기 기억 + 서사 관리 엔진
> 작성일: 2026-04-05

---

## 1. 프로젝트 정의

### 무엇을 만드는가

RisuAI(RP 프론트엔드)의 LLM API 호출을 가로채는 **리버스 프록시**. 
RisuAI의 SupaMemory/HypaMemory를 OFF하고, 이 엔진이 장기 기억 + 서사 관리를 담당한다.

### 무엇을 만들지 않는가

- RisuAI를 대체하지 않는다 (프론트엔드는 RisuAI 그대로)
- Lorebook, 변수(setvar/getvar), Regex, 감정 이미지, 트리거 스크립트는 RisuAI가 처리
- 우리는 **메모리/컨텍스트/큐레이션**만 담당

### 레퍼런스

| 프로젝트 | 참고 포인트 |
|---------|------------|
| SAGA | 캐싱 최적화 (SystemStabilizer, MessageCompressor), 3-Agent 파이프라인, 큐레이터 |
| LangMem | 에피소드 추출/저장/검색, ReflectionExecutor, NamespaceTemplate, 프로필 메모리 |
| HypaMemory V3 | 검색 전략 (Recent/Similar/Random), 임베딩 기반 메모리 |

---

## 2. 아키텍처

### 전체 흐름

```
RisuAI (프론트엔드)
  │  POST /v1/chat/completions
  ▼
┌─────────────────────────────────────┐
│  RP Memory Engine (:8000)           │
│                                     │
│  [동기] 요청 수신                    │
│    ├─ (Phase 4) SystemStabilizer     │
│    ├─ 에피소드 검색 (4-stage RRF)    │
│    ├─ 세계 상태 / 관계 로드          │
│    ├─ 큐레이션 결과 로드             │
│    └─ 동적 컨텍스트 주입             │
│                                     │
│  [동기] LLM 1회 호출 → SSE 스트리밍  │
│                                     │
│  [비동기] 응답 후                     │
│    ├─ 에피소드 추출/저장 (LangMem)   │
│    ├─ NPC 상태 업데이트              │
│    └─ 10턴마다: 큐레이션 실행         │
└─────────────────────────────────────┘
  │
  ▼
LLM API (Claude, GPT 등)
```

### 프롬프트 삽입 전략

실제 RP 프롬프트 템플릿 기반. 우리 엔진은 ⑥번 위치(기존 메모리 슬롯)에 주입한다.

```
① [system] JB/Main — SYSTEM_RULE + ROLEPLAY_RULE         ← 캐시됨 (시작)
② [system] Persona — 유저 캐릭터 프로필
③ [system] "Supplementary Information"
④ [system] Lorebook (활성 엔트리)                          ← 준고정
⑤ [system] Author's Note
⑥ [system] ★ 우리 엔진 주입 — [Roleplay Summary]          ← 기존 메모리 슬롯 대체
⑦ [system] </ROLEPLAY_INFO>
⑧ [system] RESPONSE_INSTRUCTION
─── CACHE BREAK POINT ───
⑨⑩ [user/asst] Chat History
⑪ [system] 최종 삽입 프롬프트
⑫ [system] Prefill                                        ← 끝 (참조율 높음)
```

**주입 내용 (⑥번 슬롯):**
- 에피소드 리콜 (벡터 검색 결과)
- 세계 상태 + 관계
- 큐레이션 경고 (critical/warning)
- 서사 큐 (narrative cue)

**캐싱 관점:**
- ①②③ (고정) → 항상 캐시 히트
- ④ (준고정, 키워드 따라) → 대부분 캐시 히트
- ⑥ (매 턴 변경) → 캐시 밖이지만, ①~④까지는 prefix 유지

### 플레이스홀더 마커 (유저 커스텀)

RisuAI 프롬프트 템플릿에 Plain 블록으로 배치 가능:
```
<!-- MEMORY_ENGINE:WORLD_STATE -->
<!-- MEMORY_ENGINE:EPISODE_RECALL -->
<!-- MEMORY_ENGINE:NARRATIVE_CUE -->
```
마커가 없으면 위 기본 위치로 폴백.

### 토큰 예산

엔진 주입 총량: **전체 컨텍스트의 3% 이하** (128K 기준 ~3,800 토큰)

| 구간 | 토큰 | 비고 |
|------|------|------|
| world_state | ~1,000 | 느린 변경, prefix |
| relationships | ~500 | 느린 변경, prefix |
| episode_recall | ~1,500 | 3~5개 에피소드, suffix |
| narrative_cue | ~400 | 서사 방향 힌트, suffix |
| curation_warnings | ~400 | 모순 경고 |

---

## 3. Store Namespace 설계

```
store/{user_id}/{card_id}/
  ├── episodes/                      ← 에피소드 메모리
  ├── characters/{char_name}/profile ← NPC 동적 상태 (enable_inserts=False)
  ├── relationships/                 ← 관계 그래프
  ├── npc_registry/                  ← NPC 레지스트리 (alias, 생존상태)
  ├── plot_threads/                  ← 복선 추적
  ├── world_rules/                   ← 세계관 규칙 (로어북에서 추출)
  ├── curation_history/              ← 큐레이션 판단 이력
  └── stable_prefix/                 ← 압축된 서사 요약
  
sessions/{session_id}/
  └── recent_context/                ← 세션별 슬라이딩 윈도우
```

- **card_id 단위 분리**: charx 카드마다 세계관이 다름
- **로어북과의 중복 방지**: 정적 설정은 RisuAI 로어북이 담당, 우리는 동적 변화만

---

## 4. 스키마 설계

### 에피소드

```python
class RPEpisode(BaseModel):
    observation: str              # 장면 요약
    participants: list[str]       # 등장 캐릭터
    scene_type: str               # combat, dialogue, exploration, emotional
    location: str                 # 장소
    emotional_tone: str           # 감정 톤
    player_action: str            # 유저 행동 요약
    consequence: str              # 결과/세계 변화
    importance: float             # 0.0~1.0
    turn_range: tuple[int, int]   # 턴 범위
```

importance 기준:
| 점수 | 기준 | 예시 |
|------|------|------|
| 0.9~1.0 | 세계관 변화, 사망, 핵심 비밀 | "한결이 유저 정체를 알게 됨" |
| 0.7~0.8 | 관계 변화, 전투, 장소 이동 | "이오네와 호감도 상승" |
| 0.4~0.6 | 일상, 정보 교환, 탐색 | "헌터협회 브리핑" |
| 0.1~0.3 | 사소한 대화, 반복 행동 | "일상 인사" |

### 캐릭터 상태 (프로필 메모리)

```python
class CharacterState(BaseModel):      # enable_inserts=False
    name: str
    hp: int
    location: str
    emotional_state: str
    active_effects: list[str]
    last_action: str
```

### 관계

```python
class Relationship(BaseModel):
    source: str
    target: str
    relation_type: str                # 동맹, 적대, 호감, 중립
    trust_level: float                # -1.0 ~ 1.0
    key_events: list[str]             # 관계 변화 계기 (최근 5개)
```

### 큐레이션

```python
class CurationFinding(BaseModel):
    finding_type: str                 # existence_contradiction, personality_drift, ...
    severity: str                     # critical, warning, info
    description: str
    evidence_turns: list[int]
    suggested_fix: str

class NPCRegistryEntry(BaseModel):
    canonical_name: str
    aliases: list[str]
    status: str                       # alive, dead, absent
    last_seen_turn: int
    last_seen_location: str

class PlotThread(BaseModel):
    title: str
    status: str                       # open, resolved, abandoned
    opened_at_turn: int
    related_npcs: list[str]
```

---

## 5. 검색 전략: 4-Stage RRF

| Stage | 소스 | 가중치 | 역할 |
|-------|------|--------|------|
| Recent | 최근 N개 에피소드 | 1.0 | 직전 맥락 연속성 |
| Important | importance >= 0.7 | 1.2 | 핵심 서사 이벤트 |
| Similar | 벡터 유사도 검색 | 0.8 | 관련 기억 회상 |
| Character | participants 매칭 | 0.6 | 현재 등장인물 관련 |

- HypaMemory의 Random(0.2)은 제거 — RP에서 일관성 > 다양성
- LangMem store.search로 Similar, 별도 쿼리로 나머지, 앱 레이어에서 RRF 융합

---

## 6. 큐레이터 설계

### 실행 구조

```
10턴마다 asyncio.create_task로 비동기 실행
  │
  ├─ NPC 레지스트리 갱신 (alias match → exact → LLM dedup)
  ├─ 모순 검사 (존재, 시공간, 성격, 변수-서사)
  ├─ 복선 추적 갱신
  │
  └─ CurationReport → store에 저장 → 다음 턴 프롬프트에 반영
```

### 점진 롤아웃

| Phase | 내용 | LLM 호출 | 모델 |
|-------|------|---------|------|
| **1** | NPC 레지스트리 + 존재 모순 | Flash 1회 | gpt-5-nano / Gemini Flash |
| **2** | + 복선 추적 + 성격 이탈 | Flash 1 + Mini 1 | |
| **3** | + 서사 압축 + 로어 자동생성 | Flash 1 + Mini 2 | |

### 모순 검사 항목

| Tier | 유형 | 탐지 방법 |
|------|------|----------|
| **1** | 존재 모순 (죽은 NPC 재등장) | NPC 레지스트리 status 확인 |
| **1** | 시공간 모순 (이동 없이 장소 변경) | 에피소드 location 추적 |
| **1** | 이름 중복 (한/영 별도 등록) | alias match → LLM dedup |
| **2** | 성격 이탈 | 캐릭터 설정 vs 최근 행동 벡터 유사도 |
| **2** | 미회수 복선 | PlotThread status=open, N턴 이상 경과 |
| **3** | 세계관 규칙 위반 | world_rules와 에피소드 교차 검증 |

### 프롬프트 반영

- critical → @@@end 위치 (Jailbreak 직전, 최대 영향력)
- warning/info → Author's Note depth 4

### 실패 처리

큐레이션 실패 시 메인 응답에 영향 없음 (graceful degradation). 3회 연속 실패 시 logger.error.

---

## 7. RisuAI 동적 요소와의 관계

| 요소 | 처리 주체 | 우리 엔진과의 관계 |
|------|----------|-----------------|
| Lorebook (키워드 트리거) | RisuAI | 건드리지 않음. 벡터 검색으로 보완 가능 |
| 변수 (setvar/getvar) | RisuAI | Read-Only Mirror — AI 응답에서 비동기 파싱 |
| Regex Modify Output | RisuAI | 적용 후 데이터를 "진실"로 간주 |
| Regex Modify Display | RisuAI | 영향 없음 (데이터 불변) |
| 감정 이미지 | RisuAI | 영향 없음 (클라이언트 처리) |
| SupaMemory/HypaMemory | **OFF** | 우리 엔진이 대체 |

---

## 8. 기술 스택

| 계층 | 선택 | 근거 |
|------|------|------|
| 프레임워크 | FastAPI | 기존 프로젝트 기반, OpenAI-compatible 엔드포인트 |
| 데이터 | LangMem + AsyncPostgresStore | 구조화 메모리, 기존 PostgreSQL 인프라 활용 |
| 검색 | 커스텀 RPMemoryRetriever | LangMem store.search + 앱 레이어 RRF |
| 캐싱 | SAGA SystemStabilizer 패턴 | system 해시 기반 캐시 안정화 |
| 에피소드 추출 | LangMem ReflectionExecutor | 비동기 백그라운드 처리 |
| 큐레이션 | asyncio.create_task | 10턴마다 비동기, 실패 시 서비스 정상 |
| 추출/큐레이션 모델 | Flash급 (gpt-5-nano, Gemini Flash) | 비용 절감, 단순 추출 작업 |
| 임베딩 | text-embedding-3-small | 1536 dims |
| 로깅 | structlog | AGENTS.md 규칙 |
| 트레이싱 | Langfuse | 큐레이터 LLM 호출 별도 추적 |

---

## 9. 구현 순서

### 아키텍처 전환

기존 프로토타입(roleplay_base.py)은 `create_react_agent` 에이전트 패턴. 
리버스 프록시는 **httpx SSE 패스쓰루** 패턴으로 전환 필요.

```
에이전트 패턴 (기존):     우리 에이전트가 LLM을 직접 호출
리버스 프록시 패턴 (목표): RisuAI 프롬프트를 그대로 LLM에 전달, 메모리만 주입
```

- `create_react_agent` → 사용 안 함
- `create_manage_memory_tool` → `create_memory_store_manager` + `ReflectionExecutor`로 교체
- 추가 의존성: `httpx` (pyproject.toml에 추가 필요)

### Phase 1 파일 구조

```
llm-study/app/
├── main.py              ← FastAPI 앱, lifespan, CORS
├── config.py            ← UPSTREAM_BASE_URL, API_KEY, TOKEN_BUDGET
├── schemas.py           ← RPEpisode, OpenAI 요청/응답 Pydantic 모델
├── proxy.py             ← /v1/chat/completions (핵심, ~120줄)
├── memory.py            ← InMemoryStore 초기화, 검색, 리콜 렌더링 (~60줄)
└── extractor.py         ← ReflectionExecutor + create_memory_store_manager (~50줄)
```

### Phase 1: 기본 동작 (리버스 프록시 + 에피소드 메모리)

구현 순서: **순수 프록시 먼저 → 에피소드 메모리 추가**

- [ ] OpenAI-compatible 리버스 프록시 엔드포인트 (`/v1/chat/completions`)
- [ ] httpx SSE 스트리밍 패스쓰루 (upstream LLM → RisuAI)
- [ ] LangMem Store 초기화 (InMemoryStore → 이후 PostgreSQL)
- [ ] 에피소드 추출/저장 (create_memory_store_manager + ReflectionExecutor, 비동기)
- [ ] 에피소드 검색 (기본 벡터 유사도)
- [ ] ⑥번 메모리 슬롯에 에피소드 리콜 주입

### Phase 2: 압축 + 검색 고도화

- [ ] MessageCompressor (오래된 턴 → 불변 chunk 압축, 에피소드 요약 재활용)
- [ ] WindowRecovery (슬라이딩 윈도우 감지 → 잘린 턴 요약 주입)
- [ ] 4-stage RRF 검색
- [ ] 캐릭터 상태 프로필 메모리
- [ ] 관계 추적
- [ ] 플레이스홀더 마커 인식

### Phase 3: 큐레이터

- [ ] NPC 레지스트리 + 존재 모순 검사
- [ ] 복선 추적
- [ ] 큐레이션 결과 프롬프트 반영
- [ ] 성격 이탈 검사

### Phase 4: 고도화

- [ ] SystemStabilizer (system 프롬프트 canonical 고정 — 캐시 히트율 +10~15%p)
- [ ] 서사 압축 (50턴 이상)
- [ ] AsyncPostgresStore 전환
- [ ] 비용 추적 엔드포인트
- [ ] charx 파서 (로어북 자동 임포트)

---

## 10. 비용 구조 (100턴 기준)

| 작업 | 호출 수 | 모델 | 비고 |
|------|--------|------|------|
| 메인 RP 응답 | 100회 | 유저 선택 (Opus/GPT 등) | 동기, 유저 부담 |
| 에피소드 추출 | 100회 | Flash/Nano | 비동기 |
| 임베딩 생성 | 100회 | text-embedding-3-small | 비동기 |
| 큐레이션 | 10회 | Flash 1 + Mini 1 | 10턴마다 비동기 |
| 검색 (RRF) | 100회 | - | DB 쿼리만, LLM 0회 |

---

## 11. 참조 문서

구현 시 에이전트에게 아래 문서를 함께 참조시킬 것.

### 프로젝트 문서

| 문서 | 위치 | 용도 |
|------|------|------|
| **TECHNICAL_REFERENCE.md** | `llm-study/` | 상세 기술 사양 (프롬프트 삽입 패턴, 큐레이터 구현, 검색 전략, LangMem API 등) |
| **AGENTS.md** | `LLM-FastApi/` | 코드 스타일 규칙 (structlog, tenacity, Pydantic, FastAPI 규칙) |

### RisuAI 분석 문서 (LLM-FastApi/)

| 문서 | 핵심 내용 | 구현 시 참고 포인트 |
|------|----------|-------------------|
| **RisuAI_제작_가이드.md** | 올인원 통합 가이드: 프롬프트 조립 구조, 캐릭터 카드 필드, 로어북 상세, 변수 시스템, Regex 4종 파이프라인, 감정 이미지, 트리거/Lua, HTML(risu-trigger/risu-id), 배경 임베딩, SupaMemory/HypaMemory V3 상세, 모듈/플러그인, 제작 워크플로우, 이미지 생성(NovelAI/SD봇) | **1차 참조 문서** — RisuAI 전체 시스템 이해, 리버스 프록시 설계, 상태 태그 파싱, 메모리 대체 설계 |
| **캐릭터_설계_방법론.md** | Layered Persona 5계층(Core/Voice/State/Reaction/Direction), 실제 charx 분석(위지소연 14MB/142이미지, Fate HGW 80MB/772이미지), 실무 필드 사용 패턴(personality/scenario/mes_example은 빈 값), 안티패턴, 모델별 튜닝 | 큐레이터 성격 이탈 검사 기준, 관계 State Machine 설계, 에피소드 감정 톤 추출, charx 파서에서 실제 사용 필드 식별 |
| **시나리오_제작_총정리.md** | 장르별 설계 패턴 (미스터리, 생존, 연애, 학원), 메모리 선택 가이드 | 에피소드 스키마의 scene_type 분류, 검색 가중치 튜닝. **참고: RisuAI_제작_가이드.md에 관련 내용 일부 포함** |

> **참고:** 기존 RisuAI_플랫폼_종합분석.md, RisuAI_동적요소_분석.md, RisuAI_제작_워크플로우.md의 내용은 RisuAI_제작_가이드.md로 통합됨

### SAGA 프로젝트 (RISU_ENE/)

| 문서 | 핵심 내용 | 구현 시 참고 포인트 |
|------|----------|-------------------|
| **README.md** | 3-Agent 파이프라인, 캐싱 3모듈, 벤치마크, 스토리지 설계 | SystemStabilizer, MessageCompressor, Curator 패턴 |

### 실제 캐릭터 카드 샘플 (LLM-FastApi/)

| 파일 | 내용 | 구현 시 참고 포인트 |
|------|------|-------------------|
| **현대 던전 시뮬 (1).charx** | constant/keyword 엔트리, 관계 정의, 캐릭터 프로필 | charx 파서 개발, 로어북 구조 이해, namespace 설계 검증 |
