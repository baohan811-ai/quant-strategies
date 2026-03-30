from WindPy import w
import pandas as pd
from datetime import datetime, timedelta

# =========================
# 1. 启动 Wind
# =========================
w.start()

sector_id = "a005010800000000"   # 标普500

sector = w.wset(
    "sectorconstituent",
    f"sectorid={sector_id}"
)


code_idx = sector.Fields.index("wind_code")
name_idx = sector.Fields.index("sec_name")

stock_codes = sector.Data[code_idx]
stock_names = sector.Data[name_idx]
code_to_name = dict(zip(stock_codes, stock_names))

print("标普500成分股数量：", len(stock_codes))

# =========================
# 3. 拉取近 300 个交易日收盘价
# =========================
end_date = datetime.today().strftime("%Y-%m-%d")
start_date = (datetime.today() - timedelta(days=300)).strftime("%Y-%m-%d")

price_data = w.wsd(
    stock_codes,
    "close",
    start_date,
    end_date,
    ""   # 不强制 PriceAdj，避免空数据
)

if price_data.ErrorCode != 0 or len(price_data.Times) == 0:
    raise RuntimeError("Wind 行情数据拉取失败")

close_df = pd.DataFrame(
    price_data.Data,
    index=price_data.Codes
).T
close_df.index = price_data.Times

# =========================
# 4. 条件：过去10日收盘价全部 > MA10
# =========================
ma10_df = close_df.rolling(10).mean()

close_last10 = close_df.tail(10)
ma10_last10 = ma10_df.tail(10)

result = []

for code in close_last10.columns:
    closes = close_last10[code]
    ma10s = ma10_last10[code]

    # 数据完整性校验
    if closes.isna().any() or ma10s.isna().any():
        continue

    if (closes > ma10s).all():
        result.append({
            "代码": code,
            "名称": code_to_name.get(code, code)
        })

# =========================
# 5. 输出为 Excel
# =========================
result_df = pd.DataFrame(result)

output_file = f"标普500_连续10日站上MA10_{end_date}.xlsx"
result_df.to_excel(output_file, index=False, engine="openpyxl")

print(f"\n满足条件的股票数量：{len(result_df)}")
print(f"结果已输出至：{output_file}")
