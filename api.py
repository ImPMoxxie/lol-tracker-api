import os
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
import sqlite3
import requests
from datetime import datetime, timedelta
from pydantic import BaseModel

# --- Carga de variables de entorno desde .env ---
env_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(env_path, override=True)

# --- Configuración global ---
API_KEY = os.getenv("RIOT_API_KEY")  # Clave de API de Riot Games
if not API_KEY:
    raise RuntimeError("❌ RIOT_API_KEY no definida en el entorno")
REGIONAL = "americas"               # Región para Account–V1 y Match–V5
DB_FILE = "lol_trackedb.db"         # Archivo SQLite para persistencia
daily_def_limit = 5                   # Límite de derrotas registradas por día
points_per_victory = 5                # Puntos base que aporta cada victoria
ALLOWED_QUEUES = {400, 420}           # Solo Normal Draft (400) y Ranked Solo/Duo (420)

# --- Inicialización de la API ---
app = FastAPI(
    title="LoL Tracker API",
    version="1.3.0",
    description="Procesa partidas con rachas dinámicas y plan de ejercicios interactivo"
)

# --- Modelo de entrada ---
class RiotID(BaseModel):
    game_name: str  # Nombre del invocador (sin #TAG)
    tag_line: str   # Sufijo de región, ej. LAS, BR1

