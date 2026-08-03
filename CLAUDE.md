# 🏛️ SivIA — Documentación de Arquitectura de Hardware y Software

> Este documento describe el estado **verificado** del sistema. Lo que aún no
> existe está marcado explícitamente como pendiente en la sección 8. No añadir
> aquí componentes planificados como si estuvieran operativos.
>
> Última verificación: 2026-08-02.
>
> **Mapa de la documentación:**
> - `CLAUDE.md` (este archivo) — arquitectura, decisiones técnicas y pendientes.
> - [`FUNCIONAMIENTO.md`](FUNCIONAMIENTO.md) — cómo funciona: el recorrido de un
>   dato, capas de validación, manejo de errores.
> - [`README.md`](README.md) — instalación, variables de entorno y operación.
> - [`ANDROID_APP.md`](ANDROID_APP.md) — especificación del cliente móvil para
>   quien lo construya: API por Tailscale, flujo de confirmación y voz.

## 1. Visión del Proyecto
**SivIA** (*Sistema de Inventario por Voz con Inteligencia Artificial*) es una solución Edge-AI orientada al control de stock e inventarios para negocios de alimentos y suministros. El sistema captura comandos de voz natural, extrae entidades estandarizadas (Producto, Cantidad, Tipo de Movimiento) mediante un modelo LLM local y persiste las transacciones en una base de datos relacional. Todo el procesamiento de IA es local: ningún dato del negocio sale del dispositivo.

Desde la versión 1.2 hay **dos formas de usarlo**, y conviene no confundirlas:

- **`POST /api/procesar-comando`** — un disparo: una frase, una extracción, un
  movimiento registrado. Sin estado y sin confirmación. Es el camino para
  integraciones y scripts.
- **`POST /api/conversar`** — conversación con estado (§3.7). SivIA charla,
  consulta, y **propone** las escrituras para que una persona las confirme. Es
  el camino de la app móvil, y el único que puede dar de alta productos
  hablando.

Ninguno sustituye al otro. El primero es más rápido (~6,5 s frente a 15–30 s) y
no necesita una persona al otro lado; el segundo es el que un humano quiere usar.

---

## 2. Infraestructura de Hardware (Edge Server)
- **Host:** Raspberry Pi 5 (8 GB RAM / CPU Broadcom BCM2712 ARMv8.2-A quad-core @ 2.4 GHz).
- **Almacenamiento:** SSD NVMe de 128 GB sobre HAT PCIe.
- **Sistema Operativo:** Raspberry Pi OS Lite (Debian 13 Trixie arm64, headless).
- **Directorio Raíz del Proyecto:** `/home/admin/var/www/SivIA/`
- **Usuario de Ejecución:** `admin` (con `sudo` sin contraseña).

### 2.1 Acceso remoto (Tailscale)
- **IP en el tailnet:** `100.110.69.36` · **host:** `srvr-ia-sql`.
- Es la vía por la que la app móvil llega al backend: `http://100.110.69.36:8000`.
- **El servidor no está publicado en internet.** El túnel WireGuard de Tailscale
  ya cifra y autentica extremo a extremo, y por eso la API puede servir HTTP en
  claro sin que sea un agujero (ver el pendiente de HTTPS en la §9).
- Consecuencia para el cliente: sin Tailscale conectado en el móvil **no hay
  ninguna ruta** hasta el servidor, y el fallo se manifiesta como un error de
  red genérico. La app debe distinguirlo — ver `ANDROID_APP.md` §9.

---

## 3. Arquitectura del Software y Stack Tecnológico

### 3.1 Motor de IA (Edge LLM — Nativo) ✅ Operativo
- **Plataforma:** Ollama nativo como servicio de `systemd` en el host (no en Docker), para aprovechar las instrucciones de aceleración CPU ARM.
- **Modelo:** `qwen2.5:3b` cuantizado (1.9 GB).
- **Latencia medida** (Pi 5, 2026-07-27, extremo a extremo incluyendo la escritura en MySQL):
  - **~6,5 s** por comando con el modelo ya cargado en RAM.
  - **~21,5 s** en la primera petición tras arrancar u ociosa (carga del modelo).
  La documentación previa afirmaba «<2 s»; no se corresponde con la medición.
  `OLLAMA_TIMEOUT` está en 60 s por ese margen. Si se necesita bajar la latencia,
  las vías son `OLLAMA_KEEP_ALIVE` para evitar la descarga del modelo, o un
  modelo más pequeño (`qwen2.5:1.5b`).
- **Latencia del chat conversacional** (medida 2026-08-02): **15–30 s** por turno,
  bastante más que la extracción. No es que el modelo sea más lento: genera
  muchos más tokens, porque además del JSON de control redacta la respuesta en
  lenguaje natural. Por eso el chat tiene su propio timeout
  (`OLLAMA_TIMEOUT_CHAT`, 180 s) en vez de compartir los 60 s de la extracción.
- **Modelo del chat separado por configuración** (`MODEL_CHAT`), pero por defecto
  **el mismo** que el de extracción, y conviene que siga así: apuntarlos a
  modelos distintos deja los dos residentes en RAM. Medido, 3b (2,4 GB) + 7b
  (5,1 GB) dejaron el Pi en **194 MB libres**, al borde del OOM killer.
