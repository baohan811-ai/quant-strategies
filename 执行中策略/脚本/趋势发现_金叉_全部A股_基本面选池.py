from WindPy import w
import os
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
CACHE_DIR = os.path.join(BASE_DIR, "缓存")
os.makedirs(CACHE_DIR, exist_ok=True)

# =========================
# 0. 参数
# =========================
MAX_HOLDINGS = 15
INITIAL_WEIGHT = 1 / MAX_HOLDINGS
MAX_DAILY_BUYS = 5
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
SIGNAL_MAX_DAILY_RETURN = 0.05

# 基本面观察池：先按基本面筛股票池，再在池内用技术面交易。
USE_FUNDAMENTAL_POOL = True
FUNDAMENTAL_POOL_TOP_N = 200
FUNDAMENTAL_POOL_MIN_SCORE = 0.45
FUNDAMENTAL_POOL_UPDATE_FREQ = "M"  # "M"=每月首个交易日更新；"Q"=每季度首个交易日更新；"D"=每日更新
VALUATION_SCORE_WEIGHT = 0.34
GROWTH_SCORE_WEIGHT = 0.18
REVISION_SCORE_WEIGHT = 0.28
QUALITY_SCORE_WEIGHT = 0.20
RISK_PENALTY_WEIGHT = 0.60
REVISION_LOOKBACK_DAYS = 60
MISSING_ESTIMATE_NEUTRAL_SCORE = 0.40
MISSING_ESTIMATE_RISK_PENALTY = 0.10

FUNDAMENTAL_FIELDS = {
    "pe_ttm": "pe_ttm",
    "pb_lf": "pb_lf",
    "roe_ttm": "roe_ttm2",
    "debt_to_assets": "debttoassets",
    "est_netprofit_fy1": "west_netprofit_FY1",
    "est_netprofit_yoy": "west_netprofit_YOY",
}

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
CACHE_PREFIX = "全部A股_高位回撤10"
SECTOR_CACHE_REFRESH_DAYS = 7
RETRY_ALL_NAN_FUNDAMENTAL_CACHE = True
FUNDAMENTAL_BATCH_SIZE = 1000
SAVE_EMPTY_FUNDAMENTAL_CACHE_ON_FAILURE = False
FUNDAMENTAL_FETCH_MODE = "POOL_UPDATE_DATES"  # "POOL_UPDATE_DATES"=仅按观察池更新日拉截面；"DAILY"=按交易日拉
FUNDAMENTAL_START_DATE = "2021-01-01"
GROUPED_FUNDAMENTAL_FIELD_KEYS = []

# =========================
# 1. 启动 Wind
# =========================
w.start()


def get_sector_cache_path(sector_id):
    return os.path.join(CACHE_DIR, f"{CACHE_PREFIX}_sector_{sector_id}.pkl")


def load_cached_sector_constituents(sector_id):
    cache_path = get_sector_cache_path(sector_id)
    if not os.path.exists(cache_path):
        return None

    cache_mtime = datetime.fromtimestamp(os.path.getmtime(cache_path))
    cache_age_days = (datetime.now() - cache_mtime).days
    if cache_age_days > SECTOR_CACHE_REFRESH_DAYS:
        print(f"全部A股成分股缓存已超过 {SECTOR_CACHE_REFRESH_DAYS} 天，准备刷新")
        return None

    df = pd.read_pickle(cache_path)
    if df.empty or not {"wind_code", "sec_name"}.issubset(df.columns):
        return None

    print(f"全部A股成分股命中缓存：{cache_path}")
    return df


def save_sector_constituents_cache(sector_id, stock_codes, stock_names):
    cache_path = get_sector_cache_path(sector_id)
    df = pd.DataFrame({
        "wind_code": stock_codes,
        "sec_name": stock_names,
    })
    df.to_pickle(cache_path)


def get_sector_constituents_with_cache(sector_id):
    cached_df = load_cached_sector_constituents(sector_id)
    if cached_df is not None:
        return cached_df["wind_code"].tolist(), cached_df["sec_name"].tolist()

    print("全部A股成分股未命中缓存，开始从 Wind 拉取")
    sector_data = w.wset("sectorconstituent", f"sectorid={sector_id}")
    if sector_data.ErrorCode != 0 or not sector_data.Data:
        cache_path = get_sector_cache_path(sector_id)
        if os.path.exists(cache_path):
            print(f"Wind 成分股拉取失败，使用过期缓存：{cache_path}")
            stale_df = pd.read_pickle(cache_path)
            return stale_df["wind_code"].tolist(), stale_df["sec_name"].tolist()
        raise ValueError(f"全部A股成分股拉取失败: ErrorCode={sector_data.ErrorCode}")

    code_idx = sector_data.Fields.index("wind_code")
    name_idx = sector_data.Fields.index("sec_name")
    stock_codes = sector_data.Data[code_idx]
    stock_names = sector_data.Data[name_idx]
    save_sector_constituents_cache(sector_id, stock_codes, stock_names)
    return stock_codes, stock_names


# =========================
# 2. 全部A股成分股
# =========================
sector_id = "a001010100000000"

stock_codes, stock_names = get_sector_constituents_with_cache(sector_id)
code_to_name = dict(zip(stock_codes, stock_names))

print("全部A股股票数量：", len(stock_codes))


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


def normalize_percent_df(df):
    df = sanitize_score_component(df)
    return df.where(df.abs() <= 2, df / 100)


def neutral_score_like(ref_df, value=0.0):
    return pd.DataFrame(value, index=ref_df.index, columns=ref_df.columns)


def rank_score(df, ref_df, higher_is_better=True, neutral_value=0.5):
    df = sanitize_score_component(df).reindex(index=ref_df.index, columns=ref_df.columns)
    rank_pct = df.rank(axis=1, pct=True, ascending=True)
    score = rank_pct if higher_is_better else 1 - rank_pct
    return score.fillna(neutral_value)


