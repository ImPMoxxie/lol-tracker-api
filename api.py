import os
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
import sqlite3
import requests
import threading
import schedule
import time
from datetime import datetime, timedelta
from pydantic import BaseModel

# --- Carga de variables de entorno desde .env ---
env_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(env_path, override=True)

# --- Configuración global ---
API_KEY = os.getenv("RIOT_API_KEY")  # Clave de API de Riot Games
if not API_KEY:
    raise RuntimeError("❌ RIOT_API_KEY no definida en el entorno")
PLATFORM = "la2"                 # Plataforma para Account–V1
REGIONAL = "americas"            # Región para Match–V5
DB_FILE = "lol_trackedb.db"      # Archivo SQLite
DAILY_DEF_LIMIT = 5               # Límite de derrotas por día
POINTS_PER_VICTORY_BASE = 5       # Puntos base por victoria
ALLOWED_QUEUES = {400, 420}       # Sólo Normal Draft (400) y Ranked Solo/Duo (420)

# --- Inicialización de FastAPI ---
app = FastAPI(
    title="LoL Tracker API",
    version="1.4.1",
    description="Procesa partidas con rachas dinámicas y permite 'bankear' streaks manual o automáticamente"
)

# --- Esquema de entrada ---
class RiotID(BaseModel):
    game_name: str  # Nombre del invocador
    tag_line: str   # Sufijo de región (ej. LAS)

