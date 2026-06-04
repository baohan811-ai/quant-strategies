import argparse
import os
import sqlite3
from datetime import datetime

import pandas as pd
from WindPy import w

from local_market_db import MARKET_DB_PATH, init_market_db


DEFAULT_UNIVERSE_NAME = "中证800"
DEFAULT_SECTOR_ID = "1000011893000000"
DEFAULT_START_DATE = "2018-01-01"


def normalize_date(value):
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def get_trading_days(start_date, end_date, calendar=""):
    options = f"TradingCalendar={calendar}" if calendar else ""
    data = w.tdays(start_date, end_date, options)
    if data.ErrorCode != 0 or not data.Data or len(data.Data[0]) == 0:
        raise RuntimeError(f"交易日历拉取失败: ErrorCode={data.ErrorCode}")
    return pd.DatetimeIndex(pd.to_datetime(data.Data[0])).sort_values()


def get_snapshot_dates(start_date, end_date, frequency, calendar=""):
    trading_days = get_trading_days(start_date, end_date, calendar)
    if frequency == "D":
        return [normalize_date(date) for date in trading_days]
    if frequency == "M":
        dates = (
            pd.Series(trading_days, index=trading_days)
            .groupby(trading_days.to_period("M"))
            .last()
            .tolist()
        )
        return [normalize_date(date) for date in dates]
    raise ValueError(f"不支持的频率: {frequency}")


def get_field_index(fields, candidates):
    field_map = {str(field).lower(): idx for idx, field in enumerate(fields)}
    for candidate in candidates:
        idx = field_map.get(candidate.lower())
        if idx is not None:
            return idx
    raise ValueError(f"未找到字段 {candidates}，当前返回字段为: {fields}")


def fetch_constituents(sector_id, snapshot_date):
    data = w.wset("sectorconstituent", f"date={snapshot_date};sectorid={sector_id}")
    if data.ErrorCode != 0 or not data.Data:
        raise RuntimeError(
            f"成分股拉取失败: date={snapshot_date}, sector_id={sector_id}, "
            f"ErrorCode={data.ErrorCode}"
        )
    code_idx = get_field_index(data.Fields, ["wind_code", "sec_code", "ticker"])
    name_idx = get_field_index(data.Fields, ["sec_name", "security_name", "name"])
    return pd.DataFrame({
        "snapshot_date": snapshot_date,
        "wind_code": data.Data[code_idx],
        "sec_name": data.Data[name_idx],
    })


def snapshot_exists(conn, universe_name, snapshot_date):
    row = conn.execute("""
        SELECT COUNT(*)
        FROM universe_constituents_snapshot
        WHERE universe_name = ?
          AND snapshot_date = ?
    """, (universe_name, snapshot_date)).fetchone()
    return bool(row and row[0] > 0)


def save_snapshot(conn, universe_name, sector_id, snapshot_df):
    updated_at = datetime.now().isoformat(timespec="seconds")
    rows = [
        (
            row.snapshot_date,
            universe_name,
            sector_id,
            row.wind_code,
            row.sec_name,
            updated_at,
        )
        for row in snapshot_df.itertuples(index=False)
    ]
    conn.executemany("""
        INSERT INTO universe_constituents_snapshot (
            snapshot_date, universe_name, sector_id, wind_code, sec_name, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(snapshot_date, universe_name, wind_code) DO UPDATE SET
            sector_id = excluded.sector_id,
            sec_name = excluded.sec_name,
            updated_at = excluded.updated_at
    """, rows)
    conn.commit()
    return len(rows)


def summarize(conn, universe_name):
    df = pd.read_sql_query("""
        SELECT
            snapshot_date,
            COUNT(*) AS constituent_count
        FROM universe_constituents_snapshot
        WHERE universe_name = ?
        GROUP BY snapshot_date
        ORDER BY snapshot_date
    """, conn, params=[universe_name])
    if df.empty:
        print("未写入任何快照。")
        return
    print(
        f"{universe_name} 快照汇总："
        f"{len(df)} 个日期，{df['snapshot_date'].iloc[0]} ~ {df['snapshot_date'].iloc[-1]}，"
        f"单期成分数范围 {int(df['constituent_count'].min())}~{int(df['constituent_count'].max())}"
    )


def main():
    parser = argparse.ArgumentParser(description="抓取股票池/指数历史成分快照")
    parser.add_argument("--db-path", default=MARKET_DB_PATH)
    parser.add_argument("--universe-name", default=DEFAULT_UNIVERSE_NAME)
    parser.add_argument("--sector-id", default=DEFAULT_SECTOR_ID)
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument("--end-date", default=datetime.today().strftime("%Y-%m-%d"))
    parser.add_argument("--frequency", choices=["M", "D"], default="M")
    parser.add_argument("--calendar", default="")
    parser.add_argument("--force", action="store_true", help="重新拉取并覆盖已有快照")
    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.db_path), exist_ok=True)
    init_market_db(args.db_path)

    w.start()
    conn = sqlite3.connect(args.db_path)
    try:
        snapshot_dates = get_snapshot_dates(
            args.start_date,
            args.end_date,
            args.frequency,
            args.calendar,
        )
        print(
            f"准备抓取 {args.universe_name} 成分快照："
            f"{len(snapshot_dates)} 个日期，{snapshot_dates[0]} ~ {snapshot_dates[-1]}，"
            f"frequency={args.frequency}"
        )
        written_dates = 0
        skipped_dates = 0
        written_rows = 0
        for snapshot_date in snapshot_dates:
            if not args.force and snapshot_exists(conn, args.universe_name, snapshot_date):
                skipped_dates += 1
                continue
            print(f"拉取 {args.universe_name} 成分：{snapshot_date}")
            snapshot_df = fetch_constituents(args.sector_id, snapshot_date)
            written_rows += save_snapshot(
                conn,
                args.universe_name,
                args.sector_id,
                snapshot_df,
            )
            written_dates += 1
        print(f"写入日期 {written_dates} 个，跳过已有 {skipped_dates} 个，写入行 {written_rows} 行")
        summarize(conn, args.universe_name)
    finally:
        conn.close()
        w.close()


if __name__ == "__main__":
    main()
