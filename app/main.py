import hashlib
import hmac
import json
import math
import os
import re
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import antibot, correo, privacidad
from .agente import clasificar, ia_activa
from .db import ROLES, Auditoria, Sesion, SessionLocal, Solicitud, Usuario, get_db, init_db
from .seguridad import (CABECERAS, ahora, auditar, crear_sesion, cupo_verificacion, hash_falso, hashear, ip_cliente, limitar_correos,
                        limitar_envios, limitar_retos, limitar_salud, limitar_webhooks, login_bloqueado, registrar_fallo_login,
                        requiere, revocar_sesiones, tope_publico_global, usuario_actual, validar_clave, verificar)

# La documentación interactiva queda apagada salvo que se active con PQRS_DOCS=1
_docs = os.environ.get("PQRS_DOCS") == "1"
app = FastAPI(title="Agente PQRS", version="2.0",
              docs_url="/docs" if _docs else None, redoc_url=None, openapi_url="/openapi.json" if _docs else None)
init_db()
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")

LECTURA = ("admin", "funcionario", "consulta")
GESTION = ("admin", "funcionario")


def _usuario_inicial() -> None:
    """Crea el primer administrador desde variables de entorno (la clave nunca se imprime ni se guarda en claro)."""
    with SessionLocal() as db:
        if db.scalar(select(func.count(Usuario.id))):
            return
        nombre, clave = os.environ.get("PQRS_ADMIN_USER"), os.environ.get("PQRS_ADMIN_PASSWORD")
        if not (nombre and clave):
            print("[seguridad] No hay usuarios. Cree el primero con:  python crear_usuario.py")
            return
        validar_clave(clave, nombre)
        db.add(Usuario(usuario=nombre, clave_hash=hashear(clave), rol="admin"))
        auditar(db, "sistema", "crear_usuario", nombre, "administrador inicial")
        db.commit()


_usuario_inicial()


@app.middleware("http")
async def cabeceras_de_seguridad(request, call_next):
    respuesta = await call_next(request)
    for k, v in CABECERAS.items():
        respuesta.headers.setdefault(k, v)
    if os.environ.get("PQRS_HTTPS") == "1":  # solo si el sitio se publica con HTTPS (ver DESPLIEGUE.md)
        respuesta.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return respuesta


# ------------------------------ modelos de entrada ------------------------------
_CORREO = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
_USUARIO = re.compile(r"^[a-zA-Z0-9._-]{3,40}$")


class SolicitudIn(BaseModel):
    ciudadano: str = Field(max_length=120)
    correo: str = Field(default="", max_length=160)
    texto: str = Field(max_length=4000)
    # Campos del formulario público (no se guardan): protección anti-bots y autorización de datos
    reto: str = Field(default="", max_length=200)
    prueba: str = Field(default="", max_length=20)
    web: str = Field(default="", max_length=200)  # campo trampa: las personas no lo ven; los bots lo rellenan
    acepto: bool = False

    @field_validator("ciudadano", "correo", "texto", mode="before")
    @classmethod
    def _limpiar(cls, v):
        return " ".join(v.split()) if isinstance(v, str) else v

    @field_validator("ciudadano")
    @classmethod
    def _nombre(cls, v: str) -> str:
        if len(v) < 2:
            raise ValueError("El nombre debe tener al menos 2 caracteres")
        return v

    @field_validator("correo")
    @classmethod
    def _correo(cls, v: str) -> str:
        if v and not _CORREO.match(v):
            raise ValueError("El correo no tiene un formato válido")
        return v.lower()

    @field_validator("texto")
    @classmethod
    def _texto(cls, v: str) -> str:
        if len(v) < 10 or len(v.split()) < 3:
            raise ValueError("Describa la solicitud con al menos 3 palabras (mínimo 10 caracteres)")
        return v


class EstadoIn(BaseModel):
    estado: Literal["nuevo", "en_proceso", "resuelto"]


