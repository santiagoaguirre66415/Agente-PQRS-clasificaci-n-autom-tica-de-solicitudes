"""Pruebas de las herramientas para probar con un proveedor real (con servidores simulados: no envían nada)."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from tests import test_api as t  # configura la base de datos y las variables de prueba


def cargar(nombre):
    spec = importlib.util.spec_from_file_location(nombre, Path(__file__).parent.parent / "herramientas" / f"{nombre}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[nombre] = mod
    spec.loader.exec_module(mod)
    return mod


prueba = cargar("prueba_proveedor")
evento_wh = cargar("enviar_evento_webhook")


class SMTPFalsoConSock(t.SMTPFalso):
    class _Sock:
        def version(self):
            return "TLSv1.3"
    sock = _Sock()


@pytest.fixture
def smtp(monkeypatch):
    # El simulado guarda lo enviado en atributos de la clase BASE: se reinician ahí y se devuelve esa misma clase
    t.SMTPFalso.enviados, t.SMTPFalso.eventos, t.SMTPFalso.claves, t.SMTPFalso.falla = [], [], [], False
    monkeypatch.setattr(t.correo_mod, "SMTP", SMTPFalsoConSock)
    for k, v in {"PQRS_SMTP_HOST": "smtp.ejemplo.test", "PQRS_SMTP_USER": "atencion@ejemplo.test", "PQRS_SMTP_PASSWORD": "contraseña-super-secreta-777",
                 "PQRS_SMTP_FROM": "Atención <atencion@ejemplo.test>", "PQRS_URL_PUBLICA": "https://pqrs.ejemplo.test"}.items():
        monkeypatch.setenv(k, v)
    return t.SMTPFalso


def test_sin_confirmar_solo_prueba_la_conexion_y_no_envia_nada(smtp, capsys):
    assert prueba.main(["x", "--a", "yo@mail.com"]) == 0
    salida = capsys.readouterr().out
    assert smtp.enviados == [] and "TLS=TLSv1.3" in salida and "No se envió ningún correo" in salida
    assert [e[0] for e in smtp.eventos] == ["conectar", "starttls", "login"]


def test_con_confirmar_envia_verificacion_y_respuesta_y_opcionalmente_rebote(smtp, capsys):
    assert prueba.main(["x", "--a", "yo@mail.com", "--rebote", "noexiste@mail.com", "--confirmar"]) == 0
    assert [m["To"] for m in smtp.enviados] == ["yo@mail.com", "yo@mail.com", "noexiste@mail.com"]
    salida = capsys.readouterr().out
    assert "Revise A MANO" in salida and "Mostrar original" in salida and "Message-ID=<" in salida


def test_nunca_imprime_la_contrasena(smtp, capsys):
    prueba.main(["x", "--a", "yo@mail.com", "--confirmar"])
    salida = capsys.readouterr()
    assert "contraseña-super-secreta-777" not in salida.out + salida.err and "contraseña=(oculta)" in salida.out


@pytest.mark.parametrize("args", [["--a", "no-es-correo"], ["--a", "a@b.com\nBcc: x@y.com"], ["--a", "yo@mail.com", "--rebote", "tampoco"]])
def test_rechaza_direcciones_invalidas_sin_enviar(smtp, args):
    assert prueba.main(["x", *args, "--confirmar"]) == 2 and smtp.enviados == []


def test_confirmar_sin_destino_no_envia(smtp):
    assert prueba.main(["x", "--confirmar"]) == 2 and smtp.enviados == []


def test_sin_configuracion_lo_dice_y_no_intenta_conectar(monkeypatch, capsys):
    for k in ("PQRS_SMTP_HOST", "PQRS_SMTP_USER", "PQRS_SMTP_PASSWORD", "PQRS_SMTP_FROM"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("PQRS_SMTP_PASSWORD_FILE", raising=False)
    assert prueba.main(["x"]) == 2 and "FALTA" in capsys.readouterr().out


def test_una_clave_rechazada_se_informa_sin_filtrarla(smtp, capsys):
    smtp.falla = True
    assert prueba.main(["x", "--a", "yo@mail.com", "--confirmar"]) == 1
    salida = capsys.readouterr()
    assert "contraseña-super-secreta-777" not in salida.out + salida.err and smtp.enviados == []


# ------------------------------- enviar_evento_webhook -------------------------------
def test_los_avisos_firmados_por_la_herramienta_son_aceptados_por_la_app(monkeypatch):
    """El contrato de la herramienta y el de la app deben coincidir: si no, el proveedor real tampoco funcionaría."""
    monkeypatch.setenv("PQRS_WEBHOOK_SECRETO", "secreto-compartido-de-pruebas")
    t.seguridad.reiniciar_limites()
    cuerpo = evento_wh.construir("<inexistente@ejemplo.test>", "rebotado")
    ok = t.publico.post("/webhooks/correo", content=cuerpo, headers={"X-Firma": evento_wh.firmar(cuerpo, "secreto-compartido-de-pruebas"),
                                                                       "Content-Type": "application/json"})
    mala = t.publico.post("/webhooks/correo", content=cuerpo, headers={"X-Firma": evento_wh.firmar(cuerpo, "otro"), "Content-Type": "application/json"})
    viejo = evento_wh.construir("<x@y>", "rebotado", antiguedad=3600)
    vencido = t.publico.post("/webhooks/correo", content=viejo, headers={"X-Firma": evento_wh.firmar(viejo, "secreto-compartido-de-pruebas")})
    assert (ok.status_code, mala.status_code, vencido.status_code) == (200, 401, 400)


def test_la_herramienta_exige_el_secreto_por_entorno(monkeypatch, capsys):
    monkeypatch.delenv("PQRS_WEBHOOK_SECRETO", raising=False)
    assert evento_wh.main(["x", "--url", "http://localhost", "--mensaje-id", "<a@b>", "--evento", "rebotado"]) == 2
    assert "PQRS_WEBHOOK_SECRETO" in capsys.readouterr().out
