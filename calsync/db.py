"""
Base de datos local (SQLite). Un solo archivo en ~/.calsync/calsync.db

Guarda cuatro cosas:
  - accounts       : las cuentas conectadas y su permiso de Google
  - source_events  : copia local de los eventos leídos, para comparar cambios
  - mirrors        : qué espejo corresponde a qué evento original
  - settings       : la configuración que se edita en el dashboard
  - oplog          : registro de todo lo que el motor hizo o pensó hacer
"""

import json
import sqlite3
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY,
    email       TEXT    NOT NULL UNIQUE,
    label       TEXT    NOT NULL,
    token_json  TEXT    NOT NULL,
    sync_token  TEXT,
    last_full_sync TEXT,
    last_sync   TEXT,
    last_error  TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS source_events (
    account_id   INTEGER NOT NULL,
    event_id     TEXT    NOT NULL,
    summary      TEXT,
    start_raw    TEXT    NOT NULL,
    end_raw      TEXT    NOT NULL,
    start_utc    TEXT,
    end_utc      TEXT,
    all_day      INTEGER NOT NULL DEFAULT 0,
    transparency TEXT,
    status       TEXT,
    my_response  TEXT,
    has_guests   INTEGER NOT NULL DEFAULT 0,
    is_mirror    INTEGER NOT NULL DEFAULT 0,
    updated      TEXT,
    location     TEXT,
    description  TEXT,
    meet_link    TEXT,
    guests_list  TEXT,
    organizer    TEXT,
    PRIMARY KEY (account_id, event_id)
);

CREATE TABLE IF NOT EXISTS mirrors (
    source_account_id INTEGER NOT NULL,
    source_event_id   TEXT    NOT NULL,
    target_account_id INTEGER NOT NULL,
    target_event_id   TEXT    NOT NULL,
    payload_hash      TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL,
    PRIMARY KEY (source_account_id, source_event_id, target_account_id)
);

CREATE TABLE IF NOT EXISTS habits (
    id              INTEGER PRIMARY KEY,
    name            TEXT    NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    kind            TEXT    NOT NULL DEFAULT 'flexible',
    ideal_minutes   INTEGER NOT NULL,
    min_minutes     INTEGER NOT NULL,
    window_start    TEXT    NOT NULL,
    window_end      TEXT    NOT NULL,
    preferred_start TEXT,
    days            TEXT    NOT NULL DEFAULT '0,1,2,3,4',
    per_week        INTEGER NOT NULL DEFAULT 0,
    share           INTEGER NOT NULL DEFAULT 1,
    priority        INTEGER NOT NULL DEFAULT 100,
    color_id        TEXT    NOT NULL DEFAULT '',
    home_account_id INTEGER,
    cleanup_past    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL
);

-- Los días en que borraste el bloque a mano desde la cuenta de origen.
-- Sin esto, el siguiente ciclo lo volvería a crear y sentirías que no te
-- hace caso. Se limpia al guardar los ajustes del hábito.
CREATE TABLE IF NOT EXISTS habit_skips (
    habit_id INTEGER NOT NULL,
    day      TEXT    NOT NULL,
    PRIMARY KEY (habit_id, day)
);

-- La decisión de "este hábito va este día a esta hora". Se guarda para que un
-- bloque no salte de sitio cada tres minutos: solo se recalcula si dejó de caber.
-- `by_hand` = esta hora la puso David arrastrando el bloque, no el motor.
-- Un sitio elegido a mano se respeta aunque quede fuera de la ventana; uno
-- elegido por el motor se revisa contra los ajustes de hoy en cada ciclo.
CREATE TABLE IF NOT EXISTS habit_slots (
    habit_id    INTEGER NOT NULL,
    day         TEXT    NOT NULL,
    start_local TEXT    NOT NULL,
    minutes     INTEGER NOT NULL,
    by_hand     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (habit_id, day)
);