- **Sobre usar un modelo mayor — probado y descartado (2026-08-02).** Con
  qwen2.5:7b los mismos prompts tardaron 34–42 s por turno frente a 14–18 s del
  3b (71 s en la primera petición, con el modelo cargándose). La mejora de
  redacción no compensa multiplicar por 2,5 la espera en un chat, y la memoria
  no da para tener los dos. El modelo se desinstaló.
- **Binding de Red:** `0.0.0.0:11434`, configurado mediante
  `/etc/systemd/system/ollama.service.d/override.conf`. Accesible desde los
  contenedores por `172.17.0.1:11434` y por `host.docker.internal`.
- **Contrato de salida:** la llamada a `/api/generate` usa `format: "json"` y
  `temperature: 0`. El prompt de sistema viaja en el campo `system`, separado del
  texto del usuario (`prompt`), lo que reduce la superficie de inyección de prompt.
- **Saneamiento:** `extraer_json()` en `backend/main.py` sigue existiendo como red
  de seguridad ante envoltorios de markdown, por si se cambia de modelo o se
  desactiva `format: json`.

### 3.2 Backend & API (Contenedor Docker) ✅ Operativo
- **Framework:** FastAPI (Python 3.11-slim, asíncrono). Servidor Uvicorn en el puerto `8000`.
- **ORM:** SQLAlchemy 2.0 + PyMySQL con `cryptography`, `pool_pre_ping` y `pool_recycle=1800`.
- **Imagen:** build multi-etapa; `build-essential` se queda en el builder.
  Ejecuta como `appuser` (uid 1001), sin privilegios. Healthcheck con la stdlib
  de Python, sin `curl` instalado.
- **Concurrencia:** PyMySQL es sincrónico; las escrituras se delegan al
  threadpool con `run_in_threadpool` para no bloquear el event loop mientras la
  petición al LLM está en vuelo.
- **Fail-fast:** si falta `DATABASE_URL` o `SIVIA_API_KEY`, el proceso **aborta**
  el arranque. Antes había un fallback a SQLite que hacía que la app pareciera
  arrancar bien mientras escribía a un archivo efímero dentro del contenedor.
- **Esquema:** gestionado por **Alembic** (ver §3.4). El evento `startup` con 10
  reintentos ya **no crea tablas**: solo espera a que MySQL acepte conexiones y
  registra en el log la revisión aplicada. No devolver `create_all()` aquí — ver
  la decisión 6 de la §7.

### 3.3 Base de Datos (Contenedor Docker) ✅ Operativo
- **Motor:** MySQL 8.0 (arm64).
- **Persistencia:** volumen sobre el SSD NVMe: `./database/data -> /var/lib/mysql`.
- **Red:** **no** publica el puerto 3306 al host. Solo accesible por la red interna de Docker (`db:3306`).
- **Estructura de Datos (`movimientos_inventario`):**
  - `id` (INT, PK, auto-increment)
  - `producto` (VARCHAR 100, NOT NULL, indexado) — **snapshot histórico**: el nombre
    canónico tal como estaba al registrarse. Denormalizado a propósito, para que
    renombrar en el catálogo no reescriba el pasado y para no exigir un JOIN en
    las consultas de stock e histórico.
  - `producto_id` (INT, NOT NULL, indexado, FK → `productos.id` `ON DELETE RESTRICT`)
    — la verdad referencial.
  - `cantidad` (INT, NOT NULL, `CHECK (cantidad > 0)`)
  - `tipo_movimiento` (VARCHAR 20, NOT NULL, `CHECK IN ('INGRESO','EGRESO')`)
  - `fecha` (DATETIME, NOT NULL, indexado, UTC naive — MySQL DATETIME no guarda zona horaria)
  - `usuario_id` (INT, **NULLABLE**, indexado, FK → `usuarios.id` `ON DELETE RESTRICT`)
    — autor. Nullable a propósito y de forma permanente: los movimientos
    anteriores a que existieran las cuentas no tienen autor, y la API key de
    máquina no es una persona. Un NULL aquí significa «registrado sin sesión de
    usuario», que es información cierta; rellenarlo con un usuario «sistema»
    falsearía la auditoría.
- **`productos`:** `id`, `nombre` (VARCHAR 100, NOT NULL, **UNIQUE**),
  `activo` (BOOL, NOT NULL, default 1), `creado_en` (DATETIME).
- **`alias_producto`:** `id`, `alias` (VARCHAR 100, NOT NULL, **UNIQUE global**),
  `producto_id` (FK → `productos.id` `ON DELETE CASCADE`), `creado_en`.
- **`usuarios`:** `id`, `usuario` (VARCHAR 50, **UNIQUE**), `nombre`,
  `hash_clave` (scrypt con sal por usuario), `rol` (`CHECK IN ('admin','operador')`),
  `activo`, `creado_en`, `ultimo_acceso`.
- **`sesiones`:** `id`, `token_hash` (SHA-256, **UNIQUE**), `usuario_id`
  (FK CASCADE), `creada_en`, `expira_en` (indexado), `revocada`.
- **`conversaciones`:** `id`, `usuario_id` (FK **RESTRICT**), `titulo`,
  `creada_en`, `actualizada_en` (indexado).
- **`mensajes`:** `id`, `conversacion_id` (FK CASCADE), `rol`
  (`CHECK IN ('usuario','asistente')`), `contenido` (TEXT), `creado_en`.
