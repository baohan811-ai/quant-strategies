import os
import sqlite3
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_ROOT = os.path.dirname(SCRIPT_DIR)
BASE_DIR = os.path.dirname(SCRIPT_ROOT)
OUTPUT_DIR = os.path.join(BASE_DIR, "输出")
if SCRIPT_ROOT not in sys.path:
    sys.path.insert(0, SCRIPT_ROOT)

from 维护工具.local_market_db import MARKET_DB_PATH

CACHE_PREFIX = "中证800"
STRATEGY_OUTPUT_PREFIX = "金叉买入_前高回撤11.75%卖出策略_中证800_"
TRADE_START_DATE = "2023-04-01"
LOOKBACK_DAYS = 1600
SIGNAL_MAX_DAILY_RETURN = 0.065
HORIZONS = [5, 10, 20, 60]

MA5_MA60_GAP_WEIGHT = 1.0
MA60_5D_TREND_WEIGHT = 1.0
VOLUME_RATIO_SCORE_WEIGHT = 0.25
VOLUME_RATIO_LOOKBACK = 20
VOLUME_RATIO_MAX_BONUS_BASE = 1.0


def latest_strategy_output():
    files = [
        os.path.join(OUTPUT_DIR, name)
        for name in os.listdir(OUTPUT_DIR)
        if name.startswith(STRATEGY_OUTPUT_PREFIX)
        and name.endswith(".xlsx")
        and not name.startswith("~$")
    ]
    if not files:
        raise FileNotFoundError(f"没有找到策略输出：{STRATEGY_OUTPUT_PREFIX}*.xlsx")
    return max(files, key=os.path.getmtime)


def load_run_dates(output_file):
    nav = pd.read_excel(output_file, sheet_name="净值")
    nav = nav.rename(columns={nav.columns[0]: "日期"})
    nav["日期"] = pd.to_datetime(nav["日期"])
    return nav["日期"].min(), nav["日期"].max()


def read_sql_df(query, params=None):
    with sqlite3.connect(MARKET_DB_PATH) as conn:
        return pd.read_sql_query(query, conn, params=params or [])


def load_code_metadata(codes):
    placeholders = ",".join("?" for _ in codes)
    with sqlite3.connect(MARKET_DB_PATH) as conn:
        names = pd.read_sql_query(
            f"""
            SELECT wind_code, sec_name
            FROM stock_universe
            WHERE wind_code IN ({placeholders})
            """,
            conn,
            params=codes,
        )
        industries = pd.read_sql_query(
            f"""
            SELECT wind_code, industry_level1
            FROM stock_industry
            WHERE classification_system = 'wind_level1'
              AND wind_code IN ({placeholders})
            """,
            conn,
            params=codes,
        )
    meta = pd.DataFrame({"wind_code": codes})
    meta = meta.merge(names, on="wind_code", how="left")
    meta = meta.merge(industries, on="wind_code", how="left")
    meta = meta.drop_duplicates("wind_code", keep="last")
    meta["sec_name"] = meta["sec_name"].fillna(meta["wind_code"])
    meta["industry_level1"] = meta["industry_level1"].fillna("未分类")
    return meta.set_index("wind_code")


def load_historical_snapshots(universe_name, start_date, end_date):
    return read_sql_df(
        """
        SELECT snapshot_date, wind_code, sec_name
        FROM universe_constituents_snapshot
        WHERE universe_name = ?
          AND snapshot_date <= ?
          AND snapshot_date >= (
              SELECT COALESCE(MAX(snapshot_date), ?)
              FROM universe_constituents_snapshot
              WHERE universe_name = ?
                AND snapshot_date < ?
          )
        ORDER BY snapshot_date, wind_code
        """,
        [universe_name, end_date, start_date, universe_name, start_date],
    )


def build_member_matrix(snapshots, trade_dates, codes):
    member = pd.DataFrame(False, index=trade_dates, columns=codes)
    if snapshots.empty:
        return member

    snapshots = snapshots.copy()
    snapshots["snapshot_date"] = pd.to_datetime(snapshots["snapshot_date"])
    snapshot_dates = snapshots["snapshot_date"].drop_duplicates().sort_values().tolist()
    for index, snapshot_date in enumerate(snapshot_dates):
        next_date = snapshot_dates[index + 1] if index + 1 < len(snapshot_dates) else None
        if next_date is None:
            active_dates = trade_dates[trade_dates >= snapshot_date]
        else:
            active_dates = trade_dates[(trade_dates >= snapshot_date) & (trade_dates < next_date)]
        active_codes = snapshots.loc[snapshots["snapshot_date"] == snapshot_date, "wind_code"]
        active_codes = [code for code in active_codes if code in member.columns]
        if len(active_dates) > 0 and active_codes:
            member.loc[active_dates, active_codes] = True
    return member


