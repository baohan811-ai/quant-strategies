from WindPy import w
import argparse
import os
import sqlite3
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
CACHE_DIR = os.path.join(BASE_DIR, "缓存")
os.makedirs(CACHE_DIR, exist_ok=True)

DB_PATH = os.path.join(CACHE_DIR, "全部A股_基础基本面.sqlite3")
MARKET_DB_PATH = os.path.join(CACHE_DIR, "本地行情数据库.sqlite3")
SECTOR_REFRESH_DAYS = 7

SECTOR_ID = "a001010100000000"
START_DATE = "2022-01-01"
BATCH_SIZE = 200
WIND_WEEKLY_CELL_LIMIT = 5000000
WIND_WEEKLY_STRATEGY_RESERVE = 1500000
DEFAULT_WIND_CELL_BUDGET = 300000
WIND_QUOTA_ERROR_CODES = {-40521007, -40522017}
DEFAULT_EMPTY_CONFIRM_RETRIES = 2
DAILY_VALUATION_FORCE_START_ENV = "DAILY_VALUATION_FORCE_START"

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
QUALITY_REPORT_FIELDS = {
    "roe_reported": "roe",
    "debt_to_assets_reported": "debttoassets",
}
TTM_ROE_REPORT_FIELDS = {
    "net_profit_parent_ytd": "np_belongto_parcomsh",
    "parent_equity": "eqy_belongto_parcomsh",
    "total_operating_revenue_ytd": "tot_oper_rev",
}
REPORT_DISCLOSURE_FIELD = "stm_issuingdate"
QFA_REPORT_BATCH_KEY = "qfa_report_actual"
QUALITY_REPORT_BATCH_KEY = "quality_report_actual"
TTM_ROE_REPORT_BATCH_KEY = "ttm_valuation_report_actual_v2"
MONTHLY_MARKET_FIELDS = {
    "market_cap_monthly": "mkt_cap_ard",
    "unadjusted_close_monthly": "close",
}
PIT_VALUATION_VERSION = "formal_report_asof_v1"

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
    value = os.environ.get("WIND_RETRY_QUOTA_FAILED")
    if value is not None:
        return get_bool_env("WIND_RETRY_QUOTA_FAILED", default=False)
    return os.environ.get("WIND_CELL_BUDGET") is not None


def get_empty_confirm_retries():
    value = os.environ.get("WIND_EMPTY_CONFIRM_RETRIES")
    if value is None:
        return DEFAULT_EMPTY_CONFIRM_RETRIES
    try:
        return max(1, int(value))
    except ValueError:
        raise ValueError(f"WIND_EMPTY_CONFIRM_RETRIES 必须是整数，当前值: {value}")


def get_optional_date_env(name):
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    try:
        return normalize_date(value.strip())
    except Exception as exc:
        raise ValueError(f"{name} 必须是 YYYY-MM-DD 格式，当前值: {value}") from exc


def normalize_date(value):
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def wind_wss_with_retry(codes, fields, options, attempts=4):
    for attempt in range(attempts):
        data = w.wss(codes, fields, options)
        if data.ErrorCode not in WIND_QUOTA_ERROR_CODES:
            return data
        if attempt < attempts - 1:
            wait_seconds = 2 ** (attempt + 1)
            print(
                f"Wind 暂时限流，{wait_seconds} 秒后重试 "
                f"({attempt + 1}/{attempts - 1})"
            )
            time.sleep(wait_seconds)
    return data


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
            roe_reported REAL,
            debt_to_assets_reported REAL,
            roe_ttm_calculated REAL,
            roe_ttm_report_period TEXT,
            roe_ttm_announcement_date TEXT,
            roe_ttm_available_date TEXT,
            market_cap_monthly REAL,
            unadjusted_close_monthly REAL,
            pe_ttm_calculated REAL,
            pb_lf_calculated REAL,
            ps_ttm_calculated REAL,
            dividend_yield_calculated REAL,
            valuation_report_period TEXT,
            valuation_announcement_date TEXT,
            valuation_available_date TEXT,
            valuation_calculation_version TEXT,
            dividend_event_count INTEGER,
            quality_report_period TEXT,
            quality_announcement_date TEXT,
            quality_available_date TEXT,
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
            roe_reported REAL,
            debt_to_assets_reported REAL,
            net_profit_parent_ytd REAL,
            parent_equity REAL,
            total_operating_revenue_ytd REAL,
            ttm_net_profit_parent REAL,
            ttm_total_operating_revenue REAL,
            roe_ttm_calculated REAL,
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
    for field_key in list(FUNDAMENTAL_FIELDS) + list(QFA_REPORT_FIELDS) + list(QUALITY_REPORT_FIELDS) + [
        "roe_ttm_calculated",
        *MONTHLY_MARKET_FIELDS,
        "pe_ttm_calculated",
        "pb_lf_calculated",
        "ps_ttm_calculated",
        "dividend_yield_calculated",
        "dividend_event_count",
        "valuation_calculation_version",
        "valuation_report_period",
        "valuation_announcement_date",
        "valuation_available_date",
        "qfa_report_period",
        "qfa_announcement_date",
        "qfa_available_date",
        "quality_report_period",
        "quality_announcement_date",
        "quality_available_date",
        "roe_ttm_report_period",
        "roe_ttm_announcement_date",
        "roe_ttm_available_date",
    ]:
        if field_key in existing_columns:
            continue
        column_type = "TEXT" if field_key in {
            "qfa_report_period",
            "qfa_announcement_date",
            "qfa_available_date",
            "quality_report_period",
            "quality_announcement_date",
            "quality_available_date",
            "roe_ttm_report_period",
            "roe_ttm_announcement_date",
            "roe_ttm_available_date",
            "valuation_calculation_version",
            "valuation_report_period",
            "valuation_announcement_date",
            "valuation_available_date",
        } else "REAL"
        conn.execute(f"ALTER TABLE fundamentals ADD COLUMN {field_key} {column_type}")

    existing_report_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(financial_reports)").fetchall()
    }
    for field_key in (
        list(QFA_REPORT_FIELDS)
        + list(QUALITY_REPORT_FIELDS)
        + list(TTM_ROE_REPORT_FIELDS)
        + [
            "ttm_net_profit_parent",
            "ttm_total_operating_revenue",
            "roe_ttm_calculated",
            "announcement_date",
            "available_date",
        ]
    ):
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


