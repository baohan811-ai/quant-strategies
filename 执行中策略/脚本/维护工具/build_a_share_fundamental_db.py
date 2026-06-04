from WindPy import w
import os
import sqlite3
from datetime import datetime, timedelta

import pandas as pd


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
CACHE_DIR = os.path.join(BASE_DIR, "缓存")
os.makedirs(CACHE_DIR, exist_ok=True)

DB_PATH = os.path.join(CACHE_DIR, "全部A股_基础基本面.sqlite3")
SECTOR_REFRESH_DAYS = 7

SECTOR_ID = "a001010100000000"
START_DATE = "2022-01-01"
BATCH_SIZE = 200
WIND_WEEKLY_CELL_LIMIT = 5000000
WIND_WEEKLY_STRATEGY_RESERVE = 1500000
DEFAULT_WIND_CELL_BUDGET = 300000
WIND_QUOTA_ERROR_CODES = {-40521007, -40522017}

FUNDAMENTAL_FIELDS = {
    "pe_ttm": "pe_ttm",
    "pb_lf": "pb_lf",
    "ps_ttm": "ps_ttm",
    "dividend_yield": "dividendyield2",
    "roe_ttm": "roe_ttm",
    "debt_to_assets": "debttoassets",
}

DAILY_VALUATION_FIELDS = {
    "mkt_cap_ard": "mkt_cap_ard",
    "free_float_mkt_cap": "mkt_freeshares",
}
DAILY_VALUATION_BATCH_KEY = "daily_valuation"

QFA_REPORT_FIELDS = {
    "revenue_yoy_qfa": "qfa_yoysales",
    "netprofit_yoy_qfa": "qfa_yoynetprofit",
    "gross_profit_margin_qfa": "qfa_grossprofitmargin",
    "net_profit_margin_qfa": "qfa_netprofitmargin",
}
REPORT_DISCLOSURE_FIELD = "stm_issuingdate"
QFA_REPORT_BATCH_KEY = "qfa_report_actual"

def get_bool_env(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def get_wind_cell_budget():
    value = os.environ.get("WIND_CELL_BUDGET")
    if value is None:
        return DEFAULT_WIND_CELL_BUDGET
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"WIND_CELL_BUDGET 必须是整数，当前值: {value}")


class WindCellBudget:
    def __init__(self, max_cells):
        self.max_cells = max_cells
        self.used_cells = 0
        self.blocked_cells = 0
        self.blocked_count = 0

    def reserve(self, label, cells):
        if self.max_cells <= 0:
            return True
        if self.used_cells + cells > self.max_cells:
            self.blocked_cells += cells
            self.blocked_count += 1
            if self.blocked_count <= 10:
                print(
                    f"跳过 {label}: 预计 {cells} 格，"
                    f"本次已用/预算 {self.used_cells}/{self.max_cells} 格"
                )
            elif self.blocked_count == 11:
                print("后续超预算批次继续跳过，不再逐条打印")
            return False
        self.used_cells += cells
        return True


def should_retry_quota_failed_batches():
    return get_bool_env("WIND_RETRY_QUOTA_FAILED", default=False)


