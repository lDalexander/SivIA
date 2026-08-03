# 📱 SivIA — Especificación de la app Android

> **Para quién es este documento.** Para la IA de Android Studio (o para quien
> escriba la app) que va a construir el cliente móvil de SivIA. El backend
> **ya existe, está funcionando y no hay que modificarlo**: esto describe
> exactamente qué expone y qué debe hacer la app contra él.
>
> Toda la información de este documento está verificada contra el backend en
> ejecución el 2026-08-02.
>
> Documentación relacionada del proyecto: [`CLAUDE.md`](CLAUDE.md) (arquitectura),
> [`FUNCIONAMIENTO.md`](FUNCIONAMIENTO.md) (recorrido de un dato),
> [`README.md`](README.md) (operación del servidor).

---

## 1. Qué es la app

Un chat tipo ChatGPT, en español, para llevar el inventario de un negocio de
alimentos **hablando**. La persona dice «llegaron 20 panes de yuca» y SivIA lo
entiende, lo resume y **pide confirmación** antes de guardar nada.

Tres cosas la diferencian de un chat cualquiera, y las tres condicionan el diseño
de la interfaz:

1. **Nada se guarda sin un sí explícito.** Cuando SivIA propone una escritura, la
   respuesta trae `requiere_confirmacion: true`. La app **debe** mostrar botones
   de Confirmar / Cancelar y **no debe** dar a entender que algo quedó guardado.
2. **La IA corre en una Raspberry Pi, no en la nube.** Cada turno tarda entre
   **15 y 30 segundos**. Esto no es un fallo que haya que ocultar: hay que
   diseñar la espera. Ver §7.
3. **Hay roles.** Un operador registra y consulta; solo un administrador puede
   eliminar. La app debe reflejarlo, no descubrirlo a base de errores.

---

## 2. Conexión: Tailscale

El servidor **no está en internet**. Es una Raspberry Pi accesible únicamente por
la VPN privada Tailscale.

| | |
|---|---|
| **URL base** | `http://100.110.69.36:8000` |
| **Nombre del host** | `srvr-ia-sql` |
| **Protocolo** | HTTP en claro (ver aviso abajo) |

**Requisito previo del usuario:** el móvil debe tener la app de Tailscale
instalada, con sesión iniciada en el mismo tailnet y **conectada**. Sin eso no
hay ninguna ruta hasta el servidor.

> **Sobre HTTP sin cifrar.** El tráfico va por el túnel WireGuard de Tailscale,
> que ya está cifrado y autenticado de extremo a extremo. Poner HTTPS encima
> añadiría un certificado que gestionar sin cambiar el modelo de amenaza. Lo que
> **no** hay que hacer es exponer este puerto fuera del tailnet.

### 2.1 Android bloquea HTTP en claro por defecto

Desde Android 9 el tráfico sin cifrar está prohibido salvo que se permita
explícitamente. **Sin esto la app fallará con un error de red opaco**, que es
difícil de diagnosticar. Autorizar solo esa IP, nunca todo el tráfico:

`res/xml/network_security_config.xml`:
```xml
<?xml version="1.0" encoding="utf-8"?>
<network-security-config>
    <domain-config cleartextTrafficPermitted="true">
        <domain includeSubdomains="false">100.110.69.36</domain>
    </domain-config>
</network-security-config>
```

`AndroidManifest.xml`:
```xml
<uses-permission android:name="android.permission.INTERNET" />
<uses-permission android:name="android.permission.RECORD_AUDIO" />

<application
    android:networkSecurityConfig="@xml/network_security_config"
    ... >
```

### 2.2 Los timeouts por defecto son demasiado cortos

OkHttp usa 10 segundos de lectura. Un turno de chat tarda hasta 30. **Con los
valores por defecto, cada mensaje fallará.**

```kotlin
private val cliente = OkHttpClient.Builder()
    .connectTimeout(10, TimeUnit.SECONDS)
    // Generoso a propósito: la inferencia corre en CPU ARM. 30 s es lo normal;
    // 120 deja margen para la primera petición tras un reinicio, cuando el
    // modelo todavía se está cargando en RAM y puede tardar bastante más.
    .readTimeout(120, TimeUnit.SECONDS)
    .writeTimeout(30, TimeUnit.SECONDS)
    .build()
```

