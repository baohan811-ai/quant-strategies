from WindPy import w
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

# =========================
# 0. 策略参数
# =========================
MAX_HOLDINGS = 20         # 最大持仓数
REBALANCE_FREQ = "W-FRI"  # 调仓频率：D=每日，W-FRI=每周五
MIN_SCORE = 0             # 最低信号强度，<=0 表示不额外过滤

# =========================
# 1. 启动 Wind
# =========================
w.start()

# =========================
# 2. 全A成分股
# =========================
sector_id = "1000011893000000"

sector = w.wset("sectorconstituent", f"sectorid={sector_id}")

code_idx = sector.Fields.index("wind_code")
name_idx = sector.Fields.index("sec_name")

stock_codes = sector.Data[code_idx]
stock_names = sector.Data[name_idx]
code_to_name = dict(zip(stock_codes, stock_names))

print("全A股票数量：", len(stock_codes))

# =========================
# 3. 时间区间
# =========================
end_date = datetime.today().strftime("%Y-%m-%d")
start_date = (datetime.today() - timedelta(days=400)).strftime("%Y-%m-%d")

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
# 6. 指数（沪深300）
# =========================
index_code = "000300.SH"
idx_data = w.wsd(index_code, "close", start_date, end_date, "PriceAdj=F")

index_df = pd.Series(idx_data.Data[0], index=idx_data.Times)

index_ma60 = index_df.rolling(60).mean()

# =========================
# 7. 动态仓位
# =========================
market_signal = index_df > index_ma60
market_signal = market_signal.shift(1).fillna(False)

exposure = np.where(market_signal, 1.0, 0.3)
exposure = pd.Series(exposure, index=index_df.index)

# =========================
# 8. 均线
# =========================
ma5 = close_df.rolling(5).mean()
ma60 = close_df.rolling(60).mean()

# =========================
# 9. 信号
# =========================
spread = ma5 - ma60
signal = np.sign(spread)

cross = signal.diff()

golden_signal = (cross == 2)
death_signal = (cross == -2)

golden_signal = golden_signal.shift(1).fillna(False).astype(bool)
death_signal = death_signal.shift(1).fillna(False).astype(bool)

# =========================
# 10. 持仓状态机
# =========================
strength_score = (ma5 / ma60 - 1).replace([np.inf, -np.inf], np.nan).fillna(0)

rebalance_dates = pd.Series(False, index=close_df.index)
if REBALANCE_FREQ == "D":
    rebalance_dates[:] = True
else:
    rebalance_dates.loc[pd.date_range(close_df.index[0], close_df.index[-1], freq=REBALANCE_FREQ)] = True
    rebalance_dates = rebalance_dates.reindex(close_df.index, fill_value=False)

position = pd.DataFrame(0.0, index=close_df.index, columns=close_df.columns)
current_holdings = set()

for date in close_df.index:
    buy_list = set(golden_signal.columns[golden_signal.loc[date]])
    sell_list = set(death_signal.columns[death_signal.loc[date]])

    # 死叉日优先卖出，避免无效持仓残留
    current_holdings -= sell_list

    if rebalance_dates.loc[date]:
        candidate_holdings = current_holdings | buy_list
        if candidate_holdings:
            score_today = strength_score.loc[date, list(candidate_holdings)]
            score_today = score_today[score_today > MIN_SCORE].sort_values(ascending=False)
            current_holdings = set(score_today.head(MAX_HOLDINGS).index.tolist())
        else:
            current_holdings = set()

    for code in current_holdings:
        position.loc[date, code] = 1.0

# =========================
# 11. 等权
# =========================
weight_sum = position.sum(axis=1)
position = position.div(weight_sum.replace(0, np.nan), axis=0)
position = position.fillna(0)

# =========================
# 12. 应用动态仓位
# =========================
exposure = exposure.reindex(position.index).ffill().fillna(0)
position = position.mul(exposure, axis=0)

# =========================
# 13. 收益
# =========================
ret = close_df.pct_change().fillna(0)
strategy_ret = (position.shift(1) * ret).sum(axis=1)

# =========================
# 14. 成本
# =========================
cost_rate = 0.0015
turnover = position.diff().abs().sum(axis=1)

strategy_ret = strategy_ret - turnover * cost_rate

# =========================
# 15. 净值
# =========================
nav = (1 + strategy_ret).cumprod()

# =========================
# ⭐ 16. 年化收益（修正版）
# =========================
# 找到首次建仓日
first_trade_date = position.sum(axis=1).ne(0).idxmax()

# 截取有效区间
nav_active = nav.loc[first_trade_date:]
ret_active = strategy_ret.loc[first_trade_date:]

# 年化收益
annual_ret = nav_active.iloc[-1] ** (252 / len(nav_active)) - 1

# 年化波动
annual_vol = ret_active.std() * np.sqrt(252)

# 夏普
sharpe = annual_ret / annual_vol if annual_vol != 0 else 0

# 最大回撤（仍用全周期）
rolling_max = nav.cummax()
drawdown = nav / rolling_max - 1
max_dd = drawdown.min()

# 持仓 & 换手
holding_count = (position > 0).sum(axis=1)
avg_holding = holding_count.mean()

avg_turnover = turnover.mean()
annual_turnover = avg_turnover * 252

win_rate = (strategy_ret > 0).mean()

stats = pd.DataFrame({
    "指标": ["年化收益","年化波动","夏普比率","最大回撤","平均持仓数","最大持仓数","年化换手率","日胜率"],
    "数值": [annual_ret, annual_vol, sharpe, max_dd, avg_holding, holding_count.max(), annual_turnover, win_rate]
})

print("\n【策略指标】")
print(stats)

# =========================
# 17. 持仓（最近20日）
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

last_20_dates = position.index[-20:]
position_recent = position.loc[last_20_dates]

position_df = extract_position(position_recent, code_to_name)
position_df = position_df.sort_values(by="日期", ascending=False)

# =========================
# 18. 当日信号
# =========================
today = position.index[-1]

golden_today = golden_signal.loc[today]
death_today = death_signal.loc[today]

golden_list = golden_today[golden_today].index.tolist()
death_list = death_today[death_today].index.tolist()

golden_df = pd.DataFrame({
    "代码": golden_list,
    "名称": [code_to_name.get(c, c) for c in golden_list],
    "信号": "金叉"
})

death_df = pd.DataFrame({
    "代码": death_list,
    "名称": [code_to_name.get(c, c) for c in death_list],
    "信号": "死叉"
})

# =========================
# 19. 输出
# =========================
output_file = f"./MA策略_中证800_{end_date}.xlsx"

with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
    nav.to_frame("净值").to_excel(writer, sheet_name="净值")
    stats.to_excel(writer, sheet_name="策略指标", index=False)
    position_df.to_excel(writer, sheet_name="每日持仓", index=False)
    golden_df.to_excel(writer, sheet_name="当日金叉", index=False)
    death_df.to_excel(writer, sheet_name="当日死叉", index=False)

print("输出完成：", output_file)

# =========================
# 20. 关闭 Wind
# =========================
w.close()
