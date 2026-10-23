"""
Configuración central de CalSync.

Todo lo que alguien podría querer ajustar sin leer código está aquí arriba.
Los valores que se cambian desde el dashboard NO viven aquí: viven en la
base de datos (ver db.py, tabla `settings`).
"""

import os
from pathlib import Path
from zoneinfo import ZoneInfo

# --- Dónde vive todo ---------------------------------------------------------

# Datos privados (permisos de Google, base de datos, log). Solo en tu disco.
APP_DIR = Path.home() / ".calsync"
DB_PATH = APP_DIR / "calsync.db"

# La carpeta del proyecto, donde debe estar credentials.json
PROJECT_DIR = Path(__file__).resolve().parent.parent
CREDENTIALS_FILE = PROJECT_DIR / "credentials.json"

# --- Google ------------------------------------------------------------------

# Permiso de lectura y escritura de calendarios. Es el mínimo que permite
# crear y mover los bloques espejo.
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# --- Comportamiento del motor ------------------------------------------------

def _zona_local():
    """La zona horaria del computador donde corre, no la del que lo escribió.

    Con la zona fijada a Bogotá, un compañero en Manila vería su almuerzo doce
    horas corrido. Se mira, en orden: la variable CALSYNC_TZ (para forzarla),
    la TZ del sistema, y el enlace /etc/localtime, que en macOS y Linux apunta
    a .../zoneinfo/Area/Ciudad. Bogotá queda solo como último recurso.

    Se busca el nombre de la zona y no el desfase actual a propósito: un
    desfase fijo no sabe de horario de verano, y media Europa cambia dos veces
    al año.
    """
    for variable in ("CALSYNC_TZ", "TZ"):
        nombre = os.environ.get(variable)
        if nombre:
            try:
                return ZoneInfo(nombre)
            except Exception:  # noqa: BLE001 — un nombre inválido no debe tumbar el arranque
                pass
    try:
        partes = Path("/etc/localtime").resolve().parts
        if "zoneinfo" in partes:
            return ZoneInfo("/".join(partes[partes.index("zoneinfo") + 1:]))
    except Exception:  # noqa: BLE001
        pass
    return ZoneInfo("America/Bogota")


TZ = _zona_local()

# Ventana de tiempo que se mantiene sincronizada.
#
# Va a la par de HABITS_HORIZON_DAYS a propósito: el calendario del dashboard
# muestra bloques y reuniones en la misma rejilla, y si una de las dos ventanas
# fuera más corta, al navegar a la cuarta semana verías tus bloques flotando
# sin las reuniones que los empujaron ahí.
#
# Subirlo o bajarlo funciona en ambos sentidos: lo que falta se crea en el
# siguiente ciclo, y lo que sobra se borra en vez de quedar abandonado. Ojo con
# el estreno: el ciclo que aplica un aumento grande crea cientos de eventos de
# una sentada y se va despacio a propósito para no chocar con Google.
WINDOW_PAST_DAYS = 7
WINDOW_FUTURE_DAYS = 30

# Cada cuántos minutos se revisan cambios.
POLL_MINUTES = 3

# Cada cuántas horas se hace una lectura completa en vez de incremental.
# Es necesario porque la ventana se desplaza con el tiempo: sin esto, los
# eventos que entran al día 60 nunca aparecerían.
FULL_RESYNC_HOURS = 12

# --- Bloques de hábitos ------------------------------------------------------

# Con cuántos días de anticipación se colocan los hábitos.
# Más corto que la ventana de espejos a propósito: colocar almuerzos a 60 días
# significaría cientos de eventos que se van a mover igual cuando la agenda
# se llene. Dos semanas es lo que se puede planear con sentido.
HABITS_HORIZON_DAYS = 30

# Cada cuántos minutos se prueba una posición al buscar un hueco.
PLACEMENT_STEP_MINUTES = 15

# En cuánto se va recortando un bloque flexible cuando no cabe completo.
SHRINK_STEP_MINUTES = 15

