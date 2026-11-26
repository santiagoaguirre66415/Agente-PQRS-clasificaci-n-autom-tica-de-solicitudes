"""Pruebas de las herramientas de preparación de producción: respaldo, preflight, monitoreo, prueba de humo y /salud.
Usan SQLite y archivos reales en carpetas temporales (no simulaciones): así aparecen problemas como los bloqueos de archivos de Windows."""
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import time
import zipfile
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests import test_api as t

RAIZ = Path(__file__).parent.parent


def cargar(nombre):
    spec = importlib.util.spec_from_file_location(nombre, RAIZ / "herramientas" / f"{nombre}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[nombre] = mod
    spec.loader.exec_module(mod)
    return mod


respaldo, preflight, monitoreo, humo = cargar("respaldo"), cargar("preflight"), cargar("monitoreo"), cargar("prueba_humo")
AHORA = t.seguridad.ahora


@pytest.fixture(autouse=True)
def _estado_limpio():
    """Cada prueba parte sin límites de frecuencia ni retos acumulados de las anteriores."""
    t.seguridad.reiniciar_limites()
    t.antibot.reiniciar()
    yield


# ================================== respaldo ==================================
@pytest.fixture
def base(tmp_path):
    ruta = tmp_path / "origen" / "pqrs.db"
    ruta.parent.mkdir()
    with closing(sqlite3.connect(ruta)) as c:
        c.executescript("CREATE TABLE solicitudes(id INTEGER PRIMARY KEY, texto TEXT); CREATE TABLE usuarios(id INTEGER PRIMARY KEY, n TEXT);"
                        "CREATE TABLE auditoria(id INTEGER PRIMARY KEY, a TEXT);")
        c.executemany("INSERT INTO solicitudes(texto) VALUES (?)", [(f"s{i}",) for i in range(7)])
        c.execute("INSERT INTO usuarios(n) VALUES ('ana')")
        c.commit()
    return ruta


def huella(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def es_cifrado(p: Path) -> None:
    """Comprueba PROPIEDADES del cifrado, no la ausencia de un texto corto.
    (Una versión anterior buscaba `b"s3"` dentro del texto cifrado: como el cifrado es aleatorio, esa pareja de letras aparece por azar
    en ~1 de cada 5 ejecuciones y la prueba fallaba a veces. Era un fallo de la prueba, no del cifrado.)"""
    datos = p.read_bytes()
    assert p.name.endswith(".zip.enc")
    assert datos.startswith(b"gAAAA")                         # formato de un token Fernet (versión 0x80)
    assert not datos.startswith(b"PK")                        # no es un zip a la vista
    assert not zipfile.is_zipfile(p)                          # y ninguna herramienta lo abre como zip sin la clave


def test_respaldo_crea_verifica_y_restaura_con_las_mismas_filas(base, tmp_path):
    r = respaldo.crear(base, tmp_path / "dest")
    assert r.exists() and (r.parent / (r.name + ".sha256")).exists()
    info = respaldo.verificar(r)
    assert info["integridad"] == "ok" and info["filas"] == {"solicitudes": 7, "usuarios": 1, "auditoria": 0} and info["cifrado"] is False
    destino = tmp_path / "nueva" / "pqrs.db"
    respaldo.restaurar(r, destino)
    with closing(sqlite3.connect(destino)) as c:
        assert c.execute("SELECT COUNT(*) FROM solicitudes").fetchone()[0] == 7


def test_el_respaldo_no_modifica_ni_bloquea_la_base_original(base, tmp_path):
    antes = huella(base)
    respaldo.crear(base, tmp_path / "dest")
    assert huella(base) == antes          # no se tocó
    base.unlink()                          # y no queda bloqueada (en Windows fallaría si una conexión siguiera abierta)


def test_respaldo_en_caliente_con_una_escritura_abierta(base, tmp_path):
    with closing(sqlite3.connect(base)) as vivo:
        vivo.execute("INSERT INTO solicitudes(texto) VALUES ('no confirmada')")   # transacción abierta, sin commit
        r = respaldo.crear(base, tmp_path / "dest")
        assert respaldo.verificar(r)["filas"]["solicitudes"] == 7                  # solo lo confirmado, consistente


def test_copia_externa_se_restaura_desde_la_copia_y_conserva_la_huella(base, tmp_path):
    r = respaldo.crear(base, tmp_path / "dest")
    copia, mismo = respaldo.copiar_externo(r, tmp_path / "otro_disco")
    assert copia.read_bytes() == r.read_bytes() and (copia.parent / (copia.name + ".sha256")).exists()
    assert respaldo.verificar(copia)["filas"]["solicitudes"] == 7
    assert mismo is True        # las dos carpetas de la prueba están en el mismo volumen: debe avisarlo


def test_copia_externa_cifrada_se_verifica_con_la_clave(base, tmp_path):
    clave = tmp_path / "k.key"
    respaldo.generar_clave(clave)
    r = respaldo.crear(base, tmp_path / "dest", str(clave))
    copia, _ = respaldo.copiar_externo(r, tmp_path / "otro", str(clave))
    es_cifrado(copia)
    otra = tmp_path / "otra.key"
    respaldo.generar_clave(otra)
    with pytest.raises(SystemExit, match="no corresponde"):
        respaldo.copiar_externo(r, tmp_path / "otro2", str(otra))      # una copia que no se puede restaurar no se da por buena


def test_copia_externa_danada_en_el_camino_se_descarta(base, tmp_path, monkeypatch):
    r = respaldo.crear(base, tmp_path / "dest")

    def copia_truncada(origen, destino, **k):          # simula un disco o una red que corrompe la copia
        Path(destino).write_bytes(Path(origen).read_bytes()[:-5])
    monkeypatch.setattr(respaldo.shutil, "copy2", copia_truncada)
    with pytest.raises(SystemExit, match="no coincide"):
        respaldo.copiar_externo(r, tmp_path / "otro")
    assert not list((tmp_path / "otro").glob("pqrs-*"))      # no dejó una copia engañosa


def test_copia_externa_no_puede_ser_la_misma_carpeta(base, tmp_path):
    r = respaldo.crear(base, tmp_path / "dest")
    with pytest.raises(SystemExit, match="misma"):
        respaldo.copiar_externo(r, tmp_path / "dest")


def test_copia_externa_aplica_la_rotacion_en_el_destino(base, tmp_path):
    for i in range(4):
        r = respaldo.crear(base, tmp_path / "dest", conservar=100, ahora=1_800_000_000 + i * 86400)
        respaldo.copiar_externo(r, tmp_path / "otro", conservar=2)
    assert len(list((tmp_path / "otro").glob("pqrs-*.zip"))) == 2 and len(list((tmp_path / "otro").glob("*.sha256"))) == 2


def test_cli_crear_con_copiar_a_avisa_si_es_el_mismo_disco(base, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{base}")
    assert respaldo.main(["x", "crear", "--destino", str(tmp_path / "dest"), "--copiar-a", str(tmp_path / "ext")]) == 0
    salida = capsys.readouterr().out
    assert "DESDE ELLA" in salida and "MISMO disco" in salida


def test_respaldo_se_niega_a_guardar_una_copia_que_no_pasa_la_comprobacion_de_integridad(tmp_path):
    """Una copia dañada que parece normal es peor que no tener copia. La base se daña de modo que SIGA siendo legible
    (una fila ya no coincide con su índice): solo `PRAGMA integrity_check` lo detecta."""
    ruta = tmp_path / "danada.db"
    with closing(sqlite3.connect(ruta)) as c:
        c.executescript("CREATE TABLE solicitudes(id INTEGER PRIMARY KEY, texto TEXT); CREATE INDEX ix ON solicitudes(texto);"
                        "CREATE TABLE usuarios(id INTEGER); CREATE TABLE auditoria(id INTEGER);")
        c.executemany("INSERT INTO solicitudes(texto) VALUES (?)", [(f"marca-unica-{i:04d}-xxxxxxxx",) for i in range(50)])
        c.commit()
    datos = bytearray(ruta.read_bytes())
    i = datos.index(b"marca-unica-0007-xxxxxxxx")          # la primera aparición está en la página de la TABLA, no en la del índice
    datos[i:i + 9] = b"CORROMPID"
    ruta.write_bytes(bytes(datos))
    with closing(sqlite3.connect(ruta)) as c:
        assert c.execute("SELECT COUNT(*) FROM solicitudes").fetchone()[0] == 50       # sigue leyéndose
        assert c.execute("PRAGMA integrity_check").fetchone()[0] != "ok"               # pero no está sana
    with pytest.raises(SystemExit, match="integridad"):
        respaldo.crear(ruta, tmp_path / "dest")
    assert list((tmp_path / "dest").glob("pqrs-*")) == []                              # no dejó ningún respaldo engañoso


def test_respaldo_con_un_archivo_que_no_es_sqlite_da_un_mensaje_claro(tmp_path):
    ruta = tmp_path / "no_es_una_base.db"
    ruta.write_bytes(b"esto no es una base de datos " * 200)
    with pytest.raises(SystemExit, match="No se pudo copiar"):
        respaldo.crear(ruta, tmp_path / "dest")


def test_respaldo_funciona_con_la_base_en_solo_lectura(base, tmp_path):
    # (SQLite abre en solo lectura un archivo protegido aunque se pida mode=rw, así que esto no distingue ro de rw: solo comprueba que funciona)
    import stat
    os.chmod(base, stat.S_IREAD)
    try:
        r = respaldo.crear(base, tmp_path / "dest")
        assert respaldo.verificar(r)["filas"]["solicitudes"] == 7
    finally:
        os.chmod(base, stat.S_IWRITE | stat.S_IREAD)


def test_respaldo_cifrado_ida_y_vuelta_y_rechaza_clave_ausente_o_equivocada(base, tmp_path):
    clave, otra = tmp_path / "a.key", tmp_path / "b.key"
    respaldo.generar_clave(clave)
    respaldo.generar_clave(otra)
    r = respaldo.crear(base, tmp_path / "dest", str(clave))
    es_cifrado(r)
    assert respaldo.verificar(r, str(clave))["cifrado"] is True
    with pytest.raises(SystemExit, match="cifrado"):
        respaldo.verificar(r)
    with pytest.raises(SystemExit, match="no corresponde"):
        respaldo.verificar(r, str(otra))
    with pytest.raises(SystemExit, match="ya existe"):
        respaldo.generar_clave(clave)                                              # nunca pisa una clave existente


def test_un_respaldo_modificado_o_corrupto_se_detecta(base, tmp_path):
    r = respaldo.crear(base, tmp_path / "dest")
    datos = bytearray(r.read_bytes())
    datos[len(datos) // 2] ^= 0xFF
    r.write_bytes(bytes(datos))
    with pytest.raises(SystemExit, match="SHA-256"):
        respaldo.verificar(r)
    (r.parent / (r.name + ".sha256")).unlink()          # sin huella, el contenido corrupto también debe fallar
    with pytest.raises(SystemExit):
        respaldo.verificar(r)


def test_la_rotacion_conserva_solo_los_n_mas_recientes_y_borra_sus_huellas(base, tmp_path):
    dest = tmp_path / "dest"
    for i in range(5):
        respaldo.crear(base, dest, conservar=100, ahora=1_800_000_000 + i * 86400)
    quedan = respaldo.rotar(dest, 3)
    nombres = sorted(p.name for p in dest.glob("pqrs-*.zip"))
    assert len(quedan) == 2 and len(nombres) == 3
    assert nombres == [p.name for p in respaldo.listar(dest)]
    assert len(list(dest.glob("*.sha256"))) == 3       # no quedan huellas huérfanas
    assert all(n > quedan[-1].name for n in nombres)   # se conservaron los más nuevos


def test_restaurar_no_sobrescribe_sin_forzar_y_con_forzar_guarda_lo_anterior(base, tmp_path):
    r = respaldo.crear(base, tmp_path / "dest")
    destino = tmp_path / "viva.db"
    destino.write_bytes(b"base actual")
    with pytest.raises(SystemExit, match="--forzar"):
        respaldo.restaurar(r, destino)
    assert destino.read_bytes() == b"base actual"
    previo = respaldo.restaurar(r, destino, forzar=True)
    assert previo.read_bytes() == b"base actual" and respaldo.verificar(r)["filas"]["solicitudes"] == 7
    with closing(sqlite3.connect(destino)) as c:
        assert c.execute("SELECT COUNT(*) FROM solicitudes").fetchone()[0] == 7


def test_respaldo_solo_para_sqlite_y_base_inexistente(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    with pytest.raises(SystemExit, match="pg_dump"):
        respaldo.ruta_base()
    with pytest.raises(SystemExit, match="No existe"):
        respaldo.crear(tmp_path / "no_existe.db", tmp_path / "dest")


def test_respaldo_cli_crear_avisa_si_no_va_cifrado(base, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{base}")
    assert respaldo.main(["x", "crear", "--destino", str(tmp_path / "dest")]) == 0
    salida = capsys.readouterr().out
    assert "SIN CIFRAR" in salida and "integridad=ok" in salida


# ================================== preflight ==================================
@pytest.fixture
def entorno_bueno(tmp_path):
    clave = tmp_path / "smtp.clave"
    clave.write_text("clave-smtp-de-prueba\n", encoding="utf-8")
    if os.name != "nt":
        clave.chmod(0o600)
    rd = tmp_path / "respaldos"
    rd.mkdir()
    (rd / "pqrs-20261126-030000.zip").write_bytes(b"x")
    for nombre in ("rev.admin.uno", "rev.admin.dos"):   # la comprobación de usuarios lee la base de pruebas
        t.crear_usuario_directo(nombre, "admin")
    return {"PQRS_URL_PUBLICA": "https://pqrs.ejemplo.test", "PQRS_HTTPS": "1", "PQRS_TRUST_PROXY": "1", "PQRS_EMPRESA": "Empresa S.A.",
            "PQRS_CONTACTO_DATOS": "datos@ejemplo.test", "PQRS_RETENCION": "2 años", "PQRS_PROVEEDOR_CORREO": "Proveedor",
            "PQRS_SMTP_HOST": "smtp.ejemplo.test", "PQRS_SMTP_USER": "u", "PQRS_SMTP_FROM": "pqrs@ejemplo.test", "PQRS_SMTP_PASSWORD_FILE": str(clave),
            "PQRS_WEBHOOK_SECRETO": "s" * 40, "PQRS_SECRETO": "x" * 40, "PQRS_RESPALDO_DIR": str(rd),
            "DATABASE_URL": "postgresql+psycopg://u:p@h/db"}


def faltas(r):
    return [m for n, m in r if n == "FALTA"]


def test_un_entorno_de_produccion_bien_configurado_no_tiene_faltas(entorno_bueno):
    # la comprobación de usuarios usa la base de pruebas: no debe haber usuarios de demostración
    r = preflight.evaluar(entorno_bueno, ahora=time.time())
    assert faltas(r) == [], faltas(r)


@pytest.mark.parametrize("cambio,texto", [
    ({"PQRS_URL_PUBLICA": ""}, "PQRS_URL_PUBLICA"), ({"PQRS_URL_PUBLICA": "http://x.test"}, "PQRS_URL_PUBLICA"), ({"PQRS_HTTPS": ""}, "PQRS_HTTPS"),
    ({"PQRS_PERMITIR_AVISO_INCOMPLETO": "1"}, "PQRS_PERMITIR_AVISO_INCOMPLETO"), ({"PQRS_DOCS": "1"}, "PQRS_DOCS"),
    ({"PQRS_ANTIBOT": "0"}, "anti-bots"), ({"PQRS_SCRYPT_N": "1024"}, "PQRS_SCRYPT_N"), ({"PQRS_SMTP_CA_FILE": "x.pem"}, "PQRS_SMTP_CA_FILE"),
    ({"PQRS_WEBHOOK_SECRETO": "corto"}, "PQRS_WEBHOOK_SECRETO"), ({"PQRS_EMPRESA": ""}, "PQRS_EMPRESA"), ({"PQRS_PROVEEDOR_CORREO": ""}, "PQRS_PROVEEDOR_CORREO"),
    ({"PQRS_RESPALDO_DIR": ""}, "PQRS_RESPALDO_DIR")])
def test_cada_configuracion_peligrosa_o_faltante_se_marca_como_falta(entorno_bueno, cambio, texto):
    entorno_bueno.update(cambio)
    assert any(texto in f for f in faltas(preflight.evaluar(entorno_bueno, ahora=time.time()))), cambio


def test_un_respaldo_viejo_o_inexistente_es_falta(entorno_bueno, tmp_path):
    assert any("respaldo" in f.lower() for f in faltas(preflight.evaluar(entorno_bueno, ahora=time.time() + 5 * 86400)))
    vacio = tmp_path / "vacio"
    vacio.mkdir()
    entorno_bueno["PQRS_RESPALDO_DIR"] = str(vacio)
    assert any("No hay respaldos" in f for f in faltas(preflight.evaluar(entorno_bueno, ahora=time.time())))


def test_la_contrasena_smtp_en_el_entorno_o_la_clave_admin_residual_son_avisos(entorno_bueno):
    entorno_bueno.pop("PQRS_SMTP_PASSWORD_FILE")
    entorno_bueno["PQRS_SMTP_PASSWORD"] = "x"
    entorno_bueno["PQRS_ADMIN_PASSWORD"] = "residual"
    r = preflight.evaluar(entorno_bueno, ahora=time.time())
    avisos = [m for n, m in r if n == "AVISO"]
    assert any("PQRS_SMTP_PASSWORD_FILE" in m for m in avisos) and any("PQRS_ADMIN_PASSWORD" in m for m in avisos)


def test_un_usuario_de_demostracion_es_falta(entorno_bueno):
    t.crear_usuario_directo("demo", "funcionario")
    try:
        assert any("demostración" in f for f in faltas(preflight.evaluar(entorno_bueno, ahora=time.time())))
    finally:
        with t.SessionLocal() as db:
            db.delete(db.scalar(select(t.Usuario).where(t.Usuario.usuario == "demo")))
            db.commit()


def test_preflight_main_devuelve_1_si_hay_faltas(monkeypatch, capsys):
    monkeypatch.setattr(preflight, "evaluar", lambda: [("OK", "a"), ("FALTA", "b")])
    assert preflight.main(["x"]) == 1 and "1 imprescindible" in capsys.readouterr().out
    monkeypatch.setattr(preflight, "evaluar", lambda: [("OK", "a"), ("AVISO", "b")])
    assert preflight.main(["x"]) == 0


# ================================== monitoreo ==================================
def sol_directa(nombre, **kw):
    with t.SessionLocal() as db:
        s = t.Solicitud(ciudadano=nombre, texto=f"texto de {nombre}", **kw)
        db.add(s)
        db.commit()


def audit(accion, minutos_atras):
    with t.SessionLocal() as db:
        db.add(t.Auditoria(fecha=AHORA() - timedelta(minutes=minutos_atras), usuario="x", accion=accion))
        db.commit()


def delta(antes, despues, clave):
    return despues[clave] - antes[clave]


def medir():
    with t.SessionLocal() as db:
        return monitoreo.metricas(db, env={})


def test_metricas_cuentan_solo_lo_que_cae_en_cada_ventana():
    antes = medir()
    for _ in range(3):
        audit("login_fallido", 10)                  # dentro de la última hora
    audit("login_fallido", 120)                      # fuera
    audit("login_bloqueado", 5)
    audit("envio_fallido", 60 * 20)                  # dentro de 24 h
    audit("envio_fallido", 60 * 30)                  # fuera
    audit("acceso_denegado", 30)
    sol_directa("Vencida", fecha_limite=AHORA() - timedelta(days=2))
    sol_directa("PorVencer", fecha_limite=AHORA() + timedelta(days=2))
    sol_directa("Lejana", fecha_limite=AHORA() + timedelta(days=20))
    sol_directa("Rebota", entrega="rebotado", entrega_en=AHORA() - timedelta(hours=2), fecha_limite=AHORA() + timedelta(days=20))
    sol_directa("VieJaNueva", creado=AHORA() - timedelta(hours=100), fecha_limite=AHORA() + timedelta(days=20))
    d = medir()
    assert delta(antes, d, "logins_fallidos_1h") == 3 and delta(antes, d, "bloqueos_1h") == 1
    assert delta(antes, d, "envios_fallidos_24h") == 1 and delta(antes, d, "accesos_denegados_1h") == 1
    assert delta(antes, d, "vencidas") == 1 and delta(antes, d, "por_vencer_3d") == 1 and delta(antes, d, "rebotes_24h") == 1
    assert delta(antes, d, "nuevas_sin_atender") == 1 and delta(antes, d, "total_solicitudes") == 5


def base_m(**k):
    m = {"logins_fallidos_1h": 0, "bloqueos_1h": 0, "accesos_denegados_1h": 0, "envios_fallidos_24h": 0, "rebotes_24h": 0, "vencidas": 0,
         "por_vencer_3d": 0, "nuevas_sin_atender": 0, "total_solicitudes": 10, "dias_desde_retencion": 1.0}
    m.update(k)
    return m


def test_sin_novedad_no_hay_alertas():
    assert monitoreo.alertas(base_m(), {}) == []


@pytest.mark.parametrize("cambio,nivel,texto", [
    ({"vencidas": 1}, "ALERTA", "VENCIDA"), ({"logins_fallidos_1h": 20}, "ALERTA", "fuerza bruta"), ({"bloqueos_1h": 3}, "ALERTA", "bloqueos"),
    ({"envios_fallidos_24h": 3}, "ALERTA", "fallidos"), ({"rebotes_24h": 1}, "AVISO", "rebotaron"), ({"por_vencer_3d": 5}, "AVISO", "vencen"),
    ({"accesos_denegados_1h": 10}, "AVISO", "denegados"), ({"nuevas_sin_atender": 1}, "AVISO", "sin atender"),
    ({"horas_desde_respaldo": 40.0}, "ALERTA", "respaldo"), ({"horas_desde_respaldo": None}, "ALERTA", "ningún respaldo"),
    ({"dias_desde_retencion": None}, "AVISO", "nunca"), ({"dias_desde_retencion": 30.0}, "AVISO", "14 días")])
def test_cada_umbral_genera_la_alerta_correcta(cambio, nivel, texto):
    r = monitoreo.alertas(base_m(**cambio), {})
    assert any(n == nivel and texto in tx for n, tx in r), r


def test_los_umbrales_se_ajustan_por_entorno():
    assert monitoreo.alertas(base_m(logins_fallidos_1h=5), {"PQRS_ALERTA_LOGINS_FALLIDOS_1H": "5"})
    assert not monitoreo.alertas(base_m(logins_fallidos_1h=19), {})


def test_el_respaldo_reciente_no_alerta_y_el_viejo_si(tmp_path):
    rd = tmp_path / "r"
    rd.mkdir()
    f = rd / "pqrs-20261126-030000.zip"
    f.write_bytes(b"x")
    with t.SessionLocal() as db:
        assert monitoreo.metricas(db, env={"PQRS_RESPALDO_DIR": str(rd)})["horas_desde_respaldo"] < 1
        viejo = monitoreo.metricas(db, ahora=AHORA() + timedelta(days=3), env={"PQRS_RESPALDO_DIR": str(rd)})
    assert viejo["horas_desde_respaldo"] > 36 and any("respaldo" in tx for _, tx in monitoreo.alertas(viejo, {}))


def test_monitoreo_json_solo_trae_conteos_y_el_codigo_de_salida_coincide(capsys):
    sol_directa("VencidaMon", fecha_limite=AHORA() - timedelta(days=1))
    codigo = monitoreo.main(["x", "--json"])
    salida = json.loads(capsys.readouterr().out)
    assert all(isinstance(v, (int, float)) or v is None for v in salida["metricas"].values())   # ningún dato personal, solo números
    assert "VencidaMon" not in json.dumps(salida) and codigo == 1 and any(a["nivel"] == "ALERTA" for a in salida["alertas"])


def test_el_monitoreo_envia_el_correo_solo_si_hay_alertas(monkeypatch):
    class Falso(t.SMTPFalso):
        pass
    t.SMTPFalso.enviados, t.SMTPFalso.eventos, t.SMTPFalso.claves, t.SMTPFalso.falla = [], [], [], False
    monkeypatch.setattr(t.correo_mod, "SMTP", Falso)
    for k, v in {"PQRS_SMTP_HOST": "h", "PQRS_SMTP_USER": "u", "PQRS_SMTP_PASSWORD": "p", "PQRS_SMTP_FROM": "pqrs@ejemplo.test"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(monitoreo, "alertas", lambda m, env=None: [])
    monitoreo.main(["x", "--correo-a", "admin@ejemplo.test"])
    assert t.SMTPFalso.enviados == []
    monkeypatch.setattr(monitoreo, "alertas", lambda m, env=None: [("ALERTA", "algo grave")])
    assert monitoreo.main(["x", "--correo-a", "admin@ejemplo.test"]) == 1
    assert len(t.SMTPFalso.enviados) == 1 and "algo grave" in t.SMTPFalso.enviados[0].get_content()


# ================================= prueba de humo =================================
@pytest.fixture
def humo_listo(monkeypatch):
    t.crear_usuario_directo("humo.func", "funcionario")
    for k, v in {"PQRS_ANTIBOT": "1", "PQRS_ANTIBOT_BITS": "8", "PQRS_ANTIBOT_MIN_SEG": "0", "PQRS_PUBLICO_MAX_HORA": "1000", **t.DATOS_EMPRESA}.items():
        monkeypatch.setenv(k, v)
    t.antibot.reiniciar()
    t.seguridad.reiniciar_limites()
    return TestClient(t.app)


def test_la_prueba_de_humo_completa_pasa_contra_la_app(humo_listo):
    i = humo.ejecutar(humo_listo, True, "humo.func", t.CLAVE, espera_reto=0, https=False)
    assert i.fallas == 0, [f for ok, f in i.filas if not ok]
    assert len(i.filas) >= 24


def test_la_prueba_de_humo_se_puede_repetir_el_mismo_dia(humo_listo):
    """La app rechaza duplicados exactos en 24 h: la prueba debe generar un texto distinto cada vez."""
    for _ in range(2):
        t.seguridad.reiniciar_limites()
        t.antibot.reiniciar()
        i = humo.ejecutar(humo_listo, True, "humo.func", t.CLAVE, espera_reto=0, https=False)
        assert i.fallas == 0, [f for ok, f in i.filas if not ok]


def test_sin_escritura_no_crea_ninguna_solicitud(humo_listo):
    with t.SessionLocal() as db:
        antes = db.scalar(select(func.count(t.Solicitud.id)))
    i = humo.ejecutar(humo_listo, False, https=False)
    with t.SessionLocal() as db:
        assert db.scalar(select(func.count(t.Solicitud.id))) == antes
    assert i.fallas == 0


def test_la_prueba_de_humo_detecta_un_aviso_incompleto_y_la_falta_de_hsts(humo_listo, monkeypatch):
    monkeypatch.delenv("PQRS_EMPRESA")
    i = humo.ejecutar(humo_listo, True, "humo.func", t.CLAVE, espera_reto=0, https=True)
    fallas = [f for ok, f in i.filas if not ok]
    assert any("HSTS" in f for f in fallas) and any("Aviso de privacidad" in f for f in fallas) and any("reto" in f.lower() for f in fallas)


def test_la_prueba_de_humo_detecta_credenciales_malas(humo_listo):
    i = humo.ejecutar(humo_listo, True, "humo.func", "clave-equivocada-123", espera_reto=0, https=False)
    assert any("Inicio de sesión" in f for ok, f in i.filas if not ok), [f for ok, f in i.filas if not ok]


def test_humo_main_valida_la_url():
    assert humo.main(["x", "no-es-url"]) == 2


# ===================================== /salud =====================================
def test_salud_responde_sin_sesion_y_sin_datos_sensibles():
    t.seguridad.reiniciar_limites()
    r = t.publico.get("/salud")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert "version" not in r.text.lower() and "sqlite" not in r.text.lower()


def test_salud_devuelve_503_sin_filtrar_el_error_si_la_base_falla():
    class Rota:
        def execute(self, *a, **k):
            raise RuntimeError("password=secreto en la cadena de conexión")

    def rota():
        yield Rota()
    t.app.dependency_overrides[t.get_db if hasattr(t, "get_db") else __import__("app.db", fromlist=["get_db"]).get_db] = rota
    try:
        t.seguridad.reiniciar_limites()
        r = t.publico.get("/salud")
        assert r.status_code == 503 and "secreto" not in r.text and "password" not in r.text
    finally:
        t.app.dependency_overrides.clear()


def test_salud_tiene_limite_de_frecuencia(monkeypatch):
    monkeypatch.setenv("PQRS_SALUD_LIMIT", "3")
    t.seguridad.reiniciar_limites()
    assert [t.publico.get("/salud").status_code for _ in range(5)] == [200, 200, 200, 429, 429]
