"""Envío de correos con SMTP propio. Siempre con STARTTLS; la contraseña nunca se registra.

Variables: PQRS_SMTP_HOST, PQRS_SMTP_PORT (587 por defecto), PQRS_SMTP_USER, PQRS_SMTP_FROM y la contraseña en UNA de:
  - PQRS_SMTP_PASSWORD_FILE: ruta de un archivo que contiene solo la contraseña (recomendado: Docker/Kubernetes secrets,
    o un archivo con permisos de lectura solo para el usuario del servicio). No queda en el entorno del proceso.
  - PQRS_SMTP_PASSWORD: la contraseña directamente (más simple, pero visible en el entorno del proceso).
Con Gmail: smtp.gmail.com, puerto 587, y una «contraseña de aplicación» (no la contraseña normal).
"""
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid

SMTP = smtplib.SMTP  # punto de inyección: las pruebas lo reemplazan y nunca envían correos reales

_VARIABLES = ("PQRS_SMTP_HOST", "PQRS_SMTP_USER", "PQRS_SMTP_FROM")


class ErrorCorreo(Exception):
    """No se pudo enviar. Solo lleva el tipo del error: nunca credenciales ni contenido."""


def _clave() -> str | None:
    archivo = os.environ.get("PQRS_SMTP_PASSWORD_FILE")
    if archivo:
        try:
            with open(archivo, encoding="utf-8") as f:
                return f.read().strip() or None
        except OSError:
            return None
    return os.environ.get("PQRS_SMTP_PASSWORD") or None


def configurado() -> bool:
    return all(os.environ.get(v) for v in _VARIABLES) and _clave() is not None


def _contexto_tls() -> ssl.SSLContext:
    """Verificación completa del certificado. PQRS_SMTP_CA_FILE permite confiar en UN certificado concreto
    (p. ej. el de un servidor de pruebas local); con él se verifica solo contra ese archivo, nunca se desactiva la verificación."""
    return ssl.create_default_context(cafile=os.environ.get("PQRS_SMTP_CA_FILE") or None)


def enviar(destino: str, asunto: str, cuerpo: str) -> str:
    """Envía el correo y devuelve su Message-ID (sirve para enlazar el aviso de entrega o rebote del proveedor)."""
    if not configurado():
        raise ErrorCorreo("Correo no configurado")
    try:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = os.environ["PQRS_SMTP_FROM"], destino, asunto  # ValueError si hay saltos de línea
        dominio = os.environ["PQRS_SMTP_FROM"].rpartition("@")[2].strip(" >") or "localhost"
        msg["Message-ID"] = make_msgid(domain=dominio)
        msg.set_content(cuerpo)
        with SMTP(os.environ["PQRS_SMTP_HOST"], int(os.environ.get("PQRS_SMTP_PORT", "587")),
                  timeout=float(os.environ.get("PQRS_SMTP_TIMEOUT", "15"))) as s:
            s.starttls(context=_contexto_tls())  # el correo y la contraseña nunca viajan sin cifrar
            s.login(os.environ["PQRS_SMTP_USER"], _clave())
            s.send_message(msg)
        return str(msg["Message-ID"])
    except (smtplib.SMTPException, OSError, ValueError) as e:
        raise ErrorCorreo(type(e).__name__) from None


def enviar_verificacion(destino: str, enlace: str) -> None:
    """Correo para confirmar que la dirección es de quien escribió. No incluye ningún dato de la solicitud."""
    enviar(destino, "Confirme su correo electrónico",
           "Hola,\n\nRecibimos una solicitud con esta dirección de correo. Para poder responderle por este medio, "
           f"confirme que es suya abriendo este enlace:\n\n{enlace}\n\nEl enlace vence en 7 días. "
           "Si usted no escribió a nuestra empresa, ignore este mensaje: no se enviará nada más a esta dirección.")


def enmascarar(correo: str) -> str:
    """juan.perez@mail.com -> j***@mail.com (para la auditoría: no guarda el correo completo)."""
    usuario, _, dominio = correo.partition("@")
    return f"{usuario[:1]}***@{dominio}"
