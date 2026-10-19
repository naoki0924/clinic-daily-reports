"""初期データの登録とルールの読み出し。"""
from __future__ import annotations

import copy

from sqlalchemy import func, select

from .db import init_db, session_scope
from .models import Clinic, MetricDef, ParseRule, Setting
from .rules import BODY_RULES, CLINICS, EXCEL_RULES, METRICS

DEFAULT_SETTINGS = {
    "schedule": {"enabled": True, "hour": 0, "minute": 10, "recheck_days": 31},
    "collection": {"start_date": "2026-09-01"},
    "aggregation": {"fiscal_year_start_month": 1, "month_start_day": 1},
    "notify": {"emails": [], "webhook_url": "", "smtp": {}},
    "retention": {"raw_days": 730},
    "ai": {"enabled": False},
    "backup": {"enabled": True, "hour": 3, "keep": 14},
}


def _migrate() -> None:
    """既存DBに後から追加した列を足す（軽量マイグレーション）。"""
    from sqlalchemy import inspect, text

    from .db import engine
    cols = {c["name"] for c in inspect(engine).get_columns("clinic")}
    if "open_from" not in cols:
        with engine.begin() as con:
            con.execute(text("ALTER TABLE clinic ADD COLUMN open_from DATE"))


def seed() -> None:
    init_db()
    _migrate()
    with session_scope() as s:
        for c in CLINICS:
            if not s.scalar(select(Clinic).where(Clinic.code == c["code"])):
                s.add(Clinic(**c, closed_weekdays=[], closed_dates=[], open_dates=[]))
        for i, m in enumerate(METRICS):
            row = s.get(MetricDef, m["code"])
            data = {"kind": "flow", "fee_basis": "na", "internal_tx": "na", "description": "", **m, "sort_order": i}
            if row is None:
                s.add(MetricDef(**data))
            else:  # 名称・説明はコードの定義に追随
                for k, v in data.items():
                    setattr(row, k, v)
        for code in BODY_RULES:
            for kind, src in (("body", BODY_RULES), ("excel", EXCEL_RULES)):
                exists = s.scalar(select(ParseRule).where(ParseRule.clinic_code == code, ParseRule.kind == kind))
                if not exists:
                    s.add(ParseRule(clinic_code=code, kind=kind, version=1, rule=copy.deepcopy(src[code]),
                                    active=True, note="初版（2026-10-19 実データで確認）"))
        # コード側の読み取りルールが更新されていれば新しい版として登録する。
        # 管理画面で変更されたルール（created_by が system 以外）は上書きしない。
        for code in BODY_RULES:
            for kind, src in (("body", BODY_RULES), ("excel", EXCEL_RULES)):
                cur = s.scalar(select(ParseRule).where(ParseRule.clinic_code == code, ParseRule.kind == kind,
                                                       ParseRule.active.is_(True)).order_by(ParseRule.version.desc()))
                if cur is not None and cur.created_by == "system" and cur.rule != src[code]:
                    s.flush()
                    save_rule(s, code, kind, copy.deepcopy(src[code]), "system", "コードのルール更新（起動時に自動登録）")
        for key, value in DEFAULT_SETTINGS.items():
            if not s.get(Setting, key):
                s.add(Setting(key=key, value=value))


def active_rule(s, clinic_code: str, kind: str) -> tuple[int, dict]:
    row = s.scalar(select(ParseRule).where(ParseRule.clinic_code == clinic_code, ParseRule.kind == kind,
                                           ParseRule.active.is_(True)).order_by(ParseRule.version.desc()))
    if row is None:
        src = BODY_RULES if kind == "body" else EXCEL_RULES
        return 0, src[clinic_code]
    return row.version, row.rule


def save_rule(s, clinic_code: str, kind: str, rule: dict, user: str, note: str) -> int:
    ver = (s.scalar(select(func.max(ParseRule.version)).where(ParseRule.clinic_code == clinic_code,
                                                              ParseRule.kind == kind)) or 0) + 1
    for r in s.scalars(select(ParseRule).where(ParseRule.clinic_code == clinic_code, ParseRule.kind == kind)):
        r.active = False
    s.add(ParseRule(clinic_code=clinic_code, kind=kind, version=ver, rule=rule, active=True, note=note, created_by=user))
    return ver


def get_setting(s, key: str) -> dict:
    row = s.get(Setting, key)
    base = copy.deepcopy(DEFAULT_SETTINGS.get(key, {}))
    if row:
        base.update(row.value or {})
    return base


def put_setting(s, key: str, value: dict) -> None:
    row = s.get(Setting, key)
    if row:
        row.value = value
    else:
        s.add(Setting(key=key, value=value))
