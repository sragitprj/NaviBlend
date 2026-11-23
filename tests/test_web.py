import re

import pytest
from fastapi.testclient import TestClient

from naviblend import external, service
from naviblend.store import Store
from naviblend.subsonic import Subsonic
from naviblend.web import create_app

from .fake_server import FakeSubsonic


@pytest.fixture
def env(tmp_path, monkeypatch):
    srv = FakeSubsonic()
    monkeypatch.setattr(Subsonic, "transport", srv.transport())
    monkeypatch.setattr(Subsonic, "probe", staticmethod(lambda url: "Fake 1.0"))
    monkeypatch.setattr(external, "listenbrainz_top", lambda *a, **k: [])
    monkeypatch.setattr(external, "lastfm_top", lambda *a, **k: [])
    # run blends synchronously in tests
    monkeypatch.setattr(service, "run_in_background",
                        lambda store, bid, write=True: service.run_now(store, bid, write) or True)
    store = Store(tmp_path)
    client = TestClient(create_app(store, start_scheduler=False))
    return srv, store, client


def token(html):
    return re.search(r'name="token" value="([^"]+)"', html).group(1)


def setup(client):
    r = client.get("/setup")
    r = client.post("/setup", data={"server_url": "http://music.local", "password": "longenough",
                                    "password2": "longenough", "token": token(r.text)})
    assert r.status_code == 200 and "Connected to Fake 1.0" in r.text
    return r


def add(client, html, name, user, pw):
    return client.post("/people/save", data={"name": name, "username": user, "password": pw,
                                             "color": "#5B9BFF", "token": token(html)})


def two_people(client):
    r = setup(client)
    r = add(client, r.text, "Alex", "alex", "pw1")
    assert "Saved Alex" in r.text
    r = add(client, r.text, "Jordan", "jordan", "pw2")
    assert "Create a blend" in r.text
    return r


def test_first_run_flow_to_saved_blend(env):
    srv, store, client = env
    assert client.get("/").url.path == "/setup"
    two_people(client)
    r = client.get("/")
    assert "No blends yet" in r.text
    r = client.get("/blends/new")
    assert r.text.count('name="member"') == 2
    alex, jordan = [m.id for m in store.members()]
    r = client.post("/blends/save", data={"token": token(r.text), "member": [str(alex), str(jordan)],
                                          f"weight_{alex}": "50", f"weight_{jordan}": "50", "length": "40",
                                          "both_pct": "30", "new_pct": "20", "schedule_day": "mon",
                                          "schedule_time": "06:00", "public": "on", "interleave": "on",
                                          "build": "save"})
    assert r.url.path.startswith("/blends/")
    assert "Alex + Jordan Blend" in r.text and "taste match" in r.text
    assert len(srv.playlists) == 1 and r.text.count("<tr>") == 41
    r = client.get("/")
    assert "Alex + Jordan Blend" in r.text and "Create a blend" in r.text


def test_create_a_second_blend_with_different_people(env):
    srv, store, client = env
    two_people(client)
    alex, jordan = [m.id for m in store.members()]
    for people, name in (([alex, jordan], ""), ([jordan], "Jordan solo")):
        r = client.get("/blends/new")
        client.post("/blends/save", data={"token": token(r.text), "member": [str(p) for p in people],
                                          "name": name, "build": "save", "schedule_day": "off"})
    blends = store.blends()
    assert [b.member_ids for b in blends] == [[alex, jordan], [jordan]]
    assert {p["name"] for p in srv.playlists.values()} == {"Alex + Jordan Blend", "Jordan solo"}
    home = client.get("/").text
    assert "Alex + Jordan Blend" in home and "Jordan solo" in home


def test_blend_needs_someone_in_it(env):
    _, store, client = env
    two_people(client)
    r = client.get("/blends/new")
    r = client.post("/blends/save", data={"token": token(r.text)})
    assert "Pick at least one person" in r.text and not store.blends()


def test_edit_and_delete_blend(env):
    _, store, client = env
    two_people(client)
    alex, jordan = [m.id for m in store.members()]
    r = client.get("/blends/new")
    client.post("/blends/save", data={"token": token(r.text), "member": [str(alex), str(jordan)]})
    bid = store.blends()[0].id
    r = client.get(f"/blends/{bid}/edit")
    client.post("/blends/save", data={"token": token(r.text), "id": str(bid), "member": [str(alex)],
                                      "name": "Just me", "length": "25", "schedule_day": "fri",
                                      "schedule_time": "07:30"})
    b = store.blend(bid)
    assert (b.member_ids, b.name, b.length, b.schedule_day, b.schedule_time) == ([alex], "Just me", 25, "fri", "07:30")
    r = client.get(f"/blends/{bid}/edit")
    r = client.post(f"/blends/{bid}/delete", data={"token": token(r.text)})
    assert "Blend deleted" in r.text and not store.blends()


def test_wrong_member_password_is_rejected(env):
    _, store, client = env
    r = setup(client)
    r = add(client, r.text, "Alex", "alex", "wrong")
    assert "Couldn" in r.text and not store.members()


def test_login_required_and_csrf(env):
    _, store, client = env
    setup(client)
    client.post("/logout")
    assert client.get("/settings").url.path == "/login"
    r = client.get("/login")
    bad = client.post("/login", data={"password": "nope", "token": token(r.text)})
    assert "isn" in bad.text
    ok = client.post("/login", data={"password": "longenough", "token": token(bad.text), "next": "/settings"})
    assert ok.url.path == "/settings"
    client.post("/settings", data={"lastfm_api_key": "abc"})  # no form token: ignored
    assert store.get("lastfm_api_key") == ""


def test_settings_save(env):
    _, store, client = env
    setup(client)
    r = client.get("/settings")
    r = client.post("/settings", data={"token": token(r.text), "server_url": "http://music.local",
                                       "lastfm_api_key": "abc123"})
    assert "Settings saved" in r.text
    assert store.get("lastfm_api_key") == "abc123" and b"abc123" not in store.path.read_bytes()
