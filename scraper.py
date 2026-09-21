#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scraper.py — Pilotos y campeonatos de TC Pick Up, TC Pista Pick Up y TSC.

Genera ./data/index.json con la MISMA forma que consume la PWA (index.html):
    { "updated": "...", "categories": [ {id, name, short, acc, source,
                                         pilotos:[...], campeonato:{...}|null} ] }

Uso:
    pip install requests beautifulsoup4
    python scraper.py

Se ejecuta en tu máquina o en CI (GitHub Actions). NO va en el teléfono.

NOTA: las páginas de ACTC son Next.js y las de TSC WordPress. Los selectores
de abajo se basan en la estructura observada el 21/09/2026 y usan anclas/tablas
(no clases CSS frágiles). Si algo cambia, los puntos marcados con  # TUNE  son
los que hay que revisar. Corré esto y, si un campo sale vacío, pasame la salida
o el HTML crudo de esa página y lo ajusto al toque.
"""

import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
OUT = Path("data")

# Config por categoría. La forma de salida es la que espera la app.
CATS = [
    {"id": "tcpk",  "name": "TC Pick Up",                  "short": "TC PK",
     "acc": "#e63946", "source": "actc.org.ar", "kind": "actc", "slug": "tcpk"},
    {"id": "tcppk", "name": "TC Pista Pick Up",            "short": "TC Pista PK",
     "acc": "#f4a12a", "source": "actc.org.ar", "kind": "actc", "slug": "tcppk"},
    {"id": "tsc",   "name": "Turismo Special de la Costa", "short": "TSC",
     "acc": "#38b2ac", "source": "turismospecialdelacosta.com.ar", "kind": "tsc",
     "pilotos_url": "https://turismospecialdelacosta.com.ar/pilotos/",
     "camp_url":    "https://turismospecialdelacosta.com.ar/campeonato-3/"},
]


def get(url: str) -> BeautifulSoup:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return BeautifulSoup(r.text, "html.parser")


def titlecase_es(s: str) -> str:
    """Title-case respetando de/del/la y (H)."""
    small = {"de", "del", "la", "y", "e"}
    out = []
    for i, w in enumerate(s.lower().split()):
        if w in ("(h)",):
            out.append("(H)")
        elif w in small and i != 0:
            out.append(w)
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out)


# ---------------------------------------------------------------------------
# ACTC  (TC Pick Up / TC Pista Pick Up) — Next.js, contenido en el HTML
# ---------------------------------------------------------------------------
def actc_pilotos(slug: str) -> list[dict]:
    soup = get(f"https://actc.org.ar/{slug}/pilotos")
    seen, pilotos = set(), []
    # Cada piloto es un <a href=".../pilotos/ID">. Filtramos el menú/otros.  # TUNE
    for a in soup.select(f'a[href*="/{slug}/pilotos/"]'):
        href = a.get("href", "")
        pid = href.rstrip("/").split("/")[-1]
        if not pid.isdigit() or pid in seen:
            continue
        seen.add(pid)
        text = a.get_text(" ", strip=True)
        pilotos.append(_parse_actc_piloto(text, pid))
    return pilotos


def _parse_actc_piloto(text: str, pid: str) -> dict:
    """text ~ '1 Agustín Canapino / Canning Motorsports Chevrolet (S10) Canapino, Agustín'"""
    numero = (re.match(r"^\s*(\d+)", text) or [None, ""])[1]
    # marca + modelo:  'Chevrolet (S10)' / 'Foton (Tunland G7)' / 'Ford Ranger'
    veh = re.search(r"([A-Za-zÀ-ÿ]+)\s*\(([^)]+)\)", text)         # TUNE
    if veh:
        marca, modelo = veh.group(1), veh.group(2)
    else:
        m2 = re.search(r"\b(Ford|Toyota|Chevrolet|Volkswagen|Foton|Nissan|RAM)\b\s*([A-Za-z0-9\-]*)", text)
        marca = m2.group(1) if m2 else ""
        modelo = (m2.group(2).strip() if m2 else "")
    # nombre: preferimos el formato final 'Apellido, Nombre' (el último match)
    names = re.findall(r"([A-Za-zÀ-ÿ'’.\(\) ]+?,\s*[A-Za-zÀ-ÿ'’. ]+)\s*$", text)
    nombre = names[-1].strip() if names else _fallback_name(text, numero)
    # equipo: entre '/ ' y la marca
    equipo = ""
    m_eq = re.search(r"/\s*(.+?)\s+(?:Ford|Toyota|Chevrolet|Volkswagen|Foton|Nissan|RAM)\b", text)
    if m_eq:
        equipo = m_eq.group(1).strip()
    return {"n": numero, "nombre": nombre, "equipo": equipo,
            "marca": marca, "modelo": modelo, "id": pid}


def _fallback_name(text: str, numero: str) -> str:
    # Sin 'Apellido, Nombre': tomamos lo que va antes del '/'
    head = re.sub(r"^\s*\d+\s*", "", text.split("/")[0]).strip()
    return head


def actc_campeonato(slug: str, marca_por_n: dict) -> dict | None:
    soup = get(f"https://actc.org.ar/{slug}/campeonato")
    table = None
    for t in soup.find_all("table"):
        if t.find("a", href=re.compile(r"/pilotos/")):            # la tabla de posiciones  # TUNE
            table = t
            break
    if not table:
        return None
    tabla, pos = [], 0
    for tr in table.find_all("tr"):
        a = tr.find("a", href=re.compile(r"/pilotos/"))
        if not a:
            continue
        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c != ""]
        if len(cells) < 2:
            continue
        pos += 1
        numero = cells[1] if cells[1].isdigit() else _num_from(cells)
        # el nombre 'APELLIDO, NOMBRE' está en la celda (el link envuelve la foto)
        piloto_td = a.find_parent(["td", "th"])
        cell_text = piloto_td.get_text(" ", strip=True) if piloto_td else a.get_text(" ", strip=True)
        m = re.search(r"([A-ZÁÉÍÓÚÑÜÀ-Ý][A-ZÁÉÍÓÚÑÜÀ-Ý'.()\s]*,\s*"
                      r"[A-ZÁÉÍÓÚÑÜÀ-Ý][A-ZÁÉÍÓÚÑÜÀ-Ý'.()\s]*)", cell_text)
        nombre = titlecase_es(re.sub(r"\d+$", "", m.group(1)).strip()) if m else ""
        pts = _first_points(cells)
        tabla.append({"pos": pos, "n": numero, "nombre": nombre,
                      "marca": marca_por_n.get(numero, ""), "pts": pts})
    if not tabla:
        return None
    return {"fechas": _fechas_actc(soup), "tabla": tabla}


def _num_from(cells):
    for c in cells[:3]:
        if c.isdigit():
            return c
    return "—"


def _first_points(cells):
    # El TOTAL es la primera cifra "grande" tras pos/nº/nombre; formato AR con coma.
    for c in cells[2:]:
        if re.fullmatch(r"\d+(?:[.,]\d+)?", c):
            return c.replace(".", ",")                            # TUNE (columna Total)
    return ""


def _fechas_actc(soup) -> str:
    m = re.search(r"(\d+)\s*pilotos", soup.get_text(" ", strip=True))
    return f"{m.group(1)} pilotos rankeados" if m else "Campeonato 2026"


# ---------------------------------------------------------------------------
# TSC — WordPress, tablas <table> planas. La marca es un logo (imagen):
# se identifica por el nombre de archivo del logo.  # TUNE (agregar marcas nuevas)
# ---------------------------------------------------------------------------
TSC_LOGO_MARCA = {
    "logo-dodge-2-xs-copia.png": "Dodge",
    "image.png": "Ford",
    "image-1.png": "Chevrolet",
    "image-2.png": "Torino",
    "logo-chrysler-xs-copia.png": "Chrysler",
}


def _tsc_marca(tr):
    img = tr.find("img")
    if not img:
        return ""
    src = img.get("src") or img.get("data-src") or ""
    fname = src.split("?")[0].rstrip("/").split("/")[-1]
    return TSC_LOGO_MARCA.get(fname, "")


def _tsc_table(soup, header_hint: str):
    for t in soup.find_all("table"):
        head = t.get_text(" ", strip=True).upper()[:60]
        if header_hint in head:                                  # TUNE ('PILOTO', 'POS')
            return t
    return None


def tsc_pilotos(url: str) -> list[dict]:
    soup = get(url)
    table = _tsc_table(soup, "NRO") or _tsc_table(soup, "PILOTO")
    pilotos = []
    if not table:
        return pilotos
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        n = tds[0].get_text(strip=True)
        nombre = tds[-1].get_text(" ", strip=True)               # última col = PILOTO|DUPLA
        if not n or not nombre or not re.match(r"\d", n):
            continue
        pilotos.append({"n": n, "nombre": _tsc_name(nombre), "marca": _tsc_marca(tr)})
    return pilotos


def tsc_campeonato(url: str) -> dict | None:
    soup = get(url)
    table = _tsc_table(soup, "POS")
    if not table:
        return None
    tabla, pos = [], 0
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        raw_pos = tds[0].get_text(strip=True)
        if not re.match(r"\d", raw_pos):
            continue
        pos += 1
        nombre_raw = tds[-2].get_text(" ", strip=True)           # PILOTO|DUPLA
        pts = tds[-1].get_text(strip=True).replace(".", ",")
        wins = len(tr.find_all("img", src=re.compile(r"1f3c6")))  # 🏆 emoji svg
        row = {"pos": pos, "n": _tsc_num(nombre_raw),
               "nombre": _tsc_name(re.sub(r"\(.*?\)", "", nombre_raw)), "pts": pts}
        if wins:
            row["wins"] = wins
        tabla.append(row)
    if not tabla:
        return None
    m = re.search(r"Disputadas\s+(\d+)\s+fechas", soup.get_text(" ", strip=True), re.I)
    fechas = f"{m.group(1)} fechas disputadas" if m else "Campeonato 2026"
    return {"fechas": fechas, "tabla": tabla}


def _tsc_name(s: str) -> str:
    """'DIMURO JUAN ESTEBAN' -> 'Dimuro, Juan Esteban' ; respeta duplas con '|'."""
    s = s.strip()
    if "|" in s:
        return " | ".join(_tsc_name(p) for p in s.split("|"))
    s = re.sub(r"\s*🏆?\s*\(\d+\)?\s*$", "", s)          # limpia (3) etc.
    parts = s.split()
    if len(parts) >= 2:
        return titlecase_es(parts[0]) + ", " + titlecase_es(" ".join(parts[1:]))
    return titlecase_es(s)


def _tsc_num(_s: str) -> str:
    return ""  # TSC no publica el nº junto a la tabla de campeonato; se cruza aparte si hace falta


# ---------------------------------------------------------------------------
def build():
    OUT.mkdir(exist_ok=True)
    result = {"updated": datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y %H:%M"),
              "categories": []}
    for cfg in CATS:
        out = {k: cfg[k] for k in ("id", "name", "short", "acc", "source")}
        try:
            if cfg["kind"] == "actc":
                pilotos = actc_pilotos(cfg["slug"])
                marca_por_n = {p["n"]: p["marca"] for p in pilotos}
                out["pilotos"] = pilotos
                out["campeonato"] = actc_campeonato(cfg["slug"], marca_por_n)
            else:
                out["pilotos"] = tsc_pilotos(cfg["pilotos_url"])
                out["campeonato"] = tsc_campeonato(cfg["camp_url"])
            print(f"  ✓ {cfg['id']:6} · {len(out['pilotos'])} pilotos · "
                  f"campeonato: {'sí' if out.get('campeonato') else 'no'}")
        except Exception as e:                                   # una categoría no rompe el resto
            print(f"  ✗ {cfg['id']:6} · ERROR: {e}", file=sys.stderr)
            out.setdefault("pilotos", [])
            out.setdefault("campeonato", None)
            out["error"] = str(e)
        result["categories"].append(out)
        # también un archivo por categoría, por si querés servirlos sueltos
        (OUT / f"{cfg['id']}.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    (OUT / "index.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nListo → {OUT/'index.json'}  ({result['updated']})")


if __name__ == "__main__":
    print("Scrapeando ACTC + TSC…")
    build()
