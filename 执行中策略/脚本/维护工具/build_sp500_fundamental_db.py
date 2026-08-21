"""构建标普500行业比较评分所需的最小基本面 SQLite 数据库。

口径：
- 49个月度截面：PE、PS、股息率、ROE、资产负债率；
- 过去约4年的季度财务历史，按NYSE季末可得信息口径拉取；
- 最新总市值截面。

注意：Wind当前对样本美股的 pb_lf/pb_mrq 和 mkt_freeshares 返回空，
因此保留数据库字段但不消耗额度批量请求；评分时市值用总市值兜底。
"""

from __future__ import annotations

import argparse
import sqlite3
from collections import Counter
from pathlib import Path

import pandas as pd
from WindPy import w

import build_a_share_fundamental_db as base


SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent.parent
DB_PATH = BASE_DIR / "缓存" / "标普500_基础基本面.sqlite3"
MARKET_DB_PATH = BASE_DIR / "缓存" / "本地行情数据库.sqlite3"
UNIVERSE_NAME = "标普500"
BATCH_SIZE = 200
DEFAULT_CELL_BUDGET = 220_000

MONTHLY_FIELDS = {
    "pe_ttm": "pe_ttm",
    "ps_ttm": "ps_ttm",
    "dividend_yield": "dividendyield2",
    "roe_ttm": "roe_ttm",
    "debt_to_assets": "debttoassets",
}

