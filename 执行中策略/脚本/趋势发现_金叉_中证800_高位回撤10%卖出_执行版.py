from WindPy import w
import os
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from 维护工具.local_market_db import MARKET_DB_PATH, ensure_market_data_updated, load_price_matrix

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
CACHE_DIR = os.path.join(BASE_DIR, "缓存")
os.makedirs(CACHE_DIR, exist_ok=True)

# =========================
# 0. 参数
# =========================
MAX_HOLDINGS = 20
INITIAL_WEIGHT = 1 / MAX_HOLDINGS
TRANSACTION_COST_RATE = 0.0025
STOP_DRAWDOWN = 0.2
PEAK_RETRACE_SELL_DRAWDOWN = 0.1
LOW_EFFICIENCY_MIN_HOLDING_DAYS = 100
LOW_EFFICIENCY_MAX_PROFIT_THRESHOLD = 0.03
LOW_EFFICIENCY_CURRENT_PROFIT_THRESHOLD = 0.00

# 评分系统
# total_score =
#   MA5_MA60_GAP_WEIGHT * (MA5 / MA60 - 1)
#   + MA60_5D_TREND_WEIGHT * (MA60 / MA60.shift(5) - 1)
#   + VOLUME_RATIO_SCORE_WEIGHT * clip(成交量 / 20日均量 - 1, 0, VOLUME_RATIO_MAX_BONUS_BASE)
MA5_MA60_GAP_WEIGHT = 0.8
MA60_5D_TREND_WEIGHT = 0.8
VOLUME_RATIO_SCORE_WEIGHT = 0.5
VOLUME_RATIO_LOOKBACK = 20
VOLUME_RATIO_MAX_BONUS_BASE = 1.0

# 金叉触发日最大涨幅过滤。
# 已用本地中证800缓存行情做网格回测：3%~15%中，5%在收益、回撤、夏普之间最优；
# 简单放宽到5.5%~7%会增加追高交易，组合表现反而下降。
SIGNAL_MAX_DAILY_RETURN = 0.05

BREAKEVEN_PROFIT_THRESHOLD = 0.05
PROFIT_TIER_1_THRESHOLD = 0.10
PROFIT_TIER_2_THRESHOLD = 0.30
PROFIT_TIER_3_THRESHOLD = 0.60
PROFIT_TIER_4_THRESHOLD = 1.00
PROFIT_TIER_1_REMAIN = 0.60
PROFIT_TIER_2_REMAIN = 0.65
PROFIT_TIER_3_REMAIN = 0.70
PROFIT_TIER_4_REMAIN = 0.75
PROFIT_TIER_5_REMAIN = 0.80
LOOKBACK_DAYS = 1600
TRADE_START_DATE = "2023-04-03"  # 必须写成字符串，例如 "2025-01-01"；None 表示沿用当前逻辑
CACHE_PREFIX = "中证800"

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

print("中证800股票数量：", len(stock_codes))


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


def evaluate_intraday_sell_signal(holding_info, open_price, low_price, high_price):
    entry_price = holding_info["entry_price"]
    prev_peak_price = holding_info["peak_price"]
    max_profit = prev_peak_price / entry_price - 1 if entry_price > 0 else -np.inf
    sell_price = np.nan
    is_breakeven_exit = False
    sell_reason_text = ""
    monitor_level = np.nan

    stop_loss_price = entry_price * (1 - STOP_DRAWDOWN)
    if low_price <= stop_loss_price:
        sell_price = open_price if open_price < stop_loss_price else stop_loss_price
        sell_reason_text = "固定止损触发"
        monitor_level = stop_loss_price
    elif prev_peak_price > 0:
        retrace_price = prev_peak_price * (1 - PEAK_RETRACE_SELL_DRAWDOWN)
        monitor_level = retrace_price
        if low_price <= retrace_price:
            sell_price = open_price if open_price < retrace_price else retrace_price
            sell_reason_text = "前高回撤10%卖出"

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


end_dt = datetime.today()
end_date = end_dt.strftime("%Y-%m-%d")

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

end_date = ensure_market_data_updated(w, end_date)
end_dt = pd.Timestamp(end_date).to_pydatetime()

