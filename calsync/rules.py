"""
Reglas: qué evento merece bloquear tiempo en las otras cuentas.

Este archivo no habla con Google ni con la base de datos. Recibe datos y
devuelve decisiones, lo que lo hace fácil de probar y de leer.
"""

# Permite escribir anotaciones modernas (como `datetime | None`) sin exigir
# Python 3.10. Sin esta línea, el programa no arranca en el Python 3.9 que
# viene de fábrica en macOS.
from __future__ import annotations

import json
from datetime import datetime, time, timezone

from . import config, google_cal


# --- Traducir un evento de Google a algo manejable ---------------------------

def to_utc(stamp: dict) -> datetime | None:
    """Convierte el inicio o fin de un evento de Google a UTC.

    Google usa dos formatos: {'dateTime': '2026-08-03T09:00:00-05:00'} para
    eventos con hora, y {'date': '2026-08-03'} para eventos de día completo.
    """
    if not stamp:
        return None
    if "dateTime" in stamp:
        raw = stamp["dateTime"].replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:  # defensivo: Google casi siempre manda zona
            dt = dt.replace(tzinfo=config.TZ)
        return dt.astimezone(timezone.utc)
    if "date" in stamp:
        day = datetime.fromisoformat(stamp["date"]).date()
        return datetime.combine(day, time.min, tzinfo=config.TZ).astimezone(timezone.utc)
    return None


def my_response(event: dict) -> str:
    """Cómo respondió David a la invitación: accepted, declined,
    tentative, needsAction, o 'owner' si el evento es suyo."""
    for guest in event.get("attendees") or []:
        if guest.get("self"):
            return guest.get("responseStatus", "needsAction")
    return "owner"


def meet_link(event: dict) -> str:
    """El link de la videollamada, venga como venga.

    Google lo pone en `hangoutLink` para Meet, y en `conferenceData` para
    Zoom, Teams y compañía.
    """
    if event.get("hangoutLink"):
        return event["hangoutLink"]
    for punto in (event.get("conferenceData") or {}).get("entryPoints") or []:
        if punto.get("entryPointType") == "video" and punto.get("uri"):
            return punto["uri"]
    return ""


def guests_list(event: dict) -> str:
    """Los invitados como texto legible, sin incluirte a ti.

    Es texto y no invitados de verdad a propósito: copiar la lista real haría
    que Google mandara una invitación nueva desde tu cuenta a todas esas
    personas, y les duplicaría el evento en su calendario.
    """
    nombres = []
    for guest in event.get("attendees") or []:
        if guest.get("self") or guest.get("resource"):
            continue
        nombres.append(guest.get("displayName") or guest.get("email") or "")
    return ", ".join(n for n in nombres if n)


def normalize(event: dict) -> dict:
    """Deja un evento de Google listo para guardar en la base."""
    start, end = event.get("start") or {}, event.get("end") or {}
    start_utc, end_utc = to_utc(start), to_utc(end)
    guests = event.get("attendees") or []
    return {
        "location": event.get("location") or "",
        "description": event.get("description") or "",
        "meet_link": meet_link(event),
        "guests_list": guests_list(event),
        "organizer": (event.get("organizer") or {}).get("email") or "",
        "event_id": event["id"],
        "summary": event.get("summary") or "",
        "start_raw": json.dumps(start),
        "end_raw": json.dumps(end),
        "start_utc": start_utc.isoformat() if start_utc else None,
        "end_utc": end_utc.isoformat() if end_utc else None,
        "all_day": int("date" in start),
        "transparency": event.get("transparency") or "opaque",
        "status": event.get("status") or "confirmed",
        "my_response": my_response(event),
        "has_guests": int(len(guests) > 1),
        "is_mirror": int(google_cal.is_mirror(event)),
        "updated": event.get("updated"),
    }


# --- La decisión -------------------------------------------------------------

def should_mirror(evt, rules: dict) -> tuple[bool, str]:
    """¿Este evento debe generar bloqueos en las otras cuentas?

    Devuelve (sí/no, motivo). El motivo se escribe en el log, para que
    cuando algo no aparezca se pueda ver exactamente por qué.
    """
    # 1. Nunca, jamás, un espejo genera espejos. Esto es lo que hace
    #    imposible un bucle infinito entre las tres cuentas.
    if evt["is_mirror"]:
        return False, "created by CalSync"

    if evt["status"] == "cancelled":
        return False, "cancelled event"

    if not evt["start_utc"] or not evt["end_utc"]:
        return False, "no usable dates"

    if rules.get("skip_all_day") and evt["all_day"]:
        return False, "all-day event"

    if rules.get("skip_transparent") and evt["transparency"] == "transparent":
        return False, "marked Free in Google"

    if rules.get("skip_declined") and evt["my_response"] == "declined":
        return False, "invitation declined"

    if rules.get("skip_needs_action") and evt["my_response"] == "needsAction":
        return False, "invitation unanswered"

    title = (evt["summary"] or "").lower()
    for tag in rules.get("skip_tags") or []:
        if tag.lower() in title:
            return False, f"tagged {tag}"

    for word in rules.get("skip_keywords") or []:
        if word.strip() and word.strip().lower() in title:
            return False, f'contains "{word.strip()}"'

    return True, "blocks time"


def in_window(evt) -> bool:
    """¿El evento cae dentro de la ventana que mantenemos sincronizada?"""
    if not evt["start_utc"] or not evt["end_utc"]:
        return False
    lo, hi = google_cal.window()
    start = datetime.fromisoformat(evt["start_utc"])
    end = datetime.fromisoformat(evt["end_utc"])
    return end > lo and start < hi
