"""SQLite storage for settings, people, blends and blend runs.

Secrets (people's server passwords, the Last.fm API key) are encrypted with a Fernet key
kept in the data directory, so a copied database alone does not leak them.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from cryptography.fernet import Fernet, InvalidToken

DATA_DIR = Path(os.environ.get("NAVIBLEND_DATA", "/config"))

# Server-wide settings. Per-blend options live on the Blend itself.
DEFAULT_SETTINGS: dict[str, str] = {
    "server_url": "",
    "lastfm_api_key": "",
}
SECRET_SETTINGS = {"lastfm_api_key"}

# Pre-0.2 single-blend settings, read once to migrate into the first blend.
LEGACY_BLEND_KEYS = ["playlist_name", "length", "both_pct", "new_pct", "interleave", "skip_recent_days",
                     "schedule_day", "schedule_time", "public", "owner_member_id", "playlist_id"]

COLORS = ["#5B9BFF", "#FFA24C", "#3DD6B0", "#FF6B9A", "#B794F6", "#F5D547"]


@dataclass
class Member:
    id: int
    name: str
    color: str
    username: str
    password: str  # decrypted
    lb_user: str = ""
    lastfm_user: str = ""
    weight: int = 50       # filled in per blend when a blend runs
    enabled: bool = True


@dataclass
class Blend:
    id: int = 0
    name: str = ""                 # playlist name; blank = "Alex + Jordan Blend"
    member_ids: list[int] = field(default_factory=list)
    weights: dict[int, int] = field(default_factory=dict)
    length: int = 50
    both_pct: int = 30
    new_pct: int = 20
    interleave: bool = True
    skip_recent_days: int = 0
    schedule_day: str = "mon"      # mon..sun | daily | off
    schedule_time: str = "06:00"
    public: bool = True
    owner_member_id: int = 0       # 0 = first person in the blend
    cover: bool = True             # upload a generated cover image (Navidrome)
    playlist_id: str = ""

    def weight(self, member_id: int) -> int:
        return int(self.weights.get(member_id, 50))


@dataclass
class Run:
    id: int
    blend_id: int
    created_at: str
    status: str  # running | ok | preview | error
    match_pct: int | None
    message: str
    result: dict[str, Any] = field(default_factory=dict)


class Store:
    def __init__(self, data_dir: Path | None = None):
        self.dir = Path(data_dir or DATA_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "naviblend.db"
        self._lock = threading.RLock()
        self._fernet = Fernet(self._load_key())
        self._init()

    # -- infrastructure -------------------------------------------------
    def _load_key(self) -> bytes:
        env = os.environ.get("NAVIBLEND_SECRET_KEY")
        if env:
            return env.encode()
        key_file = self.dir / "secret.key"
        if not key_file.exists():
            key_file.write_bytes(Fernet.generate_key())
            try:
                key_file.chmod(0o600)
            except OSError:
                pass
        return key_file.read_bytes().strip()

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            c = sqlite3.connect(self.path)
            c.row_factory = sqlite3.Row
            try:
                yield c
                c.commit()
            finally:
                c.close()

    def _init(self) -> None:
        with self.conn() as c:
            c.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL, color TEXT NOT NULL,
                    username TEXT NOT NULL, password_enc TEXT NOT NULL,
                    lb_user TEXT NOT NULL DEFAULT '', lastfm_user TEXT NOT NULL DEFAULT '',
                    weight INTEGER NOT NULL DEFAULT 50, enabled INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS blends (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    data_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL, status TEXT NOT NULL,
                    match_pct INTEGER, message TEXT NOT NULL DEFAULT '',
                    result_json TEXT NOT NULL DEFAULT '{}');
                """
            )
            cols = {r["name"] for r in c.execute("PRAGMA table_info(runs)")}
            if "blend_id" not in cols:
                c.execute("ALTER TABLE runs ADD COLUMN blend_id INTEGER NOT NULL DEFAULT 0")
        self._migrate_single_blend()

    def _migrate_single_blend(self) -> None:
        """0.1 had one blend configured in settings; turn it into blend #1."""
        if self.blends() or not self.members():
            return
        with self.conn() as c:
            old = {r["key"]: r["value"] for r in c.execute(
                f"SELECT key, value FROM settings WHERE key IN ({','.join('?' * len(LEGACY_BLEND_KEYS))})",
                LEGACY_BLEND_KEYS)}
            rows = c.execute("SELECT id, weight, enabled FROM members ORDER BY id").fetchall()
        b = Blend(
            name=old.get("playlist_name", ""),
            member_ids=[r["id"] for r in rows if r["enabled"]],
            weights={r["id"]: r["weight"] for r in rows},
            length=int(old.get("length") or 50), both_pct=int(old.get("both_pct") or 30),
            new_pct=int(old.get("new_pct") or 20), interleave=old.get("interleave", "1") == "1",
            skip_recent_days=int(old.get("skip_recent_days") or 0),
            schedule_day=old.get("schedule_day") or "mon", schedule_time=old.get("schedule_time") or "06:00",
            public=old.get("public", "1") == "1", owner_member_id=int(old.get("owner_member_id") or 0),
            playlist_id=old.get("playlist_id", ""),
        )
        bid = self.save_blend(b)
        with self.conn() as c:
            c.execute("UPDATE runs SET blend_id=? WHERE blend_id=0", (bid,))

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode() if value else ""

    def decrypt(self, value: str) -> str:
        if not value:
            return ""
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken:
            return ""

    # -- settings -------------------------------------------------------
    def get(self, key: str) -> str:
        with self.conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return DEFAULT_SETTINGS.get(key, "")
        return self.decrypt(row["value"]) if key in SECRET_SETTINGS else row["value"]

    def set(self, key: str, value: str) -> None:
        stored = self.encrypt(value) if key in SECRET_SETTINGS else value
        with self.conn() as c:
            c.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, stored),
            )

    def settings(self) -> dict[str, str]:
        return {k: self.get(k) for k in DEFAULT_SETTINGS}

    # -- people ---------------------------------------------------------
    def _member(self, r: sqlite3.Row) -> Member:
        return Member(
            id=r["id"], name=r["name"], color=r["color"], username=r["username"],
            password=self.decrypt(r["password_enc"]), lb_user=r["lb_user"],
            lastfm_user=r["lastfm_user"], weight=r["weight"], enabled=bool(r["enabled"]),
        )

    def members(self) -> list[Member]:
        with self.conn() as c:
            return [self._member(r) for r in c.execute("SELECT * FROM members ORDER BY id").fetchall()]

    def member(self, member_id: int) -> Member | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM members WHERE id=?", (member_id,)).fetchone()
        return self._member(r) if r else None

    def save_member(self, m: Member, keep_password: bool = False) -> int:
        with self.conn() as c:
            if m.id:
                if keep_password:
                    c.execute("UPDATE members SET name=?,color=?,username=?,lb_user=?,lastfm_user=? WHERE id=?",
                              (m.name, m.color, m.username, m.lb_user, m.lastfm_user, m.id))
                else:
                    c.execute("UPDATE members SET name=?,color=?,username=?,password_enc=?,lb_user=?,lastfm_user=? "
                              "WHERE id=?", (m.name, m.color, m.username, self.encrypt(m.password), m.lb_user,
                                             m.lastfm_user, m.id))
                return m.id
            cur = c.execute(
                "INSERT INTO members(name,color,username,password_enc,lb_user,lastfm_user,weight,enabled) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (m.name, m.color, m.username, self.encrypt(m.password), m.lb_user, m.lastfm_user, m.weight,
                 int(m.enabled)),
            )
            return int(cur.lastrowid)

    def delete_member(self, member_id: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM members WHERE id=?", (member_id,))
        for b in self.blends():  # take them out of any blend they were in
            if member_id in b.member_ids or b.owner_member_id == member_id:
                b.member_ids = [i for i in b.member_ids if i != member_id]
                b.weights.pop(member_id, None)
                if b.owner_member_id == member_id:
                    b.owner_member_id = 0
                self.save_blend(b)

    def next_color(self) -> str:
        used = {m.color for m in self.members()}
        return next((col for col in COLORS if col not in used), COLORS[len(used) % len(COLORS)])

    # -- blends ---------------------------------------------------------
    @staticmethod
    def _blend_from(row: sqlite3.Row) -> Blend:
        d = json.loads(row["data_json"])
        d["weights"] = {int(k): int(v) for k, v in d.get("weights", {}).items()}
        d["member_ids"] = [int(x) for x in d.get("member_ids", [])]
        known = {k: v for k, v in d.items() if k in Blend.__dataclass_fields__ and k != "id"}
        return Blend(id=row["id"], **known)

    def blends(self) -> list[Blend]:
        with self.conn() as c:
            return [self._blend_from(r) for r in c.execute("SELECT * FROM blends ORDER BY id").fetchall()]

    def blend(self, blend_id: int) -> Blend | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM blends WHERE id=?", (blend_id,)).fetchone()
        return self._blend_from(r) if r else None

    def save_blend(self, b: Blend) -> int:
        data = {k: getattr(b, k) for k in Blend.__dataclass_fields__ if k != "id"}
        data["weights"] = {str(k): v for k, v in b.weights.items()}
        with self.conn() as c:
            if b.id:
                c.execute("UPDATE blends SET data_json=? WHERE id=?", (json.dumps(data), b.id))
                return b.id
            cur = c.execute("INSERT INTO blends(data_json) VALUES(?)", (json.dumps(data),))
            return int(cur.lastrowid)

    def delete_blend(self, blend_id: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM blends WHERE id=?", (blend_id,))
            c.execute("DELETE FROM runs WHERE blend_id=?", (blend_id,))

    def blend_members(self, b: Blend) -> list[Member]:
        """The people in a blend, in their blend order, with this blend's weights applied."""
        by_id = {m.id: m for m in self.members()}
        out = []
        for mid in b.member_ids:
            if mid in by_id:
                m = by_id[mid]
                m.weight = b.weight(mid)
                out.append(m)
        return out

    # -- runs -----------------------------------------------------------
    def start_run(self, blend_id: int, status: str = "running") -> int:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.conn() as c:
            cur = c.execute("INSERT INTO runs(created_at,status,blend_id) VALUES(?,?,?)", (now, status, blend_id))
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, match_pct: int | None, message: str,
                   result: dict[str, Any] | None = None) -> None:
        with self.conn() as c:
            c.execute("UPDATE runs SET status=?, match_pct=?, message=?, result_json=? WHERE id=?",
                      (status, match_pct, message, json.dumps(result or {}), run_id))

    def _run(self, r: sqlite3.Row) -> Run:
        return Run(id=r["id"], blend_id=r["blend_id"], created_at=r["created_at"], status=r["status"],
                   match_pct=r["match_pct"], message=r["message"], result=json.loads(r["result_json"] or "{}"))

    def runs(self, blend_id: int, limit: int = 10) -> list[Run]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM runs WHERE blend_id=? ORDER BY id DESC LIMIT ?",
                             (blend_id, limit)).fetchall()
        return [self._run(r) for r in rows]

    def run(self, run_id: int) -> Run | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return self._run(r) if r else None

    def latest_run(self, blend_id: int, statuses: tuple[str, ...] = ("ok", "preview")) -> Run | None:
        marks = ",".join("?" * len(statuses))
        with self.conn() as c:
            r = c.execute(f"SELECT * FROM runs WHERE blend_id=? AND status IN ({marks}) ORDER BY id DESC LIMIT 1",
                          (blend_id, *statuses)).fetchone()
        return self._run(r) if r else None

    def running(self, blend_id: int | None = None) -> bool:
        q, args = "SELECT 1 FROM runs WHERE status='running'", ()
        if blend_id is not None:
            q, args = q + " AND blend_id=?", (blend_id,)
        with self.conn() as c:
            return c.execute(q + " LIMIT 1", args).fetchone() is not None

    def clear_stale_runs(self) -> None:
        with self.conn() as c:
            c.execute("UPDATE runs SET status='error', message='Interrupted (restart)' WHERE status='running'")