# =========================
# 4. 行情获取
# =========================
def get_wsd_batch(codes, field, query_start_date, query_end_date, batch_size=500):
    all_df = []
    effective_batch_size = batch_size

    for i in range(0, len(codes), effective_batch_size):
        batch = codes[i:i + effective_batch_size]
        print(f"拉取 {field}: {i}-{i + len(batch)} [{query_start_date} ~ {query_end_date}]")

        data = w.wsd(batch, field, query_start_date, query_end_date, "PriceAdj=F")

        if data.ErrorCode != 0 or len(data.Times) == 0:
            print("失败批次：", batch[:3])
            continue

        if len(data.Data) == len(data.Codes):
            df = pd.DataFrame(data.Data, index=data.Codes).T
            df.index = data.Times
        elif len(data.Data) == len(data.Times):
            df = pd.DataFrame(data.Data, index=data.Times, columns=data.Codes)
        elif len(data.Times) == 1 and len(data.Data) == 1:
            df = pd.DataFrame([data.Data[0]], index=data.Times, columns=data.Codes)
        else:
            print(
                f"返回维度异常：field={field}, codes={len(data.Codes)}, "
                f"times={len(data.Times)}, data_rows={len(data.Data)}"
            )
            print("失败批次：", batch[:3])
            continue

        df = df.apply(pd.to_numeric, errors="coerce")
        df.index = pd.to_datetime(df.index)
        all_df.append(df)

    if not all_df:
        return pd.DataFrame()

    df = pd.concat(all_df, axis=1)
    df = df.sort_index()
    df = df.loc[:, ~df.columns.duplicated()]

    return df


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
    return os.path.join(CACHE_DIR, f"{CACHE_PREFIX}_{field}_PriceAdjF.pkl")


def sanitize_price_df(df):
    if df.empty:
        return df

    df = df.apply(pd.to_numeric, errors="coerce")
    # 价格字段中 0 和负值都视为无效，避免 pct_change / 比值计算报错。
    df = df.where(df > 0, np.nan)
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

    return sanitize_price_df(df)


def save_cached_df(field, df):
    cache_path = get_cache_path(field)
    df = sanitize_price_df(df)
    df.to_pickle(cache_path)


def has_recent_all_nan_rows(df, recent_days=3):
    if df.empty:
        return False
    recent_df = df.tail(recent_days)
    return (recent_df.notna().sum(axis=1) == 0).any()


def drop_all_nan_rows(df):
    if df.empty:
        return df
    return sanitize_price_df(df.loc[df.notna().sum(axis=1) > 0])


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
    combined_df = sanitize_price_df(combined_df)
    combined_df = combined_df[~combined_df.index.duplicated(keep="last")]
    combined_df = combined_df.loc[:, [c for c in codes if c in combined_df.columns]]
    combined_df = drop_all_nan_rows(combined_df)
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
    df = sanitize_price_df(df)
    df = drop_all_nan_rows(df)
    df = supplement_with_wsq(df, codes, field, end_date)
    df = drop_all_nan_rows(df)
    if df.empty:
        raise ValueError(f"{field} 从本地行情数据库读取为空，请先运行 update_local_market_db.py 更新日行情。")
    return df

# =========================
# 5. 行情
# =========================
open_df = get_price_df_with_cache(stock_codes, "open")
low_df = get_price_df_with_cache(stock_codes, "low")
high_df = get_price_df_with_cache(stock_codes, "high")
close_df = get_price_df_with_cache(stock_codes, "close")
volume_df = get_price_df_with_cache(stock_codes, "volume")
amt_df = get_price_df_with_cache(stock_codes, "amt")

print("行情维度：", close_df.shape)
latest_data_ts = close_df.index[-1]
if latest_data_ts.strftime("%Y-%m-%d") != end_date:
    print(f"实际最新行情日期为 {latest_data_ts.strftime('%Y-%m-%d')}，策略输出日期同步调整。")
end_dt = latest_data_ts.to_pydatetime()
end_date = latest_data_ts.strftime("%Y-%m-%d")

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
golden_signal = golden_cross_candidate & limit_gain
hard_filter_excluded_signal = golden_cross_raw & ~golden_signal
buy_signal = golden_signal.shift(1).fillna(False).astype(bool)

# =========================
# 10. 持仓状态机
# 新开仓固定初始仓位，买入后仓位随涨跌自然漂移
# =========================
position = pd.DataFrame(0.0, index=close_df.index, columns=close_df.columns)
stop_signal = pd.DataFrame(False, index=close_df.index, columns=close_df.columns)
sell_reason = pd.DataFrame("", index=close_df.index, columns=close_df.columns)
sell_trigger_price = pd.DataFrame(np.nan, index=close_df.index, columns=close_df.columns)
sell_float_pnl = pd.DataFrame(np.nan, index=close_df.index, columns=close_df.columns)

current_holdings = {}
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
prev_date = None

