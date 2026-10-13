"""
Todo el contacto con Google vive en este archivo. El resto del programa no
sabe que Google existe: solo habla con estas funciones.

Eso es a propósito. Si algún día quieres agregar Outlook, se escribe un
archivo hermano de este y el motor no cambia.
"""

import json
import random
import time
from datetime import datetime, timedelta, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import config, db


class CredencialesFaltantes(Exception):
    """No existe credentials.json — falta el Paso 1."""


class AccesoBloqueado(Exception):
    """El administrador de Workspace bloqueó la app para esa cuenta."""


# --- Aguante frente a los límites de Google ----------------------------------

# Google acepta del orden de 10 escrituras por segundo por usuario. Vamos más
# despacio a propósito: la primera activación puede crear cientos de espejos de
# golpe, y que se demore dos minutos es mucho mejor que que Google nos corte.
PAUSA_ENTRE_ESCRITURAS = 0.15
_ULTIMA_ESCRITURA = [0.0]

RITMO = ("ratelimitexceeded", "userratelimitexceeded", "quotaexceeded", "backenderror")


def _es_de_ritmo(error: HttpError) -> bool:
    """¿El error es «vas muy rápido» y no «no tienes permiso»?

    Importa distinguirlos: los de ritmo se reintentan, los de permiso no.
    Ambos llegan como 403.
    """
    if error.resp.status in (429, 500, 502, 503):
        return True
    if error.resp.status != 403:
        return False
    texto = (error.content or b"").decode("utf-8", "replace").lower()
    return any(motivo in texto.replace("_", "") for motivo in RITMO)


def ejecutar(peticion, intentos=6):
    """Ejecuta una petición reintentando cuando Google pide calma.

    Espera 1, 2, 4, 8… segundos con un poquito de azar, que es lo que Google
    recomienda para no volver todos a la vez.
    """
    for intento in range(intentos):
        try:
            return peticion.execute()
        except HttpError as e:
            if intento == intentos - 1 or not _es_de_ritmo(e):
                raise
            time.sleep(2 ** intento + random.uniform(0, 1))


def frenar():
    """Deja pasar un mínimo de tiempo entre escrituras."""
    espera = PAUSA_ENTRE_ESCRITURAS - (time.monotonic() - _ULTIMA_ESCRITURA[0])
    if espera > 0:
        time.sleep(espera)
    _ULTIMA_ESCRITURA[0] = time.monotonic()


# --- Ventana de tiempo -------------------------------------------------------

def window():
    """La franja de tiempo que mantenemos sincronizada, en UTC."""
    now = datetime.now(timezone.utc)
    return (
        now - timedelta(days=config.WINDOW_PAST_DAYS),
        now + timedelta(days=config.WINDOW_FUTURE_DAYS),
    )


def window_iso():
    a, b = window()
    return a.isoformat(), b.isoformat()


# --- Autorización ------------------------------------------------------------

def _service_from_creds(creds):
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def authorize(conn, label: str) -> str:
    """Abre el navegador para autorizar una cuenta nueva. Devuelve el correo.

    Bloquea hasta que la persona termine en el navegador, así que se llama
    desde un endpoint sincrónico (FastAPI lo corre en un hilo aparte).
    """
    if not config.CREDENTIALS_FILE.exists():
        raise CredencialesFaltantes(
            f"Falta {config.CREDENTIALS_FILE}. Revisa PASO-1-Credenciales-Google.md"
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(config.CREDENTIALS_FILE), config.SCOPES
    )
    # access_type=offline + prompt=consent garantizan que Google entregue un
    # refresh_token, que es lo que permite operar sin volver a pedir permiso.
    creds = flow.run_local_server(
        port=0, access_type="offline", prompt="consent", open_browser=True
    )

    svc = _service_from_creds(creds)
    try:
        # En la API de Google, el id del calendario primario ES el correo.
        email = svc.calendars().get(calendarId="primary").execute()["id"]
    except HttpError as e:
        if e.resp.status in (401, 403):
            raise AccesoBloqueado(
                "Google rechazó el acceso a esta cuenta. Es muy probable que el "
                "administrador de Workspace tenga restringidas las apps de terceros."
            ) from e
        raise

    account_id = db.upsert_account(conn, email, label.strip() or email, creds.to_json())
    db.ensure_pairs(conn)
    db.log(conn, "cuenta_conectada", f"{label} → {email} (id {account_id})")
    return email


def service_for(conn, acct):
    """Construye un cliente de Google para una cuenta, renovando el permiso
    automáticamente si hace falta."""
    creds = Credentials.from_authorized_user_info(
        json.loads(acct["token_json"]), config.SCOPES
    )
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            db.save_token(conn, acct["id"], creds.to_json())
        else:
            raise AccesoBloqueado(
                f"El permiso de {acct['email']} ya no sirve. Reconecta la cuenta "
                "desde el dashboard."
            )
    return _service_from_creds(creds)


# --- Lectura -----------------------------------------------------------------

def _needs_full_sync(acct) -> bool:
    if not acct["sync_token"] or not acct["last_full_sync"]:
        return True
    last = datetime.fromisoformat(acct["last_full_sync"])
    age = datetime.now(timezone.utc) - last
    return age > timedelta(hours=config.FULL_RESYNC_HOURS)