class RespuestaIn(BaseModel):
    respuesta: str = Field(max_length=4000)
    canal: Literal["correo", "otro"]  # correo = la app lo envía; otro = ya se respondió por teléfono, oficina, etc.

    @field_validator("respuesta")
    @classmethod
    def _respuesta(cls, v: str) -> str:
        v = "\n".join(linea.rstrip() for linea in v.strip().splitlines())  # conserva los saltos de línea del correo
        if len(v) < 10 or len(v.split()) < 3:
            raise ValueError("Escriba una respuesta de al menos 3 palabras (mínimo 10 caracteres)")
        return v


class LoginIn(BaseModel):
    usuario: str = Field(max_length=40)
    clave: str = Field(max_length=200)


class UsuarioIn(BaseModel):
    usuario: str
    clave: str = Field(max_length=200)
    rol: Literal["admin", "funcionario", "consulta"]

    @field_validator("usuario")
    @classmethod
    def _u(cls, v: str) -> str:
        if not _USUARIO.match(v):
            raise ValueError("Usuario de 3 a 40 caracteres: letras, números, punto, guion o guion bajo")
        return v


class UsuarioPatch(BaseModel):
    activo: bool | None = None
    rol: Literal["admin", "funcionario", "consulta"] | None = None


# Transiciones permitidas: una solicitud resuelta solo puede reabrirse a "en_proceso"
TRANSICIONES = {
    "nuevo": {"nuevo", "en_proceso", "resuelto"},
    "en_proceso": {"en_proceso", "resuelto"},
    "resuelto": {"resuelto", "en_proceso"},
}


def _out(s: Solicitud) -> dict:
    return {
        "id": s.id, "ciudadano": s.ciudadano, "correo": s.correo, "texto": s.texto, "tipo": s.tipo,
        "categoria": s.categoria, "prioridad": s.prioridad, "area": s.area, "resumen": s.resumen,
        "respuesta_sugerida": s.respuesta_sugerida, "metodo": s.metodo, "estado": s.estado,
        "creado": s.creado.isoformat(),
        "resuelto_en": s.resuelto_en.isoformat() if s.resuelto_en else None,
        "fecha_limite": s.fecha_limite.isoformat(),
        "respuesta_final": s.respuesta_final, "respondido_por": s.respondido_por,
        "respondido_en": s.respondido_en.isoformat() if s.respondido_en else None,
        "canal_respuesta": s.canal_respuesta, "correo_verificado": bool(s.correo_verificado),
        "entrega": s.entrega, "entrega_en": s.entrega_en.isoformat() if s.entrega_en else None,
        "autorizacion_en": s.autorizacion_en.isoformat() if s.autorizacion_en else None,
    }


def _out_usuario(u: Usuario) -> dict:
    return {"id": u.id, "usuario": u.usuario, "rol": u.rol, "activo": u.activo, "creado": u.creado.isoformat()}


@app.get("/", include_in_schema=False)
def panel():
    return FileResponse(STATIC / "index.html")


# ------------------------------- autenticación -------------------------------
@app.post("/auth/login")
def login(datos: LoginIn, request: Request, db: Session = Depends(get_db)):
    ip = ip_cliente(request)
    if login_bloqueado(ip, datos.usuario):
        auditar(db, datos.usuario, "login_bloqueado", ip=ip)
        db.commit()
        raise HTTPException(429, "Demasiados intentos. Espere unos minutos.", headers={"Retry-After": "300"})
    u = db.scalar(select(Usuario).where(Usuario.usuario == datos.usuario))
    # Se calcula el hash siempre, exista o no el usuario: mismo tiempo y mismo mensaje (sin enumeración)
    valido = verificar(datos.clave, u.clave_hash if u else hash_falso())
    if not (u and u.activo and valido):
        registrar_fallo_login(ip, datos.usuario)
        auditar(db, datos.usuario, "login_fallido", ip=ip)  # nunca se registra la clave
        db.commit()
        raise HTTPException(401, "Usuario o clave incorrectos")
    token, expira = crear_sesion(db, u)
    auditar(db, u, "login", ip=ip)
    db.commit()
    return {"token": token, "usuario": u.usuario, "rol": u.rol, "expira": expira.isoformat()}