def linear_score(df, ref_df, lower, upper, neutral_value=0.5):
    df = normalize_percent_df(df).reindex(index=ref_df.index, columns=ref_df.columns)
    score = ((df - lower) / (upper - lower)).clip(lower=0, upper=1)
    return score.fillna(neutral_value)


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


def calculate_fundamental_score_details(fundamental_data, ref_df):
    pe_ttm = fundamental_data.get("pe_ttm", neutral_score_like(ref_df, np.nan))
    pb_lf = fundamental_data.get("pb_lf", neutral_score_like(ref_df, np.nan))
    roe_ttm = normalize_percent_df(fundamental_data.get("roe_ttm", neutral_score_like(ref_df, np.nan)))
    debt_to_assets = normalize_percent_df(fundamental_data.get("debt_to_assets", neutral_score_like(ref_df, np.nan)))
    est_netprofit_fy1 = fundamental_data.get("est_netprofit_fy1", neutral_score_like(ref_df, np.nan))
    est_netprofit_yoy = normalize_percent_df(
        fundamental_data.get("est_netprofit_yoy", neutral_score_like(ref_df, np.nan))
    )
    has_est_netprofit_fy1 = est_netprofit_fy1.notna()
    has_est_netprofit_yoy = est_netprofit_yoy.notna()
    missing_all_estimates = (~has_est_netprofit_fy1) & (~has_est_netprofit_yoy)

    pe_positive = pe_ttm.where(pe_ttm > 0)
    valuation_pe_score = rank_score(pe_positive, ref_df, higher_is_better=False)
    valuation_pb_score = rank_score(pb_lf.where(pb_lf > 0), ref_df, higher_is_better=False)
    valuation_score = 0.65 * valuation_pe_score + 0.35 * valuation_pb_score

    growth_score = linear_score(
        est_netprofit_yoy,
        ref_df,
        lower=-0.20,
        upper=0.50,
        neutral_value=MISSING_ESTIMATE_NEUTRAL_SCORE,
    )

    est_netprofit_revision = est_netprofit_fy1 / est_netprofit_fy1.shift(REVISION_LOOKBACK_DAYS).replace(0, np.nan) - 1
    revision_score = linear_score(
        est_netprofit_revision,
        ref_df,
        lower=-0.10,
        upper=0.20,
        neutral_value=MISSING_ESTIMATE_NEUTRAL_SCORE,
    )

    roe_score = linear_score(roe_ttm, ref_df, lower=0.00, upper=0.20)
    debt_quality_score = 1 - linear_score(debt_to_assets, ref_df, lower=0.30, upper=0.80)
    quality_score = 0.75 * roe_score + 0.25 * debt_quality_score

    loss_penalty = ((pe_ttm <= 0) | (est_netprofit_fy1 <= 0)).astype(float) * 0.40
    expensive_penalty = ((pe_ttm > 80).astype(float) * 0.20 + (pe_ttm > 120).astype(float) * 0.20)
    expensive_penalty = expensive_penalty + (pb_lf > 10).astype(float) * 0.20
    revision_down_penalty = (
        (est_netprofit_revision < -0.05).astype(float) * 0.20
        + (est_netprofit_revision < -0.10).astype(float) * 0.20
    )
    leverage_penalty = (
        (debt_to_assets > 0.70).astype(float) * 0.15
        + (debt_to_assets > 0.85).astype(float) * 0.15
    )
    missing_estimate_penalty = missing_all_estimates.astype(float) * MISSING_ESTIMATE_RISK_PENALTY
    risk_penalty = (
        loss_penalty
        + expensive_penalty
        + revision_down_penalty
        + leverage_penalty
        + missing_estimate_penalty
    ).clip(upper=1.0)

    total_score = (
        VALUATION_SCORE_WEIGHT * valuation_score
        + GROWTH_SCORE_WEIGHT * growth_score
        + REVISION_SCORE_WEIGHT * revision_score
        + QUALITY_SCORE_WEIGHT * quality_score
        - RISK_PENALTY_WEIGHT * risk_penalty
    )

    return {
        "valuation_score": sanitize_score_component(valuation_score),
        "growth_score": sanitize_score_component(growth_score),
        "revision_score": sanitize_score_component(revision_score),
        "quality_score": sanitize_score_component(quality_score),
        "risk_penalty": sanitize_score_component(risk_penalty),
        "has_est_netprofit_fy1": has_est_netprofit_fy1.reindex(index=ref_df.index, columns=ref_df.columns).fillna(False),
        "has_est_netprofit_yoy": has_est_netprofit_yoy.reindex(index=ref_df.index, columns=ref_df.columns).fillna(False),
        "missing_estimate_penalty": sanitize_score_component(missing_estimate_penalty),
        "total_score": sanitize_score_component(total_score),
    }


def build_fundamental_observation_pool(score_df, top_n, min_score=None, update_freq="M"):
    score_df = sanitize_score_component(score_df)
    update_freq = str(update_freq).upper()
    if update_freq == "D":
        update_dates = list(score_df.index)
    elif update_freq == "Q":
        period_index = score_df.index.to_period("Q")
        update_dates = pd.Series(score_df.index, index=score_df.index).groupby(period_index).first().tolist()
    else:
        period_index = score_df.index.to_period("M")
        update_dates = pd.Series(score_df.index, index=score_df.index).groupby(period_index).first().tolist()

    pool = pd.DataFrame(np.nan, index=score_df.index, columns=score_df.columns, dtype=object)
    for date in update_dates:
        pool.loc[date, :] = False
        score_row = score_df.loc[date].dropna()
        if min_score is not None:
            score_row = score_row[score_row >= min_score]
        selected_codes = score_row.sort_values(ascending=False).head(top_n).index
        pool.loc[date, selected_codes] = True

    return pool.ffill().fillna(False).astype(bool)


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


