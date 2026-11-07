> Esta página es una traducción automática y puede contener errores. La [versión en inglés](https://github.com/ADCDS/quak/blob/main/docs/safety.md) es la referencia. [Las correcciones son bienvenidas](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md#translations).

# Qué cambia, guarda y puede eliminar Quak

Quak controla una cámara real y puede modificar el Wi-Fi de tu computadora. Esta página explica exactamente lo que hace.

## En tu computadora

**Bluetooth:** Quak se empareja con la cámara de la forma normal, a través del Bluetooth de tu sistema. No cambia nada más.

**Wi-Fi:** la vista previa, los clips y las descargas necesitan la propia red Wi-Fi de la cámara.

- **Teléfono:** Android se conecta solo para la app, y tus otras apps siguen en tu red habitual.
- **Computadora:** un adaptador Wi-Fi se une a la red de la cámara durante un rato, y abandona cualquier red en la que estuviera hasta que Quak termina.

En una computadora, Quak:

- Prefiere un adaptador que no estés usando para conectarte a internet. Si el único adaptador es el que estás usando, la app pregunta antes, y la línea
  de comandos se detiene con un error hasta que lo elijas con `--wifi-adapter NAME`.
- En Linux, crea un perfil temporal de NetworkManager llamado `quak-camap`, que nunca toma tu ruta predeterminada ni tu DNS, y lo
  elimina después. No edita tus otros perfiles.
- En Linux, desactiva la conexión automática en ese adaptador mientras lo usa, porque de lo contrario un perfil guardado para la cámara podría volver a
  tomar el adaptador. La conexión automática se restaura después.
- Vuelve a conectar tu red anterior cuando termina. La app lo hace aproximadamente un minuto después de que salgas de Clips o detengas la vista previa.

### Si Quak se cierra a la fuerza

La línea de comandos limpia todo al recibir Ctrl-C y `kill`, y deja como estaba lo que cambió. Si se cierra de forma más brusca, o la máquina se queda sin
corriente en pleno uso, Quak recuerda lo que cambió y lo deshace la próxima vez que se inicia en Linux. En Windows, no hay recuperación
automática.

Para recuperarlo a mano en Linux, donde `DEVICE` es tu adaptador Wi-Fi:

```bash
nmcli connection delete quak-camap
nmcli device set DEVICE autoconnect yes
nmcli device connect DEVICE
```

Reiniciar también elimina el cambio de la conexión automática, y solo queda por eliminar el perfil `quak-camap`.

### Qué se guarda

| Qué | Dónde | Contiene |
|---|---|---|
| Cámaras emparejadas, ajustes | `~/.config/quak` en Linux, `%APPDATA%\quak` en Windows, el almacenamiento privado de la app en Android | Direcciones de las cámaras, modelos, preferencias de la app |
| Descargas | `~/Videos/Quak` para la app de escritorio, el almacenamiento privado de la app en Android, y `--out` o la carpeta actual para la línea de comandos | Tus clips |
| Datos de prueba | `~/.local/state/quak` en Linux, `%LOCALAPPDATA%\quak` en Windows | Solo cuando ejecutas las suites de pruebas |

Quak no guarda tus contraseñas de Wi-Fi ni tus claves de transmisión. Las envía a la cámara cuando le pides que se una a una red o que transmita.

### Qué hay en los registros

La pantalla Diagnóstico, su registro exportado y `quak -v` muestran los bytes intercambiados con la cámara. Omiten contraseñas, claves de
transmisión, nombres de red, el número de serie de la cámara y el nombre de tu computadora, e indican cuántos bytes se ocultaron.

La dirección Bluetooth de la cámara todavía puede aparecer en mensajes de error y registros, así que revisa un registro antes de compartirlo públicamente.

### Acceso de la app a la red

`quak-gui --web` sirve la app en un navegador, solo en esta computadora. **No hay inicio de sesión**, y cualquiera que pueda acceder a ella puede grabar
en tu cámara y eliminar contenido de ella, así que `--host` solo tiene sentido en una red en la que confíes plenamente.

## En la cámara

**Quak le dice a la cámara el nombre de tu computadora** al emparejar, como el nombre que la cámara muestra para este dispositivo.

**Una captura programada cambia ajustes y los deja cambiados.** Al programar una:

- Cambia la cámara a Control Pro, porque la cámara oculta la captura programada en Fácil
- Carga el preajuste de video que elijas
- Ajusta la Duración de captura, lo que **también limita las grabaciones que inicies a mano**
- Sincroniza el reloj de la cámara

Quak te avisa cuando programar cambia estas cosas. Vuelve a cambiar la Duración en los ajustes de la cámara, o en Quak, cuando termines.

**Transmisión en vivo:**

- La cámara se une a la red Wi-Fi que elijas y la guarda. Solo **Restablecer conexiones** en la cámara elimina una red guardada,
  y eso también elimina todos sus emparejamientos.
- La contraseña de tu red y la URL de tu transmisión, que contiene tu clave de transmisión, se envían a la cámara por Bluetooth.
- Con una URL `rtmp://` simple, la clave de transmisión y el video viajan sin cifrar hasta el servidor.

**El Wi-Fi de la cámara** se enciende cuando hace falta y se vuelve a apagar cuando Quak termina, si fue Quak quien lo encendió. Una app que se cierra a la fuerza
durante la vista previa puede dejarlo encendido: ejecuta `quak ap off` o apágalo en la cámara.

## Qué puede eliminar Quak

Solo los clips que elijas. Eliminar uno siempre pide confirmación en la app, y necesita `--yes` en la línea de comandos. Los clips eliminados
no se pueden recuperar.

Quak no tiene forma de formatear una tarjeta, eliminarlo todo ni restablecer una cámara a los valores de fábrica.

Las descargas nunca reemplazan un archivo existente: un segundo `GX010001.MP4` se guarda como `GX010001_1.MP4`. Una descarga que se interrumpe se
notifica como error, y no se guarda como si estuviera completa.

## Para desarrolladores

`QUAK_DEV=1` muestra la autoprueba de la app y el modo de prueba de En vivo. Cambian mucho más, así que lee primero [testing.md](https://github.com/ADCDS/quak/blob/main/docs/testing.md):

- **Autoprueba:** tarda unos 30 minutos, graba clips, cambia ajustes y los restaura, y usa el adaptador Wi-Fi.
- **Modo de prueba:** convierte esta computadora en un punto de acceso, y la cámara guarda esa red.