def normalize_date(value):
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fundamentals (
            trade_date TEXT NOT NULL,
            wind_code TEXT NOT NULL,
            pe_ttm REAL,
            pb_lf REAL,
            ps_ttm REAL,
            dividend_yield REAL,
            roe_ttm REAL,
            debt_to_assets REAL,
            revenue_yoy_qfa REAL,
            netprofit_yoy_qfa REAL,
            gross_profit_margin_qfa REAL,
            net_profit_margin_qfa REAL,
            qfa_report_period TEXT,
            qfa_announcement_date TEXT,
            qfa_available_date TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (trade_date, wind_code)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS financial_reports (
            rpt_date TEXT NOT NULL,
            wind_code TEXT NOT NULL,
            announcement_date TEXT,
            available_date TEXT,
            revenue_yoy_qfa REAL,
            netprofit_yoy_qfa REAL,
            gross_profit_margin_qfa REAL,
            net_profit_margin_qfa REAL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (rpt_date, wind_code)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_valuation (
            trade_date TEXT NOT NULL,
            wind_code TEXT NOT NULL,
            mkt_cap_ard REAL,
            free_float_mkt_cap REAL,
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
        CREATE INDEX IF NOT EXISTS idx_financial_reports_code_available
        ON financial_reports (wind_code, available_date)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_daily_valuation_code
        ON daily_valuation (wind_code)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_fetch_batches_status
        ON fetch_batches (status)
    """)
    ensure_fundamental_columns(conn)
    conn.commit()


def ensure_fundamental_columns(conn):
    existing_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(fundamentals)").fetchall()
    }
    for field_key in list(FUNDAMENTAL_FIELDS) + list(QFA_REPORT_FIELDS) + [
        "qfa_report_period",
        "qfa_announcement_date",
        "qfa_available_date",
    ]:
        if field_key in existing_columns:
            continue
        column_type = "TEXT" if field_key in {
            "qfa_report_period",
            "qfa_announcement_date",
            "qfa_available_date",
        } else "REAL"
        conn.execute(f"ALTER TABLE fundamentals ADD COLUMN {field_key} {column_type}")

    existing_report_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(financial_reports)").fetchall()
    }
    for field_key in list(QFA_REPORT_FIELDS) + ["announcement_date", "available_date"]:
        if field_key in existing_report_columns:
            continue
        column_type = "TEXT" if field_key.endswith("date") else "REAL"
        conn.execute(f"ALTER TABLE financial_reports ADD COLUMN {field_key} {column_type}")

    existing_daily_valuation_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(daily_valuation)").fetchall()
    }
    for field_key in DAILY_VALUATION_FIELDS:
        if field_key in existing_daily_valuation_columns:
            continue
        conn.execute(f"ALTER TABLE daily_valuation ADD COLUMN {field_key} REAL")


def load_stock_universe_from_db(conn, allow_stale=False):
    try:
        df = pd.read_sql_query(
            """
            SELECT wind_code, sec_name, updated_at
            FROM stock_universe
            ORDER BY wind_code
            """,
            conn,
        )
    except sqlite3.OperationalError:
        return None

    if df.empty or not {"wind_code", "sec_name", "updated_at"}.issubset(df.columns):
        return None

    latest_updated_at = pd.to_datetime(df["updated_at"], errors="coerce").max()
    if pd.isna(latest_updated_at):
        return None
    cache_age_days = (pd.Timestamp.now() - latest_updated_at).days
    if not allow_stale and cache_age_days > SECTOR_REFRESH_DAYS:
        print(f"全部A股成分股 SQLite 缓存已超过 {SECTOR_REFRESH_DAYS} 天，准备刷新")
        return None

    print(
        "全部A股成分股命中 SQLite："
        f"{len(df)} 只，updated_at={latest_updated_at.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    return df[["wind_code", "sec_name"]]


def get_sector_constituents(conn):
    cached_df = load_stock_universe_from_db(conn)
    if cached_df is not None:
        return cached_df

    print("全部A股成分股未命中 SQLite 或已过期，从 Wind 拉取")
    data = w.wset("sectorconstituent", f"sectorid={SECTOR_ID}")
    if data.ErrorCode != 0 or not data.Data:
        stale_df = load_stock_universe_from_db(conn, allow_stale=True)
        if stale_df is not None:
            print("成分股拉取失败，使用 SQLite 过期股票池兜底")
            return stale_df
        raise RuntimeError(f"全部A股成分股拉取失败: ErrorCode={data.ErrorCode}")

    code_idx = data.Fields.index("wind_code")
    name_idx = data.Fields.index("sec_name")
    df = pd.DataFrame({
        "wind_code": data.Data[code_idx],
        "sec_name": data.Data[name_idx],
    })
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


def get_quarter_report_dates(start_date, end_date):
    start_year = pd.Timestamp(start_date).year - 1
    end_ts = pd.Timestamp(end_date)
    report_dates = []
    for year in range(start_year, end_ts.year + 1):
        for month, day in [(3, 31), (6, 30), (9, 30), (12, 31)]:
            rpt_ts = pd.Timestamp(year=year, month=month, day=day)
            if rpt_ts <= end_ts:
                report_dates.append(normalize_date(rpt_ts))
    return report_dates


def get_next_trading_date(date_value, trading_days):
    if pd.isna(date_value):
        return None
    date_ts = pd.Timestamp(date_value).normalize()
    next_idx = trading_days.searchsorted(date_ts, side="right")
    if next_idx >= len(trading_days):
        return None
    return normalize_date(trading_days[next_idx])


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


def estimate_business_days(start_date, end_date):
    return len(pd.bdate_range(start_date, end_date))


def batch_status(conn, trade_date, field_key, batch_start, batch_end):
    row = conn.execute("""
        SELECT status, non_null_count, error_code
        FROM fetch_batches
        WHERE trade_date = ?
          AND field_key = ?
          AND batch_start = ?
          AND batch_end = ?
    """, (trade_date, field_key, batch_start, batch_end)).fetchone()
    if not row:
        return None, None, None
    return row[0], row[1], row[2]


def should_skip_quota_failed_batch(status, error_code, label):
    if (
        status == "failed"
        and error_code in WIND_QUOTA_ERROR_CODES
        and not should_retry_quota_failed_batches()
    ):
        print(
            f"跳过 {label}: 上次 Wind 限额/权限失败 ErrorCode={error_code}，"
            "如需重试请设置 WIND_RETRY_QUOTA_FAILED=1"
        )
        return True
    return False


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


def fetch_one_batch(conn, codes, trade_date, field_key, wind_field, batch_start, batch_end, budget=None):
    status, non_null_count, error_code = batch_status(conn, trade_date, field_key, batch_start, batch_end)
    label = f"{field_key} {trade_date} 股票 {batch_start}-{batch_end}"
    if should_skip_quota_failed_batch(status, error_code, label):
        return "quota_failed_skip", 0
    if status == "success" and non_null_count > 0:
        return "skip", 0

    batch_codes = codes[batch_start:batch_end]
    estimated_cells = len(batch_codes)
    if budget is not None and not budget.reserve(label, estimated_cells):
        return "budget_skip", 0

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

    non_null_count = int(df[field_key].notna().sum())
    if non_null_count == 0:
        print(f"空结果: {field_key} {trade_date} {batch_start}-{batch_end}，保留为待重试")
        save_batch_status(
            conn,
            trade_date,
            field_key,
            batch_start,
            batch_end,
            "empty",
            0,
            0,
            "Wind 请求成功但整批返回空值",
        )
        conn.commit()
        return "empty", 0

    upsert_fundamental_field(conn, df, field_key)
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


def parse_wsd_matrix(data, field_key):
    if data.ErrorCode != 0:
        error_message = ""
        if data.Codes and data.Codes[0] == "ErrorReport" and data.Data:
            error_message = str(data.Data[0][0])
        return pd.DataFrame(), data.ErrorCode, error_message

    if not data.Codes or not data.Data or not data.Times:
        return pd.DataFrame(), -1, "返回为空"

    if len(data.Data) == len(data.Codes):
        df = pd.DataFrame(data.Data, index=data.Codes).T
        df.index = pd.to_datetime(data.Times)
    elif len(data.Data) == len(data.Times):
        df = pd.DataFrame(data.Data, index=pd.to_datetime(data.Times), columns=data.Codes)
    elif len(data.Times) == 1 and len(data.Data) == 1:
        df = pd.DataFrame([data.Data[0]], index=pd.to_datetime(data.Times), columns=data.Codes)
    else:
        return pd.DataFrame(), -1, (
            f"日频估值返回维度异常: field={field_key}, "
            f"codes={len(data.Codes)}, times={len(data.Times)}, data_rows={len(data.Data)}"
        )

    df = df.apply(pd.to_numeric, errors="coerce")
    df = df.sort_index()
    df = df.loc[:, ~df.columns.duplicated()]
    return df, 0, ""


def upsert_daily_valuation_field(conn, df, field_key, chunk_size=100000):
    if df.empty:
        return 0

    long_df = (
        df.stack()
        .rename(field_key)
        .reset_index()
        .rename(columns={"level_0": "trade_date", "level_1": "wind_code"})
    )
    long_df = long_df[long_df[field_key].notna()]
    if long_df.empty:
        return 0

    long_df["trade_date"] = pd.to_datetime(long_df["trade_date"]).dt.strftime("%Y-%m-%d")
    long_df["updated_at"] = datetime.now().isoformat(timespec="seconds")
    rows = long_df[["trade_date", "wind_code", field_key, "updated_at"]].itertuples(index=False, name=None)

    sql = f"""
        INSERT INTO daily_valuation (
            trade_date, wind_code, {field_key}, updated_at
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(trade_date, wind_code) DO UPDATE SET
            {field_key} = excluded.{field_key},
            updated_at = excluded.updated_at
    """
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


def fetch_daily_valuation_field_batch(conn, codes, field_key, wind_field, query_start_date, query_end_date, batch_start, batch_end, budget=None):
    date_range = f"{query_start_date}~{query_end_date}"
    batch_field_key = f"{DAILY_VALUATION_BATCH_KEY}:{field_key}"
    status, non_null_count, error_code = batch_status(
        conn, date_range, batch_field_key, batch_start, batch_end
    )
    label = f"{DAILY_VALUATION_BATCH_KEY} {field_key} {date_range} 股票 {batch_start}-{batch_end}"
    if should_skip_quota_failed_batch(status, error_code, label):
        return "quota_failed_skip", 0
    if status == "success":
        return "skip", 0

    batch_codes = codes[batch_start:batch_end]
    estimated_cells = len(batch_codes) * estimate_business_days(query_start_date, query_end_date)
    if budget is not None and not budget.reserve(label, estimated_cells):
        return "budget_skip", 0

    print(
        f"拉取 {DAILY_VALUATION_BATCH_KEY} {field_key}({wind_field}) "
        f"{date_range} 股票 {batch_start}-{batch_end}"
    )
    data = w.wsd(batch_codes, wind_field, query_start_date, query_end_date, "")
    df, error_code, error_message = parse_wsd_matrix(data, field_key)

    if error_code != 0:
        print(
            f"失败: {DAILY_VALUATION_BATCH_KEY} {field_key} {date_range} {batch_start}-{batch_end} "
            f"ErrorCode={error_code} {error_message}"
        )
        save_batch_status(
            conn,
            date_range,
            batch_field_key,
            batch_start,
            batch_end,
            "failed",
            0,
            error_code,
            error_message,
        )
        conn.commit()
        return "failed", 0

    non_null_count = upsert_daily_valuation_field(conn, df, field_key)
    if non_null_count == 0:
        print(
            f"空结果: {DAILY_VALUATION_BATCH_KEY} {field_key} {date_range} "
            f"{batch_start}-{batch_end}，保留为待重试"
        )
        save_batch_status(
            conn,
            date_range,
            batch_field_key,
            batch_start,
            batch_end,
            "empty",
            0,
            0,
            "Wind 请求成功但整批返回空值",
        )
        conn.commit()
        return "empty", 0

    save_batch_status(
        conn,
        date_range,
        batch_field_key,
        batch_start,
        batch_end,
        "success",
        non_null_count,
        0,
        "",
    )
    conn.commit()
    return "success", non_null_count


def parse_report_snapshot(data, rpt_date, wind_fields, field_keys):
    if data.ErrorCode != 0:
        error_message = ""
        if data.Codes and data.Codes[0] == "ErrorReport" and data.Data:
            error_message = str(data.Data[0][0])
        return pd.DataFrame(), data.ErrorCode, error_message

    if not data.Codes or not data.Data:
        return pd.DataFrame(), -1, "返回为空"

    if len(data.Data) != len(wind_fields):
        return pd.DataFrame(), -1, (
            f"报告期返回维度异常: fields={len(wind_fields)}, "
            f"codes={len(data.Codes)}, data_rows={len(data.Data)}"
        )

    records = {
        "rpt_date": [rpt_date] * len(data.Codes),
        "wind_code": data.Codes,
    }
    for field_key, values in zip(field_keys, data.Data):
        if isinstance(values, list) and len(values) == len(data.Codes):
            records[field_key] = values
        else:
            records[field_key] = [None] * len(data.Codes)

    df = pd.DataFrame(records)
    for field_key in QFA_REPORT_FIELDS:
        df[field_key] = pd.to_numeric(df[field_key], errors="coerce")
    df["announcement_date"] = pd.to_datetime(df["announcement_date"], errors="coerce")
    return df, 0, ""


def upsert_financial_reports(conn, df, trading_days):
    if df.empty:
        return 0

    df = df.copy()
    df["announcement_date"] = df["announcement_date"].apply(
        lambda value: normalize_date(value) if pd.notna(value) else None
    )
    df["available_date"] = df["announcement_date"].apply(
        lambda value: get_next_trading_date(value, trading_days) if value is not None else None
    )

    updated_at = datetime.now().isoformat(timespec="seconds")
    rows = []
    for row in df.itertuples(index=False):
        rows.append((
            row.rpt_date,
            row.wind_code,
            row.announcement_date,
            row.available_date,
            row.revenue_yoy_qfa,
            row.netprofit_yoy_qfa,
            row.gross_profit_margin_qfa,
            row.net_profit_margin_qfa,
            updated_at,
        ))

    conn.executemany("""
        INSERT INTO financial_reports (
            rpt_date, wind_code, announcement_date, available_date,
            revenue_yoy_qfa, netprofit_yoy_qfa,
            gross_profit_margin_qfa, net_profit_margin_qfa,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(rpt_date, wind_code) DO UPDATE SET
            announcement_date = excluded.announcement_date,
            available_date = excluded.available_date,
            revenue_yoy_qfa = excluded.revenue_yoy_qfa,
            netprofit_yoy_qfa = excluded.netprofit_yoy_qfa,
            gross_profit_margin_qfa = excluded.gross_profit_margin_qfa,
            net_profit_margin_qfa = excluded.net_profit_margin_qfa,
            updated_at = excluded.updated_at
    """, rows)

    qfa_cols = list(QFA_REPORT_FIELDS)
    return int(df[qfa_cols].notna().any(axis=1).sum())


def fetch_report_batch(conn, codes, rpt_date, batch_start, batch_end, trading_days, budget=None):
    status, non_null_count, error_code = batch_status(
        conn, rpt_date, QFA_REPORT_BATCH_KEY, batch_start, batch_end
    )
    label = f"{QFA_REPORT_BATCH_KEY} {rpt_date} 股票 {batch_start}-{batch_end}"
    if should_skip_quota_failed_batch(status, error_code, label):
        return "quota_failed_skip", 0
    if status == "success" and non_null_count > 0:
        return "skip", 0

    batch_codes = codes[batch_start:batch_end]
    wind_fields = list(QFA_REPORT_FIELDS.values()) + [REPORT_DISCLOSURE_FIELD]
    field_keys = list(QFA_REPORT_FIELDS) + ["announcement_date"]
    wind_fields_text = ",".join(wind_fields)
    rpt_date_for_wind = rpt_date.replace("-", "")
    estimated_cells = len(batch_codes) * len(wind_fields)
    if budget is not None and not budget.reserve(label, estimated_cells):
        return "budget_skip", 0

    print(
        f"拉取 {QFA_REPORT_BATCH_KEY}({wind_fields_text}) "
        f"{rpt_date} 股票 {batch_start}-{batch_end}"
    )
    data = w.wss(batch_codes, wind_fields_text, f"rptDate={rpt_date_for_wind}")
    df, error_code, error_message = parse_report_snapshot(
        data, rpt_date, wind_fields, field_keys
    )

    if error_code != 0:
        print(
            f"失败: {QFA_REPORT_BATCH_KEY} {rpt_date} {batch_start}-{batch_end} "
            f"ErrorCode={error_code} {error_message}"
        )
        save_batch_status(
            conn,
            rpt_date,
            QFA_REPORT_BATCH_KEY,
            batch_start,
            batch_end,
            "failed",
            0,
            error_code,
            error_message,
        )
        conn.commit()
        return "failed", 0

    non_null_count = int(df[list(QFA_REPORT_FIELDS)].notna().any(axis=1).sum())
    if non_null_count == 0:
        print(f"空结果: {QFA_REPORT_BATCH_KEY} {rpt_date} {batch_start}-{batch_end}，保留为待重试")
        save_batch_status(
            conn,
            rpt_date,
            QFA_REPORT_BATCH_KEY,
            batch_start,
            batch_end,
            "empty",
            0,
            0,
            "Wind 请求成功但整批返回空值",
        )
        conn.commit()
        return "empty", 0

    upsert_financial_reports(conn, df, trading_days)
    save_batch_status(
        conn,
        rpt_date,
        QFA_REPORT_BATCH_KEY,
        batch_start,
        batch_end,
        "success",
        non_null_count,
        0,
        "",
    )
    conn.commit()
    return "success", non_null_count


def upsert_qfa_asof_fundamentals(conn, snapshot_dates):
    reports = pd.read_sql_query("""
        SELECT
            rpt_date,
            wind_code,
            announcement_date,
            available_date,
            revenue_yoy_qfa,
            netprofit_yoy_qfa,
            gross_profit_margin_qfa,
            net_profit_margin_qfa
        FROM financial_reports
        WHERE available_date IS NOT NULL
    """, conn)
    if reports.empty:
        print("没有可用的单季度财报实际值，跳过月度 as-of 回填")
        return 0

    reports["available_ts"] = pd.to_datetime(reports["available_date"], errors="coerce")
    reports["rpt_ts"] = pd.to_datetime(reports["rpt_date"], errors="coerce")
    reports = reports.dropna(subset=["available_ts", "rpt_ts"])
    reports = reports.sort_values(["wind_code", "available_ts", "rpt_ts"])

    snapshot_df = pd.DataFrame({
        "trade_date": snapshot_dates,
        "snapshot_ts": pd.to_datetime(snapshot_dates),
    }).sort_values("snapshot_ts")

    updated_at = datetime.now().isoformat(timespec="seconds")
    rows = []
    for code, code_reports in reports.groupby("wind_code", sort=False):
        merged = pd.merge_asof(
            snapshot_df,
            code_reports.sort_values("available_ts"),
            left_on="snapshot_ts",
            right_on="available_ts",
            direction="backward",
        )
        merged = merged[merged["rpt_date"].notna()]
        for row in merged.itertuples(index=False):
            rows.append((
                row.trade_date,
                code,
                row.revenue_yoy_qfa,
                row.netprofit_yoy_qfa,
                row.gross_profit_margin_qfa,
                row.net_profit_margin_qfa,
                row.rpt_date,
                row.announcement_date,
                row.available_date,
                updated_at,
            ))

    if not rows:
        print("没有生成单季度财报实际值月度 as-of 回填记录")
        return 0

    conn.executemany("""
        INSERT INTO fundamentals (
            trade_date, wind_code,
            revenue_yoy_qfa, netprofit_yoy_qfa,
            gross_profit_margin_qfa, net_profit_margin_qfa,
            qfa_report_period, qfa_announcement_date, qfa_available_date,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(trade_date, wind_code) DO UPDATE SET
            revenue_yoy_qfa = excluded.revenue_yoy_qfa,
            netprofit_yoy_qfa = excluded.netprofit_yoy_qfa,
            gross_profit_margin_qfa = excluded.gross_profit_margin_qfa,
            net_profit_margin_qfa = excluded.net_profit_margin_qfa,
            qfa_report_period = excluded.qfa_report_period,
            qfa_announcement_date = excluded.qfa_announcement_date,
            qfa_available_date = excluded.qfa_available_date,
            updated_at = excluded.updated_at
    """, rows)
    conn.commit()
    print(f"单季度财报实际值月度 as-of 回填完成：{len(rows)} 行")
    return len(rows)


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


def print_budget_model(budget):
    build_pool = max(WIND_WEEKLY_CELL_LIMIT - WIND_WEEKLY_STRATEGY_RESERVE, 0)
    print(
        "Wind 额度模型："
        f"周额度 {WIND_WEEKLY_CELL_LIMIT} 格，"
        f"预留日常策略/行情 {WIND_WEEKLY_STRATEGY_RESERVE} 格，"
        f"基础库回补池约 {build_pool} 格/周"
    )
    if budget.max_cells > 0:
        estimated_runs = max(build_pool // budget.max_cells, 1)
        print(
            f"本次 build 限额 {budget.max_cells} 格；"
            f"按该限额约可每周跑 {estimated_runs} 次，超出后自动跳过"
        )
    else:
        print("本次 build 限额关闭，可能一次性消耗大量 Wind 额度")


def print_run_status(status_counts, budget):
    print("\n【本次运行限额统计】")
    for status, count in sorted(status_counts.items()):
        print(f"{status}: {count}")
    if budget.max_cells > 0:
        print(
            f"本次预估已使用 Wind 格数：{budget.used_cells}/{budget.max_cells}；"
            f"超预算跳过批次：{budget.blocked_count}，约 {budget.blocked_cells} 格"
        )


def main():
    end_date = datetime.today().strftime("%Y-%m-%d")
    w.start()
    conn = sqlite3.connect(DB_PATH)
    try:
        init_db(conn)
        universe_df = get_sector_constituents(conn)
        save_stock_universe(conn, universe_df)
        codes = universe_df["wind_code"].tolist()
        budget = WindCellBudget(get_wind_cell_budget())
        status_counts = {}
        snapshot_dates = get_monthly_snapshot_dates(START_DATE, end_date)
        report_dates = get_quarter_report_dates(START_DATE, end_date)
        report_trading_days = get_trading_days(f"{pd.Timestamp(START_DATE).year - 1}-01-01", end_date)

        print(f"数据库文件：{DB_PATH}")
        print(f"股票数量：{len(codes)}")
        print(f"月度截面：{len(snapshot_dates)} 个，{snapshot_dates[0]} ~ {snapshot_dates[-1]}")
        print(f"财报报告期：{len(report_dates)} 个，{report_dates[0]} ~ {report_dates[-1]}")
        print(f"每批股票数：{BATCH_SIZE}")
        print_budget_model(budget)

        daily_valuation_chunks = list(iter_date_chunks(START_DATE, end_date, chunk="Y"))
        print(
            f"日频估值：{len(daily_valuation_chunks)} 个年度区间，"
            f"{daily_valuation_chunks[0][0]} ~ {daily_valuation_chunks[-1][1]}"
        )

        for trade_date in snapshot_dates:
            for field_key, wind_field in FUNDAMENTAL_FIELDS.items():
                for batch_start in range(0, len(codes), BATCH_SIZE):
                    batch_end = min(batch_start + BATCH_SIZE, len(codes))
                    status, _ = fetch_one_batch(
                        conn,
                        codes,
                        trade_date,
                        field_key,
                        wind_field,
                        batch_start,
                        batch_end,
                        budget,
                    )
                    status_counts[status] = status_counts.get(status, 0) + 1

        for chunk_start, chunk_end in daily_valuation_chunks:
            for field_key, wind_field in DAILY_VALUATION_FIELDS.items():
                for batch_start in range(0, len(codes), BATCH_SIZE):
                    batch_end = min(batch_start + BATCH_SIZE, len(codes))
                    status, _ = fetch_daily_valuation_field_batch(
                        conn,
                        codes,
                        field_key,
                        wind_field,
                        chunk_start,
                        chunk_end,
                        batch_start,
                        batch_end,
                        budget,
                    )
                    status_counts[status] = status_counts.get(status, 0) + 1

        for rpt_date in report_dates:
            for batch_start in range(0, len(codes), BATCH_SIZE):
                batch_end = min(batch_start + BATCH_SIZE, len(codes))
                status, _ = fetch_report_batch(
                    conn,
                    codes,
                    rpt_date,
                    batch_start,
                    batch_end,
                    report_trading_days,
                    budget,
                )
                status_counts[status] = status_counts.get(status, 0) + 1

        upsert_qfa_asof_fundamentals(conn, snapshot_dates)
        print_run_status(status_counts, budget)
        summarize_progress(conn)
        print("\n基础基本面数据库更新完成")
    finally:
        conn.close()
        w.close()


if __name__ == "__main__":
    main()
