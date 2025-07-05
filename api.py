from calendar import c
import os
import threading  # Para hilos de scheduler
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
import sqlite3
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo  # Para manejo de zona horaria
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
ALLOWED_QUEUES = {400, 420, 440}  # Colas permitidas: Normal, Solo/Dúo, Flex, Normal (Quickplay)
RECENT_MATCH_COUNT = 20           # Cantidad de partidas a recuperar
CHILE_TZ = ZoneInfo("America/Santiago")  # Zona horaria de Chile

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

def generate_base_plan(defeats: int) -> list[dict]:
    """
    Devuelve el plan completo en función de defeat (derrotas):
      reps_base = reps_por_defeat × defeats
    """
    return [
        {"nombre": name, "reps": reps * defeats}
        for name, reps in FULL_WORKOUT
    ]


# --- Inicialización de FastAPI y RiotWatcher ---
app = FastAPI(
    title="LoL Tracker API",
    version="1.6.7",
    description="Procesa partidas con cache local, streaks, plan de ejercicios y zona horaria Chile"
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
CREATE TABLE IF NOT EXISTS user_points (
  summoner_name TEXT PRIMARY KEY,
  total_points  INTEGER NOT NULL DEFAULT 0,
  last_accumulated_date TEXT DEFAULT ''
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
    end_dt = datetime.fromtimestamp(raw_end/1000, CHILE_TZ)
    creation_dt = datetime.fromtimestamp(raw_creation/1000, CHILE_TZ)
    end_str = end_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    creation_str = creation_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    
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
def calculate_dynamic_points(conn, c, cutoff_dt: datetime, summoner_name: str) -> int:
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
        (cutoff_dt.strftime("%Y-%m-%d"), summoner_name)
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

    if streak > 0:
        points += streak * p_per_victory

    return points

# Marca streak bancado al fin del día
def daily_bank_job():
    conn, c = get_db()
    date_str = datetime.now(CHILE_TZ).strftime('%Y-%m-%d')
    from api import mark_streak_banked
    mark_streak_banked(conn, c, date_str)
    
# --- Endpoint principal ---
@app.post("/procesar-partidas/")
def procesar_partidas(id: RiotID):
    conn, c = get_db()
    puuid = get_puuid(id.game_name, id.tag_line)
    summoner = id.game_name  # guardamos solo nombre, no tag_line


    # DEBUG: entrada al endpoint
    print(f"[DEBUG] Llamada a /procesar-partidas/ para {id.game_name}#{id.tag_line} en {datetime.now(CHILE_TZ)}")


    # Obtener PUUID
    try:
        puuid = get_puuid(id.game_name, id.tag_line)
        print(f"[DEBUG] PUUID obtenido: {puuid}")
    except Exception as e:
        print(f"[ERROR] get_puuid falló: {e}")
        raise
    summoner = id.game_name


   # Corte de día local
    local_now = datetime.now(CHILE_TZ)
    cutoff_dt = local_now.replace(hour=0, minute=0, second=0, microsecond=0)

    print(f"[DEBUG] cutoff (ms):{cutoff_dt}")

  
   # Conteo inicial (filtrado por summoner y game_creation)
    c.execute(
        "SELECT COUNT(*) FROM match_events me JOIN matches m ON me.match_id=m.match_id "
        "WHERE me.event='derrota' AND m.game_creation>=? AND me.summoner_name=?", 
        (cutoff_dt.strftime("%Y-%m-%d"), summoner)
    )
    defeats = c.fetchone()[0]
    c.execute(
        "SELECT COUNT(*) FROM match_events me JOIN matches m ON me.match_id=m.match_id "
        "WHERE me.event='victoria' AND m.game_creation>=? AND me.summoner_name=?", 
        (cutoff_dt.strftime("%Y-%m-%d"), summoner)
    )
    victories = c.fetchone()[0]
    print(f"[DEBUG] Derrotas hoy: {defeats}, Victorias hoy: {victories}")

    # Procesa e inserta partidas nuevas
    processed = []
    for mid in fetch_recent_matches(puuid):
        print(f"[DEBUG] Procesando match {mid}") # Debug
        # No procesar partidas ya registradas
        c.execute("SELECT 1 FROM matches WHERE match_id=? AND summoner_name=?", (mid, summoner))
        if c.fetchone():
            continue
        rec = process_match(mid, puuid, summoner, conn, c)
        if not rec or rec["raw_creation"] < int(cutoff_dt.timestamp()*1000):
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
        processed.append(rec)
        if evt == 'derrota':
            defeats += 1
            if defeats >= DAILY_DEF_LIMIT:
                break
        else:
            victories += 1


    # DEBUG: partidas nuevas procesadas
    print(f"[DEBUG] Partidas nuevas procesadas: {len(processed)} -> {[r['match_id'] for r in processed]}")

    # Calcula puntos dinámicos del día
    daily_points  = calculate_dynamic_points(conn, c, cutoff_dt, summoner)

    # fecha de hoy para saber si ya acumulamos en esta fecha
    today_str = cutoff_dt.strftime("%Y-%m-%d")

    # timestamp completo para el registro con hora real
    timestamp_str = local_now.strftime("%Y-%m-%d %H:%M:%S")

    # lee total y última fecha de acumulación
    c.execute("""
      SELECT total_points, last_accumulated_date
      FROM user_points
      WHERE summoner_name = ?
    """, (summoner,))
    row = c.fetchone()

    prev_total, last_date = (row if row else (0, ""))

    # sólo sumamos si no se hizo hoy
    if last_date.split(" ")[0] != today_str:
        new_total = prev_total + daily_points
        last_accumulated = timestamp_str
    else:
        new_total = prev_total
        last_accumulated = last_date

   
    # guardamos con la hora real
    c.execute("""
      INSERT INTO user_points(summoner_name, total_points, last_accumulated_date)
      VALUES (?, ?, ?)
      ON CONFLICT(summoner_name) DO UPDATE
        SET total_points = excluded.total_points,
            last_accumulated_date = excluded.last_accumulated_date
    """, (summoner, new_total, last_accumulated))
    conn.commit()

    #Genera plan de ejercicios
    plan_base = generate_base_plan(defeats)

    #Devuelve JSON con toda la info, incluyendo puntos totales
    return {
        "derrotas":        defeats,
        "victorias":       victories,
        "puntos_diarios":  daily_points,
        "puntos_totales":  new_total,
        "plan_base":       plan_base,
    }


# Modelos de solicitud y respuesta
class PointsRequest(BaseModel):
    summoner_name: str
    points: int

class PointsResponse(BaseModel):
    total_points: int

# Endpoint para gastar puntos (+)
@app.post("/gastar-puntos/", response_model=PointsResponse)
def spend_points(req: PointsRequest):
    conn, c = get_db()
    if req.points <= 0:
        raise HTTPException(status_code=400, detail="Points to spend must be positive")

    # Leer saldo actual
    c.execute(
        "SELECT total_points FROM user_points WHERE summoner_name = ?", 
        (req.summoner_name,)
    )
    row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Summoner not found")

    current = row[0]
    if req.points > current:
        raise HTTPException(status_code=400, detail="Not enough points to spend")

    # Actualizar saldo
    new_total = current - req.points
    c.execute(
        "UPDATE user_points SET total_points = ? WHERE summoner_name = ?",
        (new_total, req.summoner_name)
    )
    conn.commit()

    return PointsResponse(total_points=new_total)


# Endpoint para reembolsar puntos (-)
@app.post("/reembolsar-puntos/", response_model=PointsResponse)
def refund_points(req: PointsRequest):
    conn, c = get_db()
    if req.points <= 0:
        raise HTTPException(status_code=400, detail="Points to refund must be positive")

    c.execute(
        "SELECT total_points FROM user_points WHERE summoner_name = ?", 
        (req.summoner_name,)
    )
    row = c.fetchone()
    if not row:
        # Si no existe, podemos crear registro con puntos reembolsados o error
        raise HTTPException(status_code=404, detail="Summoner not found")

    current = row[0]
    new_total = current + req.points
    c.execute(
        "UPDATE user_points SET total_points = ? WHERE summoner_name = ?",
        (new_total, req.summoner_name)
    )
    conn.commit()

    return PointsResponse(total_points=new_total)

# --- Iniciar scheduler de bancar streak ---
threading.Thread(
    target=lambda: (schedule.every().day.at("00:00").do(daily_bank_job), schedule.run_pending()),
    daemon=True
).start()