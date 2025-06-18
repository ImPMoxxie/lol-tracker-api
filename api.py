import os
import threading  # Para hilos de scheduler
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
import sqlite3
import json
import time
from datetime import datetime
from pydantic import BaseModel
from riotwatcher import LolWatcher
import schedule  # Para tareas programadas
import requests

# --- Carga de variables de entorno ---
env_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(env_path, override=True)

# --- Configuración global ---
API_KEY = os.getenv("RIOT_API_KEY")  # Riot API Key
if not API_KEY:
    raise RuntimeError("❌ RIOT_API_KEY no definida en el entorno")
REGIONAL = "americas"           # Región para Account y Match API
DB_FILE = "lol_trackedb.db"     # Archivo SQLite
DAILY_DEF_LIMIT = 5               # Límite derrotas diarias
POINTS_PER_VICTORY_BASE = 5       # Puntos base por victoria
ALLOWED_QUEUES = {400, 420}       # Modos de juego permitidos

# --- Inicialización de FastAPI y RiotWatcher ---
app = FastAPI(
    title="LoL Tracker API",
    version="1.6.1",
    description="Procesa partidas con cache local y RiotWatcher"
)
watcher = LolWatcher(API_KEY)

# --- Modelo de entrada ---
class RiotID(BaseModel):
    game_name: str
    tag_line: str

# === Funciones de base de datos ===
def get_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    c.executescript("""
CREATE TABLE IF NOT EXISTS matches (
  match_id TEXT PRIMARY KEY,
  queue_id INTEGER,
  start_timestamp INTEGER,
  end_timestamp INTEGER
);
CREATE TABLE IF NOT EXISTS match_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id TEXT NOT NULL,
  event TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS streak_bank (
  date TEXT PRIMARY KEY,
  pending_streak INTEGER,
  has_banked INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS match_cache (
  match_id TEXT PRIMARY KEY,
  info_json TEXT NOT NULL
);
""")
    conn.commit()
    return conn, c

# === Helpers para Riot API ===
def riot_request(path: str) -> dict:
    url = f"https://{REGIONAL}.api.riotgames.com{path}"
    headers = {"X-Riot-Token": API_KEY}
    resp = requests.get(url, headers=headers)
    if resp.status_code in (401, 403):
        raise HTTPException(401, "API Key no autorizada o caducada")
    if resp.status_code == 429:
        retry = int(resp.headers.get("Retry-After", 1))
        time.sleep(retry)
        resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    time.sleep(0.5)
    return data

# Obtiene puuid
def get_puuid(game_name: str, tag_line: str) -> str:
    data = riot_request(f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}")
    puuid = data.get("puuid")
    if not puuid:
        raise HTTPException(500, "No se obtuvo puuid")
    return puuid

# Recupera todos los IDs en páginas de tamaño fijo
# Luego se aplica el filtro de medianoche en el endpoint
def fetch_all_match_ids(puuid: str, page_size: int = 5) -> list:
    all_ids = []
    start = 0
    while True:
        batch = riot_request(
            f"/lol/match/v5/matches/by-puuid/{puuid}/ids?start={start}&count={page_size}"
        )
        if not batch:
            break
        all_ids.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size
    return all_ids

# Procesa partida usando cache
def process_match(match_id: str, puuid: str, conn, c) -> dict:
    c.execute("SELECT info_json FROM match_cache WHERE match_id=?", (match_id,))
    row = c.fetchone()
    if row:
        info = json.loads(row[0])
    else:
        data = watcher.match.by_id(REGIONAL, match_id)
        info = data.get("info", {})
        c.execute(
            "INSERT INTO match_cache(match_id,info_json) VALUES(?,?)",
            (match_id, json.dumps(info))
        )
        conn.commit()
    # Filtrar remake o partidas <5min
    if info.get("gameEndedInEarlySurrender") or info.get("gameDuration", 0) < 300:
        return None
    queue_id = info.get("queueId", 0)
    if queue_id not in ALLOWED_QUEUES:
        return None
    me = next((p for p in info.get("participants", []) if p.get("puuid") == puuid), None)
    if not me:
        return None
    return {
        "match_id": match_id,
        "queue_id": queue_id,
        "start_timestamp": info.get("gameStartTimestamp"),
        "end_timestamp": info.get("gameEndTimestamp"),
        "events": ["victoria" if me.get("win") else "derrota"]
    }

