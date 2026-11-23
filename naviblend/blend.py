"""The blend engine: build each person's taste profile, then mix a shared playlist.

The mixing step (`assemble`) is a pure function so it can be tested without a server.
"""
from __future__ import annotations

import logging
import math
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

from . import external
from .store import Member
from .subsonic import Song, Subsonic, SubsonicError

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- profiles
@dataclass
class Profile:
    member: Member
    songs: dict[str, Song] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)        # song id -> taste score
    artist_scores: dict[str, float] = field(default_factory=dict)  # artist key -> score
    artist_plays: dict[str, int] = field(default_factory=dict)
    artist_names: dict[str, str] = field(default_factory=dict)
    artist_ids: dict[str, str] = field(default_factory=dict)

    @property
    def has_data(self) -> bool:
        return bool(self.scores)

    def normalized(self) -> dict[str, float]:
        top = max(self.scores.values(), default=0) or 1
        return {k: v / top for k, v in self.scores.items()}

    def recent_ids(self, days: int, now: datetime) -> set[str]:
        if days <= 0:
            return set()
        cutoff = now - timedelta(days=days)
        return {s.id for s in self.songs.values() if s.played and s.played >= cutoff}


def _norm(text: str) -> str:
    text = re.sub(r"\(.*?\)|\[.*?\]", "", text.lower())
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def song_score(s: Song, now: datetime) -> float:
    score = math.log1p(s.play_count)
    if s.played:
        days = (now - s.played).days
        score *= 1.5 if days < 30 else 1.0 if days < 120 else 0.7
    else:
        score *= 0.85
    if s.starred:
        score += 1.5
    return score


