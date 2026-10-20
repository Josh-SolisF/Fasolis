"""
paquete.py - Formato comun de los paquetes de FaroNet (version 1).

Lo usan el nodo y el directorio. Todos los paquetes miden TAM_PAQUETE bytes:

   0        1        2                 6        8                     512
   +--------+--------+-----------------+--------+---------------------+
   |Version |  Tipo  |   Id circuito   |Longitud| Contenido + relleno |
   +--------+--------+-----------------+--------+---------------------+

"Longitud" indica cuantos bytes del contenido son reales; el resto es relleno
aleatorio. Como el tamano es fijo, sobre TCP basta con leer exactamente
TAM_PAQUETE bytes por paquete.

Los numeros de tipo estan todos aqui para que no se repitan entre programas.
"""
import asyncio
import os
import struct

VERSION = 1
TAM_PAQUETE = 512
ENCABEZADO = struct.Struct("!BBIH")  # version, tipo, circuito, longitud
MAX_CONTENIDO = TAM_PAQUETE - ENCABEZADO.size
MAX_NOMBRE = 16  # bytes por nombre de nodo

# Tipos de la mesh (entre nodos vecinos)
TIPO_HOLA = 1    # saludo inicial: nombre del nodo
TIPO_DATOS = 2   # mensaje enrutado hacia un destino
TIPO_RUTAS = 3   # anuncio del vector de distancias

# Tipos del directorio (entre un nodo y el servicio de directorio)
TIPO_DIR_PUBLICAR = 10   # nodo -> directorio: una ficha firmada
TIPO_DIR_CONSULTAR = 11  # nodo -> directorio: vacio = todas, o un nombre
TIPO_DIR_FICHA = 12      # directorio -> nodo: una ficha por paquete
TIPO_DIR_FIN = 13        # directorio -> nodo: no hay mas fichas
TIPO_DIR_RESPUESTA = 14  # directorio -> nodo: codigo(1) + texto

RESPUESTA_OK = 0
RESPUESTA_ERROR = 1


class PaqueteInvalido(Exception):
    """El paquete recibido no respeta el formato o la version."""


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


async def leer_paquete(reader: asyncio.StreamReader) -> tuple[int, int, bytes]:
    """Lee y valida el siguiente paquete de una conexion TCP."""
    return desarmar(await reader.readexactly(TAM_PAQUETE))
