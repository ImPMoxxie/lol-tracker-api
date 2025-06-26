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
from datetime import datetime

# --- Carga de variables de entorno ---
env_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(env_path, override=True)

# --- Configuración global ---
API_KEY = os.getenv("RIOT_API_KEY")  # Riot API Key
if not API_KEY:
    raise RuntimeError("❌ RIOT_API_KEY no definida en el entorno")
REGIONAL = "americas"             # Región para Account y Match API
DB_FILE = "lol_trackedb.db"       # Archivo SQLite
DAILY_DEF_LIMIT = 5               # Límite derrotas diarias
POINTS_PER_VICTORY_BASE = 5       # Puntos base por victoria
ALLOWED_QUEUES = {400, 420, 440}  # Colas permitidas: Normal, Solo/Dúo, Flex
RECENT_MATCH_COUNT = 20           # Cantidad de partidas a recuperar

# NO BORRAR: Plan de ejercicios
FULL_WORKOUT = [
    ("Sentadillas", 40),
    ("Zancadas (lunges)", 20),
    ("Flexiones convencionales", 20),
    ("Flexiones palmas juntas", 10),
    ("Curl isométrico con toalla", 15),
    ("Superman lumbares", 15),
    ("Plancha frontal (segundos)", 60),
    ("Crunches", 30),
    ("Jumping Jacks", 30),
    ("Saltos de sentadilla", 20)
]

def calculate_plan(defeats: int, points: int) -> dict:
    """
    Genera el plan de ejercicios diario:
      - Base: reps * derrotas.
      - Se descuenta cada ejercicio completo si hay puntos suficientes.
      - Si quedan puntos parciales, se descuenta de la siguiente lista.
    """
    # Reps totales sin descuento
    base = [(name, reps * defeats) for name, reps in FULL_WORKOUT]
    remaining = points
    final = []
    for name, reps in base:
        if remaining >= reps:
            remaining -= reps
            # Ejercicio eliminado por completo (puntos cubren todo)
            continue
        if remaining > 0:
            # Parcial: se descuenta lo que quede
            final.append({"nombre": name, "reps": reps - remaining})
            remaining = 0
        else:
            # Sin descuento
            final.append({"nombre": name, "reps": reps})
    return {"puntos_restantes": remaining, "ejercicios": final}


# --- Inicialización de FastAPI y RiotWatcher ---
app = FastAPI(
    title="LoL Tracker API",
    version="1.6.6",
    description="Procesa partidas con cache local, streaks y plan de ejercicios"
)
watcher = LolWatcher(API_KEY)

# --- Modelo de entrada ---
class RiotID(BaseModel):
    game_name: str
    tag_line: str  # ya no se guarda en BD, solo para obtener puuid

