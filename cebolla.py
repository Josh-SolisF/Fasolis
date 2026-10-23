"""
cebolla.py - Enrutamiento por capas ("cebolla") de FaroNet, version 1.

Objetivo: que ningun nodo intermedio conozca a la vez el origen y el destino
de un mensaje. El cliente elige SALTOS relays del directorio y envuelve el
mensaje en una capa de cifrado por relay. Cada relay quita SU capa, descubre
solo el nombre del siguiente relay y reenvia. Nadie mas ve nada.

  cliente --> R1 --> R2 --> R3 --> destino
              ve:    ve:    ve:    ve:
              origen* solo   destino solo el mensaje
              y R2   R1,R3   y mensaje sellado, sin
                            sellado  origen
  (*) R1 recibe de un vecino de la mesh; el paquete no trae origen (ver nodo.py).

Esquema de una sola via: no hay circuito que negociar ni camino de regreso, asi
que cada mensaje es autocontenido (como un paquete Sphinx simplificado).

Criptografia (solo PyNaCl, ninguna primitiva propia):
  - Por cada relay el cliente crea una llave X25519 EFIMERA. La llave de esa
    capa sale de DH(efimera, llave de cifrado del relay), tomada de su ficha del
    directorio. Solo el relay (dueno de la llave privada) puede calcularla.
  - Flujo de cada capa: keystream de SecretBox (XSalsa20) sobre ceros, con una
    llave distinta por mensaje y por relay. Un cifrado de flujo conserva el
    tamano, asi que la celda mide siempre lo mismo en todo el recorrido.
  - Ultimo relay: reconoce que la celda es para el con un MAC corto (BLAKE2b).
  - Mensaje: SealedBox hacia la llave publica del destinatario (extremo a extremo).
  - Repeticion: cada relay recuerda (en memoria, con TTL) las llaves efimeras
    ya vistas y descarta las repetidas.

Formato de la celda (cuerpo de TIPO_CEBOLLA, TAM_CUERPO bytes, fijo):
    Efimera(32) | Bloque(R)
  Bloque tras quitar la capa de un relay intermedio:
    Siguiente(16, nombre rellenado con ceros) | EfimeraSig(32) | Resto
  El relay arma la celda siguiente con EfimeraSig | Resto | 48 bytes aleatorios,
  para que mida lo mismo. Bloque tras quitar la capa del ultimo relay:
    MAC(8) | Destino(16) | Sellado(TAM_SELLADO)        (LARGO_FINAL bytes)

Mensaje de salida (TIPO_MENSAJE) hacia el destino: solo el sellado, de tamano fijo.

Limitaciones conocidas (para el RFC / modelo de amenazas):
  - Sin secreto hacia adelante respecto a la llave de cifrado de larga vida del
    relay: si esa llave se filtra, se pueden descifrar celdas capturadas antes.
    Mitigacion: el boton de panico la borra; para tenerlo hay que agregar un
    camino de regreso (handshake tipo ntor) o rotar las llaves de cifrado.
  - Los relays intermedios no autentican la celda (solo el ultimo, con el MAC):
    dos relays coludidos (primero y ultimo) pueden marcar una celda y
    correlacionarla (tagging attack).
  - No hay mezcla ni retrasos: un observador global de trafico puede
    correlacionar por tiempos. Se documenta como fuera del alcance.
"""
from __future__ import annotations

import asyncio
import os
import random
import time

from nacl.encoding import RawEncoder
from nacl.exceptions import CryptoError
from nacl.hash import blake2b
from nacl.public import Box, PrivateKey, PublicKey, SealedBox
from nacl.secret import SecretBox

from directorio import consultar
from identidad import Ficha
from paquete import (MAX_NOMBRE, TIPO_CEBOLLA, TIPO_MENSAJE, PaqueteInvalido)

# ------------------------------------------------------------- constantes
VERSION_CEBOLLA = 1
SALTOS = 3                     # relays por mensaje (constante de la red: define LARGO_FINAL)
TAM_LLAVE = 32
TAM_NOMBRE = MAX_NOMBRE        # nombres se rellenan con ceros hasta este largo
TAM_SALTO = TAM_NOMBRE + TAM_LLAVE          # Siguiente + EfimeraSig = 48
TAM_BLOQUE = 438               # R: bloque cifrado de la celda
TAM_CUERPO = TAM_LLAVE + TAM_BLOQUE         # 470
LARGO_FINAL = TAM_BLOQUE - TAM_SALTO * (SALTOS - 1)   # 342
TAM_MAC = 8
TAM_SELLADO = LARGO_FINAL - TAM_MAC - TAM_NOMBRE      # 318
TAM_CAJA = 48                  # sobrecarga de SealedBox (efimera 32 + tag 16)
TAM_PLANO = TAM_SELLADO - TAM_CAJA                    # 270
MAX_MENSAJE = TAM_PLANO - 2                           # 268 bytes de texto
VIDA_VISTOS = 600.0            # segundos que se recuerda una efimera (anti-repeticion)
MAX_VISTOS = 4096
CONTEXTO = b"faronet-cebolla-v1"


