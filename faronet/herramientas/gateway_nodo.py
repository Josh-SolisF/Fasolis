#!/usr/bin/env python3
"""
gateway_nodo.py - Nodo gateway de FaroNet: une la mesh anonima con el edge.

Es un nodo normal (nodo.py) con dos tareas mas:
  1. Mensajes PUBLICOS: cuando la cebolla le entrega un mensaje con tema
     (comando "publico <gateway> <tema> <mensaje>" en otro nodo), lo publica en
     la sala de ese tema del Worker, con la peticion firmada. Es lo unico que
     sale de FaroNet. Los mensajes privados nunca pasan por aqui.
  2. Directorio: cada --sync-directorio segundos consulta el directorio de la
     mesh y manda las fichas al Worker, que las verifica una por una y las
     publica como replica de solo lectura (GET /directorio).

Privacidad: el gateway ve el tema y el texto (son publicos por definicion),
pero no quien los origino: llegan por la cebolla. No se guarda nada a disco ni
se imprime el contenido; solo "publicado en #tema".

Uso (la llave publica que imprime --mostrar-llave va en GATEWAY_PUBKEY del Worker):
  py -m faronet.herramientas.gateway --mostrar-llave
  py -m faronet.herramientas.gateway_nodo --nombre GW --puerto 9010 \\
        --vecino 127.0.0.1:9006 --directorio 127.0.0.1:9000 --edge http://localhost:8787
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

from faronet.herramientas.gateway import RUTA_LLAVE, cargar_llave, publicar, publicar_fichas
from faronet.mesh.directorio import consultar
from faronet.mesh.identidad import Identidad
from faronet.mesh.nodo import crear_parser, principal
from faronet.mesh.paquete import PaqueteInvalido

TEMA_RE = re.compile(r"^[a-z0-9_-]{1,16}$")   # igual que edge/src/index.ts
MAX_TEXTO = 300                               # igual que edge/src/index.ts


class Gateway:
    def __init__(self, nodo, edge: str, llave):
        self.nodo, self.edge, self.llave = nodo, edge, llave
        self.estado_sync = None   # para avisar solo cuando algo cambia

    async def publicar_mensaje(self, tema: str, texto: bytes):
        """Publica un mensaje que llego por la cebolla. Los invalidos se descartan."""
        mensaje = texto.decode("utf-8", errors="replace").strip()
        if not TEMA_RE.match(tema) or not mensaje or len(mensaje) > MAX_TEXTO:
            self.nodo.log("mensaje publico descartado: tema o texto invalidos")
            return
        codigo = await asyncio.to_thread(publicar, self.edge, self.llave, tema, mensaje)
        if codigo == 409:
            # Mismo mensaje en el mismo segundo: el Worker lo toma por repeticion. Se reintenta con otra marca de tiempo.
            await asyncio.sleep(1.1)
            codigo = await asyncio.to_thread(publicar, self.edge, self.llave, tema, mensaje)
        if codigo == 204:
            print(f"[{self.nodo.nombre}] publicado en #{tema}")
        else:
            print(f"[{self.nodo.nombre}] no se pudo publicar en #{tema} (HTTP {codigo})")

    async def sincronizar_directorio(self, cada: float):
        """Manda las fichas de la mesh al Worker, que las verifica y las replica."""
        while True:
            try:
                fichas, _ = await consultar(self.nodo.directorio)
                codigo, r = await asyncio.to_thread(
                    publicar_fichas, self.edge, self.llave, [f.codificar().hex() for f in fichas])
                estado = (f"ok: {r['aceptadas']} ficha(s)" if codigo == 200
                          else f"el Worker respondio HTTP {codigo}")
            except (OSError, asyncio.TimeoutError, PaqueteInvalido, asyncio.IncompleteReadError):
                estado = "sin conexion con el directorio de la mesh"
            if estado != self.estado_sync:   # solo avisar cuando cambia
                print(f"[{self.nodo.nombre}] directorio -> edge: {estado}")
                self.estado_sync = estado
            await asyncio.sleep(cada)


def leer_argumentos():
    parser = crear_parser("Nodo gateway de FaroNet (mesh -> edge)")
    g = parser.add_argument_group("Edge")
    g.add_argument("--edge", required=True, help="URL del Worker, ej. http://localhost:8787")
    g.add_argument("--llave-gateway", type=Path, default=RUTA_LLAVE,
                   help="llave de firma del gateway (por defecto ~/.faronet/gateway.llave)")
    g.add_argument("--sync-directorio", type=float, default=60.0,
                   help="segundos entre envios del directorio al edge (0 = no enviarlo)")
    return parser.parse_args()


def main() -> int:
    args = leer_argumentos()
    try:
        identidad = Identidad.cargar_o_crear(args.nombre, args.llaves)
        llave = cargar_llave(args.llave_gateway)
    except OSError as error:
        print(f"no se pudo leer ni crear un archivo de llaves: {error}")
        return 1

    def preparar(nodo):
        gateway = Gateway(nodo, args.edge, llave)
        loop = asyncio.get_running_loop()
        # La cebolla llama esta funcion (sincrona) al recibir un mensaje con tema.
        nodo.cebolla.al_recibir_publico = lambda tema, texto: loop.create_task(
            gateway.publicar_mensaje(tema, texto))
        if args.sync_directorio > 0 and nodo.directorio is not None:
            return [loop.create_task(gateway.sincronizar_directorio(args.sync_directorio))]
        return []

    print(f"[{args.nombre}] gateway hacia {args.edge}; GATEWAY_PUBKEY = {llave.verify_key.encode().hex()}")
    try:
        asyncio.run(principal(args, identidad, preparar))
    except KeyboardInterrupt:
        pass
    except OSError as error:
        print(f"no se pudo abrir el puerto {args.puerto}: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
