"""
Pruebas del motor de bloques protegidos.

Lo que se verifica: que el almuerzo se recorte antes que desaparecer, que un
bloque ya colocado no salte de sitio sin razón, que las veces por semana se
respeten, y que nada se coloque encima de una reunión real.
"""

import json
from datetime import date, datetime, time, timedelta, timezone

import pytest

from calsync import config, db, habits, rules


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    c = db.connect()
    yield c
    c.close()


@pytest.fixture
def cuentas(conn):
    ids = {}
    for label, email in [("Ruca", "d@ruca.co"), ("Galactic", "d@galactic.co")]:
        ids[label] = db.upsert_account(conn, email, label, json.dumps({"t": "x"}))
    db.ensure_pairs(conn)
    return ids


def proximo_lunes():
    """Un lunes dentro del horizonte, para que los días laborales apliquen."""
    dia = habits.hoy() + timedelta(days=1)
    while dia.weekday() != 0:
        dia += timedelta(days=1)
    return dia


def reunion(conn, account_id, dia, desde: str, hasta: str, event_id="junta"):
    """Mete una reunión real en la copia local, en hora de Bogotá."""
    inicio = habits.local(dia, habits.hhmm(desde))
    fin = habits.local(dia, habits.hhmm(hasta))
    conn.execute(
        "INSERT INTO source_events (account_id, event_id, summary, start_raw, end_raw, "
        "start_utc, end_utc, all_day, transparency, status, my_response, has_guests, "
        "is_mirror, updated) VALUES (?, ?, 'Reunión', '{}', '{}', ?, ?, 0, 'opaque', "
        "'confirmed', 'owner', 1, 0, '')",
        (account_id, f"{event_id}-{dia}-{desde}",
         inicio.astimezone(timezone.utc).isoformat(), fin.astimezone(timezone.utc).isoformat()),
    )
    conn.commit()


def habito(conn, nombre):
    return next(h for h in db.habits(conn) if nombre in h["name"])


def solo(conn, nombre):
    """Deja activo un único hábito, para aislar lo que se está probando."""
    conn.execute("UPDATE habits SET enabled = 0")
    conn.execute("UPDATE habits SET enabled = 1 WHERE name LIKE ?", (f"%{nombre}%",))
    conn.commit()


def slot(slots, habit_id, dia):
    return slots.get((habit_id, dia.isoformat()))


def semanas_hasta(dia):
    """Cuántas semanas hay que avanzar desde la de hoy para llegar a la de `dia`."""
    lunes_hoy = habits.hoy() - timedelta(days=habits.hoy().weekday())
    return (dia - lunes_hoy).days // 7


# --- Los hábitos que vienen de fábrica ---------------------------------------

def test_los_habitos_de_fabrica_existen_y_explore_llega_apagado(conn):
    nombres = {h["name"]: h for h in db.habits(conn)}
    assert any("Lunch" in n for n in nombres)
    assert any("Reading" in n for n in nombres)
    assert any("Exercise" in n for n in nombres)
    assert any("Not available" in n for n in nombres)

    explore = next(h for h in db.habits(conn) if "Explore" in h["name"])
    assert not explore["enabled"], "el bloque de viaje se enciende cuando David viaja"


def test_el_almuerzo_llega_con_las_reglas_que_pidio_david(conn):
    lunch = habito(conn, "Lunch")
    assert lunch["ideal_minutes"] == 90
    assert lunch["min_minutes"] == 60
    assert lunch["preferred_start"] == "13:00"
    assert lunch["window_start"] == "12:00" and lunch["window_end"] == "15:00"
    assert lunch["kind"] == "flexible"


# --- Colocación --------------------------------------------------------------

def test_sin_reuniones_el_almuerzo_va_a_su_hora_preferida(conn, cuentas):
    solo(conn, "Lunch")
    lunes = proximo_lunes()
    slots, conflictos = habits.decidir_slots(conn)

    elegido = slot(slots, habito(conn, "Lunch")["id"], lunes)
    assert elegido["minutes"] == 90
    assert datetime.fromisoformat(elegido["start_local"]).strftime("%H:%M") == "13:00"
    assert conflictos == []


def test_si_hay_una_reunion_encima_el_almuerzo_se_mueve_sin_recortarse(conn, cuentas):
    """Con una reunión de 13:00 a 13:30 quedan 90 min limpios de 13:30 a 15:00,
    así que debe moverse allá y NO recortarse: primero se cede de sitio."""
    solo(conn, "Lunch")
    lunes = proximo_lunes()
    reunion(conn, cuentas["Ruca"], lunes, "13:00", "13:30")

    slots, _ = habits.decidir_slots(conn)
    elegido = slot(slots, habito(conn, "Lunch")["id"], lunes)
    inicio = datetime.fromisoformat(elegido["start_local"])

    assert elegido["minutes"] == 90, "todavía cabe completo, solo en otro sitio"
    assert inicio.strftime("%H:%M") == "13:30"
    assert habits.libre(inicio, inicio + timedelta(minutes=90),
                        habits.ocupado_en(conn, lunes))


def test_cede_de_sitio_antes_que_de_duracion(conn, cuentas):
    """El orden importa: 90 min en otro lado es mejor que 60 min a la hora
    preferida. Si la reunión parte la ventana en dos trozos de 60, sí recorta."""
    solo(conn, "Lunch")
    lunes = proximo_lunes()
    reunion(conn, cuentas["Ruca"], lunes, "13:00", "14:00")

    slots, _ = habits.decidir_slots(conn)
    elegido = slot(slots, habito(conn, "Lunch")["id"], lunes)

    # 12:00–13:00 y 14:00–15:00 son huecos de 60: no cabe hora y media.
    assert elegido["minutes"] == 60
    inicio = datetime.fromisoformat(elegido["start_local"])
    assert habits.libre(inicio, inicio + timedelta(minutes=60),
                        habits.ocupado_en(conn, lunes))


def test_si_no_cabe_hora_y_media_se_recorta_a_una_hora(conn, cuentas):
    """El comportamiento exacto que describió David: primero cede de sitio,
    después de duración, y solo al final se rinde."""
    solo(conn, "Lunch")
    lunes = proximo_lunes()
    # Deja libre solo 12:00–13:00: hora y media no cabe en ninguna parte.
    reunion(conn, cuentas["Ruca"], lunes, "13:00", "15:00", "tarde")

    slots, conflictos = habits.decidir_slots(conn)
    elegido = slot(slots, habito(conn, "Lunch")["id"], lunes)

    assert elegido["minutes"] == 60, "se conforma con el mínimo"
    assert datetime.fromisoformat(elegido["start_local"]).strftime("%H:%M") == "12:00"
    assert conflictos == []


def test_si_no_cabe_ni_el_minimo_se_reporta_como_conflicto(conn, cuentas):
    solo(conn, "Lunch")
    lunes = proximo_lunes()
    reunion(conn, cuentas["Ruca"], lunes, "11:30", "15:30", "maraton")

    slots, conflictos = habits.decidir_slots(conn)

    assert slot(slots, habito(conn, "Lunch")["id"], lunes) is None
    delatado = [c for c in conflictos if c["dia"] == lunes.isoformat()]
    assert delatado and "not even" in delatado[0]["detalle"]
    assert delatado[0]["tipo"] == "bloque_sin_lugar"
    assert "Reunión" in delatado[0]["detalle"], (
        "debe nombrar la reunión que ocupó la franja, no solo decir que no cupo"
    )


def test_nunca_se_coloca_encima_de_una_reunion_de_otra_cuenta(conn, cuentas):
    """La razón de ser del proyecto: la ocupación es la unión de las cuentas."""
    solo(conn, "Lunch")
    lunes = proximo_lunes()
    reunion(conn, cuentas["Galactic"], lunes, "13:00", "14:30", "galactic")

    slots, _ = habits.decidir_slots(conn)
    elegido = slot(slots, habito(conn, "Lunch")["id"], lunes)
    inicio = datetime.fromisoformat(elegido["start_local"])

    assert habits.libre(inicio, inicio + timedelta(minutes=elegido["minutes"]),
                        habits.ocupado_en(conn, lunes))


