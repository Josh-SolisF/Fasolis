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
  - Autenticacion POR SALTO: cada capa lleva un MAC (BLAKE2b con llave) que
    cubre TODO el bloque que recibe el relay. Un relay que altera un solo bit
    de la celda es detectado por el siguiente relay honesto, que la descarta.
  - Mensaje: SealedBox hacia la llave publica del destinatario (extremo a extremo).
    Dentro del sellado el emisor decide si se identifica ante el DESTINATARIO:
      anonimo: Banderas(0) | Largo(2) | Texto | relleno         (no se puede contestar)
      firmado: Banderas(1) | Largo(2) | Remitente(16) | Box(texto) | relleno
      publico: Banderas(2) | Largo(2) | Tema(16) | Texto | relleno   (para el gateway)
    Box(remitente -> destinatario) autentica al remitente con su llave de la
    ficha del directorio (nadie puede suplantarlo) y cuesta 40 bytes. El nombre
    del remitente viaja DENTRO del sellado: ningun relay lo ve. Para contestar,
    el destinatario hace un envio anonimo normal hacia ese nombre.
    Un mensaje PUBLICO va sellado hacia el nodo gateway: los relays no ven ni el
    tema ni el texto, el gateway si (es publico por definicion) y solo estos
    mensajes pueden salir a clearnet. Nunca lleva remitente.
  - Rotacion: la llave de cifrado de cada nodo cambia cada cierto tiempo y la
    vieja se olvida tras un periodo de gracia (identidad.py). Un relay prueba
    la llave vigente y las que aun estan en gracia.
  - Repeticion: cada relay recuerda (en memoria, con TTL) las llaves efimeras
    ya vistas y descarta las repetidas.

Formato de la celda (cuerpo de TIPO_CEBOLLA, TAM_CUERPO bytes, fijo):
    Version(1) | Efimera(32) | MAC(8) | Bloque(R)
  La version va en claro (el relay la necesita antes de descifrar) y tambien
  entra en la derivacion de llaves, asi que alterarla hace fallar la celda.
  Una version desconocida se descarta sin reenviar.

  El MAC es el de ESTE relay sobre el Bloque tal como llega. Tras verificarlo,
  el relay descifra (Bloque | S ceros) con su flujo de R+S bytes (S = TAM_SALTO):
    Cabecera(S) = Siguiente(16, relleno de ceros) | EfimeraSig(32) | MACsig(8)
    Bloque nuevo = los R bytes que siguen a la cabecera
  y reenvia  Version | EfimeraSig | MACsig | Bloque nuevo.

  Bloque que ve el primer relay (R = SALTOS*S + TAM_SELLADO):
    Cab1 | Cab2 | ... | CabN | Sellado          (cada una cifrada en capas)
  El ultimo relay lo reconoce porque EfimeraSig es de puros ceros (nunca es una
  llave real); su cabecera trae el DESTINO en lugar del siguiente relay, y el
  mensaje sellado va justo despues.

  Relleno determinista (idea de Sphinx): al quitar una cabecera, el bloque queda
  S bytes mas corto y se completa con ceros descifrados. Esos bytes dependen solo
  de las llaves de los relays anteriores, asi que el emisor los calcula de
  antemano ("filler") y el MAC de cada salto cubre tambien ese relleno. Por eso
  no hay bytes aleatorios que un relay pueda cambiar sin ser detectado.

Mensaje de salida (TIPO_MENSAJE) hacia el destino: solo el sellado, de tamano fijo.

Limitaciones conocidas (para el RFC / modelo de amenazas):
  - Secreto hacia adelante PARCIAL: la llave de cifrado rota y la vieja se olvida
    tras GRACIA_LLAVE segundos, asi que robar el nodo despues no abre trafico
    capturado antes de esa ventana. Pero dentro de la ventana de una misma llave
    todo se puede descifrar, y no es un handshake por mensaje (tipo ntor). La
    llave de FIRMA no rota: si se roba, permite suplantar al nodo, no descifrar.
  - Los MAC por salto son de 8 bytes (64 bits) para que el mensaje quepa en el
    paquete de 512 bytes; alcanza para impedir marcar celdas, no para otros usos.
  - No hay mezcla ni retrasos: un observador global de trafico puede
    correlacionar por tiempos. Se documenta como fuera del alcance.
