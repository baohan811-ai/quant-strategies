"""中证 800 历史成分股中的质量成长 50 研究版策略入口。

本文件固定了 2026-08-25 的中证 800 股票池 100/80/50 对比中，
50 只版本的可复现口径：

- 每个调仓日只使用当时可得的中证 800 历史成分截面；
- 每月调仓，固定 50 只，按行业参考名额选股；
- 20% 成分缓冲；
- 行业内与当期中证 800 股票池混合评分；
- 行业权重以沪深 300 行业权重为中性锚，不启用行业吸引力主动倾斜；
- 六维评分中估值权重 25%，其余五项按原相对比例等比例分配；
- 按实际公告日可得数据自算 TTM ROE；
- 开启业绩预告/快报，置信权重分别为 0.40/0.70；
- 前复权行情，默认单边交易成本 0.5%；
- 沪深 300 全收益指数为主基准。

前高回撤退出没有并入本版本：29% 阈值的样本内收益改善
几乎全部来自 2026 年，且最大回撤更差，不适合当作正式风控。

为避免复制整个计算引擎后出现逻辑分叉，本文件只保留股票池、
研究参数和执行入口；底层数据、评分、选股、权重与回测复用同目录下
的《行业比较混合评分_全A股质量成长800指数.py》。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
ENGINE_PATH = SCRIPT_DIR / "行业比较混合评分_全A股质量成长800指数.py"
OUTPUT_DIR = SCRIPT_DIR.parent / "输出" / "中证800质量成长50"

STRATEGY_NAME = "中证800质量成长50"
UNIVERSE_NAME = "中证800"
TARGET_CONSTITUENTS = 50
CONSTITUENT_BUFFER_RATIO = 0.20
DEFAULT_COST_RATE = 0.005
DEFAULT_START_DATE = "2022-01-01"
DEFAULT_END_DATE = "2026-08-20"
EARNINGS_NOTICE_CONFIDENCE = 0.40
EARNINGS_EXPRESS_CONFIDENCE = 0.70
INDUSTRY_ACTIVE_TILT_STRENGTH = 0.0
CURRENT_HOLDINGS_SHEET = "中证800全体评分与持仓"

# 六维评分权重；六维综合分在混合总分中占 85%，其余为动量 10% 和低规模 5%。
FUNDAMENTAL_WEIGHT = 0.234375
VALUATION_WEIGHT = 0.25
STABILITY_WEIGHT = 0.140625
GROWTH_WEIGHT = 0.1875
CYCLE_WEIGHT = 0.09375
COMPETITION_WEIGHT = 0.09375


def load_engine():
    script_dir = str(SCRIPT_DIR)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    spec = importlib.util.spec_from_file_location(
        "csi800_quality_growth_50_engine", ENGINE_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载底层策略引擎：{ENGINE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_historical_universe_loader(engine):
    """生成一个严格按 as-of 读取中证 800 历史成分的加载器。"""

    def load_universe(as_of: str) -> pd.DataFrame:
        with sqlite3.connect(engine.MARKET_DB_PATH) as conn:
            snapshot_date = conn.execute(
                """
                SELECT MAX(snapshot_date)
                FROM universe_constituents_snapshot
                WHERE universe_name = ? AND snapshot_date <= ?
                """,
                [UNIVERSE_NAME, as_of],
            ).fetchone()[0]
            if not snapshot_date:
                raise RuntimeError(
                    f"截止 {as_of} 没有可用的{UNIVERSE_NAME}历史成分截面"
                )
            universe = pd.read_sql_query(
                """
                SELECT wind_code, sec_name
                FROM universe_constituents_snapshot
                WHERE universe_name = ? AND snapshot_date = ?
                ORDER BY wind_code
                """,
                conn,
                params=[UNIVERSE_NAME, snapshot_date],
            )
        if universe.empty:
            raise RuntimeError(f"{UNIVERSE_NAME}历史成分截面 {snapshot_date} 为空")
        universe = universe.drop_duplicates("wind_code", keep="last")
        universe["截止日"] = as_of
        universe["成分股截面日"] = str(snapshot_date)
        universe["universe_source"] = f"{UNIVERSE_NAME}历史成分截面:{snapshot_date}"
        return universe

    return load_universe


def make_config(engine, earnings_guidance_enabled: bool = True):
    """显式固定已回测的50只版本参数。"""
    config = replace(
        engine.hybrid_config(TARGET_CONSTITUENTS),
        max_constituents=TARGET_CONSTITUENTS,
        rebalance_frequency="M",
        stock_factor_scope="blended",
        constituent_selection_mode="industry_quota",
        constituent_buffer_ratio=CONSTITUENT_BUFFER_RATIO,
        industry_quota_min_score=45.0,
        industry_target_mode="industry_score",
        industry_between_tilt_strength=INDUSTRY_ACTIVE_TILT_STRENGTH,
        quality_data_mode="calculated_ttm",
        valuation_data_mode="calculated_pit",
        fundamental_weight=FUNDAMENTAL_WEIGHT,
        valuation_weight=VALUATION_WEIGHT,
        stability_weight=STABILITY_WEIGHT,
        growth_weight=GROWTH_WEIGHT,
        cycle_weight=CYCLE_WEIGHT,
        competition_weight=COMPETITION_WEIGHT,
        earnings_guidance_enabled=earnings_guidance_enabled,
        earnings_notice_confidence=EARNINGS_NOTICE_CONFIDENCE,
        earnings_express_confidence=EARNINGS_EXPRESS_CONFIDENCE,
    )
    engine.validate_config(config)
    return config


def configure_engine(engine) -> None:
    engine.load_universe = make_historical_universe_loader(engine)


def default_output_path(as_of: str) -> Path:
    stamp = as_of.replace("-", "")
    return OUTPUT_DIR / f"{STRATEGY_NAME}_行业比较混合评分_{stamp}.xlsx"


def format_selected_components_sheet(output_path: Path) -> None:
    """精简首页成分表：删除入选方式，并将指数权重移到总分左侧。"""
    from copy import copy

    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter

    workbook = load_workbook(output_path)
    sheet_name = f"{TARGET_CONSTITUENTS}成分与权重"
    if sheet_name not in workbook.sheetnames:
        workbook.close()
        raise RuntimeError(f"输出工作簿缺少页签：{sheet_name}")
    sheet = workbook[sheet_name]

    headers = {cell.value: cell.column for cell in sheet[1]}
    selection_method_column = headers.get("入选方式")
    if selection_method_column is not None:
        sheet.delete_cols(selection_method_column)

    headers = {cell.value: cell.column for cell in sheet[1]}
    weight_column = headers.get("指数权重")
    score_column = headers.get("总分")
    if weight_column is None or score_column is None:
        workbook.close()
        raise RuntimeError("成分表缺少‘指数权重’或‘总分’列")

    if weight_column != score_column - 1:
        source_width = sheet.column_dimensions[
            get_column_letter(weight_column)
        ].width
        sheet.insert_cols(score_column)
        shifted_weight_column = (
            weight_column + 1 if weight_column >= score_column else weight_column
        )
        for row_number in range(1, sheet.max_row + 1):
            source = sheet.cell(row_number, shifted_weight_column)
            target = sheet.cell(row_number, score_column)
            target.value = source.value
            if source.has_style:
                target._style = copy(source._style)
            if source.number_format:
                target.number_format = source.number_format
            if source.alignment:
                target.alignment = copy(source.alignment)
            if source.protection:
                target.protection = copy(source.protection)
            if source.comment:
                target.comment = copy(source.comment)
            if source.hyperlink:
                target._hyperlink = copy(source.hyperlink)
        sheet.delete_cols(shifted_weight_column)
        sheet.column_dimensions[get_column_letter(score_column)].width = source_width

    sheet.auto_filter.ref = sheet.dimensions
    workbook.save(output_path)
    workbook.close()


def simplify_industry_weights_sheet(output_path: Path) -> None:
    """精简行业中性版的行业权重页，避免展示已失效的主动倾斜字段。"""
    from openpyxl import load_workbook

    workbook = load_workbook(output_path)
    sheet_name = "行业权重"
    if sheet_name not in workbook.sheetnames:
        workbook.close()
        return

    sheet = workbook[sheet_name]
    inactive_headers = {
        "高分股权重信号",
        "行业吸引力得分",
        "行业吸引力信号",
        "建议行业权重",
    }
    columns_to_delete = sorted(
        (
            cell.column
            for cell in sheet[1]
            if cell.value in inactive_headers
        ),
        reverse=True,
    )
    for column_number in columns_to_delete:
        sheet.delete_cols(column_number)

    for cell in sheet[1]:
        if cell.value == "相对沪深300偏离":
            cell.value = "结构性再分配差"
            break
    sheet.auto_filter.ref = sheet.dimensions
    workbook.save(output_path)
    workbook.close()


def find_previous_components(as_of: str, current_output: Path) -> Path | None:
    cutoff = as_of.replace("-", "")
    candidates: list[tuple[str, Path]] = []
    for path in current_output.parent.glob(f"{STRATEGY_NAME}_行业比较混合评分_*.xlsx"):
        stamp = path.stem.rsplit("_", 1)[-1]
        if len(stamp) == 8 and stamp.isdigit() and stamp < cutoff:
            candidates.append((stamp, path))
    return max(candidates, default=("", None), key=lambda item: item[0])[1]


def add_current_holdings_sheet(
    engine,
    selected: pd.DataFrame,
    all_scores: pd.DataFrame,
    output_path: Path,
) -> None:
    """合并800只评分与50只当期持仓，并高亮入选行。"""
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    selected_codes = set(selected["wind_code"].astype(str))
    weight_map = selected.set_index("wind_code")["指数权重"]

    detail = all_scores.copy()
    detail["中证800评分排名"] = (
        detail["总分"].rank(method="first", ascending=False).astype("Int64")
    )
    detail["是否入选50指数"] = detail["wind_code"].astype(str).map(
        lambda code: "是" if code in selected_codes else "否"
    )
    detail["50指数权重"] = detail["wind_code"].map(weight_map).fillna(0.0)

    score_columns = [
        *engine.SCORE_WEIGHTS.keys(),
        "动量得分",
        "低规模得分",
        "行业综合得分",
        "行业基本面得分",
        "行业估值得分",
        "行业稳定性得分",
        "行业成长性得分",
        "行业周期得分",
        "行业竞争格局得分",
        "行业动量得分",
    ]
    columns = [
        "是否入选50指数",
        "中证800评分排名",
        "wind_code",
        "证券名称",
        "行业",
        "50指数权重",
        "总分",
        "原始总分",
        *score_columns,
        "行业历史观察月数",
        "周期阶段",
        "数据完整度",
        "weighting_market_cap",
        "earnings_guidance_profit_acceleration",
        "pe_ttm",
        "pb_lf",
        "ps_ttm",
        "dividend_yield",
        "valuation_report_period",
        "valuation_available_date",
        "valuation_calculation_version",
        "fundamental_source_date",
        "valuation_factor_source_date",
        "valuation_source_date",
    ]
    columns = [column for column in columns if column in detail.columns]
    detail = detail.sort_values(
        ["是否入选50指数", "中证800评分排名"],
        ascending=[False, True],
    )[columns]

    with pd.ExcelWriter(
        output_path,
        engine="openpyxl",
        mode="a",
        if_sheet_exists="replace",
    ) as writer:
        detail.to_excel(writer, sheet_name=CURRENT_HOLDINGS_SHEET, index=False)
        sheet = writer.book[CURRENT_HOLDINGS_SHEET]
        sheet.sheet_view.showGridLines = False
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions

        dark_blue = "17365D"
        white = "FFFFFF"
        selected_green = "C6E0B4"
        thin_gray = Side(style="thin", color="D9E1F2")
        for cell in sheet[1]:
            cell.font = Font(name="Microsoft YaHei", bold=True, color=white)
            cell.fill = PatternFill("solid", fgColor=dark_blue)
            cell.alignment = Alignment(horizontal="left", vertical="center")
            cell.border = Border(bottom=thin_gray)

        headers = {cell.value: cell.column for cell in sheet[1]}
        selected_column = headers["是否入选50指数"]
        weight_column = headers["50指数权重"]
        completeness_column = headers.get("数据完整度")
        score_column_numbers = {
            headers[column]
            for column in ["总分", "原始总分", *score_columns]
            if column in headers
        }

        for row in range(2, sheet.max_row + 1):
            is_selected = sheet.cell(row, selected_column).value == "是"
            for cell in sheet[row]:
                cell.font = Font(name="Microsoft YaHei", size=10)
                cell.alignment = Alignment(horizontal="left", vertical="center")
                if is_selected:
                    cell.fill = PatternFill("solid", fgColor=selected_green)
            for column in score_column_numbers:
                sheet.cell(row, column).number_format = "0.00"
            sheet.cell(row, weight_column).number_format = "0.0000%"
            if completeness_column:
                sheet.cell(row, completeness_column).number_format = "0.00%"

        preferred_widths = {
            "是否入选50指数": 16,
            "中证800评分排名": 16,
            "wind_code": 14,
            "证券名称": 18,
            "行业": 18,
            "50指数权重": 16,
        }
        for column_index, cell in enumerate(sheet[1], start=1):
            width = preferred_widths.get(str(cell.value), 15)
            sheet.column_dimensions[get_column_letter(column_index)].width = width


def format_workbook_for_review(output_path: Path) -> None:
    """统一整本工作簿的审阅格式、数字精度、单位与冻结窗格。"""
    import re
    import unicodedata
    from datetime import datetime

    from openpyxl import load_workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    dark_blue = "17365D"
    white = "FFFFFF"
    thin_gray = Side(style="thin", color="D9E1F2")
    pct_format = "0.00%;[Red]-0.00%;-"
    one_decimal_format = "0.0;[Red]-0.0;-"
    two_decimal_format = "0.00;[Red]-0.00;-"
    multiple_format = "0.00x;[Red]-0.00x;-"
    integer_format = "#,##0;[Red]-#,##0;-"
    nav_format = "0.0000"
    date_format = "yyyy-mm-dd"

    market_cap_headers = {
        "weighting_market_cap": "权重市值(亿元)",
        "mkt_cap_ard": "总市值(亿元)",
        "free_float_mkt_cap": "流通市值(亿元)",
    }
    formatted_market_cap_headers = set(market_cap_headers.values())
    percentage_headers = {
        "指数权重", "50指数权重", "数据完整度", "行业内市值权重",
        "dividend_yield", "roe_ttm", "debt_to_assets", "revenue_yoy_qfa",
        "netprofit_yoy_qfa", "gross_profit_margin_qfa", "net_profit_margin_qfa",
        "momentum_12_1", "momentum_6_1", "momentum_data_coverage",
        "earnings_guidance_profit_acceleration",
    }
    multiple_headers = {"pe_ttm", "pb_lf", "ps_ttm", "评分倾斜倍数"}
    nav_headers = {
        "策略毛净值", "策略费后净值", "沪深300价格净值",
        "沪深300全收益净值", "费后超额净值",
    }
    count_header_tokens = (
        "排名", "数量", "名额", "月数", "天数", "期数", "次数", "观察",
    )
    integer_headers = {
        "report_count", "dividend_event_count", "listing_history_days",
    }
    date_headers = {
        "信号日", "交易日", "期末日", "基准权重日", "市值截面日", "日期",
        "first_universe_date", "qfa_report_period", "qfa_available_date",
        "valuation_report_period", "valuation_available_date",
        "fundamental_source_date", "valuation_factor_source_date", "valuation_source_date",
    }

    def display_width(value) -> int:
        text = "" if value is None else str(value)
        return sum(
            2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
            for character in text
        )

    def parse_iso_date(value):
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return datetime.strptime(value, "%Y-%m-%d")
        return value

    def is_date_header(header: str) -> bool:
        return (
            header in date_headers
            or header.endswith("_date")
            or header.endswith("_period")
        )

    def inferred_number_format(header: str) -> str | None:
        if header == "总分":
            return one_decimal_format
        if header == "原始总分" or header.endswith("得分"):
            return two_decimal_format
        if header in formatted_market_cap_headers:
            return integer_format
        if header in percentage_headers:
            return pct_format
        if header in multiple_headers:
            return multiple_format
        if header in nav_headers:
            return nav_format
        if header == "行业理想名额":
            return one_decimal_format
        if (
            header == "年度"
            or header in integer_headers
            or any(token in header for token in count_header_tokens)
        ):
            return integer_format
        if header in {
            "单边换手率", "交易成本", "区间毛收益", "区间费后收益",
            "沪深300价格区间收益", "沪深300全收益区间收益", "费后超额收益",
            "策略毛收益", "策略费后收益", "沪深300价格收益",
            "沪深300全收益", "策略费后回撤",
        }:
            return pct_format
        return None

    workbook = load_workbook(output_path)
    for sheet in workbook.worksheets:
        header_row = 3 if sheet.title == "绩效汇总" else 1

        # 将原始市值转为亿元整数，并使转换可重复执行。
        for cell in sheet[header_row]:
            original_header = str(cell.value) if cell.value is not None else ""
            if original_header not in market_cap_headers:
                continue
            for row_number in range(header_row + 1, sheet.max_row + 1):
                value_cell = sheet.cell(row_number, cell.column)
                if isinstance(value_cell.value, (int, float)) and not isinstance(value_cell.value, bool):
                    value_cell.value = round(float(value_cell.value) / 100_000_000)
            cell.value = market_cap_headers[original_header]

        headers = {
            str(cell.value): cell.column
            for cell in sheet[header_row]
            if cell.value is not None
        }

        sheet.sheet_view.showGridLines = False
        sheet.sheet_view.zoomScale = 175
        sheet.sheet_view.zoomScaleNormal = 175
        if sheet.title == "50成分与权重":
            sheet.freeze_panes = "G2"
        elif sheet.title == "全市场评分":
            sheet.freeze_panes = "C2"
        elif sheet.title == CURRENT_HOLDINGS_SHEET:
            sheet.freeze_panes = "E2"
        elif sheet.title == "绩效汇总":
            sheet.freeze_panes = "A4"
        else:
            sheet.freeze_panes = "A2"

        sheet.auto_filter.ref = (
            f"A{header_row}:{get_column_letter(sheet.max_column)}{sheet.max_row}"
        )
        sheet.row_dimensions[header_row].height = 28
        for cell in sheet[header_row]:
            cell.font = Font(name="Microsoft YaHei", bold=True, color=white)
            cell.fill = PatternFill("solid", fgColor=dark_blue)
            cell.alignment = Alignment(horizontal="left", vertical="center")
            cell.border = Border(bottom=thin_gray)

        if sheet.title == "绩效汇总":
            sheet.row_dimensions[1].height = 30
            for cell in sheet[1]:
                cell.font = Font(name="Microsoft YaHei", size=14, bold=True, color=white)
                cell.fill = PatternFill("solid", fgColor=dark_blue)
                cell.alignment = Alignment(horizontal="left", vertical="center")

        for row in sheet.iter_rows(min_row=header_row + 1):
            for cell in row:
                cell.font = Font(name="Microsoft YaHei", size=10, color="000000")
                cell.alignment = Alignment(horizontal="left", vertical="center")

        for header, column_number in headers.items():
            if is_date_header(header):
                for row_number in range(header_row + 1, sheet.max_row + 1):
                    cell = sheet.cell(row_number, column_number)
                    cell.value = parse_iso_date(cell.value)
                    if isinstance(cell.value, datetime):
                        cell.number_format = date_format
                        cell.alignment = Alignment(horizontal="left", vertical="center")
            number_format = inferred_number_format(header)
            if number_format:
                for row_number in range(header_row + 1, sheet.max_row + 1):
                    sheet.cell(row_number, column_number).number_format = number_format

        if sheet.title == "行业权重":
            for header, column_number in headers.items():
                if any(token in header for token in (
                    "权重", "偏离", "承载能力", "校验差", "再分配差",
                )):
                    for row_number in range(2, sheet.max_row + 1):
                        sheet.cell(row_number, column_number).number_format = pct_format

        if sheet.title == "年度收益":
            for column_number in range(2, sheet.max_column + 1):
                for row_number in range(2, sheet.max_row + 1):
                    sheet.cell(row_number, column_number).number_format = pct_format

        if sheet.title in {"诊断", "参数", "模型参数"}:
            for row_number in range(2, sheet.max_row + 1):
                key = str(sheet.cell(row_number, 1).value or "")
                value_cell = sheet.cell(row_number, 2)
                parsed = parse_iso_date(value_cell.value)
                if isinstance(parsed, datetime) and (
                    "日" in key or "date" in key.lower()
                ):
                    value_cell.value = parsed
                    value_cell.number_format = date_format
                    value_cell.alignment = Alignment(horizontal="left", vertical="center")
                elif sheet.title == "诊断" and any(
                    token in key for token in ("覆盖率", "完整度", "权重合计", "最大个股权重")
                ):
                    value_cell.number_format = pct_format
                elif sheet.title == "参数" and any(
                    token in key for token in (
                        "_weight", "_coverage", "_quantile", "_deviation",
                        "_ratio", "_confidence",
                    )
                ):
                    value_cell.number_format = pct_format
                elif sheet.title == "模型参数" and any(
                    token in key for token in ("权重", "偏离", "比例", "置信", "交易成本")
                ) and "倾斜强度" not in key:
                    value_cell.number_format = pct_format
                elif isinstance(value_cell.value, int) and not isinstance(value_cell.value, bool):
                    value_cell.number_format = integer_format
                elif isinstance(value_cell.value, float):
                    value_cell.number_format = two_decimal_format

        if sheet.title == "绩效汇总":
            for row_number in range(4, sheet.max_row + 1):
                metric = str(sheet.cell(row_number, 2).value or "")
                value_cell = sheet.cell(row_number, 3)
                if "相关系数" in metric:
                    value_cell.number_format = two_decimal_format
                elif any(token in metric for token in (
                    "收益", "波动", "回撤", "胜率", "跑赢率", "换手", "交易成本", "跟踪误差",
                )):
                    value_cell.number_format = pct_format
                else:
                    value_cell.number_format = two_decimal_format

        for row in sheet.iter_rows(min_row=header_row + 1):
            for cell in row:
                if cell.number_format == "General":
                    if isinstance(cell.value, int) and not isinstance(cell.value, bool):
                        cell.number_format = integer_format
                    elif isinstance(cell.value, float):
                        cell.number_format = two_decimal_format

        # 所有页签统一左对齐，包括表头、文本、数字和日期。
        for row in sheet.iter_rows():
            for cell in row:
                if cell.value is not None:
                    cell.alignment = Alignment(horizontal="left", vertical="center")

        # 根据表头与数据长度调整列宽，保证首行不被截断。
        for column_number in range(1, sheet.max_column + 1):
            header_text = sheet.cell(header_row, column_number).value
            header_width = display_width(header_text) + 3
            max_data_width = 0
            for row_number in range(header_row + 1, sheet.max_row + 1):
                value = sheet.cell(row_number, column_number).value
                if isinstance(value, datetime):
                    current_width = 10
                elif isinstance(value, (int, float)) and not isinstance(value, bool):
                    current_width = 12
                else:
                    current_width = display_width(value)
                max_data_width = max(max_data_width, current_width)
            width = max(10, header_width, min(max_data_width + 2, 48))
            sheet.column_dimensions[get_column_letter(column_number)].width = width

    workbook.save(output_path)
    workbook.close()


def consolidate_all_scores_sheet(output_path: Path) -> None:
    """将‘全市场评分’的独有字段并入持仓明细后删除重复页签。"""
    from copy import copy

    from openpyxl import load_workbook

    workbook = load_workbook(output_path)
    source_name = "全市场评分"
    if source_name not in workbook.sheetnames:
        workbook.close()
        return
    if CURRENT_HOLDINGS_SHEET not in workbook.sheetnames:
        workbook.close()
        raise RuntimeError(f"缺少页签：{CURRENT_HOLDINGS_SHEET}")

    source = workbook[source_name]
    target = workbook[CURRENT_HOLDINGS_SHEET]
    source_headers = {
        str(cell.value): cell.column for cell in source[1] if cell.value is not None
    }
    target_headers = {
        str(cell.value): cell.column for cell in target[1] if cell.value is not None
    }
    if "wind_code" not in source_headers or "wind_code" not in target_headers:
        workbook.close()
        raise RuntimeError("评分页签缺少 wind_code，无法安全合并")

    source_code_column = source_headers["wind_code"]
    target_code_column = target_headers["wind_code"]
    source_rows = {
        str(source.cell(row_number, source_code_column).value): row_number
        for row_number in range(2, source.max_row + 1)
    }
    unique_headers = (
        "行业历史观察月数",
        "weighting_market_cap",
        "权重市值(亿元)",
        "earnings_guidance_profit_acceleration",
    )
    market_cap_variants = {"weighting_market_cap", "权重市值(亿元)"}

    for header in unique_headers:
        if header not in source_headers or header in target_headers:
            continue
        if header in market_cap_variants and market_cap_variants & target_headers.keys():
            continue

        new_column = target.max_column + 1
        source_column = source_headers[header]
        for row_number in range(1, target.max_row + 1):
            target_cell = target.cell(row_number, new_column)
            style_source = target.cell(row_number, new_column - 1)
            if style_source.has_style:
                target_cell._style = copy(style_source._style)
            target_cell.font = copy(style_source.font)
            target_cell.fill = copy(style_source.fill)
            target_cell.border = copy(style_source.border)
            target_cell.alignment = copy(style_source.alignment)
            target_cell.number_format = style_source.number_format

            if row_number == 1:
                target_cell.value = header
                continue
            code = str(target.cell(row_number, target_code_column).value)
            source_row = source_rows.get(code)
            if source_row is not None:
                target_cell.value = source.cell(source_row, source_column).value

        target_headers[header] = new_column

    target.auto_filter.ref = target.dimensions
    workbook.remove(source)
    workbook.save(output_path)
    workbook.close()


def run_build(engine, args, config, output_path: Path) -> Path:
    previous_path = args.previous_components or find_previous_components(
        args.as_of, output_path
    )
    previous_constituents = (
        engine.load_previous_constituents(previous_path)
        if previous_path is not None
        else None
    )
    selected, industry_summary, all_scores, diagnostics = engine.build_index(
        args.as_of,
        config,
        args.benchmark_weights,
        previous_constituents,
    )
    diagnostics.update({
        "策略名称": STRATEGY_NAME,
        "选股范围": "中证800历史成分股",
        "目标成分数": TARGET_CONSTITUENTS,
        "行业主动倾斜": "未启用",
        "行业主动倾斜强度": INDUSTRY_ACTIVE_TILT_STRENGTH,
        "前高回撤退出": "未启用",
    })
    output_path.parent.mkdir(parents=True, exist_ok=True)
    engine.export_result(
        selected,
        industry_summary,
        all_scores,
        diagnostics,
        config,
        output_path,
    )
    format_selected_components_sheet(output_path)
    simplify_industry_weights_sheet(output_path)
    add_current_holdings_sheet(engine, selected, all_scores, output_path)
    consolidate_all_scores_sheet(output_path)
    format_workbook_for_review(output_path)
    print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
    print(f"上一期成分：{previous_path or '无（本期不使用历史成分缓冲）'}")
    print(f"单期构建结果：{output_path}")
    return output_path


def relabel_backtest_workbook(output_path: Path) -> None:
    from openpyxl import load_workbook

    workbook = load_workbook(output_path)
    if "绩效汇总" in workbook.sheetnames:
        workbook["绩效汇总"]["A1"] = f"{STRATEGY_NAME}回测绩效汇总"
    workbook.save(output_path)


def run_backtest(engine, args, config, output_path: Path) -> Path:
    daily, periods, yearly, metrics = engine.run_backtest(
        args.start_date,
        args.end_date,
        args.cost_rate,
        config,
    )
    metrics.setdefault("模型配置", {}).update({
        "策略名称": STRATEGY_NAME,
        "选股范围": "中证800历史成分股",
        "目标成分数": TARGET_CONSTITUENTS,
        "单边交易成本": args.cost_rate,
        "回测开始日": args.start_date,
        "回测结束日": args.end_date,
        "行业主动倾斜": "未启用",
        "行业主动倾斜强度": INDUSTRY_ACTIVE_TILT_STRENGTH,
        "前高回撤退出": "未启用",
    })
    output_path.parent.mkdir(parents=True, exist_ok=True)
    engine.write_backtest_outputs(output_path, daily, periods, yearly, metrics)
    relabel_backtest_workbook(output_path)
    format_workbook_for_review(output_path)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    print(f"回测结果：{output_path}")
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--task",
        choices=["config", "build", "backtest", "both"],
        default="backtest",
        help="config 仅打印参数；build 构建单期成分；backtest 运行回测",
    )
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument("--end-date", default=DEFAULT_END_DATE)
    parser.add_argument("--cost-rate", type=float, default=DEFAULT_COST_RATE)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--benchmark-weights", type=Path)
    parser.add_argument("--previous-components", type=Path)
    parser.add_argument(
        "--disable-earnings-guidance",
        action="store_true",
        help="研究开关：关闭业绩预告/快报",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    engine = load_engine()
    configure_engine(engine)
    args.as_of = engine.normalize_date(args.as_of)
    config = make_config(
        engine,
        earnings_guidance_enabled=not args.disable_earnings_guidance,
    )
    output_path = args.output or default_output_path(args.as_of)

    if args.task == "config":
        print(json.dumps(asdict(config), ensure_ascii=False, indent=2))
        return
    if args.task in {"build", "both", "backtest"}:
        run_build(engine, args, config, output_path)
    if args.task in {"backtest", "both"}:
        run_backtest(engine, args, config, output_path)


if __name__ == "__main__":
    main()
