"""A tiny in-memory Subsonic server for tests (per-user play counts, playlists)."""
from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl

import httpx

ARTISTS = {f"ar{i}": name for i, name in enumerate(
    ["Daft Punk", "The xx", "Rammstein", "Gojira", "Kali Uchis", "Four Tet", "BAYNK", "Justice", "Air", "Moderat"])}


class FakeSubsonic:
    def __init__(self):
        self.users = {"alex": "pw1", "jordan": "pw2"}
        self.albums = {}
        self.songs = {}
        n = 0
        for aid, artist in ARTISTS.items():
            for a in range(2):
                alb = f"al_{aid}_{a}"
                self.albums[alb] = {"id": alb, "name": f"{artist} LP{a}", "artist": artist, "artistId": aid, "songs": []}
                for t in range(8):
                    sid = f"so{n}"
                    n += 1
                    self.songs[sid] = {"id": sid, "title": f"{artist} song {a}-{t}", "artist": artist,
                                       "artistId": aid, "album": f"{artist} LP{a}", "albumId": alb, "duration": 210}
                    self.albums[alb]["songs"].append(sid)
        likes = {"alex": ["Daft Punk", "The xx", "Rammstein", "Gojira"], "jordan": ["Daft Punk", "The xx", "Kali Uchis", "BAYNK"]}
        self.plays = {u: {sid: (12 - int(s["title"][-1])) for sid, s in self.songs.items() if s["artist"] in likes[u]}
                      for u in self.users}
        self.playlists: dict[str, dict] = {}
        self.calls: list[str] = []

    # -- helpers --------------------------------------------------------
    def _song(self, sid, user):
        s = dict(self.songs[sid])
        pc = self.plays[user].get(sid, 0)
        if pc:
            s["playCount"] = pc
            s["played"] = "2026-11-20T10:00:00Z"
        return s

    def _ok(self, **payload):
        return httpx.Response(200, json={"subsonic-response": {"status": "ok", "version": "1.16.1",
                                                               "type": "fake", "serverVersion": "1.0", **payload}})

    def _err(self, code, msg):
        return httpx.Response(200, json={"subsonic-response": {"status": "failed", "error": {"code": code, "message": msg}}})

    # -- dispatcher -----------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/auth/login":  # Navidrome native API
            import json
            body = json.loads(request.content or b"{}")
            if self.users.get(body.get("username")) == body.get("password"):
                return httpx.Response(200, json={"token": "tok-" + body["username"]})
            return httpx.Response(401, json={"error": "bad login"})
        if path.startswith("/api/playlist/") and path.endswith("/image"):
            pid = path.split("/")[3]
            if not request.headers.get("x-nd-authorization", "").startswith("Bearer tok-") or pid not in self.playlists:
                return httpx.Response(403)
            self.playlists[pid]["image"] = request.content
            return httpx.Response(200, json={})
        method = path.rsplit("/", 1)[-1]
        pairs = parse_qsl(request.url.query.decode())
        q = dict(pairs)
        self.calls.append(method)
        if method == "ping" and "u" not in q:
            return self._err(10, "Required parameter is missing")
        user = q.get("u")
        if user not in self.users or hashlib.md5((self.users[user] + q.get("s", "")).encode()).hexdigest() != q.get("t"):
            return self._err(40, "Wrong username or password")
        if method == "ping":
            return self._ok()
        if method == "getAlbumList2":
            played = {self.songs[s]["albumId"] for s in self.plays[user]}
            al = [{"id": a, "name": self.albums[a]["name"]} for a in self.albums if a in played]
            return self._ok(albumList2={"album": al[: int(q.get("size", 50))]})
        if method == "getAlbum":
            a = self.albums[q["id"]]
            return self._ok(album={"id": a["id"], "song": [self._song(s, user) for s in a["songs"]]})
        if method == "getStarred2":
            return self._ok(starred2={"song": []})
        if method == "getArtistInfo2":
            others = [k for k in reversed(list(ARTISTS)) if k != q["id"]][: int(q.get("count", 10))]
            return self._ok(artistInfo2={"similarArtist": [{"id": k, "name": ARTISTS[k]} for k in others]})
        if method == "getTopSongs":
            sids = [s for s in self.songs if self.songs[s]["artist"] == q["artist"]][: int(q.get("count", 5))]
            return self._ok(topSongs={"song": [self._song(s, user) for s in sids]})
        if method == "getArtist":
            return self._ok(artist={"album": [{"id": a} for a in self.albums if self.albums[a]["artistId"] == q["id"]]})
        if method == "search3":
            hits = [self._song(s, user) for s in self.songs if q["query"].lower() in self.songs[s]["title"].lower()]
            return self._ok(searchResult3={"song": hits[:10]})
        if method == "getPlaylists":
            return self._ok(playlists={"playlist": [{"id": k, "name": v["name"]} for k, v in self.playlists.items()]})
        if method == "createPlaylist":
            ids = [v for k, v in pairs if k == "songId"]
            pid = q.get("playlistId") or f"pl{len(self.playlists) + 1}"
            pl = self.playlists.setdefault(pid, {"name": q.get("name", ""), "owner": user})
            pl["songs"] = ids
            return self._ok(playlist={"id": pid})
        if method == "updatePlaylist":
            pl = self.playlists[q["playlistId"]]
            pl.update(name=q.get("name", pl["name"]), public=q.get("public") == "true", comment=q.get("comment", ""))
            return self._ok()
        return self._err(70, f"unknown method {method}")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
