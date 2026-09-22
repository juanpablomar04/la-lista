# -*- coding: utf-8 -*-
"""
db.py — capa de datos de La lista.

Usa MongoDB (motor) si hay MONGODB_URI; si no, un store en memoria para poder
correr y probar todo local sin configurar nada. La API pública es idéntica en
los dos casos, así que main.py no sabe cuál está usando.
"""
import os
import time
from datetime import datetime, timezone

# Metadatos de categorías (viven en código, no en la base: casi nunca cambian).
CATS_META = [
    {"id": "tcpk",  "name": "TC Pick Up",                  "short": "TC PK",       "acc": "#e63946"},
    {"id": "tcppk", "name": "TC Pista Pick Up",            "short": "TC Pista PK", "acc": "#f4a12a"},
    {"id": "tsc",   "name": "Turismo Special de la Costa", "short": "TSC",         "acc": "#38b2ac"},
]
CAT_IDS = {c["id"] for c in CATS_META}


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y %H:%M")


def _num_key(n: str):
    try:
        return (0, int(n))
    except (TypeError, ValueError):
        return (1, 0)


# ---------------------------------------------------------------------------
# Store en memoria (fallback / desarrollo)
# ---------------------------------------------------------------------------
class MemStore:
    def __init__(self):
        self.pilotos = {c: {} for c in CAT_IDS}      # cat -> {n: doc}
        self.campeonato = {c: None for c in CAT_IDS}  # cat -> {fechas, tabla}
        self.avisos = []                              # [{id, cat, txt, ts}]
        self.access = {}                              # device -> {paid, payment_id, ts}
        self.ventas = {}                              # device -> {datos comprador + factura}
        self.facturadas = set()                       # fechas YYYY-MM-DD ya facturadas en ARCA
        self.presence = {}                            # device -> último latido (epoch)
        self.peak = 0                                 # máximo de conectados en simultáneo
        self.stats = []                               # [{t, count}] muestreado ~1/min
        self._last_sample = 0.0
        self.cronograma = {"dias": []}                # {dias:[{dia, sesiones:[{hora,cat,actividad}]}]}
        self.updated = _now_iso()
        self._aid = 0

    async def connect(self): ...
    async def close(self): ...

    def _touch(self):
        self.updated = _now_iso()

    async def list_pilotos(self, cat):
        docs = list(self.pilotos.get(cat, {}).values())
        return sorted(docs, key=lambda d: _num_key(d.get("n")))

    async def upsert_piloto(self, cat, doc):
        self.pilotos.setdefault(cat, {})[str(doc["n"])] = doc
        self._touch()

    async def delete_piloto(self, cat, n):
        self.pilotos.get(cat, {}).pop(str(n), None)
        self._touch()

    async def get_campeonato(self, cat):
        return self.campeonato.get(cat)

    async def set_campeonato(self, cat, data):
        self.campeonato[cat] = data
        self._touch()

    async def list_avisos(self, cat=None):
        out = [a for a in self.avisos if (not a.get("cat") or a["cat"] == cat)] if cat \
            else list(self.avisos)
        return sorted(out, key=lambda a: a["ts"], reverse=True)

    async def add_aviso(self, cat, txt):
        self._aid += 1
        a = {"id": f"a{self._aid}", "cat": cat or None, "txt": txt, "ts": time.time()}
        self.avisos.append(a)
        self._touch()
        return a

    async def delete_aviso(self, aid):
        self.avisos = [a for a in self.avisos if a["id"] != aid]
        self._touch()

    async def set_access(self, device, payment_id):
        self.access[device] = {"paid": True, "payment_id": payment_id, "ts": time.time()}

    async def is_paid(self, device):
        return bool(self.access.get(device, {}).get("paid"))

    async def create_venta(self, v):
        self.ventas[v["device"]] = v

    async def get_venta(self, device):
        return self.ventas.get(device)

    async def update_venta(self, device, fields):
        if device in self.ventas:
            self.ventas[device].update(fields)

    async def list_ventas(self):
        return sorted(self.ventas.values(), key=lambda x: x.get("ts", 0), reverse=True)

    async def get_facturadas(self):
        return sorted(self.facturadas)

    async def toggle_facturada(self, fecha):
        if fecha in self.facturadas:
            self.facturadas.discard(fecha)
            return False
        self.facturadas.add(fecha)
        return True

    async def ping(self, device):
        self.presence[device] = time.time()

    async def count_live(self, window=45):
        cut = time.time() - window
        return sum(1 for t in self.presence.values() if t >= cut)

    async def get_peak(self):
        return self.peak

    async def set_peak(self, v):
        self.peak = v

    async def sample_stat(self, count):
        now = time.time()
        if now - self._last_sample >= 55:
            self._last_sample = now
            self.stats.append({"t": int(now), "count": count})
            self.stats = self.stats[-1000:]

    async def get_stats(self):
        return self.stats[-720:]

    async def get_cronograma(self):
        return self.cronograma

    async def set_cronograma(self, data):
        self.cronograma = data
        self._touch()

    async def get_updated(self):
        return self.updated


