"""Chequeo previo a producción: revisa la configuración REAL del entorno donde se ejecuta y dice qué falta.

    python herramientas/preflight.py            # usa las variables de entorno del proceso y la base DATABASE_URL

Ejecútelo en el servidor, con las mismas variables con que arrancará la app. Imprime [OK], [AVISO] o [FALTA] y termina con código 1 si falta algo imprescindible.
No cambia nada. Complementa (no reemplaza) la revisión independiente: solo comprueba lo que se puede medir.
"""
import os
import re
import stat
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OK, AVISO, FALTA = "OK", "AVISO", "FALTA"
RAIZ = Path(__file__).resolve().parent.parent
NOMBRES_DE_DEMO = {"admin", "administrador", "demo", "admin.demo", "test", "prueba", "root", "usuario", "user"}


def evaluar(env=None, ahora: float | None = None) -> list[tuple[str, str]]:
    env = os.environ if env is None else env
    r: list[tuple[str, str]] = []
    add = lambda nivel, texto: r.append((nivel, texto))  # noqa: E731
    g = lambda k: (env.get(k) or "").strip()  # noqa: E731

    # --- red y HTTPS ---
    url = g("PQRS_URL_PUBLICA")
    add(OK if url.startswith("https://") else FALTA, "PQRS_URL_PUBLICA con https://" if url.startswith("https://")
        else "Defina PQRS_URL_PUBLICA con https:// (sin ella no hay verificación de correo ni enlaces seguros)")
    add(OK if g("PQRS_HTTPS") == "1" else FALTA, "HSTS activo (PQRS_HTTPS=1)" if g("PQRS_HTTPS") == "1"
        else "Active PQRS_HTTPS=1 cuando el sitio ya se sirva por HTTPS")
    add(OK if g("PQRS_TRUST_PROXY") == "1" else AVISO, "PQRS_TRUST_PROXY=1 (detrás de UN proxy)" if g("PQRS_TRUST_PROXY") == "1"
        else "PQRS_TRUST_PROXY no está en 1: si hay un proxy inverso, todos los usuarios parecerán la misma IP y los límites los afectarán a todos")

    # --- cosas de desarrollo que NO deben llegar a producción ---
    if g("PQRS_PERMITIR_AVISO_INCOMPLETO"):
        add(FALTA, "PQRS_PERMITIR_AVISO_INCOMPLETO está definida: permite recibir solicitudes con el aviso de privacidad incompleto. Quítela.")
    if g("PQRS_DOCS") == "1":
        add(FALTA, "PQRS_DOCS=1 abre la documentación de la API al público. Quítela.")
    if g("PQRS_SMTP_CA_FILE"):
        add(FALTA, "PQRS_SMTP_CA_FILE está definida: es para certificados de pruebas; en producción no debe confiar en un certificado propio. Quítela.")
    if g("PQRS_ANTIBOT") == "0":
        add(FALTA, "PQRS_ANTIBOT=0 apaga la protección anti-bots del formulario público")
    if g("PQRS_SCRYPT_N") and int(g("PQRS_SCRYPT_N") or 0) < 2**17:
        add(FALTA, "PQRS_SCRYPT_N está por debajo de 2**17: las contraseñas quedan más fáciles de descifrar por fuerza bruta")
    if g("PQRS_ADMIN_PASSWORD"):
        add(AVISO, "PQRS_ADMIN_PASSWORD sigue en el entorno: solo se usa la primera vez; quítela y cambie esa clave")
    horas = float(g("PQRS_SESION_HORAS") or 8)
    add(OK if horas <= 8 else AVISO, f"Sesiones de {horas:g} h" + ("" if horas <= 8 else ": más de 8 h aumenta el riesgo si se roba un token"))

    # --- privacidad ---
    from app.privacidad import OBLIGATORIOS
    smtp_ok = all(g(k) for k in ("PQRS_SMTP_HOST", "PQRS_SMTP_USER", "PQRS_SMTP_FROM")) and bool(g("PQRS_SMTP_PASSWORD") or g("PQRS_SMTP_PASSWORD_FILE"))
    falta = [k for k in OBLIGATORIOS if not g(k)] + (["PQRS_PROVEEDOR_CORREO"] if smtp_ok and not g("PQRS_PROVEEDOR_CORREO") else [])
    add(OK if not falta else FALTA, "Aviso de privacidad completo" if not falta else "Aviso de privacidad incompleto: faltan " + ", ".join(falta))

    # --- secretos ---
    if g("PQRS_SMTP_HOST"):
        if g("PQRS_SMTP_PASSWORD_FILE"):
            add(OK, "Contraseña SMTP leída de un archivo (PQRS_SMTP_PASSWORD_FILE)")
            _permisos(Path(g("PQRS_SMTP_PASSWORD_FILE")), "archivo de la contraseña SMTP", add)
        else:
            add(AVISO, "La contraseña SMTP está en una variable de entorno: prefiera PQRS_SMTP_PASSWORD_FILE")
        if g("PQRS_SMTP_PORT") not in ("", "587"):
            add(AVISO, "PQRS_SMTP_PORT no es 587: la app usa STARTTLS (587); el puerto 465 (TLS directo) no está soportado")
    else:
        add(AVISO, "El correo no está configurado: no se podrá responder por correo ni verificar direcciones")
    sec = g("PQRS_WEBHOOK_SECRETO")
    if sec:
        add(OK if len(sec) >= 32 else FALTA, "PQRS_WEBHOOK_SECRETO fuerte (≥ 32 caracteres)" if len(sec) >= 32
            else "PQRS_WEBHOOK_SECRETO es corto: use al menos 32 caracteres aleatorios")
    add(OK if g("PQRS_SECRETO") else AVISO, "PQRS_SECRETO definido" if g("PQRS_SECRETO")
        else "PQRS_SECRETO no está definido: si ejecuta más de un proceso, los retos anti-bots de uno serán rechazados por otro")

    # --- base de datos ---
    url_bd = g("DATABASE_URL") or "sqlite:///pqrs.db"
    if url_bd.startswith("sqlite:///"):
        ruta = Path(url_bd[len("sqlite:///"):])
        add(AVISO, "Base SQLite: válida para empezar; para varios procesos o más carga use PostgreSQL")
        if ruta.exists():
            _permisos(ruta, "archivo de la base de datos", add)
            try:
                dentro = ruta.resolve().is_relative_to((RAIZ / "app" / "static").resolve())
            except OSError:
                dentro = False
            add(FALTA if dentro else OK, "La base está dentro de la carpeta pública" if dentro else "La base no está en una carpeta que se sirva al público")
    else:
        add(OK, "Base de datos de servidor (no SQLite)")
    _usuarios(add, url_bd)

    # --- operación: respaldos y retención ---
    rdir = g("PQRS_RESPALDO_DIR")
    ahora = ahora or time.time()
    if rdir:
        archivos = sorted(Path(rdir).glob("pqrs-*.zip*")) if Path(rdir).exists() else []
        archivos = [a for a in archivos if not a.name.endswith(".sha256")]
        if not archivos:
            add(FALTA, f"No hay respaldos en {rdir}")
        else:
            horas_r = (ahora - max(a.stat().st_mtime for a in archivos)) / 3600
            add(OK if horas_r <= 36 else FALTA, f"Último respaldo hace {horas_r:.0f} h" + ("" if horas_r <= 36 else " (más de 36 h: ¿se programó?)"))
    else:
        add(FALTA, "Defina PQRS_RESPALDO_DIR (carpeta de respaldos) para que el monitoreo pueda comprobar que existen copias recientes")

    # --- dependencias ---
    req = RAIZ / "requirements.txt"
    pinned = req.exists() and "--hash=sha256:" in req.read_text(encoding="utf-8")
    add(OK if pinned else FALTA, "requirements.txt con versiones y hashes fijos" if pinned else "requirements.txt sin hashes: genere uno con pip-compile --generate-hashes")
    if (RAIZ / ".env").exists():
        add(AVISO, "Hay un archivo .env en la carpeta del proyecto: no lo suba al repositorio ni a una carpeta pública")
    return r


