import os
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
import sqlite3
import requests
from datetime import datetime, timedelta
from pydantic import BaseModel

# Carga de .env
load_dotenv()

# Configuración
API_KEY = os.getenv("RIOT_API_KEY")
if not API_KEY:
    raise RuntimeError("RIOT_API_KEY no definida en el entorno")
REGIONAL = "americas"
DB_FILE = "lol_trackedb.db"
DAILY_DEF_LIMIT = 5     # Derrotas máximas por día
POINTS_PER_VICTORY = 5  # Puntos otorgados por victoria

# Inicializar FastAPI
app = FastAPI(
    title="LoL Tracker API",
    version="1.0.0",
    description="Procesa partidas de LoL y calcula plan de ejercicios"
)

# Modelo entrada
class RiotID(BaseModel):
    game_name: str
    tag_line: str

# Conexión a SQLite y creación de tablas

def get_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
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

# Funciones Riot API

def get_puuid(game_name: str, tag_line: str) -> str:
    url = (
        f"https://{REGIONAL}.api.riotgames.com"
        f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}?api_key={API_KEY}"
    )
    resp = requests.get(url)
    if resp.status_code in (401, 403):
        raise HTTPException(status_code=401, detail="API Key no autorizada o caducada")
    resp.raise_for_status()
    data = resp.json()
    if "puuid" not in data:
        raise HTTPException(status_code=500, detail="Respuesta no contiene puuid")
    return data["puuid"]


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


def process_match(match_id: str, puuid: str) -> dict:
    url = f"https://{REGIONAL}.api.riotgames.com/lol/match/v5/matches/{match_id}"
    resp = requests.get(url, headers={"X-Riot-Token": API_KEY})
    resp.raise_for_status()
    info = resp.json().get("info", {})
    participant = next((p for p in info.get("participants", []) if p.get("puuid") == puuid), None)
    if not participant:
        return None
    events = ["victoria" if participant.get("win", False) else "derrota"]
    champs = [p.get("championName", "").lower() for p in info.get("participants", [])]
    if "teemo" in champs:
        events.append("teemo")
    start_ts = info.get("gameStartTimestamp", 0)
    end_ts = info.get("gameEndTimestamp", 0)
    start_time = datetime.fromtimestamp(start_ts/1000).strftime("%Y-%m-%d %H:%M:%S")
    end_time = datetime.fromtimestamp(end_ts/1000).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "match_id": match_id,
        "start_timestamp": start_ts,
        "end_timestamp": end_ts,
        "start_time": start_time,
        "end_time": end_time,
        "events": events
    }

# Persistencia SQLite

def get_done_ids(c) -> set:
    c.execute("SELECT match_id FROM matches")
    return {row[0] for row in c.fetchall()}


def save_match(conn, c, record: dict):
    if not record:
        return
    c.execute(
        "INSERT OR IGNORE INTO matches(match_id, start_timestamp, end_timestamp, start_time, end_time) VALUES (?, ?, ?, ?, ?)",
        (record["match_id"], record["start_timestamp"], record["end_timestamp"], record["start_time"], record["end_time"])
    )
    for evt in record["events"]:
        c.execute(
            "INSERT INTO match_events(match_id, event) VALUES (?, ?)",
            (record["match_id"], evt)
        )
    conn.commit()

# Endpoint procesar partidas

@app.post("/procesar-partidas/")
def procesar_partidas(id: RiotID):
    conn, c = get_db()
    puuid = get_puuid(id.game_name, id.tag_line)
    now = datetime.now()
    cutoff = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()*1000)
    c.execute("SELECT COUNT(*) FROM match_events me JOIN matches m ON me.match_id=m.match_id WHERE me.event='derrota' AND m.end_timestamp>=?", (cutoff,))
    defeats = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM match_events me JOIN matches m ON me.match_id=m.match_id WHERE me.event='victoria' AND m.end_timestamp>=?", (cutoff,))
    victories = c.fetchone()[0]
    if defeats >= DAILY_DEF_LIMIT:
        return {"derrotas": defeats, "victorias": victories, "mensaje": "Límite diario de derrotas alcanzado"}
    recent_ids = fetch_recent_matches(puuid)
    done = get_done_ids(c)
    new_ids = [mid for mid in recent_ids if mid not in done]
    processed = []
    for mid in new_ids:
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
    return {"derrotas": defeats, "victorias": victories, "nuevas_partidas": processed}
