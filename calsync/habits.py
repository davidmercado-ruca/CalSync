"""
Bloques de hábitos: almuerzo, lectura, ejercicio, tiempo protegido.

La idea central: un hábito no es un evento, es una *intención* con margen de
maniobra. «Almuerzo hora y media entre 1 y 2:30, y si no cabe me conformo con
una hora» es una regla, no una cita. Este archivo traduce esas reglas en
eventos concretos, día por día.

Dos tipos:

  flexible — busca un hueco dentro de su ventana y se recorta antes que
             desaparecer. Si no cabe ni el mínimo, no se crea y aparece como
             conflicto en el dashboard.
  fixed    — siempre a la misma hora, exista o no algo encima. Sirve para
             proteger tiempo frente a los demás («Not available» de 5 a 8),
             no para reservarte un hueco.

Regla importante: los bloques *fijos* no estorban a los flexibles. Un
«Not available» de 5 a 8 le dice al mundo que no te agende, pero no te impide
poner el ejercicio a las 6. Los flexibles sí se respetan entre ellos, en orden
de prioridad: primero el almuerzo, después lo demás.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone

from . import config, db, google_cal

DIAS_CORTOS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# --- Opciones para los desplegables del dashboard ----------------------------
# Todo lo que se elige en la interfaz sale de estas listas, no de un campo de
# texto libre. Escribir «0 9:00» a mano es demasiado fácil.

DURACIONES = [15, 30, 45, 60, 75, 90, 105, 120, 150, 180, 240, 300, 360]


def etiqueta_duracion(minutos: int) -> str:
    if minutos < 60:
        return f"{minutos} min"
    horas, resto = divmod(minutos, 60)
    if resto == 0:
        return f"{horas} h"
    return f"{horas} h {resto} min"


def opciones_duracion(actual=None):
    """Duraciones disponibles, incluyendo la que ya tenga guardada el hábito
    aunque no esté en la lista (por si viene de una versión anterior)."""
    valores = sorted(set(DURACIONES) | ({int(actual)} if actual else set()))
    return [(m, etiqueta_duracion(m)) for m in valores]


def opciones_hora(actual=None, paso=30):
    """Las horas que se pueden elegir para una ventana, cada media hora.

    Ojo: esto es solo la lista del desplegable. El motor busca dentro de la
    ventana en pasos de `config.PLACEMENT_STEP_MINUTES` (15 min), así que un
    bloque puede perfectamente quedar a las 13:15 aunque la ventana se defina
    de 12:00 a 15:00. La lista corta es para que elegir sea rápido, no para
    limitar dónde cabe el bloque.
    """
    horas = [f"{h:02d}:{m:02d}" for h in range(24) for m in range(0, 60, paso)]
    if actual and actual not in horas:
        horas = sorted(horas + [actual])
    return horas


# --- Utilidades de tiempo ----------------------------------------------------

def hhmm(texto: str) -> time:
    horas, minutos = texto.split(":")
    return time(int(horas), int(minutos))


def local(dia: date, momento: time) -> datetime:
    return datetime.combine(dia, momento, tzinfo=config.TZ)


def ahora() -> datetime:
    return datetime.now(config.TZ)


def hoy() -> date:
    return ahora().date()


def dias_del_horizonte():
    inicio = hoy()
    return [inicio + timedelta(days=n) for n in range(config.HABITS_HORIZON_DAYS)]


def semana_de(dia: date) -> str:
    """Etiqueta de la semana ISO, para contar las veces por semana."""
    año, num, _ = dia.isocalendar()
    return f"{año}-W{num:02d}"


# --- Qué ocupa tiempo de verdad ----------------------------------------------

def ocupa_tiempo(evt) -> bool:
    """¿Este evento le quita tiempo real a David?

    Ojo con la diferencia frente a `rules.should_mirror`: un evento con
    `[nosync]` no genera espejos, pero SÍ ocupa su tiempo. Confundir las dos
    cosas pondría el almuerzo encima de una reunión real.
    """
    if evt["is_mirror"]:
        return False          # los espejos y los propios bloques no cuentan dos veces
    if evt["status"] == "cancelled":
        return False
    if not evt["start_utc"] or not evt["end_utc"]:
        return False
    if evt["all_day"]:
        return False          # un cumpleaños no te impide almorzar
    if evt["transparency"] == "transparent":
        return False          # marcado "Disponible" en Google
    if evt["my_response"] == "declined":
        return False
    return True


def eventos_reales(conn, dia: date):
    """Las reuniones de verdad de ese día, con nombre y cuenta.

    `ocupado_en` devuelve intervalos unidos, que sirven para colocar bloques
    pero no para explicar un conflicto: «algo choca a la 1» es inútil frente a
    «el Comité de producto de Ruca choca a la 1».
    """
    desde, hasta = local(dia, time.min), local(dia, time.min) + timedelta(days=1)
    filas = conn.execute(
        "SELECT e.*, a.label FROM source_events e JOIN accounts a ON a.id = e.account_id "
        "WHERE e.end_utc > ? AND e.start_utc < ? ORDER BY e.start_utc",
        (desde.astimezone(timezone.utc).isoformat(), hasta.astimezone(timezone.utc).isoformat()),
    )
    out = []
    for evt in filas:
        if not ocupa_tiempo(evt):
            continue
        out.append({
            "titulo": evt["summary"] or "(no title)",
            "cuenta": evt["label"],
            "inicio": datetime.fromisoformat(evt["start_utc"]).astimezone(config.TZ),
            "fin": datetime.fromisoformat(evt["end_utc"]).astimezone(config.TZ),
        })
    return out


def franja(inicio: datetime, fin: datetime) -> str:
    return f"{inicio:%H:%M}–{fin:%H:%M}"


def se_pisan(a, b) -> bool:
    return a["inicio"] < b["fin"] and b["inicio"] < a["fin"]


def dobles_agendas(conn):
    """Reuniones reales de cuentas distintas que se pisan entre sí.

    Es el conflicto que originó el proyecto y el único que CalSync no puede
    resolver solo: son reuniones con otras personas y no las toca. Lo que sí
    puede es no dejar que te enteres el mismo día a las 9 de la mañana.
    """
    encontrados = []
    for dia in dias_del_horizonte():
        eventos = eventos_reales(conn, dia)
        for i, uno in enumerate(eventos):
            for otro in eventos[i + 1:]:
                if uno["cuenta"] == otro["cuenta"]:
                    continue          # dentro de una cuenta lo ves tú mismo
                if not se_pisan(uno, otro):
                    continue
                encontrados.append({
                    "tipo": "doble_agenda",
                    "dia": dia.isoformat(),
                    "hora": franja(max(uno["inicio"], otro["inicio"]),
                                   min(uno["fin"], otro["fin"])),
                    "titulo": f"{uno['titulo']} · {uno['cuenta']}",
                    "detalle": f'overlaps "{otro["titulo"]}" in {otro["cuenta"]}',
                })
    return encontrados


def ocupado_en(conn, dia: date):
    """Las franjas ocupadas de ese día, en hora local, unidas y ordenadas.

    Mira las tres cuentas a la vez: es justo lo que ningún calendario de Google
    puede hacer por sí solo.
    """
    desde, hasta = local(dia, time.min), local(dia, time.min) + timedelta(days=1)
    filas = conn.execute(
        "SELECT * FROM source_events WHERE end_utc > ? AND start_utc < ?",
        (desde.astimezone(timezone.utc).isoformat(), hasta.astimezone(timezone.utc).isoformat()),
    )
    franjas = []
    for evt in filas:
        if not ocupa_tiempo(evt):
            continue
        inicio = datetime.fromisoformat(evt["start_utc"]).astimezone(config.TZ)
        fin = datetime.fromisoformat(evt["end_utc"]).astimezone(config.TZ)
        franjas.append((max(inicio, desde), min(fin, hasta)))
    return unir(franjas)


def unir(franjas):
    """Une las franjas que se solapan, para no comparar contra 40 intervalos."""
    if not franjas:
        return []
    franjas = sorted(franjas)
    unidas = [franjas[0]]
    for inicio, fin in franjas[1:]:
        ultimo_inicio, ultimo_fin = unidas[-1]
        if inicio <= ultimo_fin:
            unidas[-1] = (ultimo_inicio, max(ultimo_fin, fin))
        else:
            unidas.append((inicio, fin))
    return unidas


def libre(inicio: datetime, fin: datetime, ocupado) -> bool:
    return all(fin <= o_ini or inicio >= o_fin for o_ini, o_fin in ocupado)


# --- Colocar un hábito en un día --------------------------------------------

def duraciones_a_probar(habito):
    """De la ideal hacia abajo, en pasos, hasta el mínimo aceptable."""
    ideal, minimo = habito["ideal_minutes"], habito["min_minutes"] or habito["ideal_minutes"]
    duraciones, actual = [ideal], ideal - config.SHRINK_STEP_MINUTES
    while actual >= minimo:
        duraciones.append(actual)
        actual -= config.SHRINK_STEP_MINUTES
    return duraciones


def colocar(habito, dia: date, ocupado):
    """Devuelve (inicio, minutos) o (None, None) si no cabe ni recortado.

    Prueba primero la duración ideal en la hora preferida, que es lo que David
    quiere de verdad. Solo después empieza a ceder: primero de sitio, después
    de duración.
    """
    ventana_ini = local(dia, hhmm(habito["window_start"]))
    ventana_fin = local(dia, hhmm(habito["window_end"]))

    # Nada se agenda en el pasado. Sin esto, al abrir el dashboard un martes a
    # las 4 de la tarde aparecería un bloque de lectura a las 6 de la mañana.
    ventana_ini = max(ventana_ini, ahora())

    if habito["kind"] == "fixed":
        arranque = habito["preferred_start"] or habito["window_start"]
        inicio = local(dia, hhmm(arranque))
        return inicio, habito["ideal_minutes"]

    for minutos in duraciones_a_probar(habito):
        largo = timedelta(minutes=minutos)
        candidatos = []
        if habito["preferred_start"]:
            preferida = local(dia, hhmm(habito["preferred_start"]))
            if preferida >= ventana_ini and preferida + largo <= ventana_fin:
                candidatos.append(preferida)
        # Se arranca en el siguiente múltiplo del paso, para que los bloques
        # caigan en horas redondas y no a las 14:07.
        paso = config.PLACEMENT_STEP_MINUTES
        momento = ventana_ini.replace(second=0, microsecond=0)
        sobra = momento.minute % paso
        if sobra:
            momento += timedelta(minutes=paso - sobra)
        while momento + largo <= ventana_fin:
            candidatos.append(momento)
            momento += timedelta(minutes=paso)

        for inicio in candidatos:
            if libre(inicio, inicio + largo, ocupado):
                return inicio, minutos

    return None, None


# --- Decidir todos los slots del horizonte ----------------------------------

def sigue_siendo_legal(habito, dia: date, inicio: datetime, minutos: int,
                       a_mano: bool = False) -> bool:
    """¿Este sitio todavía respeta los ajustes ACTUALES del hábito?

    La regla de estabilidad («si ya estaba colocado y sigue cabiendo, se queda»)
    solo miraba si el hueco estaba libre, nunca si seguía dentro de la ventana.
    Un bloque colocado con la ventana vieja se quedaba ahí para siempre: bastaba
    con que nadie más ocupara ese hueco. Por eso el ejercicio seguía a las 6 de
    la mañana después de cambiar la ventana a 5–10 de la noche.
    """
    if a_mano:
        return True          # lo arrastraste tú: manda tu decisión, no la ventana
    if habito["kind"] == "fixed":
        return True          # un fijo va donde diga su hora; lo decide `colocar`
    dentro = (
        local(dia, hhmm(habito["window_start"])) <= inicio
        and inicio + timedelta(minutes=minutos) <= local(dia, hhmm(habito["window_end"]))
    )
    minimo = habito["min_minutes"] or habito["ideal_minutes"]
    return dentro and minimo <= minutos <= habito["ideal_minutes"]


def dias_validos(habito):
    return {int(d) for d in (habito["days"] or "").split(",") if d.strip().isdigit()}


def adoptar_cambios_manuales(conn):
    """Si moviste o borraste un bloque a mano en la cuenta de origen, te hace caso.

    Es la contraparte de que los bloques tengan un original: mover el bloque de
    almuerzo en tu calendario personal debe reubicarlo en las tres cuentas, no
    que CalSync lo devuelva a su sitio en el siguiente ciclo. Y borrarlo debe
    borrarlo, no recrearlo tres minutos después.

    Solo se escucha la cuenta de origen. Si se escucharan las tres, dos ediciones
    distintas el mismo día se pelearían sin ganador.
    """
    # Cada hábito puede tener su propia cuenta de origen, así que se pregunta
    # hábito por hábito en vez de asumir una sola para todos.
    origen_de = {}
    for habito in db.habits(conn):
        origen = db.cuenta_origen(conn, habito)
        if origen is not None:
            origen_de[habito["id"]] = origen["id"]
    if not origen_de:
        return

    filas = conn.execute(
        "SELECT b.habit_id, b.day, b.event_id, b.account_id, "
        "       b.wrote_start, b.wrote_minutes, "
        "       e.start_utc, e.end_utc, e.status "
        "FROM habit_blocks b "
        # El JOIN no aporta columnas: filtra. Solo se escuchan los días que
        # siguen teniendo una decisión vigente. Sin él, un bloque que ya se
        # descartó volvería a la vida por su lápida en Google.
        "JOIN habit_slots s ON s.habit_id = b.habit_id AND s.day = b.day "
        "LEFT JOIN source_events e ON e.account_id = b.account_id "
        "                         AND e.event_id = b.event_id"
    )

    for fila in filas:
        if origen_de.get(fila["habit_id"]) != fila["account_id"]:
            continue                      # solo manda la cuenta de origen
        if fila["status"] is None:
            continue                      # todavía no lo hemos leído de vuelta

        if fila["status"] == "cancelled":
            conn.execute("INSERT OR IGNORE INTO habit_skips (habit_id, day) VALUES (?, ?)",
                         (fila["habit_id"], fila["day"]))
            conn.execute("DELETE FROM habit_slots WHERE habit_id = ? AND day = ?",
                         (fila["habit_id"], fila["day"]))
            db.log(conn, "bloque_borrado_a_mano",
                   f"block {fila['habit_id']} on {fila['day']}: honoured, not recreated")
            continue

        if not fila["start_utc"] or not fila["end_utc"]:
            continue

        # Se compara contra lo que CalSync ESCRIBIÓ en ese evento, no contra lo
        # que el plan quiere ahora. Son cosas distintas justo después de cambiar
        # los ajustes de un bloque: el plan ya dice 18:00, Google todavía dice
        # 06:00, y nadie movió nada a mano. Comparando contra el plan, CalSync
        # leía su propio retraso como una decisión de David, adoptaba la hora
        # vieja y deshacía su propio cambio. Así quedó el ejercicio a las 6 de
        # la mañana con una ventana de 5 a 10 de la noche.
        if fila["wrote_start"] is None:
            continue                      # versión anterior: no hay con qué comparar

        real_ini = datetime.fromisoformat(fila["start_utc"]).astimezone(config.TZ)
        real_fin = datetime.fromisoformat(fila["end_utc"]).astimezone(config.TZ)
        minutos = int((real_fin - real_ini).total_seconds() // 60)
        if real_ini.isoformat() == fila["wrote_start"] and minutos == fila["wrote_minutes"]:
            continue                      # sigue donde lo dejamos: nadie lo movió

        # `by_hand` marca que esta hora la eligió David, no el motor. Es lo que
        # exime al bloque de la ventana: si lo arrastraste fuera de su horario,
        # fue a propósito, y el siguiente ciclo no debe corregirte.
        conn.execute(
            "UPDATE habit_slots SET start_local = ?, minutes = ?, by_hand = 1 "
            "WHERE habit_id = ? AND day = ?",
            (real_ini.isoformat(), minutos, fila["habit_id"], fila["day"]),
        )
        db.log(conn, "bloque_movido_a_mano",
               f"block {fila['habit_id']} on {fila['day']} → "
               f"{real_ini:%H:%M} ({minutos} min); pushing to the other accounts")

    conn.commit()


def slots_ya_pasados(conn):
    """Un bloque que YA EMPEZÓ se deja exactamente donde está.

    Empezó, no terminó. La diferencia costó el bug del almuerzo. Borrarlo te
    quita el registro de lo que hiciste, y además no tiene sentido reagendar
    algo que está ocurriendo ahora mismo.

    Antes desaparecían por dos vías, y la segunda seguía viva: `dias_del_horizonte`
    arranca hoy, así que los días anteriores no se volvían a decidir; y las
    guardas de «ya no queda ventana» descartaban el bloque de hoy. Protegiendo
    solo lo terminado quedaba un hueco: entre que empieza un bloque y que
    termina, la ventana puede cerrarse debajo de él.

    Al almuerzo le pasaba todos los días. Ventana 11:30–15:00, mínimo 60 min:
    a las 14:00 ya no cabe otro almuerzo completo, así que la guarda lo
    descartaba — pero el almuerzo de ese día iba de 13:00 a 14:30 y todavía
    estaba corriendo. Se borraba a media comida. Ningún otro bloque lo sufría
    porque todos terminan antes de que su propia ventana se quede corta.

    Con `cleanup_past` encendido el hábito sí deja que sus bloques se borren,
    pero solo una vez TERMINADOS: «clear when done» no es «clear a mitad».
    """
    por_id = {h["id"]: h for h in db.habits(conn)}
    quietos = {}
    for fila in conn.execute("SELECT * FROM habit_slots"):
        habito = por_id.get(fila["habit_id"])
        if habito is None:
            continue
        inicio = datetime.fromisoformat(fila["start_local"])
        fin = inicio + timedelta(minutes=fila["minutes"])
        if inicio > ahora():
            continue          # todavía no empieza: lo decide el bucle normal
        if habito["cleanup_past"] and fin <= ahora():
            continue          # terminó, y este hábito pidió que se limpie solo
        quietos[(fila["habit_id"], fila["day"])] = {
            "habit_id": fila["habit_id"],
            "day": fila["day"],
            "start_local": fila["start_local"],
            "minutes": fila["minutes"],
            "by_hand": bool(fila["by_hand"]),
            "name": habito["name"],
            "share": bool(habito["share"]),
            "kind": habito["kind"],
            "color_id": habito["color_id"],
            "origen": db.cuenta_origen(conn, habito),
        }
    return quietos


def decidir_slots(conn):
    """Decide dónde va cada hábito cada día. Devuelve (slots, conflictos).

    Estabilidad ante todo: si un bloque ya estaba colocado y sigue cabiendo, se
    queda donde está. Sin esta regla, los bloques bailarían por el calendario en
    cada ciclo de tres minutos. Y si ya pasó, no se toca nunca más.
    """
    adoptar_cambios_manuales(conn)
    habitos = db.habits(conn, only_enabled=True)
    saltar = {(r["habit_id"], r["day"]) for r in conn.execute("SELECT * FROM habit_skips")}
    previos = {
        (r["habit_id"], r["day"]): r
        for r in conn.execute("SELECT * FROM habit_slots")
    }
    # Lo ya ocurrido entra tal cual y el bucle de abajo no lo vuelve a tocar:
    # solo decide desde hoy hacia adelante.
    slots, conflictos = slots_ya_pasados(conn), []
    # Los flexibles ya colocados se respetan entre ellos; los fijos no estorban.
    reservado_por_dia = {}

    for habito in habitos:
        validos = dias_validos(habito)
        cuota = habito["per_week"] or 0

        # Un bloque que ya ocurrió gastó su cupo de la semana. Sin contarlo,
        # «3 veces por semana» pondría 3 más encima de los que ya hiciste.
        usados_por_semana = {}
        for (hid, dia_hecho) in slots:
            if hid != habito["id"]:
                continue
            hecha = semana_de(date.fromisoformat(dia_hecho))
            usados_por_semana[hecha] = usados_por_semana.get(hecha, 0) + 1

        for dia in dias_del_horizonte():
            if dia.weekday() not in validos:
                continue

            if (habito["id"], dia.isoformat()) in slots:
                continue          # ya ocurrió; no se reagenda el pasado

            semana = semana_de(dia)
            if cuota and usados_por_semana.get(semana, 0) >= cuota:
                continue

            # Si la ventana del día ya se cerró, no es un conflicto: es que el
            # día ya pasó. No se reporta nada.
            if local(dia, hhmm(habito["window_end"])) <= ahora():
                continue

            # Al final del día, lo que queda de ventana puede ser más corto que
            # el mínimo del hábito. Eso no es un conflicto que revisar: es que
            # el día se está acabando. Sin esta regla verías «Exercise no cupo»
            # cada tarde a las siete.
            if habito["kind"] == "flexible":
                queda = local(dia, hhmm(habito["window_end"])) - max(
                    local(dia, hhmm(habito["window_start"])), ahora()
                )
                if queda < timedelta(minutes=habito["min_minutes"] or habito["ideal_minutes"]):
                    continue

            clave_dia = dia.isoformat()
            if (habito["id"], clave_dia) in saltar:
                continue          # lo borraste a mano ese día; se respeta
            ocupado = unir(ocupado_en(conn, dia) + reservado_por_dia.get(dia, []))

            anterior = previos.get((habito["id"], clave_dia))
            inicio = minutos = None
            a_mano = False

            if anterior:
                # ¿Sigue sirviendo donde estaba? Dos preguntas, no una: que el
                # hueco esté libre, y que el sitio siga siendo válido según los
                # ajustes de hoy. Sin la segunda, cambiar la ventana no movía
                # nada de lo ya colocado.
                candidato = datetime.fromisoformat(anterior["start_local"])
                largo = timedelta(minutes=anterior["minutes"])
                cabe = habito["kind"] == "fixed" or libre(candidato, candidato + largo, ocupado)
                if cabe and sigue_siendo_legal(habito, dia, candidato, anterior["minutes"],
                                               bool(anterior["by_hand"])):
                    inicio, minutos = candidato, anterior["minutes"]
                    a_mano = bool(anterior["by_hand"])

            if inicio is None:
                inicio, minutos = colocar(habito, dia, ocupado)

            if inicio is None:
                estorban = [
                    f'"{e["titulo"]}" ({e["cuenta"]}, {franja(e["inicio"], e["fin"])})'
                    for e in eventos_reales(conn, dia)
                    if e["inicio"] < local(dia, hhmm(habito["window_end"]))
                    and e["fin"] > local(dia, hhmm(habito["window_start"]))
                ]
                conflictos.append({
                    "tipo": "bloque_sin_lugar",
                    "dia": clave_dia,
                    "hora": f"{habito['window_start']}–{habito['window_end']}",
                    "titulo": habito["name"],
                    "detalle": (
                        f"not even {habito['min_minutes']} min fitted. "
                        + ("Taken by: " + ", ".join(estorban[:4]) if estorban
                           else "the window had no free space")
                    ),
                })
                continue

            slots[(habito["id"], clave_dia)] = {
                "habit_id": habito["id"],
                "day": clave_dia,
                "start_local": inicio.isoformat(),
                "minutes": minutos,
                "by_hand": a_mano,
                "name": habito["name"],
                "share": bool(habito["share"]),
                "kind": habito["kind"],
                "color_id": habito["color_id"],
                "origen": db.cuenta_origen(conn, habito),
            }
            usados_por_semana[semana] = usados_por_semana.get(semana, 0) + 1

            if habito["kind"] == "flexible":
                reservado_por_dia.setdefault(dia, []).append(
                    (inicio, inicio + timedelta(minutes=minutos))
                )
            else:
                # Un bloque fijo que quedó encima de una reunión real es
                # información útil, no un error: se avisa y se deja.
                fin_bloque = inicio + timedelta(minutes=minutos)
                for evt in eventos_reales(conn, dia):
                    if evt["inicio"] >= fin_bloque or evt["fin"] <= inicio:
                        continue
                    conflictos.append({
                        "tipo": "reunion_en_bloque",
                        "dia": clave_dia,
                        "hora": franja(evt["inicio"], evt["fin"]),
                        "titulo": evt["titulo"],
                        "detalle": (
                            f'{evt["cuenta"]} · falls inside your block '
                            f'"{habito["name"].strip()}"'
                        ),
                    })

    return slots, conflictos


def guardar_slots(conn, slots):
    conn.execute("DELETE FROM habit_slots")
    for slot in slots.values():
        conn.execute(
            "INSERT INTO habit_slots (habit_id, day, start_local, minutes, by_hand) "
            "VALUES (?, ?, ?, ?, ?)",
            (slot["habit_id"], slot["day"], slot["start_local"], slot["minutes"],
             1 if slot.get("by_hand") else 0),
        )
    conn.commit()


# --- Traducir slots a eventos de Google -------------------------------------

def payload(slot, es_origen: bool, nota_generica: str = ""):
    inicio = datetime.fromisoformat(slot["start_local"])
    fin = inicio + timedelta(minutes=slot["minutes"])

    # Todos los bloques llevan el mismo mensaje editable, incluido el original.
    # Cualquier calendario puede tener a alguien mirándolo: si el bloque vive en
    # una cuenta de trabajo, una instrucción interna en español sería ruido para
    # el equipo. Al original se le agrega una línea corta, en el mismo idioma
    # que el resto, indicando que es el editable.
    nota = nota_generica
    if es_origen:
        nota = (nota + "\n\n" if nota else "") + (
            "This is the original — move or delete it here and the copies follow."
        )

    body = {
        # La marca invisible al final: identifica el bloque como nuestro aunque
        # le quites el emoji del nombre.
        "summary": slot["name"] + config.TITLE_MARK,
        "start": {"dateTime": inicio.isoformat(), "timeZone": str(config.TZ)},
        "end": {"dateTime": fin.isoformat(), "timeZone": str(config.TZ)},
        "transparency": "opaque",
        "visibility": "default" if slot["share"] else "private",
        "reminders": {"useDefault": False, "overrides": []},
        "description": nota,
        "extendedProperties": {"private": {
            config.MARKER_KEY: config.MARKER_VALUE,
            "kind": "habit",
            "habit_id": str(slot["habit_id"]),
            "day": slot["day"],
            "home": "1" if es_origen else "0",
        }},
    }
    if slot.get("color_id"):
        body["colorId"] = str(slot["color_id"])

    huella = json.dumps(
        [body["summary"], body["start"], body["end"], body["visibility"],
         body.get("colorId", ""), body["description"]],
        sort_keys=True,
    )
    digest = hashlib.sha1(huella.encode()).hexdigest()[:16]
    body["extendedProperties"]["private"]["hash"] = digest
    return body, digest


def compute_plan(conn):
    """Qué hay que crear, mover o borrar para que los hábitos existan.

    Mismo formato de operación que los espejos, para que se apliquen con el
    mismo código y aparezcan en la misma lista del dashboard.
    """
    slots, conflictos = decidir_slots(conn)
    guardar_slots(conn, slots)
    conflictos += dobles_agendas(conn)
    conflictos.sort(key=lambda c: (c["dia"], c.get("hora", "")))

    nota_generica = db.get_setting(conn, "habit_note") or ""
    todas_las_cuentas = db.accounts(conn)
    existentes = {
        (r["habit_id"], r["day"], r["account_id"]): r
        for r in conn.execute("SELECT * FROM habit_blocks")
    }

    deseados, ops = {}, []
    for slot in slots.values():
        origen = slot["origen"]
        # La cuenta de origen va primero: su bloque es el original y conviene
        # que exista antes que las copias.
        cuentas = sorted(todas_las_cuentas,
                         key=lambda c: 0 if origen and c["id"] == origen["id"] else 1)
        for cuenta in cuentas:
            es_origen = bool(origen and cuenta["id"] == origen["id"])
            body, digest = payload(slot, es_origen, nota_generica)
            clave = (slot["habit_id"], slot["day"], cuenta["id"])
            deseados[clave] = True
            base = {
                "kind": "habit",
                "habit_id": slot["habit_id"],
                "day": slot["day"],
                "target_account_id": cuenta["id"],
                "body": body,
                "hash": digest,
                # La hora que se va a escribir de verdad. Se guarda con el
                # bloque para poder distinguir después un movimiento a mano de
                # un cambio nuestro que todavía no se ha aplicado.
                "slot_start": slot["start_local"],
                "slot_minutes": slot["minutes"],
                "title": slot["name"],
                "from": "Origen" if es_origen else "Copia",
                "to": cuenta["label"],
            }
            fila = existentes.get(clave)
            if fila is None:
                ops.append({**base, "action": "create"})
            elif fila["payload_hash"] != digest:
                ops.append({**base, "action": "update",
                            "target_event_id": fila["event_id"]})

    for clave, fila in existentes.items():
        if clave in deseados:
            continue
        habit_id, dia, cuenta_id = clave
        etiqueta = next((c["label"] for c in todas_las_cuentas if c["id"] == cuenta_id),
                        f"cuenta {cuenta_id}")
        ops.append({
            "action": "delete",
            "kind": "habit",
            "habit_id": habit_id,
            "day": dia,
            "target_account_id": cuenta_id,
            "target_event_id": fila["event_id"],
            "title": "bloque de hábito",
            "from": "Hábito",
            "to": etiqueta,
        })

    return ops, conflictos


def agenda_semana(conn, dias=7):
    """Dónde quedó cada bloque en los próximos días, para verlo de un vistazo."""
    limite = (hoy() + timedelta(days=dias)).isoformat()
    por_dia = {}
    filas = conn.execute(
        "SELECT s.*, h.name FROM habit_slots s JOIN habits h ON h.id = s.habit_id "
        "ORDER BY s.day, s.start_local"
    )
    for fila in filas:
        if fila["day"] > limite:
            continue
        inicio = datetime.fromisoformat(fila["start_local"])
        fin = inicio + timedelta(minutes=fila["minutes"])
        por_dia.setdefault(fila["day"], []).append({
            "name": fila["name"],
            "desde": inicio.strftime("%H:%M"),
            "hasta": fin.strftime("%H:%M"),
            "minutos": fila["minutes"],
        })
    return [
        {
            "dia": dia,
            "etiqueta": f"{DIAS_CORTOS[date.fromisoformat(dia).weekday()]} {dia[8:10]}/{dia[5:7]}",
            "bloques": bloques,
        }
        for dia, bloques in sorted(por_dia.items())
    ]


def limpiar_todo(conn):
    """Suelta la contabilidad de hábitos. Los eventos en Google los borra el
    botón de limpieza general, que busca por la marca."""
    conn.execute("DELETE FROM habit_blocks")
    conn.execute("DELETE FROM habit_slots")
    conn.execute("DELETE FROM habit_skips")
    conn.commit()