# === BLOQUE DE GESTIÓN DE STREAKS (NO TOCAR) ===
def update_streak(conn, c, event_timestamp, is_victory):
    date_str = datetime.fromtimestamp(event_timestamp/1000).strftime('%Y-%m-%d')
    c.execute("SELECT pending_streak, has_banked FROM streak_bank WHERE date=?", (date_str,))
    row = c.fetchone()
    if not row:
        c.execute("INSERT INTO streak_bank(date,pending_streak,has_banked) VALUES(?,?,0)", (date_str, 0))
        conn.commit()
        pending, banked = 0, 0
    else:
        pending, banked = row
    if banked:
        return
    pending = pending + 1 if is_victory else 0
    c.execute("UPDATE streak_bank SET pending_streak=? WHERE date=?", (pending, date_str))
    conn.commit()

def mark_streak_banked(conn, c, date_str):
    c.execute("UPDATE streak_bank SET has_banked=1 WHERE date=?", (date_str,))
    conn.commit()

# Calcula puntos dinámicos
# Recorre eventos de hoy en orden, rompe nada si rec es None skip
def calculate_dynamic_points(conn, c, cutoff):
    rows = c.execute(
        "SELECT m.end_timestamp, me.event FROM match_events me "
        "JOIN matches m ON me.match_id=m.match_id "
        "WHERE m.end_timestamp>=? "
        "ORDER BY m.end_timestamp", (cutoff,)
    ).fetchall()
    points, streak, defeats = 0, 0, 0
    p_per_victory = POINTS_PER_VICTORY_BASE
    for ts, event in rows:
        if event == "victoria":
            streak += 1
        elif event == "derrota":
            gained = streak * p_per_victory
            points += gained
            p_per_victory += gained
            streak = 0
            defeats += 1
            if defeats >= DAILY_DEF_LIMIT:
                break
    return points

# Marca streak bancado al fin del día
def daily_bank_job():
    conn, c = get_db()
    date_str = datetime.now().strftime('%Y-%m-%d')
    mark_streak_banked(conn, c, date_str)

# --- Endpoint principal ---
@app.post("/procesar-partidas/")
def procesar_partidas(id: RiotID):
    conn, c = get_db()
    puuid = get_puuid(id.game_name, id.tag_line)
    cutoff = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)

    # Conteo inicial de derrotas y victorias de hoy
    c.execute(
        "SELECT COUNT(*) FROM match_events me JOIN matches m ON me.match_id=m.match_id "
        "WHERE me.event='derrota' AND m.end_timestamp>=?", (cutoff,)
    )
    defeats = c.fetchone()[0]
    c.execute(
        "SELECT COUNT(*) FROM match_events me JOIN matches m ON me.match_id=m.match_id "
        "WHERE me.event='victoria' AND m.end_timestamp>=?", (cutoff,)
    )
    victories = c.fetchone()[0]

    processed = []
    # Obtener IDs paginados y filtrar por tiempo
    for mid in fetch_all_match_ids(puuid):
        rec = process_match(mid, puuid, conn, c)
        # Saltar partidas inválidas o None antes de indexar
        if not rec:
            continue
        if rec["end_timestamp"] < cutoff:
            continue
        # Guardar partida y actualizar streak
        c.execute(
            "INSERT OR IGNORE INTO matches(match_id,queue_id,start_timestamp,end_timestamp) VALUES(?,?,?,?)",
            (rec['match_id'], rec['queue_id'], rec['start_timestamp'], rec['end_timestamp'])
        )
        for evt in rec['events']:
            c.execute("INSERT INTO match_events(match_id,event) VALUES(?,?)", (rec['match_id'], evt))
            update_streak(conn, c, rec['end_timestamp'], evt=='victoria')
        conn.commit()
        processed.append(rec)
        # Actualizar contadores locales
        if 'derrota' in rec['events']:
            defeats += 1
            if defeats >= DAILY_DEF_LIMIT:
                break
        else:
            victories += 1

    dyn_points = calculate_dynamic_points(conn, c, cutoff)
    return {"derrotas": defeats, "victorias": victories, "puntos": dyn_points, "nuevas": processed}

# --- Iniciar scheduler de bancar streak ---
threading.Thread(target=lambda: (schedule.every().day.at("00:00").do(daily_bank_job), schedule.run_pending()), daemon=True).start()
