"""HA initiates a resumable event connection; no inbound Tailscale grant needed."""
import asyncio
import logging
import time
import aiohttp
from homeassistant.core import SupportsResponse
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

DOMAIN = 'cabana_events'
SIGNAL = 'cabana_events_updated'
_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry):
    client = Client(hass, entry.data['url'])
    await client.load()
    hass.data[DOMAIN] = client
    await hass.config_entries.async_forward_entry_setups(entry, ['sensor', 'binary_sensor'])
    client.task = hass.async_create_background_task(client.run(), 'cabana_events')
    async def export(call):
        return client.data
    hass.services.async_register(DOMAIN, 'export', export, supports_response=SupportsResponse.ONLY)
    return True

async def async_unload_entry(hass, entry):
    client = hass.data[DOMAIN]
    client.task.cancel()
    try:
        await client.task
    except asyncio.CancelledError:
        pass
    await hass.config_entries.async_unload_platforms(entry, ['sensor', 'binary_sensor'])
    hass.services.async_remove(DOMAIN, 'export')
    hass.data.pop(DOMAIN)
    return True

class Client:
    def __init__(self, hass, url):
        self.hass, self.url = hass, url
        self.store = Store(hass, 1, 'cabana_events.history')
        self.data = {'generation': None, 'cursor': 0, 'events': [], 'active': []}
        self.connected = False
        self.error = None
        self.task = None

    async def load(self):
        saved = await self.store.async_load()
        if saved:
            self.data = saved

    async def run(self):
        session = async_get_clientsession(self.hass)
        failed_since = None
        while True:
            try:
                params = {'after': str(self.data['cursor']), 'wait': '25'}
                if self.data['generation']:
                    params['generation'] = self.data['generation']
                async with session.get(self.url + '/api/events', params=params,
                                       timeout=aiohttp.ClientTimeout(total=35)) as response:
                    response.raise_for_status()
                    payload = await response.json()
                if not isinstance(payload.get('events'), list) or not isinstance(payload.get('active'), list):
                    raise ValueError('Invalid event response')
                incoming = payload['events']
                known = {(e['generation'], e['seq']) for e in self.data['events']}
                fresh = [e for e in incoming if (e['generation'], e['seq']) not in known]
                updated = dict(generation=payload['generation'], cursor=payload['cursor'],
                               events=(self.data['events'] + fresh)[-10000:], active=payload['active'],
                               gap=bool(payload.get('gap')) or self.data.get('gap', False),
                               storage_error=payload.get('storage_error'))
                if updated != self.data:
                    # Save before acknowledging the cursor: failed HA storage retries the batch.
                    await self.store.async_save(updated)
                    self.data = updated
                    for event in fresh:
                        self.hass.bus.async_fire('cabana_important_event', event)
                self.connected, self.error, failed_since = True, None, None
                async_dispatcher_send(self.hass, SIGNAL)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if failed_since is None:
                    failed_since = time.monotonic()
                    _LOGGER.warning('Cabana event connection interrupted: %s', type(exc).__name__)
                self.connected = False
                self.error = 'Event connection or HA storage unavailable'
                async_dispatcher_send(self.hass, SIGNAL)
                await asyncio.sleep(5)
