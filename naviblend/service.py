"""Runs a blend end to end and keeps the record; shared by the web UI and the scheduler."""
from __future__ import annotations

import logging
import threading
from datetime import datetime

from .blend import run_blend
from .store import Blend, Member, Store
from .subsonic import Subsonic, SubsonicError

log = logging.getLogger(__name__)
_lock = threading.Lock()


def people_title(names: list[str]) -> str:
    if not names:
        return "Empty blend"
    if len(names) <= 3:
        return " + ".join(names)
    return f"{', '.join(names[:-1])} & {names[-1]}"


def playlist_name(b: Blend, members: list[Member]) -> str:
    return b.name or f"{people_title([m.name for m in members])} Blend"


def blend_settings(b: Blend, store: Store) -> dict[str, str]:
    return {
        "lastfm_api_key": store.get("lastfm_api_key"), "length": str(b.length), "both_pct": str(b.both_pct),
        "new_pct": str(b.new_pct), "interleave": "1" if b.interleave else "0",
        "skip_recent_days": str(b.skip_recent_days),
    }


def run_now(store: Store, blend_id: int, write: bool = True) -> int | None:
    """Build one blend. With write=True its playlist on the server is replaced. Returns the run id."""
    with _lock:  # one blend at a time keeps the music server happy
        b = store.blend(blend_id)
        if b is None:
            return None
        run_id = store.start_run(blend_id)
        try:
            server = store.get("server_url")
            members = store.blend_members(b)
            if not server:
                raise SubsonicError("Connect a music server first.")
            if not members:
                raise SubsonicError("Add at least one person to this blend.")

            result = run_blend(server, members, blend_settings(b, store))
            data = result.to_dict()
            if not data["tracks"]:
                raise SubsonicError("Not enough listening history to build a playlist yet.")

            name = playlist_name(b, members)
            data["playlist_name"] = name
            status, message = "preview", f"Preview: {len(data['tracks'])} tracks (not saved)"
            if write:
                owner = next((m for m in members if m.id == b.owner_member_id), members[0])
                comment = f"Made by NaviBlend on {datetime.now():%d %b %Y} for {', '.join(m.name for m in members)}"
                with Subsonic(server, owner.username, owner.password) as c:
                    pid = c.write_playlist(name, [t["id"] for t in data["tracks"]], b.playlist_id,
                                           public=b.public, comment=comment)
                fresh = store.blend(blend_id)  # may have been edited while we were working
                if fresh is not None:
                    fresh.playlist_id = pid
                    store.save_blend(fresh)
                data["playlist_id"] = pid
                data["owner"] = owner.name
                status, message = "ok", f"Saved {len(data['tracks'])} tracks to “{name}”"
            store.finish_run(run_id, status, data["match"], message, data)
            log.info("blend %s run %s: %s", blend_id, run_id, message)
        except Exception as e:  # noqa: BLE001 - every failure is shown in the UI
            log.exception("blend %s run %s failed", blend_id, run_id)
            store.finish_run(run_id, "error", None, str(e) or e.__class__.__name__)
        return run_id


def run_in_background(store: Store, blend_id: int, write: bool = True) -> bool:
    if store.running(blend_id):
        return False
    threading.Thread(target=run_now, args=(store, blend_id, write), daemon=True).start()
    return True
