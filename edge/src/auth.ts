/**
 * auth.ts - Autenticacion del gateway: firma Ed25519 + marca de tiempo.
 *
 * El gateway firma:  METODO \n RUTA \n TS \n SHA256(cuerpo en hex)
 * y manda  X-Faro-Ts (segundos) y X-Faro-Firma (hex de 64 bytes).
 *
 * Limitacion conocida: dentro de la ventana de 60 s una peticion identica se
 * aceptaria dos veces. TLS lo hace dificil de explotar; para cerrarlo del todo
 * habria que recordar nonces en un Durable Object.
 */
const VENTANA_S = 60;

function hexABytes(hex: string): Uint8Array | null {
	if (hex.length % 2 !== 0 || !/^[0-9a-fA-F]*$/.test(hex)) return null;
	const out = new Uint8Array(hex.length / 2);
	for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
	return out;
}

function bytesAHex(buf: ArrayBuffer): string {
	return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

let cache: { hex: string; llave: CryptoKey } | null = null;

async function llaveGateway(hex: string): Promise<CryptoKey> {
	if (cache && cache.hex === hex) return cache.llave;
	const raw = hexABytes(hex);
	if (!raw || raw.length !== 32) throw new Error("GATEWAY_PUBKEY invalida");
	let llave: CryptoKey;
	try {
		llave = await crypto.subtle.importKey("raw", raw, { name: "Ed25519" }, false, ["verify"]);
	} catch {
		// Nombre antiguo de Ed25519 en Workers.
		llave = await crypto.subtle.importKey(
			"raw", raw,
			{ name: "NODE-ED25519", namedCurve: "NODE-ED25519" } as SubtleCryptoImportKeyAlgorithm,
			false, ["verify"],
		);
	}
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