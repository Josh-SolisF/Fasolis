"""
capa1.py - Capa 1 de FaroNet (version 1 de la trama).

Contiene tres cosas:
  1. Trama + codificar():  convierte una trama en los bytes que salen por la luz.
  2. Decodificador:        recibe bytes sueltos (con basura, cortes o errores)
                           y entrega solo las tramas validas.
  3. DispositivoLuzAdaptador: envia y recibe tramas sobre un puerto serie.
                           Acceso al medio: maestro/esclavo con ACK y reintentos.

El adaptador solo necesita un objeto "tipo puerto serie" (write, read,
in_waiting, flush, reset_input_buffer). Por eso se prueba en la laptop con
un puerto virtual y luego se cambia a /dev/serial0 sin tocar nada mas.

Formato de la trama (todo en big-endian):
  Sincronia(2)=AA 55 | Version(1) | Tipo(1) | Destino(2) | Origen(2) |
  Secuencia(1) | Fragmento(1) | Longitud(1) | Datos(0..115) | CRC-16(2)
El CRC se calcula desde Version hasta Datos (binascii.crc_hqx, valor inicial 0).
"""

import binascii
import os
import secrets
import struct
import time
from collections import deque
from dataclasses import dataclass

# ----------------------------------------------------------------- constantes
SINCRONIA = b"\xAA\x55"
VERSION = 1

TIPO_DATOS = 1
TIPO_ACK = 2
TIPO_EXTERNA = 3  # trama creada fuera del Faro; el Faro solo la transporta

DIFUSION = 0xFFFF
MAX_DATOS = 115

_FORMATO_ENCABEZADO = ">BBHHBBB"  # version, tipo, destino, origen, secuencia, fragmento, longitud
TAM_ENCABEZADO = struct.calcsize(_FORMATO_ENCABEZADO)  # 9
TAM_MAX_TRAMA = len(SINCRONIA) + TAM_ENCABEZADO + MAX_DATOS + 2  # 128


@dataclass
class Trama:
    tipo: int
    destino: int
    origen: int
    secuencia: int
    datos: bytes = b""
    fragmento: int = 0  # bit 7 = "vienen mas", bits 0-6 = indice


def generar_direccion() -> int:
    """Direccion fisica aleatoria de 2 bytes (se evita 0 y la difusion)."""
    while True:
        d = secrets.randbelow(0x10000)
        if d not in (0, DIFUSION):
            return d


def direccion_persistente(ruta="~/.faronet_direccion", rotar=False) -> int:
    """
    Lee la direccion fisica del archivo (la memoria no volatil del Faro).
    Si no existe, o si rotar=True, genera una nueva y la guarda. Rotarla es
    simplemente reescribir el archivo.
    """
    ruta = os.path.expanduser(ruta)
    if not rotar:
        try:
            with open(ruta) as f:
                return int(f.read().strip(), 16)
        except (OSError, ValueError):
            pass
    d = generar_direccion()
    with open(ruta, "w") as f:
        f.write(f"{d:04X}\n")
    return d


# ------------------------------------------------------------------ codificar
def codificar(t: Trama) -> bytes:
    """Trama -> bytes listos para escribir en el puerto serie."""
    if len(t.datos) > MAX_DATOS:
        raise ValueError(f"datos de {len(t.datos)} bytes; el maximo es {MAX_DATOS}")
    cuerpo = struct.pack(
        _FORMATO_ENCABEZADO,
        VERSION, t.tipo, t.destino, t.origen, t.secuencia, t.fragmento, len(t.datos),
    ) + bytes(t.datos)
    crc = binascii.crc_hqx(cuerpo, 0)
    return SINCRONIA + cuerpo + struct.pack(">H", crc)


