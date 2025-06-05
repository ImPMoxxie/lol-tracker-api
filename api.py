# api.py

import os
import sqlite3
import requests
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

# ---------------------------
# Configuración
# ---------------------------
load_dotenv()  # Cargar variables de entorno desde .env
API_KEY = os.getenv("RIOT_API_KEY")
if not API_KEY:
    raise RuntimeError("❌ Error: la variable de entorno RIOT_API_KEY no está definida.")

REGIONAL = "americas"    # Para Match–V5
SUMMONER_HOST = "la2"    # Para Account–V1 (Por Riot ID - Latinoamérica Sur)
DB_FILE = "lol_trackedb.db"
DAILY_LIMIT = 5

# ---------------------------
# App de FastAPI
# ---------------------------

app = FastAPI(
    title="LoL Tracker API",
    description="Servicios para procesar partidas de League of Legends y calcular ejercicios.",
    version="1.0.0"
)

# Modelo Pydantic para recibir el Riot ID
class RiotID(BaseModel):
    game_name: str
    tag_line: str

# ---------------------------
# Conexión a SQLite
# ---------------------------

def get_db():
    """
    Devuelve una conexión y cursor de SQLite.
    check_same_thread=False permite que FastAPI use el cursor en distintas requests.
    """
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    return conn, c

# ---------------------------
# Funciones de API
# ---------------------------

def get_puuid(game_name: str, tag_line: str) -> str:
    """
    Obtiene el PUUID usando Account–V1 (/by-riot-id/).
    """
    url = (
        f"https://{REGIONAL}.api.riotgames.com"
        f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
        f"?api_key={API_KEY}"
    )
    resp = requests.get(url)
    if resp.status_code == 401 or resp.status_code == 403:
        raise HTTPException(
            status_code=401,
            detail="API Key no autorizada o endpoint no permitido para get_puuid."
        )
    resp.raise_for_status()
    return resp.json()["puuid"]

def fetch_recent_matches(puuid: str, count: int = 5) -> list:
    """
    Obtiene IDs de partidas recientes (Match–V5, región AMERICAS).
    """
    url = (
        f"https://{REGIONAL}.api.riotgames.com"
        f"/lol/match/v5/matches/by-puuid/{puuid}/ids"
        f"?start=0&count={count}"
    )
    headers = {"X-Riot-Token": API_KEY}
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    return resp.json()  # Lista de match IDs

def process_match(match_id: str, puuid: str) -> dict:
    """
    Descarga detalles de una partida y extrae:
    - match_id, start/end timestamps y horas
    - eventos automáticos: 'victoria' o 'derrota' y 'teemo' si aplica
    Retorna un diccionario con esa info.
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
    end_ts = info.get("gameEndTimestamp")
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

def get_done_ids(c) -> set:
    """
    Devuelve el set de match_id ya presentes en la tabla matches.
    """
    c.execute("SELECT match_id FROM matches")
    return {row[0] for row in c.fetchall()}

def save_match(c, conn, record: dict):
    """
    Inserta en matches y match_events sin duplicar:
    - matches: match_id, start/end timestamps, start_time, end_time
    - match_events: por cada evento que haya en record['events']
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

def count_since(c, days: int = 0) -> int:
    """
    Cuenta partidas en matches cuya end_timestamp >= corte.
    Si days=0, corte=medianoche de hoy; si days>0, medianoche de hace 'days' días.
    """
    now = datetime.now()
    if days == 0:
        cutoff = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    else:
        cutoff = int((now - timedelta(days=days))
                     .replace(hour=0, minute=0, second=0, microsecond=0)
                     .timestamp() * 1000)
    c.execute("SELECT COUNT(*) FROM matches WHERE end_timestamp >= ?", (cutoff,))
    return c.fetchone()[0]

# ---------------------------
# Ejercicios por grupo muscular
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

