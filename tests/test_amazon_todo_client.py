"""Tests for the Alexa to-do list (v2 API) client and multi-entry helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from homeassistant.config_entries import ConfigEntryState

from custom_components.alexa_shopping_sync.amazon_todo_client import AmazonTodoClient
from custom_components.alexa_shopping_sync.const import (
    ALEXA_LIST_SHOPPING,
    ALEXA_LIST_TODO,
    DOMAIN,
    STORAGE_KEY,
    storage_key_for,
    unique_id_for,
)
from custom_components.alexa_shopping_sync.exceptions import (
    AmazonListNotFoundError,
    ConnectionError,
    SessionExpiredError,
)
from custom_components.alexa_shopping_sync.services import _target_coordinators

LIST_ID = "abc/def+ghi="
ENCODED = "abc%2Fdef%2Bghi%3D"


def item(item_id="i1", name="Call bank", status="ACTIVE", version=1):
    """Build an item in the format returned by the live API."""
    return {
        "itemId": item_id,
        "listId": LIST_ID,
        "itemName": name,
        "itemStatus": status,
        "version": version,
        "createAt": 1753263753213,
        "updateAt": 1753263753213,
        "itemType": "KEYWORD",
        "note": None,
    }


def page(*items, next_token=None):
    """Build an items/fetch response."""
    return {"itemInfoList": list(items), "nextToken": next_token, "listInfo": {"listType": "TODO"}}


def version_mismatch():
    """Build the error Amazon returns for a stale version."""
    request = httpx.Request("PUT", "https://example.test")
    response = httpx.Response(
        400,
        request=request,
        json={"errorType": "VersionMismatchException", "errorMessage": "VersionFromRequest=1"},
    )
    return httpx.HTTPStatusError("API error: 400", request=request, response=response)


@pytest.fixture
def client(mock_auth_manager):
    """Create a client with a known list ID and mocked requests."""
    todo = AmazonTodoClient(mock_auth_manager)
    todo.shopping_list_id = LIST_ID
    todo._async_request = AsyncMock()
    return todo


async def test_discover_picks_todo_list(client):
    client.shopping_list_id = None
    client._async_request.return_value = {
        "listInfoList": [
            {"listType": "SHOP", "listId": "shop"},
            {"listType": "TODO", "listId": "todo"},
        ]
    }
    assert await client.async_discover_shopping_list_id() == "todo"
    client._async_request.assert_awaited_once_with("POST", "/v2/lists/fetch", json_data={})


async def test_discover_never_falls_back_to_shopping(client):
    client.shopping_list_id = None
    client._async_request.return_value = {"listInfoList": [{"listType": "SHOP", "listId": "shop"}]}
    with pytest.raises(AmazonListNotFoundError):
        await client.async_discover_shopping_list_id()


async def test_snapshot_parses_items_and_encodes_list_id(client):
    client._async_request.return_value = page(item("a", "Active"), item("b", "Done", "COMPLETE", 3))
    items = await client.async_get_snapshot()

    assert [(i.item_id, i.name, i.complete, i.version) for i in items] == [
        ("a", "Active", False, 1),
        ("b", "Done", True, 3),
    ]
    assert items[0].created_at is not None
    assert client._async_request.await_args.args[1] == (
        f"/v2/lists/{ENCODED}/items/fetch?limit=100"
    )


async def test_snapshot_sends_next_token_in_body(client):
    client._async_request.side_effect = [
        page(item("a"), next_token="tok1"),
        page(item("b"), next_token="tok2"),
        page(item("c")),
    ]
    items = await client.async_get_snapshot()

    assert [i.item_id for i in items] == ["a", "b", "c"]
    bodies = [c.kwargs["json_data"] for c in client._async_request.await_args_list]
    assert bodies == [{}, {"nextToken": "tok1"}, {"nextToken": "tok2"}]
    assert all("nextToken" not in c.args[1] for c in client._async_request.await_args_list)


async def test_snapshot_raises_when_paging_never_ends(client):
    """A partial snapshot would look like deletions, so it must not be returned."""
    client._async_request.return_value = page(item(), next_token="same")
    with pytest.raises(ConnectionError):
        await client.async_get_snapshot()


async def test_snapshot_raises_on_malformed_response(client):
    """An empty result must not be inferred from an unexpected response."""
    client._async_request.return_value = {"errorType": "Something"}
    with pytest.raises(ConnectionError):
        await client.async_get_snapshot()


async def test_add_item(client):
    client._async_request.return_value = {"itemInfoList": [item("new", "Task")], "failures": []}
    created = await client.async_add_item("Task")

    assert created.item_id == "new"
    client._async_request.assert_awaited_once_with(
        "POST",
        f"/v2/lists/{ENCODED}/items",
        json_data={"items": [{"itemType": "KEYWORD", "itemName": "Task"}]},
    )


async def test_add_item_reports_failure_despite_200(client):
    client._async_request.return_value = {"itemInfoList": [], "failures": [{"reason": "x"}]}
    assert await client.async_add_item("Task") is None


async def test_add_completed_item_then_marks_complete(client):
    client._async_request.side_effect = [
        {"itemInfoList": [item("new", "Task", version=1)], "failures": []},
        {"itemInfo": item("new", "Task", "COMPLETE", 2)},
    ]
    created = await client.async_add_item("Task", complete=True)

    assert created.complete is True
    assert client._async_request.await_args.args[1].endswith("/items/new?version=1")


async def test_update_sends_only_changed_attributes_and_tracks_version(client):
    client._versions = {"i1": 1}
    client._async_request.side_effect = [
        {"itemInfo": item(name="Renamed", version=2)},
        {"itemInfo": item(name="Renamed", status="COMPLETE", version=3)},
    ]
    await client.async_update_item("i1", summary="Renamed")
    updated = await client.async_mark_complete("i1", True)

    first, second = client._async_request.await_args_list
    assert first.args == ("PUT", f"/v2/lists/{ENCODED}/items/i1?version=1")
    assert first.kwargs["json_data"] == {
        "itemAttributesToUpdate": [{"type": "itemName", "value": "Renamed"}],
        "itemAttributesToRemove": [],
    }
    assert second.args[1].endswith("?version=2")
    assert second.kwargs["json_data"]["itemAttributesToUpdate"] == [
        {"type": "itemStatus", "value": "COMPLETE"}
    ]
    assert updated.complete is True


async def test_update_reopen_sets_active(client):
    client._versions = {"i1": 1}
    client._async_request.return_value = {"itemInfo": item(version=2)}
    await client.async_update_item("i1", complete=False)
    assert client._async_request.await_args.kwargs["json_data"]["itemAttributesToUpdate"] == [
        {"type": "itemStatus", "value": "ACTIVE"}
    ]


async def test_update_retries_once_on_version_mismatch(client):
    client._versions = {"i1": 1}
    client._async_request.side_effect = [
        version_mismatch(),
        page(item(version=5)),
        {"itemInfo": item(name="New", version=6)},
    ]
    updated = await client.async_update_item("i1", summary="New")

    assert updated.version == 6
    assert client._async_request.await_args.args[1].endswith("?version=5")


async def test_update_gives_up_after_second_version_mismatch(client):
    client._versions = {"i1": 1}
    client._async_request.side_effect = [version_mismatch(), page(item()), version_mismatch()]
    with pytest.raises(httpx.HTTPStatusError):
        await client.async_update_item("i1", summary="New")


async def test_update_missing_item_returns_none(client):
    client._async_request.return_value = page()
    assert await client.async_update_item("gone", summary="New") is None


async def test_delete_uses_current_version(client):
    client._versions = {"i1": 4}
    client._async_request.return_value = {}
    assert await client.async_delete_item("i1") is True
    client._async_request.assert_awaited_once_with(
        "DELETE", f"/v2/lists/{ENCODED}/items/i1?version=4"
    )


async def test_delete_already_gone_is_success(client):
    client._async_request.return_value = page()
    assert await client.async_delete_item("gone") is True


async def test_delete_failure_returns_false(client):
    client._versions = {"i1": 1}
    client._async_request.side_effect = SessionExpiredError("expired")
    assert await client.async_delete_item("i1") is False


def test_shopping_entries_keep_legacy_identity():
    """Existing installs must keep their mappings file and unique ID."""
    assert storage_key_for(ALEXA_LIST_SHOPPING, "entry1") == STORAGE_KEY
    assert unique_id_for("a@b.c", "amazon.co.uk", ALEXA_LIST_SHOPPING) == "a@b.c_amazon.co.uk"


def test_todo_entries_do_not_collide_with_shopping():
    assert storage_key_for(ALEXA_LIST_TODO, "entry1") != STORAGE_KEY
    assert storage_key_for(ALEXA_LIST_TODO, "entry1") != storage_key_for(ALEXA_LIST_TODO, "entry2")
    assert unique_id_for("a@b.c", "amazon.co.uk", ALEXA_LIST_TODO) != unique_id_for(
        "a@b.c", "amazon.co.uk", ALEXA_LIST_SHOPPING
    )


def _entry(entry_id, state=ConfigEntryState.LOADED):
    return MagicMock(entry_id=entry_id, state=state, runtime_data=f"coordinator-{entry_id}")


def test_services_target_all_loaded_entries_or_one():
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [
        _entry("a"),
        _entry("b"),
        _entry("c", ConfigEntryState.NOT_LOADED),
    ]

    everything = _target_coordinators(hass, MagicMock(data={}))
    one = _target_coordinators(hass, MagicMock(data={"config_entry_id": "b"}))

    hass.config_entries.async_entries.assert_called_with(DOMAIN)
    assert everything == ["coordinator-a", "coordinator-b"]
    assert one == ["coordinator-b"]