def load_price_panel(codes, start_date, end_date):
    placeholders = ",".join("?" for _ in codes)
    fields = ["open", "high", "low", "close", "volume", "amt"]
    query = f"""
        SELECT trade_date, wind_code, {", ".join(fields)}
        FROM daily_prices
        WHERE adjusted = 'F'
          AND trade_date >= ?
          AND trade_date <= ?
          AND wind_code IN ({placeholders})
        ORDER BY trade_date, wind_code
    """
    raw = read_sql_df(query, [start_date, end_date, *codes])
    if raw.empty:
        raise RuntimeError("本地行情库没有找到可用价格数据")

    raw["trade_date"] = pd.to_datetime(raw["trade_date"])
    panel = {}
    for field in fields:
        panel[field] = raw.pivot(index="trade_date", columns="wind_code", values=field).sort_index()
    trade_dates = panel["close"].index
    for field in fields:
        panel[field] = panel[field].reindex(index=trade_dates, columns=codes)
    return panel


def calculate_score_details(close_df, volume_df):
    ma5 = close_df.rolling(5).mean()
    ma60 = close_df.rolling(60).mean()
    ma120 = close_df.rolling(120).mean()
    spread = ma5 - ma60
    signal = np.sign(spread)
    cross = signal.diff()

    volume_ma = volume_df.rolling(VOLUME_RATIO_LOOKBACK).mean()
    volume_ratio = volume_df / volume_ma.replace(0, np.nan)
    score = {
        "ma5_ma60_gap": ma5 / ma60 - 1,
        "ma60_5d_trend": ma60 / ma60.shift(5) - 1,
        "volume_ratio": volume_ratio,
        "volume_ratio_score": VOLUME_RATIO_SCORE_WEIGHT
        * (volume_ratio - 1).clip(lower=0, upper=VOLUME_RATIO_MAX_BONUS_BASE),
    }
    score["total_score"] = (
        MA5_MA60_GAP_WEIGHT * score["ma5_ma60_gap"]
        + MA60_5D_TREND_WEIGHT * score["ma60_5d_trend"]
        + score["volume_ratio_score"]
    )
    filters = {
        "golden_cross_raw": cross == 2,
        "ma60_up": ma60 > ma60.shift(5),
        "ma120_up": ma120 > ma120.shift(5),
    }
    return score, filters


def build_signal_table(panel, member, meta, analysis_start, analysis_end):
    close_df = panel["close"]
    volume_df = panel["volume"]
    daily_ret = close_df.pct_change()
    score, filters = calculate_score_details(close_df, volume_df)
    limit_gain = daily_ret < SIGNAL_MAX_DAILY_RETURN
    golden_signal = (
        filters["golden_cross_raw"]
        & filters["ma60_up"]
        & filters["ma120_up"]
        & limit_gain
        & member
    )
    golden_signal = golden_signal.loc[analysis_start:analysis_end]

    rows = []
    trade_dates = close_df.index
    for signal_date, row in golden_signal.iterrows():
        codes = row[row].index.tolist()
        if not codes:
            continue
        date_pos = trade_dates.get_loc(signal_date)
        for code in codes:
            record = {
                "信号日期": signal_date,
                "代码": code,
                "名称": meta.at[code, "sec_name"] if code in meta.index else code,
                "行业": meta.at[code, "industry_level1"] if code in meta.index else "未分类",
                "评分": score["total_score"].at[signal_date, code],
                "MA5相对MA60强度": score["ma5_ma60_gap"].at[signal_date, code],
                "MA60近5日趋势": score["ma60_5d_trend"].at[signal_date, code],
                "量比": score["volume_ratio"].at[signal_date, code],
                "信号日涨幅": daily_ret.at[signal_date, code],
            }
            signal_close = close_df.at[signal_date, code]
            for horizon in HORIZONS:
                future_pos = date_pos + horizon
                col = f"{horizon}日后收益"
                if future_pos < len(trade_dates) and pd.notna(signal_close) and signal_close > 0:
                    future_close = close_df.iat[future_pos, close_df.columns.get_loc(code)]
                    record[col] = future_close / signal_close - 1 if pd.notna(future_close) else np.nan
                else:
                    record[col] = np.nan
            rows.append(record)

    return pd.DataFrame(rows), score, golden_signal


