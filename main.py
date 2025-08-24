# main.py
# ---------------------------------------------------------------------------
# Synchronisation iCal
# ---------------------------------------------------------------------------
async def fetch_ical(url: str) -> Calendar:
async with httpx.AsyncClient(timeout=20) as client:
r = await client.get(url)
r.raise_for_status()
return Calendar(r.text)


async def sync_property_ical(db: Session, prop: Property):
if not prop.ical_url:
return
try:
cal = await fetch_ical(prop.ical_url)
except Exception:
return
# Parser événements -> réservations
for ev in cal.events:
# Evitez les all-day vs timed: on normalise en date
start = ev.begin.date() if hasattr(ev.begin, 'date') else ev.begin
end = ev.end.date() if hasattr(ev.end, 'date') else ev.end
uid = (ev.uid or f"{ev.name}-{start}-{end}")[:255]
# Chercher doublon
existing = db.execute(select(Reservation).where(Reservation.property_id==prop.id, Reservation.external_uid==uid)).scalar_one_or_none()
if existing:
# mettre à jour si besoin
existing.start_date = start
existing.end_date = end
existing.guest_name = ev.name or existing.guest_name
else:
r = Reservation(property_id=prop.id, source="ical", guest_name=ev.name or "(iCal)", start_date=start, end_date=end, external_uid=uid)
db.add(r)
prop.last_sync = datetime.utcnow()
db.commit()


@app.get("/sync")
async def sync_now(db: Session = Depends(get_db), user: Optional[User] = Depends(current_user)):
if not user:
return RedirectResponse("/login", status_code=302)
props = db.execute(select(Property).where(Property.user_id==user.id).where(Property.ical_url.isnot(None))).scalars().all()
for p in props:
await sync_property_ical(db, p)
return RedirectResponse("/properties", status_code=302)


# Tâche de synchronisation périodique (lancée au démarrage)
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
await asyncio.sleep(SYNC_PERIOD_MIN * 60)
asyncio.create_task(_task())


# ---------------------------------------------------------------------------
# Emails – démonstration console
# ---------------------------------------------------------------------------
@app.get("/emails", response_class=HTMLResponse)
async def emails_demo(request: Request, user: Optional[User] = Depends(current_user)):
if not user:
return RedirectResponse("/login", status_code=302)
content = """
<div class="card max-w-2xl mx-auto">
<h2 class="text-xl font-semibold mb-2">Modèles d'emails (démo)</h2>
<p class="text-gray-600 text-sm">Ici on afficherait les modèles (confirmation, check-in, post-séjour). En MVP, on imprime en console.</p>
<form method="post" action="/emails/test" class="mt-3 grid gap-2">
<input name="to" placeholder="Email destinataire" class="border p-2 rounded" required>
<button class="btn">Envoyer un test (console)</button>
</form>
</div>
"""
html = LAYOUT.format(head=BASE_HEAD, title="Emails", app_title=APP_TITLE, content=content)
return HTMLResponse(templates.get_template_from_string(html).render(user=user))


@app.post("/emails/test")
async def email_test(to: EmailStr = Form(...)):
print(f"[EMAIL TEST] vers {to}: Bonjour, ceci est un test d'email automatique Saisonnier Pro – MVP.")
return RedirectResponse("/emails", status_code=302)
