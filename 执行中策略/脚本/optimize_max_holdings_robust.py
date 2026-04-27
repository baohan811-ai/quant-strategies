import os
from datetime import datetime

import numpy as np
import pandas as pd


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
CACHE_DIR = os.path.join(BASE_DIR, "缓存")
OUTPUT_DIR = os.path.join(BASE_DIR, "输出")
os.makedirs(OUTPUT_DIR, exist_ok=True)


TRANSACTION_COST_RATE = 0.0025
STOP_DRAWDOWN = 0.2
PEAK_RETRACE_SELL_DRAWDOWN = 0.1
LOW_EFFICIENCY_MIN_HOLDING_DAYS = 100
LOW_EFFICIENCY_MAX_PROFIT_THRESHOLD = 0.03
LOW_EFFICIENCY_CURRENT_PROFIT_THRESHOLD = 0.00

MA5_MA60_GAP_WEIGHT = 0.8
MA60_5D_TREND_WEIGHT = 0.8
VOLUME_RATIO_SCORE_WEIGHT = 0.5
VOLUME_RATIO_LOOKBACK = 20
VOLUME_RATIO_MAX_BONUS_BASE = 1.0
SIGNAL_MAX_DAILY_RETURN = 0.05

LOOKBACK_DAYS = 1600
TRADE_START_DATE = "2023-04-03"
CACHE_PREFIX = "中证800"

COARSE_VALUES = list(range(5, 85, 5))
FINE_RADIUS = 10
FINE_MIN_VALUE = 2


def sanitize_price_df(df):
    df = df.apply(pd.to_numeric, errors="coerce")
    df = df.where(df > 0, np.nan)
    df.index = pd.to_datetime(df.index)
    return df.sort_index().loc[:, ~df.columns.duplicated()]


def load_cached_df(field):
    path = os.path.join(CACHE_DIR, f"{CACHE_PREFIX}_{field}_PriceAdjF.pkl")
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    return sanitize_price_df(pd.read_pickle(path))


def align_dataframes(dataframes):
    common_index = None
    common_columns = None
    for df in dataframes:
        common_index = df.index if common_index is None else common_index.intersection(df.index)
        common_columns = df.columns if common_columns is None else common_columns.intersection(df.columns)
    return [df.loc[common_index, common_columns].copy() for df in dataframes]


def get_limit_up_ratio(code):
    if code.endswith(".BJ"):
        return 0.30
    if code.startswith("300") or code.startswith("688"):
        return 0.20
    return 0.10


def sanitize_score_component(df):
    return df.replace([np.inf, -np.inf], np.nan)


def calculate_score(ma5_df, ma60_df, volume_df):
    ma5_ma60_gap_score = MA5_MA60_GAP_WEIGHT * (ma5_df / ma60_df - 1)
    ma60_5d_trend_score = MA60_5D_TREND_WEIGHT * (ma60_df / ma60_df.shift(5) - 1)
    volume_ma = volume_df.rolling(VOLUME_RATIO_LOOKBACK).mean()
    volume_ratio = volume_df / volume_ma.replace(0, np.nan)
    volume_ratio_score = VOLUME_RATIO_SCORE_WEIGHT * (
        (volume_ratio - 1).clip(lower=0, upper=VOLUME_RATIO_MAX_BONUS_BASE)
    )
    return sanitize_score_component(ma5_ma60_gap_score + ma60_5d_trend_score + volume_ratio_score)


def evaluate_intraday_sell_signal(holding_info, open_price, low_price):
    entry_price = holding_info["entry_price"]
    prev_peak_price = holding_info["peak_price"]

    stop_loss_price = entry_price * (1 - STOP_DRAWDOWN)
    if low_price <= stop_loss_price:
        return open_price if open_price < stop_loss_price else stop_loss_price, "固定止损触发"

    if prev_peak_price > 0:
        retrace_price = prev_peak_price * (1 - PEAK_RETRACE_SELL_DRAWDOWN)
        if low_price <= retrace_price:
            return open_price if open_price < retrace_price else retrace_price, "前高回撤10%卖出"

    return np.nan, ""