def test_no_agenda_nada_en_el_pasado(conn, cuentas, monkeypatch):
    """Abrir el dashboard un martes a las 4pm no debe proponer lectura a las
    6am de ese mismo día."""
    solo(conn, "Reading")
    hoy_real = habits.hoy()
    tarde = habits.local(hoy_real, time(16, 0))
    monkeypatch.setattr(habits, "ahora", lambda: tarde)

    slots, conflictos = habits.decidir_slots(conn)

    assert slot(slots, habito(conn, "Reading")["id"], hoy_real) is None
    assert not [c for c in conflictos if c["dia"] == hoy_real.isoformat()], (
        "que el día ya haya pasado no es un conflicto que reportar"
    )


def test_hoy_todavia_sirve_si_la_ventana_no_se_cerro(conn, cuentas, monkeypatch):
    solo(conn, "Lunch")
    hoy_real = habits.hoy()
    manana = habits.local(hoy_real, time(9, 0))
    monkeypatch.setattr(habits, "ahora", lambda: manana)

    slots, _ = habits.decidir_slots(conn)
    if hoy_real.weekday() < 5:   # el almuerzo es de lunes a viernes
        assert slot(slots, habito(conn, "Lunch")["id"], hoy_real) is not None


def test_la_ventana_es_de_media_hora_pero_el_bloque_puede_caer_en_cuartos(conn, cuentas):
    """El desplegable ofrece medias horas para que elegir sea rápido, pero eso
    no debe limitar dónde cabe el bloque. Con una reunión de 13:00 a 13:15 el
    almuerzo tiene que poder arrancar a las 13:15."""
    solo(conn, "Lunch")
    lunes = proximo_lunes()
    reunion(conn, cuentas["Ruca"], lunes, "13:00", "13:15")

    slots, _ = habits.decidir_slots(conn)
    elegido = slot(slots, habito(conn, "Lunch")["id"], lunes)
    inicio = datetime.fromisoformat(elegido["start_local"])

    assert elegido["minutes"] == 90, "no debería recortarse: cabe completo"
    assert inicio.strftime("%H:%M") == "13:15"
    assert "13:15" not in habits.opciones_hora(), (
        "13:15 no está en el desplegable, y aun así el motor lo usa"
    )


def test_el_desplegable_de_horas_va_de_media_en_media(conn):
    horas = habits.opciones_hora()
    assert len(horas) == 48
    assert horas[:3] == ["00:00", "00:30", "01:00"]
    assert "06:30" in horas and "06:15" not in horas


def test_los_bloques_caen_en_horas_redondas(conn, cuentas, monkeypatch):
    solo(conn, "Lunch")
    hoy_real = habits.hoy()
    monkeypatch.setattr(habits, "ahora", lambda: habits.local(hoy_real, time(12, 7)))
    reunion(conn, cuentas["Ruca"], hoy_real, "13:00", "14:00")

    slots, _ = habits.decidir_slots(conn)
    elegido = slot(slots, habito(conn, "Lunch")["id"], hoy_real)
    if elegido:
        inicio = datetime.fromisoformat(elegido["start_local"])
        assert inicio.minute % config.PLACEMENT_STEP_MINUTES == 0, (
            f"un bloque a las {inicio:%H:%M} se ve como un error, no como una decisión"
        )


# --- Estabilidad -------------------------------------------------------------

def test_un_bloque_ya_colocado_no_se_mueve_sin_razon(conn, cuentas):
    """Sin esto, los bloques bailarían por el calendario cada tres minutos."""
    solo(conn, "Lunch")
    lunes = proximo_lunes()
    lunch = habito(conn, "Lunch")["id"]

    primera, _ = habits.decidir_slots(conn)
    habits.guardar_slots(conn, primera)
    elegido_antes = slot(primera, lunch, lunes)["start_local"]

    # Aparece una reunión que NO estorba al almuerzo.
    reunion(conn, cuentas["Ruca"], lunes, "09:00", "10:00", "mañana")
    segunda, _ = habits.decidir_slots(conn)

    assert slot(segunda, lunch, lunes)["start_local"] == elegido_antes


def test_pero_si_deja_de_caber_se_recoloca(conn, cuentas):
    solo(conn, "Lunch")
    lunes = proximo_lunes()
    lunch = habito(conn, "Lunch")["id"]

    primera, _ = habits.decidir_slots(conn)
    habits.guardar_slots(conn, primera)
    antes = slot(primera, lunch, lunes)["start_local"]

    reunion(conn, cuentas["Ruca"], lunes, "13:00", "14:00", "invade")
    segunda, _ = habits.decidir_slots(conn)

    assert slot(segunda, lunch, lunes)["start_local"] != antes


# --- Frecuencia --------------------------------------------------------------

def test_tres_veces_por_semana_coloca_exactamente_tres(conn, cuentas):
    solo(conn, "Exercise")
    slots, _ = habits.decidir_slots(conn)
    ejercicio = habito(conn, "Exercise")["id"]

    por_semana = {}
    for (hid, dia), _ in slots.items():
        if hid != ejercicio:
            continue
        semana = habits.semana_de(datetime.fromisoformat(dia).date())
        por_semana[semana] = por_semana.get(semana, 0) + 1

    # La semana en curso puede venir recortada; las completas deben dar 3.
    completas = [n for semana, n in por_semana.items() if n]
    assert completas, "debería colocar ejercicio"
    assert max(completas) == 3


def test_solo_se_coloca_en_los_dias_marcados(conn, cuentas):
    solo(conn, "Lunch")
    slots, _ = habits.decidir_slots(conn)
    for (_, dia) in slots:
        assert datetime.fromisoformat(dia).weekday() < 5, "el almuerzo es de lunes a viernes"


# --- Bloques de hora fija ----------------------------------------------------

def test_el_bloque_de_tarde_siempre_ocupa_su_horario(conn, cuentas):
    solo(conn, "Not available")
    lunes = proximo_lunes()
    reunion(conn, cuentas["Ruca"], lunes, "17:30", "18:00", "tarde")

    slots, conflictos = habits.decidir_slots(conn)
    elegido = slot(slots, habito(conn, "Not available")["id"], lunes)

    assert datetime.fromisoformat(elegido["start_local"]).strftime("%H:%M") == "17:00"
    assert elegido["minutes"] == 180, "el bloque fijo no se recorta"
    solapes = [c for c in conflictos if c["tipo"] == "reunion_en_bloque"]
    assert solapes, "pero sí avisa del solape"
    assert solapes[0]["titulo"] == "Reunión", "y dice qué reunión es"
    assert "Not available" in solapes[0]["detalle"]


def test_un_bloque_fijo_no_le_estorba_a_uno_flexible(conn, cuentas):
    """«Not available» le dice al mundo que no te agende, no a ti que no
    puedas hacer ejercicio a las 6.

    No basta con que el ejercicio se coloque: tiene que quedar DENTRO del
    bloque fijo. Es el caso que David pidió convertir en lógica en vez de
    tener que arrastrarlo a mano.
    """
    conn.execute("UPDATE habits SET enabled = 0")
    conn.execute("UPDATE habits SET enabled = 1 WHERE name LIKE '%Not available%' "
                 "OR name LIKE '%Exercise%'")
    # El ejercicio solo puede ir dentro del bloque de tarde.
    conn.execute("UPDATE habits SET window_start = '17:00', window_end = '20:00', "
                 "per_week = 0 WHERE name LIKE '%Exercise%'")
    conn.commit()

    slots, _ = habits.decidir_slots(conn)
    lunes = proximo_lunes()
    tarde = slot(slots, habito(conn, "Not available")["id"], lunes)
    ejercicio = slot(slots, habito(conn, "Exercise")["id"], lunes)

    assert tarde and ejercicio
    fijo_ini = datetime.fromisoformat(tarde["start_local"])
    fijo_fin = fijo_ini + timedelta(minutes=tarde["minutes"])
    flex_ini = datetime.fromisoformat(ejercicio["start_local"])
    flex_fin = flex_ini + timedelta(minutes=ejercicio["minutes"])
    assert fijo_ini <= flex_ini and flex_fin <= fijo_fin, (
        f"el ejercicio quedó {flex_ini:%H:%M}–{flex_fin:%H:%M}, fuera del bloque "
        f"fijo {fijo_ini:%H:%M}–{fijo_fin:%H:%M}"
    )


