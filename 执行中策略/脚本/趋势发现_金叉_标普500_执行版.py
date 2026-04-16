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
MAX_HOLDINGS = 30
INITIAL_WEIGHT = 1 / MAX_HOLDINGS
STOP_DRAWDOWN = 0.1
LOOKBACK_DAYS = 1600
TRADE_START_DATE = "2023-04-03"  # 必须写成字符串，例如 "2025-01-01"；None 表示沿用当前逻辑
CACHE_PREFIX = "标普500"

# =========================
# 1. 启动 Wind
# =========================
w.start()

# =========================
# 2. 标普500成分股
# =========================
sector_id = "a005010800000000"

sector = w.wset("sectorconstituent", f"sectorid={sector_id}")

code_idx = sector.Fields.index("wind_code")
name_idx = sector.Fields.index("sec_name")

stock_codes = sector.Data[code_idx]
stock_names = sector.Data[name_idx]
code_to_name = dict(zip(stock_codes, stock_names))

print("标普500股票数量：", len(stock_codes))

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
def get_wsd_batch(codes, field, query_start_date, query_end_date, batch_size=100):
    all_df = []
    effective_batch_size = len(codes) if query_start_date == query_end_date else batch_size

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

        df.index = pd.to_datetime(df.index)
        df = df.apply(pd.to_numeric, errors="coerce")
        all_df.append(df)

    if not all_df:
        return pd.DataFrame()

    df = pd.concat(all_df, axis=1)
    df = df.sort_index()
    df = df.loc[:, ~df.columns.duplicated()]

    return df


def get_wsq_realtime_batch(codes, wsq_field, trade_date, batch_size=100):
    all_rows = []
    effective_batch_size = len(codes)

    for i in range(0, len(codes), effective_batch_size):
        batch = codes[i:i + effective_batch_size]
        print(f"拉取 {wsq_field}: {i}-{i + len(batch)} [{trade_date}]")
        data = w.wsq(batch, wsq_field)

        if data.ErrorCode != 0 or len(data.Codes) == 0 or len(data.Data) == 0:
            print("失败批次：", batch[:3])
            continue

        batch_df = pd.DataFrame([data.Data[0]], index=[pd.Timestamp(trade_date)], columns=data.Codes)
        all_rows.append(batch_df)

    if not all_rows:
        return pd.DataFrame()

    df = pd.concat(all_rows, axis=1)
    df = df.sort_index()
    df = df.loc[:, ~df.columns.duplicated()]
    return df


def get_cache_path(field):
    return os.path.join(CACHE_DIR, f"{CACHE_PREFIX}_{field}_PriceAdjF.pkl")


def load_cached_df(field):
    cache_path = get_cache_path(field)
    if not os.path.exists(cache_path):
        return pd.DataFrame()

    df = pd.read_pickle(cache_path)
    if df.empty:
        return df

    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    df = df.loc[:, ~df.columns.duplicated()]
    return df


def save_cached_df(field, df):
    cache_path = get_cache_path(field)
    df = df.sort_index()
    df = df.loc[:, ~df.columns.duplicated()]
    df.to_pickle(cache_path)


def has_recent_all_nan_rows(df, recent_days=3):
    if df.empty:
        return False
    recent_df = df.tail(recent_days)
    return (recent_df.notna().sum(axis=1) == 0).any()


def get_price_df_with_cache(codes, field):
    cached_df = load_cached_df(field)
    if cached_df.empty:
        print(f"{field} 未命中缓存，开始全量拉取")
        full_df = get_wsd_batch(codes, field, start_date, end_date)
        if full_df.empty:
            raise ValueError(f"{field} 全量拉取失败，请检查 Wind 连接或字段权限。")
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

    realtime_field_map = {
        "open": "rt_open",
        "high": "rt_high",
        "close": "rt_last",
    }
    wsq_field = realtime_field_map.get(field)
    if wsq_field is not None:
        rt_df = get_wsq_realtime_batch(codes, wsq_field, end_date)
        if not rt_df.empty:
            combined_df = pd.concat([combined_df, rt_df], axis=0)
            combined_df = combined_df.sort_index()
            combined_df = combined_df[~combined_df.index.duplicated(keep="last")]
            combined_df = combined_df.loc[:, [c for c in codes if c in combined_df.columns]]

    if combined_df.empty:
        raise ValueError(f"{field} 缓存读取后为空，请检查缓存文件或 Wind 拉取结果。")

    if has_recent_all_nan_rows(combined_df):
        cache_path = get_cache_path(field)
        print(f"{field} 检测到缓存异常：最近交易日存在整行空值，删除缓存并全量重拉")
        if os.path.exists(cache_path):
            os.remove(cache_path)

        full_df = get_wsd_batch(codes, field, start_date, end_date)
        if full_df.empty:
            raise ValueError(f"{field} 重拉失败，请检查 Wind 连接、字段权限或当日数据是否已落库。")

        full_df = full_df.sort_index()
        full_df = full_df.loc[:, ~full_df.columns.duplicated()]

        if has_recent_all_nan_rows(full_df):
            raise ValueError(
                f"{field} 重拉后最近交易日仍存在整行空值，疑似 Wind 当日数据未完整返回，请稍后再试。"
            )

        combined_df = full_df

    save_cached_df(field, combined_df)
    return combined_df

