#!/usr/bin/env python3
"""
medir_enlace.py - Mide la tasa de error (BER) y la tasa efectiva del Faro.

Se corre en las dos Pi a la vez, con los mismos --baudios y --bytes. Primero
el receptor (muestra su direccion), despues el emisor con esa direccion:

  Pi B:  python3 medir_enlace.py receptor --baudios 2400 --distancia 50cm
  Pi A:  python3 medir_enlace.py emisor 3F2A --baudios 2400 --distancia 50cm

Hace dos fases:
  1. BER crudo: el emisor manda N bytes conocidos SIN trama ni CRC. El
     receptor genera la misma secuencia y cuenta bit por bit cuantos llegaron
     cambiados (un byte perdido cuenta como 8 bits errados). Esto mide la luz.
  2. Tasa efectiva: el emisor manda N bytes con el protocolo completo
     (tramas, CRC, ACK, reintentos) y mide cuanto tarda. Esto mide lo que
     realmente le sirve a la mesh: bits de carga util por segundo.

Al final cada lado imprime una linea "RESULTADO;..." para copiar a la tabla
del informe. Los LEDs funcionan durante la prueba (--sin-gpio para apagarlos).
"""
from __future__ import annotations

import argparse
import random
import sys
import time

from faronet.faro.capa1 import direccion_persistente
from faronet.faro.hardware import Indicadores, abrir_faro

MARCA = b"\x7E\x81\xC3\x3C\x5A\xA5"  # inicio de la fase de BER crudo
SEMILLA = 2026


def patron(n: int) -> bytes:
    """Secuencia pseudoaleatoria que ambos lados generan igual."""
    return random.Random(SEMILLA).randbytes(n)


def contar_errores(esperado: bytes, recibido: bytes) -> int:
    errores = sum(bin(a ^ b).count("1") for a, b in zip(esperado, recibido))
    return errores + 8 * (len(esperado) - min(len(esperado), len(recibido)))


def hexa(valor: str) -> int:
    try:
        return int(valor, 16)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{valor}' no es una direccion hexadecimal") from None


# ------------------------------------------------------------------- receptor
def recibir_crudo(adaptador, n: int, espera_inicio: float) -> bytes | None:
    """Espera la MARCA y luego lee n bytes. Devuelve None si no llega la marca."""
    puerto = adaptador.puerto
    puerto.reset_input_buffer()
    buf = bytearray()
    limite = time.monotonic() + espera_inicio
    while time.monotonic() < limite:
        buf += puerto.read(max(1, puerto.in_waiting))
        i = buf.find(MARCA)
        if i >= 0:
            datos = bytearray(buf[i + len(MARCA):])
            break
        del buf[:-len(MARCA)]  # basta conservar la cola por si la marca viene partida
    else:
        return None
    # Margen: el doble del tiempo teorico mas 2 s; si se pierden bytes, se corta ahi.
    limite = time.monotonic() + 2 * n * adaptador.t_byte + 2
    ultimo = time.monotonic()
    while len(datos) < n and time.monotonic() < limite:
        trozo = puerto.read(max(1, puerto.in_waiting))
        if trozo:
            datos += trozo
            ultimo = time.monotonic()
        elif time.monotonic() - ultimo > max(1.0, 50 * adaptador.t_byte):
            break  # la linea se quedo callada: lo que falta se perdio
    return bytes(datos[:n])


def receptor(a, adaptador) -> int:
    esperado = patron(a.bytes)
    print(f"Receptor listo. Direccion de este Faro: {adaptador.direccion:04X}")
    print(f"En la otra Pi: python3 medir_enlace.py emisor {adaptador.direccion:04X} "
          f"--baudios {a.baudios} --bytes {a.bytes} --distancia {a.distancia}")
    ber = None
    if a.fase in ("ambas", "ber"):
        print(f"[1/2] Esperando BER crudo ({a.bytes} bytes, hasta {a.espera:.0f} s)...")
        datos = recibir_crudo(adaptador, a.bytes, a.espera)
        if datos is None:
            print("      No llego la marca de inicio: el haz no esta alineado o la velocidad no coincide.")
        else:
            errores = contar_errores(esperado, datos)
            ber = errores / (8 * a.bytes)
            print(f"      Recibidos {len(datos)}/{a.bytes} bytes, {errores} bits errados, BER = {ber:.2e}")
    ok = None
    if a.fase in ("ambas", "tasa"):
        print("[2/2] Esperando el mensaje con protocolo completo...")
        m = adaptador.recibir_mensaje(timeout=a.espera)
        if m is None:
            print("      No llego el mensaje.")
            ok = False
        else:
            ok = m.datos == esperado
            print(f"      Llegaron {len(m.datos)} bytes; contenido {'CORRECTO' if ok else 'DISTINTO'}")
    ber_txt = "" if ber is None else f"{ber:.2e}"
    print(f"RESULTADO;receptor;{a.baudios};{a.distancia};{a.bytes};BER={ber_txt};"
          f"mensaje={'ok' if ok else ('fallo' if ok is False else '')}")
    return 0 if (ber is not None or a.fase == "tasa") and ok is not False else 1


