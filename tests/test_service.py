import sqlite3

import pytest

from naviblend import external, service
from naviblend.store import Blend, Member, Store
from naviblend.subsonic import Subsonic

from .fake_server import FakeSubsonic


@pytest.fixture
def fake(monkeypatch):
    srv = FakeSubsonic()
    monkeypatch.setattr(Subsonic, "transport", srv.transport())
    monkeypatch.setattr(external, "listenbrainz_top", lambda *a, **k: [])
    monkeypatch.setattr(external, "lastfm_top", lambda *a, **k: [])
    return srv


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path)
    s.set("server_url", "http://music.local")
    a = s.save_member(Member(0, "Alex", "#5B9BFF", "alex", "pw1"))
    b = s.save_member(Member(0, "Jordan", "#FFA24C", "jordan", "pw2"))
    s.ids = (a, b)
    return s


def make_blend(store, **kw):
    return store.save_blend(Blend(member_ids=list(store.ids), **kw))


def test_passwords_are_encrypted_at_rest(store):
    raw = store.path.read_bytes()
    assert b"pw1" not in raw and b"pw2" not in raw
    assert [m.password for m in store.members()] == ["pw1", "pw2"]


def test_full_run_writes_a_public_playlist(fake, store):
    bid = make_blend(store)
    run_id = service.run_now(store, bid, write=True)
    run = store.runs(bid, 1)[0]
    assert run.id == run_id and run.status == "ok", run.message
    assert len(run.result["tracks"]) == 50
    assert run.result["playlist_name"] == "Alex + Jordan Blend"
    pl = fake.playlists[run.result["playlist_id"]]
    assert pl["public"] is True and pl["owner"] == "alex" and len(pl["songs"]) == 50
    assert {"both", "member", "new"} <= {t["kind"] for t in run.result["tracks"]}
    assert 0 < run.result["match"] < 100
    assert {a["name"] for a in run.result["shared"]} == {"Daft Punk", "The xx"}


def test_second_run_replaces_the_same_playlist(fake, store):
    bid = make_blend(store)
    service.run_now(store, bid)
    first = store.blend(bid).playlist_id
    service.run_now(store, bid)
    assert store.blend(bid).playlist_id == first and len(fake.playlists) == 1


def test_two_blends_make_two_playlists(fake, store):
    alex, jordan = store.ids
    duo = make_blend(store)
    solo = store.save_blend(Blend(name="Alex's mix", member_ids=[alex], owner_member_id=alex))
    service.run_now(store, duo)
    service.run_now(store, solo)
    assert len(fake.playlists) == 2
    assert {p["name"] for p in fake.playlists.values()} == {"Alex + Jordan Blend", "Alex's mix"}
    assert store.runs(solo, 1)[0].result["match"] is None  # one person: no taste match
    assert len(store.runs(duo)) == 1 and len(store.runs(solo)) == 1


def test_owner_and_weights_are_per_blend(fake, store):
    alex, jordan = store.ids
    bid = make_blend(store, owner_member_id=jordan, weights={alex: 20, jordan: 80}, both_pct=0, new_pct=0)
    service.run_now(store, bid)
    run = store.runs(bid, 1)[0]
    pl = fake.playlists[run.result["playlist_id"]]
    assert pl["owner"] == "jordan"
    counts = run.result["counts"]
    assert counts.get(f"m{jordan}", 0) > counts.get(f"m{alex}", 0)


def test_preview_does_not_touch_the_server(fake, store):
    bid = make_blend(store)
    service.run_now(store, bid, write=False)
    assert store.runs(bid, 1)[0].status == "preview" and fake.playlists == {}


def test_wrong_password_is_reported(fake, store):
    m = store.members()[1]
    m.password = "nope"
    store.save_member(m)
    bid = make_blend(store)
    service.run_now(store, bid)
    run = store.runs(bid, 1)[0]
    assert run.status == "error" and "password" in run.message.lower()


def test_new_music_is_unplayed_by_everyone(fake, store):
    bid = make_blend(store)
    service.run_now(store, bid, write=False)
    tracks = store.runs(bid, 1)[0].result["tracks"]
    new_ids = {t["id"] for t in tracks if t["kind"] == "new"}
    assert new_ids and not (new_ids & (set(fake.plays["alex"]) | set(fake.plays["jordan"])))


def test_removing_a_person_takes_them_out_of_blends(store):
    alex, jordan = store.ids
    bid = make_blend(store, owner_member_id=jordan)
    store.delete_member(jordan)
    b = store.blend(bid)
    assert b.member_ids == [alex] and b.owner_member_id == 0


def test_migrates_a_v01_single_blend(tmp_path):
    db = sqlite3.connect(tmp_path / "naviblend.db")
    db.executescript("""
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE members (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, color TEXT NOT NULL,
            username TEXT NOT NULL, password_enc TEXT NOT NULL, lb_user TEXT NOT NULL DEFAULT '',
            lastfm_user TEXT NOT NULL DEFAULT '', weight INTEGER NOT NULL DEFAULT 50, enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, status TEXT NOT NULL,
            match_pct INTEGER, message TEXT NOT NULL DEFAULT '', result_json TEXT NOT NULL DEFAULT '{}');
        INSERT INTO members(name,color,username,password_enc,weight,enabled) VALUES ('Alex','#5B9BFF','alex','',60,1),
            ('Jordan','#FFA24C','jordan','',40,1), ('Old','#3DD6B0','old','',50,0);
        INSERT INTO settings VALUES ('length','70'), ('playlist_name','Kitchen'), ('playlist_id','pl9'),
            ('schedule_day','fri');
        INSERT INTO runs(created_at,status) VALUES ('2026-11-20T06:00:00+00:00','ok');
    """)
    db.commit()
    db.close()
    s = Store(tmp_path)
    [b] = s.blends()
    assert (b.name, b.length, b.playlist_id, b.schedule_day, b.member_ids) == ("Kitchen", 70, "pl9", "fri", [1, 2])
    assert b.weight(1) == 60 and b.weight(2) == 40
    assert len(s.runs(b.id)) == 1
