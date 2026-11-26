"""Envía a su app un aviso de entrega o rebote FIRMADO, como lo haría su proveedor de correo (o un traductor n8n/Make).
Sirve para probar el webhook y la integración con cualquier proveedor, incluidos duplicados y eventos fuera de orden.

    $env:PQRS_WEBHOOK_SECRETO = "el-mismo-secreto-que-usa-la-app"
    python herramientas/enviar_evento_webhook.py --url http://localhost:8000 --mensaje-id "<id@dominio>" --evento rebotado [--repetir 3] [--antiguedad 0]

El secreto se lee SOLO de la variable de entorno (no como argumento, para que no quede en el historial del terminal).
--antiguedad N envía un evento con N segundos de antigüedad: con más de 300 la app debe rechazarlo (prueba de repeticiones).
Contrato: cuerpo JSON {"mensaje_id", "evento": "entregado"|"rebotado", "fecha": segundos epoch}; cabecera X-Firma: sha256=HMAC-SHA256(cuerpo).
"""
import argparse
import hashlib
import hmac
import json
import os
import sys
import time

import httpx


def construir(mensaje_id: str, evento: str, antiguedad: float = 0.0) -> bytes:
    return json.dumps({"mensaje_id": mensaje_id, "evento": evento, "fecha": time.time() - antiguedad}, separators=(",", ":")).encode()


def firmar(cuerpo: bytes, secreto: str) -> str:
    return "sha256=" + hmac.new(secreto.encode(), cuerpo, hashlib.sha256).hexdigest()


def enviar(cliente: httpx.Client, url: str, mensaje_id: str, evento: str, secreto: str, antiguedad: float = 0.0) -> httpx.Response:
    cuerpo = construir(mensaje_id, evento, antiguedad)
    return cliente.post(url.rstrip("/") + "/webhooks/correo", content=cuerpo,
                        headers={"X-Firma": firmar(cuerpo, secreto), "Content-Type": "application/json"})


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--mensaje-id", required=True)
    ap.add_argument("--evento", required=True, choices=["entregado", "rebotado"])
    ap.add_argument("--repetir", type=int, default=1)
    ap.add_argument("--antiguedad", type=float, default=0.0)
    a = ap.parse_args(argv[1:])
    secreto = os.environ.get("PQRS_WEBHOOK_SECRETO", "")
    if not secreto:
        print("Defina la variable de entorno PQRS_WEBHOOK_SECRETO (el mismo secreto que usa la app).")
        return 2
    with httpx.Client(timeout=15) as c:
        for i in range(max(1, a.repetir)):
            try:
                r = enviar(c, a.url, a.mensaje_id, a.evento, secreto, a.antiguedad)
            except httpx.HTTPError as e:
                print(f"#{i + 1}: no se pudo conectar ({type(e).__name__})")
                return 1
            print(f"#{i + 1}: HTTP {r.status_code} {r.text[:120]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