def get_fundamental_wsd_batch(codes, wind_field, query_start_date, query_end_date, batch_size=FUNDAMENTAL_BATCH_SIZE):
    all_df = []

    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        print(f"拉取基本面 {wind_field}: {i}-{i + len(batch)} [{query_start_date} ~ {query_end_date}]")
        data = w.wsd(batch, wind_field, query_start_date, query_end_date, "")

        if data.ErrorCode != 0 or len(data.Times) == 0:
            print(f"基本面字段 {wind_field} 拉取失败: ErrorCode={data.ErrorCode}")
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
                f"基本面返回维度异常：field={wind_field}, codes={len(data.Codes)}, "
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


def get_fundamental_query_dates(target_index):
    target_index = pd.DatetimeIndex(target_index).sort_values()
    if FUNDAMENTAL_START_DATE is not None:
        fundamental_start_ts = pd.Timestamp(FUNDAMENTAL_START_DATE)
        target_index = target_index[target_index >= fundamental_start_ts]
    if len(target_index) == 0:
        return []

    if FUNDAMENTAL_FETCH_MODE.upper() == "DAILY":
        return list(target_index)

    update_freq = str(FUNDAMENTAL_POOL_UPDATE_FREQ).upper()
    if update_freq == "Q":
        period_index = target_index.to_period("Q")
    else:
        period_index = target_index.to_period("M")

    query_dates = pd.Series(target_index, index=target_index).groupby(period_index).first().tolist()
    if len(query_dates) == 0 or query_dates[-1] != target_index[-1]:
        query_dates.append(target_index[-1])
    return sorted(pd.Timestamp(date) for date in set(query_dates))


def get_fundamental_snapshot_batch(codes, wind_field, query_dates):
    all_df = []
    total_dates = len(query_dates)
    for idx, query_date in enumerate(query_dates, start=1):
        query_date_str = pd.Timestamp(query_date).strftime("%Y-%m-%d")
        print(f"拉取基本面截面 {wind_field}: {idx}/{total_dates} [{query_date_str}]")
        day_df = get_fundamental_wsd_batch(codes, wind_field, query_date_str, query_date_str)
        if day_df.empty:
            continue
        all_df.append(day_df)

    if not all_df:
        return pd.DataFrame()

    df = pd.concat(all_df, axis=0)
    df = sanitize_fundamental_df(df)
    df = df[~df.index.duplicated(keep="last")]
    return df


def get_grouped_fundamental_snapshot_batch(codes, field_map, query_dates, batch_size=FUNDAMENTAL_BATCH_SIZE):
    result = {
        field_key: []
        for field_key in field_map
    }
    wind_fields = ",".join(field_map.values())
    field_keys = list(field_map.keys())
    wind_field_list = list(field_map.values())
    total_dates = len(query_dates)

    for date_idx, query_date in enumerate(query_dates, start=1):
        query_date_str = pd.Timestamp(query_date).strftime("%Y-%m-%d")
        for i in range(0, len(codes), batch_size):
            batch = codes[i:i + batch_size]
            print(
                f"拉取基本面组合 {wind_fields}: 日期 {date_idx}/{total_dates} "
                f"股票 {i}-{i + len(batch)} [{query_date_str}]"
            )
            data = w.wsd(batch, wind_fields, query_date_str, query_date_str, "")

            if data.ErrorCode != 0 or len(data.Times) == 0:
                print(f"基本面组合字段拉取失败: ErrorCode={data.ErrorCode}")
                if data.Codes and data.Codes[0] == "ErrorReport" and data.Data:
                    print("Wind错误信息：", data.Data[0][0])
                print("失败批次：", batch[:3])
                continue

            if len(data.Data) != len(wind_field_list):
                print(
                    f"基本面组合返回维度异常：fields={len(wind_field_list)}, "
                    f"codes={len(data.Codes)}, times={len(data.Times)}, data_rows={len(data.Data)}"
                )
                print("失败批次：", batch[:3])
                continue

            for field_key, values in zip(field_keys, data.Data):
                if len(values) != len(data.Codes):
                    print(
                        f"基本面组合字段 {field_key} 返回列数异常："
                        f"values={len(values)}, codes={len(data.Codes)}"
                    )
                    continue
                day_df = pd.DataFrame([values], index=[pd.Timestamp(query_date)], columns=data.Codes)
                day_df = day_df.apply(pd.to_numeric, errors="coerce")
                result[field_key].append(day_df)

    output = {}
    for field_key, frames in result.items():
        if not frames:
            output[field_key] = pd.DataFrame()
            continue
        df = pd.concat(frames, axis=0)
        df = sanitize_fundamental_df(df)
        df = df.groupby(df.index).last()
        df = df.loc[:, ~df.columns.duplicated()]
        output[field_key] = df
    return output


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


def get_fundamental_cache_path(field_key):
    return os.path.join(CACHE_DIR, f"{CACHE_PREFIX}_fundamental_{field_key}.pkl")


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


def sanitize_fundamental_df(df):
    if df.empty:
        return df
    df = df.apply(pd.to_numeric, errors="coerce")
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    df = df.loc[:, ~df.columns.duplicated()]
    return df


def load_cached_fundamental_df(field_key):
    cache_path = get_fundamental_cache_path(field_key)
    if not os.path.exists(cache_path):
        return pd.DataFrame()
    df = sanitize_fundamental_df(pd.read_pickle(cache_path))
    if RETRY_ALL_NAN_FUNDAMENTAL_CACHE and not df.empty and df.notna().sum().sum() == 0:
        print(f"基本面 {field_key} 缓存全为空值，只作为占位读取，后续会补拉有效截面：{cache_path}")
    return df


def save_cached_df(field, df):
    cache_path = get_cache_path(field)
    df = sanitize_price_df(df)
    df.to_pickle(cache_path)


