# RP Memory Engine — 기술 레퍼런스

> 이 문서는 구현 시 참조하는 상세 기술 사양서입니다.
> PLAN.md의 설계 결정에 대한 근거, 구체적 구현 패턴, RisuAI 연동 상세를 포함합니다.
> 관련 문서: RisuAI_제작_가이드.md (통합), 캐릭터_설계_방법론.md, 시나리오_제작_총정리.md, SAGA README

---

## 1. RisuAI 프롬프트 조립 구조

### 1-1. 기본 전송 순서 (스펙)

RisuAI가 LLM API에 보내는 messages 배열의 **스펙 기본 순서**:

```
[role: system] (1) Main Prompt          — AI 역할/규칙 ("You are {{char}}...")
[role: system] (2) Character Description — 외모, 배경, 설정
[role: system] (3) Character Personality — 성격 요약 (실무에서 빈 값 많음)
[role: system] (4) Scenario             — 현재 상황 (실무에서 빈 값, Lorebook으로 대체)
[role: system] (5) Persona              — 유저 페르소나
[role: system] (6) Lorebook             — 활성화된 엔트리만 (키워드 트리거)
[role: system] (7) Example Messages     — 문체 학습 (실무에서 빈 값 많음)
               ── 메모리 주입 (SupaMemory/HypaMemory) ──
[user/assistant] (8) Chat History       — 대화 기록
[role: system] (9) Author's Note        — depth로 히스토리 내 위치 조절
[role: system] (10) Global Note/JB      — 맨 끝, 최대 영향력
```

### 1-1b. 실제 프롬프트 템플릿 (RP 실전)

프롬프트 템플릿으로 **완전히 재배치**된 실제 사용 구조. 스펙과 순서가 다르다:

```
① [system] JB/Main — SYSTEM_RULE + ROLEPLAY_RULE           ← 시작 (참조율 높음)
② [system] Persona — 유저 캐릭터 프로필 {{slot}}
③ [system] "Supplementary Information" 헤더
④ [system] Lorebook (활성 엔트리)
⑤ [system] Author's Note (작가의 노트)
⑥ [system] 장기 기억 — [Roleplay Summary] {{slot}}          ← ★ 우리 엔진 주입 지점
⑦ [system] </ROLEPLAY_INFO> 닫기
⑧ [system] RESPONSE_INSTRUCTION (출력 규칙)
⑨⑩ [user/asst] Chat History
⑪ [system] 최종 삽입 프롬프트
⑫ [system] Prefill (Claude용: "I will generate...")         ← 끝 (참조율 높음)
```

**스펙과의 핵심 차이:**
- JB가 맨 뒤가 아니라 **맨 앞** (Main Prompt 역할 겸함)
- 메모리 주입은 Lorebook/Author's Note 뒤, Chat History 앞 (⑥번)
- RESPONSE_INSTRUCTION이 Chat 바로 앞에 위치
- Prefill이 진짜 맨 끝

**캐싱 관점:**
```
[캐시 가능 구간]              [매 턴 변경]
①②③ (고정)                  ⑤⑥ (메모리, Author's Note)
④ (준고정, 키워드 따라)       ⑨⑩ (Chat History)
```

프롬프트 템플릿은 유저마다 다를 수 있으므로, 우리 엔진은 **플레이스홀더 마커** 또는 **메모리 주입 위치 자동 감지**(SupaMemory/HypaMemory {{slot}} 위치)로 대응한다.

### 1-2. AI 참조율 (U자형)

```
참조율 높음 ◀━━━━━━━━━━━━━━━━━━━▶ 참조율 높음
     [시작]         [중간=약함]         [끝]
```

- Main Prompt(1)와 Jailbreak(10)이 가장 강력
- 중간(6~8)은 무시되기 쉬움
- `@@@end`로 로어북을 맨 끝에 삽입 가능 → 영향력 극대화
- 이 특성 때문에 동적 컨텍스트를 끝(마지막 user 메시지)에 배치

### 1-3. 로어북 상세

각 엔트리의 핵심 설정:
- **Activation Keys**: 쉼표 구분 트리거 키워드
- **Insertion Order**: 높을수록 뒤에 배치 = 더 중요 + 토큰 초과 시 안 잘림
- **Always Active (constant)**: 키워드 무관하게 항상 활성
- **Selective**: 2차 키워드 필요 (AND 조건)
- **Use Probability**: 확률적 활성화 (랜덤 이벤트용)

