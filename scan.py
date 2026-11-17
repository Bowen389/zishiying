"""Generate dated four-quadrant preselection and opening reports."""
import argparse
import csv
import json
import math
import statistics as stats
import re
import time
from urllib.request import Request, urlopen
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from market_data import ROOT, package
from r4 import MAIN_BOARD, rank as rank_r4

def request(url, encoding="utf-8"):
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=18) as response:
                return response.read().decode(encoding)
        except (OSError, UnicodeError):
            if attempt == 2:
                raise
            time.sleep(attempt + 1)



def quotes(symbols):
    result = {}
    for start in range(0, len(symbols), 40):
        chunk = ",".join(symbols[start:start + 40])
        response = request(f"https://qt.gtimg.cn/q={chunk}", "gb18030")
        for symbol, payload in re.findall(r'v_([a-z0-9]+)="([^"]*)"', response):
            result[symbol] = payload.split("~")
    return result



SHANGHAI = ZoneInfo("Asia/Shanghai")
INDEX = "sh000001"
FIELDS = {
    "R1": {"amt": .35, "vr5": .20, "pre5": -.25, "topen": .10, "lsh": -.10},
    "R2": {"vr1": .30, "amt": .25, "tm1": -.20, "topen": .15, "pre3": -.10},
    "R3": {"pre5": -.35, "b10": -.30, "amt": .25, "pre3": -.10},
}


def trading_history(day):
    start = date.fromisoformat(day) - timedelta(days=125)
    query = urlencode({"param": f"{INDEX},day,{start},{day},85,"})
    result = json.loads(request(f"https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get?{query}"))
    return {row[0]: float(row[2]) for row in result["data"][INDEX]["day"] if row[0] <= day}


def load_bars(day):
    package(day)
    with (ROOT / f"{day}.csv").open(encoding="utf-8", newline="") as stream:
        return {row["code"]: row for row in csv.DictReader(stream)}


def price(row, field):
    return float(row[field])


def active(row):
    return price(row, "volume") > 0 and price(row, "prev_close") > 0


def main_board(row):
    return row["code"].startswith(MAIN_BOARD) and "ST" not in row["name"].upper() and "退" not in row["name"]


def ceiling(row):
    previous = Decimal(row["prev_close"]).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    return float((previous * Decimal("1.10")).quantize(Decimal(".01"), rounding=ROUND_HALF_UP))


def sealed(row):
    return bool(row and main_board(row) and active(row) and abs(price(row, "close") - ceiling(row)) < .005)


def first_boards(current, previous):
    return [
        row for code, row in current.items()
        if code in previous and active(previous[code]) and sealed(row) and not sealed(previous[code])
        and price(row, "high") > price(row, "low")
    ]


def width(bars):
    eligible = [row for row in bars.values() if active(row)]
    if len(eligible) < 3500:
        raise RuntimeError(f"有效全市场行情不足: {len(eligible)}")
    return sum(price(row, "close") > price(row, "prev_close") + .005 for row in eligible) / len(eligible)


def turnover(code, day):
    import akshare as ak

    stamp = day.replace("-", "")
    frame = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=stamp, end_date=stamp, adjust="")
    if frame.empty or str(frame.iloc[-1]["日期"])[:10] != day:
        raise RuntimeError(f"{code} 缺少 {day} 换手率")
    return float(frame.iloc[-1]["换手率"])


def features(row, history, day, quadrant):
    code = row["code"]
    prior = [bars[code] for bars in history if code in bars and active(bars[code])]
    if len(prior) < 10:
        return None
    close = price(row, "close")
    previous = price(row, "prev_close")
    volume = price(row, "volume")
    amount = price(row, "amount")
    volumes = [price(bar, "volume") for bar in prior[-6:]]
    returns = [price(bar, "close") / price(bar, "prev_close") for bar in prior[-9:]]
    adjusted = [previous]
    for ratio in reversed(returns):
        adjusted.append(adjusted[-1] / ratio)
    t_open = price(row, "open")
    lower_shadow = min(t_open, close) - price(row, "low")
    return {
        "code": code, "name": row["name"], "symbol": row["market"] + code,
        "st": False, "first_board": True, "one_price": False,
        "amount": amount, "amt": math.log(amount), "signal_gap_pct": (t_open / previous - 1) * 100,
        "positive10": sum(price(bar, "close") > price(bar, "open") for bar in prior[-9:] + [row]),
        "vwap_gain_pct": (close / (amount / volume) - 1) * 100,
        "vr1": volume / volumes[-1], "vr5": volume / stats.mean(volumes[-5:]),
        "vr6": volume / stats.mean(volumes), "pre5": math.prod(returns[-5:]) - 1,
        "pre3": math.prod(returns[-3:]) - 1, "b10": close / stats.mean([close, *adjusted[:9]]) - 1,
        "topen": (t_open / previous - 1) * 100, "lsh": lower_shadow / previous,
        "tm1": turnover(code, prior[-1]["date"]) if quadrant == "R2" else 0,
        "turnover": turnover(code, day) if quadrant == "R1" else 0,
        "t_board": abs(t_open - ceiling(row)) < .005,
        "listed35_proxy": len(prior) >= 34,
    }


