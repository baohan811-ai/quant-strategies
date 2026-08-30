#!/usr/bin/env python3
"""分析金叉触发日涨跌与次日开盘买入后收益的关系。

数据和交易规则均来自“趋势发现_金叉_中证800_前高回撤低效退出_执行版”：
- 共用行情读取层提供的前复权日线；
- 历史中证800月频成分快照；
- 次交易日开盘买入、交易成本、仓位约束、回撤/低效退出规则。

脚本输出只用于研究，不改动执行版脚本和原始工作簿。
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[3]
DB_PATH = ROOT / "执行中策略/缓存/本地行情数据库.sqlite3"
OUTPUT_DIR = ROOT / "执行中策略/输出/收益归因/金叉当日涨跌"
SCRIPT_ROOT = ROOT / "执行中策略/脚本"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from 维护工具.local_market_db import load_price_matrix

TRADE_START = pd.Timestamp("2022-01-01")
END_DATE = pd.Timestamp("2026-08-05")  # 本地完整日线截止日
MAX_HOLDINGS = 20
INITIAL_WEIGHT = 1 / MAX_HOLDINGS
COST = 0.0025
BASE_DRAWDOWN = 0.1175
CONCENTRATED_COUNT = 6
CONCENTRATED_DRAWDOWN = 0.10
BANK_DRAWDOWN = 0.08
LOW_EFF_MIN_DAYS = 60
LOW_EFF_MAX_PROFIT = 0.05
LOW_EFF_CURRENT_PROFIT = 0.00
SIGNAL_MAX_RETURN = 0.065
VOLUME_LOOKBACK = 20
VOLUME_WEIGHT = 0.25


@dataclass
class BacktestResult:
    label: str
    nav: pd.Series
    positions: pd.Series
    buy_lots: pd.DataFrame
    episodes: pd.DataFrame


def load_inputs():
    with sqlite3.connect(DB_PATH) as conn:
        snapshots = pd.read_sql_query(
            """
            SELECT snapshot_date, wind_code, sec_name
            FROM universe_constituents_snapshot
            WHERE universe_name='中证800'
              AND snapshot_date=(
                SELECT MAX(snapshot_date) FROM universe_constituents_snapshot
                WHERE universe_name='中证800' AND snapshot_date < ?
              )
            UNION ALL
            SELECT snapshot_date, wind_code, sec_name
            FROM universe_constituents_snapshot
            WHERE universe_name='中证800' AND snapshot_date BETWEEN ? AND ?
            ORDER BY snapshot_date, wind_code
            """,
            conn,
            params=[TRADE_START.strftime("%Y-%m-%d"), TRADE_START.strftime("%Y-%m-%d"), END_DATE.strftime("%Y-%m-%d")],
        )
        snapshots["snapshot_date"] = pd.to_datetime(snapshots["snapshot_date"])
        codes = snapshots["wind_code"].drop_duplicates().tolist()
        placeholders = ",".join("?" for _ in codes)
        industries = pd.read_sql_query(
            f"""
            SELECT wind_code, sec_name, industry_level1
            FROM stock_industry
            WHERE classification_system='wind_level1' AND wind_code IN ({placeholders})
            """,
            conn,
            params=codes,
        )

    fields = {}
    for field in ["open", "high", "low", "close", "volume", "amt"]:
        matrix = load_price_matrix(
            "收益归因_中证800",
            field,
            codes=codes,
            start_date="2018-01-01",
            end_date=END_DATE,
            target_columns=codes,
            prefer_sqlite=True,
            fallback_pickle=False,
            require_complete_eod=True,
            adjusted="F",
            db_path=DB_PATH,
        )
        matrix = matrix.where(matrix >= 0 if field in {"volume", "amt"} else matrix > 0, np.nan)
        fields[field] = matrix.loc[matrix.notna().sum(axis=1).gt(0)]
    names = (
        pd.concat([
            snapshots[["wind_code", "sec_name"]],
            industries[["wind_code", "sec_name"]],
        ])
        .dropna(subset=["sec_name"])
        .drop_duplicates("wind_code", keep="last")
        .set_index("wind_code")["sec_name"]
        .to_dict()
    )
    industry_map = (
        industries.drop_duplicates("wind_code", keep="last")
        .set_index("wind_code")["industry_level1"].fillna("未分类").to_dict()
    )
    bank_codes = {
        row.wind_code for row in industries.itertuples()
        if row.industry_level1 == "金融" and "银行" in str(row.sec_name)
    }
    member = build_member_matrix(snapshots, fields["close"].index, codes)
    return fields, member, names, industry_map, bank_codes


def build_member_matrix(snapshots, dates, codes):
    member = pd.DataFrame(False, index=dates, columns=codes)
    unique_dates = snapshots["snapshot_date"].drop_duplicates().sort_values().tolist()
    for i, snap_date in enumerate(unique_dates):
        next_date = unique_dates[i + 1] if i + 1 < len(unique_dates) else None
        active_dates = dates[dates >= snap_date] if next_date is None else dates[(dates >= snap_date) & (dates < next_date)]
        active_codes = snapshots.loc[snapshots["snapshot_date"].eq(snap_date), "wind_code"]
        active_codes = [c for c in active_codes if c in member.columns]
        if len(active_dates) and active_codes:
            member.loc[active_dates, active_codes] = True
    return member


def calculate_signals(fields, member):
    close = fields["close"]
    volume = fields["volume"]
    ma5 = close.rolling(5).mean()
    ma60 = close.rolling(60).mean()
    ma120 = close.rolling(120).mean()
    daily_ret = close.pct_change(fill_method=None)
    raw_cross = np.sign(ma5 - ma60).diff().eq(2)
    candidate = raw_cross & ma60.gt(ma60.shift(1)) & ma120.gt(ma120.shift(1))
    golden = candidate & daily_ret.le(SIGNAL_MAX_RETURN) & member

    gap_score = ma5 / ma60 - 1
    trend_score = ma60 / ma60.shift(5) - 1
    vol_ratio = volume / volume.rolling(VOLUME_LOOKBACK).mean().replace(0, np.nan)
    vol_score = VOLUME_WEIGHT * (vol_ratio - 1).clip(lower=0, upper=1)
    score = (gap_score + trend_score + vol_score).replace([np.inf, -np.inf], np.nan)
    buy_signal = golden.shift(1).fillna(False).astype(bool) & member
    return golden, buy_signal, daily_ret, score


def limit_ratio(code, name):
    if "ST" in str(name).upper():
        return 0.05
    if code.endswith(".BJ"):
        return 0.30
    if code.startswith("300") or code.startswith("688"):
        return 0.20
    return 0.10


def retrace_rule(code, holdings, industry_map, bank_codes):
    if code in bank_codes:
        return BANK_DRAWDOWN, "银行专属"
    target_industry = industry_map.get(code, "未分类")
    count = sum(industry_map.get(c, "未分类") == target_industry for c in holdings)
    if count >= CONCENTRATED_COUNT:
        return CONCENTRATED_DRAWDOWN, "行业集中"
    return BASE_DRAWDOWN, "常规"


def run_backtest(label, condition, fields, member, names, industry_map, bank_codes, buy_signal, signal_returns, score):
    open_df, high_df, low_df, close_df = (fields[k] for k in ["open", "high", "low", "close"])
    dates = close_df.index
    holdings = {}
    pending = {}
    cash = 1.0
    nav_values = []
    position_values = []
    lots = []
    episodes = []
    prev_date = None
    last_valid = close_df.apply(lambda x: x.dropna().index.max() if x.notna().any() else pd.NaT)

    for date in dates:
        sold_today = set()
        if prev_date is not None:
            for code in list(holdings):
                prev_close = close_df.at[prev_date, code]
                close = close_df.at[date, code]
                if "退市" in str(names.get(code, "")) and pd.isna(close) and pd.notna(last_valid[code]) and date > last_valid[code]:
                    close_episode(episodes, holdings[code], code, names, date, 0.0, -1.0, "退市归零", False)
                    del holdings[code]
                    pending.pop(code, None)
                    sold_today.add(code)
                    continue
                if pd.notna(prev_close) and pd.notna(close) and prev_close > 0:
                    holdings[code]["value"] *= close / prev_close

        for code, pending_info in list(pending.items()):
            if code not in holdings:
                pending.pop(code, None)
                continue
            op, cl = open_df.at[date, code], close_df.at[date, code]
            if pd.isna(op) or pd.isna(cl) or op <= 0 or cl <= 0:
                continue
            info = holdings[code]
            sell_value = info["value"] * op / cl
            proceeds = sell_value * (1 - COST)
            ret = proceeds / (info["cost_basis"] * (1 + COST)) - 1
            cash += proceeds
            close_episode(episodes, info, code, names, date, op, ret, pending_info["reason"], False)
            del holdings[code]
            del pending[code]
            sold_today.add(code)

        portfolio_before_buy = cash + sum(x["value"] for x in holdings.values())
        available_slots = MAX_HOLDINGS - len(holdings)
        if date >= TRADE_START and cash > 0 and prev_date is not None:
            candidates = buy_signal.columns[buy_signal.loc[date]].tolist()
            candidates = [
                c for c in candidates
                if c not in sold_today and (c in holdings or available_slots > 0)
                and condition(float(signal_returns.at[prev_date, c]))
            ]
            if candidates:
                ranked = score.loc[prev_date, candidates].dropna().sort_values(ascending=False)
                opened = 0
                max_candidates = sum(c in holdings for c in candidates) + available_slots
                for code in ranked.head(max_candidates).index:
                    existing = code in holdings
                    if not existing and opened >= available_slots:
                        continue
                    op, hi, cl = open_df.at[date, code], high_df.at[date, code], close_df.at[date, code]
                    prev_close = close_df.at[prev_date, code]
                    if any(pd.isna(x) or x <= 0 for x in [op, hi, cl]):
                        continue
                    if pd.notna(prev_close) and prev_close > 0 and op >= prev_close * (1 + limit_ratio(code, names.get(code, code))) * 0.999:
                        continue
                    target = portfolio_before_buy * INITIAL_WEIGHT
                    buy_value = min(target, cash / (1 + COST))
                    if buy_value <= 0:
                        break
                    cash -= buy_value * (1 + COST)
                    added_close_value = buy_value * cl / op
                    lot_id = len(lots)
                    lot = {
                        "lot_id": lot_id,
                        "策略组": label,
                        "代码": code,
                        "名称": names.get(code, code),
                        "信号日期": prev_date,
                        "买入日期": date,
                        "信号日涨跌幅": signal_returns.at[prev_date, code],
                        "信号评分": score.at[prev_date, code],
                        "买入价": op,
                        "买入金额": buy_value,
                        "是否加仓": existing,
                    }
                    lots.append(lot)
                    if existing:
                        info = holdings[code]
                        old_shares = info["cost_basis"] / info["entry_price"]
                        new_shares = old_shares + buy_value / op
                        info["entry_price"] = (info["cost_basis"] + buy_value) / new_shares
                        info["peak_price"] = max(info["peak_price"], hi)
                        info["value"] += added_close_value
                        info["cost_basis"] += buy_value
                        info["lots"].append(lot_id)
                    else:
                        opened += 1
                        holdings[code] = {
                            "entry_date": date,
                            "entry_price": op,
                            "peak_price": hi,
                            "value": added_close_value,
                            "cost_basis": buy_value,
                            "lots": [lot_id],
                        }

        for code in list(holdings):
            op, lo, hi, cl = (fields[k].at[date, code] for k in ["open", "low", "high", "close"])
            if any(pd.isna(x) for x in [op, lo, hi, cl]):
                continue
            info = holdings[code]
            if date <= info["entry_date"]:
                continue
            drawdown, rule_name = retrace_rule(code, holdings, industry_map, bank_codes)
            trigger = info["peak_price"] * (1 - drawdown)
            sell_price = op if lo <= trigger and op < trigger else (trigger if lo <= trigger else np.nan)
            if pd.notna(sell_price):
                prev_close = close_df.at[prev_date, code]
                limit_down = prev_close * (1 - limit_ratio(code, names.get(code, code)))
                if cl <= limit_down * 1.001:
                    continue
                sell_value = info["value"] * sell_price / cl
                proceeds = sell_value * (1 - COST)
                ret = proceeds / (info["cost_basis"] * (1 + COST)) - 1
                cash += proceeds
                close_episode(episodes, info, code, names, date, sell_price, ret, f"{rule_name}前高回撤{drawdown:.2%}", False)
                del holdings[code]
                pending.pop(code, None)
                continue
            info["peak_price"] = max(info["peak_price"], hi)
            holding_days = dates.get_loc(date) - dates.get_loc(info["entry_date"])
            max_profit = info["peak_price"] / info["entry_price"] - 1
            current_profit = cl / info["entry_price"] - 1
            if holding_days > LOW_EFF_MIN_DAYS and max_profit <= LOW_EFF_MAX_PROFIT and current_profit <= LOW_EFF_CURRENT_PROFIT:
                pending[code] = {"reason": "低效持仓卖出"}
            else:
                pending.pop(code, None)

        nav = cash + sum(x["value"] for x in holdings.values())
        nav_values.append(nav)
        position_values.append(sum(x["value"] for x in holdings.values()) / nav if nav else 0)
        prev_date = date

    for code, info in holdings.items():
        cl = close_df.at[dates[-1], code]
        liquidation = info["value"] * (1 - COST)
        ret = liquidation / (info["cost_basis"] * (1 + COST)) - 1
        close_episode(episodes, info, code, names, dates[-1], cl, ret, "期末未平仓（按收盘价估值）", True)

    lot_df = pd.DataFrame(lots)
    episode_df = pd.DataFrame(episodes)
    if not episode_df.empty:
        episode_df["持有交易日"] = episode_df.apply(
            lambda row: dates.get_loc(pd.Timestamp(row["卖出日期"]))
            - dates.get_loc(pd.Timestamp(row["买入日期"])),
            axis=1,
        )
    if not lot_df.empty and not episode_df.empty:
        lot_to_episode = {}
        for episode_id, row in enumerate(episode_df.itertuples()):
            for lot_id in json.loads(row.lot_ids):
                lot_to_episode[lot_id] = (episode_id, row.持仓片段收益, row.是否期末未平仓, row.卖出日期, row.卖出原因)
        lot_df["episode_id"] = lot_df["lot_id"].map(lambda x: lot_to_episode[x][0])
        lot_df["所属持仓片段收益"] = lot_df["lot_id"].map(lambda x: lot_to_episode[x][1])
        lot_df["所属片段是否未平仓"] = lot_df["lot_id"].map(lambda x: lot_to_episode[x][2])
        lot_df["卖出日期"] = lot_df["lot_id"].map(lambda x: lot_to_episode[x][3])
        lot_df["卖出原因"] = lot_df["lot_id"].map(lambda x: lot_to_episode[x][4])
        first_lots = lot_df.sort_values("lot_id").drop_duplicates("episode_id").set_index("episode_id")
        episode_df["首笔信号日期"] = episode_df.index.map(first_lots["信号日期"])
        episode_df["首笔信号日涨跌幅"] = episode_df.index.map(first_lots["信号日涨跌幅"])
        episode_df["首笔信号评分"] = episode_df.index.map(first_lots["信号评分"])
        episode_df["买入批次数"] = episode_df["lot_ids"].map(lambda x: len(json.loads(x)))
    return BacktestResult(
        label, pd.Series(nav_values, index=dates), pd.Series(position_values, index=dates), lot_df, episode_df
    )


def close_episode(episodes, info, code, names, exit_date, exit_price, ret, reason, open_at_end):
    episodes.append({
        "代码": code,
        "名称": names.get(code, code),
        "买入日期": info["entry_date"],
        "卖出日期": exit_date,
        "买入价": info["entry_price"],
        "卖出价": exit_price,
        "持有交易日": np.nan,
        "持仓片段收益": ret,
        "卖出原因": reason,
        "是否期末未平仓": open_at_end,
        "lot_ids": json.dumps(info["lots"]),
    })


def add_forward_returns(lot_df, fields):
    if lot_df.empty:
        return lot_df
    out = lot_df.copy()
    dates = fields["close"].index
    for horizon in [1, 5, 20, 60]:
        values = []
        for row in out.itertuples():
            loc = dates.get_loc(row.买入日期)
            target_loc = loc + horizon - 1
            if target_loc >= len(dates):
                values.append(np.nan)
                continue
            px = fields["close"].at[dates[target_loc], row.代码]
            gross = px / row.买入价 - 1 if pd.notna(px) and row.买入价 > 0 else np.nan
            net = (1 + gross) * (1 - COST) / (1 + COST) - 1 if pd.notna(gross) else np.nan
            values.append(net)
        out[f"买入后{horizon}日净收益"] = values
    return out


def build_signal_events(golden, signal_returns, score, fields, member, names):
    dates = fields["close"].index
    rows = []
    for signal_date in dates[dates >= TRADE_START - pd.Timedelta(days=10)]:
        signal_codes = golden.columns[golden.loc[signal_date]].tolist()
        loc = dates.get_loc(signal_date)
        if loc + 1 >= len(dates):
            continue
        buy_date = dates[loc + 1]
        if buy_date < TRADE_START:
            continue
        for code in signal_codes:
            if not member.at[buy_date, code]:
                continue
            op = fields["open"].at[buy_date, code]
            hi = fields["high"].at[buy_date, code]
            cl = fields["close"].at[buy_date, code]
            prev_close = fields["close"].at[signal_date, code]
            if any(pd.isna(x) or x <= 0 for x in [op, hi, cl, prev_close]):
                continue
            if op >= prev_close * (1 + limit_ratio(code, names.get(code, code))) * 0.999:
                continue
            rows.append({
                "代码": code,
                "名称": names.get(code, code),
                "信号日期": signal_date,
                "买入日期": buy_date,
                "信号日涨跌幅": signal_returns.at[signal_date, code],
                "信号评分": score.at[signal_date, code],
                "买入价": op,
            })
    return add_forward_returns(pd.DataFrame(rows), fields)


def metric_row(values):
    x = pd.Series(values).dropna().astype(float)
    return {
        "样本数": len(x),
        "均值": x.mean() if len(x) else np.nan,
        "中位数": x.median() if len(x) else np.nan,
        "胜率": x.gt(0).mean() if len(x) else np.nan,
        "标准差": x.std() if len(x) > 1 else np.nan,
    }


def two_sided_normal_p(z):
    return math.erfc(abs(float(z)) / math.sqrt(2))


def welch_test_normal_approx(a, b):
    """大样本 Welch t 检验；p 值采用标准正态近似。"""
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    variance = a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b)
    if variance <= 0:
        return np.nan
    return two_sided_normal_p((a.mean() - b.mean()) / math.sqrt(variance))


def mann_whitney_normal_approx(a, b):
    """Mann–Whitney 双侧检验，含并列秩的方差修正。"""
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    combined = pd.Series(np.r_[a, b])
    ranks = combined.rank(method="average").to_numpy()
    n1, n2 = len(a), len(b)
    u1 = ranks[:n1].sum() - n1 * (n1 + 1) / 2
    n = n1 + n2
    tie_counts = combined.value_counts().to_numpy()
    tie_term = np.sum(tie_counts**3 - tie_counts)
    var_u = n1 * n2 / 12 * ((n + 1) - tie_term / (n * (n - 1))) if n > 1 else 0
    if var_u <= 0:
        return np.nan
    z = (u1 - n1 * n2 / 2) / math.sqrt(var_u)
    return two_sided_normal_p(z)


def grouped_summary(df, return_columns):
    work = df.copy()
    work["金叉当日组"] = np.select(
        [work["信号日涨跌幅"] < 0, work["信号日涨跌幅"] == 0],
        ["下跌", "平盘"],
        default="上涨",
    )
    rows = []
    for col in return_columns:
        for group, part in work.groupby("金叉当日组"):
            row = {"收益口径": col, "金叉当日组": group, **metric_row(part[col])}
            rows.append(row)
        down = work.loc[work["金叉当日组"].eq("下跌"), col].dropna()
        up = work.loc[work["金叉当日组"].eq("上涨"), col].dropna()
        if len(down) > 1 and len(up) > 1:
            rows.append({
                "收益口径": col,
                "金叉当日组": "下跌减上涨",
                "样本数": len(down) + len(up),
                "均值": down.mean() - up.mean(),
                "中位数": down.median() - up.median(),
                "胜率": np.nan,
                "标准差": np.nan,
                "Welch_t_p值": welch_test_normal_approx(down, up),
                "MannWhitney_p值": mann_whitney_normal_approx(down, up),
            })
    return pd.DataFrame(rows)


def tag_scope(df, scope):
    out = df.copy()
    out.insert(0, "样本范围", scope)
    return out


def portfolio_metrics(result):
    valid = result.nav.index >= TRADE_START
    nav = result.nav.loc[valid]
    pos = result.positions.loc[valid]
    first = pos.gt(0).idxmax()
    nav = nav.loc[first:]
    ret = nav.pct_change().fillna(0)
    years = len(nav) / 252
    annual = (nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1
    vol = ret.std() * math.sqrt(252)
    sharpe = (ret.mean() - 0.02 / 252) / ret.std() * math.sqrt(252)
    dd = (nav / nav.cummax() - 1).min()
    return {
        "策略组": result.label,
        "期末净值": nav.iloc[-1],
        "年化收益": annual,
        "年化波动": vol,
        "夏普(2%无风险)": sharpe,
        "最大回撤": dd,
        "平均仓位": pos.loc[first:].mean(),
        "买入批次数": len(result.buy_lots),
        "持仓片段数": len(result.episodes),
    }


def annual_summary(lots, return_col):
    work = lots.copy()
    work["年度"] = pd.to_datetime(work["信号日期"]).dt.year
    work["金叉当日组"] = np.select(
        [work["信号日涨跌幅"] < 0, work["信号日涨跌幅"] == 0],
        ["下跌", "平盘"],
        default="上涨",
    )
    rows = []
    for (year, group), part in work.groupby(["年度", "金叉当日组"]):
        rows.append({"年度": year, "金叉当日组": group, **metric_row(part[return_col])})
    return pd.DataFrame(rows)


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    fields, member, names, industry_map, bank_codes = load_inputs()
    golden, buy_signal, signal_returns, score = calculate_signals(fields, member)
    conditions = [
        ("全部合格信号", lambda x: x <= SIGNAL_MAX_RETURN),
        ("仅金叉当日下跌", lambda x: x < 0),
        ("仅金叉当日上涨", lambda x: 0 < x <= SIGNAL_MAX_RETURN),
    ]
    results = []
    for label, condition in conditions:
        result = run_backtest(
            label, condition, fields, member, names, industry_map, bank_codes,
            buy_signal, signal_returns, score,
        )
        result.buy_lots = add_forward_returns(result.buy_lots, fields)
        results.append(result)

    base = results[0]
    signal_events = build_signal_events(golden, signal_returns, score, fields, member, names)
    forward_cols = [f"买入后{h}日净收益" for h in [1, 5, 20, 60]]
    event_stats = tag_scope(grouped_summary(signal_events, forward_cols), "全部可执行金叉信号（不受组合容量影响）")
    lot_stats = tag_scope(grouped_summary(base.buy_lots, forward_cols), "执行版实际买入批次")
    closed_episodes = base.episodes.loc[~base.episodes["是否期末未平仓"]].rename(
        columns={"首笔信号日期": "信号日期", "首笔信号日涨跌幅": "信号日涨跌幅"}
    )
    episode_stats = tag_scope(grouped_summary(closed_episodes, ["持仓片段收益"]), "执行版已平仓持仓片段（按首笔信号分组）")
    group_stats = pd.concat([event_stats, lot_stats, episode_stats], ignore_index=True)
    yearly = annual_summary(closed_episodes, "持仓片段收益")
    portfolio = pd.DataFrame([portfolio_metrics(x) for x in results])

    pd.DataFrame({"日期": base.nav.index, "复现策略净值": base.nav.values}).to_csv(
        OUTPUT_DIR / "复现策略净值.csv", index=False, encoding="utf-8-sig"
    )
    signal_events.to_csv(OUTPUT_DIR / "全部可执行金叉信号.csv", index=False, encoding="utf-8-sig")
    base.buy_lots.to_csv(OUTPUT_DIR / "逐次买入明细.csv", index=False, encoding="utf-8-sig")
    base.episodes.to_csv(OUTPUT_DIR / "持仓片段明细.csv", index=False, encoding="utf-8-sig")
    group_stats.to_csv(OUTPUT_DIR / "分组收益统计.csv", index=False, encoding="utf-8-sig")
    yearly.to_csv(OUTPUT_DIR / "年度分组统计.csv", index=False, encoding="utf-8-sig")
    portfolio.to_csv(OUTPUT_DIR / "组合反事实对比.csv", index=False, encoding="utf-8-sig")

    report = {
        "data_end": str(fields["close"].index.max().date()),
        "signal_count": int(golden.loc[golden.index >= TRADE_START].sum().sum()),
        "executable_signal_count": len(signal_events),
        "portfolio": portfolio.to_dict(orient="records"),
        "group_stats": group_stats.to_dict(orient="records"),
        "yearly": yearly.to_dict(orient="records"),
    }
    (OUTPUT_DIR / "分析结果.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(portfolio.to_string(index=False))
    print("\n分组统计：")
    print(group_stats.to_string(index=False))
    print("\n年度统计：")
    print(yearly.to_string(index=False))
    print(f"\n输出目录：{OUTPUT_DIR}")


if __name__ == "__main__":
    main()