# ----------------------------------------------------------------- decodificar
class Decodificador:
    """
    Maquina de recepcion. Se le entregan bytes con alimentar() y devuelve las
    tramas completas y validas que aparezcan.

    Regla de oro: cuando algo no cuadra (version distinta, longitud absurda o
    CRC malo) NO se tira todo el buffer; se descarta solo el primer byte y se
    vuelve a buscar la sincronia. Asi, si dentro de lo "malo" venia el inicio
    de una trama buena, se recupera.
    """

    def __init__(self):
        self.buf = bytearray()

    def alimentar(self, datos: bytes) -> list:
        self.buf.extend(datos)
        return self._extraer()

    def pendiente(self) -> bool:
        return len(self.buf) > 0

    def expirar(self) -> list:
        """
        Se llama cuando dejaron de llegar bytes (timeout entre bytes). Lo que
        esperaba la trama en curso ya no va a llegar: se abandona y se rescata
        cualquier trama completa que haya quedado dentro del buffer.
        """
        tramas = []
        while self.buf and not tramas:
            del self.buf[0]
            tramas = self._extraer()
        return tramas

    def _extraer(self) -> list:
        tramas = []
        while True:
            i = self.buf.find(SINCRONIA)
            if i == -1:
                # Un 0xAA al final podria ser la mitad de una sincronia.
                if self.buf.endswith(b"\xAA"):
                    self.buf[:] = b"\xAA"
                else:
                    self.buf.clear()
                break
            if i > 0:
                del self.buf[:i]  # basura antes de la sincronia

            if len(self.buf) < len(SINCRONIA) + TAM_ENCABEZADO:
                break  # falta encabezado

            version = self.buf[2]
            longitud = self.buf[10]
            if version != VERSION or longitud > MAX_DATOS:
                del self.buf[0]  # falso arranque
                continue

            total = len(SINCRONIA) + TAM_ENCABEZADO + longitud + 2
            if len(self.buf) < total:
                break  # falta el resto de la trama

            cuerpo = bytes(self.buf[2:total - 2])
            (crc_recibido,) = struct.unpack(">H", self.buf[total - 2:total])
            if binascii.crc_hqx(cuerpo, 0) != crc_recibido:
                del self.buf[0]  # trama danada
                continue

            _, tipo, destino, origen, seq, frag, _ = struct.unpack(
                _FORMATO_ENCABEZADO, cuerpo[:TAM_ENCABEZADO]
            )
            tramas.append(Trama(tipo, destino, origen, seq, cuerpo[TAM_ENCABEZADO:], frag))
            del self.buf[:total]
        return tramas


