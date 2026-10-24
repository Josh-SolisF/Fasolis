/**
 * control.ts - Durable Object "Control": limite de peticiones y anti-repeticion.
 *
 * Una sola instancia global. TODO el estado vive en memoria (nunca se escribe
 * a disco): si el objeto se reinicia se pierde, y eso esta bien, porque solo
 * sirve para frenar abuso a corto plazo.
 *
 *  - limitar(): ventana fija por clave. La clave la arma el Worker con un hash
 *    de la IP del cliente (ver index.ts), asi aqui nunca llega una IP.
 *  - primeraVez(): recuerda las firmas del gateway ya vistas para que una
 *    peticion capturada no se pueda repetir dentro de la ventana de la marca
 *    de tiempo (cierra la limitacion que documenta auth.ts).
 */
import { DurableObject } from "cloudflare:workers";

const MAX_CLAVES = 10_000;

interface Cubeta {
	n: number;
	vence: number; // ms
}

export class Control extends DurableObject<Env> {
	private cubetas = new Map<string, Cubeta>();
	private firmas = new Map<string, number>(); // firma (hex) -> vencimiento en ms

	/** Cuenta una peticion para 'clave'. Devuelve false si ya excedio el limite. */
	async limitar(clave: string, limite: number, ventanaS: number): Promise<boolean> {
		const ahora = Date.now();
		let c = this.cubetas.get(clave);
		if (!c || c.vence <= ahora) {
			this.hacerEspacio(this.cubetas, ahora);
			c = { n: 0, vence: ahora + ventanaS * 1000 };
			this.cubetas.set(clave, c);
		}
		c.n++;
		return c.n <= limite;
	}

	/** true la primera vez que se ve esta firma; false si es una repeticion. */
	async primeraVez(firma: string, vidaS: number): Promise<boolean> {
		const ahora = Date.now();
		const vence = this.firmas.get(firma);
		if (vence !== undefined && vence > ahora) return false;
		this.hacerEspacio(this.firmas, ahora);
		this.firmas.set(firma, ahora + vidaS * 1000);
		return true;
	}

	/** Si el mapa esta lleno, borra primero lo vencido y luego lo mas viejo. */
	private hacerEspacio<T extends Cubeta | number>(mapa: Map<string, T>, ahora: number): void {
		if (mapa.size < MAX_CLAVES) return;
		for (const [k, v] of mapa) {
			if ((typeof v === "number" ? v : v.vence) <= ahora) mapa.delete(k);
		}
		let sobran = mapa.size - MAX_CLAVES + MAX_CLAVES / 10;
		for (const k of mapa.keys()) {
			if (sobran-- <= 0) break;
			mapa.delete(k);
		}
	}
}
