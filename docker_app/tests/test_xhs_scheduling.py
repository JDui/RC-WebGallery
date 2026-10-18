from datetime import datetime, timedelta
import threading
from types import SimpleNamespace

from apscheduler.schedulers.background import BackgroundScheduler
import pytest

from app.db import Database
from app.services.scheduler import SchedulerService
from app.services.utils import TIMEZONE
from app.services.xhs.models import XhsValidationError
from app.services.xhs.scheduler import XhsRefreshScheduler
from app.services.xhs.sync import XhsSyncService
from app.services.xhs.transport import direct_session


@pytest.fixture
def service(tmp_path):
    db = Database(tmp_path / 'app.db')
    db.init()
    return XhsSyncService(db)


def source(service, **changes):
    return service.create_source({'name': '点赞', 'owner_key': 'test-account', 'refresh_enabled': True,
                                  'refresh_interval_minutes': 60, **changes})


class SequenceRandom:
    def __init__(self, signs=(-1, 1, -1), offsets=(1, 22, 10)):
        self.signs, self.offsets = iter(signs), iter(offsets)
        self.draws = 0

    def choice(self, choices):
        assert choices == (-1, 1)
        return next(self.signs)

    def randint(self, low, high):
        assert (low, high) == (1, 22)
        self.draws += 1
        return next(self.offsets)


def test_direct_connection_ignores_global_and_environment_proxies(service, monkeypatch):
    item = source(service)
    service.db.save_settings({'site_proxy_enabled': True, 'site_proxy_host': '127.0.0.1', 'site_proxy_port': 7890})
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        monkeypatch.setenv(name, 'http://127.0.0.1:9999')
    with direct_session() as session:
        assert session.trust_env is False
        assert session.merge_environment_settings('https://www.xiaohongshu.com/', {}, None, None, None)['proxies'] == {}
    assert service.db.get_site_source(item['id'])['use_proxy'] == 0
    assert service.status()['items'][0]['network_mode'] == 'direct'


def test_signed_jitter_is_resampled_every_run_and_retained_across_reloads(service):
    item = source(service)
    clock = [datetime(2026, 10, 19, 12, tzinfo=TIMEZONE)]
    calls = []
    scheduler = BackgroundScheduler(timezone=TIMEZONE)
    rng = SequenceRandom()
    refresher = XhsRefreshScheduler(service.db, scheduler, calls.append, lambda _: True, rng=rng, clock=lambda: clock[0])
    refresher.reload()
    job = scheduler.get_job(f'scheduled-xhs-{item["id"]}')
    assert job.trigger.run_date == clock[0] + timedelta(minutes=59)
    refresher.reload()
    assert rng.draws == 1
    clock[0] = job.trigger.run_date
    refresher._run(item['id'])
    assert calls == [item['id']]
    assert scheduler.get_job(job.id).trigger.run_date == clock[0] + timedelta(minutes=82)
    assert rng.draws == 2
    with service.db.connect() as conn:
        assert conn.execute('select jitter_minutes from xhs_schedule_state').fetchone()[0] == 22


def test_xhs_settings_and_timers_are_independent_of_bili_and_sites(service):
    item = source(service)
    service.db.save_settings({'scheduler_enabled': True, 'scheduler_interval_hours': 2,
                             'site_scheduler_enabled': True, 'site_scheduler_interval_hours': 7})
    scheduler = SchedulerService(service.db, SimpleNamespace(start_pull=lambda: None), SimpleNamespace(start_sync=lambda: None))
    scheduler.xhs.run_sync = lambda _: None
    scheduler.xhs.ready = lambda _: True
    scheduler.xhs.rng = SequenceRandom()
    scheduler.reload()
    up = scheduler.scheduler.get_job('scheduled-pull')
    site = scheduler.scheduler.get_job('scheduled-site-sync')
    xhs = scheduler.scheduler.get_job(f'scheduled-xhs-{item["id"]}')
    first_run = xhs.trigger.run_date
    service.update_source(item['id'], {'config_version': 0, 'refresh_interval_minutes': 120})
    scheduler.reload_xhs()
    assert scheduler.scheduler.get_job('scheduled-pull') is up
    assert scheduler.scheduler.get_job('scheduled-site-sync') is site
    assert scheduler.scheduler.get_job(xhs.id).trigger.run_date > first_run
    assert up.trigger.interval.total_seconds() == 7200
    assert site.trigger.interval.total_seconds() == 25200
    run_at = scheduler.scheduler.get_job(xhs.id).trigger.run_date
    service.db.save_settings({'scheduler_interval_hours': 4, 'site_scheduler_enabled': False})
    scheduler.reload()
    assert scheduler.scheduler.get_job(xhs.id).trigger.run_date == run_at