QUARTERLY_FIELDS = {
    "revenue_yoy_qfa": "yoy_or",
    "netprofit_yoy_qfa": "yoynetprofit",
    "gross_profit_margin_qfa": "grossprofitmargin",
    "net_profit_margin_qfa": "netprofitmargin",
    "announcement_date": "stm_issuingdate",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建标普500评分必要基本面库")
    parser.add_argument("--as-of", help="数据截止日，默认本地标普500最新完整行情日")
    parser.add_argument("--months", type=int, default=48, help="行业历史回看月数")
    parser.add_argument("--cell-budget", type=int, default=DEFAULT_CELL_BUDGET)
    parser.add_argument("--db-path", type=Path, default=DB_PATH)
    parser.add_argument("--skip-price-backfill", action="store_true")
    parser.add_argument(
        "--universe-mode",
        choices=["current", "historical-union", "historical-only"],
        default="current",
        help="current=当前代码池；historical-union=历史成分并集；historical-only=已调出历史成分",
    )
    return parser.parse_args()


def load_universe_and_eod(
    as_of: str | None, universe_mode: str = "current"
) -> tuple[pd.DataFrame, str]:
    with sqlite3.connect(MARKET_DB_PATH) as conn:
        if universe_mode == "current":
            universe = pd.read_sql_query(
                """
                SELECT wind_code, sec_name
                FROM stock_universe
                WHERE universe_name = ?
                ORDER BY wind_code
                """,
                conn,
                params=[UNIVERSE_NAME],
            )
        else:
            universe = pd.read_sql_query(
                """
                WITH ranked AS (
                    SELECT wind_code, sec_name,
                           ROW_NUMBER() OVER (
                               PARTITION BY wind_code ORDER BY snapshot_date DESC
                           ) AS rn
                    FROM universe_constituents_snapshot
                    WHERE universe_name = ? AND snapshot_date <= ?
                )
                SELECT wind_code, sec_name
                FROM ranked
                WHERE rn = 1
                ORDER BY wind_code
                """,
                conn,
                params=[UNIVERSE_NAME, as_of or "9999-12-31"],
            )
            if universe_mode == "historical-only":
                current_codes = pd.read_sql_query(
                    "SELECT wind_code FROM stock_universe WHERE universe_name = ?",
                    conn,
                    params=[UNIVERSE_NAME],
                )["wind_code"]
                universe = universe[~universe["wind_code"].isin(set(current_codes))]
        universe = universe.drop_duplicates("wind_code", keep="last")
        latest_eod = conn.execute(
            """
            SELECT MAX(p.trade_date)
            FROM daily_prices p
            JOIN stock_universe u ON u.wind_code = p.wind_code
            WHERE u.universe_name = ? AND p.adjusted = 'F_NYSE'
              AND p.close IS NOT NULL
            """,
            [UNIVERSE_NAME],
        ).fetchone()[0]
    if universe.empty:
        raise RuntimeError("本地行情库没有标普500代码池")
    if latest_eod is None and as_of is None:
        raise RuntimeError("本地行情库没有标普500有效收盘价")
    end_date = base.normalize_date(as_of or latest_eod)
    return universe, end_date


def nyse_trading_days(start_date: str, end_date: str) -> pd.DatetimeIndex:
    data = w.tdays(start_date, end_date, "TradingCalendar=NYSE")
    if data.ErrorCode != 0 or not data.Data:
        raise RuntimeError(f"NYSE交易日历拉取失败: ErrorCode={data.ErrorCode}")
    return pd.DatetimeIndex(pd.to_datetime(data.Data[0])).sort_values()


def monthly_snapshot_dates(start_date: str, end_date: str) -> list[str]:
    days = nyse_trading_days(start_date, end_date)
    dates = pd.Series(days, index=days).groupby(days.to_period("M")).first().tolist()
    return [base.normalize_date(value) for value in dates]


def to_date(value) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    parsed = pd.to_datetime(text, format="%Y%m%d", errors="coerce")
    if pd.isna(parsed):
        parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else parsed.strftime("%Y-%m-%d")


def fetch_quarterly_batch(
    conn: sqlite3.Connection,
    codes: list[str],
    start_date: str,
    end_date: str,
    batch_start: int,
    batch_end: int,
    trading_days: pd.DatetimeIndex,
    budget: base.WindCellBudget,
) -> tuple[str, int]:
    field_key = "us_quarterly_asof"
    range_key = f"{start_date}~{end_date}"
    status, non_null_count, _ = base.batch_status(
        conn, range_key, field_key, batch_start, batch_end
    )
    if status == "success" and non_null_count > 0:
        return "skip", 0

    batch_codes = codes[batch_start:batch_end]
    expected_periods = len(pd.date_range(start_date, end_date, freq="QE")) + 1
    estimated_cells = len(batch_codes) * expected_periods * len(QUARTERLY_FIELDS)
    label = f"美股季度as-of {batch_start}-{batch_end}"
    if not budget.reserve(label, estimated_cells):
        return "budget_skip", 0

    matrices: dict[str, tuple[list[pd.Timestamp], dict[str, list]]] = {}
    for db_field, wind_field in QUARTERLY_FIELDS.items():
        data = w.wsd(
            batch_codes,
            wind_field,
            start_date,
            end_date,
            "Period=Q;TradingCalendar=NYSE",
        )
        if data.ErrorCode != 0 or not data.Times or not data.Data:
            message = f"{wind_field} ErrorCode={data.ErrorCode}"
            base.save_batch_status(
                conn, range_key, field_key, batch_start, batch_end,
                "failed", 0, data.ErrorCode, message,
            )
            conn.commit()
            print(f"失败: {field_key} {batch_start}-{batch_end} {message}")
            return "failed", 0
        times = [pd.Timestamp(value) for value in data.Times]
        code_values = {
            code: values for code, values in zip(data.Codes, data.Data)
        }
        matrices[db_field] = (times, code_values)

    base_times = matrices["announcement_date"][0]
    rows = []
    for code in batch_codes:
        for idx, observation_ts in enumerate(base_times):
            values = {}
            for db_field, (_, by_code) in matrices.items():
                series = by_code.get(code, [])
                values[db_field] = series[idx] if idx < len(series) else None
            announcement = to_date(values["announcement_date"])
            if announcement is None:
                continue
            rows.append({
                "rpt_date": observation_ts.strftime("%Y-%m-%d"),
                "wind_code": code,
                "announcement_date": announcement,
                "revenue_yoy_qfa": values["revenue_yoy_qfa"],
                "netprofit_yoy_qfa": values["netprofit_yoy_qfa"],
                "gross_profit_margin_qfa": values["gross_profit_margin_qfa"],
                "net_profit_margin_qfa": values["net_profit_margin_qfa"],
            })
    frame = pd.DataFrame(rows)
    if frame.empty:
        base.save_batch_status(
            conn, range_key, field_key, batch_start, batch_end,
            "empty", 0, 0, "Wind季度序列无有效披露日",
        )
        conn.commit()
        return "empty", 0
    inserted = base.upsert_financial_reports(conn, frame, trading_days)
    base.save_batch_status(
        conn, range_key, field_key, batch_start, batch_end,
        "success", inserted, 0, "",
    )
    conn.commit()
    return "success", inserted


def print_completion(conn: sqlite3.Connection, codes: list[str]) -> None:
    print("\n【标普500基本面库完成度】")
    queries = [
        ("fundamentals", "trade_date"),
        ("financial_reports", "rpt_date"),
        ("daily_valuation", "trade_date"),
    ]
    for table, date_column in queries:
        row = conn.execute(
            f"SELECT MIN({date_column}), MAX({date_column}), "
            f"COUNT(DISTINCT {date_column}), COUNT(DISTINCT wind_code), COUNT(*) FROM {table}"
        ).fetchone()
        print(
            f"{table}: {row[0]} ~ {row[1]}，截面/期数={row[2]}，"
            f"标的={row[3]}/{len(codes)}，行数={row[4]}"
        )
    batches = conn.execute(
        "SELECT status, COUNT(*), COALESCE(SUM(non_null_count),0) "
        "FROM fetch_batches GROUP BY status ORDER BY status"
    ).fetchall()
    for status, count, non_null in batches:
        print(f"批次 {status}: {count}，非空={non_null}")
    latest = conn.execute(
        """
        SELECT trade_date, COUNT(*),
               SUM(pe_ttm IS NOT NULL), SUM(ps_ttm IS NOT NULL),
               SUM(dividend_yield IS NOT NULL), SUM(roe_ttm IS NOT NULL),
               SUM(debt_to_assets IS NOT NULL), SUM(revenue_yoy_qfa IS NOT NULL),
               SUM(netprofit_yoy_qfa IS NOT NULL)
        FROM fundamentals
        GROUP BY trade_date ORDER BY trade_date DESC LIMIT 1
        """
    ).fetchone()
    print(
        "最新月度截面: "
        f"{latest[0]}，行={latest[1]}，PE={latest[2]}，PS={latest[3]}，"
        f"股息率={latest[4]}，ROE={latest[5]}，资产负债率={latest[6]}，"
        f"营收增速={latest[7]}，净利增速={latest[8]}"
    )
    print("已知缺口: PB和自由流通市值在Wind样本探测中全空，本轮不请求。")


def backfill_momentum_prices(
    codes: list[str], end_date: str, budget: base.WindCellBudget
) -> tuple[str, int, str, str]:
    target_12m = pd.Timestamp(end_date) - pd.DateOffset(months=12)
    required_start = (target_12m - pd.Timedelta(days=20)).strftime("%Y-%m-%d")
    with sqlite3.connect(MARKET_DB_PATH) as conn:
        first_existing = conn.execute(
            """
            SELECT MIN(p.trade_date)
            FROM daily_prices p
            JOIN stock_universe u ON u.wind_code = p.wind_code
            WHERE u.universe_name = ? AND p.adjusted = 'F_NYSE'
              AND p.close IS NOT NULL
            """,
            [UNIVERSE_NAME],
        ).fetchone()[0]
        if first_existing is None:
            required_end = target_12m.strftime("%Y-%m-%d")
        else:
            required_end = (
                pd.Timestamp(first_existing) - pd.Timedelta(days=1)
            ).strftime("%Y-%m-%d")
        existing_codes = conn.execute(
            """
            SELECT COUNT(DISTINCT p.wind_code)
            FROM daily_prices p
            JOIN stock_universe u ON u.wind_code = p.wind_code
            WHERE u.universe_name = ? AND p.adjusted = 'F_NYSE'
              AND p.trade_date BETWEEN ? AND ? AND p.close IS NOT NULL
            """,
            [UNIVERSE_NAME, required_start, required_end],
        ).fetchone()[0]
        if required_end < required_start or existing_codes >= len(codes):
            return "skip", 0, required_start, required_end

        estimated_days = max(len(pd.bdate_range(required_start, required_end)), 1)
        estimated_cells = estimated_days * len(codes)
        if not budget.reserve("标普500 12月动量收盘价补缺", estimated_cells):
            return "budget_skip", 0, required_start, required_end

        updated_at = pd.Timestamp.now().isoformat(timespec="seconds")
        inserted = 0
        for batch_start in range(0, len(codes), 500):
            batch_codes = codes[batch_start:batch_start + 500]
            data = w.wsd(
                batch_codes, "close", required_start, required_end,
                "PriceAdj=F;TradingCalendar=NYSE",
            )
            if data.ErrorCode != 0 or not data.Times or not data.Data:
                raise RuntimeError(
                    f"收盘价补缺失败 {batch_start}-{batch_start + len(batch_codes)}: "
                    f"ErrorCode={data.ErrorCode}"
                )
            rows = []
            dates = [base.normalize_date(value) for value in data.Times]
            for code, values in zip(data.Codes, data.Data):
                for trade_date, close in zip(dates, values):
                    if close is not None and not pd.isna(close):
                        rows.append((trade_date, code, float(close), "F_NYSE", updated_at))
            conn.executemany(
                """
                INSERT INTO daily_prices (trade_date, wind_code, close, adjusted, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(trade_date, wind_code, adjusted) DO UPDATE SET
                    close = excluded.close,
                    updated_at = excluded.updated_at
                """,
                rows,
            )
            conn.commit()
            inserted += len(rows)
        return "success", inserted, required_start, required_end


def main() -> None:
    args = parse_args()
    universe, end_date = load_universe_and_eod(args.as_of, args.universe_mode)
    codes = universe["wind_code"].tolist()
    start_date = (pd.Timestamp(end_date) - pd.DateOffset(months=args.months)).strftime("%Y-%m-%d")
    quarter_start = (pd.Timestamp(start_date) - pd.DateOffset(months=3)).strftime("%Y-%m-%d")

    base.DB_PATH = str(args.db_path)
    base.FUNDAMENTAL_FIELDS = MONTHLY_FIELDS
    base.DAILY_VALUATION_FIELDS = {"mkt_cap_ard": "mkt_cap_ard"}
    args.db_path.parent.mkdir(parents=True, exist_ok=True)
    budget = base.WindCellBudget(args.cell_budget)
    status_counts: Counter[str] = Counter()

    start_result = w.start()
    if start_result.ErrorCode != 0:
        raise RuntimeError(f"Wind启动失败: ErrorCode={start_result.ErrorCode}")
    conn = sqlite3.connect(args.db_path)
    try:
        base.init_db(conn)
        base.save_stock_universe(conn, universe)
        snapshots = monthly_snapshot_dates(start_date, end_date)
        report_days = nyse_trading_days(
            (pd.Timestamp(quarter_start) - pd.DateOffset(years=1)).strftime("%Y-%m-%d"),
            (pd.Timestamp(end_date) + pd.DateOffset(years=1)).strftime("%Y-%m-%d"),
        )
        expected_monthly = len(snapshots) * len(codes) * len(MONTHLY_FIELDS)
        expected_quarterly = 17 * len(codes) * len(QUARTERLY_FIELDS)
        expected_cap = len(codes)
        print(
            f"标普500必要建库: mode={args.universe_mode}，{len(codes)}只，"
            f"{start_date}~{end_date}，"
            f"预计上限约 {expected_monthly + expected_quarterly + expected_cap:,} 格"
        )

        for trade_date in snapshots:
            for batch_start in range(0, len(codes), BATCH_SIZE):
                batch_end = min(batch_start + BATCH_SIZE, len(codes))
                status, _ = base.fetch_warmup_fundamental_batch(
                    conn, codes, trade_date, batch_start, batch_end, budget
                )
                status_counts[f"monthly_{status}"] += 1

        for batch_start in range(0, len(codes), BATCH_SIZE):
            batch_end = min(batch_start + BATCH_SIZE, len(codes))
            status, _ = fetch_quarterly_batch(
                conn, codes, quarter_start, end_date,
                batch_start, batch_end, report_days, budget,
            )
            status_counts[f"quarterly_{status}"] += 1

        base.upsert_qfa_asof_fundamentals(conn, snapshots)

        for batch_start in range(0, len(codes), BATCH_SIZE):
            batch_end = min(batch_start + BATCH_SIZE, len(codes))
            status, _ = base.fetch_daily_valuation_field_batch(
                conn, codes, "mkt_cap_ard", "mkt_cap_ard",
                end_date, end_date, batch_start, batch_end, budget,
            )
            status_counts[f"market_cap_{status}"] += 1

        if not args.skip_price_backfill:
            price_status, price_rows, price_start, price_end = backfill_momentum_prices(
                codes, end_date, budget
            )
            status_counts[f"momentum_price_{price_status}"] += 1
            print(
                f"12月动量收盘价: {price_start}~{price_end}，"
                f"状态={price_status}，写入={price_rows}行"
            )

        print("\n【本次批次】")
        for key, value in sorted(status_counts.items()):
            print(f"{key}: {value}")
        print(f"Wind预算使用: {budget.used_cells:,}/{budget.max_cells:,} 格")
        print_completion(conn, codes)
        print(f"数据库: {args.db_path}")
    finally:
        conn.close()
        w.close()


if __name__ == "__main__":
    main()
