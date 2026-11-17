"""Persist completed pulse observations independently of live boiler commands."""

from copy import deepcopy
from dataclasses import asdict

from homeassistant.helpers.storage import Store
from vtherm_api.log_collector import get_vtherm_logger

from .const import DOMAIN

_LOGGER = get_vtherm_logger(__name__)


class PulseLearningStore:
    """Save the scheduler's bounded learning data when observations change."""

    def __init__(self, hass, entry_id, flow):
        self.store = Store(hass, 1, f"{DOMAIN}.pulse.{entry_id}")
        self.scheduler = flow.pulse_scheduler
        self.settings = {
            "flow_entity": flow.entity_id,
            "sensor": flow.sensor_id,
            **asdict(flow.settings),
        }
        self.last_snapshot = None
        self.changed = False

    async def async_load(self):
        try:
            data = await self.store.async_load()
        except Exception:  # A failed read should leave the controller usable.
            _LOGGER.exception("Cannot load learned boiler pulse data")
            data = None
        if isinstance(data, dict) and data.get("settings") == self.settings:
            self.scheduler.restore_learning(data.get("learning"))
        self.last_snapshot = deepcopy(self.scheduler.dump_learning())

    def track(self):
        snapshot = self.scheduler.dump_learning()
        if snapshot == self.last_snapshot:
            return
        self.last_snapshot = deepcopy(snapshot)
        self.changed = True
        self.store.async_delay_save(self._data, 30)

    def _data(self):
        return {"settings": self.settings, "learning": self.last_snapshot}

    async def async_flush(self):
        # A fresh Store for the same entry can be created immediately on reload.
        # Complete outstanding writes before that Store reads the entry.
        self.track()
        if self.changed:
            try:
                await self.store.async_save(self._data())
            except Exception:
                _LOGGER.exception("Cannot save learned boiler pulse data on unload")
