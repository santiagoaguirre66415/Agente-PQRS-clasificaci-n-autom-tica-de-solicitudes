"""Prueba de PUNTA A PUNTA con un servidor SMTP real (local): usa smtplib, STARTTLS, autenticación y verificación de
certificado de verdad, sin simulaciones. No envía nada a internet: el servidor escucha solo en 127.0.0.1.
Se omite si no están instaladas las herramientas opcionales:  pip install aiosmtpd cryptography"""
import email
import re
import socket
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("aiosmtpd")
pytest.importorskip("cryptography")
sys.path.insert(0, str(Path(__file__).parent.parent / "herramientas"))
import servidor_correo_local as sink  # noqa: E402

from tests import test_api as t  # noqa: E402  (reutiliza la app, la base de datos de pruebas y los ayudantes)


def puerto_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def smtp_real(tmp_path, monkeypatch):
    puerto = puerto_libre()
    controlador, cert = sink.iniciar(tmp_path / "local", puerto)
    # Se usa 127.0.0.1 y no "localhost": el certificado de pruebas cubre esta IP y se evita la espera del intento por IPv6
    for k, v in {"PQRS_SMTP_HOST": "127.0.0.1", "PQRS_SMTP_PORT": str(puerto), "PQRS_SMTP_USER": sink.USUARIO,
                 "PQRS_SMTP_PASSWORD": sink.CLAVE, "PQRS_SMTP_FROM": "Atención <pqrs@ejemplo.test>",
                 "PQRS_SMTP_CA_FILE": str(cert), "PQRS_URL_PUBLICA": "https://pqrs.ejemplo.test"}.items():
        monkeypatch.setenv(k, v)
    buzon = tmp_path / "local" / "buzon"
    yield buzon
    controlador.stop()


def correos(buzon, esperados, espera=5.0):
    """Espera a que el servidor haya guardado `esperados` correos y los devuelve ya leídos."""
    limite = time.time() + espera
    while time.time() < limite:
        archivos = sorted(buzon.glob("*.eml"))
        if len(archivos) >= esperados:
            return [email.message_from_bytes(a.read_bytes()) for a in archivos]
        time.sleep(0.05)
    raise AssertionError(f"Llegaron {len(list(buzon.glob('*.eml')))} correos; se esperaban {esperados}")


def test_flujo_completo_con_smtp_real(smtp_real):
    """Registro → correo de verificación → confirmación → respuesta por correo, todo por una conexión SMTP+TLS real."""
    func, _ = t.sesion("fer.func", "funcionario")
    r = t.enviar_solicitud("Real", "real@mail.com")
    assert r.status_code == 201 and "confirmar su dirección" in r.json()["mensaje"]
    sid = r.json()["id"]

    verificacion = correos(smtp_real, 1)[0]
    assert verificacion["To"] == "real@mail.com" and verificacion["From"].endswith("<pqrs@ejemplo.test>")
    cuerpo = verificacion.get_payload(decode=True).decode()
    token = re.search(r"/verificar/([A-Za-z0-9_-]+)", cuerpo).group(1)
    assert "reclamo" not in cuerpo  # sin datos del caso

    assert "Correo confirmado" in t.publico.post(f"/verificar/{token}").text
    respuesta = "Estimada Real, revisamos su cobro y lo corregimos. Gracias por escribirnos."
    d = t.responder(func, sid, "correo", respuesta)
    assert d.status_code == 200

    enviado = correos(smtp_real, 2)[1]
    assert enviado["To"] == "real@mail.com" and f"n.º {sid}" in str(email.header.make_header(email.header.decode_header(enviado["Subject"])))
    assert "corregimos" in enviado.get_payload(decode=True).decode()
    with t.SessionLocal() as db:  # el Message-ID real del correo quedó guardado para enlazar el aviso de entrega
        assert db.get(t.Solicitud, sid).mensaje_id == enviado["Message-ID"]


def test_la_verificacion_del_certificado_sigue_activa(smtp_real, monkeypatch):
    """Sin confiar en el certificado del servidor, el envío debe fallar: la app nunca desactiva la verificación TLS."""
    monkeypatch.delenv("PQRS_SMTP_CA_FILE")
    func, _ = t.sesion("fer.func", "funcionario")
    s = t.solicitud_con_correo("Tls", "tls@mail.com")
    r = t.responder(func, s["id"], "correo")
    assert r.status_code == 502
    assert list(smtp_real.glob("*.eml")) == []  # nada llegó a entregarse
    assert func.get(f"/solicitudes/{s['id']}").json()["respondido_en"] is None  # sigue sin responder: se puede reintentar


def test_una_clave_incorrecta_falla_sin_filtrar_credenciales(smtp_real, monkeypatch):
    monkeypatch.setenv("PQRS_SMTP_TIMEOUT", "2")  # tras el rechazo, el servidor de pruebas deja de responder: no esperar 15 s
    monkeypatch.setenv("PQRS_SMTP_PASSWORD", "clave-equivocada-999")
    func, _ = t.sesion("fer.func", "funcionario")
    s = t.solicitud_con_correo("Clave", "clave@mail.com")
    r = t.responder(func, s["id"], "correo")
    assert r.status_code == 502 and "clave-equivocada-999" not in r.text and sink.CLAVE not in r.text
    assert list(smtp_real.glob("*.eml")) == []