# Alto de una hora en la rejilla semanal del dashboard, en píxeles. El cálculo
# de posiciones y la plantilla leen el mismo número: si discrepan, los bloques
# quedan corridos respecto a las líneas de la hora.
PX_HORA = 44

# --- Marca de los eventos espejo ---------------------------------------------
# Esta es la pieza de seguridad más importante del proyecto.
# CalSync SOLO puede modificar o borrar eventos que lleven esta marca.
# Un evento creado por un humano es, por diseño, inalcanzable para el motor.

MARKER_KEY = "calsync"
MARKER_VALUE = "1"
MIRROR_PREFIX = "🔒"

# Segunda red, independiente de Google. Es un carácter Unicode invisible
# (word joiner, U+2060) que se agrega al final del título de TODO evento que
# CalSync crea: espejos y bloques.
#
# Por qué existe: si Google dejara de devolvernos `extendedProperties` en una
# lectura, un espejo se leería como evento original y generaría un espejo del
# espejo, en cascada entre las tres cuentas. El prefijo 🔒 cubría los espejos,
# pero no los bloques, que se llaman «Lunch» o «Exercise» sin emoji. Esta marca
# los cubre todos sin ensuciar ningún título.
TITLE_MARK = "⁠"

# --- Colores de Google Calendar ----------------------------------------------
# Los once colores que Google permite por evento, con el nombre que muestra
# su interfaz. El id es lo que viaja en la API.
# El hex es solo para pintar la muestra en el dashboard; a Google viaja el id.
COLORES = [
    ("",   "Por defecto",  "#4285F4"),
    ("1",  "Lavender",     "#7986CB"),
    ("2",  "Sage",         "#33B679"),
    ("3",  "Grape",        "#8E24AA"),
    ("4",  "Flamingo",     "#E67C73"),
    ("5",  "Banana",       "#F6BF26"),
    ("6",  "Tangerine",    "#F4511E"),
    ("7",  "Peacock",      "#039BE5"),
    ("8",  "Graphite",     "#616161"),
    ("9",  "Blueberry",    "#3F51B5"),
    ("10", "Basil",        "#0B8043"),
    ("11", "Tomato",       "#D50000"),
]

COLOR_HEX = {cid: hexa for cid, _nombre, hexa in COLORES}


# --- Dashboard ---------------------------------------------------------------

# Los nombres internos que se escriben en el registro, con su etiqueta en
# pantalla. Traducir aquí en vez de renombrar cada llamada a db.log deja las
# filas viejas legibles y evita 30 ediciones.
LOG_LABELS = {
    "arranque": "startup",
    "ciclo": "cycle",
    "simulacion": "dry run",
    "avance": "progress",
    "lote_grande": "large batch",
    "create": "create",
    "update": "move",
    "delete": "delete",
    "cuenta_conectada": "account connected",
    "cuenta_desconectada": "account disconnected",
    "ajustes_guardados": "settings saved",
    "habitos_guardados": "blocks saved",
    "habito_creado": "block created",
    "habito_borrado": "block deleted",
    "habito_restaurado": "block restored",
    "bloque_movido_a_mano": "block moved by hand",
    "bloque_borrado_a_mano": "block deleted by hand",
    "huerfanos": "orphans",
    "huerfano_borrado": "orphan deleted",
    "limpieza_total": "full cleanup",
    "limpieza_pasada": "cleanup pass",
    "limpieza_incompleta": "cleanup incomplete",
    "limpieza_cancelada": "cleanup cancelled",
    "ventana_cambiada": "window changed",
    "sync_token_caducado": "sync token expired",
    "bloqueado_por_seguridad": "blocked by safety check",
    "error_lectura": "read error",
    "error_google": "google error",
    "error_conexion": "connection error",
    "error_limpieza": "cleanup error",
    "error_huerfano": "orphan error",
    "error_reconciliacion": "reconcile error",
}

DASHBOARD_HOST = "127.0.0.1"  # solo accesible desde tu propio Mac
DASHBOARD_PORT = 8765
