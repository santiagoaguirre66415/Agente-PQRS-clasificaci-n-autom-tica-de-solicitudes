import hashlib
import hmac
import json
import os
import re
import smtplib
import time
import tempfile
from datetime import datetime, timedelta

# Base de datos temporal, sin IA y con parámetros ligeros: pruebas deterministas y rápidas.
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"
os.environ["PQRS_RATE_LIMIT"] = "1000"
os.environ["PQRS_ANTIBOT"] = "0"  # las pruebas del formulario público lo activan por su cuenta
os.environ["PQRS_SCRYPT_N"] = "1024"  # en producción el valor por defecto es 2**17
for k in ("PQRS_IA_PROVIDER", "PQRS_IA_KEY", "PQRS_IA_MODEL", "PQRS_IA_URL",
          "PQRS_ADMIN_USER", "PQRS_ADMIN_PASSWORD", "PQRS_TRUST_PROXY"):
    os.environ.pop(k, None)

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import seguridad  # noqa: E402
from app import agente, antibot  # noqa: E402
from app import correo as correo_mod  # noqa: E402
from app.agente import AREAS, anonimizar, clasificar_reglas  # noqa: E402
from app.db import Auditoria, SessionLocal, Sesion, Solicitud, Usuario, sumar_dias_habiles  # noqa: E402
from app.main import app  # noqa: E402

CLAVE = "una-clave-larga-123"
publico = TestClient(app)  # ciudadano o atacante sin sesión


@pytest.fixture(autouse=True)
def _limpiar_limites():
    seguridad.reiniciar_limites()
    yield
    seguridad.reiniciar_limites()


def crear_usuario_directo(nombre, rol, clave=CLAVE):
    with SessionLocal() as db:
        if not db.scalar(select(Usuario).where(Usuario.usuario == nombre)):
            db.add(Usuario(usuario=nombre, clave_hash=seguridad.hashear(clave), rol=rol))
            db.commit()


def sesion(nombre, rol):
    """Cliente autenticado como `nombre` (lo crea si no existe)."""
    crear_usuario_directo(nombre, rol)
    r = publico.post("/auth/login", json={"usuario": nombre, "clave": CLAVE})
    assert r.status_code == 200, r.text
    return TestClient(app, headers={"Authorization": "Bearer " + r.json()["token"]}), r.json()["token"]


def registrar(ciudadano, texto, **extra):
    r = publico.post("/solicitudes", json={"ciudadano": ciudadano, "texto": texto, **extra})
    assert r.status_code == 201, r.text
    admin, _ = sesion("ana.admin", "admin")
    return admin.get(f"/solicitudes/{r.json()['id']}").json()


def eventos(accion=None):
    with SessionLocal() as db:
        q = select(Auditoria).order_by(Auditoria.id)
        return [e for e in db.scalars(q) if accion is None or e.accion == accion]


# ------------------------------- clasificador -------------------------------
def test_cable_caido_es_prioridad_alta_energia():
    r = clasificar_reglas("Hay un cable caído frente a mi casa, es un peligro")
    assert r.categoria == "energia" and r.prioridad == "alta"


def test_cobro_es_reclamo_de_facturacion():
    r = clasificar_reglas("Me cobraron un valor muy alto en la factura")
    assert r.tipo == "reclamo" and r.categoria == "facturacion"


def test_sugerencia_es_prioridad_baja():
    r = clasificar_reglas("Sugiero habilitar pagos por app")
    assert r.tipo == "sugerencia" and r.prioridad == "baja"


def test_extraer_json_tolera_texto_alrededor():
    assert agente._extraer_json('Claro:\n{"tipo": "queja"}\nlisto') == {"tipo": "queja"}


def test_anonimizar_quita_correos_y_numeros_largos():
    t = anonimizar("Escriban a juan.perez@mail.com o llamen al 300 123 4567, cédula 1012345678. Vivo en la calle 14 # 5")
    assert "@" not in t and "4567" not in t and "1012345678" not in t
    assert "[correo]" in t and "[número]" in t and "calle 14 # 5" in t  # no destruye direcciones cortas


# ------------------------- IA gratuita (opcional y simulada) -------------------------
class Proveedor:
    """Servidor simulado compatible con OpenAI. Ninguna prueba toca la red real ni gasta cuota."""

    def __init__(self, respuesta=None, status=200):
        self.llamadas, self.status = [], status
        self.respuesta = respuesta if respuesta is not None else {
            "tipo": "reclamo", "categoria": "facturacion", "prioridad": "media",
            "resumen": "Cobro alto", "respuesta_sugerida": "Revisaremos su factura."}

    def handler(self, request):
        self.llamadas.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "rechazado"})
        contenido = self.respuesta if isinstance(self.respuesta, str) else json.dumps(self.respuesta)
        return httpx.Response(200, json={"choices": [{"message": {"content": contenido}}]})

    def mensaje_usuario(self):
        return json.loads(self.llamadas[-1].content)["messages"][1]["content"]


@pytest.fixture
def ia(monkeypatch):
    """Activa la IA con un proveedor simulado. Uso: p = ia(Proveedor(...))"""
    agente.reiniciar_estado()
    monkeypatch.setenv("PQRS_IA_PROVIDER", "groq")
    monkeypatch.setenv("PQRS_IA_KEY", "clave-de-prueba-123")
    monkeypatch.setenv("PQRS_IA_MODEL", "modelo-gratis")

    def instalar(proveedor=None):
        proveedor = proveedor or Proveedor()
        monkeypatch.setattr(agente, "_nuevo_cliente",
                            lambda: httpx.Client(transport=httpx.MockTransport(proveedor.handler)))
        return proveedor
    yield instalar
    agente.reiniciar_estado()


def test_ia_apagada_por_defecto_no_hace_ninguna_llamada_de_red(monkeypatch):
    def prohibido():
        raise AssertionError("Se intentó abrir una conexión de red con la IA apagada")
    monkeypatch.setattr(agente, "_nuevo_cliente", prohibido)
    r = agente.clasificar("Me cobraron un valor muy alto en la factura")
    assert r.metodo == "reglas" and not agente.ia_activa()


def test_ia_usa_la_respuesta_del_proveedor_y_envia_la_clave_solo_por_https(ia):
    p = ia()
    r = agente.clasificar("Me cobraron mucho más de lo normal este mes")
    assert r.metodo == "ia" and r.categoria == "facturacion" and r.area == AREAS["facturacion"]
    assert str(p.llamadas[0].url) == "https://api.groq.com/openai/v1/chat/completions"
    assert p.llamadas[0].headers["authorization"] == "Bearer clave-de-prueba-123"


def test_ia_no_recibe_correos_ni_numeros_largos(ia):
    p = ia()
    agente.clasificar("Escriban a juan.perez@mail.com o llamen al 300 123 4567 por la fuga de agua")
    enviado = p.mensaje_usuario()
    assert "@" not in enviado and "4567" not in enviado and "[correo]" in enviado


def test_ia_nunca_baja_la_prioridad_de_las_reglas(ia):
    ia(Proveedor({"tipo": "peticion", "categoria": "energia", "prioridad": "baja",
                  "resumen": "r", "respuesta_sugerida": "ok"}))
    r = agente.clasificar("Hay un cable caído frente a mi casa, es un peligro")  # las reglas dicen "alta"
    assert r.metodo == "ia" and r.prioridad == "alta"


def test_ia_puede_subir_la_prioridad(ia):
    ia(Proveedor({"tipo": "peticion", "categoria": "otros", "prioridad": "alta",
                  "resumen": "r", "respuesta_sugerida": "ok"}))
    assert agente.clasificar("Quisiera solicitar un certificado de mi servicio").prioridad == "alta"


def test_ia_elimina_enlaces_de_phishing(ia):
    ia(Proveedor({"tipo": "queja", "categoria": "agua", "prioridad": "alta", "resumen": "ver http://malo.example/x",
                  "respuesta_sugerida": "Entre a https://phishing.example/login ya"}))
    r = agente.clasificar("Llevamos dos días sin agua en el barrio")
    assert "http" not in r.respuesta_sugerida and "http" not in r.resumen


@pytest.mark.parametrize("respuesta", ["no es json", {"tipo": "inventado", "categoria": "agua", "prioridad": "alta"}, {}])
def test_respuesta_invalida_usa_reglas_y_no_pausa_la_ia(ia, respuesta):
    p = ia(Proveedor(respuesta))
    assert agente.clasificar("Llevamos dos días sin agua en el barrio").metodo == "reglas"
    p.respuesta = {"tipo": "queja", "categoria": "agua", "prioridad": "alta", "resumen": "r", "respuesta_sugerida": "ok"}
    assert agente.clasificar("Otra solicitud sobre una fuga de agua").metodo == "ia"  # no quedó pausada


