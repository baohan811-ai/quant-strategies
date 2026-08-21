import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta

import pandas as pd


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
CACHE_DIR = os.path.join(BASE_DIR, "缓存")

MARKET_DB_PATH = os.path.join(CACHE_DIR, "本地行情数据库.sqlite3")
A_SHARE_FUNDAMENTAL_DB_PATH = os.path.join(CACHE_DIR, "全部A股_基础基本面.sqlite3")

PRICE_FIELDS = {"open", "high", "low", "close", "volume", "amt", "turn", "free_turn"}
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
                free_turn REAL,
                adjusted TEXT NOT NULL DEFAULT 'F',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (trade_date, wind_code, adjusted)
            )
        """)
        existing_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(daily_prices)").fetchall()
        }
        if "free_turn" not in existing_columns:
            conn.execute("ALTER TABLE daily_prices ADD COLUMN free_turn REAL")
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
            CREATE TABLE IF NOT EXISTS universe_constituents_snapshot (
                snapshot_date TEXT NOT NULL,
                universe_name TEXT NOT NULL,
                sector_id TEXT NOT NULL,
                wind_code TEXT NOT NULL,
                sec_name TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (snapshot_date, universe_name, wind_code)
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
            CREATE TABLE IF NOT EXISTS stock_industry_snapshot (
                snapshot_date TEXT NOT NULL,
                classification_system TEXT NOT NULL,
                universe_name TEXT NOT NULL,
                wind_code TEXT NOT NULL,
                sec_name TEXT,
                industry_level1 TEXT,
                source_field TEXT NOT NULL,
                source_options TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (
                    snapshot_date,
                    classification_system,
                    universe_name,
                    wind_code
                )
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS index_industry_weight_snapshot (
                snapshot_date TEXT NOT NULL,
                index_code TEXT NOT NULL,
                classification_system TEXT NOT NULL,
                industry_level1 TEXT NOT NULL,
                industry_weight REAL NOT NULL,
                constituent_count INTEGER NOT NULL,
                source_field TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (
                    snapshot_date,
                    index_code,
                    classification_system,
                    industry_level1
                )
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
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_stock_industry_snapshot_lookup
            ON stock_industry_snapshot (
                universe_name,
                classification_system,
                snapshot_date,
                wind_code
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_index_industry_weight_lookup
            ON index_industry_weight_snapshot (
                index_code,
                classification_system,
                snapshot_date
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_stock_industry_snapshot_level1
            ON stock_industry_snapshot (
                universe_name,
                classification_system,
                snapshot_date,
                industry_level1
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_universe_constituents_snapshot_universe_date
            ON universe_constituents_snapshot (universe_name, snapshot_date)
        """)
        _set_metadata(conn, "schema_version", "3")
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


def load_stock_industry_snapshots(
    universe_name,
    start_date,
    end_date,
    classification_system=WIND_LEVEL1_INDUSTRY_SYSTEM,
    db_path=MARKET_DB_PATH,
    include_prior=True,
):
    if not os.path.exists(db_path):
        return pd.DataFrame(
            columns=[
                "snapshot_date",
                "wind_code",
                "sec_name",
                "industry_level1",
            ]
        )

    frames = []
    with sqlite3.connect(db_path) as conn:
        if include_prior:
            prior = pd.read_sql_query(
                """
                SELECT snapshot_date, wind_code, sec_name, industry_level1
                FROM stock_industry_snapshot
                WHERE universe_name = ?
                  AND classification_system = ?
                  AND snapshot_date = (
                      SELECT MAX(snapshot_date)
                      FROM stock_industry_snapshot
                      WHERE universe_name = ?
                        AND classification_system = ?
                        AND snapshot_date < ?
                  )
                """,
                conn,
                params=[
                    universe_name,
                    classification_system,
                    universe_name,
                    classification_system,
                    start_date,
                ],
            )
            frames.append(prior)

        in_range = pd.read_sql_query(
            """
            SELECT snapshot_date, wind_code, sec_name, industry_level1
            FROM stock_industry_snapshot
            WHERE universe_name = ?
              AND classification_system = ?
              AND snapshot_date >= ?
              AND snapshot_date <= ?
            ORDER BY snapshot_date, wind_code
            """,
            conn,
            params=[
                universe_name,
                classification_system,
                start_date,
                end_date,
            ],
        )
        frames.append(in_range)

    snapshots = pd.concat(frames, ignore_index=True)
    if snapshots.empty:
        return snapshots
    snapshots["snapshot_date"] = pd.to_datetime(snapshots["snapshot_date"])
    return (
        snapshots
        .drop_duplicates(["snapshot_date", "wind_code"], keep="last")
        .sort_values(["snapshot_date", "wind_code"])
        .reset_index(drop=True)
    )