글로벌 설정:
- Recursive Scanning: 로어북 프롬프트 안의 키워드도 재스캔
- Full Word Matching: "cat"이 "caterpillar"에 반응 안 함
- Search Depth: 최근 N개 채팅만 키워드 스캔
- Max Tokens: 전체 로어북 토큰 상한 (초과 시 낮은 insertion_order부터 제거)

### 1-4. charx 파일 구조 (실제 "현대 던전 시뮬" 분석)

charx = ZIP 아카이브 (card.json + 에셋). card.json의 character_book.entries가 로어북.

**constant 엔트리 (항상 로드):**
| 이름 | 내용 |
|------|------|
| 헌터협회 | 헌터 라이선스 발급, 강남 본사 |
| 심연 주식회사 | 악마 기업, 던전 프랜차이즈 운영 |
| 글로벌 던전 솔루션 | 저가 몬스터/장비, 악덕기업 |
| 던전 관리청(DMA) | 인간 측, 이종족 협력 |
| 헌터/예비군/저랭크 | 일반 규칙 |
| 관계 | 한결→유저 싫어함, 이지은→정재현, 이오네→유저 좋아함 |

**keyword 엔트리 (키워드 트리거):**
| 이름 | 트리거 키워드 | 소속 |
|------|-------------|------|
| 루비아 | "루비아", "루비", "Rubia" | 심연 주식회사 지원담당 |
| 이오네 | "이오네", "Ione" | 심연 주식회사 A급 |
| 트릭시 | "트릭시", "Trixie" | 글로벌 던전 솔루션 영업 |
| 최은지 | "최은지", "은지" | DMA 협력관 |
| 김서연 | "김서연", "서연" | DMA 협력관 |
| 한결 | "한결" | DMA 과장 |
| 정재현 | "정재현", "재현" | E급 헌터 |
| ... 외 다수 | | |

---

## 2. RisuAI 동적 요소 — 엔진에 미치는 영향

### 2-1. 데이터 흐름 (한 턴)

```
[유저 입력]
    ▼
[Modify Input]    → 입력 전처리 (Regex)
    ▼
[Modify Request]  → 전체 채팅 데이터 조작 (Regex)
    ▼
[LLM 호출]        → ★ 우리 엔진이 여기서 개입 ★
    ▼
[Modify Output]   → AI 응답 후처리 (Regex) ⚠️ 저장 데이터 변경
    ▼
[데이터 저장]      → ★ 이 데이터가 우리에게 다음 턴에 들어옴 ★
    ▼
[Modify Display]  → 화면 렌더링 전용 (Regex) — 토큰 0, 데이터 불변
    ▼
[화면 렌더링]     → HTML/CSS + 감정 이미지 + 배경
```

### 2-2. 각 요소별 엔진과의 관계

| 요소 | 처리 주체 | 우리 엔진 영향 | 상세 |
|------|----------|--------------|------|
| Lorebook 키워드 | RisuAI | 건드리지 않음 | RisuAI가 매칭해서 프롬프트에 이미 포함시킴. 벡터 검색으로 보완 가능 |
| 변수 setvar/getvar | RisuAI | Read-Only Mirror | 클라이언트 렌더링용. AI 응답에서 `[상태]...[/상태]` 태그 비동기 파싱 |
| Regex Modify Output | RisuAI | 적용 후 데이터가 "진실" | 태그 제거 등이 이미 적용된 상태로 다음 턴에 들어옴 |
| Regex Modify Display | RisuAI | 무관 | 데이터 불변, 화면만 변경 |
| 감정 이미지 | RisuAI | 무관 | 클라이언트 텍스트 패턴 매칭, API 비용 0 |
| risu-trigger 버튼 | RisuAI | 무관 | 클라이언트 처리 |
| 트리거 스크립트 (Lua) | RisuAI | 무관 | 클라이언트 실행 |
| 배경 임베딩 | RisuAI | 무관 | 클라이언트 렌더링 |
| SupaMemory | **OFF** | 대체 | 우리 엔진이 장기 기억 담당 |
| HypaMemory V3 | **OFF** | 대체 | 우리 엔진이 장기 기억 담당 |

### 2-3. 토큰 영향

| 요소 | 토큰 소비 |
|------|-----------|
| Regex (Modify Display) | **0** |
| 감정 이미지 | **0** |
| 트리거 스크립트 | **0** |
| 배경 임베딩 | **0** |
| Regex (Modify Output) | 치환 후 값만큼 |
| 로어북 | 활성 엔트리만큼 (Max Tokens 제한) |

핵심: RisuAI의 Modify Display + HTML 조합이 토큰 0으로 UI 구현하는 패턴. AI는 짧은 태그만 출력, 클라이언트에서 변환.

