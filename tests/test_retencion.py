"""Pruebas de la conservación y supresión de datos personales."""
import importlib.util
import re
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from tests import test_api as t  # noqa: E402  (configura la base de datos de pruebas)

_spec = importlib.util.spec_from_file_location("retencion", Path(__file__).parent.parent / "herramientas" / "retencion.py")
ret = importlib.util.module_from_spec(_spec)
sys.modules["retencion"] = ret
_spec.loader.exec_module(ret)

AHORA = t.seguridad.ahora


def fila(nombre, *, correo="", estado="nuevo", dias_resuelta=None, dias_creada=None):
    """Inserta una solicitud directamente en la base con las fechas que se necesiten."""
    creado = AHORA() - timedelta(days=dias_creada if dias_creada is not None else (dias_resuelta or 0) + 1)
    kw = {}
    if estado == "resuelto":
        resuelta = AHORA() - timedelta(days=dias_resuelta)
        kw = dict(resuelto_en=resuelta, respondido_en=resuelta, respuesta_final=f"Respuesta privada para {nombre}",
                  respondido_por="fer.func", canal_respuesta="correo", mensaje_id=f"<{nombre}@ejemplo.test>")
    with t.SessionLocal() as db:
        s = t.Solicitud(ciudadano=nombre, correo=correo, texto=f"Texto privado de {nombre}", resumen=f"Resumen de {nombre}",
                        respuesta_sugerida="Borrador", estado=estado, creado=creado, correo_verificado=bool(correo), **kw)
        db.add(s)
        db.commit()
        return s.id


def datos(sid):
    with t.SessionLocal() as db:
        s = db.get(t.Solicitud, sid)
        return {c: getattr(s, c) for c in ("ciudadano", "correo", "texto", "resumen", "respuesta_sugerida", "respuesta_final",
                                          "mensaje_id", "correo_verificado", "tipo", "categoria", "prioridad", "estado", "creado", "resuelto_en")}


def contar(fn, *a, **k):
    with t.SessionLocal() as db:
        return fn(db, *a, **k)


def test_antiguas_anonimiza_solo_las_resueltas_vencidas_y_conserva_las_estadisticas():
    vieja = fila("VieJaResuelta", correo="v@mail.com", estado="resuelto", dias_resuelta=800)
    reciente = fila("RecienteResuelta", correo="r@mail.com", estado="resuelto", dias_resuelta=10)
    sin_resolver = fila("VieJaPendiente", correo="p@mail.com", estado="nuevo", dias_creada=900)
    antes = datos(vieja)
    assert contar(ret.anonimizar_antiguas, 730, True) >= 1
    d = datos(vieja)
    assert d["ciudadano"] == "Anónimo" and d["correo"] == "" and d["texto"] == ret.MARCA and d["resumen"] == "" and d["respuesta_sugerida"] == ""
    assert d["respuesta_final"] == ret.MARCA and d["mensaje_id"] is None and d["correo_verificado"] is False
    for campo in ("tipo", "categoria", "prioridad", "estado", "creado", "resuelto_en"):  # lo estadístico se conserva
        assert d[campo] == antes[campo]
    assert datos(reciente)["ciudadano"] == "RecienteResuelta"          # dentro del plazo: intacta
    assert datos(sin_resolver)["ciudadano"] == "VieJaPendiente"        # sin resolver: la política no la toca


def test_por_defecto_es_una_simulacion_y_no_cambia_nada():
    sid = fila("Simula", correo="s@mail.com", estado="resuelto", dias_resuelta=900)
    assert contar(ret.anonimizar_antiguas, 730) >= 1          # aplicar=False
    assert datos(sid)["ciudadano"] == "Simula"
    assert ret.main(["retencion", "antiguas", "--dias", "730"]) == 0 and datos(sid)["ciudadano"] == "Simula"
    assert ret.main(["retencion", "antiguas", "--dias", "730", "--aplicar"]) == 0 and datos(sid)["ciudadano"] == "Anónimo"


def test_es_idempotente():
    fila("Idem", correo="i@mail.com", estado="resuelto", dias_resuelta=900)
    contar(ret.anonimizar_antiguas, 730, True)
    assert contar(ret.anonimizar_antiguas, 730, True) == 0


