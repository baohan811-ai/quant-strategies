import os
import sqlite3
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_ROOT = os.path.dirname(SCRIPT_DIR)
BASE_DIR = os.path.dirname(SCRIPT_ROOT)
OUTPUT_DIR = os.path.join(BASE_DIR, "输出")
if SCRIPT_ROOT not in sys.path:
    sys.path.insert(0, SCRIPT_ROOT)

from 维护工具.local_market_db import MARKET_DB_PATH


CACHE_PREFIX = "中证800"
TRADE_START_DATE = "2023-04-01"
LOOKBACK_DAYS = 1600
MAX_HOLDINGS = 20
INITIAL_WEIGHT = 1 / MAX_HOLDINGS
TRANSACTION_COST_RATE = 0.0025
PEAK_RETRACE_SELL_DRAWDOWN = 0.1175
LOW_EFFICIENCY_MIN_HOLDING_DAYS = 60
LOW_EFFICIENCY_MAX_PROFIT_THRESHOLD = 0.05
LOW_EFFICIENCY_CURRENT_PROFIT_THRESHOLD = 0.00
SIGNAL_MAX_DAILY_RETURN = 0.065

MA5_MA60_GAP_WEIGHT = 1.0
MA60_5D_TREND_WEIGHT = 1.0
VOLUME_RATIO_SCORE_WEIGHT = 0.25
VOLUME_RATIO_LOOKBACK = 20
VOLUME_RATIO_MAX_BONUS_BASE = 1.0


def sql_df(query, params=None):
    with sqlite3.connect(MARKET_DB_PATH) as conn:
        return pd.read_sql_query(query, conn, params=params or [])


def latest_complete_price_date():
    row = sql_df(
        """
        SELECT MAX(trade_date) AS max_date
        FROM daily_prices
        WHERE adjusted = 'F'
          AND open IS NOT NULL
          AND high IS NOT NULL
          AND low IS NOT NULL
          AND close IS NOT NULL
          AND volume IS NOT NULL
          AND amt IS NOT NULL
        """
    )
    if row.empty or pd.isna(row.at[0, "max_date"]):
        raise RuntimeError("本地行情库没有完整日线日期")
    return row.at[0, "max_date"]


def load_snapshots(start_date, end_date):
    snapshots = sql_df(
        """
        SELECT snapshot_date, wind_code, sec_name
        FROM universe_constituents_snapshot
        WHERE universe_name = ?
          AND snapshot_date <= ?
          AND snapshot_date >= (
              SELECT COALESCE(MAX(snapshot_date), ?)
              FROM universe_constituents_snapshot
              WHERE universe_name = ?
                AND snapshot_date < ?
          )
        ORDER BY snapshot_date, wind_code
        """,
        [CACHE_PREFIX, end_date, start_date, CACHE_PREFIX, start_date],
    )
    if snapshots.empty:
        raise RuntimeError(f"没有找到 {CACHE_PREFIX} 历史成分快照")
    snapshots["snapshot_date"] = pd.to_datetime(snapshots["snapshot_date"])
    return snapshots


def load_metadata(codes):
    placeholders = ",".join("?" for _ in codes)
    names = sql_df(
        f"""
        SELECT wind_code, sec_name
        FROM stock_universe
        WHERE wind_code IN ({placeholders})
        """,
        codes,
    )
    industries = sql_df(
        f"""
        SELECT wind_code, industry_level1
        FROM stock_industry
        WHERE classification_system = 'wind_level1'
          AND wind_code IN ({placeholders})
        """,
        codes,
    )
    meta = pd.DataFrame({"wind_code": codes})
    meta = meta.merge(names, on="wind_code", how="left")
    meta = meta.merge(industries, on="wind_code", how="left")
    meta = meta.drop_duplicates("wind_code", keep="last")
    meta["sec_name"] = meta["sec_name"].fillna(meta["wind_code"])
    meta["industry_level1"] = meta["industry_level1"].fillna("未分类")
    return meta.set_index("wind_code")


