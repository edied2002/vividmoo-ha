"""Base entity for Vividmoo litter boxes."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BoxState, VividmooCoordinator


class VividmooEntity(CoordinatorEntity[VividmooCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: VividmooCoordinator, serial: str, key: str) -> None:
        super().__init__(coordinator)
        self._serial = serial
        self._attr_unique_id = f"{serial}_{key}"
        self._attr_translation_key = key
        info = coordinator.data[serial].info
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            manufacturer="Vividmoo / UBTech",
            model="Smart cat litter box",
            name=info.get("deviceName") or f"Litter box {serial[-4:]}",
            serial_number=serial,
        )

    @property
    def box(self) -> BoxState | None:
        return (self.coordinator.data or {}).get(self._serial)

    @property
    def available(self) -> bool:
        return super().available and self.box is not None
