import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta

from local_market_db import MARKET_DB_PATH, canonical_price_model_is_activated


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
LOG_DIR = os.path.join(BASE_DIR, "输出", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
FULL_ADJUSTED_REFRESH_INTERVAL_DAYS = 7
FULL_ADJUSTED_REFRESH_START_DATE = "2018-01-01"
FULL_ADJUSTED_REFRESH_METADATA_KEY = "full_adjusted_refresh:全部A股:last_run_date"
FULL_A_SHARE_SNAPSHOT_START_DATE = "2018-01-01"
FULL_A_SHARE_SECTOR_ID = "a001010100000000"
ADJUST_ANCHOR_CHECK_INTERVAL_DAYS = 7
ADJUST_ANCHOR_CHECK_METADATA_KEY = "adjust_anchor_check:全部A股:last_run_date"
ENABLE_FULL_ADJUSTED_REFRESH = os.environ.get("ENABLE_FULL_ADJUSTED_REFRESH") == "1"
ENABLE_ADJUST_ANCHOR_CHECK = os.environ.get("ENABLE_ADJUST_ANCHOR_CHECK", "1").lower() not in {"0", "false", "no"}


def run_step(name, args):
    print(f"\n===== {name} [{datetime.now().isoformat(timespec='seconds')}] =====")
    subprocess.run([sys.executable, *args], cwd=BASE_DIR, check=True)


def get_metadata(key):
    if not os.path.exists(MARKET_DB_PATH):
        return None
    with sqlite3.connect(MARKET_DB_PATH) as conn:
        row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_metadata(key, value):
    updated_at = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(MARKET_DB_PATH) as conn:
        conn.execute("""
            INSERT INTO metadata (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
        """, (key, str(value), updated_at))
        conn.commit()


def should_run_full_adjusted_refresh(today):
    if not ENABLE_FULL_ADJUSTED_REFRESH:
        return False
    if os.environ.get("FORCE_FULL_ADJUSTED_REFRESH") == "1":
        return True
    last_run_date = get_metadata(FULL_ADJUSTED_REFRESH_METADATA_KEY)
    if not last_run_date:
        return True
    return (today - datetime.strptime(last_run_date, "%Y-%m-%d").date()).days >= FULL_ADJUSTED_REFRESH_INTERVAL_DAYS


def should_run_adjust_anchor_check(today):
    if not ENABLE_ADJUST_ANCHOR_CHECK:
        return False
    if os.environ.get("FORCE_ADJUST_ANCHOR_CHECK") == "1":
        return True
    last_run_date = get_metadata(ADJUST_ANCHOR_CHECK_METADATA_KEY)
    if not last_run_date:
        return True
    return (today - datetime.strptime(last_run_date, "%Y-%m-%d").date()).days >= ADJUST_ANCHOR_CHECK_INTERVAL_DAYS


def main():
    update_script = os.path.join("脚本", "维护工具", "update_local_market_db.py")
    snapshot_script = os.path.join("脚本", "维护工具", "build_universe_constituent_snapshots.py")
    check_script = os.path.join("脚本", "维护工具", "check_local_market_db.py")
    eod_end_date = (datetime.today().date() - timedelta(days=1)).strftime("%Y-%m-%d")
    canonical_active = canonical_price_model_is_activated(MARKET_DB_PATH)
    if canonical_active:
        wind_budget = os.environ.get("WIND_DAILY_CANONICAL_CELL_BUDGET")
        if not wind_budget:
            raise RuntimeError(
                "权威行情模型已启用，但未设置 WIND_DAILY_CANONICAL_CELL_BUDGET。"
                "按准确性优先原则，未启动 Wind 请求且停止每日更新。"
            )
        canonical_script = os.path.join(
            "脚本", "维护工具", "build_canonical_market_db.py"
        )
        canonical_start_date = (
            datetime.strptime(eod_end_date, "%Y-%m-%d").date() - timedelta(days=4)
        ).strftime("%Y-%m-%d")
        run_step("更新A股原始行情与复权因子", [
            canonical_script,
            "--fetch",
            "--retry-failed",
            "--start-date", canonical_start_date,
            "--end-date", eod_end_date,
            "--raw-fields", "open", "high", "low", "close", "volume", "amt", "turn", "free_turn",
            "--batch-size", "500",
            "--wind-cell-budget", wind_budget,
        ])
    else:
        run_step("更新最近行情", [
            update_script,
            "--prices-from-wind",
            "--universe-name", "全部A股",
            "--end-date", eod_end_date,
            "--refresh-days", "5",
            "--price-fields", "open", "high", "low", "close", "volume", "amt", "turn", "free_turn",
            "--batch-size", "500",
            "--date-chunk", "Y",
            "--repair-adjust-drift",
            "--adjust-history-start-date", FULL_ADJUSTED_REFRESH_START_DATE,
        ])
    today = datetime.today().date()
    if not canonical_active and should_run_adjust_anchor_check(today):
        run_step("前复权锚点一致性校验", [
            update_script,
            "--smart-adjust-refresh",
            "--universe-name", "全部A股",
            "--end-date", eod_end_date,
            "--adjust-history-start-date", FULL_ADJUSTED_REFRESH_START_DATE,
            "--price-fields", "open", "high", "low", "close",
            "--check-days", "5",
            "--adjust-tolerance", "0.0001",
            "--deep-adjust-check",
            "--adjust-check-frequency", "Q",
            "--batch-size", "500",
            "--date-chunk", "Y",
        ])
        set_metadata(ADJUST_ANCHOR_CHECK_METADATA_KEY, today.strftime("%Y-%m-%d"))
    else:
        print(
            "\n===== 前复权锚点一致性校验：未到周期或已关闭，跳过 "
            f"[{datetime.now().isoformat(timespec='seconds')}] ====="
        )
    if canonical_active:
        print(
            "\n===== 前复权维护：由原始行情 + adjfactor 本地统一派生，"
            "不再滚动回刷 Wind PriceAdj=F "
            f"[{datetime.now().isoformat(timespec='seconds')}] ====="
        )
    else:
        print(
            "\n===== 除权除息修复：已由每日前复权边界检测与自动回刷接管，"
            "独立事件扫描停用 "
            f"[{datetime.now().isoformat(timespec='seconds')}] ====="
        )
    if not canonical_active and should_run_full_adjusted_refresh(today):
        run_step("全量回刷全部A股前复权价格", [
            update_script,
            "--prices-from-wind",
            "--force-price-refresh",
            "--universe-name", "全部A股",
            "--start-date", FULL_ADJUSTED_REFRESH_START_DATE,
            "--end-date", eod_end_date,
            "--price-fields", "open", "high", "low", "close",
            "--batch-size", "500",
            "--date-chunk", "Y",
        ])
        set_metadata(FULL_ADJUSTED_REFRESH_METADATA_KEY, today.strftime("%Y-%m-%d"))
    else:
        print(
            "\n===== 全量回刷全部A股前复权价格：未到周期，跳过 "
            f"[{datetime.now().isoformat(timespec='seconds')}] ====="
        )
    run_step("更新最近港股行情", [
        update_script,
        "--prices-from-wind",
        "--universe-name", "全部港股",
        "--sector-id", "a002010100000000",
        "--end-date", eod_end_date,
        "--refresh-days", "5",
        "--price-fields", "close", "volume",
        "--price-option", "PriceAdj=F;TradingCalendar=HKEX",
        "--adjusted", "F_HKEX",
        "--batch-size", "500",
        "--date-chunk", "ALL",
    ])
    run_step("更新最近标普500行情", [
        update_script,
        "--prices-from-wind",
        "--universe-name", "标普500",
        "--sector-id", "a005010800000000",
        "--end-date", eod_end_date,
        "--refresh-days", "5",
        "--price-fields", "close", "volume",
        "--price-option", "PriceAdj=F;TradingCalendar=NYSE",
        "--adjusted", "F_NYSE",
        "--batch-size", "500",
        "--date-chunk", "ALL",
    ])
    run_step("更新最近纳斯达克100行情", [
        update_script,
        "--prices-from-wind",
        "--universe-name", "纳斯达克100",
        "--sector-id", "1000009964000000",
        "--end-date", eod_end_date,
        "--refresh-days", "5",
        "--price-fields", "close", "volume",
        "--price-option", "PriceAdj=F;TradingCalendar=NYSE",
        "--adjusted", "F_NYSE",
        "--batch-size", "500",
        "--date-chunk", "ALL",
    ])
    run_step("季度更新万得一级行业", [
        update_script,
        "--industries-from-wind",
        "--universe-name", "全部A股",
        "--batch-size", "500",
    ])
    run_step("季度验证港股万得一级行业", [
        update_script,
        "--industries-from-wind",
        "--universe-name", "全部港股",
        "--sector-id", "a002010100000000",
        "--batch-size", "500",
    ])
    run_step("季度更新标普500万得一级行业", [
        update_script,
        "--industries-from-wind",
        "--universe-name", "标普500",
        "--sector-id", "a005010800000000",
        "--batch-size", "500",
    ])
    run_step("季度更新纳斯达克100万得一级行业", [
        update_script,
        "--industries-from-wind",
        "--universe-name", "纳斯达克100",
        "--sector-id", "1000009964000000",
        "--batch-size", "500",
    ])
    run_step("更新全部A股历史月频成分快照", [
        snapshot_script,
        "--universe-name", "全部A股",
        "--sector-id", FULL_A_SHARE_SECTOR_ID,
        "--start-date", FULL_A_SHARE_SNAPSHOT_START_DATE,
        "--end-date", eod_end_date,
        "--frequency", "M",
    ])
    run_step("更新基础基本面", [
        update_script,
        "--fundamentals",
    ])
    run_step("检查数据库", [
        check_script,
    ])


if __name__ == "__main__":
    main()
