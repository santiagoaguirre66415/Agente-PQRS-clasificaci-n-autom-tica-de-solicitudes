"""Pruebas del verificador de SPF/DKIM/DMARC con un DNS simulado (no hacen consultas reales)."""
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

_ruta = Path(__file__).parent.parent / "herramientas" / "verificar_dns.py"
_spec = importlib.util.spec_from_file_location("verificar_dns", _ruta)
dns = importlib.util.module_from_spec(_spec)
sys.modules["verificar_dns"] = dns
_spec.loader.exec_module(dns)


def cliente(zona: dict[str, list[str]]) -> httpx.Client:
    """Un Google Public DNS de mentira: devuelve los TXT de `zona` con el formato real de dns.google/resolve."""
    consultas = []

    def handler(request: httpx.Request) -> httpx.Response:
        nombre = request.url.params["name"]
        consultas.append(nombre)
        respuestas = [{"name": nombre, "type": 16, "data": " ".join(f'"{t}"' for t in (z if isinstance(z, list) else [z]))}
                      for z in zona.get(nombre, [])]
        return httpx.Response(200, json={"Status": 0, "Answer": respuestas} if respuestas else {"Status": 3})
    c = httpx.Client(transport=httpx.MockTransport(handler))
    c.consultas = consultas
    return c


def niveles(resultado):
    return [n for n, _ in resultado]


# ------------------------------------ SPF ------------------------------------
@pytest.mark.parametrize("registro,esperado", [
    ("v=spf1 include:_spf.google.com -all", "OK"),
    ("v=spf1 include:_spf.google.com ~all", "OK"),
    ("v=spf1 include:_spf.google.com ?all", "AVISO"),
    ("v=spf1 +all", "FALTA"),
    ("v=spf1 include:_spf.google.com", "AVISO"),
])
def test_spf_segun_la_regla_final(registro, esperado):
    assert niveles(dns.evaluar_spf(["otra cosa", registro])) == [esperado]


def test_spf_con_redirect_es_valido():
    # Caso real: el SPF de gmail.com es «v=spf1 redirect=_spf.google.com»; no lleva "all" y es correcto
    assert niveles(dns.evaluar_spf(["v=spf1 redirect=_spf.google.com"])) == ["OK"]


def test_spf_ausente_o_duplicado_es_falta():
    assert niveles(dns.evaluar_spf(["google-site-verification=abc"])) == ["FALTA"]
    assert niveles(dns.evaluar_spf(["v=spf1 -all", "v=spf1 ~all"])) == ["FALTA"]


# ----------------------------------- DMARC -----------------------------------
def test_dmarc_segun_la_politica():
    assert niveles(dns.evaluar_dmarc(["v=DMARC1; p=reject; rua=mailto:r@x.com"])) == ["OK"]
    assert niveles(dns.evaluar_dmarc(["v=DMARC1; p=quarantine"])) == ["OK", "AVISO"]            # sin reportes
    assert niveles(dns.evaluar_dmarc(["v=DMARC1; p=none; rua=mailto:r@x.com"])) == ["AVISO"]    # solo observación
    assert niveles(dns.evaluar_dmarc(["v=DMARC1; rua=mailto:r@x.com"])) == ["FALTA"]            # sin política
    assert niveles(dns.evaluar_dmarc([])) == ["FALTA"]


# ------------------------------------ DKIM ------------------------------------
def test_dkim():
    assert niveles(dns.evaluar_dkim(None, None)) == ["AVISO"]                       # sin selector no se puede comprobar
    assert niveles(dns.evaluar_dkim(["v=DKIM1; k=rsa; p=MIIBIjANBg"], "google")) == ["OK"]
    assert niveles(dns.evaluar_dkim(["v=DKIM1; k=rsa; p="], "google")) == ["FALTA"]  # clave revocada
    assert niveles(dns.evaluar_dkim([], "google")) == ["FALTA"]


# ------------------------------- consulta completa -------------------------------
def test_verificar_un_dominio_bien_configurado():
    c = cliente({"ejemplo.com": ["v=spf1 include:_spf.google.com -all"],
                 "google._domainkey.ejemplo.com": ["v=DKIM1; k=rsa; p=MIIBIjANBg"],
                 "_dmarc.ejemplo.com": ["v=DMARC1; p=reject; rua=mailto:r@ejemplo.com"]})
    assert niveles(dns.verificar("Ejemplo.COM.", "google", c)) == ["OK", "OK", "OK"]
    assert c.consultas == ["ejemplo.com", "google._domainkey.ejemplo.com", "_dmarc.ejemplo.com"]


def test_verificar_un_dominio_sin_nada_marca_falta():
    assert niveles(dns.verificar("vacio.com", None, cliente({}))) == ["FALTA", "AVISO", "FALTA"]


def test_los_txt_largos_partidos_en_trozos_se_unen():
    c = cliente({"largo.com": [["v=spf1 include:a.com ", "include:b.com -all"]]})
    assert dns.consultar_txt("largo.com", c) == ["v=spf1 include:a.com include:b.com -all"]


@pytest.mark.parametrize("dominio", ["", "no es un dominio", "http://x.com", "x", "a..com", "x.com/ruta", "x.com;rm -rf", "-x.com"])
def test_dominios_invalidos_se_rechazan_sin_consultar(dominio):
    c = cliente({})
    with pytest.raises(ValueError):
        dns.verificar(dominio, None, c)
    assert c.consultas == []


def test_selectores_invalidos_se_rechazan():
    with pytest.raises(ValueError):
        dns.verificar("ejemplo.com", "../etc", cliente({}))


def test_el_codigo_de_salida_es_1_si_falta_algo_imprescindible(monkeypatch, capsys):
    monkeypatch.setattr(dns, "verificar", lambda d, s=None, c=None: [("OK", "bien"), ("FALTA", "mal")])
    assert dns.main(["x", "ejemplo.com"]) == 1
    monkeypatch.setattr(dns, "verificar", lambda d, s=None, c=None: [("OK", "bien"), ("AVISO", "regular")])
    assert dns.main(["x", "ejemplo.com"]) == 0
    assert "[AVISO]" in capsys.readouterr().out
    assert dns.main(["x"]) == 2


def test_un_error_de_red_no_muestra_un_traceback(monkeypatch, capsys):
    def falla(*a, **k):
        raise httpx.ConnectError("sin red")
    monkeypatch.setattr(dns, "verificar", falla)
    assert dns.main(["x", "ejemplo.com"]) == 2 and "No se pudo consultar" in capsys.readouterr().out
