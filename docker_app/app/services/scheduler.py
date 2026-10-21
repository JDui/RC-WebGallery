from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.db import Database
from app.services.puller import PullManager
from app.services.site_syncer import SiteSyncManager
from app.services.xhs.scheduler import XhsRefreshScheduler


class SchedulerService:
    def __init__(self, db: Database, pull_manager: PullManager, site_syncer: SiteSyncManager) -> None:
        self.db = db
        self.pull_manager = pull_manager
        self.site_syncer = site_syncer
        self.scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        self.xhs = XhsRefreshScheduler(db, self.scheduler)

    def start(self) -> None:
        if not self.scheduler.running:
            self.scheduler.start()
        self.reload()

    def reload(self) -> None:
        settings = self.db.get_settings()
        self.scheduler.remove_all_jobs()
        if settings.get("scheduler_enabled"):
            interval_hours = self._interval_hours(settings.get("scheduler_interval_hours", 12))
            self.scheduler.add_job(
                self.start_scheduled_pull,
                IntervalTrigger(hours=interval_hours),
                id="scheduled-pull",
                replace_existing=True,
            )
        if settings.get("site_scheduler_enabled"):
            site_interval_hours = self._interval_hours(settings.get("site_scheduler_interval_hours", 12))
            self.scheduler.add_job(
                self.start_scheduled_site_sync,
                IntervalTrigger(hours=site_interval_hours),
                id="scheduled-site-sync",
                replace_existing=True,
            )
        self.reload_xhs()

    def reload_xhs(self) -> None:
        self.xhs.reload()

    def start_scheduled_pull(self) -> None:
        self.pull_manager.start_pull()

    def start_scheduled_site_sync(self) -> None:
        self.site_syncer.start_sync()

    def start_scheduled_sync(self) -> None:
        self.start_scheduled_pull()
        self.start_scheduled_site_sync()

    def _interval_hours(self, value: object) -> int:
        try:
            return max(int(value), 1)
        except (TypeError, ValueError):
            return 12

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def suspend_for_maintenance(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=True)
        # A stopped APScheduler executor cannot be restarted safely.
        self.scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        self.xhs.scheduler = self.scheduler