def load_price_panel(codes, start_date, end_date):
    placeholders = ",".join("?" for _ in codes)
    fields = ["open", "high", "low", "close", "volume", "amt"]
    raw = sql_df(
        f"""
        SELECT trade_date, wind_code, {", ".join(fields)}
        FROM daily_prices
        WHERE adjusted = 'F'
          AND trade_date >= ?
          AND trade_date <= ?
          AND wind_code IN ({placeholders})
        ORDER BY trade_date, wind_code
        """,
        [start_date, end_date, *codes],
    )
    if raw.empty:
        raise RuntimeError("没有读到本地行情")
    raw["trade_date"] = pd.to_datetime(raw["trade_date"])
    panel = {}
    for field in fields:
        df = raw.pivot(index="trade_date", columns="wind_code", values=field).sort_index()
        df = df.reindex(columns=codes)
        allow_zero = field in {"volume", "amt"}
        df = df.apply(pd.to_numeric, errors="coerce")
        df = df.where(df >= 0 if allow_zero else df > 0, np.nan)
        panel[field] = df
    return panel


def build_member_matrix(snapshots, dates, codes):
    member = pd.DataFrame(False, index=dates, columns=codes)
    snapshot_dates = snapshots["snapshot_date"].drop_duplicates().sort_values().tolist()
    for index, snapshot_date in enumerate(snapshot_dates):
        next_date = snapshot_dates[index + 1] if index + 1 < len(snapshot_dates) else None
        if next_date is None:
            active_dates = dates[dates >= snapshot_date]
        else:
            active_dates = dates[(dates >= snapshot_date) & (dates < next_date)]
        active_codes = snapshots.loc[snapshots["snapshot_date"] == snapshot_date, "wind_code"].tolist()
        active_codes = [code for code in active_codes if code in member.columns]
        if len(active_dates) and active_codes:
            member.loc[active_dates, active_codes] = True
    return member


def score_details(ma5, ma60, volume):
    ma5_ma60_gap_score = MA5_MA60_GAP_WEIGHT * (ma5 / ma60 - 1)
    ma60_5d_trend_score = MA60_5D_TREND_WEIGHT * (ma60 / ma60.shift(5) - 1)
    volume_ma = volume.rolling(VOLUME_RATIO_LOOKBACK).mean()
    volume_ratio = volume / volume_ma.replace(0, np.nan)
    volume_ratio_score = VOLUME_RATIO_SCORE_WEIGHT * (
        (volume_ratio - 1).clip(lower=0, upper=VOLUME_RATIO_MAX_BONUS_BASE)
    )
    return ma5_ma60_gap_score + ma60_5d_trend_score + volume_ratio_score


def get_limit_ratio(code, name):
    name = str(name).upper()
    if "ST" in name:
        return 0.05
    if code.endswith(".BJ"):
        return 0.30
    if code.startswith("300") or code.startswith("688"):
        return 0.20
    return 0.10


def is_delisted(code, code_to_name):
    return "退市" in str(code_to_name.get(code, ""))


def evaluate_sell(holding_info, open_price, low_price):
    entry_price = holding_info["entry_price"]
    prev_peak_price = holding_info["peak_price"]
    max_profit = prev_peak_price / entry_price - 1 if entry_price > 0 else -np.inf
    if prev_peak_price <= 0:
        return np.nan, "", max_profit, prev_peak_price
    retrace_price = prev_peak_price * (1 - PEAK_RETRACE_SELL_DRAWDOWN)
    if low_price <= retrace_price:
        sell_price = open_price if open_price < retrace_price else retrace_price
        return sell_price, f"前高回撤{PEAK_RETRACE_SELL_DRAWDOWN:.2%}卖出", max_profit, prev_peak_price
    return np.nan, "", max_profit, prev_peak_price


