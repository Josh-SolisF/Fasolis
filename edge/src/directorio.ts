/**
 * directorio.ts - Replica publica y de solo lectura del directorio de nodos.
 *
 * El gateway (que si habla con la mesh) empuja las fichas que obtiene del
 * directorio de FaroNet. El Worker NO confia en el gateway: cada ficha lleva
 * la firma Ed25519 del nodo que la publico y se verifica aqui. Un gateway
 * malicioso puede omitir fichas, pero no inventarlas ni alterarlas.
 *
 * Formato de la ficha (identico a faronet/mesh/identidad.py, version 1, big-endian):
 *   Version(1) | LongNombre(1) | Nombre | LlaveFirma(32) | LlaveCifrado(32) |
 *   Expira(8, segundos Unix) | Firma(64)
 * La firma cubre todos los bytes anteriores a ella. Las reglas al aceptar
 * espejan las de directorio.py: firma valida, no vencida, vencimiento a lo
 * sumo 1 h adelante, un nombre no cambia de llave mientras su ficha viva y
 * una ficha mas vieja que la registrada se rechaza.
 *
 * Cualquiera puede re-verificar: cada entrada publica incluye la ficha cruda
 * en hex ('ficha') y la llave de firma.
 */
import { DurableObject } from "cloudflare:workers";
import { bytesAHex, hexABytes, importarEd25519 } from "./auth";

export const VERSION_FICHA = 1;
export const MAX_NOMBRE = 16;
export const MAX_VIDA_FICHA = 3600;
const TAM_LLAVE = 32;
const TAM_FIRMA = 64;
const MAX_FICHAS = 500;

const segundos = () => Math.floor(Date.now() / 1000);

export interface Ficha {
	nombre: string;
	llaveFirma: Uint8Array;
	llaveCifrado: Uint8Array;
	expira: number;
	firma: Uint8Array;
	cuerpo: Uint8Array; // los bytes que cubre la firma
}

export interface FichaPublica {
	nombre: string;
	llave_firma: string;   // hex
	llave_cifrado: string; // hex
	expira: number;
	ficha: string;         // hex de la ficha completa, para verificarla fuera del edge
}

/** Devuelve la ficha, o un texto con el motivo si esta mal formada. */
export function decodificarFicha(datos: Uint8Array): Ficha | string {
	if (datos.length < 2) return "ficha vacia";
	if (datos[0] !== VERSION_FICHA) return "version de ficha no soportada";
	const n = datos[1];
	if (n === 0 || n > MAX_NOMBRE) return "nombre invalido";
	if (datos.length !== 2 + n + 2 * TAM_LLAVE + 8 + TAM_FIRMA) return "tamano de ficha incorrecto";
	let nombre: string;
	try {
		nombre = new TextDecoder("utf-8", { fatal: true, ignoreBOM: false }).decode(datos.subarray(2, 2 + n));
	} catch {
		return "nombre invalido";
	}
	if (/[\s\u0000-\u001f]/.test(nombre)) return "nombre invalido";
	let i = 2 + n;
	const llaveFirma = datos.slice(i, i + TAM_LLAVE); i += TAM_LLAVE;
	const llaveCifrado = datos.slice(i, i + TAM_LLAVE); i += TAM_LLAVE;
	const expira = Number(new DataView(datos.buffer, datos.byteOffset + i, 8).getBigUint64(0)); i += 8;
	return { nombre, llaveFirma, llaveCifrado, expira, firma: datos.slice(i), cuerpo: datos.slice(0, i) };
}

/** null si la ficha es valida; si no, el motivo del rechazo. */
export async function verificarFicha(f: Ficha, ahora: number): Promise<string | null> {
	if (f.expira <= ahora) return "ficha vencida";
	if (f.expira > ahora + MAX_VIDA_FICHA) return "vencimiento demasiado lejano";
	try {
		const llave = await importarEd25519(f.llaveFirma);
		const ok = await crypto.subtle.verify(llave.algorithm.name, llave, f.firma, f.cuerpo);
		return ok ? null : "firma invalida";
	} catch {
		return "firma invalida";
	}
}

