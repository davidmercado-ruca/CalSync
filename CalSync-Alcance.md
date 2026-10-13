# CalSync — Documento de Alcance

**Proyecto:** Skills Dev · **Owner:** David Mercado · **Fecha:** 3 de agosto de 2026
**Nombre provisional:** CalSync

---

## 1. Problema

David opera con tres identidades de calendario simultáneas (Ruca, Galactic, personal). Google no comparte disponibilidad entre ellas: **cuando alguien consulta la disponibilidad de David, Google solo revisa su calendario primario de esa cuenta**. El resultado práctico es doble agendamiento constante, y la imposibilidad de proteger tiempo personal (almuerzo, ejercicio, trabajo profundo) de forma que el resto del mundo la respete.

Reclaim.ai resuelve esto, pero es un SaaS de pago que exige entregar acceso completo a tres calendarios corporativos a un tercero.

## 2. Objetivo

Una herramienta local — corriendo en el Mac de David, sin servicios de terceros y sin costo recurrente — que:

1. Mantenga los tres calendarios mutuamente conscientes: mover un evento en uno reubica su bloqueo en los otros dos.
2. Proteja bloques de tiempo definidos por David (almuerzo, focus time, ejercicio, horario laboral) en las tres cuentas a la vez.
3. Ofrezca un dashboard local donde David configura reglas y ve qué está pasando, sin editar archivos de código.

**Objetivo secundario, igual de importante:** que David construya esto vía vibe coding y termine entendiendo cómo funciona. El código se optimiza para ser legible por un no-developer, no para ser elegante.

## 3. Restricción crítica: cero tokens de Claude en operación

Esta es una condición de diseño, no una preferencia.

| | Consumo de tokens |
|---|---|
| Construcción de la herramienta (Claude Code, una vez) | Sí — es la única inversión |
| Operación diaria: detectar cambios, crear espejos, mover bloques | **Cero.** Es código Python ejecutándose solo |
| Ajustar reglas, horas de almuerzo, hábitos | **Cero.** Se hace en el dashboard |
| Agregar una función nueva o arreglar un bug | Sí — solo cuando David decida evolucionarla |

**No hay ninguna llamada a un modelo de IA en el camino de ejecución.** La lógica de "este evento se movió, entonces muevo su espejo" es aritmética de fechas y comparación de strings: un LLM ahí sería más lento, más caro y menos confiable. La API de Google Calendar es gratuita hasta 1.000.000 de consultas diarias; el consumo estimado de CalSync es de ~1.500 al día. Costo de operación: **USD 0,00 / mes, indefinidamente.**

---

## 4. Decisiones de alcance

| Decisión | Definición |
|---|---|
| **Proveedores** | Las tres cuentas son Google. Ruca y Galactic son Workspace; la personal es Gmail. |
| **Modelo de sync** | Espejos de ocupado unidireccionales por par. Un evento real genera bloques "Ocupado" en las otras cuentas. Los espejos no se editan; se recalculan. |
| **Destino de los espejos** | **Calendario primario** de cada cuenta — es la única forma de que terceros no agenden encima. Cada espejo lleva una marca invisible (`extendedProperties.private`) más prefijo visible `🔒`. |
| **Nivel de detalle** | Configurable por calendario de origen. Ej: `Personal → Ruca` muestra solo `🔒 Ocupado (Personal)`; `Ruca → Galactic` puede mostrar el título completo. |
| **Control de qué bloquea** | Todo bloquea por defecto, con cuatro capas de excepción: tag `[nosync]` en el título, reglas en el dashboard, matriz de direcciones 3×3, y respeto al estado Free/Busy de Google. |
| **Bloques protegidos** | Almuerzo fijo, focus time flexible, buffers entre reuniones, horario laboral, y hábitos personales definibles por David (ejercicio, etc.). |
| **Manejo de conflictos** | La herramienta **nunca mueve reuniones con otras personas.** Detecta el conflicto, lo muestra en el dashboard, y reubica únicamente sus propios bloques flexibles. |
| **Latencia** | Polling incremental cada 3 minutos con sync tokens de Google. |
| **Ventana temporal** | 7 días atrás → 60 días adelante. |
| **Zona horaria** | America/Bogota como base, con manejo explícito de eventos en otras zonas. |
| **Stack** | Python 3.11+ · FastAPI · SQLite · APScheduler. Dashboard servido por el mismo proceso (HTML + Tailwind por CDN, sin build). |
| **Entrega** | Por fases. Cada fase se usa en producción antes de empezar la siguiente. |