---

## 3. SAGA 아키텍처 — 참고 패턴

### 3-1. 전체 흐름

```
유저 입력
  ├─ [동기] Sub-A: DB 읽기 + 컨텍��트 조립 (LLM 0회, ~35ms)
  ├─ [동기] LLM 1회 호출 → SSE 스트리밍 응답
  └─ 응답 후 비동기 ─────────
      ├─ Sub-B: Flash 서사 요약 → 에피소드 기록 → NPC 레지스트리 → live_state.md
      └─ Curator: N턴마다 모순 탐지 → 서사 압축 → 로어 자동생성
```

### 3-2. 3가지 캐싱 위협과 대응

| 위협 | 모듈 | 방법 |
|------|------|------|
| Lorebook 변경 → system 바뀜 | SystemStabilizer | 첫 턴 system을 canonical로 저장. 이후 delta만 분리하여 user prepend |
| 토큰 초과 → 앞쪽 잘림 | MessageCompressor | 오래된 턴을 **불변** chunk로 압축 (수정 안 함) → prefix 안정 |
| 그래도 잘림 | WindowRecovery | hash로 감지 → 잘린 턴 요약을 동적 영역에 주입 |

### 3-3. 캐싱 결과 (SAGA 벤치마크)

```
SAGA가 보내는 메시지:
  [system] 캐릭터 카드 (Stabilizer 고정)     ← BP1, 캐시됨
  [user+asst] chunk: Turn 1-8 요약           ← 불변, 캐시됨
  [user+asst] chunk: Turn 9-16 요약          ← 불변, BP2
  [user] Turn 17 ...
  [user] Turn 20 + [SAGA Dynamic]            ← 동�� 컨텍스트는 맨 끝
```

| 지표 | SAGA | HypaMemory V3 | RisuAI 자동 캐싱 |
|------|------|---------------|-----------------|
| 캐시 히트율 (50턴) | **85.7%** | — | 12.1% |
| 비용 효과 | **43.5% 절감** | — | -11.4% (손해) |

### 3-4. Sub-B Flash 요약

매 턴 비동기. Flash LLM으로 4필드 JSON 추출 → 4곳에 재활용:

```
{ summary, npcs_mentioned, scene_type, key_event }
  │
  ├─ turn_log (SQLite)     — 턴별 기록
  ├─ ChromaDB              — 에피소드 임베딩
  ├─ MessageCompressor     — chunk 요약 원본
  └─ WindowRecovery        — 잘린 턴 복원
```

Importance 스코어링: base 10 + scene_type(combat +40, event +35) + key_event +30 + NPC +10/명

### 3-5. Curator

N턴마다(기본 10턴, 우리 엔진도 10턴 채택) 비동기. Letta Memory Block으로 큐레이션 판단 이력 자기관리.

실제 탐지 예시 (요트 살인 미스터리, Turn 10):
```
[Curator] Contradiction: character_identity
  "Turn 5 '이름 모를 남성' 사망 → Turn 6-7 MacNamara로 확인
   → NPC 목록에 둘 다 HP:100 생존"

[Curator] Contradiction: character_duplication
  "Johnson(영문)과 존슨(한글)이 별도 NPC → 동일 인물, 통합 권장"
```

### 3-6. [SAGA 원본] 검색: 3-Stage RRF

SAGA는 3-Stage RRF를 사용한다. 우리 엔진은 Character stage를 추가한 **4-Stage**를 채택 (섹션 7 참조).

| Stage | 소스 | 가중치 |
|-------|------|--------|
| Recent | get_recent_episodes(n=10) | 1.2 |
| Important | search_important_episodes(>=40, n=10) | 1.0 |
| Similar | search_episodes(query, n=15) | 0.8 |

> **주의:** SAGA의 importance는 정수 0~100+ 범위 (base 10 + scene_type 보너스). 우리 엔진은 float 0.0~1.0으로 정규화 (섹션 7 참조).

asyncio.gather로 병렬 실행, 부분 실패해도 나머지로 동작.

---

## 4. LangMem API 상세

### 4-1. 전체 API

| 모듈 | 함수 | 용도 |
|------|------|------|
| Memory Management | `create_memory_manager` | 스테이트리스 추출 (저장소 없이) |
| | `create_memory_store_manager` | 스테이트풀 추출+저장+검색 |
| Memory Tools | `create_manage_memory_tool` | 에이전트가 직접 CRUD |
| | `create_search_memory_tool` | 에이전트가 직접 검색 |
| Prompt Optimization | `create_prompt_optimizer` | 프롬프트 자동 개선 |
| | `create_multi_prompt_optimizer` | 다중 ��롬프트 일관성 유지 |
| Utilities | `ReflectionExecutor` | 백그라운드 비동기 메모리 처리 |
| | `NamespaceTemplate` | namespace 동적 변수 치환 |

