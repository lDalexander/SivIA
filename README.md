# 🎙️ SivIA — Sistema de Inventario por Voz con Inteligencia Artificial

Solución **Edge-AI** de control de stock para negocios de alimentos y suministros. Captura un comando de voz en lenguaje natural, extrae las entidades de inventario (producto, cantidad, tipo de movimiento) con un LLM que corre **localmente** en una Raspberry Pi 5, y persiste la transacción en MySQL.

Todo el procesamiento de IA ocurre en el dispositivo: **ningún dato sale a la nube**.

```
Voz → transcripción → FastAPI → Qwen2.5:3b (Ollama local) → MySQL
```

Se usa de **dos maneras**, y conviene no mezclarlas:

- **Chat conversacional** (`POST /api/conversar`) — SivIA charla, responde con
  datos reales del inventario y **propone** cada escritura para que una persona
  la confirme. Es lo que usa la app móvil, y el único camino que permite dar de
  alta productos hablando. Tarda 15–30 s por turno.
- **Comando suelto** (`POST /api/procesar-comando`) — una frase, una extracción,
  un movimiento registrado. Sin estado ni confirmación, ~6,5 s. Para scripts e
  integraciones.

Además es **multiusuario**: cada persona entra con su cuenta, los movimientos
registran a su autor, y solo un administrador puede eliminar.

> 📖 **¿Quieres entender cómo funciona por dentro?** →
> [`FUNCIONAMIENTO.md`](FUNCIONAMIENTO.md) explica el recorrido completo de un
> comando, por qué cada pieza está donde está, y qué ocurre cuando algo falla.
> Este README cubre la instalación y la operación.
>
> 📱 **¿Vas a construir la app móvil?** →
> [`ANDROID_APP.md`](ANDROID_APP.md) tiene la especificación completa: conexión
> por Tailscale, flujo de confirmación, voz y los tropiezos de Android que hay
> que evitar.

---

## Stack

| Capa | Tecnología |
|---|---|
| Hardware | Raspberry Pi 5 (8 GB RAM, ARM Cortex-A76 @ 2.4 GHz) + SSD NVMe sobre HAT PCIe |
| SO | Raspberry Pi OS Lite (Debian 13 Trixie arm64, headless) |
| Motor de IA | Ollama nativo (systemd) + `qwen2.5:3b` cuantizado |
| API | FastAPI + Uvicorn (Python 3.11) en Docker |
| Persistencia | MySQL 8.0 en Docker, volumen sobre el SSD |
| ORM | SQLAlchemy 2.0 + PyMySQL |

Ollama corre **nativo en el host** (no en Docker) para aprovechar las instrucciones de aceleración de la CPU ARM. La API lo alcanza a través del bridge de Docker (`172.17.0.1:11434`).

---

## Requisitos previos

1. **Docker** y **Docker Compose**.
2. **Ollama** nativo con el modelo descargado y accesible desde Docker:

```bash
curl -fsSL https://ollama.com/install.sh | sh

# Binding en 0.0.0.0 para que los contenedores puedan alcanzarlo
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/override.conf <<'EOF'
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_KEEP_ALIVE=-1"
EOF
sudo systemctl daemon-reload && sudo systemctl restart ollama

ollama pull qwen2.5:3b
```

`OLLAMA_KEEP_ALIVE=-1` mantiene el modelo residente en RAM (1.9 GB de los 8 GB).
Sin él, la primera petición tras un rato de inactividad tarda **~21 s** en lugar
de **~6,5 s**, porque Ollama descarga el modelo al quedar ocioso.

### Latencia esperada

Medido en Raspberry Pi 5, extremo a extremo incluyendo la escritura en MySQL:

| Escenario | Latencia |
|---|---|
| `POST /api/procesar-comando`, modelo residente | **~6,5 s** |
| `POST /api/procesar-comando`, primera petición tras arrancar | ~21,5 s |
| **`POST /api/conversar`** (un turno de chat) | **15–30 s** |
| `POST /api/conversar/confirmar` y el resto de endpoints | milisegundos |