# --------------------------------------------------------------------- emisor
def emisor(a, adaptador) -> int:
    datos = patron(a.bytes)
    if a.fase in ("ambas", "ber"):
        print(f"[1/2] Enviando {a.bytes} bytes crudos (~{a.bytes * adaptador.t_byte:.1f} s)...")
        time.sleep(1.0)  # dar tiempo a que el receptor este escuchando
        adaptador.puerto.write(MARCA + datos)
        adaptador.puerto.flush()
        time.sleep((len(MARCA) + a.bytes) * adaptador.t_byte)
        if a.fase == "ambas":
            # Esperar a que el receptor termine de contar antes de la fase 2.
            time.sleep(a.bytes * adaptador.t_byte + 3)
    tasa = None
    if a.fase in ("ambas", "tasa"):
        print(f"[2/2] Enviando {a.bytes} bytes con protocolo completo hacia {a.destino:04X}...")
        tramas = 0
        original = adaptador.enviar

        def contar(trama):
            nonlocal tramas
            tramas += 1
            return original(trama)

        adaptador.enviar = contar  # para contar las retransmisiones
        inicio = time.monotonic()
        ok = adaptador.enviar_mensaje(a.destino, datos)
        segundos = time.monotonic() - inicio
        adaptador.enviar = original
        necesarias = -(-a.bytes // 115)
        if ok:
            tasa = a.bytes * 8 / segundos
            print(f"      Confirmado en {segundos:.2f} s: {tasa:.0f} bit/s de carga util "
                  f"({100 * tasa / a.baudios:.0f} % de los {a.baudios} baudios)")
        else:
            print(f"      SIN CONFIRMACION tras {segundos:.1f} s")
        print(f"      Tramas enviadas: {tramas} (minimo {necesarias}; "
              f"{tramas - necesarias} retransmisiones)")
    print(f"RESULTADO;emisor;{a.baudios};{a.distancia};{a.bytes};"
          f"tasa={'' if tasa is None else f'{tasa:.0f}'}")
    return 0 if tasa is not None or a.fase == "ber" else 1


def main() -> int:
    p = argparse.ArgumentParser(description="Mide BER y tasa efectiva del Faro.")
    p.add_argument("rol", choices=["receptor", "emisor"])
    p.add_argument("destino", nargs="?", type=hexa,
                   help="(emisor) direccion del receptor, la que el receptor muestra al arrancar")
    p.add_argument("--baudios", type=int, default=1200)
    p.add_argument("--bytes", type=int, default=1000, help="bytes por prueba (por defecto 1000)")
    p.add_argument("--distancia", default="?", help="etiqueta para el resultado, p. ej. 50cm")
    p.add_argument("--fase", choices=["ambas", "ber", "tasa"], default="ambas")
    p.add_argument("--espera", type=float, default=120.0, help="(receptor) segundos maximos de espera")
    p.add_argument("--puerto", default="/dev/serial0")
    p.add_argument("--sin-gpio", action="store_true", help="no usar los LEDs")
    a = p.parse_args()
    if a.rol == "emisor" and a.destino is None and a.fase != "ber":
        p.error("el emisor necesita la direccion del receptor")
    if not 1 <= a.bytes <= 128 * 115:
        p.error(f"--bytes debe estar entre 1 y {128 * 115}")

    indicadores = Indicadores(usar_gpio=not a.sin_gpio)
    try:
        adaptador = abrir_faro(a.puerto, a.baudios, direccion_persistente(), indicadores)
    except Exception as error:
        print(f"No pude abrir {a.puerto}: {error}")
        indicadores.cerrar()
        return 2
    try:
        return receptor(a, adaptador) if a.rol == "receptor" else emisor(a, adaptador)
    except KeyboardInterrupt:
        print("\nInterrumpido.")
        return 1
    finally:
        indicadores.cerrar()


if __name__ == "__main__":
    sys.exit(main())