def test_no_automatic_requests_without_verified_runner_even_when_setting_enabled(service):
    source(service)
    scheduler = BackgroundScheduler(timezone=TIMEZONE)
    refresher = XhsRefreshScheduler(service.db, scheduler)
    refresher.reload()
    assert scheduler.get_jobs() == []
    assert service.status()['items'][0]['next_refresh_at'] is None


def test_failure_gets_new_jitter_and_disabling_removes_the_timer(service):
    item = source(service)
    scheduler = BackgroundScheduler(timezone=TIMEZONE)
    def failing(_):
        raise RuntimeError('secret-network-response')
    refresher = XhsRefreshScheduler(service.db, scheduler, failing, lambda _: True, rng=SequenceRandom())
    refresher.reload()
    refresher._run(item['id'])
    assert scheduler.get_job(f'scheduled-xhs-{item["id"]}') is not None
    service.update_source(item['id'], {'config_version': 0, 'refresh_enabled': False})
    refresher.reload()
    assert scheduler.get_jobs() == []
    assert service.status()['items'][0]['next_refresh_at'] is None


@pytest.mark.parametrize('value', [0, 1, 22, 525601, 60.5, True, '60'])
def test_refresh_cycle_validation(service, value):
    with pytest.raises(XhsValidationError):
        source(service, refresh_interval_minutes=value)


def test_new_databases_default_to_independent_disabled_xhs_refresh(service):
    item = service.create_source({'name': '点赞', 'owner_key': 'test-account'})
    assert item['refresh_enabled'] == 0
    assert item['refresh_interval_minutes'] == 720


def test_v1_source_migrates_without_changing_existing_limits(service):
    item = source(service, anchor_count=5, initial_fetch_limit=25)
    with service.db.connect() as conn:
        conn.execute('drop table xhs_schedule_state')
        conn.execute('alter table xhs_source_accounts drop column refresh_enabled')
        conn.execute('alter table xhs_source_accounts drop column refresh_interval_minutes')
        conn.execute('delete from xhs_schema_migrations where version = 2')
    service.db.init()
    migrated = service.get_source(item['id'])
    assert migrated['anchor_count'] == 5 and migrated['initial_fetch_limit'] == 25
    assert migrated['refresh_interval_minutes'] == 720 and migrated['refresh_enabled'] == 0


def test_reload_during_runner_does_not_reschedule_until_runner_finishes(service):
    item = source(service)
    clock = [datetime(2026, 10, 19, 12, tzinfo=TIMEZONE)]
    entered, finish = threading.Event(), threading.Event()
    observations = []
    scheduler = BackgroundScheduler(timezone=TIMEZONE)
    rng = SequenceRandom()
    refresher = None

    def runner(source_id):
        assert source_id == item["id"]
        refresher.reload()
        with service.db.connect() as conn:
            scheduled = conn.execute(
                "select next_run_at from xhs_schedule_state where source_id=?", (source_id,)
            ).fetchone()
        observations.append((rng.draws, scheduled, scheduler.get_job(f"scheduled-xhs-{source_id}")))
        entered.set()
        assert finish.wait(5)

    refresher = XhsRefreshScheduler(
        service.db, scheduler, runner, lambda _: True,
        rng=rng, clock=lambda: clock[0],
    )
    refresher.reload()
    timer_id = f"scheduled-xhs-{item['id']}"
    assert scheduler.get_job(timer_id) is not None and rng.draws == 1
    scheduler.remove_job(timer_id)

    thread = threading.Thread(target=refresher._run, args=(item['id'],), daemon=True)
    thread.start()
    assert entered.wait(5)
    draws_during_run, row_during_run, job_during_run = observations[0]
    assert draws_during_run == 1
    assert row_during_run is None
    assert job_during_run is None
    finish.set()
    thread.join(5)

    assert not thread.is_alive()
    next_job = scheduler.get_job(timer_id)
    assert next_job is not None
    assert rng.draws == 2
    assert next_job.trigger.run_date == clock[0] + timedelta(minutes=82)