def save_cached_fundamental_df(field_key, df):
    cache_path = get_fundamental_cache_path(field_key)
    df = sanitize_fundamental_df(df)
    df.to_pickle(cache_path)


def build_empty_fundamental_df(index, columns):
    return pd.DataFrame(np.nan, index=pd.to_datetime(index), columns=columns)


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
    cached_df = load_cached_df(field)
    if cached_df.empty:
        print(f"{field} 未命中缓存，开始全量拉取")
        full_df = get_wsd_batch(codes, field, start_date, end_date)
        if full_df.empty:
            raise ValueError(f"{field} 全量拉取失败，请检查 Wind 连接或字段权限。")
        full_df = supplement_with_wsq(full_df, codes, field, end_date)
        full_df = drop_all_nan_rows(full_df)
        save_cached_df(field, full_df)
        return full_df

    cached_df = cached_df.loc[:, [c for c in cached_df.columns if c in codes]]
    cached_codes = set(cached_df.columns)
    missing_codes = [c for c in codes if c not in cached_codes]

    incremental_df = pd.DataFrame()
    if not cached_df.empty:
        last_cached_ts = cached_df.index.max()
        update_start_dt = last_cached_ts.date() + timedelta(days=1)
        need_incremental = update_start_dt <= end_dt.date()
    else:
        need_incremental = False

    if need_incremental:
        print(f"{field} 命中缓存，增量更新: {update_start_dt.strftime('%Y-%m-%d')} ~ {end_date}")
        incremental_df = get_wsd_batch(
            list(cached_df.columns),
            field,
            update_start_dt.strftime("%Y-%m-%d"),
            end_date
        )

    missing_df = pd.DataFrame()
    if missing_codes:
        print(f"{field} 新增股票 {len(missing_codes)} 只，补拉全历史")
        missing_df = get_wsd_batch(missing_codes, field, start_date, end_date)

    combined_df = cached_df
    if not incremental_df.empty:
        combined_df = pd.concat([combined_df, incremental_df], axis=0)
    if not missing_df.empty:
        combined_df = pd.concat([combined_df, missing_df], axis=1)

    combined_df = combined_df.sort_index()
    combined_df = combined_df.loc[:, ~combined_df.columns.duplicated()]
    combined_df = combined_df[~combined_df.index.duplicated(keep="last")]
    combined_df = combined_df.loc[:, [c for c in codes if c in combined_df.columns]]
    combined_df = sanitize_price_df(combined_df)
    combined_df = drop_all_nan_rows(combined_df)

    combined_df = supplement_with_wsq(combined_df, codes, field, end_date)
    combined_df = drop_all_nan_rows(combined_df)

    if combined_df.empty:
        raise ValueError(f"{field} 缓存读取后为空，请检查缓存文件或 Wind 拉取结果。")

    if last_row_all_nan(combined_df):
        cache_path = get_cache_path(field)
        print(f"{field} 检测到缓存异常：最后一行整行空值，删除缓存并全量重拉")
        if os.path.exists(cache_path):
            os.remove(cache_path)

        full_df = get_wsd_batch(codes, field, start_date, end_date)
        if full_df.empty:
            raise ValueError(f"{field} 重拉失败，请检查 Wind 连接、字段权限或当日数据是否已落库。")

        full_df = full_df.sort_index()
        full_df = full_df.loc[:, ~full_df.columns.duplicated()]
        full_df = supplement_with_wsq(full_df, codes, field, end_date)
        full_df = drop_all_nan_rows(full_df)

        if full_df.empty or last_row_all_nan(full_df):
            raise ValueError(
                f"{field} 重拉后最后一行仍为空，疑似 Wind 当日 wsd/wsq 都未返回有效数据，请稍后再试。"
            )

        combined_df = full_df

    save_cached_df(field, combined_df)
    return combined_df


def get_fundamental_df_with_cache(codes, field_key, wind_field, target_index, target_columns):
    query_dates = get_fundamental_query_dates(target_index)
    if not query_dates:
        print(f"基本面 {field_key} 没有可查询日期，将使用空值中性处理")
        return build_empty_fundamental_df(target_index, target_columns)
    query_index = pd.DatetimeIndex(query_dates)
    cached_df = load_cached_fundamental_df(field_key)
    if cached_df.empty:
        print(f"基本面 {field_key} 未命中缓存，按 {len(query_dates)} 个截面日期拉取")
        full_df = get_fundamental_snapshot_batch(codes, wind_field, query_dates)
        if full_df.empty:
            print(f"基本面 {field_key} 全量拉取失败，将使用空值中性处理")
            empty_df = build_empty_fundamental_df(target_index, target_columns)
            if SAVE_EMPTY_FUNDAMENTAL_CACHE_ON_FAILURE:
                print(f"基本面 {field_key} 写入空值缓存")
                save_cached_fundamental_df(field_key, empty_df)
            return empty_df
        save_cached_fundamental_df(field_key, full_df)
        cached_df = full_df
    else:
        cached_df = cached_df.loc[:, [c for c in cached_df.columns if c in codes]]
        cached_df = cached_df.loc[cached_df.index.isin(query_index)]
        cached_codes = set(cached_df.columns)
        missing_codes = [c for c in codes if c not in cached_codes]
        valid_cached_dates = set(
            pd.DatetimeIndex(cached_df.index[cached_df.notna().sum(axis=1) > 0])
        )
        missing_dates = [date for date in query_dates if pd.Timestamp(date) not in valid_cached_dates]

        incremental_df = pd.DataFrame()
        if missing_dates:
            print(f"基本面 {field_key} 命中缓存，补拉缺失截面日期 {len(missing_dates)} 个")
            incremental_df = get_fundamental_snapshot_batch(
                list(cached_df.columns),
                wind_field,
                missing_dates
            )

        missing_df = pd.DataFrame()
        if missing_codes:
            print(f"基本面 {field_key} 新增股票 {len(missing_codes)} 只，按截面日期补拉")
            missing_df = get_fundamental_snapshot_batch(missing_codes, wind_field, query_dates)
            if missing_df.empty:
                print(f"基本面 {field_key} 新增股票补拉失败")
                if SAVE_EMPTY_FUNDAMENTAL_CACHE_ON_FAILURE:
                    print(f"基本面 {field_key} 为新增股票写入空值占位")
                    missing_df = build_empty_fundamental_df(target_index, missing_codes)

        combined_df = cached_df
        if not incremental_df.empty:
            combined_df = pd.concat([combined_df, incremental_df], axis=0)
        if not missing_df.empty:
            combined_df = pd.concat([combined_df, missing_df], axis=1)

        combined_df = sanitize_fundamental_df(combined_df)
        combined_df = combined_df[~combined_df.index.duplicated(keep="last")]
        combined_df = combined_df.loc[:, [c for c in codes if c in combined_df.columns]]
        save_cached_fundamental_df(field_key, combined_df)
        cached_df = combined_df

    cached_df = sanitize_fundamental_df(cached_df)
    cached_df = cached_df.loc[:, [c for c in target_columns if c in cached_df.columns]]
    aligned_df = cached_df.reindex(target_index).ffill()
    return aligned_df.reindex(index=target_index, columns=target_columns)