def fetch_changes(conn, acct, force_full=False):
    """Devuelve (eventos, fue_lectura_completa).

    Usa el syncToken de Google para pedir solo lo que cambió. Si el token ya
    no sirve —Google los invalida cada tantos días— cae limpiamente a una
    lectura completa de la ventana.
    """
    svc = service_for(conn, acct)
    full = force_full or _needs_full_sync(acct)

    for intento in (1, 2):
        params = {
            "calendarId": "primary",
            "singleEvents": True,   # los eventos recurrentes llegan expandidos
            "showDeleted": True,    # necesitamos saber qué se borró
            "maxResults": 250,
        }
        if full:
            params["timeMin"], params["timeMax"] = window_iso()
        else:
            params["syncToken"] = acct["sync_token"]

        items, page, next_token = [], None, None
        try:
            while True:
                if page:
                    params["pageToken"] = page
                resp = ejecutar(svc.events().list(**params))
                items.extend(resp.get("items", []))
                page = resp.get("nextPageToken")
                if not page:
                    next_token = resp.get("nextSyncToken")
                    break
        except HttpError as e:
            # 410 Gone = el syncToken caducó. Se reintenta completo, una vez.
            if e.resp.status == 410 and not full and intento == 1:
                db.log(conn, "sync_token_caducado", acct["email"], "warn")
                full = True
                continue
            raise

        conn.execute(
            "UPDATE accounts SET sync_token = ?, last_sync = ?"
            + (", last_full_sync = ?" if full else "")
            + " WHERE id = ?",
            ((next_token, db.now_iso(), db.now_iso(), acct["id"])
             if full else (next_token, db.now_iso(), acct["id"])),
        )
        conn.commit()
        return items, full

    return [], full


def list_mirrors(conn, acct, all_time=False):
    """Todos los eventos que CalSync creó en esta cuenta.

    Se los pide a Google por la marca privada, no por el título. Así los
    encuentra incluso si alguien les cambió el nombre a mano.

    `all_time=True` ignora la ventana de fechas. Es imprescindible para limpiar:
    si la ventana se encogió (de 60 a 14 días, por ejemplo), los eventos que
    quedaron afuera siguen existiendo en Google y con la búsqueda acotada serían
    invisibles para siempre.
    """
    svc = service_for(conn, acct)
    parametros = {
        "calendarId": "primary",
        "privateExtendedProperty": f"{config.MARKER_KEY}={config.MARKER_VALUE}",
        "singleEvents": True,
        "maxResults": 250,
    }
    if not all_time:
        parametros["timeMin"], parametros["timeMax"] = window_iso()

    out, page = [], None
    while True:
        resp = ejecutar(svc.events().list(pageToken=page, **parametros))
        out.extend(resp.get("items", []))
        page = resp.get("nextPageToken")
        if not page:
            return out


# --- Escritura ---------------------------------------------------------------

def tiene_marca(event: dict) -> bool:
    """¿Lleva la marca privada de CalSync?

    Esta es la comprobación ESTRICTA y es la que autoriza escribir o borrar.
    Solo la pasa un evento que CalSync creó con su propia marca invisible.
    """
    props = (event.get("extendedProperties") or {}).get("private") or {}
    return props.get(config.MARKER_KEY) == config.MARKER_VALUE


def is_mirror(event: dict) -> bool:
    """¿Hay que tratar este evento como creado por CalSync al leer?

    Más amplia que `tiene_marca` a propósito, y solo se usa para decidir qué
    NO reflejar. Si por cualquier motivo Google dejara de devolvernos
    `extendedProperties`, un espejo se leería como evento original y generaría
    un espejo del espejo, en cascada entre las tres cuentas. El prefijo del
    título es un cinturón de seguridad redundante que hace eso imposible.

    El precio es que un evento tuyo cuyo título empiece con 🔒 nunca se
    reflejaría. Barato frente a la alternativa.

    Ojo: esta función NO autoriza a escribir. Para eso está `tiene_marca`, que
    es estricta — así un evento humano llamado «🔒 algo» sigue siendo intocable.
    """
    if tiene_marca(event):
        return True
    titulo = event.get("summary") or ""
    # La marca invisible cubre todo lo que crea CalSync, con o sin emoji.
    # El prefijo 🔒 se sigue mirando por los espejos creados antes de que
    # existiera la marca invisible.
    return config.TITLE_MARK in titulo or titulo.lstrip().startswith(config.MIRROR_PREFIX)


def create_event(conn, acct, body):
    svc = service_for(conn, acct)
    frenar()
    # sendUpdates="none": estos bloques no tienen invitados, pero lo dejamos
    # explícito para que ninguna versión futura pueda notificar a nadie.
    return ejecutar(
        svc.events().insert(calendarId="primary", body=body, sendUpdates="none")
    )


def update_event(conn, acct, event_id, body):
    """Actualiza un espejo. Verifica la marca ANTES de escribir."""
    svc = service_for(conn, acct)
    actual = ejecutar(svc.events().get(calendarId="primary", eventId=event_id))
    if not tiene_marca(actual):
        raise PermissionError(
            f"NEGADO: {event_id} en {acct['email']} no lleva la marca de CalSync. "
            "El motor no toca eventos que no creó."
        )
    frenar()
    return ejecutar(svc.events().update(
        calendarId="primary", eventId=event_id, body=body, sendUpdates="none"
    ))


def delete_event(conn, acct, event_id):
    """Borra un espejo. Verifica la marca ANTES de borrar."""
    svc = service_for(conn, acct)
    try:
        actual = ejecutar(svc.events().get(calendarId="primary", eventId=event_id))
    except HttpError as e:
        if e.resp.status in (404, 410):
            return  # ya no existe: nada que hacer
        raise
    if not tiene_marca(actual):
        raise PermissionError(
            f"NEGADO: {event_id} en {acct['email']} no lleva la marca de CalSync. "
            "El motor no borra eventos que no creó."
        )
    frenar()
    try:
        ejecutar(svc.events().delete(
            calendarId="primary", eventId=event_id, sendUpdates="none"
        ))
    except HttpError as e:
        if e.resp.status not in (404, 410):
            raise
