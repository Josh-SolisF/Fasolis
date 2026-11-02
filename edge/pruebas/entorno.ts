/**
 * entorno.ts - Entorno falso para probar el Worker en Node, sin wrangler.
 *
 * Cada Durable Object recibe un SQLite en memoria (node:sqlite) con la misma
 * interfaz que ctx.storage.sql. No hay WebSockets (las pruebas cubren HTTP).
 * Sirve para comprobar la logica, no el comportamiento de Cloudflare: antes de
 * entregar, probar tambien con `wrangler dev` y con el despliegue real.
 */
import { DatabaseSync } from "node:sqlite";
import { Buzon, Control, Directorio, Sala } from "../src/index";

function normalizar(fila: Record<string, unknown>): Record<string, unknown> {
	const salida: Record<string, unknown> = {};
	for (const [k, v] of Object.entries(fila)) {
		salida[k] = v instanceof Uint8Array ? v.slice().buffer : v;
	}
	return salida;
}

function contexto(db: DatabaseSync) {
	let alarma: number | null = null;
	const sql = {
		exec(consulta: string, ...args: unknown[]) {
			const enlaces = args.map((a) => (a instanceof ArrayBuffer ? new Uint8Array(a) : a));
			const st = db.prepare(consulta);
			const devuelve = /^\s*(select|with)\b/i.test(consulta) || /\breturning\b/i.test(consulta);
			const filas = devuelve ? (st.all(...(enlaces as never[])) as Record<string, unknown>[]).map(normalizar) : [];
			if (!devuelve) st.run(...(enlaces as never[]));
			return {
				toArray: () => filas,
				one: () => {
					if (filas.length !== 1) throw new Error("one(): se esperaba exactamente una fila");
					return filas[0];
				},
				[Symbol.iterator]: () => filas[Symbol.iterator](),
			};
		},
	};
	return {
		storage: {
			sql,
			getAlarm: async () => alarma,
			setAlarm: async (t: number) => { alarma = t; },
			deleteAlarm: async () => { alarma = null; },
		},
		blockConcurrencyWhile: (f: () => unknown) => f(),
		acceptWebSocket: () => {},
		getWebSockets: () => [] as unknown[],
	};
}

class Espacio {
	instancias = new Map<string, any>();
	constructor(readonly nombre: string, readonly Clase: new (c: any, e: any) => any,
		readonly entorno: Entorno) {}
	idFromName(n: string) { return n; }
	get(id: string) {
		let inst = this.instancias.get(id);
		if (!inst) {
			const db = new DatabaseSync(":memory:");
			this.entorno.bases.push(db);
			inst = new this.Clase(contexto(db), this.entorno.env);
			this.instancias.set(id, inst);
		}
		return new Proxy({}, {
			get: (_t, prop: string) => (...args: unknown[]) =>
				Promise.resolve().then(() => {
					if (prop === "fetch") {
						const [a, b] = args as [string | Request, RequestInit?];
						return inst.fetch(a instanceof Request ? a : new Request(a, b));
					}
					return inst[prop](...args);
				}),
		});
	}
}

export class Entorno {
	bases: DatabaseSync[] = [];
	env: any;
	espacios: Record<string, Espacio>;

	constructor(gatewayPubkeyHex: string) {
		this.espacios = {
			SALA: new Espacio("SALA", Sala, this),
			CONTROL: new Espacio("CONTROL", Control, this),
			BUZON: new Espacio("BUZON", Buzon, this),
			DIRECTORIO: new Espacio("DIRECTORIO", Directorio, this),
		};
		this.env = { GATEWAY_PUBKEY: gatewayPubkeyHex, ...this.espacios };
	}

	/** La instancia real de un Durable Object (para llamar alarm() o mirar su estado). */
	instancia(binding: string, id: string): any {
		this.espacios[binding].get(id); // la crea si hace falta
		return this.espacios[binding].instancias.get(id);
	}

	/** Todo el texto guardado en todas las tablas (para comprobar que no hay IPs ni encabezados). */
	volcado(): string {
		const partes: string[] = [];
		for (const db of this.bases) {
			const tablas = db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all() as { name: string }[];
			for (const { name } of tablas) {
				if (name.startsWith("sqlite_")) continue;
				partes.push(JSON.stringify(db.prepare(`SELECT * FROM ${name}`).all(), (_k, v) =>
					v instanceof Uint8Array ? Buffer.from(v).toString("latin1") : v));
			}
		}
		return partes.join("\n");
	}
}