def calculate_exercises(c, today_cutoff: int) -> dict:
    """
    Consulta la BD para contar 'victoria' y 'derrota' hoy, y calcula repeticiones netas
    por cada grupo muscular.
    Devuelve un dict con:
    {
      "victorias": int,
      "derrotas": int,
      "plan_de_ejercicio": {
         <grupo>: [ {"nombre": ..., "reps": ...}, ... ],
         ...
      }
    }
    """
    # 1) contar victorias y derrotas de hoy
    c.execute(
        "SELECT COUNT(*) FROM match_events me "
        "JOIN matches m ON me.match_id = m.match_id "
        "WHERE me.event = 'victoria' AND m.end_timestamp >= ?", (today_cutoff,)
    )
    victories = c.fetchone()[0]

    c.execute(
        "SELECT COUNT(*) FROM match_events me "
        "JOIN matches m ON me.match_id = m.match_id "
        "WHERE me.event = 'derrota' AND m.end_timestamp >= ?", (today_cutoff,)
    )
    defeats = c.fetchone()[0]

    # 2) calcular plan de ejercicios
    plan = {}
    if defeats > 0:
        for grupo, ejercicios in FULL_WORKOUT.items():
            lista = []
            for nombre, reps in ejercicios:
                net = reps * defeats - 5 * victories
                if net > 0:
                    lista.append({"nombre": nombre, "reps": net})
            if lista:
                plan[grupo] = lista
    else:
        # Si no hay derrotas y tampoco victorias, no hay ejercicios:
        if victories == 0:
            plan = {}

    return {
        "victorias": victories,
        "derrotas": defeats,
        "plan_de_ejercicio": plan
    }

# ---------------------------
# Endpoint: procesar partidas
# ---------------------------

@app.post("/procesar-partidas/", summary="Procesa nuevas partidas y devuelve plan de ejercicios")
def procesar_partidas(id: RiotID):
    """
    Recibe JSON con { "game_name": "...", "tag_line": "..." }.
    1) Obtiene puuid.
    2) Cuenta cuántas partidas hay hoy (today_count).
    3) Si today_count < DAILY_LIMIT, desembusca IDs recientes:
       - Filtra los que no estén en BD.
       - Por cada nuevo ID:
         a) hace process_match()
         b) guarda en BD (save_match)
         c) incrementa today_count
         d) si today_count == DAILY_LIMIT, deja de procesar
    4) Llama a calculate_exercises() para obtener victorias/derrotas y plan.
    5) Devuelve JSON con esos valores y la lista de partidos procesadas (opcional).
    """
    conn, c = get_db()

    # 1) Obtener PUUID
    try:
        puuid = get_puuid(id.game_name, id.tag_line)
    except HTTPException as e:
        conn.close()
        raise e

    # 2) Contar partidas de hoy ya registradas en BD
    today_count = count_since(c, days=0)
    if today_count >= DAILY_LIMIT:
        # Ya alcanzó límite diario, devolvemos el estado actual sin procesar nada nuevo
        today_cutoff = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        ejercicios = calculate_exercises(c, today_cutoff)
        conn.close()
        return {
            "partidas_hoy": today_count,
            **ejercicios,
            "nuevas_partidas": []
        }

    # 3) Traer IDs recientes
    recent_ids = fetch_recent_matches(puuid, count=5)
    done_ids = get_done_ids(c)
    new_ids = [mid for mid in recent_ids if mid not in done_ids]

    processed = []
    today_cutoff = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)

    # 3a) Por cada match_id no registrado y que termine hoy, procesar e insertar
    for mid in new_ids:
        record = process_match(mid, puuid)
        if record["end_timestamp"] < today_cutoff:
            continue
        if today_count >= DAILY_LIMIT:
            break
        save_match(c, conn, record)
        processed.append(record)
        today_count += 1

    # 4) Calcular victorias/derrotas y plan de ejercicios
    ejercicios = calculate_exercises(c, today_cutoff)
    conn.close()

    # 5) Construir respuesta
    return {
        "partidas_hoy": today_count,
        **ejercicios,
        "nuevas_partidas": processed
    }
