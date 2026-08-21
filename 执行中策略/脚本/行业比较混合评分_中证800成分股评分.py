"""
中证 800 成分股的行业比较＋混合评分（无组合权重版）。

本脚本基于《行业比较混合评分_全A股质量成长800指数.py》，保留原脚本的
行业内/全市场混合评分及八维因子口径，但只处理截止日可用的最新
中证 800 成分股截面。不选股、不使用成分缓冲、不读取基准行业权重，
不计算行业权重或个股权重，也不运行回测。

“无组合权重”指不分配指数/组合权重；为了与源脚本的评分口径保持一致，
六维度及动量、低规模因子内部的评分系数仍然保留。
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


CSI800_UNIVERSE_NAME = "中证800"
OUTPUT_DIR = base.BASE_DIR / "输出" / "中证800行业比较混合评分"


def load_csi800_universe(as_of: str) -> tuple[pd.DataFrame, str]:
    """读取截止日不晚于 as_of 的最新中证 800 历史成分截面。"""
    with sqlite3.connect(base.MARKET_DB_PATH) as conn:
        snapshot_date = conn.execute(
            """
            SELECT MAX(snapshot_date)
            FROM universe_constituents_snapshot
            WHERE universe_name = ? AND snapshot_date <= ?
            """,
            [CSI800_UNIVERSE_NAME, as_of],
        ).fetchone()[0]
        if snapshot_date is None:
            raise RuntimeError(f"截止 {as_of} 没有可用的中证800成分股历史截面")
        universe = pd.read_sql_query(
            """
            SELECT wind_code, sec_name
            FROM universe_constituents_snapshot
            WHERE universe_name = ? AND snapshot_date = ?
            ORDER BY wind_code
            """,
            conn,
            params=[CSI800_UNIVERSE_NAME, snapshot_date],
        )
    universe = universe.drop_duplicates("wind_code", keep="last")
    if universe.empty:
        raise RuntimeError(f"中证800在 {snapshot_date} 的成分股截面为空")
    universe["截止日"] = as_of
    universe["成分股截面日"] = str(snapshot_date)
    universe["universe_source"] = f"中证800历史成分截面:{snapshot_date}"
    return universe, str(snapshot_date)


def build_csi800_scores(as_of: str, config: base.StrategyConfig):
    base.validate_config(config)
    universe, snapshot_date = load_csi800_universe(as_of)
    codes = universe["wind_code"].tolist()
    industries = base.load_industries(codes, as_of)
    fundamentals = base.load_latest_fundamentals(codes, as_of)
    valuation, valuation_date = base.load_valuation(codes, as_of, config)
    momentum = base.load_momentum_features(codes, as_of)
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

    guidance = base.load_earnings_guidance(frame, as_of, config)
    frame = frame.merge(guidance, on="wind_code", how="left")
    frame["earnings_guidance_profit_acceleration"] = (
        base.finite_numeric(
            frame.get(
                "earnings_guidance_profit_yoy",
                pd.Series(np.nan, index=frame.index),
            )
        )
        - base.normalize_percent(frame["netprofit_yoy_qfa"])
    )

    industry_history_scores = base.build_industry_history_scores(
        frame[["wind_code", "行业"]], as_of, config
    )
    scored = base.build_scores(frame, config, industry_history_scores)
    scored = scored.sort_values(
        ["总分", "weighting_market_cap"], ascending=[False, False], na_position="last"
    ).reset_index(drop=True)
    scored["中证800评分排名"] = np.arange(1, len(scored) + 1)
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
        "中证800成分股截面日": snapshot_date,
        "成分股数量": len(universe),
        "完成评分数量": len(scored),
        "行业数量": int(scored["行业"].nunique()),
        "数据达标数量": int(scored["数据达标"].sum()),
        "平均数据完整度": float(scored["数据完整度"].mean()),
        "市值截面日期": valuation_date,
        "市值截面滞后天数": int(
            (pd.Timestamp(as_of) - pd.Timestamp(valuation_date)).days
        ),
        "有完整动量历史数量": int(
            (scored["momentum_data_coverage"] >= 1.0).sum()
        ),
        "有效业绩预告快报数量": int(
            scored.get("earnings_guidance_confidence", pd.Series(dtype=float))
            .fillna(0)
            .gt(0)
            .sum()
        ),
        "评分模式": config.scoring_mode,
        "个股因子比较范围": config.stock_factor_scope,
        "组合权重": "不计算",
        "成分选择": "不进行，保留全部中证800成分股",
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
        "中证800评分排名", "行业内评分排名", "wind_code", "证券名称", "行业",
        "总分", "原始总分", *base.SCORE_WEIGHTS.keys(), "动量得分", "低规模得分",
        "行业综合得分", "行业基本面得分", "行业估值得分", "行业稳定性得分",
        "行业成长性得分", "行业周期得分", "行业竞争格局得分", "行业动量得分",
        "行业历史观察月数", "周期阶段", "数据完整度", "数据达标",
        "weighting_market_cap", "mkt_cap_ard", "free_float_mkt_cap",
        "pe_ttm", "pb_lf", "ps_ttm", "dividend_yield", "roe_ttm", "debt_to_assets",
        "revenue_yoy_qfa", "netprofit_yoy_qfa", "gross_profit_margin_qfa",
        "net_profit_margin_qfa", "momentum_12_1", "momentum_6_1",
        "momentum_data_coverage", "report_count", "qfa_report_period", "qfa_available_date",
        "earnings_guidance_report_period", "earnings_guidance_announcement_date",
        "earnings_guidance_type", "earnings_guidance_style", "earnings_guidance_profit_yoy",
        "earnings_guidance_profit_acceleration", "earnings_guidance_confidence",
        "earnings_guidance_source", "fundamental_source_date", "valuation_source_date",
        "industry_source", "成分股截面日",
    ]
    score_columns = [column for column in score_columns if column in scored.columns]
    parameters = pd.DataFrame(asdict(config).items(), columns=["参数", "值"])
    notes = pd.DataFrame(
        [
            ("组合权重", "不计算行业权重、个股权重或基准权重"),
            ("评分系数", "保留源脚本的八维混合评分系数，以保持口径一致"),
            ("评分范围", "百分位和排名均只在当期中证800成分股内计算"),
        ],
        columns=["说明项", "说明"],
    )
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        scored[score_columns].to_excel(writer, sheet_name="中证800评分", index=False)
        industry_summary.to_excel(writer, sheet_name="行业评分汇总", index=False)
        pd.DataFrame(diagnostics.items(), columns=["检查项", "结果"]).to_excel(
            writer, sheet_name="诊断", index=False
        )
        parameters.to_excel(writer, sheet_name="参数", index=False)
        notes.to_excel(writer, sheet_name="说明", index=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="对中证800成分股做行业比较＋混合评分，不计算组合权重"
    )
    parser.add_argument("--as-of", default=date.today().isoformat(), help="评分截止日")
    parser.add_argument("--output", type=Path, help="自定义输出 xlsx 路径")
    parser.add_argument(
        "--stock-factor-scope",
        choices=["blended", "industry_only"],
        default="blended",
        help="默认 blended：行业内与中证800全体混合比较",
    )
    parser.add_argument("--disable-earnings-guidance", action="store_true")
    parser.add_argument("--earnings-notice-confidence", type=float, default=0.40)
    parser.add_argument("--earnings-express-confidence", type=float, default=0.70)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    as_of = base.normalize_date(args.as_of)
    config = replace(
        base.hybrid_config(800),
        stock_factor_scope=args.stock_factor_scope,
        earnings_guidance_enabled=not args.disable_earnings_guidance,
        earnings_notice_confidence=args.earnings_notice_confidence,
        earnings_express_confidence=args.earnings_express_confidence,
    )
    output_path = args.output or (
        OUTPUT_DIR / f"中证800成分股_行业比较混合评分_{as_of.replace('-', '')}.xlsx"
    )
    scored, industry_summary, diagnostics = build_csi800_scores(as_of, config)
    export_scores(scored, industry_summary, diagnostics, config, output_path)
    print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
    print(f"评分结果：{output_path}")


if __name__ == "__main__":
    main()