Las pantallas de consulta (stock, catálogo, historial) **no pasan por la IA** y
responden en milisegundos. Solo `/api/conversar` es lento.

---

## 3. Autenticación

### 3.1 Iniciar sesión

```http
POST /api/auth/login
Content-Type: application/json

{"usuario": "martha", "clave": "su-contraseña"}
```

**200 OK**
```json
{
  "token": "Er8WWauVU7yu...",
  "expira_en": "2026-08-03T13:15:03",
  "usuario": {
    "id": 2,
    "usuario": "martha",
    "nombre": "Martha Restrepo",
    "rol": "operador",
    "activo": true,
    "creado_en": "2026-08-02T20:00:00",
    "ultimo_acceso": "2026-08-03T01:15:03"
  }
}
```

**401** si el usuario no existe, está desactivado o la contraseña es incorrecta.
El mensaje es el mismo en los tres casos, a propósito: no se le confirma a nadie
qué cuentas existen. La app debe mostrar «Usuario o contraseña incorrectos» sin
intentar distinguir más.

El nombre de usuario se normaliza en el servidor (se pasa a minúsculas y se le
quitan los espacios), así que **`Martha`, `martha` y ` MARTHA ` son la misma
cuenta**. No hace falta que la app lo corrija.

### 3.2 Usar el token

Todas las peticiones autenticadas llevan:

```
Authorization: Bearer <token>
```

### 3.3 Guardar el token

Es una credencial: va en `EncryptedSharedPreferences` o en un `DataStore`
cifrado, **nunca** en `SharedPreferences` en claro ni en un fichero.

Dura **12 horas**. Al arrancar la app:

1. ¿Hay token guardado? → `GET /api/auth/yo`.
2. Responde 200 → sesión válida; usa el `rol` que devuelve para configurar la
   interfaz.
3. Responde 401 → token caducado o revocado; borrarlo y mostrar el login.

`GET /api/auth/yo` devuelve el objeto `usuario` de arriba. **Consultar el rol
aquí y no fiarse del guardado en el móvil**: a una persona pueden haberle
cambiado el rol o desactivado la cuenta, y el cambio surte efecto de inmediato.

### 3.4 Cerrar sesión

```http
POST /api/auth/logout
Authorization: Bearer <token>
```

Siempre responde 200, incluso con un token ya inválido. Borrar el token local
después.

### 3.5 Un 401 en cualquier petición

Significa siempre lo mismo: la sesión dejó de valer (caducó, se cerró desde otro
sitio, se desactivó la cuenta o se cambió la contraseña). Reacción única en toda
la app: borrar el token y llevar al login. **No reintentar.**

---

## 4. Roles y permisos

| Rol | Puede | No puede |
|---|---|---|
| `operador` | Conversar, consultar stock e historial, registrar movimientos, crear productos y alias | Eliminar movimientos, retirar productos, gestionar usuarios |
| `admin` | Todo lo anterior **más** eliminar movimientos, retirar productos y gestionar cuentas | — |

En el negocio: **John** y **Jhonnathan** son `admin`; **Martha** es `operador`.

**Qué debe hacer la app.** Con `rol == "operador"`:

- No mostrar el botón de borrar en la lista de movimientos.
- No mostrar la sección de gestión de usuarios.
- No mostrar la opción de retirar un producto del catálogo.

**Y aun así manejar el 403.** Ocultar un botón es cortesía, no seguridad: el
permiso lo aplica el servidor en cada petición, y el rol puede cambiar mientras
la app está abierta. Si llega un 403, mostrar el mensaje del campo `detail` tal
cual — ya viene redactado para leerse.

Dentro del chat el permiso se resuelve solo: si Martha le pide a SivIA que borre
algo, la respuesta llega como texto normal («Tu cuenta puede consultar y
registrar, pero no eliminar…») con `requiere_confirmacion: false`. La app no
tiene que hacer nada especial.