-- Qué evento de Google corresponde a qué bloque.
--
-- `wrote_start` y `wrote_minutes` son la hora que CalSync ESCRIBIÓ de verdad en
-- ese evento, que no es lo mismo que la hora que el plan quiere ahora. La
-- diferencia importa: es la única forma de distinguir «David movió el bloque a
-- mano» de «CalSync todavía no ha alcanzado a aplicar su propio cambio».
CREATE TABLE IF NOT EXISTS habit_blocks (
    habit_id      INTEGER NOT NULL,
    day           TEXT    NOT NULL,
    account_id    INTEGER NOT NULL,
    event_id      TEXT    NOT NULL,
    payload_hash  TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    wrote_start   TEXT,
    wrote_minutes INTEGER,
    PRIMARY KEY (habit_id, day, account_id)
);

-- Los eventos que decidiste ignorar desde un conflicto del dashboard.
--
-- Ignorar hace dos cosas a la vez: el evento deja de quitarte tiempo cuando
-- CalSync busca hueco para un bloque, y deja de reflejarse como ocupado en tus
-- otras cuentas. `summary` y `start_utc` se guardan aquí mismo para poder
-- enseñarte la lista aunque el evento ya no exista en Google.
CREATE TABLE IF NOT EXISTS ignored_events (
    account_id INTEGER NOT NULL,
    event_id   TEXT    NOT NULL,
    summary    TEXT,
    account    TEXT,
    start_utc  TEXT,
    created_at TEXT    NOT NULL,
    PRIMARY KEY (account_id, event_id)
);

