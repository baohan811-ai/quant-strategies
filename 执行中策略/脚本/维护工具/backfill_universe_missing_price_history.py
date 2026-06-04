import argparse
import os
import sqlite3
from datetime import datetime, timedelta

import pandas as pd
from WindPy import w

from local_market_db import MARKET_DB_PATH, init_market_db
from update_local_market_db import parse_wsd_matrix, upsert_price_field


DEFAULT_UNIVERSE_NAME = "中证800"
DEFAULT_ADJUSTED = "F"
DEFAULT_PRICE_FIELDS = ["open", "high", "low", "close", "volume", "amt"]


def iter_date_chunks(start_date, end_date, chunk="Y"):
    chunk_start = pd.Timestamp(start_date)
    final_end = pd.Timestamp(end_date)
    while chunk_start <= final_end:
        if chunk == "Y":
            chunk_end = min(
                pd.Timestamp(year=chunk_start.year, month=12, day=31),
                final_end,
            )
        elif chunk == "Q":
            chunk_end = min(chunk_start + pd.offsets.QuarterEnd(0), final_end)
        else:
            chunk_end = final_end
        yield chunk_start.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")
        chunk_start = chunk_end + timedelta(days=1)


def get_snapshot_range(conn, universe_name):
    row = conn.execute("""
        SELECT MIN(snapshot_date), MAX(snapshot_date)
        FROM universe_constituents_snapshot
        WHERE universe_name = ?
    """, (universe_name,)).fetchone()
    if not row or row[0] is None or row[1] is None:
        raise RuntimeError(f"没有找到 {universe_name} 成分快照")
    return row[0], row[1]


def get_missing_codes(conn, universe_name, start_date, end_date, adjusted):
    complete_condition = " AND ".join(
        f"p.{field} IS NOT NULL" for field in DEFAULT_PRICE_FIELDS
    )
    return pd.read_sql_query(f"""
        WITH hist AS (
            SELECT DISTINCT wind_code, sec_name
            FROM universe_constituents_snapshot
            WHERE universe_name = ?
              AND snapshot_date >= ?
              AND snapshot_date <= ?
        ),
        covered AS (
            SELECT DISTINCT p.wind_code
            FROM daily_prices p
            JOIN hist h ON h.wind_code = p.wind_code
            WHERE p.adjusted = ?
              AND p.trade_date >= ?
              AND p.trade_date <= ?
              AND {complete_condition}
        )
        SELECT h.wind_code, h.sec_name
        FROM hist h
        LEFT JOIN covered c ON c.wind_code = h.wind_code
        WHERE c.wind_code IS NULL
        ORDER BY h.wind_code
    """, conn, params=[universe_name, start_date, end_date, adjusted, start_date, end_date])


def fetch_and_upsert(conn, codes, fields, start_date, end_date, price_option, adjusted, date_chunk):
    status_counts = {"success": 0, "failed": 0}
    field_counts = {field: 0 for field in fields}
    for chunk_start, chunk_end in iter_date_chunks(start_date, end_date, date_chunk):
        for field in fields:
            print(f"补拉 {field}: {chunk_start} ~ {chunk_end}，股票 {len(codes)} 只")
            data = w.wsd(codes, field, chunk_start, chunk_end, price_option)
            df, error_code, error_message = parse_wsd_matrix(data, field)
            if error_code != 0:
                print(f"失败: {field} {chunk_start}~{chunk_end} ErrorCode={error_code} {error_message}")
                status_counts["failed"] += 1
                continue
            non_null = upsert_price_field(conn, df, field, adjusted=adjusted)
            conn.commit()
            field_counts[field] += non_null
            status_counts["success"] += 1
            print(f"  写入 {non_null} 个非空值")
    return status_counts, field_counts


def main():
    parser = argparse.ArgumentParser(description="补拉历史成分股中本地行情完全缺失的股票")
    parser.add_argument("--db-path", default=MARKET_DB_PATH)
    parser.add_argument("--universe-name", default=DEFAULT_UNIVERSE_NAME)
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--price-fields", nargs="*", default=DEFAULT_PRICE_FIELDS)
    parser.add_argument("--price-option", default="PriceAdj=F")
    parser.add_argument("--adjusted", default=DEFAULT_ADJUSTED)
    parser.add_argument("--date-chunk", choices=["Y", "Q", "ALL"], default="Y")
    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.db_path), exist_ok=True)
    init_market_db(args.db_path)

    conn = sqlite3.connect(args.db_path)
    try:
        snapshot_start, snapshot_end = get_snapshot_range(conn, args.universe_name)
        start_date = args.start_date or snapshot_start
        end_date = args.end_date or snapshot_end
        missing_df = get_missing_codes(conn, args.universe_name, start_date, end_date, args.adjusted)
        if missing_df.empty:
            print(f"{args.universe_name} 历史成分在 {start_date} ~ {end_date} 均已有完整行情覆盖。")
            return

        codes = missing_df["wind_code"].tolist()
        print(f"准备补拉 {args.universe_name} 完全无完整行情股票：{len(codes)} 只")
        print(missing_df.to_string(index=False))
        print(f"补拉区间：{start_date} ~ {end_date}")
        print(f"字段：{', '.join(args.price_fields)}")

        w.start()
        try:
            status_counts, field_counts = fetch_and_upsert(
                conn,
                codes,
                args.price_fields,
                start_date,
                end_date,
                args.price_option,
                args.adjusted,
                args.date_chunk,
            )
        finally:
            w.close()

        print("\n补拉完成")
        print("批次状态：", status_counts)
        print("字段写入：", field_counts)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