# ---------------------------------------------------------------------------
# Store MongoDB (producción)
# ---------------------------------------------------------------------------
class MongoStore:
    def __init__(self, uri, dbname="lalista"):
        from motor.motor_asyncio import AsyncIOMotorClient
        self.client = AsyncIOMotorClient(uri)
        self.db = self.client[dbname]

    async def connect(self):
        await self.db.pilotos.create_index([("cat", 1), ("n", 1)], unique=True)
        await self.db.avisos.create_index([("ts", -1)])
        await self.db.access.create_index("device", unique=True)
        await self.db.ventas.create_index("device", unique=True)

    async def close(self):
        self.client.close()

    async def _touch(self):
        await self.db.meta.update_one(
            {"_id": "updated"}, {"$set": {"value": _now_iso()}}, upsert=True)

    async def list_pilotos(self, cat):
        cur = self.db.pilotos.find({"cat": cat}, {"_id": 0})
        docs = [d async for d in cur]
        return sorted(docs, key=lambda d: _num_key(d.get("n")))

    async def upsert_piloto(self, cat, doc):
        doc = {**doc, "cat": cat, "n": str(doc["n"])}
        await self.db.pilotos.update_one({"cat": cat, "n": doc["n"]}, {"$set": doc}, upsert=True)
        await self._touch()

    async def delete_piloto(self, cat, n):
        await self.db.pilotos.delete_one({"cat": cat, "n": str(n)})
        await self._touch()

    async def get_campeonato(self, cat):
        return await self.db.campeonato.find_one({"cat": cat}, {"_id": 0, "cat": 0})

    async def set_campeonato(self, cat, data):
        await self.db.campeonato.update_one({"cat": cat}, {"$set": {**data, "cat": cat}}, upsert=True)
        await self._touch()

    async def list_avisos(self, cat=None):
        q = {"$or": [{"cat": None}, {"cat": cat}]} if cat else {}
        cur = self.db.avisos.find(q, {"_id": 0}).sort("ts", -1)
        return [a async for a in cur]

    async def add_aviso(self, cat, txt):
        a = {"id": f"a{int(time.time()*1000)}", "cat": cat or None, "txt": txt, "ts": time.time()}
        await self.db.avisos.insert_one(dict(a))
        await self._touch()
        return a

    async def delete_aviso(self, aid):
        await self.db.avisos.delete_one({"id": aid})
        await self._touch()

    async def set_access(self, device, payment_id):
        await self.db.access.update_one(
            {"device": device},
            {"$set": {"device": device, "paid": True, "payment_id": payment_id, "ts": time.time()}},
            upsert=True)

    async def is_paid(self, device):
        d = await self.db.access.find_one({"device": device})
        return bool(d and d.get("paid"))

    async def create_venta(self, v):
        await self.db.ventas.update_one({"device": v["device"]}, {"$set": v}, upsert=True)

    async def get_venta(self, device):
        return await self.db.ventas.find_one({"device": device}, {"_id": 0})

    async def update_venta(self, device, fields):
        await self.db.ventas.update_one({"device": device}, {"$set": fields})

    async def list_ventas(self):
        cur = self.db.ventas.find({}, {"_id": 0}).sort("ts", -1)
        return [v async for v in cur]

    async def get_facturadas(self):
        m = await self.db.meta.find_one({"_id": "facturadas"})
        return m["value"] if m else []

    async def toggle_facturada(self, fecha):
        cur = set(await self.get_facturadas())
        estado = fecha not in cur
        cur.add(fecha) if estado else cur.discard(fecha)
        await self.db.meta.update_one(
            {"_id": "facturadas"}, {"$set": {"value": sorted(cur)}}, upsert=True)
        return estado

    async def ping(self, device):
        await self.db.presence.update_one(
            {"device": device}, {"$set": {"device": device, "last": time.time()}}, upsert=True)

    async def count_live(self, window=45):
        cut = time.time() - window
        return await self.db.presence.count_documents({"last": {"$gte": cut}})

    async def get_peak(self):
        m = await self.db.meta.find_one({"_id": "peak"})
        return m["value"] if m else 0

    async def set_peak(self, v):
        await self.db.meta.update_one({"_id": "peak"}, {"$set": {"value": v}}, upsert=True)

    async def sample_stat(self, count):
        m = await self.db.meta.find_one({"_id": "laststat"})
        last = m["value"] if m else 0
        now = time.time()
        if now - last >= 55:
            await self.db.meta.update_one({"_id": "laststat"}, {"$set": {"value": now}}, upsert=True)
            await self.db.stats.insert_one({"t": int(now), "count": count})

    async def get_stats(self):
        cur = self.db.stats.find({}, {"_id": 0}).sort("t", -1).limit(720)
        return list(reversed([s async for s in cur]))

    async def get_updated(self):
        m = await self.db.meta.find_one({"_id": "updated"})
        return m["value"] if m else _now_iso()

    async def get_cronograma(self):
        m = await self.db.meta.find_one({"_id": "cronograma"})
        return m["value"] if m else {"dias": []}

    async def set_cronograma(self, data):
        await self.db.meta.update_one(
            {"_id": "cronograma"}, {"$set": {"value": data}}, upsert=True)
        await self._touch()


def create_store():
    uri = os.getenv("MONGODB_URI", "").strip()
    if uri:
        print("[db] MongoDB")
        return MongoStore(uri, os.getenv("MONGODB_DB", "lalista"))
    print("[db] EN MEMORIA (sin MONGODB_URI) — los datos se pierden al reiniciar")
    return MemStore()