def set_scores(rows, quadrant):
    weights = FIELDS.get(quadrant, {})
    distribution = {}
    for field in weights:
        values = [row[field] for row in rows]
        if values:
            distribution[field] = stats.mean(values), stats.pstdev(values)
    for row in rows:
        row["score"] = sum(
            weight * (row[field] - mean) / deviation
            for field, weight in weights.items()
            for mean, deviation in [distribution[field]] if deviation
        )


def tiers(row, snapshot):
    quadrant = snapshot["quadrant"]
    amount = row["amount"]
    score = row["score"]
    if quadrant == "R1":
        if snapshot["limit_count"] <= 85 and not row["t_board"] and 3 <= row["turnover"] <= 22:
            if -1.5 <= row["topen"] <= 5 and amount >= 2.5e8 and score >= .40:
                return [(2, 0, 1.5)]
    if quadrant == "R2":
        prior_width = snapshot["prior_width"]
        if prior_width >= .45 and snapshot["index3"] >= -.008 and snapshot["heat20"] <= 1.25:
            relative = amount / snapshot["first_board_amount_median20"]
            result = []
            if amount >= 1.6e8 and relative >= .85 and row["vr1"] >= 1.25:
                result.append((1, -4, -1.8))
            if prior_width >= .50 and relative >= 1.10 and score >= .30:
                result.append((2, -4, -1))
            return result
    if quadrant == "R3":
        change = snapshot["index_day"]
        result = []
        if change >= .001 and amount >= 1.8e8 and row["pre5"] <= .025 and row["b10"] <= .08:
            result.append((1, -5.5, -1.5))
        if change >= .0025 and row["pre5"] <= -.02 and row["b10"] <= .03 and score >= .45:
            result.append((2, -1.5, 0))
        return result
    return []


def select(snapshot):
    rows = snapshot["candidates"]
    phase = snapshot["phase"]
    quadrant = snapshot["quadrant"]
    if quadrant == "R4":
        return rank_r4(snapshot)[1]
    primary = []
    for row in rows:
        for tier, lower, upper in tiers(row, snapshot):
            if phase == "after_close" or lower <= row["opening_gap_pct"] < upper:
                primary.append({**row, "tier": tier, "lower": lower, "upper": upper})
                if phase == "opening":
                    break
    backup = [
        {**row, "tier": "B", "lower": -3, "upper": -2}
        for row in rows if row["b_pool"]
        and (phase == "after_close" or -3 <= row["opening_gap_pct"] <= -2)
    ]
    if phase == "after_close":
        return primary + backup
    def order(row):
        gap = row["opening_gap_pct"]
        if quadrant == "R2":
            return row["tier"], -(row["score"] - .25 * gap), row["code"]
        return row["tier"], gap if quadrant == "R3" else -row["score"], row["code"]
    return sorted(primary, key=order)[:2] or sorted(
        backup, key=lambda row: (row["opening_gap_pct"], row["code"])
    )[:1]


