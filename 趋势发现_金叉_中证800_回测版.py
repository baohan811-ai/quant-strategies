from WindPy import w
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

# =========================
# 0. 参数
# =========================
MAX_HOLDINGS = 20
INITIAL_WEIGHT = 0.05
LOW_PROFIT_THRESHOLD = 0.1
STOP_DRAWDOWN_LOW = 0.1
STOP_DRAWDOWN_HIGH = 0.1

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

# =========================
# 3. 时间区间
# =========================
end_date = datetime.today().strftime("%Y-%m-%d")
start_date = (datetime.today() - timedelta(days=1200)).strftime("%Y-%m-%d")

# =========================
# 4. 行情获取
# =========================
def get_wsd_batch(codes, field, batch_size=100):
    all_df = []

    for i in range(0, len(codes), batch_size):
        batch = codes[i:i+batch_size]
        print(f"拉取 {field}: {i}-{i+len(batch)}")

        data = w.wsd(batch, field, start_date, end_date, "PriceAdj=F")

        if data.ErrorCode != 0 or len(data.Times) == 0:
            print("失败批次：", batch[:3])
            continue

        df = pd.DataFrame(data.Data, index=data.Codes).T
        df.index = data.Times
        all_df.append(df)

    df = pd.concat(all_df, axis=1)
    df = df.sort_index()
    df = df.loc[:, ~df.columns.duplicated()]

    return df

# =========================
# 5. 收盘价
# =========================
close_df = get_wsd_batch(stock_codes, "close")

print("行情维度：", close_df.shape)

# =========================
# 6. 均线
# =========================
ma5 = close_df.rolling(5).mean()
ma60 = close_df.rolling(60).mean()

# =========================
# 9. 信号
# =========================
spread = ma5 - ma60
signal = np.sign(spread)
daily_ret = close_df.pct_change()
ma60_up = ma60 > ma60.shift(1)
limit_gain = daily_ret <= 0.05
candidate_score = (ma5 / ma60 - 1) + 0.5 * (ma60 / ma60.shift(5) - 1)
candidate_score = candidate_score.replace([np.inf, -np.inf], np.nan)

cross = signal.diff()

golden_signal = (cross == 2) & ma60_up & limit_gain

golden_signal = golden_signal.shift(1).fillna(False).astype(bool)

# =========================
# 10. 持仓状态机
# =========================
position = pd.DataFrame(0.0, index=close_df.index, columns=close_df.columns)
stop_signal = pd.DataFrame(False, index=close_df.index, columns=close_df.columns)
current_holdings = {}

for date in close_df.index:
    # 先检查老持仓是否触发回撤卖出
    for code in list(current_holdings.keys()):
        price = close_df.at[date, code]
        holding_info = current_holdings[code]
        entry_price = holding_info["entry_price"]
        peak_price = holding_info["peak_price"]

        if pd.notna(price):
            peak_price = max(peak_price, price) if pd.notna(peak_price) else price
            peak_gain = peak_price / entry_price - 1 if entry_price > 0 else 0
            stop_drawdown = STOP_DRAWDOWN_HIGH if peak_gain >= LOW_PROFIT_THRESHOLD else STOP_DRAWDOWN_LOW

            if peak_price > 0 and price <= peak_price * (1 - stop_drawdown):
                stop_signal.at[date, code] = True
                del current_holdings[code]
                continue

            current_holdings[code] = {
                "entry_price": entry_price,
                "peak_price": peak_price
            }

    # 有空余仓位时，从当日新金叉候选里按分数排序补仓
    available_slots = MAX_HOLDINGS - len(current_holdings)
    if available_slots > 0:
        buy_candidates = golden_signal.columns[golden_signal.loc[date]].tolist()
        buy_candidates = [code for code in buy_candidates if code not in current_holdings]

        if buy_candidates:
            score_today = candidate_score.loc[date, buy_candidates].dropna().sort_values(ascending=False)
            for code in score_today.head(available_slots).index:
                price = close_df.at[date, code]
                if pd.notna(price):
                    current_holdings[code] = {
                        "entry_price": price,
                        "peak_price": price
                    }

    for code in current_holdings:
        position.at[date, code] = INITIAL_WEIGHT

# =========================
# 11. 固定权重
# =========================
position = position.fillna(0)

# =========================
# 12. 收益
# =========================
ret = daily_ret.fillna(0)
strategy_ret = (position.shift(1) * ret).sum(axis=1)

# =========================
# 13. 成本
# =========================
cost_rate = 0.0015
turnover = position.diff().abs().sum(axis=1)

strategy_ret = strategy_ret - turnover * cost_rate

# =========================
# 14. 净值
# =========================
nav = (1 + strategy_ret).cumprod()

# =========================
# ⭐ 15. 年化收益（修正版）
# =========================
# 找到首次建仓日
first_trade_date = position.sum(axis=1).ne(0).idxmax()

# 截取有效区间
nav_active = nav.loc[first_trade_date:]
ret_active = strategy_ret.loc[first_trade_date:]
position_active = position.loc[first_trade_date:]
turnover_active = turnover.loc[first_trade_date:]

# 年化收益
annual_ret = nav_active.iloc[-1] ** (252 / len(nav_active)) - 1

# 年化波动
annual_vol = ret_active.std() * np.sqrt(252)

# 夏普
sharpe = annual_ret / annual_vol if annual_vol != 0 else 0

# 最大回撤
rolling_max = nav_active.cummax()
drawdown = nav_active / rolling_max - 1
max_dd = drawdown.min()

# 持仓 & 换手
holding_count = (position_active > 0).sum(axis=1)
avg_holding = holding_count.mean()

avg_turnover = turnover_active.mean()
annual_turnover = avg_turnover * 252

win_rate = (ret_active > 0).mean()

stats = pd.DataFrame({
    "指标": ["年化收益","年化波动","夏普比率","最大回撤","平均持仓数","年化换手率","日胜率"],
    "数值": [annual_ret, annual_vol, sharpe, max_dd, avg_holding, annual_turnover, win_rate]
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

position_df = extract_position(position, code_to_name)
position_df = position_df.sort_values(by="日期", ascending=False)

# =========================
# 18. 当日信号
# =========================
today = position.index[-1]

golden_today = golden_signal.loc[today]
stop_today = stop_signal.loc[today]

golden_list = golden_today[golden_today].index.tolist()
stop_list = stop_today[stop_today].index.tolist()

golden_df = pd.DataFrame({
    "代码": golden_list,
    "名称": [code_to_name.get(c, c) for c in golden_list],
    "信号": "金叉"
})

stop_df = pd.DataFrame({
    "代码": stop_list,
    "名称": [code_to_name.get(c, c) for c in stop_list],
    "信号": "分层回撤卖出"
})

# =========================
# 19. 输出
# =========================
output_file = f"./金叉买入_分层回撤卖出策略_中证800_{end_date}.xlsx"

with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
    nav.to_frame("净值").to_excel(writer, sheet_name="净值")
    stats.to_excel(writer, sheet_name="策略指标", index=False)
    position_df.to_excel(writer, sheet_name="每日持仓", index=False)
    golden_df.to_excel(writer, sheet_name="当日金叉", index=False)
    stop_df.to_excel(writer, sheet_name="当日回撤卖出", index=False)

print("输出完成：", output_file)

# =========================
# 20. 关闭 Wind
# =========================
w.close()
