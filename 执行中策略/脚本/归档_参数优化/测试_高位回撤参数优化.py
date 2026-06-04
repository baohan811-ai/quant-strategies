import os
import sqlite3
import sys
from datetime import timedelta

import numpy as np
import pandas as pd


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(SCRIPT_DIR)
BASE_DIR = os.path.dirname(SCRIPTS_DIR)
sys.path.insert(0, SCRIPTS_DIR)

from 维护工具.local_market_db import MARKET_DB_PATH, load_price_matrix

OUTPUT_DIR = os.path.join(BASE_DIR, "输出")
os.makedirs(OUTPUT_DIR, exist_ok=True)

CACHE_PREFIX = "中证800"
TRADE_START_DATE = "2022-04-03"
LOOKBACK_DAYS = 1600

MAX_HOLDINGS = 20
INITIAL_WEIGHT = 1 / MAX_HOLDINGS
TRANSACTION_COST_RATE = 0.0025
LOW_EFFICIENCY_MIN_HOLDING_DAYS = 60
LOW_EFFICIENCY_MAX_PROFIT_THRESHOLD = 0.05
LOW_EFFICIENCY_CURRENT_PROFIT_THRESHOLD = 0.00

MA5_MA60_GAP_WEIGHT = 1.0
MA60_5D_TREND_WEIGHT = 1.0
VOLUME_RATIO_SCORE_WEIGHT = 0.25
VOLUME_RATIO_LOOKBACK = 20
VOLUME_RATIO_MAX_BONUS_BASE = 1.0
SIGNAL_MAX_DAILY_RETURN = 0.065
SHORT_MA_DAYS = 5
MID_MA_DAYS = 60
LONG_MA_DAYS = 120
MID_MA_TREND_LOOKBACK = 5

PEAK_RETRACE_GRID = sorted(
    set([value / 100 for value in range(5, 21)])
    | set([value / 10000 for value in range(1100, 1301, 25)])
)


def load_snapshots():
    with sqlite3.connect(MARKET_DB_PATH) as conn:
        snapshots = pd.read_sql_query(
            """
            SELECT snapshot_date, wind_code, sec_name
            FROM universe_constituents_snapshot
            WHERE universe_name = ?
              AND snapshot_date <= (
                  SELECT MAX(trade_date)
                  FROM daily_prices
                  WHERE adjusted = 'F'
              )
            ORDER BY snapshot_date, wind_code
            """,
            conn,
            params=[CACHE_PREFIX],
        )
    if snapshots.empty:
        raise RuntimeError("本地数据库没有中证800历史成分快照。")
    snapshots["snapshot_date"] = pd.to_datetime(snapshots["snapshot_date"])
    return snapshots


def build_daily_universe_member_matrix(snapshots, trade_dates, codes):
    member = pd.DataFrame(False, index=trade_dates, columns=codes)
    snapshot_dates = snapshots["snapshot_date"].drop_duplicates().tolist()
    for index, snapshot_date in enumerate(snapshot_dates):
        next_snapshot_date = snapshot_dates[index + 1] if index + 1 < len(snapshot_dates) else None
        if next_snapshot_date is None:
            active_dates = trade_dates[trade_dates >= snapshot_date]
        else:
            active_dates = trade_dates[(trade_dates >= snapshot_date) & (trade_dates < next_snapshot_date)]
        if len(active_dates) == 0:
            continue
        active_codes = snapshots.loc[
            snapshots["snapshot_date"] == snapshot_date, "wind_code"
        ].tolist()
        active_codes = [code for code in active_codes if code in member.columns]
        member.loc[active_dates, active_codes] = True
    return member


def get_limit_ratio(code, stock_name):
    stock_name = str(stock_name).upper()
    if "ST" in stock_name:
        return 0.05
    if code.endswith(".BJ"):
        return 0.30
    if code.startswith("300") or code.startswith("688"):
        return 0.20
    return 0.10


