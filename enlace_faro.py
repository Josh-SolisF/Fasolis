"""
enlace_faro.py - El Faro como un enlace mas de la mesh.

El nodo (nodo.py) habla con sus vecinos mediante un "reader" y un "writer"
de asyncio. Este modulo le entrega al nodo un par equivalente, pero en vez de
TCP los paquetes de 512 bytes viajan por luz con DispositivoLuzAdaptador.
Para el nodo, el Faro es un vecino mas: el mismo saludo, las mismas rutas.

Como DispositivoLuzAdaptador bloquea (time.sleep, lecturas del puerto) y el
nodo usa asyncio, el adaptador vive en su propio hilo. El nodo y el hilo se
comunican con una cola de salida y alimentando el StreamReader.

Acceso al medio (lo que hace el hilo, en orden):
  1. Si hay un paquete por enviar y el medio esta libre (no estan llegando
     bytes), lo envia como MAESTRO: enviar_mensaje() con ACK por fragmento.
  2. Si falla, espera un tiempo aleatorio y reintenta (evita que los dos
     Faros choquen siempre al mismo tiempo).
  3. Si no hay nada por enviar, escucha como ESCLAVO un rato corto.

Direccion del otro Faro: no hace falta configurarla. Mientras no se conoce,
los paquetes se envian a difusion (FFFF, sin ACK). Al recibir el primer
paquete se aprende la direccion de origen y desde ahi se usa ACK. Si el otro
Faro rota su direccion, los envios fallan, se vuelve a difusion y se aprende
la nueva. Con --faro-par se puede fijar a mano.
"""
from __future__ import annotations

import asyncio
import random
import threading
import time
from collections import deque

from capa1 import (DIFUSION, MAX_DATOS, TAM_ACK, TAM_MAX_TRAMA, TIPO_DATOS, Trama,
                   direccion_persistente)
from paquete import TAM_PAQUETE, TIPO_RUTAS, VERSION

ESCUCHA = 0.2           # segundos escuchando antes de revisar la cola de salida
REINTENTOS_PAQUETE = 3  # intentos completos por paquete (cada uno ya reintenta por fragmento)
MAX_COLA = 16           # paquetes en espera; si se llena, se descartan los nuevos


class EscritorFaro:
    """Imita asyncio.StreamWriter: lo escrito va a la cola del hilo del Faro."""

    def __init__(self, enlace: "EnlaceFaro", sesion: int):
        self._enlace = enlace
        self._sesion = sesion
        self._cerrado = False
        self.reenvio_hola = enlace.reenvio_hola  # nodo.py lo usa para repetir el saludo

    def write(self, datos: bytes):
        if not self._cerrado:
            for i in range(0, len(datos), TAM_PAQUETE):
                self._enlace.encolar(datos[i:i + TAM_PAQUETE])

    async def drain(self):
        pass

    def close(self):
        self._cerrado = True
        self._enlace.fin_sesion(self._sesion)

    def is_closing(self) -> bool:
        return self._cerrado