def test_suprimir_titular_por_correo_anonimiza_todas_sus_solicitudes_y_no_las_de_otros():
    a1 = fila("Titular Uno", correo="titular@mail.com", estado="nuevo")
    a2 = fila("Titular Dos", correo="titular@mail.com", estado="resuelto", dias_resuelta=5)
    otra = fila("Otra Persona", correo="otra@mail.com", estado="nuevo")
    assert contar(ret.suprimir_titular, correo="  Titular@Mail.com ", aplicar=True) == 2
    assert datos(a1)["ciudadano"] == "Anónimo" and datos(a2)["ciudadano"] == "Anónimo" and datos(a2)["respuesta_final"] == ret.MARCA
    assert datos(otra)["ciudadano"] == "Otra Persona"


def test_suprimir_titular_por_id_y_validaciones():
    sid = fila("PorId", estado="nuevo")
    assert contar(ret.suprimir_titular, solicitud_id=sid, aplicar=True) == 1 and datos(sid)["ciudadano"] == "Anónimo"
    assert contar(ret.suprimir_titular, solicitud_id=99999, aplicar=True) == 0
    assert contar(ret.suprimir_titular, correo="nadie@mail.com", aplicar=True) == 0
    for kw in ({}, {"correo": "a@b.com", "solicitud_id": 1}):
        with pytest.raises(ValueError):
            contar(ret.suprimir_titular, **kw)
    assert ret.main(["retencion", "suprimir"]) == 2


def test_la_solicitud_anonimizada_sigue_funcionando_en_la_api():
    admin, _ = t.sesion("ana.admin", "admin")
    sid = fila("ApiAnon", correo="api@mail.com", estado="resuelto", dias_resuelta=900)
    contar(ret.anonimizar_antiguas, 730, True)
    r = admin.get(f"/solicitudes/{sid}")
    assert r.status_code == 200 and r.json()["ciudadano"] == "Anónimo" and "ApiAnon" not in r.text
    assert admin.get("/solicitudes").status_code == 200


def test_la_auditoria_de_la_retencion_solo_guarda_cantidades_no_datos_personales():
    fila("NoFiltrar Nombre", correo="nofiltrar@mail.com", estado="resuelto", dias_resuelta=900)
    contar(ret.anonimizar_antiguas, 730, True)
    contar(ret.suprimir_titular, correo="nofiltrar@mail.com", aplicar=True)
    with t.SessionLocal() as db:
        registros = [e for e in db.scalars(select(t.Auditoria)) if e.accion in ("retencion_anonimizar", "supresion_titular")]
    assert registros and all(e.usuario == "sistema" for e in registros)
    # Cada registro debe ser SOLO una cantidad («N solicitudes»): así no cabe ningún nombre ni correo, sea cual sea el largo
    assert all(re.fullmatch(r"\d+ solicitudes", e.objeto) and e.detalle == "" for e in registros), [(e.objeto, e.detalle) for e in registros]


def test_limpiar_sesiones_borra_las_vencidas_hace_mucho_y_conserva_las_vigentes():
    t.sesion("sesion.func", "funcionario")           # vigente
    with t.SessionLocal() as db:
        u = db.scalar(select(t.Usuario).where(t.Usuario.usuario == "sesion.func"))
        db.add(t.Sesion(token_hash="a" * 64, usuario_id=u.id, expira=AHORA() - timedelta(days=90)))
        db.commit()
    assert contar(ret.limpiar_sesiones, 30, True) >= 1
    with t.SessionLocal() as db:
        assert db.scalar(select(t.Sesion).where(t.Sesion.token_hash == "a" * 64)) is None
        assert db.scalar(select(t.Sesion).where(t.Sesion.expira > AHORA())) is not None


def test_limpiar_auditoria_borra_lo_antiguo_y_deja_constancia():
    with t.SessionLocal() as db:
        db.add(t.Auditoria(fecha=AHORA() - timedelta(days=800), usuario="viejo.evento", accion="login"))
        db.commit()
    assert contar(ret.limpiar_auditoria, 365, True) >= 1
    with t.SessionLocal() as db:
        assert db.scalar(select(t.Auditoria).where(t.Auditoria.usuario == "viejo.evento")) is None
        assert db.scalar(select(t.Auditoria).where(t.Auditoria.accion == "retencion_auditoria")) is not None


def test_dias_negativos_se_rechazan():
    assert ret.main(["retencion", "antiguas", "--dias", "-5"]) == 2
