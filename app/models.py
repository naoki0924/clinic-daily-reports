"""データベース設計。

観測値（observation）は「どこに何と書いてあったか」を出典つきで全部残す。
採用値（daily_fact）は 院×日×指標×区分 で1行に限り、集計はここだけを使う。
これにより本文とExcel、再投稿、再実行で二重計上されない。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text,
    UniqueConstraint, Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _now() -> datetime:
    return datetime.utcnow()


# ---- マスタ ----------------------------------------------------------------

class Clinic(Base):
    __tablename__ = "clinic"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    group_id: Mapped[int] = mapped_column(Integer)          # TalknoteノートID
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    closed_weekdays: Mapped[list] = mapped_column(JSON, default=list)  # 0=月 … 6=日
    closed_dates: Mapped[list] = mapped_column(JSON, default=list)     # "YYYY-MM-DD"
    open_dates: Mapped[list] = mapped_column(JSON, default=list)       # 定休日の臨時営業
    open_from: Mapped[date | None] = mapped_column(Date, nullable=True)  # 開院日（これより前は「開院前」。集計しない）


class MetricDef(Base):
    """指標の定義。合算してよいか・手数料の扱い等を明示して保存する。"""
    __tablename__ = "metric_def"
    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    unit: Mapped[str] = mapped_column(String(16))           # yen / count / person
    kind: Mapped[str] = mapped_column(String(16), default="flow")  # flow=日別に合算可 / balance=残高
    fee_basis: Mapped[str] = mapped_column(String(16), default="na")  # incl / excl / na
    internal_tx: Mapped[str] = mapped_column(String(16), default="na")  # incl / excl / na
    cross_clinic_sum: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str] = mapped_column(Text, default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class ParseRule(Base):
    """院別・書式別の読み取りルール（版管理）。変更後は過去分を再解析できる。"""
    __tablename__ = "parse_rule"
    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_code: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(16))           # body / excel
    version: Mapped[int] = mapped_column(Integer)
    rule: Mapped[dict] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    __table_args__ = (UniqueConstraint("clinic_code", "kind", "version"),)


class Setting(Base):
    __tablename__ = "setting"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class SecretStore(Base):
    """暗号化した秘密情報（Talknoteのログイン情報、セッション等）。平文は保存しない。"""
    __tablename__ = "secret_store"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class Target(Base):
    __tablename__ = "target"
    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_code: Mapped[str] = mapped_column(String(32))    # ALL=全院
    year: Mapped[int] = mapped_column(Integer)
    month: Mapped[int] = mapped_column(Integer)             # 0=年間
    metric: Mapped[str] = mapped_column(String(64))
    value: Mapped[float] = mapped_column(Float)
    __table_args__ = (UniqueConstraint("clinic_code", "year", "month", "metric"),)


# ---- 収集元 ----------------------------------------------------------------

class SourcePost(Base):
    __tablename__ = "source_post"
    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(Integer, unique=True)   # group_message_id
    clinic_code: Mapped[str] = mapped_column(String(32), index=True)
    group_id: Mapped[int] = mapped_column(Integer)
    url: Mapped[str] = mapped_column(String(255))
    posted_at: Mapped[datetime] = mapped_column(DateTime)        # JST（naive）
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    edited: Mapped[bool] = mapped_column(Boolean, default=False)
    comment_count: Mapped[int] = mapped_column(Integer, default=0)
    body_hash: Mapped[str] = mapped_column(String(64))
    body_enc: Mapped[bytes] = mapped_column(LargeBinary)          # 原文は暗号化して保持
    version: Mapped[int] = mapped_column(Integer, default=1)
    category: Mapped[str] = mapped_column(String(24), default="unclassified")  # daily / monthly / other_clinic / non_report
    category_reason: Mapped[str] = mapped_column(Text, default="")
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    purge_after: Mapped[date | None] = mapped_column(Date, nullable=True)
    files: Mapped[list["SourceFile"]] = relationship(back_populates="post")


class SourcePostVersion(Base):
    """投稿の編集履歴（本文ハッシュが変わるたびに1行）。"""
    __tablename__ = "source_post_version"
    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(Integer, index=True)
    version: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    body_hash: Mapped[str] = mapped_column(String(64))
    body_enc: Mapped[bytes] = mapped_column(LargeBinary)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class SourceComment(Base):
    __tablename__ = "source_comment"
    id: Mapped[int] = mapped_column(primary_key=True)
    comment_id: Mapped[int] = mapped_column(Integer, unique=True)
    post_id: Mapped[int] = mapped_column(Integer, index=True)
    posted_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    body_enc: Mapped[bytes] = mapped_column(LargeBinary)
    is_correction_hint: Mapped[bool] = mapped_column(Boolean, default=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class SourceFile(Base):
    __tablename__ = "source_file"
    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(Integer, unique=True)   # group_message_file_id
    post_pk: Mapped[int] = mapped_column(ForeignKey("source_post.id"))
    comment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attached_at: Mapped[datetime] = mapped_column(DateTime)       # 投稿 or コメントの日時（版の順序）
    file_name: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    storage_path: Mapped[str] = mapped_column(String(500), default="")  # 暗号化した原本
    status: Mapped[str] = mapped_column(String(24), default="pending")  # pending / stored / parsed / failed / skipped
    error: Mapped[str] = mapped_column(Text, default="")
    format_signature: Mapped[str] = mapped_column(String(64), default="")
    parsed_rule_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    post: Mapped[SourcePost] = relationship(back_populates="files")


# ---- 日報と値 --------------------------------------------------------------

class Report(Base):
    """院×対象日 の日報（1投稿＝1行）。対象日の判定根拠を保存する。"""
    __tablename__ = "report"
    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_code: Mapped[str] = mapped_column(String(32), index=True)
    target_date: Mapped[date | None] = mapped_column(Date, index=True, nullable=True)
    post_id: Mapped[int] = mapped_column(Integer, unique=True)
    primary_file_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    date_status: Mapped[str] = mapped_column(String(16))         # confirmed / review
    date_evidence: Mapped[dict] = mapped_column(JSON)
    relation: Mapped[str] = mapped_column(String(24), default="original")  # original / correction / repost / additional / review
    superseded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_closed_day: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


DIM_COLS = ("market", "cust", "category", "channel", "method")


class Observation(Base):
    """出典ごとの観測値。再解析すると同じ出典の行を置き換える（冪等）。"""
    __tablename__ = "observation"
    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_code: Mapped[str] = mapped_column(String(32))
    target_date: Mapped[date] = mapped_column(Date)
    metric: Mapped[str] = mapped_column(String(64))
    market: Mapped[str] = mapped_column(String(32), default="")
    cust: Mapped[str] = mapped_column(String(64), default="")
    category: Mapped[str] = mapped_column(String(64), default="")
    channel: Mapped[str] = mapped_column(String(32), default="")
    method: Mapped[str] = mapped_column(String(32), default="")
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    state: Mapped[str] = mapped_column(String(12))   # value / zero / blank / dash / absent / error
    raw: Mapped[str] = mapped_column(Text, default="")
    suspicious: Mapped[str] = mapped_column(Text, default="")
    source_kind: Mapped[str] = mapped_column(String(16))  # body / excel / excel_later / manual
    post_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    location: Mapped[str] = mapped_column(String(255), default="")   # 本文L12 / シート!R24
    rule_version: Mapped[int] = mapped_column(Integer, default=1)
    definition_confirmed: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    __table_args__ = (
        Index("ix_obs_key", "clinic_code", "target_date", "metric"),
        Index("ix_obs_src", "source_kind", "post_id", "file_id"),
    )


class DailyFact(Base):
    """採用値。院×日×指標×区分 で一意。集計はこのテーブルだけを使う。"""
    __tablename__ = "daily_fact"
    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_code: Mapped[str] = mapped_column(String(32))
    target_date: Mapped[date] = mapped_column(Date)
    metric: Mapped[str] = mapped_column(String(64))
    market: Mapped[str] = mapped_column(String(32), default="")
    cust: Mapped[str] = mapped_column(String(64), default="")
    category: Mapped[str] = mapped_column(String(64), default="")
    channel: Mapped[str] = mapped_column(String(32), default="")
    method: Mapped[str] = mapped_column(String(32), default="")
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    state: Mapped[str] = mapped_column(String(12))
    status: Mapped[str] = mapped_column(String(12))  # confirmed / provisional / estimated / review
    adopted_obs_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    adopted_by: Mapped[str] = mapped_column(String(64), default="auto")
    note: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
    __table_args__ = (
        UniqueConstraint("clinic_code", "target_date", "metric", "market", "cust", "category", "channel", "method",
                         name="uq_fact"),
        Index("ix_fact_date", "target_date"),
    )


class DayStatus(Base):
    __tablename__ = "day_status"
    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_code: Mapped[str] = mapped_column(String(32))
    target_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16))   # submitted / missing / closed / failed / review / future
    report_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
    __table_args__ = (UniqueConstraint("clinic_code", "target_date"),)


class CalendarFlag(Base):
    """Excelの「休」欄など、日報以外から分かった休診日。"""
    __tablename__ = "calendar_flag"
    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_code: Mapped[str] = mapped_column(String(32))
    target_date: Mapped[date] = mapped_column(Date)
    closed: Mapped[bool] = mapped_column(Boolean)
    source: Mapped[str] = mapped_column(String(255))
    __table_args__ = (UniqueConstraint("clinic_code", "target_date"),)


class Issue(Base):
    """要確認事項。同じ内容は dedupe_key で1件にまとめる（再実行で増えない）。"""
    __tablename__ = "issue"
    id: Mapped[int] = mapped_column(primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True)
    clinic_code: Mapped[str] = mapped_column(String(32), index=True)
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    kind: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(8), default="warn")  # info / warn / error
    title: Mapped[str] = mapped_column(String(255))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    post_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metric: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="open")  # open / resolved / ignored
    resolution: Mapped[str] = mapped_column(Text, default="")
    resolved_by: Mapped[str] = mapped_column(String(64), default="")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=_now)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ManualAdjustment(Base):
    __tablename__ = "manual_adjustment"
    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_code: Mapped[str] = mapped_column(String(32))
    target_date: Mapped[date] = mapped_column(Date)
    metric: Mapped[str] = mapped_column(String(64))
    dims: Mapped[dict] = mapped_column(JSON, default=dict)
    before_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    after_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    after_state: Mapped[str] = mapped_column(String(12), default="value")
    source_obs_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 採用値の選択の場合
    reason: Mapped[str] = mapped_column(Text)
    user: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Aggregate(Base):
    """月次（M）・年次（Y）の集計結果。日次データの更新に連動して再計算する。"""
    __tablename__ = "aggregate"
    id: Mapped[int] = mapped_column(primary_key=True)
    period_type: Mapped[str] = mapped_column(String(1))
    period: Mapped[str] = mapped_column(String(7))      # 2026-10 / 2026
    clinic_code: Mapped[str] = mapped_column(String(32))  # ALL=全院
    metric: Mapped[str] = mapped_column(String(64))
    dims_key: Mapped[str] = mapped_column(String(255), default="")
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    days_with_value: Mapped[int] = mapped_column(Integer, default=0)
    days_missing: Mapped[int] = mapped_column(Integer, default=0)
    days_review: Mapped[int] = mapped_column(Integer, default=0)
    provisional: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(Text, default="")
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    __table_args__ = (UniqueConstraint("period_type", "period", "clinic_code", "metric", "dims_key"),)


class AnalysisNote(Base):
    __tablename__ = "analysis_note"
    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(16))      # daily / monthly / yearly
    period: Mapped[str] = mapped_column(String(10))
    facts: Mapped[dict] = mapped_column(JSON)
    text_facts: Mapped[str] = mapped_column(Text)
    text_hypotheses: Mapped[str] = mapped_column(Text, default="")
    generator: Mapped[str] = mapped_column(String(32))  # rule / claude
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    __table_args__ = (UniqueConstraint("scope", "period"),)


# ---- 実行管理・権限 --------------------------------------------------------

class JobRun(Base):
    __tablename__ = "job_run"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(24))       # daily / manual / reparse / retry_failed / connection_test
    status: Mapped[str] = mapped_column(String(16))     # queued / running / success / partial / failed / reauth_required
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    requested_by: Mapped[str] = mapped_column(String(64), default="scheduler")
    queued_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")


class JobStep(Base):
    __tablename__ = "job_step"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("job_run.id"), index=True)
    step: Mapped[str] = mapped_column(String(48))
    clinic_code: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(16))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class JobLock(Base):
    __tablename__ = "job_lock"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    holder: Mapped[str] = mapped_column(String(64))
    acquired_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class Notification(Base):
    __tablename__ = "notification"
    id: Mapped[int] = mapped_column(primary_key=True)
    level: Mapped[str] = mapped_column(String(8))
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text, default="")
    channels: Mapped[dict] = mapped_column(JSON, default=dict)  # 送信結果
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    read: Mapped[bool] = mapped_column(Boolean, default=False)


class AppUser(Base):
    __tablename__ = "app_user"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16))   # admin / viewer_all / viewer_clinic
    clinic_codes: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    last_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    user: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(255), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    ip: Mapped[str] = mapped_column(String(64), default="")
