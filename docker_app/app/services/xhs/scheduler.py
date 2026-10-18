from __future__ import annotations

from datetime import datetime, timedelta
import random
import threading
from typing import Callable

from apscheduler.triggers.date import DateTrigger

from app.db import Database
from app.services.utils import TIMEZONE


class XhsRefreshScheduler:
    """Independent one-shot timers; no browser work is enabled without a verified runner."""

    PREFIX = "scheduled-xhs-"

    def __init__(self, db: Database, scheduler, run_sync: Callable[[int], None] | None = None,
                 ready: Callable[[int], bool] | None = None, *, rng=None, clock=None) -> None:
        self.db, self.scheduler = db, scheduler
        self.run_sync = run_sync
        self.ready = ready or (lambda source_id: False)
        self.rng = rng or random.SystemRandom()
        self.clock = clock or (lambda: datetime.now(TIMEZONE))
        self._lock = threading.RLock()
        self._running = set()

    def reload(self) -> None:
        with self._lock:
            self._reload()

    def _reload(self) -> None:
        with self.db.connect() as conn:
            sources = [dict(row) for row in conn.execute(
                """select a.* from xhs_source_accounts a join site_sources s on s.id = a.source_id
                   where s.enabled = 1 and a.refresh_enabled = 1 and s.source_type = 'xhs_likes'"""
            )]
        active = set()
        for source in sources:
            if self.run_sync is not None and self.ready(source["source_id"]):
                active.add(self.PREFIX + str(source["source_id"]))
                if source['source_id'] not in self._running:
                    self._schedule(source)
        for job in self.scheduler.get_jobs():
            if job.id.startswith(self.PREFIX) and job.id not in active:
                self.scheduler.remove_job(job.id)
        with self.db.connect() as conn:
            for row in conn.execute("select source_id from xhs_schedule_state").fetchall():
                if self.PREFIX + str(row[0]) not in active:
                    conn.execute("delete from xhs_schedule_state where source_id = ?", (row[0],))

    def _schedule(self, source: dict) -> None:
        now = self.clock()
        with self.db.connect() as conn:
            conn.execute("begin immediate")
            state = conn.execute("select * from xhs_schedule_state where source_id = ?", (source["source_id"],)).fetchone()
            if state and state["config_version"] == source["config_version"] and datetime.fromisoformat(state["next_run_at"]) > now:
                run_at = datetime.fromisoformat(state["next_run_at"])
            else:
                jitter = self.rng.choice((-1, 1)) * self.rng.randint(1, 22)
                run_at = now + timedelta(minutes=source["refresh_interval_minutes"] + jitter)
                conn.execute(
                    """insert into xhs_schedule_state(source_id, config_version, next_run_at, jitter_minutes)
                       values (?, ?, ?, ?) on conflict(source_id) do update set
                       config_version = excluded.config_version, next_run_at = excluded.next_run_at,
                       jitter_minutes = excluded.jitter_minutes""",
                    (source["source_id"], source["config_version"], run_at.isoformat(), jitter),
                )
        job_id = self.PREFIX + str(source["source_id"])
        # Pending jobs also need replacement before APScheduler has been started.
        if self.scheduler.get_job(job_id) is not None:
            self.scheduler.remove_job(job_id)
        self.scheduler.add_job(self._run, DateTrigger(run_date=run_at), args=[source["source_id"]],
                               id=job_id, replace_existing=True,
                               max_instances=1, coalesce=True)

    def _run(self, source_id: int) -> None:
        with self._lock:
            if source_id in self._running:
                return
            with self.db.connect() as conn:
                conn.execute("delete from xhs_schedule_state where source_id = ?", (source_id,))
                source = conn.execute(
                    """select a.* from xhs_source_accounts a join site_sources s on s.id = a.source_id
                       where s.id = ? and s.enabled = 1 and a.refresh_enabled = 1""", (source_id,),
                ).fetchone()
            if not source or self.run_sync is None or not self.ready(source_id):
                return
            self._running.add(source_id)
        try:
            self.run_sync(source_id)
        except Exception:
            # The runner records sanitized task failures; scheduling never logs transport errors.
            pass
        finally:
            # Sample a new signed offset after every run, including failures.
            with self._lock:
                self._running.discard(source_id)
                self._reload()
