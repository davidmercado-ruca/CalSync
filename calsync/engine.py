"""
El motor. Hace tres cosas en cada ciclo:

  1. LEER    — pregunta a Google qué cambió en cada cuenta
  2. PLANEAR — calcula qué espejos deberían existir y los compara con los que hay
  3. APLICAR — crea, mueve o borra lo justo (o solo lo anota, si está en simulación)

La separación entre PLANEAR y APLICAR es lo que permite el modo simulación y
lo que hace que el motor se pueda probar sin tocar un calendario real.
"""

import hashlib
import json
from datetime import datetime

from googleapiclient.errors import HttpError

from . import config, db, google_cal, habits, rules


# --- Construcción del bloque espejo ------------------------------------------

def _campo(evt, nombre):
    """Lee un campo que puede no existir en bases de datos viejas."""
    try:
        return evt[nombre] or ""
    except (IndexError, KeyError):
        return ""


def mirror_payload(source_account, evt, detail: dict, busy_word: str = "Busy",
                   color: str = "", nota_generica: str = ""):
    """El evento que se va a crear en la cuenta destino.

    `detail` decide qué tanto se cuenta, y viene del par origen|destino:
      {"mode": "busy"}                    → "Busy (Galactic)"
      {"mode": "title"}                   → título real, lugar y link
      {"mode": "custom", "label": "Work"} → "Work"
    """
    mode = (detail or {}).get("mode") or "busy"
    label = ((detail or {}).get("label") or "").strip()
    share = bool((detail or {}).get("share"))

    if mode == "title" and evt["summary"]:
        shown = evt["summary"]
    elif mode == "custom" and label:
        shown = label
    else:
        shown = f"{busy_word} ({source_account['label']})"

    # El texto que leen los demás. Cuando la dirección está marcada como visible
    # al equipo, esta descripción la ve tu equipo entero: por eso es un mensaje
    # editable y genérico, no una instrucción interna.
    nota = [nota_generica] if nota_generica else []

    body = {
        # Prefijo visible + marca invisible al final: dos redes independientes
        # para que un espejo nunca se lea como evento original.
        "summary": f"{config.MIRROR_PREFIX} {shown}{config.TITLE_MARK}",
        "start": json.loads(evt["start_raw"]),
        "end": json.loads(evt["end_raw"]),
        "transparency": "opaque",   # cuenta como ocupado
        # "private" = tus colegas ven solo "Busy", sin título.
        # "default" = lo ven como cualquier otro evento tuyo de esa cuenta.
        "visibility": "default" if share else "private",
        "reminders": {"useDefault": False, "overrides": []},  # cero notificaciones
        "extendedProperties": {
            "private": {
                config.MARKER_KEY: config.MARKER_VALUE,
                "src_acct": str(source_account["id"]),
                "src_evt": evt["event_id"],
            }
        },
    }
    # NUNCA se copian los invitados en el campo `attendees`: Google les mandaría
    # una invitación nueva desde esta cuenta y les duplicaría el evento. Van como
    # texto en la descripción, más abajo.

    # Con detalle completo, el espejo trae también lo que hace falta para no
    # tener que abrir el otro calendario: dónde es y por dónde se entra.
    if mode == "title":
        lugar, link = _campo(evt, "location"), _campo(evt, "meet_link")
        invitados, organiza = _campo(evt, "guests_list"), _campo(evt, "organizer")
        original = _campo(evt, "description")
        if lugar:
            body["location"] = lugar
        encabezado = []
        if link:
            encabezado.append(f"Join: {link}")
        if invitados:
            encabezado.append(f"Guests: {invitados}")
        if organiza:
            encabezado.append(f"Organizer: {organiza}")
        if encabezado:
            nota = encabezado + ([""] + nota if nota else [])
        if original:
            nota += ["", "──────────", original[:1500]]

    body["description"] = "\n".join(nota)
    if color:
        body["colorId"] = str(color)

    fingerprint = json.dumps(
        [body["summary"], body["start"], body["end"], body["visibility"],
         body.get("location", ""), body.get("colorId", ""), body["description"]],
        sort_keys=True,
    )
    digest = hashlib.sha1(fingerprint.encode()).hexdigest()[:16]
    body["extendedProperties"]["private"]["hash"] = digest
    return body, digest


