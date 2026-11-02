"""
gateway.py - Cliente del Worker (edge) para el gateway de FaroNet.

Firma y manda al Worker (edge/) las dos cosas que solo el gateway puede mandar:
  - mensajes publicos para una sala             POST /gateway/publicar
  - fichas del directorio de FaroNet (replica)  POST /gateway/directorio
El enganche con la cebolla (recibir mensajes publicos de la mesh y llamar a
publicar) esta en gateway_nodo.py.

La llave de firma del gateway es PROPIA (no la del nodo): ~/.faronet/gateway.llave

Uso (desde la raiz del repositorio):
    py -m faronet.herramientas.gateway --mostrar-llave   # esta llave va en GATEWAY_PUBKEY (edge/wrangler.jsonc)
    py -m faronet.herramientas.gateway --edge http://localhost:8787 --probar general "hola"
    py -m faronet.herramientas.gateway --edge https://faronet.USUARIO.workers.dev --probar general "hola"
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from nacl.signing import SigningKey

RUTA_LLAVE = Path.home() / ".faronet" / "gateway.llave"
RUTA_PUBLICAR = "/gateway/publicar"
RUTA_DIRECTORIO = "/gateway/directorio"
MAX_FICHAS_POR_ENVIO = 64   # igual que edge/src/index.ts


def cargar_llave(ruta: Path = RUTA_LLAVE) -> SigningKey:
    """Lee la llave de firma; si no existe, la crea."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    if ruta.exists():
        return SigningKey(ruta.read_bytes())
    llave = SigningKey.generate()
    ruta.write_bytes(bytes(llave))
    try:
        os.chmod(ruta, 0o600)
    except OSError:
        pass
    return llave


def firmar(llave: SigningKey, metodo: str, ruta: str, ts: int, cuerpo: bytes) -> str:
    """Debe coincidir con verificarGateway() de edge/src/auth.ts."""
    mensaje = f"{metodo}\n{ruta}\n{ts}\n{hashlib.sha256(cuerpo).hexdigest()}".encode()
    return llave.sign(mensaje).signature.hex()


def _enviar(edge: str, llave: SigningKey, ruta: str, cuerpo: bytes) -> tuple[int, bytes]:
    """
    POST firmado al Worker. Devuelve (codigo HTTP, cuerpo de la respuesta);
    codigo 0 si no hubo conexion. La firma cubre metodo, ruta, marca de tiempo
    y hash del cuerpo, y el Worker rechaza repeticiones.
    """
    ts = int(time.time())
    peticion = urllib.request.Request(
        edge.rstrip("/") + ruta, data=cuerpo, method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "faronet-gateway/1",
            "X-Faro-Ts": str(ts),
            "X-Faro-Firma": firmar(llave, "POST", ruta, ts, cuerpo),
        })
    try:
        with urllib.request.urlopen(peticion, timeout=10) as respuesta:
            return respuesta.status, respuesta.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, OSError):
        return 0, b""


def publicar(edge: str, llave: SigningKey, tema: str, texto: str) -> int:
    """Publica un mensaje en la sala 'tema'. Devuelve el codigo HTTP (204 = ok, 0 = sin conexion)."""
    cuerpo = json.dumps({"tema": tema, "texto": texto}, ensure_ascii=False).encode()
    return _enviar(edge, llave, RUTA_PUBLICAR, cuerpo)[0]


def publicar_fichas(edge: str, llave: SigningKey, fichas_hex: list[str]) -> tuple[int, dict]:
    """
    Manda fichas del directorio (hex de Ficha.codificar()) a la replica publica.
    El Worker verifica la firma de cada una. Devuelve (codigo, {"aceptadas": n, "rechazadas": [...]}).
    """
    resultado = {"aceptadas": 0, "rechazadas": []}
    codigo = 200
    for i in range(0, len(fichas_hex), MAX_FICHAS_POR_ENVIO):
        cuerpo = json.dumps({"fichas": fichas_hex[i:i + MAX_FICHAS_POR_ENVIO]}).encode()
        codigo, respuesta = _enviar(edge, llave, RUTA_DIRECTORIO, cuerpo)
        if codigo != 200:
            return codigo, resultado
        datos = json.loads(respuesta)
        resultado["aceptadas"] += datos["aceptadas"]
        resultado["rechazadas"] += datos["rechazadas"]
    return codigo, resultado


def main():
    p = argparse.ArgumentParser(description="Cliente del Worker de FaroNet")
    p.add_argument("--edge", help="URL del Worker, ej. http://localhost:8787")
    p.add_argument("--llave", type=Path, default=RUTA_LLAVE, help="archivo de la llave de firma")
    p.add_argument("--mostrar-llave", action="store_true", help="imprime la llave publica (hex)")
    p.add_argument("--probar", nargs=2, metavar=("TEMA", "TEXTO"), help="publica un mensaje de prueba")
    args = p.parse_args()

    llave = cargar_llave(args.llave)
    if args.mostrar_llave:
        print(llave.verify_key.encode().hex())
        return
    if args.probar:
        if not args.edge:
            p.error("--probar necesita --edge")
        codigo = publicar(args.edge, llave, *args.probar)
        print(f"respuesta del Worker: HTTP {codigo}" + (" (sin conexion)" if codigo == 0 else ""))
        sys.exit(0 if codigo == 204 else 1)
    p.print_help()


if __name__ == "__main__":
    main()