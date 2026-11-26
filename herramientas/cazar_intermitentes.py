"""Caza pruebas intermitentes: ejecuta la suite varias veces (opcionalmente bajo carga artificial de CPU y disco) y guarda cada fallo con su traza.

    python herramientas/cazar_intermitentes.py --veces 10 [--carga 8] [--ruta .] [--log intermitentes.log] [-- -k smtp]

Una prueba que falla a veces es un hallazgo: oculta un problema de temporización, de estado compartido entre pruebas o del propio código.
Esta herramienta no lo arregla: junta evidencia (qué prueba, cuántas veces, con qué traza y bajo qué carga) para poder explicarlo.
Con --carga N lanza N procesos que consumen CPU y uno que escribe y borra archivos en el disco temporal, para reproducir lo que ocurre en una máquina ocupada.
Código de salida: 1 si alguna ejecución falló.
"""
import argparse
import collections
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


class Carga:
    """Carga artificial de CPU y disco mientras dura el bloque `with`."""

    def __init__(self, procesos: int):
        self.n, self.procs, self.parar = procesos, [], threading.Event()

    def __enter__(self):
        for _ in range(self.n):
            self.procs.append(subprocess.Popen([sys.executable, "-c", "while True: pass"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        if self.n:
            self.hilo = threading.Thread(target=self._disco, daemon=True)
            self.hilo.start()
        return self

    def _disco(self):
        carpeta = Path(tempfile.mkdtemp(prefix="carga_disco_"))
        datos = os.urandom(8 * 1024 * 1024)
        i = 0
        while not self.parar.is_set():
            archivo = carpeta / f"{i % 4}.bin"
            archivo.write_bytes(datos)
            archivo.read_bytes()
            i += 1
        for f in carpeta.glob("*.bin"):
            f.unlink(missing_ok=True)
        carpeta.rmdir()

    def __exit__(self, *a):
        self.parar.set()
        for p in self.procs:
            p.kill()
        for p in self.procs:
            p.wait()


def ejecutar(ruta: Path, extra: list[str]) -> tuple[int, str]:
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=short", "-W", "ignore", *extra],
                       cwd=ruta, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def fallos(salida: str) -> dict[str, str]:
    """Devuelve {nombre de la prueba: traza} a partir de la salida de pytest."""
    nombres = re.findall(r"^FAILED (\S+)", salida, re.M)
    bloques = re.split(r"^_{3,} (.+?) _{3,}$", salida, flags=re.M)   # [texto, nombre1, traza1, nombre2, traza2…]
    trazas = {bloques[i]: bloques[i + 1].strip() for i in range(1, len(bloques) - 1, 2)}
    return {n: next((t for corto, t in trazas.items() if corto.split("[")[0] in n), "(sin traza)") for n in nombres}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--veces", type=int, default=10)
    ap.add_argument("--carga", type=int, default=0, help="procesos que consumen CPU durante las ejecuciones (0 = sin carga)")
    ap.add_argument("--ruta", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--log", default="intermitentes.log")
    ap.add_argument("extra", nargs="*", help="argumentos para pytest, después de --")
    a = ap.parse_args(argv[1:])
    ruta, cuenta, trazas = Path(a.ruta), collections.Counter(), {}
    inicio = time.time()
    with Carga(a.carga):
        for i in range(1, a.veces + 1):
            t0 = time.time()
            codigo, salida = ejecutar(ruta, a.extra)
            falladas = fallos(salida) if codigo else {}
            resumen = (re.findall(r"\d+ (?:passed|failed)[^\n]*", salida) or ["(sin resumen)"])[-1]
            print(f"ejecución {i}/{a.veces}: {'OK' if not codigo else 'FALLÓ'}  {resumen}  ({time.time() - t0:.0f} s)", flush=True)
            for nombre, traza in falladas.items():
                cuenta[nombre] += 1
                trazas.setdefault(nombre, []).append((i, traza))
    with open(a.log, "w", encoding="utf-8") as f:
        f.write(f"{a.veces} ejecuciones, carga={a.carga} procesos, {time.time() - inicio:.0f} s, {len(cuenta)} prueba(s) con fallos\n\n")
        for nombre, veces in cuenta.most_common():
            f.write(f"=== {nombre}: falló {veces} de {a.veces} ===\n")
            for i, traza in trazas[nombre]:
                f.write(f"--- ejecución {i} ---\n{traza}\n\n")
    print(f"\n{sum(cuenta.values())} fallo(s) en {a.veces} ejecuciones, {len(cuenta)} prueba(s) distinta(s).")
    for nombre, veces in cuenta.most_common():
        print(f"  {veces}/{a.veces}  {nombre}")
    if cuenta:
        print(f"Trazas guardadas en {a.log}")
    return 1 if cuenta else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
