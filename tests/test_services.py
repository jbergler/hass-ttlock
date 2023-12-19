from unittest.mock import patch

import pytest

from custom_components.ttlock.const import DOMAIN
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant


class Test_cleanup_passcodes:
    @pytest.mark.parametrize("return_response", (True, False))
    async def test_works_when_there_is_nothing_to_do(
        self, hass: HomeAssistant, component_setup, sane_default_data, return_response
    ) -> None:
        """Test get schedule service."""
        coordinator = await component_setup()

        with patch(
            "custom_components.ttlock.api.TTLockApi.list_passcodes", return_value=[]
        ) as mock:
            response = await hass.services.async_call(
                DOMAIN,
                "cleanup_passcodes",
                {ATTR_ENTITY_ID: coordinator.entities[0].entity_id},
                blocking=True,
                return_response=return_response,
            )
            await hass.async_block_till_done()
            assert mock.called

        if return_response:
            assert response == {"removed": []}
        else:
            assert response is None
