import math
import re
from pathlib import Path


TARGET_SCRIPT = Path("/Users/han/Documents/PJ1/执行中策略/脚本/趋势发现_金叉_中证800_执行版.py")


def frange(start, stop, step):
    values = []
    current = start
    while current <= stop + 1e-12:
        values.append(round(current, 6))
        current += step
    return values


def patch_source_for_snapshot(source: str) -> str:
    patched = re.sub(
        r'output_file = os\.path\.join\(output_dir, f"金叉买入_分层回撤卖出策略_中证800_\{end_date\}\.xlsx"\)\n\nwith pd\.ExcelWriter\(output_file, engine="openpyxl"\) as writer:\n(?:    .*\n)+',
        'output_file = None\n',
        source,
        count=1,
        flags=re.MULTILINE,
    )
    return patched


def load_strategy_environment():
    source = TARGET_SCRIPT.read_text(encoding="utf-8")
    globals_dict = {"__name__": "__main__", "__file__": str(TARGET_SCRIPT)}
    code = compile(patch_source_for_snapshot(source), str(TARGET_SCRIPT), "exec")
    exec(code, globals_dict)
    return globals_dict


def run_backtest(env, entry_day_profit_stop_drawdown=None):
    if entry_day_profit_stop_drawdown is not None:
        env["ENTRY_DAY_PROFIT_STOP_DRAWDOWN"] = entry_day_profit_stop_drawdown

    pd_mod = env["pd"]
    np_mod = env["np"]

    open_df = env["open_df"]
    low_df = env["low_df"]
    high_df = env["high_df"]
    close_df = env["close_df"]
    volume_df = env["volume_df"]

    ma5 = env["ma5"]
    ma60 = env["ma60"]
    ma120 = env["ma120"]
    trade_start_ts = env["trade_start_ts"]
    code_to_name = env["code_to_name"]

    spread = ma5 - ma60
    signal = np_mod.sign(spread)
    daily_ret = close_df.pct_change()
    ma60_up = ma60 > ma60.shift(1)
    ma120_up = ma120 > ma120.shift(1)
    limit_gain = daily_ret <= env["SIGNAL_MAX_DAILY_RETURN"]
    score_details = env["calculate_golden_cross_score_details"](ma5, ma60, volume_df)
    candidate_score = score_details["total_score"]

    cross = signal.diff()
    golden_signal = (cross == 2) & ma60_up & ma120_up & limit_gain
    buy_signal = golden_signal.shift(1).fillna(False).astype(bool)

    current_holdings = {}
    cash = 1.0
    portfolio_values = []
    turnover_records = []
    holding_counts = []
    closed_trade_returns = []
    closed_trade_outcomes = []
    prev_date = None

    for date in close_df.index:
        traded_amount = 0.0
        portfolio_before_buy = cash + sum(info["value"] for info in current_holdings.values())

        if prev_date is not None:
            for code in list(current_holdings.keys()):
                prev_price = close_df.at[prev_date, code]
                price = close_df.at[date, code]
                if pd_mod.isna(prev_price) or pd_mod.isna(price) or prev_price <= 0:
                    continue
                current_holdings[code]["value"] *= price / prev_price

        available_slots = env["MAX_HOLDINGS"] - len(current_holdings)
        can_open_new_position = trade_start_ts is None or date >= trade_start_ts
        if can_open_new_position and available_slots > 0 and cash > 0:
            buy_candidates = buy_signal.columns[buy_signal.loc[date]].tolist()
            buy_candidates = [code for code in buy_candidates if code not in current_holdings]

            if buy_candidates and prev_date is not None:
                score_prev = candidate_score.loc[prev_date, buy_candidates].dropna().sort_values(ascending=False)
                for code in score_prev.head(available_slots).index:
                    open_price = open_df.at[date, code]
                    high_price = high_df.at[date, code]
                    close_price = close_df.at[date, code]
                    prev_close = close_df.at[prev_date, code]
                    if (
                        pd_mod.isna(open_price)
                        or pd_mod.isna(high_price)
                        or pd_mod.isna(close_price)
                        or open_price <= 0
                        or high_price <= 0
                        or close_price <= 0
                    ):
                        continue

                    if pd_mod.notna(prev_close) and prev_close > 0:
                        limit_ratio = env["get_limit_up_ratio"](code, code_to_name.get(code, code))
                        limit_up_price = prev_close * (1 + limit_ratio)
                        if open_price >= limit_up_price * 0.999:
                            continue

                    target_value = portfolio_before_buy * env["INITIAL_WEIGHT"]
                    max_affordable = cash / (1 + env["TRANSACTION_COST_RATE"])
                    buy_value = min(target_value, max_affordable)
                    if buy_value <= 0:
                        break

                    cash -= buy_value * (1 + env["TRANSACTION_COST_RATE"])
                    traded_amount += buy_value
                    current_holdings[code] = {
                        "entry_price": open_price,
                        "entry_date": date,
                        "entry_day_close_above_cost": close_price >= open_price,
                        "peak_price": high_price,
                        "value": buy_value * (close_price / open_price),
                        "cost_basis": buy_value,
                    }

        for code in list(current_holdings.keys()):
            open_price = open_df.at[date, code]
            low_price = low_df.at[date, code]
            high_price = high_df.at[date, code]
            close_price = close_df.at[date, code]
            prev_close = close_df.at[prev_date, code] if prev_date is not None else np_mod.nan
            if pd_mod.isna(open_price) or pd_mod.isna(low_price) or pd_mod.isna(high_price) or pd_mod.isna(close_price):
                continue

            holding_info = current_holdings[code]
            entry_date = holding_info["entry_date"]
            if date <= entry_date:
                continue

            sell_eval = env["evaluate_intraday_sell_signal"](holding_info, open_price, low_price, high_price)
            prev_peak_price = sell_eval["prev_peak_price"]
            sell_price = sell_eval["sell_price"]
            is_breakeven_exit = sell_eval["is_breakeven_exit"]

            limit_down_blocked = False
            if pd_mod.notna(sell_price) and pd_mod.notna(prev_close) and prev_close > 0:
                limit_ratio = env["get_limit_down_ratio"](code, code_to_name.get(code, code))
                limit_down_price = prev_close * (1 - limit_ratio)
                if close_price <= limit_down_price * 1.001:
                    limit_down_blocked = True

            if pd_mod.notna(sell_price) and limit_down_blocked:
                continue

            if pd_mod.notna(sell_price):
                sell_value = holding_info["value"] * (sell_price / close_price)
                buy_cost = holding_info["cost_basis"] * (1 + env["TRANSACTION_COST_RATE"])
                sell_proceeds = sell_value * (1 - env["TRANSACTION_COST_RATE"])
                trade_return = sell_proceeds / buy_cost - 1 if buy_cost > 0 else 0
                closed_trade_returns.append(trade_return)
                if is_breakeven_exit:
                    trade_outcome = "draw"
                elif trade_return > 0:
                    trade_outcome = "win"
                else:
                    trade_outcome = "loss"
                closed_trade_outcomes.append(trade_outcome)
                cash += sell_proceeds
                traded_amount += sell_value
                del current_holdings[code]
                continue

            current_holdings[code]["peak_price"] = max(prev_peak_price, high_price)

        portfolio_value = cash + sum(info["value"] for info in current_holdings.values())
        portfolio_values.append(portfolio_value)
        turnover_records.append(traded_amount / portfolio_value if portfolio_value > 0 else 0.0)
        holding_counts.append(len(current_holdings))
        prev_date = date

    nav = pd_mod.Series(portfolio_values, index=close_df.index, name="净值")
    strategy_ret = nav.pct_change().fillna(0)
    turnover = pd_mod.Series(turnover_records, index=close_df.index, name="换手率")

    if trade_start_ts is not None:
        valid_analysis_dates = nav.index[nav.index >= trade_start_ts]
        analysis_start_date = valid_analysis_dates[0]
    else:
        analysis_start_date = nav.index[0]

    nav_analysis = nav.loc[analysis_start_date:]
    strategy_ret_analysis = strategy_ret.loc[analysis_start_date:]
    turnover_analysis = turnover.loc[analysis_start_date:]
    holding_count_series = pd_mod.Series(holding_counts, index=close_df.index)
    holding_count_analysis = holding_count_series.loc[analysis_start_date:]
    has_position = holding_count_analysis.gt(0)

    if has_position.any():
        first_trade_date = has_position.idxmax()
        nav_active = nav_analysis.loc[first_trade_date:]
        ret_active = strategy_ret_analysis.loc[first_trade_date:]
        turnover_active = turnover_analysis.loc[first_trade_date:]
        annual_ret = nav_active.iloc[-1] ** (252 / len(nav_active)) - 1
        annual_vol = ret_active.std() * np_mod.sqrt(252)
        sharpe = annual_ret / annual_vol if annual_vol != 0 else 0
        rolling_max = nav_active.cummax()
        drawdown = nav_active / rolling_max - 1
        max_dd = drawdown.min()
        trade_count = len(closed_trade_returns)
    else:
        annual_ret = 0
        sharpe = 0
        max_dd = 0
        trade_count = 0

    return {
        "entry_day_profit_stop_drawdown": env["ENTRY_DAY_PROFIT_STOP_DRAWDOWN"],
        "annual_ret": float(annual_ret) if annual_ret is not None and not math.isnan(annual_ret) else float("-inf"),
        "sharpe": float(sharpe) if sharpe is not None and not math.isnan(sharpe) else float("nan"),
        "max_dd": float(max_dd) if max_dd is not None and not math.isnan(max_dd) else float("nan"),
        "trade_count": int(trade_count),
    }


