from homeassistant import config_entries

class ConfigFlow(config_entries.ConfigFlow, domain='cabana_events'):
    VERSION = 1
    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id('cabana_alex_important_events')
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title='Cabana Alex important events', data={'url': 'http://100.82.58.122:8080'})
