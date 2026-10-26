"""
Pruebas del dashboard: que cada pantalla cargue y que cada formulario guarde
lo que dice guardar.

Las 109 pruebas del motor cubren la lógica de sincronización, pero ninguna
tocaba la plantilla ni las rutas. Un error de Jinja o un campo de formulario
mal nombrado no rompía ninguna prueba y sí rompía la herramienta.

Nada de esto habla con Google: el dashboard solo lee la base local.
"""

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from calsync import app as appmod
from calsync import config, db, habits


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    conn = db.connect()
    for etiqueta, correo in [("Ruca", "d@ruca.co"), ("Galactic", "d@galactic.co"),
                             ("Personal", "yo@gmail.com")]:
        db.upsert_account(conn, correo, etiqueta, json.dumps({"t": "x"}))
    db.ensure_pairs(conn)
    conn.commit()
    conn.close()
    # Sin `with`: así no arranca el lifespan, que montaría el reloj de fondo
    # y saldría a hablar con Google a los cinco segundos.
    yield TestClient(appmod.app)


def abrir(tmp_path):
    return db.connect()


# --- Que cada pantalla cargue ------------------------------------------------

def test_el_dashboard_carga_con_todas_sus_secciones(cliente):
    html = cliente.get("/").text
    for seccion in ("dashboard", "blocks", "accounts", "sharing", "general", "danger"):
        assert f'id="p-{seccion}"' in html


def test_lo_escondido_se_esconde_de_verdad(cliente):
    """`hidden` es del navegador, pero un `display` propio le gana.

    Sin esta regla, `.confirmar` —que tiene su propio display— enseñaba
    «Discard changes» y «Keep editing» desde que abría la página.
    """
    assert "[hidden]{display:none!important}" in cliente.get("/").text


def test_el_calendario_separa_sus_piezas(cliente):
    """El `gap` de la sección solo llega a sus hijos directos.

    El calendario entero vive dentro de #calWrap —para poder reemplazarlo al
    cambiar de semana—, así que la sección ve un solo hijo y dentro los
    controles, la leyenda, la rejilla y el pie quedaban pegados.
    """
    assert "#calWrap{display:flex;flex-direction:column;gap:" in cliente.get("/").text


def test_no_queda_jinja_sin_renderizar(cliente):
    """Una llave sin cerrar no rompe la página: la imprime tal cual."""
    html = cliente.get("/").text
    assert "{{" not in html and "{%" not in html


def test_los_textos_estan_en_ingles_americano(cliente):
    html = cliente.get("/").text
    for britanico in ("colour", "Colour", "organisation", "behaviour"):
        assert britanico not in html


def test_las_instrucciones_para_conectar_no_se_quedan_a_medias(cliente):
    """Quien conecta su correo puede no haber visto nunca una pantalla de
    OAuth. Sin el 'Advanced' se queda atascado en la advertencia de Google."""
    html = " ".join(cliente.get("/").text.split())   # como lo colapsa el navegador
    assert "the app is not verified" in html
    assert 'Continue via <b>"Advanced"</b>' in html


@pytest.mark.parametrize("url", ["/", "/?week=1", "/?week=-3", "/?week=99",
                                 "/?week=0&meetings=0", "/api/status"])
def test_las_urls_del_calendario_responden(cliente, url):
    assert cliente.get(url).status_code == 200


# --- Guardar un bloque -------------------------------------------------------

