"""Optional listening history from ListenBrainz and Last.fm (public data only)."""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

UA = {"User-Agent": "NaviBlend/0.1 (+https://github.com/sragitprj/NaviBlend)"}


def listenbrainz_top(username: str, count: int = 100, range_: str = "quarter") -> list[tuple[str, str]]:
    """(artist, title) pairs from a user's public ListenBrainz stats, most played first."""
    if not username:
        return []
    url = f"https://api.listenbrainz.org/1/stats/user/{username}/recordings"
    try:
        r = httpx.get(url, params={"range": range_, "count": count}, headers=UA, timeout=20)
        if r.status_code == 204:  # stats not computed yet
            return []
        r.raise_for_status()
        recs = r.json().get("payload", {}).get("recordings", [])
    except (httpx.HTTPError, ValueError) as e:
        log.warning("ListenBrainz stats for %s unavailable: %s", username, e)
        return []
    return [(x.get("artist_name", ""), x.get("track_name", "")) for x in recs if x.get("track_name")]


def lastfm_top(username: str, api_key: str, count: int = 100, period: str = "3month") -> list[tuple[str, str]]:
    """(artist, title) pairs from a user's Last.fm top tracks. Needs a free Last.fm API key."""
    if not username or not api_key:
        return []
    try:
        r = httpx.get(
            "https://ws.audioscrobbler.com/2.0/",
            params={"method": "user.gettoptracks", "user": username, "api_key": api_key,
                    "period": period, "limit": count, "format": "json"},
            headers=UA, timeout=20,
        )
        r.raise_for_status()
        tracks = r.json().get("toptracks", {}).get("track", [])
    except (httpx.HTTPError, ValueError) as e:
        log.warning("Last.fm top tracks for %s unavailable: %s", username, e)
        return []
    return [((t.get("artist") or {}).get("name", ""), t.get("name", "")) for t in tracks if t.get("name")]
