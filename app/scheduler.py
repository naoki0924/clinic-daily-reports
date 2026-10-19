"""定期実行。サーバーのプロセス内で動くので、利用者のブラウザやPCを閉じても止まらない。

- 毎日 HH:MM（既定 00:10, Asia/Tokyo）に daily をキューへ入れる
- 15秒ごとにキューを処理（画面の「今すぐ収集」等もここで実行）
- 起動時、今日の定時を過ぎているのに今日の daily が無ければ補完で実行する
- 毎日 03:00 にバックアップ
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from .bootstrap import get_setting
from .config import JST
from .db import session_scope
from .jobs import enqueue, run_next_queued
from .models import JobRun

log = logging.getLogger("scheduler")
_scheduler: BackgroundScheduler | None = None


def _daily():
    with session_scope() as s:
        if not get_setting(s, "schedule").get("enabled", True):
            log.info("自動収集は停止中")
            return
    enqueue("daily")


def _backup():
    with session_scope() as s:
        if not get_setting(s, "backup").get("enabled", True):
            return
    enqueue("backup")


def _tick():
    try:
        run_next_queued()
    except Exception:  # noqa: BLE001
        log.exception("queue tick failed")


def catch_up(now: datetime | None = None) -> bool:
    """起動時の補完：今日の定時を過ぎていて、今日まだ daily が実行されていなければ入れる。"""
    now = now or datetime.now(JST)
    with session_scope() as s:
        sch = get_setting(s, "schedule")
        if not sch.get("enabled", True):
            return False
        due = now.replace(hour=int(sch["hour"]), minute=int(sch["minute"]), second=0, microsecond=0)
        if now < due:
            return False
        since_utc = (due - timedelta(hours=9)).replace(tzinfo=None)
        ran = s.scalar(select(JobRun.id).where(JobRun.kind == "daily", JobRun.queued_at >= since_utc))
    if ran:
        return False
    enqueue("daily", {"catch_up": True})
    return True


def start() -> BackgroundScheduler:
    global _scheduler
    if _scheduler:
        return _scheduler
    with session_scope() as s:
        sch = get_setting(s, "schedule")
        bk = get_setting(s, "backup")
    sc = BackgroundScheduler(timezone=JST, job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600})
    sc.add_job(_daily, CronTrigger(hour=int(sch["hour"]), minute=int(sch["minute"]), timezone=JST), id="daily")
    sc.add_job(_backup, CronTrigger(hour=int(bk.get("hour", 3)), minute=0, timezone=JST), id="backup")
    sc.add_job(_tick, "interval", seconds=15, id="tick")
    sc.start()
    _scheduler = sc
    catch_up()
    log.info("scheduler started")
    return sc


def reschedule() -> None:
    """設定画面で収集時刻を変えたときに呼ぶ。"""
    if not _scheduler:
        return
    with session_scope() as s:
        sch = get_setting(s, "schedule")
    _scheduler.reschedule_job("daily", trigger=CronTrigger(hour=int(sch["hour"]), minute=int(sch["minute"]), timezone=JST))


def next_run() -> datetime | None:
    if not _scheduler:
        return None
    job = _scheduler.get_job("daily")
    return job.next_run_time if job else None


def shutdown() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
