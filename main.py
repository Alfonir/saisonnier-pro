from __future__ import annotations
import asyncio
from datetime import date, datetime, timedelta
from typing import Optional, Dict
from uuid import uuid4
import os
import httpx

from fastapi import FastAPI, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from pydantic import EmailStr

from sqlalchemy import (
    Column, Integer, String, Date, DateTime, Float, ForeignKey, Text,
    UniqueConstraint, select, create_engine
)
from sqlalchemy.orm import sessionmaker, declarative_base, relationship, Session

from dateutil.relativedelta import relativedelta
from ics import Calendar

# ----------------- App config -----------------
APP_TITLE = "Saisonnier Pro – MVP"

DB_URL_RAW = os.getenv("DATABASE_URL", "sqlite:///./saisonnier.db")
# Render/Heroku donnent souvent "postgres://", SQLAlchemy 2.x veut "postgresql+psycopg2://"
DB_URL = DB_URL_RAW.replace("postgres://", "postgresql+psycopg2://", 1)

connect_args = {"check_same_thread": False} if DB_URL.startswith("sqlite") else {}
engine = create_engine(
    DB_URL,
    connect_args=connect_args,
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# ----------------- Models -----------------
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, nullable=False)
    name = Column(String, default="")
    password = Column(String, nullable=False)  # MVP only (hash en prod)
    properties = relationship("Property", back_populates="owner")

class Property(Base):
    __tablename__ = "properties"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String, nullable=False)
    address = Column(String, default="")
    base_price = Column(Float, default=80.0)
    capacity = Column(Integer, default=2)
    ical_url = Column(Text)
    last_sync = Column(DateTime)
    owner = relationship("User", back_populates="properties")
    reservations = relationship("Reservation", back_populates="prop", cascade="all, delete-orphan")

class Reservation(Base):
    __tablename__ = "reservations"
    id = Column(Integer, primary_key=True)
    property_id = Column(Integer, ForeignKey("properties.id"), nullable=False)
    source = Column(String, default="manual")
    guest_name = Column(String, default="")
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    total_price = Column(Float, default=0.0)
    external_uid = Column(String)
    __table_args__ = (UniqueConstraint('property_id','external_uid', name='uix_prop_uid'),)
    prop = relationship("Property", back_populates="reservations")

Base.metadata.create_all(bind=engine)

# ----------------- App / templating -----------------
app = FastAPI(title=APP_TITLE)
app.mount("/static", StaticFiles(directory="static"), name="static")

from jinja2 import Environment, select_autoescape
env = Environment(autoescape=select_autoescape())

def render_str(html: str, **ctx) -> str:
    return env.from_string(html).render(**ctx)

BASE_HEAD = """
<link href="https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css" rel="stylesheet">
<script src="https://unpkg.com/htmx.org@1.9.12"></script>
<style>
:root{--primary:#0B1B36;--gold:#C9A959}
.btn{background:var(--primary);color:#fff;padding:.55rem 1rem;border-radius:.6rem}
.btn-gold{background:var(--gold);color:#0B1B36}
.card{background:white;border-radius:1rem;box-shadow:0 10px 25px rgba(0,0,0,.06);padding:1.25rem}
.badge{padding:.2rem .6rem;border-radius:.5rem;background:#eef}
</style>
"""

SESSIONS: Dict[str,int] = {}

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

async def current_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    token = request.cookies.get("sess")
    if token and token in SESSIONS:
        uid = SESSIONS[token]
        return db.get(User, uid)
    return None

LAYOUT = """
<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">{head}
<title>{title}</title></head>
<body class="bg-gray-50">
<header class="bg-white shadow">
  <div class="max-w-6xl mx-auto py-4 px-4 flex items-center justify-between">
    <a href="/" class="text-xl font-semibold" style="color:var(--primary)">{app_title}</a>
    <nav class="space-x-3 text-sm">
      {% if user %}
      <a class="badge" href="/properties">Logements</a>
      <a class="badge" href="/calendar">Calendrier</a>
      <a class="badge" href="/reservations">Réservations</a>
      <a class="badge" href="/sync">Sync</a>
      <a class="badge" href="/logout">Déconnexion</a>
      {% else %}
      <a class="badge" href="/login">Connexion</a>
      <a class="badge" href="/signup">Créer un compte</a>
      {% endif %}
    </nav>
  </div>
</header>
<main class="max-w-6xl mx-auto p-4">{content}</main>
</body></html>
"""