@app.post("/auth/logout")
def logout(request: Request, u: Usuario = Depends(usuario_actual), db: Session = Depends(get_db)):
    ses = db.get(Sesion, request.state.sesion.id)
    ses.revocada = True
    auditar(db, u, "logout", ip=ip_cliente(request))
    db.commit()
    return {"mensaje": "Sesión cerrada"}


@app.get("/auth/yo")
def yo(u: Usuario = Depends(usuario_actual)):
    return {"usuario": u.usuario, "rol": u.rol}


# -------------------------------- solicitudes --------------------------------
@app.post("/solicitudes", status_code=201, dependencies=[Depends(limitar_envios)])
def crear(datos: SolicitudIn, request: Request, db: Session = Depends(get_db)):
    """Público (la ciudadanía envía solicitudes), con límite de frecuencia y protección anti-bots.
    El personal con sesión iniciada puede registrar solicitudes sin el reto.
    Devuelve solo lo mínimo: la clasificación interna no se expone."""
    if antibot.activo() and not _es_personal(request, db):
        privacidad.exigir_completo()
        if datos.web:  # campo trampa relleno: es un bot. Se responde como si todo hubiera salido bien, sin guardar nada
            return {"id": 0, "estado": "nuevo", "mensaje": "Solicitud recibida. Le responderemos pronto."}
        if not datos.acepto:
            raise HTTPException(422, "Debe autorizar el tratamiento de sus datos para enviar la solicitud.")
        antibot.validar(datos.reto, datos.prueba)
        tope_publico_global()
    repetida = db.scalar(select(Solicitud).where(
        Solicitud.ciudadano == datos.ciudadano, Solicitud.texto == datos.texto,
        Solicitud.creado >= ahora() - timedelta(hours=24)))
    if repetida:
        raise HTTPException(409, f"Esta solicitud ya fue registrada (n.º {repetida.id})")
    r = clasificar(datos.texto)
    s = Solicitud(ciudadano=datos.ciudadano, correo=datos.correo, texto=datos.texto, **r.dict(),
                  autorizacion_en=ahora() if datos.acepto else None)
    # Una dirección ya confirmada antes no necesita confirmarse otra vez
    if s.correo and db.scalar(select(Solicitud.id).where(Solicitud.correo == s.correo, Solicitud.correo_verificado.is_(True)).limit(1)):
        s.correo_verificado = True
    db.add(s)
    db.commit()
    mensaje = "Solicitud recibida. Le responderemos pronto."
    if _pedir_verificacion(db, s):
        mensaje = "Solicitud recibida. Le enviamos un correo para confirmar su dirección; ábralo para que podamos responderle por ese medio."
    return {"id": s.id, "estado": s.estado, "mensaje": mensaje}


def _es_personal(request: Request, db: Session) -> bool:
    try:
        usuario_actual(request, db)
        return True
    except HTTPException:
        return False


@app.get("/salud", include_in_schema=False, dependencies=[Depends(limitar_salud)])
def salud(db: Session = Depends(get_db)):
    """Para monitores de disponibilidad (UptimeRobot, Healthchecks, etc.). Solo dice si responde y si llega a la base; nunca versiones ni detalles."""
    try:
        db.execute(select(1))
    except Exception:
        raise HTTPException(503, "no disponible")
    return {"ok": True}


@app.get("/api/reto", include_in_schema=False, dependencies=[Depends(limitar_retos)])
def reto_antibot():
    privacidad.exigir_completo()  # sin aviso de privacidad completo, el formulario no se abre
    return antibot.emitir_reto()


