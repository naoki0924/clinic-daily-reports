"""コマンドライン操作。

  python -m app.cli init-env          .env を作成（暗号鍵を生成。Talknoteのパスワードは書かない）
  python -m app.cli init              DB作成・初期データ登録
  python -m app.cli create-admin NAME 管理者を作成（パスワードは対話入力）
  python -m app.cli serve             Web画面＋スケジューラーを起動
  python -m app.cli run daily|manual|retry_failed|connection_test|backup [--start --end]
  python -m app.cli reparse --start YYYY-MM-DD --end YYYY-MM-DD
  python -m app.cli load-sample       サンプルデータ（架空）を別DBに作成
  python -m app.cli status            稼働状況を表示
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def cmd_init_env(args):
    path = BASE_DIR / ".env"
    if path.exists() and not args.force:
        print(".env は既にあります（上書きする場合は --force）")
        return
    path.write_text(
        "# 日報集計システムの設定（このファイルは公開しないこと）\n"
        f"APP_SECRET_KEY={secrets.token_urlsafe(48)}\n"
        "# APP_DATA_DIR=var\n"
        "# DATABASE_URL=postgresql+psycopg://user:pass@localhost/clinic\n"
        "TALKNOTE_NETWORK=danae.jp\n"
        "# Talknoteのログイン情報は管理画面（設定）で登録するのを推奨。環境変数で渡す場合のみ以下を使う\n"
        "# TALKNOTE_LOGIN_ID=\n# TALKNOTE_PASSWORD=\n"
        "# ANTHROPIC_API_KEY=\n# SMTP_HOST=\n# SMTP_PORT=587\n# SMTP_USER=\n# SMTP_PASSWORD=\n# SMTP_FROM=\n",
        encoding="utf-8")
    print(f"作成しました: {path}")


def cmd_init(args):
    from .bootstrap import seed
    seed()
    print("初期化しました")


def cmd_create_admin(args):
    from sqlalchemy import select

    from .bootstrap import seed
    from .db import session_scope
    from .models import AppUser
    from .security import hash_password

    seed()
    if args.generate:
        pw = secrets.token_urlsafe(14)
        out = BASE_DIR / "var" / "initial_admin_password.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"username={args.name}\npassword={pw}\n"
                       "# 初回ログイン後、設定画面でパスワードを変更し、このファイルを削除してください\n", encoding="utf-8")
        print(f"初期パスワードを {out} に保存しました")
    else:
        pw = os.environ.get("NEW_ADMIN_PASSWORD") or getpass.getpass("パスワード（10文字以上）: ")
    if len(pw) < 10:
        sys.exit("10文字以上にしてください")
    with session_scope() as s:
        u = s.scalar(select(AppUser).where(AppUser.username == args.name))
        if u:
            u.password_hash, u.role, u.active = hash_password(pw), "admin", True
        else:
            s.add(AppUser(username=args.name, password_hash=hash_password(pw), role="admin"))
    print(f"管理者 {args.name} を登録しました")


def cmd_serve(args):
    import uvicorn
    args.port = int(os.environ.get("PORT", args.port))
    if args.data_dir:
        os.environ["APP_DATA_DIR"] = args.data_dir
    if args.no_scheduler:
        os.environ["RUN_SCHEDULER"] = "0"
    uvicorn.run("app.web.main:app", host=os.environ.get("HOST", args.host), port=args.port, workers=1,
                log_level="info", proxy_headers=True, forwarded_allow_ips="*")


def cmd_run(args):
    from .bootstrap import seed
    from .jobs import enqueue, execute
    from .db import session_scope
    from .models import JobRun

    seed()
    params = {}
    if args.start:
        params = {"start": args.start, "end": args.end or args.start}
    rid = enqueue(args.kind, params, "cli")
    execute(rid)
    with session_scope() as s:
        r = s.get(JobRun, rid)
        print(json.dumps({"id": r.id, "status": r.status, "summary": r.summary, "error": r.error[:2000]},
                         ensure_ascii=False, indent=1, default=str))


def cmd_reparse(args):
    args.kind = "reparse"
    cmd_run(args)


def cmd_update_rules(args):
    """コード側の読み取りルール（app/rules）を新しい版としてDBに登録する。管理画面での変更は上書きせず、別の版になる。"""
    import copy

    from .bootstrap import active_rule, save_rule, seed
    from .db import session_scope
    from .rules import BODY_RULES, EXCEL_RULES

    seed()
    with session_scope() as s:
        for code in BODY_RULES:
            for kind, src in (("body", BODY_RULES), ("excel", EXCEL_RULES)):
                ver, cur = active_rule(s, code, kind)
                if cur != src[code]:
                    v = save_rule(s, code, kind, copy.deepcopy(src[code]), "system", args.note)
                    print(f"{code} {kind}: v{ver} -> v{v}")


def cmd_status(args):
    from sqlalchemy import func, select

    from .bootstrap import get_setting, seed
    from .db import session_scope
    from .models import DayStatus, Issue, JobRun, SourceFile, SourcePost

    seed()
    with session_scope() as s:
        runs = s.scalars(select(JobRun).order_by(JobRun.id.desc()).limit(5)).all()
        print("schedule:", get_setting(s, "schedule"), "auth:", get_setting(s, "auth_state"))
        for r in runs:
            print(f"  run#{r.id} {r.kind} {r.status} {r.queued_at} {r.finished_at}")
        print("posts:", dict(s.execute(select(SourcePost.clinic_code, func.count()).group_by(SourcePost.clinic_code)).all()))
        print("files:", dict(s.execute(select(SourceFile.status, func.count()).group_by(SourceFile.status)).all()))
        print("days:", dict(s.execute(select(DayStatus.status, func.count()).group_by(DayStatus.status)).all()))
        print("open issues:", dict(s.execute(select(Issue.kind, func.count()).where(Issue.status == "open").group_by(Issue.kind)).all()))


def cmd_load_sample(args):
    """架空のサンプルデータを作る。実データとは別のDB（APP_DATA_DIR=var/sample）に入れる。"""
    from datetime import date, datetime, timedelta

    from sqlalchemy import select

    from . import aggregate
    from .bootstrap import put_setting, seed
    from .db import session_scope
    from .models import AppUser, Clinic, SourceFile, SourcePost
    from .pipeline import process_post, reconcile_day, refresh_days
    from .sample import body_9ru, body_bio, body_osaka, body_prevention, workbook_9ru
    from .security import encrypt_text, hash_password, sha256, store_file
    import random

    seed()
    rnd = random.Random(7)
    today = aggregate.today_jst()
    start = today - timedelta(days=args.days)
    with session_scope() as s:
        put_setting(s, "data_mode", {"sample": True})
        pw_file = BASE_DIR / "var" / "sample" / "sample_admin_password.txt"
        if not s.scalar(select(AppUser).where(AppUser.username == "admin")):
            pw = secrets.token_urlsafe(12)
            pw_file.parent.mkdir(parents=True, exist_ok=True)
            pw_file.write_text(f"username=admin\npassword={pw}\n", encoding="utf-8")
            s.add(AppUser(username="admin", password_hash=hash_password(pw), role="admin"))
        s.scalar(select(Clinic).where(Clinic.code == "osaka")).closed_weekdays = [2]
        pid, fid = 500000, 800000
        values9 = {}
        for i in range(args.days):
            d = start + timedelta(days=i)
            posted = datetime(d.year, d.month, d.day, 18, rnd.randint(0, 59))
            net = rnd.randrange(200, 3000) * 1000
            values9[d] = {"gross": net, "net": net, "receipt": net if i % 9 else net + 12500}
            for code, body in (("9ru", body_9ru(d, net, net, visitors=rnd.randint(3, 10), new=rnd.randint(0, 3))),
                               ("bio", body_bio(net + 171600, net, 0, net)),
                               ("prevention", body_prevention(d, net // 2, net // 2, visitors=rnd.randint(2, 6), new=1)),
                               ("osaka", body_osaka(d, net // 3, net // 3))):
                if code == "osaka" and d.weekday() == 2:
                    continue
                if code == "prevention" and i % 13 == 5:
                    continue  # 未提出の例
                pid += 1
                gid = s.scalar(select(Clinic.group_id).where(Clinic.code == code))
                p = SourcePost(post_id=pid, clinic_code=code, group_id=gid, url=f"https://example.invalid/sample/{pid}",
                               posted_at=posted if not (code == "osaka" and i % 11 == 3) else posted + timedelta(days=2),
                               updated_at=posted, body_hash=sha256(body.encode()), body_enc=encrypt_text(body))
                s.add(p)
                s.flush()
                if code == "9ru":
                    fid += 1
                    months = sorted({(x.year, x.month) for x in values9})
                    data = workbook_9ru({k: v for k, v in values9.items() if k <= d}, months)
                    s.add(SourceFile(file_id=fid, post_pk=p.id, attached_at=posted, file_name="9ru_sample 日報.xlsx",
                                     size=len(data), sha256=sha256(data), storage_path=store_file(f"file_{fid}", data),
                                     status="stored"))
                    values9[d]["receipt"] = net  # 本文と1日だけ差異を作る（i%9==0）
        s.flush()
        affected = set()
        for p in s.scalars(select(SourcePost)):
            affected |= process_post(s, p)
        s.flush()
        for c in ("9ru", "bio", "prevention", "osaka"):
            for i in range(args.days):
                reconcile_day(s, c, start + timedelta(days=i))
            refresh_days(s, c, start, today - timedelta(days=1), today)
        aggregate.recompute(s, {((start + timedelta(days=i)).year, (start + timedelta(days=i)).month) for i in range(args.days)})
        from .analysis import generate_daily
        generate_daily(s)
    print(f"サンプルデータを作成しました（{start}〜）。ログイン情報: {BASE_DIR / 'var' / 'sample' / 'sample_admin_password.txt'}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("init-env"); a.add_argument("--force", action="store_true"); a.set_defaults(f=cmd_init_env)
    sub.add_parser("init").set_defaults(f=cmd_init)
    a = sub.add_parser("create-admin"); a.add_argument("name"); a.add_argument("--generate", action="store_true"); a.set_defaults(f=cmd_create_admin)
    a = sub.add_parser("serve"); a.add_argument("--host", default="127.0.0.1"); a.add_argument("--port", type=int, default=8000)
    a.add_argument("--data-dir"); a.add_argument("--no-scheduler", action="store_true"); a.set_defaults(f=cmd_serve)
    a = sub.add_parser("run"); a.add_argument("kind", choices=["daily", "manual", "retry_failed", "connection_test", "backup"])
    a.add_argument("--start"); a.add_argument("--end"); a.set_defaults(f=cmd_run)
    a = sub.add_parser("reparse"); a.add_argument("--start", required=True); a.add_argument("--end", required=True); a.set_defaults(f=cmd_reparse)
    sub.add_parser("status").set_defaults(f=cmd_status)
    a = sub.add_parser("update-rules"); a.add_argument("--note", default="コードのルール更新"); a.set_defaults(f=cmd_update_rules)
    a = sub.add_parser("load-sample"); a.add_argument("--days", type=int, default=50); a.set_defaults(f=cmd_load_sample)
    args = p.parse_args(argv)
    args.f(args)


if __name__ == "__main__":
    main()