# --------------------------------------------------------------------- adaptador
class DispositivoLuzAdaptador:
    """
    Envia y recibe tramas por el Faro.

    puerto:       objeto tipo pyserial (write, read, in_waiting, flush,
                  reset_input_buffer).
    direccion:    direccion fisica de este Faro (2 bytes, rotable).
    timeout_byte: segundos sin bytes antes de abandonar una trama a medias.
    guarda_eco:   segundos tras transmitir antes de limpiar el reflejo propio.
    retardo_ack:  pausa del esclavo antes de contestar, para que el otro Faro
                  termine de limpiar su eco. Debe ser mayor que guarda_eco.
    """

    def __init__(self, puerto, direccion, *, timeout_byte=0.3,
                 guarda_eco=0.01, retardo_ack=0.05):
        self.puerto = puerto
        self.direccion = direccion
        self.timeout_byte = timeout_byte
        self.guarda_eco = guarda_eco
        self.retardo_ack = retardo_ack
        self._dec = Decodificador()
        self._cola = deque()
        self._seq = 0
        self._ultima_seq = {}  # origen -> ultima secuencia entregada (duplicados)

    @classmethod
    def abrir(cls, ruta="/dev/serial0", baudios=1200, direccion=None, **kw):
        """Abre un puerto serie real (8N1) y crea el adaptador."""
        import serial  # solo se necesita en el Faro real
        puerto = serial.Serial(ruta, baudios, bytesize=8, parity="N",
                               stopbits=1, timeout=0.05)
        return cls(puerto, direccion if direccion is not None else generar_direccion(), **kw)

    # -- nivel bajo ---------------------------------------------------------
    def enviar(self, trama: Trama) -> int:
        """Transmite una trama. Devuelve cuantos bytes salieron por la luz."""
        datos = codificar(trama)
        self.puerto.write(datos)
        self.puerto.flush()  # espera a que el ultimo bit haya salido
        # Semidúplex: el receptor puede haber visto el reflejo de nuestro LED.
        # Se ignora todo lo que llego mientras transmitiamos.
        time.sleep(self.guarda_eco)
        self.puerto.reset_input_buffer()
        self._dec = Decodificador()
        self._cola.clear()
        return len(datos)

    def recibir(self, timeout=None):
        """Devuelve la siguiente trama valida, o None si vence el timeout."""
        ahora = time.monotonic()
        limite = None if timeout is None else ahora + timeout
        ultimo_byte = ahora
        while True:
            if self._cola:
                return self._cola.popleft()

            datos = self.puerto.read(max(1, self.puerto.in_waiting))
            ahora = time.monotonic()
            if datos:
                ultimo_byte = ahora
                self._cola.extend(self._dec.alimentar(datos))
            elif self._dec.pendiente() and ahora - ultimo_byte > self.timeout_byte:
                self._cola.extend(self._dec.expirar())
                ultimo_byte = ahora

            if not self._cola and limite is not None and ahora >= limite:
                return None

    # -- acceso al medio: maestro / esclavo ---------------------------------
    def _siguiente_secuencia(self) -> int:
        self._seq = (self._seq + 1) % 256
        return self._seq

    def enviar_con_ack(self, destino, datos, *, tipo=TIPO_DATOS, fragmento=0,
                       reintentos=3, espera_ack=1.0) -> bool:
        """
        Papel de MAESTRO. Envia y espera el ACK con la misma secuencia.
        Si no llega, reintenta. Devuelve True si fue confirmada.
        La secuencia es la misma en todos los reintentos: asi el receptor
        puede detectar que es un duplicado.
        """
        trama = Trama(tipo, destino, self.direccion, self._siguiente_secuencia(),
                      bytes(datos), fragmento)
        for _ in range(reintentos):
            self.enviar(trama)
            limite = time.monotonic() + espera_ack
            while (restante := limite - time.monotonic()) > 0:
                r = self.recibir(timeout=restante)
                if (r and r.tipo == TIPO_ACK and r.secuencia == trama.secuencia
                        and r.origen == destino):
                    return True
        return False

    def recibir_y_confirmar(self, timeout=None):
        """
        Papel de ESCLAVO. Espera una trama de datos dirigida a este Faro (o a
        difusion), contesta ACK y la entrega. Los duplicados (el maestro
        reintento porque se perdio el ACK) se confirman otra vez pero no se
        entregan dos veces.
        """
        limite = None if timeout is None else time.monotonic() + timeout
        while True:
            restante = None if limite is None else max(0.0, limite - time.monotonic())
            t = self.recibir(timeout=restante)
            if t is None:
                return None
            if t.tipo == TIPO_ACK or t.destino not in (self.direccion, DIFUSION):
                continue

            duplicada = self._ultima_seq.get(t.origen) == t.secuencia
            if t.destino == self.direccion:  # a la difusion no se contesta
                time.sleep(self.retardo_ack)
                self.enviar(Trama(TIPO_ACK, t.origen, self.direccion, t.secuencia))
            if duplicada:
                continue
            self._ultima_seq[t.origen] = t.secuencia
            return t

    # -- mensajes de cualquier tamano (fragmentacion) ------------------------
    def enviar_mensaje(self, destino, datos, *, tipo=TIPO_DATOS, **kw) -> bool:
        """
        MAESTRO. Parte el mensaje en tramas de hasta 115 bytes de datos y
        envia cada una con ACK. Campo Fragmento: bit 7 = "vienen mas",
        bits 0-6 = indice (maximo 128 fragmentos, o sea 14 720 bytes).
        Devuelve True solo si todos los fragmentos fueron confirmados.
        """
        datos = bytes(datos)
        trozos = [datos[i:i + MAX_DATOS] for i in range(0, len(datos), MAX_DATOS)] or [b""]
        if len(trozos) > 128:
            raise ValueError(f"mensaje de {len(datos)} bytes; el maximo es {128 * MAX_DATOS}")
        for i, trozo in enumerate(trozos):
            hay_mas = 0x80 if i < len(trozos) - 1 else 0
            if not self.enviar_con_ack(destino, trozo, tipo=tipo, fragmento=i | hay_mas, **kw):
                return False
        return True

    def recibir_mensaje(self, timeout=None, timeout_fragmento=10.0):
        """
        ESCLAVO. Espera un mensaje completo, reensamblando sus fragmentos.
        Devuelve una Trama con todos los datos juntos, o None si vence el
        timeout o falta un fragmento (se abandona el mensaje a medias).
        """
        en_curso = {}  # origen -> [tipo, datos, indice esperado]
        espera = timeout
        while True:
            t = self.recibir_y_confirmar(espera)
            if t is None:
                return None
            indice, hay_mas = t.fragmento & 0x7F, bool(t.fragmento & 0x80)
            if indice == 0:
                en_curso[t.origen] = [t.tipo, bytearray(), 0]
            est = en_curso.get(t.origen)
            if est is None or est[2] != indice:  # fragmento fuera de orden
                en_curso.pop(t.origen, None)
                espera = timeout
                continue
            est[1] += t.datos
            est[2] += 1
            if not hay_mas:
                return Trama(est[0], t.destino, t.origen, t.secuencia, bytes(est[1]), 0)
            espera = timeout_fragmento