# ----------------- Pages -----------------
@app.get("/healthz")
def health():
    return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, user: Optional[User] = Depends(current_user)):
    content = """
    <div class="grid md:grid-cols-2 gap-4">
      <div class="card">
        <h1 class="text-2xl font-semibold mb-2">Centralisez vos réservations.</h1>
        <p class="text-gray-600">Import iCal, calendrier consolidé, et planning ménage.</p>
        <a href="/signup" class="btn-gold btn inline-block mt-4">Créer un compte</a>
      </div>
      <div class="card">
        <div class="text-gray-600">Déjà un compte ?</div>
        <a href="/login" class="btn mt-2">Se connecter</a>
      </div>
    </div>
    """
    html = LAYOUT.format(head=BASE_HEAD, title=APP_TITLE, app_title=APP_TITLE, content=content)
    return HTMLResponse(render_str(html, user=user))

@app.get("/signup", response_class=HTMLResponse)
async def signup_form(request: Request, user: Optional[User] = Depends(current_user)):
    if user:
        return RedirectResponse("/properties", status_code=302)
    content = """
    <div class="card max-w-md mx-auto">
      <form method="post" class="grid gap-3">
        <input name="name" placeholder="Nom" class="border p-2 rounded" required>
        <input name="email" placeholder="Email" class="border p-2 rounded" required>
        <input type="password" name="password" placeholder="Mot de passe" class="border p-2 rounded" required>
        <button class="btn">Créer mon compte</button>
      </form>
    </div>
    """
    html = LAYOUT.format(head=BASE_HEAD, title="Créer un compte", app_title=APP_TITLE, content=content)
    return HTMLResponse(render_str(html, user=None))

@app.post("/signup")
async def signup(name: str = Form(...), email: EmailStr = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    if db.execute(select(User).where(User.email == email)).scalar_one_or_none():
        raise HTTPException(400, "Cet email est déjà utilisé")
    u = User(email=email, name=name, password=password)
    db.add(u); db.commit()
    token = str(uuid4()); SESSIONS[token] = u.id
    resp = RedirectResponse("/properties", status_code=302)
    resp.set_cookie("sess", token, httponly=True)
    return resp

@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, user: Optional[User] = Depends(current_user)):
    if user:
        return RedirectResponse("/properties", status_code=302)
    content = """
    <div class="card max-w-md mx-auto">
      <form method="post" class="grid gap-3">
        <input name="email" placeholder="Email" class="border p-2 rounded" required>
        <input type="password" name="password" placeholder="Mot de passe" class="border p-2 rounded" required>
        <button class="btn">Se connecter</button>
      </form>
    </div>
    """
    html = LAYOUT.format(head=BASE_HEAD, title="Connexion", app_title=APP_TITLE, content=content)
    return HTMLResponse(render_str(html, user=None))

