#!/usr/bin/env python3
"""Holt neue Videos der Kanäle aus kanaele.json und speichert ihre Transkripte.

Ablage:
  transkripte/<datum>_<kanal>_<videoid>.json  je Video (Metadaten + Segmente)
  index.json                                  alle bekannten Videos mit Status
  kanal_ids.json                              Cache Handle -> Channel-ID
  letzter_lauf.json                           Bericht des letzten Laufs

Status je Video: "ok" | "kein_transkript" | "blockiert" | "fehler".
Nicht-"ok"-Videos werden bei späteren Läufen erneut versucht
(höchstens MAX_VERSUCHE Mal und nur bis MAX_ALTER_TAGE nach Veröffentlichung).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASIS = Path(__file__).resolve().parent
TRANSKRIPTE = BASIS / "transkripte"
INDEX = BASIS / "index.json"
KANAL_IDS = BASIS / "kanal_ids.json"
BERICHT = BASIS / "letzter_lauf.json"

BERLIN = ZoneInfo("Europe/Berlin")
SPRACHEN = ["de", "de-DE", "en"]

START_TAGE = int(os.environ.get("START_TAGE", "7"))        # Rückblick beim allerersten Lauf je Kanal
MAX_VIDEOS = int(os.environ.get("MAX_VIDEOS", "40"))       # Transkript-Abrufe pro Lauf
MAX_VERSUCHE = int(os.environ.get("MAX_VERSUCHE", "6"))
MAX_ALTER_TAGE = int(os.environ.get("MAX_ALTER_TAGE", "5"))
PAUSE = float(os.environ.get("PAUSE_SEKUNDEN", "3"))

NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}
UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9",
}
COOKIES = {"CONSENT": "YES+cb", "SOCS": "CAI"}


def lade(pfad: Path, standard):
    if pfad.exists():
        return json.loads(pfad.read_text(encoding="utf-8"))
    return standard


def speichere(pfad: Path, daten) -> None:
    pfad.write_text(json.dumps(daten, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def log(*teile) -> None:
    print(*teile, flush=True)


# ---------------------------------------------------------------- Kanal-IDs

def kanal_id(handle: str, cache: dict) -> str | None:
    if handle in cache:
        return cache[handle]
    cid = None
    try:
        r = requests.get(f"https://www.youtube.com/@{handle}", headers=UA, cookies=COOKIES, timeout=30)
        m = re.search(r'"(?:externalId|channelId)":"(UC[\w-]{22})"', r.text) or re.search(
            r"youtube\.com/channel/(UC[\w-]{22})", r.text
        )
        if m:
            cid = m.group(1)
    except requests.RequestException as e:
        log(f"  Kanalseite @{handle} nicht erreichbar: {e}")
    if not cid:
        try:
            out = subprocess.run(
                ["yt-dlp", "--flat-playlist", "--playlist-items", "1", "--print", "channel_id",
                 f"https://www.youtube.com/@{handle}/videos"],
                capture_output=True, text=True, timeout=120,
            )
            m = re.search(r"UC[\w-]{22}", out.stdout)
            cid = m.group(0) if m else None
        except Exception as e:  # noqa: BLE001
            log(f"  yt-dlp für @{handle} fehlgeschlagen: {e}")
    if cid:
        cache[handle] = cid
    return cid


# ---------------------------------------------------------------- Videolisten

def rss(playlist_id: str) -> list[dict] | None:
    """Videos einer Uploads-Playlist per RSS. None = Feed nicht abrufbar."""
    url = f"https://www.youtube.com/feeds/videos.xml?playlist_id={playlist_id}"
    for versuch in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=30)
        except requests.RequestException:
            r = None
        if r is not None and r.status_code == 200:
            break
        if r is not None and r.status_code == 404:
            return []  # Playlist gibt es nicht (z. B. keine Livestreams)
        time.sleep(3 * (versuch + 1))
    else:
        return None
    wurzel = ET.fromstring(r.content)
    videos = []
    for e in wurzel.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", namespaces=NS)
        veroeff = e.findtext("a:published", namespaces=NS)
        if not vid or not veroeff:
            continue
        dt = datetime.fromisoformat(veroeff.replace("Z", "+00:00"))
        videos.append({
            "id": vid,
            "titel": e.findtext("a:title", default="", namespaces=NS),
            "veroeffentlicht": dt.astimezone(timezone.utc).isoformat(),
            "datum": dt.astimezone(BERLIN).date().isoformat(),
            "beschreibung": e.findtext("media:group/media:description", default="", namespaces=NS),
        })
    return videos


def ytdlp_liste(handle: str, reiter: str) -> list[dict]:
    """Reserve, falls RSS ausfällt: flache Liste mit ungefährem Datum."""
    try:
        out = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--playlist-items", "1:15", "-J",
             "--extractor-args", "youtubetab:approximate_date",
             f"https://www.youtube.com/@{handle}/{reiter}"],
            capture_output=True, text=True, timeout=180,
        )
        daten = json.loads(out.stdout or "{}")
    except Exception as e:  # noqa: BLE001
        log(f"  yt-dlp-Liste @{handle}/{reiter} fehlgeschlagen: {e}")
        return []
    videos = []
    for v in daten.get("entries") or []:
        ud = v.get("upload_date") or (
            datetime.fromtimestamp(v["timestamp"], BERLIN).strftime("%Y%m%d") if v.get("timestamp") else None
        )
        if not v.get("id") or not ud:
            continue
        d = f"{ud[:4]}-{ud[4:6]}-{ud[6:]}"
        videos.append({
            "id": v["id"], "titel": v.get("title") or "", "veroeffentlicht": d,
            "datum": d, "beschreibung": v.get("description") or "", "datum_ungefaehr": True,
        })
    return videos


def videos_des_kanals(handle: str, cid: str | None) -> tuple[list[dict], str]:
    if cid:
        ergebnis, ok = [], True
        for praefix in ("UULF", "UULV"):  # UULF = normale Videos ohne Shorts, UULV = Livestreams
            liste = rss(praefix + cid[2:])
            if liste is None:
                ok = False
                break
            ergebnis += liste
        if ok:
            return ergebnis, "rss"
    return ytdlp_liste(handle, "videos") + ytdlp_liste(handle, "streams"), "yt-dlp"


# ---------------------------------------------------------------- Transkripte

class Blockiert(Exception):
    pass


def transkript(video_id: str) -> tuple[str, dict | None, str]:
    """-> (status, daten, meldung)"""
    from youtube_transcript_api import YouTubeTranscriptApi

    api = YouTubeTranscriptApi()
    try:
        liste = api.list(video_id)
        verfuegbar = list(liste)
        try:
            t = liste.find_manually_created_transcript(SPRACHEN)
        except Exception:  # noqa: BLE001
            t = liste.find_transcript(SPRACHEN)
        geholt = t.fetch()
    except Exception as e:  # noqa: BLE001
        name = type(e).__name__
        if name in ("RequestBlocked", "IpBlocked", "TooManyRequests"):
            raise Blockiert(name) from e
        if name in ("TranscriptsDisabled", "NoTranscriptFound", "NoTranscriptAvailable"):
            return "kein_transkript", None, name
        return "fehler", None, f"{name}: {str(e).splitlines()[0][:200] if str(e) else ''}"
    segmente = [{"start": round(s.start, 2), "text": s.text} for s in geholt.snippets]
    return "ok", {
        "sprache": geholt.language_code,
        "automatisch": geholt.is_generated,
        "verfuegbar": [x.language_code + ("*" if x.is_generated else "") for x in verfuegbar],
        "segmente": segmente,
    }, ""


# ---------------------------------------------------------------- Ablauf

def main() -> int:
    kanaele = json.loads((BASIS / "kanaele.json").read_text(encoding="utf-8"))
    cache = lade(KANAL_IDS, {})
    index: dict = lade(INDEX, {})
    TRANSKRIPTE.mkdir(exist_ok=True)
    jetzt = datetime.now(timezone.utc)
    heute = jetzt.astimezone(BERLIN).date()

    bericht = {"lauf": jetzt.isoformat(timespec="seconds"), "kanaele": {}, "neu": [],
               "ok": 0, "ohne_transkript": 0, "fehler": 0, "blockiert": False, "meldungen": []}

    # 1. Kandidaten sammeln
    kandidaten = []
    for k in kanaele:
        h = k["handle"]
        cid = kanal_id(h, cache)
        videos, weg = videos_des_kanals(h, cid)
        bekannte = [v for v in index.values() if v["kanal"] == h]
        grenze = (max(v["datum"] for v in bekannte) if bekannte
                  else (heute - timedelta(days=START_TAGE)).isoformat())
        neu = 0
        for v in videos:
            alt = index.get(v["id"])
            if alt is None:
                if v["datum"] < grenze:
                    continue
                index[v["id"]] = {
                    "id": v["id"], "kanal": h, "quelle": k["quelle"], "titel": v["titel"],
                    "datum": v["datum"], "veroeffentlicht": v["veroeffentlicht"],
                    "beschreibung": v["beschreibung"], "status": "offen", "versuche": 0,
                    "datei": None, "erstmals_gesehen": jetzt.isoformat(timespec="seconds"),
                }
                if v.get("datum_ungefaehr"):
                    index[v["id"]]["datum_ungefaehr"] = True
                neu += 1
            elif v["titel"] and alt.get("datum_ungefaehr") and not v.get("datum_ungefaehr"):
                alt.update(datum=v["datum"], veroeffentlicht=v["veroeffentlicht"])
                alt.pop("datum_ungefaehr", None)
        bericht["kanaele"][h] = {"channel_id": cid, "weg": weg, "gefunden": len(videos), "neu": neu}
        log(f"@{h}: {len(videos)} in der Liste, {neu} neu (Weg: {weg}, ID: {cid})")

    for v in index.values():
        if v["status"] == "ok" or v["versuche"] >= MAX_VERSUCHE:
            continue
        if (heute - date.fromisoformat(v["datum"])).days > MAX_ALTER_TAGE and v["versuche"] > 0:
            continue
        kandidaten.append(v)
    kandidaten.sort(key=lambda v: v["veroeffentlicht"])
    kandidaten = kandidaten[:MAX_VIDEOS]
    log(f"{len(kandidaten)} Transkripte abzurufen")

    # 2. Transkripte holen
    for i, v in enumerate(kandidaten):
        if i:
            time.sleep(PAUSE)
        v["versuche"] += 1
        v["letzter_versuch"] = jetzt.isoformat(timespec="seconds")
        try:
            status, daten, meldung = transkript(v["id"])
        except Blockiert as e:
            v["status"] = "blockiert"
            bericht["blockiert"] = True
            bericht["meldungen"].append(f"{v['id']}: von YouTube blockiert ({e})")
            log(f"  {v['id']}: BLOCKIERT ({e}) – Lauf wird beendet")
            break
        v["status"] = status
        if meldung:
            v["meldung"] = meldung
        else:
            v.pop("meldung", None)
        if status == "ok":
            datei = f"transkripte/{v['datum']}_{v['kanal']}_{v['id']}.json"
            inhalt = {k: v[k] for k in ("id", "kanal", "quelle", "titel", "datum", "veroeffentlicht", "beschreibung")}
            inhalt["link"] = f"https://www.youtube.com/watch?v={v['id']}"
            inhalt.update(daten)
            speichere(BASIS / datei, inhalt)
            v["datei"] = datei
            v["segmente"] = len(daten["segmente"])
            bericht["ok"] += 1
            bericht["neu"].append(datei)
        elif status == "kein_transkript":
            bericht["ohne_transkript"] += 1
        else:
            bericht["fehler"] += 1
            bericht["meldungen"].append(f"{v['id']}: {meldung}")
        log(f"  {v['datum']} @{v['kanal']} {v['id']}: {status} {meldung}")

    # 3. Speichern
    geordnet = dict(sorted(index.items(), key=lambda kv: kv[1]["veroeffentlicht"], reverse=True))
    speichere(INDEX, geordnet)
    speichere(KANAL_IDS, cache)
    speichere(BERICHT, bericht)
    log(json.dumps({k: bericht[k] for k in ("ok", "ohne_transkript", "fehler", "blockiert")}))

    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(f"## Transkripte {heute}\n\n")
            f.write(f"- neu mit Transkript: {bericht['ok']}\n- ohne Transkript: {bericht['ohne_transkript']}\n")
            f.write(f"- Fehler: {bericht['fehler']}\n- blockiert: {'ja' if bericht['blockiert'] else 'nein'}\n")
            for m in bericht["meldungen"]:
                f.write(f"  - {m}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