for date in close_df.index:
    traded_amount = 0.0
    sold_today = set()

    # 先把老持仓按昨收到今收更新市值
    if prev_date is not None:
        for code in list(current_holdings.keys()):
            prev_price = close_df.at[prev_date, code]
            price = close_df.at[date, code]
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

    # 先用前一日信号在今日开盘买入
    portfolio_before_buy = cash + sum(info["value"] for info in current_holdings.values())
    available_slots = MAX_HOLDINGS - len(current_holdings)
    can_open_new_position = trade_start_ts is None or date >= trade_start_ts
    if can_open_new_position and available_slots > 0 and cash > 0:
        buy_candidates = buy_signal.columns[buy_signal.loc[date]].tolist()
        buy_candidates = [
            code for code in buy_candidates
            if code not in current_holdings and code not in sold_today
        ]

        if buy_candidates:
            score_prev = candidate_score.loc[prev_date, buy_candidates].dropna().sort_values(ascending=False)
            for code in score_prev.head(available_slots).index:
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
                max_affordable = cash / (1 + TRANSACTION_COST_RATE)
                buy_value = min(target_value, max_affordable)

                if buy_value <= 0:
                    break

                cash -= buy_value * (1 + TRANSACTION_COST_RATE)
                traded_amount += buy_value
                current_holdings[code] = {
                    "entry_price": open_price,
                    "entry_date": date,
                    "entry_day_close_above_cost": close_price >= open_price,
                    "peak_price": high_price,
                    "value": buy_value * (close_price / open_price),
                    "cost_basis": buy_value
                }

    # 最后检查老持仓是否在今日盘中触发回撤卖出
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
        if date == latest_trade_date and date <= entry_date:
            latest_intraday_stop_monitor_records.append({
                "代码": code,
                "名称": code_to_name.get(code, code),
                "开仓日期": entry_date,
                "开仓价": entry_price,
                "前高价": holding_info["peak_price"],
                "历史最大浮盈": np.nan,
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

        sell_eval = evaluate_intraday_sell_signal(holding_info, open_price, low_price, high_price)
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
                "开仓日期": entry_date,
                "开仓价": entry_price,
                "前高价": prev_peak_price,
                "历史最大浮盈": max_profit,
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
            and (
                max_profit_after_update <= LOW_EFFICIENCY_MAX_PROFIT_THRESHOLD
                or current_profit <= LOW_EFFICIENCY_CURRENT_PROFIT_THRESHOLD
            )
        )

        if is_low_efficiency_holding:
            low_efficiency_reason = (
                f"低效持仓卖出：持有超过{LOW_EFFICIENCY_MIN_HOLDING_DAYS}个交易日，"
                f"历史最高浮盈未超过{LOW_EFFICIENCY_MAX_PROFIT_THRESHOLD:.0%}或当前浮盈不超过"
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

    portfolio_value = cash + sum(info["value"] for info in current_holdings.values())
    portfolio_values.append(portfolio_value)
    turnover_records.append(traded_amount / portfolio_value if portfolio_value > 0 else 0.0)

    for code, info in current_holdings.items():
        position.at[date, code] = info["value"] / portfolio_value if portfolio_value > 0 else 0.0

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

# =========================
# ⭐ 15. 年化收益（修正版）
# =========================
has_position = position_analysis.sum(axis=1).gt(0)
if has_position.any():
    first_trade_date = has_position.idxmax()
    nav_active = nav_analysis.loc[first_trade_date:]
    ret_active = strategy_ret_analysis.loc[first_trade_date:]
    position_active = position_analysis.loc[first_trade_date:]
    turnover_active = turnover_analysis.loc[first_trade_date:]

    annual_ret = nav_active.iloc[-1] ** (252 / len(nav_active)) - 1
    annual_vol = ret_active.std() * np.sqrt(252)
    sharpe = annual_ret / annual_vol if annual_vol != 0 else 0
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
    "指标": ["年化收益","年化波动","夏普比率","最大回撤","平均持仓数","年化换手率","日胜率","平仓笔数","单笔盈利数","单笔平局数","单笔亏损数","单笔胜率(平局不计入)"],
    "数值": [annual_ret, annual_vol, sharpe, max_dd, avg_holding, annual_turnover, win_rate, trade_count, trade_win_count, trade_draw_count, trade_loss_count, trade_win_rate]
})

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

