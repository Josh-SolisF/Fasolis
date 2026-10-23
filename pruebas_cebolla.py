"""
pruebas_cebolla.py - Pruebas de la cebolla.   Ejecutar:  python3 pruebas_cebolla.py -v

Usan una "mesh" falsa en memoria: cada nodo falso entrega los paquetes
enrutados directamente al nodo de destino y anota lo que cada uno vio. Asi se
prueba la logica criptografica sin TCP ni directorio.
"""
import asyncio
import contextlib
import io
import os
import tempfile
import unittest

import cebolla
from cebolla import (LARGO_FINAL, MAX_MENSAJE, SALTOS, TAM_CUERPO, TAM_SELLADO, Cebolla,
                     CebollaError, construir_celda)
from identidad import Identidad
from paquete import TIPO_CEBOLLA, TIPO_MENSAJE


class Red:
    """Mesh falsa: todos se alcanzan; guarda cada carga que vio cada nodo."""

    def __init__(self, nombres):
        self.nodos = {}
        for n in nombres:
            ruta = os.path.join(tempfile.mkdtemp(), n + ".llaves")
            self.nodos[n] = NodoFalso(n, Identidad.cargar_o_crear(n, ruta), self)
        for n in self.nodos.values():
            n.conocidos = {m.nombre: m.identidad.ficha() for m in self.nodos.values()}
        self.trafico = []  # (tipo, destino, carga)


class NodoFalso:
    def __init__(self, nombre, identidad, red):
        self.nombre, self.identidad, self.red = nombre, identidad, red
        self.directorio = None
        self.conocidos = {}
        self.vio = []        # cargas que este nodo recibio
        self.cebolla = Cebolla(self)

    def log(self, texto):
        pass

    def ruta_a(self, destino):
        return object() if destino in self.red.nodos else None

    async def enviar_enrutado(self, tipo, destino, carga, ttl=16):
        self.red.trafico.append((tipo, destino, carga))
        if destino in self.red.nodos:
            receptor = self.red.nodos[destino]
            receptor.vio.append((tipo, carga))
            await receptor.cebolla.recibir(tipo, carga)


def correr(coro):
    return asyncio.run(coro)


