Start-Process py -ArgumentList "-m faronet.mesh.directorio --puerto 9000 --escuchar 127.0.0.1"
Start-Sleep 1
1..6 | ForEach-Object {
    $a = "-m faronet.mesh.nodo --nombre N$_ --puerto $(9000+$_) --escuchar 127.0.0.1 " +
         "--directorio 127.0.0.1:9000 --intervalo-rutas 2 --debug"
    if ($_ -gt 1) { $a += " --vecino 127.0.0.1:$(9000+$_-1)" }
    Start-Process py -ArgumentList $a
    Start-Sleep -Milliseconds 400
}