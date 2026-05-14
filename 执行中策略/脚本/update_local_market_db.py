import argparse
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta

import pandas as pd

from local_market_db import (
    CACHE_DIR,
    COMPLETE_EOD_PRICE_FIELDS,
    MARKET_DB_PATH,
    PRICE_FIELDS,
    get_price_cache_path,
    init_market_db,
)


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

A_SHARE_SECTOR_ID = "a001010100000000"
DEFAULT_START_DATE = "2022-01-01"
DEFAULT_PRICE_FIELDS = ["open", "high", "low", "close", "volume", "amt"]
DEFAULT_ADJUST_CHECK_DAYS = 10
DEFAULT_ADJUST_TOLERANCE = 0.0001
DEFAULT_INCREMENTAL_REFRESH_DAYS = 15


def import_price_pickle(cache_prefix, field, db_path=MARKET_DB_PATH, chunk_size=100000, adjusted="F"):
    if field not in PRICE_FIELDS:
        raise ValueError(f"不支持的行情字段: {field}")

    cache_path = get_price_cache_path(cache_prefix, field)
    if not os.path.exists(cache_path):
        print(f"跳过，未找到 pkl：{cache_path}")
        return

    print(f"导入 {cache_prefix} {field}: {cache_path}")
    df = pd.read_pickle(cache_path)
    if df.empty:
        print("  空文件，跳过")
        return

    df = df.apply(pd.to_numeric, errors="coerce")
    df.index = pd.to_datetime(df.index)
    long_df = (
        df.stack()
        .rename(field)
        .reset_index()
        .rename(columns={"level_0": "trade_date", "level_1": "wind_code"})
    )
    long_df = long_df[long_df[field].notna()]
    long_df["trade_date"] = long_df["trade_date"].dt.strftime("%Y-%m-%d")
    long_df["adjusted"] = adjusted
    long_df["updated_at"] = datetime.now().isoformat(timespec="seconds")

    rows = long_df[["trade_date", "wind_code", field, "adjusted", "updated_at"]].itertuples(index=False, name=None)
    sql = f"""
        INSERT INTO daily_prices (trade_date, wind_code, {field}, adjusted, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(trade_date, wind_code, adjusted) DO UPDATE SET
            {field} = excluded.{field},
            updated_at = excluded.updated_at
    """

    with sqlite3.connect(db_path) as conn:
        batch = []
        total = 0
        for row in rows:
            batch.append(row)
            if len(batch) >= chunk_size:
                conn.executemany(sql, batch)
                conn.commit()
                total += len(batch)
                print(f"  已导入 {total} 行")
                batch = []
        if batch:
            conn.executemany(sql, batch)
            conn.commit()
            total += len(batch)
        print(f"  完成，导入 {total} 行")


def run_fundamental_update():
    script_path = os.path.join(SCRIPT_DIR, "build_a_share_fundamental_db.py")
    print(f"更新基础基本面：{script_path}")
    subprocess.run([sys.executable, script_path], check=True)


def save_stock_universe(conn, universe_name, universe_df):
    updated_at = datetime.now().isoformat(timespec="seconds")
    rows = [
        (universe_name, row.wind_code, row.sec_name, updated_at)
        for row in universe_df.itertuples(index=False)
    ]
    conn.executemany("""
        INSERT INTO stock_universe (universe_name, wind_code, sec_name, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(universe_name, wind_code) DO UPDATE SET
            sec_name = excluded.sec_name,
            updated_at = excluded.updated_at
    """, rows)
    conn.commit()


def get_wind_sector_constituents(w, sector_id):
    print(f"从 Wind 拉取股票池：sectorid={sector_id}")
    data = w.wset("sectorconstituent", f"sectorid={sector_id}")
    if data.ErrorCode != 0 or not data.Data:
        raise RuntimeError(f"股票池拉取失败: ErrorCode={data.ErrorCode}")
    code_idx = data.Fields.index("wind_code")
    name_idx = data.Fields.index("sec_name")
    return pd.DataFrame({
        "wind_code": data.Data[code_idx],
        "sec_name": data.Data[name_idx],
    })


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


def price_batch_status(conn, dataset, date_range, field, batch_start, batch_end):
    row = conn.execute("""
        SELECT status
        FROM fetch_batches
        WHERE dataset = ?
          AND trade_date = ?
          AND field_key = ?
          AND batch_start = ?
          AND batch_end = ?
    """, (dataset, date_range, field, batch_start, batch_end)).fetchone()
    return row[0] if row else None