---

## 5. Arquitectura

Un solo proceso Python que hace tres cosas:

```
┌─────────────────────────────────────────────────────┐
│  CalSync (un proceso, arranca con el Mac)           │
│                                                     │
│  ┌──────────────┐  ┌────────────┐  ┌─────────────┐  │
│  │  Scheduler   │  │   Motor    │  │  Dashboard  │  │
│  │ cada 3 min   │→ │  de sync   │  │  FastAPI    │  │
│  └──────────────┘  └─────┬──────┘  │ :8765       │  │
│                          │         └──────┬──────┘  │
│                     ┌────▼─────────────────▼──────┐ │
│                     │  SQLite  (calsync.db)       │ │
│                     │  · mapeo evento ↔ espejos   │ │
│                     │  · sync tokens por cuenta   │ │
│                     │  · reglas, hábitos, config  │ │
│                     │  · log de operaciones       │ │
│                     └─────────────────────────────┘ │
└──────────────────┬──────────────────────────────────┘
                   │  HTTPS
        ┌──────────┼──────────┐
   ┌────▼───┐ ┌────▼────┐ ┌───▼─────┐
   │  Ruca  │ │ Galactic│ │Personal │   Google Calendar API
   └────────┘ └─────────┘ └─────────┘
```

### Cómo evita loops infinitos

El riesgo obvio de sincronizar A↔B↔C es que un espejo se lea como evento original y genere un espejo del espejo, en cascada.

La solución es una regla dura: **todo evento que CalSync crea lleva la marca `calsync_owned` en sus `extendedProperties.private`, y el motor ignora por completo cualquier evento marcado al momento de leer.** Los espejos existen pero son invisibles como fuente. No hay loop posible, ni siquiera si algo falla a medias.

Esa misma marca guarda el ID del evento original y un hash de sus datos relevantes (inicio, fin, estado). En cada ciclo el motor compara hashes: si nada cambió, no escribe nada. Solo los eventos que efectivamente se movieron generan llamadas de escritura.

### Cómo detecta cambios sin releer todo

Google entrega un `syncToken` en cada consulta. En el siguiente ciclo, CalSync lo envía de vuelta y Google responde únicamente con lo que cambió desde entonces — normalmente cero eventos. Eso hace que un ciclo típico cueste tres llamadas HTTP y milisegundos de CPU. Cuando Google invalida el token (cada pocos días, o tras un cambio grande), el motor hace una resincronización completa de la ventana y obtiene un token nuevo.

### Seguridad de datos

Los tokens OAuth y la base SQLite viven solo en el disco del Mac, en `~/.calsync/`, con permisos de solo-usuario. Nada sale hacia un servidor que no sea Google. El dashboard escucha únicamente en `127.0.0.1`, así que no es alcanzable desde la red local.

---

## 6. Fases de entrega

### Fase 0 — Cimientos (habilitador, no entrega valor visible)

- Proyecto de Google Cloud + credenciales OAuth, publicado **en producción** (ver Riesgo 1).
- Conexión y autorización de las tres cuentas.
- Lectura de los tres calendarios y volcado a SQLite.
- **Modo simulación:** el motor calcula todo lo que haría y lo escribe en el log, **sin tocar ningún calendario.**

*Listo cuando:* David ve en el dashboard sus tres calendarios leídos correctamente y una lista de "esto es lo que crearía", sin que nada se haya modificado en Google.

### Fase 1 — MVP: espejos vivos entre las tres cuentas

- Creación, actualización y borrado de espejos en los tres primarios.
- Mover un evento en Ruca reubica sus espejos en Galactic y personal en menos de 3 minutos.
- Borrar el evento original borra sus espejos.
- Matriz de direcciones 3×3 y nivel de detalle por origen, editables en el dashboard.
- Tag `[nosync]` funcionando.
- Botón **"eliminar todos los espejos"**: revierte los tres calendarios al estado previo, garantizado por la marca `calsync_owned`.
- Arranque automático con el Mac (launchd).

*Listo cuando:* David lleva una semana sin doble agendamiento y no ha tenido que abrir el dashboard.

### Fase 2 — Bloques protegidos y hábitos

