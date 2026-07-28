# 🏛️ SivIA — Documentación de Arquitectura de Hardware y Software

> Este documento describe el estado **verificado** del sistema. Lo que aún no
> existe está marcado explícitamente como pendiente en la sección 8. No añadir
> aquí componentes planificados como si estuvieran operativos.
>
> Última verificación: 2026-07-27.

## 1. Visión del Proyecto
**SivIA** (*Sistema de Inventario por Voz con Inteligencia Artificial*) es una solución Edge-AI orientada al control de stock e inventarios para negocios de alimentos y suministros. El sistema captura comandos de voz natural, extrae entidades estandarizadas (Producto, Cantidad, Tipo de Movimiento) mediante un modelo LLM local y persiste las transacciones en una base de datos relacional. Todo el procesamiento de IA es local: ningún dato del negocio sale del dispositivo.

---

## 2. Infraestructura de Hardware (Edge Server)
- **Host:** Raspberry Pi 5 (8 GB RAM / CPU Broadcom BCM2712 ARMv8.2-A quad-core @ 2.4 GHz).
- **Almacenamiento:** SSD NVMe de 128 GB sobre HAT PCIe.
- **Sistema Operativo:** Raspberry Pi OS Lite (Debian 13 Trixie arm64, headless).
- **Directorio Raíz del Proyecto:** `/home/admin/var/www/SivIA/`
- **Usuario de Ejecución:** `admin` (con `sudo` sin contraseña).

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
  la decisión 6 de la §6.

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

### 3.3 Base de Datos (Contenedor Docker) ✅ Operativo
- **Motor:** MySQL 8.0 (arm64).
- **Persistencia:** volumen sobre el SSD NVMe: `./database/data -> /var/lib/mysql`.
- **Red:** **no** publica el puerto 3306 al host. Solo accesible por la red interna de Docker (`db:3306`).
- **Estructura de Datos (`movimientos_inventario`):**
  - `id` (INT, PK, auto-increment)
  - `producto` (VARCHAR 100, NOT NULL, indexado, normalizado en MAYÚSCULAS)
  - `cantidad` (INT, NOT NULL, `CHECK (cantidad > 0)`)
  - `tipo_movimiento` (VARCHAR 20, NOT NULL, `CHECK IN ('INGRESO','EGRESO')`)
  - `fecha` (DATETIME, NOT NULL, indexado, UTC naive — MySQL DATETIME no guarda zona horaria)

### 3.5 Tests Automatizados ✅ Operativo
- **79 tests**, suite completa en **~0,6 s**. Se ejecutan con `scripts/probar.sh`.
- **Aislamiento total:** SQLite en memoria (una base limpia por test) y un doble
  de `consultar_ollama`. El contenedor corre con `--network none`: un test que
  necesite red es un test que no está aislado.
- **Etapa `tests` del Dockerfile:** `pytest` vive en `requirements-dev.txt` y solo
  se instala en esa etapa. El servicio `api` del compose fija `target: runtime`,
  así que la imagen de producción **no** contiene pytest (verificado: 385 MB y
  `ModuleNotFoundError: No module named 'pytest'`).
- **Lo que cubren:** `validar_entidades` (todos los caminos de rechazo),
  `extraer_json` (envoltorios de markdown), y los endpoints — autenticación,
  códigos de estado, aritmética del stock, y sobre todo que una extracción
  defectuosa devuelva 422 **sin escribir nada**.
- **Lo que NO cubren:** la calidad de extracción del LLM (no es nuestro código) ni
  el comportamiento específico de MySQL frente a SQLite. Para eso haría falta una
  suite de integración, aún pendiente.

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
├── scripts/
│   ├── probar.sh         # suite de tests
│   ├── migrar.sh         # gestión de migraciones (Alembic)
│   └── aplicar_rotacion.sh
├── backend/
│   ├── Dockerfile        # multi-etapa, usuario no-root
│   ├── .dockerignore
│   ├── requirements.txt
│   ├── requirements-dev.txt  # solo testing, fuera de la imagen de producción
│   ├── pytest.ini
│   ├── tests/            # 79 tests, sin dependencias externas
│   ├── alembic.ini
│   ├── migrations/       # revisiones de esquema versionadas
│   │   ├── env.py
│   │   └── versions/
│   ├── models.py         # esquema SQLAlchemy (aislado de la config de la app)
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
| `POST` | `/api/procesar-comando` | 🔑 | Procesa el comando de voz y registra el movimiento. |
| `GET` | `/api/movimientos` | — | Histórico paginado (`limit`, `offset`, `producto`). |
| `GET` | `/api/stock` | — | Stock por producto: `SUM(INGRESO) - SUM(EGRESO)`. |
| `DELETE` | `/api/movimientos/{id}` | 🔑 | Elimina un movimiento mal registrado. |

🔑 = requiere el header `X-API-Key` (comparado con `secrets.compare_digest`).

CORS habilitado vía `CORS_ORIGINS`. Con origen comodín se desactivan las
credenciales, como exige la especificación CORS.

---

## 6. Decisiones de diseño que conviene no revertir

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
6. **No reintroducir `create_all()` en el arranque.** Con Alembic gestionando el
   esquema, `create_all()` volvería a ser una fuente de divergencia silenciosa:
   crea las tablas ausentes pero nunca altera las existentes, así que un modelo
   modificado y una migración sin aplicar convivirían sin dar ningún error. El
   arranque solo verifica la conexión y registra la revisión vigente.

---

## 7. Comandos de operación

```bash
docker compose up -d --build     # reconstruir y arrancar
docker compose ps                # ambos servicios deben quedar 'healthy'
docker compose logs -f api       # logs de la API
curl localhost:8000/health       # diagnóstico de dependencias
systemctl status ollama          # estado del motor de IA
ollama list                      # modelos descargados
```

---

## 8. Pendientes conocidos

- ⬜ **Tests de integración.** La suite actual aísla MySQL y Ollama. Falta un
  puñado de tests contra los servicios reales: que las `CHECK` de MySQL
  rechacen de verdad, y que el modelo extraiga bien un comando típico.
- ⬜ **App móvil / captura de voz.** No existe. El proyecto es backend puro; la
  transcripción de audio a texto se asume externa (el endpoint recibe ya el texto).
- ⬜ **Catálogo de productos y unidades de medida.** `producto` es texto libre
  normalizado; «AREPA» y «AREPAS» son productos distintos para el sistema.
- ⬜ **Normalización de nombres de producto multipalabra.** Defecto observado el
  2026-07-28: el comando «Llegaron 20 panes de yuca» se guardó como
  `PANES_DE_YACA` — el modelo insertó guiones bajos y además escribió mal «yuca».
  El prompt exige mayúsculas pero no dice nada sobre separadores ni sobre
  preservar la ortografía literal. Con texto libre, cada variante crea un producto
  distinto y fragmenta el stock en silencio. Arreglo mínimo: reforzar el prompt
  (espacios, no guiones bajos; no corregir ni inventar palabras). Arreglo real: el
  catálogo de productos con validación contra una lista cerrada.
- ⬜ **Multiusuario y auditoría.** Una sola API key compartida; los movimientos no
  registran autor.
- ⬜ **Backups automatizados.** El comando de `mysqldump` está documentado en el
  README, pero no hay cron.
- ⬜ **HTTPS.** La API sirve HTTP en claro; la API key viaja sin cifrar en la LAN.
