from WindPy import w
import pandas as pd
from datetime import datetime, timedelta

# =========================
# 1. 启动 Wind
# =========================
w.start()

# =========================
# 2. 中证800成分
# =========================
sector_id = "1000011893000000"

sector = w.wset(
    "sectorconstituent",
    f"sectorid={sector_id}"
)

code_idx = sector.Fields.index("wind_code")
name_idx = sector.Fields.index("sec_name")

stock_codes = sector.Data[code_idx]
stock_names = sector.Data[name_idx]
code_to_name = dict(zip(stock_codes, stock_names))

print("中证800成分股数量：", len(stock_codes))

# =========================
# 3. 拉取近300日收盘价
# =========================
end_date = datetime.today().strftime("%Y-%m-%d")
start_date = (datetime.today() - timedelta(days=300)).strftime("%Y-%m-%d")

price_data = w.wsd(
    stock_codes,
    "close",
    start_date,
    end_date,
    ""
)

if price_data.ErrorCode != 0 or len(price_data.Times) == 0:
    raise RuntimeError("Wind 行情数据拉取失败")

close_df = pd.DataFrame(
    price_data.Data,
    index=price_data.Codes
).T
close_df.index = price_data.Times

# =========================
# 4. ✅ 校验 1：是否返回全部股票
# =========================
print("\n【完整性校验 1】")
print("Wind 返回股票列数：", close_df.shape[1])

missing_codes = set(stock_codes) - set(close_df.columns)
print("Wind 行情中缺失的股票数量：", len(missing_codes))
if missing_codes:
    print("缺失股票示例：", list(missing_codes)[:10])

# =========================
# 5. ✅ 校验 2：全周期无行情股票
# =========================
print("\n【完整性校验 2】")
all_nan_codes = close_df.columns[close_df.isna().all()]
print("全周期无有效行情股票数量：", len(all_nan_codes))
if len(all_nan_codes) > 0:
    print("示例股票：", list(all_nan_codes)[:10])

# =========================
# 6. MA10 & 最近10日
# =========================
ma10_df = close_df.rolling(10).mean()

close_last10 = close_df.tail(10)
ma10_last10 = ma10_df.tail(10)

# =========================
# 7. 条件筛选 + 剔除原因记录
# =========================
result = []
drop_reason = []

for code in close_last10.columns:
    closes = close_last10[code]
    ma10s = ma10_last10[code]
    name = code_to_name.get(code, code)

    # 7.1 全周期无行情
    if code in all_nan_codes:
        drop_reason.append((code, name, "全周期无行情"))
        continue

    # 7.2 近10日收盘价缺失
    if closes.isna().any():
        drop_reason.append((code, name, "近10日收盘价缺失"))
        continue

    # 7.3 MA10 数据不足
    if ma10s.isna().any():
        drop_reason.append((code, name, "MA10数据不足"))
        continue

    # 7.4 未连续站上 MA10
    if not (closes > ma10s).all():
        drop_reason.append((code, name, "未连续10日站上MA10"))
        continue

    # 7.5 满足条件
    result.append({
        "代码": code,
        "名称": name
    })

# =========================
# 8. 结果 DataFrame
# =========================
result_df = pd.DataFrame(result)
drop_df = pd.DataFrame(
    drop_reason,
    columns=["代码", "名称", "剔除原因"]
)

# =========================
# 9. ✅ 终极一致性校验
# =========================
total_checked = len(result_df) + len(drop_df)
print("\n【终极校验】")
print("参与判断股票数量：", total_checked)

assert total_checked == close_df.shape[1], \
    "⚠️ 股票数量不一致，存在未覆盖股票"

# =========================
# 10. 输出 Excel
# =========================
output_pass = f"中证800_连续10日站上MA10_{end_date}.xlsx"


result_df.to_excel(output_pass, index=False, engine="openpyxl")


print("\n满足条件股票数量：", len(result_df))
print("被剔除股票数量：", len(drop_df))
print("结果文件：", output_pass)


# =========================
# 11. 关闭 Wind
# =========================
w.close()
