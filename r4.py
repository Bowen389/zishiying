"""Rank final R4 candidates from same-session market and candidate features."""
import argparse
import json
import math
from pathlib import Path

MAIN_BOARD = ("600", "601", "603", "605", "000", "001", "002", "003")


def rank(snapshot):
    market = snapshot["market"]
    close = market["close"]
    ma20 = market["ma20"]
    if market["width"] >= .55 or (close > ma20 > market["ma20_five_sessions_ago"]):
        return "非 R4 象限", []
    if close / ma20 - 1 > -.006 + 1e-12:
        return "R4 指数乖离未达门槛", []
    median = snapshot["first_board_amount_median20"]
    if not math.isfinite(median) or median <= 0:
        raise ValueError("近20日首板成交额中位数无效")

    candidates = []
    for row in snapshot["candidates"]:
        code = row["code"]
        if not code.startswith(MAIN_BOARD) or row["st"] or not row["first_board"] or row["one_price"]:
            continue
        if not -.5 <= row["signal_gap_pct"] <= 5.5 or row["positive10"] > 5:
            continue
        amount = row["amount"]
        if amount < max(220_000_000, 1.4 * median):
            continue
        result = {**row, "amount_multiple": amount / median}
        if snapshot["phase"] == "after_close":
            candidates.append(result)
            continue
        gap = row["opening_gap_pct"]
        if not -6.5 <= gap < -2.2:
            continue
        vwap_gain = max(-2, min(6, row["vwap_gain_pct"]))
        result["score"] = .35 * math.log1p(amount / median) + .25 * vwap_gain / 3 - .25 * gap / 3
        candidates.append(result)

    if snapshot["phase"] == "after_close":
        return "R4 盘后预选，待次日开盘筛选", sorted(candidates, key=lambda row: row["code"])
    candidates.sort(key=lambda row: (-row["score"], row["code"]))
    selected = candidates[:1]
    if len(candidates) >= 2 and candidates[1]["amount_multiple"] >= 2:
        selected.append(candidates[1])
    return "R4 开盘筛选", selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    if snapshot["phase"] not in {"after_close", "opening"}:
        parser.error("phase must be after_close or opening")
    heading, rows = rank(snapshot)
    lines = [f'# {snapshot["signal_date"]} {heading}', "",
             "| 股票代码 | 成交额（亿元） | 成交额倍数 | 开盘缺口（%） | 排序分 |",
             "| --- | ---: | ---: | ---: | ---: |"]
    for row in rows:
        gap = row.get("opening_gap_pct", "待开盘")
        score = f'{row["score"]:.4f}' if "score" in row else "—"
        lines.append(f'| {row["code"]} | {row["amount"]/1e8:.2f} | {row["amount_multiple"]:.2f} | {gap} | {score} |')
    if not rows:
        lines.extend(["", "本次无 R4 信号。"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