def run_backtest(variant_name, excluded_mask, panel, member, meta):
    open_df = panel["open"]
    high_df = panel["high"]
    low_df = panel["low"]
    close_df = panel["close"]
    volume_df = panel["volume"]

    code_to_name = meta["sec_name"].to_dict()
    dates = close_df.index
    trade_start_ts = pd.Timestamp(TRADE_START_DATE)

    ma5 = close_df.rolling(5).mean()
    ma60 = close_df.rolling(60).mean()
    ma120 = close_df.rolling(120).mean()
    spread = ma5 - ma60
    signal = np.sign(spread)
    daily_ret = close_df.pct_change()
    ma60_up = ma60 > ma60.shift(1)
    ma120_up = ma120 > ma120.shift(1)
    limit_gain = daily_ret <= SIGNAL_MAX_DAILY_RETURN
    candidate_score = score_details(ma5, ma60, volume_df)

    allowed_codes = pd.Series(~excluded_mask, index=close_df.columns)
    allowed_member = member & allowed_codes.reindex(member.columns).fillna(False)
    golden_signal = (signal.diff() == 2) & ma60_up & ma120_up & limit_gain & allowed_member
    buy_signal = golden_signal.shift(1).fillna(False).astype(bool)

    position = pd.DataFrame(0.0, index=dates, columns=close_df.columns)
    current_holdings = {}
    pending_open_sell_signals = {}
    cash = 1.0
    portfolio_values = []
    turnover_records = []
    closed_trade_returns = []
    closed_trade_outcomes = []
    closed_trade_reasons = []
    closed_trade_holding_days = []
    last_valid_close_date = close_df.apply(lambda series: series.dropna().index.max())
    prev_date = None

    for date in dates:
        traded_amount = 0.0
        sold_today = set()

        if prev_date is not None:
            for code in list(current_holdings.keys()):
                prev_price = close_df.at[prev_date, code]
                price = close_df.at[date, code]
                last_price_date = last_valid_close_date.get(code)
                if is_delisted(code, code_to_name) and pd.isna(price) and pd.notna(last_price_date) and date > last_price_date:
                    holding_info = current_holdings[code]
                    entry_date = holding_info["entry_date"]
                    holding_days = dates.get_loc(date) - dates.get_loc(entry_date)
                    closed_trade_returns.append(-1.0)
                    closed_trade_outcomes.append("loss")
                    closed_trade_reasons.append("退市归零")
                    closed_trade_holding_days.append(holding_days)
                    sold_today.add(code)
                    del current_holdings[code]
                    pending_open_sell_signals.pop(code, None)
                    continue
                if pd.isna(prev_price) or pd.isna(price) or prev_price <= 0:
                    continue
                current_holdings[code]["value"] *= price / prev_price

        for code, signal_info in list(pending_open_sell_signals.items()):
            if code not in current_holdings:
                del pending_open_sell_signals[code]
                continue
            open_price = open_df.at[date, code]
            close_price = close_df.at[date, code]
            if pd.isna(open_price) or pd.isna(close_price) or open_price <= 0 or close_price <= 0:
                continue
            holding_info = current_holdings[code]
            sell_value = holding_info["value"] * (open_price / close_price)
            buy_cost = holding_info["cost_basis"] * (1 + TRANSACTION_COST_RATE)
            sell_proceeds = sell_value * (1 - TRANSACTION_COST_RATE)
            trade_return = sell_proceeds / buy_cost - 1 if buy_cost > 0 else 0
            holding_days = dates.get_loc(date) - dates.get_loc(holding_info["entry_date"])
            closed_trade_returns.append(trade_return)
            closed_trade_outcomes.append("win" if trade_return > 0 else "loss")
            closed_trade_reasons.append(signal_info["reason"])
            closed_trade_holding_days.append(holding_days)
            cash += sell_proceeds
            traded_amount += sell_value
            sold_today.add(code)
            del current_holdings[code]
            del pending_open_sell_signals[code]

        portfolio_before_buy = cash + sum(info["value"] for info in current_holdings.values())
        available_slots = MAX_HOLDINGS - len(current_holdings)
        if date >= trade_start_ts and cash > 0 and prev_date is not None:
            buy_candidates = buy_signal.columns[buy_signal.loc[date]].tolist()
            buy_candidates = [
                code for code in buy_candidates
                if code not in sold_today and (code in current_holdings or available_slots > 0)
            ]
            if buy_candidates:
                score_prev = candidate_score.loc[prev_date, buy_candidates].dropna().sort_values(ascending=False)
                opened_new_positions = 0
                max_candidates = len([code for code in buy_candidates if code in current_holdings]) + available_slots
                for code in score_prev.head(max_candidates).index:
                    is_existing = code in current_holdings
                    if not is_existing and opened_new_positions >= available_slots:
                        continue
                    open_price = open_df.at[date, code]
                    high_price = high_df.at[date, code]
                    close_price = close_df.at[date, code]
                    prev_close = close_df.at[prev_date, code]
                    if pd.isna(open_price) or pd.isna(high_price) or pd.isna(close_price) or open_price <= 0 or high_price <= 0 or close_price <= 0:
                        continue
                    if pd.notna(prev_close) and prev_close > 0:
                        limit_up_price = prev_close * (1 + get_limit_ratio(code, code_to_name.get(code, code)))
                        if open_price >= limit_up_price * 0.999:
                            continue
                    target_value = portfolio_before_buy * INITIAL_WEIGHT
                    max_affordable = cash / (1 + TRANSACTION_COST_RATE)
                    buy_value = min(target_value, max_affordable)
                    if buy_value <= 0:
                        break
                    cash -= buy_value * (1 + TRANSACTION_COST_RATE)
                    traded_amount += buy_value
                    added_value_at_close = buy_value * (close_price / open_price)
                    if is_existing:
                        holding_info = current_holdings[code]
                        old_entry_price = holding_info["entry_price"]
                        old_cost_basis = holding_info["cost_basis"]
                        old_share_proxy = old_cost_basis / old_entry_price if old_entry_price > 0 else 0
                        added_share_proxy = buy_value / open_price
                        new_share_proxy = old_share_proxy + added_share_proxy
                        holding_info["entry_price"] = (
                            (old_cost_basis + buy_value) / new_share_proxy
                            if new_share_proxy > 0 else open_price
                        )
                        holding_info["peak_price"] = max(holding_info["peak_price"], high_price)
                        holding_info["value"] += added_value_at_close
                        holding_info["cost_basis"] += buy_value
                    else:
                        opened_new_positions += 1
                        current_holdings[code] = {
                            "entry_price": open_price,
                            "entry_date": date,
                            "peak_price": high_price,
                            "value": added_value_at_close,
                            "cost_basis": buy_value,
                        }

        for code in list(current_holdings.keys()):
            open_price = open_df.at[date, code]
            low_price = low_df.at[date, code]
            high_price = high_df.at[date, code]
            close_price = close_df.at[date, code]
            prev_close = close_df.at[prev_date, code] if prev_date is not None else np.nan
            if pd.isna(open_price) or pd.isna(low_price) or pd.isna(high_price) or pd.isna(close_price):
                continue
            holding_info = current_holdings[code]
            entry_date = holding_info["entry_date"]
            if date <= entry_date:
                continue

            sell_price, sell_reason, _, prev_peak = evaluate_sell(holding_info, open_price, low_price)
            limit_down_blocked = False
            if pd.notna(sell_price) and pd.notna(prev_close) and prev_close > 0:
                limit_down_price = prev_close * (1 - get_limit_ratio(code, code_to_name.get(code, code)))
                if close_price <= limit_down_price * 1.001:
                    limit_down_blocked = True
            if pd.notna(sell_price) and not limit_down_blocked:
                sell_value = holding_info["value"] * (sell_price / close_price)
                buy_cost = holding_info["cost_basis"] * (1 + TRANSACTION_COST_RATE)
                sell_proceeds = sell_value * (1 - TRANSACTION_COST_RATE)
                trade_return = sell_proceeds / buy_cost - 1 if buy_cost > 0 else 0
                holding_days = dates.get_loc(date) - dates.get_loc(entry_date)
                closed_trade_returns.append(trade_return)
                closed_trade_outcomes.append("win" if trade_return > 0 else "loss")
                closed_trade_reasons.append(sell_reason)
                closed_trade_holding_days.append(holding_days)
                cash += sell_proceeds
                traded_amount += sell_value
                sold_today.add(code)
                del current_holdings[code]
                pending_open_sell_signals.pop(code, None)
                continue

            updated_peak_price = max(prev_peak, high_price)
            current_holdings[code]["peak_price"] = updated_peak_price
            holding_days = dates.get_loc(date) - dates.get_loc(entry_date)
            max_profit = updated_peak_price / holding_info["entry_price"] - 1 if holding_info["entry_price"] > 0 else -np.inf
            current_profit = close_price / holding_info["entry_price"] - 1 if holding_info["entry_price"] > 0 else np.nan
            if (
                holding_days > LOW_EFFICIENCY_MIN_HOLDING_DAYS
                and max_profit <= LOW_EFFICIENCY_MAX_PROFIT_THRESHOLD
                and current_profit <= LOW_EFFICIENCY_CURRENT_PROFIT_THRESHOLD
            ):
                pending_open_sell_signals[code] = {
                    "signal_date": date,
                    "reason": "低效持仓卖出",
                }
            else:
                pending_open_sell_signals.pop(code, None)

        portfolio_value = cash + sum(info["value"] for info in current_holdings.values())
        portfolio_values.append(portfolio_value)
        turnover_records.append(traded_amount / portfolio_value if portfolio_value > 0 else 0.0)
        for code, info in current_holdings.items():
            position.at[date, code] = info["value"] / portfolio_value if portfolio_value > 0 else 0.0
        prev_date = date

    nav = pd.Series(portfolio_values, index=dates, name=variant_name)
    ret = nav.pct_change().fillna(0)
    turnover = pd.Series(turnover_records, index=dates)
    analysis_dates = nav.index[nav.index >= trade_start_ts]
    nav_analysis = nav.loc[analysis_dates[0]:]
    ret_analysis = ret.loc[analysis_dates[0]:]
    position_analysis = position.loc[analysis_dates[0]:]
    turnover_analysis = turnover.loc[analysis_dates[0]:]

    has_position = position_analysis.sum(axis=1).gt(0)
    if has_position.any():
        first_trade_date = has_position.idxmax()
        nav_active = nav_analysis.loc[first_trade_date:]
        ret_active = ret_analysis.loc[first_trade_date:]
        position_active = position_analysis.loc[first_trade_date:]
        turnover_active = turnover_analysis.loc[first_trade_date:]
        annual_ret = nav_active.iloc[-1] ** (252 / len(nav_active)) - 1
        annual_vol = ret_active.std() * np.sqrt(252)
        sharpe = ret_active.mean() / ret_active.std() * np.sqrt(252) if ret_active.std() != 0 else 0
        drawdown = nav_active / nav_active.cummax() - 1
        max_dd = drawdown.min()
        avg_holding = (position_active > 0).sum(axis=1).mean()
        annual_turnover = turnover_active.mean() * 252
        day_win_rate = (ret_active > 0).mean()
    else:
        first_trade_date = pd.NaT
        annual_ret = annual_vol = sharpe = max_dd = avg_holding = annual_turnover = day_win_rate = 0

    trade_count = len(closed_trade_returns)
    trade_win_rate = (
        (pd.Series(closed_trade_outcomes) == "win").mean()
        if trade_count else 0
    )
    yearly_ret = nav_analysis.resample("YE").last().pct_change()
    if not nav_analysis.empty:
        first_year = nav_analysis.index[0].year
        first_year_ret = nav_analysis[nav_analysis.index.year == first_year].iloc[-1] / nav_analysis.iloc[0] - 1
        yearly_ret.loc[pd.Timestamp(f"{first_year}-12-31")] = first_year_ret
        yearly_ret = yearly_ret.sort_index()

    metrics = {
        "方案": variant_name,
        "剔除股票数": int(excluded_mask.sum()),
        "最终累计收益": nav_analysis.iloc[-1] / nav_analysis.iloc[0] - 1,
        "年化收益": annual_ret,
        "年化波动": annual_vol,
        "夏普比率": sharpe,
        "最大回撤": max_dd,
        "平均持仓数": avg_holding,
        "年化换手率": annual_turnover,
        "日胜率": day_win_rate,
        "平仓笔数": trade_count,
        "单笔胜率": trade_win_rate,
        "首次建仓日": first_trade_date,
    }
    return metrics, nav_analysis, yearly_ret


