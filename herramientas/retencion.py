"""Conservación y supresión de datos personales (gratuita, sin servicios externos).

Uso (SIMULA por defecto; agregue --aplicar para ejecutar de verdad):
  python herramientas/retencion.py antiguas --dias 730 [--aplicar]
      Anonimiza las solicitudes RESUELTAS hace más de N días: borra nombre, correo, texto y respuestas; conserva los datos
      estadísticos (tipo, categoría, prioridad, fechas, área).
  python herramientas/retencion.py suprimir --correo persona@mail.com [--aplicar]
  python herramientas/retencion.py suprimir --id 123 [--aplicar]
      Atiende una solicitud de supresión de un titular: anonimiza TODAS sus solicitudes, estén o no resueltas.
  python herramientas/retencion.py sesiones --dias 30 [--aplicar]       Borra sesiones vencidas hace más de N días.
  python herramientas/retencion.py auditoria --dias 365 [--aplicar]     Borra eventos de auditoría de más de N días.

Programe «antiguas», «sesiones» y «auditoria» (Programador de tareas de Windows o cron) con los plazos de PRIVACIDAD.md.
Cada ejecución real deja un registro de auditoría con CUÁNTOS registros se tocaron, nunca con datos personales.
La base de datos se toma de DATABASE_URL (la misma de la app).
"""
import argparse
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import Auditoria, Sesion, SessionLocal, Solicitud, init_db  # noqa: E402
from app.seguridad import ahora as _ahora, auditar  # noqa: E402

MARCA = "[Eliminado por política de conservación]"


def _ya_anonimizada(s: Solicitud) -> bool:
    return s.ciudadano == "Anónimo" and s.texto == MARCA and not s.correo


def _anonimizar(s: Solicitud) -> None:
    s.ciudadano, s.correo, s.texto, s.resumen, s.respuesta_sugerida = "Anónimo", "", MARCA, "", ""
    if s.respuesta_final is not None:  # la restricción de la base exige que la respuesta oficial exista si hubo respuesta
        s.respuesta_final = MARCA
    s.mensaje_id, s.correo_verificado, s.verif_hash, s.verif_expira = None, False, None, None


def _tocar(db: Session, filas: list[Solicitud], accion: str, aplicar: bool) -> int:
    filas = [s for s in filas if not _ya_anonimizada(s)]
    if aplicar and filas:
        for s in filas:
            _anonimizar(s)
        auditar(db, "sistema", accion, f"{len(filas)} solicitudes")  # solo la cantidad: nunca datos personales
        db.commit()
    return len(filas)


def anonimizar_antiguas(db: Session, dias: int, aplicar: bool = False, ahora=None) -> int:
    limite = (ahora or _ahora()) - timedelta(days=dias)
    filas = list(db.scalars(select(Solicitud).where(Solicitud.estado == "resuelto", Solicitud.resuelto_en < limite)))
    return _tocar(db, filas, "retencion_anonimizar", aplicar)


def suprimir_titular(db: Session, correo: str | None = None, solicitud_id: int | None = None, aplicar: bool = False) -> int:
    if bool(correo) == bool(solicitud_id):
        raise ValueError("Indique exactamente uno: --correo o --id")
    if correo:
        filas = list(db.scalars(select(Solicitud).where(Solicitud.correo == correo.strip().lower())))
    else:
        s = db.get(Solicitud, solicitud_id)
        filas = [s] if s else []
    return _tocar(db, filas, "supresion_titular", aplicar)


def limpiar_sesiones(db: Session, dias: int = 30, aplicar: bool = False, ahora=None) -> int:
    limite = (ahora or _ahora()) - timedelta(days=dias)
    ids = list(db.scalars(select(Sesion.id).where(Sesion.expira < limite)))
    if aplicar and ids:
        db.execute(delete(Sesion).where(Sesion.id.in_(ids)))
        auditar(db, "sistema", "retencion_sesiones", f"{len(ids)} sesiones")
        db.commit()
    return len(ids)


def limpiar_auditoria(db: Session, dias: int = 365, aplicar: bool = False, ahora=None) -> int:
    limite = (ahora or _ahora()) - timedelta(days=dias)
    ids = list(db.scalars(select(Auditoria.id).where(Auditoria.fecha < limite)))
    if aplicar and ids:
        db.execute(delete(Auditoria).where(Auditoria.id.in_(ids)))
        auditar(db, "sistema", "retencion_auditoria", f"{len(ids)} eventos")
        db.commit()
    return len(ids)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="orden", required=True)
    for nombre, dias in (("antiguas", 730), ("sesiones", 30), ("auditoria", 365)):
        p = sub.add_parser(nombre)
        p.add_argument("--dias", type=int, default=dias)
        p.add_argument("--aplicar", action="store_true")
    p = sub.add_parser("suprimir")
    p.add_argument("--correo")
    p.add_argument("--id", type=int)
    p.add_argument("--aplicar", action="store_true")
    a = ap.parse_args(argv[1:])
    if getattr(a, "dias", 1) < 0:
        print("--dias no puede ser negativo")
        return 2
    init_db()
    with SessionLocal() as db:
        try:
            if a.orden == "antiguas":
                n = anonimizar_antiguas(db, a.dias, a.aplicar)
            elif a.orden == "suprimir":
                n = suprimir_titular(db, a.correo, a.id, a.aplicar)
            elif a.orden == "sesiones":
                n = limpiar_sesiones(db, a.dias, a.aplicar)
            else:
                n = limpiar_auditoria(db, a.dias, a.aplicar)
        except ValueError as e:
            print(e)
            return 2
    verbo = "Se procesaron" if a.aplicar else "SIMULACIÓN: se procesarían"
    print(f"{verbo} {n} registro(s)." + ("" if a.aplicar else " Agregue --aplicar para ejecutarlo."))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
