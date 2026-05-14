import os
import subprocess
import sys
from datetime import datetime


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
LOG_DIR = os.path.join(BASE_DIR, "输出", "logs")
os.makedirs(LOG_DIR, exist_ok=True)


def run_step(name, args):
    print(f"\n===== {name} [{datetime.now().isoformat(timespec='seconds')}] =====")
    subprocess.run([sys.executable, *args], cwd=BASE_DIR, check=True)


def main():
    update_script = os.path.join("脚本", "update_local_market_db.py")
    check_script = os.path.join("脚本", "check_local_market_db.py")

    run_step("更新最近行情", [
        update_script,
        "--prices-from-wind",
        "--universe-name", "全部A股",
        "--refresh-days", "15",
        "--price-fields", "open", "high", "low", "close", "volume", "amt",
        "--batch-size", "500",
        "--date-chunk", "ALL",
    ])
    run_step("智能复权校验", [
        update_script,
        "--smart-adjust-refresh",
        "--universe-name", "全部A股",
        "--adjust-history-start-date", "2022-01-01",
        "--price-fields", "open", "high", "low", "close", "volume", "amt",
        "--check-days", "10",
        "--adjust-tolerance", "0.0001",
        "--batch-size", "500",
        "--date-chunk", "Y",
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

