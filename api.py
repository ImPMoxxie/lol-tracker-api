import os
import requests
import time
import schedule
import sqlite3
from datetime import datetime, timedelta
from dotenv import load_dotenv

# —————————————
# Carga de .env
# —————————————
load_dotenv()

# —————————————
# Configuración
# —————————————
API_KEY = os.getenv("RIOT_API_KEY")
if not API_KEY:
    print("❌ Error: RIOT_API_KEY no definida en el entorno.")
    exit(1)

REGIONAL = "americas"
DB_FILE = "lol_trackedb.db"
DAILY_DEF_LIMIT = 5          # Límite de DERROTAS por día
POINTS_PER_VICTORY = 5       # Puntos otorgados por victoria

# —————————————
# Inicialización de SQLite
# —————————————
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
c = conn.cursor()

# Crear tablas si no existen
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

# —————————————
# Funciones API
# —————————————
def get_puuid(game_name: str, tag_line: str) -> str:
    url = (
        f"https://{REGIONAL}.api.riotgames.com"
        f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}?api_key={API_KEY}"
    )
    resp = requests.get(url)
    resp.raise_for_status()
    return resp.json()["puuid"]


def fetch_recent_matches(puuid: str, count: int = 5) -> list:
    url = (
        f"https://{REGIONAL}.api.riotgames.com"
        f"/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count={count}"
    )
    resp = requests.get(url, headers={"X-Riot-Token": API_KEY})
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

    events = []
    # Victoria o derrota
    if participant.get("win", False):
        events.append("victoria")
    else:
        events.append("derrota")

    # Timestamps legibles
    start_ts = info.get("gameStartTimestamp", 0)
    end_ts = info.get("gameEndTimestamp", 0)
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
# Persistencia
# —————————————
def get_done_ids() -> set:
    c.execute("SELECT match_id FROM matches")
    return {row[0] for row in c.fetchall()}


def save_match(record: dict):
    if not record:
        return
    conn.execute("BEGIN")
    c.execute(
        "INSERT OR IGNORE INTO matches(match_id, start_timestamp, end_timestamp, start_time, end_time) VALUES (?,?,?,?,?)",
        (record["match_id"], record["start_timestamp"], record["end_timestamp"], record["start_time"], record["end_time"])
    )
    for evt in record["events"]:
        c.execute(
            "INSERT INTO match_events(match_id, event) VALUES (?,?)",
            (record["match_id"], evt)
        )
    conn.commit()

# —————————————
# Cálculos de puntos y ejercicios
# —————————————
FULL_WORKOUT = [
    ("Sentadillas", 40),
    ("Zancadas (lunges)", 20),
    ("Flexiones convencionales", 20),
    ("Flexiones con palmas juntas", 10),
    ("Curl isométrico con toalla", 15),
    ("Superman (lumbar)", 15),
    ("Plancha frontal (plank)", 60),
    ("Crunches", 30),
    ("Jumping Jacks", 30),
    ("Saltos de sentadilla", 20)
]


def calculate_and_print_exercises(today_cutoff, victories, defeats):
    # Puntos disponibles
    points = victories * POINTS_PER_VICTORY
    print(f"\nPuntos por victorias: {points} (\u2605 = {POINTS_PER_VICTORY} pts/victoria)")

    if defeats == 0:
        if victories > 0:
            print("\nSolo victorias hoy: ¡toma un vaso de agua!")
        else:
            print("\nNo hay ejercicio asignado.")
        return

    # Generar lista inicial de ejercicios segun derrotas
    reps = [(name, count * defeats) for name, count in FULL_WORKOUT]
    print(f"\nRepeticiones básicas tras {defeats} derrotas:")
    for n, r in reps:
        print(f"- {r} {n}")

    # Aplicar puntos para reducir o quitar ejercicios
    optimized = []
    for name, r in reps:
        if points <= 0:
            optimized.append((name, r))
            continue
        if points >= r:
            print(f"\nHas usado {r} puntos para eliminar '{name}'")
            points -= r
        else:
            new_r = r - points
            print(f"\nHas usado {points} puntos en '{name}', nuevas reps: {new_r}")
            optimized.append((name, new_r))
            points = 0

    # Mostrar resultados finales
    print("\nEjercicios finales:")
    for n, r in optimized:
        print(f"- {r} {n}")

# —————————————
# Flujo principal
# —————————————
def main_flow():
    riot_id = input("Ingresa tu Riot ID (Nombre#TAG): ").strip()
    try:
        game_name, tag_line = riot_id.split("#")
    except ValueError:
        print("Formato inválido.")
        return

    puuid = get_puuid(game_name, tag_line)

    # Conteo de victorias/derrotas hoy
    today_cutoff = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()*1000)
    c.execute("SELECT COUNT(*) FROM match_events me JOIN matches m ON me.match_id=m.match_id WHERE me.event='victoria' AND m.end_timestamp>=?", (today_cutoff,))
    victories = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM match_events me JOIN matches m ON me.match_id=m.match_id WHERE me.event='derrota' AND m.end_timestamp>=?", (today_cutoff,))
    defeats = c.fetchone()[0]

    print(f"Hoy: {defeats} derrotas (límite {DAILY_DEF_LIMIT}), {victories} victorias")
    if defeats >= DAILY_DEF_LIMIT:
        print("Has alcanzado el límite de derrotas diarias. No se procesarán más.")
        calculate_and_print_exercises(today_cutoff, victories, defeats)
        return

    recent_ids = fetch_recent_matches(puuid)
    done_ids = get_done_ids()
    new_ids = [mid for mid in recent_ids if mid not in done_ids]

    if not new_ids:
        print("No hay partidas nuevas.")
        calculate_and_print_exercises(today_cutoff, victories, defeats)
        return

    for mid in new_ids:
        record = process_match(mid, puuid)
        if not record:
            continue
        if record["end_timestamp"] < today_cutoff:
            continue
        save_match(record)
        if "derrota" in record["events"]:
            defeats += 1
            print(f"Nueva derrota registrada: hoy {defeats}/{DAILY_DEF_LIMIT}")
        else:
            victories += 1
            print("¡Nueva victoria registrada! +{POINTS_PER_VICTORY} puntos")
        if defeats >= DAILY_DEF_LIMIT:
            print("Límite de derrotas alcanzado durante el procesamiento.")
            break

    calculate_and_print_exercises(today_cutoff, victories, defeats)

# —————————————
# Scheduler
# —————————————
if __name__ == "__main__":
    main_flow()
    schedule.every(15).minutes.do(main_flow)
    while True:
        schedule.run_pending()
        time.sleep(1)
