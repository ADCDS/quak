> Esta página es una traducción automática y puede contener errores. La [versión en inglés](https://github.com/ADCDS/quak/blob/main/docs/troubleshooting.md) es la referencia. [Las correcciones son bienvenidas](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md#translations).

# Solución de problemas

## Emparejamiento y conexión

**"No camera is on its pairing screen" (ninguna cámara está en su pantalla de emparejamiento).** Una cámara ignora los dispositivos que no conoce a menos que esta pantalla esté abierta: **Preferencias >
Conexiones > Conectar dispositivo > GoPro Quik App**. Ábrela y vuelve a ejecutar `quak pair`.

**Falla emparejar el teléfono y luego la computadora.** Conectarse desde otro dispositivo cierra la pantalla de emparejamiento de la cámara. Empareja un
dispositivo a la vez, y vuelve a abrir la pantalla antes de emparejar el siguiente.

**La cámara "no se detectó en 45 s".** Comprueba que esté encendida y cerca. Cuando no está emparejando, una HERO11 puede anunciarse tan solo
cada 10–20 segundos, y una cámara suspendida puede tardar más. Inténtalo de nuevo, o pulsa un botón de la cámara para reactivarla.

**Se conecta, pero nunca termina de configurarse.** Una vez, después de que una cámara se quedara sin corriente mientras estaba conectada por USB, dejó de ofrecer sus servicios Bluetooth
hasta que se reinició. Apaga y enciende la cámara.

**La cámara se niega a grabar o a cambiar ajustes, sin dar ningún motivo.** Puede estar atascada en el modo Transferencia turbo, que bloquea la
grabación, los ajustes y la vista previa. Quak lo activa para las descargas, así que una conexión que se corta durante una descarga puede dejarlo activado. La app lo desactiva
la próxima vez que se conecta, y la línea de comandos lo indica en su error: ejecuta `quak turbo off`.

## Errores de Bluetooth

**"Bluetooth dejó de responder".** Comprueba que Bluetooth esté encendido. Si sigue pasando en Linux, mira el registro del kernel:

```bash
journalctl -k | grep "tx timeout"
```

Una línea como `command 0x2041 tx timeout`, seguida de una desconexión USB, significa que el adaptador Bluetooth se bloqueó. Quak no puede evitarlo.
Un dongle USB Bluetooth 5.4 hizo esto más de 20 veces en un día, en distintos puertos, incluso sin que nada más lo usara. El adaptador
vuelve apagado, así que enciéndelo con `bluetoothctl power on`. Si esto pasa a menudo, prueba con otro adaptador.

**"No se detectó en 45 s" después de un bloqueo del adaptador.** El mismo dongle a veces volvía indicando que estaba encendido, pero sin oír nada en absoluto,
y sin ningún error en el registro. Si tu cámara está encendida y tampoco aparece nada más cercano en una búsqueda de Bluetooth, apaga y vuelve a encender
Bluetooth: `bluetoothctl power off` y luego `bluetoothctl power on`.

## Wi-Fi, clips y vista previa

**"El adaptador Wi-Fi X es la conexión de red de esta computadora".** Quak no tomará tu conexión sin preguntar. Conecta un segundo
adaptador, o permítelo con `--wifi-adapter X`. La app, en cambio, te pregunta.

**"Conectando al Wi-Fi de la cámara" tarda mucho en un teléfono.** Si el Wi-Fi de la cámara estaba apagado, el teléfono puede tardar 30 segundos o más
en verlo.

**El teléfono dice que no se unió, "¿se rechazó el aviso de Conectar?"** Android pregunta una vez antes de unirse a la red de la cámara. Si
no viste el aviso, inténtalo de nuevo: también puede significar que el teléfono no encontró la red a tiempo.

**La vista previa está en blanco.** La cámara detiene su transmisión mientras graba, y por eso el panel lo indica. Después de detener la grabación, o
de cambiar de modo, la transmisión vuelve en unos 10 segundos y la imagen en unos 20.

**"La cámara no inició la vista previa".** La cámara se negó. Inténtalo de nuevo en un momento. Quak ya elimina las transmisiones que dejó activas
una app que se cerró a la fuerza.

**"No llegó imagen de la cámara".** Quak inició la vista previa, pero no llegó video en 30 segundos. Inténtalo de nuevo. Si sigue
pasando en una computadora, comprueba que un firewall permita el tráfico UDP entrante en el puerto 8554, que es donde la cámara envía su video.

**El Wi-Fi de la cámara sigue encendido.** Una app que se cierra a la fuerza durante la vista previa no puede apagarlo. Ejecuta `quak ap off`.

**Mi computadora perdió su Wi-Fi después de que Quak se cerrara a la fuerza.** Consulta [Si Quak se cierra a la fuerza](safety.md#si-quak-se-cierra-a-la-fuerza).

## La app

**`quak-gui` dice que no hay pantalla.** Estás en una máquina sin pantalla, por ejemplo por SSH. `quak-gui --web` la sirve a un navegador
en la misma máquina.

**El primer inicio parece colgarse.** El cliente de escritorio pesa unos 45 MB y se descarga una sola vez.

**La app de Android no se actualiza.** Los APK firmados con claves distintas no pueden actualizarse entre sí. Desinstala primero el antiguo, lo que
elimina su emparejamiento y sus descargas.

## Problemas conocidos

**La transmisión en vivo puede atascarse al unirse a una red (experimental).** La cámara se conecta a la red Wi-Fi y termina el protocolo de enlace,
pero nunca pide una dirección IP, así que la conexión agota el tiempo de espera a los 60 segundos. La transmisión en vivo funcionó de principio a fin en la cámara de pruebas, incluso
dos veces el día en que esto empezó, y después todos los intentos posteriores se atascaron. En la cámara afectada:

| Se probó | Resultado |
|---|---|
| Suspender y reactivar la cámara | Sigue atascándose |
| Reiniciar la cámara | Sigue atascándose |
| Desconectar y volver a conectar el adaptador Wi-Fi de la computadora | Sigue atascándose |
| Un nombre de red que la cámara nunca había visto | Sigue atascándose |
| Un teléfono que se une a la misma red | Funciona |

La red en sí está bien, así que el silencio viene de la cámara. No se sabe por qué, ni si lo causó algo que envió Quak.
**Restablecer conexiones** en la cámara no se ha probado, porque también elimina todos los emparejamientos. Si te pasa esto o logras resolverlo, por favor
[abre un issue](https://github.com/ADCDS/quak/issues).

**Un toque puede perderse de vez en cuando.** Algunas pantallas se redibujan por completo cuando la cámara informa de un cambio, y un toque que llega en ese momento
se pierde. Vuelve a tocar. Clips y Diagnóstico ya no hacen esto.

**Windows nunca se ha ejecutado con una cámara.** Es experimental, así que, por favor, cuéntanos lo que encuentres.