def test_error_del_proveedor_pausa_la_ia_y_no_insiste(ia):
    p = ia(Proveedor(status=429))
    assert agente.clasificar("Llevamos dos días sin agua en el barrio").metodo == "reglas"
    assert agente.clasificar("Otra solicitud sobre una fuga de agua").metodo == "reglas"
    assert len(p.llamadas) == 1  # tras el rechazo no se vuelve a llamar durante la pausa


def test_la_clave_no_se_imprime_aunque_el_proveedor_falle(ia, capsys):
    ia(Proveedor(status=401))
    agente.clasificar("Llevamos dos días sin agua en el barrio")
    salida = capsys.readouterr()
    assert "clave-de-prueba-123" not in salida.out + salida.err


def test_tope_diario_local(ia, monkeypatch):
    monkeypatch.setenv("PQRS_IA_MAX_DIA", "2")
    p = ia()
    for i in range(5):
        agente.clasificar(f"Reclamo número {i} por cobro excesivo en la factura")
    assert len(p.llamadas) == 2


def test_tope_por_minuto_local(ia, monkeypatch):
    monkeypatch.setenv("PQRS_IA_MAX_MIN", "1")
    p = ia()
    for i in range(3):
        agente.clasificar(f"Reclamo número {i} por cobro excesivo en la factura")
    assert len(p.llamadas) == 1