def test_un_bloque_fijo_que_ya_existe_en_google_tampoco_estorba(conn, cuentas):
    """El caso de verdad, no el de laboratorio.

    En producción el «Not available» ya está creado en los tres calendarios y
    lo volvemos a leer de Google. Si al leerlo contara como tiempo ocupado, el
    ejercicio no cabría nunca dentro de él y saldría como conflicto — y la
    única forma de meterlo ahí sería arrastrarlo a mano. Lo que lo salva es que
    todo lo que CalSync crea se lee marcado y `ocupa_tiempo` lo descarta.
    """
    conn.execute("UPDATE habits SET enabled = 0")
    conn.execute("UPDATE habits SET enabled = 1 WHERE name LIKE '%Exercise%'")
    conn.execute("UPDATE habits SET window_start = '17:00', window_end = '20:00', "
                 "per_week = 0 WHERE name LIKE '%Exercise%'")
    conn.commit()
    lunes = proximo_lunes()

    # El bloque fijo tal como vuelve de Google: 17:00–20:00 y con la marca.
    inicio = habits.local(lunes, habits.hhmm("17:00"))
    fin = habits.local(lunes, habits.hhmm("20:00"))
    conn.execute(
        "INSERT INTO source_events (account_id, event_id, summary, start_raw, end_raw, "
        "start_utc, end_utc, all_day, transparency, status, my_response, has_guests, "
        "is_mirror, updated) VALUES (?, 'ooo-1', ?, '{}', '{}', ?, ?, 0, 'opaque', "
        "'confirmed', 'owner', 0, 1, '')",
        (cuentas["Ruca"], "🌙 Not available" + config.TITLE_MARK,
         inicio.astimezone(timezone.utc).isoformat(),
         fin.astimezone(timezone.utc).isoformat()),
    )
    conn.commit()

    assert habits.ocupado_en(conn, lunes) == [], (
        "un bloque propio no puede contar como tiempo ocupado"
    )
    slots, conflictos = habits.decidir_slots(conn)
    ejercicio = slot(slots, habito(conn, "Exercise")["id"], lunes)
    assert ejercicio, "el ejercicio debe caber dentro del OOO ya creado"
    assert not [c for c in conflictos if c["dia"] == lunes.isoformat()]


# --- Los eventos que se generan ---------------------------------------------

def test_el_evento_lleva_la_marca_y_no_notifica(conn, cuentas):
    solo(conn, "Lunch")
    ops, _ = habits.compute_plan(conn)
    assert ops, "debería querer crear bloques"

    for op in ops:
        privados = op["body"]["extendedProperties"]["private"]
        assert privados[config.MARKER_KEY] == config.MARKER_VALUE
        assert privados["kind"] == "habit"
        assert op["body"]["reminders"]["overrides"] == []
        assert "attendees" not in op["body"]


def test_el_bloque_se_crea_en_todas_las_cuentas(conn, cuentas):
    solo(conn, "Lunch")
    ops, _ = habits.compute_plan(conn)
    lunes = proximo_lunes().isoformat()
    del_lunes = [o for o in ops if o["day"] == lunes]
    assert {o["to"] for o in del_lunes} == {"Ruca", "Galactic"}


def test_visible_al_equipo_controla_la_visibilidad(conn, cuentas):
    solo(conn, "Lunch")
    ops, _ = habits.compute_plan(conn)
    assert ops[0]["body"]["visibility"] == "default"

    conn.execute("UPDATE habits SET share = 0 WHERE name LIKE '%Lunch%'")
    conn.execute("DELETE FROM habit_slots")
    conn.commit()
    ops, _ = habits.compute_plan(conn)
    assert ops[0]["body"]["visibility"] == "private"


def test_apagar_un_habito_borra_sus_bloques(conn, cuentas):
    solo(conn, "Lunch")
    ops, _ = habits.compute_plan(conn)
    # Simula que ya se crearon en Google.
    for op in ops:
        conn.execute(
            "INSERT INTO habit_blocks (habit_id, day, account_id, event_id, "
            "payload_hash, updated_at, wrote_start, wrote_minutes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (op["habit_id"], op["day"], op["target_account_id"],
             f"remoto-{op['day']}-{op['target_account_id']}", op["hash"], db.now_iso(),
             op["slot_start"], op["slot_minutes"]),
        )
    conn.commit()

    assert not [o for o in habits.compute_plan(conn)[0] if o["action"] == "create"]

    conn.execute("UPDATE habits SET enabled = 0 WHERE name LIKE '%Lunch%'")
    conn.commit()
    ops, _ = habits.compute_plan(conn)
    assert ops and all(o["action"] == "delete" for o in ops)


# --- Qué cuenta como ocupado -------------------------------------------------

def test_un_evento_con_nosync_sigue_ocupando_tu_tiempo(conn, cuentas):
    """Distinción fina que importa: `[nosync]` significa «no lo espejes», no
    «no existe». Confundirlas pondría el almuerzo encima de algo real."""
    solo(conn, "Lunch")
    lunes = proximo_lunes()
    conn.execute(
        "INSERT INTO source_events (account_id, event_id, summary, start_raw, end_raw, "
        "start_utc, end_utc, all_day, transparency, status, my_response, has_guests, "
        "is_mirror, updated) VALUES (?, 'privado', 'Algo mío [nosync]', '{}', '{}', ?, ?, "
        "0, 'opaque', 'confirmed', 'owner', 0, 0, '')",
        (cuentas["Ruca"],
         habits.local(lunes, time(13, 0)).astimezone(timezone.utc).isoformat(),
         habits.local(lunes, time(14, 30)).astimezone(timezone.utc).isoformat()),
    )
    conn.commit()

    slots, _ = habits.decidir_slots(conn)
    elegido = slot(slots, habito(conn, "Lunch")["id"], lunes)
    inicio = datetime.fromisoformat(elegido["start_local"])
    assert inicio.strftime("%H:%M") != "13:00", "no puede pisar el evento privado"


@pytest.mark.parametrize("campo,valor", [
    ("transparency", "transparent"),   # marcado Disponible
    ("all_day", 1),                    # cumpleaños, feriados
    ("my_response", "declined"),       # invitación rechazada
    ("is_mirror", 1),                  # un espejo de CalSync
    ("status", "cancelled"),           # borrado
])
def test_lo_que_no_ocupa_tiempo(conn, campo, valor):
    base = {
        "is_mirror": 0, "status": "confirmed", "start_utc": "2026-08-03T18:00:00+00:00",
        "end_utc": "2026-08-03T19:00:00+00:00", "all_day": 0,
        "transparency": "opaque", "my_response": "owner",
    }
    assert habits.ocupa_tiempo(base), "el caso base sí ocupa"
    assert not habits.ocupa_tiempo({**base, campo: valor})


# --- Borrar y deshacer -------------------------------------------------------

def test_borrar_un_habito_se_puede_deshacer(conn, cuentas):
    """El clic único necesita red de seguridad: borrar un hábito también borra
    sus eventos de los tres calendarios."""
    from calsync.app import CAMPOS_HABITO

    lunch = habito(conn, "Lunch")
    copia = {c: lunch[c] for c in CAMPOS_HABITO}

    # Lo que hace el endpoint de borrar.
    db.set_setting(conn, "papelera", copia)
    conn.execute("DELETE FROM habits WHERE id = ?", (lunch["id"],))
    conn.execute("DELETE FROM habit_slots WHERE habit_id = ?", (lunch["id"],))
    conn.commit()
    assert not [h for h in db.habits(conn) if "Lunch" in h["name"]]

    # Y lo que hace deshacer.
    guardado = db.get_setting(conn, "papelera")
    columnas = ", ".join(CAMPOS_HABITO)
    marcas = ", ".join("?" for _ in CAMPOS_HABITO)
    conn.execute(f"INSERT INTO habits ({columnas}, created_at) VALUES ({marcas}, ?)",
                 tuple(guardado[c] for c in CAMPOS_HABITO) + (db.now_iso(),))
    conn.commit()

    vuelto = habito(conn, "Lunch")
    assert vuelto["ideal_minutes"] == 90 and vuelto["min_minutes"] == 60
    assert vuelto["preferred_start"] == "13:00" and vuelto["days"] == lunch["days"]
    assert vuelto["kind"] == "flexible" and vuelto["share"] == lunch["share"]


# --- Cuenta de origen y cambios manuales -------------------------------------

@pytest.fixture
def con_personal(conn):
    """Tres cuentas, una de ellas personal (Gmail)."""
    ids = {}
    for label, email in [("Ruca", "d@ruca.co"), ("Galactic", "d@galactic.co"),
                         ("Personal", "tu-correo@gmail.com")]:
        ids[label] = db.upsert_account(conn, email, label, json.dumps({"t": "x"}))
    db.ensure_pairs(conn)
    return ids


