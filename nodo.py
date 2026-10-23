#!/usr/bin/env python3
"""
nodo.py - Pasos 1 a 3 de FaroNet: nodo con enrutamiento y directorio firmado.

Cada maquina (laptop o Raspberry) corre una copia de este programa. El nodo:
  - escucha conexiones de otros nodos en un puerto TCP,
  - se conecta a los vecinos que se le pasan por terminal (reintentando),
  - intercambia un saludo para saber el nombre de cada vecino,
  - aprende rutas hacia nodos lejanos con un vector de distancias (tipo RIP),
  - reenvia los paquetes que no son para el hacia el vecino mas cercano al destino,
  - si se le indica un directorio, publica alli su ficha firmada cada minuto.

Uso, en cinco terminales de la misma laptop (directorio + cadena A - B - C - D):
  python3 directorio.py --puerto 9000
  python3 nodo.py --nombre A --puerto 9001 --directorio 127.0.0.1:9000
  python3 nodo.py --nombre B --puerto 9002 --vecino 127.0.0.1:9001 --directorio 127.0.0.1:9000
  python3 nodo.py --nombre C --puerto 9003 --vecino 127.0.0.1:9002 --directorio 127.0.0.1:9000
  python3 nodo.py --nombre D --puerto 9004 --vecino 127.0.0.1:9003 --directorio 127.0.0.1:9000

En Windows se usa "py" en lugar de "python3", y conviene agregar
--escuchar 127.0.0.1 para que el firewall no pregunte en pruebas locales.

Comandos en la terminal:
  <destino> <mensaje>   envia un mensaje a cualquier nodo con ruta conocida
  anonimo <destino> <mensaje>
                        lo envia por 3 relays con cifrado por capas (cebolla.py);
                        necesita --directorio y al menos 5 nodos en la mesh
  rutas                 muestra la tabla de rutas
  directorio            consulta el directorio y verifica las firmas
  vecinos               muestra los vecinos directos
  salir                 termina el nodo

Privacidad: no se imprimen ni se guardan direcciones IP. Lo unico que se
escribe a disco son las llaves del nodo (ver identidad.py). El paquete de datos lleva el destino pero NO el origen: quien recibe
un mensaje no sabe de que nodo salio. Ocultar tambien el destino a los nodos
intermedios lo hace el enrutamiento por capas (cebolla.py, comando 'anonimo').
"""
from __future__ import annotations

import argparse
import asyncio
import random
import sys
import threading
import time
from dataclasses import dataclass

from cebolla import Cebolla, CebollaError
from directorio import consultar, publicar_ficha
from identidad import Ficha, Identidad
from paquete import (MAX_CONTENIDO, MAX_NOMBRE, TAM_PAQUETE, TIPO_CEBOLLA, TIPO_DATOS,
                     TIPO_HOLA, TIPO_MENSAJE, TIPO_RUTAS, PaqueteInvalido, armar, desarmar)

# ---------------------------------------------------------------------------
# El formato base del paquete (512 bytes, encabezado y relleno) esta en
# paquete.py. Contenido segun el tipo:
#   HOLA   Bandera(1) | Nombre      (bandera 1 = "aun no te escucho, contestame")
#   DATOS  TTL(1) | LongDestino(1) | Destino | Mensaje
#   RUTAS  repeticion de: LongNombre(1) | Nombre | Distancia(1)
#   CEBOLLA y MENSAJE (enrutados, ver cebolla.py):
#          TTL(1) | Destino(16, relleno de ceros) | Carga
#          A diferencia de DATOS, el destino mide siempre 16 bytes y la carga
#          siempre lo mismo, para que el largo del paquete no filtre nada.
# ---------------------------------------------------------------------------
INFINITO = 16             # distancia que significa "inalcanzable" (como en RIP)
TTL_INICIAL = 16          # saltos maximos de un paquete de datos
INTERVALO_ANUNCIO = 5.0   # segundos entre anuncios de rutas (--intervalo-rutas)
REENVIO_HOLA = 5.0        # si el vecino no saluda, repetir el saludo cada tanto
HOLA_PIDE_RESPUESTA = 1   # saludo de quien todavia no escucha al otro
HOLA_RESPUESTA = 0        # saludo de respuesta: no se contesta (evita ping-pong)
INTERVALO_PUBLICACION = 60.0  # segundos entre publicaciones de la ficha
REINTENTO_PUBLICACION = 5.0   # si el directorio no responde, reintentar antes


