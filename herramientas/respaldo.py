"""Copias de seguridad de la base SQLite, con verificación e intento de restauración REAL (gratuito).

  python herramientas/respaldo.py crear   --destino D:\\respaldos [--conservar 14] [--clave-archivo ruta] [--copiar-a E:\\externo]
  python herramientas/respaldo.py verificar D:\\respaldos\\pqrs-20261126-030000.zip [--clave-archivo ruta]
  python herramientas/respaldo.py restaurar D:\\respaldos\\pqrs-....zip --a pqrs.db [--clave-archivo ruta] [--forzar]
  python herramientas/respaldo.py listar  --destino D:\\respaldos
  python herramientas/respaldo.py generar-clave --archivo ruta\\respaldo.key

«crear» usa la copia EN CALIENTE de SQLite (segura con la app encendida), comprueba la integridad, la comprime y guarda su huella SHA-256.
«verificar» NO toca la base viva: restaura la copia en una carpeta temporal y cuenta las filas, que es la única prueba de que un respaldo sirve.
«restaurar» no sobrescribe nada salvo que use --forzar (y entonces guarda antes lo que había como .antes). APAGUE LA APP antes de restaurar.

La copia contiene DATOS PERSONALES. Con --clave-archivo se cifra (Fernet, requiere `pip install cryptography`); sin clave queda SIN CIFRAR y se avisa.
Guarde la clave APARTE de los respaldos (sin ella no se pueden recuperar). Para PostgreSQL use `pg_dump` (ver DESPLIEGUE.md): esta herramienta es solo para SQLite.
Los respaldos antiguos conservan datos que la retención ya anonimizó en la base viva: aplique a los respaldos la misma política de conservación.
"""
import argparse
import hashlib
import io
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile
from contextlib import closing
from pathlib import Path

PATRON = re.compile(r"^pqrs-\d{8}-\d{6}\.zip(\.enc)?$")
# Consultas completas y fijas (nada se arma con texto): así no hay forma de inyectar SQL
_CONTEOS = {
    "solicitudes": "SELECT COUNT(*) FROM solicitudes",
    "usuarios": "SELECT COUNT(*) FROM usuarios",
    "auditoria": "SELECT COUNT(*) FROM auditoria",
}


def ruta_base() -> Path:
    url = os.environ.get("DATABASE_URL", "sqlite:///pqrs.db")
    if not url.startswith("sqlite:///"):
        raise SystemExit("Esta herramienta solo respalda SQLite. Para PostgreSQL use pg_dump (ver DESPLIEGUE.md).")
    return Path(url[len("sqlite:///"):])


def _fernet(clave_archivo: str | None):
    if not clave_archivo:
        return None
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        raise SystemExit("Para cifrar o descifrar instale: pip install cryptography")
    return Fernet(Path(clave_archivo).read_bytes().strip())


def generar_clave(archivo: Path) -> None:
    from cryptography.fernet import Fernet
    if archivo.exists():
        raise SystemExit(f"{archivo} ya existe: no se sobrescribe una clave.")
    archivo.write_bytes(Fernet.generate_key())
    try:
        archivo.chmod(0o600)  # en Windows no tiene efecto: restrinja el acceso al archivo con sus permisos (icacls)
    except OSError:
        pass


def _copia_en_caliente(origen: Path, destino: Path) -> None:
    if not origen.exists():
        raise SystemExit(f"No existe la base de datos: {origen}")
    # OJO: `with sqlite3.connect(...)` solo confirma o revierte, NO cierra la conexión; en Windows el archivo quedaría bloqueado.
    try:
        with closing(sqlite3.connect(f"file:{origen}?mode=ro", uri=True)) as a, closing(sqlite3.connect(destino)) as b:
            a.backup(b)
    except sqlite3.DatabaseError as e:  # base dañada, bloqueada o que no es SQLite: mensaje claro en vez de un traceback
        raise SystemExit(f"No se pudo copiar la base de datos de origen ({e}). ¿Está dañada o bloqueada?")
    with closing(sqlite3.connect(destino)) as b:
        resultado = b.execute("PRAGMA integrity_check").fetchone()[0]
    if resultado != "ok":
        raise SystemExit(f"La copia no pasó la comprobación de integridad: {resultado}")


