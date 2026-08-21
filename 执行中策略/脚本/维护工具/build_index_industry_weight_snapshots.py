"""构建指数月度万得一级行业权重历史截面。"""

import argparse
import os
import sqlite3
from datetime import datetime

import pandas as pd
from WindPy import w

from local_market_db import MARKET_DB_PATH, WIND_LEVEL1_INDUSTRY_SYSTEM, init_market_db


DEFAULT_INDEX_CODE = "000300.SH"
DEFAULT_START_DATE = "2022-01-01"


def get_month_end_trading_days(start_date, end_date):
    data = w.tdays(start_date, end_date, "")
    if data.ErrorCode != 0 or not data.Data or not data.Data[0]:
        raise RuntimeError(f"交易日历拉取失败: ErrorCode={data.ErrorCode}")
    days = pd.DatetimeIndex(pd.to_datetime(data.Data[0])).sort_values()
    return [value.strftime("%Y-%m-%d") for value in pd.Series(days, index=days).groupby(days.to_period("M")).last()]


def get_field(data, name):
    mapping = {str(field).lower(): values for field, values in zip(data.Fields, data.Data)}
    if name.lower() not in mapping:
        raise RuntimeError(f"Wind 返回缺少字段 {name}: {data.Fields}")
    return mapping[name.lower()]


def fetch_snapshot(index_code, snapshot_date, batch_size):
    result = w.wset(
        "indexconstituent",
        f"date={snapshot_date.replace('-', '')};windcode={index_code};field=wind_code,sec_name,i_weight",
    )
    if result.ErrorCode != 0 or not result.Data:
        raise RuntimeError(f"指数成分权重获取失败: {snapshot_date}, ErrorCode={result.ErrorCode}")
    frame = pd.DataFrame({
        "wind_code": get_field(result, "wind_code"),
        "sec_name": get_field(result, "sec_name"),
        "constituent_weight": pd.to_numeric(get_field(result, "i_weight"), errors="coerce") / 100.0,
    })
    industry_map = {}
    options = f"tradeDate={snapshot_date.replace('-', '')};industryType=1;"
    codes = frame["wind_code"].tolist()
    for start in range(0, len(codes), batch_size):
        batch = codes[start:start + batch_size]
        industry = w.wss(batch, "wicsname2024", options)
        if industry.ErrorCode != 0 or not industry.Data:
            raise RuntimeError(
                f"行业获取失败: {snapshot_date}, batch={start}, ErrorCode={industry.ErrorCode}"
            )
        industry_map.update(dict(zip(industry.Codes, industry.Data[0])))
    frame["industry_level1"] = frame["wind_code"].map(industry_map)
    return frame, options


def snapshot_exists(conn, index_code, snapshot_date):
    row = conn.execute(
        """
        SELECT COUNT(*) FROM index_industry_weight_snapshot
        WHERE index_code = ? AND classification_system = ? AND snapshot_date = ?
        """,
        [index_code, WIND_LEVEL1_INDUSTRY_SYSTEM, snapshot_date],
    ).fetchone()
    return bool(row and row[0])


def save_snapshot(conn, index_code, snapshot_date, frame, source_options):
    usable = frame.dropna(subset=["industry_level1", "constituent_weight"]).copy()
    coverage = usable["constituent_weight"].sum()
    if coverage < 0.95:
        raise RuntimeError(f"{snapshot_date} 有行业归属的指数权重仅 {coverage:.2%}，停止写入")
    summary = usable.groupby("industry_level1").agg(
        industry_weight=("constituent_weight", "sum"),
        constituent_count=("wind_code", "size"),
    )
    summary["industry_weight"] /= summary["industry_weight"].sum()
    updated_at = datetime.now().isoformat(timespec="seconds")
    rows = [
        (
            snapshot_date, index_code, WIND_LEVEL1_INDUSTRY_SYSTEM, industry,
            float(row.industry_weight), int(row.constituent_count),
            f"indexconstituent.i_weight;wicsname2024;{source_options}", updated_at,
        )
        for industry, row in summary.iterrows()
    ]
    conn.executemany(
        """
        INSERT INTO index_industry_weight_snapshot (
            snapshot_date, index_code, classification_system, industry_level1,
            industry_weight, constituent_count, source_field, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(snapshot_date, index_code, classification_system, industry_level1)
        DO UPDATE SET industry_weight=excluded.industry_weight,
                      constituent_count=excluded.constituent_count,
                      source_field=excluded.source_field,
                      updated_at=excluded.updated_at
        """,
        rows,
    )
    conn.commit()
    return len(rows), coverage


def main():
    parser = argparse.ArgumentParser(description="构建指数月度万得一级行业权重历史截面")
    parser.add_argument("--db-path", default=MARKET_DB_PATH)
    parser.add_argument("--index-code", default=DEFAULT_INDEX_CODE)
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument("--end-date", default=datetime.today().strftime("%Y-%m-%d"))
    parser.add_argument("--batch-size", type=int, default=300)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.db_path), exist_ok=True)
    init_market_db(args.db_path)
    start_result = w.start()
    if getattr(start_result, "ErrorCode", 0) != 0:
        raise RuntimeError(f"Wind 启动失败: {getattr(start_result, 'ErrorCode', None)}")
    conn = sqlite3.connect(args.db_path)
    try:
        dates = get_month_end_trading_days(args.start_date, args.end_date)
        written = skipped = rows = 0
        for snapshot_date in dates:
            if not args.force and snapshot_exists(conn, args.index_code, snapshot_date):
                skipped += 1
                continue
            print(f"拉取 {args.index_code} 行业权重：{snapshot_date}")
            frame, options = fetch_snapshot(args.index_code, snapshot_date, args.batch_size)
            row_count, coverage = save_snapshot(conn, args.index_code, snapshot_date, frame, options)
            print(f"写入 {row_count} 个行业，成分权重覆盖 {coverage:.2%}")
            written += 1
            rows += row_count
        print(f"完成：写入日期 {written}，跳过已有 {skipped}，写入行业行 {rows}")
    finally:
        conn.close()
        w.close()


if __name__ == "__main__":
    main()