El chat tarda más porque genera muchos más tokens: además del JSON de control,
redacta la respuesta en lenguaje natural. No es que el modelo sea más lento. Por
eso tiene su propio `OLLAMA_TIMEOUT_CHAT` (180 s) en vez de compartir los 60 s de
la extracción.

Confirmar una propuesta **no pasa por la IA**, y un «sí» escrito en el chat
tampoco: ambos responden al instante.

> **Sobre usar un modelo más grande: probado y descartado** (2026-08-02). Con
> `qwen2.5:7b` los mismos prompts tardaron 34–42 s por turno frente a 14–18 s del
> 3b, y con los dos modelos residentes el Pi se quedó en 194 MB libres, al borde
> del OOM killer. `MODEL_CHAT` permite cambiarlo, pero conviene dejarlo igual que
> `MODEL_NAME` para que solo haya un modelo en RAM.

---

## Puesta en marcha

```bash
git clone <url-del-repositorio> SivIA && cd SivIA

# Crear la configuración local a partir de la plantilla
cp .env.example .env

# Generar credenciales aleatorias y pegarlas en .env
python3 -c "import secrets,string; a=string.ascii_letters+string.digits; print(''.join(secrets.choice(a) for _ in range(32)))"
python3 -c "import secrets; print(secrets.token_urlsafe(36))"   # para SIVIA_API_KEY

docker compose up -d --build
bash scripts/migrar.sh upgrade   # crea el esquema de la base de datos
docker compose ps                # ambos servicios deben quedar 'healthy'
```

### Dar de alta los productos del negocio

**Este paso es obligatorio.** Con el catálogo vacío, `POST /api/procesar-comando`
responde `409`: no hay contra qué validar el producto extraído.

```bash
curl -X POST http://localhost:8000/api/productos \
  -H "Content-Type: application/json" -H "X-API-Key: $SIVIA_API_KEY" \
  -d '{"nombre":"AREPAS","alias":["AREPA","AREPITAS"]}'
```

Los `alias` son las formas alternativas que deben resolver al mismo producto.
Merece la pena poner las que se digan de verdad en el local: cada una ahorra un
comando rechazado. También se pueden añadir después, una a una, cuando aparezcan.

### Crear el primer usuario

**También obligatorio si se va a usar el chat**, que exige una sesión de persona.

No hay ninguna cuenta sembrada, y es deliberado: sembrarla habría exigido escribir
una contraseña en un archivo versionado. El primer administrador se crea con la
API key, que ya vive solo en `.env`:

```bash
curl -X POST http://localhost:8000/api/usuarios \
  -H "Content-Type: application/json" -H "X-API-Key: $SIVIA_API_KEY" \
  -d '{"usuario":"john","nombre":"John García","clave":"UNA-CLAVE-LARGA","rol":"admin"}'
```

A partir de ahí ese administrador crea al resto desde el mismo endpoint,
autenticándose ya con su sesión. Roles disponibles: `admin` (puede eliminar
movimientos, retirar productos y gestionar cuentas) y `operador` (consulta y
registra, no elimina).

> No escribas la contraseña en un script ni la dejes en el historial del shell.
> `read -s CLAVE` y luego usar `$CLAVE` es suficiente.

Comprobar que funciona:

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"usuario":"john","clave":"UNA-CLAVE-LARGA"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")

curl -X POST http://localhost:8000/api/conversar \
  -H "Content-Type: application/json" -H "Authorization: Bearer $TOKEN" \
  -d '{"texto":"cuántas arepas tengo"}'