def summarize_signal_performance(signals):
    rows = []
    for horizon in HORIZONS:
        col = f"{horizon}日后收益"
        data = signals[col].dropna() if col in signals else pd.Series(dtype=float)
        rows.append({
            "观察窗口": f"{horizon}日",
            "样本数": len(data),
            "平均收益": data.mean() if len(data) else np.nan,
            "中位数收益": data.median() if len(data) else np.nan,
            "胜率": (data > 0).mean() if len(data) else np.nan,
            "盈亏比": (
                data[data > 0].mean() / abs(data[data < 0].mean())
                if (data > 0).any() and (data < 0).any()
                else np.nan
            ),
            "25分位": data.quantile(0.25) if len(data) else np.nan,
            "75分位": data.quantile(0.75) if len(data) else np.nan,
        })
    return pd.DataFrame(rows)


def summarize_signal_by_group(signals, group_col):
    rows = []
    for group_value, group_df in signals.groupby(group_col, dropna=False):
        row = {group_col: group_value, "信号数": len(group_df)}
        for horizon in HORIZONS:
            data = group_df[f"{horizon}日后收益"].dropna()
            row[f"{horizon}日均值"] = data.mean() if len(data) else np.nan
            row[f"{horizon}日胜率"] = (data > 0).mean() if len(data) else np.nan
        rows.append(row)
    return pd.DataFrame(rows).sort_values("信号数", ascending=False)


def calculate_ic(score, close_df, member, analysis_start, analysis_end):
    factor_map = {
        "综合评分": score["total_score"],
        "MA5相对MA60强度": score["ma5_ma60_gap"],
        "MA60近5日趋势": score["ma60_5d_trend"],
        "量比": score["volume_ratio"],
        "量比加分": score["volume_ratio_score"],
    }
    rows = []
    dates = close_df.loc[analysis_start:analysis_end].index
    for factor_name, factor_df in factor_map.items():
        for horizon in HORIZONS:
            future_ret = close_df.shift(-horizon) / close_df - 1
            for date in dates:
                x = factor_df.loc[date]
                y = future_ret.loc[date]
                mask = member.loc[date] & x.notna() & y.notna()
                if int(mask.sum()) < 30:
                    continue
                ic = x[mask].rank().corr(y[mask].rank())
                rows.append({
                    "日期": date,
                    "因子": factor_name,
                    "窗口": f"{horizon}日",
                    "IC": ic,
                    "样本数": int(mask.sum()),
                })
    ic_daily = pd.DataFrame(rows)
    if ic_daily.empty:
        return ic_daily, pd.DataFrame()

    summary = (
        ic_daily
        .groupby(["因子", "窗口"])
        .agg(
            期数=("IC", "count"),
            IC均值=("IC", "mean"),
            IC中位数=("IC", "median"),
            IC标准差=("IC", "std"),
            IC胜率=("IC", lambda s: (s > 0).mean()),
            平均样本数=("样本数", "mean"),
        )
        .reset_index()
    )
    summary["ICIR"] = summary["IC均值"] / summary["IC标准差"].replace(0, np.nan)
    return ic_daily, summary


def load_holdings(output_file):
    holdings = pd.read_excel(output_file, sheet_name="每日持仓")
    if holdings.empty:
        return holdings
    holdings["日期"] = pd.to_datetime(holdings["日期"])
    holdings["权重"] = pd.to_numeric(holdings["权重"], errors="coerce").fillna(0)
    return holdings


def calculate_attribution(holdings, close_df, meta):
    if holdings.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    holdings = holdings[holdings["日期"].isin(close_df.index)].copy()
    if holdings.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    weights = holdings.pivot_table(index="日期", columns="代码", values="权重", aggfunc="sum").fillna(0)
    weights = weights.reindex(index=close_df.index, columns=close_df.columns).fillna(0)
    stock_ret = close_df.pct_change()
    contribution = weights.shift(1).fillna(0) * stock_ret
    contribution = contribution.replace([np.inf, -np.inf], np.nan).fillna(0)

    long = contribution.stack().rename("收益贡献").reset_index()
    long = long.rename(columns={long.columns[0]: "日期", long.columns[1]: "代码"})
    long = long[long["收益贡献"] != 0]
    long["年份"] = long["日期"].dt.year
    long["月份"] = long["日期"].dt.to_period("M").astype(str)
    long["名称"] = long["代码"].map(meta["sec_name"]).fillna(long["代码"])
    long["行业"] = long["代码"].map(meta["industry_level1"]).fillna("未分类")

    by_year = long.groupby("年份", as_index=False).agg(收益贡献=("收益贡献", "sum"))
    by_month = long.groupby("月份", as_index=False).agg(收益贡献=("收益贡献", "sum"))
    by_industry = (
        long.groupby("行业", as_index=False)
        .agg(收益贡献=("收益贡献", "sum"), 贡献天数=("收益贡献", "count"))
        .sort_values("收益贡献", ascending=False)
    )
    by_stock = (
        long.groupby(["代码", "名称", "行业"], as_index=False)
        .agg(收益贡献=("收益贡献", "sum"), 贡献天数=("收益贡献", "count"))
        .sort_values("收益贡献", ascending=False)
    )
    return by_year, by_month, by_industry, by_stock