def get_latest_universe_constituent_snapshot(
    universe_name,
    on_or_before=None,
    db_path=MARKET_DB_PATH,
):
    if not os.path.exists(db_path):
        return None, pd.DataFrame(columns=["wind_code", "sec_name"])

    conditions = ["universe_name = ?"]
    params = [universe_name]
    if on_or_before is not None:
        conditions.append("snapshot_date <= ?")
        params.append(pd.Timestamp(on_or_before).strftime("%Y-%m-%d"))

    where_clause = " AND ".join(conditions)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            f"""
            SELECT MAX(snapshot_date)
            FROM universe_constituents_snapshot
            WHERE {where_clause}
            """,
            params,
        ).fetchone()
        snapshot_date = row[0] if row and row[0] else None
        if snapshot_date is None:
            return None, pd.DataFrame(columns=["wind_code", "sec_name"])
        snapshot = pd.read_sql_query(
            """
            SELECT wind_code, sec_name
            FROM universe_constituents_snapshot
            WHERE universe_name = ?
              AND snapshot_date = ?
            ORDER BY wind_code
            """,
            conn,
            params=[universe_name, snapshot_date],
        )
    return snapshot_date, snapshot


def save_universe_constituent_snapshot(
    universe_name,
    sector_id,
    snapshot_date,
    snapshot_df,
    db_path=MARKET_DB_PATH,
):
    required_columns = {"wind_code", "sec_name"}
    missing_columns = required_columns.difference(snapshot_df.columns)
    if missing_columns:
        raise ValueError(f"成分快照缺少字段: {sorted(missing_columns)}")

    snapshot_date = pd.Timestamp(snapshot_date).strftime("%Y-%m-%d")
    updated_at = datetime.now().isoformat(timespec="seconds")
    rows = [
        (
            snapshot_date,
            universe_name,
            sector_id,
            row.wind_code,
            row.sec_name,
            updated_at,
        )
        for row in snapshot_df.itertuples(index=False)
    ]
    with connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO universe_constituents_snapshot (
                snapshot_date,
                universe_name,
                sector_id,
                wind_code,
                sec_name,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_date, universe_name, wind_code) DO UPDATE SET
                sector_id = excluded.sector_id,
                sec_name = excluded.sec_name,
                updated_at = excluded.updated_at
            """,
            rows,
        )
        conn.commit()
    return len(rows)


def fetch_wind_sector_constituents(
    wind_client,
    sector_id,
    query_date=None,
):
    options = f"sectorid={sector_id}"
    if query_date is not None:
        options = (
            f"date={pd.Timestamp(query_date).strftime('%Y-%m-%d')};"
            f"sectorid={sector_id}"
        )
    data = wind_client.wset("sectorconstituent", options)
    if data.ErrorCode != 0 or not data.Data:
        raise RuntimeError(
            f"成分股拉取失败: date={query_date or 'current'}, "
            f"sector_id={sector_id}, ErrorCode={data.ErrorCode}"
        )
    field_map = {str(field).lower(): idx for idx, field in enumerate(data.Fields)}
    code_idx = field_map.get("wind_code")
    name_idx = field_map.get("sec_name")
    if code_idx is None or name_idx is None:
        raise RuntimeError(f"成分股返回字段异常: {data.Fields}")
    snapshot = pd.DataFrame(
        {
            "wind_code": data.Data[code_idx],
            "sec_name": data.Data[name_idx],
        }
    )
    return (
        snapshot
        .dropna(subset=["wind_code"])
        .drop_duplicates("wind_code", keep="last")
        .sort_values("wind_code")
        .reset_index(drop=True)
    )


def ensure_universe_constituents_current(
    wind_client,
    universe_name,
    sector_id,
    target_date,
    current_snapshot=None,
    expected_count=None,
    db_path=MARKET_DB_PATH,
):
    target_date = pd.Timestamp(target_date).strftime("%Y-%m-%d")
    current_snapshot = (
        current_snapshot.copy()
        if current_snapshot is not None
        else fetch_wind_sector_constituents(wind_client, sector_id)
    )
    current_snapshot = (
        current_snapshot
        .dropna(subset=["wind_code"])
        .drop_duplicates("wind_code", keep="last")
        .sort_values("wind_code")
        .reset_index(drop=True)
    )
    if expected_count is not None and len(current_snapshot) != expected_count:
        raise RuntimeError(
            f"{universe_name} 当前成分数量异常："
            f"{len(current_snapshot)}，预期 {expected_count}"
        )

    latest_date, latest_snapshot = get_latest_universe_constituent_snapshot(
        universe_name,
        on_or_before=target_date,
        db_path=db_path,
    )
    current_codes = set(current_snapshot["wind_code"])
    latest_codes = set(latest_snapshot["wind_code"])
    result = {
        "target_date": target_date,
        "latest_snapshot_before_check": latest_date,
        "latest_snapshot_after_check": latest_date,
        "status": "matched",
        "current_count": len(current_snapshot),
        "added_count": len(current_codes - latest_codes),
        "removed_count": len(latest_codes - current_codes),
        "written_change_dates": [],
    }

    if latest_date is None:
        save_universe_constituent_snapshot(
            universe_name,
            sector_id,
            target_date,
            current_snapshot,
            db_path=db_path,
        )
        result.update(
            {
                "status": "initialized",
                "latest_snapshot_after_check": target_date,
                "written_change_dates": [target_date],
            }
        )
        return result

    if latest_codes == current_codes:
        return result

    trading_data = wind_client.tdays(
        (pd.Timestamp(latest_date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
        target_date,
        "",
    )
    if (
        trading_data.ErrorCode != 0
        or not trading_data.Data
        or len(trading_data.Data[0]) == 0
    ):
        raise RuntimeError(
            f"无法定位 {universe_name} 成分变化生效日："
            f"{latest_date} ~ {target_date}, ErrorCode={trading_data.ErrorCode}"
        )

    previous_codes = latest_codes
    written_change_dates = []
    last_scanned_snapshot = latest_snapshot
    for trade_date in pd.to_datetime(trading_data.Data[0]):
        trade_date_text = trade_date.strftime("%Y-%m-%d")
        daily_snapshot = fetch_wind_sector_constituents(
            wind_client,
            sector_id,
            query_date=trade_date_text,
        )
        daily_codes = set(daily_snapshot["wind_code"])
        if expected_count is not None and len(daily_snapshot) != expected_count:
            raise RuntimeError(
                f"{universe_name} {trade_date_text} 成分数量异常："
                f"{len(daily_snapshot)}，预期 {expected_count}"
            )
        if daily_codes != previous_codes:
            save_universe_constituent_snapshot(
                universe_name,
                sector_id,
                trade_date_text,
                daily_snapshot,
                db_path=db_path,
            )
            written_change_dates.append(trade_date_text)
            previous_codes = daily_codes
        last_scanned_snapshot = daily_snapshot

    scanned_codes = set(last_scanned_snapshot["wind_code"])
    if scanned_codes != current_codes:
        raise RuntimeError(
            f"{universe_name} 历史扫描终点与Wind当前成分不一致，"
            "为避免错误信号已停止运行"
        )

    final_date, _ = get_latest_universe_constituent_snapshot(
        universe_name,
        on_or_before=target_date,
        db_path=db_path,
    )
    result.update(
        {
            "status": "updated",
            "latest_snapshot_after_check": final_date,
            "written_change_dates": written_change_dates,
        }
    )
    return result


def count_complete_price_rows(
    codes,
    trade_date,
    fields=None,
    db_path=MARKET_DB_PATH,
    adjusted="F",
    chunk_size=800,
):
    codes = list(codes) if codes is not None else []
    if not os.path.exists(db_path) or len(codes) == 0:
        return 0

    fields = fields or COMPLETE_EOD_PRICE_FIELDS
    complete_conditions = " AND ".join(
        f"{field} IS NOT NULL" for field in fields
    )
    total = 0
    with sqlite3.connect(db_path) as conn:
        for start in range(0, len(codes), chunk_size):
            code_chunk = codes[start:start + chunk_size]
            placeholders = ",".join("?" for _ in code_chunk)
            row = conn.execute(f"""
                SELECT COUNT(DISTINCT wind_code)
                FROM daily_prices
                WHERE adjusted = ?
                  AND trade_date = ?
                  AND wind_code IN ({placeholders})
                  AND {complete_conditions}
            """, (adjusted, trade_date, *code_chunk)).fetchone()
            total += int(row[0] or 0)
    return total


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
    target_codes=None,
    min_coverage_ratio=0.95,
    price_option="PriceAdj=F",
    adjusted="F",
):
    recent_trading_dates = get_recent_wind_trading_dates(wind_client, end_date)
    latest_trading_date = recent_trading_dates[-1]
    latest_complete_target_date = (
        recent_trading_dates[-2]
        if len(recent_trading_dates) >= 2
        else latest_trading_date
    )

    fields = price_fields or DEFAULT_DAILY_PRICE_FIELDS
    if target_codes is not None:
        target_codes = list(target_codes)
    if target_codes:
        min_coverage = max(1, int(len(target_codes) * min_coverage_ratio))
        coverage = count_complete_price_rows(
            target_codes,
            latest_complete_target_date,
            fields=fields,
            db_path=db_path,
            adjusted=adjusted,
        )
        if coverage >= min_coverage:
            print(
                f"本地行情数据库已覆盖 {universe_name} {latest_complete_target_date}: "
                f"{coverage}/{len(target_codes)}"
            )
            print(f"最新交易日 {latest_trading_date} 由策略使用 wsq 实时行情临时补齐。")
            return latest_trading_date
        latest_price_date = None
        print(
            f"本地行情数据库覆盖不足：{universe_name} {latest_complete_target_date} "
            f"{coverage}/{len(target_codes)}，目标至少 {min_coverage}"
        )
    else:
        latest_price_date = get_latest_price_date(db_path, adjusted=adjusted)
        if latest_price_date is not None and latest_price_date >= latest_complete_target_date:
            print(f"本地行情数据库已是最新：{latest_price_date}")
            if latest_price_date < latest_trading_date:
                print(f"最新交易日 {latest_trading_date} 由策略使用 wsq 实时行情临时补齐。")
            return latest_trading_date

    print(
        "本地行情数据库需要更新："
        f"当前={latest_price_date or '无数据'}，目标={latest_complete_target_date}"
    )
    if latest_price_date is not None and not target_codes:
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
        *fields,
        "--price-option",
        price_option,
        "--adjusted",
        adjusted,
    ]
    subprocess.run(cmd, check=True)

    if target_codes:
        updated_coverage = count_complete_price_rows(
            target_codes,
            latest_complete_target_date,
            fields=fields,
            db_path=db_path,
            adjusted=adjusted,
        )
        if updated_coverage < min_coverage:
            raise RuntimeError(
                f"本地行情数据库更新后覆盖仍不足：{universe_name} "
                f"{latest_complete_target_date} {updated_coverage}/{len(target_codes)}，"
                "为避免使用残缺行情，已停止。"
            )
        print(
            f"本地行情数据库更新完成：{universe_name} "
            f"{latest_complete_target_date} {updated_coverage}/{len(target_codes)}"
        )
        return latest_trading_date

    updated_price_date = get_latest_price_date(db_path, adjusted=adjusted)
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