def save_price_batch_status(
    conn,
    dataset,
    date_range,
    field,
    batch_start,
    batch_end,
    status,
    non_null_count,
    error_code=None,
    error_message="",
):
    updated_at = datetime.now().isoformat(timespec="seconds")
    conn.execute("""
        INSERT INTO fetch_batches (
            dataset, trade_date, field_key, batch_start, batch_end,
            status, non_null_count, error_code, error_message, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(dataset, trade_date, field_key, batch_start, batch_end) DO UPDATE SET
            status = excluded.status,
            non_null_count = excluded.non_null_count,
            error_code = excluded.error_code,
            error_message = excluded.error_message,
            updated_at = excluded.updated_at
    """, (
        dataset,
        date_range,
        field,
        batch_start,
        batch_end,
        status,
        non_null_count,
        error_code,
        error_message,
        updated_at,
    ))
    conn.commit()


def parse_wsd_matrix(data, field):
    if data.ErrorCode != 0 or len(data.Times) == 0:
        error_message = ""
        if data.Codes and data.Codes[0] == "ErrorReport" and data.Data:
            error_message = str(data.Data[0][0])
        return pd.DataFrame(), data.ErrorCode, error_message

    if len(data.Data) == len(data.Codes):
        df = pd.DataFrame(data.Data, index=data.Codes).T
        df.index = pd.to_datetime(data.Times)
    elif len(data.Data) == len(data.Times):
        df = pd.DataFrame(data.Data, index=pd.to_datetime(data.Times), columns=data.Codes)
    elif len(data.Times) == 1 and len(data.Data) == 1:
        df = pd.DataFrame([data.Data[0]], index=pd.to_datetime(data.Times), columns=data.Codes)
    else:
        return pd.DataFrame(), -1, (
            f"返回维度异常: field={field}, codes={len(data.Codes)}, "
            f"times={len(data.Times)}, data_rows={len(data.Data)}"
        )

    df = df.apply(pd.to_numeric, errors="coerce")
    df = df.sort_index()
    df = df.loc[:, ~df.columns.duplicated()]
    return df, 0, ""


def get_wsd_matrix(w, codes, field, query_start_date, query_end_date, batch_size, price_option="PriceAdj=F"):
    frames = []
    for batch_start in range(0, len(codes), batch_size):
        batch_end = min(batch_start + batch_size, len(codes))
        batch_codes = codes[batch_start:batch_end]
        print(f"拉取校验 {field}: {query_start_date}~{query_end_date} 股票 {batch_start}-{batch_end}")
        data = w.wsd(batch_codes, field, query_start_date, query_end_date, price_option)
        df, error_code, error_message = parse_wsd_matrix(data, field)
        if error_code != 0:
            print(f"校验拉取失败: {field} {batch_start}-{batch_end} ErrorCode={error_code} {error_message}")
            continue
        frames.append(df)

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, axis=1)
    df = df.sort_index()
    df = df.loc[:, ~df.columns.duplicated()]
    return df


def load_sqlite_price_matrix(conn, codes, field, start_date, end_date, adjusted="F"):
    if not codes:
        return pd.DataFrame()

    placeholders = ",".join("?" for _ in codes)
    query = f"""
        SELECT trade_date, wind_code, {field} AS value
        FROM daily_prices
        WHERE adjusted = ?
          AND trade_date >= ?
          AND trade_date <= ?
          AND wind_code IN ({placeholders})
          AND {field} IS NOT NULL
        ORDER BY trade_date, wind_code
    """
    params = [adjusted, start_date, end_date, *codes]
    raw = pd.read_sql_query(query, conn, params=params)
    if raw.empty:
        return pd.DataFrame(index=pd.DatetimeIndex([]), columns=codes, dtype=float)
    raw["trade_date"] = pd.to_datetime(raw["trade_date"])
    df = raw.pivot(index="trade_date", columns="wind_code", values="value")
    return df.reindex(columns=codes).sort_index()