---

## 5. El chat

### 5.1 Enviar un mensaje

```http
POST /api/conversar
Authorization: Bearer <token>
Content-Type: application/json

{"texto": "llegaron 20 panes de yuca", "id_conversacion": 7}
```

`id_conversacion` es opcional. **Si se omite, el servidor abre un hilo nuevo** y
devuelve su id; a partir de ahí hay que enviarlo siempre para continuar la misma
conversación.

### 5.2 La respuesta, y sus tres formas

Siempre el mismo objeto; lo que cambia es qué campos vienen rellenos.

```json
{
  "id_conversacion": 7,
  "respuesta": "texto para mostrar en la burbuja",
  "intencion": "registrar_movimiento",
  "requiere_confirmacion": false,
  "accion": null,
  "datos": null
}
```

**(a) Charla o aclaración** — `requiere_confirmacion: false`, `accion: null`,
`datos: null`. Pintar `respuesta` como burbuja del asistente y ya está. Aquí
caen los saludos y también los casos en los que SivIA necesita que le repitan
algo («no capté la cantidad, ¿cuántas unidades son?»).

**(b) Consulta** — `datos` trae los registros en bruto, además del texto.

```json
{
  "id_conversacion": 7,
  "respuesta": "Este es el stock actual:\n• AREPAS: 29\n• EMPANADAS: 22",
  "intencion": "consultar_stock",
  "requiere_confirmacion": false,
  "accion": null,
  "datos": [
    {"producto": "AREPAS", "stock_actual": 29, "total_ingresos": 46,
     "total_egresos": 17, "movimientos": 4,
     "ultimo_movimiento": "2026-07-28T15:11:26"}
  ]
}
```

Recomendado: pintar la burbuja con `respuesta` y, debajo, una tarjeta o tabla
con `datos`. El texto ya es legible por sí solo, así que la tabla es una mejora,
no un requisito.

La forma de `datos` depende de `intencion`:
- `consultar_stock` → lista de stock (igual que `GET /api/stock`).
- `consultar_movimientos` → lista de movimientos (igual que `GET /api/movimientos`).
- `listar_productos` → lista de productos (igual que `GET /api/productos`).

**(c) Propuesta de escritura** — `requiere_confirmacion: true`. **Nada se ha
guardado.**

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
    "parametros": {
      "producto_id": 3, "producto": "PANES DE YUCA",
      "cantidad": 20, "tipo_movimiento": "INGRESO"
    },
    "expira_en": "2026-08-03T02:34:00"
  },
  "datos": null
}
```

La app debe:
- Mostrar `accion.resumen` de forma destacada (una tarjeta, no una burbuja más).
- Ofrecer **Confirmar** y **Cancelar**.
- Dejar claro que todavía no se ha guardado.
- Deshabilitar los botones pasada `expira_en` (30 minutos).

Valores posibles de `accion.tipo`: `registrar_movimiento`, `crear_producto`,
`agregar_alias`, `retirar_producto`, `eliminar_movimiento`. Los dos últimos solo
le llegarán a un administrador. Útiles para elegir el icono de la tarjeta.

### 5.3 Confirmar o cancelar

```http
POST /api/conversar/confirmar
Authorization: Bearer <token>
Content-Type: application/json

