# Data Contract — `data/daily/YYYY-MM-DD.json` (schema_version 1.1)

Renderer(M10)는 이 파일만 읽는다. 필드 추가는 허용, 기존 필드 의미 변경은 금지(버전 증가 필요).
v1.1 변경: Codex UI 시안 반영 — `change_bp`, `spark`, `contract`, `required`, SOX, `calendar.released/upcoming`, `briefing.points`, `relation_ko`, `banner`, `sources`.

```json
{
  "schema_version": "1.1",
  "run_date": "2026-11-05",
  "weekday_ko": "목",
  "us_session": "2026-11-04",
  "us_market_closed": false,
  "generated_at": "2026-11-04T22:16:30Z",
  "run_status": "OK",

  "banner": null,

  "indicators": [
    {
      "key": "NDX", "label": "나스닥100", "kind": "현물", "symbol": "^NDX", "group": "index",
      "required": true,
      "value": 21034.12, "unit": null,
      "change": 301.5, "change_pct": 1.45, "change_bp": null,
      "contract": null,
      "spark": [20510.2, 20633.0, 20420.8, 20732.6, 21034.12],
      "as_of": "2026-11-04T21:00:00Z", "source": "yfinance",
      "stale": false, "missing": false
    },
    {
      "key": "NQ", "label": "나스닥100", "kind": "선물", "symbol": "NQ=F", "group": "futures",
      "required": true,
      "value": 21102.5, "unit": null,
      "change": 88.25, "change_pct": 0.42, "change_bp": null,
      "contract": "2026-12",
      "spark": [],
      "as_of": "2026-11-04T22:05:00Z", "source": "yfinance",
      "stale": false, "missing": false
    },
    {
      "key": "US10Y", "label": "미국 10년물", "kind": "금리", "symbol": "^TNX", "group": "rates",
      "required": true,
      "value": 4.28, "unit": "%",
      "change": 0.03, "change_pct": null, "change_bp": 3,
      "contract": null, "spark": [4.21, 4.24, 4.22, 4.25, 4.28],
      "as_of": "2026-11-04T21:00:00Z", "source": "yfinance",
      "stale": false, "missing": false
    },
    {
      "key": "SOX", "label": "필라델피아 반도체", "kind": "업종", "symbol": "^SOX", "group": "sector",
      "required": false,
      "value": 5182.64, "unit": null,
      "change": 143.61, "change_pct": 2.85, "change_bp": null,
      "contract": null, "spark": [],
      "as_of": "2026-11-04T21:00:00Z", "source": "yfinance",
      "stale": false, "missing": false
    }
  ],

  "sentiment": {
    "source": "VIX", "vix": 14.82, "score": 62, "label": "탐욕", "label_en": "GREED",
    "method": "vix_percentile_252d_v0",
    "note_ko": "VIX 기반 자체 산출 · CNN Fear & Greed 아님"
  },

  "calendar": {
    "released": [
      {
        "time_utc": "2026-11-04T15:00:00Z", "event": "ISM Services PMI", "event_ko": "ISM 서비스업 PMI",
        "importance": "high", "forecast": "52.5", "previous": "52.0",
        "actual": "54.1", "actual_source": "forexfactory|llm_news", "tag_ko": "경기"
      }
    ],
    "upcoming": [
      {
        "time_utc": "2026-11-05T13:30:00Z", "event": "Unemployment Claims", "event_ko": "신규 실업수당 청구건수",
        "importance": "medium", "forecast": "225K", "previous": "221K", "tag_ko": "고용"
      }
    ],
    "status": "ok|missing"
  },

  "news": [
    {
      "id": "n_3f2a", "category": "theme_earnings", "importance": 5,
      "importance_label_ko": "관찰 중요도 높음",
      "title_ko": "엔비디아·TSMC AI 칩 출하 호조에 반도체 동반 강세",
      "preview_ko": "카드 접힘 상태에서 보이는 1문장",
      "published_at": "2026-11-04T10:40:00Z", "source": "CNBC", "url": "https://...",
      "summary_ko": ["...", "...", "..."],
      "kr_impact": {"direction": "positive", "text": "국내 반도체 밸류체인 개장 후 동반 반응 관찰"},
      "us_tickers": ["NVDA", "TSM"]
    }
  ],

  "themes": [
    {
      "us_ticker": "GEV", "name": "GE Vernova", "change_pct": 3.8,
      "theme": "데이터센터 · 전력 인프라",
      "momentum_ko": "초고압 변압기 수주 잔고 사상 최대 (뉴스 근거)",
      "related_news_ids": ["n_7c1d"],
      "kr_stocks": [
        {"code": "267260", "name": "HD현대일렉트릭", "link": "same_theme", "relation_ko": "동일 업종"}
      ],
      "mapping_source": "theme_map.yaml"
    }
  ],

  "briefing": {
    "direction": "gap_up|flat|gap_down",
    "direction_ko": "갭상승 우세",
    "confidence": "low|medium|high",
    "headline_ko": ["기술주가 이끈 반등,", "아침의 초점은 반도체."],
    "headline_emphasis": "반도체",
    "points": [
      {"title_ko": "위험 선호 회복", "text_ko": "..."},
      {"title_ko": "금리는 여전히 변수", "text_ko": "..."},
      {"title_ko": "국내 연결 포인트", "text_ko": "..."}
    ],
    "focus_chips": [{"label": "AI 반도체", "anchor": "#themes"}, {"label": "전력 인프라", "anchor": "#themes"}],
    "risks_ko": ["..."],
    "source": "gpt-5.5|gpt-4o-mini|template",
    "label": "참고용"
  },

  "sources": [
    {"name": "yfinance", "items": ["NDX", "SPX", "NQ", "ES", "VIX", "US10Y", "USDKRW", "SOX"]},
    {"name": "Forex Factory", "items": ["calendar"]},
    {"name": "CNBC RSS", "items": ["news"]}
  ],

  "warnings": ["캘린더 미수집"]
}
```

