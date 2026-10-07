# StockTrader

**버크셔 해서웨이의 최신 13F 공시 포트폴리오**를 소수점 매매로 따라가는 Alpaca 기반
미국 주식 자동매매 프로그램입니다. 코드가 공시를 읽어 리밸런싱 계획을 만들고, AI가
`strategy.md`(내 전략) + 스스로의 판단으로 계획을 검토해 주문합니다.
**롱(매수) 전용**이며 공매도는 원천 차단됩니다.

> 원래 코딩 에이전트(Agent Smith)였던 프로젝트에서 LLM 호출부만 남기고 재작성했습니다.

## 동작 방식

1. **공시 수집** (`trader/sec13f.py`): SEC EDGAR에서 버크셔의 최신 분기 13F를 받습니다.
   나중에 추가 공개되는 정정공시(13F-HR/A "NEW HOLDINGS")도 합치고, 옵션·채권은 뺍니다.
   CUSIP은 OpenFIGI로 티커로 바꿉니다.
2. **목표 비중** (`trader/portfolio.py`): Alpaca에서 소수점 거래가 안 되는 종목 등은 빼고
   100%로 다시 맞춥니다. 종목당 최대 비중 한도를 넘는 부분은 현금으로 남깁니다.
3. **리밸런싱 계획**: 목표 금액과 현재 보유 금액을 비교합니다.
   - 공시에서 빠진 종목 → 전량 매도
   - 목표보다 `rebalance_drift_pct`% 이상 모자람/넘침 → 차액만큼 매수/매도
   - 매수는 **금액 단위**(예: AAPL $229.30), 매도는 **수량 단위**(소수점 포함)
4. **AI 검토·실행** (`trader/agent.py`): 매매할 게 있을 때만 AI를 부릅니다.
   AI는 계획과 전략을 보고 주문하며, 모든 주문은 `OrderGuard`의 한도 검사를 거칩니다.
5. **기록**: 대화 전체와 주문 내역(거부 포함)을 `logs/날짜.jsonl`에 남깁니다.

`--loop`는 장중에 `cycle_minutes`(기본 하루)마다 위 과정을 반복합니다. 새 공시가 나오면
다음 사이클에 자동으로 반영됩니다.

### 13F의 한계
- 분기 종료 후 최대 45일 뒤에 공시되므로, 따라 사는 포트폴리오는 1.5~4.5개월 전 것입니다.
- 미국 상장 주식의 롱 포지션만 나옵니다(현금 비중·해외 주식 없음).
- 버크셔는 일부 종목 공개를 늦출 수 있습니다(나중에 정정공시로 반영됨).
- 2026년부터 CEO는 그렉 에이블이므로, 최신 공시는 에이블 체제의 판단입니다.

## 공매도 차단 (3중)

| 단계 | 위치 | 내용 |
|---|---|---|
| 1 | `trader/tools.py` | AI에게는 `buy`/`sell`만 있음. 공매도·신용·옵션 도구 자체가 없음 |
| 2 | `trader/guard.py`, `trader/broker.py` | 매도 수량 > (보유수량 − 미체결 매도수량)이면 거부. 매도는 수량 단위로만 받아 소수점까지 정확히 비교. 주문을 보내는 유일한 함수 `submit_order` 안에서도 다시 검사 |
| 3 | Alpaca 계좌 설정 | 시작할 때 계좌의 `no_shorting`(롱 전용 모드)을 켜고, 확인이 안 되거나 숏 포지션이 이미 있으면 **실행 거부** |

매수는 **현금(cash)으로만** 하며 마진(신용) 매수력은 쓰지 않습니다.
Alpaca도 소수점 주문은 공매도로 처리하지 않습니다.

## 설정

### 1. 설치
```bash
cp .env.example .env     # Alpaca 키, SEC 연락처 이메일, LLM 키 입력
uv sync
```
- Alpaca 키: https://app.alpaca.markets (페이퍼 계좌 키와 실계좌 키는 서로 다름)
- `SEC_USER_AGENT`: SEC 정책상 연락처 이메일이 필요합니다. 예: `"StockTrader you@example.com"`

### 2. 전략 — `strategy.md`
자연어로 씁니다. AI가 이 내용을 **우선** 따르고, 적혀 있지 않은 부분은 스스로 판단합니다.
(예: "공시 이후 많이 오른 종목은 나눠서 산다")

### 3. 안전 한도와 리밸런싱 — `trading_config.json`
코드에서 강제되는 값이라 AI가 무시할 수 없습니다.

