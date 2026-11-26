"""Carga solicitudes de ejemplo (ficticias) para la demo:  python seed.py"""
from app.agente import clasificar
from app.db import SessionLocal, Solicitud, init_db

EJEMPLOS = [
    ("María Gómez", "Hay un cable caído frente a mi casa en el barrio Chipre y es un peligro para los niños. Por favor atender hoy."),
    ("Carlos Ruiz", "Me cobraron en la factura de octubre un valor tres veces más alto que el normal. Solicito revisión y devolución."),
    ("Ana Torres", "Llevamos dos días sin agua en el sector y nadie nos da razón. Es una emergencia para mi familia."),
    ("Luis Pérez", "Sugiero que habiliten el pago de la factura por una aplicación móvil para evitar filas."),
    ("Paula Díaz", "Mala atención en la oficina del centro, el funcionario fue grosero y me hizo esperar tres horas."),
    ("Jorge Marín", "Quisiera solicitar un certificado de paz y salvo de mi servicio."),
    ("Sandra López", "Hay una fuga de agua en la tubería de la esquina que lleva una semana botando agua."),
]

if __name__ == "__main__":
    init_db()
    with SessionLocal() as db:
        for nombre, texto in EJEMPLOS:
            db.add(Solicitud(ciudadano=nombre, texto=texto, **clasificar(texto).dict()))
        db.commit()
    print(f"{len(EJEMPLOS)} solicitudes de ejemplo cargadas")