def upsert_price_field(conn, df, field, adjusted="F", chunk_size=100000):
    if df.empty:
        return 0

    long_df = (
        df.stack()
        .rename(field)
        .reset_index()
        .rename(columns={"level_0": "trade_date", "level_1": "wind_code"})
    )
    long_df = long_df[long_df[field].notna()]
    if long_df.empty:
        return 0

    long_df["trade_date"] = pd.to_datetime(long_df["trade_date"]).dt.strftime("%Y-%m-%d")
    long_df["adjusted"] = adjusted
    long_df["updated_at"] = datetime.now().isoformat(timespec="seconds")

    sql = f"""
        INSERT INTO daily_prices (trade_date, wind_code, {field}, adjusted, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(trade_date, wind_code, adjusted) DO UPDATE SET
            {field} = excluded.{field},
            updated_at = excluded.updated_at
    """
    rows = long_df[["trade_date", "wind_code", field, "adjusted", "updated_at"]].itertuples(index=False, name=None)
    batch = []
    total = 0
    for row in rows:
        batch.append(row)
        if len(batch) >= chunk_size:
            conn.executemany(sql, batch)
            conn.commit()
            total += len(batch)
            batch = []
    if batch:
        conn.executemany(sql, batch)
        conn.commit()
        total += len(batch)
    return total


def cleanup_incomplete_eod_rows(conn, start_date, end_date, adjusted="F"):
    complete_conditions = " AND ".join(
        f"{field} IS NOT NULL" for field in COMPLETE_EOD_PRICE_FIELDS
    )
    incomplete_dates = [
        row[0]
        for row in conn.execute(f"""
            SELECT DISTINCT trade_date
            FROM daily_prices
            WHERE adjusted = ?
              AND trade_date >= ?
              AND trade_date <= ?
              AND NOT ({complete_conditions})
            ORDER BY trade_date
        """, (adjusted, start_date, end_date)).fetchall()
    ]
    if not incomplete_dates:
        return 0, 0

    placeholders = ",".join("?" for _ in incomplete_dates)
    deleted_rows = conn.execute(f"""
        DELETE FROM daily_prices
        WHERE adjusted = ?
          AND trade_date IN ({placeholders})
          AND NOT ({complete_conditions})
    """, (adjusted, *incomplete_dates)).rowcount

    batch_rows = conn.execute("""
        SELECT rowid, trade_date
        FROM fetch_batches
        WHERE dataset LIKE 'daily_prices:%'
    """).fetchall()
    incomplete_ts = [pd.Timestamp(date) for date in incomplete_dates]
    stale_batch_rowids = []
    for rowid, date_range in batch_rows:
        if "~" not in date_range:
            continue
        range_start, range_end = date_range.split("~", 1)
        start_ts = pd.Timestamp(range_start)
        end_ts = pd.Timestamp(range_end)
        if any(start_ts <= date <= end_ts for date in incomplete_ts):
            stale_batch_rowids.append(rowid)

    deleted_batches = 0
    if stale_batch_rowids:
        batch_placeholders = ",".join("?" for _ in stale_batch_rowids)
        deleted_batches = conn.execute(f"""
            DELETE FROM fetch_batches
            WHERE rowid IN ({batch_placeholders})
        """, stale_batch_rowids).rowcount

    conn.commit()
    print(
        "清理不完整EOD行情："
        f"日期={', '.join(incomplete_dates)}，"
        f"删除行情行={deleted_rows}，删除批次状态={deleted_batches}"
    )
    return deleted_rows, deleted_batches


def find_adjustment_drift_codes(sqlite_df, wind_df, tolerance):
    if sqlite_df.empty or wind_df.empty:
        return []

    common_index = sqlite_df.index.intersection(wind_df.index)
    common_columns = sqlite_df.columns.intersection(wind_df.columns)
    if len(common_index) == 0 or len(common_columns) == 0:
        return []

    sqlite_cmp = sqlite_df.loc[common_index, common_columns].astype(float)
    wind_cmp = wind_df.loc[common_index, common_columns].astype(float)
    diff_ratio = (wind_cmp / sqlite_cmp.replace(0, pd.NA) - 1).abs()
    drift_mask = diff_ratio.gt(tolerance).any(axis=0)
    return drift_mask[drift_mask].index.tolist()