def write_report(snapshot, path):
    rows = select(snapshot)
    opening = snapshot["phase"] == "opening"
    day = snapshot.get("opening_date", snapshot["signal_date"])
    label = "开盘筛选" if opening else "盘后预选"
    market = snapshot["market"]
    lines = [
        f"# {day} 四象限首板 · {label}", "",
        f"信号日 {snapshot['signal_date']} · {snapshot['quadrant']} · 市场宽度 {market['width']:.2%} · {snapshot['coverage']}",
        "", "试运行口径见 README。B 仅在主策略零入选时补位。", "",
        "| 代码 | 名称 | 档位 | 成交额（亿元） | 缺口（%） |",
        "| --- | --- | --- | ---: | --- |",
    ]
    for row in rows:
        gap = f"{row['opening_gap_pct']:+.2f}" if opening else f"{row.get('lower', -6.5)} 至 {row.get('upper', -2.2)}"
        lines.append(f"| {row['code']} | {row['name']} | {row.get('tier', 'R4')} | {row['amount']/1e8:.2f} | {gap} |")
    if not rows:
        lines.extend(["", "本次无符合条件的股票。"])
    lines.extend(["", "T+1 开盘筛选，T+2 收盘退出。"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def scan(day, index):
    days = list(index)[-37:]
    if len(days) != 37:
        raise RuntimeError("上证交易日历史不足37日")
    with ThreadPoolExecutor(max_workers=6) as executor:
        history = list(executor.map(load_bars, days))
    previous, current = history[-2:]
    closes = list(index.values())[-25:]
    ma = stats.mean(closes[-20:])
    old_ma = stats.mean(closes[:20])
    breadth = width(current)
    quadrant = {
        (True, True): "R1", (False, True): "R2", (True, False): "R3", (False, False): "R4",
    }[breadth >= .55, closes[-1] > ma > old_ma]
    median = stats.median(
        price(row, "amount") for i in range(len(history) - 21, len(history) - 1)
        for row in first_boards(history[i], history[i - 1])
    )
    pool = first_boards(current, previous)
    with ThreadPoolExecutor(max_workers=4) as executor:
        rows = list(executor.map(lambda row: features(row, history[:-1], day, quadrant), pool))
    rows = [row for row in rows if row]
    set_scores(rows, quadrant)
    eligible_b = sorted(
        (row for row in rows if row["listed35_proxy"]), key=lambda row: (-row["vr6"], row["code"])
    )
    b_codes = {row["code"] for row in eligible_b[:math.ceil(len(eligible_b) / 2)]}
    for row in rows:
        row["b_pool"] = quadrant != "R4" and row["code"] in b_codes
    limit_count = lambda bars: sum(sealed(row) for row in bars.values())
    count = limit_count(current)
    base = stats.mean(limit_count(bars) for bars in history[-21:-1])
    snapshot = {
        "signal_date": day, "phase": "after_close", "quadrant": quadrant,
        "coverage": "沪深京" if any(row["market"] == "bj" for row in current.values()) else "沪深行情（缺北交所）",
        "market": {"close": closes[-1], "ma20": ma, "ma20_five_sessions_ago": old_ma, "width": breadth},
        "prior_width": width(previous), "first_board_amount_median20": median,
        "index3": closes[-1] / closes[-4] - 1, "index_day": closes[-1] / closes[-2] - 1,
        "limit_count": count, "heat20": count / base, "candidates": rows,
    }
    write_report(snapshot, Path("reports") / f"{day}.md")
    path = Path("state") / f"{day}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")


def opening(day, index):
    stamp = day.replace("-", "")
    quote = quotes([INDEX]).get(INDEX, [])
    if len(quote) < 31 or not quote[30].startswith(stamp):
        from exchange_calendars import get_calendar
        if get_calendar("XSHG").is_session(day):
            raise RuntimeError("交易日开盘行情尚未更新")
        print(f"{day} 休市")
        return
    signal_day = [session for session in index if session < day][-1]
    state = Path("state") / f"{signal_day}.json"
    if not state.exists():
        scan(signal_day, {session: close for session, close in index.items() if session < day})
    snapshot = json.loads(state.read_text(encoding="utf-8"))
    symbols = sorted({row["symbol"] for row in select(snapshot)})
    data = quotes(symbols)
    by_symbol = {}
    for symbol in symbols:
        quote = data.get(symbol, [])
        if len(quote) < 31 or not quote[30].startswith(stamp) or not quote[4] or not quote[5]:
            raise RuntimeError(f"{symbol} 缺当天开盘行情")
        previous, opened = float(quote[4]), float(quote[5])
        if previous <= 0:
            raise RuntimeError(f"{symbol} 昨收无效")
        if opened > 0:
            by_symbol[symbol] = float((Decimal(quote[5]) / Decimal(quote[4]) - 1) * 100)
    snapshot["candidates"] = [
        {**row, "opening_gap_pct": by_symbol[row["symbol"]]}
        for row in snapshot["candidates"] if row["symbol"] in by_symbol
    ]
    snapshot.update(phase="opening", opening_date=day)
    write_report(snapshot, Path("reports/opening") / f"{day}.md")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=date.fromisoformat)
    parser.add_argument("--opening", action="store_true")
    args = parser.parse_args()
    today = datetime.now(SHANGHAI).date()
    day = (args.date or today).isoformat()
    if args.opening and day != today.isoformat():
        parser.error("开盘任务仅接受当前交易日")
    index = trading_history(day)
    if args.opening:
        opening(day, index)
    elif day in index:
        scan(day, index)
    else:
        from exchange_calendars import get_calendar
        if get_calendar("XSHG").is_session(day):
            raise RuntimeError("交易日指数日线尚未更新")
        print(f"{day} 休市")


if __name__ == "__main__":
    main()
