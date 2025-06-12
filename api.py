import os
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
import sqlite3
import requests
from datetime import datetime, timedelta
from pydantic import BaseModel

# Carga de variables de entorno desde .env
# Override asegura que cualquier variable previa se reemplace
env_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(env_path, override=True)

# Configuración
# Clave de API de Riot Games
API_KEY = os.getenv("RIOT_API_KEY")
if not API_KEY:
    raise RuntimeError("❌ RIOT_API_KEY no definida en el entorno")
# Región para llamadas a Account–V1 y Match–V5
REGIONAL = "americas"
# Archivo local SQLite para persistencia
DB_FILE = "lol_trackedb.db"
# Límite diario de derrotas que disparará fin de conteo
daily_def_limit = 5
# Puntos otorgados por cada victoria (usados para deducir ejercicios)
points_per_victory = 5
# Solo estos modos de juego se contabilizarán (Normal Draft y Ranked)
ALLOWED_QUEUES = {400, 420}

# Inicializar FastAPI
app = FastAPI(
    title="LoL Tracker API",
    version="1.2.0",
    description="Procesa partidas (solo queueId 400 y 420) y genera plan de ejercicios"
)

# Modelo de entrada para validar el Riot ID
class RiotID(BaseModel):
    game_name: str  # Nombre de invocador
    tag_line: str   # Etiqueta de región/prefijo

# Helpers de SQLite
def get_db():
    """
    Abre conexión a SQLite y crea tablas si no existen:
     - matches: info básica de cada partida (incluye queue_id para filtrado)
     - match_events: eventos (victoria/derrota) asociados a cada partida
    """
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    # Creamos tablas con script para asegurar integridad
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

# Funciones de la Riot API
def get_puuid(game_name: str, tag_line: str) -> str:
    """
    Llama al endpoint Account–V1 para convertir Riot ID en PUUID.
    Lanza HTTPException 401 si la API key no está autorizada.
    """
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
        raise HTTPException(status_code=500, detail="Respuesta de Account–V1 sin campo puuid")
    return puuid


def fetch_recent_matches(puuid: str, count: int = 5) -> list:
    """
    Obtiene los IDs de las últimas `count` partidas de un jugador.
    No filtra por queue; eso se hace en process_match.
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
    Procesa detalles de una partida:
     - Aplica filtro de queue_id
     - Extrae victoria/derrota del participante
     - Formatea timestamps a legible
    Devuelve None si el modo no está permitido.
    """
    url = f"https://{REGIONAL}.api.riotgames.com/lol/match/v5/matches/{match_id}"
    resp = requests.get(url, headers={"X-Riot-Token": API_KEY})
    resp.raise_for_status()
    info = resp.json().get("info", {})

    # Filtro por queue_id para limitar a modos deseados
    queue_id = info.get("queueId", 0)
    if queue_id not in ALLOWED_QUEUES:
        return None

    # Localizar información del jugador dentro de la partida
    participant = next((p for p in info.get("participants", []) if p.get("puuid") == puuid), None)
    if not participant:
        return None

    # Determinar resultado: victoria o derrota
    events = ["victoria" if participant.get("win", False) else "derrota"]

    # Formateo de tiempos en ms a cadena legible
    start_ts = info.get("gameStartTimestamp", 0)
    end_ts = info.get("gameEndTimestamp", 0)
    start_time = datetime.fromtimestamp(start_ts / 1000).strftime("%Y-%m-%d %H:%M:%S")
    end_time = datetime.fromtimestamp(end_ts / 1000).strftime("%Y-%m-%d %H:%M:%S")

    return {
        "match_id": match_id,
        "queue_id": queue_id,
        "start_timestamp": start_ts,
        "end_timestamp": end_ts,
        "start_time": start_time,
        "end_time": end_time,
        "events": events
    }

# DB helpers
def get_done_ids(c) -> set:
    """
    Devuelve set de match_id ya almacenados en modos permitidos.
    """
    c.execute("SELECT match_id FROM matches WHERE queue_id IN (?,?)", tuple(ALLOWED_QUEUES))
    return {row[0] for row in c.fetchall()}


def save_match(conn, c, rec: dict):
    """
    Guarda registro de partida y sus eventos en la base de datos.
    Usa IGNORE para evitar duplicados.
    """
    if not rec:
        return
    c.execute(
        "INSERT OR IGNORE INTO matches(match_id, queue_id, start_timestamp, end_timestamp, start_time, end_time) VALUES(?,?,?,?,?,?)",
        (rec["match_id"], rec["queue_id"], rec["start_timestamp"], rec["end_timestamp"], rec["start_time"], rec["end_time"])  
    )
    for evt in rec["events"]:
        c.execute("INSERT INTO match_events(match_id, event) VALUES(?,?)", (rec["match_id"], evt))
    conn.commit()

# Ejercicios base y calculo de plan
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

def calculate_plan(defeats: int, victories: int) -> dict:
    """
    Construye el plan de ejercicios:
     - Multiplica reps base por número de derrotas
     - Resta puntos de victoria de las repeticiones totales
    """
    points = victories * points_per_victory
    base = [(name, reps * defeats) for name, reps in FULL_WORKOUT]
    final = []
    for name, reps in base:
        # Si los puntos cubren todos los reps, eliminamos ese ejercicio
        if points >= reps:
            points -= reps
            continue
        # Si quedan puntos parciales, ajustamos reps
        if points > 0:
            final.append({"nombre": name, "reps": reps - points})
            points = 0
        else:
            final.append({"nombre": name, "reps": reps})
    return {"puntos_restantes": points, "ejercicios": final}

# Crear respuesta JSON
def create_response(defeats: int, victories: int, processed: list) -> dict:
    """
    Genera respuesta final con conteos y plan de ejercicios.
    """
    plan = calculate_plan(defeats, victories)
    return {
        "derrotas_hoy": defeats,
        "victorias_hoy": victories,
        "puntos_restantes": plan["puntos_restantes"],
        "plan_ejercicio": plan["ejercicios"],
        "nuevas_partidas": processed
    }

# Endpoint principal
@app.post("/procesar-partidas/")
def procesar_partidas(id: RiotID):
    """
    Flujo principal:
     1. Obtiene puuid
     2. Cuenta derrotas/victorias actuales del día
     3. Trae IDs recientes y filtra ya procesadas
     4. Procesa nuevas partidas según queue_id
     5. Actualiza base y calcula plan de ejercicios
    """
    conn, c = get_db()
    puuid = get_puuid(id.game_name, id.tag_line)
    # Corte desde medianoche para conteos diarios
    cutoff = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)

    # Conteo de derrotas y victorias hoy
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

    # Si límite de derrotas alcanzado, no procesar más
    if defeats >= daily_def_limit:
        return create_response(defeats, victories, [])

    # Obtener y procesar partidas nuevas
    recent_ids = fetch_recent_matches(puuid)
    done_ids = get_done_ids(c)
    processed = []
    for mid in recent_ids:
        if mid in done_ids:
            continue
        rec = process_match(mid, puuid)
        # Saltar si modo no permitido o partida antigua
        if not rec or rec["end_timestamp"] < cutoff:
            continue
        save_match(conn, c, rec)
        processed.append(rec)
        # Actualizar conteo dinámico de derrotas/victorias
        if "derrota" in rec["events"]:
            defeats += 1
            if defeats >= daily_def_limit:
                break
        else:
            victories += 1

    return create_response(defeats, victories, processed)
