import argparse
import importlib.util
import sys
from pathlib import Path

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = SCRIPT_DIR.parent
BASE_DIR = SCRIPTS_DIR.parent
OUTPUT_DIR = BASE_DIR / "输出"
OUTPUT_DIR.mkdir(exist_ok=True)

RETRACE_GRID = [0.11, 0.115, 0.1175, 0.12, 0.1225, 0.125, 0.13]
SIGNAL_MAX_RETURN_GRID = [0.03, 0.04, 0.05, 0.06, 0.07]
MAX_HOLDINGS_GRID = [15, 20, 25]

FINE_RETRACE_GRID = [0.1125, 0.115, 0.11625, 0.1175, 0.11875, 0.12, 0.1225]
FINE_SIGNAL_MAX_RETURN_GRID = [0.055, 0.06, 0.065, 0.07, 0.075]
FINE_MAX_HOLDINGS_GRID = [18, 19, 20, 21, 22]


def load_optimizer():
    optimizer_path = SCRIPT_DIR / "测试_高位回撤参数优化.py"
    sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location("peak_retrace_optimizer", optimizer_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description="中证800核心三参数本地联合优化")
    parser.add_argument("--fine", action="store_true", help="围绕粗扫强势区域做局部细扫")
    args = parser.parse_args()

    if args.fine:
        retrace_grid = FINE_RETRACE_GRID
        signal_max_return_grid = FINE_SIGNAL_MAX_RETURN_GRID
        max_holdings_grid = FINE_MAX_HOLDINGS_GRID
        output_name = "参数优化_中证800_核心三参数联合细扫.csv"
    else:
        retrace_grid = RETRACE_GRID
        signal_max_return_grid = SIGNAL_MAX_RETURN_GRID
        max_holdings_grid = MAX_HOLDINGS_GRID
        output_name = "参数优化_中证800_核心三参数联合粗扫.csv"

    optimizer = load_optimizer()
    fields, universe_member, code_to_name = optimizer.load_inputs()
    records = []

    for max_holdings in max_holdings_grid:
        optimizer.MAX_HOLDINGS = max_holdings
        optimizer.INITIAL_WEIGHT = 1 / max_holdings
        for signal_max_return in signal_max_return_grid:
            optimizer.SIGNAL_MAX_DAILY_RETURN = signal_max_return
            buy_signal, candidate_score = optimizer.build_signals(
                fields["close"],
                fields["volume"],
                universe_member,
            )
            for retrace_drawdown in retrace_grid:
                print(
                    "联合回测："
                    f"持仓={max_holdings}，"
                    f"信号涨幅上限={signal_max_return:.1%}，"
                    f"前高回撤={retrace_drawdown:.2%}"
                )
                result = optimizer.run_backtest(
                    retrace_drawdown,
                    fields,
                    buy_signal,
                    candidate_score,
                    code_to_name,
                )
                records.append({
                    "最大持仓数": max_holdings,
                    "金叉日最大涨幅": signal_max_return,
                    **result,
                })

    results = pd.DataFrame(records).sort_values(
        ["夏普比率", "年化收益", "最大回撤"],
        ascending=[False, False, False],
    )
    output_path = OUTPUT_DIR / output_name
    results.to_csv(output_path, index=False, encoding="utf-8-sig")

    columns = [
        "最大持仓数",
        "金叉日最大涨幅",
        "前高回撤卖出比例",
        "累计收益",
        "年化收益",
        "年化波动",
        "夏普比率",
        "最大回撤",
        "平均持仓数",
        "年化换手率",
        "平仓笔数",
    ]
    print("\n【核心三参数联合优化 Top 30】")
    print(results[columns].head(30).to_string(index=False))
    print(f"\n输出完成：{output_path}")


if __name__ == "__main__":
    main()
