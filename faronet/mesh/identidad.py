"""
identidad.py - Llaves de un nodo y fichas firmadas del directorio.

Cada nodo tiene dos pares de llaves (PyNaCl):
  - firma   (Ed25519):  demuestra que una ficha la publico ese nodo.
  - cifrado (X25519):   permite que otros le manden datos que solo el lee.

La llave de FIRMA (larga vida) se guarda en ~/.faronet/<nombre>.llaves (32 bytes,
la semilla; un archivo viejo de 64 bytes tambien se lee). Ese archivo es el
estado sensible que el boton de panico debe borrar. No debe subirse nunca al
repositorio.

La llave de CIFRADO nunca se escribe a disco: vive en memoria y ROTA. Al rotar,
la anterior se conserva solo GRACIA_LLAVE segundos (para celdas ya en camino y
fichas que otros tienen en cache) y luego se olvida. Asi, quien robe el nodo
mas tarde no puede descifrar trafico capturado antes de la rotacion (secreto
hacia adelante). Python no permite borrar la memoria de forma garantizada: se
sueltan las referencias, y es lo mejor que se puede hacer aqui.

Formato de una ficha (version 1, big-endian):
  Version(1) | LongNombre(1) | Nombre | LlaveFirma(32) | LlaveCifrado(32) |
  Expira(8, segundos Unix) | Firma(64)
La firma Ed25519 cubre todos los campos anteriores a ella.
"""
from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass

from nacl.encoding import HexEncoder
from nacl.exceptions import CryptoError
from nacl.hash import blake2b
from nacl.public import PrivateKey
from nacl.signing import SigningKey, VerifyKey

from faronet.mesh.paquete import MAX_NOMBRE

VERSION_FICHA = 1
VIDA_FICHA = 300        # segundos de validez de una ficha recien firmada
GRACIA_LLAVE = 600      # segundos que se acepta una llave de cifrado ya rotada
MAX_VIDA_FICHA = 3600   # el directorio rechaza vencimientos mas lejanos
TAM_LLAVE = 32
TAM_FIRMA = 64


class FichaInvalida(Exception):
    """La ficha esta mal formada, vencida o con firma incorrecta."""


@dataclass
class Ficha:
    nombre: str
    llave_firma: bytes    # llave publica Ed25519
    llave_cifrado: bytes  # llave publica X25519
    expira: int           # segundos Unix
    firma: bytes = b""

    def cuerpo(self) -> bytes:
        """Los bytes que cubre la firma."""
        nombre = self.nombre.encode()
        return (struct.pack("!BB", VERSION_FICHA, len(nombre)) + nombre
                + self.llave_firma + self.llave_cifrado + struct.pack("!Q", self.expira))

    def codificar(self) -> bytes:
        return self.cuerpo() + self.firma

    @staticmethod
    def decodificar(datos: bytes) -> "Ficha":
        if len(datos) < 2:
            raise FichaInvalida("ficha vacia")
        version, n = datos[0], datos[1]
        if version != VERSION_FICHA:
            raise FichaInvalida(f"version de ficha {version} no soportada")
        if not 0 < n <= MAX_NOMBRE:
            raise FichaInvalida("nombre invalido")
        esperado = 2 + n + 2 * TAM_LLAVE + 8 + TAM_FIRMA
        if len(datos) != esperado:
            raise FichaInvalida("tamano de ficha incorrecto")
        i = 2
        nombre = datos[i:i + n].decode(errors="replace"); i += n
        llave_firma = datos[i:i + TAM_LLAVE]; i += TAM_LLAVE
        llave_cifrado = datos[i:i + TAM_LLAVE]; i += TAM_LLAVE
        (expira,) = struct.unpack("!Q", datos[i:i + 8]); i += 8
        return Ficha(nombre, llave_firma, llave_cifrado, expira, datos[i:])

    def verificar(self, ahora: float | None = None):
        """Lanza FichaInvalida si la ficha esta vencida o la firma no cuadra."""
        ahora = time.time() if ahora is None else ahora
        if self.expira <= ahora:
            raise FichaInvalida("ficha vencida")
        if self.expira > ahora + MAX_VIDA_FICHA:
            raise FichaInvalida("vencimiento demasiado lejano")
        try:
            VerifyKey(self.llave_firma).verify(self.cuerpo(), self.firma)
        except (CryptoError, ValueError, TypeError):
            raise FichaInvalida("firma invalida") from None

    def huella(self) -> str:
        """Resumen corto de la llave de firma, para comparar a simple vista."""
        return blake2b(self.llave_firma, digest_size=8, encoder=HexEncoder).decode()


class Identidad:
    """Las llaves privadas de este nodo."""

    def __init__(self, nombre: str, firma: SigningKey, cifrado: PrivateKey):
        self.nombre = nombre
        self.firma = firma
        self._actual = cifrado
        self._anteriores: list[tuple[PrivateKey, float]] = []   # (llave, vence en monotonic)

    @property
    def cifrado(self) -> PrivateKey:
        """La llave de cifrado vigente: la que se publica en la ficha y se usa para enviar."""
        return self._actual

    def _purgar(self):
        ahora = time.monotonic()
        self._anteriores = [(k, v) for k, v in self._anteriores if v > ahora]

    def llaves_cifrado(self) -> list[PrivateKey]:
        """Todas las llaves con las que aun se acepta trafico: la vigente primero."""
        self._purgar()
        return [self._actual] + [k for k, _ in self._anteriores]

    def rotar(self, gracia: float = GRACIA_LLAVE):
        """Genera una llave de cifrado nueva; la vieja se acepta 'gracia' segundos mas."""
        self._purgar()
        self._anteriores.append((self._actual, time.monotonic() + gracia))
        self._actual = PrivateKey.generate()

    def olvidar(self):
        """Descarta ya mismo todas las llaves de cifrado viejas (boton de panico)."""
        self._anteriores.clear()
        self._actual = PrivateKey.generate()   # la vigente tambien se suelta

    @staticmethod
    def ruta_por_defecto(nombre: str) -> str:
        return os.path.join(os.path.expanduser("~"), ".faronet", f"{nombre}.llaves")

    @classmethod
    def cargar_o_crear(cls, nombre: str, ruta: str | None = None) -> "Identidad":
        """
        Lee la llave de firma del archivo; si no existe o esta danado, crea una nueva.
        La llave de cifrado siempre es nueva en cada arranque (no se guarda).
        """
        ruta = ruta or cls.ruta_por_defecto(nombre)
        try:
            with open(ruta, "rb") as f:
                datos = f.read()
            if len(datos) in (TAM_LLAVE, 2 * TAM_LLAVE):
                return cls(nombre, SigningKey(datos[:TAM_LLAVE]), PrivateKey.generate())
        except OSError:
            pass
        firma = SigningKey.generate()
        # abspath: si la ruta no trae carpeta ("impostor.llaves"), es la actual.
        os.makedirs(os.path.dirname(os.path.abspath(ruta)), exist_ok=True)
        # 0o600: solo el dueno puede leerlo (en Windows se ignora el modo).
        fd = os.open(ruta, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(bytes(firma))
        return cls(nombre, firma, PrivateKey.generate())

    def ficha(self, vida: int = VIDA_FICHA) -> Ficha:
        """Crea y firma una ficha nueva con vencimiento dentro de 'vida' segundos."""
        f = Ficha(self.nombre, bytes(self.firma.verify_key),
                  bytes(self._actual.public_key), int(time.time()) + vida)
        f.firma = self.firma.sign(f.cuerpo()).signature
        return f