def refresh_codes_from_wind(
    w,
    conn,
    codes,
    fields,
    start_date,
    end_date,
    batch_size,
    date_chunk,
):
    if not codes:
        return

    print(f"开始回刷疑似复权变化股票：{len(codes)} 只")
    for chunk_start, chunk_end in iter_date_chunks(start_date, end_date, date_chunk):
        for field in fields:
            for batch_start in range(0, len(codes), batch_size):
                batch_end = min(batch_start + batch_size, len(codes))
                batch_codes = codes[batch_start:batch_end]
                print(f"回刷 {field}: {chunk_start}~{chunk_end} 股票 {batch_start}-{batch_end}")
                data = w.wsd(batch_codes, field, chunk_start, chunk_end, "PriceAdj=F")
                df, error_code, error_message = parse_wsd_matrix(data, field)
                if error_code != 0:
                    print(f"回刷失败: {field} {chunk_start}~{chunk_end} ErrorCode={error_code} {error_message}")
                    continue
                non_null_count = upsert_price_field(conn, df, field)
                print(f"  写入 {non_null_count} 个非空值")


def fetch_price_batch_from_wind(
    w,
    conn,
    dataset,
    codes,
    field,
    query_start_date,
    query_end_date,
    batch_start,
    batch_end,
    price_option,
    adjusted,
):
    date_range = f"{query_start_date}~{query_end_date}"
    if price_batch_status(conn, dataset, date_range, field, batch_start, batch_end) == "success":
        return "skip", 0

    batch_codes = codes[batch_start:batch_end]
    print(f"拉取 {field}: {date_range} 股票 {batch_start}-{batch_end}")
    data = w.wsd(batch_codes, field, query_start_date, query_end_date, price_option)
    df, error_code, error_message = parse_wsd_matrix(data, field)

    if error_code != 0:
        print(f"失败: {field} {date_range} {batch_start}-{batch_end} ErrorCode={error_code} {error_message}")
        save_price_batch_status(
            conn,
            dataset,
            date_range,
            field,
            batch_start,
            batch_end,
            "failed",
            0,
            error_code,
            error_message,
        )
        return "failed", 0

    non_null_count = upsert_price_field(conn, df, field, adjusted=adjusted)
    save_price_batch_status(
        conn,
        dataset,
        date_range,
        field,
        batch_start,
        batch_end,
        "success",
        non_null_count,
        0,
        "",
    )
    return "success", non_null_count


def update_prices_from_wind(
    db_path,
    universe_name,
    sector_id,
    start_date,
    end_date,
    fields,
    batch_size,
    date_chunk,
    price_option,
    adjusted,
):
    from WindPy import w

    w.start()
    conn = sqlite3.connect(db_path)
    try:
        universe_df = get_wind_sector_constituents(w, sector_id)
        save_stock_universe(conn, universe_name, universe_df)
        codes = universe_df["wind_code"].tolist()
        dataset = f"daily_prices:{universe_name}:{adjusted}"
        print(f"股票池：{universe_name}，股票数：{len(codes)}")
        print(f"行情区间：{start_date} ~ {end_date}")
        print(f"字段：{', '.join(fields)}")
        print(f"价格口径：{price_option} / adjusted={adjusted}")
        print(f"每批股票数：{batch_size}")

        for chunk_start, chunk_end in iter_date_chunks(start_date, end_date, date_chunk):
            for field in fields:
                for batch_start in range(0, len(codes), batch_size):
                    batch_end = min(batch_start + batch_size, len(codes))
                    fetch_price_batch_from_wind(
                        w,
                        conn,
                        dataset,
                        codes,
                        field,
                        chunk_start,
                        chunk_end,
                        batch_start,
                        batch_end,
                        price_option,
                        adjusted,
                    )

        if set(COMPLETE_EOD_PRICE_FIELDS).issubset(set(fields)) and adjusted == "F":
            cleanup_incomplete_eod_rows(conn, start_date, end_date, adjusted=adjusted)
    finally:
        conn.close()
        w.close()


