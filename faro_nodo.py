#!/usr/bin/env python3
"""
faro_nodo.py - Nodo de FaroNet que corre en la Raspberry Pi con su Faro.

Es el mismo nodo de nodo.py (vecinos por TCP, rutas, directorio) con tres
cosas mas:
  - el otro Faro como vecino, por luz (enlace_faro.py);
  - LEDs de actividad: verde al transmitir, amarillo al recibir;
  - boton de panico: detiene el nodo, borra las llaves, rota la direccion
    fisica y lo indica con los LEDs.

Ejemplo, dos Pi unidas solo por luz (cada una en su terminal SSH):
  python3 faro_nodo.py --nombre PA --puerto 9001 --baudios 2400 --intervalo-rutas 20
  python3 faro_nodo.py --nombre PB --puerto 9001 --baudios 2400 --intervalo-rutas 20
y una laptop por Ethernet a cada lado:
  py nodo.py --nombre LA --puerto 9002 --vecino <IP de PA>:9001 --intervalo-rutas 20

Comandos extra en la terminal: faro (estado del enlace) | rotar (nueva direccion fisica)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import threading

from capa1 import direccion_persistente
from enlace_faro import EnlaceFaro, mantener_enlace
from hardware import Indicadores, abrir_faro, ejecutar_panico
from identidad import Identidad
from nodo import crear_parser, principal


def hexa(valor: str) -> int:
    try:
        n = int(valor, 16)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{valor}' no es una direccion hexadecimal") from None
    if not 0 < n < 0xFFFF:
        raise argparse.ArgumentTypeError("la direccion debe estar entre 0001 y FFFE")
    return n


def leer_argumentos():
    parser = crear_parser("Nodo de FaroNet con Faro (Raspberry Pi)")
    g = parser.add_argument_group("Faro")
    g.add_argument("--faro-puerto", default="/dev/serial0", help="puerto serie del Faro")
    g.add_argument("--baudios", type=int, default=1200, help="velocidad de la luz (por defecto 1200)")
    g.add_argument("--faro-par", type=hexa, default=None,
                   help="direccion fisica del otro Faro; si falta, se aprende sola")
    g.add_argument("--rotar-cada", type=float, default=0.0,
                   help="segundos entre rotaciones de la direccion fisica (0 = solo con 'rotar')")
    g.add_argument("--sin-gpio", action="store_true", help="no usar LEDs ni boton")
    return parser.parse_args()


def main() -> int:
    args = leer_argumentos()
    ruta_llaves = args.llaves or Identidad.ruta_por_defecto(args.nombre)
    try:
        identidad = Identidad.cargar_o_crear(args.nombre, ruta_llaves)
    except OSError as error:
        print(f"no se pudo leer ni crear el archivo de llaves: {error}")
        return 1

    indicadores = Indicadores(usar_gpio=not args.sin_gpio)
    if indicadores.disponible:
        print(f"[{args.nombre}] LEDs y boton de panico listos")
    else:
        print(f"[{args.nombre}] sin LEDs ni boton ({indicadores.motivo})")
    try:
        adaptador = abrir_faro(args.faro_puerto, args.baudios, direccion_persistente(), indicadores)
    except Exception as error:
        print(f"no se pudo abrir el Faro en {args.faro_puerto}: {error}")
        indicadores.cerrar()
        return 1
    enlace = EnlaceFaro(adaptador, par=args.faro_par, rotar_cada=args.rotar_cada,
                        debug=args.debug, nombre=args.nombre)
    print(f"[{args.nombre}] Faro en {args.faro_puerto} a {args.baudios} baudios, "
          f"direccion {adaptador.direccion:04X}, ~{enlace.t_paquete():.1f} s por paquete")
    if enlace.t_paquete() * 2 > args.intervalo_rutas:
        print(f"[{args.nombre}] AVISO: a esta velocidad los anuncios de rutas cada "
              f"{args.intervalo_rutas:g} s saturan la luz; usar --intervalo-rutas "
              f"{max(20, int(enlace.t_paquete() * 4))} o mas")

    panico = threading.Event()

    def preparar(nodo):
        loop = asyncio.get_running_loop()
        enlace.iniciar(loop)

        def boton():  # corre en un hilo de gpiozero
            if not panico.is_set():
                panico.set()
                loop.call_soon_threadsafe(nodo.detener.set)

        indicadores.al_presionar(boton)
        nodo.comandos["faro"] = ("estado del enlace por luz", lambda: print(f"[{nodo.nombre}] {enlace.resumen()}"))
        nodo.comandos["rotar"] = ("rotar la direccion fisica", enlace.pedir_rotacion)
        return [asyncio.create_task(mantener_enlace(nodo, enlace))]

    codigo = 0
    try:
        asyncio.run(principal(args, identidad, preparar))
    except KeyboardInterrupt:
        pass
    except OSError as error:
        print(f"no se pudo abrir el puerto {args.puerto}: {error}")
        codigo = 1
    finally:
        enlace.detener()
        if panico.is_set():
            print(f"[{args.nombre}] BOTON DE PANICO: borrando estado sensible...")
            identidad.olvidar()  # las llaves de cifrado viejas se descartan ya
            del identidad  # se sueltan las llaves en memoria; el proceso termina enseguida
            resultado = ejecutar_panico([ruta_llaves], indicadores)
            print(f"[{args.nombre}] llaves borradas: {len(resultado['llaves_borradas'])} archivo(s); "
                  f"nueva direccion fisica {resultado['direccion_nueva']:04X}")
        indicadores.cerrar()
    return codigo


if __name__ == "__main__":
    sys.exit(main())