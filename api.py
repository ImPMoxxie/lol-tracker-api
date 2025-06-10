import os
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
import sqlite3
import requests
from datetime import datetime, timedelta
from pydantic import BaseModel

# Cargar variables de entorno desde .env
load_dotenv()

# Configuración
API_KEY = os.getenv("RIOT_API_KEY")
if not API_KEY:
    raise RuntimeError("❌ RIOT_API_KEY no definida en el entorno")
REGIONAL = "americas"                     # Región para endpoints Match–V5 y Account–V1
DB_FILE = "lol_trackedb.db"               # Archivo SQLite
DAILY_DEF_LIMIT = 5                         # Límite de DERROTAS por día
POINTS_PER_VICTORY = 5                      # Puntos otorgados por cada victoria

# Inicializar la aplicación FastAPI
app = FastAPI(
    title="LoL Tracker API",
    version="1.0.0",
    description="Procesa partidas de League of Legends con sistema de puntos y plan de ejercicios"
)

# Modelo Pydantic para validar entrada
class RiotID(BaseModel):
    game_name: str
    tag_line: str

# —————————————
# Helpers de SQLite
# —————————————

def get_db():
    """
    Abre la conexión a SQLite y crea las tablas si no existen.
    Devuelve (conn, cursor).
    """
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    # Crear tablas
    c.executescript("""
CREATE TABLE IF NOT EXISTS matches (
  match_id TEXT PRIMARY KEY,
  start_timestamp INTEGER,
  end_timestamp INTEGER,
  start_time TEXT,
  end_time TEXT
);
CREATE TABLE IF NOT EXISTS match_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id TEXT NOT NULL,
  event TEXT NOT NULL,
  FOREIGN KEY(match_id) REFERENCES matches(match_id)
);
""")
    conn.commit()
    return conn, c

# —————————————
# Funciones de la Riot API
# —————————————

def get_puuid(game_name: str, tag_line: str) -> str:
    """
    Obtiene el PUUID usando Account–V1 (/by-riot-id/).
    """
    url = (
        f"https://{REGIONAL}.api.riotgames.com"
        f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}?api_key={API_KEY}"
    )
    resp = requests.get(url)
    if resp.status_code in (401, 403):
        raise HTTPException(status_code=401, detail="API Key no autorizada o caducada")
    resp.raise_for_status()
    data = resp.json()
    puuid = data.get("puuid")
    if not puuid:
        raise HTTPException(status_code=500, detail="Respuesta de Account–V1 sin campo puuid")
    return puuid


def fetch_recent_matches(puuid: str, count: int = 5) -> list:
    """
    Obtiene los últimos `count` IDs de partidas (Match–V5) para un PUUID.
    """
    url = (
        f"https://{REGIONAL}.api.riotgames.com"
        f"/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count={count}"
    )
    resp = requests.get(url, headers={"X-Riot-Token": API_KEY})
    if resp.status_code == 429:
        raise HTTPException(status_code=429, detail="Rate limit alcanzado")
    resp.raise_for_status()
    return resp.json()


def process_match(match_id: str, puuid: str) -> dict:
    """
    Procesa una partida y retorna un dict con:
      - match_id
      - start/end timestamps y cadenas legibles
      - lista de eventos: 'victoria' | 'derrota' | 'teemo'
    """
    url = f"https://{REGIONAL}.api.riotgames.com/lol/match/v5/matches/{match_id}"
    resp = requests.get(url, headers={"X-Riot-Token": API_KEY})
    resp.raise_for_status()
    info = resp.json().get("info", {})

    # Encuentra tu participación
    participant = next((p for p in info.get("participants", []) if p.get("puuid") == puuid), None)
    if not participant:
        return None

    events = ["victoria" if participant.get("win", False) else "derrota"]
    # Agregar 'teemo' si aparece
    if any(p.get("championName", "").lower() == "teemo" for p in info.get("participants", [])):
        events.append("teemo")

    # Timestamps
    start_ts = info.get("gameStartTimestamp", 0)
    end_ts   = info.get("gameEndTimestamp", 0)
    start_time = datetime.fromtimestamp(start_ts/1000).strftime("%Y-%m-%d %H:%M:%S")
    end_time   = datetime.fromtimestamp(end_ts/1000).strftime("%Y-%m-%d %H:%M:%S")

    return {
        "match_id": match_id,
        "start_timestamp": start_ts,
        "end_timestamp": end_ts,
        "start_time": start_time,
        "end_time": end_time,
        "events": events
    }