{"id_accion": 3, "confirmar": true}
```

Responde con el mismo objeto de §5.2, ya con el resultado:

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

**Es inmediato** (no pasa por la IA): responde en milisegundos.

Con `"confirmar": false` responde «Vale, lo dejo así. No he guardado nada.».

Errores:

| Código | Cuándo | Qué hacer |
|---|---|---|
| 404 | La acción no existe | Quitar la tarjeta |
| 403 | La propuso otra persona, o falta permiso | Mostrar `detail` |
| 409 | Ya se confirmó, se canceló o caducó | Mostrar `detail` y quitar los botones |

El 409 por doble confirmación es la red de seguridad ante un doble toque: **no
registra dos veces**.

### 5.4 Confirmar hablando

Un «sí» o un «dale» escrito o dictado en el chat hace exactamente lo mismo que
el botón. El backend lo detecta y **responde al instante, sin pasar por la IA**.

Reconoce, entre otros: `sí`, `dale`, `ok`, `listo`, `confirmo`, `correcto`,
`hazlo`, `de una`, `eso es` — y del lado del no: `no`, `cancela`, `déjalo`,
`olvídalo`, `mejor no`.

Solo funciona con **frases de tres palabras o menos**. «No, mejor registra 30
arepas» no se toma como negación, sino como una petición nueva, que es lo
correcto.

Consecuencia para la app: **los dos caminos deben estar siempre disponibles a la
vez.** Por voz nadie pulsa un botón; con el móvil en la mano nadie quiere
dictar.

### 5.5 Historial de conversaciones

Vive en el servidor, así que se ve desde cualquier dispositivo y sobrevive a
reinstalar la app.

```http
GET  /api/conversaciones?limit=30      → lista de hilos, más reciente primero
GET  /api/conversaciones/{id}          → el hilo con todos sus mensajes
DELETE /api/conversaciones/{id}        → borra el hilo
```

`GET /api/conversaciones/{id}` devuelve:
```json
{
  "id": 7,
  "titulo": "llegaron 20 panes de yuca",
  "creada_en": "2026-08-03T01:30:00",
  "actualizada_en": "2026-08-03T02:04:00",
  "mensajes": [
    {"id": 1, "rol": "usuario", "contenido": "llegaron 20 panes de yuca",
     "creado_en": "2026-08-03T01:30:00"},
    {"id": 2, "rol": "asistente", "contenido": "Registrar una entrada...",
     "creado_en": "2026-08-03T01:30:22"}
  ]
}
```

`rol` es `"usuario"` o `"asistente"` — determina el lado de la burbuja.

El título se genera solo con el primer mensaje. Cada persona ve **solo sus
propias conversaciones**, también los administradores.

Borrar un hilo **no borra los movimientos** que se registraron desde él: son
hechos del negocio, no parte de una charla. Conviene decirlo en el diálogo de
confirmación para que nadie crea que está deshaciendo el inventario.

---

## 6. Voz

### 6.1 Dictado (voz → texto)

Usar el `SpeechRecognizer` nativo de Android. **La transcripción ocurre en el
móvil**; al backend se le manda el texto ya transcrito. El servidor no recibe
audio en ningún momento.

```kotlin
val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
    putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL,
             RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
    // Español de Colombia: el negocio es colombiano y el vocabulario
    // ('arepas', 'empanadas', 'panes de yuca') se reconoce bastante mejor
    // que con es-ES.
    putExtra(RecognizerIntent.EXTRA_LANGUAGE, "es-CO")
    putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
}
```

Requiere el permiso `RECORD_AUDIO`, solicitado en tiempo de ejecución.

**Muy recomendable: mostrar la transcripción en el campo de texto antes de
enviar, y dejar corregirla.** El reconocimiento de voz falla, y es mucho más
barato arreglar una palabra que descubrir el error tras 25 segundos de espera.

### 6.2 Respuesta hablada (texto → voz), opcional

`TextToSpeech` con `Locale("es", "CO")`. Si se implementa, que sea desactivable:
leer en voz alta un listado de stock de quince productos es insufrible.

Sugerencia: leer solo las respuestas cortas y los resúmenes de confirmación —
que es cuando de verdad ayuda tener las manos libres.

---

## 7. Diseñar la espera (importante)

Cada turno de chat tarda **15-30 segundos**. Medido en la Raspberry Pi 5 con
qwen2.5:3b. No es optimizable desde la app y no va a mejorar: es inferencia en
CPU ARM.

Qué hacer:

- **Mostrar la burbuja del usuario inmediatamente**, antes de la petición.
- Indicador de «SivIA está pensando…» con animación, no un spinner congelado.
- **No bloquear la pantalla.** Que se pueda seguir leyendo el historial.
- Permitir cancelar la petición.
- **Nunca reintentar automáticamente.** Un reintento son otros 25 segundos y
  duplica la propuesta.

Qué **no** hacer:

- Un diálogo modal de carga. Media hora de uso son varios minutos mirando una
  pantalla bloqueada.
- Prometer un tiempo concreto («tardará 5 segundos»).

Lo que **sí** es instantáneo, y conviene que se note la diferencia:
`/api/conversar/confirmar`, y todos los endpoints de §8.

---

## 8. Endpoints REST directos

Para las pantallas que no son el chat. Ninguno pasa por la IA; todos responden
en milisegundos.

| Método | Ruta | Auth | Descripción |
|---|---|:--:|---|
| `GET` | `/health` | — | Estado de MySQL y Ollama. Útil en una pantalla de diagnóstico |
| `GET` | `/api/stock` | — | Stock por producto |
| `GET` | `/api/movimientos` | — | Histórico paginado (`limit`, `offset`, `producto`) |
| `GET` | `/api/productos` | — | Catálogo con sus alias |
| `POST` | `/api/productos` | 🔒 | Alta de producto |
| `POST` | `/api/productos/{id}/alias` | 🔒 | Añadir una variante |
| `DELETE` | `/api/movimientos/{id}` | 🔒👑 | Borrar un movimiento |
| `PATCH` | `/api/productos/{id}?activo=` | 🔒👑 | Retirar o reincorporar |
| `DELETE` | `/api/productos/{id}` | 🔒👑 | Retirar (baja lógica) |
| `GET` | `/api/usuarios` | 🔒👑 | Cuentas del sistema |
| `POST` | `/api/usuarios` | 🔒👑 | Crear cuenta |
| `PATCH` | `/api/usuarios/{id}?rol=&activo=` | 🔒👑 | Cambiar rol o estado |
| `POST` | `/api/usuarios/{id}/clave` | 🔒 | Cambiar contraseña |

🔒 = requiere token · 👑 = requiere rol `admin`

El stock, el histórico y el catálogo son **lectura pública**: no necesitan token.
Saber qué nombres acepta el sistema no es información sensible. La lista de
usuarios sí lo es, y por eso pide administrador.

### 8.1 Formatos

`GET /api/stock`:
```json
[{"producto": "AREPAS", "stock_actual": 29, "total_ingresos": 46,
  "total_egresos": 17, "movimientos": 4,
  "ultimo_movimiento": "2026-07-28T15:11:26"}]