def test_el_origen_se_elige_solo_y_es_la_cuenta_personal(conn, con_personal):
    """Los hábitos son tiempo de David, no de una organización: su original
    debe vivir en su cuenta y no dentro del Workspace de una empresa."""
    origen = db.cuenta_origen(conn)
    assert origen["email"] == "tu-correo@gmail.com"


def test_se_puede_forzar_otra_cuenta_de_origen(conn, con_personal):
    db.set_setting(conn, "home_account_id", con_personal["Ruca"])
    assert db.cuenta_origen(conn)["label"] == "Ruca"


def test_el_original_va_primero_y_se_marca_como_tal(conn, con_personal):
    solo(conn, "Lunch")
    ops, _ = habits.compute_plan(conn)
    lunes = proximo_lunes().isoformat()
    del_dia = [o for o in ops if o["day"] == lunes]

    assert del_dia[0]["to"] == "Personal", "el original se crea primero"
    assert del_dia[0]["from"] == "Origen"
    assert del_dia[0]["body"]["extendedProperties"]["private"]["home"] == "1"

    copias = [o for o in del_dia[1:]]
    assert all(o["from"] == "Copia" for o in copias)
    assert all(o["body"]["extendedProperties"]["private"]["home"] == "0" for o in copias)

    # Ningún bloque debe llevar instrucciones internas en español: cualquier
    # calendario puede tener a alguien mirándolo. El original solo agrega una
    # línea corta, en el mismo idioma que el resto.
    nota = db.get_setting(conn, "habit_note")
    assert copias[0]["body"]["description"] == nota
    assert del_dia[0]["body"]["description"].startswith(nota)
    assert "original" in del_dia[0]["body"]["description"]

    for op in del_dia:
        texto = op["body"]["description"]
        for palabra in ("dashboard", "recalcula", "Bloque de CalSync", "no sirve"):
            assert palabra not in texto, f"«{palabra}» es texto interno y se lee"


def test_el_color_del_bloque_viaja_a_google(conn, con_personal):
    solo(conn, "Lunch")
    ops, _ = habits.compute_plan(conn)
    assert ops[0]["body"]["colorId"] == "5", "Lunch viene en Banana"

    conn.execute("UPDATE habits SET color_id = '11' WHERE name LIKE '%Lunch%'")
    conn.execute("DELETE FROM habit_slots")
    conn.commit()
    ops, _ = habits.compute_plan(conn)
    assert ops[0]["body"]["colorId"] == "11"


def test_sin_color_no_se_manda_el_campo(conn, con_personal):
    solo(conn, "Lunch")
    conn.execute("UPDATE habits SET color_id = '' WHERE name LIKE '%Lunch%'")
    conn.commit()
    ops, _ = habits.compute_plan(conn)
    assert "colorId" not in ops[0]["body"]


def test_todo_bloque_lleva_la_marca_invisible(conn, con_personal):
    """Cubre los bloques sin emoji: «Lunch» a secas también se reconoce."""
    solo(conn, "Lunch")
    conn.execute("UPDATE habits SET name = 'Lunch' WHERE name LIKE '%Lunch%'")
    conn.execute("DELETE FROM habit_slots")
    conn.commit()

    ops, _ = habits.compute_plan(conn)
    titulo = ops[0]["body"]["summary"]

    assert config.TITLE_MARK in titulo
    assert titulo.replace(config.TITLE_MARK, "") == "Lunch", "invisible para un humano"

    from calsync import google_cal
    assert google_cal.is_mirror({"id": "x", "summary": titulo}), (
        "sin marca privada y sin emoji, el título invisible tiene que bastar"
    )


def bloque_en_google(conn, habit_id, dia, account_id, event_id, desde, minutos,
                     escrito=None):
    """Simula que el bloque ya existe en Google y que lo leímos de vuelta.

    `escrito` es la hora que CalSync dejó escrita en ese evento. Cuando difiere
    de `desde`, la diferencia es un movimiento a mano. Por defecto se toma del
    slot ya guardado, que es lo que CalSync habría escrito.
    """
    inicio = habits.local(dia, habits.hhmm(desde))
    fin = inicio + timedelta(minutes=minutos)
    if escrito is None:
        fila = conn.execute(
            "SELECT start_local, minutes FROM habit_slots WHERE habit_id = ? AND day = ?",
            (habit_id, dia.isoformat()),
        ).fetchone()
        escrito = ((fila["start_local"], fila["minutes"]) if fila
                   else (inicio.isoformat(), minutos))
    else:
        escrito = (habits.local(dia, habits.hhmm(escrito[0])).isoformat(), escrito[1])
    conn.execute(
        "INSERT INTO habit_blocks (habit_id, day, account_id, event_id, payload_hash, "
        "updated_at, wrote_start, wrote_minutes) VALUES (?, ?, ?, ?, 'h', ?, ?, ?)",
        (habit_id, dia.isoformat(), account_id, event_id, db.now_iso(),
         escrito[0], escrito[1]))
    conn.execute(
        "INSERT INTO source_events (account_id, event_id, summary, start_raw, end_raw, "
        "start_utc, end_utc, all_day, transparency, status, my_response, has_guests, "
        "is_mirror, updated) VALUES (?, ?, 'Lunch', '{}', '{}', ?, ?, 0, 'opaque', "
        "'confirmed', 'owner', 0, 1, '')",
        (account_id, event_id,
         inicio.astimezone(timezone.utc).isoformat(), fin.astimezone(timezone.utc).isoformat()),
    )
    conn.commit()


def test_mover_el_bloque_en_la_cuenta_de_origen_lo_propaga(conn, con_personal):
    """Es la contraparte de tener un original: si lo mueves ahí, CalSync te
    hace caso en vez de devolverlo a su sitio."""
    solo(conn, "Lunch")
    lunes = proximo_lunes()
    lunch = habito(conn, "Lunch")["id"]

    slots, _ = habits.decidir_slots(conn)
    habits.guardar_slots(conn, slots)
    assert datetime.fromisoformat(
        slot(slots, lunch, lunes)["start_local"]).strftime("%H:%M") == "13:00"

    # David lo arrastra a las 11:30 en su calendario personal.
    bloque_en_google(conn, lunch, lunes, con_personal["Personal"], "orig-1", "11:30", 60)

    slots, _ = habits.decidir_slots(conn)
    elegido = slot(slots, lunch, lunes)

    assert datetime.fromisoformat(elegido["start_local"]).strftime("%H:%M") == "11:30"
    assert elegido["minutes"] == 60, "también adopta la duración que le dio"


def test_mover_una_copia_no_cambia_nada(conn, con_personal):
    """Solo manda la cuenta de origen. Si mandaran las tres, dos ediciones
    distintas el mismo día se pelearían sin ganador."""
    solo(conn, "Lunch")
    lunes = proximo_lunes()
    lunch = habito(conn, "Lunch")["id"]

    slots, _ = habits.decidir_slots(conn)
    habits.guardar_slots(conn, slots)

    bloque_en_google(conn, lunch, lunes, con_personal["Ruca"], "copia-1", "11:30", 60)

    slots, _ = habits.decidir_slots(conn)
    assert datetime.fromisoformat(
        slot(slots, lunch, lunes)["start_local"]).strftime("%H:%M") == "13:00"


def test_borrar_el_bloque_en_la_cuenta_de_origen_no_lo_recrea(conn, con_personal):
    """Sin esto sentirías que la herramienta no te hace caso: lo borras y
    reaparece tres minutos después."""
    solo(conn, "Lunch")
    lunes = proximo_lunes()
    lunch = habito(conn, "Lunch")["id"]

    slots, _ = habits.decidir_slots(conn)
    habits.guardar_slots(conn, slots)
    bloque_en_google(conn, lunch, lunes, con_personal["Personal"], "orig-1", "13:00", 90)

    # David lo borra: Google nos lo devuelve como «cancelled».
    conn.execute("UPDATE source_events SET status = 'cancelled' WHERE event_id = 'orig-1'")
    conn.commit()

    slots, _ = habits.decidir_slots(conn)
    assert slot(slots, lunch, lunes) is None

    # Y sigue sin recrearse en los ciclos siguientes.
    slots, _ = habits.decidir_slots(conn)
    assert slot(slots, lunch, lunes) is None

    # Pero si cambia la regla, la decisión se olvida y vuelve a colocarse.
    conn.execute("DELETE FROM habit_skips")
    conn.commit()
    slots, _ = habits.decidir_slots(conn)
    assert slot(slots, lunch, lunes) is not None


