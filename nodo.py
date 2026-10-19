#!/usr/bin/env python3
"""
nodo.py - Pasos 1 y 2 de FaroNet: nodo con paquetes de tamano fijo y enrutamiento.

Cada maquina (laptop o Raspberry) corre una copia de este programa. El nodo:
  - escucha conexiones de otros nodos en un puerto TCP,
  - se conecta a los vecinos que se le pasan por terminal (reintentando),
  - intercambia un saludo para saber el nombre de cada vecino,
  - aprende rutas hacia nodos lejanos con un vector de distancias (tipo RIP),
  - reenvia los paquetes que no son para el hacia el vecino mas cercano al destino.

Uso, en cuatro terminales de la misma laptop (cadena A - B - C - D):
  python3 nodo.py --nombre A --puerto 9001
  python3 nodo.py --nombre B --puerto 9002 --vecino 127.0.0.1:9001
  python3 nodo.py --nombre C --puerto 9003 --vecino 127.0.0.1:9002
  python3 nodo.py --nombre D --puerto 9004 --vecino 127.0.0.1:9003

En Windows se usa "py" en lugar de "python3", y conviene agregar
--escuchar 127.0.0.1 para que el firewall no pregunte en pruebas locales.

Comandos en la terminal:
  <destino> <mensaje>   envia un mensaje a cualquier nodo con ruta conocida
  rutas                 muestra la tabla de rutas
  vecinos               muestra los vecinos directos
  salir                 termina el nodo

Privacidad: no se imprimen ni se guardan direcciones IP; nada se escribe a
disco. El paquete de datos lleva el destino pero NO el origen: quien recibe
un mensaje no sabe de que nodo salio. Ocultar tambien el destino a los nodos
intermedios es tarea del enrutamiento por capas (paso siguiente).
"""
import argparse
import asyncio
import os
import struct
import sys
import threading
import time
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Formato del paquete de la mesh (version 1)
#
#   0        1        2                 6        8                     512
#   +--------+--------+-----------------+--------+---------------------+
#   |Version |  Tipo  |   Id circuito   |Longitud| Contenido + relleno |
#   +--------+--------+-----------------+--------+---------------------+
#
# Todos los paquetes miden TAM_PAQUETE bytes. "Longitud" indica cuantos bytes
# del contenido son reales; el resto es relleno aleatorio. Como el tamano es
# fijo, sobre TCP basta con leer exactamente TAM_PAQUETE bytes por paquete.
#
# Contenido segun el tipo:
#   HOLA   nombre del nodo que saluda
#   DATOS  TTL(1) | LongDestino(1) | Destino | Mensaje
#   RUTAS  repeticion de: LongNombre(1) | Nombre | Distancia(1)
# ---------------------------------------------------------------------------
VERSION = 1
TAM_PAQUETE = 512
ENCABEZADO = struct.Struct("!BBIH")  # version, tipo, circuito, longitud
MAX_CONTENIDO = TAM_PAQUETE - ENCABEZADO.size

TIPO_HOLA = 1   # saludo inicial
TIPO_DATOS = 2  # mensaje enrutado hacia un destino
TIPO_RUTAS = 3  # anuncio del vector de distancias

MAX_NOMBRE = 16           # caracteres por nombre de nodo
INFINITO = 16             # distancia que significa "inalcanzable" (como en RIP)
TTL_INICIAL = 16          # saltos maximos de un paquete de datos
INTERVALO_ANUNCIO = 5.0   # segundos entre anuncios periodicos de rutas
VIDA_RUTA = 3 * INTERVALO_ANUNCIO  # sin noticias en este tiempo, la ruta muere


class PaqueteInvalido(Exception):
    """El paquete recibido no respeta el formato o la version."""


# ------------------------------------------------------------ paquete basico
def armar(tipo: int, contenido: bytes, circuito: int = 0) -> bytes:
    """Construye un paquete de TAM_PAQUETE bytes con relleno aleatorio."""
    if len(contenido) > MAX_CONTENIDO:
        raise ValueError(f"contenido de {len(contenido)} bytes; maximo {MAX_CONTENIDO}")
    encabezado = ENCABEZADO.pack(VERSION, tipo, circuito, len(contenido))
    relleno = os.urandom(MAX_CONTENIDO - len(contenido))
    return encabezado + contenido + relleno


