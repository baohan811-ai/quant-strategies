from __future__ import annotations

import argparse
import sqlite3
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
STRATEGY_DIR = SCRIPT_DIR.parent.parent
FUNDAMENTAL_DB_PATH = STRATEGY_DIR / "缓存" / "全部A股_基础基本面.sqlite3"
MARKET_DB_PATH = STRATEGY_DIR / "缓存" / "本地行情数据库.sqlite3"

FIELDS = [
    "profitnotice_lastrptdate",
    "profitnotice_date",
    "profitnotice_style",
    "profitnotice_changemin",
    "profitnotice_changemax",
    "performanceexpress_lastrptdate",
    "performanceexpress_lastdate",
    "performanceexpress_date",
    "performanceexpress_np_yoy",
]


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS earnings_disclosures (
            wind_code TEXT NOT NULL,
            rpt_date TEXT NOT NULL,
            ann_date TEXT NOT NULL,
            disclosure_type TEXT NOT NULL,
            profit_yoy_min REAL,
            profit_yoy_max REAL,
            profit_yoy_mid REAL,
            disclosure_style TEXT,
            source_file TEXT,
            report_period_verified INTEGER NOT NULL DEFAULT 0,
            announcement_date_verified INTEGER NOT NULL DEFAULT 0,
            revision_version_verified INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (wind_code, rpt_date, ann_date, disclosure_type)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_earnings_disclosures_asof
        ON earnings_disclosures (ann_date, wind_code, rpt_date)
        """
    )
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(earnings_disclosures)").fetchall()
    }
    for column in [
        "report_period_verified",
        "announcement_date_verified",
        "revision_version_verified",
    ]:
        if column not in existing:
            conn.execute(
                f"ALTER TABLE earnings_disclosures ADD COLUMN {column} "
                "INTEGER NOT NULL DEFAULT 0"
            )


def historical_codes() -> list[str]:
    with sqlite3.connect(MARKET_DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT wind_code
            FROM universe_constituents_snapshot
            WHERE universe_name = '全部A股'
            UNION
            SELECT DISTINCT wind_code
            FROM stock_universe
            WHERE universe_name = '全部A股'
            ORDER BY wind_code
            """
        ).fetchall()
    return [str(row[0]) for row in rows]


def quarter_ends(start: str, end: str) -> list[pd.Timestamp]:
    start_date = pd.Timestamp(start)
    end_date = pd.Timestamp(end)
    values = pd.date_range(start_date, end_date, freq="QE")
    if start_date.is_quarter_end and start_date not in values:
        values = values.insert(0, start_date)
    return [value for value in values if start_date <= value <= end_date]


def as_date_text(value) -> str | None:
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else parsed.strftime("%Y-%m-%d")


def as_number(value) -> float | None:
    parsed = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(parsed) or not np.isfinite(parsed) else float(parsed)


def fetch_period(w, codes: list[str], report_period: pd.Timestamp, batch_size: int) -> list[tuple]:
    report_text = report_period.strftime("%Y-%m-%d")
    options = f"rptDate={report_period:%Y%m%d}"
    rows = []
    updated_at = pd.Timestamp.now().isoformat(timespec="seconds")
    for start in range(0, len(codes), batch_size):
        batch = codes[start:start + batch_size]
        data = w.wss(batch, ",".join(FIELDS), options)
        if data.ErrorCode != 0:
            raise RuntimeError(
                f"Wind WSS failed: report={report_text}, batch={start}, ErrorCode={data.ErrorCode}"
            )
        values = {str(field).lower(): column for field, column in zip(data.Fields, data.Data)}
        for index, code in enumerate(data.Codes):
            notice_report = as_date_text(values["profitnotice_lastrptdate"][index])
            notice_date = as_date_text(values["profitnotice_date"][index])
            notice_too_early = (
                notice_date is not None
                and (report_period - pd.Timestamp(notice_date)).days > 366
            )
            if notice_report == report_text and notice_date and not notice_too_early:
                yoy_min = as_number(values["profitnotice_changemin"][index])
                yoy_max = as_number(values["profitnotice_changemax"][index])
                available = [value for value in [yoy_min, yoy_max] if value is not None]
                if available:
                    rows.append((
                        code, report_text, notice_date, "notice",
                        yoy_min / 100 if yoy_min is not None else None,
                        yoy_max / 100 if yoy_max is not None else None,
                        float(np.mean(available)) / 100,
                        values["profitnotice_style"][index],
                        "Wind WSS historical backfill",
                        updated_at,
                    ))

            express_report = as_date_text(values["performanceexpress_lastrptdate"][index])
            express_last_date = as_date_text(values["performanceexpress_lastdate"][index])
            express_first_date = as_date_text(values["performanceexpress_date"][index])
            express_yoy = as_number(values["performanceexpress_np_yoy"][index])
            # Wind快报值可能经过后续修正，回补时用最新披露日作为可用日，
            # 而不把最终值提前到首次披露日。
            express_available_date = express_last_date or express_first_date
            express_too_early = (
                express_available_date is not None
                and (report_period - pd.Timestamp(express_available_date)).days > 366
            )
            if (
                express_report == report_text
                and express_available_date
                and not express_too_early
                and express_yoy is not None
            ):
                rows.append((
                    code, report_text, express_available_date, "express",
                    express_yoy / 100, express_yoy / 100, express_yoy / 100,
                    "业绩快报",
                    "Wind WSS historical backfill",
                    updated_at,
                ))
        print(
            f"{report_text}: {min(start + batch_size, len(codes))}/{len(codes)}",
            flush=True,
        )
    return rows


def upsert_rows(rows: list[tuple]) -> None:
    if not rows:
        return
    with sqlite3.connect(FUNDAMENTAL_DB_PATH) as conn:
        ensure_table(conn)
        conn.executemany(
            """
            INSERT INTO earnings_disclosures (
                wind_code, rpt_date, ann_date, disclosure_type,
                profit_yoy_min, profit_yoy_max, profit_yoy_mid,
                disclosure_style, source_file, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(wind_code, rpt_date, ann_date, disclosure_type)
            DO UPDATE SET
                profit_yoy_min = excluded.profit_yoy_min,
                profit_yoy_max = excluded.profit_yoy_max,
                profit_yoy_mid = excluded.profit_yoy_mid,
                disclosure_style = excluded.disclosure_style,
                source_file = excluded.source_file,
                updated_at = excluded.updated_at
            """,
            rows,
        )
        conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="回补A股历史业绩预告与业绩快报点时缓存")
    parser.add_argument("--start-report", default="2021-12-31")
    parser.add_argument("--end-report", default="2026-06-30")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()

    periods = quarter_ends(args.start_report, args.end_report)
    codes = historical_codes()
    print(f"股票数: {len(codes)} | 报告期数: {len(periods)}", flush=True)

    from WindPy import w

    start_result = w.start()
    if getattr(start_result, "ErrorCode", 0) != 0:
        raise RuntimeError(f"Wind启动失败: {getattr(start_result, 'ErrorCode', None)}")
    total_rows = 0
    try:
        for period in periods:
            rows = fetch_period(w, codes, period, args.batch_size)
            upsert_rows(rows)
            total_rows += len(rows)
            notice_count = sum(row[3] == "notice" for row in rows)
            express_count = sum(row[3] == "express" for row in rows)
            print(
                f"{period:%Y-%m-%d} 完成: 预告 {notice_count}, 快报 {express_count}",
                flush=True,
            )
    finally:
        w.close()
    print(f"回补完成，本次写入/更新 {total_rows} 条。", flush=True)


if __name__ == "__main__":
    main()