"""
from __future__ import annotations

import asyncio
import hmac
import os
import random
import time

from nacl.encoding import RawEncoder
from nacl.exceptions import CryptoError
from nacl.hash import blake2b
from nacl.public import Box, PrivateKey, PublicKey, SealedBox
from nacl.secret import SecretBox

from faronet.mesh.directorio import consultar
from faronet.mesh.identidad import Ficha, Identidad
from faronet.mesh.paquete import (MAX_CONTENIDO, MAX_NOMBRE, TIPO_CEBOLLA, TIPO_MENSAJE, PaqueteInvalido)

# ------------------------------------------------------------- constantes
VERSION_CEBOLLA = 1
VERSIONES_SOPORTADAS = {1}     # versiones que este nodo sabe procesar
SALTOS = 3                     # relays por mensaje (constante de la red: define los tamanos)
TAM_VERSION = 1                # byte de version al inicio de la celda
TAM_LLAVE = 32
TAM_MAC = 8                    # MAC por salto
TAM_NOMBRE = MAX_NOMBRE        # nombres se rellenan con ceros hasta este largo
TAM_SALTO = TAM_NOMBRE + TAM_LLAVE + TAM_MAC          # Siguiente + EfimeraSig + MAC = 56
TAM_ENCABEZADO_CELDA = TAM_VERSION + TAM_LLAVE + TAM_MAC   # 41
# El paquete enrutado (nodo.py) gasta TTL(1) + Destino(16) antes de la celda.
TAM_CUERPO = MAX_CONTENIDO - 1 - MAX_NOMBRE           # 487: la celda llena el paquete
TAM_BLOQUE = TAM_CUERPO - TAM_ENCABEZADO_CELDA        # 446: R
TAM_SELLADO = TAM_BLOQUE - SALTOS * TAM_SALTO         # 278
TAM_CAJA = 48                  # sobrecarga de SealedBox (efimera 32 + tag 16)
TAM_PLANO = TAM_SELLADO - TAM_CAJA                    # 230
TAM_ENC_PLANO = 3              # Banderas(1) | Largo(2) al inicio del plano
TAM_CAJA_FIRMA = 40            # Box remitente->destinatario: nonce(24) + tag(16)
MAX_MENSAJE = TAM_PLANO - TAM_ENC_PLANO               # 227 bytes de texto, anonimo
MAX_MENSAJE_FIRMADO = MAX_MENSAJE - TAM_NOMBRE - TAM_CAJA_FIRMA   # 171, con remitente
BANDERA_ANONIMO = 0
BANDERA_FIRMADO = 1
BANDERA_PUBLICO = 2
MAX_PUBLICO = MAX_MENSAJE - TAM_NOMBRE                # 211 bytes de texto, con tema
VIDA_VISTOS = 600.0            # segundos que se recuerda una efimera (anti-repeticion)
MAX_VISTOS = 4096
MIN_REFRESCO = 10.0            # segundos minimos entre consultas al directorio por mensajes recibidos
CONTEXTO = b"faronet-cebolla-v1"
CEROS_LLAVE = bytes(TAM_LLAVE)  # EfimeraSig de puros ceros = "este es el ultimo relay"


class CebollaError(Exception):
    """No se pudo armar o enviar el mensaje."""


# ------------------------------------------------------------ criptografia
def _derivar(secreto: bytes, efimera: bytes, relay: bytes,
             version: int = VERSION_CEBOLLA) -> tuple[bytes, bytes]:
    """De DH(efimera, relay) y la version saca (llave de flujo, llave de MAC), 32 bytes cada una."""
    k = blake2b(bytes([version]) + secreto + efimera + relay, digest_size=64,
                key=CONTEXTO, encoder=RawEncoder)
    return k[:32], k[32:]


def _xor(a: bytes, b: bytes) -> bytes:
    """XOR de dos cadenas del MISMO largo."""
    if len(a) != len(b):
        raise ValueError("xor de cadenas de distinto largo")
    return (int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).to_bytes(len(a), "big")


def _flujo(llave: bytes, n: int) -> bytes:
    """
    n bytes de keystream. Se usa SecretBox sobre ceros: el texto cifrado
    es el keystream (tras los 16 bytes del tag). Cada llave se usa en UN solo
    mensaje, asi que el nonce fijo no se repite con la misma llave.
    """
    return SecretBox(llave).encrypt(bytes(n), bytes(24)).ciphertext[16:16 + n]


def _mac(llave: bytes, datos: bytes) -> bytes:
    return blake2b(datos, digest_size=TAM_MAC, key=llave, encoder=RawEncoder)


def _nombre16(nombre: str) -> bytes:
    n = nombre.encode()
    if not 0 < len(n) <= TAM_NOMBRE or b"\0" in n:
        raise CebollaError(f"nombre invalido: {nombre!r}")
    return n.ljust(TAM_NOMBRE, b"\0")


def _sin_ceros(b: bytes) -> str:
    return b.rstrip(b"\0").decode(errors="replace")


def armar_plano(mensaje: bytes, destino: Ficha, remitente: Identidad | None = None,
                tema: str | None = None) -> bytes:
    """
    Banderas(1) | Largo(2) | cuerpo | ceros, siempre TAM_PLANO bytes: no se filtra
    el largo ni si el mensaje lleva remitente. Con remitente, el cuerpo es
    Remitente(16) | Box(remitente -> destinatario)(mensaje). Con tema (mensaje
    publico para el gateway) el cuerpo es Tema(16) | mensaje y NO hay remitente.
    """
    if tema is not None and remitente is not None:
        raise CebollaError("un mensaje publico no puede llevar remitente")
    limite = (MAX_PUBLICO if tema is not None
              else MAX_MENSAJE if remitente is None else MAX_MENSAJE_FIRMADO)
    if len(mensaje) > limite:
        raise CebollaError(f"mensaje de {len(mensaje)} bytes; el maximo es {limite}")
    if tema is not None:
        banderas, cuerpo = BANDERA_PUBLICO, _nombre16(tema) + mensaje
    elif remitente is None:
        banderas, cuerpo = BANDERA_ANONIMO, mensaje
    else:
        caja = Box(remitente.cifrado, PublicKey(destino.llave_cifrado)).encrypt(mensaje)
        banderas, cuerpo = BANDERA_FIRMADO, _nombre16(remitente.nombre) + bytes(caja)
    plano = bytes([banderas]) + len(cuerpo).to_bytes(2, "big") + cuerpo
    return plano + bytes(TAM_PLANO - len(plano))


def leer_plano(plano: bytes) -> tuple[int, bytes]:
    """Devuelve (banderas, cuerpo) de un plano de TAM_PLANO bytes."""
    n = int.from_bytes(plano[1:TAM_ENC_PLANO], "big")
    if len(plano) != TAM_PLANO or n > TAM_PLANO - TAM_ENC_PLANO:
        raise PaqueteInvalido("mensaje mal formado")
    return plano[0], plano[TAM_ENC_PLANO:TAM_ENC_PLANO + n]


# ----------------------------------------------------------------- cliente
def _relleno_determinista(flujos: list[bytes]) -> bytes:
    """
    Los (SALTOS-1)*S bytes que quedan al final del bloque cuando llega al ultimo
    relay. Cada relay anterior corre el bloque S bytes y agrega ceros descifrados
    con su propio flujo; el emisor repite ese calculo para poder fijar el MAC.
    """
    relleno = b""
    for flujo in flujos[:SALTOS - 1]:
        largo = len(relleno) + TAM_SALTO
        relleno = _xor(relleno + bytes(TAM_SALTO), flujo[-largo:])
    return relleno


def construir_celda(ruta: list[Ficha], destino: Ficha, mensaje: bytes,
                    remitente: Identidad | None = None, tema: str | None = None) -> bytes:
    """
    Arma el cuerpo de la celda (TAM_CUERPO bytes) para enviarlo al primer relay.
    'ruta' son las fichas de los relays en orden: ruta[0] es el primero.
    'remitente' (opcional): la identidad con que se firma ante el destinatario.
    'tema' (opcional): hace un mensaje PUBLICO para el gateway destino.
    """
    if len(ruta) != SALTOS:
        raise CebollaError(f"la ruta debe tener {SALTOS} relays")
    efimeras = [PrivateKey.generate() for _ in ruta]
    pubs = [bytes(e.public_key) for e in efimeras]
    llaves = [_derivar(Box(e, PublicKey(f.llave_cifrado)).shared_key(), p, f.llave_cifrado)
              for e, p, f in zip(efimeras, pubs, ruta)]
    flujos = [_flujo(k[0], TAM_BLOQUE + TAM_SALTO) for k in llaves]

    sellado = SealedBox(PublicKey(destino.llave_cifrado)).encrypt(armar_plano(mensaje, destino, remitente, tema))
    # Ultimo relay: su cabecera trae el destino (y ceros donde iria la efimera y el MAC).
    interior = _nombre16(destino.nombre) + CEROS_LLAVE + bytes(TAM_MAC) + sellado
    bloque = _xor(interior, flujos[-1][:len(interior)]) + _relleno_determinista(flujos)
    mac = _mac(llaves[-1][1], bloque)
    # Hacia afuera: cada capa agrega su cabecera (siguiente, su efimera y su MAC).
    for i in range(SALTOS - 2, -1, -1):
        cabecera = _nombre16(ruta[i + 1].nombre) + pubs[i + 1] + mac
        bloque = _xor(cabecera + bloque, flujos[i])[:TAM_BLOQUE]
        mac = _mac(llaves[i][1], bloque)
    cuerpo = bytes([VERSION_CEBOLLA]) + pubs[0] + mac + bloque
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
        self._ultimo_refresco = float("-inf")   # ultima consulta al directorio por un remitente
        # Que hacer con un mensaje recibido: (remitente o None, texto, verificado).
        # La aplicacion (chat) puede reemplazarlo; por defecto se imprime.
        self.al_recibir = self._mostrar
        # Mensajes publicos (solo tiene sentido en el gateway): (tema, texto).
        self.al_recibir_publico = self._sin_gateway

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
            await self.recibir_mensaje(carga)

    async def procesar_celda(self, cuerpo: bytes):
        """Verifica el MAC, quita la capa de este relay y reenvia, o entrega si es el ultimo."""
        if len(cuerpo) != TAM_CUERPO:
            self.nodo.log("celda descartada: tamano incorrecto")
            return
        version = cuerpo[0]
        if version not in VERSIONES_SOPORTADAS:
            self.nodo.log(f"celda descartada: version {version} no soportada")
            return
        i = TAM_VERSION
        efimera = cuerpo[i:i + TAM_LLAVE]
        mac = cuerpo[i + TAM_LLAVE:i + TAM_LLAVE + TAM_MAC]
        bloque = cuerpo[TAM_ENCABEZADO_CELDA:]

        # Se prueba la llave vigente y las que aun estan en gracia tras una rotacion.
        for clave in self.nodo.identidad.llaves_cifrado():
            try:
                secreto = Box(clave, PublicKey(efimera)).shared_key()
            except (CryptoError, ValueError, RuntimeError):   # llave efimera invalida
                continue
            k_flujo, k_mac = _derivar(secreto, efimera, bytes(clave.public_key), version)
            if hmac.compare_digest(_mac(k_mac, bloque), mac):
                break
        else:
            self.nodo.log("celda descartada: MAC invalido")
            return
        # Solo despues del MAC se recuerda la efimera: basura no llena la memoria.
        if self._ya_visto(efimera):
            self.nodo.log("celda descartada: repetida")
            return

        d = _xor(bloque + bytes(TAM_SALTO), _flujo(k_flujo, TAM_BLOQUE + TAM_SALTO))
        nombre = d[:TAM_NOMBRE]
        efimera_sig = d[TAM_NOMBRE:TAM_NOMBRE + TAM_LLAVE]
        mac_sig = d[TAM_NOMBRE + TAM_LLAVE:TAM_SALTO]
        resto = d[TAM_SALTO:]

        if efimera_sig == CEROS_LLAVE:           # soy el ultimo relay: 'nombre' es el destino
            destino = _sin_ceros(nombre)
            if not destino:
                self.nodo.log("celda descartada: destino vacio")
                return
            self.nodo.log("soy el ultimo relay; entregando el mensaje sellado")
            await self.nodo.enviar_enrutado(TIPO_MENSAJE, destino, resto[:TAM_SELLADO])
            return

        siguiente = _sin_ceros(nombre)
        if not siguiente:
            self.nodo.log("celda descartada: siguiente relay vacio")
            return
        self.nodo.log("reenviando celda al siguiente relay")
        await self.nodo.enviar_enrutado(TIPO_CEBOLLA, siguiente,
                                        bytes([version]) + efimera_sig + mac_sig + resto)

    # -- destinatario --------------------------------------------------------------
    async def recibir_mensaje(self, sellado: bytes):
        """Abre un mensaje sellado dirigido a este nodo. Si no es para el, lo descarta."""
        if len(sellado) != TAM_SELLADO:
            self.nodo.log("mensaje descartado: tamano incorrecto")
            return
        plano = None
        for clave in self.nodo.identidad.llaves_cifrado():
            try:
                plano = SealedBox(clave).decrypt(sellado)
                break
            except CryptoError:
                continue
        try:
            if plano is None:
                raise PaqueteInvalido("no se pudo abrir")
            banderas, cuerpo = leer_plano(plano)
        except PaqueteInvalido:
            self.nodo.log("mensaje descartado: no se pudo abrir")
            return
        # Un relay malicioso podria reentregar el mismo sellado: se ignora la copia.
        if self._ya_visto(sellado[:TAM_LLAVE]):
            self.nodo.log("mensaje descartado: repetido")
            return
        if banderas == BANDERA_ANONIMO:
            self.al_recibir(None, cuerpo, False)
        elif banderas == BANDERA_FIRMADO and len(cuerpo) > TAM_NOMBRE + TAM_CAJA_FIRMA:
            nombre = _sin_ceros(cuerpo[:TAM_NOMBRE])
            texto = await self._verificar_remitente(nombre, cuerpo[TAM_NOMBRE:])
            if texto is None:
                self.nodo.log("mensaje descartado: remitente no verificado")
                return
            self.al_recibir(nombre, texto, True)
        elif banderas == BANDERA_PUBLICO and len(cuerpo) >= TAM_NOMBRE:
            tema = _sin_ceros(cuerpo[:TAM_NOMBRE])
            if not tema:
                self.nodo.log("mensaje descartado: tema vacio")
                return
            self.al_recibir_publico(tema, cuerpo[TAM_NOMBRE:])
        else:
            self.nodo.log("mensaje descartado: banderas desconocidas")

    async def _verificar_remitente(self, nombre: str, caja: bytes) -> bytes | None:
        """
        Abre el Box del remitente con su ficha del directorio. Si falla, quiza el
        remitente roto su llave de cifrado desde que lo consultamos: se refresca
        el directorio una vez (con limite de frecuencia) y se reintenta.
        """
        for forzar in (False, True):
            ficha = await self._ficha_de(nombre, forzar)
            if ficha is None:
                return None
            for clave in self.nodo.identidad.llaves_cifrado():
                try:
                    return Box(clave, PublicKey(ficha.llave_cifrado)).decrypt(caja)
                except (CryptoError, ValueError):
                    continue
        return None

    async def _ficha_de(self, nombre: str, forzar: bool = False) -> Ficha | None:
        """
        La ficha verificada de un nodo; si no se conoce (o se pide refrescar) se
        consulta el directorio, a lo sumo una vez cada MIN_REFRESCO segundos para
        que mensajes basura no obliguen a consultarlo sin parar.
        """
        ahora = time.monotonic()
        if (forzar or nombre not in self.nodo.conocidos) and ahora - self._ultimo_refresco >= MIN_REFRESCO:
            self._ultimo_refresco = ahora
            try:
                await self.refrescar_directorio()
            except CebollaError:
                pass
        return self.nodo.conocidos.get(nombre)

    def _sin_gateway(self, tema: str, texto: bytes):
        """Por defecto un nodo corriente NO publica nada en clearnet."""
        self.nodo.log("mensaje publico descartado: este nodo no es gateway")

    def _mostrar(self, remitente: str | None, texto: bytes, verificado: bool):
        t = texto.decode(errors="replace")
        if remitente is None:
            print(f"[{self.nodo.nombre}] mensaje ANONIMO recibido: {t}")
        else:
            print(f"[{self.nodo.nombre}] mensaje de {remitente} (verificado): {t}")

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

    async def enviar_anonimo(self, destino: str, texto: str, firmado: bool = False,
                             tema: str | None = None):
        """
        Envia un mensaje por una ruta de SALTOS relays al azar. Con firmado=True el
        destinatario sabe quien lo envia (y puede contestar); los relays nunca.
        Con tema, es un mensaje publico para el gateway 'destino' (siempre anonimo).
        """
        await self.refrescar_directorio()
        ruta, ficha_destino = self.elegir_ruta(destino)
        cuerpo = construir_celda(ruta, ficha_destino, texto.encode(),
                                self.nodo.identidad if firmado else None, tema)
        self.nodo.log("ruta elegida: " + " -> ".join(f.nombre for f in ruta) + f" -> {destino}")
        await self.nodo.enviar_enrutado(TIPO_CEBOLLA, ruta[0].nombre, cuerpo)