def align_fundamental_df(df, target_index, target_columns):
    if df.empty:
        return build_empty_fundamental_df(target_index, target_columns)
    df = sanitize_fundamental_df(df)
    df = df.loc[:, [c for c in target_columns if c in df.columns]]
    aligned_df = df.reindex(target_index).ffill()
    return aligned_df.reindex(index=target_index, columns=target_columns)


def get_grouped_fundamental_data_with_cache(codes, grouped_field_keys, target_index, target_columns):
    query_dates = get_fundamental_query_dates(target_index)
    if not grouped_field_keys:
        return {}
    if not query_dates:
        return {
            field_key: build_empty_fundamental_df(target_index, target_columns)
            for field_key in grouped_field_keys
        }
    query_index = pd.DatetimeIndex(query_dates)
    field_map = {
        field_key: FUNDAMENTAL_FIELDS[field_key]
        for field_key in grouped_field_keys
    }

    cached_data = {}
    need_fetch = False
    for field_key in grouped_field_keys:
        cached_df = load_cached_fundamental_df(field_key)
        if cached_df.empty:
            print(f"基本面组合字段 {field_key} 未命中有效缓存")
            need_fetch = True
            cached_data[field_key] = pd.DataFrame()
            continue

        cached_df = cached_df.loc[:, [c for c in cached_df.columns if c in codes]]
        cached_df = cached_df.loc[cached_df.index.isin(query_index)]
        cached_codes = set(cached_df.columns)
        missing_codes = [c for c in codes if c not in cached_codes]
        cached_dates = set(pd.DatetimeIndex(cached_df.index))
        missing_dates = [date for date in query_dates if pd.Timestamp(date) not in cached_dates]
        if missing_codes or missing_dates:
            print(
                f"基本面组合字段 {field_key} 缓存不完整："
                f"缺股票 {len(missing_codes)} 只，缺截面 {len(missing_dates)} 个"
            )
            need_fetch = True
        cached_data[field_key] = cached_df

    if need_fetch:
        print(
            "基本面组合字段开始合并拉取："
            + ", ".join(f"{k}={v}" for k, v in field_map.items())
        )
        fetched_data = get_grouped_fundamental_snapshot_batch(codes, field_map, query_dates)
        for field_key in grouped_field_keys:
            fetched_df = fetched_data.get(field_key, pd.DataFrame())
            if fetched_df.empty:
                print(f"基本面组合字段 {field_key} 拉取结果为空，保留原缓存/空值处理")
                continue
            save_cached_fundamental_df(field_key, fetched_df)
            cached_data[field_key] = fetched_df

    return {
        field_key: align_fundamental_df(cached_data.get(field_key, pd.DataFrame()), target_index, target_columns)
        for field_key in grouped_field_keys
    }

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

grouped_fundamental_data = get_grouped_fundamental_data_with_cache(
    stock_codes,
    GROUPED_FUNDAMENTAL_FIELD_KEYS,
    close_df.index,
    close_df.columns,
)
fundamental_data = dict(grouped_fundamental_data)
for field_key, wind_field in FUNDAMENTAL_FIELDS.items():
    if field_key in GROUPED_FUNDAMENTAL_FIELD_KEYS:
        continue
    fundamental_data[field_key] = get_fundamental_df_with_cache(
        stock_codes,
        field_key,
        wind_field,
        close_df.index,
        close_df.columns
    )

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
technical_score = score_details["total_score"]
fundamental_score_details = calculate_fundamental_score_details(fundamental_data, close_df)
fundamental_score = fundamental_score_details["total_score"]
fundamental_valuation_score = fundamental_score_details["valuation_score"]
fundamental_growth_score = fundamental_score_details["growth_score"]
fundamental_revision_score = fundamental_score_details["revision_score"]
fundamental_quality_score = fundamental_score_details["quality_score"]
fundamental_risk_penalty = fundamental_score_details["risk_penalty"]
fundamental_has_est_netprofit_fy1 = fundamental_score_details["has_est_netprofit_fy1"]
fundamental_has_est_netprofit_yoy = fundamental_score_details["has_est_netprofit_yoy"]
fundamental_missing_estimate_penalty = fundamental_score_details["missing_estimate_penalty"]
fundamental_pe_ttm = fundamental_data.get("pe_ttm", neutral_score_like(close_df, np.nan))
fundamental_pb_lf = fundamental_data.get("pb_lf", neutral_score_like(close_df, np.nan))
fundamental_roe_ttm = normalize_percent_df(fundamental_data.get("roe_ttm", neutral_score_like(close_df, np.nan)))
fundamental_debt_to_assets = normalize_percent_df(
    fundamental_data.get("debt_to_assets", neutral_score_like(close_df, np.nan))
)
fundamental_est_netprofit_fy1 = fundamental_data.get("est_netprofit_fy1", neutral_score_like(close_df, np.nan))
fundamental_est_netprofit_yoy = normalize_percent_df(
    fundamental_data.get("est_netprofit_yoy", neutral_score_like(close_df, np.nan))
)
fundamental_est_netprofit_revision = (
    fundamental_est_netprofit_fy1
    / fundamental_est_netprofit_fy1.shift(REVISION_LOOKBACK_DAYS).replace(0, np.nan)
    - 1
)
fundamental_data_quality_records = []
for field_key, field_df in fundamental_data.items():
    non_na_count = int(field_df.notna().sum().sum()) if not field_df.empty else 0
    total_count = int(field_df.size) if not field_df.empty else 0
    coverage = non_na_count / total_count if total_count > 0 else 0.0
    latest_data_date = close_df.index[-1]
    latest_non_na_count = int(field_df.loc[latest_data_date].notna().sum()) if latest_data_date in field_df.index else 0
    fundamental_data_quality_records.append({
        "字段": field_key,
        "Wind字段": FUNDAMENTAL_FIELDS.get(field_key, ""),
        "总非空数量": non_na_count,
        "总单元格数量": total_count,
        "总覆盖率": coverage,
        "最新日非空股票数": latest_non_na_count,
    })