- **`acciones_pendientes`:** `id`, `conversacion_id` (FK CASCADE), `usuario_id`
  (FK RESTRICT), `tipo`, `parametros` (TEXT con JSON), `resumen`, `estado`
  (`CHECK IN ('pendiente','confirmada','rechazada','caducada')`), `creada_en`,
  `expira_en`, `resuelta_en`.
- **Verificado en el motor** (2026-07-28, por SQL directo contra MySQL): un
  `producto_id` inexistente da error 1452, borrar un producto con movimientos da
  1451, y un alias repetido da 1062. La integridad no depende de que la
  aplicación recuerde validar.

### 3.4 Migraciones de Esquema (Alembic) ✅ Operativo
- **Modelo aislado:** el esquema vive en `backend/models.py`, no en `main.py`, para
  que Alembic pueda importarlo sin la configuración de la aplicación (`main.py`
  aborta si faltan `DATABASE_URL` o `SIVIA_API_KEY`, lo que impediría generar
  migraciones).
- **Credenciales:** `alembic.ini` **no** contiene la URL de conexión; `env.py` la
  toma de `DATABASE_URL` para que los secretos vivan solo en `.env`.
- **Ejecución:** `scripts/migrar.sh` lanza Alembic en un contenedor **efímero**
  (`docker compose run --rm`), destruido al terminar. Sin proceso residente ni
  consumo de memoria permanente, y sin instalar nada en el host.
- **Detalle de implementación:** el contenedor se ejecuta con `--user $(id -u):$(id -g)`
  porque la imagen corre como `appuser` (uid 1001) y no podría escribir las
  revisiones autogeneradas en el directorio del host, propiedad de `admin` (uid 1000).
- **Revisión base:** `b7e4c1a09f52` (esquema inicial). Se aplicó a la base
  preexistente con `stamp`, no con `upgrade`, porque la tabla ya existía;
  `migrar.sh upgrade` detecta ese caso automáticamente.
- **Verificado:** `revision --autogenerate` sobre el esquema vivo produce un
  `upgrade()` vacío, lo que confirma que la revisión base es fiel al esquema real.
  El ciclo `upgrade` → `downgrade` se probó extremo a extremo sin pérdida de datos.

### 3.5 Catálogo de Productos ✅ Operativo
Cierra el pendiente más grave que quedaba: `producto` era texto libre, así que
«AREPA» y «AREPAS» eran productos distintos con saldos separados y el stock se
fragmentaba en silencio.

Tres piezas, en este orden:

1. **Normalización** (`normalizar_nombre()` en `models.py`): guiones bajos y
   guiones a espacios, espacios colapsados, MAYÚSCULAS. Vive en `models.py` y no
   en `main.py` porque las migraciones de datos deben aplicar exactamente la
   misma normalización que la aplicación; si divergieran, el backfill dejaría
   filas que la API ya no sabría resolver. **No corrige ortografía**, a propósito:
   inventar la palabra correcta es justo lo que no queremos que haga nadie.
2. **Alias** (`alias_producto`): es lo que hace usable un catálogo cerrado en un
   sistema por voz. Sin alias, decir «una arepa» falla si el canónico es «AREPAS»
   y la persona tendría que adivinar la forma exacta registrada. El alias es
   **único global**, no por producto: si apuntara a dos, la resolución elegiría
   en silencio.
3. **Resolución** (`resolver_producto()`): compara por clave insensible a
   acentos (`clave_busqueda()`, para que «MAIZ» y «MAÍZ» resuelvan igual sin un
   alias por cada tilde), primero contra los canónicos y después contra los
   alias — en ese orden, para que un alias no pueda secuestrar el nombre real de
   otro producto.

- **El catálogo se inyecta en el prompt de sistema.** Es lo que más reduce los
  rechazos: el modelo elige de un conjunto cerrado en vez de transcribir libre.
  Por encima de `MAX_PRODUCTOS_EN_PROMPT` (200) se omite y se registra en el log,
  porque la lista alargaría la inferencia sin mejorarla; la resolución sigue
  aplicándose igual.
- **El catálogo se lee en cada petición**, sin caché: un producto recién dado de
  alta resuelve inmediatamente. Cuesta una consulta de decenas de filas al lado
  de ~6,5 s de inferencia.
- **Latencia sin cambios** (medido 2026-07-28): 6,0–6,9 s con el modelo
  residente, igual que antes del catálogo.
- **Efecto verificado:** «Llegaron 20 panes de yuca», el comando que el
  2026-07-27 guardó `PANES_DE_YACA`, hoy registra `PANES DE YUCA`.

### 3.6 Multiusuario, Roles y Auditoría ✅ Operativo
Cierra el pendiente de «una sola API key compartida»: cualquiera que la tuviera
podía hacer cualquier cosa, y los movimientos no registraban quién los había
creado, así que un registro erróneo no tenía autor a quien preguntar.

- **Dos roles, no más.** `operador` conversa, consulta y registra; `admin` puede
  además eliminar movimientos, retirar productos y gestionar cuentas. La
  pregunta que el negocio hizo fue «quién puede borrar», no «qué matriz de
  permisos queremos». Un tercer rol se añade el día que haya una tercera
  respuesta a esa pregunta.
- **Contraseñas con `hashlib.scrypt`** (biblioteca estándar, cero dependencias
  nuevas), sal por usuario, parámetros almacenados junto al hash para poder
  endurecerlos sin invalidar lo ya guardado. Se eligió frente a SHA-256 porque
  es caro en CPU **y en memoria**: con el volcado de la tabla, un atacante no
  puede probar millones de contraseñas por segundo con una GPU.
