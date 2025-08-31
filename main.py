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
from sqlalchemy import text

import html
from textwrap import dedent

def esc(s: str | None) -> str:
    """Échappe &, <, > et " pour un usage sûr dans value=""."""
    return html.escape(s or "", quote=True)

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

# --- Init DB au démarrage ---
@app.on_event("startup")
def _init_db():
    try:
        Base.metadata.create_all(bind=engine)
    except Exception:
        pass

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="templates")
templates.env.globals.update(
    APP_NAME=APP_NAME,
    APP_TAGLINE=APP_TAGLINE,
)

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

env.globals.update(
    APP_NAME=APP_NAME,
    APP_TAGLINE=APP_TAGLINE,
)

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
    --bg:#f7fafc;           /* claire */
    --ink:#0f172a;          /* texte principal */
    --muted:#64748b;        /* texte secondaire */
    --card:#ffffff;         /* cartes */
    --surface:#eff6ff;      /* surfaces pâles */
    --ring:rgba(14,165,233,.35);
    --radius:16px;
    --shadow:0 10px 30px rgba(2, 6, 23, .08);
    --shadow-soft:0 6px 20px rgba(2, 6, 23, .06);
    --brand-start:#0ea5e9;  /* sky-500 */
    --brand-end:#22d3ee;    /* cyan-400 */
  }
  *{box-sizing:border-box}
  html,body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 "Inter",system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif}
  a{color:inherit;text-decoration:none}
  .container{max-width:1200px;margin:0 auto;padding:0 20px}

  /* Header sticky + blur */
  .headbar{
    position:sticky; top:0; z-index:50; backdrop-filter:saturate(140%) blur(12px);
    background:linear-gradient(180deg, rgba(255,255,255,.75), rgba(255,255,255,.35));
    border-bottom:1px solid rgba(15,23,42,.06);
  }
  .logo{
    display:flex;align-items:center;gap:.75rem;font-weight:800;font-size:1.05rem;letter-spacing:.2px;
  }
  .logo-mark{
    width:34px;height:34px;border-radius:10px;display:inline-block;box-shadow:var(--shadow-soft);
    background:radial-gradient(120% 120% at 0% 0%, var(--brand-end) 0%, var(--brand-start) 60%, #2563eb 100%);
  }

  /* NAV en deux groupes (gauche = sections, droite = auth) */
  .topnav{display:flex;align-items:center;justify-content:space-between;gap:1rem;flex-wrap:wrap}
  .nav-group{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap}
  .pill{
    display:inline-flex;align-items:center;gap:.5rem;padding:.55rem .9rem;border-radius:999px;
    background:rgba(99,102,241,.06);border:1px solid rgba(15,23,42,.06);font-weight:700;
    transition:.2s; box-shadow:0 1px 0 rgba(255,255,255,.4) inset;
  }
  .pill:hover{transform:translateY(-1px);box-shadow:var(--shadow-soft)}
  .pill.active{background:linear-gradient(90deg, var(--brand-start), var(--brand-end));color:#fff;border-color:transparent}
  .pill-accent{background:#0b1020;color:#fff}

  .spacer{height:18px}

  /* Layouts / cards / hero */
  .grid{display:grid;gap:24px}
  .card{
    background:var(--card); border-radius:var(--radius); box-shadow:var(--shadow); padding:28px;
    border:1px solid rgba(15,23,42,.06);
  }
  h1{font-size:2.35rem; line-height:1.15; margin:0 0 .5rem; letter-spacing:-.02em}
  p.lead{color:var(--muted); margin:.25rem 0 1.2rem}

  /* Hero en 2 colonnes (responsive) */
  .hero{
    display:grid;
    grid-template-columns:1.1fr .9fr;
    gap:24px;
    align-items:stretch;
    padding:32px 0;
  }
  @media (max-width: 900px){
    .hero{ grid-template-columns:1fr; }
  }

  /* Buttons */
  .btn{
    appearance:none; border:0; cursor:pointer; font-weight:800; border-radius:14px;
    padding:.9rem 1.2rem; box-shadow:var(--shadow-soft); transition:.15s;
  }
  .btn:focus{outline:3px solid var(--ring); outline-offset:2px}
  .btn.primary{
    color:#083344; background:linear-gradient(90deg, var(--brand-start), var(--brand-end));
  }
  .btn.dark{ background:#0b1020; color:#fff }
</style>
"""

def ui_notice(message: str, title: str = "Information", tone: str = "info") -> str:
    colors = {
        "success": ("#10b981", "rgba(16,185,129,.12)"),
        "info":    ("#0ea5e9", "rgba(14,165,233,.12)"),
        "warning": ("#f59e0b", "rgba(245,158,11,.12)"),
        "error":   ("#ef4444", "rgba(239,68,68,.12)"),
    }
    color, bg = colors.get(tone, colors["info"])
    return f"""
    <div class="container">
      <div class="card" style="border-left:6px solid {color}">
        <div style="display:flex;gap:12px;align-items:flex-start">
          <div style="width:10px;height:10px;border-radius:999px;background:{color};margin-top:8px"></div>
          <div>
            <div style="font-weight:800;color:{color};margin-bottom:.25rem">{title}</div>
            <div style="background:{bg};padding:.6rem .8rem;border-radius:10px">{message}</div>
          </div>
        </div>
      </div>
    </div>
    """

def page(content: str, title: str = APP_TITLE, user: Optional[User] = None, active: str = "") -> str:
    return render_str("""
<!doctype html>
<html lang="fr" class="no-js" data-theme="light">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{{ title }}</title>
  <link rel="icon" href="/static/favicon.svg" type="image/svg+xml" />
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">

  <style>
    :root{
      --bg:#f7fafc;
      --ink:#0f172a;
      --muted:#64748b;
      --card:#ffffff;
      --surface:#eff6ff;
      --ring:rgba(14,165,233,.35);
      --radius:16px;
      --shadow:0 10px 30px rgba(2, 6, 23, .08);
      --shadow-soft:0 6px 20px rgba(2, 6, 23, .06);
      --brand-start:#0ea5e9;
      --brand-end:#22d3ee;
    }
    *{box-sizing:border-box}
    html,body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 "Inter",system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif}
    a{color:inherit;text-decoration:none}
    .container{max-width:1200px;margin:0 auto;padding:0 20px}
    .headbar{position:sticky; top:0; z-index:50; backdrop-filter:saturate(140%) blur(12px);
      background:linear-gradient(180deg, rgba(255,255,255,.75), rgba(255,255,255,.35));
      border-bottom:1px solid rgba(15,23,42,.06);}
    .logo{display:flex;align-items:center;gap:.75rem;font-weight:800;font-size:1.05rem;letter-spacing:.2px;}
    .logo-mark{width:34px;height:34px;border-radius:10px;display:inline-block;box-shadow:var(--shadow-soft);
      background:radial-gradient(120% 120% at 0% 0%, var(--brand-end) 0%, var(--brand-start) 60%, #2563eb 100%);}
    .topnav{display:flex;align-items:center;justify-content:space-between;gap:1rem;flex-wrap:wrap}
    .nav-group{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap}
    .pill{display:inline-flex;align-items:center;gap:.5rem;padding:.55rem .9rem;border-radius:999px;
      background:rgba(99,102,241,.06);border:1px solid rgba(15,23,42,.06);font-weight:700;
      transition:.2s; box-shadow:0 1px 0 rgba(255,255,255,.4) inset;}
    .pill:hover{transform:translateY(-1px);box-shadow:var(--shadow-soft)}
    .pill.active{background:linear-gradient(90deg, var(--brand-start), var(--brand-end));color:#fff;border-color:transparent}
    .pill-accent{background:#0b1020;color:#fff}
    .spacer{height:18px}
    .grid{display:grid;gap:24px}
    .card{background:var(--card); border-radius:var(--radius); box-shadow:var(--shadow); padding:28px;
      border:1px solid rgba(15,23,42,.06);}
    h1{font-size:2.35rem; line-height:1.15; margin:0 0 .5rem; letter-spacing:-.02em}
    p.lead{color:var(--muted); margin:.25rem 0 1.2rem}
    .btn{appearance:none; border:0; cursor:pointer; font-weight:800; border-radius:14px;
      padding:.9rem 1.2rem; box-shadow:var(--shadow-soft); transition:.15s;}
    .btn:focus{outline:3px solid var(--ring); outline-offset:2px}
    .btn.primary{color:#083344; background:linear-gradient(90deg, var(--brand-start), var(--brand-end));}
    .btn.dark{ background:#0b1020; color:#fff }
  </style>
</head>

<body>
<header class="headbar">
  <div class="container" style="display:flex;align-items:center;justify-content:space-between;padding:.8rem 0;">
    <div class="logo">
      <span class="logo-mark"></span>
      <div>
        <div style="font-weight:800">{{ APP_NAME }}</div>
        <div style="font-size:.78rem;color:var(--muted);margin-top:-2px">{{ APP_TAGLINE }}</div>
      </div>
    </div>

    <nav class="topnav">
      <div class="nav-group">
        <a class="pill {% if active=='properties' %}active{% endif %}" href="/properties">Logements</a>
        <a class="pill {% if active=='calendar' %}active{% endif %}" href="/calendar">Calendrier</a>
        <a class="pill {% if active=='reservations' %}active{% endif %}" href="/reservations">Réservations</a>
        <a class="pill {% if active=='sync' %}active{% endif %}" href="/sync">Sync</a>
      </div>
      <div class="nav-group">
        {% if user %}
          <a class="pill" href="/logout">Déconnexion</a>
        {% else %}
          <a class="pill" href="/login">Connexion</a>
          <a class="pill pill-accent" href="/signup">Créer un compte</a>
        {% endif %}
      </div>
    </nav>
  </div>
</header>

<div class="spacer"></div>

<main class="container">
  {{ content | safe }}
</main>

</body>
</html>
""", title=title, user=user, active=active, content=content)
    
# --- UI helper : carte de notification (succès / erreur / info) -------------
def ui_notice(
    message: str,
    title: str = "Oups…",
    tone: str = "error",           # "error" | "success" | "info"
) -> str:
    colors = {
        "error":  {"bg":"#fff1f2","bd":"#fecdd3","ink":"#7f1d1d","chip":"#fecaca"},
        "success":{"bg":"#ecfdf5","bd":"#bbf7d0","ink":"#064e3b","chip":"#a7f3d0"},
        "info":   {"bg":"#eff6ff","bd":"#bfdbfe","ink":"#0c4a6e","chip":"#dbeafe"},
    }
    c = colors.get(tone, colors["info"])
    return f"""
    <div class="container">
      <div style="
        max-width: 760px; margin: 0 auto;
        background:#fff; border:1px solid rgba(15,23,42,.06);
        border-radius:18px; padding:24px; box-shadow:0 18px 40px rgba(2,6,23,.08);
      ">
        <div style="
          background:{c['bg']}; border:1px solid {c['bd']}; border-radius:14px; padding:16px 18px;
        ">
          <div style="display:flex; align-items:center; gap:.6rem; margin-bottom:.35rem">
            <span style="display:inline-block; padding:.25rem .55rem; border-radius:999px;
                         background:{c['chip']}; font-weight:800; font-size:.8rem; color:{c['ink']}">
              { 'Erreur' if tone=='error' else 'Succès' if tone=='success' else 'Info' }
            </span>
            <strong style="color:{c['ink']}; font-weight:800">{title}</strong>
          </div>
          <div style="color:{c['ink']}">{message}</div>
          <div style="margin-top:12px">
            <a href="javascript:history.back()" style="
               display:inline-flex; align-items:center; gap:.45rem;
               padding:.6rem .9rem; border-radius:12px; text-decoration:none;
               border:1px solid rgba(15,23,42,.12); color:#0f172a;
            ">Retour</a>
          </div>
        </div>
      </div>
    </div>
    """

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
<div class="hero" style="display:grid;grid-template-columns:1.1fr .9fr;gap:28px;align-items:stretch;padding:36px 0">
  
  <!-- Bloc gauche -->
  <div style="background:#fff;border-radius:18px;padding:28px;
              box-shadow:0 10px 25px rgba(2,6,23,.06);
              border:1px solid rgba(15,23,42,.06);display:flex;flex-direction:column;">
    <h1 style="font-size:2.25rem;line-height:1.15;margin:0 0 .5rem;letter-spacing:-.02em;color:#0f172a">
      Centralisez vos réservations.
    </h1>
    <p style="margin:.25rem 0 1.25rem;color:#475569;font-size:1.05rem">
      Import iCal, calendrier consolidé, et planning ménage.
    </p>
    <div style="margin-top:auto">
      <a href="/signup"
         style="text-decoration:none;display:inline-flex;align-items:center;gap:.5rem;
                padding:.9rem 1.2rem;border-radius:14px;font-weight:800;
                background:linear-gradient(90deg,#0ea5e9,#22d3ee);color:#083344;
                box-shadow:0 8px 24px rgba(14,165,233,.35)">
        Créer un compte
      </a>
    </div>
  </div>

  <!-- Bloc droit -->
  <div style="background:#fff;border-radius:18px;padding:28px;
              box-shadow:0 10px 25px rgba(2,6,23,.06);
              border:1px solid rgba(15,23,42,.06);display:flex;flex-direction:column;justify-content:space-between">
    <div style="font-size:1.2rem;font-weight:600;color:#0f172a;margin-bottom:1rem">
      Déjà un compte ?
    </div>
    <div style="margin-top:auto">
      <a href="/login"
         style="text-decoration:none;display:inline-flex;align-items:center;gap:.5rem;
                padding:.9rem 1.2rem;border-radius:14px;font-weight:800;
                background:#0b1020;color:#fff;box-shadow:0 6px 18px rgba(2,6,23,.15)">
        Se connecter
      </a>
    </div>
  </div>
</div>

</section>
"""
    print(">>>> HOME EXECUTED <<<<")
    return page(content, APP_TITLE, user=user, active="properties")

# --- Signup / Login / Logout --------------------------------
@app.get("/signup", response_class=HTMLResponse)
async def signup_get(request: Request, user: Optional[User] = Depends(current_user)):
    if user:
        return RedirectResponse("/properties", status_code=303)

    content = """
    <div class="container">
      <div style="
        max-width: 760px; margin: 0 auto;
        background:#fff; border:1px solid rgba(15,23,42,.06);
        border-radius:18px; padding:28px; box-shadow:0 18px 40px rgba(2,6,23,.08);
      ">
        <h2 style="font-size:2rem; font-weight:800; margin:0 0 1.25rem; letter-spacing:-.02em; color:#0f172a">
          Créer un compte
        </h2>

        <form method="post" action="/signup" autocomplete="off" style="display:grid; gap:14px">
          <div style="display:grid; gap:.5rem">
            <label style="font-weight:600; color:#0f172a">Email</label>
            <input name="email" type="email" required
                   style="width:100%; border:1px solid #e2e8f0; border-radius:12px; padding:.8rem .9rem; outline:0"
                   onfocus="this.style.boxShadow='0 0 0 4px rgba(14,165,233,.25)'; this.style.borderColor='#0ea5e9'"
                   onblur="this.style.boxShadow='none'; this.style.borderColor='#e2e8f0'"/>
          </div>

          <div style="display:grid; gap:.5rem">
            <label style="font-weight:600; color:#0f172a">Nom</label>
            <input name="name" type="text"
                   style="width:100%; border:1px solid #e2e8f0; border-radius:12px; padding:.8rem .9rem; outline:0"
                   onfocus="this.style.boxShadow='0 0 0 4px rgba(14,165,233,.25)'; this.style.borderColor='#0ea5e9'"
                   onblur="this.style.boxShadow='none'; this.style.borderColor='#e2e8f0'"/>
          </div>

          <div style="display:grid; gap:.5rem">
            <label style="font-weight:600; color:#0f172a">Mot de passe</label>
            <input name="password" type="password" required
                   style="width:100%; border:1px solid #e2e8f0; border-radius:12px; padding:.8rem .9rem; outline:0"
                   onfocus="this.style.boxShadow='0 0 0 4px rgba(14,165,233,.25)'; this.style.borderColor='#0ea5e9'"
                   onblur="this.style.boxShadow='none'; this.style.borderColor='#e2e8f0'"/>
          </div>

          <div style="display:flex; gap:.6rem; margin-top:.5rem">
            <button type="submit"
              style="appearance:none; border:0; cursor:pointer; font-weight:800; border-radius:14px;
                     padding:.9rem 1.2rem; color:#083344;
                     background:linear-gradient(90deg,#0ea5e9,#22d3ee);
                     box-shadow:0 8px 22px rgba(14,165,233,.35)">
              Créer mon compte
            </button>
            <a href="/login"
               style="display:inline-flex; align-items:center; padding:.85rem 1.1rem; border-radius:12px;
                      border:1px solid rgba(15,23,42,.12); color:#0f172a; text-decoration:none;">
              J’ai déjà un compte
            </a>
          </div>
        </form>
      </div>
    </div>
    """
    return page(content, APP_TITLE, user=None, active="")

from sqlalchemy import text, func
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError

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
        return HTMLResponse(
    page(ui_notice("Email invalide.", title="Inscription", tone="error"), APP_TITLE),
    status_code=400
)

    if not pwd:
        return HTMLResponse(page(ui_notice("Le mot de passe est requis.", title="Mot de passe manquant"), APP_TITLE), status_code=400)

    db = SessionLocal()
    try:
        # --- crée les tables si 'users' n'existe pas
        try:
            db.execute(text("SELECT 1 FROM users LIMIT 1"))
        except (OperationalError, ProgrammingError):
            Base.metadata.create_all(bind=engine)

        # email déjà pris ?
        exists = db.query(User).filter(func.lower(User.email) == email_clean).first()
        if exists:
            return HTMLResponse(page(ui_notice("Cet email est déjà utilisé. Essaie de te connecter.", title="Compte existant"), APP_TITLE), status_code=400)

        u = User(email=email_clean, name=name_clean, password=hash_password(pwd))
        db.add(u)
        db.commit()

        resp = RedirectResponse("/properties", status_code=303)
        resp.set_cookie("uid", str(u.id), httponly=True, samesite="lax")
        return resp

    except IntegrityError:
        db.rollback()
        return HTMLResponse(page(ui_notice("Ce compte existe déjà. Essaie avec « Mot de passe oublié » (plus tard) ou connecte-toi.", title="Compte existant"), APP_TITLE), status_code=400)

    except Exception as e:
        # Renvoie bien un code 500 en cas d’exception réelle
        return HTMLResponse(
    page(ui_notice(f"Erreur serveur pendant l’inscription.<br><small>{esc(type(e).__name__)}: {esc(str(e))}</small>", title="Désolé…", tone="error"), APP_TITLE),
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
      <div style="
        max-width: 640px; margin: 0 auto;
        background:#fff; border:1px solid rgba(15,23,42,.06);
        border-radius:18px; padding:28px; box-shadow:0 18px 40px rgba(2,6,23,.08);
      ">
        <h2 style="font-size:2rem; font-weight:800; margin:0 0 1.25rem; letter-spacing:-.02em; color:#0f172a">
          Connexion
        </h2>

        <form method="post" action="/login" autocomplete="on" style="display:grid; gap:14px">
          <div style="display:grid; gap:.5rem">
            <label style="font-weight:600; color:#0f172a">Email</label>
            <input name="email" type="email" required
                   style="width:100%; border:1px solid #e2e8f0; border-radius:12px; padding:.8rem .9rem; outline:0"
                   onfocus="this.style.boxShadow='0 0 0 4px rgba(14,165,233,.25)'; this.style.borderColor='#0ea5e9'"
                   onblur="this.style.boxShadow='none'; this.style.borderColor='#e2e8f0'"/>
          </div>

          <div style="display:grid; gap:.5rem">
            <label style="font-weight:600; color:#0f172a">Mot de passe</label>
            <input name="password" type="password" required
                   style="width:100%; border:1px solid #e2e8f0; border-radius:12px; padding:.8rem .9rem; outline:0"
                   onfocus="this.style.boxShadow='0 0 0 4px rgba(14,165,233,.25)'; this.style.borderColor='#0ea5e9'"
                   onblur="this.style.boxShadow='none'; this.style.borderColor='#e2e8f0'"/>
          </div>

          <div style="display:flex; gap:.6rem; margin-top:.5rem">
            <button type="submit"
              style="appearance:none; border:0; cursor:pointer; font-weight:800; border-radius:14px;
                     padding:.9rem 1.2rem; color:#fff; background:#0b1020;
                     border:1px solid rgba(15,23,42,.12); box-shadow:0 6px 18px rgba(2,6,23,.20)">
              Se connecter
            </button>
            <a href="/signup"
               style="display:inline-flex; align-items:center; padding:.85rem 1.1rem; border-radius:12px;
                      border:1px solid rgba(15,23,42,.12); color:#0f172a; text-decoration:none;">
              Créer un compte
            </a>
          </div>
        </form>
      </div>
    </div>
    """
    return page(content, APP_TITLE, user=None, active="")

@app.post("/login")
async def login_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...)
):
    email_clean = (email or "").strip().lower()
    pwd = (password or "").strip()

    if not email_clean or not pwd:
        return HTMLResponse(page(ui_notice("Email et mot de passe requis.", title="Champs requis"), APP_TITLE), status_code=400)

    db = SessionLocal()
    try:
        # lookup insensible à la casse
        user = db.query(User).filter(func.lower(User.email) == email_clean).first()
        if not user:
            return HTMLResponse(page(ui_notice("Identifiants invalides. Vérifie ton email et ton mot de passe.", title="Connexion impossible"), APP_TITLE), status_code=400)

        # vérif compat (hash/legacy) + migration éventuelle vers hash
        if verify_password(pwd, user.password):
            if not looks_like_sha256(user.password):
                user.password = hash_password(pwd)
                db.commit()

            resp = RedirectResponse("/properties", status_code=303)
            resp.set_cookie("uid", str(user.id), httponly=True, samesite="lax")
            return resp

            return HTMLResponse(page(ui_notice("Identifiants invalides. Vérifie ton email et ton mot de passe.", title="Connexion impossible"), APP_TITLE), status_code=400)

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
                <input name="guest_name" value="{esc(res.guest_name)}">
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

# ---- Suppression d'une réservation : confirmation (GET) --------------------
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

        prop_title = getattr(res.property, "title", "") or ""
        nights = max(0, (res.end_date - res.start_date).days)

        # IMPORTANT : on ouvre et on FERME bien la f-string triple-quoted
        content = f"""
<div class="container">
  <div class="card">
    <h2 class="text-xl font-semibold mb-2">Supprimer la réservation</h2>
    <p class="text-gray-600">
      Logement : <b>{esc(prop_title)}</b><br>
      Voyageur : <b>{esc(res.guest_name) or '-'}</b><br>
      Séjour : <b>{res.start_date} &rarr; {res.end_date}</b> ({nights} nuits)
    </p>
    <form method="post" action="/reservations/{res.id}/delete" style="display:flex; gap:.5rem">
      <button class="btn" style="background:#ef4444">Oui, supprimer</button>
      <a class="btn ghost" href="/reservations">Annuler</a>
    </form>
  </div>
</div>
"""
        return HTMLResponse(page(content, APP_TITLE, user=user))
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

    return HTMLResponse(
    page(
        ui_notice(f"Import terminé : {imported} réservation(s) ajoutée(s).", title="Import iCal", tone="success"),
        APP_TITLE, user=user
    )
)

# --- Calendrier simple ------------------------------------------------------
@app.get("/calendar", response_class=HTMLResponse)
async def calendar_view(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not user:
        return RedirectResponse("/login", status_code=303)

    # Mois courant + 2 suivants
    start = date.today().replace(day=1)
    months = [start, (start + timedelta(days=32)).replace(day=1), (start + timedelta(days=64)).replace(day=1)]

    # Réservations de l'utilisateur
    res = (
        db.query(Reservation)
        .join(Property, Reservation.property_id == Property.id)
        .filter(Property.owner_id == user.id)
        .all()
    )

    busy: dict[tuple[int, str], bool] = {}  # (prop_id, yyyy-mm-dd) -> True
    titles: dict[int, str] = {}

    for r in res:
        d = r.start_date
        while d < r.end_date:
            busy[(r.property_id, d.isoformat())] = True
            d += timedelta(days=1)
        titles[r.property_id] = getattr(r.property, "title", "")

    month_blocks: list[str] = []

    for m in months:
        next_m = (m + timedelta(days=32)).replace(day=1)
        days = (next_m - m).days

        rows: list[str] = []
        for pid, title in sorted(titles.items(), key=lambda kv: kv[1].lower()):
            cells: list[str] = []
            for d in range(1, days + 1):
                day_key = (pid, date(m.year, m.month, d).isoformat())
                mark = "●" if busy.get(day_key) else ""
                cells.append(f"<td style='text-align:center; padding:.25rem .35rem;'>{mark}</td>")

            header_days = "".join(f"<th style='padding:.25rem .35rem; text-align:center;'>{i}</th>" for i in range(1, days + 1))
            row_cells = "".join(cells)
            rows.append(f"<tr><th style='text-align:left; padding:.25rem .35rem;'>{title}</th>{row_cells}</tr>")

        table = dedent(f"""
        <div class="card" style="overflow:auto;">
          <h3 class="text-xl font-semibold mb-2">{m.strftime('%B %Y').capitalize()}</h3>
          <table style="border-collapse:separate; border-spacing:0 .25rem;">
            <thead><tr>{header_days}</tr></thead>
            <tbody>{(''.join(rows)) or "<tr><td>Aucun logement</td></tr>"}</tbody>
          </table>
        </div>
        """)
        month_blocks.append(table)

    content = dedent(f"""
    <div class="container" style="display:grid; gap:1rem;">
      {''.join(month_blocks)}
    </div>
    """)
    return page(content, APP_TITLE, user=user)

# ------------------------------------------------------------
# Lancement local (utile pour tester en dev)
# uvicorn main:app --reload
# ------------------------------------------------------------
