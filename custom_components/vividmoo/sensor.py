"""Sensors for Vividmoo litter boxes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfMass, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import VividmooConfigEntry
from .const import EVENT_VISIT_IDENTIFIED, OBSERVED_JOB_SECONDS, WORK_MODES, WORK_STATES
from .coordinator import BoxState
from .entity import VividmooEntity


def _record(box: BoxState, key: str) -> Any:
    return (box.last_record or {}).get(key)


def _last_use(box: BoxState) -> datetime | None:
    ms = _record(box, "eventTime")
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms else None


def _visit_times(box: BoxState) -> list[datetime]:
    return [datetime.fromtimestamp(v["eventTime"] / 1000, tz=timezone.utc) for v in box.visits.values()]


def _visits_today(box: BoxState) -> int | None:
    if not box.backfilled:
        return None
    today = dt_util.now().date()
    return sum(1 for t in _visit_times(box) if dt_util.as_local(t).date() == today)


def _visits_7d_avg(box: BoxState) -> float | None:
    if not box.backfilled:
        return None
    since = dt_util.utcnow() - timedelta(days=7)
    return round(sum(1 for t in _visit_times(box) if t >= since) / 7, 1)


def _identified(box: BoxState) -> dict[str, Any] | None:
    """Newest identified visit (eventType 3 with a cat id): excludes stray/unknown weights."""
    visits = [v for v in box.visits.values() if v.get("eventType") == EVENT_VISIT_IDENTIFIED and v.get("catInfoId")]
    return max(visits, key=lambda v: v["eventTime"], default=None)


def _enum(table: dict[int, str]) -> Callable[[int | None], str | None]:
    return lambda v: None if v is None else table.get(v)


@dataclass(frozen=True, kw_only=True)
class VividmooSensorDescription(SensorEntityDescription):
    value_fn: Callable[[BoxState], Any]


SENSORS: tuple[VividmooSensorDescription, ...] = (
    VividmooSensorDescription(
        key="use_count",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda b: b.use_times.get("boxUseTimes"),
    ),
    VividmooSensorDescription(
        key="waste_bin_level",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda b: b.use_times.get("percentage"),
    ),
    VividmooSensorDescription(
        key="waste_bin_capacity",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda b: b.use_times.get("boxFullMax"),
    ),
    VividmooSensorDescription(
        key="last_use",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_last_use,
    ),
    VividmooSensorDescription(key="last_cat", value_fn=lambda b: _record(b, "catNickname")),
    # Live records: weight 5.9-6.0 for one cat with weighUnit 0, costTime 36-67.
    # Inferred: kg when the box config's weighUnit is 0, and costTime in seconds.
    VividmooSensorDescription(
        key="last_weight",
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda b: _record(b, "weight"),
    ),
    VividmooSensorDescription(
        key="visits_today",
        state_class=SensorStateClass.TOTAL_INCREASING,  # resets at local midnight
        value_fn=_visits_today,
    ),
    VividmooSensorDescription(
        key="visits_7d_avg",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=_visits_7d_avg,
    ),
    # Statistics-friendly cat weight: identified visits only (unidentified readings such as 2.7 would
    # otherwise spike the long-term graph). Unit as for last_weight (kg when weighUnit 0).
    VividmooSensorDescription(
        key="cat_weight",
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda b: (_identified(b) or {}).get("weight"),
    ),
    VividmooSensorDescription(
        key="last_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_display_precision=0,
        value_fn=lambda b: _record(b, "costTime"),
    ),
    VividmooSensorDescription(
        key="work_mode",
        device_class=SensorDeviceClass.ENUM,
        options=list(WORK_MODES.values()),
        value_fn=lambda b: _enum(WORK_MODES)(b.work_mode),
    ),
    VividmooSensorDescription(
        key="work_state",
        device_class=SensorDeviceClass.ENUM,
        options=list(WORK_STATES.values()),
        # Like the app (xb/w.f): when the mode is idle, the job state is idle whatever w_state says.
        value_fn=lambda b: None if b.work_mode is None else ("idle" if b.work_mode == 0 else _enum(WORK_STATES)(b.work_state)),
    ),
    VividmooSensorDescription(
        key="wifi_network",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda b: b.config.get("wifiName"),
    ),
    VividmooSensorDescription(
        key="device_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda b: b.info.get("deviceStatus"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: VividmooConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        VividmooSensor(coordinator, serial, description)
        for serial in coordinator.data
        for description in SENSORS
    )


class VividmooSensor(VividmooEntity, SensorEntity):
    entity_description: VividmooSensorDescription

    def __init__(self, coordinator, serial: str, description: VividmooSensorDescription) -> None:
        super().__init__(coordinator, serial, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        box = self.box
        return self.entity_description.value_fn(box) if box else None

    @property
    def native_unit_of_measurement(self) -> str | None:
        if self.entity_description.key in ("last_weight", "cat_weight"):
            box = self.box
            # Only weighUnit 0 has been observed (with kg-plausible values).
            return UnitOfMass.KILOGRAMS if box and box.config.get("weighUnit", 0) == 0 else None
        return super().native_unit_of_measurement

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        box = self.box
        if box is None:
            return None
        if self.entity_description.key == "work_state":
            iso = lambda ts: None if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()  # noqa: E731
            return {
                "cause": box.work_cause,
                **box.all_state,
                # Job timing for the card (box streams nothing mid-job; see coordinator._track_job).
                "job_timing_known": box.job_timing_known,
                "job_started": iso(box.job_started),
                "job_run_seconds": round(box.run_accumulated, 1),
                "job_running_since": iso(box.running_since),
                "job_expected_seconds": OBSERVED_JOB_SECONDS.get(box.job_mode) if box.job_mode else None,
            }
        if self.entity_description.key in ("visits_today", "visits_7d_avg"):
            identified = sum(1 for v in box.visits.values() if v.get("eventType") == EVENT_VISIT_IDENTIFIED)
            return {"identified_in_history": identified, "unidentified_in_history": len(box.visits) - identified}
        if self.entity_description.key == "cat_weight":
            visit = _identified(box)
            return {"cat": visit.get("catNickname"), "measured_at": visit.get("eventTime")} if visit else None
        if self.entity_description.key == "last_use":
            record = box.last_record or {}
            attrs = {k: record.get(k) for k in ("eventType", "catInfoId", "confirmed") if k in record}
            if box.last_message:
                attrs["last_message"] = box.last_message
            return attrs or None
        return None
