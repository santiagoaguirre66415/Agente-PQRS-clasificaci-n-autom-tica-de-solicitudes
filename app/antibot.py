"""Protección propia contra bots para el formulario público: gratuita, sin servicios de terceros y sin rastrear a nadie.

Tres capas:
  1. Prueba de trabajo: el navegador debe resolver un pequeño cálculo (unos segundos en un móvil normal) antes de enviar.
     Es barato para una persona y caro para quien quiere enviar miles de solicitudes.
  2. Trampa de tiempo: el reto caduca (1 hora), no se acepta si se resolvió demasiado rápido y solo sirve una vez.
  3. Campo trampa (honeypot): un campo oculto que las personas no ven; si llega relleno, es un bot.
Es una primera capa: se combina con los topes por IP y globales (ver app/seguridad.py) y con la revisión del personal.
Variables: PQRS_ANTIBOT=0 (apaga todo; solo para pruebas), PQRS_ANTIBOT_BITS (16), PQRS_ANTIBOT_MIN_SEG (2), PQRS_SECRETO.
"""
import hashlib
import hmac
import os
import secrets
import threading
import time

from fastapi import HTTPException

_SECRETO = os.environ.get("PQRS_SECRETO", "").encode() or secrets.token_bytes(32)  # con varios procesos, defina PQRS_SECRETO
_MAX_EDAD = 3600
_usados: dict[str, float] = {}
_lock = threading.Lock()


def activo() -> bool:
    return os.environ.get("PQRS_ANTIBOT", "1") != "0"


def bits() -> int:
    return int(os.environ.get("PQRS_ANTIBOT_BITS", "16"))


def _firma(cuerpo: str) -> str:
    return hmac.new(_SECRETO, cuerpo.encode(), hashlib.sha256).hexdigest()[:32]


def emitir_reto() -> dict:
    cuerpo = f"{int(time.time())}.{secrets.token_hex(8)}"
    return {"reto": f"{cuerpo}.{_firma(cuerpo)}", "bits": bits()}


def ceros_iniciales(digest: bytes) -> int:
    n = 0
    for byte in digest:
        if byte == 0:
            n += 8
            continue
        return n + (8 - byte.bit_length())
    return n


def validar(reto: str, prueba: str) -> None:
    """Lanza 400 con un mensaje genérico si el reto o la prueba no sirven."""
    malo = HTTPException(400, "No pudimos comprobar que es una persona. Recargue la página e intente de nuevo.")
    try:
        marca, nonce, firma = reto.split(".")
        ts = int(marca)
    except (ValueError, AttributeError):
        raise malo
    if not hmac.compare_digest(firma, _firma(f"{marca}.{nonce}")):
        raise malo
    edad = time.time() - ts
    if edad > _MAX_EDAD or edad < int(os.environ.get("PQRS_ANTIBOT_MIN_SEG", "2")):
        raise malo  # vencido, o resuelto tan rápido que no lo hizo una persona
    if not isinstance(prueba, str) or not prueba or len(prueba) > 20 or ceros_iniciales(hashlib.sha256(f"{reto}:{prueba}".encode()).digest()) < bits():
        raise malo
    with _lock:
        ahora = time.time()
        for r in [r for r, t in _usados.items() if ahora - t > _MAX_EDAD]:
            del _usados[r]
        if reto in _usados:
            raise malo  # un reto sirve una sola vez
        _usados[reto] = ahora


def reiniciar() -> None:
    with _lock:
        _usados.clear()
