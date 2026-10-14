"""Binary sensors for Vividmoo litter boxes."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import VividmooConfigEntry
from .entity import VividmooEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: VividmooConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = []
    for serial in coordinator.data:
        entities.append(VividmooOnline(coordinator, serial))
        entities.append(VividmooBinFull(coordinator, serial))
        entities.append(VividmooCatInside(coordinator, serial))
    async_add_entities(entities)


class VividmooOnline(VividmooEntity, BinarySensorEntity):
    """IM contact presence (ContactDTO.onlineStatus == 1)."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, serial: str) -> None:
        super().__init__(coordinator, serial, "online")

    @property
    def is_on(self) -> bool | None:
        return self.box.online if self.box else None


class VividmooBinFull(VividmooEntity, BinarySensorEntity):
    """Waste bin full according to the server's fill percentage (BoxUseTimes.percentage >= 100).

    Live: boxUseTimes 501, boxFullMax 50, percentage 100 (capped), boxFullAlert 25.
    The alert threshold is a separate, optional notification setting (boxFullAlertSwitch).
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator, serial: str) -> None:
        super().__init__(coordinator, serial, "waste_bin_full")

    @property
    def is_on(self) -> bool | None:
        percentage = (self.box.use_times if self.box else {}).get("percentage")
        return None if percentage is None else percentage >= 100

    @property
    def extra_state_attributes(self):
        use = self.box.use_times if self.box else {}
        return {k: use.get(k) for k in ("boxUseTimes", "boxFullMax", "boxFullAlert") if k in use} or None


# Cat presence as the app derives it from the full-state message (xb/w.f).
CAT_PRESENCE = ("in_box", "near_box", "away")


class VividmooCatInside(VividmooEntity, BinarySensorEntity):
    """Cat in the box, from toilet_state in the box's 1024 state pushes.

    Live (2026-10-12): the box pushes 1024 when the cat steps in (toilet_state=1, weigh_state=1,
    w_state=2 "blocked") and again when it leaves (toilet_state=0), followed by a 1023 msg 2000.
    Push-only, so it is unavailable without a live MQTT session.
    """

    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    def __init__(self, coordinator, serial: str) -> None:
        super().__init__(coordinator, serial, "cat_inside")

    @property
    def available(self) -> bool:
        # all_state is empty until the first 1021 reply / 1024 push (requested on every connect).
        return super().available and self.coordinator.live_updates and bool(self.box.all_state)

    @property
    def is_on(self) -> bool:
        return self.box.all_state.get("toilet_state", 0) == 1  # proto3 omits zero fields

    @property
    def extra_state_attributes(self):
        state = self.box.all_state
        if state.get("toilet_state", 0) == 1:
            presence = CAT_PRESENCE[0]
        elif state.get("foreign_state", 0) == 1:
            presence = CAT_PRESENCE[1]
        else:
            presence = CAT_PRESENCE[2]
        return {"presence": presence, "weighing": state.get("weigh_state", 0) == 1}
