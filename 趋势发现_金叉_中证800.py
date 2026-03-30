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

sector = w.wset("sectorconstituent", f"sectorid={sector_id}")

code_idx = sector.Fields.index("wind_code")
name_idx = sector.Fields.index("sec_name")

stock_codes = sector.Data[code_idx]
stock_names = sector.Data[name_idx]
code_to_name = dict(zip(stock_codes, stock_names))

print("中证800成分股数量：", len(stock_codes))

# =========================
# 3. 时间区间
# =========================
end_date = datetime.today().strftime("%Y-%m-%d")
start_date = (datetime.today() - timedelta(days=300)).strftime("%Y-%m-%d")

# =========================
# 4. 分批请求函数（只取收盘价）
# =========================
def get_wsd_batch(codes, field, batch_size=100):
    all_df = []

    for i in range(0, len(codes), batch_size):
        batch = codes[i:i+batch_size]
        print(f"正在拉取 {field}：{i} - {i+len(batch)}")

        data = w.wsd(batch, field, start_date, end_date, "PriceAdj=F")

        if data.ErrorCode != 0 or len(data.Times) == 0:
            print(f"⚠️ {field} 批次失败：", batch[:3])
            continue

        df = pd.DataFrame(data.Data, index=data.Codes).T
        df.index = data.Times

        all_df.append(df)

    if not all_df:
        raise RuntimeError(f"{field} 数据全部获取失败")

    return pd.concat(all_df, axis=1)

# =========================
# 5. 获取收盘价
# =========================
close_df = get_wsd_batch(stock_codes, "close")

# =========================
# 6. 数据校验
# =========================
print("\n【数据校验】")
print("股票列数：", close_df.shape[1])

all_nan_codes = close_df.columns[close_df.isna().all()]
print("全周期无行情：", len(all_nan_codes))

# =========================
# 7. 计算均线
# =========================
ma5_df = close_df.rolling(5).mean()
ma60_df = close_df.rolling(60).mean()

ma5_last2 = ma5_df.tail(2)
ma60_last2 = ma60_df.tail(2)

# =========================
# 8. 筛选逻辑（仅金叉）
# =========================
result = []
drop_reason = []

for code in close_df.columns:
    name = code_to_name.get(code, code)

    ma5s = ma5_last2[code]
    ma60s = ma60_last2[code]

    # 无行情
    if code in all_nan_codes:
        drop_reason.append((code, name, "无行情"))
        continue

    # 数据不足
    if ma5s.isna().any() or ma60s.isna().any():
        drop_reason.append((code, name, "均线数据不足"))
        continue

    # 金叉判断（核心）
    cross = (
        ma5s.iloc[-2] <= ma60s.iloc[-2] and
        ma5s.iloc[-1] > ma60s.iloc[-1]
    )

    if not cross:
        drop_reason.append((code, name, "未发生金叉"))
        continue

    # 满足条件
    result.append({
        "代码": code,
        "名称": name
    })

# =========================
# 9. 输出结果
# =========================
result_df = pd.DataFrame(result)
drop_df = pd.DataFrame(drop_reason, columns=["代码", "名称", "剔除原因"])

print("\n满足条件数量：", len(result_df))
print("剔除数量：", len(drop_df))

# =========================
# 10. 输出 Excel
# =========================
output_file = f"./MA5上穿MA60_中证800_{end_date}.xlsx"

result_df.to_excel(output_file, index=False, engine="openpyxl")

print("结果已输出：", output_file)

# =========================
# 11. 关闭 Wind
# =========================
w.close()