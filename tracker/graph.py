"""Minimal read-only Microsoft Graph client. Only GET is ever issued; there is no write method."""
from __future__ import annotations

import time
from typing import Any, Callable, Iterator

import httpx

BASE = "https://graph.microsoft.com/v1.0"


class GraphError(RuntimeError):
    pass


class GraphClient:
    def __init__(self, token_provider: Callable[[], str], client: httpx.Client | None = None):
        self._token = token_provider
        self._http = client or httpx.Client(timeout=30)

    def get(self, path_or_url: str, params: dict | None = None, headers: dict | None = None) -> dict[str, Any]:
        url = path_or_url if path_or_url.startswith("http") else f"{BASE}{path_or_url}"
        for attempt in range(6):
            h = {"Authorization": f"Bearer {self._token()}", **(headers or {})}
            try:
                r = self._http.get(url, params=params, headers=h)
            except httpx.TransportError as e:
                if attempt == 5:
                    raise GraphError(f"Network error: {e}") from e
                time.sleep(2**attempt)
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(float(r.headers.get("Retry-After", 2**attempt)))
                continue
            if r.status_code >= 400:
                raise GraphError(f"Graph {r.status_code} for {url}: {r.text[:300]}")
            return r.json()
        raise GraphError(f"Graph kept failing for {url}")

    def paged(self, path: str, params: dict | None = None, headers: dict | None = None) -> Iterator[dict]:
        data = self.get(path, params, headers)
        while True:
            yield from data.get("value", [])
            nxt = data.get("@odata.nextLink")
            if not nxt:
                return
            data = self.get(nxt, headers=headers)

    # --- convenience wrappers -------------------------------------------------
    def me(self) -> dict:
        return self.get("/me", {"$select": "displayName,mail,userPrincipalName"})

    def recent_messages(self, top: int = 10) -> list[dict]:
        data = self.get(
            "/me/messages",
            {
                "$top": top,
                "$orderby": "receivedDateTime desc",
                "$select": "id,subject,from,receivedDateTime,parentFolderId,webLink,isRead",
            },
        )
        return data.get("value", [])

    def find_folder(self, name: str) -> dict | None:
        """Find a mail folder by display name anywhere in the tree (case-insensitive)."""
        wanted = name.strip().lower()
        select = {"$top": 100, "$select": "id,displayName,childFolderCount"}
        queue = list(self.paged("/me/mailFolders", select))
        while queue:
            f = queue.pop(0)
            if f["displayName"].strip().lower() == wanted:
                return f
            if f.get("childFolderCount"):
                queue.extend(self.paged(f"/me/mailFolders/{f['id']}/childFolders", select))
        return None

    # --- delta (change tracking) ----------------------------------------------------------------
    def delta_messages(self, folder_id: str, select: str, saved_link: str | None, since_iso: str | None) -> tuple[list[dict], str | None]:
        """Messages that are new/changed in a folder since `saved_link` (or since `since_iso` on the first run).

        Returns (messages, new_delta_link). Messages moved into the folder appear as new items. Deleted/moved-out
        items arrive as '@removed' and are dropped here.
        """
        headers = {"Prefer": "odata.maxpagesize=50"}
        if saved_link:
            first = (saved_link, None)
        else:
            base = f"/me/mailFolders/{folder_id}/messages/delta"
            flt = f"receivedDateTime ge {since_iso}" if since_iso else None
            first = (base, {"$select": select, **({"$filter": flt, "$orderby": "receivedDateTime desc"} if flt else {})})
        try:
            data = self.get(first[0], first[1], headers)
        except GraphError:
            if saved_link or not since_iso:
                raise
            # Some mailboxes reject $filter/$orderby on delta: fall back to a full delta and filter locally.
            data = self.get(f"/me/mailFolders/{folder_id}/messages/delta", {"$select": select}, headers)
        out: list[dict] = []
        while True:
            out.extend(m for m in data.get("value", []) if "@removed" not in m)
            if "@odata.nextLink" in data:
                data = self.get(data["@odata.nextLink"], headers=headers)
                continue
            return out, data.get("@odata.deltaLink")

    def calendar_view(self, start_iso: str, end_iso: str) -> list[dict]:
        """Calendar events between two UTC instants, with times returned in UTC. Read-only."""
        params = {
            "startDateTime": start_iso,
            "endDateTime": end_iso,
            "$select": "id,subject,start,end,organizer,attendees,webLink,isCancelled,isAllDay,bodyPreview,location",
            "$orderby": "start/dateTime",
            "$top": 100,
        }
        return list(self.paged("/me/calendarView", params, {"Prefer": 'outlook.timezone="UTC"'}))