@app.post("/login")
async def login(email: EmailStr = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    u = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if not u or u.password != password:
        raise HTTPException(400, "Identifiants invalides (MVP)")
    token = str(uuid4()); SESSIONS[token] = u.id
    resp = RedirectResponse("/properties", status_code=302)
    resp.set_cookie("sess", token, httponly=True)
    return resp

@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get("sess")
    if token and token in SESSIONS:
        del SESSIONS[token]
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie("sess")
    return resp

def get_user_props(db: Session, user_id: int):
    return db.execute(select(Property).where(Property.user_id == user_id).order_by(Property.id.desc())).scalars().all()

@app.get("/properties", response_class=HTMLResponse)
async def properties_page(request: Request, db: Session = Depends(get_db), user: Optional[User] = Depends(current_user)):
    if not user: return RedirectResponse("/login", status_code=302)
    props = get_user_props(db, user.id)
    content = """
    <div class="flex items-center justify-between mb-4">
      <h1 class="text-2xl font-semibold">Mes logements</h1>
      <a href="#" hx-get="/properties/new" hx-target="#modal" class="btn btn-gold">Ajouter</a>
    </div>
    <div class="grid md:grid-cols-2 gap-4">
      {% for p in props %}
      <div class="card">
        <div class="flex items-center justify-between">
          <div>
            <div class="font-semibold">{{p.title}}</div>
            <div class="text-sm text-gray-600">{{p.address}}</div>
          </div>
          <div class="text-right">
            <div class="text-sm">Base: {{'%.2f'|format(p.base_price)}} €/nuit</div>
            {% if p.last_sync %}<div class="text-xs text-gray-500">Synch: {{p.last_sync}}</div>{% endif %}
          </div>
        </div>
        <div class="mt-3 text-sm">
          <div>Capacité: {{p.capacity}}</div>
          <div class="truncate">iCal: {{p.ical_url or '—'}}</div>
        </div>
        <div class="mt-3 flex gap-2">
          <a class="badge" href="/reservations?property_id={{p.id}}">Réservations</a>
          <a class="badge" href="/calendar?property_id={{p.id}}">Calendrier</a>
          <a class="badge" href="/properties/{{p.id}}/edit" hx-get="/properties/{{p.id}}/edit" hx-target="#modal">Éditer</a>
        </div>
      </div>
      {% endfor %}
    </div>
    <div id="modal"></div>
    """
    html = LAYOUT.format(head=BASE_HEAD, title="Logements", app_title=APP_TITLE, content=content)
    return HTMLResponse(render_str(html, user=user, props=props))

@app.get("/properties/new", response_class=HTMLResponse)
async def property_new_modal():
    return HTMLResponse("""
    <div class="card max-w-xl mx-auto">
      <form method="post" action="/properties" class="grid grid-cols-2 gap-3">
        <input name="title" placeholder="Titre" class="border p-2 rounded col-span-2" required>
        <input name="address" placeholder="Adresse" class="border p-2 rounded col-span-2">
        <input name="base_price" placeholder="Prix/nuit (€)" class="border p-2 rounded" value="80">
        <input name="capacity" placeholder="Capacité" class="border p-2 rounded" value="2">
        <input name="ical_url" placeholder="URL iCal (optionnel)" class="border p-2 rounded col-span-2">
        <button class="btn col-span-2">Enregistrer</button>
      </form>
    </div>
    """)

@app.post("/properties")
async def property_create(request: Request, db: Session = Depends(get_db), user: Optional[User] = Depends(current_user)):
    if not user: return RedirectResponse("/login", status_code=302)
    form = await request.form()
    p = Property(
        user_id=user.id,
        title=form.get("title"),
        address=form.get("address",""),
        base_price=float(form.get("base_price") or 80),
        capacity=int(form.get("capacity") or 2),
        ical_url=form.get("ical_url") or None
    )
    db.add(p); db.commit()
    return RedirectResponse("/properties", status_code=302)

@app.get("/properties/{pid}/edit", response_class=HTMLResponse)
async def property_edit_modal(pid: int, db: Session = Depends(get_db), user: Optional[User] = Depends(current_user)):
    if not user: return RedirectResponse("/login", status_code=302)
    p = db.get(Property, pid)
    if not p or p.user_id != user.id: raise HTTPException(404)
    return HTMLResponse(f"""
    <div class="card max-w-xl mx-auto">
      <form method="post" action="/properties/{pid}/edit" class="grid grid-cols-2 gap-3">
        <input name="title" value="{p.title}" class="border p-2 rounded col-span-2" required>
        <input name="address" value="{p.address or ''}" class="border p-2 rounded col-span-2">
        <input name="base_price" value="{p.base_price}" class="border p-2 rounded">
        <input name="capacity" value="{p.capacity}" class="border p-2 rounded">
        <input name="ical_url" value="{p.ical_url or ''}" class="border p-2 rounded col-span-2">
        <button class="btn col-span-2">Mettre à jour</button>
      </form>
    </div>
    """)

@app.post("/properties/{pid}/edit")
async def property_update(pid: int, request: Request, db: Session = Depends(get_db), user: Optional[User] = Depends(current_user)):
    if not user: return RedirectResponse("/login", status_code=302)
    p = db.get(Property, pid)
    if not p or p.user_id != user.id: raise HTTPException(404)
    form = await request.form()
    p.title = form.get("title")
    p.address = form.get("address")
    p.base_price = float(form.get("base_price"))
    p.capacity = int(form.get("capacity"))
    p.ical_url = form.get("ical_url") or None
    db.commit()
    return RedirectResponse("/properties", status_code=302)

@app.get("/reservations", response_class=HTMLResponse)
async def reservations_page(request: Request, property_id: Optional[int] = None, db: Session = Depends(get_db), user: Optional[User] = Depends(current_user)):
    if not user: return RedirectResponse("/login", status_code=302)
    q = select(Reservation).join(Property).where(Property.user_id == user.id)
    if property_id: q = q.where(Reservation.property_id == property_id)
    rows = db.execute(q.order_by(Reservation.start_date.desc())).scalars().all()
    props = get_user_props(db, user.id)
    content = """
    <div class="flex items-center justify-between mb-4">
      <h1 class="text-2xl font-semibold">Réservations</h1>
      <a href="#" hx-get="/reservations/new" hx-target="#modal" class="btn btn-gold">Ajouter</a>
    </div>
    <form method="get" class="mb-3">
      <select name="property_id" class="border p-2 rounded" onchange="this.form.submit()">
        <option value="">Tous les logements</option>
        {% for p in props %}
          <option value="{{p.id}}">{{p.title}}</option>
        {% endfor %}
      </select>
    </form>
    <div class="card overflow-auto">
      <table class="min-w-full text-sm">
        <thead><tr class="text-left"><th>Logement</th><th>Voyageur</th><th>Source</th><th>Arrivée</th><th>Départ</th><th>Total</th></tr></thead>
        <tbody>
          {% for r in rows %}
          <tr class="border-t"><td>{{r.prop.title}}</td><td>{{r.guest_name}}</td><td>{{r.source}}</td><td>{{r.start_date}}</td><td>{{r.end_date}}</td><td>{{'%.2f'|format(r.total_price or 0)}}</td></tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
    <div id="modal"></div>
    """
    html = LAYOUT.format(head=BASE_HEAD, title="Réservations", app_title=APP_TITLE, content=content)
    return HTMLResponse(render_str(html, user=user, rows=rows, props=props, request=request))

@app.get("/reservations/new", response_class=HTMLResponse)
async def reservation_new_modal(request: Request, db: Session = Depends(get_db), user: Optional[User] = Depends(current_user)):
    if not user: return RedirectResponse("/login", status_code=302)
    props = get_user_props(db, user.id)
    options = "".join([f"<option value='{p.id}'>{p.title}</option>" for p in props])
    return HTMLResponse(f"""
    <div class="card max-w-xl mx-auto">
      <form method="post" action="/reservations" class="grid grid-cols-2 gap-3">
        <select name="property_id" class="border p-2 rounded col-span-2" required>{options}</select>
        <input name="guest_name" placeholder="Nom voyageur" class="border p-2 rounded col-span-2" required>
        <input type="date" name="start_date" class="border p-2 rounded" required>
        <input type="date" name="end_date" class="border p-2 rounded" required>
        <input name="total_price" placeholder="Total (€)" class="border p-2 rounded">
        <button class="btn col-span-2">Enregistrer</button>
      </form>
    </div>
    """)

@app.post("/reservations")
async def reservation_create(request: Request, db: Session = Depends(get_db), user: Optional[User] = Depends(current_user)):
    if not user: return RedirectResponse("/login", status_code=302)
    form = await request.form()
    r = Reservation(
        property_id=int(form.get("property_id")),
        guest_name=form.get("guest_name"),
        source="manual",
        start_date=datetime.strptime(form.get("start_date"), "%Y-%m-%d").date(),
        end_date=datetime.strptime(form.get("end_date"), "%Y-%m-%d").date(),
        total_price=float(form.get("total_price") or 0)
    )
    db.add(r); db.commit()
    return RedirectResponse("/reservations", status_code=302)

@app.get("/calendar", response_class=HTMLResponse)
async def calendar_page(request: Request, property_id: Optional[int] = None, year: Optional[int]=None, month: Optional[int]=None, db: Session = Depends(get_db), user: Optional[User] = Depends(current_user)):
    if not user: return RedirectResponse("/login", status_code=302)
    today = date.today()
    year = year or today.year
    month = month or today.month
    first = date(year, month, 1)
    last = (first + relativedelta(months=1)) - timedelta(days=1)

    props = get_user_props(db, user.id)
    prs = props if not property_id else [p for p in props if p.id == property_id]

    rows = db.execute(
        select(Reservation).join(Property)
        .where(Property.user_id == user.id)
        .where(Reservation.start_date <= last)
        .where(Reservation.end_date >= first)
    ).scalars().all()

    days = [(first + timedelta(days=i)) for i in range((last-first).days+1)]
    occ = {p.id: {d: False for d in days} for p in prs}
    for r in rows:
        for d in days:
            if r.property_id in occ and (r.start_date <= d < r.end_date):
                occ[r.property_id][d] = True

    head_days = "".join([f"<th class='px-2 text-xs'>{d.day}</th>" for d in days])
    lines = []
    for p in prs:
        tds = []
        for d in days:
            busy = occ[p.id][d]
            tds.append(f"<td class='w-5 h-5 {'bg-green-500' if busy else 'bg-gray-200'}'></td>")
        lines.append(f"<tr><td class='text-xs pr-2 whitespace-nowrap'>{p.title}</td>{''.join(tds)}</tr>")

    selector = "".join([f"<option value='{p.id}' {'selected' if property_id and p.id==property_id else ''}>{p.title}</option>" for p in props])
    content = f"""
    <div class="flex items-center justify-between mb-4">
      <h1 class="text-2xl font-semibold">Calendrier – {month:02d}/{year}</h1>
      <form method="get" class="flex items-center gap-2">
        <select name="property_id" class="border p-2 rounded"><option value="">Tous</option>{selector}</select>
        <input name="month" value="{month}" class="border p-2 rounded w-16">
        <input name="year" value="{year}" class="border p-2 rounded w-20">
        <button class="btn-gold btn">Voir</button>
      </form>
    </div>
    <div class="card overflow-auto">
      <table class="text-[11px]">
        <thead><tr><th class="pr-2">Logement</th>{head_days}</tr></thead>
        <tbody>{''.join(lines)}</tbody>
      </table>
    </div>
    """
    html = LAYOUT.format(head=BASE_HEAD, title="Calendrier", app_title=APP_TITLE, content=content)
    return HTMLResponse(render_str(html, user=user))

# ----------------- iCal sync -----------------
async def fetch_ical(url: str) -> Calendar:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url)
        r.raise_for_status()
        return Calendar(r.text)