- **Sesiones con token opaco en base de datos**, no JWT. El motivo es concreto:
  **poder revocar**. Un JWT firmado vale hasta que expira aunque se despida a
  alguien o se pierda el móvil; una fila se borra. Con tres usuarios el coste de
  la consulta por petición es irrelevante. Se guarda el SHA-256 del token, nunca
  el token.
- **La revocación es inmediata.** Desactivar una cuenta o cambiar una contraseña
  invalida sus sesiones abiertas en el acto, sin esperar a que caduquen (12 h).
- **La API key sobrevive**, como credencial de *máquina* equivalente a un
  administrador. Quitarla habría roto los scripts de operación y obligado a
  guardar la contraseña de una persona en un cron, que es peor. Lo que registra
  queda **sin autor**, y eso es información cierta.
- **No hay usuarios sembrados en ninguna migración**, a propósito: sembrarlos
  habría exigido escribir una contraseña en un archivo versionado, que la §6
  prohíbe sin excepciones. El primer administrador se crea con la API key
  (`POST /api/usuarios`), que ya vive solo en `.env`.
- **No se puede dejar el sistema sin administradores activos.** La comprobación
  mira el estado *resultante*, así que degradar y desactivar en la misma
  petición se rechaza igual que hacerlo por separado.

### 3.7 Motor Conversacional ✅ Operativo
El chat tipo ChatGPT: SivIA conversa, responde con datos reales y **propone**
las escrituras en vez de ejecutarlas.

**Dos reglas gobiernan todo el diseño, y ninguna es negociable:**

1. **El modelo interpreta; nunca ejecuta.** Toda escritura pasa por una
   `AccionPendiente` que una persona confirma. Es lo que permite que el chat dé
   de alta productos —antes prohibido automáticamente por la decisión 6— sin
   reabrir el agujero que cerró el catálogo: quien autoriza el alta es un humano
   que está viendo el nombre exacto que se va a crear. Ver la decisión 9.
2. **El modelo no redacta datos.** Las cifras de stock, los listados y los
   identificadores los compone Python leyendo la base; la `respuesta` del LLM
   solo se usa en la charla, donde no hay ningún dato que falsear. Un 3B que
   redacte «tienes 45 arepas» acierta a veces y a veces se lo inventa, y ese
   error es indistinguible del acierto para quien lo lee.

- **Nueve intenciones:** `charla`, `consultar_stock`, `consultar_movimientos`,
  `listar_productos`, `registrar_movimiento`, `crear_producto`, `agregar_alias`,
  `retirar_producto`, `eliminar_movimiento`. Una intención que el modelo se
  invente cae en `charla`, que es el modo de fallo seguro: se responde con
  palabras y no se toca nada.
- **Confirmar por botón o hablando.** `POST /api/conversar/confirmar` y un «sí»
  escrito en el chat hacen lo mismo. Existen los dos porque por voz nadie pulsa
  un botón y con el móvil en la mano nadie quiere dictar. El camino del «sí»
  **no llama al modelo**: resuelve al instante y evita que un «dale» se
  reinterprete como una intención nueva.
- **Una sola propuesta viva por hilo.** Con dos, un «sí» sería ambiguo. Al
  proponer una nueva, la anterior pasa a `caducada`.
- **Las propuestas caducan a los 30 min.** Confirmar mañana un «vendimos 20
  arepas» de hoy lo registraría con la fecha de mañana.
- **Al confirmar no se reinterpreta el texto**: se ejecutan los parámetros
  guardados. Reinterpretar permitiría ejecutar algo distinto de lo que se
  aprobó, que es justo lo que la confirmación existe para impedir. Las
  comprobaciones sí se repiten, porque entre la propuesta y el sí pasó tiempo
  real y el catálogo pudo cambiar.
- **Un rechazo de negocio no es un error HTTP.** «Ese producto no está en el
  catálogo» llega como frase y se guarda en el historial como cualquier turno.
  Devolver un 422 obligaría a la app a traducir códigos a lenguaje natural.
- **El historial vive en el servidor**, para que la conversación se vea desde
  cualquier dispositivo y sobreviva a reinstalar la app. Cada persona ve solo
  sus hilos, también los administradores: ser admin da permiso para borrar
  movimientos, no para escribir dentro de la conversación de otra persona.

#### La forma del prompt se midió, no se supuso
Es la parte menos obvia de esta sección y la que más tiempo costó. La primera
versión pasaba los tests unitarios y se comportaba mal en cuanto había
conversación de por medio: el modelo dejaba `cantidad` a null en «vendimos 5
empanadas», convertía «crea el producto chicharrón con arepa» en
`producto='AREPAS'` y arrastraba el producto del turno anterior. **Ningún doble
de test puede ver eso**, porque el defecto está en cómo entiende el modelo.

`scripts/evaluar_prompt.py` mide variantes contra el modelo real sobre catorce
casos. El recorrido fue 10/14 → 12/14, y los dos restantes se resolvieron en
código. Tres cambios explican la mejora:

1. **El mensaje actual va al final y en su propia sección**, separado del
   historial, que se etiqueta explícitamente como contexto que *no* es la
   petición. Antes iba todo como transcripción corrida terminada en `SivIA:`, y
   el modelo se ponía a conversar en vez de a extraer.