```

Documentación interactiva de la API: **http://localhost:8000/docs**

---

## Variables de entorno

| Variable | Descripción |
|---|---|
| `MYSQL_ROOT_PASSWORD` | Contraseña de root de MySQL. |
| `MYSQL_DATABASE` | Nombre de la base de datos (`sivia_db`). |
| `MYSQL_USER` / `MYSQL_PASSWORD` | Credenciales de la aplicación. |
| `OLLAMA_HOST` | URL de Ollama en el host (`http://172.17.0.1:11434`). |
| `MODEL_NAME` | Modelo de extracción (`qwen2.5:3b`). |
| `MODEL_CHAT` | Modelo del chat. Por defecto el mismo que `MODEL_NAME`, y conviene dejarlo así: dos modelos distintos quedan ambos residentes en RAM y no cabe. |
| `OLLAMA_TIMEOUT` | Segundos máximos de espera al LLM en la extracción (por defecto `60`). |
| `OLLAMA_TIMEOUT_CHAT` | Lo mismo para el chat (por defecto `180`). Es mayor porque un turno genera mucho más texto. |
| `DURACION_SESION_HORAS` | Vigencia de una sesión de usuario (por defecto `12`, una jornada). |
| `VIGENCIA_ACCION_MINUTOS` | Cuánto vive una propuesta sin confirmar (por defecto `30`). |
| `MAX_MENSAJES_CONTEXTO` | Turnos previos que se le envían al modelo (por defecto `8`). Subirlo alarga la inferencia. |
| `SIVIA_API_KEY` | Clave del header `X-API-Key`. Credencial de **máquina**, equivalente a un administrador. |
| `CORS_ORIGINS` | Orígenes permitidos, separados por coma. `*` solo en desarrollo. |

`DATABASE_URL` y `SIVIA_API_KEY` son **obligatorias**: si falta alguna, la API aborta el arranque en lugar de operar de forma insegura o escribir en una base efímera.

> ⚠️ `.env` contiene secretos y está excluido en `.gitignore`. Nunca debe subirse al repositorio.

---

## API

### Sistema

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/` | Identificación y estado del servicio. |
| `GET` | `/health` | Estado de MySQL y de Ollama. `healthy` o `degraded`. |

### Autenticación

| Método | Ruta | Auth | Descripción |
|---|---|:--:|---|
| `POST` | `/api/auth/login` | — | Usuario y contraseña por un token de sesión. |
| `POST` | `/api/auth/logout` | — | Revoca la sesión actual. Nunca falla. |
| `GET` | `/api/auth/yo` | 🔒 | Identidad y rol de quien pregunta. |

### Conversación

| Método | Ruta | Auth | Descripción |
|---|---|:--:|---|
| `POST` | `/api/conversar` | 👤 | Un turno de chat. Propone las escrituras, no las ejecuta. |
| `POST` | `/api/conversar/confirmar` | 👤 | Ejecuta o descarta una propuesta. Instantáneo. |
| `GET` | `/api/conversaciones` | 👤 | Hilos propios, por actividad reciente. |
| `GET` | `/api/conversaciones/{id}` | 👤 | Un hilo con todos sus mensajes. |
| `DELETE` | `/api/conversaciones/{id}` | 👤 | Borra el hilo. **No toca el inventario.** |

### Inventario

| Método | Ruta | Auth | Descripción |
|---|---|:--:|---|
| `POST` | `/api/procesar-comando` | 🔒 | Procesa un comando de voz y registra el movimiento. |
| `GET` | `/api/movimientos` | — | Histórico paginado (`limit`, `offset`, `producto`). |
| `GET` | `/api/stock` | — | Stock actual por producto (ingresos − egresos). |
| `DELETE` | `/api/movimientos/{id}` | 👑 | Elimina un movimiento mal registrado. |

### Catálogo de productos

| Método | Ruta | Auth | Descripción |
|---|---|:--:|---|
| `GET` | `/api/productos` | — | Catálogo con sus alias (`?incluir_inactivos=true`). |
| `POST` | `/api/productos` | 🔒 | Da de alta un producto, con alias opcionales. |
| `POST` | `/api/productos/{id}/alias` | 🔒 | Añade una variante a un producto existente. |
| `PATCH` | `/api/productos/{id}?activo=` | 👑 | Retira (`false`) o reincorpora (`true`). |
| `DELETE` | `/api/productos/{id}` | 👑 | Retira del catálogo (baja lógica). |

### Usuarios

| Método | Ruta | Auth | Descripción |
|---|---|:--:|---|
| `GET` | `/api/usuarios` | 👑 | Cuentas del sistema (`?incluir_inactivos=true`). |
| `POST` | `/api/usuarios` | 👑 | Da de alta una cuenta. |
| `PATCH` | `/api/usuarios/{id}?rol=&activo=` | 👑 | Cambia rol o estado. |
| `POST` | `/api/usuarios/{id}/clave` | 🔒 | Cambia una contraseña. |

- 🔒 = `Authorization: Bearer <token>` **o** el header `X-API-Key`.
- 👑 = además, rol `admin`. La API key cuenta como administrador.
- 👤 = **solo** sesión de persona; la API key no sirve, porque una conversación
  pertenece a alguien.

### Ejemplos

**Conversar** — todos los ejemplos de esta sección asumen `$TOKEN` obtenido en
`/api/auth/login`.

```bash
curl -X POST http://localhost:8000/api/conversar \
  -H "Content-Type: application/json" -H "Authorization: Bearer $TOKEN" \
  -d '{"texto":"llegaron 20 panes de yuca esta mañana"}'
