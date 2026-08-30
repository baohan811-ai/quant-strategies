import os
import sqlite3

import pandas as pd

from local_market_db import (
    A_SHARE_FUNDAMENTAL_DB_PATH,
    MARKET_DB_PATH,
    fundamental_coverage,
)


def pct(non_null, total):
    if total == 0:
        return 0.0
    return round(non_null / total * 100, 2)


def print_fundamental_check(db_path=A_SHARE_FUNDAMENTAL_DB_PATH):
    if not os.path.exists(db_path):
        print(f"基础基本面库不存在：{db_path}")
        return

    with sqlite3.connect(db_path) as conn:
        coverage = fundamental_coverage(db_path).iloc[0].to_dict()
        batch_status = pd.read_sql_query("""
            SELECT status, COUNT(*) AS batches
            FROM fetch_batches
            GROUP BY status
            ORDER BY status
        """, conn)
        by_field = pd.read_sql_query("""
            SELECT
                field_key,
                status,
                COUNT(*) AS batches,
                SUM(non_null_count) AS non_nulls,
                MIN(updated_at) AS first_update,
                MAX(updated_at) AS last_update
            FROM fetch_batches
            GROUP BY field_key, status
            ORDER BY field_key, status
        """, conn)
        latest_dates = pd.read_sql_query("""
            SELECT
                trade_date,
                COUNT(*) AS rows,
                SUM(pe_ttm IS NOT NULL) AS pe_ttm,
                SUM(pb_lf IS NOT NULL) AS pb_lf,
                SUM(roe_ttm IS NOT NULL) AS roe_ttm,
                SUM(debt_to_assets IS NOT NULL) AS debt_to_assets
            FROM fundamentals
            GROUP BY trade_date
            ORDER BY trade_date DESC
            LIMIT 12
        """, conn)

    total = int(coverage["total_rows"])
    print(f"基础基本面库：{db_path}")
    print(f"总行数：{total}")
    print(f"截面：{coverage['trade_dates']} 个，{coverage['min_date']} ~ {coverage['max_date']}")
    print(f"股票数：{coverage['wind_codes']}")
    print("字段覆盖率：")
    for field in ["pe_ttm", "pb_lf", "roe_ttm", "debt_to_assets"]:
        non_null = int(coverage[f"{field}_non_null"])
        print(f"  {field}: {non_null} / {total} = {pct(non_null, total)}%")

    print("\n批次状态：")
    print(batch_status.to_string(index=False))
    print("\n字段批次：")
    print(by_field.to_string(index=False))
    print("\n最近 12 个截面：")
    print(latest_dates.to_string(index=False))


def print_market_db_check(db_path=MARKET_DB_PATH):
    if not os.path.exists(db_path):
        print(f"\n通用行情库尚未创建：{db_path}")
        return

    with sqlite3.connect(db_path) as conn:
        tables = pd.read_sql_query("""
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            ORDER BY name
        """, conn)
        price_summary = pd.read_sql_query("""
            SELECT
                COUNT(*) AS rows,
                COUNT(DISTINCT trade_date) AS trade_dates,
                COUNT(DISTINCT wind_code) AS wind_codes,
                MIN(trade_date) AS min_date,
                MAX(trade_date) AS max_date
            FROM daily_prices
        """, conn)
        raw_summary = pd.read_sql_query("""
            SELECT
                COUNT(*) AS rows,
                COUNT(DISTINCT trade_date) AS trade_dates,
                COUNT(DISTINCT wind_code) AS wind_codes,
                MIN(trade_date) AS min_date,
                MAX(trade_date) AS max_date,
                SUM(open IS NOT NULL AND high IS NOT NULL AND low IS NOT NULL
                    AND close IS NOT NULL AND volume IS NOT NULL AND amt IS NOT NULL)
                    AS complete_eod_rows
            FROM raw_daily_prices
        """, conn)
        factor_summary = pd.read_sql_query("""
            SELECT
                COUNT(*) AS rows,
                COUNT(DISTINCT trade_date) AS trade_dates,
                COUNT(DISTINCT wind_code) AS wind_codes,
                MIN(trade_date) AS min_date,
                MAX(trade_date) AS max_date,
                SUM(adj_factor IS NULL OR adj_factor <= 0) AS invalid_rows
            FROM price_adjustment_factors
        """, conn)
        quality = pd.read_sql_query("""
            SELECT dataset, status, checked_at, details, updated_at
            FROM market_data_quality
            ORDER BY dataset
        """, conn)

    print(f"\n通用行情库：{db_path}")
    print("表：", ", ".join(tables["name"].tolist()))
    print("日线行情：")
    print(price_summary.to_string(index=False))
    print("\n权威原始行情：")
    print(raw_summary.to_string(index=False))
    print("\n复权因子：")
    print(factor_summary.to_string(index=False))
    print("\n行情质量状态：")
    print(quality.to_string(index=False))


def main():
    print_fundamental_check()
    print_market_db_check()


if __name__ == "__main__":
    main()