### 4-2. 메모리 3가지 타입

| 타입 | 저장하는 것 | 예시 |
|------|-----------|------|
| Semantic | 사실/지식 (Collections + Profiles) | "루비아는 서큐버스다" |
| Episodic | 경험의 전체 맥락 | "가족 비유로 설명하니 성공했다" |
| Procedural | 행동 규칙 (프롬프트 자체 개선) | 시스템 프롬프트 진화 |

### 4-3. 핵심 패턴

**복수 스키마 동시 사용:**
```python
manager = create_memory_manager(
    "anthropic:claude-3-5-sonnet-latest",
    schemas=[RPEpisode, Relationship, Triple],
    enable_inserts=True,
    enable_updates=True,
    enable_deletes=True,
)
```

**프로필 메모리 (단일 인스턴스):**
```python
# 캐릭터 상태 관리에 적합
manager = create_memory_store_manager(
    schemas=[CharacterState],
    enable_inserts=False,  # 중복 생성 방지, 업데이트만
)
```

**NamespaceTemplate (동적 치환):**
```python
template = NamespaceTemplate(("character", "{character_id}", "episodes"))
# config={"configurable": {"character_id": "rubia"}} → ("character", "rubia", "episodes")
```

**ReflectionExecutor (비동기 처리):**
```python
# Sub-B 역할을 이걸로 대체
# 응답 후 비동기로 에피소드 추출/저장
```

### 4-4. Store 옵션

| 저장소 | 용도 |
|--------|------|
| InMemoryStore | 개발/테스트 (재시작 시 소멸) |
| AsyncPostgresStore | 프로덕션 (langgraph-checkpoint-postgres 패키지) |

### 4-5. 검색

```python
store.search(
    namespace,
    query="검색어",
    limit=10,
    offset=0,
    filter={"key": "value"},  # 메타데이터 필터링
)
```

벡터 유사도 검색 기본. RRF는 앱 레이어에서 구현 필요.

### 4-6. CRUD 제어

| 파라미터 | 기본값 | 설명 |
|---------|--------|------|
| enable_inserts | True | 새 메모리 생성 |
| enable_updates | True | 기존 메모리 수정 |
| enable_deletes | False | 삭제 (기본 비활성) |

---

## 5. 프롬프트 삽입 — 구체적 구현 패턴

### 5-1. 하이브리드 전략 상세

리버스 프록시는 RisuAI가 보내는 messages 배열을 가로채서 변형한다.

```json
[
  {"role": "system", "content": "Main Prompt + Char + Scenario..."},
  {"role": "system", "content": "Lorebook entries..."},
  
  {"role": "system", "content": "[세계 상태 - Memory Engine]\n<world_state>\n현재 위치: 폐허 마법학원 지하2층\n경과: 3일차 저녁\n파티: 아리아 HP 85/100, 카이 HP 60/100\n</world_state>\n<relationships>\n아리아-카이: 동맹 (신뢰7, EP#42 전투에서 구출)\n아리아-유저: 동행자 (��뢰5, 경계 중)\n</relationships>"},
  
  {"role": "system", "content": "Example Messages..."},
  
  {"role": "user", "content": "이전 대화..."},
  {"role": "assistant", "content": "이전 응답..."},
  
  {"role": "user", "content": "[에피소드 기억 - Memory Engine]\n<episode_recall>\nEP#42 (2턴 전): 카이 함정, 아리아가 구출 (관련도: 0.94)\nEP#12 (18�� 전): 금지 마법서 조각 발견 (관련도: 0.87)\n</episode_recall>\n<narrative_cue>\n감정곡선: 긴장→안도 전환점\n미회수 복선: EP#12 마법서 (18턴 미언급)\n</narrative_cue>\n\n---\n카이가 갑자기 쓰러졌다."},
  
  {"role": "system", "content": "Author's Note / Jailbreak"}
]
```

### 5-2. 플레이스홀더 마커 처리

