"""Seguridad: contraseñas, sesiones, roles, auditoría, límites de frecuencia y cabeceras HTTP."""
import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Auditoria, Sesion, Usuario, get_db


def ahora() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ------------------------------- contraseñas -------------------------------
# scrypt (stdlib). Parámetros por defecto según OWASP: N=2^17, r=8, p=1. Se guardan en el hash,
# así que se pueden subir en el futuro sin invalidar las claves existentes.
def hashear(clave: str) -> str:
    n = int(os.environ.get("PQRS_SCRYPT_N", 2**17))
    sal = os.urandom(16)
    h = hashlib.scrypt(clave.encode(), salt=sal, n=n, r=8, p=1, maxmem=512 * 1024 * 1024, dklen=32)
    return f"scrypt${n}$8$1${sal.hex()}${h.hex()}"


def verificar(clave: str, guardado: str) -> bool:
    try:
        _, n, r, p, sal, esperado = guardado.split("$")
        h = hashlib.scrypt(clave.encode(), salt=bytes.fromhex(sal), n=int(n), r=int(r), p=int(p),
                           maxmem=512 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(h.hex(), esperado)
    except (ValueError, TypeError):
        return False


def validar_clave(clave: str, usuario: str) -> None:
    if len(clave) < 12:
        raise ValueError("La clave debe tener al menos 12 caracteres")
    if clave.lower() == usuario.lower() or usuario.lower() in clave.lower():
        raise ValueError("La clave no puede contener el nombre de usuario")
    if len(set(clave)) < 5:
        raise ValueError("La clave es demasiado repetitiva")


# Hash falso para gastar el mismo tiempo cuando el usuario no existe (evita enumerar usuarios por tiempo)
_HASH_FALSO = None


def hash_falso() -> str:
    global _HASH_FALSO
    if _HASH_FALSO is None:
        _HASH_FALSO = hashear(secrets.token_urlsafe(16))
    return _HASH_FALSO


# --------------------------------- sesiones ---------------------------------
def _huella(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def crear_sesion(db: Session, usuario: Usuario) -> tuple[str, datetime]:
    horas = float(os.environ.get("PQRS_SESION_HORAS", "8"))
    token = secrets.token_urlsafe(32)
    expira = ahora() + timedelta(hours=horas)
    db.add(Sesion(token_hash=_huella(token), usuario_id=usuario.id, expira=expira))
    db.commit()
    return token, expira


def revocar_sesiones(db: Session, usuario_id: int) -> None:
    for s in db.scalars(select(Sesion).where(Sesion.usuario_id == usuario_id, Sesion.revocada.is_(False))):
        s.revocada = True


def usuario_actual(request: Request, db: Session = Depends(get_db)) -> Usuario:
    cab = request.headers.get("authorization", "")
    if not cab.lower().startswith("bearer ") or len(cab) < 20:
        raise HTTPException(401, "Inicie sesión", headers={"WWW-Authenticate": "Bearer"})
    ses = db.scalar(select(Sesion).where(Sesion.token_hash == _huella(cab[7:].strip())))
    if not ses or ses.revocada or ses.expira < ahora():
        raise HTTPException(401, "Sesión inválida o vencida", headers={"WWW-Authenticate": "Bearer"})
    u = db.get(Usuario, ses.usuario_id)
    if not u or not u.activo:  # desactivar a alguien corta su acceso de inmediato
        raise HTTPException(401, "Sesión inválida o vencida", headers={"WWW-Authenticate": "Bearer"})
    request.state.sesion = ses
    return u


def requiere(*roles: str):
    def dependencia(request: Request, u: Usuario = Depends(usuario_actual), db: Session = Depends(get_db)) -> Usuario:
        if u.rol not in roles:
            auditar(db, u, "acceso_denegado", f"{request.method} {request.url.path}", ip=ip_cliente(request))
            db.commit()
            raise HTTPException(403, "Su rol no permite esta acción")
        return u
    return dependencia


# --------------------------------- auditoría ---------------------------------
def auditar(db: Session, u: Usuario | str | None, accion: str, objeto: str = "", detalle: str = "", ip: str = "") -> None:
    """Agrega un evento a la sesión (el llamador hace commit junto con la operación)."""
    nombre = u.usuario if isinstance(u, Usuario) else (str(u)[:40] if u else "-")
    db.add(Auditoria(usuario=nombre, rol=u.rol if isinstance(u, Usuario) else "-", accion=accion,
                     objeto=objeto[:60], detalle=detalle[:1000], ip=ip[:45]))


# ------------------------------ IP y frecuencia ------------------------------
def ip_cliente(request: Request) -> str:
    """Con PQRS_TRUST_PROXY=1 se usa la última IP de X-Forwarded-For, la añadida por el UNICO proxy de confianza.
    Sin esa variable se ignora la cabecera, porque cualquier cliente puede escribirla."""
    if os.environ.get("PQRS_TRUST_PROXY") == "1":
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[-1].strip()[:45]
    return request.client.host if request.client else "desconocida"


_eventos: dict[str, deque] = defaultdict(deque)


def _contar(clave: str, ventana: float) -> int:
    ahora_ = time.monotonic()
    cola = _eventos[clave]
    while cola and ahora_ - cola[0] > ventana:
        cola.popleft()
    return len(cola)


def limitar_envios(request: Request) -> None:
    """Máximo PQRS_RATE_LIMIT envíos por minuto por IP (por defecto 10)."""
    limite = int(os.environ.get("PQRS_RATE_LIMIT", "10"))
    clave = "envio:" + ip_cliente(request)
    if _contar(clave, 60) >= limite:
        raise HTTPException(429, "Demasiados envíos seguidos. Intente de nuevo en un minuto.", headers={"Retry-After": "60"})
    _eventos[clave].append(time.monotonic())


def limitar_correos(usuario: str) -> None:
    """Máximo PQRS_MAIL_LIMIT correos por minuto por funcionario (frena el abuso desde una cuenta comprometida)."""
    limite = int(os.environ.get("PQRS_MAIL_LIMIT", "10"))
    clave = "correo:" + usuario
    if _contar(clave, 60) >= limite:
        raise HTTPException(429, "Demasiados correos seguidos. Espere un minuto.", headers={"Retry-After": "60"})
    _eventos[clave].append(time.monotonic())


def _limitar(cubo: str, request: Request, limite: int, mensaje: str) -> None:
    clave = f"{cubo}:{ip_cliente(request)}"
    if _contar(clave, 60) >= limite:
        raise HTTPException(429, mensaje, headers={"Retry-After": "60"})
    _eventos[clave].append(time.monotonic())


def limitar_retos(request: Request) -> None:
    """Máximo 30 retos anti-bot por minuto por IP (la página pide uno cada vez que se abre)."""
    _limitar("reto", request, int(os.environ.get("PQRS_RETO_LIMIT", "30")), "Demasiadas solicitudes. Espere un minuto.")


def limitar_salud(request: Request) -> None:
    _limitar("salud", request, int(os.environ.get("PQRS_SALUD_LIMIT", "60")), "Demasiadas consultas.")


def limitar_webhooks(request: Request) -> None:
    _limitar("webhook", request, int(os.environ.get("PQRS_WEBHOOK_LIMIT", "120")), "Demasiados eventos. Espere un minuto.")


def tope_publico_global() -> None:
    """Tope TOTAL de solicitudes públicas por hora (PQRS_PUBLICO_MAX_HORA, 300 por defecto), sin importar de cuántas IP vengan.
    Es la defensa contra un abuso distribuido (muchas IP, cada una dentro de su límite): protege al personal de una inundación."""
    if _contar("publico-hora", 3600) >= int(os.environ.get("PQRS_PUBLICO_MAX_HORA", "300")):
        raise HTTPException(429, "Estamos recibiendo muchas solicitudes. Intente de nuevo en unos minutos.", headers={"Retry-After": "300"})
    _eventos["publico-hora"].append(time.monotonic())


def cupo_verificacion() -> bool:
    """Tope global diario de correos de verificación (PQRS_VERIF_MAX_DIA, 200 por defecto): evita usar la app para enviar spam."""
    if _contar("verificacion-dia", 86400) >= int(os.environ.get("PQRS_VERIF_MAX_DIA", "200")):
        return False
    _eventos["verificacion-dia"].append(time.monotonic())
    return True


def login_bloqueado(ip: str, usuario: str) -> bool:
    limite = int(os.environ.get("PQRS_LOGIN_LIMIT", "5"))
    return _contar("login-ip:" + ip, 300) >= limite or _contar("login-u:" + usuario.lower(), 300) >= limite


def registrar_fallo_login(ip: str, usuario: str) -> None:
    t = time.monotonic()
    _eventos["login-ip:" + ip].append(t)
    _eventos["login-u:" + usuario.lower()].append(t)


def reiniciar_limites() -> None:
    _eventos.clear()


CABECERAS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