def calculate_holding_episodes(holdings, close_df, meta):
    if holdings.empty:
        return pd.DataFrame(), pd.DataFrame()

    holdings = holdings[holdings["日期"].isin(close_df.index)].copy()
    if holdings.empty:
        return pd.DataFrame(), pd.DataFrame()

    rows = []
    for code, group in holdings.groupby("代码"):
        dates = sorted(group["日期"].drop_duplicates())
        if not dates or code not in close_df.columns:
            continue
        episode_start = dates[0]
        previous = dates[0]
        for date in dates[1:]:
            if close_df.index.get_loc(date) != close_df.index.get_loc(previous) + 1:
                rows.append((code, episode_start, previous))
                episode_start = date
            previous = date
        rows.append((code, episode_start, previous))

    records = []
    for code, start, end in rows:
        start_price = close_df.at[start, code] if start in close_df.index else np.nan
        end_price = close_df.at[end, code] if end in close_df.index else np.nan
        hold_days = close_df.loc[start:end].shape[0]
        ret = end_price / start_price - 1 if pd.notna(start_price) and start_price > 0 and pd.notna(end_price) else np.nan
        records.append({
            "代码": code,
            "名称": meta.at[code, "sec_name"] if code in meta.index else code,
            "行业": meta.at[code, "industry_level1"] if code in meta.index else "未分类",
            "开始日期": start,
            "结束日期": end,
            "持有交易日": hold_days,
            "区间收益": ret,
        })
    episodes = pd.DataFrame(records)
    if episodes.empty:
        return episodes, pd.DataFrame()

    summary = pd.DataFrame([{
        "持仓片段数": len(episodes),
        "平均持有交易日": episodes["持有交易日"].mean(),
        "中位持有交易日": episodes["持有交易日"].median(),
        "平均区间收益": episodes["区间收益"].mean(),
        "中位区间收益": episodes["区间收益"].median(),
        "片段胜率": (episodes["区间收益"] > 0).mean(),
    }])
    return episodes.sort_values("区间收益", ascending=False), summary


def pick_metric(df, key_col, key_value, value_col):
    if df.empty or key_col not in df or value_col not in df:
        return np.nan
    matched = df.loc[df[key_col] == key_value, value_col]
    return matched.iloc[0] if len(matched) else np.nan


def judge_positive(value, good_threshold=0, watch_threshold=None):
    if pd.isna(value):
        return "无数据"
    if value > good_threshold:
        return "正常"
    if watch_threshold is not None and value > watch_threshold:
        return "观察"
    return "偏弱"


