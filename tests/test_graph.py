"""Graph client: read-only, paging, retry, delta. HTTP is mocked with respx."""
import httpx
import pytest
import respx

from tracker import graph as graph_mod
from tracker.graph import BASE, GraphClient, GraphError


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(graph_mod.time, "sleep", lambda s: None)


@pytest.fixture
def g():
    return GraphClient(lambda: "TOKEN")


def test_the_client_has_no_way_to_write():
    for verb in ("post", "put", "patch", "delete", "send", "move", "update"):
        assert not hasattr(GraphClient, verb), f"GraphClient.{verb} must not exist (read-only app)"


@respx.mock
def test_only_get_requests_are_ever_sent_and_token_is_attached(g):
    route = respx.get(f"{BASE}/me").mock(return_value=httpx.Response(200, json={"displayName": "Huw"}))
    respx.get(f"{BASE}/me/messages").mock(return_value=httpx.Response(200, json={"value": []}))
    g.me(); g.recent_messages(3)
    assert all(c.request.method == "GET" for c in respx.calls)
    assert route.calls[0].request.headers["authorization"] == "Bearer TOKEN"


@respx.mock
def test_paging_follows_next_link(g):
    respx.get(f"{BASE}/me/mailFolders").mock(return_value=httpx.Response(200, json={"value": [{"id": 1}], "@odata.nextLink": f"{BASE}/page2"}))
    respx.get(f"{BASE}/page2").mock(return_value=httpx.Response(200, json={"value": [{"id": 2}, {"id": 3}]}))
    assert [x["id"] for x in g.paged("/me/mailFolders")] == [1, 2, 3]


@respx.mock
def test_retries_on_429_then_succeeds(g):
    route = respx.get(f"{BASE}/me").mock(side_effect=[httpx.Response(429, headers={"Retry-After": "0"}),
                                                       httpx.Response(503), httpx.Response(200, json={"ok": 1})])
    assert g.get("/me") == {"ok": 1} and route.call_count == 3


@respx.mock
def test_client_errors_are_not_retried(g):
    route = respx.get(f"{BASE}/me").mock(return_value=httpx.Response(403, text="Forbidden"))
    with pytest.raises(GraphError, match="403"):
        g.get("/me")
    assert route.call_count == 1


@respx.mock
def test_gives_up_after_repeated_failures(g):
    respx.get(f"{BASE}/me").mock(return_value=httpx.Response(500))
    with pytest.raises(GraphError):
        g.get("/me")


@respx.mock
def test_find_folder_searches_subfolders_case_insensitively(g):
    respx.get(f"{BASE}/me/mailFolders").mock(return_value=httpx.Response(200, json={"value": [{"id": "inbox", "displayName": "Inbox", "childFolderCount": 1}]}))
    respx.get(f"{BASE}/me/mailFolders/inbox/childFolders").mock(return_value=httpx.Response(200, json={"value": [{"id": "ja", "displayName": "Job Applications", "childFolderCount": 0}]}))
    assert g.find_folder("job applications")["id"] == "ja"
    assert g.find_folder("Nope") is None


@respx.mock
def test_delta_collects_pages_drops_removed_and_returns_the_delta_link(g):
    base = f"{BASE}/me/mailFolders/F1/messages/delta"
    respx.get(base).mock(return_value=httpx.Response(200, json={"value": [{"id": "a"}, {"id": "gone", "@removed": {"reason": "deleted"}}], "@odata.nextLink": f"{BASE}/delta-page-2"}))
    respx.get(f"{BASE}/delta-page-2").mock(return_value=httpx.Response(200, json={"value": [{"id": "b"}], "@odata.deltaLink": f"{BASE}/delta-token-NEXT"}))
    msgs, link = g.delta_messages("F1", "id", None, "2026-04-01T00:00:00Z")
    assert [m["id"] for m in msgs] == ["a", "b"] and link.endswith("delta-token-NEXT")


@respx.mock
def test_delta_resumes_from_saved_link(g):
    saved = f"{BASE}/saved-delta-link"
    route = respx.get(saved).mock(return_value=httpx.Response(200, json={"value": [], "@odata.deltaLink": f"{saved}2"}))
    msgs, link = g.delta_messages("F1", "id", saved, None)
    assert route.called and msgs == [] and link.endswith("saved-delta-link2")


@respx.mock
def test_delta_falls_back_when_filter_is_rejected_on_first_run(g):
    base = f"{BASE}/me/mailFolders/F1/messages/delta"
    respx.get(base).mock(side_effect=[httpx.Response(400, text="filter not supported"),
                                      httpx.Response(200, json={"value": [{"id": "a"}], "@odata.deltaLink": "L"})])
    msgs, link = g.delta_messages("F1", "id", None, "2026-04-01T00:00:00Z")
    assert [m["id"] for m in msgs] == ["a"] and link == "L"


@respx.mock
def test_calendar_view_asks_for_utc_times(g):
    route = respx.get(f"{BASE}/me/calendarView").mock(return_value=httpx.Response(200, json={"value": [{"id": "e1"}]}))
    assert g.calendar_view("2026-04-01T00:00:00Z", "2027-01-01T00:00:00Z")[0]["id"] == "e1"
    assert route.calls[0].request.headers["prefer"] == 'outlook.timezone="UTC"'