# --- Detección de doble agenda -----------------------------------------------
# Es el conflicto que originó el proyecto y el único que CalSync no puede
# resolver solo: son reuniones con otras personas.

def test_detecta_dos_reuniones_que_se_pisan_entre_cuentas(conn, cuentas):
    lunes = proximo_lunes()
    reunion(conn, cuentas["Ruca"], lunes, "10:00", "11:00", "comite")
    reunion(conn, cuentas["Galactic"], lunes, "10:30", "11:30", "roadmap")

    choques = habits.dobles_agendas(conn)
    del_dia = [c for c in choques if c["dia"] == lunes.isoformat()]

    assert len(del_dia) == 1
    assert del_dia[0]["tipo"] == "doble_agenda"
    assert del_dia[0]["hora"] == "10:30–11:00", "la franja del choque, no la del evento"
    assert "Ruca" in del_dia[0]["titulo"]
    assert "Galactic" in del_dia[0]["detalle"]


def test_dos_reuniones_de_la_misma_cuenta_no_se_reportan(conn, cuentas):
    """Dentro de una cuenta el choque lo ves tú mismo en Google. Reportarlo
    sería ruido que tapa los que sí importan."""
    lunes = proximo_lunes()
    reunion(conn, cuentas["Ruca"], lunes, "10:00", "11:00", "una")
    reunion(conn, cuentas["Ruca"], lunes, "10:30", "11:30", "otra")

    assert [c for c in habits.dobles_agendas(conn) if c["dia"] == lunes.isoformat()] == []


def test_reuniones_pegadas_no_son_un_choque(conn, cuentas):
    """Una que termina a las 11:00 y otra que arranca a las 11:00 no se pisan."""
    lunes = proximo_lunes()
    reunion(conn, cuentas["Ruca"], lunes, "10:00", "11:00", "antes")
    reunion(conn, cuentas["Galactic"], lunes, "11:00", "12:00", "despues")

    assert [c for c in habits.dobles_agendas(conn) if c["dia"] == lunes.isoformat()] == []


def test_un_espejo_no_cuenta_como_doble_agenda(conn, cuentas):
    """Cuando CalSync está activo, cada reunión tiene su espejo en la otra
    cuenta. Si los espejos contaran, TODO se vería como doble agenda."""
    lunes = proximo_lunes()
    reunion(conn, cuentas["Ruca"], lunes, "10:00", "11:00", "real")
    inicio = habits.local(lunes, habits.hhmm("10:00"))
    conn.execute(
        "INSERT INTO source_events (account_id, event_id, summary, start_raw, end_raw, "
        "start_utc, end_utc, all_day, transparency, status, my_response, has_guests, "
        "is_mirror, updated) VALUES (?, 'espejo', '🔒 Busy (Ruca)', '{}', '{}', ?, ?, 0, "
        "'opaque', 'confirmed', 'owner', 0, 1, '')",
        (cuentas["Galactic"], inicio.astimezone(timezone.utc).isoformat(),
         (inicio + timedelta(hours=1)).astimezone(timezone.utc).isoformat()),
    )
    conn.commit()

    assert [c for c in habits.dobles_agendas(conn) if c["dia"] == lunes.isoformat()] == []


def test_los_conflictos_llegan_ordenados_por_fecha(conn, cuentas):
    lunes = proximo_lunes()
    reunion(conn, cuentas["Ruca"], lunes + timedelta(days=2), "10:00", "11:00", "jueves")
    reunion(conn, cuentas["Galactic"], lunes + timedelta(days=2), "10:30", "11:30", "jueves2")
    reunion(conn, cuentas["Ruca"], lunes, "10:00", "11:00", "lunes")
    reunion(conn, cuentas["Galactic"], lunes, "10:30", "11:30", "lunes2")

    _, conflictos = habits.compute_plan(conn)
    fechas = [c["dia"] for c in conflictos]
    assert fechas == sorted(fechas), "lo más urgente primero"


# --- Cuenta de origen por bloque ---------------------------------------------

def test_cada_bloque_puede_tener_su_propia_cuenta_de_origen(conn, con_personal):
    """El almuerzo nace en la personal, pero el bloque de tarde tiene más
    sentido naciendo en la cuenta de trabajo."""
    conn.execute("UPDATE habits SET enabled = 1")
    conn.execute("UPDATE habits SET home_account_id = ? WHERE name LIKE '%Not available%'",
                 (con_personal["Ruca"],))
    conn.commit()

    lunch = habito(conn, "Lunch")
    tarde = habito(conn, "Not available")

    assert db.cuenta_origen(conn, lunch)["label"] == "Personal", "usa la general"
    assert db.cuenta_origen(conn, tarde)["label"] == "Ruca", "la propia manda"

    ops, _ = habits.compute_plan(conn)
    del_tarde = [o for o in ops if o["habit_id"] == tarde["id"] and o["from"] == "Origen"]
    assert del_tarde and all(o["to"] == "Ruca" for o in del_tarde)


def test_se_adopta_el_cambio_manual_de_la_cuenta_de_origen_propia(conn, con_personal):
    """Si el bloque nace en Ruca, es Ruca la que manda para ese bloque — no la
    cuenta de origen general."""
    solo(conn, "Not available")
    conn.execute("UPDATE habits SET home_account_id = ?, kind = 'flexible' "
                 "WHERE name LIKE '%Not available%'", (con_personal["Ruca"],))
    conn.commit()
    lunes = proximo_lunes()
    tarde = habito(conn, "Not available")["id"]

    slots, _ = habits.decidir_slots(conn)
    habits.guardar_slots(conn, slots)

    bloque_en_google(conn, tarde, lunes, con_personal["Ruca"], "orig-ruca", "18:00", 60)

    slots, _ = habits.decidir_slots(conn)
    elegido = slot(slots, tarde, lunes)
    assert datetime.fromisoformat(elegido["start_local"]).strftime("%H:%M") == "18:00"


def test_cambiar_la_ventana_reubica_lo_que_ya_estaba_colocado(conn, cuentas):
    """El bug del ejercicio a las 6 de la mañana.

    La regla de estabilidad («si ya estaba colocado y sigue cabiendo, se queda»)
    solo preguntaba si el hueco estaba libre, nunca si el sitio seguía dentro de
    la ventana. Un bloque puesto con la ventana de fábrica (06:00–20:00) se
    quedaba a las 6am para siempre después de cambiarla a 17:00–22:00.
    """
    solo(conn, "Exercise")
    ejercicio = habito(conn, "Exercise")["id"]
    conn.execute("UPDATE habits SET per_week = 0 WHERE id = ?", (ejercicio,))
    conn.commit()
    lunes = proximo_lunes()

    slots, _ = habits.decidir_slots(conn)
    habits.guardar_slots(conn, slots)
    antes = datetime.fromisoformat(slot(slots, ejercicio, lunes)["start_local"])
    assert antes.hour < 17, "la ventana de fábrica arranca a las 6am"

    conn.execute(
        "UPDATE habits SET window_start = '17:00', window_end = '22:00', "
        "preferred_start = '18:00' WHERE id = ?", (ejercicio,)
    )
    conn.commit()

    slots, _ = habits.decidir_slots(conn)
    despues = datetime.fromisoformat(slot(slots, ejercicio, lunes)["start_local"])
    assert despues.time() >= time(17, 0), (
        f"quedó a las {despues:%H:%M}, fuera de la ventana que ahora dice 17:00–22:00"
    )