fundamental_data_quality_df = pd.DataFrame(fundamental_data_quality_records)
empty_fundamental_fields = fundamental_data_quality_df.loc[
    fundamental_data_quality_df["总非空数量"] == 0,
    "字段"
].tolist()
if empty_fundamental_fields:
    print("警告：以下基本面字段当前全为空值，相关评分会退化为中性或扣分：", empty_fundamental_fields)
candidate_score = technical_score
fundamental_pool = build_fundamental_observation_pool(
    fundamental_score,
    top_n=FUNDAMENTAL_POOL_TOP_N,
    min_score=FUNDAMENTAL_POOL_MIN_SCORE,
    update_freq=FUNDAMENTAL_POOL_UPDATE_FREQ,
) if USE_FUNDAMENTAL_POOL else pd.DataFrame(True, index=close_df.index, columns=close_df.columns)
fundamental_pool_for_buy = fundamental_pool.shift(1).fillna(False).astype(bool)

cross = signal.diff()

golden_signal = (cross == 2) & ma60_up & ma120_up & limit_gain
buy_signal = golden_signal.shift(1).fillna(False).astype(bool)
buy_signal = buy_signal & fundamental_pool_for_buy

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
        daily_buy_limit = min(available_slots, MAX_DAILY_BUYS)
        daily_buy_count = 0
        buy_candidates = buy_signal.columns[buy_signal.loc[date]].tolist()
        buy_candidates = [
            code for code in buy_candidates
            if code not in current_holdings and code not in sold_today
        ]

        if buy_candidates:
            score_prev = candidate_score.loc[prev_date, buy_candidates].dropna().sort_values(ascending=False)
            for code in score_prev.index:
                if daily_buy_count >= daily_buy_limit:
                    break

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
                daily_buy_count += 1

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
stop_today = stop_signal.loc[today]

golden_trigger_list = golden_trigger_today[golden_trigger_today & fundamental_pool.loc[today]].index.tolist()
golden_exec_candidates = golden_exec_today[golden_exec_today].index.tolist()
golden_exec_score = candidate_score.shift(1).loc[today, golden_exec_candidates].dropna().sort_values(ascending=False)
golden_exec_list = golden_exec_score.head(MAX_DAILY_BUYS).index.tolist()
stop_list = stop_today[stop_today].index.tolist()

golden_trigger_df = pd.DataFrame({
    "代码": golden_trigger_list,
    "名称": [code_to_name.get(c, c) for c in golden_trigger_list],
    "技术评分": [candidate_score.at[today, c] for c in golden_trigger_list],
    "基本面评分": [fundamental_score.at[today, c] for c in golden_trigger_list],
    "估值分": [fundamental_valuation_score.at[today, c] for c in golden_trigger_list],
    "成长分": [fundamental_growth_score.at[today, c] for c in golden_trigger_list],
    "预期修正分": [fundamental_revision_score.at[today, c] for c in golden_trigger_list],
    "质量分": [fundamental_quality_score.at[today, c] for c in golden_trigger_list],
    "风险扣分": [fundamental_risk_penalty.at[today, c] for c in golden_trigger_list],
    "缺预期扣分": [fundamental_missing_estimate_penalty.at[today, c] for c in golden_trigger_list],
    "有FY1预期净利": [fundamental_has_est_netprofit_fy1.at[today, c] for c in golden_trigger_list],
    "有预期净利增速": [fundamental_has_est_netprofit_yoy.at[today, c] for c in golden_trigger_list],
    "是否在基本面池": [fundamental_pool.at[today, c] for c in golden_trigger_list],
    "MA5相对MA60强度": [score_ma5_ma60_gap.at[today, c] for c in golden_trigger_list],
    "MA60近5日趋势分": [score_ma60_5d_trend.at[today, c] for c in golden_trigger_list],
    "量比": [score_volume_ratio.at[today, c] for c in golden_trigger_list],
    "量比加分": [score_volume_ratio_score.at[today, c] for c in golden_trigger_list],
    "20日平均成交额": [avg_amount_20d.at[today, c] for c in golden_trigger_list],
    "信号": "最新金叉触发"
})
if not golden_trigger_df.empty:
    golden_trigger_df = golden_trigger_df.sort_values(by="技术评分", ascending=False, na_position="last")