-- Los avisos de «estas dos reuniones chocan» que ya viste.
--
-- Esto NO toca la sincronización: solo esconde la fila. La pareja se guarda
-- siempre en el mismo orden (ver `habits.clave_par`), porque si no, cambiar la
-- hora de una de las dos invierte cuál va primero y el aviso reaparece solo.
CREATE TABLE IF NOT EXISTS dismissed_overlaps (
    a_account  INTEGER NOT NULL,
    a_event    TEXT    NOT NULL,
    b_account  INTEGER NOT NULL,
    b_event    TEXT    NOT NULL,
    a_title    TEXT,
    b_title    TEXT,
    created_at TEXT    NOT NULL,
    PRIMARY KEY (a_account, a_event, b_account, b_event)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS oplog (
    id     INTEGER PRIMARY KEY,
    ts     TEXT NOT NULL,
    level  TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_start ON source_events(start_utc);
CREATE INDEX IF NOT EXISTS idx_oplog_ts ON oplog(ts DESC);
"""

# Configuración inicial. Deliberadamente conservadora: arranca en modo
# simulación, sin escribir nada en ningún calendario.
DEFAULT_SETTINGS = {
    "simulation": True,
    "rules": {
        "skip_transparent": True,   # ignorar eventos marcados "Disponible"
        "skip_all_day": True,       # ignorar eventos de día completo
        "skip_declined": True,      # ignorar invitaciones rechazadas
        "skip_needs_action": False, # ignorar invitaciones sin responder
        # Un evento que cruza la medianoche, o que dura más de `long_hours`,
        # no es una reunión: casi siempre es el OOO de otra persona creado con
        # horas en vez de como evento de día completo. Doce horas deja pasar un
        # offsite de verdad de 9 a 6, que sí debe bloquear tu tiempo.
        "skip_long": True,
        "long_hours": 12,
        "skip_tags": ["[nosync]"],  # tags que excluyen un evento
        "skip_keywords": [],        # palabras clave que excluyen un evento
    },
    # La palabra para los bloques de ocupado. En inglés por defecto, porque
    # Ruca y Galactic trabajan en inglés.
    "busy_word": "Busy",
    # Color de los espejos. Gris a propósito: son ruido de fondo y deben
    # desaparecer visualmente para que resalten tus reuniones y tus bloques.
    "mirror_color": "8",
    # La cuenta donde nacen los bloques de hábitos. Si mueves o borras un bloque
    # a mano ahí, CalSync te hace caso y lo propaga a las copias.
    # None = se elige sola la cuenta personal (la de Gmail).
    # Cada hábito puede tener la suya propia y esa manda sobre esta.
    "home_account_id": None,
    # --- Los textos que leen los demás -------------------------------------
    # Van en inglés porque quien los lee son los colegas de Ruca y Galactic.
    # Solo se ven cuando la dirección está marcada como visible al equipo: en
    # un evento privado, Google no muestra la descripción a nadie más.
    "mirror_note": (
        "Blocked — commitment in another calendar.\n\n"
        "Kept in sync automatically. Please schedule around it."
    ),
    "habit_note": (
        "Recurring personal block, kept in sync across calendars.\n\n"
        "If you need this time, message me instead of booking over it."
    ),
    # Si el calendario del dashboard muestra también tus reuniones reales.
    # Es una preferencia tuya, no de esa visita: la herramienta abre como la
    # dejaste. Vive aquí y no en el navegador para que sobreviva a limpiar los
    # datos del sitio.
    "show_meetings": True,
    # Qué cuenta bloquea en qué cuenta. Clave "origen|destino" -> bool.
    # Se llena automáticamente al conectar cuentas (todo en true).
    "pairs": {},
    # Qué muestra el espejo, POR PAR de calendarios. Clave "origen|destino":
    #   {"mode": "busy"}                        → "Busy (Galactic)"
    #   {"mode": "title"}                       → título real + lugar + link
    #   {"mode": "custom", "label": "Work"}     → "Work"
    # Es por par y no por origen porque el detalle que quieres ver depende de
    # dónde lo estés mirando: los eventos de Galactic con todo el detalle en
    # Ruca, pero como "Work" en el calendario personal.
    #
    # Cada par lleva además `share`:
    #   False (por defecto) → visibility "private": tus colegas ven solo "Busy"
    #   True                → visibility "default": tus colegas ven el detalle,
    #                         como en cualquier evento normal de esa cuenta
    "detail_pairs": {},
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def a_local(iso: str, formato="%H:%M:%S") -> str:
    """Una marca de tiempo guardada en UTC, mostrada en tu hora.

    Todo se guarda en UTC a propósito —es lo único que no se rompe con husos y
    horarios de verano— pero nada de eso debería llegarte a la pantalla: un
    registro en una hora que no es la tuya es un registro que no se puede leer.
    """
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone(config.TZ).strftime(formato)
    except (ValueError, TypeError):
        return str(iso)[:19]


def connect() -> sqlite3.Connection:
    """Abre la base de datos, creándola si no existe."""
    config.APP_DIR.mkdir(parents=True, exist_ok=True)
    # 0o700 = solo tu usuario puede entrar a la carpeta
    config.APP_DIR.chmod(0o700)
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate(conn)
    return conn


def _migrate(conn):
    """Pone al día una base de datos creada por una versión anterior.

    Se corre en cada arranque y no hace nada si ya está al día, así que no
    hay que acordarse de ejecutarla a mano.
    """
    columnas = {r["name"] for r in conn.execute("PRAGMA table_info(source_events)")}
    for nueva in ("location", "description", "meet_link", "guests_list", "organizer"):
        if nueva not in columnas:
            conn.execute(f"ALTER TABLE source_events ADD COLUMN {nueva} TEXT")

    de_habitos = {r["name"] for r in conn.execute("PRAGMA table_info(habits)")}
    if "home_account_id" not in de_habitos:
        conn.execute("ALTER TABLE habits ADD COLUMN home_account_id INTEGER")
    if "cleanup_past" not in de_habitos:
        conn.execute(
            "ALTER TABLE habits ADD COLUMN cleanup_past INTEGER NOT NULL DEFAULT 0"
        )
    if "color_id" not in de_habitos:
        conn.execute("ALTER TABLE habits ADD COLUMN color_id TEXT NOT NULL DEFAULT ''")
        # A los hábitos que ya existen se les pone el color que les corresponde.
        for nombre, color in (("Lunch", "5"), ("Reading", "1"), ("Exercise", "10"),
                              ("Not available", "8"), ("Explore", "6")):
            conn.execute("UPDATE habits SET color_id = ? WHERE name LIKE ?",
                         (color, f"%{nombre}%"))

    de_slots = {r["name"] for r in conn.execute("PRAGMA table_info(habit_slots)")}
    if "by_hand" not in de_slots:
        conn.execute("ALTER TABLE habit_slots ADD COLUMN by_hand INTEGER NOT NULL DEFAULT 0")

    de_bloques = {r["name"] for r in conn.execute("PRAGMA table_info(habit_blocks)")}
    if "wrote_start" not in de_bloques:
        conn.execute("ALTER TABLE habit_blocks ADD COLUMN wrote_start TEXT")
        conn.execute("ALTER TABLE habit_blocks ADD COLUMN wrote_minutes INTEGER")

    # El nivel de detalle pasó de ser por origen a ser por par origen|destino.
    viejo = conn.execute("SELECT value FROM settings WHERE key = 'detail'").fetchone()
    nuevo = conn.execute("SELECT 1 FROM settings WHERE key = 'detail_pairs'").fetchone()
    if viejo and not nuevo:
        por_origen = json.loads(viejo["value"])
        ids = [str(r["id"]) for r in conn.execute("SELECT id FROM accounts")]
        convertido = {
            f"{s}|{t}": {"mode": por_origen.get(s, "busy"), "label": ""}
            for s in ids for t in ids if s != t
        }
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('detail_pairs', ?)",
            (json.dumps(convertido),),
        )
        conn.execute("DELETE FROM settings WHERE key = 'detail'")
    conn.commit()
    _sembrar_habitos(conn)


# Los hábitos que David describió, listos para usar y editables en el dashboard.
# `kind` "fixed" = siempre a la misma hora, protege el tiempo aunque haya algo
# encima. "flexible" = busca hueco y se recorta antes que desaparecer.
HABITOS_INICIALES = [
    # nombre, kind, ideal, mín, ventana, preferida, días, x semana, prioridad, color
    ("🍽 Lunch",         "flexible", 90, 60, "12:00", "15:00", "13:00", "0,1,2,3,4", 0, 10, "5"),
    ("📚 Reading",       "flexible", 30, 30, "06:00", "09:00", None,    "0,1,2,3,4", 0, 20, "1"),
    ("🏋 Exercise",      "flexible", 60, 45, "06:00", "20:00", None,    "0,1,2,3,4", 3, 30, "10"),
    ("🌙 Not available", "fixed",   180, 180, "17:00", "20:00", "17:00", "0,1,2,3,4", 0, 90, "8"),
]


def _sembrar_habitos(conn):
    """Crea los hábitos iniciales una sola vez, la primera vez que se abre la
    base. Después manda siempre lo que David tenga configurado."""
    if conn.execute("SELECT value FROM settings WHERE key='habits_seeded'").fetchone():
        return
    for (nombre, kind, ideal, minimo, w0, w1, pref, dias, semana, prio, color) in HABITOS_INICIALES:
        conn.execute(
            "INSERT INTO habits (name, kind, ideal_minutes, min_minutes, window_start, "
            "window_end, preferred_start, days, per_week, share, priority, color_id, "
            "created_at, enabled) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 1)",
            (nombre, kind, ideal, minimo, w0, w1, pref, dias, semana, prio, color, now_iso()),
        )
    # El de viaje llega apagado: se enciende cuando David está de viaje.
    conn.execute(
        "INSERT INTO habits (name, kind, ideal_minutes, min_minutes, window_start, "
        "window_end, preferred_start, days, per_week, share, priority, color_id, "
        "created_at, enabled) VALUES ('✈️ Explore', 'flexible', 120, 60, '09:00', "
        "'19:00', NULL, '0,1,2,3,4,5,6', 0, 1, 40, '6', ?, 0)",
        (now_iso(),),
    )
    conn.execute("INSERT INTO settings (key, value) VALUES ('habits_seeded', 'true')")
    conn.commit()


def habits(conn, only_enabled=False):
    sql = "SELECT * FROM habits"
    if only_enabled:
        sql += " WHERE enabled = 1"
    return list(conn.execute(sql + " ORDER BY priority, id"))


def cuenta_origen(conn, habito=None):
    """La cuenta donde nace un bloque de hábito.

    Manda lo más específico: primero la cuenta elegida para ese hábito, después
    la general, y si no hay ninguna se toma la cuenta personal. Los hábitos son
    tiempo tuyo, no de una organización, así que su original no debería vivir
    dentro del Workspace de una empresa.
    """
    activas = accounts(conn)
    if not activas:
        return None

    propia = None
    if habito is not None:
        try:
            propia = habito["home_account_id"]
        except (IndexError, KeyError):
            propia = None
    if propia is not None:
        for cuenta in activas:
            if cuenta["id"] == propia:
                return cuenta

    elegida = get_setting(conn, "home_account_id")
    if elegida is not None:
        for cuenta in activas:
            if cuenta["id"] == elegida:
                return cuenta

    personales = ("@gmail.com", "@googlemail.com")
    for cuenta in activas:
        if cuenta["email"].lower().endswith(personales):
            return cuenta
    return activas[0]


# --- Configuración -----------------------------------------------------------

def _con_defectos(key, guardado):
    """Completa un ajuste guardado con las claves que aún no tenía.

    `rules` se guarda como un diccionario entero. Sin esto, una opción añadida
    después nacía apagada para quien ya tenía ajustes guardados —lo guardado
    reemplazaba al valor por defecto completo— y la opción parecía no hacer
    nada hasta que guardabas la pantalla una vez. Esto es la migración: no
    hace falta tocar la base.
    """
    base = DEFAULT_SETTINGS.get(key)
    if isinstance(base, dict) and isinstance(guardado, dict):
        return {**base, **guardado}
    return guardado


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return DEFAULT_SETTINGS.get(key, default)
    return _con_defectos(key, json.loads(row["value"]))


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )
    conn.commit()


def all_settings(conn) -> dict:
    out = dict(DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value FROM settings"):
        out[row["key"]] = _con_defectos(row["key"], json.loads(row["value"]))
    return out


# --- Lo que silenciaste ------------------------------------------------------
#
# Dos listas distintas a propósito. Ignorar un evento cambia lo que CalSync
# hace; descartar un aviso de choque solo cambia lo que te enseña. Meterlas en
# la misma tabla habría borrado esa diferencia, que es justo la que hay que
# tener clara antes de pulsar un botón.

def ignorar_evento(conn, account_id, event_id, summary, account, start_utc):
    conn.execute(
        "INSERT OR IGNORE INTO ignored_events "
        "(account_id, event_id, summary, account, start_utc, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (account_id, event_id, summary, account, start_utc, now_iso()),
    )
    conn.commit()


def dejar_de_ignorar(conn, account_id, event_id):
    conn.execute("DELETE FROM ignored_events WHERE account_id = ? AND event_id = ?",
                 (account_id, event_id))
    conn.commit()


def eventos_ignorados(conn):
    return list(conn.execute("SELECT * FROM ignored_events ORDER BY start_utc, summary"))


def descartar_choque(conn, par, a_title, b_title):
    (a_cuenta, a_evt), (b_cuenta, b_evt) = par
    conn.execute(
        "INSERT OR IGNORE INTO dismissed_overlaps "
        "(a_account, a_event, b_account, b_event, a_title, b_title, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (a_cuenta, a_evt, b_cuenta, b_evt, a_title, b_title, now_iso()),
    )
    conn.commit()


def recuperar_choque(conn, par):
    (a_cuenta, a_evt), (b_cuenta, b_evt) = par
    conn.execute(
        "DELETE FROM dismissed_overlaps WHERE a_account = ? AND a_event = ? "
        "AND b_account = ? AND b_event = ?",
        (a_cuenta, a_evt, b_cuenta, b_evt),
    )
    conn.commit()


def choques_descartados(conn):
    return list(conn.execute("SELECT * FROM dismissed_overlaps ORDER BY created_at"))


def limpiar_silenciados(conn) -> int:
    """Olvida lo silenciado cuyo evento ya no existe Y cuya fecha ya pasó.

    Las dos condiciones, no una. La base solo guarda una ventana de siete días
    atrás y treinta adelante, así que «no está en source_events» no significa
    «no existe»: puede ser que todavía no haya entrado en la ventana. Sin la
    segunda condición, un evento ignorado a tiempo volvería a estorbar justo
    cuando se acerca.

    Un evento futuro que alguien borró se queda en la lista hasta que su fecha
    pase. No molesta a nadie y lo ves en «Muted by you», que es mejor que
    desaparecer en silencio.
    """
    ahora = now_iso()
    cur = conn.execute(
        "DELETE FROM ignored_events WHERE start_utc IS NOT NULL AND start_utc < ? "
        "AND NOT EXISTS (SELECT 1 FROM source_events e "
        "                WHERE e.account_id = ignored_events.account_id "
        "                  AND e.event_id = ignored_events.event_id)",
        (ahora,),
    )
    borrados = cur.rowcount
    cur = conn.execute(
        "DELETE FROM dismissed_overlaps WHERE NOT EXISTS ("
        "  SELECT 1 FROM source_events e WHERE e.account_id = dismissed_overlaps.a_account "
        "    AND e.event_id = dismissed_overlaps.a_event)",
    )
    borrados += cur.rowcount
    conn.commit()
    return borrados


# --- Cuentas -----------------------------------------------------------------

def accounts(conn, only_enabled=True):
    sql = "SELECT * FROM accounts"
    if only_enabled:
        sql += " WHERE enabled = 1"
    return list(conn.execute(sql + " ORDER BY id"))


def account(conn, account_id):
    return conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()


def upsert_account(conn, email, label, token_json) -> int:
    conn.execute(
        "INSERT INTO accounts (email, label, token_json, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(email) DO UPDATE SET label = excluded.label, "
        "token_json = excluded.token_json, enabled = 1, last_error = NULL",
        (email, label, token_json, now_iso()),
    )
    conn.commit()
    return conn.execute("SELECT id FROM accounts WHERE email = ?", (email,)).fetchone()["id"]


def save_token(conn, account_id, token_json):
    conn.execute("UPDATE accounts SET token_json = ? WHERE id = ?", (token_json, account_id))
    conn.commit()


def ensure_pairs(conn):
    """Al conectar una cuenta nueva, activa por defecto la sincronización en
    ambas direcciones con todas las demás, mostrando solo "Busy"."""
    settings = all_settings(conn)
    pairs = dict(settings["pairs"])
    detail = dict(settings["detail_pairs"])
    ids = [str(a["id"]) for a in accounts(conn)]
    for src in ids:
        for tgt in ids:
            if src == tgt:
                continue
            pairs.setdefault(f"{src}|{tgt}", True)
            detail.setdefault(f"{src}|{tgt}", {"mode": "busy", "label": "", "share": False})
    set_setting(conn, "pairs", pairs)
    set_setting(conn, "detail_pairs", detail)


# --- Registro ----------------------------------------------------------------

def log(conn, action, detail="", level="info"):
    conn.execute(
        "INSERT INTO oplog (ts, level, action, detail) VALUES (?, ?, ?, ?)",
        (now_iso(), level, action, str(detail)[:2000]),
    )
    conn.commit()


def recent_log(conn, limit=120):
    return list(conn.execute("SELECT * FROM oplog ORDER BY id DESC LIMIT ?", (limit,)))


def trim_log(conn, keep=5000):
    conn.execute(
        "DELETE FROM oplog WHERE id < (SELECT MAX(id) - ? FROM oplog)", (keep,)
    )
    conn.commit()