def crear(origen: Path, destino_dir: Path, clave_archivo: str | None = None, conservar: int = 14, ahora: float | None = None) -> Path:
    destino_dir.mkdir(parents=True, exist_ok=True)
    f = _fernet(clave_archivo)
    sello = time.strftime("%Y%m%d-%H%M%S", time.localtime(ahora or time.time()))
    with tempfile.TemporaryDirectory() as tmp:
        copia = Path(tmp) / "pqrs.db"
        _copia_en_caliente(origen, copia)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(copia, "pqrs.db")
        datos = buffer.getvalue()
    if f:
        datos = f.encrypt(datos)
    archivo = destino_dir / (f"pqrs-{sello}.zip" + (".enc" if f else ""))
    archivo.write_bytes(datos)
    (archivo.parent / (archivo.name + ".sha256")).write_text(hashlib.sha256(datos).hexdigest() + "\n", encoding="utf-8")
    rotar(destino_dir, conservar)
    return archivo


def _mismo_disco(a: Path, b: Path) -> bool:
    """True si las dos rutas están en el mismo volumen (una copia así NO protege contra la falla del disco ni contra un robo del equipo)."""
    a, b = a.resolve(), b.resolve()
    if os.name == "nt":
        return a.drive.lower() == b.drive.lower()
    existente = lambda p: next(x for x in (p, *p.parents) if x.exists())  # noqa: E731
    return existente(a).stat().st_dev == existente(b).stat().st_dev


def copiar_externo(archivo: Path, destino_dir: Path, clave_archivo: str | None = None, conservar: int = 14) -> tuple[Path, bool]:
    """Copia el respaldo a OTRO lugar (otro disco, unidad de red, carpeta sincronizada) y comprueba restaurando DESDE LA COPIA.

    Devuelve (ruta de la copia, ¿está en el mismo disco que el original?). Falla si la copia no es idéntica o no se puede restaurar.
    Una copia que nunca se restauró no es una copia: por eso la verificación se hace sobre el archivo copiado, no sobre el original."""
    if destino_dir.resolve() == archivo.parent.resolve():
        raise SystemExit("La carpeta de la copia externa no puede ser la misma que la de los respaldos.")
    destino_dir.mkdir(parents=True, exist_ok=True)
    copia = destino_dir / archivo.name
    shutil.copy2(archivo, copia)
    huella_original = hashlib.sha256(archivo.read_bytes()).hexdigest()
    if hashlib.sha256(copia.read_bytes()).hexdigest() != huella_original:
        copia.unlink(missing_ok=True)
        raise SystemExit("La copia externa no coincide con el original (SHA-256 distinto): se descartó. Revise el disco o la conexión de destino.")
    (destino_dir / (archivo.name + ".sha256")).write_text(huella_original + "\n", encoding="utf-8")
    verificar(copia, clave_archivo)   # restaura desde la copia externa: si falla, esta línea corta con un mensaje claro
    rotar(destino_dir, conservar)
    return copia, _mismo_disco(archivo, copia)


def rotar(destino_dir: Path, conservar: int) -> list[Path]:
    """Conserva los N respaldos más recientes (por nombre, que lleva la fecha) y borra el resto con su huella."""
    archivos = sorted(p for p in destino_dir.iterdir() if PATRON.match(p.name))
    sobran = archivos[:-conservar] if conservar > 0 else []
    for p in sobran:
        p.unlink()
        (p.parent / (p.name + ".sha256")).unlink(missing_ok=True)
    return sobran


def listar(destino_dir: Path) -> list[Path]:
    return sorted(p for p in destino_dir.iterdir() if PATRON.match(p.name)) if destino_dir.exists() else []


def _extraer(archivo: Path, clave_archivo: str | None, carpeta: Path) -> Path:
    datos = archivo.read_bytes()
    huella = archivo.parent / (archivo.name + ".sha256")
    if huella.exists() and hashlib.sha256(datos).hexdigest() != huella.read_text(encoding="utf-8").strip():
        raise SystemExit("La huella SHA-256 no coincide: el respaldo está dañado o fue modificado.")
    if archivo.name.endswith(".enc"):
        f = _fernet(clave_archivo)
        if not f:
            raise SystemExit("Este respaldo está cifrado: indique --clave-archivo.")
        try:
            datos = f.decrypt(datos)
        except Exception:
            raise SystemExit("No se pudo descifrar: la clave no corresponde a este respaldo.")
    try:
        with zipfile.ZipFile(io.BytesIO(datos)) as z:
            z.extract("pqrs.db", carpeta)
    except (zipfile.BadZipFile, KeyError):
        raise SystemExit("El archivo no es un respaldo válido.")
    return carpeta / "pqrs.db"