class EnlaceFaro:
    def __init__(self, adaptador, *, par: int | None = None, vida: float | None = None,
                 rotar_cada: float = 0.0, debug: bool = False, nombre: str = "faro"):
        self.adaptador = adaptador
        self.par = par                  # direccion fisica del otro Faro
        self.par_fijo = par is not None
        self.rotar_cada = rotar_cada    # segundos entre rotaciones de direccion (0 = nunca)
        self.debug = debug
        self.nombre = nombre
        self.reenvio_hola = max(5.0, 3 * self.t_paquete() + 2)
        # Sin noticias del otro Faro en 'vida' segundos, se da el enlace por caido.
        self.vida = vida if vida is not None else max(60.0, 6 * self.t_paquete())
        self.estadisticas = {"enviados": 0, "recibidos": 0, "fallidos": 0,
                             "descartados": 0, "rotaciones": 0}
        self._cola: deque[bytes] = deque()
        self._lock = threading.Lock()
        self._parar = threading.Event()
        self._rotar_ya = threading.Event()
        self._hilo: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lector: asyncio.StreamReader | None = None
        self._sesion = 0
        self._inicio_sesion = 0.0
        self._ultimo_rx = 0.0
        self._ultima_rotacion = time.monotonic()

    def log(self, texto: str):
        if self.debug:
            print(f"[{self.nombre}] (faro) {texto}")

    # -- tiempos ----------------------------------------------------------------
    def t_trama(self) -> float:
        """Lo que tarda una trama maxima en ir y su ACK en volver."""
        a = self.adaptador
        return (TAM_MAX_TRAMA + TAM_ACK) * a.t_byte + a.retardo_ack + a.guarda_eco

    def t_paquete(self) -> float:
        """Lo que tarda un paquete de la mesh completo (todos sus fragmentos)."""
        fragmentos = -(-TAM_PAQUETE // MAX_DATOS)
        return fragmentos * self.t_trama()

    # -- lado asyncio (lo usa el nodo) -------------------------------------------
    def iniciar(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._hilo = threading.Thread(target=self._bucle, daemon=True)
        self._hilo.start()

    def nueva_sesion(self) -> tuple[asyncio.StreamReader, EscritorFaro]:
        """Un par reader/writer nuevo para nodo.atender()."""
        with self._lock:
            self._sesion += 1
            self._lector = asyncio.StreamReader()
            self._cola.clear()
            self._inicio_sesion = time.monotonic()
            self._ultimo_rx = 0.0
            return self._lector, EscritorFaro(self, self._sesion)

    def fin_sesion(self, sesion: int):
        with self._lock:
            if sesion == self._sesion:
                self._lector = None
                self._cola.clear()

    def encolar(self, paquete: bytes):
        with self._lock:
            if self._lector is None:
                return
            # Un anuncio de rutas reemplaza al que aun no salio: solo importa el mas nuevo.
            if len(paquete) > 1 and paquete[1] == TIPO_RUTAS:
                for i, viejo in enumerate(self._cola):
                    if len(viejo) > 1 and viejo[1] == TIPO_RUTAS:
                        self._cola[i] = paquete
                        return
            if len(self._cola) >= MAX_COLA:
                self.estadisticas["descartados"] += 1
                return
            if len(paquete) > 1 and paquete[1] != TIPO_RUTAS:
                # Los datos y saludos pasan antes que los anuncios de rutas en espera.
                for i, viejo in enumerate(self._cola):
                    if len(viejo) > 1 and viejo[1] == TIPO_RUTAS:
                        self._cola.insert(i, paquete)
                        return
            self._cola.append(paquete)

    def pedir_rotacion(self):
        self._rotar_ya.set()

    def detener(self, espera: float = 2.0):
        self._parar.set()
        if self._hilo is not None:
            self._hilo.join(espera)

    def resumen(self) -> str:
        par = "desconocido" if self.par is None else f"{self.par:04X}"
        e = self.estadisticas
        return (f"direccion propia {self.adaptador.direccion:04X}, otro Faro {par}, "
                f"{len(self._cola)} en cola | enviados {e['enviados']}, recibidos {e['recibidos']}, "
                f"fallidos {e['fallidos']}, descartados {e['descartados']}, "
                f"rotaciones {e['rotaciones']} | ~{self.t_paquete():.1f} s por paquete")

    # -- lado del hilo (lo unico que toca el adaptador) ---------------------------
    def _bucle(self):
        while not self._parar.is_set():
            try:
                self._paso()
            except Exception as error:  # el hilo nunca debe morir en silencio
                print(f"[{self.nombre}] error en el enlace por luz: {type(error).__name__}: {error}")
                time.sleep(1)

    def _paso(self):
        self._revisar_rotacion()
        paquete = None
        with self._lock:
            if self._cola and self._medio_libre():
                paquete = self._cola.popleft()
        if paquete is not None:
            self._transmitir(paquete)
            return
        m = self.adaptador.recibir_mensaje(timeout=ESCUCHA)
        if m is not None:
            self._entregar(m)
        self._revisar_vida()

    def _medio_libre(self) -> bool:
        """Escuchar antes de hablar: si estan llegando bytes, el otro esta transmitiendo."""
        if self.adaptador.puerto.in_waiting:
            return False
        dec = getattr(self.adaptador, "_dec", None)
        return not (dec is not None and dec.pendiente())

    def t_intento(self) -> float:
        """Lo que puede tardar un intento fallido de una trama (envio + espera del ACK)."""
        a = self.adaptador
        return TAM_MAX_TRAMA * a.t_byte + max(1.0, a.espera_ack_minima())

    def _escuchar(self, segundos: float):
        """Escucha como esclavo durante 'segundos', entregando lo que llegue."""
        limite = time.monotonic() + segundos
        while (restante := limite - time.monotonic()) > 0 and not self._parar.is_set():
            m = self.adaptador.recibir_mensaje(timeout=restante)
            if m is not None:
                self._entregar(m)

    def _transmitir(self, paquete: bytes):
        """
        Envia un paquete. Si los dos Faros transmiten a la vez, cada uno espera
        su ACK e ignora los datos del otro, y ambos fallan. Por eso:
          - antes de enviar se escucha un rato aleatorio corto, y
          - tras un fallo se escucha un rato aleatorio largo (mayor que un
            intento completo del otro), para recibir lo que el otro reintente.
        """
        # Pausa minima (el otro Faro puede estar limpiando su eco tras un ACK)
        # mas un rato aleatorio, escuchando, para no hablar a la vez que el otro.
        self._escuchar(self.adaptador.retardo_ack + random.uniform(0, self.t_trama()))
        while not self._medio_libre() and not self._parar.is_set():
            self._escuchar(self.t_trama())  # el otro empezo a hablar: esperar a que termine
        for intento in range(REINTENTOS_PAQUETE):
            if self.par is None:
                self._difundir(paquete)
                self.estadisticas["enviados"] += 1
                self.log(f"paquete tipo {paquete[1]} enviado a difusion")
                return
            if self.adaptador.enviar_mensaje(self.par, paquete):
                self.estadisticas["enviados"] += 1
                self.log(f"paquete tipo {paquete[1]} confirmado por {self.par:04X}")
                return
            espera = random.uniform(1.0, 3.0) * (intento + 1) * self.t_intento()
            self.log(f"sin ACK (intento {intento + 1}); escucho {espera:.1f} s y reintento")
            self._escuchar(espera)
        self.estadisticas["fallidos"] += 1
        if not self.par_fijo:
            self.log("el otro Faro no confirma; se vuelve a difusion")
            self.par = None

    def _difundir(self, paquete: bytes):
        """Envia por difusion, sin ACK, igual que enviar.py con destino FFFF."""
        trozos = [paquete[i:i + MAX_DATOS] for i in range(0, len(paquete), MAX_DATOS)]
        for i, trozo in enumerate(trozos):
            hay_mas = 0x80 if i < len(trozos) - 1 else 0
            # Se usa la secuencia del adaptador para que el receptor no confunda
            # estas tramas con duplicados de las enviadas con ACK.
            seq = self.adaptador._siguiente_secuencia()
            self.adaptador.enviar(Trama(TIPO_DATOS, DIFUSION, self.adaptador.direccion,
                                        seq, trozo, i | hay_mas))

    def _entregar(self, m):
        if m.tipo != TIPO_DATOS or len(m.datos) != TAM_PAQUETE or m.datos[0] != VERSION:
            # No es un paquete de la mesh (p. ej. una prueba con enviar.py).
            self.estadisticas["descartados"] += 1
            self.log(f"mensaje ajeno a la mesh descartado ({len(m.datos)} bytes)")
            return
        if not self.par_fijo and self.par != m.origen:
            self.log(f"otro Faro: {m.origen:04X}")
            self.par = m.origen
        self.estadisticas["recibidos"] += 1
        with self._lock:
            self._ultimo_rx = time.monotonic()
            lector = self._lector
        if lector is not None and self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(lector.feed_data, m.datos)
            except RuntimeError:
                pass  # el programa esta cerrando

    def _revisar_vida(self):
        with self._lock:
            lector = self._lector
            ultimo = max(self._ultimo_rx, self._inicio_sesion)
            vencido = lector is not None and time.monotonic() - ultimo > self.vida
            if vencido:
                self._lector = None
                self._cola.clear()
        if vencido and self._loop is not None:
            self.log(f"sin noticias del otro Faro en {self.vida:.0f} s; enlace caido")
            try:
                self._loop.call_soon_threadsafe(lector.feed_eof)
            except RuntimeError:
                pass

    def _revisar_rotacion(self):
        ahora = time.monotonic()
        periodica = self.rotar_cada > 0 and ahora - self._ultima_rotacion >= self.rotar_cada
        if periodica or self._rotar_ya.is_set():
            self._rotar_ya.clear()
            self._ultima_rotacion = ahora
            vieja = self.adaptador.direccion
            self.adaptador.direccion = direccion_persistente(rotar=True)
            self.estadisticas["rotaciones"] += 1
            print(f"[{self.nombre}] direccion fisica rotada: {vieja:04X} -> {self.adaptador.direccion:04X}")


async def mantener_enlace(nodo, enlace: EnlaceFaro):
    """Mantiene al otro Faro como vecino del nodo, reconectando si el enlace cae."""
    while True:
        reader, writer = enlace.nueva_sesion()
        await nodo.atender(reader, writer)
        await asyncio.sleep(1)