- Almuerzo fijo: rango horario y días, creado como evento real en las tres cuentas.
- Horario laboral: bloqueo automático fuera de la jornada y en franjas sin reuniones.
- **Hábitos definibles por David:** nombre, duración, frecuencia (diaria o N veces por semana), ventana horaria preferida, días válidos, y si es fijo o flexible. Ejercicio, lectura, lo que sea — se agregan desde el dashboard sin tocar código.
- Panel de conflictos: cuando una reunión pisa un bloque protegido, aparece listada con un botón para reubicar el bloque manualmente.

*Listo cuando:* el almuerzo y al menos un hábito personal se respetan sin intervención, y los conflictos aparecen antes de que David los descubra por accidente.

### Fase 3 — Automatización inteligente

- Focus time flexible: N horas por semana ubicadas automáticamente en los huecos reales de las tres agendas, reacomodándose cuando entra una reunión.
- Buffers automáticos de 10–15 min entre reuniones consecutivas.
- Reglas de prioridad entre bloques (¿el focus cede ante el ejercicio, o al revés?).
- Vista semanal unificada de las tres agendas en un solo lugar.

*Listo cuando:* David deja de mover manualmente sus bloques de trabajo profundo.

---

## 7. Riesgos y mitigaciones

**1. El token de acceso expira cada 7 días.**
Las apps de Google en estado "Testing" emiten refresh tokens que mueren a los 7 días — el daemon dejaría de funcionar cada semana pidiendo reautorización. La mitigación es publicar la app en estado **"In production"** desde el inicio. Al ser una app sin verificar, Google mostrará una pantalla de advertencia la primera vez que David autorice cada cuenta; se pasa con "Avanzado → Continuar". Es una app privada de un solo usuario, así que la verificación formal no aplica.

**2. El admin de Workspace de Ruca o Galactic puede bloquear apps de terceros.**
David no es admin de ninguna de las dos. Si alguna organización tiene restringido el acceso de apps externas a Calendar, la autorización de esa cuenta fallará. Se sabrá en 5 minutos durante la Fase 0. Si pasa, la salida es pedir al admin de Ruca que agregue el Client ID a la lista de apps confiables — una excepción pequeña y auditable, no un permiso amplio. Si la organización se niega, esa cuenta pasa a modo solo-lectura vía su URL privada de iCal: los eventos de esa cuenta bloquearían en las otras, pero no al contrario. Degradación parcial, no fracaso total.

**3. El Mac apagado o dormido detiene la sincronización.**
Es la contraparte de no depender de la nube. Al despertar, CalSync detecta el tiempo transcurrido y hace una resincronización completa antes de volver al ciclo normal — pero durante las horas de sueño del Mac no hay protección. Si esto resulta ser un problema real en la práctica, la Fase 4 podría mover el daemon a un contenedor mínimo por unos pocos dólares al mes. No se asume desde el principio.

**4. Eventos recurrentes.**
Se expanden en instancias individuales dentro de la ventana de 60 días (`singleEvents=true`). Cada instancia genera su propio espejo. Es más simple y más robusto que replicar reglas de recurrencia, a costa de más eventos espejo. Aceptable en este volumen.

**5. Zonas horarias y horario de verano.**
Toda hora se maneja internamente en UTC y se presenta en America/Bogota. Los eventos creados por viajes o por contrapartes en otras zonas conservan su zona de origen. Colombia no tiene DST, lo que elimina la fuente más común de bugs, pero las contrapartes en EE.UU. y Europa sí — el motor nunca asume offsets fijos.

**6. Un bug escribe basura en un calendario de trabajo.**
Es el riesgo con peor consecuencia reputacional. Tres capas de defensa: la Fase 0 corre en modo simulación sin escribir nada; el motor solo puede modificar o borrar eventos que llevan la marca `calsync_owned`, y esa verificación ocurre antes de cada escritura; y el botón de limpieza total revierte todo en un clic. Un evento creado por un humano es técnicamente inalcanzable para el motor.

---

## 8. Fuera de alcance

- Sincronizar invitados, adjuntos, links de videollamada o descripciones a los espejos.
- Aceptar o rechazar invitaciones automáticamente.
- Mover reuniones que involucran a otras personas.
- Páginas públicas de agendamiento (tipo Calendly).
- Proveedores no-Google (Outlook, iCloud) — la arquitectura los admite después, pero no se construyen ahora.
- Uso por más de una persona.

---

## 9. Lo que se necesita para arrancar la Fase 0

1. Las tres direcciones de correo.
2. Rango de almuerzo preferido y días (aunque se puede cambiar luego en el dashboard).
3. Un momento de David frente al Mac para autorizar las tres cuentas en el navegador — el único paso que no se puede automatizar.
