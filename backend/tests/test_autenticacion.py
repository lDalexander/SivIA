"""
Tests de autenticación, sesiones y permisos por rol.

Lo que se comprueba aquí no es que el login «funcione», sino las propiedades de
las que depende que el multiusuario signifique algo:

  - Que un operador **no pueda** eliminar, y que el rechazo sea 403 y no un 401
    confuso ni, mucho peor, un 200.
  - Que revocar el acceso surta efecto **inmediato**: desactivar una cuenta o
    cambiar una contraseña invalida las sesiones abiertas, sin esperar a que
    caduquen.
  - Que no se pueda dejar el sistema sin ningún administrador.
  - Que la API key siga funcionando, porque los scripts de operación dependen
    de ella y romperlos en silencio sería peor que no tener usuarios.
"""
from datetime import timedelta

import pytest

from conftest import API_KEY_PRUEBA, CLAVE_DE_PRUEBA
from models import Sesion, ahora_utc


# --- LOGIN ---

def test_login_devuelve_token_y_rol(cliente):
    r = cliente.post(
        "/api/auth/login", json={"usuario": "john", "clave": CLAVE_DE_PRUEBA}
    )
    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["token"]
    assert cuerpo["usuario"]["usuario"] == "john"
    # La app necesita el rol de inmediato para decidir qué botones enseñar.
    assert cuerpo["usuario"]["rol"] == "admin"


def test_login_acepta_el_usuario_en_mayusculas(cliente):
    """En el teclado de un móvil la primera letra sale en mayúscula sola."""
    r = cliente.post(
        "/api/auth/login", json={"usuario": "  John  ", "clave": CLAVE_DE_PRUEBA}
    )
    assert r.status_code == 200


def test_login_con_clave_incorrecta(cliente):
    r = cliente.post("/api/auth/login", json={"usuario": "john", "clave": "otra-cosa"})
    assert r.status_code == 401


def test_login_no_revela_si_el_usuario_existe(cliente):
    """El mensaje debe ser idéntico exista o no la cuenta.

    Si difiriera, cualquiera podría enumerar quién trabaja en el negocio
    probando nombres contra el endpoint.
    """
    inexistente = cliente.post(
        "/api/auth/login", json={"usuario": "fulanito", "clave": "loquesea"}
    )
    mala_clave = cliente.post(
        "/api/auth/login", json={"usuario": "john", "clave": "loquesea"}
    )
    assert inexistente.status_code == mala_clave.status_code == 401
    assert inexistente.json()["detail"] == mala_clave.json()["detail"]


def test_login_de_usuario_desactivado(cliente, sesion_db, usuario_db):
    usuario = usuario_db("martha")
    usuario.activo = False
    sesion_db.commit()

    r = cliente.post(
        "/api/auth/login", json={"usuario": "martha", "clave": CLAVE_DE_PRUEBA}
    )
    assert r.status_code == 401


def test_login_actualiza_el_ultimo_acceso(cliente, sesion_db, usuario_db):
    assert usuario_db("john").ultimo_acceso is None
    cliente.post("/api/auth/login", json={"usuario": "john", "clave": CLAVE_DE_PRUEBA})
    sesion_db.expire_all()
    assert usuario_db("john").ultimo_acceso is not None


# --- EL TOKEN COMO CREDENCIAL ---

def test_yo_devuelve_la_identidad(cliente, auth_operador):
    r = cliente.get("/api/auth/yo", headers=auth_operador)
    assert r.status_code == 200
    assert r.json()["usuario"] == "martha"
    assert r.json()["rol"] == "operador"


def test_yo_sin_credenciales(cliente):
    assert cliente.get("/api/auth/yo").status_code == 401


def test_token_invalido_no_autentica(cliente):
    r = cliente.get("/api/auth/yo", headers={"Authorization": "Bearer inventado"})
    assert r.status_code == 401