def load_historical_universe_union(start_date, end_date):
    """读取回测期真实出现过的股票，包含后来退市或移出当前全A的证券。"""
    if not os.path.exists(MARKET_DB_PATH):
        return pd.DataFrame(columns=["wind_code", "sec_name"])
    with sqlite3.connect(MARKET_DB_PATH) as market_conn:
        try:
            frame = pd.read_sql_query(
                """
                SELECT wind_code, MAX(sec_name) AS sec_name
                FROM universe_constituents_snapshot
                WHERE universe_name = '全部A股'
                  AND snapshot_date >= ? AND snapshot_date <= ?
                GROUP BY wind_code
                ORDER BY wind_code
                """,
                market_conn,
                params=[start_date, end_date],
            )
        except sqlite3.OperationalError:
            return pd.DataFrame(columns=["wind_code", "sec_name"])
    return frame


def merge_current_and_historical_universe(current, start_date, end_date):
    historical = load_historical_universe_union(start_date, end_date)
    if historical.empty:
        return current.drop_duplicates("wind_code", keep="last")
    combined = pd.concat([historical, current], ignore_index=True)
    combined["sec_name"] = combined["sec_name"].fillna(combined["wind_code"])
    return combined.drop_duplicates("wind_code", keep="last").sort_values("wind_code")


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
        .last()
        .tolist()
    )
    return [normalize_date(date) for date in snapshot_dates]


def get_quarter_report_dates(start_date, end_date):
    # 首个回测截面的 TTM 需要上年同季和上年年报，因此向前多取两年。
    start_year = pd.Timestamp(start_date).year - 2
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


def latest_successful_daily_valuation_end(
    conn,
    field_key,
    batch_start,
    batch_end,
    year_start,
    end_date,
):
    """返回当年该字段、该股票批次已成功抓取到的最晚日期。

    进度表的 trade_date 保存为 ``开始日~结束日``。既兼容旧的
    年度区间，也识别改造后产生的增量区间。
    """
    batch_field_key = f"{DAILY_VALUATION_BATCH_KEY}:{field_key}"
    row = conn.execute(
        """
        SELECT MAX(SUBSTR(trade_date, INSTR(trade_date, '~') + 1))
        FROM fetch_batches
        WHERE field_key = ?
          AND batch_start = ?
          AND batch_end = ?
          AND status = 'success'
          AND INSTR(trade_date, '~') > 0
          AND SUBSTR(trade_date, 1, INSTR(trade_date, '~') - 1) >= ?
          AND SUBSTR(trade_date, INSTR(trade_date, '~') + 1) <= ?
        """,
        (batch_field_key, batch_start, batch_end, year_start, end_date),
    ).fetchone()
    return row[0] if row and row[0] else None


def next_incremental_trading_date(last_success_date, year_start, trading_days):
    """返回当年日频市值批次的下一个待抓交易日。"""
    if len(trading_days) == 0:
        return None
    lower_bound = pd.Timestamp(year_start)
    if last_success_date is not None:
        lower_bound = max(lower_bound, pd.Timestamp(last_success_date) + pd.Timedelta(days=1))
    index = trading_days.searchsorted(lower_bound, side="left")
    if index >= len(trading_days):
        return None
    return normalize_date(trading_days[index])


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
            "如需重试请设置 WIND_RETRY_QUOTA_FAILED=1 或显式设置 WIND_CELL_BUDGET"
        )
        return True
    return False


def should_skip_confirmed_empty_batch(status, label):
    if status == "confirmed_empty":
        print(f"跳过 {label}: 已确认整批为空，不再重复请求 Wind")
        return True
    return False


def save_empty_or_confirmed_status(conn, trade_date, field_key, batch_start, batch_end):
    status, _, _ = batch_status(conn, trade_date, field_key, batch_start, batch_end)
    if status == "empty" or get_empty_confirm_retries() <= 1:
        next_status = "confirmed_empty"
        message = "Wind 请求连续返回整批空值，已确认无数据，后续不再重试"
    else:
        next_status = "empty"
        message = "Wind 请求成功但整批返回空值，等待下次确认"

    save_batch_status(
        conn,
        trade_date,
        field_key,
        batch_start,
        batch_end,
        next_status,
        0,
        0,
        message,
    )
    return next_status


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
    if should_skip_confirmed_empty_batch(status, label):
        return "confirmed_empty_skip", 0
    if status == "success" and non_null_count > 0:
        return "skip", 0

    batch_codes = codes[batch_start:batch_end]
    marks = ",".join("?" for _ in batch_codes)
    existing_codes = {
        row[0]
        for row in conn.execute(
            f"SELECT wind_code FROM fundamentals WHERE trade_date=? "
            f"AND {field_key} IS NOT NULL AND wind_code IN ({marks})",
            [trade_date, *batch_codes],
        ).fetchall()
    }
    query_codes = [code for code in batch_codes if code not in existing_codes]
    if not query_codes:
        save_batch_status(
            conn, trade_date, field_key, batch_start, batch_end,
            "success", len(existing_codes), 0, "本地数据已完整",
        )
        conn.commit()
        return "local_skip", len(existing_codes)
    estimated_cells = len(query_codes)
    if budget is not None and not budget.reserve(label, estimated_cells):
        return "budget_skip", 0

    print(
        f"拉取 {field_key}({wind_field}) "
        f"{trade_date} 股票 {batch_start}-{batch_end}"
    )
    data = wind_wss_with_retry(query_codes, wind_field, f"tradeDate={trade_date}")
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
        next_status = save_empty_or_confirmed_status(conn, trade_date, field_key, batch_start, batch_end)
        conn.commit()
        if next_status == "confirmed_empty":
            print(f"空结果: {field_key} {trade_date} {batch_start}-{batch_end}，已确认为空，后续不再重试")
        else:
            print(f"空结果: {field_key} {trade_date} {batch_start}-{batch_end}，等待下次确认")
        return next_status, 0

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


def upsert_monthly_market_cap_from_daily_valuation(conn, snapshot_dates):
    if not snapshot_dates:
        return 0
    marks = ",".join("?" for _ in snapshot_dates)
    rows = conn.execute(
        f"""SELECT trade_date, wind_code, mkt_cap_ard
        FROM daily_valuation
        WHERE trade_date IN ({marks}) AND mkt_cap_ard IS NOT NULL""",
        snapshot_dates,
    ).fetchall()
    if not rows:
        return 0
    updated_at = datetime.now().isoformat(timespec="seconds")
    conn.executemany(
        """INSERT INTO fundamentals
        (trade_date,wind_code,market_cap_monthly,updated_at)
        VALUES (?,?,?,?)
        ON CONFLICT(trade_date,wind_code) DO UPDATE SET
        market_cap_monthly=excluded.market_cap_monthly,
        updated_at=excluded.updated_at""",
        [(trade_date, code, value, updated_at) for trade_date, code, value in rows],
    )
    conn.commit()
    print(f"从本地日频市值复制月末截面：{len(rows)} 行")
    return len(rows)