def calculate_metrics(nav, turnover, position, closed_trade_returns, closed_trade_outcomes, start_ts):
    nav_analysis = nav.loc[nav.index >= start_ts]
    turnover_analysis = turnover.loc[turnover.index >= start_ts]
    position_analysis = position.loc[position.index >= start_ts]
    strategy_ret_analysis = nav_analysis.pct_change().fillna(0)

    has_position = position_analysis.sum(axis=1).gt(0)
    if not has_position.any():
        return {
            "年化收益": 0.0,
            "年化波动": 0.0,
            "夏普比率": 0.0,
            "最大回撤": 0.0,
            "平均持仓数": 0.0,
            "年化换手率": 0.0,
            "日胜率": 0.0,
            "平仓笔数": 0,
            "单笔盈利数": 0,
            "单笔平局数": 0,
            "单笔亏损数": 0,
            "单笔胜率(平局不计入)": 0.0,
        }

    first_trade_date = has_position.idxmax()
    nav_active = nav_analysis.loc[first_trade_date:]
    ret_active = strategy_ret_analysis.loc[first_trade_date:]
    position_active = position_analysis.loc[first_trade_date:]
    turnover_active = turnover_analysis.loc[first_trade_date:]

    annual_ret = nav_active.iloc[-1] ** (252 / len(nav_active)) - 1
    annual_vol = ret_active.std() * np.sqrt(252)
    sharpe = annual_ret / annual_vol if annual_vol != 0 else 0.0
    max_dd = (nav_active / nav_active.cummax() - 1).min()
    trade_count = len(closed_trade_returns)

    if trade_count > 0:
        trade_outcome_series = pd.Series(closed_trade_outcomes)
        trade_win_count = int((trade_outcome_series == "win").sum())
        trade_draw_count = int((trade_outcome_series == "draw").sum())
        trade_loss_count = int((trade_outcome_series == "loss").sum())
        decisive_trade_count = int(trade_outcome_series.isin(["win", "loss"]).sum())
        trade_win_rate = trade_win_count / decisive_trade_count if decisive_trade_count > 0 else 0.0
    else:
        trade_win_count = 0
        trade_draw_count = 0
        trade_loss_count = 0
        trade_win_rate = 0.0

    return {
        "年化收益": annual_ret,
        "年化波动": annual_vol,
        "夏普比率": sharpe,
        "最大回撤": max_dd,
        "平均持仓数": (position_active > 0).sum(axis=1).mean(),
        "年化换手率": turnover_active.mean() * 252,
        "日胜率": (ret_active > 0).mean(),
        "平仓笔数": trade_count,
        "单笔盈利数": trade_win_count,
        "单笔平局数": trade_draw_count,
        "单笔亏损数": trade_loss_count,
        "单笔胜率(平局不计入)": trade_win_rate,
    }


def segment_metrics(nav, start_ts, n_segments=3):
    active_nav = nav.loc[nav.index >= start_ts]
    segments = []
    if active_nav.empty:
        return segments

    splits = np.array_split(active_nav.index, n_segments)
    for idx, segment_index in enumerate(splits, start=1):
        if len(segment_index) < 2:
            continue
        seg_nav = active_nav.loc[segment_index]
        normalized_nav = seg_nav / seg_nav.iloc[0]
        seg_ret = normalized_nav.pct_change().fillna(0)
        annual_ret = normalized_nav.iloc[-1] ** (252 / len(normalized_nav)) - 1
        annual_vol = seg_ret.std() * np.sqrt(252)
        sharpe = annual_ret / annual_vol if annual_vol != 0 else 0.0
        max_dd = (normalized_nav / normalized_nav.cummax() - 1).min()
        segments.append({
            "分段": idx,
            "起始日期": segment_index[0],
            "结束日期": segment_index[-1],
            "分段年化收益": annual_ret,
            "分段夏普比率": sharpe,
            "分段最大回撤": max_dd,
        })
    return segments


