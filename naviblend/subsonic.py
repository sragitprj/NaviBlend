"""Minimal Subsonic / OpenSubsonic API client (works with Navidrome, Gonic, Ampache...)."""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

API_VERSION = "1.16.1"
CLIENT = "NaviBlend"


class SubsonicError(Exception):
    pass


@dataclass
class Song:
    id: str
    title: str
    artist: str
    artist_id: str
    album: str
    duration: int
    play_count: int = 0
    played: datetime | None = None
    starred: bool = False

    @property
    def artist_key(self) -> str:
        return (self.artist or "").strip().lower()


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def song_from(d: dict[str, Any]) -> Song:
    return Song(
        id=str(d.get("id")),
        title=d.get("title") or "",
        artist=d.get("artist") or d.get("displayArtist") or "",
        artist_id=str(d.get("artistId") or ""),
        album=d.get("album") or "",
        duration=int(d.get("duration") or 0),
        play_count=int(d.get("playCount") or 0),
        played=_parse_date(d.get("played")),
        starred=bool(d.get("starred")),
    )


class Subsonic:
    transport: httpx.BaseTransport | None = None  # tests swap in a fake server here

    def __init__(self, base_url: str, username: str, password: str, timeout: float = 30.0):
        self.base = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.http = httpx.Client(timeout=timeout, follow_redirects=True, transport=Subsonic.transport)

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "Subsonic":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _auth(self) -> dict[str, str]:
        salt = secrets.token_hex(8)
        token = hashlib.md5((self.password + salt).encode()).hexdigest()
        return {"u": self.username, "t": token, "s": salt, "v": API_VERSION, "c": CLIENT, "f": "json"}

    def call(self, method: str, params: dict[str, Any] | list[tuple[str, Any]] | None = None) -> dict[str, Any]:
        query: list[tuple[str, Any]] = list(self._auth().items())
        if isinstance(params, dict):
            query += list(params.items())
        elif params:
            query += params
        try:
            r = self.http.get(f"{self.base}/rest/{method}", params=query)
        except httpx.HTTPError as e:
            raise SubsonicError(f"Could not reach the server: {e}") from e
        if r.status_code != 200:
            raise SubsonicError(f"{method}: HTTP {r.status_code}")
        try:
            body = r.json()["subsonic-response"]
        except (ValueError, KeyError) as e:
            raise SubsonicError("That address did not answer like a Subsonic server") from e
        if body.get("status") != "ok":
            err = body.get("error", {})
            raise SubsonicError(err.get("message") or f"{method} failed (code {err.get('code')})")
        return body

    # -- probes ---------------------------------------------------------
    @staticmethod
    def probe(base_url: str) -> str:
        """Check an address is a Subsonic server without credentials. Returns its type/version."""
        try:
            r = httpx.get(f"{base_url.rstrip('/')}/rest/ping",
                          params={"v": API_VERSION, "c": CLIENT, "f": "json"}, timeout=10, follow_redirects=True)
            body = r.json()["subsonic-response"]
        except Exception as e:  # noqa: BLE001 - surfaced to the user
            raise SubsonicError("No Subsonic server answered at that address") from e
        kind = body.get("type") or "Subsonic"
        ver = body.get("serverVersion") or body.get("version") or ""
        return f"{kind.capitalize()} {ver}".strip()

    def ping(self) -> dict[str, Any]:
        return self.call("ping")

    # -- library reads (per-user: play counts and stars are the caller's) -
    def album_list(self, kind: str, size: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        body = self.call("getAlbumList2", {"type": kind, "size": size, "offset": offset})
        return body.get("albumList2", {}).get("album", []) or []

    def album_songs(self, album_id: str) -> list[Song]:
        body = self.call("getAlbum", {"id": album_id})
        return [song_from(s) for s in body.get("album", {}).get("song", []) or []]

    def starred_songs(self) -> list[Song]:
        body = self.call("getStarred2")
        return [song_from(s) for s in body.get("starred2", {}).get("song", []) or []]

    def search_songs(self, query: str, count: int = 10) -> list[Song]:
        body = self.call("search3", {"query": query, "songCount": count, "albumCount": 0, "artistCount": 0})
        return [song_from(s) for s in body.get("searchResult3", {}).get("song", []) or []]

    def similar_artists(self, artist_id: str, count: int = 10) -> list[dict[str, Any]]:
        try:
            body = self.call("getArtistInfo2", {"id": artist_id, "count": count})
        except SubsonicError:
            return []
        return body.get("artistInfo2", {}).get("similarArtist", []) or []

    def top_songs(self, artist_name: str, count: int = 10) -> list[Song]:
        try:
            body = self.call("getTopSongs", {"artist": artist_name, "count": count})
        except SubsonicError:
            return []
        return [song_from(s) for s in body.get("topSongs", {}).get("song", []) or []]

    def artist_albums(self, artist_id: str) -> list[dict[str, Any]]:
        try:
            body = self.call("getArtist", {"id": artist_id})
        except SubsonicError:
            return []
        return body.get("artist", {}).get("album", []) or []

    # -- playlists ------------------------------------------------------
    def playlists(self) -> list[dict[str, Any]]:
        body = self.call("getPlaylists")
        return body.get("playlists", {}).get("playlist", []) or []

    def write_playlist(self, name: str, song_ids: list[str], playlist_id: str = "",
                       public: bool = True, comment: str = "") -> str:
        """Create the playlist, or replace the songs of an existing one. Returns its id."""
        params: list[tuple[str, Any]] = [("songId", s) for s in song_ids]
        if playlist_id and any(str(p.get("id")) == playlist_id for p in self.playlists()):
            params.append(("playlistId", playlist_id))
        else:
            playlist_id = ""
            params.append(("name", name))
        body = self.call("createPlaylist", params)
        new_id = str(body.get("playlist", {}).get("id") or playlist_id)
        if not new_id:  # very old servers return nothing: look it up by name
            new_id = next((str(p["id"]) for p in self.playlists() if p.get("name") == name), "")
        if new_id:
            self.call("updatePlaylist", {"playlistId": new_id, "name": name,
                                         "public": "true" if public else "false", "comment": comment})
        return new_id