async def sync_property_ical(db: Session, prop: Property):
    if not prop.ical_url: return
    try:
        cal = await fetch_ical(prop.ical_url)
    except Exception:
        return
    for ev in cal.events:
        start = ev.begin.date() if hasattr(ev.begin, 'date') else ev.begin
        end = ev.end.date() if hasattr(ev.end, 'date') else ev.end
        uid = (ev.uid or f"{ev.name}-{start}-{end}")[:255]
        existing = db.execute(select(Reservation).where(Reservation.property_id==prop.id, Reservation.external_uid==uid)).scalar_one_or_none()
        if existing:
            existing.start_date = start
            existing.end_date = end
            existing.guest_name = ev.name or existing.guest_name
        else:
            db.add(Reservation(property_id=prop.id, source="ical", guest_name=ev.name or "(iCal)", start_date=start, end_date=end, external_uid=uid))
    prop.last_sync = datetime.utcnow()
    db.commit()

@app.get("/sync")
async def sync_now(db: Session = Depends(get_db), user: Optional[User] = Depends(current_user)):
    if not user: return RedirectResponse("/login", status_code=302)
    props = db.execute(select(Property).where(Property.user_id==user.id).where(Property.ical_url.isnot(None))).scalars().all()
    for p in props:
        await sync_property_ical(db, p)
    return RedirectResponse("/properties", status_code=302)

@app.on_event("startup")
async def schedule_sync_task():
    async def _task():
        while True:
            db = SessionLocal()
            try:
                props = db.execute(select(Property).where(Property.ical_url.isnot(None))).scalars().all()
                for p in props:
                    await sync_property_ical(db, p)
            finally:
                db.close()
            await asyncio.sleep(30 * 60)
    asyncio.create_task(_task())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
