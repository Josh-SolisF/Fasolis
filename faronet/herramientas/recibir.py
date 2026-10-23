#!/usr/bin/env python3
"""
recibir.py - Espera mensajes por el Faro (papel de esclavo) y los muestra.

Ejemplos:
  python3 recibir.py                          # escucha hasta Ctrl+C
  python3 recibir.py --una                    # termina tras el primer mensaje
  python3 recibir.py --una --salida recibido.bin
  python3 recibir.py --timeout 30 --una       # se rinde a los 30 s sin mensajes

Los mensajes de texto salen por stdout; la informacion de estado, por stderr.
Codigo de salida: 0 si recibio algo (o se interrumpio con Ctrl+C), 1 si vencio
el timeout sin recibir nada, 2 si hubo error de uso.
"""

import argparse
import sys

from faronet.faro.capa1 import (TIPO_EXTERNA, DispositivoLuzAdaptador, direccion_persistente)
from faronet.herramientas.enviar import hexa


def leer_argumentos(argv):
    p = argparse.ArgumentParser(description="Recibe mensajes por el Faro.")
    p.add_argument("--puerto", default="/dev/serial0", help="puerto serie (por defecto /dev/serial0)")
    p.add_argument("--baudios", type=int, default=1200, help="velocidad (por defecto 1200)")
    p.add_argument("--direccion", type=hexa, help="direccion propia; si falta se usa la guardada en ~/.faronet_direccion")
    p.add_argument("--una", action="store_true", help="terminar despues del primer mensaje")
    p.add_argument("--timeout", type=float, default=None,
                   help="segundos esperando cada mensaje (por defecto, sin limite)")
    p.add_argument("--salida", help="guardar los bytes recibidos en este archivo en vez de mostrarlos")
    return p.parse_args(argv)


def main(argv=None, adaptador=None) -> int:
    a = leer_argumentos(argv)

    if adaptador is None:
        try:
            propia = a.direccion if a.direccion is not None else direccion_persistente()
            adaptador = DispositivoLuzAdaptador.abrir(a.puerto, a.baudios, direccion=propia)
        except Exception as e:
            print(f"No pude abrir {a.puerto}: {e}", file=sys.stderr)
            return 2

    print(f"Escuchando como {adaptador.direccion:04X}... (Ctrl+C para salir)", file=sys.stderr)
    recibidos = 0
    try:
        while True:
            m = adaptador.recibir_mensaje(timeout=a.timeout)
            if m is None:
                print("Tiempo agotado sin mensajes.", file=sys.stderr)
                break
            recibidos += 1
            tipo = "externa" if m.tipo == TIPO_EXTERNA else "datos"
            print(f"[{recibidos}] de {m.origen:04X}, {len(m.datos)} bytes, tipo {tipo}", file=sys.stderr)

            if a.salida:
                with open(a.salida, "ab") as f:
                    f.write(m.datos)
            elif m.tipo == TIPO_EXTERNA:
                print(m.datos.hex(" "))  # una trama externa es binaria: se muestra en hexadecimal
            else:
                print(m.datos.decode("utf-8", errors="replace"))
            sys.stdout.flush()

            if a.una:
                break
    except KeyboardInterrupt:
        print("\nInterrumpido.", file=sys.stderr)
        return 0
    return 0 if recibidos else 1


if __name__ == "__main__":
    sys.exit(main())
