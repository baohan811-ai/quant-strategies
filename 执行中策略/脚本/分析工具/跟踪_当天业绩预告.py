import argparse
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPT_ROOT = SCRIPT_DIR.parent
OUTPUT_DIR = SCRIPT_ROOT.parent / "输出" / "业绩预告跟踪"
A_SHARE_SECTOR_ID = "a001010100000000"


# 不同 Wind 终端版本/权限下，业绩预告 WSET 表名和参数名可能略有差异。
# 如果脚本报出所有候选均失败，可在 Wind 代码生成器里查询“业绩预告”，
# 将生成的 w.wset 表名和 options 模板补到这里。
WSET_CANDIDATES = [
    {
        "name": "profitnotice",
        "option_templates": [
            "rptdate={rpt_date};startdate={start_date};enddate={end_date}",
            "rptdate={rpt_date_nodash};startdate={start_date_nodash};enddate={end_date_nodash}",
            "rptDate={rpt_date_nodash};startDate={start_date_nodash};endDate={end_date_nodash}",
            "annstartdate={start_date};annenddate={end_date};rptdate={rpt_date}",
        ],
    },
    {
        "name": "profitforecast",
        "option_templates": [
            "rptdate={rpt_date};startdate={start_date};enddate={end_date}",
            "rptdate={rpt_date_nodash};startdate={start_date_nodash};enddate={end_date_nodash}",
            "annstartdate={start_date};annenddate={end_date};rptdate={rpt_date}",
        ],
    },
    {
        "name": "performanceforecast",
        "option_templates": [
            "rptdate={rpt_date};startdate={start_date};enddate={end_date}",
            "rptdate={rpt_date_nodash};startdate={start_date_nodash};enddate={end_date_nodash}",
            "annstartdate={start_date};annenddate={end_date};rptdate={rpt_date}",
        ],
    },
    {
        "name": "ashareprofitnotice",
        "option_templates": [
            "rptdate={rpt_date};startdate={start_date};enddate={end_date}",
            "rptdate={rpt_date_nodash};startdate={start_date_nodash};enddate={end_date_nodash}",
        ],
    },
]


FIELD_ALIASES = {
    "wind_code": ["wind_code", "s_info_windcode", "code", "证券代码"],
    "sec_name": ["sec_name", "s_info_name", "name", "证券简称", "简称"],
    "ann_date": ["ann_dt", "ann_date", "s_fa_ann_date", "预披露日", "公告日期", "披露日期"],
    "rpt_date": ["report_period", "rpt_date", "report_date", "报告期", "报告截止日"],
    "forecast_type": ["type", "forecast_type", "预告类型", "业绩预告类型", "预警类型"],
    "net_profit_min": ["net_profit_min", "np_min", "净利润下限", "预告净利润下限", "归母净利润下限"],
    "net_profit_max": ["net_profit_max", "np_max", "净利润上限", "预告净利润上限", "归母净利润上限"],
    "yoy_min": ["yoy_min", "np_yoy_min", "同比增长下限", "净利润同比下限", "业绩变动下限"],
    "yoy_max": ["yoy_max", "np_yoy_max", "同比增长上限", "净利润同比上限", "业绩变动上限"],
    "reason": ["reason", "预告原因", "业绩变动原因", "说明"],
}


EXTRA_WSS_FIELDS = {
    "industry": ("wicsname2024", "tradeDate={trade_date};industryType=1;"),
    "close": ("close", "tradeDate={trade_date};PriceAdj=F"),
    "mkt_cap": ("mkt_cap", "tradeDate={trade_date};unit=1"),
    "pe_ttm": ("pe_ttm", "tradeDate={trade_date}"),
}


DISPLAY_COLUMNS = [
    "ann_date",
    "wind_code",
    "sec_name",
    "industry",
    "rpt_date",
    "forecast_type",
    "net_profit_min",
    "net_profit_max",
    "net_profit_mid",
    "yoy_min",
    "yoy_max",
    "yoy_mid",
    "close",
    "mkt_cap",
    "pe_ttm",
    "reason",
]

OUTPUT_COLUMN_NAMES = {
    "ann_date": "披露日",
    "wind_code": "代码",
    "sec_name": "名称",
    "industry": "行业",
    "rpt_date": "报告期",
    "forecast_type": "预告类型",
    "net_profit_min": "净利润下限(亿元)",
    "net_profit_max": "净利润上限(亿元)",
    "net_profit_mid": "净利润中值(亿元)",
    "yoy_min": "同比下限(%)",
    "yoy_max": "同比上限(%)",
    "yoy_mid": "同比中值(%)",
    "close": "收盘价",
    "mkt_cap": "总市值(亿元)",
    "pe_ttm": "PE_TTM",
    "reason": "原因摘要",
}