def build_exclusion_masks(meta):
    industry = meta["industry_level1"].fillna("")
    name = meta["sec_name"].fillna("")
    return {
        "原策略": pd.Series(False, index=meta.index),
        "剔除日常消费": industry.eq("日常消费"),
        "剔除日常消费+医疗保健": industry.isin(["日常消费", "医疗保健"]),
        "剔除银行+房地产": name.str.contains("银行", na=False) | industry.eq("房地产"),
    }


def main():
    end_date = latest_complete_price_date()
    trade_start = pd.Timestamp(TRADE_START_DATE)
    data_start = (trade_start - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    snapshots = load_snapshots(TRADE_START_DATE, end_date)
    codes = sorted(snapshots["wind_code"].dropna().unique().tolist())
    meta = load_metadata(codes)
    panel = load_price_panel(codes, data_start, end_date)
    member = build_member_matrix(snapshots, panel["close"].index, codes)

    metrics_rows = []
    navs = []
    yearly_rows = []
    for variant_name, excluded_mask in build_exclusion_masks(meta).items():
        metrics, nav, yearly_ret = run_backtest(variant_name, excluded_mask, panel, member, meta)
        metrics_rows.append(metrics)
        navs.append(nav.rename(variant_name))
        for date, value in yearly_ret.items():
            yearly_rows.append({"方案": variant_name, "年份": date.year, "收益": value})
        print(
            f"{variant_name}: 年化={metrics['年化收益']:.2%}, "
            f"夏普={metrics['夏普比率']:.3f}, 回撤={metrics['最大回撤']:.2%}, "
            f"累计={metrics['最终累计收益']:.2%}"
        )

    metrics_df = pd.DataFrame(metrics_rows)
    nav_df = pd.concat(navs, axis=1)
    yearly_df = pd.DataFrame(yearly_rows)
    exclusions = []
    for variant_name, excluded_mask in build_exclusion_masks(meta).items():
        excluded = meta.loc[excluded_mask, ["sec_name", "industry_level1"]].reset_index()
        excluded.insert(0, "方案", variant_name)
        exclusions.append(excluded)
    exclusions_df = pd.concat(exclusions, ignore_index=True)

    report_file = os.path.join(OUTPUT_DIR, f"金叉策略行业剔除对比_{end_date}.xlsx")
    run_info = pd.DataFrame([
        {"项目": "生成时间", "值": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
        {"项目": "行情截止", "值": end_date},
        {"项目": "交易开始", "值": TRADE_START_DATE},
        {"项目": "数据来源", "值": "本地SQLite，不调用Wind"},
        {"项目": "银行定义", "值": "股票名称包含“银行”"},
        {"项目": "房地产定义", "值": "Wind一级行业=房地产"},
    ])
    with pd.ExcelWriter(report_file, engine="openpyxl") as writer:
        run_info.to_excel(writer, sheet_name="说明", index=False)
        metrics_df.to_excel(writer, sheet_name="指标对比", index=False)
        yearly_df.to_excel(writer, sheet_name="年度收益", index=False)
        nav_df.to_excel(writer, sheet_name="净值曲线")
        exclusions_df.to_excel(writer, sheet_name="剔除股票清单", index=False)

    print(f"报告已生成：{report_file}")


if __name__ == "__main__":
    main()
