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
    WIND_LEVEL1_INDUSTRY_SYSTEM,
    get_metadata,
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
DEFAULT_INCREMENTAL_CONSISTENCY_TOLERANCE = 0.0001
DEFAULT_CORPORATE_ACTION_LOOKBACK_DAYS = 7
DEFAULT_CORPORATE_ACTION_RPT_LOOKBACK_QUARTERS = 4
DEFAULT_INDUSTRY_FIELD = "wicsname2024"
DEFAULT_INDUSTRY_OPTIONS = "tradeDate={trade_date};industryType=1;"
CORPORATE_ACTION_FIELDS = [
    "div_exdate",
    "div_cashbeforetax",
    "div_capitalization",
    "div_stock",
    "div_recorddate",
    "div_paydate",
    "div_progress",
]


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


def ensure_corporate_action_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS corporate_action_events (
            wind_code TEXT NOT NULL,
            sec_name TEXT,
            rpt_date TEXT NOT NULL,
            ex_date TEXT NOT NULL,
            record_date TEXT,
            pay_date TEXT,
            cash_before_tax REAL,
            capitalization_ratio REAL,
            stock_dividend_ratio REAL,
            progress TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (wind_code, rpt_date, ex_date)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_corporate_action_events_ex_date
        ON corporate_action_events (ex_date)
    """)
    conn.commit()


def quarter_key(date_value):
    ts = pd.Timestamp(date_value)
    quarter = (ts.month - 1) // 3 + 1
    return f"{ts.year}Q{quarter}"


def resolve_industry_options(options, end_date):
    trade_date = pd.Timestamp(end_date).strftime("%Y%m%d")
    trade_date_dash = pd.Timestamp(end_date).strftime("%Y-%m-%d")
    return options.format(
        trade_date=trade_date,
        trade_date_dash=trade_date_dash,
    )


def metadata_token(value):
    return str(value).strip().replace(":", "_").replace(" ", "_")


def save_stock_industries(
    conn,
    industry_df,
    classification_system=WIND_LEVEL1_INDUSTRY_SYSTEM,
    source_field=DEFAULT_INDUSTRY_FIELD,
    source_options=DEFAULT_INDUSTRY_OPTIONS,
):
    updated_at = datetime.now().isoformat(timespec="seconds")
    rows = [
        (
            classification_system,
            row.wind_code,
            row.sec_name,
            row.industry_level1,
            source_field,
            source_options,
            updated_at,
        )
        for row in industry_df.itertuples(index=False)
    ]
    conn.executemany("""
        INSERT INTO stock_industry (
            classification_system,
            wind_code,
            sec_name,
            industry_level1,
            source_field,
            source_options,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(classification_system, wind_code) DO UPDATE SET
            sec_name = excluded.sec_name,
            industry_level1 = excluded.industry_level1,
            source_field = excluded.source_field,
            source_options = excluded.source_options,
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


def get_historical_universe_constituents(conn, universe_name, start_date, end_date):
    prior_snapshot = pd.read_sql_query(
        """
        SELECT snapshot_date, wind_code, sec_name
        FROM universe_constituents_snapshot
        WHERE universe_name = ?
          AND snapshot_date = (
              SELECT MAX(snapshot_date)
              FROM universe_constituents_snapshot
              WHERE universe_name = ?
                AND snapshot_date < ?
          )
        """,
        conn,
        params=[universe_name, universe_name, start_date],
    )
    range_snapshots = pd.read_sql_query(
        """
        SELECT snapshot_date, wind_code, sec_name
        FROM universe_constituents_snapshot
        WHERE universe_name = ?
          AND snapshot_date >= ?
          AND snapshot_date <= ?
        ORDER BY snapshot_date, wind_code
        """,
        conn,
        params=[universe_name, start_date, end_date],
    )
    snapshots = pd.concat([prior_snapshot, range_snapshots], ignore_index=True)
    if snapshots.empty:
        raise RuntimeError(f"没有找到 {universe_name} 历史成分快照：{start_date} ~ {end_date}")
    universe_df = (
        snapshots.sort_values(["wind_code", "snapshot_date"])
        .drop_duplicates("wind_code", keep="last")
        [["wind_code", "sec_name"]]
        .sort_values("wind_code")
        .reset_index(drop=True)
    )
    print(
        f"从历史成分快照读取股票池：{universe_name}，"
        f"快照 {snapshots['snapshot_date'].min()} ~ {snapshots['snapshot_date'].max()}，"
        f"历史并集 {len(universe_df)} 只"
    )
    return universe_df


def get_wind_industry_batch(w, universe_df, field, options, batch_size):
    rows = []
    codes = universe_df["wind_code"].tolist()
    code_to_name = dict(zip(universe_df["wind_code"], universe_df["sec_name"]))
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        print(f"拉取行业 {field}: {i}-{i + len(batch)} [{options}]")
        data = w.wss(batch, field, options)
        if data.ErrorCode != 0:
            outmessage = ""
            if getattr(data, "Data", None) and len(data.Data) > 0 and len(data.Data[0]) > 0:
                outmessage = str(data.Data[0][0])
            raise RuntimeError(
                f"行业拉取失败: field={field}, options={options}, "
                f"ErrorCode={data.ErrorCode}, OutMessage={outmessage}"
            )
        if not data.Data or len(data.Data) == 0:
            values = [None] * len(batch)
        else:
            values = data.Data[0]
        if len(values) != len(batch):
            raise RuntimeError(
                f"行业返回维度异常：codes={len(batch)}, values={len(values)}"
            )
        rows.extend(
            {
                "wind_code": code,
                "sec_name": code_to_name.get(code),
                "industry_level1": value,
            }
            for code, value in zip(batch, values)
        )
    return pd.DataFrame(rows)


def update_industries_from_wind(
    db_path,
    universe_name,
    sector_id,
    end_date,
    batch_size,
    industry_field=DEFAULT_INDUSTRY_FIELD,
    industry_options=DEFAULT_INDUSTRY_OPTIONS,
    classification_system=WIND_LEVEL1_INDUSTRY_SYSTEM,
    force=False,
):
    wind_started = False
    current_quarter = quarter_key(end_date)
    resolved_industry_options = resolve_industry_options(industry_options, end_date)
    metadata_key = (
        f"stock_industry:{classification_system}:"
        f"{metadata_token(universe_name)}:last_update_quarter"
    )

    conn = sqlite3.connect(db_path)
    try:
        last_quarter = get_metadata(conn, metadata_key)
        if last_quarter == current_quarter and not force:
            print(f"行业映射本季度已更新：{last_quarter}，跳过。")
            return

        from WindPy import w

        w.start()
        wind_started = True
        universe_df = get_wind_sector_constituents(w, sector_id)
        save_stock_universe(conn, universe_name, universe_df)
        industry_df = get_wind_industry_batch(
            w,
            universe_df,
            industry_field,
            resolved_industry_options,
            batch_size,
        )
        save_stock_industries(
            conn,
            industry_df,
            classification_system=classification_system,
            source_field=industry_field,
            source_options=resolved_industry_options,
        )
        non_null_count = int(industry_df["industry_level1"].notna().sum())
        print(
            f"行业映射更新完成：{len(industry_df)} 只，"
            f"非空 {non_null_count} 只，季度={current_quarter}"
        )
        if non_null_count == 0:
            print(
                "提示：本次 Wind 字段返回全为空。可检查字段/权限，"
                "或临时用 --industry-system sw_level1 "
                "--industry-field industry_sw --industry-options industryType=1 验证。"
            )
        conn.execute("""
            INSERT INTO metadata (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
        """, (
            metadata_key,
            current_quarter,
            datetime.now().isoformat(timespec="seconds"),
        ))
        conn.commit()
    finally:
        conn.close()
        if wind_started:
            w.close()


def recent_report_dates(end_date, lookback_quarters):
    end_ts = pd.Timestamp(end_date)
    quarter_ends = []
    year = end_ts.year
    while len(quarter_ends) < lookback_quarters:
        for month, day in [(12, 31), (9, 30), (6, 30), (3, 31)]:
            rpt = pd.Timestamp(year=year, month=month, day=day)
            if rpt <= end_ts:
                quarter_ends.append(rpt)
        year -= 1
    return [date.strftime("%Y%m%d") for date in sorted(set(quarter_ends), reverse=True)[:lookback_quarters]]


def date_value_to_string(value):
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def fetch_corporate_action_events(w, universe_df, rpt_dates, batch_size):
    rows = []
    codes = universe_df["wind_code"].tolist()
    code_to_name = dict(zip(universe_df["wind_code"], universe_df["sec_name"]))
    fields = ",".join(CORPORATE_ACTION_FIELDS)
    for rpt_date in rpt_dates:
        for batch_start in range(0, len(codes), batch_size):
            batch_end = min(batch_start + batch_size, len(codes))
            batch_codes = codes[batch_start:batch_end]
            print(f"拉取除权除息事件: rptDate={rpt_date} 股票 {batch_start}-{batch_end}")
            data = w.wss(batch_codes, fields, f"rptDate={rpt_date}")
            if data.ErrorCode != 0:
                outmessage = ""
                if getattr(data, "Data", None) and len(data.Data) > 0 and len(data.Data[0]) > 0:
                    outmessage = str(data.Data[0][0])
                print(f"除权除息事件拉取失败: rptDate={rpt_date} ErrorCode={data.ErrorCode} {outmessage}")
                continue

            field_data = {
                field.lower(): values
                for field, values in zip(data.Fields, data.Data)
            }
            ex_dates = field_data.get("div_exdate", [])
            for idx, code in enumerate(batch_codes):
                ex_date = date_value_to_string(ex_dates[idx] if idx < len(ex_dates) else None)
                if not ex_date:
                    continue
                rows.append({
                    "wind_code": code,
                    "sec_name": code_to_name.get(code),
                    "rpt_date": pd.Timestamp(rpt_date).strftime("%Y-%m-%d"),
                    "ex_date": ex_date,
                    "record_date": date_value_to_string(field_data.get("div_recorddate", [None] * len(batch_codes))[idx]),
                    "pay_date": date_value_to_string(field_data.get("div_paydate", [None] * len(batch_codes))[idx]),
                    "cash_before_tax": field_data.get("div_cashbeforetax", [None] * len(batch_codes))[idx],
                    "capitalization_ratio": field_data.get("div_capitalization", [None] * len(batch_codes))[idx],
                    "stock_dividend_ratio": field_data.get("div_stock", [None] * len(batch_codes))[idx],
                    "progress": field_data.get("div_progress", [None] * len(batch_codes))[idx],
                })
    return pd.DataFrame(rows)


def save_corporate_action_events(conn, events_df):
    ensure_corporate_action_table(conn)
    if events_df.empty:
        return 0

    updated_at = datetime.now().isoformat(timespec="seconds")
    rows = []
    for row in events_df.itertuples(index=False):
        rows.append((
            row.wind_code,
            row.sec_name,
            row.rpt_date,
            row.ex_date,
            row.record_date,
            row.pay_date,
            None if pd.isna(row.cash_before_tax) else float(row.cash_before_tax),
            None if pd.isna(row.capitalization_ratio) else float(row.capitalization_ratio),
            None if pd.isna(row.stock_dividend_ratio) else float(row.stock_dividend_ratio),
            row.progress,
            updated_at,
        ))
    conn.executemany("""
        INSERT INTO corporate_action_events (
            wind_code, sec_name, rpt_date, ex_date, record_date, pay_date,
            cash_before_tax, capitalization_ratio, stock_dividend_ratio,
            progress, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(wind_code, rpt_date, ex_date) DO UPDATE SET
            sec_name = excluded.sec_name,
            record_date = excluded.record_date,
            pay_date = excluded.pay_date,
            cash_before_tax = excluded.cash_before_tax,
            capitalization_ratio = excluded.capitalization_ratio,
            stock_dividend_ratio = excluded.stock_dividend_ratio,
            progress = excluded.progress,
            updated_at = excluded.updated_at
    """, rows)
    conn.commit()
    return len(rows)


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


def get_wsd_matrix_for_dates(w, codes, field, query_dates, batch_size, price_option="PriceAdj=F"):
    frames = []
    query_dates = [pd.Timestamp(date).strftime("%Y-%m-%d") for date in query_dates]
    for query_date in query_dates:
        date_frames = []
        for batch_start in range(0, len(codes), batch_size):
            batch_end = min(batch_start + batch_size, len(codes))
            batch_codes = codes[batch_start:batch_end]
            print(f"拉取校验 {field}: {query_date} 股票 {batch_start}-{batch_end}")
            data = w.wsd(batch_codes, field, query_date, query_date, price_option)
            df, error_code, error_message = parse_wsd_matrix(data, field)
            if error_code != 0:
                print(f"校验拉取失败: {field} {query_date} {batch_start}-{batch_end} ErrorCode={error_code} {error_message}")
                continue
            date_frames.append(df)
        if date_frames:
            frames.append(pd.concat(date_frames, axis=1))

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, axis=0)
    df = df.sort_index()
    df = df.loc[~df.index.duplicated(keep="last")]
    df = df.loc[:, ~df.columns.duplicated()]
    return df


def get_adjustment_check_dates(w, start_date, end_date, frequency, recent_days):
    data = w.tdays(start_date, end_date, "")
    if data.ErrorCode != 0 or not data.Data or len(data.Data[0]) == 0:
        raise RuntimeError(f"交易日历拉取失败: ErrorCode={data.ErrorCode}")

    trading_dates = pd.DatetimeIndex(pd.to_datetime(data.Data[0])).sort_values()
    if frequency == "M":
        anchors = pd.date_range(start=trading_dates[0], end=trading_dates[-1], freq="MS")
    else:
        anchors = pd.date_range(start=trading_dates[0], end=trading_dates[-1], freq="QS")

    check_dates = []
    for anchor in anchors:
        future_dates = trading_dates[trading_dates >= anchor]
        if len(future_dates) > 0:
            check_dates.append(future_dates[0])

    if recent_days > 0:
        check_dates.extend(trading_dates[-recent_days:])

    check_dates = sorted(set(pd.Timestamp(date) for date in check_dates))
    return [date.strftime("%Y-%m-%d") for date in check_dates]


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


def is_forward_adjusted_price(price_option, adjusted):
    return adjusted == "F" and "PRICEADJ=F" in price_option.upper()


def get_previous_cached_price_date(conn, codes, start_date, adjusted="F"):
    if not codes:
        return None

    placeholders = ",".join("?" for _ in codes)
    row = conn.execute(f"""
        SELECT MAX(trade_date)
        FROM daily_prices
        WHERE adjusted = ?
          AND trade_date < ?
          AND close IS NOT NULL
          AND wind_code IN ({placeholders})
    """, [adjusted, start_date, *codes]).fetchone()
    return row[0] if row and row[0] else None


def assert_incremental_adjusted_consistency(
    w,
    conn,
    codes,
    start_date,
    price_option,
    adjusted,
    batch_size,
    tolerance=DEFAULT_INCREMENTAL_CONSISTENCY_TOLERANCE,
):
    if not is_forward_adjusted_price(price_option, adjusted):
        return []

    previous_date = get_previous_cached_price_date(conn, codes, start_date, adjusted=adjusted)
    if not previous_date:
        print("前复权一致性保护：本地没有更新起点前的 close，跳过边界检查。")
        return []

    print(
        "前复权一致性保护：检查更新边界 "
        f"{previous_date} -> {start_date}，仅抽查边界 close，避免半新半旧复权因子。"
    )
    local_close = load_sqlite_price_matrix(
        conn,
        codes,
        "close",
        previous_date,
        previous_date,
        adjusted=adjusted,
    )
    wind_close = get_wsd_matrix_for_dates(
        w,
        codes,
        "close",
        [previous_date],
        batch_size,
        price_option=price_option,
    )
    check_ts = pd.Timestamp(previous_date)
    if check_ts not in local_close.index or check_ts not in wind_close.index:
        raise RuntimeError(
            f"前复权一致性保护失败：无法同时取得本地/Wind 的 {previous_date} close。"
        )

    compare_df = pd.DataFrame({
        "local": local_close.loc[check_ts],
        "wind": wind_close.loc[check_ts],
    }).dropna()
    if compare_df.empty:
        print("前复权一致性保护：边界日没有可比较 close，跳过。")
        return []

    denominator = compare_df["local"].abs().where(compare_df["local"].abs() > 1e-12)
    compare_df["diff_ratio"] = (compare_df["wind"] - compare_df["local"]).abs() / denominator
    drift_df = compare_df[compare_df["diff_ratio"] > tolerance].sort_values("diff_ratio", ascending=False)
    if drift_df.empty:
        print("前复权一致性保护：边界 close 与 Wind 一致，允许增量写入。")
        return []

    sample = []
    for code, row in drift_df.head(20).iterrows():
        sample.append(
            f"{code}: local={row['local']:.6g}, wind={row['wind']:.6g}, "
            f"diff={row['diff_ratio']:.2%}"
        )
    message = (
        "前复权一致性保护触发：更新起点前的本地 close 已不同于 Wind 当前前复权 close，"
        "继续增量写入会造成半新半旧复权序列。\n"
        f"边界日期：{previous_date}；异常股票数：{len(drift_df)}；样例：\n"
        + "\n".join(sample)
    )
    print(message)
    return drift_df.index.tolist()


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


def corporate_action_refresh(
    db_path,
    universe_name,
    sector_id,
    start_date,
    end_date,
    fields,
    batch_size,
    date_chunk,
    event_lookback_days,
    rpt_lookback_quarters,
):
    from WindPy import w

    event_start_date = (
        pd.Timestamp(end_date) - timedelta(days=event_lookback_days - 1)
    ).strftime("%Y-%m-%d")
    event_end_date = pd.Timestamp(end_date).strftime("%Y-%m-%d")
    rpt_dates = recent_report_dates(end_date, rpt_lookback_quarters)

    w.start()
    conn = sqlite3.connect(db_path)
    try:
        universe_df = get_wind_sector_constituents(w, sector_id)
        save_stock_universe(conn, universe_name, universe_df)
        events_df = fetch_corporate_action_events(w, universe_df, rpt_dates, batch_size)
        saved_count = save_corporate_action_events(conn, events_df)
        print(f"除权除息事件写入/更新：{saved_count} 条")

        if events_df.empty:
            print("近期报告期没有返回除权除息事件，无需回刷。")
            return

        events_df = events_df[
            (events_df["ex_date"] >= event_start_date)
            & (events_df["ex_date"] <= event_end_date)
        ].copy()
        if events_df.empty:
            print(f"近 {event_lookback_days} 天没有除权除息事件，无需回刷。")
            return

        event_codes = sorted(events_df["wind_code"].dropna().unique().tolist())
        print(
            f"近 {event_lookback_days} 天除权除息股票 {len(event_codes)} 只，"
            f"事件区间 {event_start_date} ~ {event_end_date}"
        )
        print(", ".join(event_codes[:50]) + (" ..." if len(event_codes) > 50 else ""))
        refresh_codes_from_wind(
            w,
            conn,
            event_codes,
            fields,
            start_date,
            end_date,
            batch_size,
            date_chunk,
        )
    finally:
        conn.close()
        w.close()


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
    force_refresh=False,
):
    date_range = f"{query_start_date}~{query_end_date}"
    if (
        not force_refresh
        and price_batch_status(conn, dataset, date_range, field, batch_start, batch_end) == "success"
    ):
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
    force_refresh=False,
    use_historical_universe_snapshots=False,
    skip_adjust_consistency_guard=False,
    repair_adjust_drift=False,
    adjust_history_start_date=DEFAULT_START_DATE,
):
    from WindPy import w

    w.start()
    conn = sqlite3.connect(db_path)
    try:
        if use_historical_universe_snapshots:
            universe_df = get_historical_universe_constituents(conn, universe_name, start_date, end_date)
        else:
            universe_df = get_wind_sector_constituents(w, sector_id)
        save_stock_universe(conn, universe_name, universe_df)
        codes = universe_df["wind_code"].tolist()
        dataset = f"daily_prices:{universe_name}:{adjusted}"
        if use_historical_universe_snapshots:
            dataset = f"{dataset}:historical"
        print(f"股票池：{universe_name}，股票数：{len(codes)}")
        print(f"行情区间：{start_date} ~ {end_date}")
        print(f"字段：{', '.join(fields)}")
        print(f"价格口径：{price_option} / adjusted={adjusted}")
        print(f"每批股票数：{batch_size}")
        print(f"强制覆盖已成功批次：{force_refresh}")

        if not skip_adjust_consistency_guard and not force_refresh:
            drift_codes = assert_incremental_adjusted_consistency(
                w,
                conn,
                codes,
                start_date,
                price_option,
                adjusted,
                batch_size,
            )
            if drift_codes:
                if not repair_adjust_drift:
                    raise RuntimeError(
                        "前复权一致性保护已发现漂移股票。"
                        "请加 --repair-adjust-drift 自动回刷漂移股票历史，"
                        "或手工处理后再增量更新。"
                    )
                if not is_forward_adjusted_price(price_option, adjusted):
                    raise RuntimeError("--repair-adjust-drift 仅支持 PriceAdj=F / adjusted=F")
                print(
                    f"自动修复前复权漂移股票：{len(drift_codes)} 只，"
                    f"回刷 {adjust_history_start_date} ~ {end_date}"
                )
                refresh_codes_from_wind(
                    w,
                    conn,
                    drift_codes,
                    fields,
                    adjust_history_start_date,
                    end_date,
                    batch_size,
                    date_chunk,
                )
                second_drift_codes = assert_incremental_adjusted_consistency(
                    w,
                    conn,
                    codes,
                    start_date,
                    price_option,
                    adjusted,
                    batch_size,
                )
                if second_drift_codes:
                    raise RuntimeError(
                        "自动修复后仍发现前复权漂移股票，已停止增量写入。"
                        f"剩余异常股票数：{len(second_drift_codes)}"
                    )
        elif skip_adjust_consistency_guard:
            print("前复权一致性保护：已按参数显式跳过。")

        status_counts = {"success": 0, "skip": 0, "failed": 0}
        for chunk_start, chunk_end in iter_date_chunks(start_date, end_date, date_chunk):
            for field in fields:
                for batch_start in range(0, len(codes), batch_size):
                    batch_end = min(batch_start + batch_size, len(codes))
                    status, _ = fetch_price_batch_from_wind(
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
                        force_refresh=force_refresh,
                    )
                    status_counts[status] = status_counts.get(status, 0) + 1

        if set(COMPLETE_EOD_PRICE_FIELDS).issubset(set(fields)) and adjusted == "F":
            cleanup_incomplete_eod_rows(conn, start_date, end_date, adjusted=adjusted)

        if status_counts.get("failed", 0) > 0:
            raise RuntimeError(
                f"行情更新存在失败批次：success={status_counts.get('success', 0)}, "
                f"skip={status_counts.get('skip', 0)}, failed={status_counts.get('failed', 0)}。"
                "请检查上方 Wind 错误信息。"
            )
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
    deep_adjust_check=False,
    adjust_check_frequency="Q",
):
    from WindPy import w

    w.start()
    conn = sqlite3.connect(db_path)
    try:
        universe_df = get_wind_sector_constituents(w, sector_id)
        save_stock_universe(conn, universe_name, universe_df)
        codes = universe_df["wind_code"].tolist()

        print(f"智能复权校验股票池：{universe_name}，股票数：{len(codes)}")
        print(f"偏差阈值：{tolerance:.6f}")

        if deep_adjust_check:
            check_dates = get_adjustment_check_dates(
                w,
                start_date,
                end_date,
                adjust_check_frequency,
                check_days,
            )
            print(
                f"深度前复权校验日期：{len(check_dates)} 个，"
                f"{check_dates[0]} ~ {check_dates[-1]}，频率={adjust_check_frequency}"
            )
            sqlite_close = load_sqlite_price_matrix(conn, codes, "close", check_dates[0], check_dates[-1])
            sqlite_close = sqlite_close.reindex(pd.to_datetime(check_dates))
            wind_close = get_wsd_matrix_for_dates(w, codes, "close", check_dates, batch_size)
        else:
            check_start = (pd.Timestamp(end_date) - timedelta(days=check_days - 1)).strftime("%Y-%m-%d")
            print(f"校验区间：{check_start} ~ {end_date}")
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
    parser.add_argument("--force-price-refresh", action="store_true", help="忽略已成功批次状态，强制从 Wind 重拉并覆盖行情")
    parser.add_argument("--skip-adjust-consistency-guard", action="store_true", help="跳过前复权增量写入边界一致性保护，仅限手工修复时使用")
    parser.add_argument("--repair-adjust-drift", action="store_true", help="增量更新发现前复权漂移时，先回刷漂移股票历史再继续更新")
    parser.add_argument("--use-historical-universe-snapshots", action="store_true", help="使用本地历史成分快照并集作为行情股票池")
    parser.add_argument("--corporate-action-refresh", action="store_true", help="抓取近期除权除息事件，并回刷事件股票历史前复权行情")
    parser.add_argument("--industries-from-wind", action="store_true", help="从 Wind 拉取股票所属一级行业写入 SQLite，默认每季度更新一次")
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
    parser.add_argument("--industry-field", default=DEFAULT_INDUSTRY_FIELD, help="Wind wss 行业字段，默认 wicsname2024")
    parser.add_argument("--industry-options", default=DEFAULT_INDUSTRY_OPTIONS, help="Wind wss 行业参数，可用 {trade_date} 占位符，默认 tradeDate={trade_date};industryType=1;")
    parser.add_argument("--industry-system", default=WIND_LEVEL1_INDUSTRY_SYSTEM, help="写入 stock_industry.classification_system 的行业体系标识")
    parser.add_argument("--force-industry-refresh", action="store_true", help="忽略季度缓存，强制刷新行业映射")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--date-chunk", choices=["Y", "Q", "ALL"], default="Y")
    parser.add_argument("--check-days", type=int, default=DEFAULT_ADJUST_CHECK_DAYS)
    parser.add_argument("--adjust-tolerance", type=float, default=DEFAULT_ADJUST_TOLERANCE)
    parser.add_argument("--deep-adjust-check", action="store_true", help="用历史锚点日期检查前复权口径漂移，而不只检查最近几天")
    parser.add_argument("--adjust-check-frequency", choices=["M", "Q"], default="Q", help="深度前复权检查锚点频率")
    parser.add_argument("--event-lookback-days", type=int, default=DEFAULT_CORPORATE_ACTION_LOOKBACK_DAYS)
    parser.add_argument("--event-rpt-lookback-quarters", type=int, default=DEFAULT_CORPORATE_ACTION_RPT_LOOKBACK_QUARTERS)
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
            force_refresh=args.force_price_refresh,
            use_historical_universe_snapshots=args.use_historical_universe_snapshots,
            skip_adjust_consistency_guard=args.skip_adjust_consistency_guard,
            repair_adjust_drift=args.repair_adjust_drift,
            adjust_history_start_date=args.adjust_history_start_date,
        )

    if args.industries_from_wind:
        update_industries_from_wind(
            db_path=args.db_path,
            universe_name=args.universe_name,
            sector_id=args.sector_id,
            end_date=args.end_date,
            batch_size=args.batch_size,
            industry_field=args.industry_field,
            industry_options=args.industry_options,
            classification_system=args.industry_system,
            force=args.force_industry_refresh,
        )

    if args.corporate_action_refresh:
        corporate_action_refresh(
            db_path=args.db_path,
            universe_name=args.universe_name,
            sector_id=args.sector_id,
            start_date=args.adjust_history_start_date,
            end_date=args.end_date,
            fields=args.price_fields,
            batch_size=args.batch_size,
            date_chunk=args.date_chunk,
            event_lookback_days=args.event_lookback_days,
            rpt_lookback_quarters=args.event_rpt_lookback_quarters,
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
            deep_adjust_check=args.deep_adjust_check,
            adjust_check_frequency=args.adjust_check_frequency,
        )


if __name__ == "__main__":
    main()
