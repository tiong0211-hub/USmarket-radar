# Daily US Market & Macro Radar

밤사이 미 증시·매크로·테마 뉴스를 요약해 국장 아침 판단을 돕는 모바일 일간 대시보드.
Claude Code 작업 규칙은 `CLAUDE.md`, 설계는 `docs/`.

## Directory Structure
```
us-market-radar/
├── CLAUDE.md                     # Claude Code 프로젝트 규칙 (먼저 읽기)
├── README.md
├── pyproject.toml                # P1에서 생성 (uv)
├── .github/workflows/
│   ├── daily.yml                 # 07:10 KST cron → 실행 → data 커밋 → Pages 배포
│   └── ci.yml                    # PR/push 시 pytest (P1에서 생성)
├── config/                       # 사람이 관리하는 설정 (코드 하드코딩 금지)
│   ├── symbols.yaml              # 지표 6종(7값) + 섹터 ETF
│   ├── feeds.yaml                # 뉴스 RSS + 경제 캘린더 소스
│   ├── theme_map.yaml            # 미 종목 → 국내 수혜주 (사람만 수정)
│   ├── models.yaml               # OpenAI 모델 설정
│   └── settings.yaml             # 시간, 예산, 심리지수 규칙, 면책 문구
├── prompts/                      # LLM System Prompt 원문
│   ├── news_analyst.md           # M6
│   ├── theme_proposer.md         # M7 (제안 전용)
│   └── briefing_writer.md        # M8
├── schemas/
│   ├── daily.schema.json         # docs/data_contract.md 의 JSON Schema
│   └── llm/                      # Structured Output 스키마
│       ├── news_analyst.json
│       ├── theme_proposer.json
│       └── briefing_writer.json
├── src/radar/
│   ├── run_daily.py              # Orchestrator (CLI)
│   ├── market_calendar.py        # XKRX/XNYS 휴장일·기준 세션
│   ├── state.py                  # 모듈/런 상태 머신, run log
│   ├── models.py                 # pydantic 데이터 모델 (data contract)
│   ├── collectors/
│   │   ├── market.py             # M1
│   │   ├── news.py               # M2
│   │   ├── calendar.py           # M3
│   │   └── movers.py             # M4
│   ├── analytics/
│   │   └── sentiment.py          # M5
│   ├── llm/
│   │   ├── client.py             # OpenAI 래퍼: 재시도, 예산 가드, structured output
│   │   ├── news_analyst.py       # M6
│   │   ├── theme_mapper.py       # M7 (+ KRX 코드 검증, Issue 생성)
│   │   └── briefing_writer.py    # M8
│   ├── assemble/
│   │   ├── validator.py          # M9: 6종 체크, 스키마, 숫자 근거 검증
│   │   └── assembler.py          # M9: daily JSON 병합
│   └── render/
│       ├── renderer.py           # M10
│       ├── templates/
│       │   ├── reference/
│       │   │   └── morning-radar.html  # Codex UI 시안 (기준 디자인, 수정 금지)
│       │   ├── index.html.j2     # 오늘 페이지 (시안을 Jinja로 이식)
│       │   └── archive.html.j2   # 날짜별 목록
│       └── static/style.css      # 빌드 시 인라인
├── data/                         # 파이프라인 산출물 (커밋됨, 공개)
│   ├── raw/YYYY-MM-DD/           # market/news/calendar/movers 원본 (본문 저장 금지)
│   ├── daily/YYYY-MM-DD.json     # 렌더 입력 (data contract)
│   ├── proposals/YYYY-MM-DD.json # M7 테마 제안
│   └── logs/YYYY-MM-DD.run.json  # 실행 상태 로그
├── site/                         # 빌드 결과 (gitignore, Pages artifact로 배포)
├── tests/
│   ├── fixtures/                 # 녹화된 외부 응답
│   └── test_*.py
└── docs/
    ├── architecture.md           # 확정 아키텍처 & 결정 사항
    ├── orchestration_spec.md     # 상태 머신, 재시도, 예산, 수동 실행
    └── data_contract.md          # daily JSON 계약
```

## 시작하기 (Claude Code)
1. GitHub에 공개 repo `us-market-radar` 생성 → 이 파일들 push
2. Settings → Secrets → `OPENAI_API_KEY` 등록, Settings → Pages → Source: GitHub Actions
3. Claude Code에서: "CLAUDE.md의 P1을 진행해줘"