def test_no_confunde_su_propio_cambio_con_un_movimiento_a_mano(conn, con_personal):
    """La causa real del ejercicio a las 6am.

    Al cambiar la ventana, el plan ya dice la hora nueva pero Google todavía
    tiene la vieja: el ciclo no ha alcanzado a aplicarla. Comparando contra el
    plan en vez de contra lo que se escribió, CalSync leía su propio retraso
    como una decisión de David, adoptaba la hora vieja y deshacía su cambio.
    """
    solo(conn, "Lunch")
    lunes = proximo_lunes()
    lunch = habito(conn, "Lunch")["id"]

    slots, _ = habits.decidir_slots(conn)
    habits.guardar_slots(conn, slots)
    # El evento en Google está donde CalSync lo escribió: 13:00, 90 min.
    bloque_en_google(conn, lunch, lunes, con_personal["Personal"], "orig-1", "13:00", 90)

    # David cambia la ventana. El plan se recalcula y se guarda…
    conn.execute(
        "UPDATE habits SET window_start = '12:00', window_end = '12:30', "
        "ideal_minutes = 30, min_minutes = 30, preferred_start = '12:00' "
        "WHERE id = ?", (lunch,)
    )
    conn.commit()
    slots, _ = habits.decidir_slots(conn)
    habits.guardar_slots(conn, slots)
    assert datetime.fromisoformat(
        slot(slots, lunch, lunes)["start_local"]).strftime("%H:%M") == "12:00"

    # …pero Google sigue en 13:00 hasta que el ciclo escriba. Eso no es un
    # movimiento a mano y no debe deshacer nada.
    slots, _ = habits.decidir_slots(conn)
    assert datetime.fromisoformat(
        slot(slots, lunch, lunes)["start_local"]).strftime("%H:%M") == "12:00", (
        "CalSync adoptó su propio retraso como si David hubiera movido el bloque"
    )


def test_un_bloque_movido_a_mano_fuera_de_la_ventana_se_respeta(conn, con_personal):
    """La otra cara de la moneda: revisar la ventana no puede pisar una
    decisión tuya. Si arrastras el almuerzo a las 11:30, ahí se queda."""
    solo(conn, "Lunch")
    lunes = proximo_lunes()
    lunch = habito(conn, "Lunch")["id"]

    slots, _ = habits.decidir_slots(conn)
    habits.guardar_slots(conn, slots)
    bloque_en_google(conn, lunch, lunes, con_personal["Personal"], "orig-1", "11:30", 60)

    slots, _ = habits.decidir_slots(conn)
    habits.guardar_slots(conn, slots)
    assert datetime.fromisoformat(
        slot(slots, lunch, lunes)["start_local"]).strftime("%H:%M") == "11:30"

    # Y sigue ahí en los ciclos siguientes, aunque 11:30 esté fuera de 12:00–15:00.
    slots, _ = habits.decidir_slots(conn)
    assert datetime.fromisoformat(
        slot(slots, lunch, lunes)["start_local"]).strftime("%H:%M") == "11:30"


def test_que_el_dia_se_acabe_no_es_un_conflicto(conn, cuentas, monkeypatch):
    """A las 19:20 la ventana de ejercicio (06:00–20:00) solo tiene 40 min
    libres. Reportarlo como conflicto sería ruido cada tarde."""
    solo(conn, "Exercise")
    hoy_real = habits.hoy()
    monkeypatch.setattr(habits, "ahora", lambda: habits.local(hoy_real, time(19, 20)))

    _, conflictos = habits.decidir_slots(conn)

    assert not [c for c in conflictos if c["dia"] == hoy_real.isoformat()]


# --- Los bloques que ya pasaron ---------------------------------------------
# Sección escrita a raíz de un bug real: el almuerzo desaparecía del calendario
# pasadas las 15:00 y los bloques de ayer se borraban cada medianoche. Causa:
# `dias_del_horizonte` arranca hoy y las guardas de «el día se acabó»
# descartaban el slot, y todo lo que no se vuelve a decidir se borra.

def test_un_bloque_que_ya_paso_no_desaparece(conn, cuentas, monkeypatch):
    solo(conn, "Lunch")
    hoy_real = habits.hoy()
    lunch = habito(conn, "Lunch")["id"]

    # 12:30: el almuerzo se coloca normalmente.
    monkeypatch.setattr(habits, "ahora", lambda: habits.local(hoy_real, time(12, 30)))
    slots, _ = habits.decidir_slots(conn)
    habits.guardar_slots(conn, slots)
    colocado = slot(slots, lunch, hoy_real)
    assert colocado, "debería colocarse a las 12:30"

    # 16:00: la ventana (12:00–15:00) ya cerró. El bloque debe seguir ahí.
    monkeypatch.setattr(habits, "ahora", lambda: habits.local(hoy_real, time(16, 0)))
    slots, _ = habits.decidir_slots(conn)
    sigue = slot(slots, lunch, hoy_real)

    assert sigue is not None, "el almuerzo de hoy no se puede borrar porque ya pasó"
    assert sigue["start_local"] == colocado["start_local"]
    assert sigue["minutes"] == colocado["minutes"]


def test_los_bloques_de_ayer_sobreviven_a_la_medianoche(conn, cuentas, monkeypatch):
    """Ayer está fuera del horizonte, que arranca hoy. Sin protección explícita
    todo bloque anterior se borraba en el primer ciclo del día siguiente."""
    solo(conn, "Lunch")
    ayer = habits.hoy() - timedelta(days=1)
    lunch = habito(conn, "Lunch")["id"]

    conn.execute(
        "INSERT INTO habit_slots (habit_id, day, start_local, minutes) VALUES (?, ?, ?, 90)",
        (lunch, ayer.isoformat(), habits.local(ayer, time(13, 0)).isoformat()),
    )
    conn.commit()

    slots, _ = habits.decidir_slots(conn)
    assert slot(slots, lunch, ayer) is not None


def test_un_bloque_en_curso_no_se_borra_aunque_la_ventana_se_cierre(conn, cuentas, monkeypatch):
    """El bug del almuerzo: se borraba a media comida, todos los días.

    Ventana 11:30–15:00, mínimo 60 min. A las 14:00 ya no cabe otro almuerzo
    completo dentro de la ventana, así que la guarda de «ya no queda día» lo
    descartaba — y en este motor no volver a decidir algo ES borrarlo. Pero el
    almuerzo de ese día iba de 13:00 a 14:30 y todavía estaba ocurriendo.

    Ningún otro bloque lo sufría: todos terminan antes de que su propia ventana
    se quede más corta que su mínimo. Por eso solo se quejaba del Lunch.
    """
    solo(conn, "Lunch")
    lunch = habito(conn, "Lunch")["id"]
    hoy_real = habits.hoy()

    # Mediodía: el almuerzo se coloca normalmente a la 1.
    monkeypatch.setattr(habits, "ahora", lambda: habits.local(hoy_real, time(12, 0)))
    slots, _ = habits.decidir_slots(conn)
    habits.guardar_slots(conn, slots)
    colocado = slot(slots, lunch, hoy_real)
    assert datetime.fromisoformat(colocado["start_local"]).strftime("%H:%M") == "13:00"
    assert colocado["minutes"] == 90, "13:00–14:30"

    # 14:01 — el bloque está corriendo, pero en la ventana ya no caben 60 min.
    monkeypatch.setattr(habits, "ahora", lambda: habits.local(hoy_real, time(14, 1)))
    slots, _ = habits.decidir_slots(conn)
    vivo = slot(slots, lunch, hoy_real)
    assert vivo is not None, "se borró el almuerzo mientras David estaba almorzando"
    assert datetime.fromisoformat(vivo["start_local"]).strftime("%H:%M") == "13:00"

    # Y el plan no debe pedir borrarlo de ningún calendario.
    ops, _ = habits.compute_plan(conn)
    assert not [o for o in ops
                if o["action"] == "delete" and o.get("day") == hoy_real.isoformat()]


def test_con_cleanup_past_tampoco_se_borra_a_media_comida(conn, cuentas, monkeypatch):
    """«Clear when done» significa terminado, no a mitad. Con la opción
    encendida el bloque sigue vivo mientras corre, y se va después."""
    solo(conn, "Lunch")
    lunch = habito(conn, "Lunch")["id"]
    conn.execute("UPDATE habits SET cleanup_past = 1 WHERE id = ?", (lunch,))
    conn.commit()
    hoy_real = habits.hoy()

    monkeypatch.setattr(habits, "ahora", lambda: habits.local(hoy_real, time(12, 0)))
    slots, _ = habits.decidir_slots(conn)
    habits.guardar_slots(conn, slots)

    monkeypatch.setattr(habits, "ahora", lambda: habits.local(hoy_real, time(14, 1)))
    slots, _ = habits.decidir_slots(conn)
    assert slot(slots, lunch, hoy_real) is not None, "todavía está almorzando"
    habits.guardar_slots(conn, slots)

    # 14:31: ya terminó, y este hábito sí pidió limpiarse solo.
    monkeypatch.setattr(habits, "ahora", lambda: habits.local(hoy_real, time(14, 31)))
    slots, _ = habits.decidir_slots(conn)
    assert slot(slots, lunch, hoy_real) is None, "con cleanup_past debe irse al terminar"