# --- Helpers para la base de datos SQLite ---
def get_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    # Creamos tablas si no existen
    c.executescript("""
CREATE TABLE IF NOT EXISTS matches (
  match_id TEXT PRIMARY KEY,
  queue_id INTEGER,
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

# --- Riot API calls ---
def get_puuid(game_name: str, tag_line: str) -> str:
    url = (
        f"https://{REGIONAL}.api.riotgames.com"
        f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}?api_key={API_KEY}"
    )
    resp = requests.get(url)
    if resp.status_code in (401, 403):
        raise HTTPException(status_code=401, detail="API Key no autorizada o caducada")
    resp.raise_for_status()
    puuid = resp.json().get("puuid")
    if not puuid:
        raise HTTPException(status_code=500, detail="Falta puuid en respuesta de Account–V1")
    return puuid


def fetch_recent_matches(puuid: str, count: int = 5) -> list:
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
    # Llamada a Match-V5
    url = f"https://{REGIONAL}.api.riotgames.com/lol/match/v5/matches/{match_id}"
    resp = requests.get(url, headers={"X-Riot-Token": API_KEY})
    resp.raise_for_status()
    info = resp.json().get("info", {})

    # 1) Omitir remakes / early surrenders
    if info.get("gameEndedInEarlySurrender", False):
        return None

    if info.get("gameDuration", 0) < 300:  # Partidas de menos de 5 minutos
        return None

    # 2) Filtrar por modos permitidos
    queue_id = info.get("queueId", 0)
    if queue_id not in ALLOWED_QUEUES:
        return None

    # 3) Buscar la participación del invocador y determinar evento
    participant = next((p for p in info.get("participants", [])
                        if p.get("puuid") == puuid), None)
    if not participant:
        return None
    # Determinar evento: victoria o derrota
    events = ["victoria" if participant.get("win", False) else "derrota"]

    
    # 4) Formatear timestamps de la partida
    start_ts = info.get("gameStartTimestamp", 0)
    end_ts   = info.get("gameEndTimestamp", 0)
    start_time = datetime.fromtimestamp(start_ts/1000).strftime("%Y-%m-%d %H:%M:%S")
    end_time   = datetime.fromtimestamp(end_ts/1000).strftime("%Y-%m-%d %H:%M:%S")

    return {
        "match_id": match_id,
        "queue_id": queue_id,
        "start_timestamp": start_ts,
        "end_timestamp": end_ts,
        "start_time": start_time,
        "end_time": end_time,
        "events": events
    }

# --- DB helpers ---
def get_done_ids(c) -> set:
    c.execute("SELECT match_id FROM matches WHERE queue_id IN (?,?)", tuple(ALLOWED_QUEUES))
    return {row[0] for row in c.fetchall()}


def save_match(conn, c, rec: dict):
    if not rec:
        return
    c.execute(
        "INSERT OR IGNORE INTO matches(match_id, queue_id, start_timestamp, end_timestamp, start_time, end_time) VALUES(?,?,?,?,?,?)",
        (rec["match_id"], rec["queue_id"], rec["start_timestamp"], rec["end_timestamp"], rec["start_time"], rec["end_time"])  
    )
    for evt in rec["events"]:
        c.execute("INSERT INTO match_events(match_id, event) VALUES(?,?)", (rec["match_id"], evt))
    conn.commit()

# --- Lista de ejercicios y cálculo de plan ---
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

# --- Función para calcular puntos dinámicos según rachas ---
def calculate_dynamic_points(c, cutoff):
    """
    Recorre todos los eventos de hoy en orden cronológico y aplica:
      - streak reinicializable: +1 por victoria, reset a 0 en derrota.
      - al cada derrota: gained = streak * points_per_victory, acumula en points
        y aumenta points_per_victory += gained.
      - detiene al llegar al límite de derrotas diarias.
    Devuelve puntos totales acumulados.
    """
    # Obtener eventos ordenados por fin de partida\    
    c.execute(
        "SELECT m.end_timestamp, me.event FROM match_events me"
        " JOIN matches m ON me.match_id=m.match_id"
        " WHERE m.end_timestamp>=?"
        " ORDER BY m.end_timestamp",
        (cutoff,)
    )
    rows = c.fetchall()
    points = 0
    streak = 0
    defeats = 0
    p_per_victory = points_per_victory
    for ts, event in rows:
        if event == "victoria":
            streak += 1
        elif event == "derrota":
            # calcular puntos al romper la racha
            gained = streak * p_per_victory
            points += gained
            # escalar valor de victoria para siguientes rachas
            p_per_victory += gained
            streak = 0
            defeats += 1
            if defeats >= daily_def_limit:
                break
    return points

# --- Construcción de la respuesta JSON ---
def create_response(defeats: int, victories: int, points: int, processed: list) -> dict:
    """
    Arma la respuesta final:
      - derrotas_hoy, victorias_hoy, puntos_restantes
      - plan_ejercicio: lista resultante tras descuento de puntos
      - nuevas_partidas: detalles de partidas procesadas
    """
    # base de ejercicio ajustada según derrotas
    base = [(name, reps * defeats) for name, reps in FULL_WORKOUT]
    final_ex = []
    rem_points = points
    for name, reps in base:
        if rem_points >= reps:
            rem_points -= reps
            continue
        if rem_points > 0:
            final_ex.append({"nombre": name, "reps": reps - rem_points})
            rem_points = 0
        else:
            final_ex.append({"nombre": name, "reps": reps})

    return {
        "derrotas_hoy": defeats,
        "victorias_hoy": victories,
        "puntos_restantes": rem_points,
        "plan_ejercicio": final_ex,
        "nuevas_partidas": processed
    }

# --- Endpoint principal: POST /procesar-partidas/ ---
@app.post("/procesar-partidas/")
def procesar_partidas(id: RiotID):
    conn, c = get_db()
    puuid = get_puuid(id.game_name, id.tag_line)
    cutoff = int(datetime.now()
                 .replace(hour=0, minute=0, second=0, microsecond=0)
                 .timestamp() * 1000)

    # 1. Contar derrotas y victorias actuales
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


    # 2. Calcular puntos dinámicos **siempre**, antes de cortar por límite de derrotas**
    current_points = calculate_dynamic_points(c, cutoff)

    # 3. Si ya alcanzó el límite de derrotas, devolvemos sin procesar más partidas
    if defeats >= daily_def_limit:
        return create_response(defeats, victories, current_points, [])

    # 4. Procesar nuevas partidas (igual que antes)… 
    recent_ids = fetch_recent_matches(puuid)
    done_ids = get_done_ids(c)
    processed = []
    for mid in recent_ids:
        if mid in done_ids:
            continue
        rec = process_match(mid, puuid)
        if not rec or rec["end_timestamp"] < cutoff:
            continue
        save_match(conn, c, rec)
        processed.append(rec)
        # actualizar conteo básico de derrotas/victorias
        if "derrota" in rec["events"]:
            defeats += 1
            if defeats >= daily_def_limit:
                break
        else:
            victories += 1

    # 5. Al final, recalculamos puntos (o reutilizamos current_points) y devolvemos:
    dyn_points = calculate_dynamic_points(c, cutoff)
    return create_response(defeats, victories, dyn_points, processed)
