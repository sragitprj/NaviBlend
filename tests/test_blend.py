from datetime import datetime, timezone

from naviblend.blend import Profile, assemble, song_score, taste_match, shared_artists, _split
from naviblend.store import Member
from naviblend.subsonic import Song

NOW = datetime(2026, 11, 23, tzinfo=timezone.utc)


def song(i, artist, plays=0):
    return Song(id=f"s{i}", title=f"Track {i}", artist=artist, artist_id=artist.lower(), album="A",
                duration=200, play_count=plays, played=NOW)


def profile(mid, name, songs, weight=50):
    p = Profile(member=Member(id=mid, name=name, color="#fff", username=name, password="x", weight=weight))
    for s in songs:
        p.songs[s.id] = s
        if s.play_count:
            p.scores[s.id] = song_score(s, NOW)
            k = s.artist_key
            p.artist_scores[k] = p.artist_scores.get(k, 0) + p.scores[s.id]
            p.artist_plays[k] = p.artist_plays.get(k, 0) + s.play_count
            p.artist_names[k] = s.artist
            p.artist_ids[k] = s.artist_id
    return p


ALEX_ONLY = [f"Rock {i}" for i in range(8)]
JORDAN_ONLY = [f"Pop {i}" for i in range(8)]
SHARED = ["Daft Punk", "The xx", "Four Tet", "BAYNK"]


def library():
    artists = SHARED + ALEX_ONLY + JORDAN_ONLY
    return [song(i, artists[i % len(artists)]) for i in range(400)]


def make_pair():
    lib = library()
    alex = [Song(**{**s.__dict__, "play_count": (10 if s.artist in SHARED + ALEX_ONLY else 0)}) for s in lib]
    jordan = [Song(**{**s.__dict__, "play_count": (8 if s.artist in SHARED + JORDAN_ONLY else 0)}) for s in lib]
    return profile(1, "Alex", alex), profile(2, "Jordan", jordan)


def test_small_library_still_fills_playlist():
    lib = library()
    few = [Song(**{**s.__dict__, "play_count": 5 if s.artist in ("Daft Punk", "The xx") else 0}) for s in lib]
    picks = assemble([profile(1, "Alex", few)], [], length=20)
    assert len(picks) == 20


def test_split_is_exact():
    assert sum(_split(35, {1: 50, 2: 50, 3: 20}).values()) == 35
    assert _split(10, {1: 0, 2: 0}) == {1: 0, 2: 0}


def test_blend_has_requested_length_and_mix():
    alex, jordan = make_pair()
    fresh = [(song(900 + i, f"New Artist {i}"), "Similar to Daft Punk") for i in range(20)]
    picks = assemble([alex, jordan], fresh, length=40, both_pct=25, new_pct=25)
    assert len(picks) == 40
    assert len({p.song.id for p in picks}) == 40, "no duplicates"
    kinds = [p.kind for p in picks]
    assert kinds.count("new") == 10
    assert kinds.count("both") == 10
    # personal picks are split evenly by weight
    assert sum(1 for p in picks if p.kind == "member" and p.member_id == 1) == 10
    assert sum(1 for p in picks if p.kind == "member" and p.member_id == 2) == 10


def test_no_same_artist_back_to_back_when_avoidable():
    alex, jordan = make_pair()
    picks = assemble([alex, jordan], [], length=30)
    repeats = sum(1 for a, b in zip(picks, picks[1:]) if a.song.artist_key == b.song.artist_key)
    assert repeats <= 2


def test_artist_cap():
    alex, jordan = make_pair()
    picks = assemble([alex, jordan], [], length=48)
    per = {}
    for p in picks:
        per[p.song.artist_key] = per.get(p.song.artist_key, 0) + 1
    assert max(per.values()) <= max(2, 48 // 12) or len(picks) < 48


def test_weights_shift_the_mix():
    alex, jordan = make_pair()
    alex.member.weight, jordan.member.weight = 75, 25
    picks = assemble([alex, jordan], [], length=40, both_pct=0, new_pct=0)
    s = sum(1 for p in picks if p.member_id == 1)
    e = sum(1 for p in picks if p.member_id == 2)
    assert s > e


def test_person_without_history_does_not_break_it():
    alex, _ = make_pair()
    nobody = profile(3, "New", [])
    picks = assemble([alex, nobody], [], length=20)
    assert len(picks) == 20
    assert all(p.member_id in (0, 1) for p in picks)


def test_taste_match_and_shared_artists():
    alex, jordan = make_pair()
    m = taste_match([alex, jordan])
    assert 0 < m < 100
    assert taste_match([alex, alex]) == 100
    names = [a["name"] for a in shared_artists([alex, jordan])]
    assert set(names) == set(SHARED)


def test_skip_recent():
    alex, jordan = make_pair()
    skip = {s for s in alex.scores}
    picks = assemble([alex, jordan], [], length=20, skip_ids=skip)
    assert not any(p.song.id in skip for p in picks)
