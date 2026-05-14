from WindPy import w
import os
import sqlite3
from datetime import datetime, timedelta

import pandas as pd


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
CACHE_DIR = os.path.join(BASE_DIR, "缓存")
os.makedirs(CACHE_DIR, exist_ok=True)

DB_PATH = os.path.join(CACHE_DIR, "全部A股_基础基本面.sqlite3")
SECTOR_CACHE_PATH = os.path.join(CACHE_DIR, "全部A股_基础基本面_sector.pkl")
SECTOR_CACHE_REFRESH_DAYS = 7

SECTOR_ID = "a001010100000000"
START_DATE = "2022-01-01"
BATCH_SIZE = 200

FUNDAMENTAL_FIELDS = {
    "pe_ttm": "pe_ttm",
    "pb_lf": "pb_lf",
    "roe_ttm": "roe_ttm",
    "debt_to_assets": "debttoassets",
}

REFETCH_EMPTY_SUCCESS_FIELDS = {"roe_ttm", "debt_to_assets"}


def normalize_date(value):
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fundamentals (
            trade_date TEXT NOT NULL,
            wind_code TEXT NOT NULL,
            pe_ttm REAL,
            pb_lf REAL,
            roe_ttm REAL,
            debt_to_assets REAL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (trade_date, wind_code)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fetch_batches (
            trade_date TEXT NOT NULL,
            field_key TEXT NOT NULL,
            batch_start INTEGER NOT NULL,
            batch_end INTEGER NOT NULL,
            status TEXT NOT NULL,
            non_null_count INTEGER NOT NULL DEFAULT 0,
            error_code INTEGER,
            error_message TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (trade_date, field_key, batch_start, batch_end)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_universe (
            wind_code TEXT PRIMARY KEY,
            sec_name TEXT,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_fundamentals_code
        ON fundamentals (wind_code)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_fetch_batches_status
        ON fetch_batches (status)
    """)
    conn.commit()


def load_cached_sector():
    if not os.path.exists(SECTOR_CACHE_PATH):
        return None
    cache_mtime = datetime.fromtimestamp(os.path.getmtime(SECTOR_CACHE_PATH))
    if (datetime.now() - cache_mtime).days > SECTOR_CACHE_REFRESH_DAYS:
        return None
    df = pd.read_pickle(SECTOR_CACHE_PATH)
    if df.empty or not {"wind_code", "sec_name"}.issubset(df.columns):
        return None
    print(f"全部A股成分股命中缓存：{SECTOR_CACHE_PATH}")
    return df


def get_sector_constituents():
    cached_df = load_cached_sector()
    if cached_df is not None:
        return cached_df

    print("全部A股成分股未命中缓存，从 Wind 拉取")
    data = w.wset("sectorconstituent", f"sectorid={SECTOR_ID}")
    if data.ErrorCode != 0 or not data.Data:
        if os.path.exists(SECTOR_CACHE_PATH):
            print("成分股拉取失败，使用过期缓存")
            return pd.read_pickle(SECTOR_CACHE_PATH)
        raise RuntimeError(f"全部A股成分股拉取失败: ErrorCode={data.ErrorCode}")

    code_idx = data.Fields.index("wind_code")
    name_idx = data.Fields.index("sec_name")
    df = pd.DataFrame({
        "wind_code": data.Data[code_idx],
        "sec_name": data.Data[name_idx],
    })
    df.to_pickle(SECTOR_CACHE_PATH)
    return df


def save_stock_universe(conn, universe_df):
    updated_at = datetime.now().isoformat(timespec="seconds")
    rows = [
        (row.wind_code, row.sec_name, updated_at)
        for row in universe_df.itertuples(index=False)
    ]
    conn.executemany("""
        INSERT INTO stock_universe (wind_code, sec_name, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(wind_code) DO UPDATE SET
            sec_name = excluded.sec_name,
            updated_at = excluded.updated_at
    """, rows)
    conn.commit()


def get_trading_days(start_date, end_date):
    data = w.tdays(start_date, end_date, "")
    if data.ErrorCode != 0 or not data.Data:
        raise RuntimeError(f"交易日历拉取失败: ErrorCode={data.ErrorCode}")
    return pd.DatetimeIndex(pd.to_datetime(data.Data[0])).sort_values()


def get_monthly_snapshot_dates(start_date, end_date):
    trading_days = get_trading_days(start_date, end_date)
    if len(trading_days) == 0:
        return []
    snapshot_dates = (
        pd.Series(trading_days, index=trading_days)
        .groupby(trading_days.to_period("M"))
        .first()
        .tolist()
    )
    return [normalize_date(date) for date in snapshot_dates]


def batch_status(conn, trade_date, field_key, batch_start, batch_end):
    row = conn.execute("""
        SELECT status, non_null_count
        FROM fetch_batches
        WHERE trade_date = ?
          AND field_key = ?
          AND batch_start = ?
          AND batch_end = ?
    """, (trade_date, field_key, batch_start, batch_end)).fetchone()
    if not row:
        return None, None
    return row[0], row[1]


def parse_single_field_snapshot(data, trade_date, field_key):
    if data.ErrorCode != 0:
        error_message = ""
        if data.Codes and data.Codes[0] == "ErrorReport" and data.Data:
            error_message = str(data.Data[0][0])
        return pd.DataFrame(), data.ErrorCode, error_message

    if not data.Codes or not data.Data:
        return pd.DataFrame(), -1, "返回为空"

    if len(data.Data) == len(data.Codes):
        values = [
            row[0] if isinstance(row, list) and len(row) > 0 else row
            for row in data.Data
        ]
        df = pd.DataFrame({
            "trade_date": trade_date,
            "wind_code": data.Codes,
            field_key: values,
        })
    elif len(data.Data) == 1 and len(data.Data[0]) == len(data.Codes):
        df = pd.DataFrame({
            "trade_date": trade_date,
            "wind_code": data.Codes,
            field_key: data.Data[0],
        })
    else:
        return pd.DataFrame(), -1, (
            f"返回维度异常: codes={len(data.Codes)}, "
            f"times={len(data.Times)}, data_rows={len(data.Data)}"
        )

    df[field_key] = pd.to_numeric(df[field_key], errors="coerce")
    return df, 0, ""


def upsert_fundamental_field(conn, df, field_key):
    if df.empty:
        return 0

    updated_at = datetime.now().isoformat(timespec="seconds")
    rows = [
        (
            row.trade_date,
            row.wind_code,
            getattr(row, field_key),
            updated_at,
        )
        for row in df.itertuples(index=False)
    ]
    conn.executemany(f"""
        INSERT INTO fundamentals (trade_date, wind_code, {field_key}, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(trade_date, wind_code) DO UPDATE SET
            {field_key} = excluded.{field_key},
            updated_at = excluded.updated_at
    """, rows)
    return int(df[field_key].notna().sum())


def save_batch_status(conn, trade_date, field_key, batch_start, batch_end, status, non_null_count, error_code=None, error_message=""):
    updated_at = datetime.now().isoformat(timespec="seconds")
    conn.execute("""
        INSERT INTO fetch_batches (
            trade_date, field_key, batch_start, batch_end,
            status, non_null_count, error_code, error_message, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(trade_date, field_key, batch_start, batch_end) DO UPDATE SET
            status = excluded.status,
            non_null_count = excluded.non_null_count,
            error_code = excluded.error_code,
            error_message = excluded.error_message,
            updated_at = excluded.updated_at
    """, (
        trade_date,
        field_key,
        batch_start,
        batch_end,
        status,
        non_null_count,
        error_code,
        error_message,
        updated_at,
    ))


def fetch_one_batch(conn, codes, trade_date, field_key, wind_field, batch_start, batch_end):
    status, non_null_count = batch_status(conn, trade_date, field_key, batch_start, batch_end)
    should_refetch_empty_success = (
        status == "success"
        and non_null_count == 0
        and field_key in REFETCH_EMPTY_SUCCESS_FIELDS
    )
    if status == "success" and not should_refetch_empty_success:
        return "skip", 0

    batch_codes = codes[batch_start:batch_end]
    print(
        f"拉取 {field_key}({wind_field}) "
        f"{trade_date} 股票 {batch_start}-{batch_end}"
    )
    data = w.wss(batch_codes, wind_field, f"tradeDate={trade_date}")
    df, error_code, error_message = parse_single_field_snapshot(data, trade_date, field_key)

    if error_code != 0:
        print(f"失败: {field_key} {trade_date} {batch_start}-{batch_end} ErrorCode={error_code} {error_message}")
        save_batch_status(
            conn,
            trade_date,
            field_key,
            batch_start,
            batch_end,
            "failed",
            0,
            error_code,
            error_message,
        )
        conn.commit()
        return "failed", 0

    non_null_count = upsert_fundamental_field(conn, df, field_key)
    save_batch_status(
        conn,
        trade_date,
        field_key,
        batch_start,
        batch_end,
        "success",
        non_null_count,
        0,
        "",
    )
    conn.commit()
    return "success", non_null_count


def summarize_progress(conn):
    summary = pd.read_sql_query("""
        SELECT
            field_key,
            COUNT(*) AS batch_count,
            SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_batches,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_batches,
            SUM(non_null_count) AS non_null_count
        FROM fetch_batches
        GROUP BY field_key
        ORDER BY field_key
    """, conn)
    if not summary.empty:
        print("\n【当前数据库抓取进度】")
        print(summary.to_string(index=False))


def main():
    end_date = datetime.today().strftime("%Y-%m-%d")
    w.start()
    conn = sqlite3.connect(DB_PATH)
    try:
        init_db(conn)
        universe_df = get_sector_constituents()
        save_stock_universe(conn, universe_df)
        codes = universe_df["wind_code"].tolist()
        snapshot_dates = get_monthly_snapshot_dates(START_DATE, end_date)

        print(f"数据库文件：{DB_PATH}")
        print(f"股票数量：{len(codes)}")
        print(f"月度截面：{len(snapshot_dates)} 个，{snapshot_dates[0]} ~ {snapshot_dates[-1]}")
        print(f"每批股票数：{BATCH_SIZE}")

        for trade_date in snapshot_dates:
            for field_key, wind_field in FUNDAMENTAL_FIELDS.items():
                for batch_start in range(0, len(codes), BATCH_SIZE):
                    batch_end = min(batch_start + BATCH_SIZE, len(codes))
                    fetch_one_batch(
                        conn,
                        codes,
                        trade_date,
                        field_key,
                        wind_field,
                        batch_start,
                        batch_end,
                    )

        summarize_progress(conn)
        print("\n基础基本面数据库更新完成")
    finally:
        conn.close()
        w.close()


if __name__ == "__main__":
    main()
