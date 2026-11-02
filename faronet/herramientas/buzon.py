#!/usr/bin/env python3
"""
buzon.py - Cliente del buzon muerto (dead drop) del edge.

Dos personas comparten un SECRETO (acordado por un canal privado, p. ej. un
mensaje cifrado de FaroNet). De el salen, sin que el Worker sepa nada:
    id    = BLAKE2b(secreto, key="faronet-buzon-id")[:16]     (identificador opaco)
    llave = BLAKE2b(secreto, key="faronet-buzon-llave")       (llave de SecretBox)
El blob mide siempre 1024 bytes:  Nonce(24) | SecretBox(Largo(2) | mensaje | relleno).
El Worker solo ve bytes cifrados de tamano fijo; el blob se borra al recogerlo
y vence a las 24 h.

El secreto debe ser largo y al azar (16 caracteres o mas): quien lo adivine
puede recoger el blob antes que el destinatario.

Privacidad: hablar directo con el Worker le muestra tu IP a Cloudflare. Para
ocultarla, manda estas peticiones desde un nodo detras de la mesh o de otra red.

Uso:
  py -m faronet.herramientas.buzon --edge http://localhost:8787 depositar --secreto "<secreto>" "mensaje"
  py -m faronet.herramientas.buzon --edge http://localhost:8787 recoger   --secreto "<secreto>"
"""
from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request

from nacl.encoding import RawEncoder
from nacl.exceptions import CryptoError
from nacl.hash import blake2b
from nacl.secret import SecretBox

TAM_BLOB = 1024                            # igual que edge/src/buzon.ts
TAM_NONCE = 24
TAM_PLANO = TAM_BLOB - TAM_NONCE - 16      # 984: lo que cabe dentro de la caja
MAX_MENSAJE = TAM_PLANO - 2                # 982 bytes de texto
# Cloudflare bloquea (403, error 1010) el User-Agent por defecto de urllib
# ("Python-urllib/3.x"); por eso, igual que gateway.py, se manda uno propio.
USER_AGENT = "faronet-buzon/1"


class BuzonError(Exception):
    """No se pudo depositar o recoger."""


def derivar(secreto: str) -> tuple[str, bytes]:
    """Del secreto compartido saca (id en hex, llave de 32 bytes)."""
    s = secreto.encode()
    ident = blake2b(s, digest_size=16, key=b"faronet-buzon-id", encoder=RawEncoder).hex()
    llave = blake2b(s, digest_size=32, key=b"faronet-buzon-llave", encoder=RawEncoder)
    return ident, llave


def sellar(llave: bytes, mensaje: bytes) -> bytes:
    """Cifra y rellena el mensaje hasta TAM_BLOB bytes."""
    if len(mensaje) > MAX_MENSAJE:
        raise BuzonError(f"mensaje de {len(mensaje)} bytes; el maximo es {MAX_MENSAJE}")
    plano = len(mensaje).to_bytes(2, "big") + mensaje + bytes(MAX_MENSAJE - len(mensaje))
    blob = bytes(SecretBox(llave).encrypt(plano))
    assert len(blob) == TAM_BLOB
    return blob


def abrir(llave: bytes, blob: bytes) -> bytes:
    """Descifra un blob; lanza BuzonError si no es para esta llave o fue alterado."""
    if len(blob) != TAM_BLOB:
        raise BuzonError("el blob no mide lo que debe")
    try:
        plano = SecretBox(llave).decrypt(blob)
    except CryptoError:
        raise BuzonError("no se pudo descifrar: secreto equivocado o blob alterado") from None
    n = int.from_bytes(plano[:2], "big")
    if n > MAX_MENSAJE:
        raise BuzonError("blob mal formado")
    return plano[2:2 + n]


def _pedir(metodo: str, edge: str, ident: str, cuerpo: bytes | None = None) -> tuple[int, bytes]:
    peticion = urllib.request.Request(
        f"{edge.rstrip('/')}/buzon/{ident}", data=cuerpo, method=metodo,
        headers={"User-Agent": USER_AGENT,
                 **({"Content-Type": "application/octet-stream"} if cuerpo else {})})
    try:
        with urllib.request.urlopen(peticion, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, OSError) as error:
        raise BuzonError(f"sin conexion con el Worker: {error}") from None


def depositar(edge: str, secreto: str, mensaje: bytes) -> None:
    ident, llave = derivar(secreto)
    codigo, _ = _pedir("PUT", edge, ident, sellar(llave, mensaje))
    if codigo == 409:
        raise BuzonError("ya hay un mensaje pendiente con ese secreto")
    if codigo != 201:
        raise BuzonError(f"el Worker respondio HTTP {codigo}")


def recoger(edge: str, secreto: str) -> bytes | None:
    """Devuelve el mensaje (y el Worker lo borra), o None si no hay nada."""
    ident, llave = derivar(secreto)
    codigo, blob = _pedir("GET", edge, ident)
    if codigo == 404:
        return None
    if codigo != 200:
        raise BuzonError(f"el Worker respondio HTTP {codigo}")
    return abrir(llave, blob)


def main() -> int:
    p = argparse.ArgumentParser(description="Buzon muerto de FaroNet")
    p.add_argument("--edge", required=True, help="URL del Worker")
    sub = p.add_subparsers(dest="orden", required=True)
    d = sub.add_parser("depositar", help="deja un mensaje cifrado")
    d.add_argument("--secreto", required=True)
    d.add_argument("mensaje")
    r = sub.add_parser("recoger", help="recoge (y borra) el mensaje")
    r.add_argument("--secreto", required=True)
    args = p.parse_args()
    try:
        if args.orden == "depositar":
            depositar(args.edge, args.secreto, args.mensaje.encode())
            print("depositado")
        else:
            mensaje = recoger(args.edge, args.secreto)
            print(mensaje.decode(errors="replace") if mensaje is not None else "no hay nada")
    except BuzonError as error:
        print(f"error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
