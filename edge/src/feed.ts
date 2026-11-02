/**
 * feed.ts - Feed Atom (RFC 4287) de un tema. Funciones puras, sin acceso al runtime.
 *
 * Privacidad: el feed solo contiene lo que ya es publico en la sala. El autor
 * de cada entrada es siempre "anonimo": FaroNet no sabe quien escribio.
 */
export interface EntradaFeed {
	id: number;   // id autoincremental de la sala (estable, no se reutiliza)
	ts: number;   // segundos Unix
	texto: string;
}

// Caracteres que XML 1.0 no permite ni escapados.
const INVALIDOS = /[\u0000-\u0008\u000B\u000C\u000E-\u001F\uFFFE\uFFFF]/g;

export function escaparXml(s: string): string {
	return s
		.replace(INVALIDOS, "")
		.replace(/&/g, "&amp;")
		.replace(/</g, "&lt;")
		.replace(/>/g, "&gt;")
		.replace(/"/g, "&quot;")
		.replace(/'/g, "&apos;");
}

function iso(ts: number): string {
	return new Date(ts * 1000).toISOString().replace(".000Z", "Z");
}

function titulo(texto: string): string {
	const letras = Array.from(texto.replace(/\s+/g, " ").trim());
	return letras.length > 60 ? letras.slice(0, 59).join("") + "\u2026" : letras.join("");
}

/** 'entradas' debe venir de la mas nueva a la mas vieja. */
export function construirAtom(tema: string, origen: string, entradas: EntradaFeed[]): string {
	const sala = `${origen}/sala/${tema}`;
	const actualizado = entradas.length ? entradas[0].ts : Math.floor(Date.now() / 1000);
	const items = entradas
		.map(
			(e) => `  <entry>
    <id>urn:faronet:sala:${escaparXml(tema)}:${e.id}</id>
    <title>${escaparXml(titulo(e.texto))}</title>
    <updated>${iso(e.ts)}</updated>
    <link rel="alternate" href="${escaparXml(sala)}"/>
    <author><name>an\u00f3nimo</name></author>
    <content type="text">${escaparXml(e.texto)}</content>
  </entry>`,
		)
		.join("\n");
	return `<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <id>urn:faronet:sala:${escaparXml(tema)}</id>
  <title>FaroNet - #${escaparXml(tema)}</title>
  <updated>${iso(actualizado)}</updated>
  <link rel="self" href="${escaparXml(sala)}/feed.atom"/>
  <link rel="alternate" href="${escaparXml(sala)}"/>
  <author><name>an\u00f3nimo</name></author>
${items}
</feed>
`;
}
