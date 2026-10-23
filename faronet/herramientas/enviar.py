#!/usr/bin/env python3
"""
enviar.py - Envia un mensaje por el Faro (papel de maestro).

Ejemplos:
  python3 enviar.py 1234 --texto "Hola"
  python3 enviar.py 1234 --archivo foto.bin
  echo "Hola" | python3 enviar.py 1234
  python3 enviar.py FFFF --texto "para todos"          # difusion, sin ACK
  python3 enviar.py 1234 --archivo trama.bin --externa  # trama creada fuera del Faro

Codigo de salida: 0 si el mensaje fue confirmado, 1 si no, 2 si hubo error de uso.
"""

import argparse
import sys

from faronet.faro.capa1 import (DIFUSION, MAX_DATOS, TIPO_DATOS, TIPO_EXTERNA, DispositivoLuzAdaptador,
                   Trama, direccion_persistente)


def hexa(valor: str) -> int:
    try:
        n = int(valor, 16)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{valor}' no es una direccion hexadecimal")
    if not 0 <= n <= 0xFFFF:
        raise argparse.ArgumentTypeError("la direccion debe caber en 2 bytes (0000 a FFFF)")
    return n


def leer_argumentos(argv):
    p = argparse.ArgumentParser(description="Envia un mensaje por el Faro.")
    p.add_argument("destino", type=hexa, help="direccion fisica del destino en hexadecimal (FFFF = difusion)")
    fuente = p.add_mutually_exclusive_group()
    fuente.add_argument("--texto", help="texto a enviar")
    fuente.add_argument("--archivo", help="archivo cuyo contenido se envia")
    p.add_argument("--externa", action="store_true",
                   help="enviar como trama externa (creada fuera del Faro) en vez de datos")
    p.add_argument("--puerto", default="/dev/serial0", help="puerto serie (por defecto /dev/serial0)")
    p.add_argument("--baudios", type=int, default=1200, help="velocidad (por defecto 1200)")
    p.add_argument("--direccion", type=hexa, help="direccion propia; si falta se usa la guardada en ~/.faronet_direccion")
    p.add_argument("--reintentos", type=int, default=3, help="intentos por fragmento (por defecto 3)")
    p.add_argument("--espera-ack", type=float, default=2.0, help="segundos esperando cada ACK (por defecto 2)")
    return p.parse_args(argv)


def main(argv=None, adaptador=None) -> int:
    a = leer_argumentos(argv)

    if a.texto is not None:
        datos = a.texto.encode("utf-8")
    elif a.archivo:
        try:
            with open(a.archivo, "rb") as f:
                datos = f.read()
        except OSError as e:
            print(f"No pude leer el archivo: {e}", file=sys.stderr)
            return 2
    else:
        datos = sys.stdin.buffer.read()

    if adaptador is None:
        try:
            propia = a.direccion if a.direccion is not None else direccion_persistente()
            adaptador = DispositivoLuzAdaptador.abrir(a.puerto, a.baudios, direccion=propia)
        except Exception as e:
            print(f"No pude abrir {a.puerto}: {e}", file=sys.stderr)
            return 2

    fragmentos = max(1, -(-len(datos) // MAX_DATOS))
    segundos = (len(datos) + 13 * fragmentos) * 10 / a.baudios
    print(f"Enviando {len(datos)} bytes en {fragmentos} fragmento(s) "
          f"desde {adaptador.direccion:04X} hacia {a.destino:04X} "
          f"(unos {segundos:.1f} s de luz)", file=sys.stderr)

    if a.destino == DIFUSION:
        # A la difusion no contesta nadie: se envia cada fragmento una vez, sin esperar ACK.
        for i in range(fragmentos):
            trozo = datos[i * MAX_DATOS:(i + 1) * MAX_DATOS]
            hay_mas = 0x80 if i < fragmentos - 1 else 0
            adaptador.enviar(Trama(TIPO_EXTERNA if a.externa else TIPO_DATOS, DIFUSION,
                                   adaptador.direccion, (i + 1) % 256, trozo, i | hay_mas))
        print("Enviado a difusion (sin confirmacion).", file=sys.stderr)
        return 0

    ok = adaptador.enviar_mensaje(a.destino, datos,
                                  tipo=TIPO_EXTERNA if a.externa else TIPO_DATOS,
                                  reintentos=a.reintentos, espera_ack=a.espera_ack)
    print("Confirmado por el otro Faro." if ok else "SIN CONFIRMACION: el otro Faro no contesto.",
          file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