def build_focus_monitor(signal_summary, episode_summary, ic_summary):
    signal_20_mean = pick_metric(signal_summary, "观察窗口", "20日", "平均收益")
    signal_60_mean = pick_metric(signal_summary, "观察窗口", "60日", "平均收益")
    signal_60_median = pick_metric(signal_summary, "观察窗口", "60日", "中位数收益")
    signal_60_profit_loss = pick_metric(signal_summary, "观察窗口", "60日", "盈亏比")
    signal_60_win_rate = pick_metric(signal_summary, "观察窗口", "60日", "胜率")

    episode_mean = episode_summary["平均区间收益"].iloc[0] if not episode_summary.empty else np.nan
    episode_median = episode_summary["中位区间收益"].iloc[0] if not episode_summary.empty else np.nan
    episode_win_rate = episode_summary["片段胜率"].iloc[0] if not episode_summary.empty else np.nan
    episode_days = episode_summary["平均持有交易日"].iloc[0] if not episode_summary.empty else np.nan

    focus_rows = [
        {
            "优先级": 1,
            "监控项": "60日后平均收益",
            "当前值": signal_60_mean,
            "状态": judge_positive(signal_60_mean),
            "怎么看": "最核心。趋势策略要靠中期右尾赚钱，60日均值应保持为正。",
            "建议频率": "每周",
        },
        {
            "优先级": 2,
            "监控项": "20日后平均收益",
            "当前值": signal_20_mean,
            "状态": judge_positive(signal_20_mean),
            "怎么看": "确认金叉后一个月内是否已经有正向漂移。",
            "建议频率": "每周",
        },
        {
            "优先级": 3,
            "监控项": "60日盈亏比",
            "当前值": signal_60_profit_loss,
            "状态": judge_positive(signal_60_profit_loss, good_threshold=1.0, watch_threshold=0.8),
            "怎么看": "胜率不高也可以接受，但盈利样本均值要明显大于亏损样本。",
            "建议频率": "每周",
        },
        {
            "优先级": 4,
            "监控项": "持仓片段平均收益",
            "当前值": episode_mean,
            "状态": judge_positive(episode_mean),
            "怎么看": "比日胜率更贴近实际交易体验，衡量完整持仓片段是否赚钱。",
            "建议频率": "每周",
        },
        {
            "优先级": 5,
            "监控项": "综合评分20日IC",
            "当前值": pick_ic_metric(ic_summary, "综合评分", "20日", "IC均值"),
            "状态": judge_positive(pick_ic_metric(ic_summary, "综合评分", "20日", "IC均值")),
            "怎么看": "只用于评估评分排序，不是当前策略第一优先级。若长期为负，不宜按评分加权。",
            "建议频率": "每月/改评分后",
        },
        {
            "优先级": 6,
            "监控项": "综合评分60日IC",
            "当前值": pick_ic_metric(ic_summary, "综合评分", "60日", "IC均值"),
            "状态": judge_positive(pick_ic_metric(ic_summary, "综合评分", "60日", "IC均值")),
            "怎么看": "观察评分是否能解释中期收益排序。",
            "建议频率": "每月/改评分后",
        },
        {
            "优先级": 7,
            "监控项": "60日后中位数收益",
            "当前值": signal_60_median,
            "状态": judge_positive(signal_60_median, good_threshold=0, watch_threshold=-0.01),
            "怎么看": "中位数允许略弱；若均值和中位数同时恶化，信号质量需警惕。",
            "建议频率": "每周",
        },
        {
            "优先级": 8,
            "监控项": "60日胜率",
            "当前值": signal_60_win_rate,
            "状态": judge_positive(signal_60_win_rate, good_threshold=0.48, watch_threshold=0.45),
            "怎么看": "趋势策略不要求高胜率，但长期低于45%说明噪音偏大。",
            "建议频率": "每周",
        },
        {
            "优先级": 9,
            "监控项": "持仓片段中位收益",
            "当前值": episode_median,
            "状态": judge_positive(episode_median, good_threshold=0, watch_threshold=-0.01),
            "怎么看": "辅助判断收益是否过度依赖少数极端右尾。",
            "建议频率": "每月",
        },
        {
            "优先级": 10,
            "监控项": "持仓片段胜率",
            "当前值": episode_win_rate,
            "状态": judge_positive(episode_win_rate, good_threshold=0.48, watch_threshold=0.45),
            "怎么看": "辅助看完整交易片段的胜负结构。",
            "建议频率": "每月",
        },
        {
            "优先级": 11,
            "监控项": "平均持有交易日",
            "当前值": episode_days,
            "状态": "参考",
            "怎么看": "用于观察策略是否变得过短线或过度滞留。",
            "建议频率": "每月",
        },
    ]
    return pd.DataFrame(focus_rows)


def pick_ic_metric(ic_summary, factor, horizon, value_col):
    if ic_summary.empty:
        return np.nan
    matched = ic_summary.loc[
        (ic_summary["因子"] == factor)
        & (ic_summary["窗口"] == horizon),
        value_col,
    ]
    return matched.iloc[0] if len(matched) else np.nan


def build_focus_guide():
    return pd.DataFrame([
        {"顺序": 1, "事项": "每周优先看", "说明": "60日后平均收益、20日后平均收益、60日盈亏比、持仓片段平均收益。"},
        {"顺序": 2, "事项": "每月复盘看", "说明": "年度/月度/行业/个股归因，判断收益是否过度集中。"},
        {"顺序": 3, "事项": "改评分后看", "说明": "综合评分20日/60日IC和ICIR；当前评分IC偏弱，不建议直接按评分加权。"},
        {"顺序": 4, "事项": "不要过度反应", "说明": "单周波动很大，至少连续4周恶化再考虑调整规则。"},
        {"顺序": 5, "事项": "参数优化频率", "说明": "建议季度级别，避免因为最近几笔交易过拟合。"},
    ])


