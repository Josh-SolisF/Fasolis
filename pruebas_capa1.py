"""
pruebas_capa1.py - Pruebas de la Capa 1.   Ejecutar:  python3 pruebas_capa1.py -v

Usan un par de "puertos virtuales" en memoria (lo escrito en uno se lee en el
otro), asi que no hace falta hardware ni socat. Con pyserial y socat se puede
hacer lo mismo con puertos reales; ver el final de este archivo.
"""

import queue
import random
import threading
import unittest

from capa1 import (DIFUSION, MAX_DATOS, TAM_MAX_TRAMA, TIPO_DATOS, TIPO_EXTERNA,
                   Decodificador, DispositivoLuzAdaptador, Trama, codificar)


class PuertoVirtual:
    """Imita lo minimo de pyserial.Serial."""

    def __init__(self):
        self._rx = queue.Queue()
        self.otro = None
        self.corromper_escrituras = 0  # cuantas escrituras salen con un bit cambiado

    @classmethod
    def par(cls):
        a, b = cls(), cls()
        a.otro, b.otro = b, a
        return a, b

    def write(self, datos):
        datos = bytearray(datos)
        if self.corromper_escrituras > 0:
            self.corromper_escrituras -= 1
            datos[len(datos) // 2] ^= 0x01
        for x in datos:
            self.otro._rx.put(x)
        return len(datos)

    def read(self, n=1):
        salida = bytearray()
        try:
            salida.append(self._rx.get(timeout=0.02))
            while len(salida) < n:
                salida.append(self._rx.get_nowait())
        except queue.Empty:
            pass
        return bytes(salida)

    @property
    def in_waiting(self):
        return self._rx.qsize()

    def flush(self):
        pass

    def reset_input_buffer(self):
        while not self._rx.empty():
            self._rx.get_nowait()


A, B = 0xABCD, 0x1234
HOLA = Trama(TIPO_DATOS, B, A, 7, b"Hola")


def trama_larga(seq=1, n=100):
    return Trama(TIPO_DATOS, B, A, seq, bytes(range(n)))


class PruebasTrama(unittest.TestCase):
    def test_ejemplo_del_documento(self):
        esperado = bytes.fromhex("AA55 0101 1234 ABCD 07 00 04 486F6C61 0C71")
        self.assertEqual(codificar(HOLA), esperado)

    def test_ida_y_vuelta(self):
        self.assertEqual(Decodificador().alimentar(codificar(HOLA)), [HOLA])

    def test_tamano_maximo_es_128(self):
        t = Trama(TIPO_DATOS, B, A, 1, bytes(MAX_DATOS))
        self.assertEqual(len(codificar(t)), TAM_MAX_TRAMA)
        self.assertEqual(TAM_MAX_TRAMA, 128)
        with self.assertRaises(ValueError):
            codificar(Trama(TIPO_DATOS, B, A, 1, bytes(MAX_DATOS + 1)))

    def test_llegada_byte_por_byte(self):
        dec, salida = Decodificador(), []
        for x in codificar(HOLA):
            salida += dec.alimentar(bytes([x]))
        self.assertEqual(salida, [HOLA])


class PruebasRecuperacion(unittest.TestCase):
    """Las tres pruebas previstas: el receptor debe recuperarse con la siguiente trama."""

    def test_1_basura_antes_de_la_trama(self):
        basura = bytes([0x00, 0xFF, 0x13, 0xAA, 0x55, 0x09, 0xC8, 0xAA])  # incluye falsos arranques
        dec = Decodificador()
        self.assertEqual(dec.alimentar(basura + codificar(HOLA)), [HOLA])

    def test_1b_basura_aleatoria(self):
        rng = random.Random(42)
        dec, esperadas, salida = Decodificador(), [], []
        for i in range(50):
            salida += dec.alimentar(bytes(rng.randrange(256) for _ in range(rng.randrange(40))))
            t = Trama(TIPO_DATOS, B, A, i, bytes(rng.randrange(256) for _ in range(rng.randrange(60))))
            esperadas.append(t)
            salida += dec.alimentar(codificar(t))
        self.assertEqual(salida, esperadas)

    def test_2_trama_cortada_a_la_mitad_y_luego_otra_corta(self):
        primera = codificar(trama_larga(1))
        segunda = Trama(TIPO_DATOS, B, A, 2, b"ok")
        dec = Decodificador()
        # la segunda cabe dentro de lo que la primera "esperaba": solo se rescata al expirar
        self.assertEqual(dec.alimentar(primera[: len(primera) // 2] + codificar(segunda)), [])
        self.assertEqual(dec.expirar(), [segunda])

    def test_2b_trama_cortada_a_la_mitad_y_luego_otra_larga(self):
        primera = codificar(trama_larga(1))
        segunda = trama_larga(2)
        dec = Decodificador()
        # aqui el CRC de la primera falla y se rescata la segunda sin esperar timeout
        self.assertEqual(dec.alimentar(primera[: len(primera) // 2] + codificar(segunda)), [segunda])

    def test_3_un_bit_cambiado(self):
        mala = bytearray(codificar(HOLA))
        mala[13] ^= 0x04  # un bit dentro de los datos
        siguiente = Trama(TIPO_DATOS, B, A, 8, b"Chao")
        dec = Decodificador()
        self.assertEqual(dec.alimentar(bytes(mala)), [])
        self.assertEqual(dec.alimentar(codificar(siguiente)), [siguiente])

    def test_version_desconocida_se_descarta(self):
        v2 = bytearray(codificar(HOLA))
        v2[2] = 2
        dec = Decodificador()
        self.assertEqual(dec.alimentar(bytes(v2) + codificar(HOLA)), [HOLA])

    def test_longitud_mayor_a_115_se_descarta(self):
        falsa = bytearray(codificar(HOLA))
        falsa[10] = 200
        dec = Decodificador()
        self.assertEqual(dec.alimentar(bytes(falsa) + codificar(HOLA)), [HOLA])


class PruebasAdaptador(unittest.TestCase):
    def setUp(self):
        pa, pb = PuertoVirtual.par()
        self.pa, self.pb = pa, pb
        self.maestro = DispositivoLuzAdaptador(pa, A, timeout_byte=0.1)
        self.esclavo = DispositivoLuzAdaptador(pb, B, timeout_byte=0.1)

    def esclavo_en_hilo(self, timeout=3.0):
        resultado = []
        h = threading.Thread(
            target=lambda: resultado.append(self.esclavo.recibir_y_confirmar(timeout)))
        h.start()
        return h, resultado

    def test_envio_con_ack(self):
        h, res = self.esclavo_en_hilo()
        self.assertTrue(self.maestro.enviar_con_ack(B, b"Hola", espera_ack=1.0))
        h.join()
        self.assertEqual(res[0].datos, b"Hola")
        self.assertEqual(res[0].origen, A)

    def test_trama_externa_se_transporta_intacta(self):
        externa = bytes(random.Random(1).randrange(256) for _ in range(MAX_DATOS))
        h, res = self.esclavo_en_hilo()
        self.assertTrue(self.maestro.enviar_con_ack(B, externa, tipo=TIPO_EXTERNA))
        h.join()
        self.assertEqual(res[0].tipo, TIPO_EXTERNA)
        self.assertEqual(res[0].datos, externa)

    def test_reintento_si_el_primer_envio_llega_danado(self):
        self.pa.corromper_escrituras = 1  # el primer intento sale con un bit cambiado
        h, res = self.esclavo_en_hilo()
        self.assertTrue(self.maestro.enviar_con_ack(B, b"Hola", espera_ack=0.5))
        h.join()
        self.assertEqual(res[0].datos, b"Hola")

    def test_sin_esclavo_falla_tras_los_reintentos(self):
        self.assertFalse(self.maestro.enviar_con_ack(B, b"Hola", reintentos=2, espera_ack=0.1))

    def test_ack_perdido_no_duplica_la_entrega(self):
        # El esclavo recibe la misma trama dos veces (el maestro reintento).
        self.esclavo.retardo_ack = 0
        t = Trama(TIPO_DATOS, B, A, 5, b"unica")
        self.pa.write(codificar(t))
        self.pa.write(codificar(t))
        self.assertEqual(self.esclavo.recibir_y_confirmar(timeout=0.5).datos, b"unica")
        self.assertIsNone(self.esclavo.recibir_y_confirmar(timeout=0.3))  # el duplicado no se entrega

    def test_difusion_se_entrega_sin_ack(self):
        self.pa.write(codificar(Trama(TIPO_DATOS, DIFUSION, A, 1, b"todos")))
        self.assertEqual(self.esclavo.recibir_y_confirmar(timeout=0.5).datos, b"todos")
        self.assertEqual(self.pa.read(10), b"")  # nadie contesto

    def test_ignora_tramas_para_otro_faro(self):
        self.pa.write(codificar(Trama(TIPO_DATOS, 0x9999, A, 1, b"no es para ti")))
        self.assertIsNone(self.esclavo.recibir_y_confirmar(timeout=0.3))


if __name__ == "__main__":
    unittest.main()

# ---------------------------------------------------------------------------
# Con puertos reales (laptop con pyserial y socat), en dos terminales:
#   socat -d -d pty,raw,echo=0 pty,raw,echo=0     # imprime /dev/pts/X y /dev/pts/Y
#   A = DispositivoLuzAdaptador.abrir("/dev/pts/X", 1200, direccion=0xABCD)
#   B = DispositivoLuzAdaptador.abrir("/dev/pts/Y", 1200, direccion=0x1234)
# En la Pi solo cambia la ruta a /dev/serial0.
# ---------------------------------------------------------------------------