# =========================
# 5. 行情
# =========================
open_df = get_price_df_with_cache(stock_codes, "open")
high_df = get_price_df_with_cache(stock_codes, "high")
close_df = get_price_df_with_cache(stock_codes, "close")

print("行情维度：", close_df.shape)

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
limit_gain = daily_ret <= 0.05
candidate_score = (ma5 / ma60 - 1) + 0.5 * (ma60 / ma60.shift(5) - 1)
candidate_score = candidate_score.replace([np.inf, -np.inf], np.nan)

cross = signal.diff()

golden_signal = (cross == 2) & ma60_up & ma120_up & limit_gain
buy_signal = golden_signal.shift(1).fillna(False).astype(bool)

# =========================
# 10. 持仓状态机
# 新开仓固定初始仓位，买入后仓位随涨跌自然漂移
# =========================
position = pd.DataFrame(0.0, index=close_df.index, columns=close_df.columns)
stop_signal = pd.DataFrame(False, index=close_df.index, columns=close_df.columns)
cost_rate = 0.0015

current_holdings = {}
cash = 1.0
portfolio_values = []
turnover_records = []
closed_trade_returns = []
prev_date = None

for date in close_df.index:
    traded_amount = 0.0

    # 先把老持仓按昨收到今收更新市值
    if prev_date is not None:
        for code in list(current_holdings.keys()):
            prev_price = close_df.at[prev_date, code]
            price = close_df.at[date, code]
            if pd.isna(prev_price) or pd.isna(price) or prev_price <= 0:
                continue
            current_holdings[code]["value"] *= price / prev_price

    portfolio_before_buy = cash + sum(info["value"] for info in current_holdings.values())

    # 先用前一日信号在今日开盘买入
    available_slots = MAX_HOLDINGS - len(current_holdings)
    can_open_new_position = trade_start_ts is None or date >= trade_start_ts
    if can_open_new_position and available_slots > 0 and cash > 0:
        buy_candidates = buy_signal.columns[buy_signal.loc[date]].tolist()
        buy_candidates = [code for code in buy_candidates if code not in current_holdings]

        if buy_candidates:
            score_today = candidate_score.loc[date, buy_candidates].dropna().sort_values(ascending=False)
            for code in score_today.head(available_slots).index:
                open_price = open_df.at[date, code]
                high_price = high_df.at[date, code]
                close_price = close_df.at[date, code]
                if (
                    pd.isna(open_price)
                    or pd.isna(high_price)
                    or pd.isna(close_price)
                    or open_price <= 0
                    or high_price <= 0
                    or close_price <= 0
                ):
                    continue

                target_value = portfolio_before_buy * INITIAL_WEIGHT
                max_affordable = cash / (1 + cost_rate)
                buy_value = min(target_value, max_affordable)

                if buy_value <= 0:
                    break

                cash -= buy_value * (1 + cost_rate)
                traded_amount += buy_value
                current_holdings[code] = {
                    "entry_price": open_price,
                    "peak_price": high_price,
                    "value": buy_value * (close_price / open_price),
                    "cost_basis": buy_value
                }

    # 最后检查老持仓是否在今日收盘触发回撤卖出
    for code in list(current_holdings.keys()):
        high_price = high_df.at[date, code]
        close_price = close_df.at[date, code]
        if pd.isna(high_price) or pd.isna(close_price):
            continue

        holding_info = current_holdings[code]
        entry_price = holding_info["entry_price"]
        peak_price = max(holding_info["peak_price"], high_price)

        current_holdings[code]["peak_price"] = peak_price

        if peak_price > 0 and close_price <= peak_price * (1 - STOP_DRAWDOWN):
            sell_value = holding_info["value"]
            buy_cost = holding_info["cost_basis"] * (1 + cost_rate)
            sell_proceeds = sell_value * (1 - cost_rate)
            trade_return = sell_proceeds / buy_cost - 1 if buy_cost > 0 else 0
            closed_trade_returns.append(trade_return)
            cash += sell_proceeds
            traded_amount += sell_value
            stop_signal.at[date, code] = True
            del current_holdings[code]

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
    trade_win_rate = (
        pd.Series(closed_trade_returns).gt(0).mean() if trade_count > 0 else 0
    )