2. **Cómo rellenar el JSON se explica en la cola del prompt, no en el `system`.**
   Fue el cambio que más pesó. Un 3B atiende mucho más a lo último que ha leído;
   con historial de por medio, unas reglas de formato escritas al principio del
   `system` quedan sepultadas. En el `system` se queda *qué es* cada intención
   (estable); en la cola, *cómo* rellenar (mecánico, y lo que primero se
   olvidaba).
3. **El catálogo se enuncia como «productos que ya existen»**, no como «los
   productos que maneja el negocio». Con la segunda redacción el modelo lo
   trataba como un conjunto cerrado del que había que elegir sí o sí.

Lo que **no** se arregló con prompt, sino con código: el arrastre de contexto.
`_mencionado_en()` descarta el filtro de una consulta si el nombre del producto
no aparece en lo que la persona acaba de escribir. Sin él, tras preguntar «cuántas
arepas tengo», la pregunta «cómo va el stock ahora?» devolvía solo las arepas
**como si fuera el inventario entero** — y quien lo lee concluye que no tiene
nada más. Un listado de más es un incordio; un listado recortado en silencio es
una respuesta falsa.

### 3.8 Tests Automatizados ✅ Operativo
- **247 tests**, suite completa en **~8 s**. Se ejecutan con `scripts/probar.sh`.
- **Aislamiento total:** SQLite en memoria (una base limpia por test) y un doble
  de `consultar_ollama`. El contenedor corre con `--network none`: un test que
  necesite red es un test que no está aislado.
- **Etapa `tests` del Dockerfile:** `pytest` vive en `requirements-dev.txt` y solo
  se instala en esa etapa. El servicio `api` del compose fija `target: runtime`,
  así que la imagen de producción **no** contiene pytest (verificado: 385 MB y
  `ModuleNotFoundError: No module named 'pytest'`).
- **Lo que cubren:** `validar_entidades` (todos los caminos de rechazo),
  `extraer_json` (envoltorios de markdown), la normalización y la resolución
  contra el catálogo (incluido el caso `PANES_DE_YACA`), los endpoints del
  catálogo, y sobre todo que una extracción defectuosa o un producto desconocido
  devuelvan 422 **sin escribir nada**.
- **El catálogo se siembra en cada base de test** (`CATALOGO_DE_PRUEBA` en
  `conftest.py`), porque `POST /api/procesar-comando` responde 409 con el catálogo
  vacío. Sembrar productos no altera lo que devuelven `/api/stock` ni
  `/api/movimientos` — ambos se derivan de los movimientos —, así que los 79 tests
  anteriores siguen midiendo exactamente lo mismo.
- **Los tests no escriben ids a mano:** la fixture `id_de` los resuelve por
  nombre. Un id literal dependería del orden de inserción de
  `CATALOGO_DE_PRUEBA`, y añadir un producto al principio rompería tests que no
  tienen nada que ver.
- **Del multiusuario y el chat** se cubren las propiedades, no los caminos
  felices: que un operador **no pueda** eliminar y que el 403 signifique que no
  pasó nada; que desactivar una cuenta corte su sesión en el acto; que no se
  pueda dejar el sistema sin administradores; y sobre todo que **ninguna
  escritura ocurra sin confirmación** — casi todos los tests de escritura
  comprueban dos cosas, la respuesta y que la base no cambió.
- **Lo que NO cubren:** la calidad de extracción del LLM (no es nuestro código) ni
  el comportamiento específico de MySQL frente a SQLite. Para eso haría falta una
  suite de integración, aún pendiente.

  Sobre lo primero hay un matiz que costó caro aprender: «no es nuestro código»
  no significa «no es nuestro problema». El prompt **sí** es nuestro, y sus
  defectos son invisibles con un doble. Para eso está `scripts/evaluar_prompt.py`
  (§3.7), que no forma parte de `probar.sh` a propósito: necesita el modelo real,
  tarda minutos y su resultado depende de la versión de Qwen instalada. Un test
  así en la suite sería un test que falla por motivos ajenos al código.

---

## 4. Estructura de Archivos

```
/home/admin/var/www/SivIA/
├── docker-compose.yml
├── .env                  # SECRETO — excluido por .gitignore
├── .env.example          # plantilla pública de configuración
├── .gitignore
├── README.md
├── CLAUDE.md
├── FUNCIONAMIENTO.md
├── ANDROID_APP.md        # especificación del cliente móvil
├── scripts/
│   ├── probar.sh         # suite de tests
│   ├── migrar.sh         # gestión de migraciones (Alembic)
│   ├── evaluar_prompt.py # banco de pruebas del prompt contra el modelo real
│   └── aplicar_rotacion.sh
├── backend/
│   ├── Dockerfile        # multi-etapa, usuario no-root
│   ├── .dockerignore
│   ├── requirements.txt
│   ├── requirements-dev.txt  # solo testing, fuera de la imagen de producción
│   ├── pytest.ini
│   ├── tests/            # 247 tests, sin dependencias externas
│   │   ├── conftest.py           # aislamiento de MySQL y Ollama + catálogo y usuarios
│   │   ├── test_endpoints.py
│   │   ├── test_validacion.py
│   │   ├── test_extraccion_json.py
│   │   ├── test_catalogo.py      # normalización, alias, resolución, endpoints
│   │   ├── test_autenticacion.py # login, sesiones, roles, gestión de cuentas
│   │   └── test_conversacion.py  # chat, propuestas, confirmación, forma del prompt
│   ├── alembic.ini
│   ├── migrations/       # revisiones de esquema versionadas
│   │   ├── env.py
│   │   └── versions/
│   ├── models.py         # esquema + normalización (aislado de la config de la app)
│   └── main.py
└── database/
    └── data/             # volumen MySQL — excluido por .gitignore
```

