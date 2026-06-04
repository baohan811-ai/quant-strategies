import argparse
import importlib.util
import itertools
import sys
from pathlib import Path

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = SCRIPT_DIR.parent
BASE_DIR = SCRIPTS_DIR.parent
OUTPUT_DIR = BASE_DIR / "输出"
OUTPUT_DIR.mkdir(exist_ok=True)

# 每一阶段完成后，将选定组合固化在这里，供后一阶段使用。
BASELINE = {
    "MAX_HOLDINGS": 20,
    "SIGNAL_MAX_DAILY_RETURN": 0.065,
    "PEAK_RETRACE_SELL_DRAWDOWN": 0.1175,
    "LOW_EFFICIENCY_MIN_HOLDING_DAYS": 60,
    "LOW_EFFICIENCY_MAX_PROFIT_THRESHOLD": 0.05,
    "LOW_EFFICIENCY_CURRENT_PROFIT_THRESHOLD": 0.00,
    "MA5_MA60_GAP_WEIGHT": 1.0,
    "MA60_5D_TREND_WEIGHT": 1.0,
    "VOLUME_RATIO_SCORE_WEIGHT": 0.25,
    "SHORT_MA_DAYS": 5,
    "MID_MA_DAYS": 60,
    "LONG_MA_DAYS": 120,
}

LOW_EFFICIENCY_GRID = {
    "LOW_EFFICIENCY_MIN_HOLDING_DAYS": [40, 60, 80, 100],
    "LOW_EFFICIENCY_MAX_PROFIT_THRESHOLD": [0.03, 0.05, 0.07],
    "LOW_EFFICIENCY_CURRENT_PROFIT_THRESHOLD": [-0.03, 0.00, 0.03],
}

# 评分只比较相对比例。以 MA5/MA60 偏离项为 1.0，避免等价倍数组合。
SCORE_WEIGHT_GRID = {
    "MA5_MA60_GAP_WEIGHT": [1.0],
    "MA60_5D_TREND_WEIGHT": [0.0, 0.5, 1.0, 1.5],
    "VOLUME_RATIO_SCORE_WEIGHT": [0.0, 0.25, 0.5, 0.75, 1.0],
}

MA_PERIOD_GRID = {
    "SHORT_MA_DAYS": [3, 5, 8],
    "MID_MA_DAYS": [40, 60, 80],
    "LONG_MA_DAYS": [100, 120, 160],
}


def load_optimizer():
    optimizer_path = SCRIPT_DIR / "测试_高位回撤参数优化.py"
    sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location("peak_retrace_optimizer", optimizer_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def iter_grid(grid):
    keys = list(grid)
    for values in itertools.product(*(grid[key] for key in keys)):
        yield dict(zip(keys, values))


def apply_config(optimizer, config):
    for key, value in config.items():
        if key == "PEAK_RETRACE_SELL_DRAWDOWN":
            continue
        setattr(optimizer, key, value)
    optimizer.INITIAL_WEIGHT = 1 / optimizer.MAX_HOLDINGS


def run_stage(stage):
    optimizer = load_optimizer()
    fields, universe_member, code_to_name = optimizer.load_inputs()
    if stage == "low_efficiency":
        grid = LOW_EFFICIENCY_GRID
        output_name = "参数优化_中证800_阶段1_低效持仓退出.csv"
    elif stage == "score_weights":
        grid = SCORE_WEIGHT_GRID
        output_name = "参数优化_中证800_阶段2_评分权重.csv"
    elif stage == "ma_periods":
        grid = MA_PERIOD_GRID
        output_name = "参数优化_中证800_阶段3_均线周期.csv"
    else:
        raise ValueError(f"不支持的阶段: {stage}")

    records = []
    combinations = list(iter_grid(grid))
    for index, stage_config in enumerate(combinations, start=1):
        config = {**BASELINE, **stage_config}
        apply_config(optimizer, config)
        buy_signal, candidate_score = optimizer.build_signals(
            fields["close"],
            fields["volume"],
            universe_member,
        )
        print(f"[{index}/{len(combinations)}] {stage_config}")
        result = optimizer.run_backtest(
            config["PEAK_RETRACE_SELL_DRAWDOWN"],
            fields,
            buy_signal,
            candidate_score,
            code_to_name,
        )
        records.append({**stage_config, **result})

    results = pd.DataFrame(records).sort_values(
        ["夏普比率", "年化收益", "最大回撤"],
        ascending=[False, False, False],
    )
    output_path = OUTPUT_DIR / output_name
    results.to_csv(output_path, index=False, encoding="utf-8-sig")
    print(f"\n【{stage} Top 20】")
    print(results.head(20).to_string(index=False))
    print(f"\n输出完成：{output_path}")


def main():
    parser = argparse.ArgumentParser(description="中证800策略参数分阶段本地优化")
    parser.add_argument(
        "stage",
        choices=["low_efficiency", "score_weights", "ma_periods"],
    )
    args = parser.parse_args()
    run_stage(args.stage)


if __name__ == "__main__":
    main()
