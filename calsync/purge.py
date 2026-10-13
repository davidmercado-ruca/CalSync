"""
Emergency cleanup, from the terminal, bypassing the web app.

Deletes from every account EVERY event carrying the CalSync marker, on any
date, with retries, verifying at the end that nothing is left.

Run it with `cleanup.command` (double-click) or:

    ./.venv/bin/python -m calsync.purge

It cannot touch a single event of yours: it can only delete events carrying
the private marker CalSync writes when it creates them.
"""

from __future__ import annotations

import sys

from . import db, engine, google_cal


def contar(conn):
    """How many CalSync events exist in each account, across all dates."""
    por_cuenta = {}
    for acct in db.accounts(conn):
        try:
            por_cuenta[acct["label"]] = len(
                google_cal.list_mirrors(conn, acct, all_time=True)
            )
        except Exception as e:  # noqa: BLE001
            por_cuenta[acct["label"]] = f"error: {e}"
    return por_cuenta


def main():
    conn = db.connect()
    try:
        cuentas = db.accounts(conn)
        if not cuentas:
            print("  No accounts connected. Nothing to clean.")
            return 0

        print("──────────────────────────────────────────")
        print("  CalSync cleanup")
        print("──────────────────────────────────────────")
        print()
        print("  This deletes EVERY event CalSync created in your calendars:")
        print("  busy blocks and protected blocks, on any date.")
        print()
        print("  Your own events CANNOT be touched: the delete only reaches")
        print("  events carrying the private CalSync marker.")
        print()

        print("  Counting what is there…")
        antes = contar(conn)
        for etiqueta, cuantos in antes.items():
            print(f"    {etiqueta:12} {cuantos}")
        total = sum(v for v in antes.values() if isinstance(v, int))
        print()

        if total == 0:
            print("  Already clean. Nothing to delete.")
            return 0

        print(f"  {total} events will be deleted.")
        respuesta = input("  Type DELETE to continue: ").strip().upper()
        if respuesta != "DELETE":
            print("  Cancelled. Nothing was touched.")
            return 1

        print()
        print("  Deleting. Going slowly to stay inside Google's limits…")
        resultado = engine.cleanup_all_mirrors(conn)
        print()
        print(f"    deleted:  {resultado['borrados']}")
        print(f"    errors:   {resultado['fallos']}")
        print(f"    left:     {resultado['quedan']}")
        print()

        # An error count without the error is useless.
        if resultado["errores"]:
            print("  What exactly failed:")
            for detalle in resultado["errores"]:
                print(f"    · {detalle}")
            print()

        if resultado["quedan"] == 0:
            print("  ✓ Done. Your calendars are back to how they were before CalSync.")
            return 0

        print("  ⚠ Some events were not deleted. Run this cleanup again;")
        print("    bookkeeping was kept on purpose so nothing gets duplicated.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
