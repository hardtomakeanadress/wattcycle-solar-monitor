from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from . import DOMAIN, SIGNAL

async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([EventSensor(hass.data[DOMAIN])])

class EventSensor(SensorEntity):
    _attr_should_poll = False
    _attr_name = 'Cabana Alex important events'
    _attr_unique_id = 'cabana_alex_important_events'
    _attr_icon = 'mdi:alert-circle-outline'

    def __init__(self, client):
        self.client = client
    async def async_added_to_hass(self):
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL, self.async_write_ha_state))
    @property
    def native_value(self):
        return len(self.client.data['active'])
    @property
    def extra_state_attributes(self):
        data = self.client.data
        return dict(active=data['active'], recent_events=data['events'][-20:],
                    retained_events=len(data['events']), connected=self.client.connected,
                    error=self.client.error, pi_storage_error=data.get('storage_error'),
                    history_gap_detected=data.get('gap', False))