class CebollaError(Exception):
    """No se pudo armar o enviar el mensaje."""


# ------------------------------------------------------------ criptografia
def _derivar(secreto: bytes, efimera: bytes, relay: bytes) -> tuple[bytes, bytes]:
    """De DH(efimera, relay) saca (llave de flujo, llave de MAC), 32 bytes cada una."""
    k = blake2b(secreto + efimera + relay, digest_size=64, key=CONTEXTO, encoder=RawEncoder)
    return k[:32], k[32:]


def _xor(a: bytes, b: bytes) -> bytes:
    return (int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).to_bytes(len(a), "big")


def _flujo(llave: bytes, n: int) -> bytes:
    """
    n bytes de keystream. Se usa SecretBox sobre ceros: el texto cifrado
    es el keystream (tras los 16 bytes del tag). Cada llave se usa en UN solo
    mensaje, asi que el nonce fijo no se repite con la misma llave.
    """
    return SecretBox(llave).encrypt(bytes(n), bytes(24)).ciphertext[16:16 + n]


def _aplicar(llave: bytes, datos: bytes) -> bytes:
    return _xor(datos, _flujo(llave, len(datos)))


def _mac(llave: bytes, datos: bytes) -> bytes:
    return blake2b(datos, digest_size=TAM_MAC, key=llave, encoder=RawEncoder)


def _nombre16(nombre: str) -> bytes:
    n = nombre.encode()
    if not 0 < len(n) <= TAM_NOMBRE or b"\0" in n:
        raise CebollaError(f"nombre invalido: {nombre!r}")
    return n.ljust(TAM_NOMBRE, b"\0")


def _sin_ceros(b: bytes) -> str:
    return b.rstrip(b"\0").decode(errors="replace")


def rellenar_mensaje(mensaje: bytes) -> bytes:
    """Largo(2) | mensaje | ceros, siempre TAM_PLANO bytes: no se filtra el largo."""
    if len(mensaje) > MAX_MENSAJE:
        raise CebollaError(f"mensaje de {len(mensaje)} bytes; el maximo es {MAX_MENSAJE}")
    return len(mensaje).to_bytes(2, "big") + mensaje + bytes(MAX_MENSAJE - len(mensaje))


def quitar_relleno(plano: bytes) -> bytes:
    n = int.from_bytes(plano[:2], "big")
    if len(plano) != TAM_PLANO or n > MAX_MENSAJE:
        raise PaqueteInvalido("mensaje mal formado")
    return plano[2:2 + n]


# ----------------------------------------------------------------- cliente
def construir_celda(ruta: list[Ficha], destino: Ficha, mensaje: bytes) -> bytes:
    """
    Arma el cuerpo de la celda (TAM_CUERPO bytes) para enviarlo al primer relay.
    'ruta' son las fichas de los relays en orden: ruta[0] es el primero.
    """
    if len(ruta) != SALTOS:
        raise CebollaError(f"la ruta debe tener {SALTOS} relays")
    efimeras = [PrivateKey.generate() for _ in ruta]
    pubs = [bytes(e.public_key) for e in efimeras]
    llaves = [_derivar(Box(e, PublicKey(f.llave_cifrado)).shared_key(), p, f.llave_cifrado)
              for e, p, f in zip(efimeras, pubs, ruta)]

    sellado = SealedBox(PublicKey(destino.llave_cifrado)).encrypt(rellenar_mensaje(mensaje))
    final = _nombre16(destino.nombre) + sellado
    bloque = _aplicar(llaves[-1][0], _mac(llaves[-1][1], final) + final)
    for i in range(SALTOS - 2, -1, -1):
        plano = _nombre16(ruta[i + 1].nombre) + pubs[i + 1] + bloque
        bloque = _aplicar(llaves[i][0], plano)
    cuerpo = pubs[0] + bloque
    assert len(cuerpo) == TAM_CUERPO
    return cuerpo


