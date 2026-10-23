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

from faronet.mesh import cebolla


from faronet.mesh.cebolla import (MAX_MENSAJE, MAX_MENSAJE_FIRMADO, MAX_PUBLICO, SALTOS, TAM_BLOQUE, TAM_CUERPO, TAM_SALTO, TAM_SELLADO,
                     Cebolla, CebollaError, construir_celda)
from faronet.mesh.identidad import Identidad
from faronet.mesh.paquete import TIPO_CEBOLLA, TIPO_MENSAJE


class Red:
    """Mesh falsa: todos se alcanzan; guarda cada carga que vio cada nodo."""

    def __init__(self, nombres):
        self.nodos = {}
        self.manipular = None   # f(quien_envia, destino, carga) -> carga: simula un relay malicioso
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
        if tipo == TIPO_CEBOLLA and self.red.manipular is not None:
            carga = self.red.manipular(self.nombre, destino, carga)
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

    def enviar(self, texto=b"hola desde la cebolla", ruta=None, remitente=None, tema=None):
        cuerpo = construir_celda(ruta or self.ruta, self.dest, texto, remitente, tema)
        correr(self.n["origen"].enviar_enrutado(TIPO_CEBOLLA, "R1", cuerpo))
        return cuerpo

    def mensajes(self):
        return [c for t, c in self.n["destino-secreto"].vio if t == TIPO_MENSAJE]

    def test_el_mensaje_llega_al_destino(self):
        self.enviar()
        self.assertEqual(len(self.mensajes()), 1)

    def recibidos(self):
        """Captura lo que el destino entrega a la aplicacion."""
        salida = []
        self.n["destino-secreto"].cebolla.al_recibir = lambda r, tx, v: salida.append((r, tx, v))
        return salida

    def test_el_destino_lo_puede_abrir(self):
        salida = self.recibidos()
        self.enviar(b"mensaje de prueba")
        self.assertEqual(salida, [(None, b"mensaje de prueba", False)])

    def test_mensaje_firmado_llega_con_remitente_verificado(self):
        salida = self.recibidos()
        self.enviar(b"soy yo", remitente=self.n["origen"].identidad)
        self.assertEqual(salida, [("origen", b"soy yo", True)])

    def test_se_puede_contestar_a_un_mensaje_firmado(self):
        salida = self.recibidos()
        self.enviar(b"ida", remitente=self.n["origen"].identidad)
        remitente = salida[0][0]
        # El destino contesta con otro envio anonimo normal hacia ese nombre.
        respuestas = []
        self.n["origen"].cebolla.al_recibir = lambda r, tx, v: respuestas.append((r, tx, v))
        ruta = [self.n[x].identidad.ficha() for x in ("R2", "R3", "R4")]
        cuerpo = construir_celda(ruta, self.n[remitente].identidad.ficha(), b"vuelta",
                                 self.n["destino-secreto"].identidad)
        correr(self.n["destino-secreto"].enviar_enrutado(TIPO_CEBOLLA, "R2", cuerpo))
        self.assertEqual(respuestas, [("destino-secreto", b"vuelta", True)])

    def test_firma_suplantada_es_rechazada(self):
        salida = self.recibidos()
        # Un impostor dice llamarse "origen" pero usa las llaves de R4.
        impostor = Identidad("origen", self.n["R4"].identidad.firma, self.n["R4"].identidad.cifrado)
        self.enviar(b"soy origen, creeme", remitente=impostor)
        self.assertEqual(salida, [])

    def test_remitente_fuera_del_directorio_se_descarta(self):
        salida = self.recibidos()
        self.n["destino-secreto"].conocidos.pop("origen")   # sin --directorio no hay como consultarlo
        self.enviar(b"quien soy", remitente=self.n["origen"].identidad)
        self.assertEqual(salida, [])

    def test_mensaje_firmado_cabe_el_maximo_y_no_mas(self):
        salida = self.recibidos()
        self.enviar(b"f" * MAX_MENSAJE_FIRMADO, remitente=self.n["origen"].identidad)
        self.assertEqual(len(salida), 1)
        with self.assertRaises(CebollaError):
            construir_celda(self.ruta, self.dest, b"f" * (MAX_MENSAJE_FIRMADO + 1),
                            self.n["origen"].identidad)

    def test_ningun_relay_ve_el_remitente_del_mensaje_firmado(self):
        self.enviar(remitente=self.n["origen"].identidad)
        for nombre in ("R1", "R2", "R3"):
            visto = b"".join(c for _, c in self.n[nombre].vio)
            self.assertNotIn(b"origen", visto)
        self.assertNotIn(b"origen", self.mensajes()[0])   # ni el sellado que sale del ultimo

    def test_mensaje_firmado_mide_igual_que_el_anonimo(self):
        self.enviar(b"a")
        self.enviar(b"a", remitente=self.n["origen"].identidad)
        self.assertEqual(len({len(c) for t, _, c in self.red.trafico if t == TIPO_MENSAJE}), 1)
        self.assertEqual(len({len(c) for t, _, c in self.red.trafico if t == TIPO_CEBOLLA}), 1)

    # ---- rotacion de llaves de cifrado (secreto hacia adelante)
    def renovar_directorio(self):
        """Cada nodo vuelve a 'consultar el directorio': fichas con las llaves vigentes."""
        for nodo in self.n.values():
            nodo.conocidos = {m.nombre: m.identidad.ficha() for m in self.n.values()}

    def test_la_ficha_cambia_al_rotar_pero_la_firma_no(self):
        ident = self.n["R1"].identidad
        antes = ident.ficha()
        ident.rotar()
        despues = ident.ficha()
        self.assertNotEqual(antes.llave_cifrado, despues.llave_cifrado)
        self.assertEqual(antes.llave_firma, despues.llave_firma)
        despues.verificar()   # sigue firmada y valida

    def test_celda_en_camino_sobrevive_a_una_rotacion(self):
        cuerpo = construir_celda(self.ruta, self.dest, b"ya iba en camino")
        for x in ("R1", "R2", "R3", "destino-secreto"):
            self.n[x].identidad.rotar()   # con la gracia normal
        correr(self.n["origen"].enviar_enrutado(TIPO_CEBOLLA, "R1", cuerpo))
        self.assertEqual(len(self.mensajes()), 1)

    def test_llave_olvidada_ya_no_abre_celdas_capturadas_antes(self):
        capturada = construir_celda(self.ruta, self.dest, b"capturada antes de rotar")
        for x in ("R1", "R2", "R3"):
            self.n[x].identidad.rotar(gracia=0)   # la vieja se olvida enseguida
        # Aunque el atacante robe ahora el nodo, ya no hay llave para esa celda:
        correr(self.n["origen"].enviar_enrutado(TIPO_CEBOLLA, "R1", capturada))
        self.assertEqual(self.mensajes(), [])

    def test_sellado_capturado_no_se_abre_tras_olvidar_la_llave_del_destino(self):
        salida = self.recibidos()
        self.enviar(b"mensaje viejo")
        sellado = self.mensajes()[0]
        self.n["destino-secreto"].identidad.rotar(gracia=0)
        self.n["destino-secreto"].cebolla.vistos.clear()
        correr(self.n["destino-secreto"].cebolla.recibir(TIPO_MENSAJE, sellado))
        self.assertEqual(len(salida), 1)   # solo el original; la copia ya no se abre

    def test_con_fichas_nuevas_todo_sigue_funcionando(self):
        for x in ("R1", "R2", "R3", "destino-secreto"):
            self.n[x].identidad.rotar(gracia=0)
        self.renovar_directorio()
        ruta = [self.n[x].identidad.ficha() for x in ("R1", "R2", "R3")]
        self.enviar(b"ficha nueva", ruta=ruta)
        # self.dest es la ficha vieja del destino: se usa la nueva
        cuerpo = construir_celda(ruta, self.n["destino-secreto"].identidad.ficha(), b"con llaves nuevas")
        salida = self.recibidos()
        correr(self.n["origen"].enviar_enrutado(TIPO_CEBOLLA, "R1", cuerpo))
        self.assertEqual(salida, [(None, b"con llaves nuevas", False)])

    def test_remitente_que_roto_su_llave_se_verifica_tras_refrescar(self):
        salida = self.recibidos()
        destino = self.n["destino-secreto"]
        self.n["origen"].identidad.rotar()    # el destino tiene en cache la ficha vieja
        async def refrescar():
            destino.conocidos = {m.nombre: m.identidad.ficha() for m in self.n.values()}
        destino.cebolla.refrescar_directorio = refrescar
        self.enviar(b"con llave nueva", remitente=self.n["origen"].identidad)
        self.assertEqual(salida, [("origen", b"con llave nueva", True)])

    def test_la_llave_de_cifrado_no_se_guarda_en_disco(self):
        ruta = os.path.join(tempfile.mkdtemp(), "X.llaves")
        a = Identidad.cargar_o_crear("X", ruta)
        self.assertEqual(os.path.getsize(ruta), 32)   # solo la semilla de firma
        b = Identidad.cargar_o_crear("X", ruta)
        self.assertEqual(bytes(a.firma), bytes(b.firma))      # la identidad persiste
        self.assertNotEqual(bytes(a.cifrado), bytes(b.cifrado))   # la de cifrado, no

    def test_archivo_de_llaves_viejo_de_64_bytes_se_sigue_leyendo(self):
        ruta = os.path.join(tempfile.mkdtemp(), "V.llaves")
        semilla = os.urandom(32)
        with open(ruta, "wb") as f:
            f.write(semilla + os.urandom(32))
        self.assertEqual(bytes(Identidad.cargar_o_crear("V", ruta).firma), semilla)

    def test_olvidar_descarta_las_llaves_viejas(self):
        ident = self.n["R1"].identidad
        ident.rotar()
        ident.rotar()
        self.assertEqual(len(ident.llaves_cifrado()), 3)
        ident.olvidar()
        self.assertEqual(len(ident.llaves_cifrado()), 1)

    def publicos(self):
        """Captura lo que el destino (gateway) publicaria en clearnet."""
        salida = []
        self.n["destino-secreto"].cebolla.al_recibir_publico = lambda tema, tx: salida.append((tema, tx))
        return salida

    def test_mensaje_publico_llega_al_gateway_con_su_tema(self):
        salida, privados = self.publicos(), self.recibidos()
        self.enviar(b"hola mundo", tema="general")
        self.assertEqual(salida, [("general", b"hola mundo")])
        self.assertEqual(privados, [])   # un publico no se entrega como privado

    def test_mensaje_privado_nunca_llega_al_gancho_publico(self):
        salida = self.publicos()
        self.enviar(b"secreto")
        self.enviar(b"firmado", remitente=self.n["origen"].identidad)
        self.assertEqual(salida, [])

    def test_nodo_que_no_es_gateway_no_publica_nada(self):
        self.enviar(b"hola", tema="general")   # el gancho por defecto solo descarta
        self.assertEqual(len(self.mensajes()), 1)

    def test_mensaje_publico_no_puede_llevar_remitente(self):
        with self.assertRaises(CebollaError):
            construir_celda(self.ruta, self.dest, b"x", self.n["origen"].identidad, "general")

    def test_tema_invalido_se_rechaza(self):
        for tema in ("", "a" * 17):
            with self.assertRaises(CebollaError):
                construir_celda(self.ruta, self.dest, b"x", None, tema)

    def test_mensaje_publico_cabe_el_maximo_y_no_mas(self):
        salida = self.publicos()
        self.enviar(b"p" * MAX_PUBLICO, tema="general")
        self.assertEqual(len(salida), 1)
        with self.assertRaises(CebollaError):
            construir_celda(self.ruta, self.dest, b"p" * (MAX_PUBLICO + 1), None, "general")

    def test_ningun_relay_ve_el_tema_ni_el_texto_publico(self):
        self.enviar(b"texto-publico-visible", tema="general")
        for nombre in ("R1", "R2", "R3"):
            visto = b"".join(c for _, c in self.n[nombre].vio)
            self.assertNotIn(b"general", visto)
            self.assertNotIn(b"texto-publico", visto)
        self.assertNotIn(b"general", self.mensajes()[0])

    def test_mensaje_publico_mide_igual_que_los_demas(self):
        self.enviar(b"a")
        self.enviar(b"a", tema="general")
        self.assertEqual(len({len(c) for t, _, c in self.red.trafico if t == TIPO_MENSAJE}), 1)
        self.assertEqual(len({len(c) for t, _, c in self.red.trafico if t == TIPO_CEBOLLA}), 1)

    def test_sellado_repetido_en_el_destino_se_ignora(self):
        salida = self.recibidos()
        self.enviar(b"una vez")
        sellado = self.mensajes()[0]
        correr(self.n["destino-secreto"].cebolla.recibir(TIPO_MENSAJE, sellado))  # copia del relay
        self.assertEqual(len(salida), 1)

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

    def test_la_celda_lleva_version(self):
        cuerpo = construir_celda(self.ruta, self.dest, b"x")
        self.assertEqual(cuerpo[0], cebolla.VERSION_CEBOLLA)
        self.assertEqual(len(cuerpo), TAM_CUERPO)

    def test_version_desconocida_se_descarta(self):
        cuerpo = bytearray(construir_celda(self.ruta, self.dest, b"version rara"))
        cuerpo[0] = 99
        correr(self.n["origen"].enviar_enrutado(TIPO_CEBOLLA, "R1", bytes(cuerpo)))
        self.assertEqual(self.mensajes(), [])
        self.assertEqual([d for _, d, _ in self.red.trafico], ["R1"])  # R1 no reenvio

    def test_geometria_de_la_celda(self):
        self.assertEqual(TAM_BLOQUE, SALTOS * TAM_SALTO + TAM_SELLADO)
        self.assertLessEqual(1 + 16 + TAM_CUERPO, 504)  # cabe en un paquete de 512

    def _alterar(self, quien, posicion):
        """Un relay malicioso (quien) cambia un bit de la celda que reenvia."""
        def manipular(nombre, destino, carga):
            if nombre != quien:
                return carga
            b = bytearray(carga)
            b[posicion] ^= 0x01
            return bytes(b)
        self.red.manipular = manipular

    def test_relay_que_altera_la_celda_es_detectado_por_el_siguiente(self):
        # Posiciones: efimera, MAC, primer byte del bloque, medio, y el RELLENO final.
        posiciones = [1, 33, 41, 200, TAM_CUERPO - 1, TAM_CUERPO - 20]
        for quien, esperado in (("origen", ["R1"]), ("R1", ["R1", "R2"]),
                                ("R2", ["R1", "R2", "R3"])):
            for pos in posiciones:
                with self.subTest(quien=quien, posicion=pos):
                    self.red.trafico.clear()
                    self._alterar(quien, pos)
                    self.enviar(b"marcada")
                    self.assertEqual(self.mensajes(), [])
                    # La celda no pasa del primer relay honesto que la recibe.
                    self.assertEqual([d for _, d, _ in self.red.trafico], esperado)

    def test_sin_alteracion_el_mismo_recorrido_funciona(self):
        self.red.manipular = lambda nombre, destino, carga: carga
        self.enviar(b"limpia")
        self.assertEqual(len(self.mensajes()), 1)

    def test_llave_efimera_invalida_no_rompe_el_relay(self):
        cuerpo = bytes([cebolla.VERSION_CEBOLLA]) + bytes(32) + bytes(8) + os.urandom(TAM_BLOQUE)
        correr(self.n["origen"].enviar_enrutado(TIPO_CEBOLLA, "R1", cuerpo))
        self.assertEqual(self.mensajes(), [])


if __name__ == "__main__":
    unittest.main()