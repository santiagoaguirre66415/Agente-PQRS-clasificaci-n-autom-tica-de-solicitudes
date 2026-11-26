"""Monitoreo de la aplicación: calcula métricas desde la base de datos y avisa si algo requiere atención.

    python herramientas/monitoreo.py [--json] [--correo-a admin@tuempresa.com]

Programe esta herramienta (cada 15 minutos o cada hora) y haga que su programador reaccione al código de salida:
0 = todo en orden, 1 = hay alertas. Con --correo-a envía el resumen por el correo que la app ya tiene configurado (solo si hay alertas).
Además, `GET /salud` responde {"ok": true} sin datos sensibles: úselo con un monitor de disponibilidad gratuito (UptimeRobot, Healthchecks.io, etc.).

Umbrales ajustables por variable de entorno (entre paréntesis, el valor por defecto):
  PQRS_ALERTA_LOGINS_FALLIDOS_1H (20)  PQRS_ALERTA_BLOQUEOS_1H (3)  PQRS_ALERTA_ENVIOS_FALLIDOS_24H (3)  PQRS_ALERTA_ACCESOS_DENEGADOS_1H (10)
  PQRS_ALERTA_POR_VENCER (5)  PQRS_ALERTA_NUEVAS_SIN_ATENDER_HORAS (48)  PQRS_RESPALDO_DIR (carpeta de respaldos)  PQRS_ALERTA_RESPALDO_HORAS (36)
No guarda ni muestra datos personales: solo conteos.
"""
import argparse
import calendar
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import Auditoria, SessionLocal, Solicitud, init_db  # noqa: E402
from app.seguridad import ahora as _ahora  # noqa: E402

ALERTA, AVISO = "ALERTA", "AVISO"


def _n(db: Session, accion: str, desde) -> int:
    return db.scalar(select(func.count(Auditoria.id)).where(Auditoria.accion == accion, Auditoria.fecha >= desde)) or 0


def metricas(db: Session, ahora=None, env=None) -> dict:
    ahora = ahora or _ahora()
    env = os.environ if env is None else env
    hora, dia = ahora - timedelta(hours=1), ahora - timedelta(hours=24)
    sin_responder = Solicitud.respondido_en.is_(None)
    horas_sin_atender = float(env.get("PQRS_ALERTA_NUEVAS_SIN_ATENDER_HORAS", 48))
    m = {
        "logins_fallidos_1h": _n(db, "login_fallido", hora),
        "bloqueos_1h": _n(db, "login_bloqueado", hora),
        "accesos_denegados_1h": _n(db, "acceso_denegado", hora),
        "envios_fallidos_24h": _n(db, "envio_fallido", dia),
        "rebotes_24h": db.scalar(select(func.count(Solicitud.id)).where(Solicitud.entrega == "rebotado", Solicitud.entrega_en >= dia)) or 0,
        "vencidas": db.scalar(select(func.count(Solicitud.id)).where(sin_responder, Solicitud.fecha_limite < ahora)) or 0,
        "por_vencer_3d": db.scalar(select(func.count(Solicitud.id)).where(sin_responder, Solicitud.fecha_limite >= ahora,
                                                                           Solicitud.fecha_limite < ahora + timedelta(days=3))) or 0,
        "nuevas_sin_atender": db.scalar(select(func.count(Solicitud.id)).where(
            Solicitud.estado == "nuevo", Solicitud.creado < ahora - timedelta(hours=horas_sin_atender))) or 0,
        "total_solicitudes": db.scalar(select(func.count(Solicitud.id))) or 0,
    }
    ultima_ret = db.scalar(select(func.max(Auditoria.fecha)).where(Auditoria.accion.in_(("retencion_anonimizar", "supresion_titular", "retencion_sesiones"))))
    m["dias_desde_retencion"] = None if ultima_ret is None else round((ahora - ultima_ret).total_seconds() / 86400, 1)
    rdir = env.get("PQRS_RESPALDO_DIR")
    if rdir:
        archivos = [a for a in Path(rdir).glob("pqrs-*.zip*") if not a.name.endswith(".sha256")] if Path(rdir).exists() else []
        # `ahora` es una fecha UTC SIN zona: datetime.timestamp() la tomaría como hora LOCAL y daría un error igual al desfase horario del servidor
        ahora_epoch = calendar.timegm(ahora.timetuple())
        m["horas_desde_respaldo"] = round((ahora_epoch - max(a.stat().st_mtime for a in archivos)) / 3600, 1) if archivos else None
    return m