# --------------------------------------------------------------- el servicio
class Cebolla:
    """
    La parte de la cebolla de un nodo: hace de relay para las celdas ajenas y
    permite enviar mensajes anonimos. 'nodo' es el Nodo de nodo.py; solo se
    usan nodo.identidad, nodo.nombre, nodo.directorio, nodo.conocidos,
    nodo.ruta_a(), nodo.enviar_enrutado() y nodo.log().
    """

    def __init__(self, nodo):
        self.nodo = nodo
        self.vistos: dict[bytes, float] = {}   # efimera -> vencimiento (solo en memoria)
        self.rng = random.SystemRandom()

    # -- anti-repeticion ---------------------------------------------------
    def _ya_visto(self, efimera: bytes) -> bool:
        ahora = time.monotonic()
        if len(self.vistos) >= MAX_VISTOS:
            self.vistos = {e: v for e, v in self.vistos.items() if v > ahora}
            if len(self.vistos) >= MAX_VISTOS:   # llena de vigentes: se descarta lo nuevo
                return True
        if self.vistos.get(efimera, 0) > ahora:
            return True
        self.vistos[efimera] = ahora + VIDA_VISTOS
        return False

    # -- relay -----------------------------------------------------------------
    async def recibir(self, tipo: int, carga: bytes):
        """Entrada desde el nodo: una carga enrutada que llego para este nodo."""
        if tipo == TIPO_CEBOLLA:
            await self.procesar_celda(carga)
        elif tipo == TIPO_MENSAJE:
            self.abrir_mensaje(carga)

    async def procesar_celda(self, cuerpo: bytes):
        """Quita la capa de este relay y reenvia, o entrega si es el ultimo."""
        if len(cuerpo) != TAM_CUERPO:
            self.nodo.log("celda descartada: tamano incorrecto")
            return
        efimera, bloque = cuerpo[:TAM_LLAVE], cuerpo[TAM_LLAVE:]
        if self._ya_visto(efimera):
            self.nodo.log("celda descartada: repetida")
            return
        id_cifrado = self.nodo.identidad.cifrado
        secreto = Box(id_cifrado, PublicKey(efimera)).shared_key()
        k_flujo, k_mac = _derivar(secreto, efimera, bytes(id_cifrado.public_key))
        d = _aplicar(k_flujo, bloque)

        # Si el MAC cuadra, la celda era para el ultimo relay: este.
        final = d[TAM_MAC:LARGO_FINAL]
        if _mac(k_mac, final) == d[:TAM_MAC]:
            destino = _sin_ceros(final[:TAM_NOMBRE])
            sellado = final[TAM_NOMBRE:]
            self.nodo.log("soy el ultimo relay; entregando el mensaje sellado")
            await self.nodo.enviar_enrutado(TIPO_MENSAJE, destino, sellado)
            return

        # Relay intermedio: Siguiente | EfimeraSig | Resto
        siguiente = _sin_ceros(d[:TAM_NOMBRE])
        efimera_sig = d[TAM_NOMBRE:TAM_SALTO]
        resto = d[TAM_SALTO:]
        nueva = efimera_sig + resto + os.urandom(TAM_SALTO)
        self.nodo.log("reenviando celda al siguiente relay")
        await self.nodo.enviar_enrutado(TIPO_CEBOLLA, siguiente, nueva)

    # -- destinatario --------------------------------------------------------------
    def abrir_mensaje(self, sellado: bytes):
        """Abre un mensaje sellado dirigido a este nodo. Si no es para el, lo descarta."""
        if len(sellado) != TAM_SELLADO:
            self.nodo.log("mensaje descartado: tamano incorrecto")
            return
        try:
            plano = SealedBox(self.nodo.identidad.cifrado).decrypt(sellado)
            texto = quitar_relleno(plano)
        except (CryptoError, PaqueteInvalido):
            self.nodo.log("mensaje descartado: no se pudo abrir")
            return
        print(f"[{self.nodo.nombre}] mensaje ANONIMO recibido: {texto.decode(errors='replace')}")

    # -- emisor -----------------------------------------------------------------------
    async def refrescar_directorio(self):
        if self.nodo.directorio is None:
            raise CebollaError("el nodo no tiene --directorio")
        try:
            fichas, _ = await consultar(self.nodo.directorio)
        except (OSError, asyncio.TimeoutError, PaqueteInvalido, asyncio.IncompleteReadError):
            raise CebollaError("no se pudo consultar el directorio") from None
        self.nodo.conocidos = {f.nombre: f for f in fichas}

    def elegir_ruta(self, destino: str) -> tuple[list[Ficha], Ficha]:
        """Escoge SALTOS relays alcanzables, distintos de este nodo y del destino."""
        conocidos = self.nodo.conocidos
        if destino not in conocidos:
            raise CebollaError(f"{destino} no esta en el directorio")
        candidatos = [f for n, f in conocidos.items()
                      if n not in (self.nodo.nombre, destino) and self.nodo.ruta_a(n) is not None]
        if len(candidatos) < SALTOS:
            raise CebollaError(f"se necesitan {SALTOS} relays alcanzables; hay {len(candidatos)}")
        return self.rng.sample(candidatos, SALTOS), conocidos[destino]

    async def enviar_anonimo(self, destino: str, texto: str):
        """Envia un mensaje por una ruta de SALTOS relays al azar."""
        await self.refrescar_directorio()
        ruta, ficha_destino = self.elegir_ruta(destino)
        cuerpo = construir_celda(ruta, ficha_destino, texto.encode())
        self.nodo.log("ruta elegida: " + " -> ".join(f.nombre for f in ruta) + f" -> {destino}")
        await self.nodo.enviar_enrutado(TIPO_CEBOLLA, ruta[0].nombre, cuerpo)