def verificar(archivo: Path, clave_archivo: str | None = None) -> dict:
    """Restaura en una carpeta temporal (la base viva no se toca) y cuenta las filas de las tablas principales."""
    with tempfile.TemporaryDirectory() as tmp:
        base = _extraer(archivo, clave_archivo, Path(tmp))
        with closing(sqlite3.connect(base)) as c:
            integridad = c.execute("PRAGMA integrity_check").fetchone()[0]
            if integridad != "ok":
                raise SystemExit(f"Integridad: {integridad}")
            tablas = {nombre: c.execute(consulta).fetchone()[0] for nombre, consulta in _CONTEOS.items()}
    return {"integridad": integridad, "filas": tablas, "cifrado": archivo.name.endswith(".enc")}


def restaurar(archivo: Path, destino: Path, clave_archivo: str | None = None, forzar: bool = False) -> Path | None:
    if destino.exists() and not forzar:
        raise SystemExit(f"{destino} ya existe. Apague la app y use --forzar para reemplazarla (se guarda una copia .antes).")
    with tempfile.TemporaryDirectory() as tmp:
        base = _extraer(archivo, clave_archivo, Path(tmp))
        previo = None
        if destino.exists():
            previo = destino.with_name(destino.name + ".antes")
            destino.replace(previo)
        destino.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(base, destino)
    return previo


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="orden", required=True)
    p = sub.add_parser("crear")
    p.add_argument("--destino", required=True)
    p.add_argument("--conservar", type=int, default=14)
    p.add_argument("--clave-archivo")
    p.add_argument("--copiar-a", help="segunda carpeta, EN OTRO DISCO o equipo: copia y restaura desde allí para comprobarla")
    p = sub.add_parser("verificar")
    p.add_argument("archivo")
    p.add_argument("--clave-archivo")
    p = sub.add_parser("restaurar")
    p.add_argument("archivo")
    p.add_argument("--a", required=True)
    p.add_argument("--clave-archivo")
    p.add_argument("--forzar", action="store_true")
    p = sub.add_parser("listar")
    p.add_argument("--destino", required=True)
    p = sub.add_parser("generar-clave")
    p.add_argument("--archivo", required=True)
    a = ap.parse_args(argv[1:])
    if a.orden == "crear":
        archivo = crear(ruta_base(), Path(a.destino), a.clave_archivo, a.conservar)
        print(f"Respaldo creado: {archivo} ({archivo.stat().st_size / 1024:,.0f} KB)")
        if not a.clave_archivo:
            print("AVISO: SIN CIFRAR. Contiene datos personales: use --clave-archivo o guárdelo en un disco cifrado.")
        r = verificar(archivo, a.clave_archivo)
        print(f"Verificado restaurando en una carpeta temporal: integridad={r['integridad']} filas={r['filas']}")
        if a.copiar_a:
            copia, mismo = copiar_externo(archivo, Path(a.copiar_a), a.clave_archivo, a.conservar)
            print(f"Copia externa creada y verificada restaurando DESDE ELLA: {copia}")
            if mismo:
                print("AVISO: la copia externa está en el MISMO disco que el original: no protege contra la falla del disco ni el robo del equipo. Use otro disco, otro equipo o la nube.")
    elif a.orden == "verificar":
        r = verificar(Path(a.archivo), a.clave_archivo)
        print(f"OK. integridad={r['integridad']} cifrado={r['cifrado']} filas={r['filas']}")
    elif a.orden == "restaurar":
        previo = restaurar(Path(a.archivo), Path(a.a), a.clave_archivo, a.forzar)
        print(f"Restaurado en {a.a}." + (f" Lo anterior quedó en {previo}." if previo else ""))
    elif a.orden == "listar":
        for p in listar(Path(a.destino)):
            print(f"{p.name}  {p.stat().st_size / 1024:,.0f} KB")
    else:
        generar_clave(Path(a.archivo))
        print(f"Clave creada en {a.archivo}. Guárdela APARTE de los respaldos y restrinja su acceso.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
