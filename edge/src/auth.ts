/**
 * auth.ts - Autenticacion del gateway: firma Ed25519 + marca de tiempo.
 *
 * El gateway firma:  METODO \n RUTA \n TS \n SHA256(cuerpo en hex)
 * y manda  X-Faro-Ts (segundos) y X-Faro-Firma (hex de 64 bytes).
 *
 * Repeticion (replay): dentro de la ventana de 60 s una peticion identica
 * tendria la misma firma. index.ts la recuerda en el Durable Object Control
 * (primeraVez) y rechaza la segunda con 409.
 */
export const VENTANA_S = 60;

export function hexABytes(hex: string): Uint8Array | null {
	if (hex.length % 2 !== 0 || !/^[0-9a-fA-F]*$/.test(hex)) return null;
	const out = new Uint8Array(hex.length / 2);
	for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
	return out;
}

export function bytesAHex(buf: ArrayBuffer | Uint8Array): string {
	const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
	return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** Importa una llave publica Ed25519 cruda (32 bytes) para verificar firmas. */
export async function importarEd25519(raw: Uint8Array): Promise<CryptoKey> {
	try {
		return await crypto.subtle.importKey("raw", raw, { name: "Ed25519" }, false, ["verify"]);
	} catch {
		// Nombre antiguo de Ed25519 en Workers.
		return await crypto.subtle.importKey(
			"raw", raw,
			{ name: "NODE-ED25519", namedCurve: "NODE-ED25519" } as SubtleCryptoImportKeyAlgorithm,
			false, ["verify"],
		);
	}
}

let cache: { hex: string; llave: CryptoKey } | null = null;

async function llaveGateway(hex: string): Promise<CryptoKey> {
	if (cache && cache.hex === hex) return cache.llave;
	const raw = hexABytes(hex);
	if (!raw || raw.length !== 32) throw new Error("GATEWAY_PUBKEY invalida");
	const llave = await importarEd25519(raw);
	cache = { hex, llave };
	return llave;
}

export async function verificarGateway(
	request: Request, ruta: string, cuerpo: ArrayBuffer, llavePublicaHex: string,
): Promise<boolean> {
	const ts = Number(request.headers.get("X-Faro-Ts"));
	if (!Number.isInteger(ts) || Math.abs(Date.now() / 1000 - ts) > VENTANA_S) return false;

	const firma = hexABytes(request.headers.get("X-Faro-Firma") ?? "");
	if (!firma || firma.length !== 64) return false;

	let llave: CryptoKey;
	try {
		llave = await llaveGateway(llavePublicaHex);
	} catch {
		return false;
	}
	const hash = bytesAHex(await crypto.subtle.digest("SHA-256", cuerpo));
	const mensaje = new TextEncoder().encode(`${request.method}\n${ruta}\n${ts}\n${hash}`);
	try {
		return await crypto.subtle.verify(llave.algorithm.name, llave, firma, mensaje);
	} catch {
		return false;
	}
}