```python
# 리버스 프록시 메시지 변환 로직 (의사코드)
for i, msg in enumerate(messages):
    if "<!-- MEMORY_ENGINE:WORLD_STATE -->" in msg["content"]:
        msg["content"] = msg["content"].replace(
            "<!-- MEMORY_ENGINE:WORLD_STATE -->",
            engine.render_world_state(session_id)
        )
    if "<!-- MEMORY_ENGINE:EPISODE_RECALL -->" in msg["content"]:
        msg["content"] = msg["content"].replace(
            "<!-- MEMORY_ENGINE:EPISODE_RECALL -->",
            engine.render_episode_recall(session_id, query=last_user_msg)
        )
# 마커 없으면 기본 위치(하이브리드 전략)로 폴백
```

### 5-3. 캐싱 원리

Anthropic/OpenAI 프롬프트 캐싱: messages 배열의 **prefix가 동일하면 캐시 히트**.

```
[캐시 히트 구간]                         [캐시 미스 구간]
system (1~6) + world_state (느린 변경)  │  최근 히스토리 + episode_recall + user msg
                                         │  (매 턴 변경)
```

- 고정 블록(1~5): 항상 캐시 히트
- world_state: 5~10턴마다 변경 → 캐시 히트율 80%+
- episode_recall: 매 턴 변경 → suffix에 배치 → 캐시에 영향 없음

---

## 6. 큐레이터 — 구체적 구현 패턴

### 6-1. 모순 유형 전체 목록

**Tier 1 (필수 — 서사 파괴 방지):**

| 유형 | 탐지 방법 | RisuAI 연동 |
|------|----------|------------|
| 존재 모순 (죽은 NPC 재등장) | NPC 레지스트리 status 확인 | 로어북 constant NPC 목록과 대조 |
| 시공간 모순 (이동 없이 장소 변경) | 에피소드 location 추적 | 변수 `{{getvar::location}}`과 비교 |
| 이름 중복 (한/영 별도 등록) | alias match → exact → LLM dedup | 로어북 activation_keys와 동기화 |
| 캐릭터 성격 이탈 | 설정 벡터 vs 최근 행동 유사도 | 로어북 Always Active 설정과 대조 |

**Tier 2 (서사 품질 향상):**

| 유형 | 탐지 방법 |
|------|----------|
| 미회수 복선 | PlotThread status=open, N턴 이상 경과 시 경고 |
| 세계관 규칙 위반 | world_rules와 에피소드 교차 검증 |
| 변수-서사 불일치 | HP=0인데 전투 중, 호감도 1인데 연인 행동 등 |

**Tier 3 (고급):**

| 유형 | 탐지 방법 |
|------|----------|
| 서사 압축 | 에피소드 50개 초과 시 오래된 것 요약 → stable_prefix |
| 로어 자동생성 | NPC 레지스트리에 이름만 있고 설명 없는 엔티티 |

### 6-2. 실행 구조 (의사코드)

```python
async def run_curator(chat_id: str, recent_turns: list, store: BaseStore):
    """10턴마다 비동기 실행"""
    
    # 1. 현재 상태 로드
    npc_registry = store.search(("chat", chat_id, "npc_registry"), query="", limit=100)
    plot_threads = store.search(("chat", chat_id, "plot_threads"), query="", limit=50)
    prev_curation = store.search(("chat", chat_id, "curation_history"), query="", limit=1)
    
    # 2. 엔티티 추출 (Flash LLM 1회)
    extracted = await flash_llm(build_extraction_prompt(recent_turns, npc_registry))
    
    # 3. NPC 레지스트리 갱신
    for entity in extracted.entities:
        match = alias_match(entity.name, npc_registry)
        if not match:
            match = await llm_dedup(entity.name, npc_registry)  # 필요시만
        # 갱신 또는 신규 등록
    
    # 4. 모순 검사 (Mini LLM 1회)
    findings = await mini_llm(build_contradiction_prompt(
        recent_turns, npc_registry, plot_threads, world_rules
    ))
    
    # 5. 결과 저장
    report = CurationReport(run_at_turn=current_turn, findings=findings, ...)
    store.put(("chat", chat_id, "curation_history"), str(current_turn), report.model_dump())
```

### 6-3. 프롬프트 반영 위치

```
critical 모순:
  → @@@end 위치 (Jailbreak 직전)
  → 예: "CRITICAL: 김서연은 3턴 전 사망함. 재등장 불가."
  
warning/info:
  → Author's Note depth 4
  → 예: "활성 복선: '지하 통로의 열쇠' (12턴 전, 미회수)"
  
NPC 현황:
  → world_state 블록에 포함
  → 예: "김서연: 사망 / 이도현: 도시 / 아리아: 숲"
```

### 6-4. NPC Dedup 패턴 (SAGA 기반)