## 규칙
- `indicators`는 항상 8개 항목: 필수 7개(NDX, NQ, SPX, ES, VIX, US10Y, USDKRW — `required: true`) + 참고 SOX(`required: false`). 표시 순서는 이 순서.
  - 수집 실패 시 `value: null, missing: true`. `required: true` 중 하나라도 missing이면 `run_status = FAILED_INDICATORS`.
  - 금리(`group: rates`)는 `change_bp` 필수, `change_pct`는 null.
  - 선물(`group: futures`)은 `contract`(근월물 YYYY-MM) 필수, `spark`는 비워도 됨.
  - `spark`는 최근 5거래일 종가(마지막 값 = `value`), 실패 시 `[]`.
- `banner`: null 또는 `{"level": "info|warn|error", "text_ko": "..."}`.
  - 미장 휴장 → info "어젯밤 미국장은 휴장했습니다. 직전 거래일 기준입니다."
  - DEGRADED → warn "일부 데이터를 가져오지 못했습니다: ..."
  - 전일 페이지 유지 → error "오늘 브리핑 생성에 실패해 어제 내용을 표시합니다."
- `category`: `macro_rates` | `theme_earnings` | `geopolitics_commodities` | `other`.
  - 화면 필터 매핑: macro_rates → 매크로, theme_earnings → 테마·실적, geopolitics_commodities → 원자재·리스크, other → 전체에만.
- `importance_label_ko`: 5·4 "관찰 중요도 높음", 3 "업종 확산 관찰". importance ≤ 2는 `news`에 넣지 않는다.
- `kr_impact.direction`: `positive` | `negative` | `neutral`.
- `themes[]`: `settings.themes.benchmark_key` 지수보다 `min_excess_pct`%p 이상 오른 상승 종목만(상승률 순). 지수 데이터가 없으면 빈 배열. `themes_rule_ko`는 적용 규칙 문구(M7 미실행 시 빈 문자열 → 화면 "데이터 없음").
- `calendar.upcoming[]`: `settings.calendar.upcoming_importance`(기본 high)만. `released[]`는 high·medium 모두. `kr_stocks`는 `theme_map.yaml`에 있는 것만, 제안은 `data/proposals/`에만.
- `briefing.headline_emphasis`는 `headline_ko` 안에 실제로 포함된 단어여야 한다(M9 검증).
- `calendar.upcoming`: run_date 당일 KST 18:00 ~ 익일 06:00에 발표되는 USD High/Medium 지표.
- 원본(`data/raw/`)에는 기사 제목·링크·RSS 요약(≤300자)만 저장.