PROFIT_NOTICE_WSS_FIELDS = {
    "rpt_date": "profitnotice_lastrptdate",
    "summary": "profitnotice_abstract",
    "reason": "profitnotice_reason",
    "forecast_type": "profitnotice_style",
    "ann_date": "profitnotice_date",
    "first_ann_date": "profitnotice_firstdate",
    "net_profit_max": "profitnotice_netprofitmax",
    "net_profit_min": "profitnotice_netprofitmin",
    "yoy_max": "profitnotice_changemax",
    "yoy_min": "profitnotice_changemin",
}


def parse_yyyymmdd(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def nodash(d):
    return d.strftime("%Y%m%d")


def default_rpt_date(query_date):
    year = query_date.year
    if query_date.month <= 4:
        return date(year - 1, 12, 31)
    if query_date.month <= 8:
        return date(year, 6, 30)
    if query_date.month <= 10:
        return date(year, 9, 30)
    return date(year, 12, 31)


def default_lookback_days(query_date):
    """覆盖上一个工作日至查询日，避免周末公告在周一被漏掉。"""
    previous_workday = query_date - timedelta(days=1)
    while previous_workday.weekday() >= 5:
        previous_workday -= timedelta(days=1)
    return (query_date - previous_workday).days + 1


def ensure_wind_ok(data, label):
    if data.ErrorCode != 0:
        outmessage = getattr(data, "Data", None)
        raise RuntimeError(f"Wind {label} failed: {data.ErrorCode} {outmessage}")


def wind_data_to_df(data):
    fields = [str(field) for field in data.Fields]
    if not fields or not getattr(data, "Data", None):
        return pd.DataFrame()
    values = {field: data.Data[index] for index, field in enumerate(fields)}
    return pd.DataFrame(values)


def normalize_columns(df):
    rename = {}
    lowered = {str(col).strip().lower(): col for col in df.columns}
    raw_names = {str(col).strip(): col for col in df.columns}
    for standard, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            source = lowered.get(alias.lower()) or raw_names.get(alias)
            if source is not None:
                rename[source] = standard
                break
    normalized = df.rename(columns=rename).copy()
    for column in FIELD_ALIASES:
        if column not in normalized.columns:
            normalized[column] = pd.NA
    return normalized


def to_date_string(series):
    values = pd.to_datetime(series, errors="coerce")
    return values.dt.strftime("%Y-%m-%d")


def normalize_number_columns(df):
    for column in ["net_profit_min", "net_profit_max", "yoy_min", "yoy_max", "close", "mkt_cap", "pe_ttm"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    # Wind 预告净利润常见单位为万元；如果返回元，脚本不擅自换算，列名保持原始含义。
    df["net_profit_mid"] = df[["net_profit_min", "net_profit_max"]].mean(axis=1)
    df["yoy_mid"] = df[["yoy_min", "yoy_max"]].mean(axis=1)
    return df


def prepare_output_table(df):
    shown = df.copy()
    money_cols = ["net_profit_min", "net_profit_max", "net_profit_mid", "mkt_cap"]
    one_decimal_cols = money_cols + ["yoy_min", "yoy_max", "yoy_mid", "pe_ttm"]
    two_decimal_cols = ["close"]

    for col in money_cols:
        if col in shown.columns:
            shown[col] = pd.to_numeric(shown[col], errors="coerce") / 100000000
    for col in one_decimal_cols:
        if col in shown.columns:
            shown[col] = pd.to_numeric(shown[col], errors="coerce").round(1)
    for col in two_decimal_cols:
        if col in shown.columns:
            shown[col] = pd.to_numeric(shown[col], errors="coerce").round(2)

    return shown.rename(columns=OUTPUT_COLUMN_NAMES)


def format_for_display(df):
    shown = prepare_output_table(df)
    money_cols = ["净利润下限(亿元)", "净利润上限(亿元)", "净利润中值(亿元)", "总市值(亿元)"]
    pct_cols = ["同比下限(%)", "同比上限(%)", "同比中值(%)"]
    one_decimal_cols = money_cols + pct_cols + ["PE_TTM"]
    two_decimal_cols = ["收盘价"]
    for col in money_cols:
        if col in shown.columns:
            shown[col] = shown[col].map(lambda x: "" if pd.isna(x) else f"{x:,.1f}")
    for col in pct_cols:
        if col in shown.columns:
            shown[col] = shown[col].map(lambda x: "" if pd.isna(x) else f"{x:.1f}")
    for col in ["PE_TTM"]:
        if col in shown.columns:
            shown[col] = shown[col].map(lambda x: "" if pd.isna(x) else f"{x:.1f}")
    for col in two_decimal_cols:
        if col in shown.columns:
            shown[col] = shown[col].map(lambda x: "" if pd.isna(x) else f"{x:.2f}")
    return shown


def fetch_a_share_codes(w):
    data = w.wset("sectorconstituent", f"sectorid={A_SHARE_SECTOR_ID};field=wind_code,sec_name")
    ensure_wind_ok(data, "wset sectorconstituent")
    df = wind_data_to_df(data)
    if "wind_code" not in df.columns:
        raise RuntimeError(f"Wind 全A成分返回字段异常: {df.columns.tolist()}")
    names = dict(zip(df["wind_code"], df.get("sec_name", df["wind_code"])))
    return set(df["wind_code"].dropna().astype(str)), names


def fetch_profit_notice_by_wss(w, query_date, lookback_days, rpt_date):
    a_share_codes, code_to_name = fetch_a_share_codes(w)
    codes = sorted(a_share_codes)
    wind_fields = list(PROFIT_NOTICE_WSS_FIELDS.values())
    rows = []
    options = f"rptDate={nodash(rpt_date)}"

    for start in range(0, len(codes), 200):
        batch = codes[start:start + 200]
        print(f"拉取业绩预告 WSS: {start + 1}-{start + len(batch)} / {len(codes)}")
        data = w.wss(batch, ",".join(wind_fields), options)
        ensure_wind_ok(data, "wss profitnotice")
        by_field = {field.lower(): data.Data[index] for index, field in enumerate(data.Fields)}
        for row_index, code in enumerate(batch):
            row = {"wind_code": code, "sec_name": code_to_name.get(code, code)}
            for standard, wind_field in PROFIT_NOTICE_WSS_FIELDS.items():
                values = by_field.get(wind_field.lower())
                row[standard] = values[row_index] if values is not None and row_index < len(values) else None
            rows.append(row)

    df = pd.DataFrame(rows)
    if df.empty:
        return df, "wss profitnotice", options

    df["ann_date"] = to_date_string(df["ann_date"])
    df["first_ann_date"] = to_date_string(df["first_ann_date"])
    df["rpt_date"] = to_date_string(df["rpt_date"])

    start_date = query_date - timedelta(days=lookback_days - 1)
    ann = pd.to_datetime(df["ann_date"], errors="coerce").dt.date
    df = df[(ann >= start_date) & (ann <= query_date)].copy()
    if not df.empty:
        df["reason"] = df["reason"].where(df["reason"].notna(), df.get("summary"))
    return df, "wss profitnotice", options


def fetch_forecast_by_wset(w, start_date, end_date, rpt_date, strict):
    params = {
        "start_date": start_date.strftime("%Y-%m-%d"),
        "end_date": end_date.strftime("%Y-%m-%d"),
        "rpt_date": rpt_date.strftime("%Y-%m-%d"),
        "start_date_nodash": nodash(start_date),
        "end_date_nodash": nodash(end_date),
        "rpt_date_nodash": nodash(rpt_date),
    }
    errors = []
    for candidate in WSET_CANDIDATES:
        for template in candidate["option_templates"]:
            options = template.format(**params)
            data = w.wset(candidate["name"], options)
            if data.ErrorCode != 0:
                errors.append(f"{candidate['name']} | {options} | ErrorCode={data.ErrorCode} | {getattr(data, 'Data', None)}")
                continue
            df = wind_data_to_df(data)
            if df.empty:
                if strict:
                    errors.append(f"{candidate['name']} | {options} | 成功但无数据")
                    continue
                return df, candidate["name"], options, errors
            return df, candidate["name"], options, errors
    msg = "未能从 Wind WSET 读取业绩预告。\n\n已尝试:\n" + "\n".join(errors[:30])
    if len(errors) > 30:
        msg += f"\n... 其余 {len(errors) - 30} 条省略"
    raise RuntimeError(msg)


def fetch_wss_enrichment(w, codes, trade_date):
    if not codes:
        return pd.DataFrame()
    result = pd.DataFrame({"wind_code": codes})
    for column, (field, option_template) in EXTRA_WSS_FIELDS.items():
        values = []
        options = option_template.format(trade_date=nodash(trade_date))
        for start in range(0, len(codes), 200):
            batch = codes[start:start + 200]
            data = w.wss(batch, field, options)
            ensure_wind_ok(data, f"wss {field}")
            values.extend(data.Data[0])
        result[column] = values
    return result


def build_table(w, query_date, lookback_days, rpt_date, strict):
    raw, source_name, source_options = fetch_profit_notice_by_wss(w, query_date, lookback_days, rpt_date)
    errors = []
    if raw.empty:
        return pd.DataFrame(columns=DISPLAY_COLUMNS), source_name, source_options, []

    df = normalize_columns(raw)
    df["wind_code"] = df["wind_code"].astype(str)
    df["ann_date"] = to_date_string(df["ann_date"])
    df["rpt_date"] = to_date_string(df["rpt_date"])
    if strict:
        df = df[df["ann_date"] == query_date.strftime("%Y-%m-%d")].copy()

    df = normalize_number_columns(df)
    enrich = fetch_wss_enrichment(w, df["wind_code"].drop_duplicates().tolist(), query_date)
    if not enrich.empty:
        df = df.merge(enrich, on="wind_code", how="left")
    df = normalize_number_columns(df)

    for column in DISPLAY_COLUMNS:
        if column not in df.columns:
            df[column] = pd.NA
    df = df[DISPLAY_COLUMNS].sort_values(["ann_date", "wind_code"], ascending=[False, True])
    return df, source_name, source_options, errors


def save_outputs(df, query_date):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"A股业绩预告跟踪_{query_date:%Y%m%d}"
    csv_path = OUTPUT_DIR / f"{stem}.csv"
    xlsx_path = OUTPUT_DIR / f"{stem}.xlsx"
    output_df = prepare_output_table(df)
    output_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        output_df.to_excel(writer, index=False, sheet_name="业绩预告")
        ws = writer.book["业绩预告"]
        ws.freeze_panes = "A2"
        for idx, column in enumerate(output_df.columns, start=1):
            width = min(max(len(str(column)) + 4, 12), 32)
            ws.column_dimensions[ws.cell(row=1, column=idx).column_letter].width = width
    return csv_path, xlsx_path


def main():
    parser = argparse.ArgumentParser(description="跟踪上一个工作日至查询日新披露的 A 股业绩预告。")
    parser.add_argument("--date", default=date.today().strftime("%Y-%m-%d"), help="查询日期，默认今天，格式 YYYY-MM-DD")
    parser.add_argument("--lookback-days", type=int, default=None, help="披露日回看天数；默认自动覆盖上一个工作日至查询日")
    parser.add_argument("--rpt-date", default=None, help="报告期，默认按查询日期自动推断，如 2026-06-30")
    parser.add_argument("--include-window", action="store_true", help="兼容旧参数；当前默认显示整个回看窗口")
    parser.add_argument("--no-save", action="store_true", help="只在终端显示，不保存 CSV/XLSX")
    args = parser.parse_args()

    query_date = parse_yyyymmdd(args.date)
    rpt_date = parse_yyyymmdd(args.rpt_date) if args.rpt_date else default_rpt_date(query_date)
    lookback_days = default_lookback_days(query_date) if args.lookback_days is None else args.lookback_days
    if lookback_days < 1:
        parser.error("--lookback-days 必须大于等于 1")
    strict = False

    from WindPy import w

    w.start()
    try:
        df, source_name, source_options, errors = build_table(w, query_date, lookback_days, rpt_date, strict)
    finally:
        w.close()

    print(f"Wind 数据源: {source_name}")
    print(f"Wind 参数: {source_options}")
    start_date = query_date - timedelta(days=lookback_days - 1)
    print(
        f"查询披露日: {start_date:%Y-%m-%d} 至 {query_date:%Y-%m-%d}"
        f" | 报告期: {rpt_date:%Y-%m-%d} | 公司数: {len(df)}"
    )
    if df.empty:
        print("没有找到符合条件的 A 股业绩预告。")
    else:
        print(format_for_display(df).to_string(index=False, max_colwidth=24))

    if not args.no_save:
        csv_path, xlsx_path = save_outputs(df, query_date)
        print(f"CSV: {csv_path}")
        print(f"Excel: {xlsx_path}")

    if errors:
        print(f"备注: 前面有 {len(errors)} 个 Wind 候选未命中，已自动使用第一个成功候选。")


if __name__ == "__main__":
    main()