| 항목 | 의미 | 기본값 |
|---|---|---|
| `follow_name`, `follow_cik` | 따라갈 운용사 (SEC CIK 번호) | 버크셔, 0001067983 |
| `ticker_overrides` | 티커 변환이 틀린 종목 수동 지정 `{"CUSIP": "티커"}` | {} |
| `min_weight_pct` | 이 비중(%) 미만 종목은 제외 | 0 (전부 포함) |
| `only_target_symbols` | true면 목표 포트폴리오 종목만 매수 가능 | true |
| `rebalance_drift_pct` | 목표 대비 이 % 이상 벗어나면 리밸런싱 | 10 |
| `min_trade_usd` | 최소 주문 금액 ($, Alpaca 최소 $1) | 1 |
| `max_order_usd` | 주문 1건 최대 금액 ($). 큰 매수는 자동으로 나눠짐 | 2000 |
| `max_position_pct` | 한 종목 최대 비중 (총자산 대비 %) | 30 |
| `min_cash_reserve_pct` | 항상 남겨둘 현금 비율 (%) | 2 |
| `max_orders_per_day` | 하루 최대 주문 수 | 60 |
| `cycle_minutes` | `--loop` 반복 간격 (분) | 1440 (하루) |
| `max_steps_per_cycle` | 한 사이클의 최대 AI 응답 횟수 | 60 |
| `data_feed` | 시세 피드 (`iex` 무료, `sip` 유료) | iex |

애플 비중이 20%를 넘기 때문에 `max_position_pct`를 그보다 낮추면 애플은 한도까지만 삽니다.

### 4. AI 모델 — `models.json`
OpenAI 호환 API(OpenRouter, Gemini 등)의 모델을 지정합니다.
**함수 호출(tool calling)을 지원하는 모델이어야 합니다.**

```bash
--provider gemini                             # 다른 제공자
--provider gemini --model-name <model>        # 특정 모델
--model-name <model> --provider-url <url>     # 직접 지정
```

## 실행

```bash
uv run python -m trader --plan           # 리밸런싱 계획만 출력 (AI·주문 없음) — 먼저 이걸로 확인
uv run python -m trader --dry-run        # 1회, AI가 판단하지만 주문은 로그만
uv run python -m trader                  # 1회, 페이퍼 계좌 (장중에만)
uv run python -m trader --loop           # 매일 반복, 페이퍼 계좌
uv run python -m trader --loop --live    # 실계좌 (실제 돈, 10초 대기 후 시작)
```

기본은 항상 **페이퍼 계좌**이며, 실계좌는 `--live`를 붙여야만 사용됩니다.
이 계좌의 보유 종목은 모두 전략 대상으로 간주하므로, **목표 포트폴리오에 없는 종목은
매도 계획에 들어갑니다.** 다른 용도의 종목이 있는 계좌에서는 쓰지 마세요.

## 테스트
```bash
uv run --extra dev pytest
```
공매도 차단(소수점 포함), 각 한도, 13F 파싱·정정공시 처리, 리밸런싱 계획을 네트워크 없이 검증합니다.

## 구조

| 파일 | 역할 |
|---|---|
| `trader/__main__.py` | CLI, 계획 출력/1회/반복 실행 |
| `trader/agent.py` | 시스템 프롬프트, AI 사이클 루프, 로그 |
| `trader/tools.py` | AI가 쓸 수 있는 도구 정의와 실행, 기술 지표 계산 |
| `trader/portfolio.py` | 목표 비중, 리밸런싱 계획 |
| `trader/sec13f.py` | SEC 13F 수집, CUSIP→티커 변환 (캐시: `cache/`) |
| `trader/guard.py` | 주문 게이트: 롱 전용 + 리스크 한도 |
| `trader/broker.py` | Alpaca REST 클라이언트 (주문 전송은 여기 한 곳뿐) |
| `trader/config.py` | `trading_config.json` 로드 |
| `trader/llm_provider.py` | OpenAI 호환 LLM 호출, 키 로테이션/재시도 |
| `trader/model_config.py` | `models.json`으로 모델 선택 |

## 주의

이 프로그램은 투자 조언이 아니며, 공시 시차와 AI 판단 오류로 손실이 날 수 있습니다.
충분히 페이퍼 계좌로 검증한 뒤 실계좌를 사용하고, 실계좌에서는 작은 금액부터 시작하세요.