```
새 이름 등장: "루비"
  │
  ├─ alias_match: "루비" in aliases of "루비아"? → Yes → 같은 NPC
  │
  ├─ (alias 없으면) exact_match: "루비" == 기존 이름? → No
  │
  └─ (매칭 안 되면) LLM dedup: "루비와 루비아는 같은 캐릭터인가?" → Yes/No
     → Yes: alias 추가, 통합
     → No: 새 NPC 등록
```

---

## 7. 검색 전략 — 구현 상세

### 7-1. 4-Stage RRF

```python
async def search_episodes(session_id, query, store, turn_count):
    # 4개 소스 병렬 실행
    recent, important, similar, character = await asyncio.gather(
        get_recent(store, session_id, n=10),
        get_important(store, session_id, threshold=0.7, n=10),
        get_similar(store, session_id, query=query, n=15),
        get_by_character(store, session_id, active_chars, n=10),
    )
    
    # RRF 점수 계산
    k = 60
    weights = {"recent": 1.0, "important": 1.2, "similar": 0.8, "character": 0.6}
    
    for ep in all_candidates:
        score = 0
        for source_name, source_list in sources.items():
            if ep in source_list:
                rank = source_list.index(ep)
                score += weights[source_name] / (k + rank)
        ep.rrf_score = score
    
    # 토큰 예산 내 packing
    return pack_within_budget(sorted_by_score, max_tokens=1500)
```

### 7-2. HypaMemory Random 제거 근거

- Random(0.2)은 "잊힌 디테일 재등장"이라는 매력이 있음
- 하지만 리버스 프록시에서 토큰 예산 제한적
- RP에서 일관성(coherence) > 다양성(diversity)
- Character 전략이 "잊힌 NPC 관련 기억"을 자연스럽게 커버

### 7-3. LangMem store.search 한계

store.search는 단일 벡터 유사도만 제공. 4-stage RRF를 위해:
- Similar → store.search 사용
- Recent → turn_range 기준 정렬 (별도 쿼리 또는 메타데이터 필터)
- Important → importance 필터 (메타데이터)
- Character → participants 필터 (메타데이터)

→ 커스텀 `RPMemoryRetriever` 클래스가 필요

---

## 8. 캐릭터 상태 동기화 — Read-Only Mirror 패턴

```
RisuAI (권위적 소스)
  {{setvar::hp::80}}  ────────────────────┐
  {{setvar::location::던전3층}}  ──────────┤
                                          ▼
리버스 프록시 (비동기 파싱)
  AI 응답에서 [상태] 태그 파싱  ──────▶  프로필 메모리 갱신
  예: "[상태]HP: 80/100, 위치: 던전3층[/상��]"
```

- RisuAI setvar가 source of truth
- 우리는 읽기 전용 미러 (비동기, 최선의 노력)
- 완벽한 동기화 불가능하지만 "대략적 상태"만으로도 검색 품질 충분
- 동기화 실패 시에도 메인 응답 영향 없음

---

## 9. 관계 추적 — 정적 vs 동적

### charx 초기값 → 시드

```
카드 로드 시:
  charx "한결→유저 싫어함"
  → Relationship(source="한결", target="유저", trust_level=-0.6, relation_type="적대")

RP 진행 중 (에피소드 추출과 동시에):
  "한결이 유저를 도와줌"
  → trust_level: -0.6 → -0.3, key_events에 추가
```

관계 업데이트는 에피소드 추출 시 "관계 변화도 함께 추출하라"는 프롬프트로 통합 → 별도 LLM 호출 불필요

---

## 10. HypaMemory V3 vs 우리 엔진 비교

| 항목 | HypaMemory V3 | 우리 엔진 |
|------|--------------|----------|
| 요약 | 보조 AI 6개씩 묶어 요약 | Flash LLM 구조화 추출 (RPEpisode 스키마) |
| 검색 | Recent(0.4) + Similar(0.4) + Random(0.2) | Recent(1.0) + Important(1.2) + Similar(0.8) + Character(0.6) |
| 랭킹 | childToParentRRF, k=60 | RRF, k=60 |
| 캐싱 | 안 됨 (system에 매번 다른 메모리 주입) | 하이브리드 (느린/빠른 분리) |
| 모순 탐지 | 없음 | 큐레이터 (10턴마다) |
| NPC 추적 | 없음 | 레지스트리 + LLM dedup |
| 복선 관리 | 없음 | PlotThread 추적 |
| 구조화 | 텍스트 청크 | Pydantic 스키마 (에피소드, 관계, 상태) |

---

