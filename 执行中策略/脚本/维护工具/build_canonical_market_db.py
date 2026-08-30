"""构建“原始行情 + 公司行为 + 复权因子”的权威行情层。

本脚本不会在未显式给出 ``--wind-cell-budget`` 时调用 Wind。历史补数可分批
执行；新模型只有通过完整审计并显式 ``--activate`` 后，正式策略才会切换读取。
"""

import argparse
import hashlib
import json
import os
import sqlite3
import time
from collections import defaultdict
from datetime import datetime

import pandas as pd

from local_market_db import (
    CANONICAL_A_SHARE_ACTIVATED_KEY,
    CANONICAL_A_SHARE_STATUS_KEY,
    COMPLETE_EOD_PRICE_FIELDS,
    MARKET_DB_PATH,
    connect,
    init_market_db,
)


UNIVERSE_NAME = "全部A股"
RAW_FIELDS = ["open", "high", "low", "close", "volume", "amt", "turn", "free_turn"]
REQUIRED_RAW_FIELDS = ["open", "high", "low", "close", "volume", "amt"]
FACTOR_FIELD = "adjfactor"
MODEL_DATASET = "canonical_prices:a_share"
DEFAULT_START_DATE = "2018-01-01"
FACTOR_CHANGE_TOLERANCE = 1e-12
DEFAULT_CORPORATE_ACTION_REPORT_PERIODS = 4
CORPORATE_ACTION_FIELDS = [
    "div_exdate",
    "div_cashbeforetax",
    "div_prelandate",
    "div_impdate",
    "div_capitalization",
    "div_stock",
    "div_recorddate",
    "div_paydate",
    "div_progress",
]
RIGHTS_ISSUE_FIELDS = [
    "rightsissue_price",
    "rightsissue_pershare",
    "rightsissue_progress",
    "rightsissue_baseshare",
    "rightsissue_amount",
    "rightsissue_exdividenddate",
    "rightsissue_listeddate",
    "rightsissue_payenddate",
    "rightsissue_anncedate",
]
ACCEPTED_FACTOR_EVENT_AUDIT_STATUSES = {"matched", "reviewed_other"}


def now_text():
    return datetime.now().isoformat(timespec="seconds")