```

`GET /api/movimientos`:
```json
[{"id": 13, "producto": "PANES DE YUCA", "cantidad": 20,
  "tipo_movimiento": "INGRESO", "fecha": "2026-08-03T02:04:11",
  "autor": "martha"}]
```

`autor` puede ser `null` en los movimientos anteriores a que existieran las
cuentas y en los registrados por scripts. Mostrar «—» o dejarlo en blanco; no
inventar un autor.

`GET /api/productos`:
```json
[{"id": 3, "nombre": "PANES DE YUCA", "activo": true,
  "creado_en": "2026-07-28T15:10:49", "alias": ["PAN DE YUCA"]}]
```

### 8.2 Cambiar la propia contraseña

```http
POST /api/usuarios/{mi_id}/clave
{"clave_actual": "la de ahora", "clave_nueva": "la nueva"}
```

`clave_actual` es **obligatoria** al cambiar la propia (401 si no coincide). Un
administrador que restablece la de otra persona la omite.

**Al cambiarla se cierran todas las sesiones de esa cuenta**, incluida la que
hizo el cambio. La app debe llevar al login inmediatamente después, con un
mensaje que lo explique.

### 8.3 Las fechas son UTC

Todas las fechas llegan en ISO 8601 **sin zona horaria** (`2026-08-03T02:04:11`)
y están en **UTC**, porque MySQL `DATETIME` no almacena zona.

**La app debe interpretarlas como UTC y convertirlas a la hora local**, o
mostrará todo desfasado 5 horas en Colombia.

```kotlin
val utc = LocalDateTime.parse("2026-08-03T02:04:11").atOffset(ZoneOffset.UTC)
val local = utc.atZoneSameInstant(ZoneId.systemDefault())
```

---

## 9. Errores

| Código | Significado | Reacción |
|---|---|---|
| 401 | Sesión inválida o caducada | Borrar token → login. **Sin reintentar** |
| 403 | Sin permiso para eso | Mostrar `detail` |
| 404 | No existe | Refrescar la pantalla |
| 409 | Conflicto (duplicado, ya confirmado, caducado) | Mostrar `detail` |
| 422 | Datos inválidos | Mostrar `detail` |
| 502/503 | Ollama no responde | «SivIA no está disponible ahora mismo» + reintentar a mano |
| 504 | La IA tardó demasiado | «Tardó demasiado, inténtalo otra vez» |
| Sin red | Tailscale desconectado | **Mensaje específico**: ver abajo |

Todos los errores traen `{"detail": "mensaje en español"}`. Los mensajes están
redactados para que los lea una persona: **mostrarlos tal cual** en vez de
sustituirlos por un genérico.

**El fallo de red merece trato aparte.** La causa más probable con diferencia no
es que el servidor esté caído, sino que Tailscale no esté conectado en el móvil.
Un «error de conexión» genérico manda a la gente a reiniciar la app cuando lo
que hay que hacer es abrir Tailscale. Sugerido:

> **No se pudo conectar con SivIA.**
> Comprueba que Tailscale esté activo en este dispositivo.
> [Abrir Tailscale]

---

## 10. Pantallas

**1 · Login** — usuario, contraseña, entrar. Recordar sesión (12 h). Si falla la
conexión, el aviso de Tailscale de §9.

**2 · Chat** (principal) — lista de burbujas, campo de texto, botón de micrófono
prominente, tarjetas de confirmación intercaladas, indicador de «pensando…»,
acceso al historial de hilos.

**3 · Stock** — `GET /api/stock`. Lista con el saldo por producto, buscador,
tirar para refrescar.

**4 · Movimientos** — `GET /api/movimientos` paginado. Filtro por producto,
fecha en local, autor. Deslizar para borrar **solo si `rol == "admin"`**.

**5 · Catálogo** — `GET /api/productos` con sus alias. Alta de producto. Retirar
solo para administradores.

**6 · Usuarios** — solo administradores. Listar, crear, cambiar rol, activar y
desactivar.

**7 · Ajustes** — quién soy, cambiar contraseña, cerrar sesión, estado del
servidor (`/health`), voz activada o no.

---

## 11. Identidad de la app

**Esto no es negociable ni hay que deducirlo.** Android Studio propone un
`applicationId` derivado del dominio de quien crea el proyecto; **ignóralo** y usa
exactamente este:

```kotlin
// app/build.gradle.kts
android {
    namespace = "com.sivia.app"
    defaultConfig {
        applicationId = "com.sivia.app"
        // ...
    }
}
```

| Campo | Valor |
|---|---|
| `applicationId` / `namespace` | `com.sivia.app` |
| Nombre visible (`app_name`) | `SivIA` |
| Estructura de paquetes | `com.sivia.app.ui`, `com.sivia.app.data`, `com.sivia.app.network` |

SivIA es un proyecto independiente. **No debe llevar el nombre, el dominio ni la
marca de ninguna empresa** en el paquete, en el nombre visible, en los recursos
ni en los metadatos del manifiesto.

El `applicationId` es la identidad permanente de la app para Android: cambiarlo
después de instalarla equivale a una app distinta, y obliga a desinstalar y
reinstalar perdiendo la sesión guardada. Conviene acertar a la primera.

---

## 12. Stack sugerido

No es obligatorio, pero encaja bien con lo que hay:

- **Kotlin + Jetpack Compose**
- **Retrofit + OkHttp** (con los timeouts de §2.2) + **Moshi** o **kotlinx.serialization**
- **ViewModel + StateFlow**
- **DataStore cifrado** o `EncryptedSharedPreferences` para el token
- **Room**, opcional, para cachear el último stock y que la app abra con algo
  que mostrar aunque Tailscale tarde en levantar
- `SpeechRecognizer` y `TextToSpeech` nativos — sin dependencias externas ni
  servicios en la nube

Un interceptor de OkHttp que añada el `Authorization` y detecte el 401 de forma
centralizada ahorra repetir esa lógica en cada pantalla.

---

## 13. Lista de comprobación

Funcionalidad:
- [ ] Login, sesión persistida, arranque con `GET /api/auth/yo`
- [ ] Chat con historial servidor, hilos, y creación de hilo nuevo
- [ ] Tarjeta de confirmación con Confirmar / Cancelar
- [ ] Confirmar diciendo «sí» o «no» en el chat
- [ ] Dictado por voz con transcripción editable antes de enviar
- [ ] Pantallas de stock, movimientos y catálogo
- [ ] Gestión de usuarios visible solo para administradores
- [ ] Cambio de contraseña con vuelta al login

Los que se olvidan y duelen después:
- [ ] `applicationId = "com.sivia.app"` — **no** el que proponga Android Studio
      por defecto a partir del dominio de quien crea el proyecto (§11)
- [ ] `network_security_config.xml` (sin él **no conecta**)
- [ ] `readTimeout` de 120 s (sin él **todo mensaje falla**)
- [ ] Fechas convertidas de UTC a local
- [ ] Mensaje específico de Tailscale ante un fallo de red
- [ ] Nada de reintentos automáticos en `/api/conversar`
- [ ] Un 403 se maneja aunque el botón esté oculto
- [ ] Botones de confirmación deshabilitados tras `expira_en`

Probar a mano:
- [ ] Con Tailscale **desconectado** → sale el aviso correcto
- [ ] Doble toque en Confirmar → 409, y **no** se registra dos veces
- [ ] Sesión de Martha → sin botones de borrar, y un 403 forzado se muestra bien
- [ ] Girar la pantalla mientras SivIA piensa → la respuesta no se pierde
- [ ] Matar la app y reabrirla → el hilo se recupera del servidor

---

## 14. Cómo probar contra el servidor real

Con Tailscale conectado, desde cualquier terminal:

```bash
# ¿Hay servidor?
curl http://100.110.69.36:8000/health