def alertas(m: dict, env=None) -> list[tuple[str, str]]:
    env = os.environ if env is None else env
    u = lambda k, d: float(env.get(k, d))  # noqa: E731
    r = []
    if m["vencidas"] >= 1:
        r.append((ALERTA, f"{m['vencidas']} solicitud(es) VENCIDA(S) sin responder: riesgo de incumplir el plazo legal"))
    if m["por_vencer_3d"] >= u("PQRS_ALERTA_POR_VENCER", 5):
        r.append((AVISO, f"{m['por_vencer_3d']} solicitudes vencen en menos de 3 días"))
    if m["logins_fallidos_1h"] >= u("PQRS_ALERTA_LOGINS_FALLIDOS_1H", 20):
        r.append((ALERTA, f"{m['logins_fallidos_1h']} inicios de sesión fallidos en la última hora: posible fuerza bruta"))
    if m["bloqueos_1h"] >= u("PQRS_ALERTA_BLOQUEOS_1H", 3):
        r.append((ALERTA, f"{m['bloqueos_1h']} bloqueos por intentos en la última hora"))
    if m["accesos_denegados_1h"] >= u("PQRS_ALERTA_ACCESOS_DENEGADOS_1H", 10):
        r.append((AVISO, f"{m['accesos_denegados_1h']} accesos denegados por rol en la última hora"))
    if m["envios_fallidos_24h"] >= u("PQRS_ALERTA_ENVIOS_FALLIDOS_24H", 3):
        r.append((ALERTA, f"{m['envios_fallidos_24h']} envíos de correo fallidos en 24 h: revise el proveedor o la contraseña"))
    if m["rebotes_24h"] >= 1:
        r.append((AVISO, f"{m['rebotes_24h']} respuesta(s) rebotaron en 24 h: contacte a esas personas por otro medio"))
    if m["nuevas_sin_atender"] >= 1:
        r.append((AVISO, f"{m['nuevas_sin_atender']} solicitud(es) nuevas sin atender hace más de {u('PQRS_ALERTA_NUEVAS_SIN_ATENDER_HORAS', 48):g} h"))
    if "horas_desde_respaldo" in m:
        h = m["horas_desde_respaldo"]
        if h is None:
            r.append((ALERTA, "No hay ningún respaldo en PQRS_RESPALDO_DIR"))
        elif h > u("PQRS_ALERTA_RESPALDO_HORAS", 36):
            r.append((ALERTA, f"El último respaldo fue hace {h:g} h"))
    d = m["dias_desde_retencion"]
    if m["total_solicitudes"] and (d is None or d > 14):
        r.append((AVISO, "La retención de datos no se ha ejecutado en los últimos 14 días" if d is not None else "La retención de datos nunca se ha ejecutado"))
    return r


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--correo-a")
    a = ap.parse_args(argv[1:])
    init_db()
    with SessionLocal() as db:
        m = metricas(db)
    al = alertas(m)
    if a.json:
        print(json.dumps({"metricas": m, "alertas": [{"nivel": n, "texto": t} for n, t in al]}, ensure_ascii=False, indent=2))
    else:
        for k, v in m.items():
            print(f"{k:24} {v}")
        print()
        for n, t in al:
            print(f"[{n}] {t}")
        if not al:
            print("[OK] Sin alertas.")
    hay_alerta = any(n == ALERTA for n, _ in al)
    if a.correo_a and al:
        from app import correo
        if correo.configurado():
            try:
                correo.enviar(a.correo_a, f"{'ALERTA' if hay_alerta else 'Aviso'} del sistema de PQRS",
                              "\n".join(f"[{n}] {t}" for n, t in al) + "\n\nMensaje automático del monitoreo.")
            except correo.ErrorCorreo as e:
                print(f"No se pudo enviar el correo de alerta ({e}).")
    return 1 if hay_alerta else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