else:
    annual_ret = 0
    annual_vol = 0
    sharpe = 0
    max_dd = 0
    avg_holding = 0
    annual_turnover = 0
    win_rate = 0
    trade_count = 0
    trade_win_rate = 0

stats = pd.DataFrame({
    "指标": ["年化收益", "年化波动", "夏普比率", "最大回撤", "平均持仓数", "年化换手率", "日胜率", "平仓笔数", "单笔胜率"],
    "数值": [annual_ret, annual_vol, sharpe, max_dd, avg_holding, annual_turnover, win_rate, trade_count, trade_win_rate]
})

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
    holding_records.append({
        "代码": code,
        "名称": code_to_name.get(code, code),
        "开仓价": entry_price,
        "最新价": latest_price,
        "最新市值": info["value"],
        "组合权重": position.at[today, code] if code in position.columns else 0.0,
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

golden_trigger_list = golden_trigger_today[golden_trigger_today].index.tolist()
golden_exec_list = golden_exec_today[golden_exec_today].index.tolist()
stop_list = stop_today[stop_today].index.tolist()

golden_trigger_df = pd.DataFrame({
    "代码": golden_trigger_list,
    "名称": [code_to_name.get(c, c) for c in golden_trigger_list],
    "评分": [candidate_score.at[today, c] for c in golden_trigger_list],
    "信号": "最新金叉触发"
})
if not golden_trigger_df.empty:
    golden_trigger_df = golden_trigger_df.sort_values(by="评分", ascending=False, na_position="last")

golden_exec_df = pd.DataFrame({
    "代码": golden_exec_list,
    "名称": [code_to_name.get(c, c) for c in golden_exec_list],
    "评分": [candidate_score.at[today, c] for c in golden_exec_list],
    "信号": "昨日金叉今日执行"
})
if not golden_exec_df.empty:
    golden_exec_df = golden_exec_df.sort_values(by="评分", ascending=False, na_position="last")

stop_df = pd.DataFrame({
    "代码": stop_list,
    "名称": [code_to_name.get(c, c) for c in stop_list],
    "信号": "今日收盘触发并卖出"
})

# =========================
# 20. 输出
# =========================
output_dir = os.path.join(BASE_DIR, "输出")
os.makedirs(output_dir, exist_ok=True)
output_file = os.path.join(output_dir, f"金叉买入_分层回撤卖出策略_标普500_{end_date}.xlsx")

with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
    nav_analysis.to_frame("净值").to_excel(writer, sheet_name="净值")
    stats.to_excel(writer, sheet_name="策略指标", index=False)
    position_df.to_excel(writer, sheet_name="每日持仓", index=False)
    current_holding_df.to_excel(writer, sheet_name="当前持仓", index=False)
    golden_trigger_df.to_excel(writer, sheet_name="最新金叉触发", index=False)
    golden_exec_df.to_excel(writer, sheet_name="今日执行买入", index=False)
    stop_df.to_excel(writer, sheet_name="当日回撤卖出", index=False)

print("\n【最新信号】")
print(f"最新金叉触发数量: {len(golden_trigger_list)}")
print(f"昨日金叉今日执行数量: {len(golden_exec_list)}")
print(f"今日收盘触发并卖出数量: {len(stop_list)}")
print(f"当前持仓数量: {len(current_holding_df)}")

print("输出完成：", output_file)

# =========================
# 21. 关闭 Wind
# =========================
w.close()
