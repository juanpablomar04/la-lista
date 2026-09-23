# -*- coding: utf-8 -*-
"""
main.py — Backend de La lista (FastAPI).

  Público:
    GET  /api/data                 -> categorías + pilotos + campeonato
    GET  /api/avisos?cat=tcpk      -> avisos vigentes
    POST /api/pay                  -> crea preferencia Mercado Pago, devuelve init_point
    GET  /api/access?device=...    -> {paid: bool}
    POST /api/webhook              -> notificaciones de Mercado Pago
  Admin (header  Authorization: Bearer <ADMIN_TOKEN>):
    GET/POST/DELETE /api/admin/pilotos ...
    POST/DELETE     /api/admin/avisos ...
    PUT             /api/admin/campeonato/{cat}
    POST            /api/admin/login   -> valida la clave
  Panel:
    GET  /admin                    -> panel web (admin.html)
"""
import os
import re
import time
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path

ARG_TZ = timezone(timedelta(hours=-3))   # Argentina, sin horario de verano

from fastapi import FastAPI, Depends, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from db import create_store, CATS_META, CAT_IDS

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "cambiame")           # la "clave" del panel
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()   # token de Mercado Pago
PRICE_ARS = float(os.getenv("PRICE_ARS", "1500"))            # monto fijo
PAYWALL = os.getenv("PAYWALL_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
COMP_CODE = os.getenv("COMP_CODE", "").strip()               # código de acceso gratis (cortesía)
PUBLIC_API = os.getenv("PUBLIC_API_URL", "").rstrip("/")     # url pública del backend
PUBLIC_WEB = os.getenv("PUBLIC_WEB_URL", "").rstrip("/")     # url pública de la PWA
CORS_ORIGINS = [o for o in os.getenv("CORS_ORIGINS", "*").split(",") if o]

app = FastAPI(title="La lista API")
app.add_middleware(
    CORSMiddleware, allow_origins=CORS_ORIGINS or ["*"],
    allow_methods=["*"], allow_headers=["*"],
)
store = create_store()


@app.on_event("startup")
async def _startup():
    await store.connect()


@app.on_event("shutdown")
async def _shutdown():
    await store.close()


# ----- auth -----
def require_admin(authorization: str = Header(default="")):
    token = authorization[7:] if authorization.lower().startswith("bearer ") else authorization
    if not token or token != ADMIN_TOKEN:
        raise HTTPException(401, "Clave inválida")
    return True


# ----- modelos -----
class Piloto(BaseModel):
    n: str
    nombre: str
    equipo: str = ""
    marca: str = ""
    modelo: str = ""


class AvisoIn(BaseModel):
    txt: str = Field(min_length=1, max_length=400)
    cat: str | None = None


class CampeonatoIn(BaseModel):
    fechas: str = ""
    tabla: list[dict]


class Sesion(BaseModel):
    hora: str = ""
    cat: str | None = None          # None = general / todas
    actividad: str


class DiaCronograma(BaseModel):
    dia: str                        # "Sábado", "Domingo"
    sesiones: list[Sesion] = []


class CronogramaIn(BaseModel):
    dias: list[DiaCronograma] = []


class PayIn(BaseModel):
    device: str = Field(min_length=6, max_length=100)


class CompIn(BaseModel):
    device: str = Field(min_length=6, max_length=100)
    code: str = Field(min_length=1, max_length=100)


class PingIn(BaseModel):
    device: str = Field(min_length=6, max_length=100)


def _check_cat(cat: str):
    if cat not in CAT_IDS:
        raise HTTPException(404, "Categoría inexistente")


# =====================================================================
# PÚBLICO
# =====================================================================
@app.get("/api/health")
async def health():
    return {"ok": True, "mp": bool(MP_ACCESS_TOKEN)}


@app.get("/api/config")
async def config():
    # El muro solo se activa si además hay token de MP, para no bloquear sin poder cobrar.
    return {"paywall": PAYWALL and bool(MP_ACCESS_TOKEN),
            "price": int(PRICE_ARS) if PRICE_ARS == int(PRICE_ARS) else PRICE_ARS,
            "currency": "ARS"}


@app.post("/api/ping")
async def ping(body: PingIn):
    """Latido de presencia: cada app abierta pega acá cada ~20s.
    Aprovechamos la respuesta para devolver los avisos, así aparecen solos."""
    await store.ping(body.device)
    live = await store.count_live()
    peak = await store.get_peak()
    if live > peak:
        await store.set_peak(live)
        peak = live
    await store.sample_stat(live)
    avisos = await store.list_avisos()      # todos; la app filtra por categoría
    return {"live": live, "peak": peak, "avisos": avisos}


@app.get("/api/live")
async def live():
    return {"live": await store.count_live(), "peak": await store.get_peak()}


@app.post("/api/visit")
async def visit(body: PingIn):
    """Registra una visita (único por dispositivo y día). La app pega una vez al abrir."""
    await store.register_visit(body.device)
    return {"ok": True}


@app.get("/api/data")
async def get_data():
    cats = []
    for meta in CATS_META:
        pilotos = await store.list_pilotos(meta["id"])
        camp = await store.get_campeonato(meta["id"])
        _enrich_campeonato(camp, pilotos)
        cats.append({**meta, "pilotos": pilotos, "campeonato": camp})
    return {"updated": await store.get_updated(),
            "cronograma": await store.get_cronograma(),
            "categories": cats}


def _tokens(nombre: str) -> frozenset:
    """Nombre -> conjunto de palabras normalizadas (sin acentos ni puntuación).
    Sirve para cruzar sin importar el orden: 'Agustín Gusmeroli' y
    'Gusmeroli, Agustín' dan el mismo conjunto."""
    x = unicodedata.normalize("NFKD", nombre or "").encode("ascii", "ignore").decode().lower()
    return frozenset(t for t in re.sub(r"[^a-z0-9 ]", " ", x).split() if t)


def _enrich_campeonato(camp, pilotos):
    """El campeonato guarda posición y puntos; el nombre, la marca y (para el TSC)
    el número los toma de la lista de pilotos, cruzando por número o, si no hay,
    por nombre — comparando el conjunto de palabras, así tolera el orden invertido
    (Nombre Apellido) y los nombres parciales. La lista de pilotos manda."""
    if not camp or not camp.get("tabla"):
        return
    by_num = {str(p.get("n")): p for p in pilotos if p.get("n")}
    pil_tokens = [(p, _tokens(p.get("nombre", ""))) for p in pilotos if p.get("nombre")]

    def by_name(nombre):
        rt = _tokens(nombre)
        if len(rt) < 2:
            return None
        hits = [p for p, pt in pil_tokens if len(pt) >= 2 and rt <= pt]
        return hits[0] if len(hits) == 1 else None      # único, si no es ambiguo

    for row in camp["tabla"]:
        sin_num = not row.get("n") or str(row["n"]) in ("", "—")
        p = None if sin_num else by_num.get(str(row.get("n")))
        if not p and sin_num:
            p = by_name(row.get("nombre", ""))
        if p:
            if p.get("nombre"):
                row["nombre"] = p["nombre"]
            if p.get("marca"):
                row["marca"] = p["marca"]
            if sin_num and p.get("n"):
                row["n"] = p["n"]


@app.get("/api/cronograma")
async def get_cronograma():
    return await store.get_cronograma()


@app.get("/api/avisos")
async def get_avisos(cat: str | None = None):
    return await store.list_avisos(cat)


# =====================================================================
# ADMIN
# =====================================================================
@app.post("/api/admin/login")
async def admin_login(_=Depends(require_admin)):
    return {"ok": True}


@app.get("/api/admin/pilotos")
async def admin_list_pilotos(cat: str, _=Depends(require_admin)):
    _check_cat(cat)
    return await store.list_pilotos(cat)


@app.post("/api/admin/pilotos")
async def admin_upsert_piloto(cat: str, p: Piloto, _=Depends(require_admin)):
    _check_cat(cat)
    await store.upsert_piloto(cat, p.model_dump())
    return {"ok": True}


@app.delete("/api/admin/pilotos/{cat}/{n}")
async def admin_delete_piloto(cat: str, n: str, _=Depends(require_admin)):
    _check_cat(cat)
    await store.delete_piloto(cat, n)
    return {"ok": True}


@app.put("/api/admin/campeonato/{cat}")
async def admin_set_campeonato(cat: str, c: CampeonatoIn, _=Depends(require_admin)):
    _check_cat(cat)
    await store.set_campeonato(cat, c.model_dump())
    return {"ok": True}


@app.put("/api/admin/cronograma")
async def admin_set_cronograma(c: CronogramaIn, _=Depends(require_admin)):
    for d in c.dias:
        for s in d.sesiones:
            if s.cat:
                _check_cat(s.cat)
    await store.set_cronograma(c.model_dump())
    return {"ok": True}


@app.post("/api/admin/avisos")
async def admin_add_aviso(a: AvisoIn, _=Depends(require_admin)):
    if a.cat:
        _check_cat(a.cat)
    return await store.add_aviso(a.cat, a.txt)


@app.delete("/api/admin/avisos/{aid}")
async def admin_delete_aviso(aid: str, _=Depends(require_admin)):
    await store.delete_aviso(aid)
    return {"ok": True}


@app.get("/api/admin/resumen")
async def admin_resumen(_=Depends(require_admin)):
    """Ventas pagadas agrupadas por día (hora argentina), para emitir una sola
    Factura C a Consumidor Final por el total de cada día."""
    ventas = await store.list_ventas()
    facturadas = set(await store.get_facturadas())
    dias = {}
    for v in ventas:
        if v.get("estado") not in ("pagado", "facturado"):
            continue
        dt = datetime.fromtimestamp(v.get("ts", 0), ARG_TZ)
        key = dt.strftime("%Y-%m-%d")
        d = dias.setdefault(key, {"fecha": key, "display": dt.strftime("%d/%m/%Y"),
                                  "cantidad": 0, "total": 0.0})
        d["cantidad"] += 1
        d["total"] += float(v.get("importe") or 0)
    out = sorted(dias.values(), key=lambda x: x["fecha"], reverse=True)
    for d in out:
        d["facturada"] = d["fecha"] in facturadas
    return out


@app.post("/api/admin/resumen/{fecha}/facturar")
async def admin_marcar_facturada(fecha: str, _=Depends(require_admin)):
    estado = await store.toggle_facturada(fecha)
    return {"fecha": fecha, "facturada": estado}


@app.get("/api/admin/comp-link")
async def admin_comp_link(_=Depends(require_admin)):
    return {"code": COMP_CODE or None}


@app.get("/api/admin/stats")
async def admin_stats(_=Depends(require_admin)):
    return {"live": await store.count_live(),
            "peak": await store.get_peak(),
            "series": await store.get_stats(),
            "visitsToday": await store.visits_today(),
            "visitsTotal": await store.visits_total(),
            "visitsSeries": await store.visits_series()}


# =====================================================================
# MERCADO PAGO
# =====================================================================
def _mp():
    if not MP_ACCESS_TOKEN:
        raise HTTPException(503, "Mercado Pago no configurado (falta MP_ACCESS_TOKEN)")
    import mercadopago
    return mercadopago.SDK(MP_ACCESS_TOKEN)


@app.post("/api/pay")
async def create_payment(body: PayIn):
    sdk = _mp()
    await store.create_venta({"device": body.device, "importe": PRICE_ARS,
                              "estado": "iniciada", "payment_id": None, "ts": time.time()})
    pref = {
        "items": [{
            "title": "La lista — acceso carreras Balcarce",
            "quantity": 1,
            "currency_id": "ARS",
            "unit_price": PRICE_ARS,
        }],
        "external_reference": body.device,
        "back_urls": {
            "success": f"{PUBLIC_WEB}/?paid=1",
            "failure": f"{PUBLIC_WEB}/?paid=0",
            "pending": f"{PUBLIC_WEB}/?paid=pending",
        },
        "auto_return": "approved",
        "notification_url": f"{PUBLIC_API}/api/webhook",
    }
    resp = sdk.preference().create(pref)
    data = resp.get("response", {})
    init_point = data.get("init_point") or data.get("sandbox_init_point")
    if not init_point:
        await store.update_venta(body.device, {"estado": "error_mp"})
        raise HTTPException(502, f"Mercado Pago no devolvió init_point: {data}")
    await store.update_venta(body.device, {"pref_id": data.get("id")})
    return {"init_point": init_point, "preference_id": data.get("id")}


@app.post("/api/webhook")
async def mp_webhook(request: Request):
    """Mercado Pago avisa acá. Nunca confiamos en el cliente: pedimos el pago
    a la API de MP y recién si está 'approved' habilitamos el acceso."""
    params = dict(request.query_params)
    try:
        body = await request.json()
    except Exception:
        body = {}
    ptype = params.get("type") or params.get("topic") or body.get("type") or body.get("topic")
    pid = (params.get("data.id") or params.get("id")
           or (body.get("data") or {}).get("id") or body.get("id"))
    if ptype in ("payment", "merchant_order") and pid and MP_ACCESS_TOKEN:
        try:
            sdk = _mp()
            info = sdk.payment().get(pid).get("response", {})
            if info.get("status") == "approved" and info.get("external_reference"):
                dev = info["external_reference"]
                await store.set_access(dev, str(pid))
                v = await store.get_venta(dev)
                if v and v.get("estado") == "iniciada":
                    await store.update_venta(dev, {"estado": "pagado", "payment_id": str(pid)})
        except Exception as e:
            print("[webhook] error verificando pago:", e)
    return JSONResponse({"ok": True})


@app.get("/api/access")
async def check_access(device: str):
    return {"paid": await store.is_paid(device)}


@app.post("/api/comp")
async def redeem_comp(body: CompIn):
    """Acceso de cortesía: si el código coincide, desbloquea ese dispositivo."""
    if COMP_CODE and body.code == COMP_CODE:
        await store.set_access(body.device, "cortesia")
        return {"ok": True}
    raise HTTPException(403, "Código inválido")


# =====================================================================
# PANEL ADMIN (estático)
# =====================================================================
@app.get("/admin")
async def admin_page():
    return FileResponse(Path(__file__).parent / "admin.html")


WEB_DIR = Path(__file__).parent.parent / "web"


@app.get("/", response_class=HTMLResponse)
async def pwa(request: Request):
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    base = PUBLIC_WEB or str(request.base_url).rstrip("/")
    return HTMLResponse(html.replace("__BASE_URL__", base))


# Estáticos de la PWA (sw.js, manifest, iconos). Va último para no pisar /api ni /admin.
app.mount("/", StaticFiles(directory=WEB_DIR), name="web")