# --- Helpers de BD y tablas ---
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
""")
    conn.commit()
    return conn, c

# --- Riot API funcs ---
def get_puuid(game_name: str, tag_line: str) -> str:
    """
    Obtiene el PUUID usando Account–V1. Utiliza header X-Riot-Token en lugar de parámetro.
    """
    url = (
        f"https://{PLATFORM}.api.riotgames.com"
        f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
    )
    headers = {"X-Riot-Token": API_KEY}
    resp = requests.get(url, headers=headers)
    if resp.status_code in (401, 403):
        raise HTTPException(status_code=401, detail="API Key no autorizada o caducada")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Invocador no encontrado en PLATFORM")
    resp.raise_for_status()
    data = resp.json()
    puuid = data.get("puuid")
    if not puuid:
        raise HTTPException(status_code=500, detail="No se obtuvo puuid")
    return puuid

# --- Fetch recent matches IDs ---
def fetch_recent_matches(puuid: str, count: int = 5) -> list:
    url = (
        f"https://{REGIONAL}.api.riotgames.com"
        f"/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count={count}"
    )
    headers = {"X-Riot-Token": API_KEY}
    resp = requests.get(url, headers=headers)
    if resp.status_code == 429:
        raise HTTPException(status_code=429, detail="Rate limit alcanzado")
    resp.raise_for_status()
    return resp.json()

# --- Process a single match ---
def process_match(match_id: str, puuid: str) -> dict:
    url = f"https://{REGIONAL}.api.riotgames.com/lol/match/v5/matches/{match_id}"
    headers = {"X-Riot-Token": API_KEY}
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    info = resp.json().get("info", {})

    # Omitir remakes / early surrenders y partidas muy cortas
    if info.get("gameEndedInEarlySurrender", False) or info.get("gameDuration", float('inf')) < 300:
        return None

    queue_id = info.get("queueId", 0)
    if queue_id not in ALLOWED_QUEUES:
        return None

    participants = info.get("participants", [])
    me = next((p for p in participants if p.get("puuid") == puuid), None)
    if not me:
        return None

    events = ["victoria" if me.get("win") else "derrota"]
    start_ts = info.get("gameStartTimestamp", 0)
    end_ts = info.get("gameEndTimestamp", 0)

    return {
        "match_id": match_id,
        "queue_id": queue_id,
        "start_timestamp": start_ts,
        "end_timestamp": end_ts,
        "events": events
    }

# --- Gestión de streak manual ---
def update_streak(date_str: str, streak: int, conn, c):
    c.execute(
        "INSERT INTO streak_bank(date,pending_streak,has_banked) VALUES(?,?,0) "
        "ON CONFLICT(date) DO UPDATE SET pending_streak=excluded.pending_streak",
        (date_str, streak)
    )
    conn.commit()

def mark_streak_banked(date_str: str, conn, c, full_bonus: int) -> int:
    c.execute("SELECT pending_streak,has_banked FROM streak_bank WHERE date=?", (date_str,))
    row = c.fetchone()
    if not row or row[1] == 1:
        return 0
    c.execute("UPDATE streak_bank SET has_banked=1 WHERE date=?", (date_str,))
    conn.commit()
    return full_bonus

# --- Cálculo de puntos dinámicos (victorias + rachas) ---
def calculate_dynamic_points(conn, c, cutoff):
    c.execute(
        "SELECT m.end_timestamp, me.event FROM match_events me "
        "JOIN matches m ON me.match_id=m.match_id "
        "WHERE m.end_timestamp>=? ORDER BY m.end_timestamp",
        (cutoff,)
    )
    rows = c.fetchall()
    points = 0
    streak = 0
    defeats = 0
    base = POINTS_PER_VICTORY_BASE
    for ts, evt in rows:
        if evt == 'victoria':
            points += POINTS_PER_VICTORY_BASE
            streak += 1
        else:
            bonus = streak * base
            points += bonus
            base += bonus
            streak = 0
            defeats += 1
            if defeats >= DAILY_DEF_LIMIT:
                break
    date_str = datetime.now().strftime('%Y-%m-%d')
    update_streak(date_str, streak, conn, c)
    return points

# --- Endpoint principal ---
@app.post("/procesar-partidas/")
def procesar_partidas(id: RiotID):
    conn, c = get_db()
    puuid = get_puuid(id.game_name, id.tag_line)
    cutoff = int(datetime.now().replace(hour=0,minute=0,second=0,microsecond=0).timestamp()*1000)

    c.execute(
        "SELECT COUNT(*) FROM match_events me JOIN matches m ON me.match_id=m.match_id "
        "WHERE me.event='derrota' AND m.end_timestamp>=?",
        (cutoff,)
    )
    defeats = c.fetchone()[0]
    c.execute(
        "SELECT COUNT(*) FROM match_events me JOIN matches m ON me.match_id=m.match_id "
        "WHERE me.event='victoria' AND m.end_timestamp>=?",
        (cutoff,)
    )
    victories = c.fetchone()[0]

    recent = fetch_recent_matches(puuid)
    done = {r[0] for r in c.execute("SELECT match_id FROM matches").fetchall()}
    processed = []
    for mid in recent:
        if mid in done: continue
        rec = process_match(mid, puuid)
        if not rec or rec['end_timestamp']<cutoff: continue
        c.execute(
            "INSERT OR IGNORE INTO matches(match_id,queue_id,start_timestamp,end_timestamp) VALUES(?,?,?,?)",
            (rec['match_id'],rec['queue_id'],rec['start_timestamp'],rec['end_timestamp'])
        )
        for evt in rec['events']:
            c.execute(
                "INSERT INTO match_events(match_id,event) VALUES(?,?)",
                (rec['match_id'],evt)
            )
        conn.commit()
        processed.append(rec)
        if 'derrota' in rec['events']:
            defeats+=1
            if defeats>=DAILY_DEF_LIMIT:
                date_str = datetime.now().strftime('%Y-%m-%d')
                c.execute("SELECT pending_streak FROM streak_bank WHERE date=?",(date_str,))
                pending = c.fetchone()[0] if c.fetchone() else 0
                full_bonus = pending * POINTS_PER_VICTORY_BASE
                mark_streak_banked(date_str, conn, c, full_bonus)
                break
        else:
            victories+=1

    dyn_points = calculate_dynamic_points(conn, c, cutoff)
    return {"derrotas":defeats, "victorias":victories, "puntos":dyn_points, "nuevas":processed}

# --- Job de medianoche: bankear 25% ---
def daily_bank_job():
    conn, c = get_db()
    date_str = (datetime.now()-timedelta(days=1)).strftime('%Y-%m-%d')
    c.execute("SELECT pending_streak,has_banked FROM streak_bank WHERE date=?",(date_str,))
    row = c.fetchone()
    if row and row[1]==0:
        pending, _ = row
        bonus = (pending * POINTS_PER_VICTORY_BASE) * 0.25
        c.execute("UPDATE streak_bank SET has_banked=1 WHERE date=?",(date_str,))
        conn.commit()
        print(f"[DAILY BANK] {bonus} points banked for {date_str}")


def schedule_jobs():
    schedule.every().day.at("00:00").do(daily_bank_job)
    while True:
        schedule.run_pending()
        time.sleep(60)

threading.Thread(target=schedule_jobs, daemon=True).start()
