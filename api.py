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

# --- Carga de variables de entorno ---
env_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(env_path, override=True)

# --- Configuración global ---
API_KEY = os.getenv("RIOT_API_KEY")  # Riot API Key
if not API_KEY:
    raise RuntimeError("❌ RIOT_API_KEY no definida en el entorno")
REGIONAL = "americas"           # Región para Match-V5 y Account-V1
DB_FILE = "lol_trackedb.db"     # Archivo SQLite
DAILY_DEF_LIMIT = 5               # Límite derrotas diarias
POINTS_PER_VICTORY_BASE = 5       # Puntos base por victoria
ALLOWED_QUEUES = {400, 420}       # Modos permitidos (Normal Draft, Ranked Solo/Duo)

# --- Inicialización de FastAPI ---
app = FastAPI(
    title="LoL Tracker API",
    version="1.5.1",
    description="Procesa partidas con paginación, streaks y bankeo manual"
)

# --- Modelo de entrada ---
class RiotID(BaseModel):
    game_name: str
    tag_line: str

# --- Conexión a la base de datos ---
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

# --- Helpers para llamadas a Riot API ---
def riot_request(path: str) -> dict:
    """
    Realiza una petición GET a Riot con el header X-Riot-Token.
    """
    url = f"https://{REGIONAL}.api.riotgames.com{path}"
    headers = {"X-Riot-Token": API_KEY}
    resp = requests.get(url, headers=headers)
    if resp.status_code in (401, 403):
        raise HTTPException(401, "API Key no autorizada o caducada")
    if resp.status_code == 429:
        raise HTTPException(429, "Rate limit alcanzado")
    resp.raise_for_status()
    return resp.json()

# --- Obtención de PUUID ---
def get_puuid(game_name: str, tag_line: str) -> str:
    data = riot_request(f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}")
    puuid = data.get("puuid")
    if not puuid:
        raise HTTPException(500, "No se obtuvo puuid")
    return puuid

# --- Paginación para IDs de partidas ---
def fetch_all_match_ids(puuid: str, page_size: int = 20) -> list:
    """
    Obtiene todas las match IDs de Riot, paginando hasta que batch < page_size.
    """
    ids = []
    start = 0
    while True:
        batch = riot_request(
            f"/lol/match/v5/matches/by-puuid/{puuid}/ids?start={start}&count={page_size}"
        )
        if not batch:
            break
        ids.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size
    return ids

# --- Procesamiento de una partida ---
def process_match(match_id: str, puuid: str) -> dict:
    info = riot_request(f"/lol/match/v5/matches/{match_id}")["info"]
    # Filtrar remakes y partidas cortas
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

# --- Cálculo de puntos dinámicos ---
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

# --- Endpoint principal: /procesar-partidas/ ---
@app.post("/procesar-partidas/")
def procesar_partidas(id: RiotID):
    conn, c = get_db()
    puuid = get_puuid(id.game_name, id.tag_line)
    # Calcular cutoff a medianoche UTC del servidor
    cutoff = int(
        datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
    )
    # Conteo inicial de defeats / victories
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
    # Obtener todas las IDs con paginación
    all_ids = fetch_all_match_ids(puuid)
    # Procesar cada ID
    for mid in all_ids:
        c.execute("SELECT 1 FROM matches WHERE match_id=?", (mid,))
        if c.fetchone():
            continue
        rec = process_match(mid, puuid)
        if not rec or rec["end_timestamp"] < cutoff:
            continue
        # Guardar partida y eventos
        c.execute(
            "INSERT OR IGNORE INTO matches(match_id,queue_id,start_timestamp,end_timestamp) "
            "VALUES(?,?,?,?)",
            (rec['match_id'], rec['queue_id'], rec['start_timestamp'], rec['end_timestamp'])
        )
        for evt in rec['events']:
            c.execute(
                "INSERT INTO match_events(match_id,event) VALUES(?,?)", 
                (rec['match_id'], evt)
            )
        conn.commit()
        processed.append(rec)
        if 'derrota' in rec['events']:
            defeats += 1
            if defeats >= DAILY_DEF_LIMIT:
                # Auto-bank si es necesario
                date_str = datetime.now().strftime('%Y-%m-%d')
                # (lógica de mark_streak_banked idéntica)
                break
        else:
            victories += 1
    # Calcular puntos dinámicos finales
    dyn_points = calculate_dynamic_points(conn, c, cutoff)
    return {"derrotas": defeats, "victorias": victories, "puntos": dyn_points, "nuevas": processed}

# --- Job de medianoche: bankear 25% ---
def daily_bank_job():
    conn, c = get_db()
    date_str = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    c.execute("SELECT pending_streak,has_banked FROM streak_bank WHERE date=?", (date_str,))
    row = c.fetchone()
    if row and row[1] == 0:
        pending, _ = row
        bonus = (pending * POINTS_PER_VICTORY_BASE) * 0.25
        c.execute("UPDATE streak_bank SET has_banked=1 WHERE date=?", (date_str,))
        conn.commit()
        print(f"[DAILY BANK] {bonus} points banked for {date_str}")

def schedule_jobs():
    schedule.every().day.at("00:00").do(daily_bank_job)
    while True:
        schedule.run_pending()
        time.sleep(60)

threading.Thread(target=schedule_jobs, daemon=True).start()
