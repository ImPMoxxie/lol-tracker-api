# api.py

import os
import sqlite3
import requests
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# ---------------------------
# Configuración
# ---------------------------

API_KEY = os.getenv("RIOT_API_KEY")
if not API_KEY:
    raise RuntimeError("❌ Error: la variable de entorno RIOT_API_KEY no está definida.")

REGIONAL = "americas"    # Para Match–V5
SUMMONER_HOST = "la2"    # Para Account–V1 (Por Riot ID - Latinoamérica Sur)
DB_FILE = "lol_trackedb.db"
DAILY_LIMIT = 5

# ---------------------------
# FastAPI App
# ---------------------------

app = FastAPI(
    title="LoL Tracker API",
    description="Servicio REST para procesar partidas de LoL y calcular ejercicios.",
    version="1.0.0"
)

class RiotID(BaseModel):
    game_name: str
    tag_line: str

# ---------------------------
# Inicialización de SQLite (al importar el módulo)
# ---------------------------

# Conexión global a la base, abierta cuando se importa el módulo
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
c = conn.cursor()

# Creamos las dos tablas si no existen
c.executescript("""
CREATE TABLE IF NOT EXISTS matches (
  match_id        TEXT PRIMARY KEY,
  start_timestamp INTEGER,
  end_timestamp   INTEGER,
  start_time      TEXT,
  end_time        TEXT
);
CREATE TABLE IF NOT EXISTS match_events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id  TEXT NOT NULL,
  event     TEXT NOT NULL,
  FOREIGN KEY(match_id) REFERENCES matches(match_id)
);
""")
conn.commit()

# ---------------------------
# Funciones auxiliares de Riot API
# ---------------------------

def get_puuid(game_name: str, tag_line: str) -> str:
    """
    Obtiene el PUUID a través del endpoint Account–V1 (por Riot ID).
    """
    url = (
        f"https://{REGIONAL}.api.riotgames.com"
        f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
        f"?api_key={API_KEY}"
    )
    resp = requests.get(url)
    if resp.status_code in (401, 403):
        raise HTTPException(
            status_code=401,
            detail="API_KEY no autorizada o endpoint no permitido para get_puuid."
        )
    resp.raise_for_status()
    return resp.json()["puuid"]

def fetch_recent_matches(puuid: str, count: int = 5) -> list:
    """
    Obtiene IDs de partidas recientes (Match–V5, región AMERICAS).
    """
    url = (
        f"https://{REGIONAL}.api.riotgames.com"
        f"/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count={count}"
    )
    headers = {"X-Riot-Token": API_KEY}
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    return resp.json()

def process_match(match_id: str, puuid: str) -> dict:
    """
    Descarga detalles de una partida y extrae:
      - match_id, start/end timestamps y horas
      - eventos automáticos: 'victoria' o 'derrota' y 'teemo' si aplica
    Retorna un diccionario con esa información.
    """
    url = f"https://{REGIONAL}.api.riotgames.com/lol/match/v5/matches/{match_id}"
    headers = {"X-Riot-Token": API_KEY}
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    info = resp.json()["info"]

    # Encuentra tu participante
    participant = next(p for p in info["participants"] if p["puuid"] == puuid)
    events = []
    if participant.get("win", False):
        events.append("victoria")
    else:
        events.append("derrota")

    champions = [p.get("championName", "").lower() for p in info["participants"]]
    if "teemo" in champions:
        events.append("teemo")

    start_ts = info.get("gameStartTimestamp")
    end_ts   = info.get("gameEndTimestamp")
    start_time = datetime.fromtimestamp(start_ts / 1000).strftime("%Y-%m-%d %H:%M:%S")
    end_time   = datetime.fromtimestamp(end_ts / 1000).strftime("%Y-%m-%d %H:%M:%S")

    return {
        "match_id": match_id,
        "start_timestamp": start_ts,
        "end_timestamp": end_ts,
        "start_time": start_time,
        "end_time": end_time,
        "events": events
    }

# ---------------------------
# Persistencia en SQLite
# ---------------------------

def get_done_ids() -> set:
    """
    Devuelve un set de match_id que ya están en la tabla 'matches'.
    """
    c.execute("SELECT match_id FROM matches")
    return {row[0] for row in c.fetchall()}

def save_match(record: dict):
    """
    Inserta en tabla 'matches' y en 'match_events' (uno por cada evento).
    """
    c.execute(
        "INSERT OR IGNORE INTO matches(match_id, start_timestamp, end_timestamp, start_time, end_time) "
        "VALUES (?, ?, ?, ?, ?)",
        (record["match_id"],
         record["start_timestamp"],
         record["end_timestamp"],
         record["start_time"],
         record["end_time"])
    )
    for evt in record["events"]:
        c.execute(
            "INSERT INTO match_events(match_id, event) VALUES (?, ?)",
            (record["match_id"], evt)
        )
    conn.commit()

