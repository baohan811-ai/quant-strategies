"""将主库缺失、但前复权覆盖库已有 close 的历史K线补成完整 OHLCVA。"""

from __future__ import annotations

import argparse
import importlib.util
import sqlite3
import sys
from pathlib import Path


FIELDS = ["open", "high", "low", "volume", "amt"]


def load_updater(path: Path):
    spec = importlib.util.spec_from_file_location("local_market_updater", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--main-db", type=Path, required=True)
    parser.add_argument("--overlay-db", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()

    with sqlite3.connect(args.main_db) as conn:
        conn.execute("ATTACH DATABASE ? AS fix", (str(args.overlay_db),))
        rows = conn.execute("""
            SELECT DISTINCT f.wind_code
            FROM fix.daily_prices AS f
            LEFT JOIN daily_prices AS m
              ON m.trade_date = f.trade_date
             AND m.wind_code = f.wind_code
             AND m.adjusted = f.adjusted
            WHERE f.adjusted = 'F' AND f.close IS NOT NULL AND m.wind_code IS NULL
            ORDER BY f.wind_code
        """).fetchall()
        date_range = conn.execute("""
            SELECT MIN(f.trade_date), MAX(f.trade_date)
            FROM fix.daily_prices AS f
            LEFT JOIN daily_prices AS m
              ON m.trade_date = f.trade_date
             AND m.wind_code = f.wind_code
             AND m.adjusted = f.adjusted
            WHERE f.adjusted = 'F' AND f.close IS NOT NULL AND m.wind_code IS NULL
        """).fetchone()
    codes = [row[0] for row in rows]
    if not codes:
        print("没有需要补齐的覆盖库历史行")
        return
    start_date, end_date = date_range
    print(
        f"补齐覆盖库历史K线：{len(codes)}只，{start_date}~{end_date}，"
        f"字段={','.join(FIELDS)}"
    )

    updater_path = Path(__file__).resolve().with_name("update_local_market_db.py")
    updater = load_updater(updater_path)
    from WindPy import w

    result = w.start()
    if getattr(result, "ErrorCode", 0) != 0:
        raise RuntimeError(f"Wind启动失败：{getattr(result, 'ErrorCode', None)}")
    conn = sqlite3.connect(args.overlay_db)
    try:
        updater.refresh_codes_from_wind(
            w,
            conn,
            codes,
            FIELDS,
            start_date,
            end_date,
            args.batch_size,
            "Y",
        )
    finally:
        conn.close()
        w.close()

    with sqlite3.connect(args.main_db) as conn:
        conn.execute("ATTACH DATABASE ? AS fix", (str(args.overlay_db),))
        incomplete = conn.execute("""
            SELECT COUNT(*)
            FROM fix.daily_prices AS f
            LEFT JOIN daily_prices AS m
              ON m.trade_date = f.trade_date
             AND m.wind_code = f.wind_code
             AND m.adjusted = f.adjusted
            WHERE f.adjusted = 'F' AND f.close IS NOT NULL AND m.wind_code IS NULL
              AND (f.open IS NULL OR f.high IS NULL OR f.low IS NULL
                   OR f.volume IS NULL OR f.amt IS NULL)
        """).fetchone()[0]
    if incomplete:
        raise RuntimeError(f"补齐后仍有 {incomplete} 行缺失 OHLCVA")
    print("覆盖库缺失历史行已补成完整 OHLCVA")


if __name__ == "__main__":
    main()