# --- Paso 1: leer ------------------------------------------------------------

def store_events(conn, account_id, items):
    """Guarda los eventos leídos en la copia local."""
    for raw in items:
        evt = rules.normalize(raw)
        conn.execute(
            "INSERT INTO source_events (account_id, event_id, summary, start_raw, "
            "end_raw, start_utc, end_utc, all_day, transparency, status, my_response, "
            "has_guests, is_mirror, updated, location, description, meet_link, "
            "guests_list, organizer) "
            "VALUES (:account_id, :event_id, :summary, :start_raw, :end_raw, :start_utc, "
            ":end_utc, :all_day, :transparency, :status, :my_response, :has_guests, "
            ":is_mirror, :updated, :location, :description, :meet_link, "
            ":guests_list, :organizer) "
            "ON CONFLICT(account_id, event_id) DO UPDATE SET "
            "summary=excluded.summary, start_raw=excluded.start_raw, "
            "end_raw=excluded.end_raw, start_utc=excluded.start_utc, "
            "end_utc=excluded.end_utc, all_day=excluded.all_day, "
            "transparency=excluded.transparency, status=excluded.status, "
            "my_response=excluded.my_response, has_guests=excluded.has_guests, "
            "is_mirror=excluded.is_mirror, updated=excluded.updated, "
            "location=excluded.location, description=excluded.description, "
            "meet_link=excluded.meet_link, guests_list=excluded.guests_list, "
            "organizer=excluded.organizer",
            {**evt, "account_id": account_id},
        )
    conn.commit()


def purge_outside_window(conn):
    """Borra de la copia local los eventos que ya salieron de la ventana."""
    lo, hi = google_cal.window()
    conn.execute(
        "DELETE FROM source_events WHERE end_utc IS NOT NULL "
        "AND (end_utc < ? OR start_utc > ?)",
        (lo.isoformat(), hi.isoformat()),
    )
    conn.commit()


def purge_cancelled(conn):
    """Suelta las lápidas de eventos borrados que ya cumplieron su función.

    Google nos avisa de un evento borrado mandándonos el evento con estado
    "cancelled". Lo guardamos solo el tiempo necesario para borrar sus espejos;
    una vez no queda ninguno, la lápida no sirve para nada.
    """
    conn.execute(
        "DELETE FROM source_events WHERE status = 'cancelled' AND NOT EXISTS ("
        "  SELECT 1 FROM mirrors m WHERE m.source_account_id = source_events.account_id"
        "    AND m.source_event_id = source_events.event_id) "
        # Los bloques de hábitos borrados a mano también se conservan: su lápida
        # es la señal de «David lo borró», y sin ella se recrearía el bloque.
        "AND NOT EXISTS ("
        "  SELECT 1 FROM habit_blocks b WHERE b.account_id = source_events.account_id"
        "    AND b.event_id = source_events.event_id)"
    )
    conn.commit()


# --- Paso 2: planear ---------------------------------------------------------