def fetch_warmup_fundamental_batch(
    conn,
    codes,
    trade_date,
    batch_start,
    batch_end,
    budget=None,
):
    """预热专用：同一股票批次的待补月频字段一次 WSS 拉取。"""
    pending_fields = []
    for field_key, wind_field in FUNDAMENTAL_FIELDS.items():
        status, non_null_count, error_code = batch_status(
            conn, trade_date, field_key, batch_start, batch_end
        )
        label = f"{field_key} {trade_date} 股票 {batch_start}-{batch_end}"
        if should_skip_quota_failed_batch(status, error_code, label):
            continue
        if status == "confirmed_empty":
            continue
        if status == "success" and non_null_count > 0:
            continue
        pending_fields.append((field_key, wind_field))

    if not pending_fields:
        return "skip", 0

    batch_codes = codes[batch_start:batch_end]
    estimated_cells = len(batch_codes) * len(pending_fields)
    label = (
        f"预热月频基本面 {trade_date} 股票 {batch_start}-{batch_end} "
        f"字段 {len(pending_fields)} 个"
    )
    if budget is not None and not budget.reserve(label, estimated_cells):
        return "budget_skip", 0

    field_keys = [item[0] for item in pending_fields]
    wind_fields = [item[1] for item in pending_fields]
    print(
        f"拉取预热月频基本面({','.join(wind_fields)}) "
        f"{trade_date} 股票 {batch_start}-{batch_end}"
    )
    data = wind_wss_with_retry(
        batch_codes, ",".join(wind_fields), f"tradeDate={trade_date}"
    )
    if data.ErrorCode != 0 or not data.Codes or not data.Data:
        error_message = ""
        if data.Codes and data.Codes[0] == "ErrorReport" and data.Data:
            error_message = str(data.Data[0][0])
        error_code = data.ErrorCode if data.ErrorCode != 0 else -1
        for field_key in field_keys:
            save_batch_status(
                conn,
                trade_date,
                field_key,
                batch_start,
                batch_end,
                "failed",
                0,
                error_code,
                error_message or "Wind 返回为空",
            )
        conn.commit()
        print(f"失败: {label} ErrorCode={error_code} {error_message}")
        return "failed", 0

    if len(data.Data) != len(field_keys):
        error_message = (
            f"返回维度异常: fields={len(field_keys)}, "
            f"codes={len(data.Codes)}, data_rows={len(data.Data)}"
        )
        for field_key in field_keys:
            save_batch_status(
                conn,
                trade_date,
                field_key,
                batch_start,
                batch_end,
                "failed",
                0,
                -1,
                error_message,
            )
        conn.commit()
        print(f"失败: {label} {error_message}")
        return "failed", 0

    total_non_null = 0
    for field_key, values in zip(field_keys, data.Data):
        if not isinstance(values, list) or len(values) != len(data.Codes):
            values = [None] * len(data.Codes)
        frame = pd.DataFrame({
            "trade_date": trade_date,
            "wind_code": data.Codes,
            field_key: pd.to_numeric(values, errors="coerce"),
        })
        non_null_count = upsert_fundamental_field(conn, frame, field_key)
        total_non_null += non_null_count
        if non_null_count == 0:
            save_empty_or_confirmed_status(
                conn, trade_date, field_key, batch_start, batch_end
            )
        else:
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
    return "success", total_non_null


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
    if should_skip_confirmed_empty_batch(status, label):
        return "confirmed_empty_skip", 0
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
        next_status = save_empty_or_confirmed_status(
            conn,
            date_range,
            batch_field_key,
            batch_start,
            batch_end,
        )
        conn.commit()
        if next_status == "confirmed_empty":
            print(
                f"空结果: {DAILY_VALUATION_BATCH_KEY} {field_key} {date_range} "
                f"{batch_start}-{batch_end}，已确认为空，后续不再重试"
            )
        else:
            print(
                f"空结果: {DAILY_VALUATION_BATCH_KEY} {field_key} {date_range} "
                f"{batch_start}-{batch_end}，等待下次确认"
            )
        return next_status, 0

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
    for field_key in (
        list(QFA_REPORT_FIELDS)
        + list(QUALITY_REPORT_FIELDS)
        + list(TTM_ROE_REPORT_FIELDS)
    ):
        if field_key in df.columns:
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
    if should_skip_confirmed_empty_batch(status, label):
        return "confirmed_empty_skip", 0
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
    data = wind_wss_with_retry(
        batch_codes, wind_fields_text, f"rptDate={rpt_date_for_wind}"
    )
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
        next_status = save_empty_or_confirmed_status(
            conn,
            rpt_date,
            QFA_REPORT_BATCH_KEY,
            batch_start,
            batch_end,
        )
        conn.commit()
        if next_status == "confirmed_empty":
            print(f"空结果: {QFA_REPORT_BATCH_KEY} {rpt_date} {batch_start}-{batch_end}，已确认为空，后续不再重试")
        else:
            print(f"空结果: {QFA_REPORT_BATCH_KEY} {rpt_date} {batch_start}-{batch_end}，等待下次确认")
        return next_status, 0

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