def desarmar(paquete: bytes) -> tuple[int, int, bytes]:
    """Valida un paquete y devuelve (tipo, circuito, contenido sin relleno)."""
    if len(paquete) != TAM_PAQUETE:
        raise PaqueteInvalido("tamano incorrecto")
    version, tipo, circuito, longitud = ENCABEZADO.unpack_from(paquete)
    if version != VERSION:
        raise PaqueteInvalido(f"version {version} no soportada")
    if longitud > MAX_CONTENIDO:
        raise PaqueteInvalido("longitud fuera de rango")
    inicio = ENCABEZADO.size
    return tipo, circuito, paquete[inicio:inicio + longitud]


# ------------------------------------------------------------- datos y rutas
def armar_datos(destino: str, mensaje: bytes, ttl: int = TTL_INICIAL) -> bytes:
    nombre = destino.encode()
    contenido = bytes([ttl, len(nombre)]) + nombre + mensaje
    return armar(TIPO_DATOS, contenido)


def leer_datos(contenido: bytes) -> tuple[int, str, bytes]:
    """Devuelve (ttl, destino, mensaje) de un paquete de datos."""
    if len(contenido) < 2:
        raise PaqueteInvalido("datos sin encabezado")
    ttl, n = contenido[0], contenido[1]
    if n == 0 or n > MAX_NOMBRE or len(contenido) < 2 + n:
        raise PaqueteInvalido("destino invalido")
    return ttl, contenido[2:2 + n].decode(errors="replace"), contenido[2 + n:]


def codificar_rutas(entradas: list[tuple[str, int]]) -> bytes:
    """Lista de (nombre, distancia) -> bytes. Corta si no cabe en un paquete."""
    salida = bytearray()
    for nombre, distancia in entradas:
        n = nombre.encode()
        entrada = bytes([len(n)]) + n + bytes([distancia])
        if len(salida) + len(entrada) > MAX_CONTENIDO:
            break
        salida += entrada
    return bytes(salida)


def decodificar_rutas(contenido: bytes) -> list[tuple[str, int]]:
    entradas, i = [], 0
    while i < len(contenido):
        n = contenido[i]
        if n == 0 or n > MAX_NOMBRE or i + 1 + n + 1 > len(contenido):
            raise PaqueteInvalido("anuncio de rutas mal formado")
        nombre = contenido[i + 1:i + 1 + n].decode(errors="replace")
        distancia = min(contenido[i + 1 + n], INFINITO)
        entradas.append((nombre, distancia))
        i += n + 2
    return entradas


@dataclass
class Ruta:
    distancia: int    # saltos hasta el destino (INFINITO = inalcanzable)
    siguiente: str    # vecino al que se le entrega el paquete
    actualizada: float  # ultima vez que se supo de esta ruta (time.monotonic)


