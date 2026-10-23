#!/usr/bin/env python3
"""
directorio.py - Directorio de nodos de FaroNet.

Servicio donde los nodos publican su ficha firmada (nombre + llaves publicas)
y donde cualquiera puede consultarlas. Reglas al publicar:
  - la firma debe ser valida y la ficha no puede estar vencida;
  - si el nombre ya esta registrado con OTRA llave de firma y esa ficha no ha
    vencido, se rechaza (asi nadie puede hacerse pasar por otro nodo);
  - una ficha mas vieja que la registrada se rechaza (evita repetir fichas
    viejas capturadas).

Los nodos no confian en el directorio: al consultar, verifican cada firma.

Privacidad: las fichas solo viven en memoria y se borran al vencer. No se
guardan ni se imprimen direcciones IP; con --debug solo se muestran nombres.

Uso:
  python3 directorio.py --puerto 9000
Comandos en la terminal: fichas | salir
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import threading
import time

from faronet.mesh.identidad import Ficha, FichaInvalida
from faronet.mesh.paquete import (MAX_NOMBRE, RESPUESTA_ERROR, RESPUESTA_OK, TIPO_DIR_CONSULTAR,
                     TIPO_DIR_FICHA, TIPO_DIR_FIN, TIPO_DIR_PUBLICAR, TIPO_DIR_RESPUESTA,
                     PaqueteInvalido, armar, leer_paquete)

TIEMPO_ESPERA = 5.0  # segundos maximos para una operacion con el directorio
INTERVALO_LIMPIEZA = 30.0


# ------------------------------------------------------------------- servidor
class Directorio:
    def __init__(self, debug: bool = False):
        self.debug = debug
        self.fichas: dict[str, Ficha] = {}

    def log(self, texto: str):
        if self.debug:
            print(f"[directorio] (debug) {texto}")

    def publicar(self, ficha: Ficha) -> str:
        """Valida y guarda una ficha. Devuelve el motivo si la rechaza."""
        ficha.verificar()
        actual = self.fichas.get(ficha.nombre)
        if actual is not None and actual.expira > time.time():
            if actual.llave_firma != ficha.llave_firma:
                raise FichaInvalida("el nombre ya esta registrado con otra llave")
            if ficha.expira < actual.expira:
                raise FichaInvalida("ficha mas vieja que la registrada")
        self.fichas[ficha.nombre] = ficha
        return "ficha aceptada"

    async def atender(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            while True:
                tipo, _, contenido = await leer_paquete(reader)
                if tipo == TIPO_DIR_PUBLICAR:
                    try:
                        ficha = Ficha.decodificar(contenido)
                        texto = self.publicar(ficha)
                        self.log(f"ficha de {ficha.nombre} aceptada")
                        codigo = RESPUESTA_OK
                    except FichaInvalida as error:
                        texto, codigo = str(error), RESPUESTA_ERROR
                        self.log(f"ficha rechazada: {error}")
                    writer.write(armar(TIPO_DIR_RESPUESTA, bytes([codigo]) + texto.encode()))
                elif tipo == TIPO_DIR_CONSULTAR:
                    nombre = contenido.decode(errors="replace")
                    ahora = time.time()
                    for ficha in list(self.fichas.values()):
                        if ficha.expira > ahora and (not nombre or ficha.nombre == nombre):
                            writer.write(armar(TIPO_DIR_FICHA, ficha.codificar()))
                    writer.write(armar(TIPO_DIR_FIN, b""))
                else:
                    raise PaqueteInvalido(f"tipo {tipo} no esperado")
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except PaqueteInvalido as error:
            self.log(f"conexion cerrada: {error}")
        finally:
            writer.close()

    async def limpiar(self):
        """Borra las fichas vencidas: nada se guarda mas alla de su TTL."""
        while True:
            await asyncio.sleep(INTERVALO_LIMPIEZA)
            ahora = time.time()
            for nombre in [n for n, f in self.fichas.items() if f.expira <= ahora]:
                del self.fichas[nombre]
                self.log(f"ficha de {nombre} vencida y borrada")

    def mostrar(self):
        ahora = time.time()
        vigentes = [f for f in self.fichas.values() if f.expira > ahora]
        if not vigentes:
            print("[directorio] sin fichas vigentes")
        for f in sorted(vigentes, key=lambda f: f.nombre):
            print(f"    {f.nombre:<{MAX_NOMBRE}} huella {f.huella()}  vence en {int(f.expira - ahora)} s")


# -------------------------------------------------------- cliente (para nodos)
async def _conversar(direccion: tuple[str, int], peticion: bytes, hasta_fin: bool):
    """Abre una conexion, manda una peticion y devuelve los paquetes de respuesta."""
    reader, writer = await asyncio.open_connection(*direccion)
    try:
        writer.write(peticion)
        await writer.drain()
        respuestas = []
        while True:
            tipo, _, contenido = await leer_paquete(reader)
            respuestas.append((tipo, contenido))
            if not hasta_fin or tipo == TIPO_DIR_FIN:
                return respuestas
    finally:
        writer.close()


async def publicar_ficha(direccion: tuple[str, int], ficha: Ficha) -> tuple[bool, str]:
    """Publica una ficha. Devuelve (aceptada, texto de la respuesta)."""
    peticion = armar(TIPO_DIR_PUBLICAR, ficha.codificar())
    respuestas = await asyncio.wait_for(_conversar(direccion, peticion, False), TIEMPO_ESPERA)
    tipo, contenido = respuestas[0]
    if tipo != TIPO_DIR_RESPUESTA or not contenido:
        raise PaqueteInvalido("respuesta inesperada del directorio")
    return contenido[0] == RESPUESTA_OK, contenido[1:].decode(errors="replace")


async def consultar(direccion: tuple[str, int], nombre: str = "") -> tuple[list[Ficha], int]:
    """
    Pide fichas al directorio y verifica cada firma por cuenta propia.
    Devuelve (fichas validas, cantidad descartadas).
    """
    peticion = armar(TIPO_DIR_CONSULTAR, nombre.encode())
    respuestas = await asyncio.wait_for(_conversar(direccion, peticion, True), TIEMPO_ESPERA)
    validas, descartadas = [], 0
    for tipo, contenido in respuestas:
        if tipo != TIPO_DIR_FICHA:
            continue
        try:
            ficha = Ficha.decodificar(contenido)
            ficha.verificar()
            validas.append(ficha)
        except FichaInvalida:
            descartadas += 1
    return validas, descartadas


# ------------------------------------------------------------------- arranque
async def leer_terminal(directorio: Directorio):
    loop = asyncio.get_running_loop()
    cola: asyncio.Queue[str] = asyncio.Queue()

    def lector():
        try:
            for linea in sys.stdin:
                loop.call_soon_threadsafe(cola.put_nowait, linea)
            loop.call_soon_threadsafe(cola.put_nowait, "")
        except RuntimeError:
            pass

    threading.Thread(target=lector, daemon=True).start()
    while True:
        linea = (await cola.get())
        if linea == "" or linea.strip() == "salir":
            return
        if linea.strip() == "fichas":
            directorio.mostrar()
        elif linea.strip():
            print("uso: fichas | salir")


async def principal(args):
    directorio = Directorio(debug=args.debug)
    servidor = await asyncio.start_server(directorio.atender, args.escuchar, args.puerto)
    print(f"[directorio] escuchando en el puerto {args.puerto}")
    limpieza = asyncio.create_task(directorio.limpiar())
    try:
        await leer_terminal(directorio)
    finally:
        limpieza.cancel()
        servidor.close()


def main():
    parser = argparse.ArgumentParser(description="Directorio de nodos de FaroNet")
    parser.add_argument("--puerto", type=int, default=9000, help="puerto TCP (por defecto 9000)")
    parser.add_argument("--escuchar", default="0.0.0.0",
                        help="interfaz donde escuchar (0.0.0.0 = toda la red local)")
    parser.add_argument("--debug", action="store_true", help="mostrar fichas aceptadas y rechazadas")
    args = parser.parse_args()
    try:
        asyncio.run(principal(args))
    except KeyboardInterrupt:
        pass
    except OSError as error:
        print(f"no se pudo abrir el puerto {args.puerto}: {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
