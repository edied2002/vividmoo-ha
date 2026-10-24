"""Buttons for Vividmoo litter boxes (d2c1019 operate commands over the cloud IM channel)."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import VividmooConfigEntry
from .entity import VividmooEntity
from .protocol import risp


@dataclass(frozen=True, kw_only=True)
class VividmooButtonDescription(ButtonEntityDescription):
    op_mode: int | None  # None: use the running job's mode (pause/resume)
    op_state: int


BUTTONS: tuple[VividmooButtonDescription, ...] = (
    VividmooButtonDescription(key="clean", op_mode=risp.OP_MODE_CLEAN, op_state=risp.OP_STATE_START),
    VividmooButtonDescription(key="smooth", op_mode=risp.OP_MODE_SMOOTH, op_state=risp.OP_STATE_START),
    VividmooButtonDescription(key="pause", op_mode=None, op_state=risp.OP_STATE_PAUSE),
    VividmooButtonDescription(key="resume", op_mode=None, op_state=risp.OP_STATE_RESUME),
)


WASTE_BIN_EMPTIED = ButtonEntityDescription(key="waste_bin_emptied")


async def async_setup_entry(
    hass: HomeAssistant, entry: VividmooConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        VividmooButton(coordinator, serial, description) for serial in coordinator.data for description in BUTTONS
    )
    async_add_entities(VividmooWasteBinEmptied(coordinator, serial) for serial in coordinator.data)


class VividmooButton(VividmooEntity, ButtonEntity):
    entity_description: VividmooButtonDescription

    def __init__(self, coordinator, serial: str, description: VividmooButtonDescription) -> None:
        super().__init__(coordinator, serial, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        return super().available and self.box.online is not False

    async def async_press(self) -> None:
        description = self.entity_description
        op_mode = description.op_mode
        if op_mode is None:
            op_mode = await self._running_op_mode()
        await self.coordinator.async_operate(self._serial, op_mode, description.op_state)

    async def _running_op_mode(self) -> int:
        """Pause/resume resend the running job's mode, as the app does (CLEAN+PAUSE, SMOOTH+RESUME...)."""
        box = self.box
        if box.work_mode not in risp.WORK_MODE_TO_OP_MODE:
            box = await self.coordinator.async_request_state(self._serial)
        op_mode = risp.WORK_MODE_TO_OP_MODE.get(box.work_mode)
        if op_mode is None:
            raise HomeAssistantError("Nothing to pause or resume: the litter box is idle")
        return op_mode


class VividmooWasteBinEmptied(VividmooEntity, ButtonEntity):
    """Resets the cloud's waste-bin use counter, like the app's "emptied" action (REST, no box command)."""

    entity_description = WASTE_BIN_EMPTIED

    def __init__(self, coordinator, serial: str) -> None:
        super().__init__(coordinator, serial, WASTE_BIN_EMPTIED.key)

    async def async_press(self) -> None:
        await self.coordinator.async_reset_use_times(self._serial)
