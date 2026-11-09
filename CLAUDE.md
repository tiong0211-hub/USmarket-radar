# CLAUDE.md — Daily US Market & Macro Radar

## 1. 프로젝트 개요
매 국장 영업일 06:40·07:10·07:40 KST(3회 예약, 먼저 게시되면 나머지는 `--skip-if-done`으로 건너뜀)에 GitHub Actions가 파이프라인을 실행한다. GitHub 예약은 수 시간 늦을 수 있다(11/06: 07:10 슬롯이 11:21 시작). 운영 통계: `python -m radar.stats`.
밤사이 미국 시장 데이터·뉴스·경제 캘린더를 무료 소스로 수집하고, OpenAI API로 뉴스를 분류·요약한다.
미국 주도 종목을 국내 수혜주에 매핑한 뒤 **모바일 우선 정적 웹페이지 1장**으로 렌더링해 GitHub Pages(공개 repo)에 게시한다.

- 성공 기준: (1) 정해진 루틴대로 자동 생성 (2) **핵심 지표 6종(7개 값) 누락 없이 표시**
- 사용자: 본인 + 소수 지인(링크 공유). 투자 권유 아님 문구를 항상 노출한다.
- 범위 밖(이번 버전): 키움 API, 야간선물·외국인 수급(2026-11-09 결정: 예측용 대시보드 아님).

상세 설계: `docs/architecture.md`, 실행 명세: `docs/orchestration_spec.md`, 데이터 계약: `docs/data_contract.md`.

## 2. 기술 스택 & 컨벤션
- Python 3.12, 패키지 관리 `uv` (`pyproject.toml`). 주요 의존성: `yfinance`, `feedparser`, `httpx`, `pydantic>=2`, `jinja2`, `pyyaml`, `exchange_calendars`, `openai`, `FinanceDataReader`(KRX 종목 목록 검증), `pytest`.
- 모든 모듈 간 데이터는 **pydantic 모델 → JSON 파일**로만 주고받는다. 모듈끼리 함수 직접 호출로 데이터를 넘기지 않는다(Orchestrator만 호출).
- 시간: 내부 저장은 모두 **UTC ISO-8601**, 화면 표시는 KST(뉴스 카드는 ET 병기).
- 설정값(티커, RSS, 모델명, 임계값)은 전부 `config/*.yaml`. 코드에 하드코딩 금지.
- 비밀값은 GitHub Secrets → 환경변수(`OPENAI_API_KEY`)로만. 로그·커밋·HTML에 절대 출력 금지.
- 공개 repo이므로 `data/raw`에는 기사 **제목·링크·RSS 요약(최대 300자)**만 저장. 기사 본문 전문 저장 금지.
- 로깅: 표준 `logging`, 모듈별 logger. 실행 결과는 `data/logs/YYYY-MM-DD.run.json`(상태 머신 기록).
- 커밋 메시지: `feat(m1): ...`, `fix(m6): ...` 형식(모듈 ID 포함).

## 3. 모듈 맵 & 역할 경계 (침범 금지)
| ID | 모듈 | 위치 | 하는 일 | 하지 않는 일 |
|---|---|---|---|---|
| OR | Orchestrator | `src/radar/run_daily.py` | 휴장일 판단, 순서/병렬, 재시도·폴백, run log | 데이터 해석, HTML 생성 |
| M1 | Market | `collectors/market.py` | 지표 6종(7값) + 참고 SOX 수집, 5거래일 종가(spark) | 해석, 누락값 보간 |
| M2 | News | `collectors/news.py` | RSS 수집·시간창 필터·중복 제거 | 요약, LLM 호출 |
| M3 | Calendar | `collectors/calendar.py` | 경제 캘린더(Forex Factory JSON) 수집 — 어젯밤 발표(`released`) + 오늘 밤 예정(`upcoming`) | 실제치 추정 |
| M4 | Movers | `collectors/movers.py` | 감시 종목·섹터 ETF 등락률 | 테마 해석 |
| M5 | Sentiment | `analytics/sentiment.py` | VIX → 0~100 점수(규칙) | 뉴스 반영, LLM |
| M12 | Outlook | `analytics/outlook.py` | SOX·EWY·VIX → 코스피 시가 갭 방향(규칙, 매달 자동 재보정 → `data/outlook_model.json`). 로직: `docs/outlook_scoring.md` | 장중 흐름 예측, 갭 % 표시, LLM |
| M6 | News Analyst | `llm/news_analyst.py` | 분류·중요도·3줄 요약·국장 영향·티커 추출·캘린더 실제치 보완 | 시세 숫자 생성, 국내 수혜주 지정 |
| M7 | Theme Mapper | `llm/theme_mapper.py` | YAML(사람) + 자동 매핑(`data/theme_auto.json`, LLM, 검토 없이 표시·`auto_review_days` 후 재판단), 결정 기록 → GitHub Issue | **YAML 직접 수정**, KRX 목록에 없는 종목·확신도 low 후보 표시 |
| M8 | Briefing Writer | `llm/briefing_writer.py` | 입력 JSON 근거 종합 코멘트 | 입력에 없는 숫자, **갭 % 수치 예측**, 매수/매도 권유 |
| M9 | Validator/Assembler | `assemble/` | 6종 체크·스키마 검증·숫자 근거 검증·병합 | 값 수정/보간, 실패 은폐 |
| M10 | Renderer | `render/` | JSON → 모바일 HTML, 아카이브 | 데이터 가공, 외부 호출 |
| M11 | Deploy | `.github/workflows/daily.yml` | Pages 게시, 실패 알림 | 콘텐츠 변경 |