@app.get("/solicitar", include_in_schema=False)
def pagina_publica():
    return FileResponse(STATIC / "solicitar.html")


@app.get("/privacidad", include_in_schema=False)
def pagina_privacidad():
    return HTMLResponse(privacidad.pagina())


def _pedir_verificacion(db: Session, s: Solicitud) -> bool:
    """Envía el enlace de confirmación al correo escrito. Nunca hace fallar el registro de la solicitud.

    Defensas contra abuso (alguien podría escribir el correo de otra persona): un solo mensaje por dirección cada 24 h,
    tope global diario, el mensaje no contiene datos del caso y el enlace usa la URL pública configurada, no el Host recibido."""
    base = os.environ.get("PQRS_URL_PUBLICA", "").rstrip("/")
    if not s.correo or s.correo_verificado or not base.startswith("https://") or not correo.configurado():
        return False
    reciente = db.scalar(select(Solicitud.id).where(
        Solicitud.correo == s.correo, Solicitud.id != s.id, Solicitud.verif_hash.is_not(None),
        Solicitud.creado >= ahora() - timedelta(hours=24)).limit(1))
    if reciente or not cupo_verificacion():
        return False
    token = secrets.token_urlsafe(24)
    s.verif_hash, s.verif_expira = _huella(token), ahora() + timedelta(days=7)
    db.commit()
    try:
        correo.enviar_verificacion(s.correo, f"{base}/verificar/{token}")
    except correo.ErrorCorreo as e:
        print(f"[correo] no se pudo enviar la verificación ({e})")
        return False
    return True