def test_no_se_emite_ningun_borrado_para_bloques_pasados(conn, cuentas):
    """La prueba que habría atrapado el bug: el plan no debe querer borrar
    nada de lo que ya ocurrió."""
    solo(conn, "Lunch")
    ayer = habits.hoy() - timedelta(days=1)
    lunch = habito(conn, "Lunch")["id"]

    conn.execute("INSERT INTO habit_slots (habit_id, day, start_local, minutes) VALUES (?, ?, ?, 90)",
                 (lunch, ayer.isoformat(), habits.local(ayer, time(13, 0)).isoformat()))
    for cuenta in cuentas.values():
        conn.execute(
            "INSERT INTO habit_blocks (habit_id, day, account_id, event_id, "
            "payload_hash, updated_at) VALUES (?, ?, ?, ?, 'h', ?)",
            (lunch, ayer.isoformat(), cuenta, f"remoto-{cuenta}", db.now_iso()))
    conn.commit()

    ops, _ = habits.compute_plan(conn)
    assert not [o for o in ops if o["action"] == "delete" and o["day"] == ayer.isoformat()]


def test_con_la_opcion_encendida_si_se_borran(conn, cuentas):
    """Es la opción que pidió David: quien quiera el calendario limpio la
    enciende, bloque por bloque."""
    solo(conn, "Lunch")
    ayer = habits.hoy() - timedelta(days=1)
    lunch = habito(conn, "Lunch")["id"]

    conn.execute("INSERT INTO habit_slots (habit_id, day, start_local, minutes) VALUES (?, ?, ?, 90)",
                 (lunch, ayer.isoformat(), habits.local(ayer, time(13, 0)).isoformat()))
    conn.execute(
        "INSERT INTO habit_blocks (habit_id, day, account_id, event_id, payload_hash, "
        "updated_at) VALUES (?, ?, ?, 'remoto-1', 'h', ?)",
        (lunch, ayer.isoformat(), cuentas["Ruca"], db.now_iso()))
    conn.execute("UPDATE habits SET cleanup_past = 1 WHERE id = ?", (lunch,))
    conn.commit()

    ops, _ = habits.compute_plan(conn)
    borrados = [o for o in ops if o["action"] == "delete" and o["day"] == ayer.isoformat()]
    assert len(borrados) == 1


def test_un_bloque_pasado_gasta_su_cupo_de_la_semana(conn, cuentas, monkeypatch):
    """Exercise va 3 veces por semana. Si lo ya hecho no contara, pondría 3 más
    encima y terminarías con 4 o 5."""
    solo(conn, "Exercise")
    ejercicio = habito(conn, "Exercise")["id"]
    hoy_real = habits.hoy()
    monkeypatch.setattr(habits, "ahora", lambda: habits.local(hoy_real, time(21, 0)))

    slots, _ = habits.decidir_slots(conn)
    por_semana = {}
    for (hid, dia) in slots:
        if hid != ejercicio:
            continue
        semana = habits.semana_de(date.fromisoformat(dia))
        por_semana[semana] = por_semana.get(semana, 0) + 1

    assert por_semana, "debería colocar ejercicio"
    assert max(por_semana.values()) == 3, f"nunca más de 3 por semana: {por_semana}"


# --- La semana que se dibuja en el dashboard --------------------------------

def test_los_solapes_se_reparten_en_carriles_como_google():
    """El caso real de David: Not available 5–8pm con Meditation y Exercise
    dentro. Google los pone al lado, no uno encima del otro."""
    piezas = [
        {"ini": 17 * 60, "fin": 20 * 60, "t": "Not available"},
        {"ini": 17 * 60, "fin": 18 * 60, "t": "Meditation"},
        {"ini": 18 * 60, "fin": 19 * 60 + 30, "t": "Exercise"},
    ]
    colocadas = {p["t"]: p for p in habits.carriles(piezas)}
    assert colocadas["Not available"]["carril"] == 0
    assert colocadas["Meditation"]["carril"] == 1
    assert colocadas["Exercise"]["carril"] == 1, "cabe donde terminó Meditation"
    assert {p["total"] for p in colocadas.values()} == {2}, "mismo ancho para el grupo"


def test_sin_solape_cada_pieza_ocupa_la_columna_entera():
    piezas = [{"ini": 600, "fin": 660}, {"ini": 700, "fin": 760}]
    assert [p["total"] for p in habits.carriles(piezas)] == [1, 1]


def test_dos_reuniones_pegadas_no_son_un_solape():
    """Una que termina a las 11:00 y otra que empieza a las 11:00 no se pisan."""
    piezas = [{"ini": 600, "fin": 660}, {"ini": 660, "fin": 720}]
    assert [p["total"] for p in habits.carriles(piezas)] == [1, 1]


def test_la_rejilla_cubre_el_dia_entero_y_solo_cambia_donde_abre(conn, cuentas):
    """La primera versión dibujaba solo las horas con contenido, así que no se
    podía bajar a ver el resto del día. Ahora recorta la vista, no la rejilla."""
    solo(conn, "Lunch")
    lunes = proximo_lunes()
    slots, _ = habits.decidir_slots(conn)
    habits.guardar_slots(conn, slots)

    vista = habits.vista_semana(conn, offset=semanas_hasta(lunes))
    assert len(vista["horas"]) == 24, "la rejilla siempre es el día completo"
    assert vista["alto_dia"] == 24 * config.PX_HORA
    assert not vista["vacia"]
    # El almuerzo va a la 1. El scroll abre a las 12 —media hora de aire,
    # redondeada hacia abajo— pero lo que se le DICE al usuario es la hora real
    # del primer evento. Decirle «abre a las 12» cuando lo primero es a la 1 se
    # lee como un error de cálculo.
    assert vista["primera"] == "1pm", "el texto nombra el primer evento"
    assert vista["foco_px"] == 12 * config.PX_HORA, "el scroll deja aire encima"


def test_las_reuniones_mueven_donde_abre_la_vista(conn, cuentas):
    solo(conn, "Lunch")
    lunes = proximo_lunes()
    reunion(conn, cuentas["Ruca"], lunes, "06:00", "07:00", "madrugada")
    slots, _ = habits.decidir_slots(conn)
    habits.guardar_slots(conn, slots)
    offset = semanas_hasta(lunes)

    con = habits.vista_semana(conn, offset=offset, con_reuniones=True)
    sin = habits.vista_semana(conn, offset=offset, con_reuniones=False)
    assert con["primera"] == "6am", "con las reuniones, lo primero es la de las 6"
    assert sin["primera"] == "1pm", "sin ellas, lo primero es el almuerzo"
    assert con["foco_px"] == 5 * config.PX_HORA, "y el scroll abre una hora antes"
    assert len(con["horas"]) == len(sin["horas"]) == 24


def test_una_semana_mas_alla_del_horizonte_se_explica(conn, cuentas):
    vista = habits.vista_semana(conn, offset=20)
    assert vista["vacia"] and vista["mas_alla"]


def test_la_vista_no_se_sale_del_dia(conn, cuentas):
    """Una reunión que cruza la medianoche se recorta a su propia columna."""
    solo(conn, "Lunch")
    lunes = proximo_lunes()
    inicio = habits.local(lunes, habits.hhmm("23:00"))
    fin = inicio + timedelta(hours=3)
    conn.execute(
        "INSERT INTO source_events (account_id, event_id, summary, start_raw, end_raw, "
        "start_utc, end_utc, all_day, transparency, status, my_response, has_guests, "
        "is_mirror, updated) VALUES (?, 'larga', 'Trasnocho', '{}', '{}', ?, ?, 0, 'opaque', "
        "'confirmed', 'owner', 1, 0, '')",
        (cuentas["Ruca"], inicio.astimezone(timezone.utc).isoformat(),
         fin.astimezone(timezone.utc).isoformat()))
    conn.commit()

    vista = habits.vista_semana(conn, offset=semanas_hasta(lunes))
    for columna in vista["columnas"]:
        for pieza in columna["piezas"]:
            assert 0 <= pieza["ini"] < pieza["fin"] <= 24 * 60
            assert pieza["top"] + pieza["alto"] <= vista["alto_dia"] + 2