# Entrar
TOKEN=$(curl -s -X POST http://100.110.69.36:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"usuario":"martha","clave":"..."}' | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")

# Hablar
curl -X POST http://100.110.69.36:8000/api/conversar \
  -H "Content-Type: application/json" -H "Authorization: Bearer $TOKEN" \
  -d '{"texto":"cuántas arepas tengo"}'

# Confirmar una propuesta
curl -X POST http://100.110.69.36:8000/api/conversar/confirmar \
  -H "Content-Type: application/json" -H "Authorization: Bearer $TOKEN" \
  -d '{"id_accion":1,"confirmar":true}'
```

**Documentación interactiva de la API**, con todos los esquemas y un botón para
probar cada endpoint: <http://100.110.69.36:8000/docs>

---

## 15. Límites conocidos

Cosas que el backend **no** hace hoy. Conviene saberlas para no diseñar
pantallas que prometan lo que no hay:

- **No hay unidades de medida.** La cantidad es un entero pelado, así que
  «2 cajas de arepas» y «2 arepas» son indistinguibles.
- **El stock puede quedar negativo.** Nada impide registrar una salida mayor que
  las existencias. La app puede avisarlo visualmente (en rojo), pero el servidor
  lo permite.
- **No se corrige un movimiento, se borra y se rehace.** No hay endpoint de
  edición.
- **El modelo se equivoca a veces**, y por eso existe la confirmación. Un caso
  conocido: «vendimos 3 chicharrones con arepa» puede interpretarse como
  `AREPAS`, porque la frase contiene esa palabra. La persona lo ve en el resumen
  y cancela — **razón de más para que el resumen de la tarjeta se lea bien y no
  esté escondido en letra pequeña**.
- **Un solo idioma**, español.
- **Sin notificaciones push.** No hay nada que empuje desde el servidor.