## 4. LLM 사용 규칙
- 모델명은 `config/models.yaml`에서만 읽는다(현재: 분석 `gpt-4o-mini`, 브리핑 `gpt-5.5`).
- 모든 호출은 **Structured Outputs(JSON Schema, strict)** 사용. 스키마는 `schemas/llm/*.json`.
- System Prompt 원문은 `prompts/*.md`. 코드에서 프롬프트 문자열을 새로 작성하지 말 것.
- 일일 토큰 예산 가드: `config/settings.yaml`의 `llm_budget` 초과 시 이후 호출 중단 → 폴백.
- M8 출력의 모든 숫자는 M9가 입력 digest와 대조한다. 불일치 시 M8 출력을 폐기하고 템플릿 문구로 대체.

## 5. 화면(UI) 규칙
### 5.1 기준 디자인
- **`src/radar/render/templates/reference/morning-radar.html`(Codex 시안)이 기준 디자인이다.** 레이아웃·색상 토큰(`:root` 변수)·컴포넌트(hero, 게이지, market-card, news-card, theme-card, timeline, dialog, mobile-nav)·반응형 breakpoint(1000/760/365px)를 그대로 Jinja 템플릿으로 옮긴다.
- 시안의 예시 데이터 배열, PREVIEW 배너, "예시/SAMPLE" 문구는 모두 제거하고 실데이터로 대체한다.
- 밝은 테마. 색상은 **한국 관례: 상승 빨강(`--up:#c85543`) / 하락 파랑(`--down:#3073bc`)** (확정).
- 폰트: Noto Sans KR + Manrope (Google Fonts, `display=swap`), 실패 시 시스템 폰트.

### 5.2 렌더링 방식
- **서버 사이드 렌더링(Jinja, autoescape=True)**으로 완성된 HTML을 생성한다. 시안처럼 JS 배열로 `innerHTML` 조립 금지(카톡 인앱 브라우저 속도·XSS 방지).
- JS는 뉴스 필터, dialog 열기/닫기, 스크롤 nav 활성화에만 사용. JS가 꺼져도 모든 내용이 보여야 한다(`<details>` 활용).
- CSS는 빌드 시 인라인. 외부 요청은 폰트만 허용.

