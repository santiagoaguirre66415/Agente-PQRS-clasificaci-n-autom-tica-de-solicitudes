"""Mide qué tan barato es para un atacante superar la prueba de trabajo del formulario público.

Uso:  python herramientas/simular_abuso.py costos
      python herramientas/simular_abuso.py inundar http://localhost:8000 50      (solo contra localhost)

«costos» resuelve retos con hashlib (un solo núcleo, Python) y muestra cuánto tarda por cada dificultad. Sirve para decidir
PQRS_ANTIBOT_BITS con datos: más bits encarecen el cálculo, pero también lo hacen más lento para las personas en móviles modestos.

«inundar» envía N solicitudes con prueba válida a una instancia LOCAL para comprobar los topes (por IP, globales). Por seguridad
solo acepta localhost / 127.0.0.1: no se usa contra sistemas ajenos.
"""
import hashlib
import statistics
import sys
import time
from urllib.parse import urlparse

import httpx


def ceros(digest: bytes) -> int:
    n = 0
    for b in digest:
        if b == 0:
            n += 8
            continue
        return n + 8 - b.bit_length()
    return n


def resolver(reto: str, bits: int) -> tuple[str, int]:
    n = 0
    while True:
        if ceros(hashlib.sha256(f"{reto}:{n}".encode()).digest()) >= bits:
            return str(n), n + 1
        n += 1


def costos(muestras: int = 15) -> None:
    print(f"{'bits':>5} {'hashes (media)':>16} {'ms por reto (Python)':>22} {'retos/segundo/núcleo':>22}")
    for bits in (12, 16, 18, 20, 22):
        tiempos, hashes = [], []
        for i in range(muestras):
            t0 = time.perf_counter()
            _, h = resolver(f"1.reto{bits}-{i}.firma", bits)
            tiempos.append(time.perf_counter() - t0)
            hashes.append(h)
        media = statistics.mean(tiempos)
        print(f"{bits:>5} {statistics.mean(hashes):>16,.0f} {media * 1000:>22,.1f} {1 / media:>22,.1f}")
    print("\nLectura: la prueba de trabajo es una primera capa contra envíos automáticos simples. Úsela junto con los topes (por IP y global)\n"
          "y la revisión del personal; si el volumen de abuso lo exige, añada además un CAPTCHA. Use estos números para elegir PQRS_ANTIBOT_BITS.")


def inundar(base: str, n: int) -> None:
    host = urlparse(base).hostname
    if host not in ("localhost", "127.0.0.1", "::1"):
        sys.exit("Por seguridad, 'inundar' solo funciona contra localhost.")
    codigos: dict[int, int] = {}
    with httpx.Client(base_url=base, timeout=30) as c:
        for i in range(n):
            info = c.get("/api/reto")
            if info.status_code != 200:
                codigos[info.status_code] = codigos.get(info.status_code, 0) + 1
                continue
            reto, bits = info.json()["reto"], info.json()["bits"]
            time.sleep(2.1)  # respeta la trampa de tiempo mínima: simula a un atacante paciente
            prueba, _ = resolver(reto, bits)
            r = c.post("/solicitudes", json={"ciudadano": f"Simulado {i}", "texto": f"Solicitud simulada número {i} para probar los topes",
                                             "acepto": True, "reto": reto, "prueba": prueba})
            codigos[r.status_code] = codigos.get(r.status_code, 0) + 1
    print("Resultados por código HTTP:", dict(sorted(codigos.items())))


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "costos":
        costos()
    elif len(sys.argv) == 4 and sys.argv[1] == "inundar":
        inundar(sys.argv[2], int(sys.argv[3]))
    else:
        print(__doc__)
