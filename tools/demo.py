"""Run NaviBlend against an in-memory demo library (for screenshots / trying the UI).

    python tools/demo.py   ->  http://127.0.0.1:4545  (password: demo-password)
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn  # noqa: E402

from naviblend import external, service  # noqa: E402
from naviblend.store import Blend, Member, Store  # noqa: E402
from naviblend.subsonic import Subsonic  # noqa: E402
from naviblend.web import create_app, hash_password  # noqa: E402
from tests.fake_server import FakeSubsonic  # noqa: E402

LIBRARY = {
    "Daft Punk": ("Discovery", ["One More Time", "Aerodynamic", "Digital Love", "Harder, Better, Faster, Stronger", "Something About Us", "Veridis Quo"]),
    "The xx": ("xx", ["Intro", "VCR", "Crystalised", "Islands", "Heart Skipped a Beat", "Shelter"]),
    "Four Tet": ("Sixteen Oceans", ["School", "Baby", "Teenage Birdsong", "Love Salad", "Insect Near Piha Beach", "Romantics"]),
    "Queens of the Stone Age": ("Songs for the Deaf", ["No One Knows", "Go with the Flow", "First It Giveth", "Song for the Dead", "Mosquito Song", "Hangin' Tree"]),
    "Gojira": ("Magma", ["The Shooting Star", "Silvera", "Stranded", "Yellow Stone", "Magma", "Pray"]),
    "Kali Uchis": ("Isolation", ["After the Storm", "Tyrant", "Dead to Me", "Just a Stranger", "Miami", "Feel Like a Fool"]),
    "Little Simz": ("Grey Area", ["Offence", "Boss", "Selfish", "Wounds", "Venom", "Pressure"]),
    "Bonobo": ("Migration", ["Kerala", "Break Apart", "Surface", "Outlier", "Bambro Koyo Ganda", "Ontario"]),
    "Justice": ("Cross", ["Genesis", "D.A.N.C.E.", "Phantom", "DVNO", "Stress", "Waters of Nazareth"]),
    "Air": ("Moon Safari", ["La femme d'argent", "Sexy Boy", "All I Need", "Kelly Watch the Stars", "Talisman", "Remember"]),
    "Moderat": ("II", ["Bad Kingdom", "Last Time", "Gita", "Milk", "Damage Done", "Clouded"]),
    "Floating Points": ("Crush", ["Last Bloom", "Anasickmodular", "Bias", "LesAlpx", "Requiem for CS70", "Birth4000"]),
}


class DemoServer(FakeSubsonic):
    def __init__(self):
        self.users = {"alex": "pw1", "jordan": "pw2"}
        self.albums, self.songs, self.playlists, self.calls = {}, {}, {}, []
        n = 0
        for i, (artist, (album, titles)) in enumerate(LIBRARY.items()):
            aid, alb = f"ar{i}", f"al{i}"
            self.albums[alb] = {"id": alb, "name": album, "artist": artist, "artistId": aid, "songs": []}
            for t in titles:
                sid = f"so{n}"; n += 1
                self.songs[sid] = {"id": sid, "title": t, "artist": artist, "artistId": aid, "album": album,
                                   "albumId": alb, "duration": 180 + (n * 37) % 160}
                self.albums[alb]["songs"].append(sid)
        likes = {"alex": ["Daft Punk", "The xx", "Four Tet", "Queens of the Stone Age", "Gojira"],
                 "jordan": ["Daft Punk", "The xx", "Four Tet", "Kali Uchis", "Little Simz"]}
        self.plays = {u: {sid: 3 + (hash((u, sid)) % 20) for sid, s in self.songs.items() if s["artist"] in likes[u]}
                      for u in self.users}

    def handler(self, request):
        import httpx
        from urllib.parse import parse_qsl
        if request.url.path.endswith("getArtistInfo2"):
            q = dict(parse_qsl(request.url.query.decode()))
            sim = {"ar0": ["ar8", "ar9"], "ar1": ["ar10", "ar7"], "ar2": ["ar11", "ar7"]}.get(q["id"], [])
            names = list(LIBRARY)
            return self._ok(artistInfo2={"similarArtist": [{"id": a, "name": names[int(a[2:])]} for a in sim]})
        return super().handler(request)


if __name__ == "__main__":
    srv = DemoServer()
    Subsonic.transport = srv.transport()
    Subsonic.probe = staticmethod(lambda url: "Navidrome (demo)")
    external.listenbrainz_top = lambda *a, **k: []
    external.lastfm_top = lambda *a, **k: []
    store = Store(Path(tempfile.mkdtemp()))
    store.set("server_url", "http://navidrome:4533")
    store.set("admin_hash", hash_password("demo-password"))
    alex = store.save_member(Member(0, "Alex", "#5B9BFF", "alex", "pw1", lb_user="alex"))
    jordan = store.save_member(Member(0, "Jordan", "#FFA24C", "jordan", "pw2"))
    duo = store.save_blend(Blend(member_ids=[alex, jordan], length=30))
    solo = store.save_blend(Blend(name="Alex's late-night mix", member_ids=[alex], length=25, schedule_day="daily",
                                  schedule_time="22:00"))
    service.run_now(store, duo, write=True)
    service.run_now(store, solo, write=True)
    uvicorn.run(create_app(store, start_scheduler=False), host="127.0.0.1", port=4545, log_level="warning")