---

## 5. API

| Método | Ruta | Auth | Descripción |
|---|---|:--:|---|
| `GET` | `/` | — | Identificación y estado del servicio. |
| `GET` | `/health` | — | Estado de MySQL y Ollama (`healthy` / `degraded`). |
| `POST` | `/api/auth/login` | — | Usuario y contraseña por un token de sesión. |
| `POST` | `/api/auth/logout` | — | Revoca la sesión actual. Idempotente. |
| `GET` | `/api/auth/yo` | 🔒 | Identidad y rol de quien pregunta. |
| `POST` | `/api/conversar` | 👤 | Un turno de conversación. |
| `POST` | `/api/conversar/confirmar` | 👤 | Ejecuta o descarta una propuesta. |
| `GET` | `/api/conversaciones` | 👤 | Hilos propios, por actividad reciente. |
| `GET` | `/api/conversaciones/{id}` | 👤 | Un hilo con todos sus mensajes. |
| `DELETE` | `/api/conversaciones/{id}` | 👤 | Borra un hilo (no toca el inventario). |
| `POST` | `/api/procesar-comando` | 🔒 | Procesa el comando de voz y registra el movimiento. |
| `GET` | `/api/movimientos` | — | Histórico paginado (`limit`, `offset`, `producto`). |
| `GET` | `/api/stock` | — | Stock por producto: `SUM(INGRESO) - SUM(EGRESO)`. |
| `DELETE` | `/api/movimientos/{id}` | 👑 | Elimina un movimiento mal registrado. |
| `GET` | `/api/productos` | — | Catálogo con sus alias (`incluir_inactivos`). |
| `POST` | `/api/productos` | 🔒 | Da de alta un producto, con alias opcionales. |
| `POST` | `/api/productos/{id}/alias` | 🔒 | Enseña una variante tras un 422. |
| `PATCH` | `/api/productos/{id}?activo=` | 👑 | Retira o reincorpora un producto. |
| `DELETE` | `/api/productos/{id}` | 👑 | Retira del catálogo (**baja lógica**). |
| `GET` | `/api/usuarios` | 👑 | Cuentas del sistema. |
| `POST` | `/api/usuarios` | 👑 | Da de alta una cuenta. |
| `PATCH` | `/api/usuarios/{id}?rol=&activo=` | 👑 | Cambia rol o estado. |
| `POST` | `/api/usuarios/{id}/clave` | 🔒 | Cambia una contraseña. |

- 🔒 = cualquier identidad válida: `Authorization: Bearer <token>` **o** el
  header `X-API-Key` (comparado con `secrets.compare_digest`).
- 👑 = además, rol `admin`. La API key cuenta como administrador.
- 👤 = **exige una persona**: solo Bearer, la API key no sirve. Una conversación
  pertenece a alguien, y el historial de «la API key» no sería el de nadie.

El catálogo es **lectura pública**, como el stock y el histórico: saber qué
nombres acepta el sistema no es información sensible, y un cliente necesita la
lista sin tener la clave de escritura. La lista de **usuarios sí** es sensible —
quién tiene acceso al sistema — y por eso pide administrador.

CORS habilitado vía `CORS_ORIGINS`. Con origen comodín se desactivan las
credenciales, como exige la especificación CORS.

---

## 6. Control de Versiones y Repositorio

- **Remoto:** https://github.com/lDalexander/SivIA — **privado**, rama `main`.
- **Autenticación:** `gh` CLI con credential helper (`gh auth setup-git`), protocolo
  HTTPS. El token vive en `~/.config/gh/hosts.yml`, fuera del repositorio.
- **Higiene de secretos — regla que no se negocia:**
  - `.env` y `database/data/` están en `.gitignore`. Verificar con
    `git check-ignore -v .env database/data/` antes de cualquier primer commit.
  - **Nunca** escribir una credencial en un archivo versionado, ni siquiera una ya
    rotada e inválida: revela el patrón de nomenclatura. Ocurrió con
    `scripts/aplicar_rotacion.sh`, que llevaba embebida la contraseña antigua; se
    reescribieron los commits afectados antes de publicar y ahora la recibe por la
    variable de entorno `PASS_ROOT_ANTERIOR`.
  - Auditoría antes de publicar:
    ```bash
    git grep -l -E 'patron|de|secretos' $(git rev-list --all)
    ```
- **`scripts/aplicar_rotacion.sh`:** rota las contraseñas de MySQL con `ALTER USER`.
  Existe porque las variables del compose solo se aplican en la **primera**
  inicialización del volumen: cambiar `.env` no las propaga. Es idempotente.

---

## 7. Decisiones de diseño que conviene no revertir

1. **Sin defaults silenciosos en la extracción.** Si el LLM no determina producto,
   cantidad o tipo con certeza, la API responde **422 y no escribe nada**. La
   versión anterior sustituía los huecos por `cantidad=0` y
   `tipo_movimiento="INGRESO"`: un EGRESO mal interpretado se convertía en un
   INGRESO fantasma que descuadraba el inventario sin dejar rastro.
2. **`except HTTPException: raise` antes del `except Exception`.** Sin eso, el
   manejador genérico capturaba los propios `HTTPException` del código y los
   reconvertía en 500, dejando el manejo del 502 de Ollama como código muerto.