### 5.3 섹션 구성 (시안 순서)
| 섹션 | 내용 | 데이터 |
|---|---|---|
| Header | **날짜·요일(KST) 크게** + 실제 생성 시각 + "어제 / 아카이브" 링크 | `run_date`, `generated_at` |
| 상태 배너 | 평소 숨김. 미장 휴장 / DEGRADED 누락 항목 / 전일 페이지 유지 경고일 때만 표시 | `banner`, `warnings` |
| Hero (OVERNIGHT BRIEF) | 헤드라인 2줄(강조 단어 1개) + 포인트 3개 + "먼저 볼 것" 칩 2개 | `briefing` |
| 시장 심리 | VIX 기반 자체 점수 게이지 + VIX 값 + "VIX 기반 자체 산출" 주석 / "오늘 국장은?" M12 갭 방향 + 근거 지표 + 적중률·보정일 + 장중 예측 불가 문구 + `참고용` 라벨 | `sentiment`, `outlook`, `briefing.direction` |
| 시장 지표 (8카드) | 필수 7값 + 참고 SOX. 카드 탭 → dialog(설명·출처·기준 시각) | `indicators` |
| 흐름을 만든 뉴스 | 필터(전체/매크로/테마·실적/원자재·리스크, 건수 동적), 카드 펼침 → 3줄 요약 + 국내장 관찰 포인트 + 출처·원문 링크·KST 시각 | `news` |
| 미국 주도주, 국내 연결 | **S&P 500보다 1%p 이상 오른 상승 종목만**(하락 종목 제외, 없으면 "없음" 문구), 국내 종목 칩에 관계 라벨(`relation_ko`) | `themes`, `themes_rule_ko` |
| 다음 체크포인트 | **오늘 밤(KST) 발표 예정** 미국 **고영향(high)** 지표 타임라인 | `calendar.upcoming` |
| 어젯밤 지표 결과 | 실제치·예상치·이전치 비교 (뉴스 섹션 상단 또는 사이드) | `calendar.released` |
| 데이터 상태 (헤더 ⓘ dialog만, 하단 카드 없음. JS 꺼지면 카드로 표시) | 출처, 기준 시각, run_status, 누락·경고 목록 | `run_status`, `sources`, `warnings` |
| Footer | `settings.yaml`의 면책 문구 | 고정 |

### 5.4 표시 규칙
- 1차 버전에서 **갭 % 범위 수치 표시 금지**. 방향성(갭상승/보합/갭하락 우세) + "참고용" 라벨만.
- 누락 데이터는 숨기지 말고 "데이터 없음" 회색 표시.
- 금리 변동은 **bp**로, 지수·환율은 %와 절대 변동을 함께. 음수 기호는 `−`(U+2212) 통일.
- 선물 카드에는 근월물 계약월과 기준 시각(KST) 표시.
- 스파크라인은 최근 5거래일 종가(`spark`)로 그린다. 데이터 없으면 숨김.
- 뉴스 중요도 → 라벨: 5·4 "관찰 중요도 높음", 3 "업종 확산 관찰", 2 이하는 화면 제외.
- **가독성 최소 글자 크기: 본문 12px, 라벨·보조 10px** (시안의 7~9px 라벨은 상향).
- `<head>`: `<meta name="robots" content="noindex,nofollow">`, Open Graph(`og:title` = 날짜 + 헤드라인, `og:description` = 포인트 1, `og:image` = 고정 썸네일) — 카톡 링크 미리보기용.
- 하단 mobile-nav: 브리핑 / 지표 / 뉴스 / 테마 / 일정.

## 6. 테스트 규칙
- 네트워크 없는 단위 테스트가 기본. 외부 응답은 `tests/fixtures/`에 녹화본 사용.
- LLM 모듈은 fixture 응답으로 스키마/폴백 경로를 테스트. 실제 API 호출 테스트는 `-m live` 마커로 분리.
- `python -m radar.run_daily --date 2026-11-05 --dry-run --no-llm --no-deploy` 가 로컬에서 항상 동작해야 한다.

## 7. 구현 순서 (Phase)
1. **P1 뼈대**: M0 셋업 → M1 → M9(6종 체크) → M10 → M11. *지표만 있는 페이지가 매일 게시되면 완료.*
2. **P2 뉴스**: M2 → M3 → M6.
3. **P3 테마/심리**: M4 → M7(+Issue 제안) → M5.
4. **P4 브리핑**: M8 + 숫자 근거 검증.
5. **P5 안정화**: 2주간 실운영, 수집 성공률 측정(`data/logs` 집계).
6. 이후 별도 협의: M12 국장 전망 알고리즘 → M13 카카오톡 푸시.

각 Phase 끝에 사용자 확인을 받고 다음 Phase로 넘어간다.

## 8. 금지 사항
- 지어낸 데이터·플레이스홀더 숫자를 실제 화면에 게시하지 말 것(시연용 시뮬레이션 기능 넣지 않음).
- `config/theme_map.yaml`을 파이프라인이 자동 수정하지 말 것(사람 전용, 같은 티커면 자동 매핑보다 항상 우선. `verified: false`로 넣으면 그 티커를 숨김).
  자동 매핑은 2026-11-07 사용자 결정으로 검토 없이 표시하되 화면에 "자동 매핑 · 판단일" 라벨을 붙인다.
- 인베스팅닷컴 등 자동수집을 약관으로 금지한 사이트 스크래핑을 기본 경로로 넣지 말 것.
- 실패를 숨기기 위해 전일 데이터를 오늘 데이터처럼 표시하지 말 것(전일 페이지 유지 시 경고 배너 필수).
