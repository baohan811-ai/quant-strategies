from WindPy import w
import pandas as pd
from datetime import datetime, timedelta

# =========================
# 0. 申万二级行业指数映射表
# =========================
SW_LEVEL2_INDEX = {
    "种植业": "801011.SI",
    "林业": "801012.SI",
    "渔业": "801013.SI",
    "农产品加工": "801014.SI",
    "农业综合": "801015.SI",
    "畜禽养殖": "801016.SI",
    "动物保健": "801017.SI",

    "煤炭开采": "801021.SI",
    "石油开采": "801022.SI",
    "其他采掘": "801023.SI",
    "采掘服务": "801024.SI",

    "石油化工": "801031.SI",
    "基础化工": "801032.SI",
    "化学制品": "801033.SI",
    "化学纤维": "801034.SI",
    "化工新材料": "801035.SI",

    "钢铁": "801041.SI",

    "工业金属": "801051.SI",
    "贵金属": "801052.SI",
    "小金属": "801053.SI",
    "金属新材料": "801054.SI",

    "半导体": "801081.SI",
    "元件": "801082.SI",
    "光学光电子": "801083.SI",
    "其他电子": "801084.SI",
    "电子化学品": "801085.SI",

    "白色家电": "801111.SI",
    "黑色家电": "801112.SI",
    "小家电": "801113.SI",
    "厨卫电器": "801114.SI",
    "家电零部件": "801115.SI",

    "食品加工": "801121.SI",
    "饮料制造": "801122.SI",
    "白酒": "801123.SI",
    "其他酒类": "801124.SI",

    "纺织制造": "801131.SI",
    "服装家纺": "801132.SI",
    "饰品": "801133.SI",

    "造纸": "801141.SI",
    "包装印刷": "801142.SI",
    "家居用品": "801143.SI",
    "其他轻工": "801144.SI",

    "化学制药": "801151.SI",
    "中药": "801152.SI",
    "生物制品": "801153.SI",
    "医药商业": "801154.SI",
    "医疗器械": "801155.SI",
    "医疗服务": "801156.SI",

    "电力": "801161.SI",
    "燃气": "801162.SI",
    "水务": "801163.SI",
    "环保": "801164.SI",

    "港口": "801171.SI",
    "公交": "801172.SI",
    "铁路运输": "801173.SI",
    "航空运输": "801174.SI",
    "机场": "801175.SI",
    "航运": "801176.SI",
    "物流": "801177.SI",

    "房地产开发": "801181.SI",
    "房地产服务": "801182.SI",

    "一般零售": "801201.SI",
    "专业零售": "801202.SI",
    "商业物业经营": "801203.SI",

    "景点": "801211.SI",
    "酒店": "801212.SI",
    "餐饮": "801213.SI",
    "旅游综合": "801214.SI",

    "综合": "801231.SI",

    "水泥": "801711.SI",
    "玻璃玻纤": "801712.SI",
    "其他建材": "801713.SI",

    "房屋建设": "801721.SI",
    "装修装饰": "801722.SI",
    "基础建设": "801723.SI",
    "专业工程": "801724.SI",

    "电机": "801731.SI",
    "电气自动化设备": "801732.SI",
    "电源设备": "801733.SI",
    "高低压设备": "801734.SI",

    "航空装备": "801741.SI",
    "航天装备": "801742.SI",
    "地面兵装": "801743.SI",
    "军工电子": "801744.SI",

    "计算机设备": "801751.SI",
    "计算机软件": "801752.SI",
    "IT服务": "801753.SI",

    "文化传媒": "801761.SI",
    "广告营销": "801762.SI",
    "影视院线": "801763.SI",
    "数字媒体": "801764.SI",

    "通信设备": "801771.SI",
    "通信服务": "801772.SI",

    "银行": "801781.SI",

    "证券": "801791.SI",
    "保险": "801792.SI",
    "多元金融": "801793.SI",

    "汽车整车": "801881.SI",
    "汽车零部件": "801882.SI",
    "汽车服务": "801883.SI",

    "通用机械": "801891.SI",
    "专用设备": "801892.SI",
    "轨交设备": "801893.SI",
    "工程机械": "801894.SI",
    "自动化设备": "801895.SI"
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
# 4. 申万二级行业名称（关键修改）
# =========================
industry = w.wss(stock_codes, "industry_sw", "industryType=2")
sw_industry_2 = industry.Data[0]

# =========================
# 5. 行情窗口
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
# 7. 个股 5 日涨跌幅
# =========================
ret_5d = []

for sub in chunk_list(stock_codes, 100):
    data = w.wsd(sub, "close", start_date, end_date)

    for x in data.Data:
        if x is None or len(x) < 6:
            ret_5d.append(None)
            continue
        ret_5d.append((x[-1] / x[-6] - 1) * 100)

# =========================
# 8. 组装 DataFrame
# =========================
df = pd.DataFrame({
    "wind_code": stock_codes,
    "sec_name": stock_names,
    "sw_industry_2": sw_industry_2,
    "ret_5d": ret_5d
}).dropna()

# =========================
# 9. 行业指数映射（二级）
# =========================
df["sw_industry_2_index"] = df["sw_industry_2"].map(SW_LEVEL2_INDEX)

# =========================
# 10. 二级行业指数 5 日涨跌幅
# =========================
industry_ret = {}

for sub in chunk_list(df["sw_industry_2_index"].dropna().unique().tolist(), 20):
    data = w.wsd(sub, "close", start_date, end_date)

    for code, x in zip(sub, data.Data):
        if x is None or len(x) < 6:
            industry_ret[code] = None
        else:
            industry_ret[code] = (x[-1] / x[-6] - 1) * 100

df["industry_2_ret_5d"] = df["sw_industry_2_index"].map(industry_ret)
df = df.dropna()

# =========================
# 11. 超额收益
# =========================
df["excess_ret_5d"] = df["ret_5d"] - df["industry_2_ret_5d"]

# =========================
# 12. 筛选 |超额| > 3%
# =========================
result = df[df["excess_ret_5d"].abs() > 3] \
           .sort_values("excess_ret_5d", ascending=False)

print("符合条件的个股数量：", len(result))

# =========================
# 13. 导出 Excel
# =========================
output_path = "中证800_相对申万二级行业5日超额涨跌幅.xlsx"
result.to_excel(output_path, index=False)

print("结果已导出至：", output_path)

# =========================
# 14. 关闭 Wind
# =========================
w.close()