def build_profile(client: Subsonic, member: Member, lastfm_key: str = "",
                  now: datetime | None = None, album_limit: int = 60) -> Profile:
    """Read one person's history through their own Subsonic login (+ optional ListenBrainz/Last.fm)."""
    now = now or datetime.now(timezone.utc)
    p = Profile(member=member)

    album_ids: list[str] = []
    for kind, size in (("frequent", album_limit), ("recent", album_limit // 2)):
        for a in client.album_list(kind, size=size):
            if str(a.get("id")) not in album_ids:
                album_ids.append(str(a.get("id")))

    def fetch(aid: str) -> list[Song]:
        try:
            return client.album_songs(aid)
        except SubsonicError as e:
            log.warning("album %s skipped: %s", aid, e)
            return []

    with ThreadPoolExecutor(max_workers=6) as pool:
        for songs in pool.map(fetch, album_ids):
            for s in songs:
                p.songs[s.id] = s
    for s in client.starred_songs():
        p.songs[s.id] = s

    for s in p.songs.values():
        if s.play_count > 0 or s.starred:
            p.scores[s.id] = song_score(s, now)

    # Optional outside history: matched against the library by title + artist.
    outside = external.listenbrainz_top(member.lb_user)[:60] + external.lastfm_top(member.lastfm_user, lastfm_key)[:60]
    for rank, (artist, title) in enumerate(outside):
        match = _match_in_library(client, artist, title)
        if match:
            p.songs.setdefault(match.id, match)
            p.scores[match.id] = p.scores.get(match.id, 0) + 2.0 * (1 - rank / max(len(outside), 1))

    for sid, sc in p.scores.items():
        s = p.songs[sid]
        key = s.artist_key
        if not key:
            continue
        p.artist_scores[key] = p.artist_scores.get(key, 0) + sc
        p.artist_plays[key] = p.artist_plays.get(key, 0) + s.play_count
        p.artist_names.setdefault(key, s.artist)
        if s.artist_id:
            p.artist_ids.setdefault(key, s.artist_id)
    return p


def _match_in_library(client: Subsonic, artist: str, title: str) -> Song | None:
    if not title:
        return None
    try:
        hits = client.search_songs(title, count=15)
    except SubsonicError:
        return None
    want_t, want_a = _norm(title), _norm(artist)
    for s in hits:
        if _norm(s.title) == want_t and (not want_a or want_a in _norm(s.artist) or _norm(s.artist) in want_a):
            return s
    return None


# ---------------------------------------------------------------- taste match
def taste_match(profiles: list[Profile]) -> int | None:
    """Weighted artist overlap between people, as a friendly 0-100 score.

    Each person's artist scores are turned into shares of their listening; the overlap is the
    sum of the smaller share per artist (1.0 = identical). Shown as sqrt(overlap) x 100,
    because raw overlaps between two real people are usually small.
    """
    ps = [p for p in profiles if p.artist_scores]
    if len(ps) < 2:
        return None
    vals = []
    for i in range(len(ps)):
        for j in range(i + 1, len(ps)):
            a, b = ps[i].artist_scores, ps[j].artist_scores
            ta, tb = sum(a.values()) or 1, sum(b.values()) or 1
            overlap = sum(min(a[k] / ta, b[k] / tb) for k in set(a) & set(b))
            vals.append(math.sqrt(overlap))
    return round(100 * sum(vals) / len(vals))


def shared_artists(profiles: list[Profile], limit: int = 5) -> list[dict]:
    ps = [p for p in profiles if p.artist_scores]
    if len(ps) < 2:
        return []
    common = set.intersection(*(set(p.artist_scores) for p in ps))
    ranked = sorted(common, key=lambda k: min(p.artist_scores[k] / max(p.artist_scores.values()) for p in ps),
                    reverse=True)
    return [{"name": ps[0].artist_names.get(k, k), "plays": sum(p.artist_plays.get(k, 0) for p in ps)}
            for k in ranked[:limit]]


# ---------------------------------------------------------------- new music
def find_new_music(client: Subsonic, profiles: list[Profile], want: int) -> list[tuple[Song, str]]:
    """Unplayed library songs by artists similar to the ones everyone likes."""
    if want <= 0:
        return []
    ps = [p for p in profiles if p.artist_scores]
    if not ps:
        return []
    played = {sid for p in ps for sid, s in p.songs.items() if s.play_count > 0}
    known_artists = {k for p in ps for k in p.artist_scores}

    if len(ps) >= 2:
        common = set.intersection(*(set(p.artist_scores) for p in ps))
        seeds = sorted(common, key=lambda k: sum(p.artist_scores[k] for p in ps), reverse=True)[:8]
    else:
        seeds = []
    for p in ps:  # top up with each person's favourites
        for k in sorted(p.artist_scores, key=p.artist_scores.get, reverse=True)[:4]:
            if k not in seeds:
                seeds.append(k)

    out: list[tuple[Song, str]] = []
    seen: set[str] = set()
    for key in seeds:
        if len(out) >= want * 2:
            break
        seed_name = next((p.artist_names[key] for p in ps if key in p.artist_names), key)
        seed_id = next((p.artist_ids[key] for p in ps if key in p.artist_ids), "")
        if not seed_id:
            continue
        for sim in client.similar_artists(seed_id, count=6):
            sim_name = sim.get("name") or ""
            if not sim.get("id") or _norm(sim_name) in {_norm(k) for k in known_artists}:
                continue
            songs = client.top_songs(sim_name, count=5)
            if not songs:  # server has no top-songs source: take tracks from an album instead
                albums = client.artist_albums(str(sim["id"]))
                songs = client.album_songs(str(albums[0]["id"]))[:5] if albums else []
            for s in songs[:3]:
                if s.id in played or s.id in seen:
                    continue
                seen.add(s.id)
                out.append((s, f"Similar to {seed_name}"))
    return out


# ---------------------------------------------------------------- assembly
@dataclass
class Pick:
    song: Song
    kind: str          # both | member | new
    member_id: int = 0
    detail: str = ""


def _split(total: int, weights: dict[int, float]) -> dict[int, int]:
    """Largest-remainder split of `total` slots by weight."""
    w = {k: max(v, 0) for k, v in weights.items()}
    s = sum(w.values())
    if total <= 0 or s <= 0:
        return {k: 0 for k in weights}
    raw = {k: total * v / s for k, v in w.items()}
    out = {k: int(v) for k, v in raw.items()}
    for k in sorted(raw, key=lambda k: raw[k] - out[k], reverse=True)[: total - sum(out.values())]:
        out[k] += 1
    return out


def assemble(profiles: list[Profile], new_candidates: Iterable[tuple[Song, str]], length: int = 50,
             both_pct: int = 30, new_pct: int = 20, interleave: bool = True,
             skip_ids: set[str] | None = None) -> list[Pick]:
    skip_ids = skip_ids or set()
    ps = [p for p in profiles if p.has_data]
    if not ps:
        return []
    norms = {p.member.id: p.normalized() for p in ps}
    songs: dict[str, Song] = {}
    for p in ps:
        songs.update(p.songs)

    # Pools ---------------------------------------------------------------
    both: list[Pick] = []
    if len(ps) >= 2:
        shared = [sid for sid in songs if sum(1 for p in ps if sid in norms[p.member.id]) >= 2]
        shared.sort(key=lambda sid: sum(norms[p.member.id].get(sid, 0) for p in ps), reverse=True)
        both = [Pick(songs[sid], "both", detail="You both play it") for sid in shared]
        common = set.intersection(*(set(p.artist_scores) for p in ps))
        art_rank = sorted(common, key=lambda k: sum(p.artist_scores[k] for p in ps), reverse=True)
        for key in art_rank:
            for p in ps:
                for sid in sorted((s for s in norms[p.member.id] if songs[s].artist_key == key),
                                  key=norms[p.member.id].get, reverse=True)[:2]:
                    both.append(Pick(songs[sid], "both", detail=f"You both like {songs[sid].artist}"))
    personal = {
        p.member.id: [Pick(songs[sid], "member", p.member.id, f"{p.member.name}'s top")
                      for sid in sorted(norms[p.member.id], key=norms[p.member.id].get, reverse=True)]
        for p in ps
    }
    fresh = [Pick(s, "new", detail=why) for s, why in new_candidates]

    # Targets -------------------------------------------------------------
    n_both = round(length * both_pct / 100) if len(ps) >= 2 else 0
    n_new = round(length * new_pct / 100)
    shares = _split(length - n_both - n_new, {p.member.id: p.member.weight for p in ps})

    used: set[str] = set()
    per_artist: dict[str, int] = defaultdict(int)
    limit = {"cap": max(2, length // 12)}  # loosened below if the library is too small

    def take(pool: list[Pick], n: int) -> list[Pick]:
        got: list[Pick] = []
        for pk in pool:
            if len(got) >= n:
                break
            s = pk.song
            if s.id in used or s.id in skip_ids or per_artist[s.artist_key] >= limit["cap"]:
                continue
            used.add(s.id)
            per_artist[s.artist_key] += 1
            got.append(pk)
        return got

    buckets: dict[str, list[Pick]] = {"both": take(both, n_both)}
    for mid, n in shares.items():
        buckets[f"m{mid}"] = take(personal[mid], n)
    buckets["new"] = take(fresh, n_new)

    # Short pools: top up from the personal pools, then shared. If the artist cap is what's
    # stopping us, loosen it a little before leaning harder on new music.
    short = length - sum(len(b) for b in buckets.values())
    order = [f"m{p.member.id}" for p in sorted(ps, key=lambda p: -p.member.weight)]
    weight = {f"m{p.member.id}": max(p.member.weight, 0) for p in ps}
    target = {f"m{mid}": n for mid, n in shares.items()}
    soft_cap = max(limit["cap"] + 2, length // 6)

    def fill(key: str, pool: list[Pick], n: int) -> int:
        extra = take(pool, n)
        buckets[key] += extra
        return len(extra)

    while short > 0:
        before = short
        # 1. people still below their own share, then the shared pool below its share
        for key in order:
            if short > 0 and len(buckets[key]) < target[key]:
                short -= fill(key, personal[int(key[1:])], min(short, target[key] - len(buckets[key])))
        if short > 0 and len(buckets["both"]) < n_both:
            short -= fill("both", both, min(short, n_both - len(buckets["both"])))
        if short != before:
            continue
        if limit["cap"] < soft_cap:  # the artist cap is what's stopping us: loosen it a little
            limit["cap"] += 1
            continue
        # 2. still short: give extra slots to whoever is furthest below their weighted share
        for key in sorted((k for k in order if weight[k] > 0),
                          key=lambda k: len(buckets[k]) / weight[k]):
            if short > 0:
                short -= fill(key, personal[int(key[1:])], 1)
                if short != before:
                    break
        if short == before and short > 0:
            short -= fill("both", both, 1)
        if short == before and short > 0 and n_new:
            short -= fill("new", fresh, short)
        if short == before:
            if limit["cap"] >= length:
                break
            limit["cap"] += 1  # very small library: allow more tracks per artist

    # Ordering ------------------------------------------------------------
    if not interleave:
        return [pk for key in ["both", *order, "new"] for pk in buckets.get(key, [])]
    return _interleave(buckets)


def _interleave(buckets: dict[str, list[Pick]]) -> list[Pick]:
    """Spread buckets evenly through the playlist and avoid the same artist twice in a row."""
    total = sum(len(b) for b in buckets.values())
    queues = {k: list(v) for k, v in buckets.items() if v}
    placed = defaultdict(int)
    out: list[Pick] = []
    prio = {"both": 2, "new": 0}  # on a tie: shared first, new music last
    while queues:
        n = len(out) + 1
        key = max(queues, key=lambda k: (round(len(buckets[k]) * n / total - placed[k], 6), prio.get(k, 1)))
        q = queues[key]
        idx = 0
        if out:
            last = out[-1].song.artist_key
            idx = next((i for i, pk in enumerate(q) if pk.song.artist_key != last), 0)
        out.append(q.pop(idx))
        placed[key] += 1
        if not q:
            del queues[key]
    return out


# ---------------------------------------------------------------- whole run
@dataclass
class BlendResult:
    picks: list[Pick]
    match_pct: int | None
    shared: list[dict]
    profiles: list[Profile]

    def to_dict(self) -> dict:
        names = {p.member.id: p.member.name for p in self.profiles}
        counts: dict[str, int] = defaultdict(int)
        for pk in self.picks:
            counts[pk.kind if pk.kind != "member" else f"m{pk.member_id}"] += 1
        return {
            "tracks": [
                {"id": pk.song.id, "title": pk.song.title, "artist": pk.song.artist, "album": pk.song.album,
                 "duration": pk.song.duration, "kind": pk.kind, "member_id": pk.member_id,
                 "who": names.get(pk.member_id, ""), "detail": pk.detail}
                for pk in self.picks
            ],
            "match": self.match_pct,
            "shared": self.shared,
            "counts": dict(counts),
            "members": [{"id": p.member.id, "name": p.member.name, "color": p.member.color,
                         "songs": len(p.scores)} for p in self.profiles],
            "duration": sum(pk.song.duration for pk in self.picks),
        }


def run_blend(server_url: str, members: list[Member], settings: dict[str, str]) -> BlendResult:
    now = datetime.now(timezone.utc)
    lastfm_key = settings.get("lastfm_api_key", "")
    length = int(settings.get("length") or 50)
    new_pct = int(settings.get("new_pct") or 20)

    def profile(m: Member) -> Profile:
        with Subsonic(server_url, m.username, m.password) as c:
            return build_profile(c, m, lastfm_key, now)

    with ThreadPoolExecutor(max_workers=max(1, len(members))) as pool:
        profiles = list(pool.map(profile, members))
    if not any(p.has_data for p in profiles):
        raise SubsonicError("Nobody has any listening history yet, so there is nothing to blend.")

    days = int(settings.get("skip_recent_days") or 0)
    skip = set().union(*(p.recent_ids(days, now) for p in profiles)) if days else set()

    first = members[0]
    with Subsonic(server_url, first.username, first.password) as c:
        fresh = find_new_music(c, profiles, round(length * new_pct / 100))

    picks = assemble(profiles, fresh, length=length, both_pct=int(settings.get("both_pct") or 30),
                     new_pct=new_pct, interleave=settings.get("interleave", "1") == "1", skip_ids=skip)
    return BlendResult(picks=picks, match_pct=taste_match(profiles), shared=shared_artists(profiles),
                       profiles=profiles)