golden_exec_df = pd.DataFrame({
    "代码": golden_exec_list,
    "名称": [code_to_name.get(c, c) for c in golden_exec_list],
    "技术评分": [candidate_score.shift(1).at[today, c] for c in golden_exec_list],
    "基本面评分": [fundamental_score.shift(1).at[today, c] for c in golden_exec_list],
    "估值分": [fundamental_valuation_score.shift(1).at[today, c] for c in golden_exec_list],
    "成长分": [fundamental_growth_score.shift(1).at[today, c] for c in golden_exec_list],
    "预期修正分": [fundamental_revision_score.shift(1).at[today, c] for c in golden_exec_list],
    "质量分": [fundamental_quality_score.shift(1).at[today, c] for c in golden_exec_list],
    "风险扣分": [fundamental_risk_penalty.shift(1).at[today, c] for c in golden_exec_list],
    "缺预期扣分": [fundamental_missing_estimate_penalty.shift(1).at[today, c] for c in golden_exec_list],
    "有FY1预期净利": [fundamental_has_est_netprofit_fy1.shift(1).at[today, c] for c in golden_exec_list],
    "有预期净利增速": [fundamental_has_est_netprofit_yoy.shift(1).at[today, c] for c in golden_exec_list],
    "是否在基本面池": [fundamental_pool.shift(1).at[today, c] for c in golden_exec_list],
    "MA5相对MA60强度": [score_ma5_ma60_gap.shift(1).at[today, c] for c in golden_exec_list],
    "MA60近5日趋势分": [score_ma60_5d_trend.shift(1).at[today, c] for c in golden_exec_list],
    "量比": [score_volume_ratio.shift(1).at[today, c] for c in golden_exec_list],
    "量比加分": [score_volume_ratio_score.shift(1).at[today, c] for c in golden_exec_list],
    "20日平均成交额": [avg_amount_20d.shift(1).at[today, c] for c in golden_exec_list],
    "信号": "昨日金叉今日执行"
})
if not golden_exec_df.empty:
    golden_exec_df = golden_exec_df.sort_values(by="技术评分", ascending=False, na_position="last")

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

current_pool_codes = fundamental_pool.loc[today]
current_pool_codes = current_pool_codes[current_pool_codes].index.tolist()
current_pool_df = pd.DataFrame({
    "代码": current_pool_codes,
    "名称": [code_to_name.get(c, c) for c in current_pool_codes],
    "基本面评分": [fundamental_score.at[today, c] for c in current_pool_codes],
    "估值分": [fundamental_valuation_score.at[today, c] for c in current_pool_codes],
    "成长分": [fundamental_growth_score.at[today, c] for c in current_pool_codes],
    "预期修正分": [fundamental_revision_score.at[today, c] for c in current_pool_codes],
    "质量分": [fundamental_quality_score.at[today, c] for c in current_pool_codes],
    "风险扣分": [fundamental_risk_penalty.at[today, c] for c in current_pool_codes],
    "缺预期扣分": [fundamental_missing_estimate_penalty.at[today, c] for c in current_pool_codes],
    "有FY1预期净利": [fundamental_has_est_netprofit_fy1.at[today, c] for c in current_pool_codes],
    "有预期净利增速": [fundamental_has_est_netprofit_yoy.at[today, c] for c in current_pool_codes],
    "技术评分": [technical_score.at[today, c] for c in current_pool_codes],
})
if not current_pool_df.empty:
    current_pool_df = current_pool_df.sort_values(by="基本面评分", ascending=False, na_position="last")

current_pool_fundamental_detail_df = pd.DataFrame({
    "代码": current_pool_codes,
    "名称": [code_to_name.get(c, c) for c in current_pool_codes],
    "基本面评分": [fundamental_score.at[today, c] for c in current_pool_codes],
    "估值分": [fundamental_valuation_score.at[today, c] for c in current_pool_codes],
    "成长分": [fundamental_growth_score.at[today, c] for c in current_pool_codes],
    "预期修正分": [fundamental_revision_score.at[today, c] for c in current_pool_codes],
    "质量分": [fundamental_quality_score.at[today, c] for c in current_pool_codes],
    "风险扣分": [fundamental_risk_penalty.at[today, c] for c in current_pool_codes],
    "缺预期扣分": [fundamental_missing_estimate_penalty.at[today, c] for c in current_pool_codes],
    "PE_TTM": [fundamental_pe_ttm.at[today, c] for c in current_pool_codes],
    "PB_LF": [fundamental_pb_lf.at[today, c] for c in current_pool_codes],
    "ROE_TTM": [fundamental_roe_ttm.at[today, c] for c in current_pool_codes],
    "资产负债率": [fundamental_debt_to_assets.at[today, c] for c in current_pool_codes],
    "FY1预期净利": [fundamental_est_netprofit_fy1.at[today, c] for c in current_pool_codes],
    "预期净利增速": [fundamental_est_netprofit_yoy.at[today, c] for c in current_pool_codes],
    f"FY1预期净利{REVISION_LOOKBACK_DAYS}日变化": [
        fundamental_est_netprofit_revision.at[today, c] for c in current_pool_codes
    ],
    "有FY1预期净利": [fundamental_has_est_netprofit_fy1.at[today, c] for c in current_pool_codes],
    "有预期净利增速": [fundamental_has_est_netprofit_yoy.at[today, c] for c in current_pool_codes],
    "技术评分": [technical_score.at[today, c] for c in current_pool_codes],
    "最新价": [close_df.at[today, c] for c in current_pool_codes],
    "20日平均成交额": [avg_amount_20d.at[today, c] for c in current_pool_codes],
})
if not current_pool_fundamental_detail_df.empty:
    current_pool_fundamental_detail_df = current_pool_fundamental_detail_df.sort_values(
        by="基本面评分",
        ascending=False,
        na_position="last"
    )

