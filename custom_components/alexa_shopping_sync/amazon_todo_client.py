"""Amazon Alexa To-Do List API client (v2 list endpoints).

Verified against live Amazon API on amazon.co.uk (2026-11).

The legacy /getlistitems endpoint only exposes the shopping list, so the to-do
list has to use the v2 endpoints:
  POST   /v2/lists/fetch                              - All lists ({"listInfoList": [...]})
  POST   /v2/lists/{list_id}/items/fetch?limit=N      - Items, N <= 100 (150+ returns 400)
  POST   /v2/lists/{list_id}/items                    - Add ({"items": [...]})
  PUT    /v2/lists/{list_id}/items/{item_id}?version= - Update changed attributes only
  DELETE /v2/lists/{list_id}/items/{item_id}?version= - Delete

Paging: the nextToken from a response goes in the JSON body of the next request.
A nextToken in the query string is silently ignored and returns page one forever.

Concurrency: a stale version is rejected with 400 VersionMismatchException and
nothing is changed. The item may have been edited in the Alexa app since the last
snapshot, so the client refreshes the version and retries once.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import httpx

from .amazon_client import AmazonShoppingClient
from .const import (
    AMAZON_API_V2_ITEM,
    AMAZON_API_V2_ITEMS,
    AMAZON_API_V2_ITEMS_FETCH,
    AMAZON_API_V2_LISTS_FETCH,
    AMAZON_V2_MAX_PAGES,
    AMAZON_V2_PAGE_SIZE,
    AMAZON_V2_STATUS_ACTIVE,
    AMAZON_V2_STATUS_COMPLETE,
    AMAZON_V2_TODO_LIST_TYPE,
)
from .exceptions import AmazonListNotFoundError, ConnectionError
from .models import AlexaShoppingItem

_LOGGER = logging.getLogger(__name__)


def _is_version_mismatch(err: httpx.HTTPStatusError) -> bool:
    """Return True if Amazon rejected a write because the version was stale."""
    if err.response.status_code != 400:
        return False
    try:
        return err.response.json().get("errorType") == "VersionMismatchException"
    except (ValueError, AttributeError):
        return False


class AmazonTodoClient(AmazonShoppingClient):
    """Client for the Alexa To-Do List, with the same interface as the shopping client.

    Inherits auth, retry and backoff handling. `shopping_list_id` is the interface
    name the sync engine uses for "the Alexa list ID"; here it holds the to-do list ID.
    """

    def __init__(self, auth_manager: Any) -> None:
        """Initialize the client."""
        super().__init__(auth_manager)
        self._versions: dict[str, int] = {}

    def _list_path(self) -> str:
        assert self._shopping_list_id is not None
        return quote(self._shopping_list_id, safe="")

    async def _async_ensure_list_id(self) -> None:
        if not self._shopping_list_id:
            await self.async_discover_shopping_list_id()

    async def async_discover_shopping_list_id(self) -> str:
        """Discover the to-do list ID."""
        data = await self._async_request("POST", AMAZON_API_V2_LISTS_FETCH, json_data={})
        lists = data.get("listInfoList") if isinstance(data, dict) else None
        for info in lists or []:
            if info.get("listType") == AMAZON_V2_TODO_LIST_TYPE and info.get("listId"):
                self._shopping_list_id = info["listId"]
                return self._shopping_list_id
        raise AmazonListNotFoundError("No Alexa to-do list found")

    async def async_get_snapshot(self) -> list[AlexaShoppingItem]:
        """Get all to-do items, following pagination.

        Raises rather than returning a partial result: the sync engine treats an
        item missing from a snapshot as deleted.
        """
        await self._async_ensure_list_id()
        path = AMAZON_API_V2_ITEMS_FETCH.format(
            list_id=self._list_path(), limit=AMAZON_V2_PAGE_SIZE
        )

        raw_items: list[dict[str, Any]] = []
        body: dict[str, Any] = {}
        for _page in range(AMAZON_V2_MAX_PAGES):
            data = await self._async_request("POST", path, json_data=body)
            if not isinstance(data, dict) or not isinstance(data.get("itemInfoList"), list):
                raise ConnectionError("Unexpected response format from Amazon to-do API")
            raw_items.extend(data["itemInfoList"])
            token = data.get("nextToken")
            if not token:
                break
            body = {"nextToken": token}
        else:
            raise ConnectionError(f"To-do list exceeded {AMAZON_V2_MAX_PAGES} pages")

        items = [AlexaShoppingItem.from_v2_response(raw) for raw in raw_items]
        self._versions = {i.item_id: i.version for i in items if i.version is not None}
        _LOGGER.debug("Loaded %d items from Alexa to-do list", len(items))
        return items

    async def async_add_item(
        self, summary: str, complete: bool = False
    ) -> AlexaShoppingItem | None:
        """Add an item. Returns None if Amazon reports it was not created."""
        await self._async_ensure_list_id()
        path = AMAZON_API_V2_ITEMS.format(list_id=self._list_path())
        payload = {"items": [{"itemType": "KEYWORD", "itemName": summary}]}
        data = await self._async_request("POST", path, json_data=payload)

        # A 200 can still carry per-item failures.
        created = data.get("itemInfoList") if isinstance(data, dict) else None
        if not created or (isinstance(data, dict) and data.get("failures")):
            _LOGGER.error("Amazon did not create to-do item")
            return None

        item = AlexaShoppingItem.from_v2_response(created[0])
        if item.version is not None:
            self._versions[item.item_id] = item.version
        if complete and not item.complete:
            return await self.async_mark_complete(item.item_id, True)
        return item

    async def _async_with_version(
        self, item_id: str, call: Callable[[int], Awaitable[Any]]
    ) -> tuple[bool, Any]:
        """Run a versioned write, retrying once on a stale version.

        Returns (found, result); found is False if the item no longer exists.
        """
        if item_id not in self._versions:
            await self.async_get_snapshot()
        if item_id not in self._versions:
            return False, None

        try:
            return True, await call(self._versions[item_id])
        except httpx.HTTPStatusError as err:
            if not _is_version_mismatch(err):
                raise

        _LOGGER.debug("Stale version for to-do item %s, refreshing", item_id)
        await self.async_get_snapshot()
        if item_id not in self._versions:
            return False, None
        return True, await call(self._versions[item_id])

    async def async_update_item(
        self,
        item_id: str,
        summary: str | None = None,
        complete: bool | None = None,
        version: int | None = None,
    ) -> AlexaShoppingItem | None:
        """Update an item's name and/or completion. `version` is accepted but refreshed."""
        attributes: list[dict[str, str]] = []
        if summary is not None:
            attributes.append({"type": "itemName", "value": summary})
        if complete is not None:
            status = AMAZON_V2_STATUS_COMPLETE if complete else AMAZON_V2_STATUS_ACTIVE
            attributes.append({"type": "itemStatus", "value": status})
        if not attributes:
            return None

        await self._async_ensure_list_id()
        if version is not None:
            self._versions[item_id] = version
        payload = {"itemAttributesToUpdate": attributes, "itemAttributesToRemove": []}

        async def _put(current: int) -> Any:
            path = AMAZON_API_V2_ITEM.format(
                list_id=self._list_path(), item_id=item_id, version=current
            )
            return await self._async_request("PUT", path, json_data=payload)

        found, data = await self._async_with_version(item_id, _put)
        if not found:
            _LOGGER.warning("To-do item %s not found for update", item_id)
            return None

        info = data.get("itemInfo") if isinstance(data, dict) else None
        if not info:
            self._versions.pop(item_id, None)  # unknown new version: re-read before next write
            return None
        item = AlexaShoppingItem.from_v2_response(info)
        if item.version is not None:
            self._versions[item_id] = item.version
        return item

    async def async_delete_item(self, item_id: str) -> bool:
        """Delete an item. An item that is already gone counts as success."""
        await self._async_ensure_list_id()

        async def _delete(current: int) -> Any:
            path = AMAZON_API_V2_ITEM.format(
                list_id=self._list_path(), item_id=item_id, version=current
            )
            return await self._async_request("DELETE", path)

        try:
            await self._async_with_version(item_id, _delete)
        except Exception as err:
            _LOGGER.error("Failed to delete Alexa to-do item %s: %s", item_id, err)
            return False
        self._versions.pop(item_id, None)
        return True
