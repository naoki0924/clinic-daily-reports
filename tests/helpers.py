from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select

from app import aggregate
from app.models import Clinic, DailyFact, SourceComment, SourceFile, SourcePost
from app.pipeline import process_post, reconcile_day, refresh_days
from app.security import encrypt_text, sha256, store_file

_next = {"file": 9000}


def add_post(s, clinic: str, post_id: int, posted_at: datetime, body: str, excel: bytes | None = None,
             file_name: str = "日報.xlsx", comment: str | None = None, comment_excel: bytes | None = None) -> SourcePost:
    gid = s.scalar(select(Clinic.group_id).where(Clinic.code == clinic))
    p = s.scalar(select(SourcePost).where(SourcePost.post_id == post_id))
    if p is None:
        p = SourcePost(post_id=post_id, clinic_code=clinic, group_id=gid, url=f"https://example.test/{post_id}",
                       posted_at=posted_at, updated_at=posted_at, body_hash=sha256(body.encode()),
                       body_enc=encrypt_text(body))
        s.add(p)
    else:
        p.body_enc, p.body_hash, p.version, p.updated_at = encrypt_text(body), sha256(body.encode()), p.version + 1, posted_at
    s.flush()
    if excel is not None:
        _add_file(s, p, excel, file_name, posted_at, None)
    if comment is not None:
        cid = post_id * 10 + 1
        from app.pipeline import is_correction_comment
        s.add(SourceComment(comment_id=cid, post_id=post_id, posted_at=posted_at + timedelta(hours=3),
                            updated_at=posted_at + timedelta(hours=3), body_enc=encrypt_text(comment),
                            is_correction_hint=is_correction_comment(comment)))
        if comment_excel is not None:
            _add_file(s, p, comment_excel, file_name, posted_at + timedelta(hours=3), cid)
    s.flush()
    return p


def _add_file(s, p, data, name, at, cid):
    _next["file"] += 1
    fid = _next["file"]
    s.add(SourceFile(file_id=fid, post_pk=p.id, comment_id=cid, attached_at=at, file_name=name, size=len(data),
                     sha256=sha256(data), storage_path=store_file(f"file_{fid}", data), status="stored"))


def run(s, posts, days, today=date(2026, 10, 20)):
    affected = set()
    for p in posts:
        affected |= process_post(s, p)
    s.flush()
    for c, d in sorted(affected | set(days)):
        reconcile_day(s, c, d)
    clinics = {c for c, _ in affected | set(days)}
    for c in clinics:
        ds = [d for cc, d in affected | set(days) if cc == c]
        refresh_days(s, c, min(ds), max(ds), today)
    aggregate.recompute(s, {(d.year, d.month) for _, d in affected | set(days)})
    s.flush()
    return affected


def fact(s, clinic, d, metric, **dims):
    q = select(DailyFact).where(DailyFact.clinic_code == clinic, DailyFact.target_date == d, DailyFact.metric == metric)
    for k in ("market", "cust", "category", "channel", "method"):
        q = q.where(getattr(DailyFact, k) == dims.get(k, ""))
    return s.scalar(q)
