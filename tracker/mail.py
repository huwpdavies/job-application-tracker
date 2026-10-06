"""Read messages from Outlook folders (read-only)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .graph import GraphClient
from .prefilter import domain_of
from .timeutil import to_utc_iso

SELECT = "id,internetMessageId,subject,from,receivedDateTime,body,webLink,parentFolderId,conversationId"


@dataclass
class Message:
    graph_id: str
    internet_message_id: str | None
    subject: str
    sender: str
    sender_domain: str
    received_at: str  # UTC ISO
    web_link: str
    folder_id: str
    body: str
    body_type: str  # 'html' | 'text'


def message_from_graph(m: dict) -> Message:
    addr = ((m.get("from") or {}).get("emailAddress") or {}).get("address") or ""
    body = m.get("body") or {}
    return Message(
        graph_id=m["id"],
        internet_message_id=m.get("internetMessageId"),
        subject=m.get("subject") or "(no subject)",
        sender=addr,
        sender_domain=domain_of(addr),
        received_at=to_utc_iso(m["receivedDateTime"], timezone.utc),
        web_link=m.get("webLink") or "",
        folder_id=m.get("parentFolderId") or "",
        body=body.get("content") or "",
        body_type=(body.get("contentType") or "html").lower(),
    )


def folder_tree(g: GraphClient, root_id: str, root_name: str) -> list[tuple[str, str]]:
    """The folder and all its subfolders, as (id, path)."""
    out = [(root_id, root_name)]
    queue = [(root_id, root_name)]
    while queue:
        fid, path = queue.pop(0)
        for c in g.paged(f"/me/mailFolders/{fid}/childFolders", {"$top": 100, "$select": "id,displayName"}):
            item = (c["id"], f"{path}/{c['displayName']}")
            out.append(item)
            queue.append(item)
    return out


def messages_since(g: GraphClient, folder_id: str, since: datetime) -> list[Message]:
    since_s = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {
        "$filter": f"receivedDateTime ge {since_s}",
        "$orderby": "receivedDateTime desc",
        "$select": SELECT,
        "$top": 50,
    }
    return [message_from_graph(m) for m in g.paged(f"/me/mailFolders/{folder_id}/messages", params)]


def default_since(months: int = 6) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=round(months * 30.44))
