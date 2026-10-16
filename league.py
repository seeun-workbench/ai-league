"""AI 투자 리그: Claude 여러 명이 가상 1천만 원으로 한국 주식을 모의 매매한다.

하루 한 번(평일 장 마감 후) 실행한다.
  1) 어제 낸 주문을 오늘 시가로 체결
  2) 오늘 종가로 평가
  3) 각 선수가 오늘 종가까지의 정보로 내일 주문(목표 비중)을 냄
  4) 상태 저장, 페이지(docs/index.html) 생성, 슬랙 알림(선택)

실제 돈은 들어가지 않는다. 결과는 구경거리일 뿐 투자 조언이 아니다.

사용법
  python league.py              # 실제 실행 (ANTHROPIC_API_KEY 필요)
  python league.py --force      # 휴장일 판단, 중복 실행 방지를 건너뜀
  python league.py --dry-run    # 가짜 시세와 가짜 판단으로 30일 시뮬레이션 (API 호출 없음)
  python league.py --reset      # 리그를 처음부터 다시 시작
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state" / "state.json"
PAGE_PATH = ROOT / "docs" / "index.html"
KST = ZoneInfo("Asia/Seoul")

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
MODEL = os.environ.get("LEAGUE_MODEL", "claude-fable-5-1")                    # 매매 판단
RESEARCH_MODEL = os.environ.get("LEAGUE_RESEARCH_MODEL", "claude-opus-5-5")  # 웹 조사(검색 결과가 길어서 입력 토큰이 큼)
MARKET_CLOSE_READY = (15, 50)  # 이 시각(KST) 이후에만 그날 시세를 종가로 인정
FALLBACK_MODEL = "claude-sonnet-5-5"   # 예산이 빠듯하면 이 모델로 내려감
MONTHLY_BUDGET = float(os.environ.get("LEAGUE_MONTHLY_BUDGET", "100"))  # 달러
SEARCH_MAX = int(os.environ.get("LEAGUE_SEARCH_MAX", "5"))             # 선수당 하루 웹 검색 수
# 크레딧 구간. 프로모션 크레딧마다 만료일이 달라서, 구간별로 예산과 강도를 정한다.
# end는 그 크레딧으로 마지막 실행할 날(KST). 구간이 없는 날은 Claude를 부르지 않고 보유만 유지.
CREDIT_WINDOWS = [
    # 10/18(UTC) 만료 크레딧 93.5달러: 남은 거래일이 10/16 하루뿐이라 최대 강도로
    {"id": "promo-1018", "start": "2026-10-16", "end": "2026-10-17", "budget": 90.0,
     "research_model": "claude-fable-5-1", "search_max": 10},
    # 11/18(UTC) 만료 크레딧 100달러: 약 22거래일에 나눠 쓰기
    {"id": "promo-1118", "start": "2026-10-18", "end": "2026-11-17", "budget": 97.0,
     "research_model": "claude-opus-5-5", "search_max": 5},
]


def window_for(d):
    for w in CREDIT_WINDOWS:
        if w["start"] <= d.isoformat() <= w["end"]:
            return w
    return None


def month_setting(d, key, default):
    w = window_for(d)
    return w.get(key, default) if w else default


TRADING_DAYS_PER_MONTH = 21
# 100만 토큰당 달러 (입력, 출력). 바뀌면 여기만 고치면 됨
PRICES = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
SEARCH_PRICE = 0.01
START_CASH = 10_000_000
BUY_FEE = 0.00015            # 매수 수수료 0.015%
SELL_FEE = 0.00015 + 0.002   # 매도 수수료 0.015% + 거래세 약 0.2% (단순화)
MAX_WEIGHT = 40.0            # 한 종목 최대 비중(%)
JOURNAL_KEEP = 60            # 선수별 일지 보관 개수

UNIVERSE = {
    "005930.KS": "삼성전자",
    "000660.KS": "SK하이닉스",
    "373220.KS": "LG에너지솔루션",
    "207940.KS": "삼성바이오로직스",
    "005380.KS": "현대차",
    "000270.KS": "기아",
    "068270.KS": "셀트리온",
    "035420.KS": "NAVER",
    "035720.KS": "카카오",
    "105560.KS": "KB금융",
    "055550.KS": "신한지주",
    "005490.KS": "POSCO홀딩스",
    "051910.KS": "LG화학",
    "006400.KS": "삼성SDI",
    "012330.KS": "현대모비스",
    "028260.KS": "삼성물산",
    "066570.KS": "LG전자",
    "017670.KS": "SK텔레콤",
    "032830.KS": "삼성생명",
    "329180.KS": "HD현대중공업",
}
BENCH = "069500.KS"  # KODEX 200
BENCH_NAME = "KOSPI200 지수"

PLAYERS = {
    "value": {
        "name": "가치투자자 김버핏",
        "short": "김버핏",
        "kind": "llm",
        "color": "#2F6B4F",
        "persona": (
            "당신은 장기 가치투자자다. 많이 빠졌지만 사업이 튼튼한 대형주를 싸게 사서 오래 들고 간다. "
            "매매는 드물게 한다. 특별한 이유가 없으면 지금 포트폴리오를 그대로 유지한다. "
            "단기 등락에 흔들리지 않고, 3~6종목에 나눠 담는다."
        ),
        "research": "보유 종목과 관심 종목의 최근 실적, PER, PBR, 배당, 재무 건전성 관련 소식",
    },
    "momentum": {
        "name": "추세 추종자 박모멘텀",
        "short": "박모멘텀",
        "kind": "llm",
        "color": "#C6613F",
        "persona": (
            "당신은 추세 추종 트레이더다. 최근 20일과 60일 수익률이 강한 종목을 타고, "
            "산 가격보다 8% 넘게 빠진 종목은 미련 없이 자른다. 2~5종목에 집중한다."
        ),
        "research": "오늘 거래대금과 외국인, 기관 수급이 몰린 종목, 신고가 종목, 주도 업종",
    },
    "news": {
        "name": "뉴스 헌터 이속보",
        "short": "이속보",
        "kind": "llm",
        "color": "#2D5FA8",
        "persona": (
            "당신은 뉴스에 반응하는 단기 트레이더다. 오늘 나온 실적, 정책, 업황 뉴스를 보고 "
            "수혜가 예상되는 종목을 사고 악재가 나온 종목은 판다. 뉴스가 별로 없으면 관망한다."
        ),
        "research": "오늘 한국 증시 마감 시황과 종목별 실적 발표, 정책, 수주, 업황 뉴스",
    },
    "contrarian": {
        "name": "역발상 최반대",
        "short": "최반대",
        "kind": "llm",
        "color": "#7A4FA8",
        "persona": (
            "당신은 역발상 투자자다. 시장이 공포에 빠져 과하게 빠진 종목을 사고, 모두가 열광하는 종목은 판다. "
            "최근 5일과 20일 낙폭이 큰 종목을 눈여겨보되, 망할 회사는 피한다. 3~5종목."
        ),
        "research": "최근 급락한 종목의 하락 이유와 과매도 여부, 시장 심리와 공포 지표",
    },
    "semi": {
        "name": "반도체 덕후 정반도",
        "short": "정반도",
        "kind": "llm",
        "color": "#B8860B",
        "persona": (
            "당신은 반도체와 IT 하드웨어만 깊게 파는 전문 투자자다. 삼성전자, SK하이닉스를 중심으로 "
            "LG전자, 삼성SDI 같은 관련주만 다룬다. 업황이 나쁘다고 판단하면 현금 비중을 크게 늘린다."
        ),
        "research": "메모리 반도체 가격, HBM, AI 서버 수요, 엔비디아 등 해외 반도체 업황과 삼성전자, SK하이닉스 소식",
    },
    "bio": {
        "name": "바이오 전문가 한바이오",
        "short": "한바이오",
        "kind": "llm",
        "color": "#1F8A8A",
        "persona": (
            "당신은 바이오 전문 투자자다. 삼성바이오로직스와 셀트리온을 주로 다루고, 바이오에 확신이 없을 땐 "
            "다른 대형주나 현금으로 피신한다. 임상, 수주, 규제 뉴스에 민감하다."
        ),
        "research": "삼성바이오로직스, 셀트리온의 수주, 임상, 바이오시밀러, 미국 관세와 규제 소식, 글로벌 바이오 업황",
    },
    "defensive": {
        "name": "배당 지킴이 오배당",
        "short": "오배당",
        "kind": "llm",
        "color": "#6B675C",
        "persona": (
            "당신은 잃지 않는 게 최우선인 방어형 투자자다. 금융, 통신, 보험처럼 배당이 높고 덜 흔들리는 종목을 "
            "선호하고, 시장이 불안하면 현금을 늘린다. 매매는 적게 한다."
        ),
        "research": "금리, 환율, 배당 정책, 밸류업 프로그램, 은행과 보험, 통신사 관련 소식",
    },
    "flow": {
        "name": "수급 추종자 강수급",
        "short": "강수급",
        "kind": "llm",
        "color": "#A0522D",
        "persona": (
            "당신은 외국인과 기관 수급을 따라가는 투자자다. 큰손이 며칠째 사는 종목을 따라 사고, "
            "큰손이 팔기 시작하면 먼저 뺀다. 2~5종목."
        ),
        "research": "오늘과 최근 며칠 외국인, 기관 순매수와 순매도 상위 종목, 프로그램 매매 동향",
    },
    "macro": {
        "name": "거시경제 박사 윤매크로",
        "short": "윤매크로",
        "kind": "llm",
        "color": "#4A5A86",
        "persona": (
            "당신은 금리, 환율, 미국 증시, 유가 같은 거시 변수로 업종을 고르는 투자자다. "
            "환율이 오르면 수출주, 금리가 오르면 금융주처럼 큰 흐름에 맞춰 비중을 조절한다. 3~6종목."
        ),
        "research": "미국 증시 마감, 미국 국채 금리, 원달러 환율, 유가, 한국은행과 연준 관련 최신 소식",
    },
    "chart": {
        "name": "차트 장인 서캔들",
        "short": "서캔들",
        "kind": "llm",
        "color": "#9C3D6B",
        "persona": (
            "당신은 차트만 믿는 기술적 분석가다. 이동평균, 신고가 돌파, 과매수와 과매도, 거래량 급증을 보고 "
            "짧게 사고판다. 손절은 -5%. 2~4종목."
        ),
        "research": "대형주들의 기술적 신호(골든크로스, 신고가 돌파, RSI 과매수와 과매도, 거래량 급증) 관련 오늘 시황 기사",
    },
    "monkey": {
        "name": "원숭이 랜덤",
        "short": "원숭이",
        "kind": "random",
        "color": "#8C897F",
        "persona": "매주 월요일마다 아무 종목이나 3개 골라 똑같이 나눠 산다.",
    },
}

# ---------------------------------------------------------------------------
# 시세
# ---------------------------------------------------------------------------

def fetch_prices() -> dict[str, pd.DataFrame]:
    import yfinance as yf

    tickers = list(UNIVERSE) + [BENCH]
    raw = yf.download(tickers, period="6mo", interval="1d", group_by="ticker",
                      auto_adjust=False, progress=False, threads=True)
    out = {}
    for t in tickers:
        try:
            df = raw[t][["Open", "Close"]].dropna()
        except KeyError:
            continue
        df.index = pd.to_datetime(df.index).date
        if len(df):
            out[t] = df
    return out


def fake_prices(n_days: int, end: date, seed: int = 7) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    days = pd.bdate_range(end=end, periods=n_days).date
    out = {}
    for i, t in enumerate(list(UNIVERSE) + [BENCH]):
        drift = rng.normal(0.0004, 0.0008)
        vol = 0.012 if t == BENCH else rng.uniform(0.015, 0.03)
        close = 50_000 * (1 + i * 0.3) * np.exp(np.cumsum(rng.normal(drift, vol, n_days)))
        open_ = close * np.exp(rng.normal(0, vol / 3, n_days))
        out[t] = pd.DataFrame({"Open": open_.round(-1), "Close": close.round(-1)}, index=days)
    return out


def px(data, t, d, col):
    df = data.get(t)
    if df is None or d not in df.index:
        return None
    v = float(df.loc[d, col])
    return v if v > 0 and not math.isnan(v) else None


def market_table(data, d) -> tuple[str, dict]:
    rows, stats = [], {}
    for t, name in UNIVERSE.items():
        df = data.get(t)
        if df is None:
            continue
        c = df.loc[:d, "Close"]
        if len(c) < 2:
            continue
        last = float(c.iloc[-1])

        def ret(n):
            return (last / float(c.iloc[-n - 1]) - 1) * 100 if len(c) > n else float("nan")

        vol20 = float(c.pct_change().iloc[-20:].std() * math.sqrt(252) * 100)
        hi60 = float(c.iloc[-60:].max())
        s = {"close": last, "r1": ret(1), "r5": ret(5), "r20": ret(20), "r60": ret(60),
             "vol": vol20, "from_hi": (last / hi60 - 1) * 100}
        stats[t] = s
        rows.append(f"{t} {name} | 종가 {last:,.0f} | 1일 {s['r1']:+.1f}% | 5일 {s['r5']:+.1f}% | "
                    f"20일 {s['r20']:+.1f}% | 60일 {s['r60']:+.1f}% | 변동성 {vol20:.0f}% | "
                    f"60일 고점 대비 {s['from_hi']:+.1f}%")
    return "\n".join(rows), stats


# ---------------------------------------------------------------------------
# 상태와 체결
# ---------------------------------------------------------------------------

def new_state(d: date) -> dict:
    st = {"start_date": d.isoformat(), "last_date": None, "players": {},
          "bench": {"shares": 0, "cash": START_CASH, "history": []},
          "usage": {"input_tokens": 0, "output_tokens": 0, "web_searches": 0,
                    "cost_usd": 0.0, "by_month": {}}}
    return ensure_players(st)


def ensure_players(st: dict) -> dict:
    """PLAYERS에 새 선수를 추가하면 다음 실행부터 1천만 원으로 합류한다."""
    for pid in PLAYERS:
        st["players"].setdefault(pid, {"cash": START_CASH, "positions": {}, "cost": {},
                                       "pending": None, "history": [], "journal": [], "trades": []})
    u = st.setdefault("usage", {})
    for k, v in {"input_tokens": 0, "output_tokens": 0, "web_searches": 0,
                 "cost_usd": 0.0, "by_month": {}}.items():
        u.setdefault(k, v)
    return st


def load_state(d: date) -> dict:
    if STATE_PATH.exists():
        return ensure_players(json.loads(STATE_PATH.read_text()))
    return new_state(d)


def save_state(st: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=1))


def equity_at(p, data, d, col="Close"):
    total = p["cash"]
    for t, sh in p["positions"].items():
        price = px(data, t, d, col) or _last_close(data, t, d)
        total += sh * (price or 0)
    return total


def _last_close(data, t, d):
    df = data.get(t)
    if df is None:
        return None
    s = df.loc[:d, "Close"]
    return float(s.iloc[-1]) if len(s) else None


def rebalance(p, targets: dict[str, float], data, d):
    """목표 비중(%)대로 오늘 시가에 맞춘다. 매도 먼저, 그다음 매수."""
    prices = {t: px(data, t, d, "Open") for t in set(targets) | set(p["positions"])}
    eq = p["cash"] + sum(sh * (prices.get(t) or 0) for t, sh in p["positions"].items())
    want = {}
    for t, w in targets.items():
        if prices.get(t):
            want[t] = int(eq * w / 100 // (prices[t] * (1 + BUY_FEE)))
    # 매도
    for t, sh in list(p["positions"].items()):
        price = prices.get(t)
        if not price:
            continue
        diff = sh - want.get(t, 0)
        if diff > 0:
            p["cash"] += diff * price * (1 - SELL_FEE)
            pnl = (price / p["cost"].get(t, price) - 1) * 100
            p["trades"].append({"date": d.isoformat(), "side": "매도", "ticker": t,
                                "shares": diff, "price": price, "pnl": round(pnl, 2)})
            p["positions"][t] = sh - diff
            if p["positions"][t] == 0:
                del p["positions"][t]
                p["cost"].pop(t, None)
    # 매수
    for t, target_sh in want.items():
        price = prices[t]
        cur = p["positions"].get(t, 0)
        diff = target_sh - cur
        if diff <= 0:
            continue
        diff = min(diff, int(p["cash"] // (price * (1 + BUY_FEE))))
        if diff <= 0:
            continue
        p["cash"] -= diff * price * (1 + BUY_FEE)
        old_cost = p["cost"].get(t, price)
        p["cost"][t] = (old_cost * cur + price * diff) / (cur + diff)
        p["positions"][t] = cur + diff
        p["trades"].append({"date": d.isoformat(), "side": "매수", "ticker": t,
                            "shares": diff, "price": price})
    p["trades"] = p["trades"][-200:]


def clean_targets(raw) -> dict[str, float]:
    out = {}
    for item in raw or []:
        t, w = item.get("ticker"), item.get("weight")
        if t in UNIVERSE and isinstance(w, (int, float)) and w > 0:
            out[t] = min(float(w), MAX_WEIGHT)
    s = sum(out.values())
    if s > 100:
        out = {t: w * 100 / s for t, w in out.items()}
    return out


# ---------------------------------------------------------------------------
# 판단
# ---------------------------------------------------------------------------
SUBMIT_TOOL = {
    "name": "submit_orders",
    "description": "내일 시가에 맞출 목표 포트폴리오를 제출한다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "keep_current": {"type": "boolean",
                             "description": "true면 지금 포트폴리오를 그대로 둔다(targets 무시)."},
            "targets": {
                "type": "array",
                "description": "종목별 목표 비중(%). 합이 100 이하, 나머지는 현금.",
                "items": {"type": "object",
                          "properties": {"ticker": {"type": "string"},
                                         "weight": {"type": "number"}},
                          "required": ["ticker", "weight"]},
            },
            "reason": {"type": "string", "description": "판단 근거 2~3문장"},
            "diary": {"type": "string", "description": "오늘의 한 줄 일기, 캐릭터 말투로"},
        },
        "required": ["keep_current", "targets", "reason", "diary"],
    },
}


def holdings_text(p, stats):
    if not p["positions"]:
        return "보유 종목 없음"
    lines = []
    for t, sh in p["positions"].items():
        cur = stats.get(t, {}).get("close")
        cost = p["cost"].get(t)
        pnl = f"{(cur / cost - 1) * 100:+.1f}%" if cur and cost else "?"
        lines.append(f"{t} {UNIVERSE.get(t, t)} {sh}주 (평단 {cost:,.0f}, 수익률 {pnl})")
    return "\n".join(lines)


def build_prompt(pid, p, table, stats, d, equity, news=None):  # news = 내 조사 노트
    recent = "\n".join(f"- {j['date']}: {j['diary']}" for j in p["journal"][-3:]) or "- 없음"
    ret = (equity / START_CASH - 1) * 100
    parts = [
        f"오늘은 {d.isoformat()} 한국 증시 마감 직후다. 가상 1천만 원으로 하는 모의투자 대회에 참가 중이다.",
        f"규칙: 아래 20개 종목만 거래 가능. 한 종목 최대 {MAX_WEIGHT:.0f}%. 주문은 내일 시가에 체결된다. "
        "매도할 때 약 0.2% 비용이 든다. 다른 참가자보다 수익률이 높아야 한다.",
        f"\n[내 상태]\n총자산 {equity:,.0f}원 (시작 대비 {ret:+.2f}%), 현금 {p['cash']:,.0f}원\n"
        + holdings_text(p, stats),
        f"\n[최근 내 일기]\n{recent}",
        f"\n[오늘 시세]\n{table}",
    ]
    if news:
        parts.append(f"\n[오늘 내가 조사한 내용]\n{news}")
    parts.append("\nsubmit_orders 도구로 내일 목표 포트폴리오를 제출하라. 일기는 한국어로.")
    return "\n".join(parts)


def spend_key(d):
    w = window_for(d)
    return w["id"] if w else d.strftime("%Y-%m")


def month_cost(usage, d):
    """지금 크레딧 구간에서 쓴 금액(구간 밖이면 그달)."""
    return usage["by_month"].get(spend_key(d), 0.0)


def pick_model(usage, d):
    """지금 크레딧 구간의 남은 예산과 남은 거래일을 보고 모델과 조사 여부를 정한다."""
    w = window_for(d)
    if w is None:
        return None, False                      # 쓸 크레딧이 없는 날: 판단 쉼(보유 유지)
    spent = month_cost(usage, d)
    budget = w["budget"]
    if spent >= budget:
        return None, False                      # 예산 소진
    remaining = budget - spent
    days_left = max(1, len(pd.bdate_range(d, date.fromisoformat(w["end"]))))
    if remaining / days_left < 1.0:             # 하루 1달러도 안 남으면 싼 모델, 조사 생략
        return FALLBACK_MODEL, False
    return MODEL, True


def call_claude(client, usage, d, model, **kw):
    resp = client.messages.create(model=model, **kw)
    it, ot = resp.usage.input_tokens, resp.usage.output_tokens
    usage["input_tokens"] += it
    usage["output_tokens"] += ot
    searches = 0
    stu = getattr(resp.usage, "server_tool_use", None)
    if stu is not None:
        searches = getattr(stu, "web_search_requests", 0) or 0
        usage["web_searches"] += searches
    pin, pout = PRICES.get(model, PRICES["claude-fable-5-1"])
    cost = it / 1e6 * pin + ot / 1e6 * pout + searches * SEARCH_PRICE
    usage["cost_usd"] += cost
    key = spend_key(d)
    usage["by_month"][key] = usage["by_month"].get(key, 0.0) + cost
    return resp


def research(client, usage, d, model, pid):
    names = ", ".join(UNIVERSE.values())
    focus = PLAYERS[pid].get("research", "오늘 한국 증시 주요 뉴스")
    msgs = [{"role": "user", "content":
             f"{d.isoformat()} 기준으로 웹에서 다음을 조사해서 15줄 이내로 정리해줘: {focus}. "
             f"관련 종목명을 꼭 붙이고, 확인한 사실과 추측을 구분해. 거래 가능 종목: {names}. "
             "이 단계는 조사만 한다. 매수, 매도, 유지 같은 결정이나 비중 제안은 쓰지 말고, "
             "변수 이름이나 코드 같은 프로그램 용어도 쓰지 마."}]
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": month_setting(d, "search_max", SEARCH_MAX),
              "user_location": {"type": "approximate", "country": "KR", "timezone": "Asia/Seoul"}}]
    resp, parts = None, []
    for _ in range(4):
        resp = call_claude(client, usage, d, model, max_tokens=8000,
                           system=PLAYERS[pid]["persona"], tools=tools, messages=msgs)
        # 검색 전 "찾아볼게요" 같은 말은 버리고, 마지막 검색 결과 뒤에 나온 정리만 모은다
        chunk = []
        for b in resp.content:
            if b.type == "web_search_tool_result":
                chunk, parts = [], []
            elif b.type == "text":
                chunk.append(b.text)
        parts.extend(chunk)
        if resp.stop_reason != "pause_turn":
            break
        msgs = msgs + [{"role": "assistant", "content": resp.content}]
    text = "".join(parts).strip()
    if resp.stop_reason == "max_tokens":
        text += "\n(길이 제한으로 여기까지)"
    return text or "조사 결과 없음"


def decide_llm(client, usage, d, model, pid, p, table, stats, equity, news=None):
    # 일부 모델(Fable 등)은 도구 강제 지정(tool_choice tool/any)을 지원하지 않아 auto로 두고,
    # 도구를 안 쓰고 끝나면 한 번 더 요청한다.
    system = (PLAYERS[pid]["persona"]
              + " 답할 때는 반드시 submit_orders 도구를 정확히 한 번 호출해서 결정을 제출한다."
              " 일기와 근거에서 단어를 가운뎃점(·)으로 잇지 말고 쉼표를 쓴다.")
    msgs = [{"role": "user", "content": build_prompt(pid, p, table, stats, d, equity, news)}]
    for _ in range(2):
        resp = call_claude(client, usage, d, model, max_tokens=4000, system=system,
                           tools=[SUBMIT_TOOL], messages=msgs)
        for b in resp.content:
            if b.type == "tool_use" and b.name == "submit_orders":
                return b.input
        msgs = msgs + [{"role": "assistant", "content": resp.content},
                       {"role": "user", "content": "submit_orders 도구로 결정을 제출해줘."}]
    return {"keep_current": True, "targets": [], "reason": "도구 호출 없이 끝나 유지", "diary": "(응답 없음)"}


def decide_mock(pid, p, stats, d):
    """--dry-run 용 가짜 판단. API를 부르지 않는다."""
    rng = random.Random(f"{pid}{d}")
    ranked = sorted(stats, key=lambda t: stats[t]["r20"], reverse=True)
    if pid == "contrarian":
        picks = ranked[-3:]
        diary = f"다들 {UNIVERSE[picks[0]]}를 버릴 때 나는 줍는다."
    elif pid == "semi":
        picks = ["005930.KS", "000660.KS"]
        diary = "HBM이 답이다."
    elif pid == "bio":
        picks = ["207940.KS", "068270.KS"]
        diary = "바이오는 기다림의 미학."
    elif pid == "defensive":
        if p["positions"] and rng.random() < 0.85:
            return {"keep_current": True, "targets": [], "reason": "유지", "diary": "배당은 배신하지 않는다."}
        picks = ["105560.KS", "055550.KS", "017670.KS", "032830.KS"]
        diary = "금융과 통신으로 단단하게."
    elif pid == "momentum":
        picks = ranked[:3]
        diary = f"{UNIVERSE[picks[0]]}가 제일 세다. 올라탄다."
    elif pid == "value":
        if p["positions"] and rng.random() < 0.8:
            return {"keep_current": True, "targets": [], "reason": "기다린다.", "diary": "오늘도 버틴다."}
        picks = ranked[-4:]
        diary = f"{UNIVERSE[picks[0]]}가 싸 보인다. 조금씩 담는다."
    else:
        picks = rng.sample(list(stats), 3)
        diary = f"{UNIVERSE[picks[0]]} 관련 뉴스가 많았다."
    w = 90 / len(picks)
    return {"keep_current": False, "targets": [{"ticker": t, "weight": w} for t in picks],
            "reason": "가짜 판단(dry-run)", "diary": diary}


def decide_monkey(p, d):
    if p["positions"] and d.weekday() != 0:
        return {"keep_current": True, "targets": [], "reason": "월요일이 아님", "diary": "바나나 먹는 중."}
    rng = random.Random(d.isoformat())
    picks = rng.sample(list(UNIVERSE), 3)
    return {"keep_current": False, "targets": [{"ticker": t, "weight": 33} for t in picks],
            "reason": "무작위", "diary": "우끼끼! " + ", ".join(UNIVERSE[t] for t in picks)}


# ---------------------------------------------------------------------------
# 하루 진행
# ---------------------------------------------------------------------------

def run_day(st, data, d, client=None, dry=False):
    # 1) 어제 주문 체결
    for pid, p in st["players"].items():
        if p["pending"] is not None:
            rebalance(p, p["pending"], data, d)
            p["pending"] = None
    b = st["bench"]
    if b["shares"] == 0 and px(data, BENCH, d, "Open"):
        o = px(data, BENCH, d, "Open")
        b["shares"] = int(b["cash"] // (o * (1 + BUY_FEE)))
        b["cash"] -= b["shares"] * o * (1 + BUY_FEE)

    # 2) 평가
    for pid, p in st["players"].items():
        p["history"].append({"date": d.isoformat(), "equity": round(equity_at(p, data, d))})
    bc = px(data, BENCH, d, "Close") or _last_close(data, BENCH, d) or 0
    b["history"].append({"date": d.isoformat(), "equity": round(b["cash"] + b["shares"] * bc)})

    # 3) 내일 주문
    table, stats = market_table(data, d)
    usage = st["usage"]
    days_key = "days_" + d.strftime("%Y-%m")
    for pid, p in st["players"].items():
        if pid not in PLAYERS:
            continue
        kind = PLAYERS[pid]["kind"]
        eq = p["history"][-1]["equity"]
        model, do_research = (None, False) if dry else pick_model(usage, d)
        note = None
        if kind == "random":
            dec = decide_monkey(p, d)
        elif dry:
            dec = decide_mock(pid, p, stats, d)
        elif model is None:
            dec = {"keep_current": True, "targets": [], "reason": "쓸 수 있는 크레딧이 없어 쉬는 중",
                   "diary": "(크레딧 소진, 보유 유지)"}
        else:
            try:
                if do_research:
                    note = research(client, usage, d, month_setting(d, "research_model", RESEARCH_MODEL), pid)
                dec = decide_llm(client, usage, d, model, pid, p, table, stats, eq, note)
            except Exception as e:  # 한 선수 오류로 리그 전체가 멈추지 않게
                print(f"{pid} 오류:", e)
                dec = {"keep_current": True, "targets": [], "reason": f"오류로 유지: {e}"[:200],
                       "diary": "(오늘은 판단 실패)"}
        targets = None if dec.get("keep_current") else clean_targets(dec.get("targets"))
        p["pending"] = targets
        p["journal"].append({"date": d.isoformat(), "diary": dec.get("diary", "").replace("·", ", "),
                             "reason": dec.get("reason", "").replace("·", ", "), "targets": targets,
                             "model": model, "research": note.replace("·", ", ") if note else note})
        p["journal"] = p["journal"][-JOURNAL_KEEP:]
    usage.setdefault(days_key, [])
    if d.isoformat() not in usage[days_key]:
        usage[days_key].append(d.isoformat())
    st["last_date"] = d.isoformat()


# ---------------------------------------------------------------------------
# 페이지
# ---------------------------------------------------------------------------

def md(s):
    """조사 노트, 일기의 간단한 마크다운(굵게, 기울임, 코드, 제목, 목록)을 HTML로."""
    out = []
    for line in esc(s or "").split("\n"):
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])", r"<i>\1</i>", line)
        line = re.sub(r"`([^`]+)`", r"<code>\1</code>", line)
        m = re.match(r"^\s*#{1,6}\s+(.*)$", line)
        if m:
            line = f"<b>{m.group(1)}</b>"
        line = re.sub(r"^(\s*)[-*]\s+", r"\1• ", line)
        out.append(line)
    return "\n".join(out)


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def chart_svg(st):
    series = [(PLAYERS[pid]["name"], PLAYERS[pid]["color"], p["history"])
              for pid, p in st["players"].items()]
    series.append((BENCH_NAME, "var(--bench)", st["bench"]["history"]))
    n = max(len(h) for _, _, h in series)
    if n < 2:
        return "<p class='muted'>이틀 이상 쌓이면 그래프가 그려져요.</p>"
    W, H, L, R, T, B = 640, 260, 44, 12, 12, 26
    vals = [(e["equity"] / START_CASH - 1) * 100 for _, _, h in series for e in h]
    lo, hi = min(vals + [0]), max(vals + [0])
    pad = max((hi - lo) * 0.1, 0.5)
    lo, hi = lo - pad, hi + pad

    def x(i):
        return L + (W - L - R) * i / (n - 1)

    def y(v):
        return T + (H - T - B) * (hi - v) / (hi - lo)

    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="수익률 추이">']
    step = (hi - lo) / 4
    for k in range(5):
        v = lo + step * k
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="grid"/>'
                   f'<text x="{L - 6}" y="{y(v) + 4:.1f}" class="ax" text-anchor="end">{v:+.0f}%</text>')
    out.append(f'<line x1="{L}" x2="{W - R}" y1="{y(0):.1f}" y2="{y(0):.1f}" class="zero"/>')
    dates = series[0][2]
    for i in (0, n - 1):
        if i < len(dates):
            out.append(f'<text x="{x(i):.1f}" y="{H - 6}" class="ax" text-anchor="{"start" if i == 0 else "end"}">'
                       f'{dates[i]["date"][5:]}</text>')
    for name, color, h in series:
        off = n - len(h)
        pts = " ".join(f"{x(i + off):.1f},{y((e['equity'] / START_CASH - 1) * 100):.1f}"
                       for i, e in enumerate(h))
        dash = ' stroke-dasharray="5 4"' if name == BENCH_NAME else ""
        out.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2.2"{dash}>'
                   f'<title>{esc(name)}</title></polyline>')
    out.append("</svg>")
    return "".join(out)


def render(st, data=None):
    d = st["last_date"] or "-"
    rows = []
    for pid, p in st["players"].items():
        h = p["history"]
        eq = h[-1]["equity"] if h else START_CASH
        prev = h[-2]["equity"] if len(h) > 1 else START_CASH
        rows.append((pid, eq, (eq / START_CASH - 1) * 100, (eq / prev - 1) * 100))
    bh = st["bench"]["history"]
    beq = bh[-1]["equity"] if bh else START_CASH
    rows.sort(key=lambda r: r[1], reverse=True)

    board = []
    for rank, (pid, eq, tot, day) in enumerate(rows, 1):
        pl = PLAYERS[pid]
        board.append(
            f'<tr><td class="rk">{rank}</td><td><span class="dot" style="background:{pl["color"]}"></span>'
            f'<span class="full">{esc(pl["name"])}</span><span class="short">{esc(pl["short"])}</span></td><td class="num">{eq:,.0f}원</td>'
            f'<td class="num {"up" if tot >= 0 else "down"}">{tot:+.2f}%</td>'
            f'<td class="num {"up" if day >= 0 else "down"}">{day:+.2f}%</td></tr>')
    btot = (beq / START_CASH - 1) * 100
    board.append(f'<tr class="bench"><td class="rk"></td><td><span class="dot dash"></span>지수</td>'
                 f'<td class="num">{beq:,.0f}원</td><td class="num {"up" if btot >= 0 else "down"}">{btot:+.2f}%</td>'
                 f'<td></td></tr>')

    cards = []
    for pid, eq, tot, _ in rows:
        p, pl = st["players"][pid], PLAYERS[pid]
        j = p["journal"][-1] if p["journal"] else {}
        hold = []
        for t, sh in sorted(p["positions"].items(), key=lambda kv: -kv[1]):
            price = _last_close(data, t, date.fromisoformat(st["last_date"])) if data else None
            cost = p["cost"].get(t)
            pnl = f' <span class="{"up" if price >= cost else "down"}">{(price / cost - 1) * 100:+.1f}%</span>' \
                if price and cost else ""
            hold.append(f"<li>{esc(UNIVERSE.get(t, t))} {sh}주{pnl}</li>")
        cash_pct = p["cash"] / eq * 100 if eq else 0
        hold.append(f'<li class="muted">현금 {cash_pct:.0f}%</li>')
        plan = j.get("targets")
        plan_txt = "그대로 유지" if plan is None else (
            ", ".join(f"{UNIVERSE[t]} {w:.0f}%" for t, w in plan.items()) or "전부 현금")
        cards.append(
            f'<article class="card" style="--c:{pl["color"]}"><h3>{esc(pl["name"])}</h3>'
            f'<p class="diary">“{md(j.get("diary", ""))}”</p>'
            f'<p class="reason">{md(j.get("reason", ""))}</p>'
            f'<div class="k">보유</div><ul>{"".join(hold)}</ul>'
            f'<div class="k">내일 계획</div><p class="plan">{esc(plan_txt)}</p>'
            + (f'<details><summary>오늘 조사 노트</summary><p class="note">{md(j["research"])}</p></details>'
               if j.get("research") else "")
            + '</article>')

    trades = []
    for pid, p in st["players"].items():
        for t in p["trades"]:
            trades.append((t["date"], PLAYERS[pid]["short"], t))
    trades.sort(key=lambda x: x[0], reverse=True)
    trow_list = []
    for dt, nm, t in trades[:20]:
        pnl = f'{t["pnl"]:+.1f}%' if "pnl" in t else ""
        side_cls = "up" if t["side"] == "매수" else "down"
        trow_list.append(
            f'<tr><td>{dt[5:]}</td><td>{esc(nm)}</td><td class="{side_cls}">{t["side"]}</td>'
            f'<td>{esc(UNIVERSE.get(t["ticker"], t["ticker"]))}</td><td class="num">{t["shares"]}주</td>'
            f'<td class="num hide-m">{t["price"]:,.0f}</td><td class="num">{pnl}</td></tr>')
    trows = "".join(trow_list) or '<tr><td colspan="7" class="muted">아직 체결된 거래가 없어요.</td></tr>'

    u = st["usage"]
    days = len(st["bench"]["history"])
    legend = "".join(f'<span><i style="background:{PLAYERS[pid]["color"]}"></i>{esc(PLAYERS[pid]["name"])}</span>'
                     for pid in st["players"]) + f'<span><i class="dash"></i>{BENCH_NAME}</span>'

    html = f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="light dark"><title>AI 투자 리그</title>
<style>
:root{{--bg:#F7F6F2;--card:#FFFFFF;--ink:#23221F;--sub:#55534C;--muted:#8C897F;--line:#E6E3DA;--up:#C2352B;--down:#2D5FA8;--bench:#55534C}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1A1916;--card:#22211D;--ink:#EEEBE3;--sub:#C2BEB3;--muted:#8B877D;--line:#34322D;--up:#F07C70;--down:#8DB4EC;--bench:#C2BEB3}}}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:var(--bg);color:var(--ink);font-family:-apple-system,"Apple SD Gothic Neo","Noto Sans KR","Malgun Gothic",sans-serif;line-height:1.55;word-break:keep-all}}
.shell{{max-width:760px;margin:0 auto;padding:0 16px 40px}}
header{{display:flex;justify-content:space-between;align-items:baseline;gap:12px;padding:20px 0 12px;border-bottom:3px solid var(--ink)}}
h1{{font-size:22px;font-weight:800}} .date{{color:var(--sub);font-size:14px}}
.lede{{padding:18px 0;font-size:17px;font-weight:700;color:var(--sub)}}
h2{{font-size:16px;font-weight:800;margin:32px 0 10px;padding-bottom:8px;border-bottom:1.5px solid var(--ink)}}
table{{width:100%;border-collapse:collapse;font-size:14.5px}}
td{{padding:10px 6px;border-bottom:1px solid var(--line)}} .num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
.rk{{width:24px;font-weight:800;color:var(--muted)}} tr.bench td{{color:var(--sub)}}
.dot{{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:8px;vertical-align:0}}
.dot.dash,.legend i.dash{{background:none;border-top:2px dashed var(--bench);border-radius:0;height:0;width:14px;vertical-align:4px}}
.up{{color:var(--up)}} .down{{color:var(--down)}} .muted{{color:var(--muted)}}
svg{{width:100%;height:auto;display:block}} .grid{{stroke:var(--line)}} .zero{{stroke:var(--muted);stroke-width:1}}
.ax{{fill:var(--muted);font-size:11px}}
.legend{{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:13px;color:var(--sub);margin-top:8px}}
.legend i{{display:inline-block;width:14px;height:3px;margin-right:6px;vertical-align:4px;border-radius:2px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}}
.card{{background:var(--card);border:1px solid var(--line);border-top:4px solid var(--c);border-radius:10px;padding:14px}}
.card h3{{font-size:15px;font-weight:800}} .diary{{margin-top:8px;font-size:15px;font-weight:600}}
.reason{{margin-top:6px;font-size:13px;color:var(--sub)}} .k{{margin-top:12px;font-size:12px;font-weight:700;color:var(--muted)}}
.card ul{{list-style:none;font-size:13.5px;margin-top:2px}} .plan{{font-size:13.5px}}
details{{margin-top:10px;font-size:13px}} summary{{cursor:pointer;color:var(--sub);font-weight:700}}
.note{{white-space:pre-wrap;margin-top:6px;color:var(--sub);font-size:12.5px}} .note b{{color:var(--ink)}} code{{font-size:.95em;background:var(--line);padding:0 3px;border-radius:3px}}
.trades td{{font-size:13px;padding:7px 4px;white-space:nowrap}} .short{{display:none}}
@media (max-width:520px){{.full{{display:none}} .short{{display:inline}} .hide-m{{display:none}} h1{{font-size:20px}}}}
footer{{margin-top:36px;padding-top:12px;border-top:3px solid var(--ink);font-size:12.5px;color:var(--muted)}}
</style></head><body><div class="shell">
<header><h1>AI 투자 리그</h1><span class="date">{d} 마감 기준</span></header>
<p class="lede">가상 1천만 원으로 {days}거래일째. 실제 돈은 들어가지 않아요.</p>
<h2>순위</h2>
<table><tr><td></td><td class="muted">선수</td><td class="num muted">총자산</td><td class="num muted">누적</td><td class="num muted">오늘</td></tr>{"".join(board)}</table>
<h2>수익률 추이</h2>{chart_svg(st)}<div class="legend">{legend}</div>
<h2>선수별 오늘</h2><div class="cards">{"".join(cards)}</div>
<h2>최근 거래</h2><table class="trades">{trows}</table>
<footer>{st["start_date"]} 시작. 주문은 다음 거래일 시가에 체결, 매도 시 약 0.2% 비용 반영.<br>
지금 크레딧 구간 사용액 약 ${month_cost(u, date.fromisoformat(st["last_date"])) if st["last_date"] else 0:.2f} / ${month_setting(date.fromisoformat(st["last_date"]), "budget", MONTHLY_BUDGET) if st["last_date"] else MONTHLY_BUDGET:.0f}.
누적 ${u["cost_usd"]:.2f}, 입력 {u["input_tokens"]:,} 토큰, 출력 {u["output_tokens"]:,} 토큰, 웹 검색 {u["web_searches"]}회.</footer>
</div></body></html>"""
    PAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PAGE_PATH.write_text(html)


