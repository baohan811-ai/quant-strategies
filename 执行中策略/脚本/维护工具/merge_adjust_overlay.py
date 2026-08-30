"""将已验证的前复权 close 覆盖库事务性合并回主行情库。

覆盖库只保存 Wind 重拉的 close。合并时按同日 new_close / old_close
比例同步缩放 open/high/low，保证 OHLC 处于同一前复权口径。
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path


def quick_check(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        result = conn.execute("PRAGMA quick_check").fetchone()
    if not result or result[0] != "ok":
        raise RuntimeError(f"SQLite quick_check 失败：{path}: {result}")


def merge(main_db: Path, overlay_db: Path, backup_db: Path) -> dict[str, int]:
    for path in (main_db, overlay_db, backup_db):
        if not path.exists():
            raise FileNotFoundError(path)
    if main_db.resolve() == overlay_db.resolve():
        raise ValueError("主库和覆盖库不能是同一文件")

    quick_check(backup_db)
    quick_check(overlay_db)
    now = datetime.now().isoformat(timespec="seconds")
    conn = sqlite3.connect(main_db, timeout=120)
    try:
        conn.execute("PRAGMA busy_timeout=120000")
        conn.execute("ATTACH DATABASE ? AS fix", (str(overlay_db),))
        overlay_rows, overlay_codes = conn.execute("""
            SELECT COUNT(*), COUNT(DISTINCT wind_code)
            FROM fix.daily_prices
            WHERE adjusted = 'F' AND close IS NOT NULL
        """).fetchone()
        matched_rows = conn.execute("""
            SELECT COUNT(*)
            FROM daily_prices AS m
            JOIN fix.daily_prices AS f
              ON f.trade_date = m.trade_date
             AND f.wind_code = m.wind_code
             AND f.adjusted = m.adjusted
            WHERE f.adjusted = 'F' AND f.close IS NOT NULL
        """).fetchone()[0]
        missing_rows = overlay_rows - matched_rows
        incomplete_missing_rows = conn.execute("""
            SELECT COUNT(*)
            FROM fix.daily_prices AS f
            LEFT JOIN daily_prices AS m
              ON f.trade_date = m.trade_date
             AND f.wind_code = m.wind_code
             AND f.adjusted = m.adjusted
            WHERE f.adjusted = 'F' AND f.close IS NOT NULL AND m.wind_code IS NULL
              AND (f.open IS NULL OR f.high IS NULL OR f.low IS NULL
                   OR f.volume IS NULL OR f.amt IS NULL)
        """).fetchone()[0]
        if overlay_rows == 0 or incomplete_missing_rows:
            raise RuntimeError(
                f"覆盖库行数={overlay_rows}，主库缺失行数={missing_rows}，"
                f"其中OHLCVA不完整={incomplete_missing_rows}，拒绝合并"
            )

        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP TABLE IF EXISTS temp.adjust_fix_rows")
        conn.execute("""
            CREATE TEMP TABLE adjust_fix_rows AS
            SELECT
                f.trade_date,
                f.wind_code,
                f.adjusted,
                m.rowid AS main_rowid,
                m.close AS old_close,
                f.open AS fix_open,
                f.high AS fix_high,
                f.low AS fix_low,
                f.close AS new_close,
                f.volume AS fix_volume,
                f.amt AS fix_amt
            FROM fix.daily_prices AS f
            LEFT JOIN daily_prices AS m
              ON f.trade_date = m.trade_date
             AND f.wind_code = m.wind_code
             AND f.adjusted = m.adjusted
            WHERE f.adjusted = 'F' AND f.close IS NOT NULL
        """)
        conn.execute("""
            CREATE UNIQUE INDEX temp.idx_adjust_fix_rows
            ON adjust_fix_rows(trade_date, wind_code, adjusted)
        """)
        changed_rows = conn.execute("""
            SELECT COUNT(*) FROM adjust_fix_rows
            WHERE main_rowid IS NULL OR old_close IS NULL
               OR ABS(old_close / NULLIF(new_close, 0) - 1) > 0.0000000001
        """).fetchone()[0]

        conn.execute("""
            UPDATE daily_prices AS m
            SET
                open = CASE
                    WHEN m.open IS NOT NULL AND r.old_close IS NOT NULL AND r.old_close != 0
                    THEN m.open * r.new_close / r.old_close ELSE m.open END,
                high = CASE
                    WHEN m.high IS NOT NULL AND r.old_close IS NOT NULL AND r.old_close != 0
                    THEN m.high * r.new_close / r.old_close ELSE m.high END,
                low = CASE
                    WHEN m.low IS NOT NULL AND r.old_close IS NOT NULL AND r.old_close != 0
                    THEN m.low * r.new_close / r.old_close ELSE m.low END,
                close = r.new_close,
                updated_at = ?
            FROM adjust_fix_rows AS r
            WHERE m.trade_date = r.trade_date
              AND m.wind_code = r.wind_code
              AND m.adjusted = r.adjusted
              AND r.main_rowid IS NOT NULL
        """, (now,))

        conn.execute("""
            INSERT INTO daily_prices(
                trade_date, wind_code, open, high, low, close, volume, amt,
                turn, adjusted, updated_at, free_turn
            )
            SELECT
                trade_date, wind_code, fix_open, fix_high, fix_low, new_close,
                fix_volume, fix_amt, NULL, adjusted, ?, NULL
            FROM adjust_fix_rows
            WHERE main_rowid IS NULL
        """, (now,))

        mismatches = conn.execute("""
            SELECT COUNT(*) FROM fix.daily_prices AS f
            LEFT JOIN daily_prices AS m
              ON f.trade_date = m.trade_date
             AND f.wind_code = m.wind_code
             AND f.adjusted = m.adjusted
            WHERE f.adjusted = 'F' AND f.close IS NOT NULL
              AND (m.wind_code IS NULL
                   OR ABS(m.close / NULLIF(f.close, 0) - 1) > 0.0000000001)
        """).fetchone()[0]
        invalid_ohlc = conn.execute("""
            SELECT COUNT(*)
            FROM daily_prices AS m
            JOIN adjust_fix_rows AS r
              ON r.trade_date = m.trade_date
             AND r.wind_code = m.wind_code
             AND r.adjusted = m.adjusted
            WHERE (m.low IS NOT NULL AND m.high IS NOT NULL AND m.low > m.high)
               OR (m.open IS NOT NULL AND m.low IS NOT NULL AND m.open < m.low - 1e-8)
               OR (m.open IS NOT NULL AND m.high IS NOT NULL AND m.open > m.high + 1e-8)
               OR (m.close IS NOT NULL AND m.low IS NOT NULL AND m.close < m.low - 1e-8)
               OR (m.close IS NOT NULL AND m.high IS NOT NULL AND m.close > m.high + 1e-8)
        """).fetchone()[0]
        if mismatches or invalid_ohlc:
            raise RuntimeError(
                f"合并后校验失败：close不一致={mismatches}, OHLC逻辑异常={invalid_ohlc}"
            )

        metadata = {
            "adjust_overlay_merge:last_run_at": now,
            "adjust_overlay_merge:overlay_db": str(overlay_db.resolve()),
            "adjust_overlay_merge:backup_db": str(backup_db.resolve()),
            "adjust_overlay_merge:rows": str(overlay_rows),
            "adjust_overlay_merge:codes": str(overlay_codes),
        }
        conn.executemany("""
            INSERT INTO metadata(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
        """, [(key, value, now) for key, value in metadata.items()])
        conn.commit()
        return {
            "overlay_rows": int(overlay_rows),
            "overlay_codes": int(overlay_codes),
            "changed_rows": int(changed_rows),
            "inserted_rows": int(missing_rows),
            "mismatches": int(mismatches),
            "invalid_ohlc": int(invalid_ohlc),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--main-db", type=Path, required=True)
    parser.add_argument("--overlay-db", type=Path, required=True)
    parser.add_argument("--backup-db", type=Path, required=True)
    args = parser.parse_args()
    result = merge(args.main_db, args.overlay_db, args.backup_db)
    print(result)


if __name__ == "__main__":
    main()