def main():
    env = load_strategy_environment()
    try:
        coarse_candidates = frange(0.02, 0.20, 0.01)
        coarse_results = []

        print("开始粗扫 ENTRY_DAY_PROFIT_STOP_DRAWDOWN ...")
        for value in coarse_candidates:
            result = run_backtest(env, entry_day_profit_stop_drawdown=value)
            coarse_results.append(result)
            print(
                f"粗扫 entry_day_profit_stop_drawdown={value:.3f} "
                f"annual_ret={result['annual_ret']:.6f} "
                f"sharpe={result['sharpe']:.6f} "
                f"max_dd={result['max_dd']:.6f} "
                f"trades={result['trade_count']}"
            )

        coarse_best = max(coarse_results, key=lambda item: item["annual_ret"])
        center = coarse_best["entry_day_profit_stop_drawdown"]
        fine_candidates = frange(max(0.0, center - 0.01), center + 0.01, 0.002)
        fine_results = []

        print("\n开始细扫 ENTRY_DAY_PROFIT_STOP_DRAWDOWN ...")
        for value in fine_candidates:
            result = run_backtest(env, entry_day_profit_stop_drawdown=value)
            fine_results.append(result)
            print(
                f"细扫 entry_day_profit_stop_drawdown={value:.3f} "
                f"annual_ret={result['annual_ret']:.6f} "
                f"sharpe={result['sharpe']:.6f} "
                f"max_dd={result['max_dd']:.6f} "
                f"trades={result['trade_count']}"
            )

        fine_best = max(fine_results, key=lambda item: item["annual_ret"])
        top5 = sorted(fine_results, key=lambda item: item["annual_ret"], reverse=True)[:5]

        print("\n粗扫最优:")
        print(coarse_best)
        print("\n细扫最优:")
        print(fine_best)
        print("\n细扫前五:")
        for item in top5:
            print(item)
    finally:
        wind_client = env.get("w")
        if wind_client is not None:
            try:
                wind_client.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