## 11. 메모리 시스템 비교 (Mem0 vs LangMem)

이 프로젝트에서 LangMem을 선택한 이유:

| 항목 | Mem0 | LangMem |
|------|------|---------|
| 에피소드 추출 | 자동 key-value 팩트 | 커스텀 스키마로 구조화 |
| 관계 추적 | relations 기능 내장 (v2) | 직접 스키마 설계 |
| 프로필 | 자동 추출 | enable_inserts=False 패턴 |
| 비동기 | API 호출 | ReflectionExecutor |
| 저장소 | 자체 관리 | LangGraph Store (InMemory, Postgres) |
| 생태계 | 독립적 | LangGraph와 깊은 통합 |
| 선택 근거 | | 기존 LangGraph 기반 + 커스텀 스키마 자유도 |

---

## 12. 비용 모델

### 100턴 기준

| 작업 | 호출 수 | 모델 | 빈도 |
|------|--------|------|------|
| 메인 RP 응답 | 100 | 유저 선택 (Opus/GPT) | 매 턴 동기 |
| 에피소드 추출 | 100 | Flash/Nano | 매 턴 비동기 |
| 임베딩 생성 | 100 | text-embedding-3-small | 매 턴 비동기 |
| 큐레이션 추출 | 10 | Flash/Nano | 10턴마다 비동기 |
| 큐레이션 검사 | 10 | Mini | 10턴마다 비동기 |
| 검색 (RRF) | 100 | - | 매 턴 동기 (DB만) |

### SAGA 참고 수치

```
프롬프트 토큰: 697 (Turn 1) → 32,292 (Turn 50)  ← 46배 증가
레이턴시:    4.0초 (Turn 1) →  5.5초 (Turn 50)  ← 1.4배만 증가 (캐싱 효과)
캐시 히트율: 85.7%, 비용 43.5% 절감
```

---

## 13. Layered Persona — 캐릭터 설계 프레임워크

> 출처: 캐릭터_설계_방법론.md

캐릭터 설정을 5계층으로 나눠서, 각 계층이 AI의 다른 행동 영역을 담당하게 하는 프레임워크. 
**엔진이 큐레이터/에피소드 추출 시 이 구조를 이해해야 한다.**

### 13-1. 5계층 요약

```
Layer 1: Core       → "왜 이렇게 행동하는가" (Anchor, Wound, Want/Need, Mask/Leak)
Layer 2: Voice      → "어떻게 말하는가" (Ghost Dialogue, Silence Rules, Barks, Truth Budget)
Layer 3: State      → "관계가 어디인가" (상태 머신: 경계→관찰→허용→신뢰, 역행 가능)
Layer 4: Reaction   → "이 상황에서 어떻게 반응하는가" (State별 분기 반응)
Layer 5: Direction  → "AI야 이렇게 써라" (알고리즘형 지시, 금지 규칙)
```

### 13-2. 배치 위치

| 계층 | 배치 필드 | 비고 |
|------|----------|------|
| Core + Voice | **Description** | 상단 500토큰 + 하단 300토큰 |
| State + Reaction | **Lorebook** | 키워드/Always Active 엔트리 |
| Direction | **Author's Note** (post_history_instructions) | 알고리즘형 순서 지시 |

### 13-3. 엔진에 미치는 영향

**큐레이터 성격 이탈 검사:**
- Core의 Anchor/Wound가 캐릭터의 "기준점"
- State Machine의 현재 단계와 행동이 불일치하면 이탈
- Voice의 Silence Rules 위반도 이탈 (예: Trust Budget 30%인데 "사랑해"라고 직접 말함)

**에피소드 추출 시:**
- emotional_tone은 Layer 2 Voice의 감정 패턴과 연관
- consequence는 Layer 3 State 전이 조건과 연관 (관계 변화 감지)
- 에피소드에서 Mask vs Leak 패턴 감지 → 캐릭터 일관성 추적

**관계 추적 (State Machine):**
```
State 0: 경계 → 전이: 3회 약속 이행 + 개인 공간 존중
State 1: 관찰 → 전이: Wound 주제에서 도망 안 함
State 2: 허용 → 전이: Wound를 자발적으로 꺼냄
State 3: 신뢰 → 역행: 배신 시 State 0으로 직행 + 벽 강화
```
- 역행(Regression) 필수 — 실수하면 후퇴
- 역행 후 벽이 더 높아짐 ("한 번 열었다가 닫은 문은 더 단단히 잠긴다")
- 이 패턴을 Relationship 스키마의 trust_level 변화로 추적

---

