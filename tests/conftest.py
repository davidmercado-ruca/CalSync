"""Fija la zona horaria antes de que se importe `calsync.config`.

`config.TZ` ahora se detecta del sistema, y el contenedor de pruebas está en
UTC. Sin esto, las pruebas que verifican horas de Bogotá fallarían según dónde
se corran, que es justo el tipo de prueba frágil que no sirve para nada.
"""

import os

os.environ.setdefault("CALSYNC_TZ", "America/Bogota")