def smart_adjustment_refresh(
    db_path,
    universe_name,
    sector_id,
    start_date,
    end_date,
    fields,
    batch_size,
    date_chunk,
    check_days,
    tolerance,
):
    from WindPy import w

    check_start = (pd.Timestamp(end_date) - timedelta(days=check_days - 1)).strftime("%Y-%m-%d")
    w.start()
    conn = sqlite3.connect(db_path)
    try:
        universe_df = get_wind_sector_constituents(w, sector_id)
        save_stock_universe(conn, universe_name, universe_df)
        codes = universe_df["wind_code"].tolist()

        print(f"智能复权校验股票池：{universe_name}，股票数：{len(codes)}")
        print(f"校验区间：{check_start} ~ {end_date}")
        print(f"偏差阈值：{tolerance:.6f}")

        sqlite_close = load_sqlite_price_matrix(conn, codes, "close", check_start, end_date)
        wind_close = get_wsd_matrix(w, codes, "close", check_start, end_date, batch_size)
        drift_codes = find_adjustment_drift_codes(sqlite_close, wind_close, tolerance)

        if not drift_codes:
            print("未发现复权口径偏差，无需个股全历史回刷")
            return

        print(f"发现疑似复权口径变化股票 {len(drift_codes)} 只：")
        print(", ".join(drift_codes[:50]) + (" ..." if len(drift_codes) > 50 else ""))
        refresh_codes_from_wind(
            w,
            conn,
            drift_codes,
            fields,
            start_date,
            end_date,
            batch_size,
            date_chunk,
        )
    finally:
        conn.close()
        w.close()


def main():
    parser = argparse.ArgumentParser(description="初始化/更新本地行情数据库")
    parser.add_argument("--db-path", default=MARKET_DB_PATH)
    parser.add_argument("--fundamentals", action="store_true", help="运行全部A股基础基本面更新")
    parser.add_argument("--import-price-pkl", nargs="*", default=[], help="从现有 pkl 导入行情，例如：全部A股 中证800")
    parser.add_argument("--prices-from-wind", action="store_true", help="直接从 Wind 拉取日频行情写入 SQLite")
    parser.add_argument("--smart-adjust-refresh", action="store_true", help="校验最近 close 偏差，仅回刷疑似除权复权变化个股")
    parser.add_argument("--universe-name", default="全部A股")
    parser.add_argument("--sector-id", default=A_SHARE_SECTOR_ID)
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument("--end-date", default=datetime.today().strftime("%Y-%m-%d"))
    parser.add_argument("--refresh-days", type=int, help="从结束日期往前回刷最近 N 天行情，用于日常增量更新")
    parser.add_argument("--adjust-history-start-date", default=DEFAULT_START_DATE, help="发现复权偏差时，单只股票回刷历史的起始日期")
    parser.add_argument("--price-fields", nargs="*", default=DEFAULT_PRICE_FIELDS)
    parser.add_argument("--price-option", default="PriceAdj=F", help="Wind wsd 行情参数，例如 PriceAdj=F;TradingCalendar=HKEX")
    parser.add_argument("--adjusted", default="F", help="写入 daily_prices.adjusted 的行情口径标识")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--date-chunk", choices=["Y", "Q", "ALL"], default="Y")
    parser.add_argument("--check-days", type=int, default=DEFAULT_ADJUST_CHECK_DAYS)
    parser.add_argument("--adjust-tolerance", type=float, default=DEFAULT_ADJUST_TOLERANCE)
    args = parser.parse_args()
    effective_start_date = args.start_date
    if args.refresh_days is not None:
        effective_start_date = (
            pd.Timestamp(args.end_date) - timedelta(days=args.refresh_days - 1)
        ).strftime("%Y-%m-%d")

    os.makedirs(CACHE_DIR, exist_ok=True)
    init_market_db(args.db_path)
    print(f"通用行情库已初始化：{args.db_path}")

    if args.fundamentals:
        run_fundamental_update()

    for cache_prefix in args.import_price_pkl:
        for field in args.price_fields:
            import_price_pickle(cache_prefix, field, args.db_path, adjusted=args.adjusted)

    if args.prices_from_wind:
        update_prices_from_wind(
            db_path=args.db_path,
            universe_name=args.universe_name,
            sector_id=args.sector_id,
            start_date=effective_start_date,
            end_date=args.end_date,
            fields=args.price_fields,
            batch_size=args.batch_size,
            date_chunk=args.date_chunk,
            price_option=args.price_option,
            adjusted=args.adjusted,
        )

    if args.smart_adjust_refresh:
        smart_adjustment_refresh(
            db_path=args.db_path,
            universe_name=args.universe_name,
            sector_id=args.sector_id,
            start_date=args.adjust_history_start_date,
            end_date=args.end_date,
            fields=args.price_fields,
            batch_size=args.batch_size,
            date_chunk=args.date_chunk,
            check_days=args.check_days,
            tolerance=args.adjust_tolerance,
        )


if __name__ == "__main__":
    main()