def compute_plan(conn):
    """Compara lo que debería existir con lo que existe.

    No habla con Google: trabaja solo sobre la copia local. Por eso es
    instantáneo y por eso se puede probar sin credenciales.
    """
    settings = db.all_settings(conn)
    rule_cfg, pairs = settings["rules"], settings["pairs"]
    detail, busy_word = settings["detail_pairs"], settings.get("busy_word") or "Busy"
    color_espejo = settings.get("mirror_color") or ""
    nota_espejo = settings.get("mirror_note") or ""
    accounts = {a["id"]: a for a in db.accounts(conn)}

    # Tercera red contra los bucles, independiente de Google y del título:
    # los ids de todo lo que CalSync creó. Un evento que está en esta lista
    # jamás puede volver a reflejarse, pase lo que pase con la marca.
    propios = conocidos(conn)

    desired, skipped = {}, []
    for src_id, source_account in accounts.items():
        # Lo mismo que en `habits`: un evento ignorado no existe para CalSync.
        # Al desaparecer de `desired`, el bucle de abajo emite el borrado de los
        # espejos que ya había escrito, sin código nuevo para eso.
        for evt in conn.execute(
            "SELECT * FROM source_events e WHERE account_id = ? " + habits.SIN_IGNORADOS,
            (src_id,),
        ):
            if evt["event_id"] in propios:
                continue
            ok, reason = rules.should_mirror(evt, rule_cfg)
            if not ok:
                # Los espejos y los eventos borrados no se reportan: son ruido.
                # Un evento "cancelado" es una lápida que Google nos manda para
                # avisarnos que se borró, no algo que David pueda revisar.
                if reason not in ("created by CalSync", "cancelled event"):
                    skipped.append((source_account["label"], evt["summary"], reason))
                continue
            if not rules.in_window(evt):
                continue
            for tgt_id in accounts:
                if tgt_id == src_id:
                    continue
                if not pairs.get(f"{src_id}|{tgt_id}", True):
                    continue
                body, digest = mirror_payload(
                    source_account,
                    evt,
                    detail.get(f"{src_id}|{tgt_id}", {"mode": "busy"}),
                    busy_word,
                    color_espejo,
                    nota_espejo,
                )
                desired[(src_id, evt["event_id"], tgt_id)] = (body, digest, evt["summary"])

    existing = {
        (r["source_account_id"], r["source_event_id"], r["target_account_id"]): r
        for r in conn.execute("SELECT * FROM mirrors")
    }

    ops = []
    for key, (body, digest, title) in desired.items():
        src_id, src_evt, tgt_id = key
        row = existing.get(key)
        base = {
            "source_account_id": src_id,
            "source_event_id": src_evt,
            "target_account_id": tgt_id,
            "body": body,
            "hash": digest,
            "title": title,
            "from": accounts[src_id]["label"],
            "to": accounts[tgt_id]["label"],
        }
        if row is None:
            ops.append({**base, "action": "create"})
        elif row["payload_hash"] != digest:
            ops.append({**base, "action": "update",
                        "target_event_id": row["target_event_id"]})

    for key, row in existing.items():
        if key in desired:
            continue
        src_id, src_evt, tgt_id = key
        ops.append({
            "action": "delete",
            "source_account_id": src_id,
            "source_event_id": src_evt,
            "target_account_id": tgt_id,
            "target_event_id": row["target_event_id"],
            "from": accounts[src_id]["label"] if src_id in accounts else f"cuenta {src_id}",
            "to": accounts[tgt_id]["label"] if tgt_id in accounts else f"cuenta {tgt_id}",
            "title": "",
        })

    return ops, skipped


def describe(op) -> str:
    verbo = {"create": "create", "update": "move", "delete": "delete"}[op["action"]]
    titulo = op.get("title") or "(no title)"
    if op.get("kind") == "habit":
        return f'{verbo} "{titulo}" for {op.get("day", "")} in {op["to"]}'
    return f'{verbo} busy block for "{titulo}" — {op["from"]} → {op["to"]}'


# --- Paso 3: aplicar ---------------------------------------------------------

