from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from . import DOMAIN, SIGNAL

async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([ConnectionSensor(hass.data[DOMAIN])])

class ConnectionSensor(BinarySensorEntity):
    _attr_should_poll = False
    _attr_name = 'Cabana Alex event connection problem'
    _attr_unique_id = 'cabana_alex_event_connection_problem'
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    def __init__(self, client):
        self.client = client
    async def async_added_to_hass(self):
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL, self.async_write_ha_state))
    @property
    def is_on(self):
        return not self.client.connected or bool(self.client.error) or bool(self.client.data.get('storage_error'))