# ---------------------------------------------------------------------------
# 알림
# ---------------------------------------------------------------------------

def notify_slack(st):
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        return
    ranked = sorted(st["players"].items(), key=lambda kv: kv[1]["history"][-1]["equity"], reverse=True)
    parts = [f"{i}위 {PLAYERS[pid]['name']} {(p['history'][-1]['equity'] / START_CASH - 1) * 100:+.1f}%"
             for i, (pid, p) in enumerate(ranked, 1)]
    b = st["bench"]["history"][-1]["equity"]
    text = (f"AI 투자 리그 {st['last_date']}\n" + "\n".join(parts)
            + f"\n(지수 {(b / START_CASH - 1) * 100:+.1f}%)")
    top = ranked[0][1]["journal"][-1]["diary"] if ranked[0][1]["journal"] else ""
    if top:
        text += f"\n1위의 한마디: {top}"
    page = os.environ.get("LEAGUE_PAGE_URL")
    if page:
        text += f"\n{page}"
    req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # 알림 실패는 리그 진행에 영향 없음
        print("슬랙 알림 실패:", e)


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()
    today = datetime.now(KST).date()

    if a.dry_run:
        global STATE_PATH, PAGE_PATH
        STATE_PATH = ROOT / "state" / "dry_state.json"
        PAGE_PATH = ROOT / "docs" / "dry_run.html"
        data = fake_prices(130, today)
        dates = sorted(data[BENCH].index)[-a.days:]
        st = new_state(dates[0])
        for d in dates:
            run_day(st, data, d, dry=True)
        save_state(st)
        render(st, data)
        print(f"dry-run 완료: {PAGE_PATH}")
        return

    if a.reset and STATE_PATH.exists():
        STATE_PATH.unlink()

    data = fetch_prices()
    if BENCH not in data:
        sys.exit("시세를 받지 못했어요.")
    last = max(data[BENCH].index)
    st = load_state(last)
    now = datetime.now(KST)
    if not a.force:
        if last == today and (now.hour, now.minute) < MARKET_CLOSE_READY:
            print(f"장이 아직 안 끝났어요({now:%H:%M}). 15:50 이후에 실행돼야 해요.")
            return
        if last != today:
            print(f"오늘({today})은 휴장일이거나 시세가 아직 없어요. 마지막 거래일 {last}")
            return
        if st["last_date"] == last.isoformat():
            print("오늘 이미 실행했어요.")
            return

    import anthropic
    client = anthropic.Anthropic()
    run_day(st, data, last, client=client)
    save_state(st)
    render(st, data)
    notify_slack(st)
    print(f"{last} 완료")


if __name__ == "__main__":
    main()