def apply_plan(conn, ops, simulation: bool):
    """Ejecuta el plan. En simulación solo lo escribe en el log."""
    accounts = {a["id"]: a for a in db.accounts(conn)}
    done = {"create": 0, "update": 0, "delete": 0, "error": 0}
    total = len(ops)

    if not simulation and total > 50:
        db.log(conn, "lote_grande",
               f"{total} operations to apply. Going deliberately slow to stay inside "
               f"Google's limits: about {int(total * 0.2)} seconds.",
               "warn")

    for indice, op in enumerate(ops, start=1):
        target = accounts.get(op["target_account_id"])
        if target is None:
            continue

        if simulation:
            db.log(conn, "simulacion", describe(op))
            done[op["action"]] += 1
            continue

        # En lotes grandes, dejar rastro del avance para que el dashboard no
        # parezca congelado durante dos minutos.
        if total > 50 and indice % 50 == 0:
            db.log(conn, "avance", f"{indice} of {total}")

        es_habito = op.get("kind") == "habit"
        try:
            if op["action"] == "create":
                created = google_cal.create_event(conn, target, op["body"])
                if es_habito:
                    conn.execute(
                        "INSERT INTO habit_blocks (habit_id, day, account_id, event_id, "
                        "payload_hash, updated_at, wrote_start, wrote_minutes) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(habit_id, day, account_id) DO UPDATE SET "
                        "event_id=excluded.event_id, payload_hash=excluded.payload_hash, "
                        "updated_at=excluded.updated_at, "
                        "wrote_start=excluded.wrote_start, "
                        "wrote_minutes=excluded.wrote_minutes",
                        (op["habit_id"], op["day"], op["target_account_id"],
                         created["id"], op["hash"], db.now_iso(),
                         op["slot_start"], op["slot_minutes"]),
                    )
                else:
                    conn.execute(
                        "INSERT INTO mirrors (source_account_id, source_event_id, "
                        "target_account_id, target_event_id, payload_hash, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(source_account_id, source_event_id, target_account_id) "
                        "DO UPDATE SET target_event_id=excluded.target_event_id, "
                        "payload_hash=excluded.payload_hash, updated_at=excluded.updated_at",
                        (op["source_account_id"], op["source_event_id"],
                         op["target_account_id"], created["id"], op["hash"], db.now_iso()),
                    )

            elif op["action"] == "update":
                google_cal.update_event(conn, target, op["target_event_id"], op["body"])
                if es_habito:
                    conn.execute(
                        "UPDATE habit_blocks SET payload_hash = ?, updated_at = ?, "
                        "wrote_start = ?, wrote_minutes = ? "
                        "WHERE habit_id = ? AND day = ? AND account_id = ?",
                        (op["hash"], db.now_iso(), op["slot_start"], op["slot_minutes"],
                         op["habit_id"], op["day"], op["target_account_id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE mirrors SET payload_hash = ?, updated_at = ? "
                        "WHERE source_account_id = ? AND source_event_id = ? "
                        "AND target_account_id = ?",
                        (op["hash"], db.now_iso(), op["source_account_id"],
                         op["source_event_id"], op["target_account_id"]),
                    )

            elif op["action"] == "delete":
                google_cal.delete_event(conn, target, op["target_event_id"])
                if es_habito:
                    conn.execute(
                        "DELETE FROM habit_blocks WHERE habit_id = ? AND day = ? "
                        "AND account_id = ?",
                        (op["habit_id"], op["day"], op["target_account_id"]),
                    )
                else:
                    conn.execute(
                        "DELETE FROM mirrors WHERE source_account_id = ? "
                        "AND source_event_id = ? AND target_account_id = ?",
                        (op["source_account_id"], op["source_event_id"],
                         op["target_account_id"]),
                    )

            conn.commit()
            done[op["action"]] += 1
            db.log(conn, op["action"], describe(op))

        except PermissionError as e:
            # La barrera de seguridad se activó. Es una buena noticia.
            done["error"] += 1
            db.log(conn, "bloqueado_por_seguridad", str(e), "error")
        except HttpError as e:
            done["error"] += 1
            db.log(conn, "error_google", f"{describe(op)} → {e}", "error")

    return done


def conocidos(conn) -> set:
    """Los ids de eventos que CalSync sabe que creó, de los DOS tipos.

    Olvidar los bloques de hábitos aquí los convertía en huérfanos: se borraban
    en cada lectura completa y se volvían a crear enseguida.
    """
    ids = {r["target_event_id"] for r in conn.execute("SELECT target_event_id FROM mirrors")}
    ids |= {r["event_id"] for r in conn.execute("SELECT event_id FROM habit_blocks")}
    return ids


def reconcile_orphans(conn, simulation: bool):
    """Borra los eventos de CalSync que ya nadie reclama.

    Un huérfano es un evento con nuestra marca que no está en la contabilidad:
    se creó en Google pero el registro se perdió. Si no se limpian, el siguiente
    ciclo crea otro para el mismo hueco y aparecen duplicados que se acumulan.

    Mira TODAS las fechas, no solo la ventana: si la ventana se encogió, los
    huérfanos de más allá serían invisibles para siempre.
    """
    known = conocidos(conn)
    removed, fallos = 0, 0
    for acct in db.accounts(conn):
        try:
            eventos = google_cal.list_mirrors(conn, acct, all_time=True)
        # A propósito se atrapa todo: un permiso vencido en UNA cuenta no debe
        # tumbar el ciclo completo ni dejar a las otras dos sin sincronizar.
        except Exception as e:  # noqa: BLE001
            db.log(conn, "error_reconciliacion", f"{acct['label']}: {e}", "error")
            continue

        for event in eventos:
            if event["id"] in known:
                continue
            titulo = event.get("summary", "")
            if simulation:
                db.log(conn, "simulacion",
                       f'delete orphan "{titulo}" in {acct["label"]}')
                removed += 1
                continue
            # Cada borrado por separado: que uno falle no puede abandonar el resto.
            try:
                google_cal.delete_event(conn, acct, event["id"])
                removed += 1
            except Exception as e:  # noqa: BLE001
                fallos += 1
                db.log(conn, "error_huerfano", f"«{titulo}» en {acct['label']}: {e}", "error")

    if removed or fallos:
        db.log(conn, "huerfanos", f"{removed} deleted, {fallos} failed",
               "warn" if fallos else "info")
    return removed


# --- El ciclo completo -------------------------------------------------------

def run_cycle(conn, force_full=False) -> dict:
    settings = db.all_settings(conn)
    simulation = bool(settings["simulation"])
    started = db.now_iso()
    read, full_any = 0, False

    # Si cambiaste la ventana en config.py, hay que releer completo: con lectura
    # incremental, Google solo cuenta lo que cambió y los días recién incluidos
    # nunca aparecerían. Sin esto, editar el número parecería no hacer nada
    # durante horas.
    ventana = [config.WINDOW_PAST_DAYS, config.WINDOW_FUTURE_DAYS]
    if db.get_setting(conn, "ventana_usada") != ventana:
        if db.get_setting(conn, "ventana_usada") is not None:
            db.log(conn, "ventana_cambiada",
                   f"now {ventana[0]} days back and {ventana[1]} ahead; "
                   "re-reading everything to apply it", "warn")
        db.set_setting(conn, "ventana_usada", ventana)
        force_full = True

    for acct in db.accounts(conn):
        try:
            items, was_full = google_cal.fetch_changes(conn, acct, force_full=force_full)
            store_events(conn, acct["id"], items)
            read += len(items)
            full_any = full_any or was_full
            conn.execute("UPDATE accounts SET last_error = NULL WHERE id = ?", (acct["id"],))
            conn.commit()
        except Exception as e:  # noqa: BLE001 — el ciclo nunca debe morir
            conn.execute(
                "UPDATE accounts SET last_error = ? WHERE id = ?", (str(e)[:500], acct["id"])
            )
            conn.commit()
            db.log(conn, "error_lectura", f"{acct['label']}: {e}", "error")

    if full_any:
        purge_outside_window(conn)

    # La limpieza de huérfanos va ANTES de planear, no después. Si la
    # contabilidad se perdió, los eventos viejos se borran primero y el plan
    # los recrea limpios. Al revés —planear y limpiar después— se crea un juego
    # nuevo encima del viejo y quedan duplicados.
    orphans = reconcile_orphans(conn, simulation) if full_any else 0

    # Primero los espejos, para que los hábitos vean la ocupación completa de
    # las tres cuentas antes de decidir dónde caben.
    ops, skipped = compute_plan(conn)
    ops_habitos, conflictos = habits.compute_plan(conn)
    done = apply_plan(conn, ops + ops_habitos, simulation)
    if not simulation:
        purge_cancelled(conn)
    db.set_setting(conn, "conflicts", conflictos)
    # Lo que silenciaste se olvida solo cuando su evento ya no existe y su
    # fecha pasó. Sin esto la lista crece para siempre y, peor, un ignorado
    # viejo puede volver a aplicarse si Google reutiliza un id.
    db.limpiar_silenciados(conn)

    summary = {
        "inicio": started,
        "fin": db.now_iso(),
        "modo": "dry run" if simulation else "live",
        "eventos_leidos": read,
        "lectura_completa": full_any,
        "creados": done["create"],
        "movidos": done["update"],
        "borrados": done["delete"],
        "errores": done["error"],
        "huerfanos": orphans,
        "ignorados": len(skipped),
        "conflictos": len(conflictos),
    }
    db.set_setting(conn, "last_cycle", summary)
    # Legible, no un volcado de JSON con fechas en UTC dentro.
    # Las claves del resumen se quedan en español (las usan el pie del dashboard
    # y las pruebas); lo que se muestra va en inglés.
    EN = {"creados": "created", "movidos": "moved", "borrados": "deleted",
          "conflictos": "conflicts", "errores": "errors"}
    partes = [f"{summary['eventos_leidos']} read"]
    for clave in ("creados", "movidos", "borrados", "conflictos", "errores"):
        if summary[clave]:
            partes.append(f"{summary[clave]} {EN[clave]}")
    if full_any:
        partes.append("full read")
    db.log(conn, "ciclo", f"{summary['modo']} · " + ", ".join(partes),
           "error" if summary["errores"] else "info")
    db.trim_log(conn)
    return summary


def _mensaje(e) -> str:
    """Un error de Google en una línea legible, sin el volcado entero."""
    texto = str(e).strip().replace("\n", " ")
    return texto[:200]


def cleanup_all_mirrors(conn, pasadas=6) -> dict:
    """El botón de pánico: borra TODO lo que CalSync creó en las tres cuentas.

    Tres cosas que la primera versión hacía mal y causaron duplicados:

      1. Buscaba solo dentro de la ventana de fechas. Lo de más allá quedaba
         vivo en Google y ya nadie lo encontraba nunca.
      2. Un solo error abandonaba el resto de los eventos de esa cuenta.
      3. Borraba la contabilidad igual, hubiera funcionado o no. Los eventos que
         sobrevivían quedaban huérfanos, y el siguiente ciclo creaba otro juego
         encima. Repetir el botón multiplicaba el problema.

    Ahora: busca en todas las fechas, cada borrado va por separado, repite
    pasadas hasta que no quede nada, y solo suelta la contabilidad si de verdad
    quedó todo limpio.
    """
    borrados, errores = 0, []

    for pasada in range(1, pasadas + 1):
        encontrados_en_pasada = 0
        for acct in db.accounts(conn):
            try:
                eventos = google_cal.list_mirrors(conn, acct, all_time=True)
            except Exception as e:  # noqa: BLE001
                detalle = f"listing {acct['label']}: {_mensaje(e)}"
                errores.append(detalle)
                db.log(conn, "error_limpieza", detalle, "error")
                continue

            encontrados_en_pasada += len(eventos)
            for event in eventos:
                try:
                    google_cal.delete_event(conn, acct, event["id"])
                    borrados += 1
                except Exception as e:  # noqa: BLE001
                    detalle = (f"{acct['label']} · \"{event.get('summary','(no title)')}\" "
                               f"({event['id']}): {_mensaje(e)}")
                    errores.append(detalle)
                    db.log(conn, "error_limpieza", detalle, "error")

        db.log(conn, "limpieza_pasada",
               f"pass {pasada}: {encontrados_en_pasada} found, "
               f"{borrados} deleted so far")
        if encontrados_en_pasada == 0:
            break

    # Verificación final: preguntarle a Google si de verdad no queda nada.
    quedan = 0
    for acct in db.accounts(conn):
        try:
            quedan += len(google_cal.list_mirrors(conn, acct, all_time=True))
        except Exception as e:  # noqa: BLE001
            quedan += 1   # si no se puede verificar, se asume que sí quedan
            errores.append(f"could not verify {acct['label']}: {_mensaje(e)}")

    if quedan == 0:
        conn.execute("DELETE FROM mirrors")
        conn.commit()
        habits.limpiar_todo(conn)
        db.log(conn, "limpieza_total", f"{borrados} events deleted, all clean", "warn")
    else:
        # La contabilidad se conserva a propósito: es lo que impide que el
        # siguiente ciclo cree un juego nuevo encima de lo que sobrevivió.
        db.log(conn, "limpieza_incompleta",
               f"{borrados} deleted but {quedan} remain. Bookkeeping was kept so "
               f"nothing gets duplicated. Try again.",
               "error")

    return {"borrados": borrados, "fallos": len(errores),
            "errores": errores, "quedan": quedan}