# —————————————
# Funciones de base de datos
# —————————————

def get_done_ids(c) -> set:
    c.execute("SELECT match_id FROM matches")
    return {row[0] for row in c.fetchall()}


def save_match(conn, c, record: dict):
    if not record:
        return
    c.execute(
        "INSERT OR IGNORE INTO matches(match_id, start_timestamp, end_timestamp, start_time, end_time) VALUES(?,?,?,?,?)",
        (record["match_id"], record["start_timestamp"], record["end_timestamp"], record["start_time"], record["end_time"])
    )
    for evt in record["events"]:
        c.execute(
            "INSERT INTO match_events(match_id, event) VALUES(?,?)",
            (record["match_id"], evt)
        )
    conn.commit()

# —————————————
# Definición de ejercicios base
# —————————————
FULL_WORKOUT = [
    ("Sentadillas", 40),
    ("Zancadas (lunges)", 20),
    ("Flexiones convencionales", 20),
    ("Flexiones palmas juntas", 10),
    ("Curl isométrico con toalla", 15),
    ("Superman lumbares", 15),
    ("Plancha frontal", 60),
    ("Crunches", 30),
    ("Jumping Jacks", 30),
    ("Saltos de sentadilla", 20)
]

# —————————————
# Cálculo de plan con puntos de victoria
# —————————————

def calculate_plan(defeats: int, victories: int) -> dict:
    points = victories * POINTS_PER_VICTORY
    base = [(name, reps * defeats) for name, reps in FULL_WORKOUT]
    final = []
    for name, reps in base:
        if points >= reps:
            points -= reps
            continue
        if points > 0:
            final.append({"nombre": name, "reps": reps - points})
            points = 0
        else:
            final.append({"nombre": name, "reps": reps})
    return {"puntos_restantes": points, "ejercicios": final}

# —————————————
# Crear estructura de respuesta JSON
# —————————————

def create_response(defeats: int, victories: int, processed: list) -> dict:
    plan = calculate_plan(defeats, victories)
    return {
        "derrotas_hoy": defeats,
        "victorias_hoy": victories,
        "puntos_restantes": plan["puntos_restantes"],
        "plan_ejercicio": plan["ejercicios"],
        "nuevas_partidas": processed
    }

# —————————————
# Endpoint principal
# —————————————

@app.post("/procesar-partidas/")
def procesar_partidas(id: RiotID):
    conn, c = get_db()
    puuid = get_puuid(id.game_name, id.tag_line)
    now = datetime.now()
    cutoff = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)

    # Conteo de derrotas y victorias de hoy
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

    # Si límite de derrotas alcanzado
    if defeats >= DAILY_DEF_LIMIT:
        return create_response(defeats, victories, [])

    # Obtener IDs recientes y filtrar
    recent_ids = fetch_recent_matches(puuid)
    done_ids = get_done_ids(c)
    processed = []

    # Procesar nuevas partidas
    for mid in recent_ids:
        if mid in done_ids:
            continue
        record = process_match(mid, puuid)
        if not record or record["end_timestamp"] < cutoff:
            continue
        save_match(conn, c, record)
        processed.append(record)
        if "derrota" in record["events"]:
            defeats += 1
            if defeats >= DAILY_DEF_LIMIT:
                break
        else:
            victories += 1

    # Devolver JSON final
    return create_response(defeats, victories, processed)