def set_metadata(conn, key, value):
    conn.execute(
        """
        INSERT INTO metadata (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (key, str(value), now_text()),
    )


def get_metadata(conn, key):
    row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_quality(conn, status, details, checked=False):
    timestamp = now_text()
    conn.execute(
        """
        INSERT INTO market_data_quality (
            dataset, status, checked_at, details, updated_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(dataset) DO UPDATE SET
            status = excluded.status,
            checked_at = excluded.checked_at,
            details = excluded.details,
            updated_at = excluded.updated_at
        """,
        (MODEL_DATASET, status, timestamp if checked else None, details, timestamp),
    )
    set_metadata(conn, CANONICAL_A_SHARE_STATUS_KEY, status)
    conn.commit()


def selected_codes(conn, start_date, end_date):
    rows = conn.execute(
        """
        SELECT DISTINCT wind_code
        FROM universe_constituents_snapshot
        WHERE universe_name = ?
          AND snapshot_date >= ?
          AND snapshot_date <= ?
        UNION
        SELECT wind_code
        FROM stock_universe
        WHERE universe_name = ?
        ORDER BY wind_code
        """,
        (UNIVERSE_NAME, start_date, end_date, UNIVERSE_NAME),
    ).fetchall()
    return [row[0] for row in rows]


def legacy_trade_dates(conn, start_date, end_date):
    return [
        row[0]
        for row in conn.execute(
            """
            SELECT DISTINCT trade_date
            FROM daily_prices
            WHERE adjusted = 'F'
              AND trade_date >= ?
              AND trade_date <= ?
            ORDER BY trade_date
            """,
            (start_date, end_date),
        ).fetchall()
    ]


def estimate_cells(conn, start_date, end_date, raw_fields, include_factor=True):
    codes = selected_codes(conn, start_date, end_date)
    trade_dates = legacy_trade_dates(conn, start_date, end_date)
    field_count = len(raw_fields) + int(include_factor)
    requested_upper_bound = len(codes) * len(trade_dates) * field_count
    legacy_rows = conn.execute(
        """
        SELECT COUNT(*)
        FROM daily_prices AS p
        JOIN (
            SELECT DISTINCT wind_code
            FROM universe_constituents_snapshot
            WHERE universe_name = ?
        ) AS u ON u.wind_code = p.wind_code
        WHERE p.adjusted = 'F'
          AND p.trade_date >= ?
          AND p.trade_date <= ?
          AND p.open IS NOT NULL AND p.high IS NOT NULL AND p.low IS NOT NULL
          AND p.close IS NOT NULL AND p.volume IS NOT NULL AND p.amt IS NOT NULL
        """,
        (UNIVERSE_NAME, start_date, end_date),
    ).fetchone()[0]
    expected_non_null = legacy_rows * field_count
    known_action_pairs = set()
    known_rights_issue_checks = set()
    if include_factor:
        for change in factor_changes_between(conn, start_date, end_date):
            matched_event = matching_corporate_action(
                conn, change["wind_code"], change["factor_change_date"]
            )
            audit_status = existing_factor_event_audit_status(
                conn, change["wind_code"], change["factor_change_date"]
            )
            if matched_event or audit_status in ACCEPTED_FACTOR_EVENT_AUDIT_STATUSES:
                continue
            for report_period in candidate_report_dates(
                change["factor_change_date"],
                DEFAULT_CORPORATE_ACTION_REPORT_PERIODS,
            ):
                known_action_pairs.add((change["wind_code"], report_period))
            if audit_status == "unexplained":
                known_rights_issue_checks.add(
                    (change["wind_code"], change["factor_change_date"])
                )
    known_action_cells = len(known_action_pairs) * len(CORPORATE_ACTION_FIELDS)
    known_rights_issue_cells = (
        len(known_rights_issue_checks) * len(RIGHTS_ISSUE_FIELDS)
    )
    return {
        "start_date": start_date,
        "end_date": end_date,
        "codes": len(codes),
        "trade_dates": len(trade_dates),
        "raw_fields": list(raw_fields),
        "include_factor": include_factor,
        "field_count": field_count,
        "wind_requested_cells_upper_bound": requested_upper_bound,
        "known_factor_change_action_cells": known_action_cells,
        "known_unexplained_rights_issue_cells": known_rights_issue_cells,
        "wind_requested_cells_with_known_actions": (
            requested_upper_bound + known_action_cells + known_rights_issue_cells
        ),
        "corporate_action_estimate_note": (
            "仅包含当前库已知的因子变化；本次新抓因子产生的新变化会在预算余额内动态查询。"
        ),
        "expected_non_null_cells_approx": expected_non_null,
        "legacy_rows": legacy_rows,
    }


def seed_non_price_from_legacy(conn, start_date, end_date):
    """复用不受价格复权影响的量价字段；OHLC 仍必须从 Wind 不复权口径重抓。"""
    timestamp = now_text()
    before = conn.total_changes
    conn.execute(
        """
        INSERT INTO raw_daily_prices (
            trade_date, wind_code, volume, amt, turn, free_turn,
            source, source_options, updated_at
        )
        SELECT
            p.trade_date, p.wind_code, p.volume, p.amt, p.turn, p.free_turn,
            'Wind-legacy-verified-non-price', 'copied-from-PriceAdj=F', ?
        FROM daily_prices AS p
        JOIN (
            SELECT DISTINCT wind_code
            FROM universe_constituents_snapshot
            WHERE universe_name = ?
        ) AS u ON u.wind_code = p.wind_code
        WHERE p.adjusted = 'F'
          AND p.trade_date >= ?
          AND p.trade_date <= ?
          AND (
              p.volume IS NOT NULL OR p.amt IS NOT NULL
              OR p.turn IS NOT NULL OR p.free_turn IS NOT NULL
          )
          AND 1 = 1
        ON CONFLICT(trade_date, wind_code) DO UPDATE SET
            volume = excluded.volume,
            amt = excluded.amt,
            turn = excluded.turn,
            free_turn = excluded.free_turn,
            source = excluded.source,
            source_options = excluded.source_options,
            updated_at = excluded.updated_at
        """,
        (timestamp, UNIVERSE_NAME, start_date, end_date),
    )
    conn.commit()
    return conn.total_changes - before


def parse_wsd_matrix(data):
    if data.ErrorCode != 0 or not getattr(data, "Times", None):
        message = ""
        if getattr(data, "Codes", None) == ["ErrorReport"] and getattr(data, "Data", None):
            message = str(data.Data[0][0])
        return pd.DataFrame(), int(data.ErrorCode), message
    if len(data.Data) == len(data.Codes):
        frame = pd.DataFrame(data.Data, index=data.Codes).T
        frame.index = pd.to_datetime(data.Times)
    elif len(data.Data) == len(data.Times):
        frame = pd.DataFrame(data.Data, index=pd.to_datetime(data.Times), columns=data.Codes)
    elif len(data.Times) == 1 and len(data.Data) == 1:
        frame = pd.DataFrame([data.Data[0]], index=pd.to_datetime(data.Times), columns=data.Codes)
    else:
        return pd.DataFrame(), -1, "Wind 返回矩阵维度无法识别"
    frame = frame.apply(pd.to_numeric, errors="coerce")
    frame = frame.sort_index().loc[:, ~frame.columns.duplicated()]
    return frame, 0, ""


def batch_status(conn, dataset, date_range, field, batch_start, batch_end):
    row = conn.execute(
        """
        SELECT status FROM fetch_batches
        WHERE dataset = ? AND trade_date = ? AND field_key = ?
          AND batch_start = ? AND batch_end = ?
        """,
        (dataset, date_range, field, batch_start, batch_end),
    ).fetchone()
    return row[0] if row else None


def save_batch_status(
    conn,
    dataset,
    date_range,
    field,
    batch_start,
    batch_end,
    status,
    non_null_count=0,
    error_code=None,
    error_message="",
):
    conn.execute(
        """
        INSERT INTO fetch_batches (
            dataset, trade_date, field_key, batch_start, batch_end,
            status, non_null_count, error_code, error_message, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(dataset, trade_date, field_key, batch_start, batch_end)
        DO UPDATE SET
            status = excluded.status,
            non_null_count = excluded.non_null_count,
            error_code = excluded.error_code,
            error_message = excluded.error_message,
            updated_at = excluded.updated_at
        """,
        (
            dataset,
            date_range,
            field,
            batch_start,
            batch_end,
            status,
            non_null_count,
            error_code,
            error_message,
            now_text(),
        ),
    )
    conn.commit()


def upsert_raw_field(conn, frame, field):
    if frame.empty:
        return 0
    long_df = frame.stack().rename(field).reset_index()
    long_df.columns = ["trade_date", "wind_code", field]
    long_df = long_df[long_df[field].notna()]
    if long_df.empty:
        return 0
    timestamp = now_text()
    rows = [
        (
            pd.Timestamp(row.trade_date).strftime("%Y-%m-%d"),
            row.wind_code,
            float(getattr(row, field)),
            timestamp,
        )
        for row in long_df.itertuples(index=False)
    ]
    conn.executemany(
        f"""
        INSERT INTO raw_daily_prices (
            trade_date, wind_code, {field}, source, source_options, updated_at
        ) VALUES (?, ?, ?, 'Wind', 'PriceAdj=U', ?)
        ON CONFLICT(trade_date, wind_code) DO UPDATE SET
            {field} = excluded.{field},
            source = excluded.source,
            source_options = excluded.source_options,
            updated_at = excluded.updated_at
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def upsert_factors(conn, frame):
    if frame.empty:
        return 0
    long_df = frame.stack().rename("adj_factor").reset_index()
    long_df.columns = ["trade_date", "wind_code", "adj_factor"]
    long_df = long_df[
        long_df["adj_factor"].notna() & (long_df["adj_factor"] > 0)
    ]
    timestamp = now_text()
    rows = [
        (
            pd.Timestamp(row.trade_date).strftime("%Y-%m-%d"),
            row.wind_code,
            float(row.adj_factor),
            timestamp,
        )
        for row in long_df.itertuples(index=False)
    ]
    conn.executemany(
        """
        INSERT INTO price_adjustment_factors (
            trade_date, wind_code, adj_factor, source, source_field, updated_at
        ) VALUES (?, ?, ?, 'Wind', 'adjfactor', ?)
        ON CONFLICT(trade_date, wind_code) DO UPDATE SET
            adj_factor = excluded.adj_factor,
            source = excluded.source,
            source_field = excluded.source_field,
            updated_at = excluded.updated_at
        """,
        rows,
    )
    latest_by_code = (
        long_df.sort_values("trade_date")
        .groupby("wind_code", as_index=False)
        .tail(1)
    )
    latest_rows = [
        (
            row.wind_code,
            pd.Timestamp(row.trade_date).strftime("%Y-%m-%d"),
            float(row.adj_factor),
            timestamp,
        )
        for row in latest_by_code.itertuples(index=False)
    ]
    conn.executemany(
        """
        INSERT INTO latest_adjustment_factors (
            wind_code, trade_date, adj_factor, source, updated_at
        ) VALUES (?, ?, ?, 'Wind', ?)
        ON CONFLICT(wind_code) DO UPDATE SET
            trade_date = excluded.trade_date,
            adj_factor = excluded.adj_factor,
            source = excluded.source,
            updated_at = excluded.updated_at
        WHERE excluded.trade_date >= latest_adjustment_factors.trade_date
        """,
        latest_rows,
    )
    conn.commit()
    return len(rows)


def date_value_to_string(value):
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def candidate_report_dates(change_date, lookback_periods):
    """返回公司行为可能对应的最近若干季度报告期。"""
    end_ts = pd.Timestamp(change_date)
    dates = []
    year = end_ts.year
    while len(dates) < lookback_periods:
        for month, day in [(12, 31), (9, 30), (6, 30), (3, 31)]:
            value = pd.Timestamp(year=year, month=month, day=day)
            if value <= end_ts:
                dates.append(value)
        year -= 1
    return [
        value.strftime("%Y%m%d")
        for value in sorted(set(dates), reverse=True)[:lookback_periods]
    ]


def factor_changes_between(conn, start_date, end_date):
    rows = conn.execute(
        """
        SELECT trade_date, wind_code, previous_adj_factor, adj_factor,
               factor_change_ratio
        FROM price_adjustment_factor_changes
        WHERE trade_date BETWEEN ? AND ?
          AND previous_adj_factor IS NOT NULL
          AND ABS(factor_change_ratio) > ?
        ORDER BY trade_date, wind_code
        """,
        (start_date, end_date, FACTOR_CHANGE_TOLERANCE),
    ).fetchall()
    return [
        {
            "factor_change_date": row[0],
            "wind_code": row[1],
            "previous_adj_factor": float(row[2]),
            "adj_factor": float(row[3]),
            "factor_change_ratio": float(row[4]),
        }
        for row in rows
    ]


def matching_corporate_action(conn, wind_code, factor_change_date):
    dividend = conn.execute(
        """
        SELECT rpt_date, ex_date
        FROM corporate_action_events
        WHERE wind_code = ? AND ex_date = ?
        ORDER BY
            CASE WHEN progress LIKE '实施%' OR progress = '实施' THEN 0 ELSE 1 END,
            rpt_date DESC
        LIMIT 1
        """,
        (wind_code, factor_change_date),
    ).fetchone()
    if dividend:
        return {
            "event_type": "dividend_or_bonus",
            "reference_date": dividend[0],
            "rpt_date": dividend[0],
            "ex_date": dividend[1],
        }
    rights_issue = conn.execute(
        """
        SELECT announcement_date, ex_date
        FROM rights_issue_events
        WHERE wind_code = ? AND ex_date = ?
        ORDER BY announcement_date DESC
        LIMIT 1
        """,
        (wind_code, factor_change_date),
    ).fetchone()
    if rights_issue:
        return {
            "event_type": "rights_issue",
            "reference_date": rights_issue[0],
            "rpt_date": None,
            "ex_date": rights_issue[1],
        }
    return None


def existing_factor_event_audit_status(conn, wind_code, factor_change_date):
    row = conn.execute(
        """
        SELECT status FROM factor_change_event_audit
        WHERE wind_code = ? AND factor_change_date = ?
        """,
        (wind_code, factor_change_date),
    ).fetchone()
    return row[0] if row else None


def save_factor_event_audit(
    conn,
    change,
    status,
    report_periods,
    matched_event=None,
    details=None,
):
    matched_rpt_date = matched_event.get("rpt_date") if matched_event else None
    matched_ex_date = matched_event.get("ex_date") if matched_event else None
    matched_event_type = matched_event.get("event_type") if matched_event else None
    matched_event_reference_date = (
        matched_event.get("reference_date") if matched_event else None
    )
    conn.execute(
        """
        INSERT INTO factor_change_event_audit (
            wind_code, factor_change_date, previous_adj_factor, adj_factor,
            factor_change_ratio, status, matched_rpt_date, matched_ex_date,
            matched_event_type, matched_event_reference_date,
            checked_report_periods, source, details, checked_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Wind', ?, ?)
        ON CONFLICT(wind_code, factor_change_date) DO UPDATE SET
            previous_adj_factor = excluded.previous_adj_factor,
            adj_factor = excluded.adj_factor,
            factor_change_ratio = excluded.factor_change_ratio,
            status = excluded.status,
            matched_rpt_date = excluded.matched_rpt_date,
            matched_ex_date = excluded.matched_ex_date,
            matched_event_type = excluded.matched_event_type,
            matched_event_reference_date = excluded.matched_event_reference_date,
            checked_report_periods = excluded.checked_report_periods,
            source = excluded.source,
            details = excluded.details,
            checked_at = excluded.checked_at
        """,
        (
            change["wind_code"],
            change["factor_change_date"],
            change["previous_adj_factor"],
            change["adj_factor"],
            change["factor_change_ratio"],
            status,
            matched_rpt_date,
            matched_ex_date,
            matched_event_type,
            matched_event_reference_date,
            json.dumps(report_periods, ensure_ascii=False),
            json.dumps(details or {}, ensure_ascii=False),
            now_text(),
        ),
    )
    conn.commit()


def save_corporate_action_events(conn, events):
    if not events:
        return 0
    timestamp = now_text()
    rows = [
        (
            event["wind_code"],
            event.get("sec_name"),
            event["rpt_date"],
            event["ex_date"],
            event.get("proposal_announcement_date"),
            event.get("implementation_announcement_date"),
            event.get("record_date"),
            event.get("pay_date"),
            event.get("cash_before_tax"),
            event.get("capitalization_ratio"),
            event.get("stock_dividend_ratio"),
            event.get("progress"),
            "Wind",
            ",".join(CORPORATE_ACTION_FIELDS),
            timestamp,
            timestamp,
        )
        for event in events
    ]
    conn.executemany(
        """
        INSERT INTO corporate_action_events (
            wind_code, sec_name, rpt_date, ex_date,
            proposal_announcement_date, implementation_announcement_date,
            record_date, pay_date, cash_before_tax, capitalization_ratio,
            stock_dividend_ratio, progress, source, source_fields,
            source_updated_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(wind_code, rpt_date, ex_date) DO UPDATE SET
            sec_name = excluded.sec_name,
            proposal_announcement_date = excluded.proposal_announcement_date,
            implementation_announcement_date = excluded.implementation_announcement_date,
            record_date = excluded.record_date,
            pay_date = excluded.pay_date,
            cash_before_tax = excluded.cash_before_tax,
            capitalization_ratio = excluded.capitalization_ratio,
            stock_dividend_ratio = excluded.stock_dividend_ratio,
            progress = excluded.progress,
            source = excluded.source,
            source_fields = excluded.source_fields,
            source_updated_at = excluded.source_updated_at,
            updated_at = excluded.updated_at
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def parse_corporate_action_response(data, codes, rpt_date, code_to_name):
    if data.ErrorCode != 0:
        return [], int(data.ErrorCode), str(getattr(data, "Data", ""))[:500]
    field_data = {
        str(field).lower(): values
        for field, values in zip(data.Fields, data.Data)
    }
    events = []
    for index, code in enumerate(codes):
        ex_values = field_data.get("div_exdate", [])
        ex_date = date_value_to_string(
            ex_values[index] if index < len(ex_values) else None
        )
        if not ex_date:
            continue

        def field_value(field):
            values = field_data.get(field, [])
            return values[index] if index < len(values) else None

        def numeric_value(field):
            value = field_value(field)
            return None if value is None or pd.isna(value) else float(value)

        events.append(
            {
                "wind_code": code,
                "sec_name": code_to_name.get(code),
                "rpt_date": pd.Timestamp(rpt_date).strftime("%Y-%m-%d"),
                "ex_date": ex_date,
                "proposal_announcement_date": date_value_to_string(
                    field_value("div_prelandate")
                ),
                "implementation_announcement_date": date_value_to_string(
                    field_value("div_impdate")
                ),
                "record_date": date_value_to_string(field_value("div_recorddate")),
                "pay_date": date_value_to_string(field_value("div_paydate")),
                "cash_before_tax": numeric_value("div_cashbeforetax"),
                "capitalization_ratio": numeric_value("div_capitalization"),
                "stock_dividend_ratio": numeric_value("div_stock"),
                "progress": field_value("div_progress"),
            }
        )
    return events, 0, ""


def parse_rights_issue_response(data, codes, code_to_name):
    if data.ErrorCode != 0:
        return [], int(data.ErrorCode), str(getattr(data, "Data", ""))[:500]
    field_data = {
        str(field).lower(): values
        for field, values in zip(data.Fields, data.Data)
    }
    events = []
    for index, code in enumerate(codes):
        def field_value(field):
            values = field_data.get(field, [])
            return values[index] if index < len(values) else None

        def numeric_value(field):
            value = field_value(field)
            return None if value is None or pd.isna(value) else float(value)

        ex_date = date_value_to_string(field_value("rightsissue_exdividenddate"))
        announcement_date = date_value_to_string(field_value("rightsissue_anncedate"))
        if not ex_date or not announcement_date:
            continue
        events.append(
            {
                "wind_code": code,
                "sec_name": code_to_name.get(code),
                "announcement_date": announcement_date,
                "ex_date": ex_date,
                "rights_issue_price": numeric_value("rightsissue_price"),
                "rights_issue_per_share": numeric_value("rightsissue_pershare"),
                "base_shares": numeric_value("rightsissue_baseshare"),
                "actual_issue_amount": numeric_value("rightsissue_amount"),
                "listed_date": date_value_to_string(field_value("rightsissue_listeddate")),
                "pay_end_date": date_value_to_string(field_value("rightsissue_payenddate")),
                "progress": field_value("rightsissue_progress"),
            }
        )
    return events, 0, ""


def save_rights_issue_events(conn, events):
    if not events:
        return 0
    timestamp = now_text()
    rows = [
        (
            event["wind_code"],
            event.get("sec_name"),
            event["announcement_date"],
            event["ex_date"],
            event.get("rights_issue_price"),
            event.get("rights_issue_per_share"),
            event.get("base_shares"),
            event.get("actual_issue_amount"),
            event.get("listed_date"),
            event.get("pay_end_date"),
            event.get("progress"),
            "Wind",
            ",".join(RIGHTS_ISSUE_FIELDS),
            timestamp,
            timestamp,
        )
        for event in events
    ]
    conn.executemany(
        """
        INSERT INTO rights_issue_events (
            wind_code, sec_name, announcement_date, ex_date,
            rights_issue_price, rights_issue_per_share, base_shares,
            actual_issue_amount, listed_date, pay_end_date, progress,
            source, source_fields, source_updated_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(wind_code, announcement_date, ex_date) DO UPDATE SET
            sec_name = excluded.sec_name,
            rights_issue_price = excluded.rights_issue_price,
            rights_issue_per_share = excluded.rights_issue_per_share,
            base_shares = excluded.base_shares,
            actual_issue_amount = excluded.actual_issue_amount,
            listed_date = excluded.listed_date,
            pay_end_date = excluded.pay_end_date,
            progress = excluded.progress,
            source = excluded.source,
            source_fields = excluded.source_fields,
            source_updated_at = excluded.source_updated_at,
            updated_at = excluded.updated_at
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def sync_factor_change_corporate_actions(
    w,
    conn,
    start_date,
    end_date,
    wind_cell_budget,
    report_period_lookback=DEFAULT_CORPORATE_ACTION_REPORT_PERIODS,
    batch_size=500,
):
    """仅对复权因子发生变化且尚未解释的股票拉取公司行为。"""
    changes = factor_changes_between(conn, start_date, end_date)
    result = {
        "factor_change_count": len(changes),
        "already_matched_count": 0,
        "queried_change_count": 0,
        "requested_cells": 0,
        "saved_event_count": 0,
        "saved_rights_issue_count": 0,
        "matched_count": 0,
        "unexplained_count": 0,
        "fetch_error_count": 0,
        "pending_budget_count": 0,
        "stopped_by_budget": False,
    }
    pending = []
    periods_by_change = {}
    for change in changes:
        key = (change["wind_code"], change["factor_change_date"])
        periods = candidate_report_dates(
            change["factor_change_date"], report_period_lookback
        )
        periods_by_change[key] = periods
        matched_event = matching_corporate_action(
            conn, change["wind_code"], change["factor_change_date"]
        )
        old_status = existing_factor_event_audit_status(
            conn, change["wind_code"], change["factor_change_date"]
        )
        if matched_event or old_status in ACCEPTED_FACTOR_EVENT_AUDIT_STATUSES:
            save_factor_event_audit(
                conn,
                change,
                old_status if old_status == "reviewed_other" else "matched",
                periods,
                matched_event=matched_event,
                details={"reused_existing_event_or_audit": True},
            )
            result["already_matched_count"] += 1
            continue
        pending.append(change)

    if not pending:
        result["matched_count"] = result["already_matched_count"]
        return result

    code_rows = conn.execute(
        """
        SELECT wind_code, MAX(sec_name)
        FROM stock_universe
        WHERE universe_name = ?
        GROUP BY wind_code
        """,
        (UNIVERSE_NAME,),
    ).fetchall()
    code_to_name = dict(code_rows)
    period_to_codes = defaultdict(set)
    unexplained_changes = []
    for change in pending:
        key = (change["wind_code"], change["factor_change_date"])
        for period in periods_by_change[key]:
            period_to_codes[period].add(change["wind_code"])

    pair_status = {}
    fields = ",".join(CORPORATE_ACTION_FIELDS)
    for rpt_date in sorted(period_to_codes):
        codes = sorted(period_to_codes[rpt_date])
        for batch_start in range(0, len(codes), batch_size):
            batch_codes = codes[batch_start:batch_start + batch_size]
            estimated_cells = len(batch_codes) * len(CORPORATE_ACTION_FIELDS)
            if result["requested_cells"] + estimated_cells > wind_cell_budget:
                result["stopped_by_budget"] = True
                for code in batch_codes:
                    pair_status[(code, rpt_date)] = "pending_budget"
                continue
            print(
                f"拉取 corporate_action: rptDate={rpt_date} "
                f"股票 {batch_start}-{batch_start + len(batch_codes)}，"
                f"预计 {estimated_cells:,} cells"
            )
            data = w.wss(batch_codes, fields, f"rptDate={rpt_date}")
            result["requested_cells"] += estimated_cells
            events, error_code, error_message = parse_corporate_action_response(
                data, batch_codes, rpt_date, code_to_name
            )
            if error_code:
                for code in batch_codes:
                    pair_status[(code, rpt_date)] = "fetch_error"
                continue
            result["saved_event_count"] += save_corporate_action_events(conn, events)
            for code in batch_codes:
                pair_status[(code, rpt_date)] = "success"

    for change in pending:
        key = (change["wind_code"], change["factor_change_date"])
        periods = periods_by_change[key]
        statuses = [pair_status.get((change["wind_code"], period)) for period in periods]
        matched_event = matching_corporate_action(
            conn, change["wind_code"], change["factor_change_date"]
        )
        if matched_event:
            status = "matched"
            result["matched_count"] += 1
        elif "fetch_error" in statuses:
            status = "fetch_error"
            result["fetch_error_count"] += 1
        elif any(value in {None, "pending_budget"} for value in statuses):
            status = "pending_budget"
            result["pending_budget_count"] += 1
        else:
            status = "unexplained"
            result["unexplained_count"] += 1
            unexplained_changes.append(change)
        save_factor_event_audit(
            conn,
            change,
            status,
            periods,
            matched_event=matched_event,
            details={"report_period_statuses": dict(zip(periods, statuses))},
        )

    # 现金分红/送转字段不能覆盖配股。仅对仍未解释的因子变化追加配股查询。
    rights_pair_status = {}
    changes_by_date = defaultdict(list)
    for change in unexplained_changes:
        changes_by_date[change["factor_change_date"]].append(change)
    rights_fields = ",".join(RIGHTS_ISSUE_FIELDS)
    for change_date in sorted(changes_by_date):
        date_changes = changes_by_date[change_date]
        codes = sorted({change["wind_code"] for change in date_changes})
        for batch_start in range(0, len(codes), batch_size):
            batch_codes = codes[batch_start:batch_start + batch_size]
            estimated_cells = len(batch_codes) * len(RIGHTS_ISSUE_FIELDS)
            if result["requested_cells"] + estimated_cells > wind_cell_budget:
                result["stopped_by_budget"] = True
                for code in batch_codes:
                    rights_pair_status[(code, change_date)] = "pending_budget"
                continue
            print(
                f"拉取 rights_issue: tradeDate={change_date.replace('-', '')} "
                f"股票 {batch_start}-{batch_start + len(batch_codes)}，"
                f"预计 {estimated_cells:,} cells"
            )
            data = w.wss(
                batch_codes,
                rights_fields,
                f"tradeDate={change_date.replace('-', '')}",
            )
            result["requested_cells"] += estimated_cells
            rights_events, error_code, error_message = parse_rights_issue_response(
                data, batch_codes, code_to_name
            )
            if error_code:
                for code in batch_codes:
                    rights_pair_status[(code, change_date)] = "fetch_error"
                continue
            result["saved_rights_issue_count"] += save_rights_issue_events(
                conn, rights_events
            )
            for code in batch_codes:
                rights_pair_status[(code, change_date)] = "success"

    for change in unexplained_changes:
        key = (change["wind_code"], change["factor_change_date"])
        rights_status = rights_pair_status.get(key)
        matched_event = matching_corporate_action(
            conn, change["wind_code"], change["factor_change_date"]
        )
        if matched_event:
            result["unexplained_count"] -= 1
            result["matched_count"] += 1
            final_status = "matched"
        elif rights_status == "pending_budget":
            result["unexplained_count"] -= 1
            result["pending_budget_count"] += 1
            final_status = "pending_budget"
        elif rights_status == "fetch_error":
            result["unexplained_count"] -= 1
            result["fetch_error_count"] += 1
            final_status = "fetch_error"
        else:
            final_status = "unexplained"
        save_factor_event_audit(
            conn,
            change,
            final_status,
            periods_by_change[key],
            matched_event=matched_event,
            details={
                "dividend_report_periods_checked": periods_by_change[key],
                "rights_issue_status": rights_status,
            },
        )
    result["queried_change_count"] = len(pending)
    result["matched_count"] += result["already_matched_count"]
    return result


def sync_corporate_actions_only(
    conn,
    start_date,
    end_date,
    wind_cell_budget,
    report_period_lookback=DEFAULT_CORPORATE_ACTION_REPORT_PERIODS,
    batch_size=500,
):
    if wind_cell_budget is None or wind_cell_budget <= 0:
        raise RuntimeError(
            "公司行为同步必须显式提供正数 --wind-cell-budget，未经确认不调用 Wind。"
        )
    from WindPy import w

    activated = str(
        get_metadata(conn, CANONICAL_A_SHARE_ACTIVATED_KEY) or "0"
    ).lower() in {"1", "true", "yes"}
    set_quality(
        conn,
        "updating" if activated else "building",
        f"正在对账 {start_date}~{end_date} 的复权因子变化与公司行为。",
    )
    w.start()
    try:
        result = sync_factor_change_corporate_actions(
            w,
            conn,
            start_date,
            end_date,
            wind_cell_budget,
            report_period_lookback=report_period_lookback,
            batch_size=batch_size,
        )
    finally:
        w.close()

    incomplete = (
        result["unexplained_count"]
        + result["fetch_error_count"]
        + result["pending_budget_count"]
    )
    if incomplete:
        set_quality(
            conn,
            "invalid" if activated else "building",
            json.dumps(result, ensure_ascii=False),
            checked=True,
        )
    elif activated:
        audit = audit_database(conn, start_date, end_date)
        unresolved = (
            audit["factor_change_audit_unexplained"]
            + audit["factor_change_audit_fetch_error"]
            + audit["factor_change_audit_pending_budget"]
            + audit["factor_change_audit_missing"]
        )
        if audit["missing_factor_rows"] or audit["incomplete_raw_rows"] or unresolved:
            set_quality(conn, "invalid", json.dumps(audit, ensure_ascii=False), checked=True)
        else:
            set_quality(conn, "ready", json.dumps(audit, ensure_ascii=False), checked=True)
    else:
        set_quality(
            conn,
            "building",
            "复权因子变化与公司行为对账通过；全量行情尚未完成，保持 building。",
            checked=True,
        )
    return result


def year_chunks(start_date, end_date):
    """按年切分日期区间，但从最新年份向历史年份返回。

    历史补数受 Wind 额度或限流影响时，优先保证近期数据完整；
    已成功批次仍使用原有年度断点键，不会因顺序调整而重抓。
    """
    first = pd.Timestamp(start_date)
    cursor = pd.Timestamp(end_date)
    while cursor >= first:
        chunk_start = max(pd.Timestamp(cursor.year, 1, 1), first)
        yield chunk_start.strftime("%Y-%m-%d"), cursor.strftime("%Y-%m-%d")
        cursor = chunk_start - pd.Timedelta(days=1)


def fetch_from_wind(
    conn,
    start_date,
    end_date,
    raw_fields,
    include_factor,
    batch_size,
    wind_cell_budget,
    retry_failed,
    sync_corporate_actions=True,
    corporate_action_report_periods=DEFAULT_CORPORATE_ACTION_REPORT_PERIODS,
    request_interval_seconds=0.0,
    stop_on_wind_error=False,
):
    if wind_cell_budget is None or wind_cell_budget <= 0:
        raise RuntimeError(
            "未提供正数 --wind-cell-budget，按准确性/额度约定不启动 Wind 补抓。"
        )
    from WindPy import w

    codes = selected_codes(conn, start_date, end_date)
    if not codes:
        raise RuntimeError("没有找到全部A股历史/当前股票池代码，无法补抓。")
    universe_version = hashlib.sha256("\n".join(codes).encode("utf-8")).hexdigest()[:16]
    activated = str(
        get_metadata(conn, CANONICAL_A_SHARE_ACTIVATED_KEY) or "0"
    ).lower() in {"1", "true", "yes"}
    set_quality(
        conn,
        "updating" if activated else "building",
        f"正在补抓 {start_date}~{end_date}；在完成校验前不得供正式策略读取。",
    )

    used_cells = 0
    success_batches = 0
    skipped_batches = 0
    failed_batches = 0
    stopped_by_budget = False
    stopped_by_wind_error = False
    last_wind_error = None
    last_request_at = None
    corporate_action_result = None
    w.start()
    try:
        for chunk_start, chunk_end in year_chunks(start_date, end_date):
            # 用工作日作为额度上界，避免数据库尚无未来交易日时低估请求量。
            estimated_days = max(
                1,
                len(pd.bdate_range(chunk_start, chunk_end)),
            )
            fields = [(field, "PriceAdj=U", "raw") for field in raw_fields]
            if include_factor:
                fields.append((FACTOR_FIELD, "", "factor"))
            date_range = f"{chunk_start}~{chunk_end}"
            for batch_start in range(0, len(codes), batch_size):
                batch_end = min(batch_start + batch_size, len(codes))
                pending_fields = []
                for field, options, target in fields:
                    dataset = f"canonical:{target}:{UNIVERSE_NAME}:{universe_version}"
                    old_status = batch_status(
                        conn, dataset, date_range, field, batch_start, batch_end
                    )
                    if old_status == "success" or (
                        old_status == "failed" and not retry_failed
                    ):
                        if old_status == "failed":
                            failed_batches += 1
                        skipped_batches += 1
                        continue
                    pending_fields.append((field, options, target, dataset))

                estimated_cells_per_field = (batch_end - batch_start) * estimated_days
                batch_estimated_cells = estimated_cells_per_field * len(pending_fields)
                if used_cells + batch_estimated_cells > wind_cell_budget:
                    stopped_by_budget = True
                    break

                batch_codes = codes[batch_start:batch_end]
                for field, options, target, dataset in pending_fields:
                    batch_codes = codes[batch_start:batch_end]
                    print(
                        f"拉取 {target}/{field}: {date_range} "
                        f"股票 {batch_start}-{batch_end}，"
                        f"预计 {estimated_cells_per_field:,} cells"
                    )
                    if last_request_at is not None and request_interval_seconds > 0:
                        remaining = request_interval_seconds - (
                            time.monotonic() - last_request_at
                        )
                        if remaining > 0:
                            time.sleep(remaining)
                    data = w.wsd(
                        batch_codes,
                        field,
                        chunk_start,
                        chunk_end,
                        options,
                    )
                    last_request_at = time.monotonic()
                    used_cells += estimated_cells_per_field
                    frame, error_code, error_message = parse_wsd_matrix(data)
                    if error_code != 0:
                        failed_batches += 1
                        save_batch_status(
                            conn,
                            dataset,
                            date_range,
                            field,
                            batch_start,
                            batch_end,
                            "failed",
                            error_code=error_code,
                            error_message=error_message,
                        )
                        last_wind_error = {
                            "date_range": date_range,
                            "field": field,
                            "batch_start": batch_start,
                            "batch_end": batch_end,
                            "error_code": error_code,
                            "error_message": error_message,
                        }
                        if stop_on_wind_error:
                            stopped_by_wind_error = True
                            break
                        continue
                    written = (
                        upsert_raw_field(conn, frame, field)
                        if target == "raw"
                        else upsert_factors(conn, frame)
                    )
                    save_batch_status(
                        conn,
                        dataset,
                        date_range,
                        field,
                        batch_start,
                        batch_end,
                        "success",
                        non_null_count=written,
                    )
                    success_batches += 1
                if stopped_by_wind_error:
                    break
            if stopped_by_budget or stopped_by_wind_error:
                break
        if include_factor and sync_corporate_actions and not stopped_by_wind_error:
            remaining_budget = max(0, wind_cell_budget - used_cells)
            corporate_action_result = sync_factor_change_corporate_actions(
                w,
                conn,
                start_date,
                end_date,
                remaining_budget,
                report_period_lookback=corporate_action_report_periods,
                batch_size=batch_size,
            )
            used_cells += corporate_action_result["requested_cells"]
            if corporate_action_result["stopped_by_budget"]:
                stopped_by_budget = True
    finally:
        w.close()

    result = {
        "wind_cell_budget": wind_cell_budget,
        "estimated_cells_used": used_cells,
        "success_batches": success_batches,
        "skipped_batches": skipped_batches,
        "failed_batches": failed_batches,
        "stopped_by_budget": stopped_by_budget,
        "stopped_by_wind_error": stopped_by_wind_error,
        "last_wind_error": last_wind_error,
        "corporate_action_sync": corporate_action_result,
    }
    corporate_action_incomplete = bool(
        corporate_action_result
        and (
            corporate_action_result["unexplained_count"]
            or corporate_action_result["fetch_error_count"]
            or corporate_action_result["pending_budget_count"]
        )
    )
    if (
        stopped_by_budget
        or stopped_by_wind_error
        or failed_batches
        or corporate_action_incomplete
    ):
        status = "invalid" if activated else "building"
        set_quality(conn, status, json.dumps(result, ensure_ascii=False))
    elif activated:
        audit = audit_database(conn, start_date, end_date)
        unresolved_factor_events = (
            audit["factor_change_audit_unexplained"]
            + audit["factor_change_audit_fetch_error"]
            + audit["factor_change_audit_pending_budget"]
            + audit["factor_change_audit_missing"]
        )
        if (
            audit["missing_factor_rows"]
            or audit["incomplete_raw_rows"]
            or unresolved_factor_events
        ):
            set_quality(conn, "invalid", json.dumps(audit, ensure_ascii=False), checked=True)
            raise RuntimeError("权威行情增量更新后校验失败，数据库已标记 invalid。")
        set_quality(conn, "ready", json.dumps(audit, ensure_ascii=False), checked=True)
    else:
        set_quality(
            conn,
            "building",
            "本批抓取及因子变化公司行为对账完成；全量审计和激活前仍不得供正式策略读取。",
        )
    return result


def audit_database(conn, start_date, end_date):
    required = " AND ".join(f"p.{field} IS NOT NULL" for field in REQUIRED_RAW_FIELDS)
    incomplete = " OR ".join(f"p.{field} IS NULL" for field in REQUIRED_RAW_FIELDS)
    any_required = " OR ".join(
        f"p.{field} IS NOT NULL" for field in REQUIRED_RAW_FIELDS
    )
    raw_rows = conn.execute(
        "SELECT COUNT(*) FROM raw_daily_prices WHERE trade_date BETWEEN ? AND ?",
        (start_date, end_date),
    ).fetchone()[0]
    complete_rows = conn.execute(
        f"""
        SELECT COUNT(*) FROM raw_daily_prices AS p
        WHERE p.trade_date BETWEEN ? AND ? AND {required}
        """,
        (start_date, end_date),
    ).fetchone()[0]
    incomplete_rows = conn.execute(
        f"""
        SELECT COUNT(*) FROM raw_daily_prices AS p
        WHERE p.trade_date BETWEEN ? AND ?
          AND ({any_required})
          AND ({incomplete})
        """,
        (start_date, end_date),
    ).fetchone()[0]
    missing_factor_rows = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM raw_daily_prices AS p
        LEFT JOIN price_adjustment_factors AS f
          ON f.trade_date = p.trade_date AND f.wind_code = p.wind_code
        WHERE p.trade_date BETWEEN ? AND ?
          AND {required}
          AND (f.adj_factor IS NULL OR f.adj_factor <= 0)
        """,
        (start_date, end_date),
    ).fetchone()[0]
    factor_rows = conn.execute(
        """
        SELECT COUNT(*) FROM price_adjustment_factors
        WHERE trade_date BETWEEN ? AND ? AND adj_factor > 0
        """,
        (start_date, end_date),
    ).fetchone()[0]
    complete_raw_codes = conn.execute(
        f"""
        SELECT COUNT(DISTINCT p.wind_code)
        FROM raw_daily_prices AS p
        WHERE p.trade_date BETWEEN ? AND ? AND {required}
        """,
        (start_date, end_date),
    ).fetchone()[0]
    latest_factor_codes = conn.execute(
        """
        SELECT COUNT(DISTINCT a.wind_code)
        FROM latest_adjustment_factors AS a
        WHERE a.adj_factor > 0
          AND EXISTS (
              SELECT 1 FROM raw_daily_prices AS p
              WHERE p.wind_code = a.wind_code
                AND p.trade_date BETWEEN ? AND ?
          )
        """,
        (start_date, end_date),
    ).fetchone()[0]
    legacy_complete = conn.execute(
        f"""
        SELECT COUNT(*) FROM daily_prices AS p
        JOIN (
            SELECT DISTINCT wind_code
            FROM universe_constituents_snapshot
            WHERE universe_name = ?
        ) AS u ON u.wind_code = p.wind_code
        WHERE p.adjusted = 'F'
          AND p.trade_date BETWEEN ? AND ?
          AND {required}
        """,
        (UNIVERSE_NAME, start_date, end_date),
    ).fetchone()[0]
    coverage = complete_rows / legacy_complete if legacy_complete else 0.0
    ranges = conn.execute(
        """
        SELECT MIN(trade_date), MAX(trade_date), COUNT(DISTINCT wind_code)
        FROM raw_daily_prices
        WHERE trade_date BETWEEN ? AND ?
        """,
        (start_date, end_date),
    ).fetchone()
    factor_change_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM price_adjustment_factor_changes
        WHERE trade_date BETWEEN ? AND ?
          AND previous_adj_factor IS NOT NULL
          AND ABS(factor_change_ratio) > ?
        """,
        (start_date, end_date, FACTOR_CHANGE_TOLERANCE),
    ).fetchone()[0]
    factor_change_audit_rows = conn.execute(
        """
        SELECT
            SUM(CASE WHEN a.status IN ('matched', 'reviewed_other') THEN 1 ELSE 0 END),
            SUM(CASE WHEN a.status = 'unexplained' THEN 1 ELSE 0 END),
            SUM(CASE WHEN a.status = 'fetch_error' THEN 1 ELSE 0 END),
            SUM(CASE WHEN a.status = 'pending_budget' THEN 1 ELSE 0 END),
            SUM(CASE WHEN a.status IS NULL THEN 1 ELSE 0 END)
        FROM price_adjustment_factor_changes AS c
        LEFT JOIN factor_change_event_audit AS a
          ON a.wind_code = c.wind_code
         AND a.factor_change_date = c.trade_date
        WHERE c.trade_date BETWEEN ? AND ?
          AND c.previous_adj_factor IS NOT NULL
          AND ABS(c.factor_change_ratio) > ?
        """,
        (start_date, end_date, FACTOR_CHANGE_TOLERANCE),
    ).fetchone()
    audit_matched, audit_unexplained, audit_fetch_error, audit_pending, audit_missing = [
        int(value or 0) for value in factor_change_audit_rows
    ]
    return {
        "start_date": start_date,
        "end_date": end_date,
        "raw_rows": raw_rows,
        "complete_raw_rows": complete_rows,
        "incomplete_raw_rows": incomplete_rows,
        "factor_rows": factor_rows,
        "complete_raw_codes": complete_raw_codes,
        "latest_factor_codes": latest_factor_codes,
        "missing_factor_rows": missing_factor_rows,
        "legacy_complete_rows": legacy_complete,
        "coverage_vs_legacy": coverage,
        "raw_min_date": ranges[0],
        "raw_max_date": ranges[1],
        "raw_codes": ranges[2],
        "factor_change_count": factor_change_count,
        "factor_change_audit_matched": audit_matched,
        "factor_change_audit_unexplained": audit_unexplained,
        "factor_change_audit_fetch_error": audit_fetch_error,
        "factor_change_audit_pending_budget": audit_pending,
        "factor_change_audit_missing": audit_missing,
    }


def activate_if_valid(conn, start_date, end_date):
    audit = audit_database(conn, start_date, end_date)
    sample_status = get_metadata(conn, "canonical_prices:a_share:wind_sample_validation")
    problems = []
    legacy_bounds = conn.execute(
        """
        SELECT MIN(p.trade_date), MAX(p.trade_date)
        FROM daily_prices AS p
        JOIN (
            SELECT DISTINCT wind_code
            FROM universe_constituents_snapshot
            WHERE universe_name = ?
        ) AS u ON u.wind_code = p.wind_code
        WHERE p.adjusted = 'F'
          AND p.open IS NOT NULL AND p.high IS NOT NULL AND p.low IS NOT NULL
          AND p.close IS NOT NULL AND p.volume IS NOT NULL AND p.amt IS NOT NULL
        """,
        (UNIVERSE_NAME,),
    ).fetchone()
    if legacy_bounds[0] and pd.Timestamp(start_date) > pd.Timestamp(legacy_bounds[0]):
        problems.append(f"激活范围未覆盖最早完整历史 {legacy_bounds[0]}")
    if legacy_bounds[1] and pd.Timestamp(end_date) < pd.Timestamp(legacy_bounds[1]):
        problems.append(f"激活范围未覆盖最新完整历史 {legacy_bounds[1]}")
    if audit["coverage_vs_legacy"] < 1.0:
        problems.append("相对 legacy 完整行覆盖率低于 100%")
    if audit["incomplete_raw_rows"]:
        problems.append(f"存在 {audit['incomplete_raw_rows']} 行不完整原始OHLCVA")
    if audit["missing_factor_rows"]:
        problems.append(f"存在 {audit['missing_factor_rows']} 行缺复权因子")
    if audit["latest_factor_codes"] < audit["complete_raw_codes"]:
        problems.append("部分有完整原始行情的股票缺少最新锚点因子")
    unresolved_factor_events = (
        audit["factor_change_audit_unexplained"]
        + audit["factor_change_audit_fetch_error"]
        + audit["factor_change_audit_pending_budget"]
        + audit["factor_change_audit_missing"]
    )
    if unresolved_factor_events:
        problems.append(
            f"存在 {unresolved_factor_events} 个复权因子变化尚未由公司行为解释或人工复核"
        )
    if sample_status != "passed":
        problems.append("尚未完成本地派生前复权与Wind PriceAdj=F抽样对数")
    if problems:
        set_quality(
            conn,
            "building",
            json.dumps({"audit": audit, "problems": problems}, ensure_ascii=False),
            checked=True,
        )
        raise RuntimeError("不能激活权威行情模型：" + "；".join(problems))
    set_metadata(conn, CANONICAL_A_SHARE_ACTIVATED_KEY, "1")
    set_quality(conn, "ready", json.dumps(audit, ensure_ascii=False), checked=True)
    conn.commit()
    return audit


def validate_against_wind(
    conn,
    start_date,
    end_date,
    wind_cell_budget,
    sample_code_count=20,
    sample_date_count=12,
    tolerance=0.000001,
):
    if wind_cell_budget is None or wind_cell_budget <= 0:
        raise RuntimeError(
            "抽样对数也必须显式提供正数 --wind-cell-budget，未经确认不调用 Wind。"
        )
    code_rows = conn.execute(
        """
        SELECT p.wind_code, COUNT(*) AS n
        FROM raw_daily_prices AS p
        JOIN price_adjustment_factors AS f
          ON f.trade_date = p.trade_date AND f.wind_code = p.wind_code
        WHERE p.trade_date BETWEEN ? AND ?
          AND p.close IS NOT NULL AND f.adj_factor > 0
        GROUP BY p.wind_code
        ORDER BY n DESC, p.wind_code
        LIMIT ?
        """,
        (start_date, end_date, sample_code_count),
    ).fetchall()
    codes = [row[0] for row in code_rows]
    date_rows = conn.execute(
        """
        SELECT DISTINCT trade_date
        FROM raw_daily_prices
        WHERE trade_date BETWEEN ? AND ? AND close IS NOT NULL
        ORDER BY trade_date
        """,
        (start_date, end_date),
    ).fetchall()
    all_dates = [row[0] for row in date_rows]
    if not codes or not all_dates:
        raise RuntimeError("权威表尚无足够数据，不能进行Wind抽样对数。")
    if len(all_dates) <= sample_date_count:
        sample_dates = all_dates
    else:
        positions = {
            round(i * (len(all_dates) - 1) / (sample_date_count - 1))
            for i in range(sample_date_count)
        }
        sample_dates = [all_dates[i] for i in sorted(positions)]
    estimated_cells = len(codes) * len(sample_dates)
    if estimated_cells > wind_cell_budget:
        raise RuntimeError(
            f"抽样对数预计需要 {estimated_cells:,} cells，超过本次明确额度 "
            f"{wind_cell_budget:,}；未调用 Wind。"
        )

    placeholders = ",".join("?" for _ in codes)
    anchor_rows = conn.execute(
        f"""
        SELECT wind_code, adj_factor
        FROM latest_adjustment_factors
        WHERE wind_code IN ({placeholders}) AND adj_factor > 0
        """,
        codes,
    ).fetchall()
    anchors = dict(anchor_rows)

    from WindPy import w

    comparisons = 0
    failures = []
    w.start()
    try:
        for trade_date in sample_dates:
            local_rows = conn.execute(
                f"""
                SELECT p.wind_code, p.close * f.adj_factor
                FROM raw_daily_prices AS p
                JOIN price_adjustment_factors AS f
                  ON f.trade_date = p.trade_date AND f.wind_code = p.wind_code
                WHERE p.trade_date = ?
                  AND p.wind_code IN ({placeholders})
                  AND p.close IS NOT NULL AND f.adj_factor > 0
                """,
                (trade_date, *codes),
            ).fetchall()
            local = {
                code: numerator / anchors[code]
                for code, numerator in local_rows
                if code in anchors and anchors[code]
            }
            data = w.wsd(codes, "close", trade_date, trade_date, "PriceAdj=F")
            frame, error_code, error_message = parse_wsd_matrix(data)
            if error_code != 0:
                failures.append(
                    {"trade_date": trade_date, "error_code": error_code, "message": error_message}
                )
                continue
            wind_values = frame.iloc[0].to_dict() if not frame.empty else {}
            for code in codes:
                local_value = local.get(code)
                wind_value = wind_values.get(code)
                if local_value is None or pd.isna(wind_value):
                    continue
                comparisons += 1
                denominator = max(abs(float(wind_value)), 1.0)
                relative_error = abs(local_value - float(wind_value)) / denominator
                if relative_error > tolerance:
                    failures.append(
                        {
                            "trade_date": trade_date,
                            "wind_code": code,
                            "local": local_value,
                            "wind": float(wind_value),
                            "relative_error": relative_error,
                        }
                    )
    finally:
        w.close()

    result = {
        "status": "passed" if comparisons and not failures else "failed",
        "codes": len(codes),
        "dates": len(sample_dates),
        "comparisons": comparisons,
        "estimated_cells_used": estimated_cells,
        "tolerance": tolerance,
        "failure_count": len(failures),
        "failure_examples": failures[:20],
    }
    set_metadata(
        conn,
        "canonical_prices:a_share:wind_sample_validation",
        result["status"],
    )
    set_metadata(
        conn,
        "canonical_prices:a_share:wind_sample_validation_details",
        json.dumps(result, ensure_ascii=False),
    )
    conn.commit()
    if result["status"] != "passed":
        set_quality(conn, "building", json.dumps(result, ensure_ascii=False), checked=True)
        raise RuntimeError("本地派生前复权与Wind抽样对数未通过。")
    return result


def main():
    parser = argparse.ArgumentParser(description="构建A股权威原始行情与复权因子库")
    parser.add_argument("--db-path", default=MARKET_DB_PATH)
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument("--end-date", default=datetime.today().strftime("%Y-%m-%d"))
    parser.add_argument("--estimate-only", action="store_true")
    parser.add_argument("--seed-non-price-from-legacy", action="store_true")
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument(
        "--sync-corporate-actions-only",
        action="store_true",
        help="不重抓行情，仅对账当前库已有的复权因子变化与Wind公司行为",
    )
    parser.add_argument("--raw-fields", nargs="*", default=RAW_FIELDS)
    parser.add_argument("--skip-factor", action="store_true")
    parser.add_argument(
        "--skip-corporate-actions",
        action="store_true",
        help="不按复权因子变化增量查询公司行为；仅用于明确的行情单项修复",
    )
    parser.add_argument(
        "--corporate-action-report-periods",
        type=int,
        default=DEFAULT_CORPORATE_ACTION_REPORT_PERIODS,
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--wind-cell-budget", type=int)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument(
        "--request-interval-seconds",
        type=float,
        default=0.0,
        help="两次 Wind 行情请求之间的最小间隔秒数",
    )
    parser.add_argument(
        "--stop-on-wind-error",
        action="store_true",
        help="Wind 首次返回错误时立即停止后续批次",
    )
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--validate-wind-sample", action="store_true")
    parser.add_argument("--sample-code-count", type=int, default=20)
    parser.add_argument("--sample-date-count", type=int, default=12)
    parser.add_argument("--activate", action="store_true")
    args = parser.parse_args()

    unknown_fields = sorted(set(args.raw_fields) - set(RAW_FIELDS))
    if unknown_fields:
        raise ValueError(f"不支持的原始行情字段：{unknown_fields}")
    if args.corporate_action_report_periods <= 0:
        raise ValueError("--corporate-action-report-periods 必须为正数")
    if args.request_interval_seconds < 0:
        raise ValueError("--request-interval-seconds 不能为负数")

    init_market_db(args.db_path)
    with connect(args.db_path) as conn:
        estimate = estimate_cells(
            conn,
            args.start_date,
            args.end_date,
            args.raw_fields,
            include_factor=not args.skip_factor,
        )
        print("额度估算：")
        print(json.dumps(estimate, ensure_ascii=False, indent=2))

        if args.estimate_only:
            return
        if args.seed_non_price_from_legacy:
            written = seed_non_price_from_legacy(conn, args.start_date, args.end_date)
            set_quality(
                conn,
                "building",
                f"已复用 legacy 非价格字段 {written:,} 行变更；OHLC与复权因子尚待补抓。",
            )
            print(f"已复用 legacy 非价格字段：{written:,} 行变更")
        if args.fetch:
            result = fetch_from_wind(
                conn,
                args.start_date,
                args.end_date,
                args.raw_fields,
                include_factor=not args.skip_factor,
                batch_size=args.batch_size,
                wind_cell_budget=args.wind_cell_budget,
                retry_failed=args.retry_failed,
                sync_corporate_actions=not args.skip_corporate_actions,
                corporate_action_report_periods=args.corporate_action_report_periods,
                request_interval_seconds=args.request_interval_seconds,
                stop_on_wind_error=args.stop_on_wind_error,
            )
            print("本批补抓结果：")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            completion = audit_database(conn, args.start_date, args.end_date)
            print("补抓后完成度：")
            print(json.dumps(completion, ensure_ascii=False, indent=2))
        if args.sync_corporate_actions_only:
            action_result = sync_corporate_actions_only(
                conn,
                args.start_date,
                args.end_date,
                args.wind_cell_budget,
                report_period_lookback=args.corporate_action_report_periods,
                batch_size=args.batch_size,
            )
            print("复权因子变化与公司行为对账：")
            print(json.dumps(action_result, ensure_ascii=False, indent=2))
        if args.audit:
            audit = audit_database(conn, args.start_date, args.end_date)
            print("完整性审计：")
            print(json.dumps(audit, ensure_ascii=False, indent=2))
        if args.validate_wind_sample:
            validation = validate_against_wind(
                conn,
                args.start_date,
                args.end_date,
                args.wind_cell_budget,
                sample_code_count=args.sample_code_count,
                sample_date_count=args.sample_date_count,
            )
            print("Wind前复权抽样对数：")
            print(json.dumps(validation, ensure_ascii=False, indent=2))
        if args.activate:
            audit = activate_if_valid(conn, args.start_date, args.end_date)
            print("权威行情模型已激活：")
            print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
