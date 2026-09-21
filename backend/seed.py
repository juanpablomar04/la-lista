# -*- coding: utf-8 -*-
"""
seed.py — carga inicial de la base a partir de la salida del scraper.

    python scraper.py                 # genera data/index.json
    MONGODB_URI="..." python seed.py  # lo mete en Mongo (una vez)

Después de sembrar, la lista se mantiene desde el panel /admin. El scraper es
solo el arranque, no un feed permanente.
"""
import asyncio
import json
import os
import sys

from db import create_store, CAT_IDS


async def main(path):
    data = json.load(open(path, encoding="utf-8"))
    store = create_store()
    await store.connect()
    total = 0
    for c in data.get("categories", []):
        cid = c.get("id")
        if cid not in CAT_IDS:
            print(f"  · categoría desconocida, salteada: {cid}")
            continue
        for p in c.get("pilotos", []):
            await store.upsert_piloto(cid, {
                "n": str(p.get("n", "")),
                "nombre": p.get("nombre", ""),
                "equipo": p.get("equipo", ""),
                "marca": p.get("marca", ""),
                "modelo": p.get("modelo", ""),
            })
            total += 1
        if c.get("campeonato"):
            await store.set_campeonato(cid, c["campeonato"])
        print(f"  ✓ {cid}: {len(c.get('pilotos', []))} pilotos"
              f"{' + campeonato' if c.get('campeonato') else ''}")
    await store.close()
    print(f"\nSembrado: {total} pilotos.")


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "../data/index.json"
    if not os.path.exists(src):
        sys.exit(f"No encuentro {src}. Corré primero: python scraper.py")
    asyncio.run(main(src))
