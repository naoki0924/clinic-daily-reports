"""投稿・コメント・添付の差分収集と保存。

- 投稿ID・URL・投稿日時・更新日時・取得日時・添付ファイルIDを管理する
- 本文が変わったら版を記録し、再解析の対象にする（同じ投稿を二重に登録しない）
- 添付は内容のハッシュ（SHA-256）で同一性を判定する。同じファイル名でも中身が違えば別の版
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select

from ..models import SourceComment, SourceFile, SourcePost, SourcePostVersion
from ..pipeline import EXCEL_EXT, is_correction_comment
from ..security import encrypt_text, sha256, store_file
from .talknote import SessionExpired, TalknoteClient, TalknoteError

log = logging.getLogger("collector")
PAGE = 20
MAX_PAGES = 200


@dataclass
class CollectStats:
    clinic: str
    fetched: int = 0
    new_posts: int = 0
    updated_posts: int = 0
    comments: int = 0
    files_downloaded: int = 0
    files_failed: int = 0
    changed_post_ids: set[int] = field(default_factory=set)
    oldest_seen: str = ""
    errors: list[str] = field(default_factory=list)
    complete: bool = False

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["changed_post_ids"] = sorted(self.changed_post_ids)
        return d


def _dt(s: str) -> datetime:
    """Talknoteの日時（JST、'2026-10-19 18:53:39.693'）→ naive datetime（JST）。"""
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")


def collect_clinic(s, client: TalknoteClient, clinic_code: str, group_id: int, since: date,
                   updated_since: datetime | None = None, retention_days: int = 730) -> CollectStats:
    st = CollectStats(clinic_code)
    since_dt = datetime.combine(since, datetime.min.time())
    seen_ids: set[int] = set()

    # 1) 投稿日時の新しい順に、対象期間の始まりまでさかのぼる（ページ送り）
    for page in range(MAX_PAGES):
        msgs = client.list_posts(group_id, offset=page * PAGE, limit=PAGE, sort_key="regist_date")
        if not msgs:
            st.complete = True
            break
        stop = False
        for m in msgs:
            if _dt(m["regist_date"]) < since_dt:
                stop = True
                continue
            seen_ids.add(m["group_message_id"])
            _upsert_post(s, client, clinic_code, group_id, m, st, retention_days)
        st.oldest_seen = msgs[-1]["regist_date"]
        if stop or len(msgs) < PAGE:
            st.complete = True
            break

    # 2) 期間外でも、前回以降に更新（編集）された投稿を拾う
    if updated_since:
        for page in range(20):
            msgs = client.list_posts(group_id, offset=page * PAGE, limit=PAGE, sort_key="update_date")
            done = False
            for m in msgs:
                if _dt(m["update_date"]) < updated_since:
                    done = True
                    break
                if m["group_message_id"] not in seen_ids:
                    _upsert_post(s, client, clinic_code, group_id, m, st, retention_days)
            if done or len(msgs) < PAGE:
                break

    # 3) 期間内でDBにあるのに一覧に出なかった投稿は削除扱い（一覧を最後まで読めた場合のみ）
    if st.complete:
        for p in s.scalars(select(SourcePost).where(SourcePost.clinic_code == clinic_code,
                                                    SourcePost.posted_at >= since_dt, SourcePost.deleted.is_(False))):
            if p.post_id not in seen_ids:
                p.deleted = True
                st.changed_post_ids.add(p.post_id)
    return st


def _upsert_post(s, client: TalknoteClient, clinic_code: str, group_id: int, m: dict, st: CollectStats,
                 retention_days: int) -> None:
    st.fetched += 1
    pid = m["group_message_id"]
    if m.get("group_id") and int(m["group_id"]) != int(group_id):
        return  # 共有投稿など他ノートのもの（全社・タイムラインへの重複は収集しない）
    body = m.get("message") or ""
    h = sha256(body.encode("utf-8"))
    posted, updated = _dt(m["regist_date"]), _dt(m["update_date"])
    row = s.scalar(select(SourcePost).where(SourcePost.post_id == pid))
    changed = False
    if row is None:
        row = SourcePost(post_id=pid, clinic_code=clinic_code, group_id=group_id, url=client.post_url(group_id, pid),
                         posted_at=posted, updated_at=updated, edited=bool(m.get("edited")),
                         comment_count=int(m.get("comment_count") or 0), body_hash=h, body_enc=encrypt_text(body),
                         version=1, purge_after=(posted + timedelta(days=retention_days)).date())
        s.add(row)
        s.flush()
        s.add(SourcePostVersion(post_id=pid, version=1, updated_at=updated, body_hash=h, body_enc=row.body_enc))
        st.new_posts += 1
        changed = True
    else:
        row.fetched_at = datetime.utcnow()
        if row.deleted:
            row.deleted = False
            changed = True
        if h != row.body_hash:
            row.version += 1
            row.body_hash, row.body_enc = h, encrypt_text(body)
            s.add(SourcePostVersion(post_id=pid, version=row.version, updated_at=updated, body_hash=h, body_enc=row.body_enc))
            st.updated_posts += 1
            changed = True
        row.updated_at, row.edited = updated, bool(m.get("edited"))

    cc = int(m.get("comment_count") or 0)
    if cc and (changed or cc != row.comment_count or not s.scalar(select(SourceComment.id).where(SourceComment.post_id == pid).limit(1))):
        for c in client.list_comments(group_id, pid):
            if _upsert_comment(s, row, c):
                changed = True
            for f in c.get("attach_file") or []:
                if _register_file(s, row, f, _dt(c["regist_date"]), c.get("group_message_comment_id")):
                    changed = True
            st.comments += 1
    row.comment_count = cc
    for f in m.get("attach_file") or []:
        if _register_file(s, row, f, posted, None):
            changed = True

    for f in s.scalars(select(SourceFile).where(SourceFile.post_pk == row.id, SourceFile.status.in_(("pending", "failed_fetch")))):
        if f.attempts >= 5:
            continue
        try:
            data = client.download(group_id, f.file_id)
            f.size, f.sha256 = len(data), sha256(data)
            dup = s.scalar(select(SourceFile).where(SourceFile.sha256 == f.sha256, SourceFile.id != f.id,
                                                    SourceFile.storage_path != ""))
            f.storage_path = dup.storage_path if dup else store_file(f"file_{f.file_id}", data)
            f.status, f.error, f.fetched_at = "stored", "", datetime.utcnow()
            st.files_downloaded += 1
            changed = True
        except SessionExpired:
            raise
        except TalknoteError as e:
            f.attempts += 1
            f.status, f.error = "failed_fetch", str(e)[:500]
            st.files_failed += 1
            st.errors.append(f"添付 {f.file_name}: {e}")
            changed = True
    if changed:
        st.changed_post_ids.add(pid)


def _upsert_comment(s, post: SourcePost, c: dict) -> bool:
    cid = c["group_message_comment_id"]
    text = c.get("message_com") or ""
    row = s.scalar(select(SourceComment).where(SourceComment.comment_id == cid))
    enc = encrypt_text(text)
    if row is None:
        s.add(SourceComment(comment_id=cid, post_id=post.post_id, posted_at=_dt(c["regist_date"]),
                            updated_at=_dt(c["update_date"]), body_enc=enc, is_correction_hint=is_correction_comment(text)))
        return True
    if _dt(c["update_date"]) != row.updated_at:
        row.updated_at, row.body_enc = _dt(c["update_date"]), enc
        row.is_correction_hint = is_correction_comment(text)
        return True
    return False


def _register_file(s, post: SourcePost, f: dict, attached_at: datetime, comment_id: int | None) -> bool:
    fid = f.get("group_message_file_id")
    if not fid:
        return False
    if s.scalar(select(SourceFile.id).where(SourceFile.file_id == fid)):
        return False
    name = f.get("file_file_name") or ""
    status = "pending" if name.lower().endswith(EXCEL_EXT) else "skipped"
    s.add(SourceFile(file_id=fid, post_pk=post.id, comment_id=comment_id, attached_at=attached_at, file_name=name,
                     status=status))
    s.flush()
    return True