# === Funciones de base de datos ===
def get_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    # match_events ahora con índice único para evitar duplicados
    c.executescript("""
CREATE TABLE IF NOT EXISTS matches (
  match_id TEXT PRIMARY KEY,
  queue_id INTEGER,
  end_timestamp DATE,
  game_creation DATE,
  summoner_name TEXT
);
CREATE TABLE IF NOT EXISTS match_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id TEXT NOT NULL,
  event TEXT NOT NULL,
  summoner_name TEXT,
  UNIQUE(match_id, event, summoner_name)
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
    time.sleep(0.5)  # Throttle para evitar rate limits
    return data

# Obtiene puuid usando Account–V1
def get_puuid(game_name: str, tag_line: str) -> str:
    data = riot_request(f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}")
    puuid = data.get("puuid")
    if not puuid:
        raise HTTPException(500, "No se obtuvo puuid")
    return puuid

# Obtiene IDs recientes (Match–V5)
def fetch_recent_matches(puuid: str, count: int = RECENT_MATCH_COUNT) -> list:
    """
    Obtiene los últimos `count` IDs de partidas (Match–V5) para un PUUID.
    El valor por defecto viene de RECENT_MATCH_COUNT.
    """
    return riot_request(
        f"/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count={count}"
    )

# Procesa partida con cache y filtros
def process_match(match_id: str, puuid: str, summoner_name: str, conn, c) -> dict:
    # Cache local
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

    # Filtros básicos
    if info.get("gameEndedInEarlySurrender") or info.get("gameDuration", 0) < 300:
        return None
    queue_id = info.get("queueId", 0)
    if queue_id not in ALLOWED_QUEUES:
        return None
    # Verifica participación
    me = next((p for p in info.get("participants", []) if p.get("puuid") == puuid), None)
    if not me:
        return None

    # Formatea fechas legibles
    raw_end = info.get("gameEndTimestamp")
    raw_creation = info.get("gameCreation")
    end_str = datetime.fromtimestamp(raw_end/1000).strftime("%Y-%m-%d %H:%M:%S")
    creation_str = datetime.fromtimestamp(raw_creation/1000).strftime("%Y-%m-%d %H:%M:%S")
    
    result = {
        "match_id": match_id,
        "queue_id": queue_id,
        "end_timestamp_str": end_str,
        "game_creation_str": creation_str,
        "raw_creation": raw_creation,
        "events": ["victoria" if me.get("win") else "derrota"],
        "summoner_name": summoner_name
    }
     # Verificación explícita de retorno
    if not isinstance(result, dict):
        raise RuntimeError(f"process_match devolvió valor inesperado para {match_id}: {result}")
    return result


# === BLOQUE DE GESTIÓN DE STREAKS + CÁLCULO DE PUNTOS DINÁMICOS ===
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
def calculate_dynamic_points(conn, c, cutoff: str, summoner_name: str) -> int:
    """
    Recorre los eventos de hoy para un invocador específico y acumula puntos de streak:
      - Por victoria aumenta streak.
      - Al derrota: ganó = streak * p_per_victory, acumula y escala base.
      - Se detiene al llegar al límite de derrotas.
    """
    rows = c.execute(
        "SELECT m.end_timestamp, me.event FROM match_events me "
        "JOIN matches m ON me.match_id=m.match_id "
        "WHERE m.game_creation>=? AND me.summoner_name=? "
        "ORDER BY m.end_timestamp", 
        (cutoff, summoner_name)
    ).fetchall()
    points = 0 
    streak = 0
    defeats = 0
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
    summoner = id.game_name  # guardamos solo nombre, no tag_line
    # Debug timezone: mostrar hora local y UTC y cutoff
    local_now = datetime.now()
    utc_now = datetime.utcnow()
    print(f"DEBUG: local now = {local_now}")
    print(f"DEBUG: UTC now   = {utc_now}")

    # Definir corte de día en formato cadena ISO
    cutoff_dt = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff_fmt = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")
    print(f"DEBUG: cutoff_dt     = {cutoff_dt}")
    print(f"DEBUG: cutoff_fmt   = {cutoff_fmt}")


   # Conteo inicial (filtrado por summoner)
    c.execute(
        "SELECT COUNT(*) FROM match_events me JOIN matches m ON me.match_id=m.match_id "
        "WHERE me.event='derrota' AND m.game_creation>=? AND me.summoner_name=?", 
        (cutoff_fmt, summoner)
    )
    defeats = c.fetchone()[0]
    c.execute(
        "SELECT COUNT(*) FROM match_events me JOIN matches m ON me.match_id=m.match_id "
        "WHERE me.event='victoria' AND m.game_creation>=? AND me.summoner_name=?", 
        (cutoff_fmt, summoner)
    )
    victories = c.fetchone()[0]

    # Procesa e inserta partidas nuevas
    for mid in fetch_recent_matches(puuid):
        # No procesar partidas ya registradas
        c.execute("SELECT 1 FROM matches WHERE match_id=? AND summoner_name=?", (mid, summoner))
        if c.fetchone():
            continue
        rec = process_match(mid, puuid, summoner, conn, c)
        if rec is None or rec['game_creation_str'] < cutoff_fmt:
            continue
        # Inserción evitando duplicados gracias a UNIQUE
        c.execute(
            "INSERT INTO matches(match_id,queue_id,end_timestamp,game_creation,summoner_name) VALUES(?,?,?,?,?)",
            (rec['match_id'], rec['queue_id'], rec['end_timestamp_str'], rec['game_creation_str'], rec['summoner_name'])
        )
        for evt in rec['events']:
            c.execute(
                "INSERT OR IGNORE INTO match_events(match_id,event,summoner_name) VALUES(?,?,?)",
                (rec['match_id'], evt, rec['summoner_name'])
            )
            update_streak(conn, c, rec['raw_creation'], evt == 'victoria')
        conn.commit()
        if 'derrota' in rec['events']:
            defeats += 1
            if defeats >= DAILY_DEF_LIMIT:
                break
        else:
            victories += 1

    dyn_points = calculate_dynamic_points(conn, c, cutoff_fmt, summoner)
    plan = calculate_plan(defeats, dyn_points)  # NO BORRAR: Plan de ejercicios

    return {
        "derrotas": defeats,
        "victorias": victories,
        "puntos": dyn_points,
        "puntos_restantes": plan["puntos_restantes"],
        "plan_ejercicio": plan["ejercicios"]
    }

# --- Iniciar scheduler de bancar streak ---
threading.Thread(
    target=lambda: (schedule.every().day.at("00:00").do(daily_bank_job), schedule.run_pending()),
    daemon=True
).start()