```

```json
{
  "id_conversacion": 7,
  "respuesta": "Registrar una entrada de 20 PANES DE YUCA.\n\n¿Lo confirmo?",
  "intencion": "registrar_movimiento",
  "requiere_confirmacion": true,
  "accion": {
    "id": 3,
    "tipo": "registrar_movimiento",
    "resumen": "Registrar una entrada de 20 PANES DE YUCA.",
    "parametros": {"producto_id": 3, "producto": "PANES DE YUCA",
                   "cantidad": 20, "tipo_movimiento": "INGRESO"},
    "expira_en": "2026-08-03T02:34:00"
  },
  "datos": null
}
```

**Nada se ha guardado todavía.** Hace falta confirmar:

```bash
curl -X POST http://localhost:8000/api/conversar/confirmar \
  -H "Content-Type: application/json" -H "Authorization: Bearer $TOKEN" \
  -d '{"id_accion":3,"confirmar":true}'
```

```json
{
  "id_conversacion": 7,
  "respuesta": "Listo. Entrada de 20 PANES DE YUCA registrada con el número #13.",
  "intencion": "registrar_movimiento",
  "requiere_confirmacion": false,
  "accion": null,
  "datos": null
}
```

Un `{"texto":"dale","id_conversacion":7}` hace exactamente lo mismo, y también
al instante: el «sí» no pasa por el modelo.

**Crear un producto hablando** — lo que antes obligaba a salir a la API:

```bash
curl -X POST http://localhost:8000/api/conversar \
  -H "Content-Type: application/json" -H "Authorization: Bearer $TOKEN" \
  -d '{"texto":"crea el producto chicharrón con arepa","id_conversacion":7}'
```

Responde con una propuesta `crear_producto`. Se confirma igual que arriba, y el
producto queda disponible para registrarle movimientos en el turno siguiente.

**Registrar un movimiento** (camino directo, sin conversación)

```bash
curl -X POST http://localhost:8000/api/procesar-comando \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $SIVIA_API_KEY" \
  -d '{"texto":"Llegaron 45 arepas para el inventario"}'
```

```json
{
  "status": "success",
  "mensaje": "Movimiento de inventario procesado y registrado correctamente",
  "datos_extraidos": {
    "id_registro": 1,
    "producto": "AREPAS",
    "cantidad": 45,
    "tipo_movimiento": "INGRESO",
    "fecha_utc": "2026-07-27T22:15:03+00:00"
  }
}
```

**Consultar el stock**

```bash
curl http://localhost:8000/api/stock
```

```json
[
  {
    "producto": "AREPAS",
    "stock_actual": 33,
    "total_ingresos": 45,
    "total_egresos": 12,
    "movimientos": 2,
    "ultimo_movimiento": "2026-07-27T22:18:41"
  }
]
```

**Corregir un error de transcripción**

```bash
curl -X DELETE http://localhost:8000/api/movimientos/3 \
  -H "X-API-Key: $SIVIA_API_KEY"
```

**Cuando el producto no se reconoce**

```bash
curl -X POST http://localhost:8000/api/procesar-comando \
  -H "Content-Type: application/json" -H "X-API-Key: $SIVIA_API_KEY" \
  -d '{"texto":"Llegaron 20 panes de yaca"}'
