"""
pruebas_scripts.py - Prueba enviar.py y recibir.py de punta a punta.

Se les inyecta un adaptador sobre puertos virtuales en vez de abrir
/dev/serial0, asi que no hace falta hardware.   Ejecutar:  python3 pruebas_scripts.py -v
"""

import contextlib
import io
import os
import tempfile
import threading
import unittest

import enviar
import recibir
from capa1 import DispositivoLuzAdaptador
from pruebas_capa1 import PuertoVirtual

A, B = 0xABCD, 0x1234


class PruebasScripts(unittest.TestCase):
    def setUp(self):
        pa, pb = PuertoVirtual.par()
        self.faro_a = DispositivoLuzAdaptador(pa, A, timeout_byte=0.1)
        self.faro_b = DispositivoLuzAdaptador(pb, B, timeout_byte=0.1)

    def recibir_en_hilo(self, argv):
        resultado = {}

        def tarea():
            salida, errores = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(salida), contextlib.redirect_stderr(errores):
                resultado["codigo"] = recibir.main(argv, adaptador=self.faro_b)
            resultado["stdout"], resultado["stderr"] = salida.getvalue(), errores.getvalue()

        h = threading.Thread(target=tarea)
        h.start()
        return h, resultado

    def enviar(self, argv):
        with contextlib.redirect_stderr(io.StringIO()):
            return enviar.main(argv, adaptador=self.faro_a)

    def test_texto_de_punta_a_punta(self):
        h, r = self.recibir_en_hilo(["--una", "--timeout", "5"])
        self.assertEqual(self.enviar(["1234", "--texto", "Hola FaroNet"]), 0)
        h.join()
        self.assertEqual(r["codigo"], 0)
        self.assertEqual(r["stdout"].strip(), "Hola FaroNet")

    def test_archivo_grande_a_archivo(self):
        with tempfile.TemporaryDirectory() as d:
            origen, destino = os.path.join(d, "in.bin"), os.path.join(d, "out.bin")
            contenido = bytes(range(256)) * 3  # 768 bytes, 7 fragmentos
            with open(origen, "wb") as f:
                f.write(contenido)
            h, r = self.recibir_en_hilo(["--una", "--timeout", "5", "--salida", destino])
            self.assertEqual(self.enviar(["1234", "--archivo", origen]), 0)
            h.join()
            with open(destino, "rb") as f:
                self.assertEqual(f.read(), contenido)

    def test_trama_externa_sale_en_hexadecimal(self):
        h, r = self.recibir_en_hilo(["--una", "--timeout", "5"])
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"\x01\x02\xff")
        try:
            self.assertEqual(self.enviar(["1234", "--archivo", f.name, "--externa"]), 0)
        finally:
            os.unlink(f.name)
        h.join()
        self.assertEqual(r["stdout"].strip(), "01 02 ff")
        self.assertIn("externa", r["stderr"])

    def test_sin_receptor_el_codigo_de_salida_es_1(self):
        self.assertEqual(self.enviar(["1234", "--texto", "x", "--reintentos", "1",
                                      "--espera-ack", "0.1"]), 1)

    def test_recibir_sin_mensajes_vence_el_timeout(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(recibir.main(["--una", "--timeout", "0.2"], adaptador=self.faro_b), 1)

    def test_difusion_llega_sin_ack(self):
        h, r = self.recibir_en_hilo(["--una", "--timeout", "5"])
        self.assertEqual(self.enviar(["FFFF", "--texto", "para todos"]), 0)
        h.join()
        self.assertEqual(r["stdout"].strip(), "para todos")

    def test_direccion_invalida_es_error_de_uso(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as e:
            enviar.main(["XYZ", "--texto", "x"], adaptador=self.faro_a)
        self.assertEqual(e.exception.code, 2)

    def test_archivo_inexistente(self):
        self.assertEqual(self.enviar(["1234", "--archivo", "/no/existe.bin"]), 2)


if __name__ == "__main__":
    unittest.main()
