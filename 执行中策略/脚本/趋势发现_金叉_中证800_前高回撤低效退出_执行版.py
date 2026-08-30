from WindPy import w
import os
import sqlite3
import tempfile
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from openpyxl.styles import Font
from openpyxl.drawing.image import Image as OpenpyxlImage

from 维护工具.local_market_db import (
    MARKET_DB_PATH,
    TEMP_CACHE_DIR,
    ensure_market_data_updated,
    ensure_universe_constituents_current,
    get_latest_price_date,
    load_price_matrix,
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
os.makedirs(TEMP_CACHE_DIR, exist_ok=True)

# =========================
# 0. 参数
# =========================
A_SHARE_TIMEZONE = ZoneInfo("Asia/Shanghai")
A_SHARE_CONTINUOUS_TRADING_START = (9, 30)
MAX_HOLDINGS = 20
INITIAL_WEIGHT = 1 / MAX_HOLDINGS
TRANSACTION_COST_RATE = 0.0025
PEAK_RETRACE_SELL_DRAWDOWN = 0.1175
CONCENTRATED_INDUSTRY_HOLDING_COUNT = 6
CONCENTRATED_INDUSTRY_RETRACE_SELL_DRAWDOWN = 0.10
LOW_EFFICIENCY_MIN_HOLDING_DAYS = 60
LOW_EFFICIENCY_MAX_PROFIT_THRESHOLD = 0.05
LOW_EFFICIENCY_CURRENT_PROFIT_THRESHOLD = 0.00

# 评分系统
# total_score =
#   MA5_MA60_GAP_WEIGHT * (MA5 / MA60 - 1)
#   + MA60_5D_TREND_WEIGHT * (MA60 / MA60.shift(5) - 1)
#   + VOLUME_RATIO_SCORE_WEIGHT * clip(成交量 / 20日均量 - 1, 0, VOLUME_RATIO_MAX_BONUS_BASE)
MA5_MA60_GAP_WEIGHT = 1.0
MA60_5D_TREND_WEIGHT = 1.0
VOLUME_RATIO_SCORE_WEIGHT = 0.25
VOLUME_RATIO_LOOKBACK = 20
VOLUME_RATIO_MAX_BONUS_BASE = 1.0

# 金叉触发日最大涨幅过滤。
# 已用本地中证800历史成分与缓存行情做核心三参数联合优化。
SIGNAL_MAX_DAILY_RETURN = 0.065

LOOKBACK_DAYS = 1600
TRADE_START_DATE = "2022-01-01"  # 必须写成字符串，例如 "2025-01-01"；None 表示沿用当前逻辑
CACHE_PREFIX = "中证800"
WIND_INDEX_CODE = "000906.SH"
BOTTOM_ETF_CODE = "515800.SH"
BOTTOM_ETF_NAME = "添富中证800ETF"
BOTTOM_ETF_MAX_HOLDING_DAYS = 20
REVERSAL_VOLATILITY_WINDOW = 20
REVERSAL_VOLATILITY_THRESHOLD_WINDOW = 756
REVERSAL_VOLATILITY_THRESHOLD_QUANTILE = 0.80
REVERSAL_INDEX_POSITION_WINDOW = 250
REVERSAL_INDEX_POSITION_MAX = 0.20
REVERSAL_INDEX_RETURN_WINDOW = 20
REVERSAL_SIGNAL_COOLDOWN_DAYS = 20
USE_HISTORICAL_CONSTITUENTS = True
BANK_RETRACE_SELL_DRAWDOWN = 0.08
RISK_FREE_WIND_INDICATOR = "S0059741"
RISK_FREE_NAME = "中债国债到期收益率:3个月"
FALLBACK_ANNUAL_RISK_FREE_RATE = 0.02

constituent_snapshot_cutoff = ""
constituent_validation_date = ""
constituent_validation_status = "尚未校验"
constituent_current_count = 0
constituent_added_count = 0
constituent_removed_count = 0
constituent_written_change_dates = ""

RUN_PARAMETER_EXPLANATIONS = {
    "脚本路径": "本次生成报表所使用的脚本文件位置，用于追溯版本。",
    "脚本最后修改时间": "脚本文件在本机最后一次保存的时间，用于判断报表是否来自最新代码。",
    "报表生成时间": "本次运行脚本并生成Excel的时间。",
    "交易开始日期": "回测/跟踪统计从这个交易日开始计算；早于该日期的数据只用于均线和前置指标预热。",
    "行情结束日期": "本次报表使用到的最后一个行情日期。",
    "本地完整日线截止日期": "本地SQLite行情库中已经确认完整入库的最新交易日。",
    "最大持仓数": "组合最多同时持有的股票数量，也是单只股票初始权重的分母。",
    "初始目标仓位": "新买入股票的目标权重，当前等于1/最大持仓数。",
    "交易成本率": "买卖时扣除的单边近似交易成本，用于让净值更接近真实交易。",
    "前高回撤卖出比例": "持仓从买入后最高价回撤超过该比例时触发卖出。",
    "行业集中持仓数量阈值": "当某个Wind一级行业当前持仓数量达到该阈值时，该行业内持仓使用更严格的前高回撤卖出比例。",
    "行业集中前高回撤卖出比例": "行业集中时，仅该行业内个股使用的前高回撤卖出比例。",
    "低效退出最短持仓日": "持有时间超过该交易日数后，才会检查是否属于低效持仓。",
    "低效退出历史最高浮盈阈值": "低效退出条件之一：持仓期间最高浮盈没有超过该阈值。",
    "低效退出当前浮盈阈值": "低效退出条件之一：当前浮盈不高于该阈值。",
    "MA5相对MA60评分权重": "综合评分中，短期均线相对中期均线强度的权重。",
    "MA60近5日趋势评分权重": "综合评分中，MA60自身近5日上行强度的权重。",
    "量比评分权重": "综合评分中，成交量放大加分项的权重。",
    "金叉日最大涨幅": "金叉当天涨幅超过该阈值会被剔除，避免追入过热信号。",
    "使用历史成分股快照": "为True时按历史中证800成分回测，减少用当前成分回看历史造成的幸存者偏差。",
    "银行专属回撤比例": "Wind二级行业为银行的个股使用更严格的前高回撤卖出比例。",
    "底部临时仓位ETF": "市场底部反转信号确认后，下一交易日开盘用闲置现金买入的中证800ETF。",
    "底部ETF最长持有交易日": "临时ETF仓位若未被后续个股金叉信号完全替换，达到该交易日数后在开盘卖出。",
    "底部信号波动率窗口": "以指数日收益计算已实现波动率的滚动交易日窗口。",
    "波动率阈值历史窗口": "用多少个历史交易日的已实现波动率计算动态分位数阈值。",
    "波动率阈值分位数": "当前波动率向上穿越该历史分位数时，构成底部反转信号的波动率条件。",
    "指数位置窗口": "用滚动区间最高点和最低点衡量指数当前所处位置的交易日窗口。",
    "指数低位位置上限": "指数在滚动高低区间中的位置不高于该比例，才视为处于低位。",
    "指数收益确认窗口": "要求信号日前指数该窗口累计收益为负，避免把高位波动放大误判为底部。",
    "底部信号冷却交易日": "两次底部信号之间至少间隔的交易日数，避免同一轮波动反复触发。",
    "历史成分快照截止日期": "本次回测实际使用的最后一期中证800历史成分快照日期。",
    "当前成分校验日期": "本次与Wind当前中证800具体成分代码进行身份比对的日期。",
    "当前成分校验状态": "matched表示代码集合一致；updated表示发现变化并补写了实际生效日快照。",
    "Wind当前成分数量": "Wind当前返回的中证800成分数量，正常应为800只。",
    "相对旧快照调入数量": "本次校验时，Wind当前成分相对校验前最后一期快照新增的代码数量。",
    "相对旧快照调出数量": "本次校验时，校验前最后一期快照中已不在Wind当前成分的代码数量。",
    "本次补写成分变更日期": "发现成分身份变化时，逐交易日定位并写入的实际生效日期。",
}

STRATEGY_METRIC_EXPLANATIONS = {
    "年化收益": "按实际持仓后的净值区间折算到一年的收益率。",
    "年化波动": "日收益波动率按252个交易日折算到一年，衡量净值波动大小。",
    "平均年化无风险利率": "优先取Wind回测区间内3个月国债收益率的算术平均；Wind数据不可用时采用2%的固定年化利率。",
    "无风险利率来源": "本次夏普计算所采用的无风险利率数据来源。",
    "夏普比率": "策略日收益减去固定日无风险收益后的年化超额收益波动比；越高说明单位波动的超额收益越好。",
    "最大回撤": "统计期内净值从阶段高点到后续低点的最大跌幅。",
    "平均持仓数": "有持仓统计期内，组合每日平均持有的股票数量。",
    "年化换手率": "日均换手率乘以252，粗略衡量一年内组合换仓频率。",
    "日胜率": "策略日收益大于0的交易日占比。",
    "平仓笔数": "已经完成卖出的持仓片段数量。",
    "单笔盈利数": "平仓后单笔收益为正的交易数量。",
    "单笔平局数": "平仓后单笔收益等于0或接近0的交易数量。",
    "单笔亏损数": "平仓后单笔收益为负的交易数量。",
    "单笔胜率(平局不计入)": "只在盈利和亏损交易中计算胜率，不把平局计入分母。",
}


def build_run_parameters(actual_end_date):
    df = pd.DataFrame([
        {"参数": "脚本路径", "数值": os.path.abspath(__file__)},
        {
            "参数": "脚本最后修改时间",
            "数值": datetime.fromtimestamp(os.path.getmtime(__file__)).strftime("%Y-%m-%d %H:%M:%S"),
        },
        {"参数": "报表生成时间", "数值": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
        {"参数": "交易开始日期", "数值": TRADE_START_DATE},
        {"参数": "行情结束日期", "数值": actual_end_date},
        {"参数": "本地完整日线截止日期", "数值": get_latest_price_date() or ""},
        {"参数": "最大持仓数", "数值": MAX_HOLDINGS},
        {"参数": "初始目标仓位", "数值": INITIAL_WEIGHT},
        {"参数": "交易成本率", "数值": TRANSACTION_COST_RATE},
        {"参数": "前高回撤卖出比例", "数值": PEAK_RETRACE_SELL_DRAWDOWN},
        {"参数": "行业集中持仓数量阈值", "数值": CONCENTRATED_INDUSTRY_HOLDING_COUNT},
        {"参数": "行业集中前高回撤卖出比例", "数值": CONCENTRATED_INDUSTRY_RETRACE_SELL_DRAWDOWN},
        {"参数": "低效退出最短持仓日", "数值": LOW_EFFICIENCY_MIN_HOLDING_DAYS},
        {"参数": "低效退出历史最高浮盈阈值", "数值": LOW_EFFICIENCY_MAX_PROFIT_THRESHOLD},
        {"参数": "低效退出当前浮盈阈值", "数值": LOW_EFFICIENCY_CURRENT_PROFIT_THRESHOLD},
        {"参数": "MA5相对MA60评分权重", "数值": MA5_MA60_GAP_WEIGHT},
        {"参数": "MA60近5日趋势评分权重", "数值": MA60_5D_TREND_WEIGHT},
        {"参数": "量比评分权重", "数值": VOLUME_RATIO_SCORE_WEIGHT},
        {"参数": "金叉日最大涨幅", "数值": SIGNAL_MAX_DAILY_RETURN},
        {"参数": "使用历史成分股快照", "数值": USE_HISTORICAL_CONSTITUENTS},
        {"参数": "银行专属回撤比例", "数值": BANK_RETRACE_SELL_DRAWDOWN},
        {"参数": "底部临时仓位ETF", "数值": f"{BOTTOM_ETF_CODE} {BOTTOM_ETF_NAME}"},
        {"参数": "底部ETF最长持有交易日", "数值": BOTTOM_ETF_MAX_HOLDING_DAYS},
        {"参数": "底部信号波动率窗口", "数值": REVERSAL_VOLATILITY_WINDOW},
        {"参数": "波动率阈值历史窗口", "数值": REVERSAL_VOLATILITY_THRESHOLD_WINDOW},
        {"参数": "波动率阈值分位数", "数值": REVERSAL_VOLATILITY_THRESHOLD_QUANTILE},
        {"参数": "指数位置窗口", "数值": REVERSAL_INDEX_POSITION_WINDOW},
        {"参数": "指数低位位置上限", "数值": REVERSAL_INDEX_POSITION_MAX},
        {"参数": "指数收益确认窗口", "数值": REVERSAL_INDEX_RETURN_WINDOW},
        {"参数": "底部信号冷却交易日", "数值": REVERSAL_SIGNAL_COOLDOWN_DAYS},
        {"参数": "历史成分快照截止日期", "数值": constituent_snapshot_cutoff},
        {"参数": "当前成分校验日期", "数值": constituent_validation_date},
        {"参数": "当前成分校验状态", "数值": constituent_validation_status},
        {"参数": "Wind当前成分数量", "数值": constituent_current_count},
        {"参数": "相对旧快照调入数量", "数值": constituent_added_count},
        {"参数": "相对旧快照调出数量", "数值": constituent_removed_count},
        {"参数": "本次补写成分变更日期", "数值": constituent_written_change_dates},
    ])
    df["解释"] = df["参数"].map(RUN_PARAMETER_EXPLANATIONS).fillna("")
    return df


print("\n【本次运行核心参数】")
print(build_run_parameters("").to_string(index=False))

# =========================
# 1. 启动 Wind
# =========================
w.start()

# =========================
# 2. 中证800成分股
# =========================
sector_id = "1000011893000000"

sector = w.wset("sectorconstituent", f"sectorid={sector_id}")

code_idx = sector.Fields.index("wind_code")
name_idx = sector.Fields.index("sec_name")

stock_codes = sector.Data[code_idx]
stock_names = sector.Data[name_idx]
code_to_name = dict(zip(stock_codes, stock_names))
current_constituent_df = pd.DataFrame({
    "wind_code": stock_codes,
    "sec_name": stock_names,
})
current_constituent_codes = set(stock_codes)

print("中证800股票数量：", len(stock_codes))


def load_historical_constituent_snapshots(universe_name, query_start_date, query_end_date):
    with sqlite3.connect(MARKET_DB_PATH) as conn:
        prior_snapshot = pd.read_sql_query(
            """
            SELECT snapshot_date, wind_code, sec_name
            FROM universe_constituents_snapshot
            WHERE universe_name = ?
              AND snapshot_date = (
                  SELECT MAX(snapshot_date)
                  FROM universe_constituents_snapshot
                  WHERE universe_name = ?
                    AND snapshot_date < ?
              )
            """,
            conn,
            params=[universe_name, universe_name, query_start_date],
        )
        range_snapshots = pd.read_sql_query(
            """
            SELECT snapshot_date, wind_code, sec_name
            FROM universe_constituents_snapshot
            WHERE universe_name = ?
              AND snapshot_date >= ?
              AND snapshot_date <= ?
            ORDER BY snapshot_date, wind_code
            """,
            conn,
            params=[universe_name, query_start_date, query_end_date],
        )

    snapshots = pd.concat([prior_snapshot, range_snapshots], ignore_index=True)
    if snapshots.empty:
        return snapshots

    snapshots["snapshot_date"] = pd.to_datetime(snapshots["snapshot_date"])
    snapshots = snapshots.drop_duplicates(["snapshot_date", "wind_code"], keep="last")
    snapshots = snapshots.sort_values(["snapshot_date", "wind_code"]).reset_index(drop=True)
    return snapshots


def build_daily_universe_member_matrix(snapshots, trade_dates, codes):
    member = pd.DataFrame(False, index=trade_dates, columns=codes)
    if snapshots.empty or len(trade_dates) == 0:
        return member

    snapshots = snapshots.sort_values("snapshot_date")
    snapshot_dates = snapshots["snapshot_date"].drop_duplicates().tolist()
    for i, snapshot_date in enumerate(snapshot_dates):
        next_snapshot_date = snapshot_dates[i + 1] if i + 1 < len(snapshot_dates) else None
        if next_snapshot_date is None:
            active_dates = trade_dates[trade_dates >= snapshot_date]
        else:
            active_dates = trade_dates[(trade_dates >= snapshot_date) & (trade_dates < next_snapshot_date)]
        if len(active_dates) == 0:
            continue

        snapshot_codes = snapshots.loc[snapshots["snapshot_date"] == snapshot_date, "wind_code"].tolist()
        active_codes = [code for code in snapshot_codes if code in member.columns]
        if active_codes:
            member.loc[active_dates, active_codes] = True

    return member


def load_stock_industry_map(codes):
    if not codes:
        return {}

    placeholders = ",".join("?" for _ in codes)
    with sqlite3.connect(MARKET_DB_PATH) as conn:
        industry_df = pd.read_sql_query(
            f"""
            SELECT wind_code, industry_level1
            FROM stock_industry
            WHERE classification_system = 'wind_level1'
              AND wind_code IN ({placeholders})
            """,
            conn,
            params=codes,
        )

    if industry_df.empty:
        return {}

    return (
        industry_df
        .dropna(subset=["wind_code"])
        .drop_duplicates("wind_code", keep="last")
        .set_index("wind_code")["industry_level1"]
        .fillna("未分类")
        .to_dict()
    )


def fetch_wind_level2_industry_map(codes, query_date, batch_size=500):
    if not codes:
        return {}

    industry_map = {}
    options = (
        f"tradeDate={pd.Timestamp(query_date).strftime('%Y%m%d')};"
        "industryType=2;"
    )
    for start in range(0, len(codes), batch_size):
        batch_codes = codes[start:start + batch_size]
        data = w.wss(batch_codes, "wicsname2024", options)
        if data.ErrorCode != 0 or not data.Data:
            raise RuntimeError(
                "万得二级行业拉取失败："
                f"{start}-{start + len(batch_codes)} ErrorCode={data.ErrorCode}"
            )
        industry_map.update(dict(zip(data.Codes, data.Data[0])))
    return industry_map


def get_industry_name(code):
    return code_to_industry.get(code, "未分类")


def get_industry_holding_counts(holdings):
    counts = {}
    for holding_code in holdings:
        industry = get_industry_name(holding_code)
        counts[industry] = counts.get(industry, 0) + 1
    return counts


def get_applicable_retrace_drawdown(code, industry_holding_counts):
    industry = get_industry_name(code)
    industry_holding_count = industry_holding_counts.get(industry, 0)
    if code_to_industry_level2.get(code) == "银行":
        return BANK_RETRACE_SELL_DRAWDOWN, "银行专属"
    if industry_holding_count >= CONCENTRATED_INDUSTRY_HOLDING_COUNT:
        return (
            CONCENTRATED_INDUSTRY_RETRACE_SELL_DRAWDOWN,
            f"行业集中({industry_holding_count}只)",
        )
    return PEAK_RETRACE_SELL_DRAWDOWN, "常规"


def get_limit_up_ratio(code, stock_name):
    stock_name = str(stock_name).upper()
    if "ST" in stock_name:
        return 0.05
    if code.endswith(".BJ"):
        return 0.30
    if code.startswith("300") or code.startswith("688"):
        return 0.20
    return 0.10


def get_limit_down_ratio(code, stock_name):
    return get_limit_up_ratio(code, stock_name)


def is_delisted_security(code):
    return "退市" in str(code_to_name.get(code, ""))


def sanitize_score_component(df):
    return df.replace([np.inf, -np.inf], np.nan)


def calculate_golden_cross_score_details(ma5_df, ma60_df, volume_df):
    ma5_ma60_gap_score = MA5_MA60_GAP_WEIGHT * (ma5_df / ma60_df - 1)
    ma60_5d_trend_score = MA60_5D_TREND_WEIGHT * (ma60_df / ma60_df.shift(5) - 1)
    volume_ma = volume_df.rolling(VOLUME_RATIO_LOOKBACK).mean()
    volume_ratio = volume_df / volume_ma.replace(0, np.nan)
    volume_ratio_score = VOLUME_RATIO_SCORE_WEIGHT * (
        (volume_ratio - 1).clip(lower=0, upper=VOLUME_RATIO_MAX_BONUS_BASE)
    )
    total_score = ma5_ma60_gap_score + ma60_5d_trend_score + volume_ratio_score
    return {
        "ma5_ma60_gap_score": sanitize_score_component(ma5_ma60_gap_score),
        "ma60_5d_trend_score": sanitize_score_component(ma60_5d_trend_score),
        "volume_ratio": sanitize_score_component(volume_ratio),
        "volume_ratio_score": sanitize_score_component(volume_ratio_score),
        "total_score": sanitize_score_component(total_score),
    }


def evaluate_intraday_sell_signal(
    holding_info,
    open_price,
    low_price,
    high_price,
    retrace_drawdown=PEAK_RETRACE_SELL_DRAWDOWN,
    sell_reason_prefix="前高回撤",
):
    entry_price = holding_info["entry_price"]
    prev_peak_price = holding_info["peak_price"]
    max_profit = prev_peak_price / entry_price - 1 if entry_price > 0 else -np.inf
    sell_price = np.nan
    is_breakeven_exit = False
    sell_reason_text = ""
    monitor_level = np.nan

    if prev_peak_price > 0:
        retrace_price = prev_peak_price * (1 - retrace_drawdown)
        monitor_level = retrace_price
        if low_price <= retrace_price:
            sell_price = open_price if open_price < retrace_price else retrace_price
            sell_reason_text = f"{sell_reason_prefix}{retrace_drawdown:.2%}卖出"

    return {
        "sell_price": sell_price,
        "is_breakeven_exit": is_breakeven_exit,
        "sell_reason_text": sell_reason_text,
        "monitor_level": monitor_level,
        "max_profit": max_profit,
        "prev_peak_price": prev_peak_price,
    }

# =========================
# 3. 时间区间
# =========================
def parse_date(date_str):
    if isinstance(date_str, pd.Timestamp):
        return date_str.date()
    if isinstance(date_str, datetime):
        return date_str.date()
    if hasattr(date_str, "year") and hasattr(date_str, "month") and hasattr(date_str, "day") and not isinstance(date_str, str):
        return date_str
    if not isinstance(date_str, str):
        raise TypeError('TRADE_START_DATE 必须是 "YYYY-MM-DD" 格式的字符串，例如 "2025-01-01"。')
    return datetime.strptime(date_str, "%Y-%m-%d").date()


def realtime_quotes_are_usable(latest_trading_date, now=None):
    """仅在中国市场当日已经开盘时，才允许把 WSQ 行情标为当天数据。"""
    current_time = now or datetime.now(A_SHARE_TIMEZONE)
    today_str = current_time.strftime("%Y-%m-%d")
    opening_reached = (
        current_time.hour,
        current_time.minute,
    ) >= A_SHARE_CONTINUOUS_TRADING_START
    return latest_trading_date == today_str and opening_reached


end_dt = datetime.now(A_SHARE_TIMEZONE)
end_date = end_dt.strftime("%Y-%m-%d")
realtime_quotes_enabled = False

if TRADE_START_DATE is not None:
    trade_start_dt = parse_date(TRADE_START_DATE)
    trade_start_ts = pd.Timestamp(trade_start_dt)
    data_start_dt = trade_start_dt - timedelta(days=LOOKBACK_DAYS)
else:
    trade_start_dt = None
    trade_start_ts = None
    data_start_dt = end_dt.date() - timedelta(days=LOOKBACK_DAYS)

start_date = data_start_dt.strftime("%Y-%m-%d")

print(f"数据开始日期: {start_date}")
print(f"数据结束日期: {end_date}")
if TRADE_START_DATE is not None:
    print(f"开始建仓日期: {TRADE_START_DATE}")
    if trade_start_dt > end_dt.date():
        raise ValueError("TRADE_START_DATE 不能晚于 end_date，请检查参数设置。")

wind_latest_trading_date = ensure_market_data_updated(
    w,
    end_date,
    universe_name=CACHE_PREFIX,
    sector_id=sector_id,
    price_fields=["open", "high", "low", "close", "volume", "amt"],
    target_codes=stock_codes,
)
now_local = datetime.now(A_SHARE_TIMEZONE)
today_str = now_local.strftime("%Y-%m-%d")
realtime_quotes_enabled = realtime_quotes_are_usable(
    wind_latest_trading_date,
    now=now_local,
)

if realtime_quotes_enabled:
    end_date = wind_latest_trading_date
    print(f"A股已开盘，允许使用 {end_date} 的 WSQ 实时行情。")
else:
    latest_local_price_date = get_latest_price_date()
    if wind_latest_trading_date == today_str:
        # 开盘前 WSQ 的 rt_last 往往仍是昨收，绝不能把它标成今天的行情。
        complete_date_ceiling = (
            pd.Timestamp(wind_latest_trading_date) - pd.Timedelta(days=1)
        ).strftime("%Y-%m-%d")
    else:
        # 周末和休市日，Wind 返回的最近交易日本身就是可用上限。
        complete_date_ceiling = wind_latest_trading_date

    if latest_local_price_date is None:
        end_date = complete_date_ceiling
    else:
        end_date = min(latest_local_price_date, complete_date_ceiling)
    print(
        f"当前时间 {now_local.strftime('%H:%M:%S')} 尚无可用当日完整实时行情，"
        f"策略统一使用本地最近完整交易日 {end_date}。"
    )
end_dt = pd.Timestamp(end_date).to_pydatetime()

constituent_check = ensure_universe_constituents_current(
    w,
    CACHE_PREFIX,
    sector_id,
    end_date,
    current_snapshot=current_constituent_df,
    expected_count=800,
)
constituent_validation_date = constituent_check["target_date"]
constituent_validation_status = constituent_check["status"]
constituent_current_count = constituent_check["current_count"]
constituent_added_count = constituent_check["added_count"]
constituent_removed_count = constituent_check["removed_count"]
constituent_written_change_dates = "、".join(
    constituent_check["written_change_dates"]
)
print(
    "中证800当前成分身份校验："
    f"status={constituent_validation_status}, "
    f"current={constituent_current_count}, "
    f"added={constituent_added_count}, "
    f"removed={constituent_removed_count}, "
    f"written_dates={constituent_written_change_dates or '无'}"
)

historical_constituent_snapshots = pd.DataFrame()
if USE_HISTORICAL_CONSTITUENTS:
    constituent_start_date = TRADE_START_DATE if TRADE_START_DATE is not None else start_date
    historical_constituent_snapshots = load_historical_constituent_snapshots(
        CACHE_PREFIX,
        constituent_start_date,
        end_date,
    )
    if historical_constituent_snapshots.empty:
        print("未找到中证800历史成分快照，回退为当前中证800成分。")
    else:
        snapshot_codes = historical_constituent_snapshots["wind_code"].drop_duplicates().tolist()
        snapshot_names = (
            historical_constituent_snapshots
            .dropna(subset=["sec_name"])
            .drop_duplicates("wind_code", keep="last")
            .set_index("wind_code")["sec_name"]
            .to_dict()
        )
        stock_codes = snapshot_codes
        code_to_name.update(snapshot_names)
        first_snapshot_date = historical_constituent_snapshots["snapshot_date"].min().strftime("%Y-%m-%d")
        last_snapshot_date = historical_constituent_snapshots["snapshot_date"].max().strftime("%Y-%m-%d")
        constituent_snapshot_cutoff = last_snapshot_date
        print(
            f"使用中证800历史月频成分：{first_snapshot_date} ~ {last_snapshot_date}，"
            f"历史并集股票数量：{len(stock_codes)}"
        )

code_to_industry = load_stock_industry_map(stock_codes)
code_to_industry_level2 = fetch_wind_level2_industry_map(stock_codes, end_date)
missing_industry_count = len([code for code in stock_codes if code not in code_to_industry])
if missing_industry_count:
    print(f"本地行业映射缺失数量：{missing_industry_count}，缺失股票在报表中标记为未分类。")

def get_wsq_rt_open_batch(codes, trade_date, batch_size=500):
    all_rows = []
    effective_batch_size = 1000

    for i in range(0, len(codes), effective_batch_size):
        batch = codes[i:i + effective_batch_size]
        print(f"拉取 rt_open: {i}-{i + len(batch)} [{trade_date}]")
        data = w.wsq(batch, "rt_open")

        if data.ErrorCode != 0 or len(data.Codes) == 0 or len(data.Data) == 0:
            print(
                f"rt_open 拉取失败: ErrorCode={data.ErrorCode}, "
                f"codes={len(data.Codes)}, data_rows={len(data.Data)}"
            )
            print("失败批次示例：", batch[:3])
            continue

        batch_df = pd.DataFrame([data.Data[0]], index=[pd.Timestamp(trade_date)], columns=data.Codes)
        batch_df = batch_df.apply(pd.to_numeric, errors="coerce")
        all_rows.append(batch_df)

    if not all_rows:
        return pd.DataFrame()

    df = pd.concat(all_rows, axis=1)
    df = df.apply(pd.to_numeric, errors="coerce")
    df = df.sort_index()
    df = df.loc[:, ~df.columns.duplicated()]
    return df


def get_wsq_realtime_batch(codes, wsq_field, trade_date, batch_size=500):
    all_rows = []
    effective_batch_size = 1000

    for i in range(0, len(codes), effective_batch_size):
        batch = codes[i:i + effective_batch_size]
        print(f"拉取 {wsq_field}: {i}-{i + len(batch)} [{trade_date}]")
        data = w.wsq(batch, wsq_field)

        if data.ErrorCode != 0 or len(data.Codes) == 0 or len(data.Data) == 0:
            print(
                f"{wsq_field} 拉取失败: ErrorCode={data.ErrorCode}, "
                f"codes={len(data.Codes)}, data_rows={len(data.Data)}"
            )
            print("失败批次示例：", batch[:3])
            continue

        batch_df = pd.DataFrame([data.Data[0]], index=[pd.Timestamp(trade_date)], columns=data.Codes)
        batch_df = batch_df.apply(pd.to_numeric, errors="coerce")
        all_rows.append(batch_df)

    if not all_rows:
        return pd.DataFrame()

    df = pd.concat(all_rows, axis=1)
    df = df.apply(pd.to_numeric, errors="coerce")
    df = df.sort_index()
    df = df.loc[:, ~df.columns.duplicated()]
    return df


def get_cache_path(field):
    return os.path.join(TEMP_CACHE_DIR, f"{CACHE_PREFIX}_{field}_PriceAdjF.pkl")


def sanitize_price_df(df, allow_zero=False):
    if df.empty:
        return df

    df = df.apply(pd.to_numeric, errors="coerce")
    # 停牌日成交量和成交额可以为 0；价格字段中的 0 和负值仍视为无效。
    df = df.where(df >= 0 if allow_zero else df > 0, np.nan)
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    df = df.loc[:, ~df.columns.duplicated()]
    return df


def load_cached_df(field):
    cache_path = get_cache_path(field)
    if not os.path.exists(cache_path):
        return pd.DataFrame()

    df = pd.read_pickle(cache_path)
    if df.empty:
        return df

    return sanitize_price_df(df, allow_zero=field in {"volume", "amt"})


def save_cached_df(field, df):
    cache_path = get_cache_path(field)
    df = sanitize_price_df(df, allow_zero=field in {"volume", "amt"})
    df.to_pickle(cache_path)


def has_recent_all_nan_rows(df, recent_days=3):
    if df.empty:
        return False
    recent_df = df.tail(recent_days)
    return (recent_df.notna().sum(axis=1) == 0).any()


def drop_all_nan_rows(df, allow_zero=False):
    if df.empty:
        return df
    return sanitize_price_df(df.loc[df.notna().sum(axis=1) > 0], allow_zero=allow_zero)


def last_row_all_nan(df):
    if df.empty:
        return False
    return df.iloc[-1].notna().sum() == 0


def supplement_with_wsq(df, codes, field, trade_date):
    realtime_field_map = {
        "open": "rt_open",
        "low": "rt_low",
        "high": "rt_high",
        "close": "rt_last",
        "volume": "rt_vol",
        "amt": "rt_amt",
    }
    wsq_field = realtime_field_map.get(field)
    if wsq_field is None:
        return df

    rt_df = get_wsq_realtime_batch(codes, wsq_field, trade_date)
    if rt_df.empty:
        return df

    combined_df = pd.concat([df, rt_df], axis=0)
    allow_zero = field in {"volume", "amt"}
    combined_df = sanitize_price_df(combined_df, allow_zero=allow_zero)
    combined_df = combined_df[~combined_df.index.duplicated(keep="last")]
    combined_df = combined_df.loc[:, [c for c in codes if c in combined_df.columns]]
    combined_df = drop_all_nan_rows(combined_df, allow_zero=allow_zero)
    return combined_df


def get_price_df_with_cache(codes, field):
    print(f"{field} 从本地行情数据库读取：{MARKET_DB_PATH}")
    df = load_price_matrix(
        CACHE_PREFIX,
        field,
        codes=codes,
        start_date=start_date,
        end_date=end_date,
        target_columns=codes,
        prefer_sqlite=True,
        fallback_pickle=False,
    )
    allow_zero = field in {"volume", "amt"}
    df = sanitize_price_df(df, allow_zero=allow_zero)
    df = drop_all_nan_rows(df, allow_zero=allow_zero)
    if realtime_quotes_enabled:
        df = supplement_with_wsq(df, codes, field, end_date)
    df = drop_all_nan_rows(df, allow_zero=allow_zero)
    if df.empty:
        raise ValueError(f"{field} 从本地行情数据库读取为空，请先运行 update_local_market_db.py 更新日行情。")
    return df


def fetch_wind_index_close(index_code, query_start_date, query_end_date):
    data = w.wsd(index_code, "close", query_start_date, query_end_date, "PriceAdj=F")
    if data.ErrorCode != 0 or not data.Data:
        print(f"中证800指数行情拉取失败，改用等权基准: ErrorCode={data.ErrorCode}")
        return pd.Series(dtype=float)
    close = pd.Series(data.Data[0], index=pd.to_datetime(data.Times), name=index_code)
    return close.dropna().sort_index()


def fetch_wind_single_price(code, field, query_start_date, query_end_date):
    data = w.wsd(code, field, query_start_date, query_end_date, "PriceAdj=F")
    if data.ErrorCode != 0 or not data.Data or not data.Times:
        raise RuntimeError(
            f"{code} {field} 行情拉取失败: ErrorCode={data.ErrorCode}"
        )
    price = pd.Series(
        pd.to_numeric(pd.Series(data.Data[0]), errors="coerce").to_numpy(),
        index=pd.to_datetime(data.Times),
        name=code,
    )
    return price.replace([np.inf, -np.inf], np.nan).dropna().sort_index()


def build_bottom_reversal_signal(index_close, trade_dates):
    aligned_close = index_close.reindex(trade_dates).ffill()
    index_return = aligned_close.pct_change()
    realized_volatility = (
        index_return.rolling(REVERSAL_VOLATILITY_WINDOW).std() * np.sqrt(252)
    )
    volatility_threshold = (
        realized_volatility.shift(1)
        .rolling(REVERSAL_VOLATILITY_THRESHOLD_WINDOW)
        .quantile(REVERSAL_VOLATILITY_THRESHOLD_QUANTILE)
    )
    volatility_cross_up = (
        realized_volatility.gt(volatility_threshold)
        & realized_volatility.shift(1).le(volatility_threshold.shift(1))
    )
    rolling_high = aligned_close.rolling(REVERSAL_INDEX_POSITION_WINDOW).max()
    rolling_low = aligned_close.rolling(REVERSAL_INDEX_POSITION_WINDOW).min()
    index_range = (rolling_high - rolling_low).replace(0, np.nan)
    index_position = (aligned_close - rolling_low) / index_range
    trailing_return = aligned_close.pct_change(REVERSAL_INDEX_RETURN_WINDOW)
    raw_signal = (
        volatility_cross_up
        & index_position.le(REVERSAL_INDEX_POSITION_MAX)
        & trailing_return.lt(0)
    ).fillna(False)

    deduplicated_signal = pd.Series(False, index=trade_dates, dtype=bool)
    last_signal_loc = None
    for date in trade_dates[raw_signal.to_numpy()]:
        current_loc = trade_dates.get_loc(date)
        if (
            last_signal_loc is None
            or current_loc - last_signal_loc > REVERSAL_SIGNAL_COOLDOWN_DAYS
        ):
            deduplicated_signal.at[date] = True
            last_signal_loc = current_loc

    details = pd.DataFrame({
        "指数收盘点位": aligned_close,
        "20日年化波动率": realized_volatility,
        "动态波动率阈值": volatility_threshold,
        "250日区间位置": index_position,
        "20日指数收益": trailing_return,
        "原始信号": raw_signal,
        "冷却后信号": deduplicated_signal,
    })
    return deduplicated_signal, details


def fetch_average_annual_risk_free_rate(query_start_date, query_end_date):
    data = w.edb(
        RISK_FREE_WIND_INDICATOR,
        query_start_date,
        query_end_date,
        "",
    )
    if data.ErrorCode == 0 and data.Data:
        values = pd.to_numeric(pd.Series(data.Data[0]), errors="coerce")
        # Wind收益率单位为百分比。零值通常表示当前终端没有返回有效EDB数据，
        # 不纳入平均，避免夏普悄然退化为零无风险利率口径。
        valid_values = values[(values > 0) & (values < 20)]
        if not valid_values.empty:
            average_rate = valid_values.mean() / 100
            source = (
                f"Wind {RISK_FREE_NAME}（{RISK_FREE_WIND_INDICATOR}）"
                f"区间算术平均"
            )
            print(f"{source}：{average_rate:.4%}")
            return average_rate, source

    source = f"固定年化利率（Wind {RISK_FREE_WIND_INDICATOR} 未返回有效数据）"
    print(
        f"{RISK_FREE_NAME}拉取无效，夏普改用固定年化无风险利率："
        f"{FALLBACK_ANNUAL_RISK_FREE_RATE:.2%}"
    )
    return FALLBACK_ANNUAL_RISK_FREE_RATE, source


# =========================
# 5. 行情
# =========================
open_df = get_price_df_with_cache(stock_codes, "open")
low_df = get_price_df_with_cache(stock_codes, "low")
high_df = get_price_df_with_cache(stock_codes, "high")
close_df = get_price_df_with_cache(stock_codes, "close")
volume_df = get_price_df_with_cache(stock_codes, "volume")
amt_df = get_price_df_with_cache(stock_codes, "amt")

# 所有价格矩阵必须使用同一交易日索引。实时接口可能暂时只返回最新价、
# 尚未返回开高低；此时回退到六个字段共同具备数据的最近日期，避免把
# 不完整的实时截面混入回测，也避免对缺失日期使用 .at 强索引而报错。
price_frames = {
    "open": open_df,
    "low": low_df,
    "high": high_df,
    "close": close_df,
    "volume": volume_df,
    "amt": amt_df,
}
common_dates = close_df.index
for frame in price_frames.values():
    common_dates = common_dates.intersection(frame.index)
if common_dates.empty:
    raise RuntimeError("开高低收、成交量和成交额不存在共同交易日，无法运行策略。")

latest_common_date = common_dates.max()
if latest_common_date < close_df.index.max():
    print(
        f"最新实时截面字段不完整，策略从 {close_df.index.max().strftime('%Y-%m-%d')} "
        f"回退到共同完整日期 {latest_common_date.strftime('%Y-%m-%d')}。"
    )

close_df = close_df.loc[:latest_common_date]
aligned_index = close_df.index
aligned_columns = close_df.columns
open_df = open_df.reindex(index=aligned_index, columns=aligned_columns)
low_df = low_df.reindex(index=aligned_index, columns=aligned_columns)
high_df = high_df.reindex(index=aligned_index, columns=aligned_columns)
volume_df = volume_df.reindex(index=aligned_index, columns=aligned_columns)
amt_df = amt_df.reindex(index=aligned_index, columns=aligned_columns)

print("行情维度：", close_df.shape)
latest_data_ts = close_df.index[-1]
if latest_data_ts.strftime("%Y-%m-%d") != end_date:
    print(f"实际最新行情日期为 {latest_data_ts.strftime('%Y-%m-%d')}，策略输出日期同步调整。")
end_dt = latest_data_ts.to_pydatetime()
end_date = latest_data_ts.strftime("%Y-%m-%d")

if USE_HISTORICAL_CONSTITUENTS and not historical_constituent_snapshots.empty:
    universe_member = build_daily_universe_member_matrix(
        historical_constituent_snapshots,
        close_df.index,
        close_df.columns,
    )
    # 最新执行日必须以Wind当前成分身份为准，不能把最后一期历史快照
    # 无限向后延用。历史回测日期仍使用实际生效日快照。
    universe_member.loc[latest_data_ts, :] = False
    latest_current_codes = [
        code for code in current_constituent_codes
        if code in universe_member.columns
    ]
    universe_member.loc[latest_data_ts, latest_current_codes] = True
    latest_member_count = int(universe_member.loc[latest_data_ts].sum())
    if latest_member_count != constituent_current_count:
        raise RuntimeError(
            "最新交易日成分掩码与Wind当前成分数量不一致："
            f"mask={latest_member_count}, wind={constituent_current_count}"
        )
    print(f"最新交易日中证800历史成分掩码数量：{latest_member_count}")
else:
    universe_member = pd.DataFrame(True, index=close_df.index, columns=close_df.columns)

# =========================
# 6. 均线
# =========================
ma5 = close_df.rolling(5).mean()
ma60 = close_df.rolling(60).mean()
ma120 = close_df.rolling(120).mean()

# =========================
# 9. 信号
# =========================
spread = ma5 - ma60
signal = np.sign(spread)
daily_ret = close_df.pct_change()
ma60_up = ma60 > ma60.shift(1)
ma120_up = ma120 > ma120.shift(1)
limit_gain = daily_ret <= SIGNAL_MAX_DAILY_RETURN
avg_amount_20d = amt_df.rolling(VOLUME_RATIO_LOOKBACK).mean()
score_details = calculate_golden_cross_score_details(ma5, ma60, volume_df)
score_ma5_ma60_gap = score_details["ma5_ma60_gap_score"]
score_ma60_5d_trend = score_details["ma60_5d_trend_score"]
score_volume_ratio = score_details["volume_ratio"]
score_volume_ratio_score = score_details["volume_ratio_score"]
candidate_score = score_details["total_score"]

cross = signal.diff()

golden_cross_raw = cross == 2
golden_cross_candidate = golden_cross_raw & ma60_up & ma120_up
golden_signal = golden_cross_candidate & limit_gain & universe_member
hard_filter_excluded_signal = golden_cross_raw & ~golden_signal
buy_signal_before_membership_check = golden_signal.shift(1).fillna(False).astype(bool)
membership_blocked_buy_signal = buy_signal_before_membership_check & ~universe_member
buy_signal = buy_signal_before_membership_check & universe_member

# 市场底部反转信号只使用当日及此前数据；信号在收盘后确认，次日开盘执行。
market_index_close = fetch_wind_index_close(WIND_INDEX_CODE, start_date, end_date)
if market_index_close.empty:
    raise RuntimeError("中证800指数行情为空，无法计算市场底部反转信号。")
bottom_reversal_signal, bottom_reversal_details = build_bottom_reversal_signal(
    market_index_close,
    close_df.index,
)
bottom_etf_buy_signal = bottom_reversal_signal.shift(1).fillna(False).astype(bool)
bottom_etf_open = fetch_wind_single_price(
    BOTTOM_ETF_CODE, "open", start_date, end_date,
).reindex(close_df.index)
bottom_etf_close = fetch_wind_single_price(
    BOTTOM_ETF_CODE, "close", start_date, end_date,
).reindex(close_df.index).ffill()

# =========================
# 10. 持仓状态机
# 新开仓固定初始仓位，买入后仓位随涨跌自然漂移
# =========================
position = pd.DataFrame(0.0, index=close_df.index, columns=close_df.columns)
bottom_etf_position = pd.Series(0.0, index=close_df.index, name=BOTTOM_ETF_CODE)
stop_signal = pd.DataFrame(False, index=close_df.index, columns=close_df.columns)
sell_reason = pd.DataFrame("", index=close_df.index, columns=close_df.columns)
sell_trigger_price = pd.DataFrame(np.nan, index=close_df.index, columns=close_df.columns)
sell_float_pnl = pd.DataFrame(np.nan, index=close_df.index, columns=close_df.columns)

current_holdings = {}
bottom_etf_holding = None
bottom_etf_trade_records = []
pending_open_sell_signals = {}
cash = 1.0
portfolio_values = []
turnover_records = []
closed_trade_returns = []
closed_trade_outcomes = []
closed_trade_reasons = []
closed_trade_holding_days = []
latest_trade_date = close_df.index[-1]
latest_intraday_stop_monitor_records = []
latest_low_efficiency_sell_plan_records = []
last_valid_close_date = close_df.apply(lambda series: series.dropna().index.max())
prev_date = None


def execute_bottom_etf_sale(holding, date, gross_sale_value, reason, replacement_code=""):
    open_price = bottom_etf_open.at[date]
    if (
        holding is None
        or pd.isna(open_price)
        or open_price <= 0
        or gross_sale_value <= 0
    ):
        return holding, 0.0, 0.0

    shares_before = holding["shares"]
    available_gross_value = shares_before * open_price
    actual_gross_value = min(gross_sale_value, available_gross_value)
    sold_shares = actual_gross_value / open_price
    allocated_cost_basis = (
        holding["cost_basis_gross"] * sold_shares / shares_before
        if shares_before > 0 else 0.0
    )
    net_proceeds = actual_gross_value * (1 - TRANSACTION_COST_RATE)
    net_buy_cost = allocated_cost_basis * (1 + TRANSACTION_COST_RATE)
    realized_return = net_proceeds / net_buy_cost - 1 if net_buy_cost > 0 else np.nan
    holding_days = close_df.index.get_loc(date) - close_df.index.get_loc(holding["entry_date"])
    bottom_etf_trade_records.append({
        "日期": date,
        "动作": "卖出",
        "原因": reason,
        "ETF代码": BOTTOM_ETF_CODE,
        "ETF名称": BOTTOM_ETF_NAME,
        "关联个股": replacement_code,
        "成交价": open_price,
        "份额": sold_shares,
        "成交金额": actual_gross_value,
        "交易成本": actual_gross_value * TRANSACTION_COST_RATE,
        "净现金流": net_proceeds,
        "对应成本": allocated_cost_basis,
        "本次实现收益率": realized_return,
        "持有交易日": holding_days,
        "原始底部信号日": holding["signal_date"],
    })

    remaining_shares = shares_before - sold_shares
    remaining_cost_basis = max(holding["cost_basis_gross"] - allocated_cost_basis, 0.0)
    if remaining_shares <= 1e-12 or remaining_cost_basis <= 1e-12:
        return None, net_proceeds, actual_gross_value
    holding["shares"] = remaining_shares
    holding["cost_basis_gross"] = remaining_cost_basis
    return holding, net_proceeds, actual_gross_value

for date in close_df.index:
    traded_amount = 0.0
    sold_today = set()

    # 先把老持仓按昨收到今收更新市值
    if prev_date is not None:
        for code in list(current_holdings.keys()):
            prev_price = close_df.at[prev_date, code]
            price = close_df.at[date, code]
            last_price_date = last_valid_close_date.get(code)
            if (
                is_delisted_security(code)
                and pd.isna(price)
                and pd.notna(last_price_date)
                and date > last_price_date
            ):
                holding_info = current_holdings[code]
                entry_price = holding_info["entry_price"]
                entry_date = holding_info["entry_date"]
                holding_days = close_df.index.get_loc(date) - close_df.index.get_loc(entry_date)
                delist_reason = "退市归零"
                closed_trade_returns.append(-1.0)
                closed_trade_outcomes.append("loss")
                closed_trade_reasons.append(delist_reason)
                closed_trade_holding_days.append(holding_days)
                stop_signal.at[date, code] = True
                sell_reason.at[date, code] = delist_reason
                sell_trigger_price.at[date, code] = 0.0
                sell_float_pnl.at[date, code] = -1.0 if entry_price > 0 else np.nan
                sold_today.add(code)
                del current_holdings[code]
                pending_open_sell_signals.pop(code, None)
                continue
            if pd.isna(prev_price) or pd.isna(price) or prev_price <= 0:
                continue
            current_holdings[code]["value"] *= price / prev_price

    # 执行上一交易日收盘后确认的低效持仓卖出信号：今日开盘卖出。
    for code, signal_info in list(pending_open_sell_signals.items()):
        if code not in current_holdings:
            del pending_open_sell_signals[code]
            continue

        open_price = open_df.at[date, code]
        close_price = close_df.at[date, code]
        if pd.isna(open_price) or pd.isna(close_price) or open_price <= 0 or close_price <= 0:
            continue

        holding_info = current_holdings[code]
        sell_price = open_price
        sell_value = holding_info["value"] * (sell_price / close_price)
        buy_cost = holding_info["cost_basis"] * (1 + TRANSACTION_COST_RATE)
        sell_proceeds = sell_value * (1 - TRANSACTION_COST_RATE)
        trade_return = sell_proceeds / buy_cost - 1 if buy_cost > 0 else 0
        holding_days = close_df.index.get_loc(date) - close_df.index.get_loc(holding_info["entry_date"])
        closed_trade_returns.append(trade_return)
        if trade_return > 0:
            trade_outcome = "win"
        else:
            trade_outcome = "loss"
        closed_trade_outcomes.append(trade_outcome)
        closed_trade_reasons.append(signal_info["reason"])
        closed_trade_holding_days.append(holding_days)
        cash += sell_proceeds
        traded_amount += sell_value
        stop_signal.at[date, code] = True
        sell_reason.at[date, code] = signal_info["reason"]
        sell_trigger_price.at[date, code] = sell_price
        sell_float_pnl.at[date, code] = sell_price / holding_info["entry_price"] - 1 if holding_info["entry_price"] > 0 else np.nan
        sold_today.add(code)
        del current_holdings[code]
        del pending_open_sell_signals[code]

    # 底部信号形成的临时ETF仓位，最迟在第20个交易日开盘退出。
    if bottom_etf_holding is not None:
        bottom_etf_holding_days = (
            close_df.index.get_loc(date)
            - close_df.index.get_loc(bottom_etf_holding["entry_date"])
        )
        if bottom_etf_holding_days >= BOTTOM_ETF_MAX_HOLDING_DAYS:
            etf_open_price = bottom_etf_open.at[date]
            if pd.notna(etf_open_price) and etf_open_price > 0:
                expiry_gross_value = bottom_etf_holding["shares"] * etf_open_price
                bottom_etf_holding, etf_net_proceeds, etf_traded_value = (
                    execute_bottom_etf_sale(
                        bottom_etf_holding,
                        date,
                        expiry_gross_value,
                        f"持有满{BOTTOM_ETF_MAX_HOLDING_DAYS}个交易日到期退出",
                    )
                )
                cash += etf_net_proceeds
                traded_amount += etf_traded_value

    # 底部信号在前一交易日收盘确认，今日开盘用全部闲置现金买ETF。
    # 随后若同日有个股金叉买入，再按所需金额卖出ETF完成替换。
    if can_open_new_position := (trade_start_ts is None or date >= trade_start_ts):
        if bottom_etf_buy_signal.at[date] and bottom_etf_holding is None and cash > 0:
            etf_open_price = bottom_etf_open.at[date]
            if pd.notna(etf_open_price) and etf_open_price > 0:
                etf_buy_value = cash / (1 + TRANSACTION_COST_RATE)
                etf_shares = etf_buy_value / etf_open_price
                signal_date = prev_date
                cash -= etf_buy_value * (1 + TRANSACTION_COST_RATE)
                if abs(cash) < 1e-12:
                    cash = 0.0
                traded_amount += etf_buy_value
                bottom_etf_holding = {
                    "shares": etf_shares,
                    "entry_date": date,
                    "entry_price": etf_open_price,
                    "cost_basis_gross": etf_buy_value,
                    "signal_date": signal_date,
                }
                bottom_etf_trade_records.append({
                    "日期": date,
                    "动作": "买入",
                    "原因": "底部反转信号次日开盘填充闲置现金",
                    "ETF代码": BOTTOM_ETF_CODE,
                    "ETF名称": BOTTOM_ETF_NAME,
                    "关联个股": "",
                    "成交价": etf_open_price,
                    "份额": etf_shares,
                    "成交金额": etf_buy_value,
                    "交易成本": etf_buy_value * TRANSACTION_COST_RATE,
                    "净现金流": -etf_buy_value * (1 + TRANSACTION_COST_RATE),
                    "对应成本": etf_buy_value,
                    "本次实现收益率": np.nan,
                    "持有交易日": 0,
                    "原始底部信号日": signal_date,
                })

    # 先用前一日信号在今日开盘买入；已持仓股票重复触发时允许继续加仓。
    bottom_etf_value_before_buy = 0.0
    if bottom_etf_holding is not None:
        etf_close_price = bottom_etf_close.at[date]
        if pd.notna(etf_close_price) and etf_close_price > 0:
            bottom_etf_value_before_buy = bottom_etf_holding["shares"] * etf_close_price
    portfolio_before_buy = (
        cash
        + sum(info["value"] for info in current_holdings.values())
        + bottom_etf_value_before_buy
    )
    available_slots = MAX_HOLDINGS - len(current_holdings)
    if can_open_new_position and (cash > 0 or bottom_etf_holding is not None):
        buy_candidates = buy_signal.columns[buy_signal.loc[date]].tolist()
        buy_candidates = [
            code for code in buy_candidates
            if code not in sold_today and (code in current_holdings or available_slots > 0)
        ]
        if buy_candidates:
            score_prev = candidate_score.loc[prev_date, buy_candidates].dropna().sort_values(ascending=False)
            opened_new_positions = 0
            max_candidates = len([code for code in buy_candidates if code in current_holdings]) + available_slots
            for code in score_prev.head(max_candidates).index:
                is_existing_holding = code in current_holdings
                if not is_existing_holding and opened_new_positions >= available_slots:
                    continue

                open_price = open_df.at[date, code]
                high_price = high_df.at[date, code]
                close_price = close_df.at[date, code]
                prev_close = close_df.at[prev_date, code] if prev_date is not None else np.nan
                if (
                    pd.isna(open_price)
                    or pd.isna(high_price)
                    or pd.isna(close_price)
                    or open_price <= 0
                    or high_price <= 0
                    or close_price <= 0
                ):
                    continue

                if pd.notna(prev_close) and prev_close > 0:
                    limit_ratio = get_limit_up_ratio(code, code_to_name.get(code, code))
                    limit_up_price = prev_close * (1 + limit_ratio)
                    if open_price >= limit_up_price * 0.999:
                        continue

                target_value = portfolio_before_buy * INITIAL_WEIGHT
                required_cash = max(target_value * (1 + TRANSACTION_COST_RATE) - cash, 0.0)
                if required_cash > 0 and bottom_etf_holding is not None:
                    etf_open_price = bottom_etf_open.at[date]
                    if pd.notna(etf_open_price) and etf_open_price > 0:
                        required_etf_gross_sale = required_cash / (1 - TRANSACTION_COST_RATE)
                        bottom_etf_holding, etf_net_proceeds, etf_traded_value = (
                            execute_bottom_etf_sale(
                                bottom_etf_holding,
                                date,
                                required_etf_gross_sale,
                                "金叉买入替换ETF",
                                replacement_code=code,
                            )
                        )
                        cash += etf_net_proceeds
                        traded_amount += etf_traded_value
                max_affordable = cash / (1 + TRANSACTION_COST_RATE)
                buy_value = min(target_value, max_affordable)

                if buy_value <= 0:
                    break

                cash -= buy_value * (1 + TRANSACTION_COST_RATE)
                traded_amount += buy_value
                added_value_at_close = buy_value * (close_price / open_price)

                if is_existing_holding:
                    holding_info = current_holdings[code]
                    old_entry_price = holding_info["entry_price"]
                    old_cost_basis = holding_info["cost_basis"]
                    old_share_proxy = old_cost_basis / old_entry_price if old_entry_price > 0 else 0
                    added_share_proxy = buy_value / open_price
                    new_share_proxy = old_share_proxy + added_share_proxy
                    holding_info["entry_price"] = (
                        (old_cost_basis + buy_value) / new_share_proxy
                        if new_share_proxy > 0 else open_price
                    )
                    holding_info["entry_day_close_above_cost"] = (
                        holding_info["entry_day_close_above_cost"] or close_price >= open_price
                    )
                    holding_info["peak_price"] = max(holding_info["peak_price"], high_price)
                    holding_info["value"] += added_value_at_close
                    holding_info["cost_basis"] += buy_value
                else:
                    opened_new_positions += 1
                    current_holdings[code] = {
                        "entry_price": open_price,
                        "entry_date": date,
                        "entry_day_close_above_cost": close_price >= open_price,
                        "peak_price": high_price,
                        "value": added_value_at_close,
                        "cost_basis": buy_value
                    }

    # 最后检查老持仓是否在今日盘中触发回撤卖出
    industry_holding_counts = get_industry_holding_counts(current_holdings)
    for code in list(current_holdings.keys()):
        open_price = open_df.at[date, code]
        low_price = low_df.at[date, code]
        high_price = high_df.at[date, code]
        close_price = close_df.at[date, code]
        prev_close = close_df.at[prev_date, code] if prev_date is not None else np.nan
        if pd.isna(open_price) or pd.isna(low_price) or pd.isna(high_price) or pd.isna(close_price):
            continue

        holding_info = current_holdings[code]
        entry_price = holding_info["entry_price"]
        entry_date = holding_info["entry_date"]
        industry_name = get_industry_name(code)
        industry_holding_count = industry_holding_counts.get(industry_name, 0)
        applicable_retrace_drawdown, retrace_rule = get_applicable_retrace_drawdown(
            code,
            industry_holding_counts,
        )
        if date == latest_trade_date and date <= entry_date:
            latest_intraday_stop_monitor_records.append({
                "代码": code,
                "名称": code_to_name.get(code, code),
                "行业": industry_name,
                "行业持仓数": industry_holding_count,
                "开仓日期": entry_date,
                "开仓价": entry_price,
                "前高价": holding_info["peak_price"],
                "历史最大浮盈": np.nan,
                "适用回撤比例": applicable_retrace_drawdown,
                "回撤规则": retrace_rule,
                "监控价位": np.nan,
                "今日开盘价": open_price,
                "今日盘中最低": low_price,
                "今日盘中最高": high_price,
                "最新价": close_price,
                "是否触发卖出": "否",
                "触发原因": "当日新开仓，不参与盘中卖出监控",
                "监控卖价": np.nan,
            })
        if date <= entry_date:
            continue

        sell_reason_prefix = "行业集中前高回撤" if retrace_rule.startswith("行业集中") else "前高回撤"
        sell_eval = evaluate_intraday_sell_signal(
            holding_info,
            open_price,
            low_price,
            high_price,
            retrace_drawdown=applicable_retrace_drawdown,
            sell_reason_prefix=sell_reason_prefix,
        )
        prev_peak_price = sell_eval["prev_peak_price"]
        max_profit = sell_eval["max_profit"]
        sell_price = sell_eval["sell_price"]
        is_breakeven_exit = sell_eval["is_breakeven_exit"]
        sell_reason_text = sell_eval["sell_reason_text"]
        monitor_level = sell_eval["monitor_level"]

        limit_down_blocked = False

        if pd.notna(sell_price):
            if pd.notna(prev_close) and prev_close > 0:
                limit_ratio = get_limit_down_ratio(code, code_to_name.get(code, code))
                limit_down_price = prev_close * (1 - limit_ratio)
                if close_price <= limit_down_price * 1.001:
                    limit_down_blocked = True

        if date == latest_trade_date:
            latest_intraday_stop_monitor_records.append({
                "代码": code,
                "名称": code_to_name.get(code, code),
                "行业": industry_name,
                "行业持仓数": industry_holding_count,
                "开仓日期": entry_date,
                "开仓价": entry_price,
                "前高价": prev_peak_price,
                "历史最大浮盈": max_profit,
                "适用回撤比例": applicable_retrace_drawdown,
                "回撤规则": retrace_rule,
                "监控价位": monitor_level,
                "今日开盘价": open_price,
                "今日盘中最低": low_price,
                "今日盘中最高": high_price,
                "最新价": close_price,
                "是否触发卖出": "是" if pd.notna(sell_price) and not limit_down_blocked else "否",
                "触发原因": "跌停附近封死，无法卖出" if limit_down_blocked else (sell_reason_text if pd.notna(sell_price) else ""),
                "监控卖价": sell_price if not limit_down_blocked else np.nan,
            })

        if pd.notna(sell_price) and limit_down_blocked:
            continue

        if pd.notna(sell_price):

            sell_value = holding_info["value"] * (sell_price / close_price)
            buy_cost = holding_info["cost_basis"] * (1 + TRANSACTION_COST_RATE)
            sell_proceeds = sell_value * (1 - TRANSACTION_COST_RATE)
            trade_return = sell_proceeds / buy_cost - 1 if buy_cost > 0 else 0
            holding_days = close_df.index.get_loc(date) - close_df.index.get_loc(entry_date)
            closed_trade_returns.append(trade_return)
            if is_breakeven_exit:
                trade_outcome = "draw"
            elif trade_return > 0:
                trade_outcome = "win"
            else:
                trade_outcome = "loss"
            closed_trade_outcomes.append(trade_outcome)
            closed_trade_reasons.append(sell_reason_text)
            closed_trade_holding_days.append(holding_days)
            cash += sell_proceeds
            traded_amount += sell_value
            stop_signal.at[date, code] = True
            sell_reason.at[date, code] = sell_reason_text
            sell_trigger_price.at[date, code] = sell_price
            sell_float_pnl.at[date, code] = sell_price / entry_price - 1 if entry_price > 0 else np.nan
            sold_today.add(code)
            del current_holdings[code]
            pending_open_sell_signals.pop(code, None)
            continue

        # 当日高点只能在收盘后确认，因此仅用于更新下一交易日可用的峰值。
        updated_peak_price = max(prev_peak_price, high_price)
        current_holdings[code]["peak_price"] = updated_peak_price

        holding_days = close_df.index.get_loc(date) - close_df.index.get_loc(entry_date)
        max_profit_after_update = updated_peak_price / entry_price - 1 if entry_price > 0 else -np.inf
        current_profit = close_price / entry_price - 1 if entry_price > 0 else np.nan
        is_low_efficiency_holding = (
            holding_days > LOW_EFFICIENCY_MIN_HOLDING_DAYS
            and max_profit_after_update <= LOW_EFFICIENCY_MAX_PROFIT_THRESHOLD
            and current_profit <= LOW_EFFICIENCY_CURRENT_PROFIT_THRESHOLD
        )

        if is_low_efficiency_holding:
            low_efficiency_reason = (
                f"低效持仓卖出：持有超过{LOW_EFFICIENCY_MIN_HOLDING_DAYS}个交易日，"
                f"历史最高浮盈未超过{LOW_EFFICIENCY_MAX_PROFIT_THRESHOLD:.0%}且当前浮盈不超过"
                f"{LOW_EFFICIENCY_CURRENT_PROFIT_THRESHOLD:.0%}"
            )
            pending_open_sell_signals[code] = {
                "signal_date": date,
                "reason": low_efficiency_reason,
                "holding_days": holding_days,
                "max_profit": max_profit_after_update,
                "current_profit": current_profit,
            }
            if date == latest_trade_date:
                latest_low_efficiency_sell_plan_records.append({
                    "代码": code,
                    "名称": code_to_name.get(code, code),
                    "开仓日期": entry_date,
                    "开仓价": entry_price,
                    "最新价": close_price,
                    "持有交易日": holding_days,
                    "历史最高浮盈": max_profit_after_update,
                    "当前浮盈": current_profit,
                    "计划动作": "下一交易日开盘卖出",
                    "触发原因": low_efficiency_reason,
                })
        else:
            pending_open_sell_signals.pop(code, None)

    bottom_etf_value = 0.0
    if bottom_etf_holding is not None:
        etf_close_price = bottom_etf_close.at[date]
        if pd.notna(etf_close_price) and etf_close_price > 0:
            bottom_etf_value = bottom_etf_holding["shares"] * etf_close_price
    portfolio_value = (
        cash
        + sum(info["value"] for info in current_holdings.values())
        + bottom_etf_value
    )
    portfolio_values.append(portfolio_value)
    turnover_records.append(traded_amount / portfolio_value if portfolio_value > 0 else 0.0)

    for code, info in current_holdings.items():
        position.at[date, code] = info["value"] / portfolio_value if portfolio_value > 0 else 0.0
    bottom_etf_position.at[date] = (
        bottom_etf_value / portfolio_value if portfolio_value > 0 else 0.0
    )

    prev_date = date

# =========================
# 11. 收益与净值
# =========================
nav = pd.Series(portfolio_values, index=close_df.index, name="净值")
strategy_ret = nav.pct_change().fillna(0)
turnover = pd.Series(turnover_records, index=close_df.index, name="换手率")

if trade_start_ts is not None:
    valid_analysis_dates = nav.index[nav.index >= trade_start_ts]
    if len(valid_analysis_dates) == 0:
        raise ValueError("TRADE_START_DATE 晚于当前获取到的全部行情日期，请检查参数设置。")
    analysis_start_date = valid_analysis_dates[0]
else:
    analysis_start_date = nav.index[0]

nav_analysis = nav.loc[analysis_start_date:]
strategy_ret_analysis = strategy_ret.loc[analysis_start_date:]
turnover_analysis = turnover.loc[analysis_start_date:]
position_analysis = position.loc[analysis_start_date:]

benchmark_name = "中证800指数"
benchmark_close = market_index_close.reindex(nav_analysis.index).ffill()
if benchmark_close.notna().sum() >= 2:
    benchmark_nav = benchmark_close / benchmark_close.dropna().iloc[0]
else:
    benchmark_name = "中证800等权基准"
    benchmark_close_analysis = close_df.loc[analysis_start_date:]
    benchmark_nav = (
        benchmark_close_analysis
        .pct_change()
        .mean(axis=1, skipna=True)
        .fillna(0)
        .add(1)
        .cumprod()
    )
benchmark_nav.name = f"{benchmark_name}净值"
benchmark_ret_analysis = benchmark_nav.pct_change().fillna(0)
nav_output_df = pd.DataFrame({
    "日期": nav_analysis.index.strftime("%Y-%m-%d"),
    "策略净值": nav_analysis,
    "策略每日涨跌幅": strategy_ret_analysis,
    f"{benchmark_name}净值": benchmark_nav,
    f"{benchmark_name}每日涨跌幅": benchmark_ret_analysis,
})
nav_output_df = nav_output_df.sort_values("日期", ascending=False).reset_index(drop=True)
return_curve_df = pd.DataFrame({
    "日期": nav_analysis.index.strftime("%Y-%m-%d"),
    "策略累计收益": nav_analysis / nav_analysis.iloc[0] - 1,
    f"{benchmark_name}累计收益": benchmark_nav / benchmark_nav.iloc[0] - 1,
    "策略净值": nav_analysis,
    f"{benchmark_name}净值": benchmark_nav,
    "策略每日涨跌幅": strategy_ret_analysis,
    f"{benchmark_name}每日涨跌幅": benchmark_ret_analysis,
})
stock_position_series = position_analysis.sum(axis=1)
bottom_etf_position_analysis = bottom_etf_position.loc[analysis_start_date:]
total_position_series = (
    stock_position_series + bottom_etf_position_analysis
).clip(lower=0.0, upper=1.0)
active_position_mask = total_position_series.gt(0)
if active_position_mask.any():
    average_position_start_date = active_position_mask.idxmax()
    average_position = total_position_series.loc[average_position_start_date:].mean()
    average_position_period = (
        f"{average_position_start_date.strftime('%Y-%m-%d')} 至 "
        f"{total_position_series.index[-1].strftime('%Y-%m-%d')}"
    )
else:
    average_position = 0.0
    average_position_period = "回测期内尚未建仓"
position_benchmark_df = pd.DataFrame({
    "日期": total_position_series.index.strftime("%Y-%m-%d"),
    "总仓位": total_position_series,
    "个股仓位": stock_position_series,
    "底部ETF仓位": bottom_etf_position_analysis,
    f"{benchmark_name}净值（起点=1）": benchmark_nav.reindex(total_position_series.index),
})

# =========================
# ⭐ 15. 年化收益（修正版）
# =========================
has_position = total_position_series.gt(0)
if has_position.any():
    first_trade_date = has_position.idxmax()
    nav_active = nav_analysis.loc[first_trade_date:]
    ret_active = strategy_ret_analysis.loc[first_trade_date:]
    position_active = position_analysis.loc[first_trade_date:]
    turnover_active = turnover_analysis.loc[first_trade_date:]

    annual_ret = nav_active.iloc[-1] ** (252 / len(nav_active)) - 1
    annual_vol = ret_active.std() * np.sqrt(252)
    annual_risk_free_rate, risk_free_source = fetch_average_annual_risk_free_rate(
        first_trade_date.strftime("%Y-%m-%d"),
        nav_active.index[-1].strftime("%Y-%m-%d"),
    )
    daily_risk_free_rate = annual_risk_free_rate / 252
    excess_ret_active = ret_active - daily_risk_free_rate
    # 简化夏普：回测区间平均年化国债收益率 / 252，作为固定日无风险收益。
    sharpe = (
        excess_ret_active.mean() / excess_ret_active.std() * np.sqrt(252)
        if excess_ret_active.std() != 0
        else 0
    )
    rolling_max = nav_active.cummax()
    drawdown = nav_active / rolling_max - 1
    max_dd = drawdown.min()
    holding_count = (position_active > 0).sum(axis=1)
    avg_holding = holding_count.mean()
    avg_turnover = turnover_active.mean()
    annual_turnover = avg_turnover * 252
    win_rate = (ret_active > 0).mean()
    trade_count = len(closed_trade_returns)
    if trade_count > 0:
        trade_outcome_series = pd.Series(closed_trade_outcomes)
        trade_win_count = (trade_outcome_series == "win").sum()
        trade_draw_count = (trade_outcome_series == "draw").sum()
        trade_loss_count = (trade_outcome_series == "loss").sum()
        decisive_trade_count = trade_outcome_series.isin(["win", "loss"]).sum()
        trade_win_rate = (
            trade_win_count / decisive_trade_count
            if decisive_trade_count > 0 else 0
        )
    else:
        trade_win_count = 0
        trade_draw_count = 0
        trade_loss_count = 0
        trade_win_rate = 0
else:
    annual_ret = 0
    annual_vol = 0
    annual_risk_free_rate = FALLBACK_ANNUAL_RISK_FREE_RATE
    risk_free_source = "固定年化利率（策略无持仓，未调用Wind）"
    sharpe = 0
    max_dd = 0
    avg_holding = 0
    annual_turnover = 0
    win_rate = 0
    trade_count = 0
    trade_win_count = 0
    trade_draw_count = 0
    trade_loss_count = 0
    trade_win_rate = 0

stats = pd.DataFrame({
    "指标": ["年化收益","年化波动","平均年化无风险利率","无风险利率来源","夏普比率","最大回撤","平均持仓数","年化换手率","日胜率","平仓笔数","单笔盈利数","单笔平局数","单笔亏损数","单笔胜率(平局不计入)"],
    "数值": [annual_ret, annual_vol, annual_risk_free_rate, risk_free_source, sharpe, max_dd, avg_holding, annual_turnover, win_rate, trade_count, trade_win_count, trade_draw_count, trade_loss_count, trade_win_rate]
})
stats["解释"] = stats["指标"].map(STRATEGY_METRIC_EXPLANATIONS).fillna("")

if trade_count > 0:
    close_reason_stats = pd.DataFrame({
        "平仓原因": closed_trade_reasons,
        "单笔收益": closed_trade_returns,
        "持有天数": closed_trade_holding_days,
    })
    close_reason_stats = (
        close_reason_stats
        .groupby("平仓原因", dropna=False)
        .agg(
            平仓笔数=("平仓原因", "size"),
            平均单笔收益=("单笔收益", "mean"),
            平均持有天数=("持有天数", "mean"),
        )
        .reset_index()
    )
    close_reason_stats["平仓占比"] = close_reason_stats["平仓笔数"] / trade_count
    close_reason_stats = close_reason_stats[
        ["平仓原因", "平仓笔数", "平仓占比", "平均单笔收益", "平均持有天数"]
    ].sort_values(by="平仓笔数", ascending=False)
else:
    close_reason_stats = pd.DataFrame(
        columns=["平仓原因", "平仓笔数", "平仓占比", "平均单笔收益", "平均持有天数"]
    )

print("\n【策略指标】")
print(stats)

# =========================
# 17. 持仓（全历史）
# =========================
def extract_position(df, code_map):
    records = []
    for date in df.index:
        row = df.loc[date]
        holdings = row[row > 0]
        for code, weight in holdings.items():
            records.append({
                "日期": date,
                "代码": code,
                "名称": code_map.get(code, code),
                "权重": weight
            })
    return pd.DataFrame(records)

position_df = extract_position(position_analysis, code_to_name)
position_df = position_df.sort_values(by="日期", ascending=False)

# =========================
# 18. 当前持仓
# =========================
today = position.index[-1]
today_loc = close_df.index.get_loc(today)
prev_today = close_df.index[today_loc - 1] if today_loc > 0 else None

holding_records = []
current_industry_holding_counts = get_industry_holding_counts(current_holdings)
for code, info in current_holdings.items():
    latest_price = close_df.at[today, code]
    prev_close_price = close_df.at[prev_today, code] if prev_today is not None else np.nan
    daily_return = (
        latest_price / prev_close_price - 1
        if pd.notna(latest_price) and pd.notna(prev_close_price) and prev_close_price > 0
        else np.nan
    )
    entry_price = info["entry_price"]
    float_pnl = latest_price / entry_price - 1 if entry_price > 0 and pd.notna(latest_price) else np.nan
    max_float_pnl = info["peak_price"] / entry_price - 1 if entry_price > 0 else np.nan
    holding_days = close_df.index.get_loc(today) - close_df.index.get_loc(info["entry_date"])
    industry_name = get_industry_name(code)
    industry_holding_count = current_industry_holding_counts.get(industry_name, 0)
    applicable_retrace_drawdown, retrace_rule = get_applicable_retrace_drawdown(
        code,
        current_industry_holding_counts,
    )
    retrace_trigger_price = (
        info["peak_price"] * (1 - applicable_retrace_drawdown)
        if info["peak_price"] > 0 else np.nan
    )
    holding_records.append({
        "代码": code,
        "名称": code_to_name.get(code, code),
        "行业": industry_name,
        "当前是否中证800成分": "是" if code in current_constituent_codes else "否",
        "行业持仓数": industry_holding_count,
        "开仓日期": info["entry_date"],
        "开仓价": entry_price,
        "最新价": latest_price,
        "个股当日涨跌幅": daily_return,
        "最新市值": info["value"],
        "组合权重": position.at[today, code] if code in position.columns else 0.0,
        "持有交易日": holding_days,
        "历史最高浮盈": max_float_pnl,
        "适用回撤比例": applicable_retrace_drawdown,
        "卖出触发价": retrace_trigger_price,
        "回撤规则": retrace_rule,
        "浮赢浮亏": float_pnl
    })

current_holding_df = pd.DataFrame(holding_records)
if not current_holding_df.empty:
    current_holding_df = current_holding_df.sort_values(by="组合权重", ascending=False)
    current_stock_position = current_holding_df["组合权重"].sum()
else:
    current_stock_position = 0.0
current_used_position = current_stock_position + bottom_etf_position.at[today]

if bottom_etf_holding is not None:
    latest_etf_price = bottom_etf_close.at[today]
    bottom_etf_current_df = pd.DataFrame([{
        "ETF代码": BOTTOM_ETF_CODE,
        "ETF名称": BOTTOM_ETF_NAME,
        "原始底部信号日": bottom_etf_holding["signal_date"],
        "开仓日期": bottom_etf_holding["entry_date"],
        "开仓价": bottom_etf_holding["entry_price"],
        "最新价": latest_etf_price,
        "剩余份额": bottom_etf_holding["shares"],
        "最新市值": bottom_etf_holding["shares"] * latest_etf_price,
        "组合权重": bottom_etf_position.at[today],
        "持有交易日": today_loc - close_df.index.get_loc(bottom_etf_holding["entry_date"]),
        "剩余最长持有交易日": max(
            BOTTOM_ETF_MAX_HOLDING_DAYS
            - (today_loc - close_df.index.get_loc(bottom_etf_holding["entry_date"])),
            0,
        ),
    }])
else:
    bottom_etf_current_df = pd.DataFrame(columns=[
        "ETF代码", "ETF名称", "原始底部信号日", "开仓日期", "开仓价", "最新价",
        "剩余份额", "最新市值", "组合权重", "持有交易日", "剩余最长持有交易日",
    ])

# =========================
# 19. 当日信号
# =========================
golden_trigger_today = golden_signal.loc[today]
golden_exec_today = buy_signal.loc[today]
hard_filter_excluded_today = hard_filter_excluded_signal.loc[today]
membership_blocked_buy_today = membership_blocked_buy_signal.loc[today]
stop_today = stop_signal.loc[today]

golden_trigger_list = golden_trigger_today[golden_trigger_today].index.tolist()
golden_exec_list = golden_exec_today[golden_exec_today].index.tolist()
hard_filter_excluded_list = hard_filter_excluded_today[hard_filter_excluded_today].index.tolist()
membership_blocked_buy_list = membership_blocked_buy_today[
    membership_blocked_buy_today
].index.tolist()
stop_list = stop_today[stop_today].index.tolist()

golden_trigger_df = pd.DataFrame({
    "代码": golden_trigger_list,
    "名称": [code_to_name.get(c, c) for c in golden_trigger_list],
    "评分": [candidate_score.at[today, c] for c in golden_trigger_list],
    "MA5相对MA60强度": [score_ma5_ma60_gap.at[today, c] for c in golden_trigger_list],
    "MA60近5日趋势分": [score_ma60_5d_trend.at[today, c] for c in golden_trigger_list],
    "量比": [score_volume_ratio.at[today, c] for c in golden_trigger_list],
    "量比加分": [score_volume_ratio_score.at[today, c] for c in golden_trigger_list],
    "20日平均成交额": [avg_amount_20d.at[today, c] for c in golden_trigger_list],
    "信号": "最新金叉触发"
})
if not golden_trigger_df.empty:
    golden_trigger_df = golden_trigger_df.sort_values(by="评分", ascending=False, na_position="last")

golden_exec_df = pd.DataFrame({
    "代码": golden_exec_list,
    "名称": [code_to_name.get(c, c) for c in golden_exec_list],
    "评分": [candidate_score.shift(1).at[today, c] for c in golden_exec_list],
    "MA5相对MA60强度": [score_ma5_ma60_gap.shift(1).at[today, c] for c in golden_exec_list],
    "MA60近5日趋势分": [score_ma60_5d_trend.shift(1).at[today, c] for c in golden_exec_list],
    "量比": [score_volume_ratio.shift(1).at[today, c] for c in golden_exec_list],
    "量比加分": [score_volume_ratio_score.shift(1).at[today, c] for c in golden_exec_list],
    "20日平均成交额": [avg_amount_20d.shift(1).at[today, c] for c in golden_exec_list],
    "信号": "昨日金叉今日执行"
})
if not golden_exec_df.empty:
    golden_exec_df = golden_exec_df.sort_values(by="评分", ascending=False, na_position="last")

def build_hard_filter_excluded_record(
    signal_date,
    code,
    prompt_text,
    record_type,
    membership_check_date=None,
):
    membership_check_date = membership_check_date or signal_date
    excluded_reasons = []
    if not bool(universe_member.at[membership_check_date, code]):
        excluded_reasons.append("不在当日中证800成分")
    if not bool(ma60_up.at[signal_date, code]):
        excluded_reasons.append("MA60未向上")
    if not bool(ma120_up.at[signal_date, code]):
        excluded_reasons.append("MA120未向上")
    if not bool(limit_gain.at[signal_date, code]):
        excluded_reasons.append("当日涨幅超过阈值")

    return {
        "记录类型": record_type,
        "信号日期": signal_date,
        "股票池检查日期": membership_check_date,
        "最新交易日": today,
        "代码": code,
        "名称": code_to_name.get(code, code),
        "评分": candidate_score.at[signal_date, code],
        "信号日涨幅": daily_ret.at[signal_date, code],
        "最新交易日涨幅": daily_ret.at[today, code] if code in daily_ret.columns else np.nan,
        "涨幅阈值": SIGNAL_MAX_DAILY_RETURN,
        "超出阈值": daily_ret.at[signal_date, code] - SIGNAL_MAX_DAILY_RETURN,
        "MA60是否向上": "是" if bool(ma60_up.at[signal_date, code]) else "否",
        "MA120是否向上": "是" if bool(ma120_up.at[signal_date, code]) else "否",
        "涨幅是否合格": "是" if bool(limit_gain.at[signal_date, code]) else "否",
        "是否中证800成分": (
            "是" if bool(universe_member.at[membership_check_date, code]) else "否"
        ),
        "剔除原因": "、".join(excluded_reasons),
        "MA5相对MA60强度": score_ma5_ma60_gap.at[signal_date, code],
        "MA60近5日趋势分": score_ma60_5d_trend.at[signal_date, code],
        "量比": score_volume_ratio.at[signal_date, code],
        "量比加分": score_volume_ratio_score.at[signal_date, code],
        "20日平均成交额": avg_amount_20d.at[signal_date, code],
        "提示": prompt_text
    }

hard_filter_excluded_records = []
for code in hard_filter_excluded_list:
    hard_filter_excluded_records.append(
        build_hard_filter_excluded_record(
            today,
            code,
            "今日发生MA5上穿MA60，但未通过硬性过滤条件",
            "今日剔除"
        )
    )

prev_trade_date = close_df.index[-2] if len(close_df.index) >= 2 else None
if prev_trade_date is not None:
    for code in membership_blocked_buy_list:
        hard_filter_excluded_records.append(
            build_hard_filter_excluded_record(
                prev_trade_date,
                code,
                "昨日金叉原计划今日执行，但今日已不在中证800成分，因此取消买入",
                "今日股票池剔除",
                membership_check_date=today,
            )
        )

if prev_trade_date is not None:
    prev_filtered = hard_filter_excluded_signal.loc[prev_trade_date]
    prev_filtered_list = prev_filtered[prev_filtered].index.tolist()
    for code in prev_filtered_list:
        hard_filter_excluded_records.append(
            build_hard_filter_excluded_record(
                prev_trade_date,
                code,
                "昨日发生MA5上穿MA60但被剔除，因此今日不执行买入",
                "昨日剔除"
            )
        )

hard_filter_excluded_df = pd.DataFrame(hard_filter_excluded_records)
if not hard_filter_excluded_df.empty:
    hard_filter_excluded_df = hard_filter_excluded_df.sort_values(
        by=["信号日期", "评分"],
        ascending=[False, False],
        na_position="last"
    )

stop_df = pd.DataFrame({
    "代码": stop_list,
    "名称": [code_to_name.get(c, c) for c in stop_list],
    "触发原因": [sell_reason.at[today, c] for c in stop_list],
    "触发卖价": [sell_trigger_price.at[today, c] for c in stop_list],
    "浮赢浮亏": [sell_float_pnl.at[today, c] for c in stop_list],
    "信号": "最新交易日卖出"
})
latest_intraday_stop_monitor_df = pd.DataFrame(latest_intraday_stop_monitor_records)
if not latest_intraday_stop_monitor_df.empty:
    latest_intraday_stop_monitor_df = latest_intraday_stop_monitor_df.sort_values(
        by=["是否触发卖出", "历史最大浮盈"],
        ascending=[False, False],
        na_position="last"
    )

latest_low_efficiency_sell_plan_df = pd.DataFrame(latest_low_efficiency_sell_plan_records)
if not latest_low_efficiency_sell_plan_df.empty:
    latest_low_efficiency_sell_plan_df = latest_low_efficiency_sell_plan_df.sort_values(
        by=["持有交易日", "当前浮盈"],
        ascending=[False, True],
        na_position="last"
    )

bottom_etf_trade_df = pd.DataFrame(bottom_etf_trade_records)
if not bottom_etf_trade_df.empty:
    bottom_etf_trade_df = bottom_etf_trade_df.sort_values(
        by=["日期", "动作"], ascending=[False, True]
    )

bottom_signal_rows = []
etf_buy_dates_by_signal = {}
for record in bottom_etf_trade_records:
    if record["动作"] == "买入":
        etf_buy_dates_by_signal[pd.Timestamp(record["原始底部信号日"])] = pd.Timestamp(record["日期"])
for signal_date in bottom_reversal_signal.index[bottom_reversal_signal]:
    detail = bottom_reversal_details.loc[signal_date]
    signal_loc = close_df.index.get_loc(signal_date)
    planned_trade_date = (
        close_df.index[signal_loc + 1]
        if signal_loc + 1 < len(close_df.index)
        else pd.NaT
    )
    actual_buy_date = etf_buy_dates_by_signal.get(pd.Timestamp(signal_date), pd.NaT)
    if pd.notna(actual_buy_date):
        execution_status = "已买入ETF"
    elif pd.isna(planned_trade_date):
        execution_status = "等待下一交易日执行"
    else:
        execution_status = "未买入（当日无闲置现金或ETF行情不可用）"
    bottom_signal_rows.append({
        "信号日期": signal_date,
        "计划执行日期": planned_trade_date,
        "实际ETF买入日期": actual_buy_date,
        "执行状态": execution_status,
        "指数收盘点位": detail["指数收盘点位"],
        "20日年化波动率": detail["20日年化波动率"],
        "动态波动率阈值": detail["动态波动率阈值"],
        "250日区间位置": detail["250日区间位置"],
        "20日指数收益": detail["20日指数收益"],
    })
bottom_signal_df = pd.DataFrame(bottom_signal_rows)
if not bottom_signal_df.empty:
    bottom_signal_df = bottom_signal_df.sort_values("信号日期", ascending=False)

# =========================
# 20. 输出
# =========================
output_dir = os.path.join(BASE_DIR, "输出", "金叉执行结果")
os.makedirs(output_dir, exist_ok=True)
output_file = os.path.join(
    output_dir,
    f"金叉买入_前高回撤低效退出_底部ETF动态替换_中证800_{end_date}.xlsx",
)
return_curve_image_file = os.path.join(
    tempfile.gettempdir(),
    f"收益走势图_策略_vs_{benchmark_name}_{end_date}_{os.getpid()}.png",
)
position_benchmark_image_file = os.path.join(
    tempfile.gettempdir(),
    f"仓位_vs_{benchmark_name}_{end_date}_{os.getpid()}.png",
)


def save_return_curve_image(df, image_file):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.ticker as mtick
        import matplotlib.dates as mdates
    except ImportError:
        print("未安装 matplotlib，跳过 PNG 收益走势图。可执行：python3 -m pip install matplotlib")
        return False

    plot_df = df.copy()
    plot_df["日期"] = pd.to_datetime(plot_df["日期"])

    plt.rcParams["font.sans-serif"] = [
        "Arial Unicode MS",
        "PingFang SC",
        "Heiti SC",
        "SimHei",
        "DejaVu Sans",
    ]
    plt.rcParams["axes.unicode_minus"] = False

    fig, ax = plt.subplots(figsize=(13.5, 7.2), dpi=180)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("#fbfbfb")

    ax.plot(
        plot_df["日期"],
        plot_df["策略累计收益"],
        label="策略累计收益",
        color="#2563eb",
        linewidth=2.3,
    )
    ax.plot(
        plot_df["日期"],
        plot_df[f"{benchmark_name}累计收益"],
        label=f"{benchmark_name}累计收益",
        color="#64748b",
        linewidth=2.0,
        linestyle="--",
    )
    ax.axhline(0, color="#111827", linewidth=0.8, alpha=0.55)

    latest_date = plot_df["日期"].iloc[-1].strftime("%Y-%m-%d")
    strategy_latest = plot_df["策略累计收益"].iloc[-1]
    benchmark_latest = plot_df[f"{benchmark_name}累计收益"].iloc[-1]
    excess_latest = strategy_latest - benchmark_latest
    title = (
        f"金叉策略累计收益 vs {benchmark_name}  |  截至 {latest_date}\n"
        f"策略 {strategy_latest:.2%}    基准 {benchmark_latest:.2%}    超额 {excess_latest:.2%}"
    )
    ax.set_title(title, fontsize=15, fontweight="bold", color="#111827", pad=18)
    ax.set_xlabel("日期", fontsize=11, color="#374151", labelpad=10)
    ax.set_ylabel("累计收益率", fontsize=11, color="#374151", labelpad=10)
    ax.yaxis.set_major_formatter(mtick.PercentFormatter(1.0))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.grid(True, axis="y", color="#d1d5db", linewidth=0.8, alpha=0.7)
    ax.grid(True, axis="x", color="#e5e7eb", linewidth=0.5, alpha=0.35)
    ax.legend(
        loc="upper left",
        frameon=True,
        facecolor="white",
        edgecolor="#e5e7eb",
        framealpha=0.95,
        fontsize=10,
    )
    for spine in ["top", "right"]:
        ax.spines[spine].set_visible(False)
    ax.spines["left"].set_color("#9ca3af")
    ax.spines["bottom"].set_color("#9ca3af")
    ax.tick_params(axis="x", labelrotation=35, labelsize=9, colors="#4b5563")
    ax.tick_params(axis="y", labelsize=9, colors="#4b5563")

    fig.tight_layout()
    fig.savefig(image_file, bbox_inches="tight")
    plt.close(fig)
    return True


def add_return_curve_image(writer, sheet_name, image_file):
    sheet = writer.sheets[sheet_name]
    header_row = 35
    first_data_row = header_row + 1

    for cell in sheet[header_row]:
        cell.font = Font(bold=True)

    if os.path.exists(image_file):
        image = OpenpyxlImage(image_file)
        image.width = 1050
        image.height = 560
        sheet.add_image(image, "A1")

    for col_letter in ["B", "C"]:
        for cell in sheet[col_letter][header_row:]:
            cell.number_format = "0.00%"
    for col_letter in ["D", "E"]:
        for cell in sheet[col_letter][header_row:]:
            cell.number_format = "0.0000"
    sheet.freeze_panes = f"A{first_data_row}"
    sheet.column_dimensions["A"].width = 14
    for col_letter in ["B", "C", "D", "E"]:
        sheet.column_dimensions[col_letter].width = 18


def save_position_benchmark_image(df, average_weight, image_file):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.ticker as mtick
        import matplotlib.dates as mdates
    except ImportError:
        print("未安装 matplotlib，跳过 PNG 仓位对标走势图。可执行：python3 -m pip install matplotlib")
        return False

    plot_df = df.copy()
    plot_df["日期"] = pd.to_datetime(plot_df["日期"])
    benchmark_column = f"{benchmark_name}净值（起点=1）"

    plt.rcParams["font.sans-serif"] = [
        "Arial Unicode MS", "PingFang SC", "Heiti SC", "SimHei", "DejaVu Sans",
    ]
    plt.rcParams["axes.unicode_minus"] = False

    fig, position_axis = plt.subplots(figsize=(13.5, 7.2), dpi=180)
    fig.patch.set_facecolor("white")
    position_axis.set_facecolor("#fbfbfb")
    benchmark_axis = position_axis.twinx()

    position_axis.plot(
        plot_df["日期"], plot_df["总仓位"],
        label="策略总仓位", color="#2563eb", linewidth=2.1,
    )
    position_axis.fill_between(
        plot_df["日期"], 0, plot_df["总仓位"],
        color="#93c5fd", alpha=0.22,
    )
    position_axis.axhline(
        average_weight, label=f"平均仓位 {average_weight:.2%}",
        color="#16a34a", linewidth=1.5, linestyle=":",
    )
    benchmark_axis.plot(
        plot_df["日期"], plot_df[benchmark_column],
        label=benchmark_column, color="#64748b", linewidth=2.0, linestyle="--",
    )

    latest_date = plot_df["日期"].iloc[-1].strftime("%Y-%m-%d")
    position_axis.set_title(
        f"策略仓位与{benchmark_name}走势  |  截至 {latest_date}\n"
        f"平均仓位（首笔建仓起） {average_weight:.2%}",
        fontsize=15, fontweight="bold", color="#111827", pad=18,
    )
    position_axis.set_xlabel("日期", fontsize=11, color="#374151", labelpad=10)
    position_axis.set_ylabel("策略总仓位", fontsize=11, color="#2563eb", labelpad=10)
    benchmark_axis.set_ylabel(
        f"{benchmark_name}净值（起点=1）", fontsize=11, color="#64748b", labelpad=10,
    )
    position_axis.set_ylim(0, 1.05)
    position_axis.yaxis.set_major_formatter(mtick.PercentFormatter(1.0))
    benchmark_axis.yaxis.set_major_formatter(mtick.FormatStrFormatter("%.2f"))
    position_axis.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    position_axis.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    position_axis.grid(True, axis="y", color="#d1d5db", linewidth=0.8, alpha=0.7)
    position_axis.grid(True, axis="x", color="#e5e7eb", linewidth=0.5, alpha=0.35)

    lines = position_axis.get_lines() + benchmark_axis.get_lines()
    labels = [line.get_label() for line in lines]
    position_axis.legend(
        lines, labels, loc="upper left", frameon=True, facecolor="white",
        edgecolor="#e5e7eb", framealpha=0.95, fontsize=10,
    )
    position_axis.spines["top"].set_visible(False)
    benchmark_axis.spines["top"].set_visible(False)
    position_axis.spines["left"].set_color("#9ca3af")
    position_axis.spines["bottom"].set_color("#9ca3af")
    benchmark_axis.spines["right"].set_color("#9ca3af")
    position_axis.tick_params(axis="x", labelrotation=35, labelsize=9, colors="#4b5563")
    position_axis.tick_params(axis="y", labelsize=9, colors="#2563eb")
    benchmark_axis.tick_params(axis="y", labelsize=9, colors="#64748b")

    fig.tight_layout()
    fig.savefig(image_file, bbox_inches="tight")
    plt.close(fig)
    return True


def add_position_benchmark_image(writer, sheet_name, image_file):
    sheet = writer.sheets[sheet_name]
    header_row = 35
    first_data_row = header_row + 1
    sheet.cell(row=1, column=1, value="平均仓位（首笔建仓至最新）")
    sheet.cell(row=1, column=2, value=average_position)
    sheet.cell(row=1, column=2).number_format = "0.00%"
    sheet.cell(row=1, column=3, value="平均仓位计算区间")
    sheet.cell(row=1, column=4, value=average_position_period)
    for cell in [sheet.cell(row=1, column=1), sheet.cell(row=1, column=3)]:
        cell.font = Font(bold=True)
    for cell in sheet[header_row]:
        cell.font = Font(bold=True)
    if os.path.exists(image_file):
        image = OpenpyxlImage(image_file)
        image.width = 1050
        image.height = 560
        sheet.add_image(image, "A3")
    for col_letter in ["B", "C", "D"]:
        for cell in sheet[col_letter][header_row:]:
            cell.number_format = "0.00%"
    for cell in sheet["E"][header_row:]:
        cell.number_format = "0.0000"
    sheet.freeze_panes = f"A{first_data_row}"
    sheet.column_dimensions["A"].width = 14
    sheet.column_dimensions["B"].width = 18
    sheet.column_dimensions["C"].width = 18
    sheet.column_dimensions["D"].width = 18
    sheet.column_dimensions["E"].width = 26


save_return_curve_image(return_curve_df, return_curve_image_file)
save_position_benchmark_image(
    position_benchmark_df, average_position, position_benchmark_image_file,
)
run_parameters_df = build_run_parameters(end_date)

with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
    run_parameters_df.to_excel(writer, sheet_name="运行参数", index=False)
    run_parameters_sheet = writer.sheets["运行参数"]
    run_parameters_sheet.column_dimensions["A"].width = 24
    run_parameters_sheet.column_dimensions["B"].width = 28
    run_parameters_sheet.column_dimensions["C"].width = 88
    nav_output_df.to_excel(writer, sheet_name="净值", index=False)
    nav_sheet = writer.sheets["净值"]
    nav_sheet.freeze_panes = "A2"
    for col_letter in ["B", "D"]:
        nav_sheet.column_dimensions[col_letter].width = 16
    for col_letter in ["C", "E"]:
        nav_sheet.column_dimensions[col_letter].width = 18
        for cell in nav_sheet[col_letter][1:]:
            cell.number_format = "0.00%"
    return_curve_df.to_excel(writer, sheet_name="收益走势图", index=False, startrow=34)
    add_return_curve_image(writer, "收益走势图", return_curve_image_file)
    position_benchmark_df.to_excel(
        writer, sheet_name="仓位与中证800", index=False, startrow=34,
    )
    add_position_benchmark_image(
        writer, "仓位与中证800", position_benchmark_image_file,
    )
    stats.to_excel(writer, sheet_name="策略指标", index=False)
    metrics_sheet = writer.sheets["策略指标"]
    metrics_sheet.column_dimensions["A"].width = 24
    metrics_sheet.column_dimensions["B"].width = 16
    metrics_sheet.column_dimensions["C"].width = 88
    metrics_sheet.cell(row=len(stats) + 3, column=1, value="平仓原因统计")
    close_reason_stats.to_excel(
        writer,
        sheet_name="策略指标",
        startrow=len(stats) + 3,
        index=False,
    )
    position_df.to_excel(writer, sheet_name="每日持仓", index=False)
    bottom_signal_df.to_excel(writer, sheet_name="市场底部信号", index=False)
    bottom_etf_trade_df.to_excel(writer, sheet_name="底部ETF交易", index=False)
    bottom_etf_current_df.to_excel(writer, sheet_name="当前底部ETF", index=False)
    for sheet_name in ["市场底部信号", "底部ETF交易", "当前底部ETF"]:
        audit_sheet = writer.sheets[sheet_name]
        audit_sheet.freeze_panes = "A2"
        audit_sheet.auto_filter.ref = audit_sheet.dimensions
        for cell in audit_sheet[1]:
            cell.font = Font(bold=True)
        for column_cells in audit_sheet.columns:
            max_length = max(
                len(str(cell.value)) if cell.value is not None else 0
                for cell in column_cells
            )
            audit_sheet.column_dimensions[column_cells[0].column_letter].width = min(
                max(max_length + 2, 12), 34
            )
    current_holding_df.to_excel(writer, sheet_name="当前持仓", startrow=2, index=False)
    current_holding_sheet = writer.sheets["当前持仓"]
    current_holding_sheet.cell(row=1, column=1, value="当前已使用仓位")
    current_holding_sheet.cell(row=1, column=2, value=current_used_position)
    for col_idx in range(1, current_holding_sheet.max_column + 1):
        if current_holding_sheet.cell(row=3, column=col_idx).value == "个股当日涨跌幅":
            current_holding_sheet.column_dimensions[
                current_holding_sheet.cell(row=3, column=col_idx).column_letter
            ].width = 16
            for cell in current_holding_sheet.iter_cols(
                min_col=col_idx,
                max_col=col_idx,
                min_row=4,
                max_row=current_holding_sheet.max_row,
            ):
                for data_cell in cell:
                    data_cell.number_format = "0.00%"
            break
    golden_trigger_df.to_excel(writer, sheet_name="最新金叉触发", index=False)
    golden_exec_df.to_excel(writer, sheet_name="今日执行买入", index=False)
    hard_filter_excluded_df.to_excel(writer, sheet_name="硬性条件剔除", index=False)
    stop_df.to_excel(writer, sheet_name="最新交易日卖出", index=False)
    latest_intraday_stop_monitor_df.to_excel(writer, sheet_name="最新盘中卖出监控", index=False)
    latest_low_efficiency_sell_plan_df.to_excel(writer, sheet_name="明日低效持仓卖出", index=False)
    for sheet in writer.sheets.values():
        sheet.sheet_view.zoomScale = 240
        sheet.sheet_view.zoomScaleNormal = 240

if os.path.exists(return_curve_image_file):
    os.remove(return_curve_image_file)
if os.path.exists(position_benchmark_image_file):
    os.remove(position_benchmark_image_file)

print("\n【最新信号】")
print(f"最新金叉触发数量: {len(golden_trigger_list)}")
print(f"昨日金叉今日执行数量: {len(golden_exec_list)}")
print(f"硬性条件剔除提示数量: {len(hard_filter_excluded_df)}")
print(f"最新交易日卖出数量: {len(stop_list)}")
print(f"最新盘中卖出监控数量: {len(latest_intraday_stop_monitor_df)}")
print(f"明日低效持仓开盘卖出数量: {len(latest_low_efficiency_sell_plan_df)}")
print(f"当前持仓数量: {len(current_holding_df)}")

print("输出完成：", output_file)

# =========================
# 21. 关闭 Wind
# =========================
w.close()
