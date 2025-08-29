# main.py
# Saisonnier Pro - MVP complet (FastAPI)
# ------------------------------------------------------------
# Dépendances (déjà dans ton requirements.txt) :
# fastapi, uvicorn, jinja2, sqlalchemy, aiosqlite, httpx,
# python-multipart, pydantic[email], ics, python-dateutil
# ------------------------------------------------------------

from __future__ import annotations

import os
import io
import csv
import re
import hashlib
from datetime import datetime, date, timedelta
from typing import Optional, List, Tuple

import httpx
from fastapi import FastAPI, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr
from pathlib import Path

from sqlalchemy import (
    create_engine, Column, Integer, String, Date, Float, ForeignKey,
    UniqueConstraint, func
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session

from ics import Calendar as IcsCalendar
from dateutil.parser import parse as dparse

import hashlib, secrets, string
from sqlalchemy import func

SALT = "stayflow$2025"   # fixe; tu peux le mettre en env si tu veux

def hash_password(p: str) -> str:
    p = (p or "").strip()
    return hashlib.sha256((SALT + p).encode("utf-8")).hexdigest()

_HEX = set(string.hexdigits)

def looks_like_sha256(s: str) -> bool:
    return isinstance(s, str) and len(s) == 64 and all(c in _HEX for c in s)

def verify_password(input_password: str, stored: str) -> bool:
    """Compat : accepte l'ancien stockage éventuel en clair, puis migre vers le hash."""
    if not stored:
        return False
    # cas hash standard
    if looks_like_sha256(stored):
        return secrets.compare_digest(hash_password(input_password), stored)
    # cas legacy (mot de passe en clair stocké)
    return secrets.compare_digest((input_password or "").strip(), stored)

# ============================================================
# Config appli
# ============================================================

APP_NAME = "StayFlow"
APP_TAGLINE = "Le cockpit de vos locations"

APP_TITLE = f"{APP_NAME} - {APP_TAGLINE}"

# DATABASE_URL normalisée (sqlite local par défaut)
DB_URL_RAW = os.getenv("DATABASE_URL", "sqlite:///./saisonnier.db")

DB_URL = DB_URL_RAW
if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

# Ajout driver psycopg2 / psycopg si dispo
driver = ""
try:
    import psycopg2  # type: ignore
    driver = "+psycopg2"
except Exception:
    try:
        import psycopg  # type: ignore
        driver = "+psycopg"
    except Exception:
        driver = ""

if DB_URL.startswith("postgresql://") and driver:
    DB_URL = DB_URL.replace("postgresql://", f"postgresql{driver}://", 1)

connect_args = {"check_same_thread": False} if DB_URL.startswith("sqlite") else {}
engine = create_engine(DB_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ============================================================
# Utilitaires
# ============================================================

def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()

def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- Validation URL iCal ---------------------------------------------------
ICAL_RE = re.compile(r"^https?://.+\.ics(\?.*)?$", re.IGNORECASE)

def validate_ical_url(url: str) -> bool:
    if not url or not ICAL_RE.match(url):
        return False
    try:
        with httpx.Client(timeout=5) as c:
            r = c.head(url, follow_redirects=True)
            return r.status_code < 400
    except Exception:
        return False

# --- Reservation helpers (dates & overlaps) -------------------------------
def parse_date(s: str) -> date | None:
    if not s:
        return None
    try:
        return dparse(s).date()
    except Exception:
        return None

def overlaps(a_start: date, a_end: date, b_start: date, b_end: date) -> bool:
    # intervalle [start, end) — fin exclusive
    return a_start < b_end and b_start < a_end

# ============================================================
# Modèles SQLAlchemy
# ============================================================

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, nullable=False, index=True)
    name = Column(String, default="")
    password = Column(String, nullable=False)

    properties = relationship("Property", back_populates="owner")


class Property(Base):
    __tablename__ = "properties"

    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    ical_url = Column(String, default="")
    owner_id = Column("user_id", Integer, ForeignKey("users.id"), index=True, nullable=False)

    owner = relationship("User", back_populates="properties")
    reservations = relationship("Reservation", back_populates="property", cascade="all, delete-orphan")


class Reservation(Base):
    __tablename__ = "reservations"

    id = Column(Integer, primary_key=True)
    property_id = Column(Integer, ForeignKey("properties.id"), nullable=False, index=True)

    source = Column(String, default="manual")    # manual / airbnb / booking
    guest_name = Column(String, default="")
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    total_price = Column(Float, default=0.0)
    external_uid = Column(String)

    __table_args__ = (
        UniqueConstraint('property_id', 'external_uid', name='uix_prop_uid'),
    )

    property = relationship("Property", back_populates="reservations")


Base.metadata.create_all(bind=engine)

# --- Ownership helper ------------------------------------------------------
def get_owned_property(db, user_id: int, prop_id: int) -> "Property | None":
    return db.query(Property).filter(
        Property.id == prop_id,
        Property.owner_id == user_id   # si tu as Column("user_id", ...) garde .owner_id ici
    ).first()

# ============================================================
# App / templating
# ============================================================

app = FastAPI(title=APP_TITLE)

from starlette.responses import Response

@app.head("/")
def head_root():
    return Response(status_code=200)

# static (évite l’erreur si dossier absent)
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(parents=True, exist_ok=True)
(app.mount if hasattr(app, "mount") else lambda *a, **k: None)(
    "/static", StaticFiles(directory=str(static_dir)), name="static"
)

# --- Route de diagnostic ---
@app.get("/_diag/init", response_class=HTMLResponse)
def diag_init():
    try:
        Base.metadata.create_all(bind=engine)
        msg = "Tables (re)créées."
    except Exception as e:
        msg = f"Erreur create_all: {type(e).__name__}: {e}"
    return page(f"<div class='container'><div class='card'>{msg}</div></div>", "Init DB")
    
# Jinja minimal depuis string
from jinja2 import Environment, select_autoescape
env = Environment(autoescape=select_autoescape())

def render_str(html: str, **ctx) -> str:
    return env.from_string(html).render(**ctx)
    
from sqlalchemy import inspect
import urllib.parse

from sqlalchemy import inspect

def _mask_db_url(url: str) -> str:
    try:
        from urllib.parse import urlsplit, urlunsplit
        u = urlsplit(DB_URL)
        netloc = u.netloc
        if "@" in netloc and ":" in netloc.split("@",1)[0]:
            user = netloc.split("@",1)[0].split(":",1)[0]
            host = netloc.split("@",1)[1]
            netloc = f"{user}:***@{host}"
        return urlunsplit((u.scheme, netloc, u.path, u.query, u.fragment))
    except Exception:
        return "<mask>"

@app.get("/_diag/db", response_class=HTMLResponse)
def diag_db():
    lines = []
    # 1) URL masquée
    lines.append(f"<li><b>DATABASE_URL</b>: {_mask_db_url(DB_URL)}</li>")
    # 2) Ping
    try:
        with engine.connect() as conn:
            conn.execute("SELECT 1")
        lines.append("<li><b>Connexion</b>: OK</li>")
    except Exception as e:
        lines.append(f"<li><b>Connexion</b>: ERREUR — {type(e).__name__}: {e}</li>")
    # 3) Tables
    try:
        insp = inspect(engine)
        tables = insp.get_table_names()
        lines.append(f"<li><b>Tables</b>: {', '.join(tables) or '(aucune)'} </li>")
    except Exception as e:
        lines.append(f"<li><b>Tables</b>: ERREUR — {type(e).__name__}: {e}</li>")

    html = f"""
    <div class="container"><div class="card">
      <h2 class="text-xl font-semibold">Diag DB</h2>
      <ul>{"".join(lines)}</ul>
      <p style="margin-top:1rem">
        <a class="badge" href="/_diag/init">Créer les tables</a>
      </p>
    </div></div>
    """
    return page(html, "Diag DB")

# --- HEAD / Styles
BASE_HEAD = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css" rel="stylesheet">
<script src="https://unpkg.com/htmx.org@1.9.12"></script>
<style>
:root{
  --primary:#0f172a;
  --accent:#06b6d4;
  --muted:#f1f5f9;
  --ring:rgba(6,182,212,.35);
}
*{ font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }
body{ background: linear-gradient(180deg,#f8fafc 0%, #f1f5f9 100%); color:#0f172a; }

.btn{ display:inline-flex; align-items:center; gap:.5rem; padding:.7rem 1.1rem; border-radius:.9rem;
      background:var(--primary); color:#fff; font-weight:600; box-shadow:0 6px 20px rgba(2,6,23,.15); }
.btn:hover{ filter:brightness(1.06); transform: translateY(-1px); transition:.2s; }
.btn-accent{ background:var(--accent); color:#06253A; }
.badge{ background:#eef2ff; color:#0b1b36; padding:.35rem .7rem; border-radius:.6rem; font-weight:500; }
.card{ background:#fff; border-radius:1.25rem; box-shadow:0 20px 40px rgba(2,6,23,.08); padding:1.25rem; }
.container{ max-width:1120px; margin:0 auto; padding:1rem; }
.headbar{ backdrop-filter:saturate(180%) blur(8px); background:rgba(255,255,255,.82); border-bottom:1px solid #eef2f7; }
.logo{ display:flex; align-items:center; gap:.6rem; font-weight:700; letter-spacing:.2px; }
.logo-mark{ width:28px; height:28px; border-radius:.7rem; background:linear-gradient(135deg,var(--accent),#60a5fa); box-shadow:0 6px 20px rgba(6,182,212,.35) }
.hero{ display:grid; grid-template-columns:1.1fr .9fr; gap:1.25rem; align-items:stretch; }
.cta-stack{ margin-top:auto; }
input, select{ border:1px solid #e2e8f0; padding:.65rem .8rem; border-radius:.8rem; outline:0; width:100%; }
input:focus, select:focus{ border-color:var(--accent); box-shadow:0 0 0 4px var(--ring); }
.text-gray-600{ color:#64748b; }
.text-xl{ font-size:1.25rem; }
.text-3xl{ font-size:1.875rem; }
.text-4xl{ font-size:2.25rem; }
.font-semibold{ font-weight:600; }
.mb-2{ margin-bottom:.5rem; }
.mt-6{ margin-top:1.5rem; }
.flex{ display:flex; }
.items-center{ align-items:center; }
.justify-between{ justify-content:space-between; }
.mb-3{ margin-bottom:.75rem; }
</style>
"""

def page(content: str, title: str = APP_TITLE, user: Optional[User] = None) -> str:
    # NE RENVOIE PLUS HTMLResponse ICI — juste la chaîne HTML !
    return render_str("""
<!doctype html>
<html lang="fr">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>{{ title }}</title>
    {{ head|safe }}
  </head>
  <body>
    <header class="headbar">
      <div class="container" style="display:flex; align-items:center; justify-content:space-between; padding:.9rem 1rem;">
        <div class="logo"><span class="logo-mark"></span> StayFlow</div>
        <nav style="display:flex; gap:.5rem;">
          <a class="badge" href="/properties">Logements</a>
          <a class="badge" href="/calendar">Calendrier</a>
          <a class="badge" href="/reservations">Réservations</a>
          <a class="badge" href="/sync">Sync</a>
          {% if user %}
            <a class="badge" href="/logout">Déconnexion</a>
          {% else %}
            <a class="badge" href="/login">Connexion</a>
            <a class="badge" href="/signup">Créer un compte</a>
          {% endif %}
        </nav>
      </div>
    </header>

    <main style="padding:1rem 0;">
      {{ content|safe }}
    </main>
  </body>
</html>
    """, title=title, head=BASE_HEAD, content=content, user=user)

# ============================================================
# Auth minimale (cookie 'uid')
# ============================================================

def current_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    uid = request.cookies.get("uid")
    if not uid:
        return None
    try:
        uid_int = int(uid)
    except Exception:
        return None
    return db.query(User).filter(User.id == uid_int).first()


# ============================================================
# Routes
# ============================================================

@app.get("/healthz")
def health() -> dict:
    return {"status": "ok"}


# --- Home ---------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def home(request: Request, user: Optional[User] = Depends(current_user)):
    content = """
    <div class="container">
      <div class="hero">
        <div class="card" style="display:flex; flex-direction:column;">
          <h1 class="text-3xl md:text-4xl font-semibold mb-2">Centralisez vos réservations.</h1>
          <p class="text-gray-600">Import iCal, calendrier consolidé, et planning ménage.</p>
          <div class="cta-stack">
            <a href="/signup" class="btn btn-accent mt-6">Créer un compte</a>
          </div>
        </div>
        <div class="card" style="display:flex; flex-direction:column;">
          <div class="text-gray-600">Déjà un compte ?</div>
          <div class="cta-stack">
            <a href="/login" class="btn mt-6">Se connecter</a>
          </div>
        </div>
      </div>
    </div>
    """
    return page(content, APP_TITLE, user=user)


# --- Signup / Login / Logout --------------------------------
@app.get("/signup", response_class=HTMLResponse)
async def signup_get(request: Request, user: Optional[User] = Depends(current_user)):
    if user:
        return RedirectResponse("/properties", status_code=303)
    content = """
    <div class="container">
      <div class="card" style="max-width:480px; margin:0 auto;">
        <h2 class="text-xl font-semibold mb-2">Créer un compte</h2>
        <form method="post" action="/signup">
          <label>Email</label>
          <input name="email" type="email" required />
          <label class="mt-6">Nom</label>
          <input name="name" type="text" />
          <label class="mt-6">Mot de passe</label>
          <input name="password" type="password" required />
          <button class="btn btn-accent mt-6" type="submit">Créer</button>
        </form>
      </div>
    </div>
    """
    return page(content, APP_TITLE, user=None)

from sqlalchemy.exc import IntegrityError, OperationalError

@app.post("/signup")
async def signup_post(
    request: Request,
    email: str = Form(...),
    name: str = Form(""),
    password: str = Form(...)
):
    email_clean = (email or "").strip().lower()
    name_clean  = (name or "").strip()
    pwd         = (password or "").strip()

    if not email_clean or "@" not in email_clean:
        return HTMLResponse(page("<div class='container'><div class='card'>Email invalide.</div></div>", APP_TITLE), status_code=400)
    if not pwd:
        return HTMLResponse(page("<div class='container'><div class='card'>Mot de passe requis.</div></div>", APP_TITLE), status_code=400)

    db = SessionLocal()
    try:
        # force la création des tables si besoin
        try:
            db.execute("SELECT 1 FROM users LIMIT 1")
        except OperationalError:
            Base.metadata.create_all(bind=engine)

        exists = db.query(User).filter(User.email == email_clean).first()
        if exists:
            return HTMLResponse(page("<div class='container'><div class='card'>Email déjà utilisé.</div></div>", APP_TITLE), status_code=400)

        u = User(email=email_clean, name=name_clean, password=hash_password(pwd))
        db.add(u)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return HTMLResponse(page("<div class='container'><div class='card'>Ce compte existe déjà.</div></div>", APP_TITLE), status_code=400)

        resp = RedirectResponse("/properties", status_code=303)
        resp.set_cookie("uid", str(u.id), httponly=True, samesite="lax")
        return resp

    except Exception as e:
        return HTMLResponse(
            page(f"<div class='container'><div class='card'>Erreur serveur pendant l’inscription.<br><small>{type(e).__name__}: {e}</small></div></div>", APP_TITLE),
            status_code=500
        )
    finally:
        db.close()

@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request, user: Optional[User] = Depends(current_user)):
    if user:
        return RedirectResponse("/properties", status_code=303)
    content = """
    <div class="container">
      <div class="card" style="max-width:480px; margin:0 auto;">
        <h2 class="text-xl font-semibold mb-2">Connexion</h2>
        <form method="post" action="/login">
          <label>Email</label>
          <input name="email" type="email" required />
          <label class="mt-6">Mot de passe</label>
          <input name="password" type="password" required />
          <button class="btn mt-6" type="submit">Se connecter</button>
        </form>
      </div>
    </div>
    """
    return page(content, APP_TITLE, user=None)

@app.post("/login")
async def login_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...)
):
    email_clean = (email or "").strip().lower()
    pwd = (password or "").strip()

    if not email_clean or not pwd:
        return HTMLResponse(
            page("<div class='container'><div class='card'>Email et mot de passe requis.</div></div>", APP_TITLE),
            status_code=400,
        )

    db = SessionLocal()
    try:
        # lookup insensible à la casse
        user = db.query(User).filter(func.lower(User.email) == email_clean).first()
        if not user:
            return HTMLResponse(
                page("<div class='container'><div class='card'>Identifiants invalides.</div></div>", APP_TITLE),
                status_code=400,
            )

        # vérif compat (hash/legacy) + migration éventuelle vers hash
        if verify_password(pwd, user.password):
            if not looks_like_sha256(user.password):
                user.password = hash_password(pwd)
                db.commit()

            resp = RedirectResponse("/properties", status_code=303)
            resp.set_cookie("uid", str(user.id), httponly=True, samesite="lax")
            return resp

        return HTMLResponse(
            page("<div class='container'><div class='card'>Identifiants invalides.</div></div>", APP_TITLE),
            status_code=400,
        )

    finally:
        db.close()

@app.get("/logout")
async def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("uid")
    return resp

# --- Logements --------------------------------------------------------------
@app.get("/properties", response_class=HTMLResponse)
async def properties_list(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    props = db.query(Property).filter(Property.owner_id == user.id).order_by(Property.id.desc()).all()
    rows = []
    for p in props:
        rows.append(f"""
          <tr>
            <td>{p.title}</td>
            <td>{p.ical_url or "-"}</td>
            <td style="text-align:right;">
              <a class="badge" href="/properties/{p.id}/edit">Éditer</a>
              <a class="badge" href="/properties/{p.id}/delete" onclick="return confirm('Supprimer ?')">Supprimer</a>
            </td>
          </tr>
        """)
    table = "<table style='width:100%; border-collapse:separate; border-spacing:0 .5rem;'>" + "".join(rows) + "</table>" if rows else "<div class='text-gray-600'>Aucun logement.</div>"

    content = f"""
    <div class="container">
      <div class="card">
        <div class="flex items-center justify-between mb-3">
          <h2 class="text-xl font-semibold">Logements</h2>
          <a class="badge" href="/properties/add">Ajouter</a>
        </div>
        {table}
      </div>
    </div>
    """
    return page(content, APP_TITLE, user=user)

@app.get("/properties/add", response_class=HTMLResponse)
async def properties_add_form(request: Request, user: User = Depends(current_user)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    content = """
    <div class="container">
      <div class="card" style="max-width:640px; margin:0 auto;">
        <h2 class="text-xl font-semibold mb-2">Ajouter un logement</h2>
        <form method="post" action="/properties/add">
          <label>Titre</label>
          <input name="title" required />

          <label class="mt-6">URL iCal (optionnel)</label>
          <input name="ical_url" placeholder="https://... .ics" />

          <button class="btn btn-accent mt-6" type="submit">Créer</button>
        </form>
      </div>
    </div>
    """
    return page(content, APP_TITLE, user=user)

@app.post("/properties/add")
async def properties_add(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    title = (form.get("title") or "").strip()
    ical_url = (form.get("ical_url") or "").strip()

    if not title:
        return HTMLResponse(page("<div class='container'><div class='card'>Le titre est requis.</div></div>", APP_TITLE, user=user), status_code=400)

    if ical_url and not validate_ical_url(ical_url):
        return HTMLResponse(page("<div class='container'><div class='card'>URL iCal invalide. Vérifie le lien public .ics.</div></div>", APP_TITLE, user=user), status_code=400)

    p = Property(title=title, ical_url=ical_url, owner_id=user.id)
    db.add(p)
    db.commit()
    return RedirectResponse("/properties", status_code=303)

@app.get("/properties/{prop_id}/edit", response_class=HTMLResponse)
async def properties_edit_form(prop_id: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    p = db.query(Property).filter(Property.id == prop_id, Property.owner_id == user.id).first()
    if not p:
        return HTMLResponse(page("<div class='container'><div class='card'>Logement introuvable.</div></div>", APP_TITLE, user=user), status_code=404)
    content = f"""
    <div class="container">
      <div class="card" style="max-width:640px; margin:0 auto;">
        <h2 class="text-xl font-semibold mb-2">Éditer le logement</h2>
        <form method="post" action="/properties/{p.id}/edit">
          <label>Titre</label>
          <input name="title" value="{p.title}" required />

          <label class="mt-6">URL iCal (optionnel)</label>
          <input name="ical_url" value="{p.ical_url or ''}" placeholder="https://... .ics" />

          <button class="btn mt-6" type="submit">Enregistrer</button>
        </form>
      </div>
    </div>
    """
    return page(content, APP_TITLE, user=user)

@app.post("/properties/{prop_id}/edit")
async def properties_edit(prop_id: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    title = (form.get("title") or "").strip()
    ical_url = (form.get("ical_url") or "").strip()

    if not title:
        return HTMLResponse(page("<div class='container'><div class='card'>Le titre est requis.</div></div>", APP_TITLE, user=user), status_code=400)

    if ical_url and not validate_ical_url(ical_url):
        return HTMLResponse(page("<div class='container'><div class='card'>URL iCal invalide. Vérifie le lien public .ics.</div></div>", APP_TITLE, user=user), status_code=400)

    p = db.query(Property).filter(Property.id == prop_id, Property.owner_id == user.id).first()
    if not p:
        return HTMLResponse(page("<div class='container'><div class='card'>Logement introuvable.</div></div>", APP_TITLE, user=user), status_code=404)

    p.title = title
    p.ical_url = ical_url
    db.commit()
    return RedirectResponse("/properties", status_code=303)

@app.get("/properties/{prop_id}/delete")
async def properties_delete(prop_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    p = db.query(Property).filter(Property.id == prop_id, Property.owner_id == user.id).first()
    if p:
        db.delete(p)
        db.commit()
    return RedirectResponse("/properties", status_code=303)


# --- Réservations -----------------------------------------------------------
from fastapi.responses import HTMLResponse

@app.get("/reservations", response_class=HTMLResponse)
async def reservations_page(request: Request, user: "User" = Depends(current_user)):
    db = SessionLocal()
    try:
        rows = (
            db.query(Reservation)
            .join(Property, Reservation.property_id == Property.id)
            .filter(Property.owner_id == user.id)
            .order_by(Reservation.start_date.desc())
            .all()
        )

        # En-tête avec "Ajouter" + "Exporter CSV"
        header = """
        <div class="flex items-center justify-between mb-3">
          <h2 class="text-xl font-semibold">Réservations</h2>
          <div class="flex" style="gap:.5rem">
            <a class="badge" href="/reservations/new">Ajouter</a>
            <a class="badge" href="/reservations.csv" download>Exporter CSV</a>
          </div>
        </div>
        """

        # Liste avec bouton "Modifier" par réservation
        items = []
        for r in rows:
            nights = (r.end_date - r.start_date).days if r.end_date and r.start_date else ""
            prop_title = getattr(r.property, "title", "")
            items.append(
    f"<li>{r.guest_name or '–'} — {r.start_date} → {r.end_date} "
    f"({max(0, (r.end_date - r.start_date).days)} nuits) — <small>{getattr(r.property,'title','')}</small> "
    f"<a class='badge' href='/reservations/{r.id}/edit'>Modifier</a> "
    f"<a class='badge' style='background:#fee2e2;color:#991b1b' href='/reservations/{r.id}/delete'>Supprimer</a>"
    f"</li>"
)

        listing = "<ul>" + "\n".join(items) + "</ul>" if items else "<div class='text-gray-600'>Aucune réservation.</div>"

        content = f"""
        <div class="container">
          <div class="card">
            {header}
            {listing}
          </div>
        </div>
        """
        return page(content, APP_TITLE, user=user)
    finally:
        db.close()

# --- Création d'une réservation : formulaire (GET) --------------------------
@app.get("/reservations/new", response_class=HTMLResponse)
async def reservation_new_form(user: "User" = Depends(current_user)):
    db = SessionLocal()
    try:
        # Liste des logements de l'utilisateur pour le select
        props = (
            db.query(Property)
              .filter(Property.owner_id == user.id)
              .order_by(Property.title)
              .all()
        )
        if not props:
            content = "<div class='container'><div class='card'>Crée d'abord un logement pour pouvoir ajouter une réservation.</div></div>"
            return page(content, APP_TITLE, user=user)

        options = "".join(f"<option value='{p.id}'>{p.title}</option>" for p in props)

        today = date.today().isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()

        content = f"""
        <div class="container">
          <div class="card">
            <h2 class="text-xl font-semibold mb-2">Ajouter une réservation</h2>
            <form method="post" action="/reservations/new">
              <div class="mb-2">
                <label>Logement</label>
                <select name="property_id">{options}</select>
              </div>
              <div class="mb-2">
                <label>Nom du client</label>
                <input name="guest_name" placeholder="Nom du voyageur">
              </div>
              <div class="mb-2">
                <label>Début</label>
                <input type="date" name="start_date" value="{today}">
              </div>
              <div class="mb-2">
                <label>Fin</label>
                <input type="date" name="end_date" value="{tomorrow}">
              </div>
              <div class="mb-2">
                <label>Prix total</label>
                <input type="number" step="0.01" name="total_price" placeholder="Facultatif">
              </div>
              <div class="mt-6">
                <button class="btn btn-accent" type="submit">Enregistrer</button>
                <a class="badge" href="/reservations">Annuler</a>
              </div>
            </form>
          </div>
        </div>
        """
        return page(content, APP_TITLE, user=user)
    finally:
        db.close()


# --- Création d'une réservation : enregistrement (POST) --------------------
@app.post("/reservations/new")
async def reservation_new_post(request: Request, user: "User" = Depends(current_user)):
    form = await request.form()
    prop_id  = int(form.get("property_id") or 0)
    guest    = (form.get("guest_name") or "").strip()
    sd       = (form.get("start_date") or "").strip()
    ed       = (form.get("end_date")   or "").strip()
    price_in = form.get("total_price")

    # Validation basique des dates
    try:
        sd_dt = date.fromisoformat(sd)
        ed_dt = date.fromisoformat(ed)
    except Exception:
        return HTMLResponse(
            page("<div class='container'><div class='card'>Dates invalides.</div></div>", APP_TITLE, user=user),
            status_code=400,
        )
    if ed_dt <= sd_dt:
        return HTMLResponse(
            page("<div class='container'><div class='card'>La date de fin doit être après la date de début.</div></div>", APP_TITLE, user=user),
            status_code=400,
        )

    db = SessionLocal()
    try:
        # Vérifie que le logement appartient bien à l'utilisateur
        prop = (
            db.query(Property)
              .filter(Property.id == prop_id, Property.owner_id == user.id)
              .first()
        )
        if not prop:
            return HTMLResponse(
                page("<div class='container'><div class='card'>Logement invalide.</div></div>", APP_TITLE, user=user),
                status_code=400,
            )

        res = Reservation(
            property_id = prop.id,
            guest_name  = guest,
            start_date  = sd_dt,
            end_date    = ed_dt,
            nights      = (ed_dt - sd_dt).days,
            total_price = float(price_in) if price_in not in (None, "") else None,
            source      = "manual",
        )
        db.add(res)
        db.commit()
    finally:
        db.close()

    return RedirectResponse("/reservations", status_code=303)

@app.get("/reservations.csv")
async def reservations_csv(user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    rows = (
        db.query(Reservation)
        .join(Property, Reservation.property_id == Property.id)
        .filter(Property.owner_id == user.id)
        .order_by(Reservation.start_date.desc())
        .all()
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Logement", "Voyageur", "Début", "Fin", "Nuits", "Source", "Montant"])
    for r in rows:
        nights = max(0, (r.end_date - r.start_date).days)
        w.writerow([
            getattr(r.property, "title", ""),
            r.guest_name,
            r.start_date,
            r.end_date,
            nights,
            r.source,
            r.total_price,
        ])
    buf.seek(0)
    return StreamingResponse(
        io.BytesIO(buf.getvalue().encode("utf-8")),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="reservations.csv"'}
    )

# --- Édition d'une réservation : formulaire (GET) ---------------------------
@app.get("/reservations/{res_id}/edit", response_class=HTMLResponse)
async def reservation_edit_form(res_id: int, user: "User" = Depends(current_user)):
    db = SessionLocal()
    try:
        res = (
            db.query(Reservation)
            .join(Property, Reservation.property_id == Property.id)
            .filter(Reservation.id == res_id, Property.owner_id == user.id)
            .first()
        )
        if not res:
            return HTMLResponse(
                page("<div class='container'><div class='card'>Réservation introuvable.</div></div>", APP_TITLE, user=user),
                status_code=404,
            )

        # Logements de l'utilisateur pour le select
        props = (
            db.query(Property)
            .filter(Property.owner_id == user.id)
            .order_by(Property.title)
            .all()
        )
        options = "".join(
            f"<option value='{p.id}' {'selected' if p.id == res.property_id else ''}>{p.title}</option>"
            for p in props
        )

        content = f"""
        <div class="container">
          <div class="card">
            <h2 class="text-xl font-semibold mb-2">Modifier la réservation</h2>
            <form method="post" action="/reservations/{res.id}/edit">
              <div class="mb-2">
                <label>Logement</label>
                <select name="property_id">{options}</select>
              </div>
              <div class="mb-2">
                <label>Nom du client</label>
                <input name="guest_name" value="{(res.guest_name or '').replace('"','&quot;')}">
              </div>
              <div class="mb-2">
                <label>Début</label>
                <input type="date" name="start_date" value="{res.start_date}">
              </div>
              <div class="mb-2">
                <label>Fin</label>
                <input type="date" name="end_date" value="{res.end_date}">
              </div>
              <div class="mb-2">
                <label>Prix total</label>
                <input type="number" step="0.01" name="total_price" value="{res.total_price if res.total_price is not None else ''}">
              </div>
              <div class="mt-6">
                <button class="btn btn-accent" type="submit">Enregistrer</button>
                <a class="badge" href="/reservations">Annuler</a>
              </div>
            </form>
          </div>
        </div>
        """
        return page(content, APP_TITLE, user=user)
    finally:
        db.close()

# --- Édition d'une réservation : enregistrement (POST) ----------------------
@app.post("/reservations/{res_id}/edit")
async def reservation_edit_post(res_id: int, request: Request, user: "User" = Depends(current_user)):
    form = await request.form()
    prop_id  = int(form.get("property_id") or 0)
    guest    = (form.get("guest_name") or "").strip()
    sd       = (form.get("start_date") or "").strip()
    ed       = (form.get("end_date")   or "").strip()
    price_in = form.get("total_price")

    from datetime import date
    try:
        sd_dt = date.fromisoformat(sd)
        ed_dt = date.fromisoformat(ed)
    except Exception:
        return HTMLResponse(
            page("<div class='container'><div class='card'>Dates invalides.</div></div>", APP_TITLE, user=user),
            status_code=400,
        )
    if ed_dt <= sd_dt:
        return HTMLResponse(
            page("<div class='container'><div class='card'>La date de fin doit être après la date de début.</div></div>", APP_TITLE, user=user),
            status_code=400,
        )

    db = SessionLocal()
    try:
        res = (
            db.query(Reservation)
            .join(Property, Reservation.property_id == Property.id)
            .filter(Reservation.id == res_id, Property.owner_id == user.id)
            .first()
        )
        if not res:
            return HTMLResponse(
                page("<div class='container'><div class='card'>Réservation introuvable.</div></div>", APP_TITLE, user=user),
                status_code=404,
            )

        # Vérifie que le logement cible appartient bien à l'utilisateur
        prop = (
            db.query(Property)
            .filter(Property.id == prop_id, Property.owner_id == user.id)
            .first()
        )
        if not prop:
            return HTMLResponse(
                page("<div class='container'><div class='card'>Logement invalide.</div></div>", APP_TITLE, user=user),
                status_code=400,
            )

        # Mise à jour des champs
        res.property_id = prop.id
        res.guest_name  = guest
        res.start_date  = sd_dt
        res.end_date    = ed_dt
        res.nights      = (ed_dt - sd_dt).days  # <-- bien aligné ici !
        res.total_price = float(price_in) if price_in not in (None, "") else None

        db.commit()
    finally:
        db.close()

    return RedirectResponse("/reservations", status_code=303)

# --- Suppression d'une réservation : confirmation (GET) ---------------------
@app.get("/reservations/{res_id}/delete", response_class=HTMLResponse)
async def reservation_delete_confirm(res_id: int, user: "User" = Depends(current_user)):
    db = SessionLocal()
    try:
        res = (
            db.query(Reservation)
            .join(Property, Reservation.property_id == Property.id)
            .filter(Reservation.id == res_id, Property.owner_id == user.id)
            .first()
        )
        if not res:
            return HTMLResponse(
                page("<div class='container'><div class='card'>Réservation introuvable.</div></div>", APP_TITLE, user=user),
                status_code=404,
            )

        prop_title = getattr(res.property, "title", "")
        nights = max(0, (res.end_date - res.start_date).days)  # <-- calcule ici

        content = f"""
        <div class="container">
          <div class="card">
            <h2 class="text-xl font-semibold mb-2">Supprimer la réservation</h2>
            <p class="text-gray-600">
              Logement : <b>{prop_title}</b><br>
              Voyageur : <b>{res.guest_name or '–'}</b><br>
              Séjour : <b>{res.start_date} → {res.end_date}</b> ({nights} nuits)
            </p>
            <form method="post" action="/reservations/{res.id}/delete">
              <button class="btn" style="background:#ef4444">Oui, supprimer</button>
              <a class="badge" href="/reservations">Annuler</a>
            </form>
          </div>
        </div>
        """
        return page(content, APP_TITLE, user=user)
    finally:
        db.close()

# --- Suppression d'une réservation : exécution (POST) ----------------------
@app.post("/reservations/{res_id}/delete")
async def reservation_delete(res_id: int, user: "User" = Depends(current_user)):
    db = SessionLocal()
    try:
        res = (
            db.query(Reservation)
            .join(Property, Reservation.property_id == Property.id)
            .filter(Reservation.id == res_id, Property.owner_id == user.id)
            .first()
        )
        if not res:
            return HTMLResponse(
                page("<div class='container'><div class='card'>Réservation introuvable.</div></div>", APP_TITLE, user=user),
                status_code=404,
            )

        db.delete(res)
        db.commit()
    finally:
        db.close()

    return RedirectResponse("/reservations", status_code=303)

# --- Sync iCal --------------------------------------------------------------
@app.get("/sync")
async def sync_all(user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not user:
        return RedirectResponse("/login", status_code=303)

    imported = 0
    props = db.query(Property).filter(Property.owner_id == user.id, Property.ical_url != "").all()
    for p in props:
        if not validate_ical_url(p.ical_url):
            continue
        try:
            with httpx.Client(timeout=15) as c:
                r = c.get(p.ical_url, follow_redirects=True)
                r.raise_for_status()
                cal = IcsCalendar(r.text)
        except Exception:
            continue

        for ev in cal.events:
            # dates
            try:
                dt_start = ev.begin.date() if hasattr(ev.begin, "date") else dparse(str(ev.begin)).date()
                dt_end = ev.end.date() if hasattr(ev.end, "date") else dparse(str(ev.end)).date()
            except Exception:
                continue

            uid = str(ev.uid or f"{p.id}-{ev.begin}-{ev.end}")
            already = db.query(Reservation).filter(
                Reservation.property_id == p.id,
                Reservation.external_uid == uid
            ).first()
            if already:
                continue

            guest = (ev.name or "").strip()
            db.add(Reservation(
                property_id=p.id,
                source="ical",
                guest_name=guest,
                start_date=dt_start,
                end_date=dt_end,
                total_price=0.0,
                external_uid=uid
            ))
            imported += 1
    db.commit()

    return HTMLResponse(page(f"<div class='container'><div class='card'>Import terminé : {imported} réservation(s) ajoutée(s).</div></div>", APP_TITLE, user=user))


# --- Calendrier simple ------------------------------------------------------
@app.get("/calendar", response_class=HTMLResponse)
async def calendar_view(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not user:
        return RedirectResponse("/login", status_code=303)

    # Création d’un tableau mois en cours + 2 mois
    start = date.today().replace(day=1)
    months = [start, (start + timedelta(days=32)).replace(day=1), (start + timedelta(days=64)).replace(day=1)]

    # Cache des réservations par jour
    res = (
        db.query(Reservation)
        .join(Property, Reservation.property_id == Property.id)
        .filter(Property.owner_id == user.id)
        .all()
    )
    busy = {}  # (prop_id, day) -> True
    titles = {}
    for r in res:
        d0, d1 = r.start_date, r.end_date
        d = d0
        while d < d1:
            busy[(r.property_id, d.isoformat())] = True
            d += timedelta(days=1)
        titles[r.property_id] = getattr(r.property, "title", "")

    # rendus
    month_blocks = []
    for m in months:
        # nombre de jours du mois
        next_m = (m + timedelta(days=32)).replace(day=1)
        days = (next_m - m).days

        rows = []
        for pid, title in sorted(titles.items(), key=lambda kv: kv[1].lower()):
            cells = []
            for d in range(1, days + 1):
                key = (pid, date(m.year, m.month, d).isoformat())
                mark = "●" if busy.get(key) else ""
                cells.append(f"<td style='text-align:center; padding:.25rem .35rem;'>{mark}</td>")
            rows.append(f"<tr><th style='text-align:left; padding:.25rem .35rem;'>{title}</th>{''.join(cells)}</tr>")
        header_days = "".join([f"<th style='padding:.25rem .35rem; text-align:center;'>{i}</th>" for i in range(1, days + 1)])
        table = f"""
          <div class="card" style="overflow:auto;">
            <h3 class="text-xl font-semibold mb-2">{m.strftime('%B %Y').capitalize()}</h3>
            <table style="border-collapse:separate; border-spacing:0 .25rem;">
              <thead><tr><th></th>{header_days}</tr></thead>
              <tbody>{''.join(rows) or "<tr><td>Aucun logement</td></tr>"}</tbody>
            </table>
          </div>
        """
        month_blocks.append(table)

    content = f"""
    <div class="container" style="display:grid; gap:1rem;">
      {''.join(month_blocks)}
    </div>
    """
    return page(content, APP_TITLE, user=user)


# ------------------------------------------------------------
# Lancement local (utile pour tester en dev)
# uvicorn main:app --reload
# ------------------------------------------------------------