def fetch_quality_report_batch(
    conn, codes, rpt_date, batch_start, batch_end, trading_days, budget=None
):
    """按报告期抓取质量指标，避免 roe_ttm/debttoassets 的 tradeDate 失真。"""
    status, non_null_count, error_code = batch_status(
        conn, rpt_date, QUALITY_REPORT_BATCH_KEY, batch_start, batch_end
    )
    label = f"{QUALITY_REPORT_BATCH_KEY} {rpt_date} 股票 {batch_start}-{batch_end}"
    if should_skip_quota_failed_batch(status, error_code, label):
        return "quota_failed_skip", 0
    if should_skip_confirmed_empty_batch(status, label):
        return "confirmed_empty_skip", 0
    if status == "success" and non_null_count > 0:
        return "skip", 0

    batch_codes = codes[batch_start:batch_end]
    wind_fields = list(QUALITY_REPORT_FIELDS.values()) + [REPORT_DISCLOSURE_FIELD]
    field_keys = list(QUALITY_REPORT_FIELDS) + ["announcement_date"]
    estimated_cells = len(batch_codes) * len(wind_fields)
    if budget is not None and not budget.reserve(label, estimated_cells):
        return "budget_skip", 0

    rpt_date_for_wind = rpt_date.replace("-", "")
    wind_fields_text = ",".join(wind_fields)
    print(
        f"拉取 {QUALITY_REPORT_BATCH_KEY}({wind_fields_text}) "
        f"{rpt_date} 股票 {batch_start}-{batch_end}"
    )
    data = wind_wss_with_retry(
        batch_codes, wind_fields_text, f"rptDate={rpt_date_for_wind}"
    )
    df, error_code, error_message = parse_report_snapshot(
        data, rpt_date, wind_fields, field_keys
    )
    if error_code != 0:
        save_batch_status(
            conn, rpt_date, QUALITY_REPORT_BATCH_KEY, batch_start, batch_end,
            "failed", 0, error_code, error_message,
        )
        conn.commit()
        print(f"失败: {label} ErrorCode={error_code} {error_message}")
        return "failed", 0

    for field_key in QUALITY_REPORT_FIELDS:
        df[field_key] = pd.to_numeric(df[field_key], errors="coerce")
    non_null_count = int(df[list(QUALITY_REPORT_FIELDS)].notna().any(axis=1).sum())
    if non_null_count == 0:
        next_status = save_empty_or_confirmed_status(
            conn, rpt_date, QUALITY_REPORT_BATCH_KEY, batch_start, batch_end
        )
        conn.commit()
        return next_status, 0

    df["announcement_date"] = df["announcement_date"].apply(
        lambda value: normalize_date(value) if pd.notna(value) else None
    )
    df["available_date"] = df["announcement_date"].apply(
        lambda value: get_next_trading_date(value, trading_days)
        if value is not None else None
    )
    updated_at = datetime.now().isoformat(timespec="seconds")
    rows = [
        (
            row.rpt_date, row.wind_code, row.announcement_date, row.available_date,
            row.roe_reported, row.debt_to_assets_reported, updated_at,
        )
        for row in df.itertuples(index=False)
    ]
    conn.executemany("""
        INSERT INTO financial_reports (
            rpt_date, wind_code, announcement_date, available_date,
            roe_reported, debt_to_assets_reported, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(rpt_date, wind_code) DO UPDATE SET
            announcement_date = COALESCE(excluded.announcement_date, financial_reports.announcement_date),
            available_date = COALESCE(excluded.available_date, financial_reports.available_date),
            roe_reported = excluded.roe_reported,
            debt_to_assets_reported = excluded.debt_to_assets_reported,
            updated_at = excluded.updated_at
    """, rows)
    save_batch_status(
        conn, rpt_date, QUALITY_REPORT_BATCH_KEY, batch_start, batch_end,
        "success", non_null_count, 0, "",
    )
    conn.commit()
    return "success", non_null_count


def fetch_ttm_roe_report_batch(
    conn, codes, rpt_date, batch_start, batch_end, trading_days, budget=None
):
    """抓取自算 ROE/PE/PB/PS 所需的累计利润、净资产和营业收入。"""
    status, non_null_count, error_code = batch_status(
        conn, rpt_date, TTM_ROE_REPORT_BATCH_KEY, batch_start, batch_end
    )
    label = f"{TTM_ROE_REPORT_BATCH_KEY} {rpt_date} 股票 {batch_start}-{batch_end}"
    if should_skip_quota_failed_batch(status, error_code, label):
        return "quota_failed_skip", 0
    if should_skip_confirmed_empty_batch(status, label):
        return "confirmed_empty_skip", 0
    if status == "success" and non_null_count > 0:
        return "skip", 0

    batch_codes = codes[batch_start:batch_end]
    wind_fields = list(TTM_ROE_REPORT_FIELDS.values()) + [REPORT_DISCLOSURE_FIELD]
    field_keys = list(TTM_ROE_REPORT_FIELDS) + ["announcement_date"]
    estimated_cells = len(batch_codes) * len(wind_fields)
    if budget is not None and not budget.reserve(label, estimated_cells):
        return "budget_skip", 0

    rpt_date_for_wind = rpt_date.replace("-", "")
    wind_fields_text = ",".join(wind_fields)
    print(
        f"拉取 {TTM_ROE_REPORT_BATCH_KEY}({wind_fields_text}) "
        f"{rpt_date} 股票 {batch_start}-{batch_end}"
    )
    data = wind_wss_with_retry(
        batch_codes,
        wind_fields_text,
        f"rptDate={rpt_date_for_wind};rptType=1;unit=1",
    )
    df, error_code, error_message = parse_report_snapshot(
        data, rpt_date, wind_fields, field_keys
    )
    if error_code != 0:
        save_batch_status(
            conn, rpt_date, TTM_ROE_REPORT_BATCH_KEY, batch_start, batch_end,
            "failed", 0, error_code, error_message,
        )
        conn.commit()
        print(f"失败: {label} ErrorCode={error_code} {error_message}")
        return "failed", 0

    for field_key in TTM_ROE_REPORT_FIELDS:
        df[field_key] = pd.to_numeric(df[field_key], errors="coerce")
    non_null_count = int(df[list(TTM_ROE_REPORT_FIELDS)].notna().any(axis=1).sum())
    if non_null_count == 0:
        next_status = save_empty_or_confirmed_status(
            conn, rpt_date, TTM_ROE_REPORT_BATCH_KEY, batch_start, batch_end
        )
        conn.commit()
        return next_status, 0

    df["announcement_date"] = df["announcement_date"].apply(
        lambda value: normalize_date(value) if pd.notna(value) else None
    )
    df["available_date"] = df["announcement_date"].apply(
        lambda value: get_next_trading_date(value, trading_days)
        if value is not None else None
    )
    updated_at = datetime.now().isoformat(timespec="seconds")
    rows = [
        (
            row.rpt_date, row.wind_code, row.announcement_date,
            row.available_date, row.net_profit_parent_ytd,
            row.parent_equity, row.total_operating_revenue_ytd, updated_at,
        )
        for row in df.itertuples(index=False)
    ]
    conn.executemany("""
        INSERT INTO financial_reports (
            rpt_date, wind_code, announcement_date, available_date,
            net_profit_parent_ytd, parent_equity,
            total_operating_revenue_ytd, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(rpt_date, wind_code) DO UPDATE SET
            announcement_date = COALESCE(excluded.announcement_date, financial_reports.announcement_date),
            available_date = COALESCE(excluded.available_date, financial_reports.available_date),
            net_profit_parent_ytd = excluded.net_profit_parent_ytd,
            parent_equity = excluded.parent_equity,
            total_operating_revenue_ytd = excluded.total_operating_revenue_ytd,
            updated_at = excluded.updated_at
    """, rows)
    save_batch_status(
        conn, rpt_date, TTM_ROE_REPORT_BATCH_KEY, batch_start, batch_end,
        "success", non_null_count, 0, "",
    )
    conn.commit()
    return "success", non_null_count


