"""
test_gateway.py - Pruebas del cliente del edge (gateway.py) y del buzon (buzon.py).

Levantan un servidor HTTP falso en localhost que imita al Worker, asi que no
hace falta Cloudflare ni red.   Ejecutar:  py -m unittest pruebas.test_gateway -v
"""
import hashlib
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

from faronet.herramientas import buzon, gateway


class WorkerFalso(HTTPServer):
    """Guarda lo que recibe y responde segun la ruta, como el Worker."""

    def __init__(self):
        super().__init__(("127.0.0.1", 0), ManejadorFalso)
        self.recibidas = []   # (metodo, ruta, encabezados, cuerpo)
        self.buzones = {}
        self.codigo_publicar = 204
        threading.Thread(target=self.serve_forever, args=(0.01,), daemon=True).start()

    def cerrar(self):
        self.shutdown()
        self.server_close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_port}"


class ManejadorFalso(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _responder(self, codigo, cuerpo=b""):
        self.send_response(codigo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _leer(self):
        return self.rfile.read(int(self.headers.get("Content-Length", "0")))

    def do_POST(self):
        cuerpo = self._leer()
        self.server.recibidas.append(("POST", self.path, dict(self.headers), cuerpo))
        if self.path == "/gateway/publicar":
            self._responder(self.server.codigo_publicar)
        elif self.path == "/gateway/directorio":
            n = len(json.loads(cuerpo)["fichas"])
            self._responder(200, json.dumps({"aceptadas": n, "rechazadas": []}).encode())
        else:
            self._responder(404)

    def do_PUT(self):
        cuerpo = self._leer()
        ident = self.path.rsplit("/", 1)[-1]
        if len(cuerpo) != buzon.TAM_BLOB:
            return self._responder(400)
        if ident in self.server.buzones:
            return self._responder(409)
        self.server.buzones[ident] = cuerpo
        self._responder(201)

    def do_GET(self):
        ident = self.path.rsplit("/", 1)[-1]
        blob = self.server.buzones.pop(ident, None)   # borra al leer
        self._responder(200, blob) if blob is not None else self._responder(404)


class PruebasGateway(unittest.TestCase):
    def setUp(self):
        self.worker = WorkerFalso()
        self.addCleanup(self.worker.cerrar)
        self.llave = SigningKey.generate()

    def test_publicar_firma_la_peticion_como_espera_el_worker(self):
        self.assertEqual(gateway.publicar(self.worker.url, self.llave, "general", "hola"), 204)
        _, ruta, enc, cuerpo = self.worker.recibidas[0]
        self.assertEqual(ruta, "/gateway/publicar")
        self.assertEqual(json.loads(cuerpo), {"tema": "general", "texto": "hola"})
        ts = enc["X-Faro-Ts"]
        esperado = f"POST\n/gateway/publicar\n{ts}\n{hashlib.sha256(cuerpo).hexdigest()}".encode()
        # Lo mismo que hace verificarGateway() en edge/src/auth.ts:
        self.llave.verify_key.verify(esperado, bytes.fromhex(enc["X-Faro-Firma"]))

    def test_la_firma_no_sirve_para_otro_cuerpo(self):
        gateway.publicar(self.worker.url, self.llave, "general", "hola")
        _, ruta, enc, cuerpo = self.worker.recibidas[0]
        alterado = f"POST\n{ruta}\n{enc['X-Faro-Ts']}\n{hashlib.sha256(cuerpo + b' ').hexdigest()}".encode()
        with self.assertRaises(BadSignatureError):
            self.llave.verify_key.verify(alterado, bytes.fromhex(enc["X-Faro-Firma"]))

    def test_publicar_devuelve_el_codigo_del_worker_y_0_sin_conexion(self):
        self.worker.codigo_publicar = 429
        self.assertEqual(gateway.publicar(self.worker.url, self.llave, "general", "x"), 429)
        self.assertEqual(gateway.publicar("http://127.0.0.1:1", self.llave, "general", "x"), 0)

    def test_publicar_fichas_las_manda_en_grupos_de_64(self):
        fichas = [f"{i:02x}" for i in range(150)]
        codigo, r = gateway.publicar_fichas(self.worker.url, self.llave, fichas)
        self.assertEqual((codigo, r["aceptadas"]), (200, 150))
        envios = [e for e in self.worker.recibidas if e[1] == "/gateway/directorio"]
        self.assertEqual([len(json.loads(e[3])["fichas"]) for e in envios], [64, 64, 22])


class PruebasBuzon(unittest.TestCase):
    def setUp(self):
        self.worker = WorkerFalso()
        self.addCleanup(self.worker.cerrar)

    def test_ida_y_vuelta_y_se_borra_al_leer(self):
        buzon.depositar(self.worker.url, "secreto-compartido-123", "hola, esto es privado".encode())
        self.assertEqual(buzon.recoger(self.worker.url, "secreto-compartido-123"),
                         "hola, esto es privado".encode())
        self.assertIsNone(buzon.recoger(self.worker.url, "secreto-compartido-123"))

    def test_el_worker_solo_ve_blobs_cifrados_de_tamano_fijo(self):
        buzon.depositar(self.worker.url, "otro-secreto-456", b"corto")
        buzon.depositar(self.worker.url, "otro-secreto-789", b"x" * 900)
        blobs = list(self.worker.buzones.values())
        self.assertEqual({len(b) for b in blobs}, {buzon.TAM_BLOB})
        self.assertTrue(all(b"corto" not in b for b in blobs))

    def test_el_id_no_revela_el_secreto_y_es_estable(self):
        ident, llave = buzon.derivar("mi-secreto-largo-000")
        self.assertEqual((ident, llave), buzon.derivar("mi-secreto-largo-000"))
        self.assertEqual(len(ident), 32)
        self.assertNotIn("mi-secreto", ident)
        self.assertNotEqual(ident, buzon.derivar("mi-secreto-largo-001")[0])

    def test_secreto_equivocado_no_abre_el_blob(self):
        _, llave = buzon.derivar("secreto-bueno-1234")
        blob = buzon.sellar(llave, b"confidencial")
        with self.assertRaises(buzon.BuzonError):
            buzon.abrir(buzon.derivar("secreto-malo-5678")[1], blob)

    def test_mensaje_demasiado_largo(self):
        with self.assertRaises(buzon.BuzonError):
            buzon.sellar(bytes(32), b"x" * (buzon.MAX_MENSAJE + 1))

    def test_no_deja_depositar_dos_veces_con_el_mismo_secreto(self):
        buzon.depositar(self.worker.url, "secreto-unico-1111", b"uno")
        with self.assertRaises(buzon.BuzonError):
            buzon.depositar(self.worker.url, "secreto-unico-1111", b"dos")


if __name__ == "__main__":
    unittest.main()