def load_inputs():
    snapshots = load_snapshots()
    codes = snapshots["wind_code"].drop_duplicates().tolist()
    code_to_name = (
        snapshots.dropna(subset=["sec_name"])
        .drop_duplicates("wind_code", keep="last")
        .set_index("wind_code")["sec_name"]
        .to_dict()
    )
    start_date = (
        pd.Timestamp(TRADE_START_DATE) - timedelta(days=LOOKBACK_DAYS)
    ).strftime("%Y-%m-%d")
    with sqlite3.connect(MARKET_DB_PATH) as conn:
        end_date = conn.execute(
            """
            SELECT MAX(trade_date)
            FROM daily_prices
            WHERE adjusted = 'F'
              AND open IS NOT NULL
              AND high IS NOT NULL
              AND low IS NOT NULL
              AND close IS NOT NULL
              AND volume IS NOT NULL
              AND amt IS NOT NULL
            """
        ).fetchone()[0]

    fields = {}
    for field in ["open", "high", "low", "close", "volume"]:
        print(f"读取本地行情：{field}")
        fields[field] = load_price_matrix(
            CACHE_PREFIX,
            field,
            codes=codes,
            start_date=start_date,
            end_date=end_date,
            target_columns=codes,
            prefer_sqlite=True,
            fallback_pickle=False,
        )

    common_index = fields["close"].index
    for field in fields:
        fields[field] = fields[field].reindex(index=common_index, columns=codes)
    universe_member = build_daily_universe_member_matrix(snapshots, common_index, codes)
    print(f"行情区间：{common_index[0]:%Y-%m-%d} ~ {common_index[-1]:%Y-%m-%d}")
    print(f"历史并集股票数量：{len(codes)}")
    return fields, universe_member, code_to_name


def build_signals(close_df, volume_df, universe_member):
    ma5 = close_df.rolling(SHORT_MA_DAYS).mean()
    ma60 = close_df.rolling(MID_MA_DAYS).mean()
    ma120 = close_df.rolling(LONG_MA_DAYS).mean()
    volume_ma = volume_df.rolling(VOLUME_RATIO_LOOKBACK).mean()
    volume_ratio = volume_df / volume_ma.replace(0, np.nan)
    candidate_score = (
        MA5_MA60_GAP_WEIGHT * (ma5 / ma60 - 1)
        + MA60_5D_TREND_WEIGHT * (ma60 / ma60.shift(MID_MA_TREND_LOOKBACK) - 1)
        + VOLUME_RATIO_SCORE_WEIGHT
        * (volume_ratio - 1).clip(lower=0, upper=VOLUME_RATIO_MAX_BONUS_BASE)
    )
    signal = np.sign(ma5 - ma60)
    golden_cross_raw = signal.diff() == 2
    golden_cross_candidate = golden_cross_raw & (ma60 > ma60.shift(1)) & (ma120 > ma120.shift(1))
    golden_signal = golden_cross_candidate & (close_df.pct_change() <= SIGNAL_MAX_DAILY_RETURN) & universe_member
    return golden_signal.shift(1).fillna(False).astype(bool), candidate_score


def calculate_metrics(nav, turnover, holding_count, closed_trade_returns, start_date):
    start_ts = pd.Timestamp(start_date)
    nav = nav.loc[nav.index >= start_ts]
    turnover = turnover.loc[turnover.index >= start_ts]
    holding_count = holding_count.loc[holding_count.index >= start_ts]
    if nav.empty:
        return {}
    nav = nav / nav.iloc[0]
    strategy_ret = nav.pct_change().fillna(0)
    annual_return = nav.iloc[-1] ** (252 / len(nav)) - 1
    annual_volatility = strategy_ret.std() * np.sqrt(252)
    sharpe = (
        strategy_ret.mean() / strategy_ret.std() * np.sqrt(252)
        if annual_volatility else 0
    )
    max_drawdown = (nav / nav.cummax() - 1).min()
    return {
        "累计收益": nav.iloc[-1] - 1,
        "年化收益": annual_return,
        "年化波动": annual_volatility,
        "夏普比率": sharpe,
        "最大回撤": max_drawdown,
        "平均持仓数": holding_count.mean(),
        "年化换手率": turnover.mean() * 252,
        "平仓笔数": len(closed_trade_returns),
        "平均单笔收益": np.mean(closed_trade_returns) if closed_trade_returns else 0,
        "单笔胜率": np.mean(np.array(closed_trade_returns) > 0) if closed_trade_returns else 0,
    }


