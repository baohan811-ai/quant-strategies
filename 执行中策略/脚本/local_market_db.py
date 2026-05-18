import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta

import pandas as pd


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
CACHE_DIR = os.path.join(BASE_DIR, "缓存")

MARKET_DB_PATH = os.path.join(CACHE_DIR, "本地行情数据库.sqlite3")
A_SHARE_FUNDAMENTAL_DB_PATH = os.path.join(CACHE_DIR, "全部A股_基础基本面.sqlite3")

PRICE_FIELDS = {"open", "high", "low", "close", "volume", "amt", "turn"}
FUNDAMENTAL_FIELDS = {"pe_ttm", "pb_lf", "roe_ttm", "debt_to_assets"}
A_SHARE_SECTOR_ID = "a001010100000000"
DEFAULT_DAILY_PRICE_FIELDS = ["open", "high", "low", "close", "volume", "amt"]
COMPLETE_EOD_PRICE_FIELDS = ["open", "high", "low", "close", "volume", "amt"]
WIND_LEVEL1_INDUSTRY_SYSTEM = "wind_level1"


def connect(db_path=MARKET_DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_market_db(db_path=MARKET_DB_PATH):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_prices (
                trade_date TEXT NOT NULL,
                wind_code TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                amt REAL,
                turn REAL,
                adjusted TEXT NOT NULL DEFAULT 'F',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (trade_date, wind_code, adjusted)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_universe (
                universe_name TEXT NOT NULL,
                wind_code TEXT NOT NULL,
                sec_name TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (universe_name, wind_code)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_industry (
                classification_system TEXT NOT NULL,
                wind_code TEXT NOT NULL,
                sec_name TEXT,
                industry_level1 TEXT,
                source_field TEXT NOT NULL,
                source_options TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (classification_system, wind_code)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fetch_batches (
                dataset TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                field_key TEXT NOT NULL,
                batch_start INTEGER NOT NULL,
                batch_end INTEGER NOT NULL,
                status TEXT NOT NULL,
                non_null_count INTEGER NOT NULL DEFAULT 0,
                error_code INTEGER,
                error_message TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (dataset, trade_date, field_key, batch_start, batch_end)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_daily_prices_code_date
            ON daily_prices (wind_code, trade_date)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_fetch_batches_status
            ON fetch_batches (status)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_stock_industry_level1
            ON stock_industry (classification_system, industry_level1)
        """)
        _set_metadata(conn, "schema_version", "2")
        conn.commit()


def get_latest_price_date(db_path=MARKET_DB_PATH, adjusted="F"):
    if not os.path.exists(db_path):
        return None
    complete_conditions = " AND ".join(
        f"{field} IS NOT NULL" for field in COMPLETE_EOD_PRICE_FIELDS
    )
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(f"""
            SELECT MAX(trade_date)
            FROM daily_prices
            WHERE adjusted = ?
              AND {complete_conditions}
        """, (adjusted,)).fetchone()
    return row[0] if row and row[0] else None


def get_recent_wind_trading_dates(wind_client, end_date):
    query_end = pd.Timestamp(end_date)
    query_start = query_end - pd.Timedelta(days=30)
    data = wind_client.tdays(
        query_start.strftime("%Y-%m-%d"),
        query_end.strftime("%Y-%m-%d"),
        "",
    )
    if data.ErrorCode != 0 or not data.Data or len(data.Data[0]) == 0:
        raise RuntimeError(f"交易日历拉取失败: ErrorCode={data.ErrorCode}")
    return [
        pd.Timestamp(date).strftime("%Y-%m-%d")
        for date in data.Data[0]
    ]


def get_latest_wind_trading_date(wind_client, end_date):
    return get_recent_wind_trading_dates(wind_client, end_date)[-1]


def ensure_market_data_updated(
    wind_client,
    end_date,
    db_path=MARKET_DB_PATH,
    universe_name="全部A股",
    sector_id=A_SHARE_SECTOR_ID,
    refresh_days=15,
    price_fields=None,
):
    recent_trading_dates = get_recent_wind_trading_dates(wind_client, end_date)
    latest_trading_date = recent_trading_dates[-1]
    latest_complete_target_date = (
        recent_trading_dates[-2]
        if len(recent_trading_dates) >= 2
        else latest_trading_date
    )
    latest_price_date = get_latest_price_date(db_path)
    if latest_price_date is not None and latest_price_date >= latest_complete_target_date:
        print(f"本地行情数据库已是最新：{latest_price_date}")
        if latest_price_date < latest_trading_date:
            print(f"最新交易日 {latest_trading_date} 由策略使用 wsq 实时行情临时补齐。")
        return latest_trading_date

    print(
        "本地行情数据库需要更新："
        f"当前={latest_price_date or '无数据'}，目标={latest_complete_target_date}"
    )
    if latest_price_date is not None:
        update_start_date = (
            pd.Timestamp(latest_price_date) + pd.Timedelta(days=1)
        ).strftime("%Y-%m-%d")
    else:
        update_start_date = (
            pd.Timestamp(latest_complete_target_date) - pd.Timedelta(days=refresh_days - 1)
        ).strftime("%Y-%m-%d")

    script_path = os.path.join(SCRIPT_DIR, "update_local_market_db.py")
    cmd = [
        sys.executable,
        script_path,
        "--db-path",
        db_path,
        "--prices-from-wind",
        "--universe-name",
        universe_name,
        "--sector-id",
        sector_id,
        "--end-date",
        latest_complete_target_date,
        "--start-date",
        update_start_date,
        "--price-fields",
        *(price_fields or DEFAULT_DAILY_PRICE_FIELDS),
    ]
    subprocess.run(cmd, check=True)

    updated_price_date = get_latest_price_date(db_path)
    if updated_price_date is None or updated_price_date < latest_complete_target_date:
        print(
            "本地行情数据库更新后仍未到最新完整交易日，"
            "将由策略尝试使用 wsq 实时行情补齐："
            f"当前={updated_price_date or '无数据'}，目标={latest_complete_target_date}"
        )
        return latest_trading_date
    print(f"本地行情数据库更新完成：{updated_price_date}")
    return latest_trading_date


def _set_metadata(conn, key, value):
    updated_at = datetime.now().isoformat(timespec="seconds")
    conn.execute("""
        INSERT INTO metadata (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
    """, (key, str(value), updated_at))


def get_metadata(conn, key):
    row = conn.execute(
        "SELECT value FROM metadata WHERE key = ?",
        (key,),
    ).fetchone()
    return row[0] if row else None


def _placeholders(values):
    return ",".join("?" for _ in values)


def _normalize_dates(df):
    if df.empty:
        return df
    df.index = pd.to_datetime(df.index)
    return df.sort_index()


def _read_matrix_from_long_table(
    db_path,
    table,
    field,
    codes=None,
    start_date=None,
    end_date=None,
    extra_where=None,
    extra_params=None,
):
    conditions = [f"{field} IS NOT NULL"]
    params = []
    if start_date is not None:
        conditions.append("trade_date >= ?")
        params.append(pd.Timestamp(start_date).strftime("%Y-%m-%d"))
    if end_date is not None:
        conditions.append("trade_date <= ?")
        params.append(pd.Timestamp(end_date).strftime("%Y-%m-%d"))
    if codes:
        conditions.append(f"wind_code IN ({_placeholders(codes)})")
        params.extend(codes)
    if extra_where:
        conditions.append(extra_where)
        params.extend(extra_params or [])

    query = f"""
        SELECT trade_date, wind_code, {field} AS value
        FROM {table}
        WHERE {' AND '.join(conditions)}
        ORDER BY trade_date, wind_code
    """
    with sqlite3.connect(db_path) as conn:
        raw = pd.read_sql_query(query, conn, params=params)

    if raw.empty:
        index = pd.DatetimeIndex([])
        columns = codes or []
        return pd.DataFrame(index=index, columns=columns, dtype=float)

    raw["trade_date"] = pd.to_datetime(raw["trade_date"])
    matrix = raw.pivot(index="trade_date", columns="wind_code", values="value")
    matrix = matrix.sort_index()
    if codes:
        matrix = matrix.reindex(columns=codes)
    return matrix


def load_fundamental_matrix(
    field,
    codes=None,
    start_date=None,
    end_date=None,
    target_index=None,
    target_columns=None,
    ffill=True,
    db_path=A_SHARE_FUNDAMENTAL_DB_PATH,
):
    if field not in FUNDAMENTAL_FIELDS:
        raise ValueError(f"不支持的基本面字段: {field}")
    codes = list(target_columns if target_columns is not None else (codes or []))
    df = _read_matrix_from_long_table(
        db_path=db_path,
        table="fundamentals",
        field=field,
        codes=codes or None,
        start_date=start_date,
        end_date=end_date,
    )
    if target_index is not None:
        df = df.reindex(pd.to_datetime(target_index))
        if ffill:
            df = df.ffill()
    if target_columns is not None:
        df = df.reindex(columns=list(target_columns))
    return _normalize_dates(df)


def load_fundamental_data(
    fields,
    codes=None,
    start_date=None,
    end_date=None,
    target_index=None,
    target_columns=None,
    ffill=True,
    db_path=A_SHARE_FUNDAMENTAL_DB_PATH,
):
    return {
        field: load_fundamental_matrix(
            field,
            codes=codes,
            start_date=start_date,
            end_date=end_date,
            target_index=target_index,
            target_columns=target_columns,
            ffill=ffill,
            db_path=db_path,
        )
        for field in fields
    }


def get_price_cache_path(cache_prefix, field):
    return os.path.join(CACHE_DIR, f"{cache_prefix}_{field}_PriceAdjF.pkl")


def load_price_matrix(
    cache_prefix,
    field,
    codes=None,
    start_date=None,
    end_date=None,
    target_index=None,
    target_columns=None,
    prefer_sqlite=True,
    fallback_pickle=True,
    require_complete_eod=True,
    adjusted="F",
    db_path=MARKET_DB_PATH,
):
    if field not in PRICE_FIELDS:
        raise ValueError(f"不支持的行情字段: {field}")

    codes = list(target_columns if target_columns is not None else (codes or []))
    df = pd.DataFrame()
    if prefer_sqlite and os.path.exists(db_path):
        extra_where_parts = ["adjusted = ?"]
        if require_complete_eod:
            extra_where_parts.extend(
                f"{complete_field} IS NOT NULL"
                for complete_field in COMPLETE_EOD_PRICE_FIELDS
            )
        df = _read_matrix_from_long_table(
            db_path=db_path,
            table="daily_prices",
            field=field,
            codes=codes or None,
            start_date=start_date,
            end_date=end_date,
            extra_where=" AND ".join(extra_where_parts),
            extra_params=[adjusted],
        )

    if df.empty and fallback_pickle:
        cache_path = get_price_cache_path(cache_prefix, field)
        if not os.path.exists(cache_path):
            return pd.DataFrame(index=pd.to_datetime(target_index or []), columns=target_columns or codes)
        df = pd.read_pickle(cache_path)
        df = df.apply(pd.to_numeric, errors="coerce")
        df.index = pd.to_datetime(df.index)
        df = df.sort_index()
        if start_date is not None:
            df = df.loc[df.index >= pd.Timestamp(start_date)]
        if end_date is not None:
            df = df.loc[df.index <= pd.Timestamp(end_date)]
        if codes:
            df = df.reindex(columns=codes)

    if target_index is not None:
        df = df.reindex(pd.to_datetime(target_index))
    if target_columns is not None:
        df = df.reindex(columns=list(target_columns))
    return _normalize_dates(df)


def load_universe_from_fundamental_db(db_path=A_SHARE_FUNDAMENTAL_DB_PATH):
    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql_query(
            "SELECT wind_code, sec_name FROM stock_universe ORDER BY wind_code",
            conn,
        )
    return df


def load_stock_industry_map(
    codes=None,
    classification_system=WIND_LEVEL1_INDUSTRY_SYSTEM,
    db_path=MARKET_DB_PATH,
):
    if not os.path.exists(db_path):
        return {}

    params = [classification_system]
    conditions = ["classification_system = ?"]
    if codes:
        codes = list(codes)
        conditions.append(f"wind_code IN ({_placeholders(codes)})")
        params.extend(codes)

    query = f"""
        SELECT wind_code, industry_level1
        FROM stock_industry
        WHERE {' AND '.join(conditions)}
    """
    with sqlite3.connect(db_path) as conn:
        table_exists = conn.execute("""
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'stock_industry'
        """).fetchone()
        if not table_exists:
            return {}
        df = pd.read_sql_query(query, conn, params=params)
    if df.empty:
        return {}
    return dict(zip(df["wind_code"], df["industry_level1"]))


def fundamental_coverage(db_path=A_SHARE_FUNDAMENTAL_DB_PATH):
    query = """
        SELECT
            COUNT(*) AS total_rows,
            COUNT(DISTINCT trade_date) AS trade_dates,
            COUNT(DISTINCT wind_code) AS wind_codes,
            MIN(trade_date) AS min_date,
            MAX(trade_date) AS max_date,
            SUM(pe_ttm IS NOT NULL) AS pe_ttm_non_null,
            SUM(pb_lf IS NOT NULL) AS pb_lf_non_null,
            SUM(roe_ttm IS NOT NULL) AS roe_ttm_non_null,
            SUM(debt_to_assets IS NOT NULL) AS debt_to_assets_non_null
        FROM fundamentals
    """
    with sqlite3.connect(db_path) as conn:
        return pd.read_sql_query(query, conn)
