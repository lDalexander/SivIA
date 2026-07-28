# 🎙️ SivIA — Sistema de Inventario por Voz con Inteligencia Artificial

Solución **Edge-AI** de control de stock para negocios de alimentos y suministros. Captura un comando de voz en lenguaje natural, extrae las entidades de inventario (producto, cantidad, tipo de movimiento) con un LLM que corre **localmente** en una Raspberry Pi 5, y persiste la transacción en MySQL.

Todo el procesamiento de IA ocurre en el dispositivo: **ningún dato sale a la nube**.

```
Voz → transcripción → FastAPI → Qwen2.5:3b (Ollama local) → MySQL
```

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
| Modelo residente en RAM | **~6,5 s** |
| Primera petición tras arrancar | ~21,5 s |

`OLLAMA_TIMEOUT` está en 60 s por ese margen. Para bajar de 6,5 s la vía es un
modelo más pequeño (`qwen2.5:1.5b`), a costa de precisión en la extracción.

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
| `OLLAMA_TIMEOUT` | Segundos máximos de espera al LLM (por defecto `60`). |
| `SIVIA_API_KEY` | Clave del header `X-API-Key` para los endpoints de escritura. |
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

### Inventario

| Método | Ruta | Auth | Descripción |
|---|---|:--:|---|
| `POST` | `/api/procesar-comando` | 🔑 | Procesa un comando de voz y registra el movimiento. |
| `GET` | `/api/movimientos` | — | Histórico paginado (`limit`, `offset`, `producto`). |
| `GET` | `/api/stock` | — | Stock actual por producto (ingresos − egresos). |
| `DELETE` | `/api/movimientos/{id}` | 🔑 | Elimina un movimiento mal registrado. |

🔑 = requiere el header `X-API-Key`.

### Ejemplos

**Registrar un movimiento**

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

---

## Reglas de negocio

- `producto` se normaliza siempre a **MAYÚSCULAS**.
- `cantidad` debe ser un **entero positivo** (validado en Pydantic y por `CHECK` en MySQL).
- `tipo_movimiento` admite únicamente `INGRESO` o `EGRESO`.
  - **INGRESO**: llega, entra, abastece, compra, mete.
  - **EGRESO**: sale, vende, gasta, desecha.
- `fecha` se almacena en **UTC**.

Si el LLM no logra determinar con certeza alguno de los tres campos, la API responde **422** y **no escribe nada**. Esto es deliberado: un `EGRESO` mal interpretado que se guardara como `INGRESO` descuadraría el inventario sin dejar rastro.

---

## Códigos de respuesta

| Código | Significado |
|---|---|
| `200` | Operación correcta. |
| `401` | `X-API-Key` ausente o inválida. |
| `404` | El movimiento solicitado no existe. |
| `422` | El comando no contiene entidades de inventario reconocibles. |
| `502` | Ollama respondió con un error. |
| `503` | Ollama no está disponible (servicio caído). |
| `504` | El LLM excedió `OLLAMA_TIMEOUT`. |

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
bash scripts/probar.sh                      # toda la suite (~0,6 s)
bash scripts/probar.sh -k validar           # solo los que coincidan
bash scripts/probar.sh tests/test_endpoints.py   # un archivo concreto
```

**79 tests** sobre la validación de entidades, la extracción de JSON y los
endpoints HTTP. No necesitan MySQL ni Ollama: usan SQLite en memoria y un doble
del LLM, y corren con `--network none` para garantizar el aislamiento. Por eso la
suite entera tarda menos de un segundo.

Las dependencias de test viven en `requirements-dev.txt` y solo se instalan en la
etapa `tests` de la imagen, así que **la imagen de producción no incluye pytest**.

Sustituir el LLM por un doble permite provocar a voluntad los fallos que con el
modelo real son casi imposibles de reproducir: que devuelva `cantidad: 0`, un
`tipo_movimiento` inventado, JSON no parseable, o que Ollama esté caído. Lo que se
verifica en esos casos no es solo el código de respuesta, sino que **no se escriba
nada en la base de datos**.

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

Para cambiar el esquema: se edita el modelo en `backend/models.py`, se genera la
revisión con `nueva`, **se revisa el archivo producido** en
`backend/migrations/versions/`, y se aplica con `upgrade`.

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
- Los endpoints de escritura exigen `X-API-Key`, comparada con `secrets.compare_digest`.
- El texto del usuario se envía a Ollama en el campo `prompt`, separado del `system`, y el prompt de sistema instruye explícitamente al modelo a ignorar instrucciones incrustadas en él.
- Los detalles de las excepciones van al log, nunca al cliente.

---

## Estado del proyecto

| Componente | Estado |
|---|:--:|
| Infraestructura Docker + MySQL | ✅ |
| Motor de IA (Ollama + Qwen2.5:3b) | ✅ |
| API de movimientos y stock | ✅ |
| Autenticación + CORS | ✅ |
| Migraciones de esquema (Alembic) | ✅ |
| Suite de tests automatizados | ✅ 79 tests |
| App móvil / captura de voz | ⬜ Pendiente |
| Catálogo de productos y unidades de medida | ⬜ Pendiente |
| Multiusuario y auditoría | ⬜ Pendiente |