```

```json
{
  "detail": "El producto 'PANES DE YACA' no está en el catálogo. ¿Quisiste decir PANES DE YUCA? Si es correcto, registrar la variante con POST /api/productos/{id}/alias para que se reconozca en adelante."
}
```

No se registra nada. Si la sugerencia era la correcta, se enseña la variante una
sola vez y el sistema la reconoce a partir de entonces:

```bash
curl -X POST http://localhost:8000/api/productos/3/alias \
  -H "Content-Type: application/json" -H "X-API-Key: $SIVIA_API_KEY" \
  -d '{"alias":"PANES DE YACA"}'
```

---

## Reglas de negocio

- `producto` debe existir en el **catálogo**. Se normaliza a MAYÚSCULAS con
  espacios simples (los guiones bajos y guiones que a veces inserta el modelo se
  convierten en espacios), y después se resuelve contra los nombres canónicos y
  sus alias, ignorando acentos.
- `cantidad` debe ser un **entero positivo** (validado en Pydantic y por `CHECK` en MySQL).
- `tipo_movimiento` admite únicamente `INGRESO` o `EGRESO`.
  - **INGRESO**: llega, entra, abastece, compra, mete.
  - **EGRESO**: sale, vende, gasta, desecha.
- `fecha` se almacena en **UTC**.

Si el LLM no logra determinar con certeza alguno de los tres campos, la API responde **422** y **no escribe nada**. Esto es deliberado: un `EGRESO` mal interpretado que se guardara como `INGRESO` descuadraría el inventario sin dejar rastro.

Un producto que no está en el catálogo tampoco se registra: la API responde
**422** con los nombres más parecidos. **No se da de alta automáticamente**, y es
la misma razón — un nombre mal transcrito se convertiría en un producto legítimo
con su propio saldo, y el stock volvería a fragmentarse en silencio.

Retirar un producto (`DELETE /api/productos/{id}`) es **baja lógica**: la fila se
conserva porque los movimientos históricos la referencian. Deja de aceptar
comandos nuevos y sigue sumando lo ya registrado.

### En el chat, la regla cambia de forma pero no de fondo

El chat **sí** puede dar de alta un producto, y eso no contradice lo anterior: lo
que sigue prohibido es el alta **automática**. En `POST /api/conversar` nada se
escribe nunca; SivIA propone y una persona confirma viendo el nombre exacto que
se va a crear. La confirmación es la intervención humana que la regla exige, solo
que ocurre dentro de la conversación en lugar de en otra llamada a la API.

De ahí se siguen tres cosas:

- Una propuesta caduca a los **30 minutos**. Confirmar mañana un «vendimos 20
  arepas» de hoy lo registraría con la fecha de mañana.
- Al confirmar **no se reinterpreta el texto**: se ejecuta lo que se le enseñó a
  la persona. Reinterpretar permitiría ejecutar algo distinto de lo aprobado.
- Los **datos de las consultas los calcula el backend**, no el modelo. El chat
  comparte el cálculo del stock con `GET /api/stock`, así que las dos vías no
  pueden dar cifras distintas.

### Permisos

| | `operador` | `admin` |
|---|:--:|:--:|
| Conversar, consultar | ✅ | ✅ |
| Registrar movimientos, crear productos y alias | ✅ | ✅ |
| Eliminar movimientos, retirar productos | ❌ | ✅ |
| Gestionar cuentas | ❌ | ✅ |

Vale igual por chat que por API: si un operador le pide a SivIA que borre algo,
la respuesta explica que no tiene permiso y **no se propone nada**. La
`SIVIA_API_KEY` equivale a un administrador, pero no a una persona: lo que
registra queda con `autor: null`, y no sirve para el chat.

---

## Códigos de respuesta

| Código | Significado |
|---|---|
| `200` | Operación correcta. |
| `201` | Producto o usuario creado. |
| `401` | Credenciales ausentes, inválidas o caducadas. |
| `403` | Autenticado, pero sin permiso: falta el rol `admin`, o es un recurso de otra persona, o se intentó usar la API key en el chat. |
| `404` | El movimiento, producto, usuario o conversación no existe. |
| `409` | El catálogo está vacío; el nombre, alias o usuario ya está en uso; la propuesta ya se resolvió o caducó; o se intentó dejar el sistema sin administradores. |
| `422` | El comando no contiene entidades reconocibles, o el producto no está en el catálogo. |
| `502` | Ollama respondió con un error. |
| `503` | Ollama no está disponible (servicio caído). |
| `504` | El LLM excedió su timeout. |

En el **chat** casi nada de esto se ve: un rechazo de negocio («ese producto no
está en el catálogo», «tu cuenta no puede eliminar») llega como un `200` con la
explicación en `respuesta`, porque es parte de la conversación y no un fallo de
la petición. Los códigos de error quedan para lo que de verdad falló: la sesión,
el permiso de un endpoint REST, o el motor de IA.

---

## Operación

```bash
docker compose logs -f api          # logs de la API
docker compose logs -f db           # logs de MySQL
docker compose restart api          # reiniciar tras cambiar .env
docker compose up -d --build        # reconstruir tras cambiar el código
systemctl status ollama             # estado del motor de IA
curl localhost:8000/health          # diagnóstico rápido de dependencias
```

### Tests automatizados

```bash
bash scripts/probar.sh                      # toda la suite (~8 s)
bash scripts/probar.sh -k validar           # solo los que coincidan
bash scripts/probar.sh tests/test_endpoints.py   # un archivo concreto
```

**247 tests** sobre la validación de entidades, la extracción de JSON, el catálogo
de productos, la autenticación y los roles, el motor conversacional y los
endpoints HTTP. No necesitan MySQL ni Ollama: usan SQLite en memoria y un doble
del LLM, y corren con `--network none` para garantizar el aislamiento.

De los tests del chat, la mayoría comprueban **dos** cosas y no una: la respuesta
esperada, **y que la base de datos no haya cambiado** hasta la confirmación. Es
la propiedad de la que depende todo el diseño.

Las dependencias de test viven en `requirements-dev.txt` y solo se instalan en la
etapa `tests` de la imagen, así que **la imagen de producción no incluye pytest**.

Sustituir el LLM por un doble permite provocar a voluntad los fallos que con el
modelo real son casi imposibles de reproducir: que devuelva `cantidad: 0`, un
`tipo_movimiento` inventado, JSON no parseable, o que Ollama esté caído. Lo que se
verifica en esos casos no es solo el código de respuesta, sino que **no se escriba
nada en la base de datos**.

### Evaluar el prompt contra el modelo real

Aislar el LLM tiene un punto ciego: **el prompt es nuestro y sus defectos no se
ven con un doble.** La primera versión del prompt del chat pasaba los 239 tests y
aun así, en cuanto había conversación de por medio, el modelo dejaba `cantidad` a
null en «vendimos 5 empanadas» y arrastraba el producto del turno anterior.

```bash
docker run --rm --network host -v "$PWD/backend:/app" -v "$PWD/scripts:/s" \
  -w /app -e DATABASE_URL=sqlite:// -e SIVIA_API_KEY=x \
  --entrypoint python sivia-api:latest /s/evaluar_prompt.py