3. **Nunca devolver `str(e)` ni la respuesta cruda del LLM al cliente.** Filtra
   rutas, drivers y potencialmente el prompt completo. Van al log.
4. **`DELETE /api/movimientos/{id}` es un requisito de producto, no un extra.** En
   un sistema por voz la transcripción falla; sin este endpoint la única forma de
   corregir un error es entrar a MySQL a mano.
5. **`GET /api/stock` es lo que da sentido al producto.** El histórico de
   movimientos no responde «¿cuántas arepas tengo?».
6. **Un producto desconocido se rechaza con sugerencias, no se da de alta solo.**
   Fue la decisión central del catálogo. El alta automática habría arreglado
   `PANES_DE_YACA` sin intervención, pero reintroduciendo el problema entero: se
   habría convertido en un producto legítimo con su propio saldo. Es la misma
   regla que la decisión 1 — si hay duda, no se escribe — y no es un callejón sin
   salida: la respuesta dice qué hacer y `POST /api/productos/{id}/alias` deja la
   variante aprendida para siempre. El umbral (`UMBRAL_SUGERENCIA = 0.7`) solo
   afecta al **texto del mensaje**; nunca registra nada por su cuenta.
7. **Retirar un producto es baja lógica (`activo = False`), nunca `DELETE`.** Los
   movimientos históricos lo referencian con una FK `RESTRICT`: borrar la fila
   falla, y con otra configuración de la FK se llevaría el histórico por delante
   y falsearía el inventario. Un producto retirado deja de aceptar comandos y
   sigue sumando lo ya registrado. Por eso existe también
   `PATCH ?activo=true`: un catálogo del que se puede dar de baja pero no volver
   a dar de alta estaría roto, y `POST` no puede recrearlo porque el nombre sigue
   ocupado por la fila inactiva.
8. **No reintroducir `create_all()` en el arranque.** Con Alembic gestionando el
   esquema, `create_all()` volvería a ser una fuente de divergencia silenciosa:
   crea las tablas ausentes pero nunca altera las existentes, así que un modelo
   modificado y una migración sin aplicar convivirían sin dar ningún error. El
   arranque solo verifica la conexión y registra la revisión vigente.
9. **En el chat, el modelo propone y una persona confirma.** Es la extensión
   natural de la decisión 6, no su excepción. La decisión 6 prohíbe que un
   producto desconocido se dé de alta **solo**; lo que el chat añade es un
   humano que autoriza viendo el nombre exacto. Sin esa confirmación, el chat
   sería exactamente el alta automática que la decisión 6 rechazó, con la
   agravante de que un modelo conversacional se equivoca más que un extractor de
   una frase. Consecuencias prácticas que conviene no deshacer: nada se escribe
   en `POST /api/conversar`, y al confirmar **no se reinterpreta el texto**, se
   ejecutan los parámetros que se le enseñaron a la persona.
10. **Los datos de una consulta los redacta Python, nunca el modelo.** Un 3B que
   escriba «tienes 45 arepas» acierta a veces y a veces se lo inventa, y para
   quien lo lee el error es indistinguible del acierto. El chat comparte
   `_consultar_stock()` con `GET /api/stock` en lugar de calcularlo por su
   cuenta: si divergieran, no habría forma de saber cuál de las dos cifras es la
   buena. La `respuesta` del LLM solo se usa en la charla.
11. **La API key no desaparece con la llegada de los usuarios.** Es la credencial
   de máquina. Quitarla habría roto los scripts de operación y obligado a
   guardar la contraseña de una persona en un cron. Lo que registra queda sin
   autor, y ese NULL es información cierta.
12. **Un rechazo de negocio en el chat es una frase, no un código HTTP.** «Ese
   producto no está en el catálogo» se responde hablando y se guarda en el
   historial. Devolver un 422 obligaría a la app a traducir códigos de error a
   lenguaje natural, y el mismo rechazo diría cosas distintas según quién lo
   pintara.

---

## 8. Comandos de operación

```bash
# --- Ciclo de vida de los servicios ---
docker compose up -d --build     # reconstruir y arrancar
docker compose ps                # ambos servicios deben quedar 'healthy'
docker compose logs -f api       # logs de la API
docker compose restart api       # reiniciar tras cambiar .env

# --- Diagnóstico ---
curl localhost:8000/health       # estado de MySQL y Ollama
systemctl status ollama          # estado del motor de IA
ollama list                      # modelos descargados

# --- Tests y esquema ---
bash scripts/probar.sh           # suite completa (~1,7 s)
bash scripts/migrar.sh estado    # revisión de esquema aplicada
bash scripts/migrar.sh upgrade   # aplicar migraciones pendientes

# --- Catálogo ---
curl localhost:8000/api/productos                      # qué nombres acepta el sistema
curl -X POST localhost:8000/api/productos \
  -H "Content-Type: application/json" -H "X-API-Key: $SIVIA_API_KEY" \
  -d '{"nombre":"PANES DE YUCA","alias":["PAN DE YUCA"]}'

# --- Usuarios ---
# El primer administrador se crea con la API key: no hay ninguno sembrado.
curl -X POST localhost:8000/api/usuarios \
  -H "Content-Type: application/json" -H "X-API-Key: $SIVIA_API_KEY" \
  -d '{"usuario":"john","nombre":"John García","clave":"...","rol":"admin"}'
curl localhost:8000/api/usuarios -H "X-API-Key: $SIVIA_API_KEY"

# --- Prompt del chat ---
# Contra el modelo real, tarda minutos. Ejecutar antes y después de tocar el
# prompt; no dar por buena una redacción que baje el marcador.
docker run --rm --network host -v "$PWD/backend:/app" -v "$PWD/scripts:/s" \
  -w /app -e DATABASE_URL=sqlite:// -e SIVIA_API_KEY=x \
  --entrypoint python sivia-api:latest /s/evaluar_prompt.py
```

