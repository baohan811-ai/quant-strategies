import argparse
import os
import sqlite3
from datetime import datetime

import pandas as pd
from WindPy import w

from local_market_db import (
    MARKET_DB_PATH,
    WIND_LEVEL1_INDUSTRY_SYSTEM,
    init_market_db,
)


DEFAULT_UNIVERSE_NAME = "中证800"
DEFAULT_START_DATE = "2018-01-01"
DEFAULT_FIELD = "wicsname2024"
DEFAULT_INDUSTRY_TYPE = 1


def load_constituent_snapshots(conn, universe_name, start_date, end_date):
    snapshots = pd.read_sql_query(
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
    if snapshots.empty:
        raise RuntimeError(
            f"没有找到 {universe_name} 历史成分快照：{start_date} ~ {end_date}"
        )
    return snapshots


def existing_snapshot_count(
    conn,
    universe_name,
    classification_system,
    snapshot_date,
):
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM stock_industry_snapshot
        WHERE universe_name = ?
          AND classification_system = ?
          AND snapshot_date = ?
        """,
        (universe_name, classification_system, snapshot_date),
    ).fetchone()
    return int(row[0] or 0)


def fetch_industries(codes, snapshot_date, field, industry_type, batch_size):
    values = {}
    options = (
        f"tradeDate={pd.Timestamp(snapshot_date).strftime('%Y%m%d')};"
        f"industryType={industry_type};"
    )
    for start in range(0, len(codes), batch_size):
        batch = codes[start:start + batch_size]
        data = w.wss(batch, field, options)
        if data.ErrorCode != 0:
            message = ""
            if getattr(data, "Data", None) and data.Data[0]:
                message = str(data.Data[0][0])
            raise RuntimeError(
                f"行业拉取失败：date={snapshot_date}, batch={start}, "
                f"ErrorCode={data.ErrorCode}, message={message}"
            )
        batch_values = data.Data[0] if data.Data else [None] * len(batch)
        if len(batch_values) != len(batch):
            raise RuntimeError(
                f"行业返回维度异常：date={snapshot_date}, "
                f"codes={len(batch)}, values={len(batch_values)}"
            )
        values.update(dict(zip(batch, batch_values)))
    return values, options


def save_snapshot(
    conn,
    universe_name,
    classification_system,
    snapshot_df,
    field,
    source_options,
):
    updated_at = datetime.now().isoformat(timespec="seconds")
    rows = [
        (
            row.snapshot_date,
            classification_system,
            universe_name,
            row.wind_code,
            row.sec_name,
            row.industry_level1,
            field,
            source_options,
            updated_at,
        )
        for row in snapshot_df.itertuples(index=False)
    ]
    conn.executemany(
        """
        INSERT INTO stock_industry_snapshot (
            snapshot_date,
            classification_system,
            universe_name,
            wind_code,
            sec_name,
            industry_level1,
            source_field,
            source_options,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(
            snapshot_date,
            classification_system,
            universe_name,
            wind_code
        ) DO UPDATE SET
            sec_name = excluded.sec_name,
            industry_level1 = excluded.industry_level1,
            source_field = excluded.source_field,
            source_options = excluded.source_options,
            updated_at = excluded.updated_at
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def summarize(conn, universe_name, classification_system):
    summary = pd.read_sql_query(
        """
        SELECT
            snapshot_date,
            COUNT(*) AS row_count,
            SUM(CASE WHEN industry_level1 IS NOT NULL THEN 1 ELSE 0 END)
                AS non_null_count,
            COUNT(DISTINCT industry_level1) AS industry_count
        FROM stock_industry_snapshot
        WHERE universe_name = ?
          AND classification_system = ?
        GROUP BY snapshot_date
        ORDER BY snapshot_date
        """,
        conn,
        params=[universe_name, classification_system],
    )
    if summary.empty:
        print("未写入任何历史行业截面。")
        return
    total_rows = int(summary["row_count"].sum())
    total_non_null = int(summary["non_null_count"].sum())
    print(
        f"{universe_name} 历史行业截面汇总："
        f"{len(summary)} 个日期，"
        f"{summary['snapshot_date'].iloc[0]} ~ {summary['snapshot_date'].iloc[-1]}，"
        f"共 {total_rows} 行，非空 {total_non_null} 行，"
        f"完成度 {total_non_null / total_rows:.2%}，"
        f"单期行数 {int(summary['row_count'].min())}"
        f"~{int(summary['row_count'].max())}"
    )


def main():
    parser = argparse.ArgumentParser(description="按历史成分快照抓取月频Wind行业分类")
    parser.add_argument("--db-path", default=MARKET_DB_PATH)
    parser.add_argument("--universe-name", default=DEFAULT_UNIVERSE_NAME)
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument(
        "--end-date",
        default=datetime.today().strftime("%Y-%m-%d"),
    )
    parser.add_argument("--classification-system", default=WIND_LEVEL1_INDUSTRY_SYSTEM)
    parser.add_argument("--field", default=DEFAULT_FIELD)
    parser.add_argument("--industry-type", type=int, default=DEFAULT_INDUSTRY_TYPE)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--min-non-null-ratio", type=float, default=0.95)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.db_path), exist_ok=True)
    init_market_db(args.db_path)

    conn = sqlite3.connect(args.db_path)
    w.start()
    try:
        constituents = load_constituent_snapshots(
            conn,
            args.universe_name,
            args.start_date,
            args.end_date,
        )
        snapshot_dates = constituents["snapshot_date"].drop_duplicates().tolist()
        print(
            f"准备抓取 {args.universe_name} 历史行业截面："
            f"{len(snapshot_dates)} 个日期，"
            f"{snapshot_dates[0]} ~ {snapshot_dates[-1]}"
        )

        written_dates = 0
        skipped_dates = 0
        written_rows = 0
        for snapshot_date in snapshot_dates:
            snapshot_df = constituents[
                constituents["snapshot_date"] == snapshot_date
            ].copy()
            expected_count = len(snapshot_df)
            existing_count = existing_snapshot_count(
                conn,
                args.universe_name,
                args.classification_system,
                snapshot_date,
            )
            if not args.force and existing_count >= expected_count:
                skipped_dates += 1
                continue

            print(
                f"拉取行业截面：{snapshot_date}，"
                f"成分 {expected_count} 只，已有 {existing_count} 行"
            )
            industry_map, source_options = fetch_industries(
                snapshot_df["wind_code"].tolist(),
                snapshot_date,
                args.field,
                args.industry_type,
                args.batch_size,
            )
            snapshot_df["industry_level1"] = snapshot_df["wind_code"].map(industry_map)
            non_null_count = int(snapshot_df["industry_level1"].notna().sum())
            non_null_ratio = non_null_count / expected_count if expected_count else 0
            if non_null_ratio < args.min_non_null_ratio:
                raise RuntimeError(
                    f"{snapshot_date} 行业非空率 {non_null_ratio:.2%}，"
                    f"低于阈值 {args.min_non_null_ratio:.2%}，停止写入"
                )
            written_rows += save_snapshot(
                conn,
                args.universe_name,
                args.classification_system,
                snapshot_df,
                args.field,
                source_options,
            )
            written_dates += 1

        print(
            f"写入日期 {written_dates} 个，跳过已有 {skipped_dates} 个，"
            f"写入/更新 {written_rows} 行"
        )
        summarize(conn, args.universe_name, args.classification_system)
    finally:
        conn.close()
        w.close()


if __name__ == "__main__":
    main()