# --------------------------------------------------------------------- nodo
class Nodo:
    def __init__(self, nombre: str, debug: bool = False):
        self.nombre = nombre
        self.debug = debug
        self.vecinos: dict[str, asyncio.StreamWriter] = {}
        self.tabla: dict[str, Ruta] = {}

    def log(self, texto: str):
        """Mensajes de depuracion: solo a la terminal y solo con --debug."""
        if self.debug:
            print(f"[{self.nombre}] (debug) {texto}")

    # -- conexiones ---------------------------------------------------------
    async def atender(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Maneja una conexion (entrante o saliente) durante toda su vida."""
        vecino = None
        try:
            writer.write(armar(TIPO_HOLA, self.nombre.encode()))
            await writer.drain()
            tipo, _, contenido = desarmar(await reader.readexactly(TAM_PAQUETE))
            if tipo != TIPO_HOLA:
                raise PaqueteInvalido("se esperaba un saludo")
            vecino = contenido.decode(errors="replace")
            if vecino == self.nombre or not 0 < len(vecino) <= MAX_NOMBRE:
                raise PaqueteInvalido("nombre de vecino invalido")
            self.vecinos[vecino] = writer
            print(f"[{self.nombre}] conectado con {vecino}")
            await self.vecino_conectado(vecino)

            while True:
                tipo, _, contenido = desarmar(await reader.readexactly(TAM_PAQUETE))
                try:
                    if tipo == TIPO_DATOS:
                        await self.recibir_datos(contenido)
                    elif tipo == TIPO_RUTAS:
                        await self.recibir_rutas(vecino, decodificar_rutas(contenido))
                except PaqueteInvalido as error:
                    print(f"[{self.nombre}] paquete descartado: {error}")
        except (asyncio.IncompleteReadError, ConnectionError):
            pass  # el vecino se desconecto
        except PaqueteInvalido as error:
            print(f"[{self.nombre}] conexion rechazada: {error}")
        finally:
            if vecino is not None and self.vecinos.get(vecino) is writer:
                del self.vecinos[vecino]
                print(f"[{self.nombre}] se desconecto {vecino}")
                await self.vecino_desconectado(vecino)
            writer.close()

    async def conectar(self, host: str, puerto: int):
        """Se conecta a un vecino, reintentando cada 2 s mientras no responda."""
        while True:
            try:
                reader, writer = await asyncio.open_connection(host, puerto)
            except OSError:
                await asyncio.sleep(2)
                continue
            await self.atender(reader, writer)
            await asyncio.sleep(2)  # si se cae la conexion, volver a intentar

    async def entregar(self, vecino: str, paquete: bytes) -> bool:
        """Escribe un paquete hacia un vecino directo."""
        writer = self.vecinos.get(vecino)
        if writer is None:
            return False
        try:
            writer.write(paquete)
            await writer.drain()
            return True
        except ConnectionError:
            return False

    # -- vector de distancias -------------------------------------------------
    def anuncio_para(self, vecino: str) -> bytes:
        """
        Tabla que se le anuncia a un vecino. "Envenenamiento en reversa": las
        rutas que pasan por ese mismo vecino se le anuncian como INFINITO, para
        que nunca intente llegar a un destino pasando de vuelta por nosotros.
        """
        entradas = [(self.nombre, 0)]
        for destino, ruta in self.tabla.items():
            distancia = INFINITO if ruta.siguiente == vecino else ruta.distancia
            entradas.append((destino, distancia))
        return armar(TIPO_RUTAS, codificar_rutas(entradas))

    async def anunciar(self):
        for vecino in list(self.vecinos):
            await self.entregar(vecino, self.anuncio_para(vecino))

    async def vecino_conectado(self, vecino: str):
        self.tabla[vecino] = Ruta(1, vecino, time.monotonic())
        await self.anunciar()

    async def vecino_desconectado(self, vecino: str):
        ahora = time.monotonic()
        for destino, ruta in self.tabla.items():
            if ruta.siguiente == vecino and ruta.distancia < INFINITO:
                ruta.distancia, ruta.actualizada = INFINITO, ahora
                self.log(f"ruta a {destino} perdida")
        await self.anunciar()

    async def recibir_rutas(self, vecino: str, entradas: list[tuple[str, int]]):
        """Aplica el algoritmo de Bellman-Ford con lo que anuncia un vecino."""
        ahora, cambio = time.monotonic(), False
        for destino, distancia in entradas:
            if destino == self.nombre:
                continue
            nueva = min(distancia + 1, INFINITO)
            actual = self.tabla.get(destino)
            if actual is not None and actual.siguiente == vecino:
                # Noticia de nuestro propio camino: se acepta aunque empeore.
                if nueva != actual.distancia:
                    cambio = True
                    self.log(f"ruta a {destino}: {actual.distancia} -> {nueva} via {vecino}")
                actual.distancia = nueva
                if nueva < INFINITO:
                    actual.actualizada = ahora
            elif nueva < INFINITO and (actual is None or nueva < actual.distancia):
                self.tabla[destino] = Ruta(nueva, vecino, ahora)
                cambio = True
                self.log(f"ruta nueva a {destino}: {nueva} saltos via {vecino}")
        if cambio:
            await self.anunciar()  # actualizacion inmediata, sin esperar el periodo

    async def mantenimiento(self):
        """Anuncia periodicamente y envejece las rutas sin noticias."""
        while True:
            await asyncio.sleep(INTERVALO_ANUNCIO)
            ahora = time.monotonic()
            for destino in list(self.tabla):
                ruta = self.tabla[destino]
                if ahora - ruta.actualizada > VIDA_RUTA:
                    if ruta.distancia < INFINITO:
                        ruta.distancia, ruta.actualizada = INFINITO, ahora
                        self.log(f"ruta a {destino} expiro")
                    else:
                        del self.tabla[destino]
            await self.anunciar()

    # -- datos ----------------------------------------------------------------
    async def recibir_datos(self, contenido: bytes):
        ttl, destino, mensaje = leer_datos(contenido)
        if destino == self.nombre:
            print(f"[{self.nombre}] mensaje recibido: {mensaje.decode(errors='replace')}")
            return
        if ttl <= 1:
            self.log(f"paquete hacia {destino} descartado: TTL agotado")
            return
        ruta = self.tabla.get(destino)
        if ruta is None or ruta.distancia >= INFINITO:
            self.log(f"paquete hacia {destino} descartado: sin ruta")
            return
        self.log(f"reenviando paquete hacia {destino} via {ruta.siguiente}")
        await self.entregar(ruta.siguiente, armar_datos(destino, mensaje, ttl - 1))

    async def enviar(self, destino: str, texto: str):
        if destino == self.nombre:
            print(f"[{self.nombre}] ese es este mismo nodo")
            return
        ruta = self.tabla.get(destino)
        if ruta is None or ruta.distancia >= INFINITO:
            print(f"[{self.nombre}] no hay ruta hacia {destino}")
            return
        try:
            paquete = armar_datos(destino, texto.encode())
        except ValueError as error:
            print(f"[{self.nombre}] no se envio: {error}")
            return
        if not await self.entregar(ruta.siguiente, paquete):
            print(f"[{self.nombre}] fallo el envio hacia {destino}")

    # -- terminal ---------------------------------------------------------------
    def mostrar_rutas(self):
        if not self.tabla:
            print(f"[{self.nombre}] tabla de rutas vacia")
            return
        print(f"[{self.nombre}] tabla de rutas:")
        for destino in sorted(self.tabla):
            ruta = self.tabla[destino]
            if ruta.distancia >= INFINITO:
                print(f"    {destino}: inalcanzable")
            else:
                print(f"    {destino}: {ruta.distancia} salto(s) via {ruta.siguiente}")

    async def leer_terminal(self):
        """
        Lee comandos de la terminal sin bloquear la red.

        La lectura ocurre en un hilo "daemon": si se presiona Ctrl+C mientras
        el hilo espera una linea, el programa puede terminar sin esperar a que
        alguien presione Enter.
        """
        loop = asyncio.get_running_loop()
        cola: asyncio.Queue[str] = asyncio.Queue()

        def lector():
            try:
                for linea in sys.stdin:
                    loop.call_soon_threadsafe(cola.put_nowait, linea)
                loop.call_soon_threadsafe(cola.put_nowait, "")  # fin de la entrada
            except RuntimeError:
                pass  # el programa ya esta cerrando

        threading.Thread(target=lector, daemon=True).start()
        while True:
            linea = await cola.get()
            if linea == "":  # fin de la entrada
                return
            linea = linea.strip()
            if linea == "salir":
                return
            if linea == "vecinos":
                print(f"[{self.nombre}] vecinos: {', '.join(sorted(self.vecinos)) or 'ninguno'}")
            elif linea == "rutas":
                self.mostrar_rutas()
            elif " " in linea:
                destino, texto = linea.split(" ", 1)
                await self.enviar(destino, texto)
            elif linea:
                print("uso: <destino> <mensaje> | rutas | vecinos | salir")


# ------------------------------------------------------------------- arranque
def direccion(texto: str) -> tuple[str, int]:
    """Convierte 'host:puerto' en una tupla; lo usa argparse."""
    host, _, puerto = texto.rpartition(":")
    if not host or not puerto.isdigit():
        raise argparse.ArgumentTypeError("formato esperado host:puerto")
    return host, int(puerto)


def nombre_valido(texto: str) -> str:
    if not 0 < len(texto.encode()) <= MAX_NOMBRE or " " in texto:
        raise argparse.ArgumentTypeError(f"de 1 a {MAX_NOMBRE} caracteres, sin espacios")
    return texto


async def principal(args):
    nodo = Nodo(args.nombre, debug=args.debug)
    servidor = await asyncio.start_server(nodo.atender, args.escuchar, args.puerto)
    print(f"[{nodo.nombre}] escuchando en el puerto {args.puerto}")
    tareas = [asyncio.create_task(nodo.conectar(h, p)) for h, p in args.vecino]
    tareas.append(asyncio.create_task(nodo.mantenimiento()))
    try:
        await nodo.leer_terminal()
    finally:
        for tarea in tareas:
            tarea.cancel()
        servidor.close()


def main():
    parser = argparse.ArgumentParser(description="Nodo de FaroNet (pasos 1 y 2)")
    parser.add_argument("--nombre", required=True, type=nombre_valido,
                        help="nombre de este nodo, p. ej. A")
    parser.add_argument("--puerto", type=int, required=True, help="puerto TCP donde escuchar")
    parser.add_argument("--escuchar", default="0.0.0.0",
                        help="interfaz donde escuchar (0.0.0.0 = toda la red local)")
    parser.add_argument("--vecino", type=direccion, action="append", default=[],
                        help="vecino al que conectarse, host:puerto (se puede repetir)")
    parser.add_argument("--debug", action="store_true",
                        help="mostrar en la terminal los cambios de rutas y los reenvios")
    args = parser.parse_args()
    try:
        asyncio.run(principal(args))
    except KeyboardInterrupt:
        pass  # Ctrl+C termina limpio, sin traza de error
    except OSError as error:
        print(f"no se pudo abrir el puerto {args.puerto}: {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()