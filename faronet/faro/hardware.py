#!/usr/bin/env python3
"""
hardware.py - LEDs indicadores y boton de panico del Faro (Raspberry Pi).

Pines (numeracion BCM, ver "Conexiones del Faro"):
  GPIO17  LED verde,    actividad TX (el Faro esta transmitiendo)
  GPIO27  LED amarillo, actividad RX (llegaron bytes por la luz)
  GPIO22  boton de panico, con pull-up externo: presionado lee 0

Contiene:
  - Indicadores: maneja los LEDs y el boton con gpiozero. En una maquina sin
    GPIO (la laptop) no falla: simplemente no hay LEDs ni boton.
  - PuertoConIndicadores: envuelve el puerto serie para que cada escritura
    encienda el LED verde y cada lectura con datos encienda el amarillo.
    DispositivoLuzAdaptador no se entera: solo ve un "objeto tipo puerto".
  - abrir_faro(): igual que DispositivoLuzAdaptador.abrir(), pero con LEDs.
  - borrar_archivo_seguro() y ejecutar_panico(): lo que hace el boton.

Autoprueba del circuito (en la Pi):
  python3 hardware.py
Enciende cada LED, hace la senal de panico y espera que se presione el
boton. En la autoprueba el boton NO borra nada.
"""
from __future__ import annotations

import os
import sys
import threading
import time

PIN_LED_TX = 17
PIN_LED_RX = 27
PIN_BOTON = 22
DESTELLO_MINIMO = 0.05  # segundos: un destello mas corto no se alcanza a ver


class Indicadores:
    """
    LEDs TX/RX y boton de panico.

    Los LEDs se manejan con "estiramiento de pulso": cada actividad los
    enciende y fija cuando apagarlos; un solo hilo los apaga al vencer. Asi
    un byte suelto se ve como un destello y un envio largo como luz continua,
    sin crear un hilo por cada byte.
    """

    def __init__(self, usar_gpio: bool = True):
        self.disponible = False
        self.motivo = "desactivado con --sin-gpio"
        self.led_tx = self.led_rx = self.boton = None
        self._fin = {"tx": 0.0, "rx": 0.0}
        self._lock = threading.Lock()
        self._congelado = False  # durante la senal de panico nadie toca los LEDs
        if not usar_gpio:
            return
        try:
            from gpiozero import LED, Button
            self.led_tx = LED(PIN_LED_TX)
            self.led_rx = LED(PIN_LED_RX)
            # pull_up=None: la resistencia de pull-up esta en el circuito;
            # active_state=False: presionado = 0.
            self.boton = Button(PIN_BOTON, pull_up=None, active_state=False, bounce_time=0.05)
        except Exception as error:  # sin gpiozero, sin GPIO o pin ocupado
            self.motivo = f"{type(error).__name__}: {error}"
            self.cerrar()
            return
        self.disponible = True
        threading.Thread(target=self._apagador, daemon=True).start()

    # -- actividad ----------------------------------------------------------
    def _encender(self, cual: str, segundos: float):
        if not self.disponible or self._congelado:
            return
        led = self.led_tx if cual == "tx" else self.led_rx
        with self._lock:
            self._fin[cual] = max(self._fin[cual], time.monotonic() + max(segundos, DESTELLO_MINIMO))
        led.on()

    def actividad_tx(self, segundos: float = 0.0):
        self._encender("tx", segundos)

    def actividad_rx(self, segundos: float = 0.0):
        self._encender("rx", segundos)

    def _apagador(self):
        while self.disponible:
            ahora = time.monotonic()
            if not self._congelado:
                with self._lock:
                    vencidos = [c for c, fin in self._fin.items() if fin and ahora >= fin]
                    for c in vencidos:
                        self._fin[c] = 0.0
                for c in vencidos:
                    (self.led_tx if c == "tx" else self.led_rx).off()
            time.sleep(0.02)

    # -- boton y senales ------------------------------------------------------
    def al_presionar(self, funcion):
        """Llama a 'funcion' (desde un hilo de gpiozero) al presionar el boton."""
        if self.disponible:
            self.boton.when_pressed = funcion

    def senal_panico(self):
        """Los dos LEDs parpadean rapido 10 veces y quedan encendidos 1 s."""
        if not self.disponible:
            return
        self._congelado = True
        for _ in range(10):
            self.led_tx.on(); self.led_rx.on(); time.sleep(0.1)
            self.led_tx.off(); self.led_rx.off(); time.sleep(0.1)
        self.led_tx.on(); self.led_rx.on(); time.sleep(1.0)
        self.led_tx.off(); self.led_rx.off()
        self._congelado = False

    def cerrar(self):
        self.disponible = False
        for dispositivo in (self.led_tx, self.led_rx, self.boton):
            if dispositivo is not None:
                try:
                    dispositivo.close()
                except Exception:
                    pass


