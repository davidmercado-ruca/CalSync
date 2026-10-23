"""
El dashboard y el reloj.

Un solo proceso: sirve la interfaz en 127.0.0.1:8765 y corre el ciclo de
sincronización cada pocos minutos en un hilo de fondo.
"""

import threading
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import config, db, engine, google_cal, habits

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Un solo ciclo a la vez. Si un ciclo se demora más que el intervalo, el
# siguiente se salta en vez de pisarlo.
_cycle_lock = threading.Lock()
_last_summary: dict = {}


def cycle_job(force_full: bool = False):
    global _last_summary
    if not _cycle_lock.acquire(blocking=False):
        return {"saltado": "ya había un ciclo corriendo"}
    conn = db.connect()
    try:
        _last_summary = engine.run_cycle(conn, force_full=force_full)
        return _last_summary
    finally:
        conn.close()
        _cycle_lock.release()


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect()
    # Dejar constancia de DESDE DÓNDE arrancó: el servicio de fondo corre sobre
    # una copia del código, así que esta línea es la forma de saber si estás
    # viendo la versión desplegada o la de tu carpeta de trabajo.
    db.log(conn, "arranque",
           f"listening on http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT} "
           f"· code at {config.PROJECT_DIR}")
    conn.close()

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        cycle_job,
        "interval",
        minutes=config.POLL_MINUTES,
        id="ciclo",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    # Un primer ciclo poco después de arrancar, sin bloquear el arranque.
    threading.Timer(5.0, cycle_job).start()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title="CalSync", lifespan=lifespan)


# --- Interfaz ----------------------------------------------------------------

# Cada formulario vuelve a la sección desde la que se envió. Sin esto, guardar
# un bloque te devolvía al principio del dashboard y tocaba volver a navegar.
def volver(seccion: str = "dashboard"):
    return RedirectResponse(f"/#{seccion}", status_code=303)


@app.get("/")
def dashboard(request: Request, week: int = 0, meetings: int = 1):
    conn = db.connect()
    try:
        settings = db.all_settings(conn)
        accounts = db.accounts(conn, only_enabled=False)
        ops, skipped = engine.compute_plan(conn)
        ops_habitos, conflictos = habits.compute_plan(conn)
        counts = {
            "eventos": conn.execute(
                "SELECT COUNT(*) c FROM source_events WHERE is_mirror = 0"
            ).fetchone()["c"],
            "espejos": conn.execute("SELECT COUNT(*) c FROM mirrors").fetchone()["c"],
            "bloques": conn.execute("SELECT COUNT(*) c FROM habit_blocks").fetchone()["c"],
        }
        todas = ops + ops_habitos
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "accounts": accounts,
                "settings": settings,
                "last_cycle": settings.get("last_cycle"),
                "ops": todas[:60],
                "ops_total": len(todas),
                "skipped": skipped[:30],
                "skipped_total": len(skipped),
                "counts": counts,
                "habits": db.habits(conn),
                "papelera": settings.get("papelera"),
                "conflicts": conflictos,
                # Las flechas de semana llegan como ?week=; se acotan para que
                # una URL escrita a mano no pida el año 3000.
                "vista": habits.vista_semana(
                    conn, offset=max(-52, min(52, week)), con_reuniones=bool(meetings)
                ),
                "log": db.recent_log(conn, 60),
                "credentials_ok": config.CREDENTIALS_FILE.exists(),
                "poll": config.POLL_MINUTES,
                "horizonte": config.HABITS_HORIZON_DAYS,
                "dias": habits.DIAS_CORTOS,
                "opciones_duracion": habits.opciones_duracion,
                "opciones_hora": habits.opciones_hora,
                "colores": config.COLORES,
                "colores_hex": config.COLOR_HEX,
                "px_hora": config.PX_HORA,
                "a_local": db.a_local,
                "acciones": config.LOG_LABELS,
                # El desplegable de cada bloque muestra la cuenta que le toca
                # de verdad, incluso si nunca se guardó explícitamente.
                "origen_de": lambda h: db.cuenta_origen(conn, h),
                "describe": engine.describe,
            },
        )
    finally:
        conn.close()


@app.post("/connect")
def conectar(label: str = Form("")):
    """Abre el navegador para autorizar una cuenta. Bloquea hasta terminar."""
    conn = db.connect()
    try:
        google_cal.authorize(conn, label)
    except Exception as e:  # noqa: BLE001
        db.log(conn, "error_conexion", str(e), "error")
    finally:
        conn.close()
    return volver("accounts")