fundamental_config_df = pd.DataFrame({
    "参数": [
        "USE_FUNDAMENTAL_POOL",
        "FUNDAMENTAL_POOL_TOP_N",
        "FUNDAMENTAL_POOL_MIN_SCORE",
        "FUNDAMENTAL_POOL_UPDATE_FREQ",
        "VALUATION_SCORE_WEIGHT",
        "GROWTH_SCORE_WEIGHT",
        "REVISION_SCORE_WEIGHT",
        "QUALITY_SCORE_WEIGHT",
        "RISK_PENALTY_WEIGHT",
        "REVISION_LOOKBACK_DAYS",
        "MISSING_ESTIMATE_NEUTRAL_SCORE",
        "MISSING_ESTIMATE_RISK_PENALTY",
        "FUNDAMENTAL_START_DATE",
        "FUNDAMENTAL_FETCH_MODE",
        "FUNDAMENTAL_BATCH_SIZE",
        "GROUPED_FUNDAMENTAL_FIELD_KEYS",
        "SAVE_EMPTY_FUNDAMENTAL_CACHE_ON_FAILURE",
    ],
    "数值": [
        USE_FUNDAMENTAL_POOL,
        FUNDAMENTAL_POOL_TOP_N,
        FUNDAMENTAL_POOL_MIN_SCORE,
        FUNDAMENTAL_POOL_UPDATE_FREQ,
        VALUATION_SCORE_WEIGHT,
        GROWTH_SCORE_WEIGHT,
        REVISION_SCORE_WEIGHT,
        QUALITY_SCORE_WEIGHT,
        RISK_PENALTY_WEIGHT,
        REVISION_LOOKBACK_DAYS,
        MISSING_ESTIMATE_NEUTRAL_SCORE,
        MISSING_ESTIMATE_RISK_PENALTY,
        FUNDAMENTAL_START_DATE,
        FUNDAMENTAL_FETCH_MODE,
        FUNDAMENTAL_BATCH_SIZE,
        ", ".join(GROUPED_FUNDAMENTAL_FIELD_KEYS),
        SAVE_EMPTY_FUNDAMENTAL_CACHE_ON_FAILURE,
    ],
    "说明": [
        "是否启用基本面观察池",
        "每次更新时按基本面评分最多纳入的股票数量",
        "基本面评分低于该值的不纳入观察池",
        "观察池更新频率：M=月度，Q=季度，D=每日",
        "估值分权重",
        "成长分权重",
        "预期修正分权重",
        "质量分权重",
        "风险惩罚权重",
        "预期利润修正观察窗口",
        "缺失券商预期数据时，成长分/预期修正分使用的中性偏低分",
        "FY1预期净利和预期净利增速都缺失时追加的风险扣分",
        "基本面数据最早拉取日期；早于该日期的回测区间用后续截面前向填充",
        "基本面数据拉取模式：默认仅拉观察池更新日截面，避免全历史日频拉取",
        "基本面字段每批拉取的股票数量",
        "合并到同一次 Wind 请求里的基础基本面字段",
        "基本面拉取失败时是否写入空值缓存；默认关闭，避免额度超限结果污染缓存",
    ],
})

# =========================
# 20. 输出
# =========================
output_dir = os.path.join(BASE_DIR, "输出")
os.makedirs(output_dir, exist_ok=True)
output_file = os.path.join(output_dir, f"金叉买入_前高回撤10%卖出策略_全部A股_{end_date}.xlsx")

with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
    nav_analysis.to_frame("净值").to_excel(writer, sheet_name="净值")
    stats.to_excel(writer, sheet_name="策略指标", index=False)
    close_reason_stats.to_excel(writer, sheet_name="平仓原因统计", index=False)
    position_df.to_excel(writer, sheet_name="每日持仓", index=False)
    current_holding_df.to_excel(writer, sheet_name="当前持仓", index=False)
    golden_trigger_df.to_excel(writer, sheet_name="最新金叉触发", index=False)
    golden_exec_df.to_excel(writer, sheet_name="今日执行买入", index=False)
    stop_df.to_excel(writer, sheet_name="最新交易日卖出", index=False)
    latest_intraday_stop_monitor_df.to_excel(writer, sheet_name="最新盘中卖出监控", index=False)
    latest_low_efficiency_sell_plan_df.to_excel(writer, sheet_name="明日低效持仓卖出", index=False)
    current_pool_df.to_excel(writer, sheet_name="当前基本面观察池", index=False)
    current_pool_fundamental_detail_df.to_excel(writer, sheet_name="当前池基本面明细", index=False)
    fundamental_data_quality_df.to_excel(writer, sheet_name="基本面数据覆盖", index=False)
    fundamental_config_df.to_excel(writer, sheet_name="基本面评分参数", index=False)

print("\n【最新信号】")
print(
    f"基本面观察池: {'启用' if USE_FUNDAMENTAL_POOL else '关闭'} | "
    f"更新频率={FUNDAMENTAL_POOL_UPDATE_FREQ} | "
    f"TopN={FUNDAMENTAL_POOL_TOP_N} | "
    f"最低分={FUNDAMENTAL_POOL_MIN_SCORE} | "
    f"当前池数量={len(current_pool_df)}"
)
print(f"最新金叉触发数量: {len(golden_trigger_list)}")
print(f"昨日金叉今日执行数量: {len(golden_exec_list)}")
print(f"最新交易日卖出数量: {len(stop_list)}")
print(f"最新盘中卖出监控数量: {len(latest_intraday_stop_monitor_df)}")
print(f"明日低效持仓开盘卖出数量: {len(latest_low_efficiency_sell_plan_df)}")
print(f"当前持仓数量: {len(current_holding_df)}")

print("输出完成：", output_file)

# =========================
# 21. 关闭 Wind
# =========================
w.close()
