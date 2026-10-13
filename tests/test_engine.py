"""
Pruebas del motor. No tocan internet ni Google: trabajan sobre una base de
datos temporal con eventos inventados.

Lo que se verifica aquí es exactamente lo que da miedo del proyecto:
que no se formen bucles, que un evento ajeno sea intocable, y que mover
un evento produzca un movimiento y no un duplicado.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from calsync import config, db, engine, google_cal, rules


# --- Andamiaje ---------------------------------------------------------------

@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    c = db.connect()
    yield c
    c.close()


@pytest.fixture
def tres_cuentas(conn):
    ids = {}
    for label, email in [("Ruca", "d@ruca.co"), ("Galactic", "d@galactic.co"),
                         ("Personal", "d@gmail.com")]:
        ids[label] = db.upsert_account(conn, email, label, json.dumps({"token": "falso"}))
    db.ensure_pairs(conn)
    return ids


def en(dias: int, hora: int = 10):
    """Una hora concreta dentro de la ventana de sincronización."""
    base = datetime.now(timezone.utc) + timedelta(days=dias)
    return base.replace(hour=hora, minute=0, second=0, microsecond=0)


def evento(event_id, inicio=None, fin=None, **extra):
    inicio = inicio or en(2, 10)
    fin = fin or en(2, 11)
    body = {
        "id": event_id,
        "summary": extra.get("summary", "Reunión de equipo"),
        "status": extra.get("status", "confirmed"),
        "updated": datetime.now(timezone.utc).isoformat(),
        "start": {"dateTime": inicio.isoformat(), "timeZone": "America/Bogota"},
        "end": {"dateTime": fin.isoformat(), "timeZone": "America/Bogota"},
    }
    if extra.get("all_day"):
        body["start"] = {"date": inicio.date().isoformat()}
        body["end"] = {"date": fin.date().isoformat()}
    if extra.get("transparency"):
        body["transparency"] = extra["transparency"]
    if extra.get("attendees"):
        body["attendees"] = extra["attendees"]
    if extra.get("mirror"):
        body["extendedProperties"] = {"private": {
            config.MARKER_KEY: config.MARKER_VALUE,
            "src_acct": "1", "src_evt": "original-123", "hash": "abc",
        }}
    return body


# --- Lo esencial -------------------------------------------------------------

def test_un_evento_genera_un_espejo_en_cada_otra_cuenta(conn, tres_cuentas):
    engine.store_events(conn, tres_cuentas["Ruca"], [evento("junta-1")])
    ops, _ = engine.compute_plan(conn)

    assert len(ops) == 2
    assert {op["action"] for op in ops} == {"create"}
    assert {op["to"] for op in ops} == {"Galactic", "Personal"}


def test_un_espejo_nunca_genera_otro_espejo(conn, tres_cuentas):
    """La prueba más importante del proyecto: sin esto habría un bucle
    infinito de espejos de espejos entre las tres cuentas."""
    engine.store_events(conn, tres_cuentas["Ruca"], [evento("junta-1")])
    # Simulamos que ya existe el espejo en Galactic y que lo leímos de vuelta.
    engine.store_events(conn, tres_cuentas["Galactic"], [evento("espejo-1", mirror=True)])

    ops, _ = engine.compute_plan(conn)

    # Siguen siendo solo los 2 espejos del evento original de Ruca.
    assert len(ops) == 2
    assert all(op["source_account_id"] == tres_cuentas["Ruca"] for op in ops)


def test_mover_el_evento_produce_un_movimiento_no_un_duplicado(conn, tres_cuentas):
    ruca, galactic = tres_cuentas["Ruca"], tres_cuentas["Galactic"]
    engine.store_events(conn, ruca, [evento("junta-1")])
    ops, _ = engine.compute_plan(conn)
    creado = next(o for o in ops if o["target_account_id"] == galactic)

    # Anotamos el espejo como si ya se hubiera creado en Google.
    conn.execute(
        "INSERT INTO mirrors VALUES (?, ?, ?, ?, ?, ?)",
        (ruca, "junta-1", galactic, "evento-remoto-xyz", creado["hash"], db.now_iso()),
    )
    conn.commit()

    # Nada cambió todavía: no debería haber operación para Galactic.
    ops, _ = engine.compute_plan(conn)
    assert not [o for o in ops if o["target_account_id"] == galactic]

    # Ahora el evento se mueve dos horas.
    engine.store_events(conn, ruca, [evento("junta-1", en(2, 12), en(2, 13))])
    ops, _ = engine.compute_plan(conn)
    para_galactic = [o for o in ops if o["target_account_id"] == galactic]

    assert len(para_galactic) == 1
    assert para_galactic[0]["action"] == "update"
    assert para_galactic[0]["target_event_id"] == "evento-remoto-xyz"


def test_borrar_el_evento_borra_sus_espejos(conn, tres_cuentas):
    ruca, galactic = tres_cuentas["Ruca"], tres_cuentas["Galactic"]
    engine.store_events(conn, ruca, [evento("junta-1")])
    conn.execute(
        "INSERT INTO mirrors VALUES (?, ?, ?, ?, ?, ?)",
        (ruca, "junta-1", galactic, "evento-remoto-xyz", "hash-viejo", db.now_iso()),
    )
    conn.commit()

    engine.store_events(conn, ruca, [evento("junta-1", status="cancelled")])
    ops, _ = engine.compute_plan(conn)

    borrados = [o for o in ops if o["action"] == "delete"]
    assert len(borrados) == 1
    assert borrados[0]["target_event_id"] == "evento-remoto-xyz"


def test_direccion_apagada_no_genera_espejo(conn, tres_cuentas):
    ruca, personal = tres_cuentas["Ruca"], tres_cuentas["Personal"]
    pares = db.get_setting(conn, "pairs")
    pares[f"{ruca}|{personal}"] = False
    db.set_setting(conn, "pairs", pares)

    engine.store_events(conn, ruca, [evento("junta-1")])
    ops, _ = engine.compute_plan(conn)

    assert len(ops) == 1
    assert ops[0]["to"] == "Galactic"


# --- Reglas de exclusión -----------------------------------------------------

def test_los_eventos_borrados_no_ensucian_la_lista_de_ignorados(conn, tres_cuentas):
    """Google manda los eventos borrados como «cancelled» para avisarnos.
    Son lápidas, no algo que David pueda revisar: no se reportan."""
    engine.store_events(conn, tres_cuentas["Ruca"], [
        evento("borrado-1", summary="CANCELLED", status="cancelled"),
        evento("dia-libre", summary="Caro OOO", all_day=True),
    ])
    ops, skipped = engine.compute_plan(conn)

    motivos = [s[2] for s in skipped]
    assert ops == []
    assert "cancelled event" not in motivos
    assert "all-day event" in motivos, "esos sí son informativos"


def test_las_lapidas_se_limpian_cuando_ya_no_tienen_espejos(conn, tres_cuentas):
    ruca, galactic = tres_cuentas["Ruca"], tres_cuentas["Galactic"]
    engine.store_events(conn, ruca, [evento("borrado-1", status="cancelled")])

    # Mientras quede un espejo por borrar, la lápida se queda.
    conn.execute("INSERT INTO mirrors VALUES (?, ?, ?, ?, ?, ?)",
                 (ruca, "borrado-1", galactic, "remoto-1", "h", db.now_iso()))
    conn.commit()
    engine.purge_cancelled(conn)
    assert conn.execute("SELECT COUNT(*) c FROM source_events").fetchone()["c"] == 1

    # Una vez borrado el espejo, la lápida ya no sirve.
    conn.execute("DELETE FROM mirrors")
    conn.commit()
    engine.purge_cancelled(conn)
    assert conn.execute("SELECT COUNT(*) c FROM source_events").fetchone()["c"] == 0


@pytest.mark.parametrize("descripcion,extra", [
    ("tag nosync", {"summary": "Bloqueo tentativo [nosync]"}),
    ("día completo", {"all_day": True}),
    ("marcado disponible", {"transparency": "transparent"}),
    ("invitación rechazada", {"attendees": [
        {"self": True, "responseStatus": "declined"},
        {"email": "otro@x.com", "responseStatus": "accepted"}]}),
])
def test_eventos_excluidos_no_generan_espejos(conn, tres_cuentas, descripcion, extra):
    engine.store_events(conn, tres_cuentas["Ruca"], [evento("junta-1", **extra)])
    ops, skipped = engine.compute_plan(conn)
    assert ops == [], f"{descripcion} no debería generar espejos"
    assert skipped, f"{descripcion} debería quedar registrado como ignorado"


def test_palabra_clave_configurable(conn, tres_cuentas):
    reglas = db.get_setting(conn, "rules")
    reglas["skip_keywords"] = ["dentista"]
    db.set_setting(conn, "rules", reglas)

    engine.store_events(conn, tres_cuentas["Personal"], [evento("cita", summary="Dentista")])
    ops, skipped = engine.compute_plan(conn)

    assert ops == []
    assert "dentista" in skipped[0][2].lower()


def test_evento_fuera_de_la_ventana_se_ignora(conn, tres_cuentas):
    lejos = en(400, 10)
    engine.store_events(conn, tres_cuentas["Ruca"],
                        [evento("lejano", lejos, lejos + timedelta(hours=1))])
    ops, _ = engine.compute_plan(conn)
    assert ops == []


# --- El contenido del espejo -------------------------------------------------

def test_el_espejo_dice_busy_por_defecto(conn, tres_cuentas):
    engine.store_events(conn, tres_cuentas["Ruca"],
                        [evento("junta-1", summary="Negociación confidencial")])
    ops, _ = engine.compute_plan(conn)

    resumen = ops[0]["body"]["summary"]
    assert "Busy (Ruca)" in resumen
    assert "confidencial" not in resumen, "el título real no debe filtrarse"


def test_la_palabra_de_ocupado_es_configurable(conn, tres_cuentas):
    db.set_setting(conn, "busy_word", "Ocupado")
    engine.store_events(conn, tres_cuentas["Ruca"], [evento("junta-1")])
    ops, _ = engine.compute_plan(conn)
    assert "Ocupado (Ruca)" in ops[0]["body"]["summary"]


def poner_detalle(conn, origen, destino, mode, label="", share=False):
    detalle = db.get_setting(conn, "detail_pairs")
    detalle[f"{origen}|{destino}"] = {"mode": mode, "label": label, "share": share}
    db.set_setting(conn, "detail_pairs", detalle)


def test_el_detalle_depende_del_par_no_solo_del_origen(conn, tres_cuentas):
    """El caso real de David: los eventos de Galactic se ven completos en
    Ruca, pero como «Work» en el calendario personal."""
    galactic = tres_cuentas["Galactic"]
    poner_detalle(conn, galactic, tres_cuentas["Ruca"], "title")
    poner_detalle(conn, galactic, tres_cuentas["Personal"], "custom", "Work")

    engine.store_events(conn, galactic, [evento("junta-1", summary="Roadmap Q4")])
    ops, _ = engine.compute_plan(conn)
    por_destino = {op["to"]: op["body"]["summary"] for op in ops}

    assert "Roadmap Q4" in por_destino["Ruca"]
    assert "Work" in por_destino["Personal"]
    assert "Roadmap" not in por_destino["Personal"], "el personal no debe ver el título"


def test_detalle_completo_copia_lugar_y_link_de_videollamada(conn, tres_cuentas):
    ruca = tres_cuentas["Ruca"]
    poner_detalle(conn, ruca, tres_cuentas["Personal"], "title")

    crudo = evento("junta-1", summary="Comité")
    crudo["location"] = "Sala 3, WeWork"
    crudo["hangoutLink"] = "https://meet.google.com/abc-defg-hij"
    crudo["description"] = "Agenda: revisar métricas"
    engine.store_events(conn, ruca, [crudo])

    op = next(o for o in engine.compute_plan(conn)[0] if o["to"] == "Personal")
    assert op["body"]["location"] == "Sala 3, WeWork"
    assert "meet.google.com/abc-defg-hij" in op["body"]["description"]
    assert "revisar métricas" in op["body"]["description"]


def test_modo_busy_no_filtra_lugar_ni_link(conn, tres_cuentas):
    crudo = evento("junta-1", summary="Due diligence")
    crudo["location"] = "Oficina del inversionista"
    crudo["hangoutLink"] = "https://meet.google.com/secreto"
    crudo["description"] = "Cifras confidenciales"
    engine.store_events(conn, tres_cuentas["Ruca"], [crudo])

    for op in engine.compute_plan(conn)[0]:
        assert "location" not in op["body"]
        assert "secreto" not in op["body"]["description"]
        assert "confidencial" not in op["body"]["description"].lower()


def test_extrae_el_link_de_zoom_y_no_solo_de_meet():
    zoom = {"id": "x", "conferenceData": {"entryPoints": [
        {"entryPointType": "phone", "uri": "tel:+123"},
        {"entryPointType": "video", "uri": "https://zoom.us/j/999"},
    ]}}
    assert rules.meet_link(zoom) == "https://zoom.us/j/999"
    assert rules.meet_link({"id": "x"}) == ""


def test_cambiar_el_detalle_mueve_el_espejo_existente(conn, tres_cuentas):
    """Si cambias el modo de un par, el espejo ya creado se actualiza en vez
    de quedarse con el texto viejo."""
    ruca, personal = tres_cuentas["Ruca"], tres_cuentas["Personal"]
    engine.store_events(conn, ruca, [evento("junta-1", summary="Comité")])
    antes = next(o for o in engine.compute_plan(conn)[0] if o["to"] == "Personal")
    conn.execute("INSERT INTO mirrors VALUES (?, ?, ?, ?, ?, ?)",
                 (ruca, "junta-1", personal, "remoto-1", antes["hash"], db.now_iso()))
    conn.commit()

    poner_detalle(conn, ruca, personal, "custom", "Work")
    despues = [o for o in engine.compute_plan(conn)[0] if o["to"] == "Personal"]

    assert len(despues) == 1
    assert despues[0]["action"] == "update"
    assert "Work" in despues[0]["body"]["summary"]


def test_todo_espejo_lleva_la_marca_y_no_notifica(conn, tres_cuentas):
    engine.store_events(conn, tres_cuentas["Ruca"], [evento("junta-1")])
    ops, _ = engine.compute_plan(conn)

    for op in ops:
        privados = op["body"]["extendedProperties"]["private"]
        assert privados[config.MARKER_KEY] == config.MARKER_VALUE
        assert privados["src_evt"] == "junta-1"
        assert op["body"]["transparency"] == "opaque"     # cuenta como ocupado
        assert op["body"]["visibility"] == "private"      # nadie ve el detalle
        assert op["body"]["reminders"]["overrides"] == [] # cero notificaciones


# --- Visibilidad frente al equipo --------------------------------------------

def test_por_defecto_el_equipo_solo_ve_busy(conn, tres_cuentas):
    poner_detalle(conn, tres_cuentas["Galactic"], tres_cuentas["Ruca"], "title")
    engine.store_events(conn, tres_cuentas["Galactic"], [evento("junta-1")])
    op = next(o for o in engine.compute_plan(conn)[0] if o["to"] == "Ruca")
    assert op["body"]["visibility"] == "private"


def test_se_puede_compartir_con_el_equipo(conn, tres_cuentas):
    """Galactic es un cliente de Ruca: sus llamadas son trabajo de Ruca y el
    equipo puede verlas como cualquier otra reunión."""
    poner_detalle(conn, tres_cuentas["Galactic"], tres_cuentas["Ruca"], "title", share=True)
    engine.store_events(conn, tres_cuentas["Galactic"], [evento("junta-1", summary="Roadmap")])

    ops = engine.compute_plan(conn)[0]
    a_ruca = next(o for o in ops if o["to"] == "Ruca")
    a_personal = next(o for o in ops if o["to"] == "Personal")

    assert a_ruca["body"]["visibility"] == "default", "el equipo de Ruca sí lo ve"
    assert a_personal["body"]["visibility"] == "private", "el personal no cambia"


def test_cambiar_la_visibilidad_actualiza_el_espejo_existente(conn, tres_cuentas):
    galactic, ruca = tres_cuentas["Galactic"], tres_cuentas["Ruca"]
    poner_detalle(conn, galactic, ruca, "title")
    engine.store_events(conn, galactic, [evento("junta-1")])
    antes = next(o for o in engine.compute_plan(conn)[0] if o["to"] == "Ruca")
    conn.execute("INSERT INTO mirrors VALUES (?, ?, ?, ?, ?, ?)",
                 (galactic, "junta-1", ruca, "remoto-1", antes["hash"], db.now_iso()))
    conn.commit()

    poner_detalle(conn, galactic, ruca, "title", share=True)
    despues = [o for o in engine.compute_plan(conn)[0] if o["to"] == "Ruca"]

    assert len(despues) == 1 and despues[0]["action"] == "update"
    assert despues[0]["body"]["visibility"] == "default"


# --- Invitados ---------------------------------------------------------------

CON_INVITADOS = [
    {"self": True, "responseStatus": "accepted", "email": "david@galactic.co"},
    {"email": "ana@cliente.com", "displayName": "Ana Restrepo", "responseStatus": "accepted"},
    {"email": "luis@galactic.co", "responseStatus": "tentative"},
    {"email": "sala-3@resource.calendar.google.com", "resource": True},
]


def test_los_invitados_nunca_se_copian_como_invitados_reales(conn, tres_cuentas):
    """La prueba que evita un incidente vergonzoso: si copiáramos `attendees`,
    Google le mandaría una invitación nueva a todo el equipo del cliente desde
    la otra cuenta de David, y les duplicaría el evento."""
    ruca = tres_cuentas["Ruca"]
    poner_detalle(conn, ruca, tres_cuentas["Personal"], "title", share=True)
    engine.store_events(conn, ruca, [evento("junta-1", attendees=CON_INVITADOS)])

    for op in engine.compute_plan(conn)[0]:
        assert "attendees" not in op["body"]
        assert "guests" not in op["body"]


def test_con_detalle_completo_los_invitados_van_como_texto(conn, tres_cuentas):
    galactic = tres_cuentas["Galactic"]
    poner_detalle(conn, galactic, tres_cuentas["Ruca"], "title", share=True)
    crudo = evento("junta-1", attendees=CON_INVITADOS)
    crudo["organizer"] = {"email": "pm@galactic.co"}
    engine.store_events(conn, galactic, [crudo])

    texto = next(o for o in engine.compute_plan(conn)[0] if o["to"] == "Ruca")["body"]["description"]
    assert "Ana Restrepo" in texto
    assert "luis@galactic.co" in texto
    assert "pm@galactic.co" in texto
    assert "david@galactic.co" not in texto, "no tiene sentido listarte a ti mismo"
    assert "resource.calendar.google.com" not in texto, "las salas no son invitados"


def test_en_modo_busy_no_se_filtran_los_invitados(conn, tres_cuentas):
    engine.store_events(conn, tres_cuentas["Ruca"],
                        [evento("junta-1", attendees=CON_INVITADOS)])
    for op in engine.compute_plan(conn)[0]:
        assert "Ana Restrepo" not in op["body"]["description"]


def test_el_espejo_conserva_la_zona_horaria_del_original(conn, tres_cuentas):
    engine.store_events(conn, tres_cuentas["Ruca"], [evento("junta-1")])
    ops, _ = engine.compute_plan(conn)
    assert ops[0]["body"]["start"]["timeZone"] == "America/Bogota"


# --- La barrera de seguridad -------------------------------------------------

def test_reconoce_sus_propios_eventos():
    assert google_cal.is_mirror(evento("x", mirror=True))
    assert not google_cal.is_mirror(evento("x"))
    assert not google_cal.is_mirror({"id": "x", "extendedProperties": {"private": {}}})
    assert not google_cal.is_mirror({"id": "x"})


def test_se_niega_a_borrar_un_evento_ajeno(conn, tres_cuentas, monkeypatch):
    """Aunque la base de datos apunte por error a un evento real, el motor
    verifica la marca contra Google antes de borrar y se detiene."""
    ajeno = evento("evento-de-un-humano")  # sin marca

    class ServicioFalso:
        def events(self):
            return self
        def get(self, **_):
            return self
        def execute(self):
            return ajeno
        def delete(self, **_):
            raise AssertionError("¡nunca debería llegar aquí!")

    monkeypatch.setattr(google_cal, "service_for", lambda *a, **k: ServicioFalso())
    acct = db.account(conn, tres_cuentas["Ruca"])

    with pytest.raises(PermissionError):
        google_cal.delete_event(conn, acct, "evento-de-un-humano")

    with pytest.raises(PermissionError):
        google_cal.update_event(conn, acct, "evento-de-un-humano", {"summary": "x"})


def test_modo_simulacion_no_escribe_nada(conn, tres_cuentas, monkeypatch):
    def explotar(*a, **k):
        raise AssertionError("en simulación no se puede llamar a Google")

    monkeypatch.setattr(google_cal, "create_event", explotar)
    monkeypatch.setattr(google_cal, "update_event", explotar)
    monkeypatch.setattr(google_cal, "delete_event", explotar)

    engine.store_events(conn, tres_cuentas["Ruca"], [evento("junta-1")])
    ops, _ = engine.compute_plan(conn)
    hecho = engine.apply_plan(conn, ops, simulation=True)

    assert hecho["create"] == 2
    assert conn.execute("SELECT COUNT(*) c FROM mirrors").fetchone()["c"] == 0
    registro = [r["action"] for r in db.recent_log(conn)]
    assert "simulacion" in registro


# --- Fechas ------------------------------------------------------------------

def test_distingue_ir_muy_rapido_de_no_tener_permiso():
    """Los dos llegan como 403 y hay que tratarlos al revés: el de ritmo se
    reintenta, el de permiso se propaga para que David lo vea."""
    class Resp:
        def __init__(self, status):
            self.status = status
            self.reason = "simulado"  # HttpError lo usa al construir su mensaje

    from googleapiclient.errors import HttpError

    ritmo = HttpError(Resp(403), b'{"error":{"errors":[{"reason":"rateLimitExceeded"}]}}')
    bloqueo = HttpError(Resp(403), b'{"error":{"errors":[{"reason":"forbidden"}]}}')

    assert google_cal._es_de_ritmo(ritmo)
    assert not google_cal._es_de_ritmo(bloqueo), "un bloqueo del admin no se reintenta"
    assert google_cal._es_de_ritmo(HttpError(Resp(429), b""))
    assert google_cal._es_de_ritmo(HttpError(Resp(503), b""))
    assert not google_cal._es_de_ritmo(HttpError(Resp(404), b""))


def test_reintenta_cuando_google_pide_calma(monkeypatch):
    monkeypatch.setattr(google_cal.time, "sleep", lambda _: None)  # sin esperas reales

    class Resp:
        status = 429
        reason = "Too Many Requests"

    from googleapiclient.errors import HttpError

    class Peticion:
        def __init__(self): self.intentos = 0
        def execute(self):
            self.intentos += 1
            if self.intentos < 3:
                raise HttpError(Resp(), b"slow down")
            return {"id": "creado"}

    p = Peticion()
    assert google_cal.ejecutar(p) == {"id": "creado"}
    assert p.intentos == 3


def test_convierte_bien_las_horas_a_utc():
    bogota = rules.to_utc({"dateTime": "2026-08-03T09:00:00-05:00"})
    assert bogota.hour == 14 and bogota.tzinfo == timezone.utc

    utc = rules.to_utc({"dateTime": "2026-08-03T14:00:00Z"})
    assert utc.hour == 14

    madrid = rules.to_utc({"dateTime": "2026-08-03T16:00:00+02:00"})
    assert madrid.hour == 14

    # Los tres son el mismo instante visto desde tres husos distintos.
    assert bogota == utc == madrid


def test_dia_completo_arranca_a_medianoche_de_bogota():
    inicio = rules.to_utc({"date": "2026-08-03"})
    assert inicio.isoformat() == "2026-08-03T05:00:00+00:00"


# --- Cambiar la ventana ------------------------------------------------------

def test_cambiar_la_ventana_fuerza_una_lectura_completa(conn, tres_cuentas, monkeypatch):
    """Con lectura incremental, Google solo cuenta lo que cambió: los días
    recién incluidos en la ventana no aparecerían nunca. Editar el número en
    config.py parecería no hacer nada."""
    llamadas = []

    def fetch_falso(conn_, acct, force_full=False):
        llamadas.append(force_full)
        return [], force_full

    monkeypatch.setattr(google_cal, "fetch_changes", fetch_falso)
    monkeypatch.setattr(google_cal, "list_mirrors", lambda c, a: [])
    monkeypatch.setattr(engine.habits, "compute_plan", lambda c: ([], []))

    engine.run_cycle(conn)                      # primera vez: completa
    assert all(llamadas), "el primer ciclo siempre lee completo"

    llamadas.clear()
    engine.run_cycle(conn)                      # sin cambios: incremental
    assert not any(llamadas)

    llamadas.clear()
    monkeypatch.setattr(config, "WINDOW_FUTURE_DAYS", 60)
    engine.run_cycle(conn)                      # ventana nueva: completa otra vez
    assert all(llamadas), "cambiar la ventana debe forzar lectura completa"

    motivos = [r["action"] for r in db.recent_log(conn)]
    assert "ventana_cambiada" in motivos, "y quedar anotado en el registro"


def test_un_permiso_vencido_en_una_cuenta_no_tumba_el_ciclo(conn, tres_cuentas, monkeypatch):
    """Si el token de Ruca se venció, Galactic y Personal tienen que seguir
    sincronizándose. Antes una excepción en la reconciliación mataba el ciclo."""
    monkeypatch.setattr(google_cal, "fetch_changes", lambda c, a, force_full=False: ([], True))
    monkeypatch.setattr(engine.habits, "compute_plan", lambda c: ([], []))

    def explota(conn_, acct, all_time=False):
        if acct["label"] == "Ruca":
            raise ValueError("token corrupto")
        return []

    monkeypatch.setattr(google_cal, "list_mirrors", explota)

    resumen = engine.run_cycle(conn)          # no debe lanzar

    assert resumen["modo"] == "dry run"
    errores = [r for r in db.recent_log(conn) if r["action"] == "error_reconciliacion"]
    assert len(errores) == 1 and "Ruca" in errores[0]["detail"]


# --- La limpieza y los huérfanos --------------------------------------------
# Esta sección existe por un incidente real: el botón de pánico borraba la
# contabilidad aunque el borrado en Google hubiera fallado, y el ciclo siguiente
# creaba un juego nuevo encima de lo que sobrevivió. Repetirlo multiplicaba
# los duplicados.

class GoogleFalso:
    """Un Google de mentiras: guarda eventos en un diccionario por cuenta."""

    def __init__(self, fallar_ids=()):
        self.eventos = {}          # {account_id: {event_id: body}}
        self.fallar_ids = set(fallar_ids)
        self.creados = 0

    def instalar(self, monkeypatch, engine_mod, google_mod):
        def listar(conn, acct, all_time=False):
            return [
                {"id": eid, "summary": body.get("summary", ""),
                 "extendedProperties": body.get("extendedProperties", {})}
                for eid, body in self.eventos.get(acct["id"], {}).items()
            ]

        def crear(conn, acct, body):
            self.creados += 1
            eid = f"remoto-{self.creados}"
            self.eventos.setdefault(acct["id"], {})[eid] = body
            return {"id": eid}

        def borrar(conn, acct, event_id):
            if event_id in self.fallar_ids:
                raise RuntimeError("Google dijo no")
            self.eventos.get(acct["id"], {}).pop(event_id, None)

        monkeypatch.setattr(google_mod, "list_mirrors", listar)
        monkeypatch.setattr(google_mod, "create_event", crear)
        monkeypatch.setattr(google_mod, "delete_event", borrar)

    def total(self):
        return sum(len(v) for v in self.eventos.values())


def test_la_limpieza_borra_todo_y_suelta_la_contabilidad(conn, tres_cuentas, monkeypatch):
    falso = GoogleFalso()
    falso.instalar(monkeypatch, engine, google_cal)

    engine.store_events(conn, tres_cuentas["Ruca"], [evento("junta-1")])
    ops, _ = engine.compute_plan(conn)
    engine.apply_plan(conn, ops, simulation=False)
    assert falso.total() == 2 and conn.execute("SELECT COUNT(*) c FROM mirrors").fetchone()["c"] == 2

    resultado = engine.cleanup_all_mirrors(conn)

    assert resultado["quedan"] == 0 and falso.total() == 0
    assert conn.execute("SELECT COUNT(*) c FROM mirrors").fetchone()["c"] == 0


def test_si_la_limpieza_falla_NO_borra_la_contabilidad(conn, tres_cuentas, monkeypatch):
    """El bug que causó los duplicados. Si un evento sobrevive al borrado, la
    contabilidad tiene que conservarse: es lo único que impide que el siguiente
    ciclo cree otro encima."""
    falso = GoogleFalso(fallar_ids={"remoto-1"})
    falso.instalar(monkeypatch, engine, google_cal)

    engine.store_events(conn, tres_cuentas["Ruca"], [evento("junta-1")])
    ops, _ = engine.compute_plan(conn)
    engine.apply_plan(conn, ops, simulation=False)

    resultado = engine.cleanup_all_mirrors(conn)

    assert resultado["quedan"] == 1, "quedó uno vivo en Google"
    assert conn.execute("SELECT COUNT(*) c FROM mirrors").fetchone()["c"] > 0, (
        "la contabilidad debe sobrevivir para no duplicar"
    )
    motivos = [r["action"] for r in db.recent_log(conn)]
    assert "limpieza_incompleta" in motivos


def test_no_se_crean_duplicados_si_la_contabilidad_se_perdio(conn, tres_cuentas, monkeypatch):
    """El escenario exacto de David: los eventos siguen en Google pero la
    contabilidad está vacía. El ciclo debe reconciliar antes de crear."""
    falso = GoogleFalso()
    falso.instalar(monkeypatch, engine, google_cal)
    monkeypatch.setattr(google_cal, "fetch_changes", lambda c, a, force_full=False: ([], True))
    monkeypatch.setattr(engine.habits, "compute_plan", lambda c: ([], []))
    db.set_setting(conn, "simulation", False)

    engine.store_events(conn, tres_cuentas["Ruca"], [evento("junta-1")])
    engine.apply_plan(conn, engine.compute_plan(conn)[0], simulation=False)
    assert falso.total() == 2

    # Se pierde la contabilidad, como pasaba tras un pánico a medias.
    conn.execute("DELETE FROM mirrors")
    conn.commit()

    engine.run_cycle(conn)

    assert falso.total() == 2, f"deberían seguir siendo 2, no {falso.total()}"


def test_los_bloques_de_habitos_no_son_huerfanos(conn, tres_cuentas, monkeypatch):
    """Olvidarlos en la reconciliación los borraba en cada lectura completa y
    los volvía a crear enseguida: un churn invisible pero constante."""
    falso = GoogleFalso()
    falso.instalar(monkeypatch, engine, google_cal)

    falso.eventos[tres_cuentas["Ruca"]] = {"bloque-1": {"summary": "🍽 Lunch"}}
    conn.execute(
        "INSERT INTO habit_blocks (habit_id, day, account_id, event_id, payload_hash, "
        "updated_at) VALUES (1, '2026-08-10', ?, 'bloque-1', 'h', ?)",
        (tres_cuentas["Ruca"], db.now_iso()),
    )
    conn.commit()

    engine.reconcile_orphans(conn, simulation=False)

    assert falso.total() == 1, "el bloque de hábito no es un huérfano"


# --- Las tres redes contra los bucles ---------------------------------------
# Cada una es independiente de las otras. Para que se formara un bucle tendrían
# que fallar las tres a la vez.

def test_red_1_la_marca_privada(conn, tres_cuentas):
    engine.store_events(conn, tres_cuentas["Ruca"], [evento("espejo", mirror=True)])
    assert engine.compute_plan(conn)[0] == []


def test_red_2_la_marca_invisible_del_titulo(conn, tres_cuentas):
    """Cubre los bloques sin emoji: «Lunch» a secas también queda protegido,
    aunque Google no nos devolviera las propiedades privadas."""
    sin_marca = evento("bloque", summary="Lunch" + config.TITLE_MARK)
    assert google_cal.is_mirror(sin_marca), "el título invisible debe bastar"

    engine.store_events(conn, tres_cuentas["Ruca"], [sin_marca])
    assert engine.compute_plan(conn)[0] == []


def test_red_3_los_ids_de_lo_que_ya_creamos(conn, tres_cuentas):
    """Ni marca privada, ni marca en el título: solo el id. Aun así no se
    refleja, porque la contabilidad lo reconoce como propio."""
    ruca, galactic = tres_cuentas["Ruca"], tres_cuentas["Galactic"]
    conn.execute("INSERT INTO mirrors VALUES (?, ?, ?, 'creado-por-calsync', 'h', ?)",
                 (ruca, "otro-evento", galactic, db.now_iso()))
    conn.commit()

    engine.store_events(conn, galactic, [evento("creado-por-calsync", summary="Sin marcas")])
    ops, _ = engine.compute_plan(conn)

    assert not [o for o in ops if o["source_event_id"] == "creado-por-calsync"]


def test_un_evento_tuyo_llamado_con_candado_sigue_siendo_intocable(conn, tres_cuentas,
                                                                  monkeypatch):
    """La red del título evita reflejarlo, pero NO autoriza a borrarlo. Escribir
    y borrar exige la marca privada estricta."""
    humano = {"id": "mio", "summary": "🔒 Contraseñas del banco"}
    assert google_cal.is_mirror(humano), "no se refleja"
    assert not google_cal.tiene_marca(humano), "pero no es nuestro"

    class ServicioFalso:
        def events(self): return self
        def get(self, **_): return self
        def execute(self): return humano
        def delete(self, **_): raise AssertionError("¡jamás!")

    monkeypatch.setattr(google_cal, "service_for", lambda *a, **k: ServicioFalso())
    with pytest.raises(PermissionError):
        google_cal.delete_event(conn, db.account(conn, tres_cuentas["Ruca"]), "mio")


def test_el_color_de_los_espejos_es_configurable(conn, tres_cuentas):
    engine.store_events(conn, tres_cuentas["Ruca"], [evento("junta-1")])
    assert engine.compute_plan(conn)[0][0]["body"]["colorId"] == "8", "gris por defecto"

    db.set_setting(conn, "mirror_color", "")
    assert "colorId" not in engine.compute_plan(conn)[0][0]["body"]

    db.set_setting(conn, "mirror_color", "3")
    assert engine.compute_plan(conn)[0][0]["body"]["colorId"] == "3"


def test_cambiar_el_color_mueve_los_espejos_existentes(conn, tres_cuentas):
    """Si el color no entrara en la huella, cambiarlo no actualizaría nada."""
    ruca, galactic = tres_cuentas["Ruca"], tres_cuentas["Galactic"]
    engine.store_events(conn, ruca, [evento("junta-1")])
    antes = next(o for o in engine.compute_plan(conn)[0] if o["target_account_id"] == galactic)
    conn.execute("INSERT INTO mirrors VALUES (?, ?, ?, 'remoto-1', ?, ?)",
                 (ruca, "junta-1", galactic, antes["hash"], db.now_iso()))
    conn.commit()

    db.set_setting(conn, "mirror_color", "11")
    despues = [o for o in engine.compute_plan(conn)[0] if o["target_account_id"] == galactic]

    assert len(despues) == 1 and despues[0]["action"] == "update"


def test_la_limpieza_explica_cada_error(conn, tres_cuentas, monkeypatch):
    """Un contador que dice «1 error» sin decir cuál no sirve para nada."""
    falso = GoogleFalso(fallar_ids={"remoto-1"})
    falso.instalar(monkeypatch, engine, google_cal)

    engine.store_events(conn, tres_cuentas["Ruca"], [evento("junta-1")])
    engine.apply_plan(conn, engine.compute_plan(conn)[0], simulation=False)

    resultado = engine.cleanup_all_mirrors(conn)

    assert resultado["fallos"] >= 1
    assert resultado["errores"], "debe venir el detalle, no solo el número"
    assert "Google dijo no" in " ".join(resultado["errores"])


def test_ninguna_descripcion_lleva_texto_interno_en_espanol(conn, tres_cuentas):
    """Cualquier calendario puede tener a alguien mirándolo. Lo que CalSync
    escribe en un evento lo puede leer el equipo de Ruca."""
    detalle = db.get_setting(conn, "detail_pairs")
    for clave in detalle:
        detalle[clave] = {"mode": "title", "label": "", "share": True}
    db.set_setting(conn, "detail_pairs", detalle)

    crudo = evento("junta-1", summary="Comité")
    crudo["location"] = "Sala 3"
    crudo["hangoutLink"] = "https://meet.google.com/x"
    crudo["attendees"] = [{"email": "ana@x.com", "displayName": "Ana"}]
    engine.store_events(conn, tres_cuentas["Ruca"], [crudo])

    for op in engine.compute_plan(conn)[0]:
        texto = op["body"]["description"]
        for palabra in ("Bloque automático", "No lo edites", "recalcula",
                        "Videollamada", "Invitados", "Organiza:", "Detalle original"):
            assert palabra not in texto, f"«{palabra}» se lee y está en español"
        assert texto.startswith("Join:"), "el encabezado va en inglés"
        assert db.get_setting(conn, "mirror_note") in texto


def test_la_hora_del_registro_se_muestra_en_hora_local():
    """Un registro en UTC es un registro que no se puede leer. Colombia es
    UTC−5, así que las 21:30 UTC son las 16:30 en pantalla."""
    assert db.a_local("2026-08-04T21:30:00+00:00", "%H:%M") == "16:30"
    assert db.a_local("2026-08-04T02:00:00+00:00", "%d/%m %H:%M") == "03/08 21:00"
    assert db.a_local("") == "", "sin fecha no debe explotar"
    assert db.a_local("cualquier cosa") == "cualquier cosa"


def test_la_zona_horaria_sale_del_computador_no_del_codigo():
    """Con la zona fijada a Bogotá, un compañero en Manila vería su almuerzo
    doce horas corrido. Es requisito para que la use un equipo internacional."""
    import os
    from importlib import reload

    previo = os.environ.get("CALSYNC_TZ")
    try:
        os.environ["CALSYNC_TZ"] = "Asia/Manila"
        reload(config)
        assert str(config.TZ) == "Asia/Manila"

        # Un nombre inválido no puede tumbar el arranque.
        os.environ["CALSYNC_TZ"] = "no/existe"
        reload(config)
        assert config.TZ is not None
    finally:
        if previo is None:
            os.environ.pop("CALSYNC_TZ", None)
        else:
            os.environ["CALSYNC_TZ"] = previo
        reload(config)