# ------------------------------------------------------------- datos y rutas
def armar_hola(nombre: str, pide_respuesta: bool) -> bytes:
    bandera = HOLA_PIDE_RESPUESTA if pide_respuesta else HOLA_RESPUESTA
    return armar(TIPO_HOLA, bytes([bandera]) + nombre.encode())


def leer_hola(contenido: bytes) -> tuple[bool, str]:
    """Devuelve (pide_respuesta, nombre del vecino)."""
    if len(contenido) < 2:
        raise PaqueteInvalido("saludo vacio")
    nombre = contenido[1:].decode(errors="replace")
    if not 0 < len(contenido) - 1 <= MAX_NOMBRE:
        raise PaqueteInvalido("nombre de vecino invalido")
    return contenido[0] == HOLA_PIDE_RESPUESTA, nombre


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


def armar_enrutado(tipo: int, destino: str, carga: bytes, ttl: int = TTL_INICIAL) -> bytes:
    """Paquete enrutado hacia 'destino' (CEBOLLA o MENSAJE), con destino de largo fijo."""
    nombre = destino.encode()
    if not 0 < len(nombre) <= MAX_NOMBRE:
        raise PaqueteInvalido("destino invalido")
    return armar(tipo, bytes([ttl]) + nombre.ljust(MAX_NOMBRE, b"\0") + carga)