def calculate_and_upsert_ttm_roe(conn):
    """用累计报表按公告期口径计算 TTM 归母净利润、收入和 ROE。"""
    reports = pd.read_sql_query("""
        SELECT rpt_date, wind_code, net_profit_parent_ytd, parent_equity,
               total_operating_revenue_ytd
        FROM financial_reports
        WHERE net_profit_parent_ytd IS NOT NULL OR parent_equity IS NOT NULL
           OR total_operating_revenue_ytd IS NOT NULL
    """, conn)
    if reports.empty:
        print("没有可用的 TTM ROE 原始报表数据")
        return 0

    reports["rpt_ts"] = pd.to_datetime(reports["rpt_date"], errors="coerce")
    reports = reports.dropna(subset=["rpt_ts"]).copy()
    lookup = reports.set_index(["wind_code", "rpt_date"])
    updates = []
    for row in reports.itertuples(index=False):
        rpt_ts = row.rpt_ts
        prior_year = rpt_ts - pd.DateOffset(years=1)
        prior_same_date = prior_year.strftime("%Y-%m-%d")
        prior_annual_date = f"{rpt_ts.year - 1}-12-31"
        try:
            prior_same = lookup.loc[(row.wind_code, prior_same_date)]
        except KeyError:
            prior_same = None
        if rpt_ts.month == 12:
            ttm_profit = row.net_profit_parent_ytd
            ttm_revenue = row.total_operating_revenue_ytd
        else:
            try:
                prior_annual = lookup.loc[(row.wind_code, prior_annual_date)]
            except KeyError:
                prior_annual = None
            values = [
                row.net_profit_parent_ytd,
                None if prior_annual is None else prior_annual.net_profit_parent_ytd,
                None if prior_same is None else prior_same.net_profit_parent_ytd,
            ]
            ttm_profit = (
                values[0] + values[1] - values[2]
                if all(pd.notna(value) for value in values) else None
            )
            revenue_values = [
                row.total_operating_revenue_ytd,
                None if prior_annual is None else prior_annual.total_operating_revenue_ytd,
                None if prior_same is None else prior_same.total_operating_revenue_ytd,
            ]
            ttm_revenue = (
                revenue_values[0] + revenue_values[1] - revenue_values[2]
                if all(pd.notna(value) for value in revenue_values) else None
            )
        prior_equity = None if prior_same is None else prior_same.parent_equity
        average_equity = (
            (row.parent_equity + prior_equity) / 2
            if pd.notna(row.parent_equity) and pd.notna(prior_equity) else None
        )
        roe = (
            100.0 * ttm_profit / average_equity
            if pd.notna(ttm_profit) and pd.notna(average_equity) and average_equity > 0
            else None
        )
        updates.append((ttm_profit, ttm_revenue, roe, row.rpt_date, row.wind_code))

    conn.executemany("""
        UPDATE financial_reports
        SET ttm_net_profit_parent = ?,
            ttm_total_operating_revenue = ?,
            roe_ttm_calculated = ?
        WHERE rpt_date = ? AND wind_code = ?
    """, updates)
    conn.commit()
    non_null = sum(value[0] is not None for value in updates)
    revenue_non_null = sum(value[1] is not None for value in updates)
    print(
        f"自算 TTM 完成：利润 {non_null}/{len(updates)}，"
        f"营业收入 {revenue_non_null}/{len(updates)}"
    )
    return non_null


def upsert_ttm_roe_asof_fundamentals(conn, snapshot_dates):
    reports = pd.read_sql_query("""
        SELECT rpt_date, wind_code, announcement_date, available_date,
               roe_ttm_calculated
        FROM financial_reports
        WHERE available_date IS NOT NULL AND roe_ttm_calculated IS NOT NULL
    """, conn)
    if reports.empty:
        print("没有可用的自算 TTM ROE，跳过月度 as-of 回填")
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
                row.trade_date, code, row.roe_ttm_calculated, row.rpt_date,
                row.announcement_date, row.available_date, updated_at,
            ))
    conn.executemany("""
        INSERT INTO fundamentals (
            trade_date, wind_code, roe_ttm_calculated,
            roe_ttm_report_period, roe_ttm_announcement_date,
            roe_ttm_available_date, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(trade_date, wind_code) DO UPDATE SET
            roe_ttm_calculated = excluded.roe_ttm_calculated,
            roe_ttm_report_period = excluded.roe_ttm_report_period,
            roe_ttm_announcement_date = excluded.roe_ttm_announcement_date,
            roe_ttm_available_date = excluded.roe_ttm_available_date,
            updated_at = excluded.updated_at
    """, rows)
    conn.commit()
    print(f"自算 TTM ROE 月度 as-of 回填完成：{len(rows)} 行")
    return len(rows)


def load_confirmed_dividend_events():
    """只读取已实施且日期链完整的现金分红事件。"""
    if not os.path.exists(MARKET_DB_PATH):
        return pd.DataFrame(columns=[
            "wind_code", "ex_date", "cash_before_tax",
            "proposal_announcement_date", "implementation_announcement_date",
        ])
    with sqlite3.connect(MARKET_DB_PATH) as market_conn:
        columns = {
            row[1]
            for row in market_conn.execute(
                "PRAGMA table_info(corporate_action_events)"
            ).fetchall()
        }
        required = {
            "wind_code", "ex_date", "cash_before_tax",
            "proposal_announcement_date", "implementation_announcement_date",
        }
        if not required.issubset(columns):
            return pd.DataFrame(columns=sorted(required))
        events = pd.read_sql_query(
            """
            SELECT wind_code, ex_date, cash_before_tax,
                   proposal_announcement_date,
                   implementation_announcement_date
            FROM corporate_action_events
            WHERE ex_date IS NOT NULL AND cash_before_tax IS NOT NULL
              AND cash_before_tax > 0
              AND proposal_announcement_date IS NOT NULL
              AND implementation_announcement_date IS NOT NULL
              AND implementation_announcement_date <= ex_date
            """,
            market_conn,
        )
    if events.empty:
        return events
    for column in [
        "ex_date", "proposal_announcement_date", "implementation_announcement_date"
    ]:
        events[column] = pd.to_datetime(events[column], errors="coerce")
    return events.dropna(subset=[
        "ex_date", "proposal_announcement_date", "implementation_announcement_date"
    ])