```

Mide catorce casos representativos contra Qwen. **No forma parte de `probar.sh`**
a propósito: necesita el modelo real, tarda varios minutos y su resultado depende
de la versión instalada. Ejecutarlo antes y después de tocar el prompt, y no dar
por buena una redacción que baje el marcador.

### Migraciones de esquema (Alembic)

El esquema lo gestiona Alembic. La aplicación **no** crea ni modifica tablas al
arrancar: solo verifica la conexión y registra en el log la revisión aplicada.

```bash
bash scripts/migrar.sh estado                 # revisión aplicada actualmente
bash scripts/migrar.sh historial              # historial de migraciones
bash scripts/migrar.sh upgrade                # aplicar las pendientes
bash scripts/migrar.sh nueva "descripción"    # autogenerar desde models.py
bash scripts/migrar.sh downgrade              # revertir la última
bash scripts/migrar.sh sql                    # ver el SQL sin ejecutarlo
```

Para cambiar el esquema: se edita el modelo en `backend/models.py`, **se
reconstruye la imagen** con `docker compose build api`, se genera la revisión con
`nueva`, **se revisa el archivo producido** en `backend/migrations/versions/`, y
se aplica con `upgrade`.

> ⚠️ La reconstrucción no es opcional. Alembic corre sobre la imagen construida y
> solo `backend/migrations/` se monta desde el host, así que sin reconstruir
> importaría la versión anterior de `models.py` y compararía contra un modelo
> desactualizado.

Si la migración toca datos (siembra, backfill), conviene además respaldar antes
con `mysqldump` y probar el ciclo `upgrade` → `downgrade` → `upgrade` comprobando
que el número de filas no cambia. Un `downgrade()` que nunca se ejecuta es un
`downgrade()` que no existe.

> ⚠️ Si un `downgrade` **falla a mitad**, revisar el esquema a mano: MySQL no
> revierte el DDL ya aplicado, pero `alembic_version` se queda como estaba, así
> que `migrar.sh estado` informará de una revisión que no corresponde a la base.

Alembic se ejecuta en un contenedor efímero que se destruye al terminar: no hay
proceso residente ni consumo de memoria permanente, y no requiere instalar nada
en el host.

> ⚠️ El autogenerado acierta con altas y bajas de columnas, pero no adivina las
> migraciones de datos ni los renombrados: un renombrado lo ve como una baja más
> un alta, lo que **borraría los datos** de esa columna. Revisar siempre el
> archivo antes de aplicarlo, y comprobar el SQL con `migrar.sh sql`.

**Backup de la base de datos**

```bash
docker exec sivia_db mysqldump -u root -p"$MYSQL_ROOT_PASSWORD" sivia_db \
  | gzip > "backup_sivia_$(date +%F).sql.gz"