@app.post("/account/{account_id}/disconnect")
def desconectar(account_id: int):
    conn = db.connect()
    try:
        acct = db.account(conn, account_id)
        conn.execute("UPDATE accounts SET enabled = 0 WHERE id = ?", (account_id,))
        conn.execute("DELETE FROM source_events WHERE account_id = ?", (account_id,))
        conn.commit()
        db.log(conn, "cuenta_desconectada", acct["email"] if acct else account_id, "warn")
    finally:
        conn.close()
    return volver("accounts")


@app.post("/settings/general")
async def ajustes_generales(request: Request):
    """Modo y qué eventos NO bloquean tiempo."""
    form = await request.form()
    conn = db.connect()
    try:
        settings = db.all_settings(conn)
        db.set_setting(conn, "simulation", form.get("simulation") == "on")

        reglas = dict(settings["rules"])
        for flag in ("skip_transparent", "skip_all_day", "skip_declined", "skip_needs_action"):
            reglas[flag] = form.get(flag) == "on"
        reglas["skip_tags"] = [
            t.strip() for t in (form.get("skip_tags") or "").split(",") if t.strip()
        ]
        reglas["skip_keywords"] = [
            k.strip() for k in (form.get("skip_keywords") or "").split(",") if k.strip()
        ]
        db.set_setting(conn, "rules", reglas)
        db.log(conn, "ajustes_guardados", "general")
    finally:
        conn.close()
    return volver("general")


@app.post("/settings/sharing")
async def ajustes_compartir(request: Request):
    """Qué revela cada calendario a los otros, y qué leen los demás."""
    form = await request.form()
    conn = db.connect()
    try:
        db.set_setting(conn, "busy_word", (form.get("busy_word") or "Busy").strip() or "Busy")
        db.set_setting(conn, "mirror_color", (form.get("mirror_color") or "").strip())
        db.set_setting(conn, "mirror_note", (form.get("mirror_note") or "").strip())
        db.set_setting(conn, "habit_note", (form.get("habit_note") or "").strip())

        ids = [str(a["id"]) for a in db.accounts(conn)]
        pares, detalle = {}, {}
        for src in ids:
            for tgt in ids:
                if src == tgt:
                    continue
                pares[f"{src}|{tgt}"] = form.get(f"pair_{src}_{tgt}") == "on"
                detalle[f"{src}|{tgt}"] = {
                    "mode": form.get(f"mode_{src}_{tgt}") or "busy",
                    "label": (form.get(f"label_{src}_{tgt}") or "").strip(),
                    "share": form.get(f"share_{src}_{tgt}") == "on",
                }
        db.set_setting(conn, "pairs", pares)
        db.set_setting(conn, "detail_pairs", detalle)
        db.log(conn, "ajustes_guardados", "sharing")
    finally:
        conn.close()
    return volver("sharing")


@app.post("/block/{habit_id}")
async def guardar_habito(habit_id: int, request: Request):
    """Guarda UN bloque. Los demás no se tocan.

    Antes se guardaban los seis de una vez y se borraban todas las posiciones
    ya decididas, así que cambiarle la ventana al almuerzo también reubicaba el
    ejercicio sin que nadie lo pidiera. Ahora solo se reabre la decisión del
    bloque que cambió.
    """
    form = await request.form()
    conn = db.connect()
    try:
        habito = conn.execute(
            "SELECT * FROM habits WHERE id = ?", (habit_id,)).fetchone()
        if habito is None:
            return volver("blocks")

        dias = ",".join(str(n) for n in range(7) if form.get(f"day_{n}") == "on")
        tipo = form.get("kind") or "flexible"
        ideal = max(15, int(form.get("ideal") or habito["ideal_minutes"]))
        # Un bloque de hora fija no se recorta: su mínimo ES su duración.
        minimo = ideal if tipo == "fixed" else max(15, int(form.get("min") or ideal))

        conn.execute(
            "UPDATE habits SET name = ?, enabled = ?, kind = ?, ideal_minutes = ?, "
            "min_minutes = ?, window_start = ?, window_end = ?, preferred_start = ?, "
            "days = ?, per_week = ?, share = ?, color_id = ?, home_account_id = ?, "
            "cleanup_past = ? WHERE id = ?",
            (
                (form.get("name") or habito["name"]).strip(),
                1 if form.get("enabled") == "on" else 0,
                tipo,
                ideal,
                min(minimo, ideal),   # el mínimo nunca puede pasar del ideal
                form.get("w0") or habito["window_start"],
                form.get("w1") or habito["window_end"],
                (form.get("pref") or "").strip() or None,
                dias or habito["days"],
                int(form.get("per_week") or 0),
                1 if form.get("share") == "on" else 0,
                (form.get("color") or "").strip(),
                int(form.get("home")) if (form.get("home") or "").isdigit() else None,
                1 if form.get("cleanup") == "on" else 0,
                habit_id,
            ),
        )
        # Cambió su regla: se vuelven a decidir SUS posiciones, y se olvidan los
        # días que habías borrado a mano de ESTE bloque (la regla nueva manda).
        conn.execute("DELETE FROM habit_skips WHERE habit_id = ?", (habit_id,))
        conn.execute("DELETE FROM habit_slots WHERE habit_id = ?", (habit_id,))
        conn.commit()
        db.log(conn, "habitos_guardados", habito["name"])
    finally:
        conn.close()
    return volver("blocks")


