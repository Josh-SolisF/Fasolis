# reinstalar_llave_gateway.ps1 - Vuelve a copiar la llave del gateway a la Raspberry Pi
# despues de usar el boton de panico en el Faro gateway.
#
# Uso (PowerShell, en la laptop que tiene la llave original):
#   .\scripts\reinstalar_llave_gateway.ps1
#   .\scripts\reinstalar_llave_gateway.ps1 -Destino soliberrypi@192.168.40.165
#
# Copia %USERPROFILE%\.faronet\gateway.llave a ~/.faronet/gateway.llave en la Pi (permisos 600)
# y muestra la llave publica para comprobar que coincide con GATEWAY_PUBKEY del Worker.
# Despues hay que volver a arrancar gateway_nodo en la Pi.
param([string]$Destino = "soliberrypi@192.168.40.165")

$llave = Join-Path $env:USERPROFILE ".faronet\gateway.llave"
if (-not (Test-Path $llave)) {
    Write-Host "No existe $llave. Si la llave se perdio o pudo copiarse, hay que generar una nueva" -ForegroundColor Red
    Write-Host "y actualizar GATEWAY_PUBKEY en edge/wrangler.jsonc (ver docs/DOCUMENTACION_PRUEBAS.md)." -ForegroundColor Red
    exit 1
}
ssh $Destino "mkdir -p ~/.faronet && chmod 700 ~/.faronet"
if ($LASTEXITCODE -ne 0) { Write-Host "No se pudo conectar con $Destino" -ForegroundColor Red; exit 1 }
scp $llave "${Destino}:~/.faronet/gateway.llave"
ssh $Destino "chmod 600 ~/.faronet/gateway.llave && cd ~/Fasolis && python3 -m faronet.herramientas.gateway --mostrar-llave"
Write-Host "Listo. La llave publica de arriba debe ser igual a GATEWAY_PUBKEY (edge/wrangler.jsonc)." -ForegroundColor Green
Write-Host "Ahora vuelvan a arrancar gateway_nodo en la Pi." -ForegroundColor Green