def test_token_sin_el_prefijo_bearer(cliente, sesion_db, usuario_db):
    """Se tolera el token pelado: es un error frecuente al montar el cliente,
    y un 401 sin explicación es difícil de diagnosticar desde un móvil."""
    import main
    token = "token-pelado"
    sesion_db.add(
        Sesion(
            token_hash=main._hash_token(token),
            usuario_id=usuario_db("john").id,
            expira_en=ahora_utc() + timedelta(hours=1),
        )
    )
    sesion_db.commit()

    assert cliente.get("/api/auth/yo", headers={"Authorization": token}).status_code == 200


def test_token_caducado_no_vale(cliente, sesion_db, usuario_db):
    import main
    token = "token-caducado"
    sesion_db.add(
        Sesion(
            token_hash=main._hash_token(token),
            usuario_id=usuario_db("john").id,
            expira_en=ahora_utc() - timedelta(minutes=1),
        )
    )
    sesion_db.commit()

    r = cliente.get("/api/auth/yo", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_logout_revoca_la_sesion(cliente, auth_admin):
    assert cliente.get("/api/auth/yo", headers=auth_admin).status_code == 200
    assert cliente.post("/api/auth/logout", headers=auth_admin).status_code == 200
    assert cliente.get("/api/auth/yo", headers=auth_admin).status_code == 401


def test_logout_sin_sesion_no_falla(cliente):
    """Cerrar sesión nunca debe fallar, o la app se queda atrapada con un token roto."""
    assert cliente.post("/api/auth/logout").status_code == 200


def test_desactivar_un_usuario_corta_su_sesion_al_momento(
    cliente, auth_admin, auth_operador, usuario_db
):
    assert cliente.get("/api/auth/yo", headers=auth_operador).status_code == 200

    r = cliente.patch(
        f"/api/usuarios/{usuario_db('martha').id}?activo=false", headers=auth_admin
    )
    assert r.status_code == 200

    # Sin la revocación explícita, su token seguiría valiendo hasta 12 h.
    assert cliente.get("/api/auth/yo", headers=auth_operador).status_code == 401


# --- PERMISOS POR ROL ---

def test_operador_no_puede_eliminar_un_movimiento(
    cliente, auth_operador, crear_movimiento
):
    movimiento = crear_movimiento("AREPAS", 10, "INGRESO")
    r = cliente.delete(f"/api/movimientos/{movimiento.id}", headers=auth_operador)
    assert r.status_code == 403


def test_el_movimiento_sigue_ahi_tras_el_403(
    cliente, auth_operador, crear_movimiento, sesion_db
):
    """Un 403 debe significar que no pasó nada, no solo que la respuesta fue fea."""
    from models import MovimientoInventario

    movimiento = crear_movimiento("AREPAS", 10, "INGRESO")
    cliente.delete(f"/api/movimientos/{movimiento.id}", headers=auth_operador)
    assert sesion_db.get(MovimientoInventario, movimiento.id) is not None


def test_admin_si_puede_eliminar_un_movimiento(cliente, auth_admin, crear_movimiento):
    movimiento = crear_movimiento("AREPAS", 10, "INGRESO")
    r = cliente.delete(f"/api/movimientos/{movimiento.id}", headers=auth_admin)
    assert r.status_code == 200


def test_operador_no_puede_retirar_un_producto(cliente, auth_operador, id_de):
    r = cliente.delete(f"/api/productos/{id_de('QUESO')}", headers=auth_operador)
    assert r.status_code == 403


def test_operador_si_puede_registrar_y_crear(cliente, auth_operador):
    """El operador no está capado para trabajar: solo para destruir."""
    r = cliente.post(
        "/api/productos", json={"nombre": "MORCILLA"}, headers=auth_operador
    )
    assert r.status_code == 201


def test_operador_no_puede_listar_usuarios(cliente, auth_operador):
    """Quién tiene acceso al sistema no es lectura pública, al revés que el catálogo."""
    assert cliente.get("/api/usuarios", headers=auth_operador).status_code == 403


# --- LA API KEY SIGUE SIENDO VÁLIDA ---

def test_la_api_key_equivale_a_administrador(cliente, auth, crear_movimiento):
    """Los scripts de operación dependen de ella; romperlos sería peor que no
    tener usuarios."""
    movimiento = crear_movimiento("AREPAS", 5, "EGRESO")
    assert cliente.delete(f"/api/movimientos/{movimiento.id}", headers=auth).status_code == 200


def test_la_api_key_no_es_una_persona(cliente, auth, simular_llm, sesion_db):
    """Lo registrado con la API key queda sin autor, y eso es información cierta:
    ningún usuario concreto lo hizo."""
    from models import MovimientoInventario

    simular_llm({"producto": "AREPAS", "cantidad": 12, "tipo_movimiento": "INGRESO"})
    r = cliente.post(
        "/api/procesar-comando", json={"texto": "llegaron 12 arepas"}, headers=auth
    )
    assert r.status_code == 200

    movimiento = sesion_db.get(
        MovimientoInventario, r.json()["datos_extraidos"]["id_registro"]
    )
    assert movimiento.usuario_id is None
    assert movimiento.autor is None


def test_la_api_key_no_sirve_para_el_chat(cliente, auth):
    """Una conversación pertenece a una persona; la de «la API key» no sería de nadie."""
    r = cliente.post("/api/conversar", json={"texto": "hola"}, headers=auth)
    assert r.status_code == 403


def test_el_movimiento_registra_a_su_autor(cliente, auth_operador, simular_llm, sesion_db):
    from models import MovimientoInventario

    simular_llm({"producto": "AREPAS", "cantidad": 7, "tipo_movimiento": "EGRESO"})
    r = cliente.post(
        "/api/procesar-comando",
        json={"texto": "vendimos 7 arepas"},
        headers=auth_operador,
    )
    assert r.status_code == 200

    movimiento = sesion_db.get(
        MovimientoInventario, r.json()["datos_extraidos"]["id_registro"]
    )
    assert movimiento.autor == "martha"


# --- GESTIÓN DE USUARIOS ---

def test_crear_usuario_y_entrar_con_el(cliente, auth_admin):
    r = cliente.post(
        "/api/usuarios",
        json={
            "usuario": "Jhonnathan",
            "nombre": "Jhonnathan Ortega",
            "clave": "otra-clave-larga",
            "rol": "admin",
        },
        headers=auth_admin,
    )
    assert r.status_code == 201
    assert r.json()["usuario"] == "jhonnathan"

    entrada = cliente.post(
        "/api/auth/login",
        json={"usuario": "jhonnathan", "clave": "otra-clave-larga"},
    )
    assert entrada.status_code == 200


def test_no_se_devuelve_nunca_el_hash_de_la_clave(cliente, auth_admin):
    r = cliente.get("/api/usuarios", headers=auth_admin)
    assert r.status_code == 200
    for usuario in r.json():
        assert "hash_clave" not in usuario
        assert "clave" not in usuario


def test_usuario_duplicado(cliente, auth_admin):
    r = cliente.post(
        "/api/usuarios",
        json={"usuario": "john", "nombre": "Otro John", "clave": "clave-larga-1"},
        headers=auth_admin,
    )
    assert r.status_code == 409


def test_rol_invalido(cliente, auth_admin):
    r = cliente.post(
        "/api/usuarios",
        json={
            "usuario": "pepe",
            "nombre": "Pepe",
            "clave": "clave-larga-1",
            "rol": "superusuario",
        },
        headers=auth_admin,
    )
    assert r.status_code == 422


def test_clave_demasiado_corta(cliente, auth_admin):
    r = cliente.post(
        "/api/usuarios",
        json={"usuario": "pepe", "nombre": "Pepe", "clave": "corta"},
        headers=auth_admin,
    )
    assert r.status_code == 422


def test_no_se_puede_dejar_el_sistema_sin_administradores(
    cliente, auth_admin, usuario_db
):
    """John es el único admin de la base de prueba. Degradarlo dejaría el sistema
    sin nadie capaz de volver a promover a nadie."""
    r = cliente.patch(
        f"/api/usuarios/{usuario_db('john').id}?rol=operador", headers=auth_admin
    )
    assert r.status_code == 409


def test_se_puede_degradar_si_queda_otro_administrador(
    cliente, auth_admin, usuario_db
):
    cliente.post(
        "/api/usuarios",
        json={
            "usuario": "jhonnathan",
            "nombre": "Jhonnathan Ortega",
            "clave": "clave-larga-2",
            "rol": "admin",
        },
        headers=auth_admin,
    )
    r = cliente.patch(
        f"/api/usuarios/{usuario_db('john').id}?rol=operador", headers=auth_admin
    )
    assert r.status_code == 200
    assert r.json()["rol"] == "operador"


def test_desactivar_al_unico_administrador_tambien_se_rechaza(
    cliente, auth_admin, usuario_db
):
    """La comprobación mira el estado resultante, no solo el cambio de rol."""
    r = cliente.patch(
        f"/api/usuarios/{usuario_db('john').id}?activo=false", headers=auth_admin
    )
    assert r.status_code == 409


def test_promover_a_un_operador(cliente, auth_admin, auth_operador, usuario_db):
    assert cliente.get("/api/usuarios", headers=auth_operador).status_code == 403

    r = cliente.patch(
        f"/api/usuarios/{usuario_db('martha').id}?rol=admin", headers=auth_admin
    )
    assert r.status_code == 200
    # El rol se lee de la base en cada petición, así que el cambio es inmediato
    # sin volver a iniciar sesión.
    assert cliente.get("/api/usuarios", headers=auth_operador).status_code == 200


# --- CAMBIO DE CONTRASEÑA ---

def test_cambiar_la_propia_clave(cliente, auth_operador, usuario_db):
    r = cliente.post(
        f"/api/usuarios/{usuario_db('martha').id}/clave",
        json={"clave_actual": CLAVE_DE_PRUEBA, "clave_nueva": "clave-nueva-martha"},
        headers=auth_operador,
    )
    assert r.status_code == 200
    assert cliente.post(
        "/api/auth/login",
        json={"usuario": "martha", "clave": "clave-nueva-martha"},
    ).status_code == 200


def test_cambiar_la_propia_clave_exige_la_actual(cliente, auth_operador, usuario_db):
    """Sin este requisito, un móvil desbloqueado encima del mostrador basta para
    secuestrar la cuenta de forma permanente."""
    r = cliente.post(
        f"/api/usuarios/{usuario_db('martha').id}/clave",
        json={"clave_nueva": "clave-nueva-martha"},
        headers=auth_operador,
    )
    assert r.status_code == 401


def test_un_admin_restablece_la_clave_de_otro_sin_conocerla(
    cliente, auth_admin, usuario_db
):
    r = cliente.post(
        f"/api/usuarios/{usuario_db('martha').id}/clave",
        json={"clave_nueva": "clave-restablecida"},
        headers=auth_admin,
    )
    assert r.status_code == 200


def test_un_operador_no_puede_cambiar_la_clave_de_otro(
    cliente, auth_operador, usuario_db
):
    r = cliente.post(
        f"/api/usuarios/{usuario_db('john').id}/clave",
        json={"clave_nueva": "secuestrada"},
        headers=auth_operador,
    )
    assert r.status_code == 403


def test_cambiar_la_clave_revoca_las_sesiones_abiertas(
    cliente, auth_admin, auth_operador, usuario_db
):
    """Si se cambia porque se sospecha que alguien la sabía, dejar sus sesiones
    abiertas no arreglaría nada."""
    assert cliente.get("/api/auth/yo", headers=auth_operador).status_code == 200

    cliente.post(
        f"/api/usuarios/{usuario_db('martha').id}/clave",
        json={"clave_nueva": "clave-restablecida"},
        headers=auth_admin,
    )
    assert cliente.get("/api/auth/yo", headers=auth_operador).status_code == 401
