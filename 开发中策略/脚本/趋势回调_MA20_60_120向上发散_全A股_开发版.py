#!/usr/bin/env python3
"""MA20/60/120 向上发散后回调买入的全 A 股策略开发版。

交易规则：
1. MA20 > MA60 > MA120，三条均线均高于 60 个交易日前；
2. MA20/MA60 与 MA60/MA120 的间距均比 60 个交易日前扩大；
3. 按20日年化波动率选择参考线：低波动用 MA20，中波动用 MA60，高波动用 MA120；
4. 若所选均线近期斜率不稳定，自动升级到更长周期的均线；
5. 股价正在回落，且收盘价与自适应参考线的绝对差异不超过 5%，当日收盘买入；
6. 收盘价高于自适应参考线 40% 或相对买入价下跌 10%，当日收盘卖出。

说明：
- 选股范围使用本地数据库的历史“全部A股”成分快照，避免明显的幸存者偏差；
- 买卖价均按当日收盘价模拟，因此回测假设收盘附近可以成交；
- 默认最多持有 20 只，等权目标仓位，单边交易成本 0.20%；
- 只读取“开发中策略/缓存”指向的共用 SQLite 数据库，不调用 Wind。
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
STRATEGY_DIR = SCRIPT_DIR.parent
DB_PATH = STRATEGY_DIR / "缓存" / "本地行情数据库.sqlite3"
OUTPUT_DIR = STRATEGY_DIR / "输出" / "MA多头发散回调策略"
EXECUTION_SCRIPT_DIR = STRATEGY_DIR.parent / "执行中策略" / "脚本"
if str(EXECUTION_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(EXECUTION_SCRIPT_DIR))

from 维护工具.local_market_db import get_latest_price_date, load_price_matrix

UNIVERSE_NAME = "全部A股"
DEFAULT_START_DATE = "2023-04-03"
DEFAULT_MAX_HOLDINGS = 20
TRANSACTION_COST_RATE = 0.002
BUY_REFERENCE_DISTANCE = 0.05
SELL_REFERENCE_PREMIUM = 0.40
STOP_LOSS_RATE = 0.10
CALENDAR_LOOKBACK_DAYS = 550
VOLATILITY_LOOKBACK = 20
LOW_VOLATILITY_THRESHOLD = 0.35
HIGH_VOLATILITY_THRESHOLD = 0.65
TREND_DIRECTION_LOOKBACK = 60
SLOPE_STABILITY_LOOKBACK = 60
MIN_SLOPE_STABILITY = 1.0


@dataclass
class Holding:
    entry_date: pd.Timestamp
    entry_price: float
    value: float
    cost_basis: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="全A股 MA20/60/120 向上发散回调策略")
    parser.add_argument("--start-date", default=DEFAULT_START_DATE, help="开始建仓日，YYYY-MM-DD")
    parser.add_argument("--end-date", default=None, help="结束日，默认使用本地数据库最新日期")
    parser.add_argument("--max-holdings", type=int, default=DEFAULT_MAX_HOLDINGS, help="最大持股数")
    parser.add_argument(
        "--code-limit",
        type=int,
        default=0,
        help="仅用于快速调试；0 表示使用全部 A 股",
    )
    parser.add_argument("--no-excel", action="store_true", help="运行回测但不生成 Excel")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> tuple[pd.Timestamp, pd.Timestamp]:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"未找到共用本地数据库：{DB_PATH}")
    if args.max_holdings <= 0:
        raise ValueError("--max-holdings 必须大于 0")
    if args.code_limit < 0:
        raise ValueError("--code-limit 不能为负数")

    start_date = pd.Timestamp(args.start_date).normalize()
    latest = get_latest_price_date(db_path=DB_PATH, adjusted="F")
    if latest is None:
        raise RuntimeError("本地数据库没有可用的收盘价")
    end_date = pd.Timestamp(args.end_date or latest).normalize()
    if start_date > end_date:
        raise ValueError("开始日不能晚于结束日")
    return start_date, end_date


def load_universe_snapshots(
    data_start: pd.Timestamp,
    end_date: pd.Timestamp,
    code_limit: int,
) -> tuple[pd.DataFrame, list[str], dict[str, str]]:
    start_text = data_start.strftime("%Y-%m-%d")
    end_text = end_date.strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as conn:
        snapshots = pd.read_sql_query(
            """
            SELECT snapshot_date, wind_code, sec_name
            FROM universe_constituents_snapshot
            WHERE universe_name = ?
              AND snapshot_date = (
                  SELECT MAX(snapshot_date)
                  FROM universe_constituents_snapshot
                  WHERE universe_name = ? AND snapshot_date < ?
              )
            UNION ALL
            SELECT snapshot_date, wind_code, sec_name
            FROM universe_constituents_snapshot
            WHERE universe_name = ? AND snapshot_date BETWEEN ? AND ?
            ORDER BY snapshot_date, wind_code
            """,
            conn,
            params=[
                UNIVERSE_NAME,
                UNIVERSE_NAME,
                start_text,
                UNIVERSE_NAME,
                start_text,
                end_text,
            ],
        )
        current_names = pd.read_sql_query(
            """
            SELECT wind_code, sec_name
            FROM stock_universe
            WHERE universe_name = ?
            ORDER BY wind_code
            """,
            conn,
            params=[UNIVERSE_NAME],
        )

    if snapshots.empty:
        raise RuntimeError("本地数据库没有“全部A股”历史成分快照")
    snapshots["snapshot_date"] = pd.to_datetime(snapshots["snapshot_date"])
    codes = sorted(snapshots["wind_code"].dropna().unique().tolist())
    if code_limit:
        codes = codes[:code_limit]
        snapshots = snapshots.loc[snapshots["wind_code"].isin(codes)].copy()

    names = (
        pd.concat(
            [snapshots[["wind_code", "sec_name"]], current_names],
            ignore_index=True,
        )
        .dropna(subset=["wind_code"])
        .drop_duplicates("wind_code", keep="last")
        .set_index("wind_code")["sec_name"]
        .fillna("")
        .to_dict()
    )
    return snapshots, codes, names


def load_close_matrix(
    codes: list[str],
    data_start: pd.Timestamp,
    end_date: pd.Timestamp,
) -> pd.DataFrame:
    close = load_price_matrix(
        "开发中策略_全A股_MA回调",
        "close",
        codes=codes,
        start_date=data_start,
        end_date=end_date,
        target_columns=codes,
        prefer_sqlite=True,
        fallback_pickle=False,
        require_complete_eod=True,
        adjusted="F",
        db_path=DB_PATH,
    )
    close = close.apply(pd.to_numeric, errors="coerce").where(lambda frame: frame > 0)
    if close.empty or close.notna().sum().sum() == 0:
        raise RuntimeError("指定时间段没有可用行情")
    return close.reindex(columns=codes).sort_index().astype("float64")


def build_membership_matrix(
    snapshots: pd.DataFrame,
    dates: pd.DatetimeIndex,
    codes: list[str],
) -> pd.DataFrame:
    member = pd.DataFrame(False, index=dates, columns=codes, dtype=bool)
    snapshot_dates = snapshots["snapshot_date"].drop_duplicates().sort_values().tolist()
    for index, snapshot_date in enumerate(snapshot_dates):
        next_date = snapshot_dates[index + 1] if index + 1 < len(snapshot_dates) else None
        active_dates = dates[dates >= snapshot_date]
        if next_date is not None:
            active_dates = active_dates[active_dates < next_date]
        active_codes = snapshots.loc[
            snapshots["snapshot_date"].eq(snapshot_date), "wind_code"
        ].tolist()
        active_codes = [code for code in active_codes if code in member.columns]
        if len(active_dates) and active_codes:
            member.loc[active_dates, active_codes] = True
    return member


def calculate_signals(
    close: pd.DataFrame,
    member: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    ma20 = close.rolling(20, min_periods=20).mean()
    ma60 = close.rolling(60, min_periods=60).mean()
    ma120 = close.rolling(120, min_periods=120).mean()

    ordered = ma20.gt(ma60) & ma60.gt(ma120)
    all_rising = (
        ma20.gt(ma20.shift(TREND_DIRECTION_LOOKBACK))
        & ma60.gt(ma60.shift(TREND_DIRECTION_LOOKBACK))
        & ma120.gt(ma120.shift(TREND_DIRECTION_LOOKBACK))
    )
    gap_20_60 = ma20 / ma60 - 1
    gap_60_120 = ma60 / ma120 - 1
    spreading = gap_20_60.gt(
        gap_20_60.shift(TREND_DIRECTION_LOOKBACK)
    ) & gap_60_120.gt(gap_60_120.shift(TREND_DIRECTION_LOOKBACK))
    trend = ordered & all_rising & spreading

    annualized_volatility = (
        close.pct_change(fill_method=None)
        .rolling(VOLATILITY_LOOKBACK, min_periods=VOLATILITY_LOOKBACK)
        .std()
        * math.sqrt(252)
    )

    def slope_stability(ma: pd.DataFrame) -> pd.DataFrame:
        daily_slope = ma.pct_change(fill_method=None)
        mean_slope = daily_slope.rolling(
            SLOPE_STABILITY_LOOKBACK,
            min_periods=SLOPE_STABILITY_LOOKBACK,
        ).mean()
        slope_std = daily_slope.rolling(
            SLOPE_STABILITY_LOOKBACK,
            min_periods=SLOPE_STABILITY_LOOKBACK,
        ).std()
        return (mean_slope / slope_std.replace(0, np.nan)).replace(
            [np.inf, -np.inf], np.nan
        )

    stability20 = slope_stability(ma20)
    stability60 = slope_stability(ma60)
    stability120 = slope_stability(ma120)

    reference_period = pd.DataFrame(
        120, index=close.index, columns=close.columns, dtype="int16"
    )
    reference_period = reference_period.mask(
        annualized_volatility.lt(HIGH_VOLATILITY_THRESHOLD), 60
    )
    reference_period = reference_period.mask(
        annualized_volatility.lt(LOW_VOLATILITY_THRESHOLD), 20
    )

    unstable20 = stability20.lt(MIN_SLOPE_STABILITY) | ma20.le(
        ma20.shift(TREND_DIRECTION_LOOKBACK)
    )
    reference_period = reference_period.mask(reference_period.eq(20) & unstable20, 60)
    unstable60 = stability60.lt(MIN_SLOPE_STABILITY) | ma60.le(
        ma60.shift(TREND_DIRECTION_LOOKBACK)
    )
    reference_period = reference_period.mask(reference_period.eq(60) & unstable60, 120)

    reference_ma = ma120.copy()
    reference_ma = reference_ma.mask(reference_period.eq(60), ma60)
    reference_ma = reference_ma.mask(reference_period.eq(20), ma20)
    reference_stability = stability120.copy()
    reference_stability = reference_stability.mask(reference_period.eq(60), stability60)
    reference_stability = reference_stability.mask(reference_period.eq(20), stability20)

    price_reference_gap = close / reference_ma - 1
    pulling_back = price_reference_gap.lt(price_reference_gap.shift(1))
    in_buy_zone = price_reference_gap.abs().le(BUY_REFERENCE_DISTANCE)
    stable_reference = reference_stability.ge(MIN_SLOPE_STABILITY)
    buy = trend & stable_reference & pulling_back & in_buy_zone & member & close.notna()
    ma_sell = price_reference_gap.ge(SELL_REFERENCE_PREMIUM) & close.notna()
    trend_strength = gap_20_60 + gap_60_120

    return {
        "ma20": ma20,
        "ma60": ma60,
        "ma120": ma120,
        "annualized_volatility": annualized_volatility,
        "reference_period": reference_period,
        "reference_ma": reference_ma,
        "reference_stability": reference_stability,
        "price_reference_gap": price_reference_gap,
        "gap_20_60": gap_20_60,
        "gap_60_120": gap_60_120,
        "trend_strength": trend_strength,
        "trend": trend,
        "buy": buy,
        "ma_sell": ma_sell,
    }


def rank_buy_candidates(
    date: pd.Timestamp,
    candidates: list[str],
    signals: dict[str, pd.DataFrame],
) -> list[str]:
    if not candidates:
        return []
    ranking = pd.DataFrame(
        {
            "code": candidates,
            "trend_strength": [signals["trend_strength"].at[date, code] for code in candidates],
            "abs_reference_gap": [
                abs(signals["price_reference_gap"].at[date, code]) for code in candidates
            ],
        }
    )
    ranking = ranking.replace([np.inf, -np.inf], np.nan).dropna()
    return ranking.sort_values(
        ["trend_strength", "abs_reference_gap", "code"],
        ascending=[False, True, True],
    )["code"].tolist()


def signal_details(
    date: pd.Timestamp,
    codes: list[str],
    close: pd.DataFrame,
    signals: dict[str, pd.DataFrame],
    names: dict[str, str],
    signal_type: str,
) -> pd.DataFrame:
    rows = []
    for code in codes:
        rows.append(
            {
                "信号类型": signal_type,
                "日期": date,
                "代码": code,
                "名称": names.get(code, code),
                "收盘价": close.at[date, code],
                "MA20": signals["ma20"].at[date, code],
                "MA60": signals["ma60"].at[date, code],
                "MA120": signals["ma120"].at[date, code],
                "20日年化波动率": signals["annualized_volatility"].at[date, code],
                "参考均线周期": int(signals["reference_period"].at[date, code]),
                "参考均线值": signals["reference_ma"].at[date, code],
                "参考线斜率稳定度": signals["reference_stability"].at[date, code],
                "股价相对参考均线": signals["price_reference_gap"].at[date, code],
                "MA20相对MA60": signals["gap_20_60"].at[date, code],
                "MA60相对MA120": signals["gap_60_120"].at[date, code],
                "趋势强度": signals["trend_strength"].at[date, code],
            }
        )
    return pd.DataFrame(rows)


def run_backtest(
    close: pd.DataFrame,
    member: pd.DataFrame,
    signals: dict[str, pd.DataFrame],
    names: dict[str, str],
    start_date: pd.Timestamp,
    max_holdings: int,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    dates = close.index[close.index >= start_date]
    if dates.empty:
        raise RuntimeError("开始建仓日之后没有可用交易日")

    cash = 1.0
    holdings: dict[str, Holding] = {}
    daily_rows = []
    trade_rows = []
    previous_date: pd.Timestamp | None = None

    for date in dates:
        sold_today: set[str] = set()
        if previous_date is not None:
            for code, holding in list(holdings.items()):
                previous_close = close.at[previous_date, code]
                current_close = close.at[date, code]
                if pd.notna(previous_close) and pd.notna(current_close) and previous_close > 0:
                    holding.value *= current_close / previous_close

        for code, holding in list(holdings.items()):
            current_close = close.at[date, code]
            if pd.isna(current_close) or current_close <= 0:
                continue
            stop_triggered = current_close <= holding.entry_price * (1 - STOP_LOSS_RATE)
            ma_sell_triggered = bool(signals["ma_sell"].at[date, code])
            left_universe = not bool(member.at[date, code])
            if not (stop_triggered or ma_sell_triggered or left_universe):
                continue

            if stop_triggered:
                reason = "相对买入价止损10%"
            elif ma_sell_triggered:
                reason = "股价高于自适应参考均线达40%"
            else:
                reason = "移出全A股历史成分快照"
            proceeds = holding.value * (1 - TRANSACTION_COST_RATE)
            cash += proceeds
            net_return = proceeds / (holding.cost_basis * (1 + TRANSACTION_COST_RATE)) - 1
            trade_rows.append(
                {
                    "日期": date,
                    "动作": "卖出",
                    "代码": code,
                    "名称": names.get(code, code),
                    "价格": current_close,
                    "交易前市值": holding.value,
                    "交易成本率": TRANSACTION_COST_RATE,
                    "单笔净收益": net_return,
                    "买入日期": holding.entry_date,
                    "买入价": holding.entry_price,
                    "触发原因": reason,
                    "参考均线周期": int(signals["reference_period"].at[date, code]),
                    "股价相对参考均线": signals["price_reference_gap"].at[date, code],
                }
            )
            del holdings[code]
            sold_today.add(code)

        portfolio_value = cash + sum(item.value for item in holdings.values())
        available_slots = max_holdings - len(holdings)
        if available_slots > 0 and cash > 0:
            candidate_mask = signals["buy"].loc[date]
            candidates = [
                code
                for code in candidate_mask.index[candidate_mask].tolist()
                if code not in holdings and code not in sold_today
            ]
            ranked = rank_buy_candidates(date, candidates, signals)
            for code in ranked[:available_slots]:
                buy_price = close.at[date, code]
                if pd.isna(buy_price) or buy_price <= 0:
                    continue
                target_value = portfolio_value / max_holdings
                buy_value = min(target_value, cash / (1 + TRANSACTION_COST_RATE))
                if buy_value <= 0:
                    break
                cash -= buy_value * (1 + TRANSACTION_COST_RATE)
                holdings[code] = Holding(
                    entry_date=date,
                    entry_price=float(buy_price),
                    value=float(buy_value),
                    cost_basis=float(buy_value),
                )
                trade_rows.append(
                    {
                        "日期": date,
                        "动作": "买入",
                        "代码": code,
                        "名称": names.get(code, code),
                        "价格": buy_price,
                        "交易前市值": buy_value,
                        "交易成本率": TRANSACTION_COST_RATE,
                        "单笔净收益": np.nan,
                        "买入日期": date,
                        "买入价": buy_price,
                        "触发原因": "MA多头向上发散，回落至自适应参考线差异5%内",
                        "参考均线周期": int(signals["reference_period"].at[date, code]),
                        "股价相对参考均线": signals["price_reference_gap"].at[date, code],
                    }
                )

        market_value = sum(item.value for item in holdings.values())
        nav = cash + market_value
        daily_rows.append(
            {
                "日期": date,
                "策略净值": nav,
                "现金": cash,
                "持仓市值": market_value,
                "总仓位": market_value / nav if nav > 0 else 0.0,
                "持股数": len(holdings),
                "当日买入数": sum(
                    row["日期"] == date and row["动作"] == "买入" for row in trade_rows
                ),
                "当日卖出数": sum(
                    row["日期"] == date and row["动作"] == "卖出" for row in trade_rows
                ),
            }
        )
        previous_date = date

    latest_date = dates[-1]
    holding_rows = []
    final_nav = daily_rows[-1]["策略净值"]
    for code, holding in holdings.items():
        latest_close = close.at[latest_date, code]
        holding_rows.append(
            {
                "截止日": latest_date,
                "代码": code,
                "名称": names.get(code, code),
                "买入日期": holding.entry_date,
                "买入价": holding.entry_price,
                "最新价": latest_close,
                "持仓市值": holding.value,
                "组合权重": holding.value / final_nav if final_nav > 0 else 0.0,
                "浮动收益": latest_close / holding.entry_price - 1,
                "参考均线周期": int(signals["reference_period"].at[latest_date, code]),
                "参考均线值": signals["reference_ma"].at[latest_date, code],
                "股价相对参考均线": signals["price_reference_gap"].at[latest_date, code],
                "止损价": holding.entry_price * (1 - STOP_LOSS_RATE),
            }
        )
    return pd.DataFrame(daily_rows), pd.DataFrame(trade_rows), pd.DataFrame(holding_rows)


def build_metrics(daily: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    nav = daily.set_index("日期")["策略净值"]
    returns = nav.pct_change().dropna()
    drawdown = nav / nav.cummax() - 1
    years = max((nav.index[-1] - nav.index[0]).days / 365.25, 1 / 365.25)
    annual_return = nav.iloc[-1] ** (1 / years) - 1
    annual_volatility = returns.std(ddof=1) * math.sqrt(252) if len(returns) > 1 else np.nan
    sharpe = annual_return / annual_volatility if annual_volatility and annual_volatility > 0 else np.nan
    sells = trades.loc[trades["动作"].eq("卖出")].copy() if not trades.empty else pd.DataFrame()
    win_rate = sells["单笔净收益"].gt(0).mean() if not sells.empty else np.nan
    return pd.DataFrame(
        [
            {
                "开始日": nav.index[0],
                "结束日": nav.index[-1],
                "累计收益": nav.iloc[-1] - 1,
                "年化收益": annual_return,
                "最大回撤": drawdown.min(),
                "年化波动率": annual_volatility,
                "夏普比率（无风险利率=0）": sharpe,
                "平均仓位": daily["总仓位"].mean(),
                "买入次数": int(trades["动作"].eq("买入").sum()) if not trades.empty else 0,
                "已完成交易数": len(sells),
                "已完成交易胜率": win_rate,
            }
        ]
    )


def save_excel(
    output_path: Path,
    metrics: pd.DataFrame,
    daily: pd.DataFrame,
    trades: pd.DataFrame,
    holdings: pd.DataFrame,
    latest_buys: pd.DataFrame,
    latest_ma_sells: pd.DataFrame,
    parameters: pd.DataFrame,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        metrics.to_excel(writer, sheet_name="策略指标", index=False)
        parameters.to_excel(writer, sheet_name="策略参数", index=False)
        latest_buys.to_excel(writer, sheet_name="最新买入信号", index=False)
        latest_ma_sells.to_excel(writer, sheet_name="最新参考线40%卖出", index=False)
        holdings.to_excel(writer, sheet_name="当前持仓", index=False)
        trades.to_excel(writer, sheet_name="交易记录", index=False)
        daily.to_excel(writer, sheet_name="每日净值", index=False)

        percent_headers = {
            "累计收益", "年化收益", "最大回撤", "年化波动率", "平均仓位",
            "已完成交易胜率", "总仓位", "交易成本率", "单笔净收益", "浮动收益",
            "组合权重", "20日年化波动率", "股价相对参考均线", "MA20相对MA60",
            "MA60相对MA120", "趋势强度",
        }
        for sheet in writer.book.worksheets:
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions
            for cell in sheet[1]:
                cell.font = cell.font.copy(bold=True)
            header_map = {cell.value: cell.column for cell in sheet[1]}
            for header, column_index in header_map.items():
                if header in percent_headers:
                    for column in sheet.iter_cols(
                        min_col=column_index,
                        max_col=column_index,
                        min_row=2,
                        max_row=sheet.max_row,
                    ):
                        for cell in column:
                            cell.number_format = "0.00%"
            for column_cells in sheet.columns:
                width = max(
                    len(str(cell.value)) if cell.value is not None else 0
                    for cell in column_cells
                )
                sheet.column_dimensions[column_cells[0].column_letter].width = min(max(width + 2, 11), 30)


def main() -> None:
    args = parse_args()
    start_date, end_date = validate_args(args)
    data_start = start_date - pd.Timedelta(days=CALENDAR_LOOKBACK_DAYS)

    print(f"共用数据库：{DB_PATH}")
    print(f"读取区间：{data_start.date()} ~ {end_date.date()}")
    snapshots, codes, names = load_universe_snapshots(data_start, end_date, args.code_limit)
    print(f"股票数量：{len(codes)}")
    close = load_close_matrix(codes, data_start, end_date)
    print(f"行情矩阵：{close.shape[0]} 个交易日 x {close.shape[1]} 只股票")
    member = build_membership_matrix(snapshots, close.index, codes)
    signals = calculate_signals(close, member)

    daily, trades, holdings = run_backtest(
        close=close,
        member=member,
        signals=signals,
        names=names,
        start_date=start_date,
        max_holdings=args.max_holdings,
    )
    metrics = build_metrics(daily, trades)
    latest_date = daily["日期"].iloc[-1]
    latest_buy_codes = signals["buy"].columns[signals["buy"].loc[latest_date]].tolist()
    latest_sell_codes = signals["ma_sell"].columns[
        signals["ma_sell"].loc[latest_date] & member.loc[latest_date]
    ].tolist()
    latest_buy_codes = rank_buy_candidates(latest_date, latest_buy_codes, signals)
    latest_buys = signal_details(
        latest_date, latest_buy_codes, close, signals, names, "买入"
    )
    latest_ma_sells = signal_details(
        latest_date, latest_sell_codes, close, signals, names, "自适应参考线之上40%卖出"
    )
    parameters = pd.DataFrame(
        [
            ("选股范围", UNIVERSE_NAME),
            ("建仓开始日", start_date.strftime("%Y-%m-%d")),
            ("回测截止日", latest_date.strftime("%Y-%m-%d")),
            ("最大持股数", args.max_holdings),
            ("低波动上限（低于则优先MA20）", LOW_VOLATILITY_THRESHOLD),
            ("高波动下限（高于则优先MA120）", HIGH_VOLATILITY_THRESHOLD),
            ("均线向上及发散回看交易日", TREND_DIRECTION_LOOKBACK),
            ("斜率稳定度回看交易日", SLOPE_STABILITY_LOOKBACK),
            ("最低斜率稳定度", MIN_SLOPE_STABILITY),
            ("买入参考线最大绝对差异", BUY_REFERENCE_DISTANCE),
            ("卖出参考线正向差异", SELL_REFERENCE_PREMIUM),
            ("固定止损", STOP_LOSS_RATE),
            ("单边交易成本", TRANSACTION_COST_RATE),
            ("交易价格假设", "信号当日收盘价"),
        ],
        columns=["参数", "参数值"],
    )

    print("\n【回测指标】")
    print(metrics.to_string(index=False))
    print(f"\n最新买入信号：{len(latest_buys)}")
    if not latest_buys.empty:
        print(
            latest_buys[
                ["代码", "名称", "收盘价", "参考均线周期", "股价相对参考均线"]
            ].head(30).to_string(index=False)
        )
    print(f"最新参考均线之上 40% 卖出信号：{len(latest_ma_sells)}")

    if not args.no_excel:
        output_path = OUTPUT_DIR / f"MA多头发散回调_全A股_{latest_date:%Y-%m-%d}.xlsx"
        save_excel(
            output_path,
            metrics,
            daily,
            trades,
            holdings,
            latest_buys,
            latest_ma_sells,
            parameters,
        )
        print(f"\n输出文件：{output_path}")


if __name__ == "__main__":
    main()
