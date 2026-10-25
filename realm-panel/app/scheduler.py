"""
APScheduler wrapper.  Runs recalculate_weights() every WEIGHT_INTERVAL seconds
(default 60) and optionally triggers a realm config regeneration + service
restart when weights actually change.
"""
import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler

from .weight_engine import recalculate_weights
from .realm_ctrl import reload_realm
from .config_io import regenerate_config

log = logging.getLogger(__name__)
INTERVAL = int(os.environ.get("WEIGHT_INTERVAL", "60"))
_scheduler: BackgroundScheduler | None = None


def _job():
    try:
        recalculate_weights()
        regenerate_config()
        reload_realm()
    except Exception as e:
        log.error("scheduler job failed: %s", e)


def start_scheduler(app=None):
    global _scheduler
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(_job, "interval", seconds=INTERVAL, id="weight_rebalance")
    _scheduler.start()
    log.info("Weight scheduler started, interval=%ds", INTERVAL)


def trigger_now():
    if _scheduler:
        _scheduler.get_job("weight_rebalance").modify(next_run_time=None)
    _job()
