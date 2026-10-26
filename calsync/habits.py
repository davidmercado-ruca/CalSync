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

from . import config, db, google_cal, rules

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

def ocupa_tiempo(evt, reglas=None) -> bool:
    """¿Este evento le quita tiempo real a David?

    Ojo con la diferencia frente a `rules.should_mirror`: un evento con
    `[nosync]` no genera espejos, pero SÍ ocupa su tiempo. Confundir las dos
    cosas pondría el almuerzo encima de una reunión real.

    `reglas` son los ajustes guardados. Sin ellas solo se aplican las cinco
    condiciones fijas de aquí abajo; la única que es configurable —la de los
    eventos demasiado largos— vive en `rules` y la comparten las dos puertas.
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
    if rules.demasiado_largo(evt, reglas or {}):
        return False
    return True


# El trozo de SQL que deja fuera lo que ignoraste. Vive en una constante para
# que las dos consultas que leen eventos no se separen nunca: si una lo lleva y
# la otra no, CalSync coloca un bloque en un hueco y acto seguido avisa de que
# ese hueco está ocupado.
SIN_IGNORADOS = ("AND NOT EXISTS (SELECT 1 FROM ignored_events g "
                 "WHERE g.account_id = e.account_id AND g.event_id = e.event_id)")


def reglas_de(conn, reglas=None) -> dict:
    """Los ajustes de qué cuenta como tiempo ocupado.

    Quien recorre muchos días los lee una vez y los va pasando; quien llama
    suelto puede no pasar nada y se leen aquí. Así ningún sitio se queda sin
    ellos por olvido, que es justo como nacen los bugs de «la opción no hace
    nada».
    """
    return db.all_settings(conn)["rules"] if reglas is None else reglas


def eventos_reales(conn, dia: date, reglas=None):
    """Las reuniones de verdad de ese día, con nombre y cuenta.

    `ocupado_en` devuelve intervalos unidos, que sirven para colocar bloques
    pero no para explicar un conflicto: «algo choca a la 1» es inútil frente a
    «el Comité de producto de Ruca choca a la 1».
    """
    reglas = reglas_de(conn, reglas)
    desde, hasta = local(dia, time.min), local(dia, time.min) + timedelta(days=1)
        # Lo que ignoraste no se filtra en `ocupa_tiempo` sino aquí, en la
        # consulta: ignorar no es «no me quita tiempo», es «para CalSync este
        # evento no está». Hacerlo en el SQL lo aplica de una vez a las dos
        # lecturas y a la vista de la semana, sin pasar listas por media docena
        # de funciones.
    filas = conn.execute(
        "SELECT e.*, a.label FROM source_events e JOIN accounts a ON a.id = e.account_id "
        "WHERE e.end_utc > ? AND e.start_utc < ? " + SIN_IGNORADOS
        + " ORDER BY e.start_utc",
        (desde.astimezone(timezone.utc).isoformat(), hasta.astimezone(timezone.utc).isoformat()),
    )
    out = []
    for evt in filas:
        if not ocupa_tiempo(evt, reglas):
            continue
        out.append({
            "titulo": evt["summary"] or "(no title)",
            "cuenta": evt["label"],
            "cuenta_id": evt["account_id"],
            "evento_id": evt["event_id"],
            "inicio": datetime.fromisoformat(evt["start_utc"]).astimezone(config.TZ),
            "fin": datetime.fromisoformat(evt["end_utc"]).astimezone(config.TZ),
        })
    return out


def franja(inicio: datetime, fin: datetime) -> str:
    """La franja en hora local, diciendo el día cuando el evento lo cambia.

    Sin esto, un evento que va de las 5 de la mañana del lunes a las 6 de la
    del martes se imprimía «05:00–06:00» y se leía como el más corto del día.
    Era literalmente lo que escondía al culpable: el bloqueo más grande de la
    semana parecía durar una hora.
    """
    if inicio.date() == fin.date():
        return f"{inicio:%H:%M}–{fin:%H:%M}"
    return f"{inicio:%H:%M} → {fin:%b} {fin.day}, {fin:%H:%M}"


def cuando_largo(evt) -> str:
    """La fecha y la franja, para el panel del conflicto.

    En la fila basta la hora, porque la fila ya lleva el día. En el panel el
    evento es el protagonista y tiene que decir cuándo es de principio a fin.
    """
    return f"{evt['inicio']:%b} {evt['inicio'].day}, {franja(evt['inicio'], evt['fin'])}"


def culpable(evt) -> dict:
    """Lo mínimo para poder señalar a un evento desde el dashboard.

    Sin `cuenta_id` y `evento_id` un conflicto es solo texto, y un texto no se
    puede ignorar: hay que saber de qué evento de qué cuenta se está hablando.
    """
    return {
        "cuenta_id": evt["cuenta_id"],
        "evento_id": evt["evento_id"],
        "titulo": evt["titulo"],
        "cuenta": evt["cuenta"],
        "cuando": cuando_largo(evt),
    }


def clave_par(a, b):
    """La pareja de un choque, siempre en el mismo orden.

    Si no se ordena, cambiar la hora de una de las dos reuniones invierte cuál
    se encuentra primero, la clave guardada deja de coincidir, y un aviso que
    descartaste reaparece sin que nada haya cambiado de verdad.
    """
    return tuple(sorted([(a["cuenta_id"], a["evento_id"]),
                         (b["cuenta_id"], b["evento_id"])]))


def se_pisan(a, b) -> bool:
    return a["inicio"] < b["fin"] and b["inicio"] < a["fin"]


def dobles_agendas(conn):
    """Reuniones reales de cuentas distintas que se pisan entre sí.

    Es el conflicto que originó el proyecto y el único que CalSync no puede
    resolver solo: son reuniones con otras personas y no las toca. Lo que sí
    puede es no dejar que te enteres el mismo día a las 9 de la mañana.
    """
    encontrados = []
    reglas = reglas_de(conn)
    descartados = {
        (f["a_account"], f["a_event"], f["b_account"], f["b_event"])
        for f in db.choques_descartados(conn)
    }
    for dia in dias_del_horizonte():
        eventos = eventos_reales(conn, dia, reglas)
        for i, uno in enumerate(eventos):
            for otro in eventos[i + 1:]:
                if uno["cuenta"] == otro["cuenta"]:
                    continue          # dentro de una cuenta lo ves tú mismo
                if not se_pisan(uno, otro):
                    continue
                par = clave_par(uno, otro)
                if (par[0][0], par[0][1], par[1][0], par[1][1]) in descartados:
                    continue          # ya lo viste y dijiste que lo dejáramos
                encontrados.append({
                    "tipo": "doble_agenda",
                    "dia": dia.isoformat(),
                    "hora": franja(max(uno["inicio"], otro["inicio"]),
                                   min(uno["fin"], otro["fin"])),
                    "titulo": f"{uno['titulo']} · {uno['cuenta']}",
                    "detalle": f'overlaps "{otro["titulo"]}" in {otro["cuenta"]}',
                    "culpables": [culpable(uno), culpable(otro)],
                    "par": [list(par[0]), list(par[1])],
                })
    return encontrados


def ocupado_en(conn, dia: date, reglas=None):
    """Las franjas ocupadas de ese día, en hora local, unidas y ordenadas.

    Mira las tres cuentas a la vez: es justo lo que ningún calendario de Google
    puede hacer por sí solo.
    """
    reglas = reglas_de(conn, reglas)
    desde, hasta = local(dia, time.min), local(dia, time.min) + timedelta(days=1)
    filas = conn.execute(
        "SELECT * FROM source_events e WHERE end_utc > ? AND start_utc < ? " + SIN_IGNORADOS,
        (desde.astimezone(timezone.utc).isoformat(), hasta.astimezone(timezone.utc).isoformat()),
    )
    franjas = []
    for evt in filas:
        if not ocupa_tiempo(evt, reglas):
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

        # Se comparan INSTANTES, no el texto de la fecha. «2026-11-05T13:00-05:00»
        # y «2026-11-05T18:00+00:00» son el mismo momento escrito de dos formas,
        # y comparándolos como cadenas son distintos. Eso importa el día que
        # cambie la zona horaria del computador —viajas, macOS te pregunta si la
        # actualizas y dices que sí— porque entonces CADA bloque se leería como
        # movido a mano y CalSync reescribiría la agenda entera.
        escrito = datetime.fromisoformat(fila["wrote_start"])
        if real_ini == escrito and minutos == fila["wrote_minutes"]:
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
    reglas = reglas_de(conn)
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
            ocupado = unir(ocupado_en(conn, dia, reglas) + reservado_por_dia.get(dia, []))

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
                    e for e in eventos_reales(conn, dia, reglas)
                    if e["inicio"] < local(dia, hhmm(habito["window_end"]))
                    and e["fin"] > local(dia, hhmm(habito["window_start"]))
                ]
                nombres = [
                    f'"{e["titulo"]}" ({e["cuenta"]}, {franja(e["inicio"], e["fin"])})'
                    for e in estorban[:4]
                ]
                conflictos.append({
                    "tipo": "bloque_sin_lugar",
                    "dia": clave_dia,
                    "hora": f"{habito['window_start']}–{habito['window_end']}",
                    "titulo": habito["name"],
                    "detalle": (
                        f"not even {habito['min_minutes']} min fitted. "
                        + ("Taken by: " + ", ".join(nombres) if nombres
                           else "the window had no free space")
                    ),
                    # Los mismos cuatro que nombra el texto: el panel no puede
                    # ofrecerte ignorar algo que la fila no mencionó.
                    "culpables": [culpable(e) for e in estorban[:4]],
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
                for evt in eventos_reales(conn, dia, reglas):
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
                        "culpables": [culpable(evt)],
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


# --- La semana que se dibuja en el dashboard --------------------------------
# Antes esto era una lista de «día: bloque, bloque». Una lista no deja ver lo
# único que de verdad importa: dónde quedan los huecos. La rejilla sí.

MESES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Lo mínimo y lo máximo que puede medir la caja del calendario, en píxeles.
# El mínimo evita que una semana floja quede como una rebanada; el máximo evita
# que una semana larga empuje todo lo demás fuera de la pantalla.
ALTO_MINIMO, ALTO_MAXIMO = 320, 620


def reloj12(minutos: int) -> str:
    hora = minutos // 60
    return f"{hora % 12 or 12}{'am' if hora < 12 else 'pm'}"


def reloj12_exacto(minutos: int) -> str:
    """Igual, pero diciendo los minutos cuando no son en punto: 7:30am."""
    hora, resto = divmod(minutos, 60)
    minuto = f":{resto:02d}" if resto else ""
    return f"{hora % 12 or 12}{minuto}{'am' if hora < 12 else 'pm'}"


def carriles(piezas):
    """Reparte en carriles lo que se solapa, igual que Google Calendar.

    Las piezas que se pisan entre sí forman un grupo, y dentro del grupo cada
    una se mete en el primer carril que ya esté libre. El ancho de todas las
    piezas del grupo lo fija el carril más poblado, para que las columnas
    queden parejas y no escalonadas.
    """
    piezas = sorted(piezas, key=lambda p: (p["ini"], -p["fin"]))
    grupos, actual, tope = [], [], None
    for pieza in piezas:
        if tope is not None and pieza["ini"] >= tope:
            grupos.append(actual)
            actual = []
        actual.append(pieza)
        tope = pieza["fin"] if tope is None else max(tope, pieza["fin"])
    if actual:
        grupos.append(actual)

    for grupo in grupos:
        libre_desde = []
        for pieza in grupo:
            carril = next(
                (i for i, fin in enumerate(libre_desde) if fin <= pieza["ini"]), None
            )
            if carril is None:
                carril = len(libre_desde)
                libre_desde.append(0)
            libre_desde[carril] = pieza["fin"]
            pieza["carril"] = carril
        for pieza in grupo:
            pieza["total"] = len(libre_desde)
    return piezas


def _minutos_del_dia(momento: datetime, dia: date) -> int:
    """Minutos desde la medianoche de ESE día, recortado al propio día.

    Una reunión puede empezar la víspera o terminar al día siguiente. Sin
    recortar, la pieza se dibujaría fuera de la columna.
    """
    delta = momento - local(dia, time.min)
    return max(0, min(24 * 60, int(delta.total_seconds() // 60)))


def piezas_del_dia(conn, dia: date, con_reuniones: bool = True, reglas=None):
    """Todo lo que ocupa espacio en la columna de ese día.

    Dibuja lo mismo que el motor tiene en cuenta: si un evento dejó de ocupar
    tu tiempo, tampoco pinta una barra en tu semana. Lo contrario sería ver un
    bloque de 25 horas sobre un hueco donde CalSync acaba de poner el almuerzo.
    """
    piezas = []
    for fila in conn.execute(
        "SELECT s.start_local, s.minutes, h.name, h.color_id "
        "FROM habit_slots s JOIN habits h ON h.id = s.habit_id WHERE s.day = ?",
        (dia.isoformat(),),
    ):
        inicio = datetime.fromisoformat(fila["start_local"])
        fin = inicio + timedelta(minutes=fila["minutes"])
        piezas.append({
            "ini": _minutos_del_dia(inicio, dia),
            "fin": _minutos_del_dia(fin, dia),
            "titulo": (fila["name"] or "").strip(),
            "color": config.COLOR_HEX.get(fila["color_id"] or "", ""),
            "cuenta": "",
            "tipo": "bloque",
        })
    if con_reuniones:
        for evt in eventos_reales(conn, dia, reglas):
            piezas.append({
                "ini": _minutos_del_dia(evt["inicio"], dia),
                "fin": _minutos_del_dia(evt["fin"], dia),
                "titulo": evt["titulo"],
                "color": "",
                "cuenta": evt["cuenta"],
                "tipo": "reunion",
            })
    return [p for p in piezas if p["fin"] > p["ini"]]


def rotulo_semana(lunes: date, domingo: date) -> str:
    if lunes.month == domingo.month:
        return f"{MESES[lunes.month - 1]} {lunes.day} – {domingo.day}, {domingo.year}"
    return (f"{MESES[lunes.month - 1]} {lunes.day} – "
            f"{MESES[domingo.month - 1]} {domingo.day}, {domingo.year}")


def vista_semana(conn, offset: int = 0, con_reuniones: bool = True):
    """La semana lista para dibujar: columnas, posiciones y dónde abrir.

    La rejilla siempre cubre el día completo, de 00:00 a 24:00, para que se
    pueda subir o bajar a cualquier hora. Lo que se ajusta a cada semana es
    DÓNDE abre la vista y qué tan alta es la caja: encuadra lo que esa semana
    tiene de verdad, con media hora de aire, sin recortar nada.
    """
    hoy_ = hoy()
    reglas = reglas_de(conn)
    lunes = hoy_ - timedelta(days=hoy_.weekday()) + timedelta(weeks=offset)
    dias = [lunes + timedelta(days=n) for n in range(7)]

    columnas, mas_temprano, mas_tarde = [], None, None
    for dia in dias:
        piezas = carriles(piezas_del_dia(conn, dia, con_reuniones, reglas))
        for pieza in piezas:
            mas_temprano = (pieza["ini"] if mas_temprano is None
                            else min(mas_temprano, pieza["ini"]))
            mas_tarde = (pieza["fin"] if mas_tarde is None
                         else max(mas_tarde, pieza["fin"]))
            pieza["top"] = pieza["ini"] * config.PX_HORA // 60
            pieza["alto"] = max(
                (pieza["fin"] - pieza["ini"]) * config.PX_HORA // 60 - 2, 15)
            pieza["ancho"] = round(100 / pieza["total"], 4)
            pieza["izq"] = round(pieza["carril"] * 100 / pieza["total"], 4)
            pieza["rango"] = f'{reloj12(pieza["ini"])} – {reloj12(pieza["fin"])}'
            pieza["corto"] = (pieza["fin"] - pieza["ini"]) <= 30
        columnas.append({
            "dia": dia,
            "nombre": DIAS_CORTOS[dia.weekday()].upper(),
            "numero": dia.day,
            "es_hoy": dia == hoy_,
            "piezas": piezas,
        })

        vacia = mas_temprano is None
    if vacia:
        foco, alto = 8 * 60, 420
    else:  # noqa: RET505
                # El scroll abre media hora ANTES de lo primero, para que el bloque no
        # quede pegado al borde. Pero el texto de abajo nombra `primera`, no
        # `foco`: decir «abre a las 4am» cuando lo primero es a las 5 se lee
        # como un error de cálculo, y es solo el aire.
        foco = max(0, ((mas_temprano - 30) // 60) * 60)
        tope = min(24 * 60, ((mas_tarde + 30 + 59) // 60) * 60)
        alto = min(max((tope - foco) * config.PX_HORA // 60, ALTO_MINIMO), ALTO_MAXIMO)

    ahora_px = None
    if any(c["es_hoy"] for c in columnas):
        instante = ahora()
        ahora_px = (instante.hour * 60 + instante.minute) * config.PX_HORA // 60

    usados = {}
    for columna in columnas:
        for pieza in columna["piezas"]:
            if pieza["tipo"] == "bloque":
                usados[pieza["titulo"]] = pieza["color"]

    return {
        "offset": offset,
        "con_reuniones": con_reuniones,
        "etiqueta": rotulo_semana(lunes, dias[-1]),
        "columnas": columnas,
        "horas": [(h, reloj12(h * 60), h * config.PX_HORA) for h in range(24)],
        "alto_dia": 24 * config.PX_HORA,
                "vacia": vacia,
        "alto": alto,
        "foco_px": foco * config.PX_HORA // 60,
        "primera": None if vacia else reloj12_exacto(mas_temprano),
        "ahora_px": ahora_px,
        "leyenda": sorted(usados.items()),
        # Más allá del horizonte no hay nada decidido todavía, y conviene
        # decirlo en vez de mostrar una semana vacía que parece un error.
        "mas_alla": lunes > hoy_ + timedelta(days=config.HABITS_HORIZON_DAYS),
    }


def limpiar_todo(conn):
    """Suelta la contabilidad de hábitos. Los eventos en Google los borra el
    botón de limpieza general, que busca por la marca."""
    conn.execute("DELETE FROM habit_blocks")
    conn.execute("DELETE FROM habit_slots")
    conn.execute("DELETE FROM habit_skips")
    conn.commit()