def main():
    output_file = latest_strategy_output()
    analysis_start, analysis_end = load_run_dates(output_file)
    data_start = (analysis_start - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    end_date = analysis_end.strftime("%Y-%m-%d")

    snapshots = load_historical_snapshots(CACHE_PREFIX, analysis_start.strftime("%Y-%m-%d"), end_date)
    codes = sorted(snapshots["wind_code"].dropna().unique().tolist())
    if not codes:
        raise RuntimeError(f"没有找到 {CACHE_PREFIX} 历史成分快照")

    panel = load_price_panel(codes, data_start, end_date)
    close_df = panel["close"]
    meta = load_code_metadata(codes)
    member = build_member_matrix(snapshots, close_df.index, codes)

    signals, score, _ = build_signal_table(panel, member, meta, analysis_start, analysis_end)
    signal_summary = summarize_signal_performance(signals)
    signal_by_industry = summarize_signal_by_group(signals, "行业") if not signals.empty else pd.DataFrame()
    signal_by_year = summarize_signal_by_group(
        signals.assign(年份=signals["信号日期"].dt.year), "年份"
    ) if not signals.empty else pd.DataFrame()

    ic_daily, ic_summary = calculate_ic(score, close_df, member, analysis_start, analysis_end)

    holdings = load_holdings(output_file)
    by_year, by_month, by_industry, by_stock = calculate_attribution(holdings, close_df, meta)
    episodes, episode_summary = calculate_holding_episodes(holdings, close_df, meta)
    focus_monitor = build_focus_monitor(signal_summary, episode_summary, ic_summary)
    focus_guide = build_focus_guide()

    report_date = analysis_end.strftime("%Y-%m-%d")
    report_file = os.path.join(OUTPUT_DIR, f"金叉策略有效性分析_归因_IC_{report_date}.xlsx")
    run_info = pd.DataFrame([
        {"项目": "策略输出文件", "值": output_file},
        {"项目": "分析生成时间", "值": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
        {"项目": "分析起始日期", "值": analysis_start.strftime("%Y-%m-%d")},
        {"项目": "分析结束日期", "值": report_date},
        {"项目": "股票池", "值": CACHE_PREFIX},
        {"项目": "股票数", "值": len(codes)},
        {"项目": "数据来源", "值": "本地SQLite + 策略Excel，不调用Wind"},
        {"项目": "归因说明", "值": "用前一日持仓权重乘以当日个股收盘收益，近似估算日度收益贡献"},
    ])

    with pd.ExcelWriter(report_file, engine="openpyxl") as writer:
        run_info.to_excel(writer, sheet_name="说明", index=False)
        focus_monitor.to_excel(writer, sheet_name="重点监控", index=False)
        focus_guide.to_excel(writer, sheet_name="阅读顺序", index=False)
        by_year.to_excel(writer, sheet_name="年度归因", index=False)
        by_month.to_excel(writer, sheet_name="月度归因", index=False)
        by_industry.to_excel(writer, sheet_name="行业归因", index=False)
        by_stock.head(200).to_excel(writer, sheet_name="个股归因Top200", index=False)
        episode_summary.to_excel(writer, sheet_name="持仓片段概览", index=False)
        episodes.head(500).to_excel(writer, sheet_name="持仓片段Top500", index=False)
        signal_summary.to_excel(writer, sheet_name="金叉后收益概览", index=False)
        signal_by_year.to_excel(writer, sheet_name="金叉后收益_按年份", index=False)
        signal_by_industry.to_excel(writer, sheet_name="金叉后收益_按行业", index=False)
        signals.to_excel(writer, sheet_name="金叉信号明细", index=False)
        ic_summary.to_excel(writer, sheet_name="IC汇总", index=False)
        ic_daily.to_excel(writer, sheet_name="IC日度", index=False)

    print(f"策略输出：{output_file}")
    print(f"有效性分析已生成：{report_file}")
    print("\n【金叉后收益概览】")
    print(signal_summary.to_string(index=False))
    print("\n【IC汇总】")
    if ic_summary.empty:
        print("无可用IC")
    else:
        print(ic_summary.to_string(index=False))


if __name__ == "__main__":
    main()
