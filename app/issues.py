"""要確認事項の登録。dedupe_key で1件にまとめるので、再実行しても増えない。"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select

from .models import Issue

KIND_LABELS = {
    "date_review": "対象日の要確認",
    "body_excel_mismatch": "本文とExcelの差異",
    "excel_later_change": "後日のExcelで値が変更",
    "duplicate_report": "同じ日の複数投稿",
    "inconsistency": "合計と内訳の不整合",
    "suspicious_value": "誤記の疑い",
    "excel_format": "Excel書式変更・読取失敗",
    "excel_formula": "数式の計算結果の問題",
    "excel_missing": "Excel添付なし",
    "fetch_failed": "取得失敗",
    "comment_correction": "コメントでの訂正指摘",
    "other_clinic": "他院の日報の混在",
    "definition": "定義の未確認",
    "missing_report": "未提出",
    "auth": "ログイン・認証",
}


class IssueTracker:
    """ある範囲（scope_prefix）を処理するときに使う。触れなかった自動の指摘は解消扱いにする。"""

    def __init__(self, session, scope_prefix: str):
        self.s = session
        self.prefix = scope_prefix
        self.touched: set[str] = set()

    def add(self, key_suffix: str, *, clinic: str, kind: str, title: str, target_date: date | None = None,
            severity: str = "warn", detail: dict | None = None, post_id: int | None = None,
            file_id: int | None = None, metric: str | None = None) -> Issue:
        key = f"{self.prefix}{key_suffix}"
        self.touched.add(key)
        return upsert_issue(self.s, key, clinic=clinic, kind=kind, title=title, target_date=target_date,
                            severity=severity, detail=detail, post_id=post_id, file_id=file_id, metric=metric)

    def close_untouched(self) -> int:
        rows = self.s.scalars(select(Issue).where(Issue.dedupe_key.like(f"{self.prefix}%"),
                                                  Issue.status == "open")).all()
        n = 0
        for row in rows:
            if row.dedupe_key not in self.touched:
                row.status = "resolved"
                row.resolution = "再処理で解消（自動）"
                row.resolved_by = "system"
                row.resolved_at = datetime.utcnow()
                n += 1
        return n


def upsert_issue(s, key: str, *, clinic: str, kind: str, title: str, target_date: date | None = None,
                 severity: str = "warn", detail: dict | None = None, post_id: int | None = None,
                 file_id: int | None = None, metric: str | None = None) -> Issue:
    row = s.scalar(select(Issue).where(Issue.dedupe_key == key))
    if row is None:
        row = Issue(dedupe_key=key, clinic_code=clinic, kind=kind, title=title, target_date=target_date,
                    severity=severity, detail=detail or {}, post_id=post_id, file_id=file_id, metric=metric)
        s.add(row)
        s.flush()
        return row
    row.title, row.severity, row.detail = title, severity, detail or {}
    row.target_date, row.post_id, row.file_id, row.metric = target_date, post_id, file_id, metric
    row.last_seen = datetime.utcnow()
    if row.status == "resolved" and row.resolved_by == "system":
        row.status = "open"  # 自動解消後に再発
        row.resolution = ""
    return row
