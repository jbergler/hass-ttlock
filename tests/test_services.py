from unittest.mock import call, patch

import pytest

from custom_components.ttlock.const import DOMAIN
from custom_components.ttlock.models import Passcode, PasscodeType
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

    async def test_works_when_there_is_an_expired_passcode(
        self,
        hass: HomeAssistant,
        component_setup,
        sane_default_data,
    ) -> None:
        """Test get schedule service."""
        coordinator = await component_setup()

        with patch(
            "custom_components.ttlock.api.TTLockApi.list_passcodes",
            return_value=[
                Passcode(
                    keyboardPwdId=123,
                    keyboardPwdType=PasscodeType.temporary,
                    keyboardPwdName="Test",
                    endDate=0,
                )
            ],
        ), patch(
            "custom_components.ttlock.api.TTLockApi.delete_passcode", return_value=True
        ) as mock:
            response = await hass.services.async_call(
                DOMAIN,
                "cleanup_passcodes",
                {ATTR_ENTITY_ID: coordinator.entities[0].entity_id},
                blocking=True,
                return_response=True,
            )
            await hass.async_block_till_done()
            assert mock.call_args_list == [call(coordinator.lock_id, 123)]

        assert response == {"removed": ["Test"]}