> ⚠️ **`migrar.sh` corre sobre la imagen construida, no sobre un bind mount del
> código.** Solo `backend/migrations/` se monta desde el host. Tras cambiar
> `models.py` hay que `docker compose build api` **antes** de `migrar.sh nueva` o
> `upgrade`, o Alembic importará la versión antigua del modelo.

**Orden al cambiar el esquema:** editar `backend/models.py` →
`docker compose build api` → `migrar.sh nueva "desc"` → **revisar el archivo
generado** → `migrar.sh sql` para ver el SQL → `migrar.sh upgrade` → `probar.sh`.

**Si la migración toca datos** (siembra, backfill), añadir dos pasos: respaldar
con `mysqldump` antes de `upgrade`, y probar el ciclo `upgrade` → `downgrade` →
`upgrade` comprobando que el número de filas no cambia. La revisión del catálogo
llevaba un `downgrade()` roto que solo apareció así: MySQL rechaza borrar el
índice que respalda una clave ajena, y `DROP TABLE` ya se lleva sus índices, así
que enumerarlos era además innecesario. Un `downgrade()` que nunca se ejecuta es
un `downgrade()` que no existe.

---

## 9. Pendientes conocidos

- ⬜ **Tests de integración.** La suite actual aísla MySQL y Ollama. Falta un
  puñado de tests contra los servicios reales: que las `CHECK` de MySQL
  rechacen de verdad, y que el modelo extraiga bien un comando típico.
- ⬜ **App móvil / captura de voz.** El backend ya la soporta por completo y su
  especificación está escrita ([`ANDROID_APP.md`](ANDROID_APP.md)), pero la app
  no existe todavía. La transcripción de audio a texto se hace en el móvil con
  el `SpeechRecognizer` de Android: el servidor recibe ya el texto y nunca ve
  audio.
- ⬜ **El stock puede quedar negativo.** Nada impide registrar una salida mayor
  que las existencias, ni en el chat ni en la API. Se vio en las pruebas del
  2026-08-02 (un EGRESO de 3 sobre un producto recién creado dejó el saldo en
  −3). No es obvio qué debe hacer el sistema —rechazar la venta sería peor que
  registrarla, porque la mercancía salió de verdad y lo que falla es un ingreso
  no registrado—, así que la decisión está pendiente, no el arreglo.
- ⬜ **Unidades de medida.** Queda fuera del catálogo a propósito: es un eje
  ortogonal (cada producto necesitaría unidad base y factores de conversión) y
  habría duplicado el trabajo. Hoy la cantidad es un entero sin unidad, así que
  «llegaron 2 cajas de arepas» y «llegaron 2 arepas» son indistinguibles.
- ⬜ **Detección de divergencia entre `alembic_version` y el esquema real.**
  Encontrado el 2026-07-28 al probar el ciclo de la migración del catálogo: un
  `downgrade` que falla a mitad deja el esquema alterado pero la revisión sin
  cambiar, y el siguiente `migrar.sh upgrade` informa «head» sobre una base a la
  que le falta una columna. Es la misma clase de divergencia silenciosa que
  motivó la decisión 8, llegando por otra vía. Mientras no exista la
  comprobación: si un `downgrade` falla, **revisar el esquema a mano** antes de
  confiar en lo que diga `migrar.sh estado`.
- ⬜ **Corrección de un movimiento sin borrarlo.** Hoy un comando mal
  interpretado se arregla borrando y repitiendo. Con el catálogo hay un caso
  nuevo y frecuente: el movimiento es correcto salvo el producto, y reasignarlo
  a otro `producto_id` sería más directo que rehacerlo.
- ⬜ **Backups automatizados.** El comando de `mysqldump` está documentado en el
  README, pero no hay cron.
- ⬜ **Auditoría de las lecturas y de los cambios de cuenta.** Los movimientos ya
  registran autor (§3.6), pero quién consultó qué, y quién cambió el rol de
  quién, solo queda en los logs del contenedor, que rotan.
- ⬜ **Limpieza del historial de conversación.** Los hilos y sus mensajes crecen
  sin límite; nadie los purga. Con tres usuarios tardará mucho en importar, pero
  no hay nada que lo impida.
- ⬜ **Intentos de login sin límite.** No hay bloqueo por fuerza bruta. Hoy lo
  mitiga que scrypt es lento a propósito y que la API solo es accesible desde el
  tailnet, no desde internet.
- ⬜ **HTTPS.** La API sirve HTTP en claro. Dejó de ser urgente al pasar el
  acceso por Tailscale (§2.1), que ya cifra y autentica extremo a extremo con
  WireGuard: poner TLS encima añadiría un certificado que gestionar sin cambiar
  el modelo de amenaza. **Vuelve a ser bloqueante el día que se exponga el
  puerto fuera del tailnet**, y hasta entonces la regla es no exponerlo.
