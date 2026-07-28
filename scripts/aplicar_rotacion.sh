#!/usr/bin/env bash
# =========================================================
#  Rotación de credenciales de MySQL + endurecimiento del esquema.
#
#  Uso (una sola vez, desde /home/admin/var/www/SivIA):
#      bash scripts/aplicar_rotacion.sh
#
#  Es idempotente: se puede volver a ejecutar sin efectos adversos.
#  Lee las credenciales nuevas de .env y sustituye las antiguas dentro de
#  MySQL, que no se actualizan solas porque las variables del compose únicamente
#  se aplican en la primera inicialización del volumen.
# =========================================================
set -euo pipefail
cd "$(dirname "$0")/.."

# --- Credenciales nuevas (desde .env) ---
set -a; source ./.env; set +a
: "${MYSQL_ROOT_PASSWORD:?falta en .env}"
: "${MYSQL_PASSWORD:?falta en .env}"
: "${MYSQL_USER:?falta en .env}"
: "${MYSQL_DATABASE:?falta en .env}"

# Contraseña de root vigente en el volumen, si aún no coincide con la del .env.
# Se pasa por entorno y nunca se escribe en el repositorio:
#     PASS_ROOT_ANTERIOR='la_de_antes' bash scripts/aplicar_rotacion.sh
PASS_ROOT_ANTERIOR="${PASS_ROOT_ANTERIOR:-}"

echo "==> [1/5] Determinando la contraseña de root vigente"
if docker exec -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" sivia_db \
     mysql -uroot -e "SELECT 1" >/dev/null 2>&1; then
  PASS_ACTUAL="$MYSQL_ROOT_PASSWORD"
  echo "    La contraseña del .env ya está vigente. Continuando (idempotente)."
elif [ -n "$PASS_ROOT_ANTERIOR" ] && docker exec -e MYSQL_PWD="$PASS_ROOT_ANTERIOR" sivia_db \
       mysql -uroot -e "SELECT 1" >/dev/null 2>&1; then
  PASS_ACTUAL="$PASS_ROOT_ANTERIOR"
  echo "    Contraseña anterior aceptada. Se procede a rotar."
else
  echo "ERROR: no se pudo autenticar contra MySQL con la contraseña de .env."
  echo "       Si el volumen aún tiene la contraseña antigua, reejecutar como:"
  echo "         PASS_ROOT_ANTERIOR='la_de_antes' bash scripts/aplicar_rotacion.sh"
  exit 1
fi

ejecutar_sql() {
  docker exec -i -e MYSQL_PWD="$PASS_ACTUAL" sivia_db mysql -uroot --batch "$@"
}

echo "==> [2/5] Rotando contraseñas de root y de la aplicación"
ejecutar_sql <<SQL
ALTER USER IF EXISTS 'root'@'localhost'      IDENTIFIED BY '${MYSQL_ROOT_PASSWORD}';
ALTER USER IF EXISTS 'root'@'%'              IDENTIFIED BY '${MYSQL_ROOT_PASSWORD}';
ALTER USER IF EXISTS '${MYSQL_USER}'@'%'     IDENTIFIED BY '${MYSQL_PASSWORD}';
ALTER USER IF EXISTS '${MYSQL_USER}'@'localhost' IDENTIFIED BY '${MYSQL_PASSWORD}';
FLUSH PRIVILEGES;
SQL
PASS_ACTUAL="$MYSQL_ROOT_PASSWORD"
echo "    Contraseñas rotadas."

echo "==> [3/5] Aplicando constraints e índice al esquema"
# create_all() no altera tablas que ya existen, así que las constraints del
# modelo se aplican aquí sobre el volumen ya inicializado. Cada ALTER se tolera
# como fallo si ya estaba aplicado, para que el script sea repetible.
aplicar_opcional() {
  local etiqueta="$1"; shift
  if ejecutar_sql "$MYSQL_DATABASE" -e "$1" 2>/dev/null; then
    echo "    [ok]      $etiqueta"
  else
    echo "    [omitido] $etiqueta (ya aplicado)"
  fi
}
aplicar_opcional "CHECK cantidad > 0" \
  "ALTER TABLE movimientos_inventario ADD CONSTRAINT ck_cantidad_positiva CHECK (cantidad > 0);"
aplicar_opcional "CHECK tipo_movimiento" \
  "ALTER TABLE movimientos_inventario ADD CONSTRAINT ck_tipo_movimiento_valido CHECK (tipo_movimiento IN ('INGRESO','EGRESO'));"
aplicar_opcional "fecha NOT NULL" \
  "ALTER TABLE movimientos_inventario MODIFY fecha DATETIME NOT NULL;"
aplicar_opcional "índice en fecha" \
  "CREATE INDEX ix_movimientos_inventario_fecha ON movimientos_inventario (fecha);"

echo "==> [4/5] Recreando los contenedores con la configuración nueva"
docker compose up -d

echo "==> [5/5] Esperando a que los servicios queden saludables"
for i in $(seq 1 60); do
  if curl -sf -m 3 http://127.0.0.1:8000/ >/dev/null 2>&1; then
    echo "    API respondiendo tras ${i}s."
    break
  fi
  sleep 2
done

echo
echo "=================== VERIFICACIÓN ==================="
docker compose ps --format '{{.Name}}\t{{.Status}}'
echo
echo "--- Esquema resultante ---"
ejecutar_sql "$MYSQL_DATABASE" -e "SHOW CREATE TABLE movimientos_inventario\G" | sed 's/^/    /'
echo
echo "--- /health ---"
curl -s http://127.0.0.1:8000/health; echo
echo "===================================================="
echo "Rotación completada."