def dividend_history_is_complete(snapshot_dates):
    if not snapshot_dates or not os.path.exists(MARKET_DB_PATH):
        return False
    required_start = (
        pd.Timestamp(min(snapshot_dates)) - pd.DateOffset(years=1)
    ).strftime("%Y-%m-%d")
    required_end = max(snapshot_dates)
    with sqlite3.connect(MARKET_DB_PATH) as market_conn:
        try:
            values = dict(market_conn.execute(
                """SELECT key, value FROM metadata
                WHERE key IN ('pit_dividend_complete_start_date',
                              'pit_dividend_complete_end_date')"""
            ).fetchall())
        except sqlite3.OperationalError:
            return False
    start = values.get("pit_dividend_complete_start_date")
    end = values.get("pit_dividend_complete_end_date")
    return bool(start and end and start <= required_start and end >= required_end)


def calculate_and_upsert_pit_valuations(conn, snapshot_dates):
    """用月末市值与当时已可用的正式财报重算 PE/PB/PS/股息率。"""
    market = pd.read_sql_query(
        """
        SELECT trade_date, wind_code, market_cap_monthly,
               unadjusted_close_monthly
        FROM fundamentals
        WHERE trade_date IN ({})
          AND (market_cap_monthly IS NOT NULL
               OR unadjusted_close_monthly IS NOT NULL)
        """.format(",".join("?" for _ in snapshot_dates)),
        conn,
        params=snapshot_dates,
    )
    reports = pd.read_sql_query(
        """
        SELECT rpt_date, wind_code, announcement_date, available_date,
               ttm_net_profit_parent, parent_equity,
               ttm_total_operating_revenue
        FROM financial_reports
        WHERE available_date IS NOT NULL
          AND (ttm_net_profit_parent IS NOT NULL OR parent_equity IS NOT NULL
               OR ttm_total_operating_revenue IS NOT NULL)
        """,
        conn,
    )
    if market.empty or reports.empty:
        print("月频市值或正式财报不足，跳过 PIT 估值计算")
        return 0

    market["trade_ts"] = pd.to_datetime(market["trade_date"], errors="coerce")
    reports["available_ts"] = pd.to_datetime(reports["available_date"], errors="coerce")
    reports["rpt_ts"] = pd.to_datetime(reports["rpt_date"], errors="coerce")
    reports = reports.dropna(subset=["available_ts", "rpt_ts"])
    report_groups = {
        code: group.sort_values(["available_ts", "rpt_ts"])
        for code, group in reports.groupby("wind_code", sort=False)
    }
    dividend_coverage_complete = dividend_history_is_complete(snapshot_dates)
    dividends = (
        load_confirmed_dividend_events()
        if dividend_coverage_complete else pd.DataFrame()
    )
    dividend_groups = {
        code: group.sort_values("ex_date")
        for code, group in dividends.groupby("wind_code", sort=False)
    } if not dividends.empty else {}

    pieces = []
    for code, code_market in market.groupby("wind_code", sort=False):
        code_reports = report_groups.get(code)
        if code_reports is None:
            merged = code_market.copy()
            for column in [
                "rpt_date", "announcement_date", "available_date",
                "ttm_net_profit_parent", "parent_equity",
                "ttm_total_operating_revenue",
            ]:
                merged[column] = np.nan
        else:
            merged = pd.merge_asof(
                code_market.sort_values("trade_ts"),
                code_reports[[
                    "available_ts", "rpt_date", "announcement_date",
                    "available_date", "ttm_net_profit_parent", "parent_equity",
                    "ttm_total_operating_revenue",
                ]],
                left_on="trade_ts",
                right_on="available_ts",
                direction="backward",
            )
        code_dividends = dividend_groups.get(code)
        dividend_sums = []
        dividend_counts = []
        for trade_ts in merged["trade_ts"]:
            if not dividend_coverage_complete:
                dividend_sums.append(np.nan)
                dividend_counts.append(None)
                continue
            if code_dividends is None:
                dividend_sums.append(0.0)
                dividend_counts.append(0)
                continue
            window_start = trade_ts - pd.DateOffset(years=1)
            used = code_dividends[
                (code_dividends["ex_date"] > window_start)
                & (code_dividends["ex_date"] <= trade_ts)
                & (code_dividends["implementation_announcement_date"] <= trade_ts)
            ]
            dividend_sums.append(float(used["cash_before_tax"].sum()))
            dividend_counts.append(int(len(used)))
        merged["trailing_cash_dividend_per_share"] = dividend_sums
        merged["dividend_event_count"] = dividend_counts
        pieces.append(merged)

    result = pd.concat(pieces, ignore_index=True)
    market_cap = pd.to_numeric(result["market_cap_monthly"], errors="coerce")
    close = pd.to_numeric(result["unadjusted_close_monthly"], errors="coerce")
    profit = pd.to_numeric(result["ttm_net_profit_parent"], errors="coerce")
    equity = pd.to_numeric(result["parent_equity"], errors="coerce")
    revenue = pd.to_numeric(result["ttm_total_operating_revenue"], errors="coerce")
    result["pe_ttm_calculated"] = market_cap / profit.where(profit != 0)
    result["pb_lf_calculated"] = market_cap / equity.where(equity != 0)
    result["ps_ttm_calculated"] = market_cap / revenue.where(revenue != 0)
    result["dividend_yield_calculated"] = (
        result["trailing_cash_dividend_per_share"] / close.where(close > 0)
    )
    violation_count = int(
        (
            pd.to_datetime(result["available_date"], errors="coerce")
            > result["trade_ts"]
        ).sum()
    )
    if violation_count:
        raise RuntimeError(f"PIT 估值出现可用日晚于交易日：{violation_count} 行")

    updated_at = datetime.now().isoformat(timespec="seconds")
    rows = [
        (
            row.pe_ttm_calculated, row.pb_lf_calculated,
            row.ps_ttm_calculated, row.dividend_yield_calculated,
            row.rpt_date if pd.notna(row.rpt_date) else None,
            row.announcement_date if pd.notna(row.announcement_date) else None,
            row.available_date if pd.notna(row.available_date) else None,
            PIT_VALUATION_VERSION,
            int(row.dividend_event_count)
            if pd.notna(row.dividend_event_count) else None,
            updated_at,
            row.trade_date, row.wind_code,
        )
        for row in result.itertuples(index=False)
    ]
    conn.executemany(
        """
        UPDATE fundamentals
        SET pe_ttm_calculated = ?, pb_lf_calculated = ?,
            ps_ttm_calculated = ?, dividend_yield_calculated = ?,
            valuation_report_period = ?, valuation_announcement_date = ?,
            valuation_available_date = ?, valuation_calculation_version = ?,
            dividend_event_count = ?, updated_at = ?
        WHERE trade_date = ? AND wind_code = ?
        """,
        rows,
    )
    conn.commit()
    print(
        "PIT 月频估值完成："
        f"PE {result['pe_ttm_calculated'].notna().sum()}，"
        f"PB {result['pb_lf_calculated'].notna().sum()}，"
        f"PS {result['ps_ttm_calculated'].notna().sum()}，"
        f"股息率 {result['dividend_yield_calculated'].notna().sum()}"
    )
    return len(rows)


