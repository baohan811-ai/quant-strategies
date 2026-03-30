from WindPy import w
import pandas as pd
from datetime import datetime, timedelta

# =========================
# 0. 申万一级行业指数映射表（固定）
# =========================
SW_LEVEL1_INDEX = {
    "农林牧渔": "801010.SI",
    "采掘": "801020.SI",
    "化工": "801030.SI",
    "钢铁": "801040.SI",
    "有色金属": "801050.SI",
    "电子": "801080.SI",
    "家用电器": "801110.SI",
    "食品饮料": "801120.SI",
    "纺织服装": "801130.SI",
    "轻工制造": "801140.SI",
    "医药生物": "801150.SI",
    "公用事业": "801160.SI",
    "交通运输": "801170.SI",
    "房地产": "801180.SI",
    "商业贸易": "801200.SI",
    "休闲服务": "801210.SI",
    "综合": "801230.SI",
    "建筑材料": "801710.SI",
    "建筑装饰": "801720.SI",
    "电气设备": "801730.SI",
    "国防军工": "801740.SI",
    "计算机": "801750.SI",
    "传媒": "801760.SI",
    "通信": "801770.SI",
    "银行": "801780.SI",
    "非银金融": "801790.SI",
    "汽车": "801880.SI",
    "机械设备": "801890.SI"
}

# =========================
# 1. 启动 Wind
# =========================
w.start()

# =========================
# 2. 中证800 sectorid
# =========================
sector_id = "1000011893000000"

# =========================
# 3. 获取成分股
# =========================
sector = w.wset("sectorconstituent", f"sectorid={sector_id}")

code_idx = sector.Fields.index("wind_code")
name_idx = sector.Fields.index("sec_name")

stock_codes = sector.Data[code_idx]
stock_names = sector.Data[name_idx]

print("中证800成分股数量：", len(stock_codes))

# =========================
# 4. 申万一级行业名称
# =========================
industry = w.wss(stock_codes, "industry_sw", "industryType=1")
sw_industry = industry.Data[0]

# =========================
# 5. 行情窗口（20 天冗余）
# =========================
end_date = datetime.today().strftime("%Y-%m-%d")
start_date = (datetime.today() - timedelta(days=20)).strftime("%Y-%m-%d")

print("行情区间：", start_date, "→", end_date)

# =========================
# 6. 分批工具
# =========================
def chunk_list(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]

# =========================
# 7. 个股 5 日涨跌幅（严格 5TD）
# =========================
ret_5d = []

for sub in chunk_list(stock_codes, 100):
    data = w.wsd(sub, "close", start_date, end_date)

    if data.ErrorCode != 0:
        raise RuntimeError("获取个股收盘价失败")

    for x in data.Data:
        if x is None or len(x) < 6:
            ret_5d.append(None)
            continue

        s = x[-6]   # T-5
        e = x[-1]   # T
        ret_5d.append((e / s - 1) * 100 if s not in (None, 0) else None)

# =========================
# 8. 组装 DataFrame
# =========================
df = pd.DataFrame({
    "wind_code": stock_codes,
    "sec_name": stock_names,
    "sw_industry": sw_industry,
    "ret_5d": ret_5d
})

df = df.dropna(subset=["ret_5d"])

# =========================
# 9. 行业指数映射
# =========================
df["sw_industry_index"] = df["sw_industry"].map(SW_LEVEL1_INDEX)

# =========================
# 10. 行业指数 5 日涨跌幅
# =========================
industry_ret = {}

for sub in chunk_list(df["sw_industry_index"].dropna().unique().tolist(), 20):
    data = w.wsd(sub, "close", start_date, end_date)

    if data.ErrorCode != 0:
        raise RuntimeError("获取行业指数收盘价失败")

    for code, x in zip(sub, data.Data):
        if x is None or len(x) < 6:
            industry_ret[code] = None
            continue

        s = x[-6]
        e = x[-1]
        industry_ret[code] = (e / s - 1) * 100 if s not in (None, 0) else None

df["industry_ret_5d"] = df["sw_industry_index"].map(industry_ret)
df = df.dropna(subset=["industry_ret_5d"])

# =========================
# 11. 相对行业超额涨跌幅
# =========================
df["excess_ret_5d"] = df["ret_5d"] - df["industry_ret_5d"]

# =========================
# 12. 筛选 |超额| > 3%
# =========================
result = df[df["excess_ret_5d"].abs() > 3] \
           .sort_values("excess_ret_5d", ascending=False)

# ===== 新增功能 1：显示数量 =====
print("符合条件的个股数量：", len(result))

# =========================
# 13. 导出 Excel（新增功能 2）
# =========================
output_path = "中证800_相对申万行业指数5日超额涨跌幅.xlsx"
result.to_excel(output_path, index=False)

print("结果已导出至：", output_path)

# =========================
# 14. 关闭 Wind
# =========================
w.close()