class PruebasCebolla(unittest.TestCase):
    def setUp(self):
        # el destino imprime cada mensaje recibido; en las pruebas se silencia
        silencio = contextlib.redirect_stdout(io.StringIO())
        silencio.__enter__()
        self.addCleanup(silencio.__exit__, None, None, None)
        self.red = Red(["origen", "R1", "R2", "R3", "R4", "destino-secreto"])
        self.n = self.red.nodos
        self.ruta = [self.n[x].identidad.ficha() for x in ("R1", "R2", "R3")]
        self.dest = self.n["destino-secreto"].identidad.ficha()

    def enviar(self, texto=b"hola desde la cebolla", ruta=None):
        cuerpo = construir_celda(ruta or self.ruta, self.dest, texto)
        correr(self.n["origen"].enviar_enrutado(TIPO_CEBOLLA, "R1", cuerpo))
        return cuerpo

    def mensajes(self):
        return [c for t, c in self.n["destino-secreto"].vio if t == TIPO_MENSAJE]

    def test_el_mensaje_llega_al_destino(self):
        self.enviar()
        self.assertEqual(len(self.mensajes()), 1)

    def test_el_destino_lo_puede_abrir(self):
        salida = []
        self.n["destino-secreto"].cebolla.abrir_mensaje = lambda s: salida.append(
            cebolla.quitar_relleno(
                cebolla.SealedBox(self.n["destino-secreto"].identidad.cifrado).decrypt(s)))
        self.enviar(b"mensaje de prueba")
        self.assertEqual(salida, [b"mensaje de prueba"])

    def test_solo_el_destinatario_puede_abrirlo(self):
        self.enviar()
        sellado = self.mensajes()[0]
        otro = self.n["R4"].identidad.cifrado
        with self.assertRaises(cebolla.CryptoError):
            cebolla.SealedBox(otro).decrypt(sellado)

    def test_recorrido_por_los_tres_relays_en_orden(self):
        self.enviar()
        destinos = [d for _, d, _ in self.red.trafico]
        self.assertEqual(destinos, ["R1", "R2", "R3", "destino-secreto"])

    def test_todas_las_celdas_miden_lo_mismo(self):
        self.enviar(b"x")
        self.enviar(b"y" * MAX_MENSAJE)
        celdas = [len(c) for t, _, c in self.red.trafico if t == TIPO_CEBOLLA]
        self.assertEqual(set(celdas), {TAM_CUERPO})
        mensajes = [len(c) for t, _, c in self.red.trafico if t == TIPO_MENSAJE]
        self.assertEqual(set(mensajes), {TAM_SELLADO})

    def test_ningun_relay_ve_origen_y_destino_a_la_vez(self):
        self.enviar()
        for nombre in ("R1", "R2", "R3"):
            visto = b"".join(c for _, c in self.n[nombre].vio)
            # La carga que recibe un relay no contiene el origen ni el destino en claro...
            self.assertNotIn(b"destino-secreto", visto)
            self.assertNotIn(b"origen", visto)
        # ...y el nombre del destino solo lo conoce quien lo recibe como destino del paquete.
        quien_sabe = {d for _, d, _ in self.red.trafico}
        self.assertIn("destino-secreto", quien_sabe)

    def test_ultimo_relay_no_ve_el_origen(self):
        self.enviar()
        visto = b"".join(c for _, c in self.n["R3"].vio)
        self.assertNotIn(b"origen", visto)

    def test_celdas_distintas_aunque_se_mande_el_mismo_mensaje(self):
        a = self.enviar(b"igual")
        b = self.enviar(b"igual")
        self.assertNotEqual(a, b)

    def test_repeticion_es_descartada(self):
        cuerpo = construir_celda(self.ruta, self.dest, b"una sola vez")
        for _ in range(2):
            correr(self.n["origen"].enviar_enrutado(TIPO_CEBOLLA, "R1", cuerpo))
        self.assertEqual(len(self.mensajes()), 1)

    def test_celda_alterada_no_llega(self):
        cuerpo = bytearray(construir_celda(self.ruta, self.dest, b"no tocar"))
        cuerpo[100] ^= 0x01  # un relay malicioso cambia un bit del bloque
        correr(self.n["origen"].enviar_enrutado(TIPO_CEBOLLA, "R1", bytes(cuerpo)))
        self.assertEqual(self.mensajes(), [])

    def test_celda_de_tamano_incorrecto_se_descarta(self):
        correr(self.n["origen"].enviar_enrutado(TIPO_CEBOLLA, "R1", b"x" * 10))
        self.assertEqual(self.mensajes(), [])

    def test_relay_equivocado_no_puede_pelar_la_celda(self):
        cuerpo = construir_celda(self.ruta, self.dest, b"para R1")
        correr(self.n["origen"].enviar_enrutado(TIPO_CEBOLLA, "R4", cuerpo))  # llega a R4, no a R1
        self.assertEqual(self.mensajes(), [])

    def test_mensaje_demasiado_largo(self):
        with self.assertRaises(CebollaError):
            construir_celda(self.ruta, self.dest, b"x" * (MAX_MENSAJE + 1))

    def test_mensaje_del_largo_maximo_y_vacio(self):
        self.enviar(b"z" * MAX_MENSAJE)
        self.enviar(b"")
        self.assertEqual(len(self.mensajes()), 2)

    def test_ruta_con_menos_relays_se_rechaza(self):
        with self.assertRaises(CebollaError):
            construir_celda(self.ruta[:2], self.dest, b"x")

    def test_elegir_ruta_excluye_origen_y_destino(self):
        for _ in range(30):
            ruta, dest = self.n["origen"].cebolla.elegir_ruta("destino-secreto")
            nombres = {f.nombre for f in ruta}
            self.assertEqual(len(nombres), SALTOS)
            self.assertFalse(nombres & {"origen", "destino-secreto"})

    def test_elegir_ruta_sin_suficientes_relays(self):
        chica = Red(["a", "b", "c", "d"])
        with self.assertRaises(CebollaError):
            chica.nodos["a"].cebolla.elegir_ruta("d")

    def test_geometria_de_la_celda(self):
        self.assertEqual(LARGO_FINAL, 342)
        self.assertLessEqual(1 + 16 + TAM_CUERPO, 504)  # cabe en un paquete de 512


if __name__ == "__main__":
    unittest.main()