```

---

## Notas de seguridad

- MySQL **no** publica el puerto 3306 al host: solo es accesible desde la red interna de Docker.
- El contenedor de la API corre con un usuario **sin privilegios** (`appuser`, uid 1001).
- Los endpoints de escritura exigen una sesión de usuario (`Authorization: Bearer`) o la `X-API-Key`, comparada con `secrets.compare_digest`.
- Las contraseñas se guardan con **scrypt** (`hashlib`, sin dependencias nuevas) y sal por usuario. De las sesiones se guarda el **SHA-256 del token**, nunca el token: quien lea la tabla no puede suplantar a nadie.
- **La revocación es inmediata.** Desactivar una cuenta o cambiar una contraseña invalida sus sesiones abiertas en el acto, sin esperar a que caduquen. Fue la razón de elegir tokens en base de datos en vez de JWT: un JWT firmado vale hasta que expira aunque se pierda el móvil.
- El login responde lo mismo si el usuario no existe, está desactivado o la clave es incorrecta, y verifica contra un hash señuelo cuando la cuenta no existe para que el tiempo de respuesta tampoco lo delate.
- El texto del usuario se envía a Ollama en el campo `prompt`, separado del `system`, y el prompt de sistema instruye explícitamente al modelo a ignorar instrucciones incrustadas en él. El historial de la conversación viaja también en el `prompt`, nunca en el `system`, para que un mensaje guardado no pueda reescribir las reglas en un turno posterior.
- Los detalles de las excepciones van al log, nunca al cliente.
- El acceso remoto va por **Tailscale** (`100.110.69.36`), no por internet. El túnel WireGuard cifra y autentica extremo a extremo; el puerto 8000 **no debe exponerse fuera del tailnet**.

---

## Estado del proyecto

| Componente | Estado |
|---|:--:|
| Infraestructura Docker + MySQL | ✅ |
| Motor de IA (Ollama + Qwen2.5:3b) | ✅ |
| API de movimientos y stock | ✅ |
| Autenticación + CORS | ✅ |
| Migraciones de esquema (Alembic) | ✅ |
| Catálogo de productos con alias | ✅ |
| Multiusuario con roles y autoría | ✅ |
| Chat conversacional con confirmación | ✅ |
| Suite de tests automatizados | ✅ 247 tests |
| App móvil / captura de voz | ⬜ Backend listo; [especificación escrita](ANDROID_APP.md), app sin construir |
| Unidades de medida | ⬜ Pendiente |
| Stock negativo sin control | ⬜ Pendiente de decisión |
| Backups automatizados | ⬜ Pendiente |
| Tests de integración | ⬜ Pendiente |
