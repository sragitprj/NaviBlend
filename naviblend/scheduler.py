"""Automatic refresh: one scheduled job per blend."""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import service
from .store import Store

log = logging.getLogger(__name__)
_sched: BackgroundScheduler | None = None


def _job_id(blend_id: int) -> str:
    return f"blend-{blend_id}"


def start(store: Store) -> None:
    global _sched
    if _sched is None:
        _sched = BackgroundScheduler()
        _sched.start()
    reschedule(store)


def reschedule(store: Store) -> None:
    """Make the scheduled jobs match the blends as they are now."""
    if _sched is None:
        return
    for job in _sched.get_jobs():
        job.remove()
    for b in store.blends():
        if b.schedule_day == "off" or not b.member_ids:
            continue
        try:
            hh, mm = (b.schedule_time or "06:00").split(":")
            trigger = CronTrigger(day_of_week="*" if b.schedule_day == "daily" else b.schedule_day,
                                  hour=int(hh), minute=int(mm))
        except ValueError:
            log.warning("blend %s has a bad schedule, skipping", b.id)
            continue
        _sched.add_job(service.run_now, trigger, args=[store, b.id, True], id=_job_id(b.id),
                       misfire_grace_time=3600, coalesce=True)
        log.info("blend %s next refresh: %s", b.id, next_run_text(b.id))


def next_run_text(blend_id: int) -> str:
    job = _sched.get_job(_job_id(blend_id)) if _sched else None
    if not job or not job.next_run_time:
        return ""
    return job.next_run_time.strftime("%A %d %b, %H:%M")