def test_openrouter_solo_con_modelos_gratuitos(monkeypatch):
    monkeypatch.setenv("PQRS_IA_PROVIDER", "openrouter")
    monkeypatch.setenv("PQRS_IA_KEY", "k")
    monkeypatch.setenv("PQRS_IA_MODEL", "openai/gpt-4o")  # de pago
    assert not agente.ia_activa()
    monkeypatch.setenv("PQRS_IA_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
    assert agente.ia_activa()


def test_no_se_envia_la_clave_por_http(monkeypatch):
    monkeypatch.setenv("PQRS_IA_PROVIDER", "custom")
    monkeypatch.setenv("PQRS_IA_URL", "http://servidor.example/v1")
    monkeypatch.setenv("PQRS_IA_KEY", "k")
    monkeypatch.setenv("PQRS_IA_MODEL", "m")
    assert not agente.ia_activa()


def test_el_texto_enviado_se_limita_de_tamano(ia):
    p = ia()
    agente.clasificar("fuga de agua " * 600)
    assert len(p.mensaje_usuario()) < 1700


def test_la_api_guarda_que_la_clasificacion_uso_ia(ia):
    ia()
    r = publico.post("/solicitudes", json={"ciudadano": "Ivo", "texto": "Me cobraron de más en el recibo de este mes"})
    admin, _ = sesion("ana.admin", "admin")
    assert admin.get(f"/solicitudes/{r.json()['id']}").json()["metodo"] == "ia"


def test_el_proyecto_no_depende_de_la_api_de_claude():
    raiz = os.path.join(os.path.dirname(__file__), "..")
    for nombre in ("requirements.in", "requirements.txt"):
        assert "anthropic" not in open(os.path.join(raiz, nombre), encoding="utf-8").read().lower(), nombre
    for archivo in os.listdir(os.path.join(raiz, "app")):
        if archivo.endswith(".py"):
            assert "anthropic" not in open(os.path.join(raiz, "app", archivo), encoding="utf-8").read().lower(), archivo


# ------------------------------ envío ciudadano ------------------------------
def test_crear_y_consultar_solicitud():
    s = registrar("Ana", "Llevamos dos días sin agua en el sector")
    assert s["categoria"] == "agua" and s["estado"] == "nuevo" and s["metodo"] == "reglas"


def test_validacion_de_entrada():
    assert publico.post("/solicitudes", json={"ciudadano": "A", "texto": "corto"}).status_code == 422


@pytest.mark.parametrize("cuerpo", [
    {"ciudadano": "Ana", "correo": "no-es-correo", "texto": "Llevamos tres dias sin agua"},
    {"ciudadano": "   ", "texto": "Llevamos tres dias sin agua"},
    {"ciudadano": "Ana", "texto": "            "},
    {"ciudadano": "Ana", "texto": "aaaaaaaaaaaaaaaa"},
])
def test_rechaza_datos_incoherentes(cuerpo):
    assert publico.post("/solicitudes", json=cuerpo).status_code == 422


def test_limpia_espacios_y_normaliza_correo():
    s = registrar("  Pedro   Gil ", "  Hay   una   fuga de agua en mi calle  ", correo="Pedro@Mail.COM")
    assert s["ciudadano"] == "Pedro Gil" and s["correo"] == "pedro@mail.com"


def test_duplicado_en_24_horas_devuelve_409():
    cuerpo = {"ciudadano": "Rosa", "texto": "El transformador de mi cuadra hace ruido fuerte"}
    assert publico.post("/solicitudes", json=cuerpo).status_code == 201
    assert publico.post("/solicitudes", json=cuerpo).status_code == 409


def test_el_envio_publico_no_expone_la_clasificacion_interna():
    r = publico.post("/solicitudes", json={"ciudadano": "Mario", "texto": "Hay un cable caído y es un peligro"}).json()
    assert set(r) == {"id", "estado", "mensaje"}


def test_asignacion_masiva_ignorada():
    r = publico.post("/solicitudes", json={"ciudadano": "Hack", "texto": "Fuga de agua grande en la esquina",
                                           "estado": "resuelto", "id": 9999, "prioridad": "baja"}).json()
    admin, _ = sesion("ana.admin", "admin")
    s = admin.get(f"/solicitudes/{r['id']}").json()
    assert r["id"] != 9999 and s["estado"] == "nuevo"


def test_limite_de_frecuencia(monkeypatch):
    monkeypatch.setenv("PQRS_RATE_LIMIT", "3")
    codigos = [publico.post("/solicitudes", json={"ciudadano": f"Bot{i}", "texto": f"Mensaje automático número {i} sobre fuga"}).status_code
               for i in range(5)]
    assert codigos == [201, 201, 201, 429, 429]


# ----------------------------- gestión de estados -----------------------------
RESPUESTA = "Hola, revisamos su caso y ya quedó resuelto. Gracias por escribirnos."


def responder(cli, sid, canal="otro", texto=RESPUESTA):
    return cli.post(f"/solicitudes/{sid}/responder", json={"respuesta": texto, "canal": canal})


def test_cambiar_estado_y_estadisticas():
    func, _ = sesion("fer.func", "funcionario")
    s = registrar("Luis", "Solicito certificado de paz y salvo")
    assert func.patch(f"/solicitudes/{s['id']}", json={"estado": "en_proceso"}).json()["estado"] == "en_proceso"
    assert func.patch(f"/solicitudes/{s['id']}", json={"estado": "xx"}).status_code == 422
    assert responder(func, s["id"]).status_code == 200
    assert func.get("/estadisticas").json()["por_estado"].get("resuelto", 0) >= 1


def test_no_se_puede_resolver_sin_responder():
    func, _ = sesion("fer.func", "funcionario")
    s = registrar("Eva", "Solicito copia de mi factura anterior")
    r = func.patch(f"/solicitudes/{s['id']}", json={"estado": "resuelto"})
    assert r.status_code == 409 and "Responda" in r.json()["detail"]
    assert func.get(f"/solicitudes/{s['id']}").json()["estado"] == "nuevo"


def test_resuelto_en_es_coherente_con_el_estado():
    func, _ = sesion("fer.func", "funcionario")
    s = registrar("Eva Lía", "Solicito copia de mi factura anterior del servicio")
    assert s["resuelto_en"] is None and s["respondido_en"] is None
    assert responder(func, s["id"]).json()["resuelto_en"] is not None
    assert func.patch(f"/solicitudes/{s['id']}", json={"estado": "nuevo"}).status_code == 409
    r = func.patch(f"/solicitudes/{s['id']}", json={"estado": "en_proceso"}).json()
    assert r["estado"] == "en_proceso" and r["resuelto_en"] is None and r["respuesta_final"] == RESPUESTA  # la respuesta se conserva
    assert func.patch(f"/solicitudes/{s['id']}", json={"estado": "resuelto"}).status_code == 200  # ya fue respondida


def test_la_base_de_datos_rechaza_valores_invalidos():
    from sqlalchemy.exc import IntegrityError
    with SessionLocal() as db:
        db.add(Solicitud(ciudadano="x", texto="t", tipo="inventado"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        db.add(Solicitud(ciudadano="x", texto="t", estado="resuelto"))  # resuelta sin fecha ni respuesta
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        db.add(Solicitud(ciudadano="x", texto="t", estado="resuelto", resuelto_en=seguridad.ahora() + timedelta(days=1)))
        with pytest.raises(IntegrityError):  # resuelta sin respuesta oficial
            db.commit()
        db.rollback()
        db.add(Solicitud(ciudadano="x", texto="t", respuesta_final="solo texto"))  # respuesta incompleta
        with pytest.raises(IntegrityError):
            db.commit()


# ------------------------------ responder y enviar ------------------------------
class SMTPFalso:
    """Reemplaza smtplib.SMTP: registra lo que se enviaría. Ninguna prueba envía un correo real."""
    enviados, eventos, claves, falla = [], [], [], False

    def __init__(self, host, port, timeout=None):
        SMTPFalso.eventos.append(("conectar", host, port))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        SMTPFalso.eventos.append(("starttls",))

    def login(self, usuario, clave):
        if SMTPFalso.falla:
            raise smtplib.SMTPAuthenticationError(535, b"rechazado")
        SMTPFalso.eventos.append(("login", usuario))
        SMTPFalso.claves.append(clave)

    def send_message(self, msg):
        SMTPFalso.enviados.append(msg)


@pytest.fixture
def smtp(monkeypatch):
    SMTPFalso.enviados, SMTPFalso.eventos, SMTPFalso.claves, SMTPFalso.falla = [], [], [], False
    monkeypatch.setattr(correo_mod, "SMTP", SMTPFalso)
    for k, v in {"PQRS_SMTP_HOST": "smtp.ejemplo.test", "PQRS_SMTP_USER": "atencion@ejemplo.test",
                 "PQRS_SMTP_PASSWORD": "contraseña-secreta-123", "PQRS_SMTP_FROM": "Atención <atencion@ejemplo.test>"}.items():
        monkeypatch.setenv(k, v)
    return SMTPFalso


def solicitud_con_correo(nombre="Cora", correo="cora@mail.com", sufijo="", verificado=True):
    s = registrar(nombre, f"Me cobraron de más en el recibo de este mes {nombre}{sufijo}", correo=correo)
    if verificado:  # simula que la persona ya confirmó su correo (el flujo real se prueba más abajo)
        with SessionLocal() as db:
            db.get(Solicitud, s["id"]).correo_verificado = True
            db.commit()
    return s


def test_responder_por_correo_envia_guarda_resuelve_y_audita(smtp):
    func, _ = sesion("fer.func", "funcionario")
    s = solicitud_con_correo("Cora")
    editado = "Estimada Cora, corregimos el cobro y le devolveremos la diferencia.\n\nSaludos."
    r = responder(func, s["id"], "correo", editado)
    assert r.status_code == 200
    d = r.json()
    assert d["estado"] == "resuelto" and d["canal_respuesta"] == "correo" and d["respondido_por"] == "fer.func"
    assert d["respuesta_final"] == editado != d["respuesta_sugerida"]  # se guarda lo que de verdad se envió
    assert len(smtp.enviados) == 1
    msg = smtp.enviados[0]
    assert msg["To"] == "cora@mail.com" and f"n.º {s['id']}" in msg["Subject"]
    assert "corregimos el cobro" in msg.get_content()
    ev = [e[0] for e in smtp.eventos]
    assert ev.index("starttls") < ev.index("login")  # nunca se envía la contraseña sin cifrar
    reg = [e for e in eventos("responder_solicitud") if e.objeto == f"solicitud {s['id']}"][-1]
    assert reg.usuario == "fer.func" and "c***@mail.com" in reg.detalle and "cora@mail.com" not in reg.detalle
    todo = " ".join(f"{e.usuario}{e.objeto}{e.detalle}" for e in eventos())
    assert "contraseña-secreta-123" not in todo


def test_no_se_responde_dos_veces_ni_se_manda_dos_correos(smtp):
    func, _ = sesion("fer.func", "funcionario")
    s = solicitud_con_correo("Dora")
    assert responder(func, s["id"], "correo").status_code == 200
    assert responder(func, s["id"], "correo").status_code == 409
    assert len(smtp.enviados) == 1


def test_canal_correo_sin_correo_o_sin_configuracion_no_marca_respondida(monkeypatch):
    for k in ("PQRS_SMTP_HOST", "PQRS_SMTP_USER", "PQRS_SMTP_PASSWORD", "PQRS_SMTP_FROM"):
        monkeypatch.delenv(k, raising=False)
    func, _ = sesion("fer.func", "funcionario")
    con_correo = solicitud_con_correo("Eli")
    r = responder(func, con_correo["id"], "correo")
    assert r.status_code == 409 and "no está configurado" in r.json()["detail"]
    sin_correo = registrar("Fer", "Solicito un certificado de paz y salvo de mi servicio")
    monkeypatch.setenv("PQRS_SMTP_HOST", "h")
    for k in ("PQRS_SMTP_USER", "PQRS_SMTP_PASSWORD", "PQRS_SMTP_FROM"):
        monkeypatch.setenv(k, "x")
    r = responder(func, sin_correo["id"], "correo")
    assert r.status_code == 409 and "no tiene correo" in r.json()["detail"]
    for sid in (con_correo["id"], sin_correo["id"]):
        assert func.get(f"/solicitudes/{sid}").json()["respondido_en"] is None


def test_canal_otro_registra_la_respuesta_sin_enviar_nada(smtp):
    func, _ = sesion("fer.func", "funcionario")
    s = registrar("Gus", "Hay una fuga de agua frente a mi casa desde ayer")
    r = responder(func, s["id"], "otro").json()
    assert r["canal_respuesta"] == "otro" and r["estado"] == "resuelto"
    assert smtp.enviados == [] and smtp.eventos == []


def test_si_el_envio_falla_la_solicitud_sigue_sin_responder_y_se_puede_reintentar(smtp):
    func, _ = sesion("fer.func", "funcionario")
    s = solicitud_con_correo("Hana")
    smtp.falla = True
    r = responder(func, s["id"], "correo")
    assert r.status_code == 502 and "contraseña-secreta-123" not in r.text
    d = func.get(f"/solicitudes/{s['id']}").json()
    assert d["respondido_en"] is None and d["estado"] != "resuelto"
    assert any(e.accion == "envio_fallido" and e.objeto == f"solicitud {s['id']}" for e in eventos())
    smtp.falla = False
    assert responder(func, s["id"], "correo").status_code == 200


def test_solo_funcionario_o_admin_pueden_responder(smtp):
    s = solicitud_con_correo("Ines")
    consulta, _ = sesion("pedro.consulta", "consulta")
    assert responder(consulta, s["id"], "otro").status_code == 403
    assert responder(publico, s["id"], "otro").status_code == 401
    assert smtp.enviados == []


@pytest.mark.parametrize("cuerpo", [{"respuesta": "ok gracias", "canal": "otro"}, {"respuesta": "Respuesta suficientemente larga", "canal": "sms"},
                                    {"respuesta": "          ", "canal": "otro"}, {"respuesta": "x " * 3000, "canal": "otro"}])
def test_validacion_de_la_respuesta(cuerpo):
    func, _ = sesion("fer.func", "funcionario")
    s = registrar("Val", f"Solicito revisión de mi factura de este mes por favor {os.urandom(4).hex()}")
    assert func.post(f"/solicitudes/{s['id']}/responder", json=cuerpo).status_code == 422


def test_limite_de_correos_por_funcionario(smtp, monkeypatch):
    monkeypatch.setenv("PQRS_MAIL_LIMIT", "2")
    func, _ = sesion("fer.func", "funcionario")
    codigos = [responder(func, solicitud_con_correo(f"Lim{i}", f"lim{i}@mail.com", str(i))["id"], "correo").status_code for i in range(3)]
    assert codigos == [200, 200, 429] and len(smtp.enviados) == 2


def test_el_correo_del_ciudadano_no_admite_inyeccion_de_cabeceras():
    r = publico.post("/solicitudes", json={"ciudadano": "Mal", "texto": "Solicito revisión de mi factura de este mes",
                                           "correo": "a@b.com\nBcc: victima@mail.com"})
    assert r.status_code == 422


def test_enviar_rechaza_destinos_con_saltos_de_linea(smtp):
    with pytest.raises(correo_mod.ErrorCorreo):
        correo_mod.enviar("a@b.com\nBcc: victima@mail.com", "Asunto", "Cuerpo")
    assert smtp.enviados == []


def test_endpoint_de_configuracion_no_expone_secretos(smtp):
    func, _ = sesion("fer.func", "funcionario")
    r = func.get("/config")
    assert r.json()["correo_configurado"] is True and "secret" not in r.text and "contraseña" not in r.text
    assert publico.get("/config").status_code == 401


# --------------------- verificación del correo del ciudadano ---------------------
@pytest.fixture
def url_publica(monkeypatch):
    monkeypatch.setenv("PQRS_URL_PUBLICA", "https://pqrs.ejemplo.test")


def enlace_de(msg):
    m = re.search(r"https://pqrs\.ejemplo\.test/verificar/([A-Za-z0-9_-]+)", msg.get_content())
    assert m, "el correo no trae el enlace de verificación"
    return m.group(1)


def enviar_solicitud(nombre, correo, **kw):
    return publico.post("/solicitudes", json={"ciudadano": nombre, "correo": correo,
                                              "texto": f"Quiero saber el estado de mi reclamo número {nombre} por la factura", **kw})


def estado_correo(sid):
    admin, _ = sesion("ana.admin", "admin")
    return admin.get(f"/solicitudes/{sid}").json()["correo_verificado"]


def test_registrar_con_correo_envia_verificacion_sin_datos_del_caso(smtp, url_publica):
    r = enviar_solicitud("Vera", "vera@mail.com")
    assert r.status_code == 201 and "confirmar su dirección" in r.json()["mensaje"]
    assert len(smtp.enviados) == 1
    msg = smtp.enviados[0]
    assert msg["To"] == "vera@mail.com" and "Confirme su correo" in msg["Subject"]
    assert "reclamo" not in msg.get_content() and "factura" not in msg.get_content()  # ningún dato del caso
    assert not estado_correo(r.json()["id"])


def test_abrir_el_enlace_no_confirma_solo_el_boton_lo_hace(smtp, url_publica):
    sid = enviar_solicitud("Wanda", "wanda@mail.com").json()["id"]
    token = enlace_de(smtp.enviados[0])
    pagina = publico.get(f"/verificar/{token}")
    assert pagina.status_code == 200 and "Confirmar mi correo" in pagina.text
    assert not estado_correo(sid)  # un antivirus que abre el enlace no confirma por la persona
    assert "Correo confirmado" in publico.post(f"/verificar/{token}").text
    assert estado_correo(sid)
    assert publico.post(f"/verificar/{token}").status_code == 400  # el enlace sirve una sola vez
    assert publico.get(f"/verificar/{token}").status_code == 400


def test_sin_verificar_no_se_puede_responder_por_correo_y_con_verificar_si(smtp, url_publica):
    func, _ = sesion("fer.func", "funcionario")
    sid = enviar_solicitud("Xena", "xena@mail.com").json()["id"]
    r = responder(func, sid, "correo")
    assert r.status_code == 409 and "no está verificado" in r.json()["detail"]
    assert len(smtp.enviados) == 1  # solo el de verificación: no se filtró ninguna respuesta
    publico.post(f"/verificar/{enlace_de(smtp.enviados[0])}")
    assert responder(func, sid, "correo").status_code == 200 and len(smtp.enviados) == 2


def test_un_solo_correo_de_verificacion_por_direccion_cada_24h(smtp, url_publica):
    enviar_solicitud("Yeni", "yeni@mail.com")
    enviar_solicitud("Yeni Dos", "yeni@mail.com")
    assert len(smtp.enviados) == 1  # así nadie puede inundar de mensajes a una dirección ajena


def test_una_direccion_ya_verificada_no_se_vuelve_a_pedir(smtp, url_publica):
    enviar_solicitud("Zoe", "zoe@mail.com")
    publico.post(f"/verificar/{enlace_de(smtp.enviados[0])}")
    sid2 = enviar_solicitud("Zoe Dos", "zoe@mail.com").json()["id"]
    assert estado_correo(sid2) and len(smtp.enviados) == 1


def test_tope_global_diario_de_verificaciones(smtp, url_publica, monkeypatch):
    monkeypatch.setenv("PQRS_VERIF_MAX_DIA", "1")
    enviar_solicitud("Ada", "ada@mail.com")
    enviar_solicitud("Bea", "bea@mail.com")
    assert len(smtp.enviados) == 1


def test_si_falla_la_verificacion_la_solicitud_igual_se_registra(smtp, url_publica):
    smtp.falla = True
    r = enviar_solicitud("Cris", "cris@mail.com")
    assert r.status_code == 201 and "confirmar su dirección" not in r.json()["mensaje"]


def test_el_enlace_usa_la_url_configurada_y_no_el_host_recibido(smtp, url_publica):
    enviar_solicitud("Dani", "dani@mail.com", **{})  # sin cabeceras raras
    publico.post("/solicitudes", json={"ciudadano": "Eri", "correo": "eri@mail.com", "texto": "Consulta sobre mi factura de agua del mes"},
                 headers={"Host": "malo.example.test", "X-Forwarded-Host": "malo.example.test"})
    for msg in smtp.enviados:
        assert enlace_de(msg) and "malo.example" not in msg.get_content()


@pytest.mark.parametrize("url", ["", "http://pqrs.ejemplo.test"])
def test_sin_url_publica_https_no_se_envia_verificacion(smtp, monkeypatch, url):
    monkeypatch.setenv("PQRS_URL_PUBLICA", url)
    assert enviar_solicitud(f"Fabi{len(url)}", f"fabi{len(url)}@mail.com").status_code == 201
    assert smtp.enviados == []


def test_enlaces_mal_formados_inexistentes_o_vencidos_se_rechazan(smtp, url_publica):
    for token in ("corto", "a" * 30, "x" * 80, "..%2F..%2Fetc"):
        assert publico.get(f"/verificar/{token}").status_code in (400, 404)
        assert publico.post(f"/verificar/{token}").status_code in (400, 404)
    sid = enviar_solicitud("Gigi", "gigi@mail.com").json()["id"]
    token = enlace_de(smtp.enviados[0])
    with SessionLocal() as db:
        db.get(Solicitud, sid).verif_expira = seguridad.ahora() - timedelta(minutes=1)
        db.commit()
    assert publico.post(f"/verificar/{token}").status_code == 400
    assert not estado_correo(sid)


def test_el_token_se_guarda_solo_como_hash_y_la_verificacion_se_audita_enmascarada(smtp, url_publica):
    sid = enviar_solicitud("Hugo", "hugo@mail.com").json()["id"]
    token = enlace_de(smtp.enviados[0])
    with SessionLocal() as db:
        fila = db.get(Solicitud, sid)
        assert len(fila.verif_hash) == 64 and token not in fila.verif_hash
        assert token not in " ".join(str(v) for v in vars(fila).values())
    publico.post(f"/verificar/{token}")
    reg = [e for e in eventos("correo_verificado") if e.objeto == f"solicitud {sid}"][-1]
    assert reg.detalle == "h***@mail.com" and token not in reg.detalle


def test_la_contrasena_smtp_puede_leerse_de_un_archivo(smtp, monkeypatch, tmp_path):
    archivo = tmp_path / "smtp_clave"
    archivo.write_text("clave-en-archivo-456\n", encoding="utf-8")
    monkeypatch.delenv("PQRS_SMTP_PASSWORD")
    monkeypatch.setenv("PQRS_SMTP_PASSWORD_FILE", str(archivo))
    assert correo_mod.configurado()
    correo_mod.enviar("a@b.com", "Asunto", "Cuerpo")
    assert smtp.claves == ["clave-en-archivo-456"]  # sin el salto de línea final
    monkeypatch.setenv("PQRS_SMTP_PASSWORD_FILE", str(tmp_path / "no_existe"))
    assert not correo_mod.configurado()


# ------------------------- formulario público y anti-bots -------------------------
@pytest.fixture
def con_antibot(monkeypatch):
    monkeypatch.setenv("PQRS_ANTIBOT", "1")
    monkeypatch.setenv("PQRS_ANTIBOT_BITS", "8")      # barato para pruebas; en producción 16 por defecto
    monkeypatch.setenv("PQRS_ANTIBOT_MIN_SEG", "0")
    monkeypatch.setenv("PQRS_PERMITIR_AVISO_INCOMPLETO", "1")  # el aviso de privacidad se prueba aparte
    antibot.reiniciar()


def resolver_reto(reto, bits=8, valida=True):
    n = 0
    while True:
        ok = antibot.ceros_iniciales(hashlib.sha256(f"{reto}:{n}".encode()).digest()) >= bits
        if ok == valida:
            return str(n)
        n += 1


def post_publico(nombre="Pía", texto=None, *, reto=None, prueba=None, acepto=True, web="", **extra):
    texto = texto or f"Quiero saber por qué me llegó un cobro doble {nombre} {os.urandom(3).hex()}"
    if reto is None:
        reto = publico.get("/api/reto").json()["reto"]
    if prueba is None:
        prueba = resolver_reto(reto)
    return publico.post("/solicitudes", json={"ciudadano": nombre, "texto": texto, "reto": reto, "prueba": prueba,
                                              "acepto": acepto, "web": web, **extra})


def test_el_reto_se_pide_sin_sesion_y_trae_la_dificultad(con_antibot):
    r = publico.get("/api/reto").json()
    assert set(r) == {"reto", "bits"} and r["bits"] == 8


def test_envio_publico_con_prueba_valida_y_autorizacion(con_antibot):
    r = post_publico("Pía")
    assert r.status_code == 201 and r.json()["id"] > 0
    admin, _ = sesion("ana.admin", "admin")
    d = admin.get(f"/solicitudes/{r.json()['id']}").json()
    assert d["autorizacion_en"] is not None  # queda constancia de cuándo autorizó el tratamiento de datos


def test_sin_reto_o_con_prueba_falsa_se_rechaza(con_antibot):
    assert publico.post("/solicitudes", json={"ciudadano": "Bot", "texto": "Mensaje automático de relleno aquí", "acepto": True}).status_code == 400
    reto = publico.get("/api/reto").json()["reto"]
    assert post_publico("Bot2", reto=reto, prueba=resolver_reto(reto, valida=False)).status_code == 400


def test_un_reto_sirve_una_sola_vez(con_antibot):
    reto = publico.get("/api/reto").json()["reto"]
    prueba = resolver_reto(reto)
    assert post_publico("Uno", reto=reto, prueba=prueba).status_code == 201
    assert post_publico("Dos", reto=reto, prueba=prueba).status_code == 400  # repetir la misma prueba no sirve


def test_un_reto_manipulado_o_inventado_se_rechaza(con_antibot):
    reto = publico.get("/api/reto").json()["reto"]
    alterado = reto[:-1] + ("0" if reto[-1] != "0" else "1")
    assert post_publico("Mani", reto=alterado, prueba=resolver_reto(alterado)).status_code == 400
    inventado = f"{int(time.time()) - 10}.abcdef0123456789.{'0' * 32}"
    assert post_publico("Inve", reto=inventado, prueba=resolver_reto(inventado)).status_code == 400


def test_un_reto_resuelto_demasiado_rapido_no_lo_hizo_una_persona(con_antibot, monkeypatch):
    monkeypatch.setenv("PQRS_ANTIBOT_MIN_SEG", "30")
    assert post_publico("Veloz").status_code == 400


def test_un_reto_vencido_se_rechaza(con_antibot, monkeypatch):
    monkeypatch.setattr(antibot, "_MAX_EDAD", -1)
    assert post_publico("Viejo").status_code == 400


def test_sin_autorizacion_de_datos_no_se_recibe(con_antibot):
    r = post_publico("Sinaut", acepto=False)
    assert r.status_code == 422 and "autorizar el tratamiento" in r.json()["detail"]


def test_el_campo_trampa_relleno_aparenta_exito_pero_no_guarda(con_antibot):
    nombre = f"Trampa{os.urandom(2).hex()}"
    r = post_publico(nombre, web="http://spam.example")
    assert r.status_code == 201 and r.json()["id"] == 0
    with SessionLocal() as db:
        assert db.scalar(select(Solicitud).where(Solicitud.ciudadano == nombre)) is None


def test_el_personal_con_sesion_no_necesita_el_reto(con_antibot):
    func, _ = sesion("fer.func", "funcionario")
    r = func.post("/solicitudes", json={"ciudadano": "Mostrador", "texto": "Cliente que vino a la oficina por su recibo"})
    assert r.status_code == 201


def test_un_token_de_sesion_falso_no_salta_la_proteccion(con_antibot):
    r = publico.post("/solicitudes", headers={"Authorization": "Bearer " + "x" * 43},
                     json={"ciudadano": "Falso", "texto": "Intento de saltarse el reto con un token inventado"})
    assert r.status_code in (400, 422)


def test_el_reto_tiene_limite_de_frecuencia(con_antibot, monkeypatch):
    monkeypatch.setenv("PQRS_RETO_LIMIT", "2")
    assert [publico.get("/api/reto").status_code for _ in range(3)] == [200, 200, 429]


def test_abuso_distribuido_muchas_ip_cada_una_dentro_de_su_limite_lo_frena_el_tope_global(con_antibot, monkeypatch):
    """30 'IP' distintas, una solicitud cada una (ninguna supera el límite por IP): sin tope global pasarían todas."""
    monkeypatch.setenv("PQRS_TRUST_PROXY", "1")
    monkeypatch.setenv("PQRS_PUBLICO_MAX_HORA", "12")
    codigos = []
    for i in range(30):
        reto = publico.get("/api/reto", headers={"X-Forwarded-For": f"203.0.113.{i}"}).json()["reto"]
        r = publico.post("/solicitudes", headers={"X-Forwarded-For": f"203.0.113.{i}"},
                         json={"ciudadano": f"Red{i}", "texto": f"Solicitud distribuida número {i} {os.urandom(2).hex()} sobre mi factura",
                               "acepto": True, "reto": reto, "prueba": resolver_reto(reto)})
        codigos.append(r.status_code)
    assert codigos.count(201) == 12 and codigos.count(429) == 18
    assert codigos[:12] == [201] * 12  # los primeros pasan; luego se corta aunque cada IP sea «nueva»


def test_el_tope_global_no_afecta_al_personal_con_sesion(con_antibot, monkeypatch):
    monkeypatch.setenv("PQRS_PUBLICO_MAX_HORA", "1")
    post_publico("Llena")  # agota el tope público
    func, _ = sesion("fer.func", "funcionario")
    assert func.post("/solicitudes", json={"ciudadano": "Mostr", "texto": "Cliente en la oficina por su recibo de agua"}).status_code == 201


def test_un_reto_obtenido_en_una_ip_y_usado_en_otra_sigue_siendo_de_un_solo_uso(con_antibot, monkeypatch):
    monkeypatch.setenv("PQRS_TRUST_PROXY", "1")
    reto = publico.get("/api/reto", headers={"X-Forwarded-For": "198.51.100.1"}).json()["reto"]
    prueba = resolver_reto(reto)
    uno = publico.post("/solicitudes", headers={"X-Forwarded-For": "198.51.100.1"},
                       json={"ciudadano": "Uno", "texto": f"Primera solicitud con el reto {os.urandom(2).hex()} sobre mi factura", "acepto": True, "reto": reto, "prueba": prueba})
    dos = publico.post("/solicitudes", headers={"X-Forwarded-For": "198.51.100.2"},
                       json={"ciudadano": "Dos", "texto": f"Segunda solicitud con el mismo reto {os.urandom(2).hex()} sobre mi factura", "acepto": True, "reto": reto, "prueba": prueba})
    assert (uno.status_code, dos.status_code) == (201, 400)  # el reto no se «comparte» entre IP


DATOS_EMPRESA = {"PQRS_EMPRESA": "Aguas del Café S.A. E.S.P. (NIT 900.000.000-0)", "PQRS_CONTACTO_DATOS": "datos@aguas.example",
                 "PQRS_RETENCION": "2 años desde que resolvemos su solicitud", "PQRS_PROVEEDOR_CORREO": "Proveedor Correo S.A."}


def test_el_aviso_de_privacidad_incompleto_lo_dice_y_cierra_el_formulario(con_antibot, monkeypatch):
    monkeypatch.delenv("PQRS_PERMITIR_AVISO_INCOMPLETO")
    for k in DATOS_EMPRESA:
        monkeypatch.delenv(k, raising=False)
    pagina = publico.get("/privacidad")
    assert pagina.status_code == 200 and "Aviso incompleto" in pagina.text and "PQRS_EMPRESA" in pagina.text
    assert publico.get("/api/reto").status_code == 503     # la página ni siquiera obtiene el reto
    r = publico.post("/solicitudes", json={"ciudadano": "Sin", "texto": "Solicitud sin aviso de privacidad completo", "acepto": True})
    assert r.status_code == 503 and "aviso de privacidad" in r.json()["detail"]


def test_el_aviso_de_privacidad_completo_muestra_los_datos_reales_y_abre_el_formulario(con_antibot, monkeypatch):
    monkeypatch.delenv("PQRS_PERMITIR_AVISO_INCOMPLETO")
    for k, v in DATOS_EMPRESA.items():
        monkeypatch.setenv(k, v)
    texto = publico.get("/privacidad").text
    assert "Aviso incompleto" not in texto and "Aguas del Café" in texto and "datos@aguas.example" in texto and "2 años" in texto
    assert "<mark" not in texto  # ningún marcador sin completar
    assert publico.get("/api/reto").status_code == 200 and post_publico("Ok").status_code == 201


def test_el_aviso_nombra_al_proveedor_de_correo_solo_si_hay_correo_y_lo_exige(con_antibot, monkeypatch, smtp):
    monkeypatch.delenv("PQRS_PERMITIR_AVISO_INCOMPLETO")
    for k in ("PQRS_EMPRESA", "PQRS_CONTACTO_DATOS", "PQRS_RETENCION"):
        monkeypatch.setenv(k, DATOS_EMPRESA[k])
    monkeypatch.delenv("PQRS_PROVEEDOR_CORREO", raising=False)
    assert "PQRS_PROVEEDOR_CORREO" in publico.get("/privacidad").text and publico.get("/api/reto").status_code == 503
    monkeypatch.setenv("PQRS_PROVEEDOR_CORREO", "Proveedor Correo S.A.")
    assert "Proveedor Correo S.A." in publico.get("/privacidad").text and publico.get("/api/reto").status_code == 200


def test_el_aviso_escapa_los_valores_configurados(monkeypatch):
    monkeypatch.setenv("PQRS_EMPRESA", "<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in publico.get("/privacidad").text and "&lt;script&gt;" in publico.get("/privacidad").text


def test_ceros_iniciales():
    assert antibot.ceros_iniciales(b"\xff\x00") == 0
    assert antibot.ceros_iniciales(b"\x00\x0f") == 12
    assert antibot.ceros_iniciales(b"\x01") == 7
    assert antibot.ceros_iniciales(b"\x00\x00") == 16


def test_las_paginas_publicas_se_sirven_sin_sesion_y_sin_scripts_en_linea():
    for ruta, texto in (("/solicitar", "Escríbanos su solicitud"), ("/privacidad", "Aviso de privacidad")):
        r = publico.get(ruta)
        assert r.status_code == 200 and texto in r.text
        assert "script-src 'self'" in r.headers["content-security-policy"]
        assert not re.search(r"<script(?![^>]*\bsrc=)", r.text)  # ningún script en línea (la CSP lo bloquearía)
    assert publico.get("/static/solicitar.js").status_code == 200
    html = publico.get("/solicitar").text
    assert 'name="web"' in html and 'aria-hidden="true"' in html and 'name="acepto"' in html  # trampa y autorización


def test_el_javascript_publico_no_inserta_html_con_datos_del_usuario():
    js = publico.get("/static/solicitar.js").text
    assert "innerHTML" not in js and "document.write" not in js and "eval(" not in js


# ------------------------ webhook de entrega del correo ------------------------
SECRETO_WEBHOOK = "secreto-webhook-de-pruebas-123"


def firmar(cuerpo: bytes, secreto=SECRETO_WEBHOOK):
    return "sha256=" + hmac.new(secreto.encode(), cuerpo, hashlib.sha256).hexdigest()


def evento(mid, tipo="entregado", fecha=None, firma_secreto=SECRETO_WEBHOOK, cuerpo=None):
    cuerpo = cuerpo if cuerpo is not None else json.dumps({"mensaje_id": mid, "evento": tipo, "fecha": fecha or time.time()}).encode()
    return publico.post("/webhooks/correo", content=cuerpo,
                        headers={"X-Firma": firmar(cuerpo, firma_secreto), "Content-Type": "application/json"})


@pytest.fixture
def webhook(monkeypatch):
    monkeypatch.setenv("PQRS_WEBHOOK_SECRETO", SECRETO_WEBHOOK)


def respondida_por_correo(nombre):
    func, _ = sesion("fer.func", "funcionario")
    s = solicitud_con_correo(nombre, f"{nombre.lower()}@mail.com")
    assert responder(func, s["id"], "correo").status_code == 200
    with SessionLocal() as db:
        return s["id"], db.get(Solicitud, s["id"]).mensaje_id


def entrega_de(sid):
    admin, _ = sesion("ana.admin", "admin")
    return admin.get(f"/solicitudes/{sid}").json()["entrega"]


def test_responder_por_correo_guarda_el_message_id(smtp):
    sid, mid = respondida_por_correo("Mida")
    assert mid and mid.startswith("<") and mid.endswith("@ejemplo.test>") and mid == str(smtp.enviados[-1]["Message-ID"])


def test_el_webhook_esta_apagado_si_no_hay_secreto(smtp):
    assert evento("<x@y>").status_code == 404


def test_el_webhook_rechaza_firmas_falsas_o_ausentes(smtp, webhook):
    sid, mid = respondida_por_correo("Fira")
    assert evento(mid, firma_secreto="otro-secreto").status_code == 401
    assert publico.post("/webhooks/correo", json={"mensaje_id": mid, "evento": "rebotado", "fecha": time.time()}).status_code == 401
    assert entrega_de(sid) is None


def test_el_webhook_registra_entrega_y_rebote(smtp, webhook):
    sid, mid = respondida_por_correo("Entre")
    assert evento(mid, "entregado").status_code == 200 and entrega_de(sid) == "entregado"
    assert evento(mid, "rebotado").status_code == 200 and entrega_de(sid) == "rebotado"
    assert evento(mid, "entregado").status_code == 200 and entrega_de(sid) == "rebotado"  # un rebote es definitivo
    assert any(e.accion == "entrega_correo" and e.objeto == f"solicitud {sid}" for e in eventos())


def test_el_webhook_rechaza_eventos_viejos_o_mal_formados(smtp, webhook):
    sid, mid = respondida_por_correo("Viejo")
    assert evento(mid, "rebotado", fecha=time.time() - 3600).status_code == 400  # repetición de un mensaje antiguo
    assert evento(mid, "explotado").status_code == 422
    assert evento(mid, cuerpo=b"esto no es json").status_code == 422
    assert evento(mid, cuerpo=b"{" + b'"a": "' + b"x" * 11_000 + b'"}').status_code == 413
    assert entrega_de(sid) is None


def post_webhook(cuerpo: bytes):
    return publico.post("/webhooks/correo", content=cuerpo, headers={"X-Firma": firmar(cuerpo), "Content-Type": "application/json"})


@pytest.mark.parametrize("fecha", ["NaN", "Infinity", "-Infinity", "1e999", "null", "true", '"ayer"', "[]", "{}", "-1e18"])
def test_webhook_fechas_hostiles_se_rechazan_sin_error_500(smtp, webhook, fecha):
    """La fecha debe ser un número real y finito: NaN, infinitos, booleanos, nulos y estructuras se rechazan sin error 500."""
    sid, mid = respondida_por_correo(f"Fecha{os.urandom(3).hex()}")
    cuerpo = f'{{"mensaje_id": {json.dumps(mid)}, "evento": "rebotado", "fecha": {fecha}}}'.encode()
    assert post_webhook(cuerpo).status_code in (400, 422)
    assert entrega_de(sid) is None


@pytest.mark.parametrize("plantilla", [
    b"[" * 5000 + b"]" * 5000, b'"solo un texto"', b"[1, 2, 3]", b"null", b"123", b"\xff\xfe\x00 no es utf8",
    # Estos usan la fecha ACTUAL (FECHA): así llegan hasta la validación de cada campo en vez de caer antes por «evento vencido»
    b'{"mensaje_id": ["x"], "evento": "rebotado", "fecha": FECHA}', b'{"mensaje_id": 5, "evento": "rebotado", "fecha": FECHA}',
    b'{"mensaje_id": null, "evento": "rebotado", "fecha": FECHA}', b'{"mensaje_id": "", "evento": "rebotado", "fecha": FECHA}',
    b'{"mensaje_id": "x", "evento": ["rebotado"], "fecha": FECHA}', b'{"mensaje_id": "x", "evento": {"a": 1}, "fecha": FECHA}',
    b'{"mensaje_id": "' + b"a" * 500 + b'", "evento": "rebotado", "fecha": FECHA}'])
def test_webhook_cuerpos_hostiles_nunca_producen_error_500(smtp, webhook, plantilla):
    cuerpo = plantilla.replace(b"FECHA", str(time.time()).encode())
    assert post_webhook(cuerpo).status_code in (400, 422)
    if b"FECHA" in plantilla:  # con fecha vigente, el rechazo debe venir de la validación del campo, no de la antigüedad
        assert post_webhook(cuerpo).status_code == 422


def test_webhook_eventos_duplicados_son_idempotentes_y_no_inflan_la_auditoria(smtp, webhook):
    sid, mid = respondida_por_correo("Dupli")
    antes = len([e for e in eventos("entrega_correo") if e.objeto == f"solicitud {sid}"])
    for _ in range(5):
        assert evento(mid, "entregado").status_code == 200
    despues = len([e for e in eventos("entrega_correo") if e.objeto == f"solicitud {sid}"])
    assert entrega_de(sid) == "entregado" and despues - antes == 1  # cinco repeticiones, un solo registro


def test_webhook_un_rebote_posterior_a_la_entrega_prevalece_y_un_aviso_tardio_no_lo_deshace(smtp, webhook):
    """Algunos proveedores avisan «entregado» (el servidor receptor aceptó) y luego «rebotado» (rechazo asíncrono); y los avisos llegan desordenados."""
    sid, mid = respondida_por_correo("Orden")
    assert evento(mid, "entregado").status_code == 200 and entrega_de(sid) == "entregado"
    assert evento(mid, "rebotado").status_code == 200 and entrega_de(sid) == "rebotado"
    for _ in range(3):  # llegan de nuevo avisos «entregado» atrasados o repetidos
        assert evento(mid, "entregado").status_code == 200
    assert entrega_de(sid) == "rebotado"


def test_webhook_un_evento_para_otro_mensaje_no_afecta_a_las_demas_solicitudes(smtp, webhook):
    sid_a, mid_a = respondida_por_correo("Aaa")
    sid_b, mid_b = respondida_por_correo("Bbb")
    evento(mid_a, "rebotado")
    assert entrega_de(sid_a) == "rebotado" and entrega_de(sid_b) is None


def test_el_webhook_no_revela_si_el_mensaje_existe(smtp, webhook):
    assert evento("<no-existe@ejemplo.test>").json() == {"ok": True}


def test_el_webhook_tiene_limite_de_frecuencia(smtp, webhook, monkeypatch):
    monkeypatch.setenv("PQRS_WEBHOOK_LIMIT", "2")
    assert [evento("<x@y>").status_code for _ in range(3)] == [200, 200, 429]


def test_plazo_en_dias_habiles():
    assert datetime(2026, 12, 4).weekday() == 4  # viernes
    assert sumar_dias_habiles(datetime(2026, 12, 4), 1) == datetime(2026, 12, 7)   # salta el fin de semana
    assert sumar_dias_habiles(datetime(2026, 12, 4), 5) == datetime(2026, 12, 11)


def test_cada_solicitud_nace_con_fecha_limite_en_dia_habil():
    s = registrar("Pla", "Solicito el estado de cuenta de mi servicio de agua")
    limite, creado = datetime.fromisoformat(s["fecha_limite"]), datetime.fromisoformat(s["creado"])
    assert limite.weekday() < 5 and (limite - creado).days >= 15


def test_404():
    admin, _ = sesion("ana.admin", "admin")
    assert admin.get("/solicitudes/99999").status_code == 404


# ------------------------- autenticación individual -------------------------
def test_login_correcto_devuelve_token_y_rol():
    crear_usuario_directo("lia.func", "funcionario")
    r = publico.post("/auth/login", json={"usuario": "lia.func", "clave": CLAVE}).json()
    assert r["rol"] == "funcionario" and len(r["token"]) >= 40


def test_login_fallido_no_revela_si_el_usuario_existe():
    crear_usuario_directo("lia.func", "funcionario")
    a = publico.post("/auth/login", json={"usuario": "lia.func", "clave": "equivocada-123456"})
    b = publico.post("/auth/login", json={"usuario": "no.existe", "clave": "equivocada-123456"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


def test_bloqueo_tras_intentos_fallidos():
    crear_usuario_directo("lia.func", "funcionario")
    codigos = [publico.post("/auth/login", json={"usuario": "lia.func", "clave": f"mala-{i}-xxxxxxx"}).status_code for i in range(7)]
    assert codigos[:5] == [401] * 5 and codigos[5:] == [429, 429]
    # incluso con la clave correcta, mientras dure el bloqueo
    assert publico.post("/auth/login", json={"usuario": "lia.func", "clave": CLAVE}).status_code == 429


def test_inyeccion_sql_en_el_login():
    assert publico.post("/auth/login", json={"usuario": "x' OR '1'='1", "clave": "cualquiera-123456"}).status_code == 401


def test_la_clave_se_guarda_con_scrypt_y_el_token_solo_como_hash():
    _, token = sesion("lia.func", "funcionario")
    with SessionLocal() as db:
        u = db.scalar(select(Usuario).where(Usuario.usuario == "lia.func"))
        assert u.clave_hash.startswith("scrypt$") and CLAVE not in u.clave_hash
        assert db.scalar(select(Sesion).where(Sesion.token_hash == token)) is None  # el token en claro no está
        assert len(db.scalars(select(Sesion.token_hash)).first()) == 64


def test_politica_de_claves():
    for mala in ("corta1", "aaaaaaaaaaaaaaaa", "maria.lopez-2026"):
        with pytest.raises(ValueError):
            seguridad.validar_clave(mala, "maria.lopez")
    seguridad.validar_clave("Frase-larga-y-unica-9", "maria.lopez")


@pytest.mark.parametrize("metodo,ruta", [("get", "/solicitudes"), ("get", "/solicitudes/1"), ("get", "/estadisticas"),
                                         ("patch", "/solicitudes/1"), ("get", "/usuarios"), ("get", "/auditoria")])
def test_sin_sesion_o_con_token_falso_se_rechaza(metodo, ruta):
    kw = {"json": {"estado": "resuelto"}} if metodo == "patch" else {}
    assert getattr(publico, metodo)(ruta, **kw).status_code == 401
    assert getattr(publico, metodo)(ruta, headers={"Authorization": "Bearer " + "x" * 43}, **kw).status_code == 401


def test_cerrar_sesion_invalida_el_token():
    cli, _ = sesion("lia.func", "funcionario")
    assert cli.get("/estadisticas").status_code == 200
    assert cli.post("/auth/logout").status_code == 200
    assert cli.get("/estadisticas").status_code == 401


def test_sesion_vencida_se_rechaza():
    cli, _ = sesion("lia.func", "funcionario")
    with SessionLocal() as db:
        for s in db.scalars(select(Sesion)):
            s.expira = seguridad.ahora() - timedelta(minutes=1)
        db.commit()
    assert cli.get("/estadisticas").status_code == 401


# ------------------------------------ roles ------------------------------------
def test_matriz_de_roles():
    s = registrar("Rol", "Hay una fuga de agua en la esquina de mi casa")
    consulta, _ = sesion("pedro.consulta", "consulta")
    func, _ = sesion("fer.func", "funcionario")
    admin, _ = sesion("ana.admin", "admin")
    # lectura: todos
    for cli in (consulta, func, admin):
        assert cli.get("/solicitudes").status_code == 200
    # cambiar estado: funcionario y admin
    assert consulta.patch(f"/solicitudes/{s['id']}", json={"estado": "en_proceso"}).status_code == 403
    assert func.patch(f"/solicitudes/{s['id']}", json={"estado": "en_proceso"}).status_code == 200
    # administración: solo admin
    for cli in (consulta, func):
        assert cli.get("/usuarios").status_code == 403
        assert cli.get("/auditoria").status_code == 403
        assert cli.post("/usuarios", json={"usuario": "intruso", "clave": CLAVE, "rol": "admin"}).status_code == 403
    assert admin.get("/usuarios").status_code == 200 and admin.get("/auditoria").status_code == 200


def test_escalamiento_vertical_bloqueado():
    func, _ = sesion("fer.func", "funcionario")
    with SessionLocal() as db:
        mi_id = db.scalar(select(Usuario.id).where(Usuario.usuario == "fer.func"))
    assert func.patch(f"/usuarios/{mi_id}", json={"rol": "admin"}).status_code == 403
    with SessionLocal() as db:
        assert db.get(Usuario, mi_id).rol == "funcionario"


def test_admin_crea_usuario_y_valida_la_clave():
    admin, _ = sesion("ana.admin", "admin")
    assert admin.post("/usuarios", json={"usuario": "nuevo.func", "clave": "corta", "rol": "funcionario"}).status_code == 422
    assert admin.post("/usuarios", json={"usuario": "nuevo.func", "clave": CLAVE, "rol": "superadmin"}).status_code == 422
    r = admin.post("/usuarios", json={"usuario": "nuevo.func", "clave": CLAVE, "rol": "funcionario", "activo": True, "id": 1})
    assert r.status_code == 201 and "clave" not in r.text and "hash" not in r.text
    assert admin.post("/usuarios", json={"usuario": "nuevo.func", "clave": CLAVE, "rol": "funcionario"}).status_code == 409


def test_desactivar_usuario_corta_el_acceso_inmediatamente():
    admin, _ = sesion("ana.admin", "admin")
    cli, _ = sesion("temporal.func", "funcionario")
    assert cli.get("/estadisticas").status_code == 200
    uid = next(u["id"] for u in admin.get("/usuarios").json() if u["usuario"] == "temporal.func")
    assert admin.patch(f"/usuarios/{uid}", json={"activo": False}).status_code == 200
    assert cli.get("/estadisticas").status_code == 401
    assert publico.post("/auth/login", json={"usuario": "temporal.func", "clave": CLAVE}).status_code == 401
    admin.patch(f"/usuarios/{uid}", json={"activo": True})


def test_usuario_desactivado_en_la_bd_no_pasa_aunque_su_sesion_siga_vigente():
    """Defensa en profundidad: aunque alguien desactive la cuenta sin revocar sesiones (p. ej. directo en la base)."""
    cli, _ = sesion("directo.func", "funcionario")
    with SessionLocal() as db:
        db.scalar(select(Usuario).where(Usuario.usuario == "directo.func")).activo = False
        db.commit()
    assert cli.get("/estadisticas").status_code == 401


def test_cambiar_el_rol_revoca_las_sesiones_anteriores():
    admin, _ = sesion("ana.admin", "admin")
    cli, _ = sesion("sube.func", "funcionario")
    uid = next(u["id"] for u in admin.get("/usuarios").json() if u["usuario"] == "sube.func")
    admin.patch(f"/usuarios/{uid}", json={"rol": "consulta"})
    assert cli.get("/estadisticas").status_code == 401  # debe volver a iniciar sesión con el rol nuevo


def test_un_admin_no_puede_quitarse_el_acceso_a_si_mismo():
    admin, _ = sesion("ana.admin", "admin")
    yo = next(u["id"] for u in admin.get("/usuarios").json() if u["usuario"] == "ana.admin")
    assert admin.patch(f"/usuarios/{yo}", json={"activo": False}).status_code == 409
    assert admin.patch(f"/usuarios/{yo}", json={"rol": "consulta"}).status_code == 409


# --------------------------------- auditoría ---------------------------------
def test_la_auditoria_registra_quien_cambio_que():
    func, _ = sesion("fer.func", "funcionario")
    s = registrar("Aud", "Solicito revisión de mi factura del mes pasado")
    func.get(f"/solicitudes/{s['id']}")
    func.patch(f"/solicitudes/{s['id']}", json={"estado": "en_proceso"})
    cambio =[e for e in eventos("cambiar_estado") if e.objeto == f"solicitud {s['id']}"][-1]
    assert cambio.usuario == "fer.func" and cambio.rol == "funcionario" and cambio.detalle == "nuevo -> en_proceso"
    assert any(e.accion == "consultar_solicitud" and e.usuario == "fer.func" and e.objeto == f"solicitud {s['id']}" for e in eventos())
    assert eventos("login") and cambio.fecha is not None


def test_la_auditoria_registra_fallos_y_accesos_denegados_sin_guardar_claves():
    publico.post("/auth/login", json={"usuario": "atacante", "clave": "SecretoSuperLargo-999"})
    consulta, _ = sesion("pedro.consulta", "consulta")
    consulta.get("/usuarios")
    assert any(e.accion == "login_fallido" and e.usuario == "atacante" for e in eventos())
    assert any(e.accion == "acceso_denegado" and e.usuario == "pedro.consulta" for e in eventos())
    todo = " ".join(f"{e.usuario}{e.objeto}{e.detalle}" for e in eventos())
    assert "SecretoSuperLargo-999" not in todo and CLAVE not in todo


def test_el_registro_de_auditoria_no_se_puede_modificar_por_la_api():
    admin, _ = sesion("ana.admin", "admin")
    for metodo in ("delete", "put", "patch", "post"):
        assert getattr(admin, metodo)("/auditoria").status_code == 405
    assert admin.get("/auditoria", params={"limite": 3}).status_code == 200


# --------------------- red, cabeceras y otras defensas ---------------------
def test_x_forwarded_for_se_ignora_si_no_se_confia_en_el_proxy(monkeypatch):
    monkeypatch.setenv("PQRS_RATE_LIMIT", "2")
    codigos = [publico.post("/solicitudes", json={"ciudadano": f"Px{i}", "texto": f"Mensaje con IP falsa número {i} fuga"},
                            headers={"X-Forwarded-For": f"10.0.0.{i}"}).status_code for i in range(4)]
    assert codigos == [201, 201, 429, 429]  # falsificar la cabecera no evade el límite


def test_con_proxy_de_confianza_se_usa_la_ultima_ip(monkeypatch):
    monkeypatch.setenv("PQRS_TRUST_PROXY", "1")
    monkeypatch.setenv("PQRS_RATE_LIMIT", "1")
    def enviar(i, xff):
        return publico.post("/solicitudes", json={"ciudadano": f"Pr{i}", "texto": f"Mensaje detrás de proxy número {i} fuga"},
                            headers={"X-Forwarded-For": xff}).status_code
    # lo que el cliente antepone (falsificable) no cuenta; cuenta la última IP, añadida por el proxy
    assert enviar(1, "1.1.1.1, 9.9.9.9") == 201
    assert enviar(2, "2.2.2.2, 9.9.9.9") == 429   # misma IP real -> limitado
    assert enviar(3, "1.1.1.1, 8.8.8.8") == 201   # otra IP real -> permitido


def test_documentacion_de_la_api_apagada_por_defecto():
    assert publico.get("/docs").status_code == 404 and publico.get("/openapi.json").status_code == 404


def test_cabeceras_de_seguridad():
    h = publico.get("/").headers
    assert "script-src 'self'" in h["content-security-policy"]
    assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"


def test_hsts_solo_cuando_se_declara_https(monkeypatch):
    assert "strict-transport-security" not in publico.get("/").headers
    monkeypatch.setenv("PQRS_HTTPS", "1")
    assert "max-age=31536000" in publico.get("/").headers["strict-transport-security"]


def test_inyeccion_sql_en_filtros_no_devuelve_datos():
    admin, _ = sesion("ana.admin", "admin")
    assert admin.get("/solicitudes", params={"estado": "x' OR '1'='1"}).json() == []


def test_paginacion_con_limite():
    admin, _ = sesion("ana.admin", "admin")
    assert len(admin.get("/solicitudes", params={"limite": 2}).json()) <= 2
    assert admin.get("/solicitudes", params={"limite": 100000}).status_code == 422


def test_xss_toda_interpolacion_de_campos_libres_esta_escapada():
    registrar("<img src=x onerror=alert(1)>", "<script>alert(1)</script> fuga de agua grande")
    js = open(os.path.join(os.path.dirname(__file__), "..", "app", "static", "app.js"), encoding="utf-8").read()
    for interpolacion in re.findall(r"\$\{([^}]*)\}", js):
        if re.search(r"\bd\.(ciudadano|correo|texto|resumen|respuesta_sugerida|respuesta_final|respondido_por|area)", interpolacion):
            assert interpolacion.lstrip().startswith("esc("), f"Sin escapar: ${{{interpolacion}}}"
    assert "localStorage" not in js  # el token vive solo en sessionStorage


def test_no_hay_secretos_en_el_codigo():
    raiz = os.path.join(os.path.dirname(__file__), "..")
    patron = re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY")
    for carpeta, _, archivos in os.walk(raiz):
        if any(x in carpeta for x in (".pytest_cache", "__pycache__", ".git")):
            continue
        for a in archivos:
            if a.endswith((".py", ".js", ".html", ".md", ".txt", ".in", ".example")):
                assert not patron.search(open(os.path.join(carpeta, a), encoding="utf-8", errors="ignore").read()), a
