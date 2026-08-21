"""
标普500成分股的行业比较＋混合评分（无组合权重版）。

评分口径参考《行业比较混合评分_中证800成分股评分.py》，保留八维
混合评分，但使用标普500独立基本面库和 NYSE 行情口径。

成分股和行业归属均优先使用截止日不晚于 as_of 的最新历史快照。
美股 PB、自由流通市值和 A 股业绩预告字段当前不可用；
PB 保持缺失，市值用总市值兜底，业绩预告修正禁用。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

import 行业比较混合评分_全A股质量成长800指数 as base


SP500_UNIVERSE_NAME = "标普500"
SP500_FUNDAMENTAL_DB_PATH = base.BASE_DIR / "缓存" / "标普500_基础基本面.sqlite3"
OUTPUT_DIR = base.BASE_DIR / "输出" / "标普500行业比较混合评分"
PRICE_ADJUSTED = "F_NYSE"


def configure_sp500_data_source() -> None:
    if not SP500_FUNDAMENTAL_DB_PATH.exists():
        raise FileNotFoundError(f"标普500基本面库不存在: {SP500_FUNDAMENTAL_DB_PATH}")
    base.FUNDAMENTAL_DB_PATH = SP500_FUNDAMENTAL_DB_PATH


def load_sp500_universe(as_of: str) -> tuple[pd.DataFrame, str]:
    """读取截止日最新的标普500历史成分快照。"""
    with sqlite3.connect(base.MARKET_DB_PATH) as conn:
        snapshot_date = conn.execute(
            """
            SELECT MAX(snapshot_date)
            FROM universe_constituents_snapshot
            WHERE universe_name = ? AND snapshot_date <= ?
            """,
            [SP500_UNIVERSE_NAME, as_of],
        ).fetchone()[0]
        if snapshot_date is None:
            raise RuntimeError(f"截止 {as_of} 没有标普500历史成分快照")
        universe = pd.read_sql_query(
            """
            SELECT wind_code, sec_name
            FROM universe_constituents_snapshot
            WHERE universe_name = ? AND snapshot_date = ?
            ORDER BY wind_code
            """,
            conn,
            params=[SP500_UNIVERSE_NAME, snapshot_date],
        )
    universe = universe.drop_duplicates("wind_code", keep="last")
    if universe.empty:
        raise RuntimeError(f"标普500在 {snapshot_date} 的成分快照为空")
    snapshot_label = str(snapshot_date)
    universe["截止日"] = as_of
    universe["成分股截面日"] = snapshot_label
    universe["universe_source"] = f"标普500历史成分快照:{snapshot_label}"
    return universe, snapshot_label


def load_sp500_momentum_features(codes: list[str], as_of: str) -> pd.DataFrame:
    """使用 F_NYSE 收盘价计算12-1个月和6-1个月动量。"""
    as_of_date = pd.Timestamp(as_of)
    targets = {
        "close_1m": as_of_date - pd.DateOffset(months=1),
        "close_6m": as_of_date - pd.DateOffset(months=6),
        "close_12m": as_of_date - pd.DateOffset(months=12),
    }
    code_set = set(codes)
    result = pd.DataFrame({"wind_code": codes})
    with sqlite3.connect(base.MARKET_DB_PATH) as conn:
        for column, target in targets.items():
            start = target - pd.Timedelta(days=20)
            prices = pd.read_sql_query(
                """
                SELECT trade_date, wind_code, close
                FROM daily_prices
                WHERE adjusted = ? AND trade_date >= ? AND trade_date <= ?
                  AND close IS NOT NULL
                ORDER BY wind_code, trade_date
                """,
                conn,
                params=[
                    PRICE_ADJUSTED,
                    start.strftime("%Y-%m-%d"),
                    target.strftime("%Y-%m-%d"),
                ],
            )
            prices = prices[prices["wind_code"].isin(code_set)]
            latest = prices.groupby("wind_code", as_index=False).last()
            latest = latest.rename(
                columns={"close": column, "trade_date": f"{column}_date"}
            )
            result = result.merge(
                latest[["wind_code", column, f"{column}_date"]],
                on="wind_code",
                how="left",
            )
    result["momentum_12_1"] = (
        base.finite_numeric(result["close_1m"])
        / base.finite_numeric(result["close_12m"])
        - 1
    )
    result["momentum_6_1"] = (
        base.finite_numeric(result["close_1m"])
        / base.finite_numeric(result["close_6m"])
        - 1
    )
    result["momentum_data_coverage"] = result[
        ["momentum_12_1", "momentum_6_1"]
    ].notna().mean(axis=1)
    return result


def load_sp500_industries(codes: list[str], as_of: str) -> pd.DataFrame:
    """读取截止日最新的标普500历史行业快照。"""
    with sqlite3.connect(base.MARKET_DB_PATH) as conn:
        snapshot_date = conn.execute(
            """
            SELECT MAX(snapshot_date)
            FROM stock_industry_snapshot
            WHERE universe_name = ? AND classification_system = ?
              AND snapshot_date <= ?
            """,
            [SP500_UNIVERSE_NAME, base.INDUSTRY_SYSTEM, as_of],
        ).fetchone()[0]
        if snapshot_date is None:
            raise RuntimeError(f"截止 {as_of} 没有标普500历史行业快照")
        industry = pd.read_sql_query(
            """
            SELECT wind_code, sec_name, industry_level1 AS industry
            FROM stock_industry_snapshot
            WHERE universe_name = ? AND classification_system = ?
              AND snapshot_date = ?
            """,
            conn,
            params=[SP500_UNIVERSE_NAME, base.INDUSTRY_SYSTEM, snapshot_date],
        )
    industry = industry[industry["wind_code"].isin(set(codes))].copy()
    industry = industry.drop_duplicates("wind_code", keep="last")
    industry["industry_source"] = f"标普500历史行业快照:{snapshot_date}"
    return industry


def build_sp500_scores(as_of: str, config: base.StrategyConfig):
    configure_sp500_data_source()
    base.validate_config(config)
    universe, snapshot_date = load_sp500_universe(as_of)
    codes = universe["wind_code"].tolist()
    industries = load_sp500_industries(codes, as_of)
    fundamentals = base.load_latest_fundamentals(codes, as_of)
    valuation, valuation_date = base.load_valuation(codes, as_of, config)
    momentum = load_sp500_momentum_features(codes, as_of)
    history = base.report_history_features(
        base.load_report_history(codes, as_of, config.history_quarters)
    )

    frame = (
        universe
        .merge(
            industries[["wind_code", "industry", "industry_source"]],
            on="wind_code",
            how="left",
        )
        .merge(fundamentals, on="wind_code", how="left")
        .merge(valuation, on="wind_code", how="left")
        .merge(momentum, on="wind_code", how="left")
        .merge(history, on="wind_code", how="left")
    )
    frame["行业"] = frame["industry"].fillna("未分类")
    frame["证券名称"] = frame["sec_name"]

    industry_history_scores = base.build_industry_history_scores(
        frame[["wind_code", "行业"]], as_of, config
    )
    scored = base.build_scores(frame, config, industry_history_scores)
    scored = scored.sort_values(
        ["总分", "weighting_market_cap"],
        ascending=[False, False],
        na_position="last",
    ).reset_index(drop=True)
    scored["标普500评分排名"] = np.arange(1, len(scored) + 1)
    scored["行业内评分排名"] = (
        scored.groupby("行业")["总分"]
        .rank(method="first", ascending=False)
        .astype(int)
    )
    scored["数据达标"] = scored["数据完整度"] >= config.min_data_coverage

    industry_summary = (
        scored.groupby("行业", dropna=False)
        .agg(
            成分股数量=("wind_code", "size"),
            平均总分=("总分", "mean"),
            中位总分=("总分", "median"),
            最高总分=("总分", "max"),
            平均数据完整度=("数据完整度", "mean"),
            数据达标数量=("数据达标", "sum"),
        )
        .reset_index()
        .sort_values(["平均总分", "成分股数量"], ascending=[False, False])
    )
    diagnostics = {
        "评分截止日": as_of,
        "标普500成分股截面日": snapshot_date,
        "成分股口径": "截止日最新历史成分快照",
        "成分股数量": len(universe),
        "完成评分数量": len(scored),
        "行业数量": int(scored["行业"].nunique()),
        "未分类数量": int(scored["行业"].eq("未分类").sum()),
        "数据达标数量": int(scored["数据达标"].sum()),
        "平均数据完整度": float(scored["数据完整度"].mean()),
        "PB有效数量": int(scored["pb_lf"].notna().sum()),
        "市值截面日期": valuation_date,
        "市值截面滞后天数": int(
            (pd.Timestamp(as_of) - pd.Timestamp(valuation_date)).days
        ),
        "有完整动量历史数量": int(
            (scored["momentum_data_coverage"] >= 1.0).sum()
        ),
        "业绩预告快报功能": "禁用（A股口径不适用美股）",
        "评分模式": config.scoring_mode,
        "个股因子比较范围": config.stock_factor_scope,
        "组合权重": "不计算",
        "成分选择": "不进行，保留当期标普500历史成分",
    }
    return scored, industry_summary, diagnostics


def export_scores(
    scored: pd.DataFrame,
    industry_summary: pd.DataFrame,
    diagnostics: dict,
    config: base.StrategyConfig,
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    score_columns = [
        "标普500评分排名", "行业内评分排名", "wind_code", "证券名称", "行业",
        "总分", "原始总分", *base.SCORE_WEIGHTS.keys(), "动量得分", "低规模得分",
        "行业综合得分", "行业基本面得分", "行业估值得分", "行业稳定性得分",
        "行业成长性得分", "行业周期得分", "行业竞争格局得分", "行业动量得分",
        "行业历史观察月数", "周期阶段", "数据完整度", "数据达标",
        "weighting_market_cap", "mkt_cap_ard", "free_float_mkt_cap",
        "pe_ttm", "pb_lf", "ps_ttm", "dividend_yield", "roe_ttm", "debt_to_assets",
        "revenue_yoy_qfa", "netprofit_yoy_qfa", "gross_profit_margin_qfa",
        "net_profit_margin_qfa", "momentum_12_1", "momentum_6_1",
        "momentum_data_coverage", "report_count", "qfa_report_period", "qfa_available_date",
        "fundamental_source_date", "valuation_source_date", "industry_source", "成分股截面日",
    ]
    score_columns = [column for column in score_columns if column in scored.columns]
    parameters = pd.DataFrame(asdict(config).items(), columns=["参数", "值"])
    notes = pd.DataFrame(
        [
            ("组合权重", "不计算行业权重、个股权重或基准权重"),
            ("评分系数", "保留中证800版八维混合评分系数"),
            ("评分范围", "百分位和排名只在当期标普500历史成分内计算"),
            ("PB", "Wind当前美股PB字段全空，评分按其他有效估值因子自动重分配"),
            ("市值", "自由流通市值缺失时使用总市值兜底"),
            ("历史成分", "按截止日读取标普500最新历史成分及行业快照"),
        ],
        columns=["说明项", "说明"],
    )
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        scored[score_columns].to_excel(writer, sheet_name="标普500评分", index=False)
        industry_summary.to_excel(writer, sheet_name="行业评分汇总", index=False)
        pd.DataFrame(diagnostics.items(), columns=["检查项", "结果"]).to_excel(
            writer, sheet_name="诊断", index=False
        )
        parameters.to_excel(writer, sheet_name="参数", index=False)
        notes.to_excel(writer, sheet_name="说明", index=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="对当前标普500代码池做行业比较＋混合评分，不计算组合权重"
    )
    parser.add_argument("--as-of", default=date.today().isoformat(), help="评分截止日")
    parser.add_argument("--output", type=Path, help="自定义输出 xlsx 路径")
    parser.add_argument(
        "--stock-factor-scope",
        choices=["blended", "industry_only"],
        default="blended",
        help="默认 blended：行业内与标普500全体混合比较",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    as_of = base.normalize_date(args.as_of)
    config = replace(
        base.hybrid_config(500),
        stock_factor_scope=args.stock_factor_scope,
        earnings_guidance_enabled=False,
    )
    output_path = args.output or (
        OUTPUT_DIR / f"标普500成分股_行业比较混合评分_{as_of.replace('-', '')}.xlsx"
    )
    scored, industry_summary, diagnostics = build_sp500_scores(as_of, config)
    export_scores(scored, industry_summary, diagnostics, config, output_path)
    print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
    print(f"评分结果：{output_path}")


if __name__ == "__main__":
    main()