function publica(hex: string): FichaPublica | null {
	const bytes = hexABytes(hex);
	const f = bytes ? decodificarFicha(bytes) : null;
	if (!f || typeof f === "string") return null;
	return {
		nombre: f.nombre,
		llave_firma: bytesAHex(f.llaveFirma),
		llave_cifrado: bytesAHex(f.llaveCifrado),
		expira: f.expira,
		ficha: hex,
	};
}

export class Directorio extends DurableObject<Env> {
	constructor(ctx: DurableObjectState, env: Env) {
		super(ctx, env);
		ctx.blockConcurrencyWhile(async () => {
			ctx.storage.sql.exec(
				"CREATE TABLE IF NOT EXISTS fichas (nombre TEXT PRIMARY KEY, llave_firma TEXT NOT NULL, expira INTEGER NOT NULL, ficha TEXT NOT NULL)",
			);
		});
	}

	/** Valida y guarda fichas (hex). Devuelve cuantas se aceptaron y los motivos de las demas. */
	async publicar(fichas: string[]): Promise<{ aceptadas: number; rechazadas: string[] }> {
		let aceptadas = 0;
		const rechazadas: string[] = [];
		for (const hex of fichas) {
			const motivo = await this.aceptar(hex);
			if (motivo === null) aceptadas++;
			else rechazadas.push(motivo);
		}
		await this.programarAlarma();
		return { aceptadas, rechazadas };
	}

	private async aceptar(hex: string): Promise<string | null> {
		const bytes = hexABytes(hex);
		if (!bytes) return "hex invalido";
		const f = decodificarFicha(bytes);
		if (typeof f === "string") return f;
		const motivo = await verificarFicha(f, segundos());
		if (motivo) return motivo;

		// A partir de aqui todo es sincrono: la comprobacion y el guardado no se intercalan.
		const sql = this.ctx.storage.sql;
		sql.exec("DELETE FROM fichas WHERE expira <= ?", segundos());
		const llave = bytesAHex(f.llaveFirma);
		const actual = sql
			.exec<{ llave_firma: string; expira: number }>(
				"SELECT llave_firma, expira FROM fichas WHERE nombre = ?", f.nombre,
			)
			.toArray()[0];
		if (actual) {
			if (actual.llave_firma !== llave) return "el nombre ya esta registrado con otra llave";
			if (f.expira < actual.expira) return "ficha mas vieja que la registrada";
		} else {
			const { n } = sql.exec<{ n: number }>("SELECT COUNT(*) AS n FROM fichas").one();
			if (n >= MAX_FICHAS) return "directorio lleno";
		}
		sql.exec(
			"INSERT OR REPLACE INTO fichas (nombre, llave_firma, expira, ficha) VALUES (?, ?, ?, ?)",
			f.nombre, llave, f.expira, hex.toLowerCase(),
		);
		return null;
	}

	async listar(): Promise<FichaPublica[]> {
		const filas = this.ctx.storage.sql
			.exec<{ ficha: string }>("SELECT ficha FROM fichas WHERE expira > ? ORDER BY nombre", segundos())
			.toArray();
		return filas.map((f) => publica(f.ficha)).filter((f): f is FichaPublica => f !== null);
	}

	async obtener(nombre: string): Promise<FichaPublica | null> {
		const fila = this.ctx.storage.sql
			.exec<{ ficha: string }>("SELECT ficha FROM fichas WHERE nombre = ? AND expira > ?", nombre, segundos())
			.toArray()[0];
		return fila ? publica(fila.ficha) : null;
	}

	/** Las fichas vencidas se borran de verdad, no solo se esconden. */
	async alarm(): Promise<void> {
		this.ctx.storage.sql.exec("DELETE FROM fichas WHERE expira <= ?", segundos());
		await this.programarAlarma(true);
	}

	private async programarAlarma(forzar = false): Promise<void> {
		if (!forzar && (await this.ctx.storage.getAlarm()) !== null) return;
		const { m } = this.ctx.storage.sql
			.exec<{ m: number | null }>("SELECT MIN(expira) AS m FROM fichas")
			.one();
		if (m !== null) await this.ctx.storage.setAlarm(m * 1000 + 1000);
	}
}