def run_backtest(retrace_drawdown, fields, buy_signal, candidate_score, code_to_name):
    open_df = fields["open"]
    high_df = fields["high"]
    low_df = fields["low"]
    close_df = fields["close"]
    trade_start_ts = pd.Timestamp(TRADE_START_DATE)
    holdings = {}
    pending_open_sell_signals = {}
    cash = 1.0
    portfolio_values = []
    turnover_records = []
    holding_count_records = []
    closed_trade_returns = []
    prev_date = None

    for date in close_df.index:
        traded_amount = 0.0
        sold_today = set()
        if prev_date is not None:
            for code in list(holdings):
                prev_price = close_df.at[prev_date, code]
                close_price = close_df.at[date, code]
                if pd.notna(prev_price) and pd.notna(close_price) and prev_price > 0:
                    holdings[code]["value"] *= close_price / prev_price

        for code in list(pending_open_sell_signals):
            if code not in holdings:
                del pending_open_sell_signals[code]
                continue
            open_price = open_df.at[date, code]
            close_price = close_df.at[date, code]
            if pd.isna(open_price) or pd.isna(close_price) or open_price <= 0 or close_price <= 0:
                continue
            info = holdings[code]
            sell_value = info["value"] * open_price / close_price
            sell_proceeds = sell_value * (1 - TRANSACTION_COST_RATE)
            buy_cost = info["cost_basis"] * (1 + TRANSACTION_COST_RATE)
            closed_trade_returns.append(sell_proceeds / buy_cost - 1 if buy_cost > 0 else 0)
            cash += sell_proceeds
            traded_amount += sell_value
            sold_today.add(code)
            del holdings[code]
            del pending_open_sell_signals[code]

        portfolio_before_buy = cash + sum(info["value"] for info in holdings.values())
        available_slots = MAX_HOLDINGS - len(holdings)
        if date >= trade_start_ts and cash > 0 and prev_date is not None:
            buy_candidates = buy_signal.columns[buy_signal.loc[date]].tolist()
            buy_candidates = [
                code for code in buy_candidates
                if code not in sold_today and (code in holdings or available_slots > 0)
            ]
            if buy_candidates:
                score_prev = candidate_score.loc[prev_date, buy_candidates].dropna().sort_values(ascending=False)
                opened_new_positions = 0
                existing_count = len([code for code in buy_candidates if code in holdings])
                for code in score_prev.head(existing_count + available_slots).index:
                    is_existing = code in holdings
                    if not is_existing and opened_new_positions >= available_slots:
                        continue
                    open_price = open_df.at[date, code]
                    high_price = high_df.at[date, code]
                    close_price = close_df.at[date, code]
                    prev_close = close_df.at[prev_date, code]
                    if any(pd.isna(value) or value <= 0 for value in [open_price, high_price, close_price]):
                        continue
                    if pd.notna(prev_close) and prev_close > 0:
                        limit_up_price = prev_close * (1 + get_limit_ratio(code, code_to_name.get(code, code)))
                        if open_price >= limit_up_price * 0.999:
                            continue
                    buy_value = min(portfolio_before_buy * INITIAL_WEIGHT, cash / (1 + TRANSACTION_COST_RATE))
                    if buy_value <= 0:
                        break
                    cash -= buy_value * (1 + TRANSACTION_COST_RATE)
                    traded_amount += buy_value
                    added_value_at_close = buy_value * close_price / open_price
                    if is_existing:
                        info = holdings[code]
                        old_share_proxy = info["cost_basis"] / info["entry_price"] if info["entry_price"] > 0 else 0
                        added_share_proxy = buy_value / open_price
                        new_share_proxy = old_share_proxy + added_share_proxy
                        info["entry_price"] = (info["cost_basis"] + buy_value) / new_share_proxy
                        info["peak_price"] = max(info["peak_price"], high_price)
                        info["value"] += added_value_at_close
                        info["cost_basis"] += buy_value
                    else:
                        opened_new_positions += 1
                        holdings[code] = {
                            "entry_price": open_price,
                            "entry_date": date,
                            "peak_price": high_price,
                            "value": added_value_at_close,
                            "cost_basis": buy_value,
                        }

        for code in list(holdings):
            open_price = open_df.at[date, code]
            low_price = low_df.at[date, code]
            high_price = high_df.at[date, code]
            close_price = close_df.at[date, code]
            prev_close = close_df.at[prev_date, code] if prev_date is not None else np.nan
            if any(pd.isna(value) for value in [open_price, low_price, high_price, close_price]):
                continue
            info = holdings[code]
            if date <= info["entry_date"]:
                continue
            retrace_price = info["peak_price"] * (1 - retrace_drawdown)
            trigger_price = np.nan
            if low_price <= retrace_price:
                trigger_price = open_price if open_price < retrace_price else retrace_price
            if pd.notna(trigger_price) and pd.notna(prev_close) and prev_close > 0:
                limit_down_price = prev_close * (1 - get_limit_ratio(code, code_to_name.get(code, code)))
                if close_price <= limit_down_price * 1.001:
                    trigger_price = np.nan
            if pd.notna(trigger_price):
                sell_value = info["value"] * trigger_price / close_price
                sell_proceeds = sell_value * (1 - TRANSACTION_COST_RATE)
                buy_cost = info["cost_basis"] * (1 + TRANSACTION_COST_RATE)
                closed_trade_returns.append(sell_proceeds / buy_cost - 1 if buy_cost > 0 else 0)
                cash += sell_proceeds
                traded_amount += sell_value
                sold_today.add(code)
                del holdings[code]
                pending_open_sell_signals.pop(code, None)
                continue

            info["peak_price"] = max(info["peak_price"], high_price)
            holding_days = close_df.index.get_loc(date) - close_df.index.get_loc(info["entry_date"])
            max_profit = info["peak_price"] / info["entry_price"] - 1
            current_profit = close_price / info["entry_price"] - 1
            if (
                holding_days > LOW_EFFICIENCY_MIN_HOLDING_DAYS
                and max_profit <= LOW_EFFICIENCY_MAX_PROFIT_THRESHOLD
                and current_profit <= LOW_EFFICIENCY_CURRENT_PROFIT_THRESHOLD
            ):
                pending_open_sell_signals[code] = True
            else:
                pending_open_sell_signals.pop(code, None)

        portfolio_value = cash + sum(info["value"] for info in holdings.values())
        portfolio_values.append(portfolio_value)
        turnover_records.append(traded_amount / portfolio_value if portfolio_value > 0 else 0)
        holding_count_records.append(len(holdings))
        prev_date = date

    nav = pd.Series(portfolio_values, index=close_df.index)
    turnover = pd.Series(turnover_records, index=close_df.index)
    holding_count = pd.Series(holding_count_records, index=close_df.index)
    metrics = calculate_metrics(nav, turnover, holding_count, closed_trade_returns, TRADE_START_DATE)
    return {
        "前高回撤卖出比例": retrace_drawdown,
        **metrics,
    }


def main():
    fields, universe_member, code_to_name = load_inputs()
    buy_signal, candidate_score = build_signals(fields["close"], fields["volume"], universe_member)
    records = []
    for retrace_drawdown in PEAK_RETRACE_GRID:
        print(f"回测前高回撤比例：{retrace_drawdown:.2%}")
        records.append(
            run_backtest(retrace_drawdown, fields, buy_signal, candidate_score, code_to_name)
        )
    results = pd.DataFrame(records)
    results = results.sort_values(
        ["夏普比率", "年化收益", "最大回撤"],
        ascending=[False, False, False],
    )
    output_path = os.path.join(OUTPUT_DIR, "参数优化_中证800_前高回撤卖出比例.csv")
    results.to_csv(output_path, index=False, encoding="utf-8-sig")
    display_columns = [
        "前高回撤卖出比例",
        "累计收益",
        "年化收益",
        "年化波动",
        "夏普比率",
        "最大回撤",
        "平均持仓数",
        "年化换手率",
        "平仓笔数",
    ]
    print("\n【前高回撤卖出比例优化结果】")
    print(results[display_columns].to_string(index=False))
    print(f"\n输出完成：{output_path}")


if __name__ == "__main__":
    main()
