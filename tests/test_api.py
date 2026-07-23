"""Test the TTLockApi card and fingerprint methods.

These exercise the real method bodies with only the HTTP layer (``get``/``post``)
mocked, so the request params, response parsing, and success/error handling are
covered without any network access.
"""

from unittest.mock import AsyncMock

import pytest

from custom_components.ttlock.models import Card, CardType, Fingerprint


class TestListCards:
    async def test_parses_cards_and_requests_all_pages(self, api):
        api.get = AsyncMock(
            return_value={
                "list": [
                    {
                        "cardId": 124242,
                        "lockId": 163377,
                        "cardNumber": "1723612378",
                        "cardName": "Card for mom",
                        "cardType": 1,
                        "senderUsername": "alexa@google.com",
                        "startDate": 0,
                        "endDate": 0,
                    }
                ]
            }
        )

        cards = await api.list_cards(163377)

        api.get.assert_awaited_once_with(
            "identityCard/list",
            lockId=163377,
            pageNo=1,
            pageSize=200,
            orderBy=1,
        )
        assert len(cards) == 1
        assert isinstance(cards[0], Card)
        assert cards[0].id == 124242
        assert cards[0].type == CardType.normal
        # startDate/endDate == 0 means permanent -> no dates, never expired.
        assert cards[0].start_date is None
        assert cards[0].end_date is None
        assert cards[0].expired is False

    async def test_empty_list(self, api):
        api.get = AsyncMock(return_value={"list": []})
        assert await api.list_cards(1) == []


class TestRenameCard:
    async def test_success(self, api):
        api.post = AsyncMock(return_value={"errcode": 0, "errmsg": "none"})

        assert await api.rename_card(163377, 124242, "New name") is True
        api.post.assert_awaited_once_with(
            "identityCard/rename",
            lockId=163377,
            cardId=124242,
            cardName="New name",
        )

    async def test_failure(self, api):
        api.post = AsyncMock(return_value={"errcode": -3, "errmsg": "boom"})
        assert await api.rename_card(163377, 124242, "New name") is False


class TestDeleteCard:
    async def test_success(self, api):
        api.post = AsyncMock(return_value={"errcode": 0, "errmsg": "none"})

        assert await api.delete_card(163377, 124242) is True
        api.post.assert_awaited_once_with(
            "identityCard/delete",
            lockId=163377,
            cardId=124242,
            deleteType=2,
        )

    async def test_failure(self, api):
        api.post = AsyncMock(return_value={"errcode": -3, "errmsg": "boom"})
        assert await api.delete_card(163377, 124242) is False


class TestListFingerprints:
    async def test_parses_fingerprints_and_requests_all_pages(self, api):
        api.get = AsyncMock(
            return_value={
                "list": [
                    {
                        "fingerprintId": 224242,
                        "lockId": 163377,
                        "fingerprintNumber": "44668054142981",
                        "fingerprintName": "Thumb",
                        "fingerprintType": 1,
                        "senderUsername": "alexa@google.com",
                        "startDate": 0,
                        "endDate": 0,
                    }
                ]
            }
        )

        prints = await api.list_fingerprints(163377)

        api.get.assert_awaited_once_with(
            "fingerprint/list",
            lockId=163377,
            pageNo=1,
            pageSize=200,
            orderBy=1,
        )
        assert len(prints) == 1
        assert isinstance(prints[0], Fingerprint)
        assert prints[0].id == 224242
        assert prints[0].name == "Thumb"
        assert prints[0].start_date is None
        assert prints[0].end_date is None

    async def test_empty_list(self, api):
        api.get = AsyncMock(return_value={"list": []})
        assert await api.list_fingerprints(1) == []


class TestRenameFingerprint:
    async def test_success(self, api):
        api.post = AsyncMock(return_value={"errcode": 0, "errmsg": "none"})

        assert await api.rename_fingerprint(163377, 224242, "Left thumb") is True
        api.post.assert_awaited_once_with(
            "fingerprint/rename",
            lockId=163377,
            fingerprintId=224242,
            fingerprintName="Left thumb",
        )

    async def test_failure(self, api):
        api.post = AsyncMock(return_value={"errcode": -3, "errmsg": "boom"})
        assert await api.rename_fingerprint(163377, 224242, "Left thumb") is False


class TestDeleteFingerprint:
    async def test_success(self, api):
        api.post = AsyncMock(return_value={"errcode": 0, "errmsg": "none"})

        assert await api.delete_fingerprint(163377, 224242) is True
        api.post.assert_awaited_once_with(
            "fingerprint/delete",
            lockId=163377,
            fingerprintId=224242,
            deleteType=2,
        )

    async def test_failure(self, api):
        api.post = AsyncMock(return_value={"errcode": -3, "errmsg": "boom"})
        assert await api.delete_fingerprint(163377, 224242) is False


@pytest.mark.parametrize("method", ["list_cards", "list_fingerprints"])
async def test_list_methods_propagate_missing_list_key(api, method):
    """A malformed response without a `list` key should raise, not silently pass."""
    api.get = AsyncMock(return_value={})
    with pytest.raises(KeyError):
        await getattr(api, method)(1)