def test_cambiar_de_zona_horaria_no_se_lee_como_mover_los_bloques(conn, con_personal, monkeypatch):
    """El día que viajes y macOS te cambie la zona, CalSync no debe creer que
    moviste todo a mano.

    El mismo instante se escribe distinto en cada zona: 13:00-05:00 y
    18:00+00:00 son la misma hora. Comparando el texto en vez del instante,
    cada bloque se leía como movido y la agenda entera se reescribía — y
    además quedaba marcada «a mano», es decir exenta de su propia ventana.
    """
    from zoneinfo import ZoneInfo

    solo(conn, "Lunch")
    lunes = proximo_lunes()
    lunch = habito(conn, "Lunch")["id"]

    slots, _ = habits.decidir_slots(conn)
    habits.guardar_slots(conn, slots)
    colocado = slot(slots, lunch, lunes)
    bloque_en_google(conn, lunch, lunes, con_personal["Personal"], "orig-1",
                     datetime.fromisoformat(colocado["start_local"]).strftime("%H:%M"),
                     colocado["minutes"])

    # El computador cambia de zona. Nada se movió: solo se escribe distinto.
    monkeypatch.setattr(config, "TZ", ZoneInfo("Europe/Madrid"))

    slots, _ = habits.decidir_slots(conn)
    fila = conn.execute(
        "SELECT by_hand FROM habit_slots WHERE habit_id = ? AND day = ?",
        (lunch, lunes.isoformat())).fetchone()
    assert fila is None or not fila["by_hand"], (
        "cambiar de zona se leyó como un movimiento a mano")
    movidos = [l for l in db.recent_log(conn, 50) if l["action"] == "bloque_movido_a_mano"]
    assert not movidos, "no debería haber registrado ningún movimiento"


def test_la_hora_que_se_muestra_dice_los_minutos_cuando_los_hay(conn, cuentas):
    """7:30am, no «7am». Media hora de diferencia en un aviso es confusión."""
    assert habits.reloj12_exacto(7 * 60 + 30) == "7:30am"
    assert habits.reloj12_exacto(5 * 60) == "5am"
    assert habits.reloj12_exacto(0) == "12am"
    assert habits.reloj12_exacto(12 * 60 + 5) == "12:05pm"
    assert habits.reloj12_exacto(23 * 60 + 45) == "11:45pm"


# --- Eventos demasiado largos para ser una reunión ---------------------------
#
# El caso que lo originó: "Janina OOO" del 9 de noviembre a las 5:00 al 10 a
# las 6:00. No es un evento de día completo, así que CalSync lo trataba como
# una reunión de verdad y dejaba sin sitio a Lunch, Meditation y Exercise.

REGLA = {"skip_long": True, "long_hours": 12}


def evento_largo(inicio_local, fin_local, **extra):
    """Un evento con horas, en la zona horaria de la app."""
    base = {
        "is_mirror": 0, "status": "confirmed", "all_day": 0,
        "transparency": "opaque", "my_response": "accepted",
        "summary": "Janina OOO",
        "start_utc": inicio_local.astimezone(timezone.utc).isoformat(),
        "end_utc": fin_local.astimezone(timezone.utc).isoformat(),
    }
    base.update(extra)
    return base


def test_el_ooo_de_veinticinco_horas_deja_de_ocupar_tiempo():
    lunes = date(2026, 11, 9)
    evt = evento_largo(habits.local(lunes, time(5, 0)),
                       habits.local(lunes + timedelta(days=1), time(6, 0)))
    assert habits.ocupa_tiempo(evt, {}), "sin la regla, sigue ocupando como siempre"
    assert not habits.ocupa_tiempo(evt, REGLA)
    assert rules.should_mirror(evt, {**REGLA, "skip_tags": []})[0] is False, (
        "y tampoco se refleja: las dos puertas usan la misma función"
    )


def test_un_offsite_de_nueve_a_seis_sigue_ocupando():
    """Doce horas no es un número cualquiera: un día entero de trabajo cabe
    debajo, y ese sí tiene que bloquear tu tiempo."""
    lunes = date(2026, 11, 9)
    evt = evento_largo(habits.local(lunes, time(9, 0)), habits.local(lunes, time(18, 0)))
    assert habits.ocupa_tiempo(evt, REGLA)


def test_terminar_a_medianoche_no_es_cruzar_de_dia():
    """Un evento de 22:00 a 00:00 lleva la fecha del día siguiente en su final
    pero no se mete en él. Sin el margen, toda reunión que acaba a las doce
    saldría marcada como larga."""
    lunes = date(2026, 11, 9)
    evt = evento_largo(habits.local(lunes, time(22, 0)),
                       habits.local(lunes + timedelta(days=1), time(0, 0)))
    assert habits.ocupa_tiempo(evt, REGLA)


def test_un_ooo_largo_del_mismo_dia_tambien_cae():
    """La segunda condición existe para esto: no cruza la medianoche, pero
    tampoco es una reunión."""
    lunes = date(2026, 11, 9)
    evt = evento_largo(habits.local(lunes, time(7, 0)), habits.local(lunes, time(22, 0)))
    assert not habits.ocupa_tiempo(evt, REGLA)


def test_un_dia_completo_lo_sigue_decidiendo_su_propia_regla():
    """Un evento de día completo dura 24 horas y cruza la medianoche, así que
    la regla nueva lo atraparía. No debe: si apagaste «All-day events» es
    porque quieres que te bloqueen, y esta regla no puede pisar esa decisión."""
    lunes = date(2026, 11, 9)
    evt = evento_largo(habits.local(lunes, time(0, 0)),
                       habits.local(lunes + timedelta(days=1), time(0, 0)), all_day=1)
    assert not rules.demasiado_largo(evt, REGLA)


def test_el_ooo_largo_libera_el_almuerzo(conn, cuentas):
    """La prueba de verdad: el conflicto que veías en el dashboard desaparece
    y el bloque encuentra sitio."""
    lunes = date(2026, 11, 9)
    conn.execute(
        "INSERT INTO source_events (account_id, event_id, summary, start_raw, end_raw, "
        "start_utc, end_utc, all_day, transparency, status, my_response, has_guests, "
        "is_mirror, updated) VALUES (?, 'janina', 'Janina OOO', '{}', '{}', ?, ?, "
        "0, 'opaque', 'confirmed', 'accepted', 1, 0, '')",
        (cuentas["Ruca"],
         habits.local(lunes, time(5, 0)).astimezone(timezone.utc).isoformat(),
         habits.local(lunes + timedelta(days=1), time(6, 0)).astimezone(timezone.utc).isoformat()),
    )
    conn.commit()

    apagada = dict(db.all_settings(conn)["rules"]); apagada["skip_long"] = False
    db.set_setting(conn, "rules", apagada)
    assert habits.ocupado_en(conn, lunes), "con la regla apagada, el día está tomado"

    encendida = dict(apagada); encendida["skip_long"] = True
    db.set_setting(conn, "rules", encendida)
    assert habits.ocupado_en(conn, lunes) == [], (
        "con la regla encendida, el día vuelve a estar libre"
    )


def test_la_franja_dice_el_dia_cuando_el_evento_lo_cambia():
    """Veinticinco horas no pueden leerse como una. Era lo que escondía al
    culpable en «Needs your attention»."""
    lunes = date(2026, 11, 9)
    corto = habits.franja(habits.local(lunes, time(13, 0)), habits.local(lunes, time(14, 30)))
    assert corto == "13:00–14:30"
    largo = habits.franja(habits.local(lunes, time(5, 0)),
                          habits.local(lunes + timedelta(days=1), time(6, 0)))
    assert "Nov 10" in largo and largo != "05:00–06:00"


def test_una_opcion_nueva_no_nace_apagada(conn):
    """Los ajustes guardados son un diccionario entero. Sin fusionar con los
    valores por defecto, cualquier opción añadida después llegaba vacía a quien
    ya tenía ajustes, y parecía no hacer nada."""
    db.set_setting(conn, "rules", {"skip_all_day": False})
    reglas = db.all_settings(conn)["rules"]
    assert reglas["skip_all_day"] is False, "lo que guardaste manda"
    assert reglas["skip_long"] is True and reglas["long_hours"] == 12, (
        "y lo que no guardaste llega con su valor por defecto"
    )