holding_records = []
for code, info in current_holdings.items():
    latest_price = close_df.at[today, code]
    entry_price = info["entry_price"]
    float_pnl = latest_price / entry_price - 1 if entry_price > 0 and pd.notna(latest_price) else np.nan
    max_float_pnl = info["peak_price"] / entry_price - 1 if entry_price > 0 else np.nan
    holding_days = close_df.index.get_loc(today) - close_df.index.get_loc(info["entry_date"])
    holding_records.append({
        "代码": code,
        "名称": code_to_name.get(code, code),
        "开仓日期": info["entry_date"],
        "开仓价": entry_price,
        "最新价": latest_price,
        "最新市值": info["value"],
        "组合权重": position.at[today, code] if code in position.columns else 0.0,
        "持有交易日": holding_days,
        "历史最高浮盈": max_float_pnl,
        "浮赢浮亏": float_pnl
    })

current_holding_df = pd.DataFrame(holding_records)
if not current_holding_df.empty:
    current_holding_df = current_holding_df.sort_values(by="组合权重", ascending=False)

# =========================
# 19. 当日信号
# =========================
golden_trigger_today = golden_signal.loc[today]
golden_exec_today = buy_signal.loc[today]
hard_filter_excluded_today = hard_filter_excluded_signal.loc[today]
stop_today = stop_signal.loc[today]

golden_trigger_list = golden_trigger_today[golden_trigger_today].index.tolist()
golden_exec_list = golden_exec_today[golden_exec_today].index.tolist()
hard_filter_excluded_list = hard_filter_excluded_today[hard_filter_excluded_today].index.tolist()
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

def build_hard_filter_excluded_record(signal_date, code, prompt_text):
    excluded_reasons = []
    if not bool(ma60_up.at[signal_date, code]):
        excluded_reasons.append("MA60未向上")
    if not bool(ma120_up.at[signal_date, code]):
        excluded_reasons.append("MA120未向上")
    if not bool(limit_gain.at[signal_date, code]):
        excluded_reasons.append("当日涨幅超过阈值")

    return {
        "日期": signal_date,
        "代码": code,
        "名称": code_to_name.get(code, code),
        "评分": candidate_score.at[signal_date, code],
        "当日涨幅": daily_ret.at[signal_date, code],
        "涨幅阈值": SIGNAL_MAX_DAILY_RETURN,
        "超出阈值": daily_ret.at[signal_date, code] - SIGNAL_MAX_DAILY_RETURN,
        "MA60是否向上": "是" if bool(ma60_up.at[signal_date, code]) else "否",
        "MA120是否向上": "是" if bool(ma120_up.at[signal_date, code]) else "否",
        "涨幅是否合格": "是" if bool(limit_gain.at[signal_date, code]) else "否",
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
            "今日发生MA5上穿MA60，但未通过硬性过滤条件"
        )
    )

prev_trade_date = close_df.index[-2] if len(close_df.index) >= 2 else None
if prev_trade_date is not None:
    prev_filtered = hard_filter_excluded_signal.loc[prev_trade_date]
    prev_filtered_list = prev_filtered[prev_filtered].index.tolist()
    for code in prev_filtered_list:
        hard_filter_excluded_records.append(
            build_hard_filter_excluded_record(
                prev_trade_date,
                code,
                "昨日发生MA5上穿MA60但被剔除，因此今日不执行买入"
            )
        )

hard_filter_excluded_df = pd.DataFrame(hard_filter_excluded_records)
if not hard_filter_excluded_df.empty:
    hard_filter_excluded_df = hard_filter_excluded_df.sort_values(
        by=["日期", "评分"],
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

# =========================
# 20. 输出
# =========================
output_dir = os.path.join(BASE_DIR, "输出")
os.makedirs(output_dir, exist_ok=True)
output_file = os.path.join(output_dir, f"金叉买入_前高回撤10%卖出策略_中证800_{end_date}.xlsx")

with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
    nav_analysis.to_frame("净值").to_excel(writer, sheet_name="净值")
    stats.to_excel(writer, sheet_name="策略指标", index=False)
    close_reason_stats.to_excel(writer, sheet_name="平仓原因统计", index=False)
    position_df.to_excel(writer, sheet_name="每日持仓", index=False)
    current_holding_df.to_excel(writer, sheet_name="当前持仓", index=False)
    golden_trigger_df.to_excel(writer, sheet_name="最新金叉触发", index=False)
    golden_exec_df.to_excel(writer, sheet_name="今日执行买入", index=False)
    hard_filter_excluded_df.to_excel(writer, sheet_name="硬性条件剔除", index=False)
    stop_df.to_excel(writer, sheet_name="最新交易日卖出", index=False)
    latest_intraday_stop_monitor_df.to_excel(writer, sheet_name="最新盘中卖出监控", index=False)
    latest_low_efficiency_sell_plan_df.to_excel(writer, sheet_name="明日低效持仓卖出", index=False)

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