def upsert_quality_asof_fundamentals(conn, snapshot_dates):
    reports = pd.read_sql_query("""
        SELECT rpt_date, wind_code, announcement_date, available_date,
               roe_reported, debt_to_assets_reported
        FROM financial_reports
        WHERE available_date IS NOT NULL
          AND (roe_reported IS NOT NULL OR debt_to_assets_reported IS NOT NULL)
    """, conn)
    if reports.empty:
        print("没有可用的报告期质量指标，跳过月度 as-of 回填")
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
                row.trade_date, code, row.roe_reported,
                row.debt_to_assets_reported, row.rpt_date,
                row.announcement_date, row.available_date, updated_at,
            ))

    conn.executemany("""
        INSERT INTO fundamentals (
            trade_date, wind_code, roe_reported, debt_to_assets_reported,
            quality_report_period, quality_announcement_date,
            quality_available_date, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(trade_date, wind_code) DO UPDATE SET
            roe_reported = excluded.roe_reported,
            debt_to_assets_reported = excluded.debt_to_assets_reported,
            quality_report_period = excluded.quality_report_period,
            quality_announcement_date = excluded.quality_announcement_date,
            quality_available_date = excluded.quality_available_date,
            updated_at = excluded.updated_at
    """, rows)
    conn.commit()
    print(f"报告期质量指标月度 as-of 回填完成：{len(rows)} 行")
    return len(rows)


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
            SUM(CASE WHEN status = 'empty' THEN 1 ELSE 0 END) AS empty_batches,
            SUM(CASE WHEN status = 'confirmed_empty' THEN 1 ELSE 0 END) AS confirmed_empty_batches,
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


def parse_args():
    parser = argparse.ArgumentParser(description="构建或增量更新全A股基础基本面库")
    parser.add_argument(
        "--warmup-only",
        action="store_true",
        help="仅回补预热期的月频基本面和季度财报，不抓日频市值",
    )
    parser.add_argument("--warmup-start-date", default="2018-01-01")
    parser.add_argument("--warmup-end-date", default="2021-12-31")
    parser.add_argument(
        "--recalculate-only", action="store_true",
        help="不连接 Wind，仅用本地原始数据重算月末 PIT 指标",
    )
    parser.add_argument(
        "--pit-backfill-only", action="store_true",
        help="只补月频市值/未复权收盘价和PIT估值原始财报，不抓旧便捷因子或日频市值",
    )
    return parser.parse_args()


def recalculate_from_local_data(start_date, end_date):
    conn = sqlite3.connect(DB_PATH)
    try:
        init_db(conn)
        snapshot_dates = [
            row[0]
            for row in conn.execute(
                """SELECT MAX(trade_date) FROM daily_valuation
                WHERE trade_date BETWEEN ? AND ?
                GROUP BY SUBSTR(trade_date,1,7) ORDER BY 1""",
                (start_date, end_date),
            ).fetchall()
        ]
        if not snapshot_dates:
            raise RuntimeError("本地日频市值库没有可用月末截面")
        upsert_monthly_market_cap_from_daily_valuation(conn, snapshot_dates)
        upsert_qfa_asof_fundamentals(conn, snapshot_dates)
        upsert_quality_asof_fundamentals(conn, snapshot_dates)
        calculate_and_upsert_ttm_roe(conn)
        upsert_ttm_roe_asof_fundamentals(conn, snapshot_dates)
        calculate_and_upsert_pit_valuations(conn, snapshot_dates)
        summarize_progress(conn)
    finally:
        conn.close()