def _permisos(ruta: Path, que: str, add) -> None:
    if os.name == "nt":
        add(AVISO, f"Windows: compruebe a mano que solo la cuenta del servicio pueda leer el {que} ({ruta}). Ejemplo: icacls \"{ruta}\"")
        return
    modo = stat.S_IMODE(ruta.stat().st_mode)
    add(OK if modo & 0o077 == 0 else FALTA, f"Permisos del {que}: {oct(modo)}" + ("" if modo & 0o077 == 0 else " (otros usuarios del servidor pueden leerlo: chmod 600)"))


def _usuarios(add, url_bd: str) -> None:
    try:
        from sqlalchemy import func, select

        from app.db import SessionLocal, Usuario
        with SessionLocal() as db:
            nombres = list(db.scalars(select(Usuario.usuario)))
            admins = db.scalar(select(func.count(Usuario.id)).where(Usuario.rol == "admin", Usuario.activo.is_(True)))
    except Exception as e:  # base inexistente o sin tablas
        add(AVISO, f"No se pudo leer la tabla de usuarios ({type(e).__name__}); revise DATABASE_URL")
        return
    demo = sorted(n for n in nombres if n.lower() in NOMBRES_DE_DEMO)
    add(FALTA if demo else OK, "Hay usuarios con nombre de demostración: " + ", ".join(demo) + ". Desactívelos." if demo else "Ningún usuario con nombre de demostración")
    add(OK if admins and admins >= 1 else FALTA, f"{admins} administrador(es) activo(s)" if admins else "No hay ningún administrador activo")
    if admins == 1:
        add(AVISO, "Solo hay 1 administrador: si pierde esa cuenta no podrá gestionar usuarios. Cree un segundo")


def main(argv: list[str]) -> int:
    resultado = evaluar()
    for nivel, texto in resultado:
        print(f"[{nivel}] {texto}")
    faltas = sum(1 for n, _ in resultado if n == FALTA)
    avisos = sum(1 for n, _ in resultado if n == AVISO)
    print(f"\n{faltas} imprescindible(s) por resolver y {avisos} aviso(s).")
    return 1 if faltas else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