def leer_enrutado(contenido: bytes) -> tuple[int, str, bytes]:
    """Devuelve (ttl, destino, carga) de un paquete CEBOLLA o MENSAJE."""
    if len(contenido) < 1 + MAX_NOMBRE:
        raise PaqueteInvalido("paquete enrutado sin encabezado")
    destino = contenido[1:1 + MAX_NOMBRE].rstrip(b"\0").decode(errors="replace")
    if not destino:
        raise PaqueteInvalido("destino vacio")
    return contenido[0], destino, contenido[1 + MAX_NOMBRE:]


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
    def __init__(self, identidad: Identidad, directorio: tuple[str, int] | None = None,
                 debug: bool = False, intervalo: float = INTERVALO_ANUNCIO):
        self.identidad = identidad
        self.nombre = identidad.nombre
        self.directorio = directorio
        self.debug = debug
        self.intervalo = intervalo        # segundos entre anuncios de rutas
        self.vida_ruta = 3 * intervalo    # sin noticias en este tiempo, la ruta muere
        self.detener = asyncio.Event()    # otro componente (p. ej. el boton de panico) pide salir
        # Comandos de terminal que agregan otros programas: nombre -> (ayuda, funcion)
        self.comandos: dict[str, tuple[str, object]] = {}
        self.vecinos: dict[str, asyncio.StreamWriter] = {}
        self.tabla: dict[str, Ruta] = {}
        self.conocidos: dict[str, Ficha] = {}  # fichas verificadas del directorio
        self.cebolla = Cebolla(self)           # relay y emisor anonimo (cebolla.py)

    def log(self, texto: str):
        """Mensajes de depuracion: solo a la terminal y solo con --debug."""
        if self.debug:
            print(f"[{self.nombre}] (debug) {texto}")

    # -- conexiones ---------------------------------------------------------
    async def atender(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """
        Maneja una conexion durante toda su vida. Sirve igual para TCP y para
        el Faro (enlace_faro.py imita un reader/writer de asyncio).

        Saludo: se envia un HOLA que pide respuesta y se repite cada
        'reenvio_hola' segundos hasta recibir el HOLA del vecino. En TCP nunca
        se pierde, pero por luz si: si el otro Faro arranco despues, nuestro
        primer saludo se perdio y el suyo nos llega cuando ya estamos
        conectados; por eso un HOLA que pide respuesta siempre se contesta.
        """
        vecino = None
        reenvio = getattr(writer, "reenvio_hola", REENVIO_HOLA)
        try:
            writer.write(armar_hola(self.nombre, True))
            await writer.drain()
            while vecino is None:
                try:
                    # Variacion aleatoria: dos nodos que arrancan juntos no deben
                    # repetir el saludo siempre al mismo tiempo (por luz chocarian).
                    paquete = await asyncio.wait_for(reader.readexactly(TAM_PAQUETE),
                                                     reenvio * random.uniform(0.7, 1.3))
                except asyncio.TimeoutError:
                    writer.write(armar_hola(self.nombre, True))
                    await writer.drain()
                    continue
                tipo, _, contenido = desarmar(paquete)
                if tipo != TIPO_HOLA:
                    continue  # trafico previo al saludo: se ignora
                pide_respuesta, vecino = leer_hola(contenido)
                if vecino == self.nombre:
                    raise PaqueteInvalido("el vecino usa el mismo nombre que este nodo")
                if pide_respuesta:
                    writer.write(armar_hola(self.nombre, False))
                    await writer.drain()
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
                    elif tipo in (TIPO_CEBOLLA, TIPO_MENSAJE):
                        await self.recibir_enrutado(tipo, contenido)
                    elif tipo == TIPO_HOLA and leer_hola(contenido)[0]:
                        # El vecino no recibio nuestro saludo: se lo repetimos.
                        writer.write(armar_hola(self.nombre, False))
                        await writer.drain()
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
            await asyncio.sleep(self.intervalo)
            ahora = time.monotonic()
            for destino in list(self.tabla):
                ruta = self.tabla[destino]
                if ahora - ruta.actualizada > self.vida_ruta:
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

    def ruta_a(self, destino: str) -> Ruta | None:
        """La ruta vigente hacia un nodo, o None si no hay."""
        ruta = self.tabla.get(destino)
        return ruta if ruta is not None and ruta.distancia < INFINITO else None

    async def enviar_enrutado(self, tipo: int, destino: str, carga: bytes, ttl: int = TTL_INICIAL):
        """
        Manda una carga hacia 'destino' por la mesh. Si el destino es este mismo
        nodo, se procesa aqui. Si no hay ruta, se descarta en silencio.
        """
        if destino == self.nombre:
            await self.cebolla.recibir(tipo, carga)
            return
        ruta = self.ruta_a(destino)
        if ruta is None:
            self.log(f"paquete hacia {destino} descartado: sin ruta")
            return
        await self.entregar(ruta.siguiente, armar_enrutado(tipo, destino, carga, ttl))

    async def recibir_enrutado(self, tipo: int, contenido: bytes):
        """Un paquete CEBOLLA o MENSAJE: es para este nodo o hay que reenviarlo."""
        ttl, destino, carga = leer_enrutado(contenido)
        if destino == self.nombre:
            await self.cebolla.recibir(tipo, carga)
        elif ttl <= 1:
            self.log(f"paquete hacia {destino} descartado: TTL agotado")
        else:
            await self.enviar_enrutado(tipo, destino, carga, ttl - 1)

    async def enviar_anonimo(self, destino: str, texto: str):
        try:
            await self.cebolla.enviar_anonimo(destino, texto)
        except (CebollaError, PaqueteInvalido) as error:
            print(f"[{self.nombre}] no se envio: {error}")

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

    # -- directorio -------------------------------------------------------------
    async def publicar_periodicamente(self):
        """
        Publica la ficha al arrancar y luego cada INTERVALO_PUBLICACION. Si el
        directorio no responde (por ejemplo, aun no arranca), reintenta cada
        REINTENTO_PUBLICACION segundos.
        """
        estado_anterior = None
        while True:
            try:
                aceptada, texto = await publicar_ficha(self.directorio, self.identidad.ficha())
                estado = "ok" if aceptada else f"rechazada: {texto}"
            except (OSError, asyncio.TimeoutError, PaqueteInvalido, asyncio.IncompleteReadError):
                estado = "sin conexion con el directorio"
            if estado != estado_anterior:  # solo avisar cuando algo cambia
                if estado == "ok":
                    print(f"[{self.nombre}] ficha publicada en el directorio")
                else:
                    print(f"[{self.nombre}] ficha no publicada ({estado})")
                estado_anterior = estado
            sin_conexion = estado == "sin conexion con el directorio"
            await asyncio.sleep(REINTENTO_PUBLICACION if sin_conexion else INTERVALO_PUBLICACION)

    async def consultar_directorio(self):
        if self.directorio is None:
            print(f"[{self.nombre}] no se indico --directorio")
            return
        try:
            fichas, descartadas = await consultar(self.directorio)
        except (OSError, asyncio.TimeoutError, PaqueteInvalido, asyncio.IncompleteReadError):
            print(f"[{self.nombre}] no se pudo consultar el directorio")
            return
        self.conocidos = {f.nombre: f for f in fichas}
        print(f"[{self.nombre}] directorio: {len(fichas)} ficha(s) con firma valida")
        ahora = time.time()
        for f in sorted(fichas, key=lambda f: f.nombre):
            marca = "  <- este nodo" if f.nombre == self.nombre else ""
            print(f"    {f.nombre:<{MAX_NOMBRE}} huella {f.huella()}  "
                  f"vence en {int(f.expira - ahora)} s{marca}")
        if descartadas:
            print(f"[{self.nombre}] ATENCION: {descartadas} ficha(s) con firma invalida descartadas")

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
            elif linea == "directorio":
                await self.consultar_directorio()
            elif linea.startswith("anonimo "):
                partes = linea.split(" ", 2)
                if len(partes) == 3:
                    await self.enviar_anonimo(partes[1], partes[2])
                else:
                    print("uso: anonimo <destino> <mensaje>")
            elif linea in self.comandos:
                resultado = self.comandos[linea][1]()
                if asyncio.iscoroutine(resultado):
                    await resultado
            elif " " in linea:
                destino, texto = linea.split(" ", 1)
                await self.enviar(destino, texto)
            elif linea:
                extra = "".join(f" | {c}" for c in self.comandos)
                print(f"uso: <destino> <mensaje> | anonimo <destino> <mensaje> | rutas | directorio | vecinos{extra} | salir")


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


async def principal(args, identidad: Identidad, preparar=None) -> "Nodo":
    """
    Arranca el nodo y espera hasta "salir", fin de la entrada o nodo.detener.
    'preparar(nodo)' permite que otro programa (faro_nodo.py) agregue tareas
    propias, como el enlace por luz o el boton de panico.
    """
    nodo = Nodo(identidad, args.directorio, debug=args.debug, intervalo=args.intervalo_rutas)
    servidor = await asyncio.start_server(nodo.atender, args.escuchar, args.puerto)
    print(f"[{nodo.nombre}] escuchando en el puerto {args.puerto}, "
          f"huella {identidad.ficha().huella()}")
    tareas = [asyncio.create_task(nodo.conectar(h, p)) for h, p in args.vecino]
    tareas.append(asyncio.create_task(nodo.mantenimiento()))
    if args.directorio is not None:
        tareas.append(asyncio.create_task(nodo.publicar_periodicamente()))
    if preparar is not None:
        tareas += preparar(nodo)
    terminal = asyncio.create_task(nodo.leer_terminal())
    fin = asyncio.create_task(nodo.detener.wait())
    try:
        await asyncio.wait({terminal, fin}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for tarea in tareas + [terminal, fin]:
            tarea.cancel()
        servidor.close()
    return nodo


def crear_parser(descripcion: str = "Nodo de FaroNet") -> argparse.ArgumentParser:
    """Argumentos del nodo; faro_nodo.py los reutiliza y agrega los del Faro."""
    parser = argparse.ArgumentParser(description=descripcion)
    parser.add_argument("--nombre", required=True, type=nombre_valido,
                        help="nombre de este nodo, p. ej. A")
    parser.add_argument("--puerto", type=int, required=True, help="puerto TCP donde escuchar")
    parser.add_argument("--escuchar", default="0.0.0.0",
                        help="interfaz donde escuchar (0.0.0.0 = toda la red local)")
    parser.add_argument("--vecino", type=direccion, action="append", default=[],
                        help="vecino al que conectarse, host:puerto (se puede repetir)")
    parser.add_argument("--directorio", type=direccion, default=None,
                        help="directorio donde publicar la ficha, host:puerto")
    parser.add_argument("--llaves", default=None,
                        help="archivo de llaves (por defecto ~/.faronet/<nombre>.llaves)")
    parser.add_argument("--intervalo-rutas", type=float, default=INTERVALO_ANUNCIO,
                        help=f"segundos entre anuncios de rutas (por defecto {INTERVALO_ANUNCIO:g}); "
                             "por luz conviene uno mayor")
    parser.add_argument("--debug", action="store_true",
                        help="mostrar en la terminal los cambios de rutas y los reenvios")
    return parser


def main():
    args = crear_parser("Nodo de FaroNet (pasos 1 a 3)").parse_args()
    # Las llaves se cargan aparte, para que un error de archivo no se
    # confunda con un error del puerto.
    try:
        identidad = Identidad.cargar_o_crear(args.nombre, args.llaves)
    except OSError as error:
        print(f"no se pudo leer ni crear el archivo de llaves: {error}")
        sys.exit(1)
    try:
        asyncio.run(principal(args, identidad))
    except KeyboardInterrupt:
        pass  # Ctrl+C termina limpio, sin traza de error
    except OSError as error:
        print(f"no se pudo abrir el puerto {args.puerto}: {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()