def backtest(max_holdings, prepared):
    (
        open_df,
        low_df,
        high_df,
        close_df,
        buy_signal,
        candidate_score,
        trade_start_ts,
    ) = prepared
    initial_weight = 1 / max_holdings
    current_holdings = {}
    pending_open_sell_signals = {}
    cash = 1.0
    portfolio_values = []
    turnover_records = []
    closed_trade_returns = []
    closed_trade_outcomes = []
    position = pd.DataFrame(0.0, index=close_df.index, columns=close_df.columns)
    prev_date = None

    for date in close_df.index:
        traded_amount = 0.0
        sold_today = set()

        if prev_date is not None:
            for code in list(current_holdings.keys()):
                prev_price = close_df.at[prev_date, code]
                price = close_df.at[date, code]
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
            trade_return = sell_proceeds / buy_cost - 1 if buy_cost > 0 else 0.0
            closed_trade_returns.append(trade_return)
            closed_trade_outcomes.append("win" if trade_return > 0 else "loss")
            cash += sell_proceeds
            traded_amount += sell_value
            sold_today.add(code)
            del current_holdings[code]
            del pending_open_sell_signals[code]

        portfolio_before_buy = cash + sum(info["value"] for info in current_holdings.values())
        available_slots = max_holdings - len(current_holdings)
        can_open_new_position = date >= trade_start_ts
        if can_open_new_position and available_slots > 0 and cash > 0 and prev_date is not None:
            buy_candidates = buy_signal.columns[buy_signal.loc[date]].tolist()
            buy_candidates = [
                code for code in buy_candidates
                if code not in current_holdings and code not in sold_today
            ]

            if buy_candidates:
                score_prev = candidate_score.loc[prev_date, buy_candidates].dropna().sort_values(ascending=False)
                for code in score_prev.head(available_slots).index:
                    open_price = open_df.at[date, code]
                    high_price = high_df.at[date, code]
                    close_price = close_df.at[date, code]
                    prev_close = close_df.at[prev_date, code]
                    if (
                        pd.isna(open_price)
                        or pd.isna(high_price)
                        or pd.isna(close_price)
                        or open_price <= 0
                        or high_price <= 0
                        or close_price <= 0
                    ):
                        continue

                    if pd.notna(prev_close) and prev_close > 0:
                        limit_up_price = prev_close * (1 + get_limit_up_ratio(code))
                        if open_price >= limit_up_price * 0.999:
                            continue

                    target_value = portfolio_before_buy * initial_weight
                    buy_value = min(target_value, cash / (1 + TRANSACTION_COST_RATE))
                    if buy_value <= 0:
                        break

                    cash -= buy_value * (1 + TRANSACTION_COST_RATE)
                    traded_amount += buy_value
                    current_holdings[code] = {
                        "entry_price": open_price,
                        "entry_date": date,
                        "peak_price": high_price,
                        "value": buy_value * (close_price / open_price),
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
            entry_price = holding_info["entry_price"]
            entry_date = holding_info["entry_date"]
            if date <= entry_date:
                continue

            sell_price, sell_reason = evaluate_intraday_sell_signal(holding_info, open_price, low_price)
            limit_down_blocked = False
            if pd.notna(sell_price) and pd.notna(prev_close) and prev_close > 0:
                limit_down_price = prev_close * (1 - get_limit_up_ratio(code))
                if close_price <= limit_down_price * 1.001:
                    limit_down_blocked = True

            if pd.notna(sell_price) and not limit_down_blocked:
                sell_value = holding_info["value"] * (sell_price / close_price)
                buy_cost = holding_info["cost_basis"] * (1 + TRANSACTION_COST_RATE)
                sell_proceeds = sell_value * (1 - TRANSACTION_COST_RATE)
                trade_return = sell_proceeds / buy_cost - 1 if buy_cost > 0 else 0.0
                closed_trade_returns.append(trade_return)
                closed_trade_outcomes.append("win" if trade_return > 0 else "loss")
                cash += sell_proceeds
                traded_amount += sell_value
                sold_today.add(code)
                del current_holdings[code]
                pending_open_sell_signals.pop(code, None)
                continue

            updated_peak_price = max(holding_info["peak_price"], high_price)
            current_holdings[code]["peak_price"] = updated_peak_price
            holding_days = close_df.index.get_loc(date) - close_df.index.get_loc(entry_date)
            max_profit_after_update = updated_peak_price / entry_price - 1 if entry_price > 0 else -np.inf
            current_profit = close_price / entry_price - 1 if entry_price > 0 else np.nan
            is_low_efficiency_holding = (
                holding_days > LOW_EFFICIENCY_MIN_HOLDING_DAYS
                and (
                    max_profit_after_update <= LOW_EFFICIENCY_MAX_PROFIT_THRESHOLD
                    or current_profit <= LOW_EFFICIENCY_CURRENT_PROFIT_THRESHOLD
                )
            )
            if is_low_efficiency_holding:
                pending_open_sell_signals[code] = {"signal_date": date, "reason": "低效持仓卖出"}
            else:
                pending_open_sell_signals.pop(code, None)

        portfolio_value = cash + sum(info["value"] for info in current_holdings.values())
        portfolio_values.append(portfolio_value)
        turnover_records.append(traded_amount / portfolio_value if portfolio_value > 0 else 0.0)
        for code, info in current_holdings.items():
            position.at[date, code] = info["value"] / portfolio_value if portfolio_value > 0 else 0.0

        prev_date = date

    nav = pd.Series(portfolio_values, index=close_df.index, name="净值")
    turnover = pd.Series(turnover_records, index=close_df.index, name="换手率")
    metrics = calculate_metrics(
        nav,
        turnover,
        position,
        closed_trade_returns,
        closed_trade_outcomes,
        trade_start_ts,
    )
    segments = segment_metrics(nav, trade_start_ts)
    return metrics, segments


def add_robust_columns(result_df):
    result_df = result_df.sort_values("MAX_HOLDINGS").reset_index(drop=True)
    result_df["分段正收益数"] = result_df["分段表现"].apply(
        lambda rows: sum(row["分段年化收益"] > 0 for row in rows)
    )
    result_df["分段正夏普数"] = result_df["分段表现"].apply(
        lambda rows: sum(row["分段夏普比率"] > 0 for row in rows)
    )
    result_df["分段年化收益均值"] = result_df["分段表现"].apply(
        lambda rows: np.mean([row["分段年化收益"] for row in rows])
    )
    result_df["分段年化收益标准差"] = result_df["分段表现"].apply(
        lambda rows: np.std([row["分段年化收益"] for row in rows], ddof=0)
    )
    result_df["分段夏普均值"] = result_df["分段表现"].apply(
        lambda rows: np.mean([row["分段夏普比率"] for row in rows])
    )
    result_df["分段夏普标准差"] = result_df["分段表现"].apply(
        lambda rows: np.std([row["分段夏普比率"] for row in rows], ddof=0)
    )
    result_df["分段最差最大回撤"] = result_df["分段表现"].apply(
        lambda rows: min(row["分段最大回撤"] for row in rows)
    )

    values = result_df["MAX_HOLDINGS"].tolist()
    for idx, row in result_df.iterrows():
        value = row["MAX_HOLDINGS"]
        neighbors = [v for v in values if abs(v - value) <= 2]
        neighbor_df = result_df[result_df["MAX_HOLDINGS"].isin(neighbors)]
        result_df.at[idx, "邻域样本数"] = len(neighbor_df)
        result_df.at[idx, "邻域年化收益均值"] = neighbor_df["年化收益"].mean()
        result_df.at[idx, "邻域夏普均值"] = neighbor_df["夏普比率"].mean()
        result_df.at[idx, "邻域最大回撤均值"] = neighbor_df["最大回撤"].mean()
        result_df.at[idx, "邻域年化收益标准差"] = neighbor_df["年化收益"].std(ddof=0)
        result_df.at[idx, "邻域夏普标准差"] = neighbor_df["夏普比率"].std(ddof=0)

    high_good_cols = [
        "年化收益",
        "夏普比率",
        "邻域年化收益均值",
        "邻域夏普均值",
        "分段正收益数",
    ]
    low_good_cols = [
        "最大回撤",
        "邻域年化收益标准差",
        "邻域夏普标准差",
        "分段年化收益标准差",
        "分段夏普标准差",
        "分段最差最大回撤",
    ]
    score_map = {
        "年化收益": "收益分",
        "夏普比率": "夏普分",
        "最大回撤": "回撤分",
        "邻域年化收益均值": "邻域收益分",
        "邻域夏普均值": "邻域夏普分",
        "邻域年化收益标准差": "邻域收益稳定分",
        "邻域夏普标准差": "邻域夏普稳定分",
        "分段年化收益标准差": "分段收益稳定分",
        "分段夏普标准差": "分段夏普稳定分",
        "分段正收益数": "分段正收益分",
        "分段最差最大回撤": "分段最差回撤分",
    }

    for col in high_good_cols:
        result_df[score_map[col]] = result_df[col].rank(pct=True)
    for col in low_good_cols:
        result_df[score_map[col]] = result_df[col].rank(pct=True, ascending=False)

    result_df["均衡综合得分"] = (
        0.35 * result_df["收益分"]
        + 0.25 * result_df["夏普分"]
        + 0.15 * result_df["回撤分"]
        + 0.15 * result_df["邻域收益分"]
        + 0.10 * result_df["邻域夏普分"]
    )
    result_df["鲁棒性综合得分"] = (
        0.15 * result_df["收益分"]
        + 0.15 * result_df["夏普分"]
        + 0.10 * result_df["回撤分"]
        + 0.12 * result_df["邻域收益分"]
        + 0.12 * result_df["邻域夏普分"]
        + 0.08 * result_df["邻域收益稳定分"]
        + 0.08 * result_df["邻域夏普稳定分"]
        + 0.06 * result_df["分段收益稳定分"]
        + 0.06 * result_df["分段夏普稳定分"]
        + 0.04 * result_df["分段正收益分"]
        + 0.04 * result_df["分段最差回撤分"]
    )
    return result_df


def run_grid(values, prepared):
    rows = []
    segment_rows = []
    for value in values:
        metrics, segments = backtest(value, prepared)
        row = {"MAX_HOLDINGS": value, **metrics, "分段表现": segments}
        rows.append(row)
        for segment in segments:
            segment_rows.append({"MAX_HOLDINGS": value, **segment})
        print(
            f"MAX_HOLDINGS={value:>2} 年化={metrics['年化收益']:.2%} "
            f"夏普={metrics['夏普比率']:.3f} 回撤={metrics['最大回撤']:.2%} "
            f"平均持仓={metrics['平均持仓数']:.1f}"
        )

    result_df = add_robust_columns(pd.DataFrame(rows))
    segment_df = pd.DataFrame(segment_rows)
    export_df = result_df.drop(columns=["分段表现"])
    return result_df, export_df, segment_df


def main():
    open_df = load_cached_df("open")
    low_df = load_cached_df("low")
    high_df = load_cached_df("high")
    close_df = load_cached_df("close")
    volume_df = load_cached_df("volume")
    open_df, low_df, high_df, close_df, volume_df = align_dataframes(
        [open_df, low_df, high_df, close_df, volume_df]
    )

    ma5 = close_df.rolling(5).mean()
    ma60 = close_df.rolling(60).mean()
    ma120 = close_df.rolling(120).mean()
    spread = ma5 - ma60
    signal = np.sign(spread)
    daily_ret = close_df.pct_change()
    golden_signal = (
        (signal.diff() == 2)
        & (ma60 > ma60.shift(1))
        & (ma120 > ma120.shift(1))
        & (daily_ret <= SIGNAL_MAX_DAILY_RETURN)
    )
    buy_signal = golden_signal.shift(1).fillna(False).astype(bool)
    candidate_score = calculate_score(ma5, ma60, volume_df)
    trade_start_ts = pd.Timestamp(datetime.strptime(TRADE_START_DATE, "%Y-%m-%d"))
    prepared = (open_df, low_df, high_df, close_df, buy_signal, candidate_score, trade_start_ts)

    print(f"行情区间: {close_df.index.min().date()} ~ {close_df.index.max().date()}, shape={close_df.shape}")
    print("开始粗扫")
    coarse_result, coarse_export, coarse_segments = run_grid(COARSE_VALUES, prepared)
    coarse_best = int(coarse_result.sort_values("鲁棒性综合得分", ascending=False).iloc[0]["MAX_HOLDINGS"])
    coarse_perf_best = coarse_result.sort_values("年化收益", ascending=False).iloc[0]
    coarse_perf_best_value = int(coarse_perf_best["MAX_HOLDINGS"])

    fine_start = max(FINE_MIN_VALUE, min(coarse_best, coarse_perf_best_value) - FINE_RADIUS)
    fine_end = max(coarse_best, coarse_perf_best_value) + FINE_RADIUS
    fine_values = list(range(fine_start, fine_end + 1))
    print(f"开始细扫: {fine_start} ~ {fine_end}")
    fine_result, fine_export, fine_segments = run_grid(fine_values, prepared)

    fine_balance_best = fine_result.sort_values("均衡综合得分", ascending=False).iloc[0]
    fine_robust_best = fine_result.sort_values("鲁棒性综合得分", ascending=False).iloc[0]

    conclusion = pd.DataFrame({
        "项目": [
            "粗扫绩效最优参数",
            "粗扫绩效最优年化收益",
            "粗扫绩效最优夏普比率",
            "粗扫鲁棒最优参数",
            "细扫均衡最优参数",
            "细扫均衡最优年化收益",
            "细扫均衡最优夏普比率",
            "细扫均衡最优最大回撤",
            "细扫均衡综合得分",
            "细扫鲁棒最优参数",
            "细扫鲁棒最优年化收益",
            "细扫鲁棒最优夏普比率",
            "细扫鲁棒最优最大回撤",
            "细扫鲁棒性综合得分",
            "细扫鲁棒最优分段正收益数",
            "细扫鲁棒最优邻域夏普均值",
        ],
        "数值": [
            coarse_perf_best["MAX_HOLDINGS"],
            coarse_perf_best["年化收益"],
            coarse_perf_best["夏普比率"],
            coarse_best,
            fine_balance_best["MAX_HOLDINGS"],
            fine_balance_best["年化收益"],
            fine_balance_best["夏普比率"],
            fine_balance_best["最大回撤"],
            fine_balance_best["均衡综合得分"],
            fine_robust_best["MAX_HOLDINGS"],
            fine_robust_best["年化收益"],
            fine_robust_best["夏普比率"],
            fine_robust_best["最大回撤"],
            fine_robust_best["鲁棒性综合得分"],
            fine_robust_best["分段正收益数"],
            fine_robust_best["邻域夏普均值"],
        ],
    })

    output_path = os.path.join(OUTPUT_DIR, "MAX_HOLDINGS_鲁棒性测试.xlsx")
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        conclusion.to_excel(writer, sheet_name="结论", index=False)
        coarse_export.to_excel(writer, sheet_name="粗扫结果", index=False)
        fine_export.to_excel(writer, sheet_name="细扫结果", index=False)
        coarse_segments.insert(0, "阶段", "粗扫")
        fine_segments.insert(0, "阶段", "细扫")
        coarse_segments.to_excel(writer, sheet_name="粗扫分段表现", index=False)
        fine_segments.to_excel(writer, sheet_name="细扫分段表现", index=False)

    print(f"已输出: {output_path}")
    print(conclusion.to_string(index=False))


if __name__ == "__main__":
    main()
