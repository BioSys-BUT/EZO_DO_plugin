# -*- coding: utf-8 -*-
import sqlite3
import click

from pioreactor.whoami import get_unit_name, get_assigned_experiment_name
from pioreactor.config import config
from pioreactor.background_jobs.base import BackgroundJobContrib
from pioreactor.utils import timing
from pioreactor.utils.timing import RepeatedTimer
from pioreactor.background_jobs.leader.mqtt_to_db_streaming import produce_metadata
from pioreactor.background_jobs.leader.mqtt_to_db_streaming import register_source_to_sink
from pioreactor.background_jobs.leader.mqtt_to_db_streaming import TopicToParserToTable
from atlas_ezo_do import AtlasEzoDO


def __dir__():
    return ["click_do_reading"]


def _ensure_do_readings_table():
    try:
        db_path = config.get("storage", "database")
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS do_readings (
                    experiment       TEXT NOT NULL,
                    pioreactor_unit  TEXT NOT NULL,
                    timestamp        TEXT NOT NULL,
                    do_reading       REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS do_readings_experiment_ix ON do_readings (experiment)"
            )
    except Exception:
        pass


_ensure_do_readings_table()


def parser(topic, payload) -> dict:
    metadata = produce_metadata(topic)
    return {
        "experiment": metadata.experiment,
        "pioreactor_unit": metadata.pioreactor_unit,
        "timestamp": timing.current_utc_timestamp(),
        "do_reading": float(payload),
    }


register_source_to_sink(
    TopicToParserToTable(
        ["pioreactor/+/+/do_reading/DO"],
        parser,
        "do_readings",
    )
)


class DOReader(BackgroundJobContrib):
    job_name = "do_reading"
    published_settings = {
        "DO": {"datatype": "float", "settable": False},
    }

    def __init__(self, unit, experiment, **kwargs) -> None:
        super().__init__(unit=unit, experiment=experiment, plugin_name="do_reading", **kwargs)

        try:
            time_between_readings = config.getfloat("do_reading.config", "time_between_readings")
        except Exception:
            time_between_readings = 2.0

        if time_between_readings < 2.0:
            self.logger.error(
                "Invalid time_between_readings=%.2f. Minimum allowed is 2.0 seconds.",
                time_between_readings,
            )
            raise ValueError(
                "time_between_readings must be at least 2.0 seconds. "
                "Please increase it in configuration."
            )

        self.probe = AtlasEzoDO.from_config()
        self.probe.ensure_i2c_mode_and_address(desired_address=AtlasEzoDO.DEFAULT_I2C_ADDRESS)

        self.timer_thread = RepeatedTimer(
            time_between_readings, self.read_do, job_name=self.job_name, run_immediately=True
        ).start()

    def read_do(self):
        self.DO = float(self.probe.read_do(samples=2))
        return self.DO

    def on_ready_to_sleeping(self) -> None:
        self.timer_thread.pause()

    def on_sleeping_to_ready(self) -> None:
        self.timer_thread.unpause()

    def on_disconnect(self) -> None:
        self.timer_thread.cancel()


__plugin_name__ = "do_reading"
__plugin_version__ = "0.1.0"


@click.command(name="do_reading")
def click_do_reading():
    unit = get_unit_name()
    job = DOReader(
        unit=unit,
        experiment=get_assigned_experiment_name(unit),
    )
    job.block_until_disconnected()
