"""Crea un usuario pidiendo la clave por teclado (no queda en el historial ni en variables):
    python crear_usuario.py            # pregunta usuario, rol y clave
"""
import getpass
import sys

from app.db import ROLES, SessionLocal, Usuario, init_db
from app.seguridad import auditar, hashear, validar_clave


def main() -> None:
    init_db()
    usuario = input("Usuario: ").strip()
    rol = input(f"Rol {ROLES} [funcionario]: ").strip() or "funcionario"
    if rol not in ROLES:
        sys.exit("Rol inválido")
    clave = getpass.getpass("Clave (mínimo 12 caracteres): ")
    if clave != getpass.getpass("Repita la clave: "):
        sys.exit("Las claves no coinciden")
    try:
        validar_clave(clave, usuario)
    except ValueError as e:
        sys.exit(str(e))
    with SessionLocal() as db:
        if db.query(Usuario).filter_by(usuario=usuario).first():
            sys.exit("Ese usuario ya existe")
        db.add(Usuario(usuario=usuario, clave_hash=hashear(clave), rol=rol))
        auditar(db, "sistema", "crear_usuario", usuario, f"rol={rol} (por línea de comandos)")
        db.commit()
    print(f"Usuario '{usuario}' creado con rol {rol}")


if __name__ == "__main__":
    main()