def main():
    args = parse_args()
    if args.recalculate_only:
        recalculate_from_local_data(
            normalize_date(args.warmup_start_date),
            normalize_date(args.warmup_end_date),
        )
        return
    if args.warmup_only or args.pit_backfill_only:
        build_start_date = normalize_date(args.warmup_start_date)
        end_date = normalize_date(args.warmup_end_date)
        if pd.Timestamp(end_date) < pd.Timestamp(build_start_date):
            raise ValueError("预热结束日不能早于开始日")
    else:
        build_start_date = START_DATE
        end_date = datetime.today().strftime("%Y-%m-%d")
    w.start()
    conn = sqlite3.connect(DB_PATH)
    try:
        init_db(conn)
        current_universe_df = get_sector_constituents(conn)
        universe_df = merge_current_and_historical_universe(
            current_universe_df, build_start_date, end_date
        )
        save_stock_universe(conn, universe_df)
        codes = universe_df["wind_code"].tolist()
        budget = WindCellBudget(get_wind_cell_budget())
        status_counts = {}
        snapshot_dates = get_monthly_snapshot_dates(build_start_date, end_date)
        report_dates = get_quarter_report_dates(build_start_date, end_date)
        report_trading_days = get_trading_days(
            f"{pd.Timestamp(build_start_date).year - 3}-01-01",
            end_date,
        )

        print(f"数据库文件：{DB_PATH}")
        mode_text = (
            "仅补PIT估值原始数据" if args.pit_backfill_only
            else "仅补预热数据" if args.warmup_only
            else "日常构建/增量更新"
        )
        print(f"运行模式：{mode_text}")
        print(f"股票数量：{len(codes)}")
        print(f"月度截面：{len(snapshot_dates)} 个，{snapshot_dates[0]} ~ {snapshot_dates[-1]}")
        print(f"财报报告期：{len(report_dates)} 个，{report_dates[0]} ~ {report_dates[-1]}")
        print(f"每批股票数：{BATCH_SIZE}")
        print_budget_model(budget)
        upsert_monthly_market_cap_from_daily_valuation(conn, snapshot_dates)

        historical_daily_valuation_chunks = []
        force_daily_valuation_chunks = []
        current_year = None
        current_year_start = None
        current_year_trading_days = pd.DatetimeIndex([])
        if args.warmup_only or args.pit_backfill_only:
            print("日频估值：预热模式明确跳过，不消耗历史日频市值额度")
        else:
            daily_valuation_chunks = list(
                iter_date_chunks(build_start_date, end_date, chunk="Y")
            )
            current_year = pd.Timestamp(end_date).year
            current_year_start = f"{current_year}-01-01"
            historical_daily_valuation_chunks = [
                (chunk_start, chunk_end)
                for chunk_start, chunk_end in daily_valuation_chunks
                if pd.Timestamp(chunk_end).year < current_year
            ]
            current_year_trading_days = report_trading_days[
                (report_trading_days >= pd.Timestamp(current_year_start))
                & (report_trading_days <= pd.Timestamp(end_date))
            ]
            print(
                f"日频估值：{len(historical_daily_valuation_chunks)} 个已结束年度区间，"
                f"当年 {current_year} 按字段和股票批次增量更新"
            )
            force_daily_valuation_start = get_optional_date_env(
                DAILY_VALUATION_FORCE_START_ENV
            )
            if force_daily_valuation_start is not None:
                if pd.Timestamp(force_daily_valuation_start) > pd.Timestamp(end_date):
                    print(
                        f"{DAILY_VALUATION_FORCE_START_ENV}={force_daily_valuation_start} "
                        f"晚于结束日期 {end_date}，跳过强制补拉"
                    )
                else:
                    force_daily_valuation_chunks = [
                        (force_daily_valuation_start, end_date)
                    ]
                    print(
                        "日频估值强制补拉："
                        f"{force_daily_valuation_start} ~ {end_date}，"
                        "使用独立批次键覆盖已有年度 success 的尾部缺口"
                    )

        for trade_date in snapshot_dates:
            if args.warmup_only and not args.pit_backfill_only:
                for batch_start in range(0, len(codes), BATCH_SIZE):
                    batch_end = min(batch_start + BATCH_SIZE, len(codes))
                    status, _ = fetch_warmup_fundamental_batch(
                        conn,
                        codes,
                        trade_date,
                        batch_start,
                        batch_end,
                        budget,
                    )
                    status_counts[status] = status_counts.get(status, 0) + 1
            elif not args.pit_backfill_only:
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

            for field_key, wind_field in MONTHLY_MARKET_FIELDS.items():
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
                    status_counts[f"monthly_market_{status}"] = (
                        status_counts.get(f"monthly_market_{status}", 0) + 1
                    )

        # 已结束年份的起止日不再变化，继续使用固定年度批次回填。
        for chunk_start, chunk_end in historical_daily_valuation_chunks:
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

        # 当年的结束日每天都会变。每个字段、每个股票批次从上次
        # 成功区间的下一交易日续拉，避免每天重复申请年初至今的数据。
        if not args.warmup_only and not args.pit_backfill_only:
            for field_key, wind_field in DAILY_VALUATION_FIELDS.items():
                for batch_start in range(0, len(codes), BATCH_SIZE):
                    batch_end = min(batch_start + BATCH_SIZE, len(codes))
                    last_success_date = latest_successful_daily_valuation_end(
                        conn,
                        field_key,
                        batch_start,
                        batch_end,
                        current_year_start,
                        end_date,
                    )
                    query_start = next_incremental_trading_date(
                        last_success_date,
                        current_year_start,
                        current_year_trading_days,
                    )
                    if query_start is None or pd.Timestamp(query_start) > pd.Timestamp(end_date):
                        status_counts["incremental_skip"] = (
                            status_counts.get("incremental_skip", 0) + 1
                        )
                        continue
                    status, _ = fetch_daily_valuation_field_batch(
                        conn,
                        codes,
                        field_key,
                        wind_field,
                        query_start,
                        end_date,
                        batch_start,
                        batch_end,
                        budget,
                    )
                    status_counts[f"incremental_{status}"] = (
                        status_counts.get(f"incremental_{status}", 0) + 1
                    )

        for chunk_start, chunk_end in force_daily_valuation_chunks:
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
                    status_counts[f"force_{status}"] = status_counts.get(f"force_{status}", 0) + 1

        if not args.pit_backfill_only:
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

            for rpt_date in report_dates:
                for batch_start in range(0, len(codes), BATCH_SIZE):
                    batch_end = min(batch_start + BATCH_SIZE, len(codes))
                    status, _ = fetch_quality_report_batch(
                        conn,
                        codes,
                        rpt_date,
                        batch_start,
                        batch_end,
                        report_trading_days,
                        budget,
                    )
                    status_counts[f"quality_{status}"] = (
                        status_counts.get(f"quality_{status}", 0) + 1
                    )

        for rpt_date in report_dates:
            for batch_start in range(0, len(codes), BATCH_SIZE):
                batch_end = min(batch_start + BATCH_SIZE, len(codes))
                status, _ = fetch_ttm_roe_report_batch(
                    conn,
                    codes,
                    rpt_date,
                    batch_start,
                    batch_end,
                    report_trading_days,
                    budget,
                )
                status_counts[f"ttm_roe_{status}"] = (
                    status_counts.get(f"ttm_roe_{status}", 0) + 1
                )

        upsert_qfa_asof_fundamentals(conn, snapshot_dates)
        upsert_quality_asof_fundamentals(conn, snapshot_dates)
        calculate_and_upsert_ttm_roe(conn)
        upsert_ttm_roe_asof_fundamentals(conn, snapshot_dates)
        calculate_and_upsert_pit_valuations(conn, snapshot_dates)
        print_run_status(status_counts, budget)
        summarize_progress(conn)
        print("\n基础基本面数据库更新完成")
    finally:
        conn.close()
        w.close()


if __name__ == "__main__":
    main()
