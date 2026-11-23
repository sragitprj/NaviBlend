"""NaviBlend web interface (FastAPI + server-rendered pages)."""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, scheduler, service
from .store import COLORS, Blend, Member, Store
from .subsonic import Subsonic, SubsonicError

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
AUTH_DISABLED = os.environ.get("NAVIBLEND_AUTH", "").lower() == "none"


# ---------------------------------------------------------------- helpers
def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
    except ValueError:
        return False
    test = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
    return hmac.compare_digest(test.hex(), digest)


def fmt_duration(seconds: int) -> str:
    seconds = int(seconds or 0)
    if seconds >= 3600:
        return f"{seconds // 3600} hr {seconds % 3600 // 60} min"
    return f"{seconds // 60}:{seconds % 60:02d}"


def fmt_date(iso: str) -> str:
    try:
        d = datetime.fromisoformat(iso).astimezone()
    except ValueError:
        return iso
    return d.strftime("%a %d %b, %H:%M")


DAYS = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday", "fri": "Friday",
        "sat": "Saturday", "sun": "Sunday", "daily": "Every day"}


def _int(value, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(str(value))))
    except (TypeError, ValueError):
        return default


def create_app(store: Store | None = None, start_scheduler: bool = True) -> FastAPI:
    store = store or Store()
    store.clear_stale_runs()
    session_key = store.get("session_key")
    if not session_key:
        session_key = secrets.token_hex(32)
        store.set("session_key", session_key)

    app = FastAPI(title="NaviBlend", version=__version__, docs_url=None, redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=session_key, same_site="lax", max_age=60 * 60 * 24 * 30)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["duration"] = fmt_duration
    templates.env.filters["date"] = fmt_date
    app.state.store = store

    if start_scheduler:
        scheduler.start(store)

    # -- auth -----------------------------------------------------------
    def logged_in(request: Request) -> bool:
        return AUTH_DISABLED or bool(request.session.get("user"))

    def csrf(request: Request) -> str:
        tok = request.session.get("csrf")
        if not tok:
            tok = secrets.token_urlsafe(24)
            request.session["csrf"] = tok
        return tok

    def csrf_ok(request: Request, token: str) -> bool:
        return bool(token) and hmac.compare_digest(token, request.session.get("csrf", ""))

    def page(request: Request, tpl: str, **ctx) -> HTMLResponse:
        ctx.update(request=request, csrf=csrf(request), version=__version__, members=store.members(),
                   nav=ctx.get("nav", ""), auth_disabled=AUTH_DISABLED, server=store.get("server_url"),
                   flash=request.session.pop("flash", None), error=request.session.pop("error", None))
        return templates.TemplateResponse(request, tpl, ctx)

    def gate(request: Request) -> RedirectResponse | None:
        if not store.get("admin_hash") and not AUTH_DISABLED:
            return RedirectResponse("/setup", 303)
        if not store.get("server_url"):
            return RedirectResponse("/setup", 303)
        if not logged_in(request):
            return RedirectResponse(f"/login?next={quote(str(request.url.path))}", 303)
        return None

    def back(url: str, flash: str | None = None, error: str | None = None, request: Request | None = None):
        if request is not None:
            if flash:
                request.session["flash"] = flash
            if error:
                request.session["error"] = error
        return RedirectResponse(url, 303)

    def expired(request: Request, url: str):
        return back(url, error="Your session expired, please try again.", request=request)

    # -- setup / login --------------------------------------------------
    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request):
        if store.get("server_url") and (store.get("admin_hash") or AUTH_DISABLED) and not logged_in(request):
            return RedirectResponse("/login", 303)
        return page(request, "setup.html", need_password=not store.get("admin_hash") and not AUTH_DISABLED,
                    server_url=store.get("server_url") or "http://navidrome:4533")

    @app.post("/setup")
    def setup_save(request: Request, server_url: str = Form(...), password: str = Form(""),
                   password2: str = Form(""), token: str = Form("")):
        if not csrf_ok(request, token):
            return expired(request, "/setup")
        first_time = not store.get("admin_hash")
        if store.get("admin_hash") and not logged_in(request):
            return RedirectResponse("/login", 303)
        if first_time and not AUTH_DISABLED:
            if len(password) < 8:
                return back("/setup", error="Choose a NaviBlend password of at least 8 characters.", request=request)
            if password != password2:
                return back("/setup", error="The two passwords don't match.", request=request)
        server_url = server_url.strip().rstrip("/")
        try:
            kind = Subsonic.probe(server_url)
        except SubsonicError as e:
            return back("/setup", error=f"{e}: {server_url}", request=request)
        store.set("server_url", server_url)
        if first_time and not AUTH_DISABLED:
            store.set("admin_hash", hash_password(password))
            request.session["user"] = "admin"
        return back("/people", flash=f"Connected to {kind}. Now add the people who'll be in blends.",
                    request=request)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/"):
        if logged_in(request):
            return RedirectResponse("/", 303)
        return page(request, "login.html", next=next if next.startswith("/") else "/")

    @app.post("/login")
    def login(request: Request, password: str = Form(...), next: str = Form("/"), token: str = Form("")):
        if csrf_ok(request, token) and check_password(password, store.get("admin_hash")):
            request.session["user"] = "admin"
            return RedirectResponse(next if next.startswith("/") and not next.startswith("//") else "/", 303)
        return back("/login", error="That password isn't right.", request=request)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", 303)

    # -- blends list ----------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        if (r := gate(request)) is not None:
            return r
        if not store.members():
            return RedirectResponse("/people", 303)
        cards = []
        for b in store.blends():
            ms = store.blend_members(b)
            last = store.latest_run(b.id)
            cards.append({"blend": b, "members": ms, "title": service.people_title([m.name for m in ms]),
                          "name": service.playlist_name(b, ms), "last": last,
                          "running": store.running(b.id), "next_run": scheduler.next_run_text(b.id)})
        return page(request, "blends.html", nav="blends", cards=cards,
                    any_running=any(c["running"] for c in cards))

    # -- one blend ------------------------------------------------------
    def blend_view(request: Request, b: Blend, run=None):
        ms = store.blend_members(b)
        runs = store.runs(b.id, 12)
        current = runs[0] if runs else None
        run = run or store.latest_run(b.id)
        return page(request, "dashboard.html", nav="blends", b=b, blend_members=ms, run=run, runs=runs,
                    running=bool(current and current.status == "running"),
                    last_error=current if current and current.status == "error" else None,
                    title=service.people_title([m.name for m in ms]), name=service.playlist_name(b, ms),
                    next_run=scheduler.next_run_text(b.id), days=DAYS)

    def blend_form(request: Request, b: Blend, is_new: bool):
        return page(request, "blend_form.html", nav="blends", b=b, is_new=is_new, days=DAYS,
                    next_run=scheduler.next_run_text(b.id) if b.id else "")

    @app.get("/blends/new", response_class=HTMLResponse, name="new_blend")
    def new_blend(request: Request):
        if (r := gate(request)) is not None:
            return r
        ms = store.members()
        return blend_form(request, Blend(member_ids=[m.id for m in ms][:2], weights={m.id: 50 for m in ms}), True)

    @app.get("/blends/{blend_id}", response_class=HTMLResponse)
    def show_blend(request: Request, blend_id: int):
        if (r := gate(request)) is not None:
            return r
        b = store.blend(blend_id)
        if b is None:
            return back("/", error="That blend doesn't exist any more.", request=request)
        return blend_view(request, b)

    @app.get("/blends/{blend_id}/runs/{run_id}", response_class=HTMLResponse)
    def show_run(request: Request, blend_id: int, run_id: int):
        if (r := gate(request)) is not None:
            return r
        b, run = store.blend(blend_id), store.run(run_id)
        if b is None or run is None or run.blend_id != blend_id:
            return back(f"/blends/{blend_id}", request=request)
        return blend_view(request, b, run)

    @app.post("/blends/{blend_id}/run")
    def blend_now(request: Request, blend_id: int, mode: str = Form("save"), token: str = Form("")):
        if (r := gate(request)) is not None:
            return r
        url = f"/blends/{blend_id}"
        if not csrf_ok(request, token):
            return expired(request, url)
        if not service.run_in_background(store, blend_id, write=(mode != "preview")):
            return back(url, error="This blend is already being built.", request=request)
        return RedirectResponse(url, 303)

    @app.get("/blends/{blend_id}/status")
    def status(request: Request, blend_id: int):
        if not logged_in(request):
            return JSONResponse({"error": "login"}, 401)
        runs = store.runs(blend_id, 1)
        return {"running": bool(runs and runs[0].status == "running"), "last": runs[0].id if runs else None}

    # -- create / edit a blend -----------------------------------------
    @app.get("/blends/{blend_id}/edit", response_class=HTMLResponse)
    def edit_blend(request: Request, blend_id: int):
        if (r := gate(request)) is not None:
            return r
        b = store.blend(blend_id)
        if b is None:
            return RedirectResponse("/", 303)
        return blend_form(request, b, False)

    @app.post("/blends/save")
    async def save_blend(request: Request):
        if (r := gate(request)) is not None:
            return r
        form = await request.form()
        if not csrf_ok(request, str(form.get("token", ""))):
            return expired(request, "/")
        bid = _int(form.get("id"), 0, 10**9, 0)
        b = store.blend(bid) if bid else Blend()
        if b is None:
            return RedirectResponse("/", 303)
        known = {m.id for m in store.members()}
        picked = [int(x) for x in form.getlist("member") if str(x).isdigit() and int(x) in known]
        if not picked:
            back_to = f"/blends/{bid}/edit" if bid else "/blends/new"
            return back(back_to, error="Pick at least one person for the blend.", request=request)
        b.member_ids = picked
        b.weights = {mid: _int(form.get(f"weight_{mid}"), 0, 100, 50) for mid in known}
        b.name = str(form.get("name", "")).strip()[:100]
        b.length = _int(form.get("length"), 10, 200, 50)
        b.both_pct = _int(form.get("both_pct"), 0, 100, 30)
        b.new_pct = _int(form.get("new_pct"), 0, 60, 20)
        b.skip_recent_days = _int(form.get("skip_recent_days"), 0, 60, 0)
        b.interleave = bool(form.get("interleave"))
        b.public = bool(form.get("public"))
        day = str(form.get("schedule_day", "mon"))
        b.schedule_day = day if day in DAYS or day == "off" else "mon"
        tm = str(form.get("schedule_time", "06:00"))
        b.schedule_time = tm if len(tm) == 5 and tm[2] == ":" else "06:00"
        owner = _int(form.get("owner_member_id"), 0, 10**9, 0)
        if owner != b.owner_member_id:
            b.playlist_id = ""  # a different account owns it now: start a fresh playlist there
        b.owner_member_id = owner if owner in picked else 0
        new_id = store.save_blend(b)
        scheduler.reschedule(store)
        if not bid and form.get("build"):
            service.run_in_background(store, new_id, write=form.get("build") != "preview")
        return back(f"/blends/{new_id}", flash="Blend saved." if bid else "Blend created.", request=request)

    @app.post("/blends/{blend_id}/delete")
    def delete_blend(request: Request, blend_id: int, token: str = Form("")):
        if (r := gate(request)) is not None:
            return r
        if not csrf_ok(request, token):
            return expired(request, f"/blends/{blend_id}/edit")
        store.delete_blend(blend_id)
        scheduler.reschedule(store)
        return back("/", flash="Blend deleted. Its playlist is still on your server if you want to keep it.",
                    request=request)

    # -- people ---------------------------------------------------------
    @app.get("/people", response_class=HTMLResponse)
    def people(request: Request, edit: int = 0):
        if (r := gate(request)) is not None:
            return r
        in_blends = {m.id: [service.playlist_name(b, store.blend_members(b)) for b in store.blends()
                            if m.id in b.member_ids] for m in store.members()}
        return page(request, "people.html", nav="people", edit=edit, colors=COLORS,
                    next_color=store.next_color(), in_blends=in_blends, has_blends=bool(store.blends()))

    def _check_login(username: str, password: str) -> str | None:
        try:
            with Subsonic(store.get("server_url"), username, password) as c:
                c.ping()
        except SubsonicError as e:
            return str(e)
        return None

    @app.post("/people/save")
    def people_save(request: Request, id: int = Form(0), name: str = Form(...), username: str = Form(...),
                    password: str = Form(""), color: str = Form("#5B9BFF"), lb_user: str = Form(""),
                    lastfm_user: str = Form(""), token: str = Form("")):
        if (r := gate(request)) is not None:
            return r
        if not csrf_ok(request, token):
            return expired(request, "/people")
        existing = store.member(id) if id else None
        if not existing and not password:
            return back("/people", error=f"Enter {name}'s server password so NaviBlend can read their plays.",
                        request=request)
        pw = password or (existing.password if existing else "")
        if password or not existing or existing.username != username.strip():
            if (err := _check_login(username.strip(), pw)) is not None:
                return back(f"/people?edit={id}" if id else "/people",
                            error=f"Couldn't sign in as {username}: {err}", request=request)
        color = color if color.startswith("#") and len(color) in (4, 7) else store.next_color()
        m = Member(id=id, name=name.strip()[:40] or username, color=color, username=username.strip(), password=pw,
                   lb_user=lb_user.strip(), lastfm_user=lastfm_user.strip())
        store.save_member(m, keep_password=bool(existing and not password))
        return back("/people", flash=f"Saved {m.name}.", request=request)

    @app.post("/people/{member_id}/delete")
    def people_delete(request: Request, member_id: int, token: str = Form("")):
        if (r := gate(request)) is not None:
            return r
        if not csrf_ok(request, token):
            return expired(request, "/people")
        m = store.member(member_id)
        store.delete_member(member_id)
        scheduler.reschedule(store)
        return back("/people", flash=f"Removed {m.name if m else 'that person'}.", request=request)

    # -- server settings ------------------------------------------------
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        if (r := gate(request)) is not None:
            return r
        return page(request, "settings.html", nav="settings", s=store.settings(),
                    has_lastfm=bool(store.get("lastfm_api_key")))

    @app.post("/settings")
    async def settings_save(request: Request):
        if (r := gate(request)) is not None:
            return r
        form = await request.form()
        if not csrf_ok(request, str(form.get("token", ""))):
            return expired(request, "/settings")
        key = str(form.get("lastfm_api_key", "")).strip()
        if key == "-":
            store.set("lastfm_api_key", "")
        elif key:
            store.set("lastfm_api_key", key)
        new_server = str(form.get("server_url", "")).strip().rstrip("/")
        if new_server and new_server != store.get("server_url"):
            try:
                Subsonic.probe(new_server)
            except SubsonicError as e:
                return back("/settings", error=f"{e}: {new_server}", request=request)
            store.set("server_url", new_server)
            for b in store.blends():  # playlists live on the old server
                b.playlist_id = ""
                store.save_blend(b)
        return back("/settings", flash="Settings saved.", request=request)

    @app.get("/health")
    def health():
        return {"ok": True, "version": __version__, "time": datetime.now(timezone.utc).isoformat()}

    return app
