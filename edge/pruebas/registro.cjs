// Precarga (--require): redirige require("cloudflare:workers") al sustituto de pruebas.
const Module = require("node:module");
const path = require("node:path");
const original = Module._resolveFilename;
Module._resolveFilename = function (peticion, ...resto) {
	if (peticion === "cloudflare:workers") return path.join(__dirname, "falso-cloudflare.cjs");
	return original.call(this, peticion, ...resto);
};
