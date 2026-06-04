import argparse
import os
import sqlite3

import pandas as pd

from local_market_db import MARKET_DB_PATH


DEFAULT_UNIVERSE_NAME = "中证800"
DEFAULT_ADJUSTED = "F"
PRICE_FIELDS = ["open", "high", "low", "close", "volume", "amt"]


def get_history_constituents(conn, universe_name, start_date=None, end_date=None):
    conditions = ["universe_name = ?"]
    params = [universe_name]
    if start_date:
        conditions.append("snapshot_date >= ?")
        params.append(pd.Timestamp(start_date).strftime("%Y-%m-%d"))
    if end_date:
        conditions.append("snapshot_date <= ?")
        params.append(pd.Timestamp(end_date).strftime("%Y-%m-%d"))

    return pd.read_sql_query(f"""
        SELECT DISTINCT wind_code, sec_name
        FROM universe_constituents_snapshot
        WHERE {' AND '.join(conditions)}
        ORDER BY wind_code
    """, conn, params=params)


def get_snapshot_summary(conn, universe_name, start_date=None, end_date=None):
    conditions = ["universe_name = ?"]
    params = [universe_name]
    if start_date:
        conditions.append("snapshot_date >= ?")
        params.append(pd.Timestamp(start_date).strftime("%Y-%m-%d"))
    if end_date:
        conditions.append("snapshot_date <= ?")
        params.append(pd.Timestamp(end_date).strftime("%Y-%m-%d"))

    return pd.read_sql_query(f"""
        SELECT snapshot_date, COUNT(*) AS constituent_count
        FROM universe_constituents_snapshot
        WHERE {' AND '.join(conditions)}
        GROUP BY snapshot_date
        ORDER BY snapshot_date
    """, conn, params=params)


def get_price_coverage(conn, codes, start_date, end_date, adjusted):
    if not codes:
        return pd.DataFrame()

    placeholders = ",".join("?" for _ in codes)
    complete_condition = " AND ".join(f"{field} IS NOT NULL" for field in PRICE_FIELDS)
    params = [adjusted, pd.Timestamp(start_date).strftime("%Y-%m-%d"), pd.Timestamp(end_date).strftime("%Y-%m-%d"), *codes]
    return pd.read_sql_query(f"""
        SELECT
            wind_code,
            MIN(trade_date) AS first_price_date,
            MAX(trade_date) AS last_price_date,
            COUNT(*) AS complete_days
        FROM daily_prices
        WHERE adjusted = ?
          AND trade_date >= ?
          AND trade_date <= ?
          AND wind_code IN ({placeholders})
          AND {complete_condition}
        GROUP BY wind_code
    """, conn, params=params)


def get_daily_coverage(conn, codes, start_date, end_date, adjusted):
    if not codes:
        return pd.DataFrame()

    placeholders = ",".join("?" for _ in codes)
    complete_condition = " AND ".join(f"{field} IS NOT NULL" for field in PRICE_FIELDS)
    params = [adjusted, pd.Timestamp(start_date).strftime("%Y-%m-%d"), pd.Timestamp(end_date).strftime("%Y-%m-%d"), *codes]
    return pd.read_sql_query(f"""
        SELECT
            trade_date,
            COUNT(*) AS complete_codes
        FROM daily_prices
        WHERE adjusted = ?
          AND trade_date >= ?
          AND trade_date <= ?
          AND wind_code IN ({placeholders})
          AND {complete_condition}
        GROUP BY trade_date
        ORDER BY trade_date
    """, conn, params=params)


def main():
    parser = argparse.ArgumentParser(description="检查历史股票池成分在本地行情库中的覆盖率")
    parser.add_argument("--db-path", default=MARKET_DB_PATH)
    parser.add_argument("--universe-name", default=DEFAULT_UNIVERSE_NAME)
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--adjusted", default=DEFAULT_ADJUSTED)
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db_path)
    try:
        snapshots = get_snapshot_summary(conn, args.universe_name, args.start_date, args.end_date)
        if snapshots.empty:
            raise RuntimeError(f"没有找到 {args.universe_name} 成分快照")

        start_date = args.start_date or snapshots["snapshot_date"].iloc[0]
        end_date = args.end_date or snapshots["snapshot_date"].iloc[-1]
        constituents = get_history_constituents(conn, args.universe_name, start_date, end_date)
        codes = constituents["wind_code"].tolist()
        coverage = get_price_coverage(conn, codes, start_date, end_date, args.adjusted)
        daily_coverage = get_daily_coverage(conn, codes, start_date, end_date, args.adjusted)

        merged = constituents.merge(coverage, on="wind_code", how="left")
        merged["has_any_price"] = merged["complete_days"].fillna(0).gt(0)
        missing = merged[~merged["has_any_price"]].copy()

        total_codes = len(merged)
        covered_codes = int(merged["has_any_price"].sum())
        trading_days = len(daily_coverage)
        expected_cells = total_codes * trading_days if trading_days else 0
        actual_cells = int(daily_coverage["complete_codes"].sum()) if not daily_coverage.empty else 0
        cell_coverage = actual_cells / expected_cells if expected_cells else 0

        print(f"股票池：{args.universe_name}")
        print(f"快照日期：{len(snapshots)} 个，{snapshots['snapshot_date'].iloc[0]} ~ {snapshots['snapshot_date'].iloc[-1]}")
        print(f"检查区间：{start_date} ~ {end_date}")
        print(f"历史出现股票：{total_codes} 只")
        print(f"有任意完整行情：{covered_codes} 只，占 {covered_codes / total_codes:.2%}")
        print(f"完全无完整行情：{len(missing)} 只")
        print(f"行情交易日：{trading_days} 个")
        print(f"完整行情格子覆盖：{actual_cells}/{expected_cells} = {cell_coverage:.2%}")

        if not daily_coverage.empty:
            print("\n最近10个交易日覆盖：")
            print(daily_coverage.tail(10).to_string(index=False))

        if not missing.empty:
            print("\n完全无完整行情的股票前50只：")
            print(missing[["wind_code", "sec_name"]].head(50).to_string(index=False))

        output_dir = args.output_dir
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
            safe_name = args.universe_name.replace("/", "_")
            detail_path = os.path.join(output_dir, f"{safe_name}_历史成分行情覆盖明细.csv")
            daily_path = os.path.join(output_dir, f"{safe_name}_历史成分每日行情覆盖.csv")
            merged.to_csv(detail_path, index=False, encoding="utf-8-sig")
            daily_coverage.to_csv(daily_path, index=False, encoding="utf-8-sig")
            print(f"\n明细输出：{detail_path}")
            print(f"每日覆盖输出：{daily_path}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