def _huella(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


_FORMATO_TOKEN = re.compile(r"^[A-Za-z0-9_-]{20,64}$")


def _pagina(titulo: str, texto: str, boton: str | None = None, accion: str = "", codigo: int = 200) -> HTMLResponse:
    form = (f'<form method="post" action="{accion}"><button style="font:inherit;font-weight:600;padding:12px 20px;border:0;'
            f'border-radius:6px;background:#0F5E63;color:#fff;cursor:pointer">{boton}</button></form>') if boton else ""
    html = (f'<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{titulo}</title><body style="font:18px/1.6 system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 24px;color:#13262B">'
            f'<h1 style="font-size:1.6rem">{titulo}</h1><p style="color:#55686D">{texto}</p>{form}</body></html>')
    return HTMLResponse(html, status_code=codigo)


def _solicitud_por_token(db: Session, token: str) -> Solicitud | None:
    if not _FORMATO_TOKEN.match(token):
        return None
    s = db.scalar(select(Solicitud).where(Solicitud.verif_hash == _huella(token)))
    return s if s and s.verif_expira and s.verif_expira >= ahora() else None


_ENLACE_INVALIDO = ("Enlace no válido", "El enlace ya se usó o venció. Si todavía necesita confirmar su correo, escríbanos de nuevo.")


@app.get("/verificar/{token}", include_in_schema=False)
def verificar_correo_pagina(token: str, db: Session = Depends(get_db)):
    """Muestra un botón de confirmación. Abrir el enlace NO confirma nada: así los antivirus y vistas previas
    que abren los enlaces automáticamente no pueden confirmar por la persona."""
    if not _solicitud_por_token(db, token):
        return _pagina(*_ENLACE_INVALIDO, codigo=400)
    return _pagina("Confirme su correo", "Pulse el botón para confirmar que esta dirección es suya y que podemos responderle por este medio.",
                   "Confirmar mi correo", f"/verificar/{token}")


@app.post("/verificar/{token}", include_in_schema=False, dependencies=[Depends(limitar_envios)])
def verificar_correo(token: str, db: Session = Depends(get_db)):
    s = _solicitud_por_token(db, token)
    if not s:
        return _pagina(*_ENLACE_INVALIDO, codigo=400)
    for otra in db.scalars(select(Solicitud).where(Solicitud.correo == s.correo)):
        otra.correo_verificado, otra.verif_hash, otra.verif_expira = True, None, None
    auditar(db, "ciudadano", "correo_verificado", f"solicitud {s.id}", correo.enmascarar(s.correo))
    db.commit()
    return _pagina("Correo confirmado", "Gracias. Ya podemos responderle por este medio.")


@app.get("/solicitudes")
def listar(request: Request, estado: str | None = None, prioridad: str | None = None, categoria: str | None = None,
           limite: int = Query(100, ge=1, le=200), desde: int = Query(0, ge=0),
           u: Usuario = Depends(requiere(*LECTURA)), db: Session = Depends(get_db)):
    q = select(Solicitud).order_by(Solicitud.creado.desc()).limit(limite).offset(desde)
    filtros = {"estado": estado, "prioridad": prioridad, "categoria": categoria}
    for campo, valor in filtros.items():
        if valor:
            q = q.where(getattr(Solicitud, campo) == valor)
    filas = list(db.scalars(q))
    auditar(db, u, "consultar_listado", f"{len(filas)} solicitudes",
            ", ".join(f"{k}={v}" for k, v in filtros.items() if v), ip_cliente(request))
    db.commit()
    return [_out(s) for s in filas]


@app.get("/solicitudes/{sid}")
def detalle(sid: int, request: Request, u: Usuario = Depends(requiere(*LECTURA)), db: Session = Depends(get_db)):
    s = db.get(Solicitud, sid)
    if not s:
        raise HTTPException(404, "Solicitud no encontrada")
    auditar(db, u, "consultar_solicitud", f"solicitud {sid}", ip=ip_cliente(request))
    db.commit()
    return _out(s)


@app.patch("/solicitudes/{sid}")
def cambiar_estado(sid: int, datos: EstadoIn, request: Request,
                   u: Usuario = Depends(requiere(*GESTION)), db: Session = Depends(get_db)):
    s = db.get(Solicitud, sid)
    if not s:
        raise HTTPException(404, "Solicitud no encontrada")
    if datos.estado not in TRANSICIONES[s.estado]:
        raise HTTPException(409, f"No se puede pasar de '{s.estado}' a '{datos.estado}'")
    if datos.estado == "resuelto" and not s.respondido_en:
        raise HTTPException(409, "Responda la solicitud antes de resolverla. Se marca como resuelta al enviar la respuesta.")
    anterior = s.estado
    if datos.estado == "resuelto" and anterior != "resuelto":
        s.resuelto_en = ahora()
    elif datos.estado != "resuelto":
        s.resuelto_en = None
    s.estado = datos.estado
    auditar(db, u, "cambiar_estado", f"solicitud {sid}", f"{anterior} -> {datos.estado}", ip_cliente(request))
    db.commit()
    return _out(s)


@app.post("/solicitudes/{sid}/responder")
def responder(sid: int, datos: RespuestaIn, request: Request,
              u: Usuario = Depends(requiere(*GESTION)), db: Session = Depends(get_db)):
    """Guarda la respuesta oficial y, si el canal es 'correo', la envía. Solo se marca como respondida si el envío salió bien."""
    s = db.get(Solicitud, sid)
    if not s:
        raise HTTPException(404, "Solicitud no encontrada")
    if s.respondido_en:
        raise HTTPException(409, f"Esta solicitud ya fue respondida el {s.respondido_en:%d/%m/%Y} por {s.respondido_por}")
    ip, detalle, mensaje_id = ip_cliente(request), f"canal={datos.canal}; {len(datos.respuesta)} caracteres", None
    if datos.canal == "correo":
        if not s.correo:
            raise HTTPException(409, "La solicitud no tiene correo. Responda por otro medio y registre la respuesta con el canal 'otro'.")
        if not s.correo_verificado:
            raise HTTPException(409, "El correo de esta persona aún no está verificado. Responda por otro medio (canal 'otro') o espere a que confirme su dirección.")
        if not correo.configurado():
            raise HTTPException(409, "El envío de correo no está configurado. Registre la respuesta con el canal 'otro' o configure el correo (ver DESPLIEGUE.md).")
        limitar_correos(u.usuario)
        try:
            mensaje_id = correo.enviar(s.correo, f"Respuesta a su solicitud n.º {s.id}",
                                       f"{datos.respuesta}\n\n—\nEste mensaje responde a su solicitud n.º {s.id}.")
        except correo.ErrorCorreo as e:
            auditar(db, u, "envio_fallido", f"solicitud {sid}", f"canal=correo; error={e}", ip)
            db.commit()
            raise HTTPException(502, "No se pudo enviar el correo. La solicitud sigue sin responder; intente de nuevo.")
        detalle += f"; destinatario={correo.enmascarar(s.correo)}"
    ahora_ = ahora()
    s.respuesta_final, s.respondido_por, s.respondido_en, s.canal_respuesta = datos.respuesta, u.usuario, ahora_, datos.canal
    s.estado, s.resuelto_en, s.mensaje_id = "resuelto", ahora_, mensaje_id
    auditar(db, u, "responder_solicitud", f"solicitud {sid}", detalle, ip)
    db.commit()
    return _out(s)


@app.post("/webhooks/correo", include_in_schema=False, dependencies=[Depends(limitar_webhooks)])
async def webhook_correo(request: Request, db: Session = Depends(get_db)):
    """Recibe avisos de entrega o rebote del proveedor de correo (o de un traductor como n8n/Make).

    Contrato (JSON): {"mensaje_id": "<Message-ID del correo>", "evento": "entregado" | "rebotado", "fecha": <segundos epoch>}
    Cabecera X-Firma: "sha256=" + HMAC-SHA256(cuerpo crudo, PQRS_WEBHOOK_SECRETO) en hexadecimal.
    Apagado (404) mientras no se defina PQRS_WEBHOOK_SECRETO."""
    secreto = os.environ.get("PQRS_WEBHOOK_SECRETO", "")
    if not secreto:
        raise HTTPException(404, "No encontrado")
    cuerpo = await request.body()
    if len(cuerpo) > 10_000:
        raise HTTPException(413, "Cuerpo demasiado grande")
    esperada = "sha256=" + hmac.new(secreto.encode(), cuerpo, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(request.headers.get("x-firma", ""), esperada):
        raise HTTPException(401, "Firma inválida")
    try:
        datos = json.loads(cuerpo)
        mid, evento, fecha = datos["mensaje_id"], datos["evento"], datos["fecha"]
    except (ValueError, KeyError, TypeError, RecursionError):  # JSON roto, sin campos, no es un objeto o anidado hasta desbordar la pila
        raise HTTPException(422, "Cuerpo inválido")
    if not isinstance(mid, str) or not 1 <= len(mid) <= 200:
        raise HTTPException(422, "mensaje_id inválido")
    if evento not in ("entregado", "rebotado"):  # `in` sobre una tupla de str: una lista o un dict nunca coinciden
        raise HTTPException(422, "Evento no reconocido")
    # La fecha debe ser un número REAL y finito: con NaN, `abs(ahora - fecha) > 300` es falso y se saltaba la protección contra repeticiones
    if isinstance(fecha, bool) or not isinstance(fecha, (int, float)) or not math.isfinite(fecha):
        raise HTTPException(422, "fecha inválida")
    if abs(time.time() - fecha) > 300:
        raise HTTPException(400, "Evento vencido")  # frena la repetición de mensajes antiguos capturados
    s = db.scalar(select(Solicitud).where(Solicitud.mensaje_id == mid))
    # Idempotente: repetir el mismo aviso no cambia nada ni llena la auditoría; un rebote es definitivo
    if s and s.entrega != evento and not (s.entrega == "rebotado" and evento == "entregado"):
        s.entrega, s.entrega_en = evento, ahora()
        auditar(db, "proveedor_correo", "entrega_correo", f"solicitud {s.id}", evento)
        db.commit()
    return {"ok": True}  # misma respuesta exista o no el mensaje


@app.get("/config")
def configuracion(u: Usuario = Depends(requiere(*LECTURA))):
    """Lo que el panel necesita saber para mostrar u ocultar acciones. No expone ningún secreto."""
    return {"correo_configurado": correo.configurado(), "ia_activa": ia_activa(),
            "plazo_dias": int(os.environ.get("PQRS_PLAZO_DIAS", "15"))}


@app.get("/estadisticas")
def estadisticas(u: Usuario = Depends(requiere(*LECTURA)), db: Session = Depends(get_db)):
    def conteo(col):
        return {k: n for k, n in db.execute(select(col, func.count()).group_by(col))}
    return {
        "total": db.scalar(select(func.count(Solicitud.id))),
        "por_estado": conteo(Solicitud.estado),
        "por_prioridad": conteo(Solicitud.prioridad),
        "por_categoria": conteo(Solicitud.categoria),
        "por_tipo": conteo(Solicitud.tipo),
    }


# --------------------------- administración (solo admin) ---------------------------
@app.get("/usuarios")
def listar_usuarios(u: Usuario = Depends(requiere("admin")), db: Session = Depends(get_db)):
    return [_out_usuario(x) for x in db.scalars(select(Usuario).order_by(Usuario.id))]


@app.post("/usuarios", status_code=201)
def crear_usuario(datos: UsuarioIn, request: Request, u: Usuario = Depends(requiere("admin")),
                  db: Session = Depends(get_db)):
    try:
        validar_clave(datos.clave, datos.usuario)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if db.scalar(select(Usuario).where(Usuario.usuario == datos.usuario)):
        raise HTTPException(409, "Ese usuario ya existe")
    nuevo = Usuario(usuario=datos.usuario, clave_hash=hashear(datos.clave), rol=datos.rol)
    db.add(nuevo)
    auditar(db, u, "crear_usuario", datos.usuario, f"rol={datos.rol}", ip_cliente(request))
    db.commit()
    return _out_usuario(nuevo)


@app.patch("/usuarios/{uid}")
def modificar_usuario(uid: int, datos: UsuarioPatch, request: Request, u: Usuario = Depends(requiere("admin")),
                      db: Session = Depends(get_db)):
    x = db.get(Usuario, uid)
    if not x:
        raise HTTPException(404, "Usuario no encontrado")
    if x.id == u.id and (datos.activo is False or (datos.rol and datos.rol != "admin")):
        raise HTTPException(409, "No puede quitarse a sí mismo el acceso de administrador")
    cambios = []
    if datos.rol and datos.rol != x.rol:
        cambios.append(f"rol {x.rol} -> {datos.rol}")
        x.rol = datos.rol
    if datos.activo is not None and datos.activo != x.activo:
        cambios.append(f"activo {x.activo} -> {datos.activo}")
        x.activo = datos.activo
    if cambios:
        revocar_sesiones(db, x.id)  # los permisos anteriores dejan de valer de inmediato
        auditar(db, u, "modificar_usuario", x.usuario, "; ".join(cambios), ip_cliente(request))
    db.commit()
    return _out_usuario(x)


@app.get("/auditoria")
def ver_auditoria(limite: int = Query(100, ge=1, le=500), desde: int = Query(0, ge=0),
                  u: Usuario = Depends(requiere("admin")), db: Session = Depends(get_db)):
    q = select(Auditoria).order_by(Auditoria.id.desc()).limit(limite).offset(desde)
    return [{"id": a.id, "fecha": a.fecha.isoformat(), "usuario": a.usuario, "rol": a.rol, "accion": a.accion,
             "objeto": a.objeto, "detalle": a.detalle, "ip": a.ip} for a in db.scalars(q)]