CAMPOS_HABITO = (
    "name", "enabled", "kind", "ideal_minutes", "min_minutes", "window_start",
    "window_end", "preferred_start", "days", "per_week", "share", "priority",
)


@app.post("/block/{habit_id}/delete")
def eliminar_habito(habit_id: int):
    """Borra un bloque de un solo clic, guardando una copia para deshacer.

    Un clic sin red de seguridad sería una trampa: borrar un hábito también
    borra sus eventos de los tres calendarios.
    """
    conn = db.connect()
    try:
        fila = conn.execute("SELECT * FROM habits WHERE id = ?", (habit_id,)).fetchone()
        if fila is not None:
            db.set_setting(conn, "papelera", {c: fila[c] for c in CAMPOS_HABITO})
            conn.execute("DELETE FROM habits WHERE id = ?", (habit_id,))
            conn.execute("DELETE FROM habit_slots WHERE habit_id = ?", (habit_id,))
            conn.commit()
            db.log(conn, "habito_borrado", fila["name"], "warn")
    finally:
        conn.close()
    return volver("blocks")


@app.post("/trash/restore")
def restaurar_habito():
    conn = db.connect()
    try:
        guardado = db.get_setting(conn, "papelera")
        if guardado:
            columnas = ", ".join(CAMPOS_HABITO)
            marcas = ", ".join("?" for _ in CAMPOS_HABITO)
            conn.execute(
                f"INSERT INTO habits ({columnas}, created_at) VALUES ({marcas}, ?)",
                tuple(guardado[c] for c in CAMPOS_HABITO) + (db.now_iso(),),
            )
            conn.commit()
            db.log(conn, "habito_restaurado", guardado["name"])
        db.set_setting(conn, "papelera", None)
    finally:
        conn.close()
    return volver("blocks")


@app.post("/trash/dismiss")
def descartar_papelera():
    conn = db.connect()
    try:
        db.set_setting(conn, "papelera", None)
    finally:
        conn.close()
    return volver("blocks")


@app.post("/blocks/new")
def nuevo_habito(nombre: str = Form("")):
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO habits (name, kind, ideal_minutes, min_minutes, window_start, "
            "window_end, days, per_week, share, priority, created_at, enabled) "
            "VALUES (?, 'flexible', 60, 30, '09:00', '18:00', '0,1,2,3,4', 0, 1, 50, ?, 0)",
            ((nombre or "Nuevo hábito").strip(), db.now_iso()),
        )
        conn.commit()
        db.log(conn, "habito_creado", nombre)
    finally:
        conn.close()
    return volver("blocks")


@app.post("/sync")
def sincronizar(full: str = Form("")):
    cycle_job(force_full=(full == "on"))
    return volver("dashboard")


@app.post("/wipe")
def limpiar(confirmacion: str = Form("")):
    """Limpieza total. Se hace con el reloj pausado.

    Sin pausar, el ciclo de fondo podía dispararse a mitad de la limpieza y
    empezar a crear lo mismo que se estaba borrando.
    """
    conn = db.connect()
    try:
        if confirmacion.strip().upper() != "BORRAR":
            db.log(conn, "limpieza_cancelada", "wrong confirmation word", "warn")
            return volver("danger")

        # El modo simulación queda encendido: al terminar, los calendarios están
        # vacíos de CalSync y nada se recrea hasta que tú lo decidas.
        db.set_setting(conn, "simulation", True)
        with _cycle_lock:
            engine.cleanup_all_mirrors(conn)
    finally:
        conn.close()
    return volver("danger")


@app.get("/api/status")
def estado():
    conn = db.connect()
    try:
        settings = db.all_settings(conn)
        ops, _ = engine.compute_plan(conn)
        return {
            "modo": "simulación" if settings["simulation"] else "activo",
            "cuentas": [
                {"email": a["email"], "label": a["label"],
                 "ultimo_sync": a["last_sync"], "error": a["last_error"]}
                for a in db.accounts(conn)
            ],
            "operaciones_pendientes": len(ops),
            "ultimo_ciclo": settings.get("last_cycle"),
        }
    finally:
        conn.close()


def main():
    import uvicorn

    uvicorn.run(
        app,
        host=config.DASHBOARD_HOST,
        port=config.DASHBOARD_PORT,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