def count_since(days: int = 0) -> int:
    """
    Cuenta cuántas filas hay en 'matches' cuya end_timestamp >= corte.
    days=0 => “desde medianoche de hoy”
    days>0 => “desde medianoche de hace 'days' días”
    """
    now = datetime.now()
    if days == 0:
        cutoff = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    else:
        cutoff = int(
            (now - timedelta(days=days))
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp() * 1000
        )
    c.execute("SELECT COUNT(*) FROM matches WHERE end_timestamp >= ?", (cutoff,))
    return c.fetchone()[0]

# ---------------------------
# Definición de ejercicios por grupo muscular
# ---------------------------

FULL_WORKOUT = {
    "piernas": [
        ("Sentadillas", 40),
        ("Zancadas (lunges)", 20)
    ],
    "pecho_hombros_triceps": [
        ("Flexiones convencionales", 20),
        ("Flexiones con palmas juntas", 10)
    ],
    "espalda_biceps": [
        ("Curl isométrico con toalla", 15),
        ("Superman (lumbar)", 15)
    ],
    "core": [
        ("Plancha frontal (plank)", 60),
        ("Crunches", 30)
    ],
    "cardio": [
        ("Jumping Jacks", 30),
        ("Saltos de sentadilla", 20)
    ]
}

def calculate_exercises(cutoff_ts: int) -> dict:
    """
    Cuenta victorias y derrotas de hoy y calcula repeticiones netas por cada grupo.
    Devuelve un dict:
    {
      "victorias": <int>,
      "derrotas": <int>,
      "plan_de_ejercicio": {
          "piernas": [ { "nombre": ..., "reps": ... }, ... ],
          ...
      }
    }
    """
    # 1) Contar victorias de hoy
    c.execute(
        "SELECT COUNT(*) FROM match_events me "
        "JOIN matches m ON me.match_id = m.match_id "
        "WHERE me.event = 'victoria' AND m.end_timestamp >= ?", (cutoff_ts,)
    )
    victories = c.fetchone()[0]

    # 2) Contar derrotas de hoy
    c.execute(
        "SELECT COUNT(*) FROM match_events me "
        "JOIN matches m ON me.match_id = m.match_id "
        "WHERE me.event = 'derrota' AND m.end_timestamp >= ?", (cutoff_ts,)
    )
    defeats = c.fetchone()[0]

    # 3) Calcular plan de ejercicios netos
    plan = {}
    if defeats > 0:
        for grupo, ejercicios in FULL_WORKOUT.items():
            lista = []
            for nombre, reps in ejercicios:
                net_reps = reps * defeats - 5 * victories
                if net_reps > 0:
                    lista.append({"nombre": nombre, "reps": net_reps})
            if lista:
                plan[grupo] = lista
    else:
        # Si no hay derrotas y tampoco victorias => plan vacío
        if victories == 0:
            plan = {}

    return {
        "victorias": victories,
        "derrotas": defeats,
        "plan_de_ejercicio": plan
    }

# ---------------------------
# Endpoint: Procesar Partidas
# ---------------------------

@app.post("/procesar-partidas/", summary="Procesa nuevas partidas y devuelve plan de ejercicios")
def procesar_partidas(id: RiotID):
    """
    1) Obtenemos PUUID (Account–V1).
    2) Contamos cuántas partidas hay hoy (count_since).
    3) Si no llegó al límite diario, descargamos IDs recientes de Match–V5,
       filtramos los que ya estén en BD, procesamos e insertamos (hasta tope).
    4) Calculamos victorias/derrotas y plan de ejercicios (calculate_exercises).
    5) Devolvemos JSON con:
       - partidas_hoy
       - victorias, derrotas
       - plan_de_ejercicio { … }
       - nuevas_partidas (lista de partidas procesadas aquí)
    """
    # 1) Conectar y obtener PUUID
    puuid = None
    try:
        puuid = get_puuid(id.game_name, id.tag_line)
    except HTTPException as e:
        # Si la clave está mal o no tiene permiso, devolvemos error 401 al cliente
        raise e

    # 2) Contar partidas ya registradas de hoy
    today_count = count_since(days=0)
    today_cutoff = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)

    if today_count >= DAILY_LIMIT:
        # Ya llegó al límite diario; devolvemos el estado sin procesar partidas nuevas
        ejercicios = calculate_exercises(today_cutoff)
        return {
            "partidas_hoy": today_count,
            **ejercicios,
            "nuevas_partidas": []
        }

    # 3) Traer IDs recientes y filtrar los ya guardados
    recent_ids = fetch_recent_matches(puuid, count=5)
    done_ids = get_done_ids()
    new_ids = [mid for mid in recent_ids if mid not in done_ids]

    processed = []
    # Iterar sobre nuevos IDs: procesar, insertar y contar
    for mid in new_ids:
        record = process_match(mid, puuid)
        if record["end_timestamp"] < today_cutoff:
            # Si la partida no terminó hoy, la ignoramos
            continue
        if today_count >= DAILY_LIMIT:
            # Límite alcanzado—detenerse
            break
        save_match(record)
        processed.append(record)
        today_count += 1

    # 4) Calcular victorias/derrotas y plan de ejercicios tras insertar
    ejercicios = calculate_exercises(today_cutoff)

    # 5) Devolver JSON con toda la info
    return {
        "partidas_hoy": today_count,
        **ejercicios,
        "nuevas_partidas": processed
    }