def test_guardar_un_bloque_no_toca_los_demas(cliente, tmp_path):
    """El guardado era de los seis a la vez y borraba todas las posiciones ya
    decididas, así que cambiarle la ventana al almuerzo reubicaba el ejercicio
    sin que nadie lo pidiera."""
    conn = db.connect()
    lunch = next(h for h in db.habits(conn) if "Lunch" in h["name"])
    otro = next(h for h in db.habits(conn) if "Exercise" in h["name"])
    antes = dict(otro)
    conn.close()

    r = cliente.post(f"/block/{lunch['id']}", data={
        "name": "🍽 Almuerzo", "kind": "flexible", "w0": "12:00", "w1": "14:30",
        "pref": "13:00", "ideal": "60", "min": "45", "per_week": "3",
        "color": "4", "day_0": "on", "day_1": "on", "enabled": "on",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].endswith("#blocks")

    conn = db.connect()
    lunch2 = conn.execute("SELECT * FROM habits WHERE id = ?", (lunch["id"],)).fetchone()
    otro2 = conn.execute("SELECT * FROM habits WHERE id = ?", (otro["id"],)).fetchone()
    assert lunch2["name"] == "🍽 Almuerzo"
    assert (lunch2["window_start"], lunch2["window_end"]) == ("12:00", "14:30")
    assert lunch2["ideal_minutes"] == 60 and lunch2["min_minutes"] == 45
    assert lunch2["per_week"] == 3 and lunch2["days"] == "0,1"
    for campo in ("name", "window_start", "window_end", "ideal_minutes", "per_week", "days"):
        assert otro2[campo] == antes[campo], f"se movió {campo} del bloque de al lado"
    conn.close()


def test_un_bloque_fijo_no_guarda_un_minimo_menor_que_su_duracion(cliente):
    """Un bloque de hora fija no se recorta, así que su mínimo ES su duración.
    El formulario ni siquiera manda el campo: se deshabilita en pantalla."""
    conn = db.connect()
    tarde = next(h for h in db.habits(conn) if "Not available" in h["name"])
    conn.close()

    cliente.post(f"/block/{tarde['id']}", data={
        "name": tarde["name"], "kind": "fixed", "pref": "17:00",
        "ideal": "180", "per_week": "0", "enabled": "on", "day_0": "on",
    })
    conn = db.connect()
    fila = conn.execute("SELECT * FROM habits WHERE id = ?", (tarde["id"],)).fetchone()
    assert fila["min_minutes"] == fila["ideal_minutes"] == 180
    conn.close()


def test_guardar_un_bloque_solo_reabre_sus_propias_posiciones(cliente):
    conn = db.connect()
    lunch = next(h for h in db.habits(conn) if "Lunch" in h["name"])
    ejercicio = next(h for h in db.habits(conn) if "Exercise" in h["name"])
    dia = habits.hoy().isoformat()
    for hid in (lunch["id"], ejercicio["id"]):
        conn.execute(
            "INSERT INTO habit_slots (habit_id, day, start_local, minutes) VALUES (?, ?, ?, 60)",
            (hid, dia, habits.local(habits.hoy(), habits.hhmm("13:00")).isoformat()))
    conn.commit()
    conn.close()

    # Sin seguir el redirect a propósito: el GET del dashboard vuelve a decidir
    # las posiciones de todos, y taparía justo lo que se quiere comprobar.
    cliente.post(f"/block/{lunch['id']}", follow_redirects=False, data={
        "name": lunch["name"], "kind": "flexible", "w0": "12:00", "w1": "15:00",
        "ideal": "90", "min": "60", "per_week": "0", "enabled": "on", "day_0": "on"})

    conn = db.connect()
    quedan = {r["habit_id"] for r in conn.execute("SELECT habit_id FROM habit_slots")}
    assert lunch["id"] not in quedan, "la posición del bloque editado debe recalcularse"
    assert ejercicio["id"] in quedan, "la del bloque de al lado no se toca"
    conn.close()


# --- Guardar ajustes ---------------------------------------------------------

def test_general_guarda_modo_y_reglas(cliente):
    r = cliente.post("/settings/general", data={
        "simulation": "on", "skip_all_day": "on", "skip_tags": "[nosync], [privado]",
        "skip_keywords": "",
    }, follow_redirects=False)
    assert r.headers["location"].endswith("#general")

    conn = db.connect()
    ajustes = db.all_settings(conn)
    assert ajustes["simulation"] is True
    assert ajustes["rules"]["skip_all_day"] is True
    assert ajustes["rules"]["skip_transparent"] is False   # no venía marcado
    assert ajustes["rules"]["skip_tags"] == ["[nosync]", "[privado]"]
    assert ajustes["rules"]["skip_keywords"] == []
    conn.close()


def test_sharing_guarda_pares_palabra_color_y_notas(cliente):
    conn = db.connect()
    ids = [a["id"] for a in db.accounts(conn)]
    conn.close()
    o, d = ids[0], ids[1]

    r = cliente.post("/settings/sharing", data={
        "busy_word": "Ocupado", "mirror_color": "5",
        "mirror_note": "nota espejo", "habit_note": "nota bloque",
        f"pair_{o}_{d}": "on", f"mode_{o}_{d}": "custom",
        f"label_{o}_{d}": "Work", f"share_{o}_{d}": "on",
    }, follow_redirects=False)
    assert r.headers["location"].endswith("#sharing")

    conn = db.connect()
    ajustes = db.all_settings(conn)
    assert ajustes["busy_word"] == "Ocupado"
    assert ajustes["mirror_color"] == "5"
    assert ajustes["mirror_note"] == "nota espejo"
    assert ajustes["habit_note"] == "nota bloque"
    par = ajustes["detail_pairs"][f"{o}|{d}"]
    assert par == {"mode": "custom", "label": "Work", "share": True}
    conn.close()


def test_la_palabra_de_ocupado_nunca_queda_vacia(cliente):
    """Un espejo sin título sería un evento en blanco en el calendario ajeno."""
    cliente.post("/settings/sharing", data={"busy_word": "   "})
    conn = db.connect()
    assert db.get_setting(conn, "busy_word") == "Busy"
    conn.close()


def test_cada_formulario_vuelve_a_su_propia_seccion(cliente):
    conn = db.connect()
    hid = db.habits(conn)[0]["id"]
    conn.close()
    esperado = [
        ("/settings/general", {}, "#general"),
        ("/settings/sharing", {}, "#sharing"),
        (f"/block/{hid}", {"kind": "flexible", "ideal": "60"}, "#blocks"),
        ("/blocks/new", {"nombre": "Nuevo"}, "#blocks"),
        ("/trash/dismiss", {}, "#blocks"),
    ]
    for ruta, datos, destino in esperado:
        r = cliente.post(ruta, data=datos, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].endswith(destino), f"{ruta} fue a {r.headers['location']}"


def test_borrar_y_deshacer_un_bloque(cliente):
    conn = db.connect()
    victima = next(h for h in db.habits(conn) if "Reading" in h["name"])
    conn.close()

    cliente.post(f"/block/{victima['id']}/delete")
    conn = db.connect()
    assert not [h for h in db.habits(conn) if h["id"] == victima["id"]]
    assert db.get_setting(conn, "papelera")["name"] == victima["name"]
    conn.close()

    cliente.post("/trash/restore")
    conn = db.connect()
    assert any(h["name"] == victima["name"] for h in db.habits(conn))
    assert db.get_setting(conn, "papelera") is None
    conn.close()


def test_la_limpieza_total_exige_la_palabra_exacta(cliente):
    r = cliente.post("/wipe", data={"confirmacion": "borrar porfa"}, follow_redirects=False)
    assert r.headers["location"].endswith("#danger")
    conn = db.connect()
    assert any(l["action"] == "limpieza_cancelada" for l in db.recent_log(conn, 10))
    conn.close()


# --- El calendario que cambia sin recargar ----------------------------------

def test_la_ruta_week_devuelve_solo_el_calendario(cliente):
    """Es lo que pide el dashboard al pulsar una flecha. Si devolviera la
    página entera, meterla dentro de sí misma rompería el documento."""
    r = cliente.get("/week?week=1")
    assert r.status_code == 200
    html = r.text
    assert 'class="cal-barra"' in html, "debe traer el calendario"
    assert "<html" not in html and "<nav" not in html, "y nada más"


def test_las_flechas_no_llevan_la_preferencia_de_reuniones(cliente):
    """La preferencia se anota durante una lectura, así que ninguna dirección
    navegable puede llevarla dentro: bastaría con que el navegador precargara
    un enlace al pasar el ratón para cambiarla sin que nadie la toque."""
    import re
    html = cliente.get("/").text
    for enlace in re.findall(r'href="(/\?week=[^"]*)"', html):
        assert "meetings" not in enlace, f"la flecha {enlace} lleva la preferencia"


def test_la_casilla_de_reuniones_se_recuerda(cliente):
    conn = db.connect()
    assert db.get_setting(conn, "show_meetings") is True, "por defecto se muestran"
    conn.close()

    cliente.get("/?meetings=0")
    conn = db.connect()
    assert db.get_setting(conn, "show_meetings") is False
    conn.close()

    # Y una carga sin el parámetro respeta lo guardado.
    html = cliente.get("/").text
    assert 'id="verReuniones"' in html
    assert 'id="verReuniones" checked' not in html.replace("\n", " ")

    cliente.get("/?meetings=1")
    conn = db.connect()
    assert db.get_setting(conn, "show_meetings") is True
    conn.close()


# --- El copy no asume cuántas cuentas hay -----------------------------------

def test_el_copy_cuenta_los_calendarios_conectados(cliente):
    html = " ".join(cliente.get("/").text.split())
    assert "all 3 calendars" in html
    for supuesto in ("three calendars", "three accounts", "all 1 calendars",
                     "all 2 calendars"):
        assert supuesto not in html


def test_con_dos_cuentas_dice_both_y_con_una_no_cuenta(cliente):
    conn = db.connect()
    ids = [a["id"] for a in db.accounts(conn)]
    conn.close()

    cliente.post(f"/account/{ids[2]}/disconnect")
    html = " ".join(cliente.get("/").text.split())
    assert "both calendars" in html and "all 2 calendars" not in html

    cliente.post(f"/account/{ids[1]}/disconnect")
    html = " ".join(cliente.get("/").text.split())
    assert "your calendar" in html and "calendars" not in html.split("Danger zone")[0][-400:]
    # Con un solo calendario, la promesa de ver varios a la vez sobra.
    assert "which no single Google Calendar can show you" not in html


def test_el_ejemplo_de_la_palabra_busy_no_nombra_una_cuenta(cliente):
    """Elegía la primera cuenta conectada, así que para alguien con varias el
    ejemplo era falso la mayoría de las veces."""
    html = " ".join(cliente.get("/").text.split())
    assert "followed by the name of the calendar it came from" in html
    assert '"Busy (Ruca)"' not in html


def test_el_pie_nombra_el_primer_evento_no_donde_abre_el_scroll(cliente):
    conn = db.connect()
    lunch = next(h for h in db.habits(conn) if "Lunch" in h["name"])
    dia = habits.hoy()
    conn.execute(
        "INSERT INTO habit_slots (habit_id, day, start_local, minutes) VALUES (?, ?, ?, 60)",
        (lunch["id"], dia.isoformat(), habits.local(dia, habits.hhmm("05:00")).isoformat()))
    conn.commit()
    conn.close()

    html = " ".join(cliente.get("/week?week=0").text.split())
    assert "Earliest this week is 5am" in html, "nombra el evento"
    assert "Opens at" not in html


def test_general_guarda_la_regla_de_eventos_largos(cliente):
    r = cliente.post("/settings/general", data={
        "skip_long": "on", "long_hours": "9", "skip_tags": "", "skip_keywords": "",
    }, follow_redirects=False)
    assert r.headers["location"].endswith("#general")

    conn = db.connect()
    reglas = db.all_settings(conn)["rules"]
    assert reglas["skip_long"] is True and reglas["long_hours"] == 9
    conn.close()


@pytest.mark.parametrize("basura", ["", "doce", "0", "99", "-3"])
def test_las_horas_invalidas_no_rompen_el_ajuste(cliente, basura):
    """Un número imposible no puede dejar la regla en un estado que el motor no
    sepa leer: se queda el que ya estaba guardado."""
    cliente.post("/settings/general", data={
        "skip_long": "on", "long_hours": "10", "skip_tags": "", "skip_keywords": "",
    })
    cliente.post("/settings/general", data={
        "skip_long": "on", "long_hours": basura, "skip_tags": "", "skip_keywords": "",
    })
    conn = db.connect()
    assert db.all_settings(conn)["rules"]["long_hours"] == 10
    conn.close()


def test_la_regla_de_eventos_largos_aparece_en_general(cliente):
    html = cliente.get("/").text
    assert "Events too long to be a meeting" in html
    assert 'name="long_hours"' in html and 'name="skip_long"' in html