## 14. 실제 charx 분석 결과 — 필드 사용 패턴

> 출처: 캐릭터_설계_방법론.md (위지소연.charx, Fate HGW V1.charx 분석)

### 14-1. 실무에서 사용하는 필드

| 필드 | 위지소연 (싱글 캐릭터) | Fate HGW (게임 시스템) | 결론 |
|------|----------------------|----------------------|------|
| description | 5,784자 (전부 여기) | 5,919자 (게임 룰북) | **핵심 필드** |
| personality | "" (빈 문자열) | "" (빈 문자열) | 실무에서 안 씀 |
| scenario | "" (빈 문자열) | "" (빈 문자열) | 실무에서 안 씀 |
| first_mes | "" | 10,949자 (분기 소환씬) | 카드마다 다름 |
| mes_example | "" (빈 문자열) | "" (빈 문자열) | 실무에서 안 씀 |
| post_history_instructions | "" | **30,994자** (GM 시스템) | 복잡한 카드의 핵심 |
| character_book | 8 엔트리 (전부 Always Active) | **767 엔트리** (14 AA + 753 키워드) | **핵심 필드** |

### 14-2. 엔진 설계에 미치는 영향

**charx 파서 설계 시:**
- personality, scenario, mes_example은 빈 값일 수 있음 → 무시해도 됨
- 실제 캐릭터 정보는 description + character_book에 집중
- post_history_instructions가 Direction(AI 지시) 역할 → 이 필드도 파싱 필요

**스케일 인식:**
- 소규모 카드: 로어북 8개, 이미지 142장 (위지소연)
- 대규모 카드: 로어북 767개, 이미지 772장, backgroundHTML 51,549자 (Fate HGW)
- 가장 큰 단일 엔트리: 110,213자 ("HGW Servant Summary")
- 엔진은 이 스케일 범위를 모두 처리할 수 있어야 함

**이미지 시스템 (위지소연 사례):**
- 로어북(order 999, Always Active)에서 이미지 커맨드 규칙을 AI에게 지시
- 포맷: `[🖼|{outfit}_{suffix}|{name}]`
- 의상 태그 × 감정 태그 = 100장 조합
- 엔진의 에피소드 추출 시 이미지 태그는 무시해야 함 (서사 아님)

**변수 시스템 (Fate HGW 사례):**
- defaultVariables에 HP, 스탯, 령주 수, 동조율, 적 서번트 6팀 정보 전부 변수로 관리
- `{{getvar::cv_servant_class}}` 등 조건 분기로 모드 분리
- 엔진의 상태 파싱이 이 변수 구조를 인식할 수 있으면 이상적

### 14-3. 프롬프트 조립에서 실제 사용되는 필드 (업데이트)

```
실제 전송 순서 (실무 기준):
① Main Prompt          [system]  ← 참조율 높음
② Description          [system]  ← 핵심. Personality/Scenario 내용도 포함
③ Personality          [system]  ← 보통 빈 값 (Description에 통합됨)
④ Scenario             [system]  ← 보통 빈 값 (Lorebook Always Active로 대체)
⑤ Persona              [system]
⑥ Lorebook (활성)      [system]  ← 핵심. 세계관+NPC+게임규칙 전부 여기
⑦ Example Messages     [system]  ← 보통 빈 값 (선택적)
── 메모리 주입 (SupaMemory/HypaMemory → 우리 엔진이 대체) ──
⑧ Chat History         [user/assistant]
⑨ Author's Note        [system]  ← post_history_instructions (복잡한 카드의 핵심)
⑩ Global Note/JB       [system]  ← 참조율 높음
```

---

## 15. RisuAI 제작 가이드 통합 참조

> 기존 RisuAI_플랫폼_종합분석.md, RisuAI_동적요소_분석.md, RisuAI_제작_워크플로우.md는
> **RisuAI_제작_가이드.md**로 통합됨.

주요 통합 내용:
- 프롬프트 조립 구조 (섹션 2)
- 캐릭터 카드 필드 상세 + 실무 사용 패턴 (섹션 3)
- 로어북 상세 설정 (섹션 4)
- 변수 시스템 (섹션 5)
- @ Syntax (섹션 6)
- Regex 4종 파이프라인 (섹션 7)
- 동적 요소: 감정 이미지, 트리거, Lua, HTML, 배경 (섹션 8)
- 메모리 시스템: SupaMemory, HypaMemory V3 상세 (섹션 9)
- 모듈 & 플러그인 (섹션 10)
- 제작 워크플로우: 기획→이미지→카드→모듈→테스트 (섹션 11)