class PuertoConIndicadores:
    """
    Envuelve un puerto tipo pyserial y enciende los LEDs con la actividad.

    El eco del propio LED no enciende el amarillo: el adaptador descarta con
    reset_input_buffer() lo que llego mientras transmitia, sin leerlo.
    """

    def __init__(self, puerto, indicadores: Indicadores, t_byte: float):
        self._puerto = puerto
        self._ind = indicadores
        self._t_byte = t_byte

    def write(self, datos):
        self._ind.actividad_tx(len(datos) * self._t_byte)
        return self._puerto.write(datos)

    def read(self, n=1):
        datos = self._puerto.read(n)
        if datos:
            self._ind.actividad_rx(len(datos) * self._t_byte)
        return datos

    @property
    def in_waiting(self):
        return self._puerto.in_waiting

    def flush(self):
        return self._puerto.flush()

    def reset_input_buffer(self):
        return self._puerto.reset_input_buffer()

    def __getattr__(self, nombre):  # baudrate, close, etc.
        return getattr(self._puerto, nombre)


def abrir_faro(ruta: str = "/dev/serial0", baudios: int = 1200, direccion=None,
               indicadores: Indicadores | None = None, **kw):
    """DispositivoLuzAdaptador.abrir() con los LEDs conectados al puerto."""
    from faronet.faro.capa1 import DispositivoLuzAdaptador
    adaptador = DispositivoLuzAdaptador.abrir(ruta, baudios, direccion=direccion, **kw)
    if indicadores is not None and indicadores.disponible:
        adaptador.puerto = PuertoConIndicadores(adaptador.puerto, indicadores, adaptador.t_byte)
    return adaptador


# ------------------------------------------------------------------- panico
def borrar_archivo_seguro(ruta: str) -> bool:
    """
    Sobrescribe el archivo con ceros, lo fuerza a disco y lo borra.
    Devuelve True si existia.

    Limitacion conocida (para el modelo de amenazas): en una microSD el
    controlador puede escribir los ceros en otro bloque fisico, asi que la
    copia vieja podria sobrevivir en la memoria flash. Guardar las llaves en
    /dev/shm (RAM) evita ese problema.
    """
    try:
        tam = os.path.getsize(ruta)
    except OSError:
        return False
    try:
        with open(ruta, "r+b") as f:
            f.write(b"\x00" * tam)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass  # si no se puede sobrescribir, al menos se intenta borrar
    try:
        os.remove(ruta)
    except OSError:
        return False
    return True


def ejecutar_panico(archivos_llaves: list[str], indicadores: Indicadores | None = None) -> dict:
    """
    Lo que hace el boton de panico despues de detener el nodo:
      1. borra (sobrescribe y elimina) los archivos de llaves,
      2. rota la direccion fisica del Faro,
      3. lo indica con los LEDs.
    El estado en memoria (tablas de rutas, fichas, llaves cargadas) se pierde
    al terminar el proceso, que es lo que hace faro_nodo.py a continuacion.
    """
    from faronet.faro.capa1 import direccion_persistente
    borrados = [r for r in archivos_llaves if borrar_archivo_seguro(r)]
    nueva = direccion_persistente(rotar=True)
    if indicadores is not None:
        indicadores.senal_panico()
    return {"llaves_borradas": borrados, "direccion_nueva": nueva}


# --------------------------------------------------------------- autoprueba
def autoprueba() -> int:
    ind = Indicadores()
    if not ind.disponible:
        print(f"No hay GPIO disponible ({ind.motivo}).")
        print("En la Pi: sudo apt install python3-gpiozero python3-lgpio")
        return 1
    try:
        print("1/4  LED verde (TX) encendido 2 s...")
        ind.led_tx.on(); time.sleep(2); ind.led_tx.off()
        print("2/4  LED amarillo (RX) encendido 2 s...")
        ind.led_rx.on(); time.sleep(2); ind.led_rx.off()
        print("3/4  Senal de panico (los dos parpadean)...")
        ind.senal_panico()
        estado = "PRESIONADO" if ind.boton.is_pressed else "suelto"
        print(f"4/4  Boton ahora: {estado}. Presionalo (en esta prueba no borra nada).")
        print("     Ctrl+C para terminar.")
        veces = 0

        def presionado():
            nonlocal veces
            veces += 1
            print(f"     Boton presionado ({veces})")
            ind.senal_panico()

        ind.al_presionar(presionado)
        while True:
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nAutoprueba terminada.")
        return 0
    finally:
        ind.cerrar()


if __name__ == "__main__":
    sys.exit(autoprueba())
