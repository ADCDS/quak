> Esta página es una traducción automática y puede contener errores. La [versión en inglés](https://github.com/ADCDS/quak/blob/main/docs/install.md) es la referencia. [Las correcciones son bienvenidas](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md#translations).

# Instalar Quak

No necesitas una cámara para probarlo: todos los comandos aceptan `--sim`, que usa una cámara simulada.

## Linux

Necesitas:

- Python 3.11 o posterior
- Para una cámara real: un adaptador Bluetooth y BlueZ (la mayoría de las distribuciones de escritorio lo incluyen), y NetworkManager con `nmcli` para unirse
  al Wi-Fi de la cámara
- FFmpeg, para la vista previa en vivo en la app de escritorio y para `quak preview --play`, que también necesita `ffplay`

```bash
pipx install 'quak-camera[gui]'     # or 'quak-camera' for just the command line
```

El paquete se llama `quak-camera` porque `quak` en PyPI es un proyecto sin relación. Los comandos son `quak` y `quak-gui`.

O descarga el `.tar.gz` desde [Releases](https://github.com/ADCDS/quak/releases), descomprímelo y ejecuta `./quak`. Necesita glibc 2.34 o
posterior (Ubuntu 22.04, Debian 12), GTK 3 y libmpv: en Debian y Ubuntu, `sudo apt install libgtk-3-0 libmpv2`.

La primera vez que ejecutas `quak-gui`, descarga el cliente de escritorio de Flet, de unos 45 MB, en `~/.flet`.

Conviene tener un segundo adaptador Wi-Fi, por ejemplo uno USB barato, porque el adaptador que se une a la cámara abandona la
red en la que estuviera. Elígelo con `--wifi-adapter NAME` o `QUAK_WIFI_ADAPTER`. Con un solo adaptador, Quak pregunta antes.

## Windows (experimental)

Esto no se ha ejecutado con una cámara, ni se ha abierto siquiera, así que espera problemas y, por favor, avísanos de ellos.

```powershell
py -m venv quak-env
quak-env\Scripts\activate
pip install "quak-camera[gui]"
```

O descarga el `.zip` desde [Releases](https://github.com/ADCDS/quak/releases), descomprímelo y ejecuta `quak.exe`. No tiene firma de código,
así que puede que Windows muestre una advertencia.

## Android

Descarga el APK desde [Releases](https://github.com/ADCDS/quak/releases) e instálalo. Android te pide que permitas instalar desde
tu navegador o gestor de archivos. Necesita Android 10 o posterior, y pide Bluetooth y Ubicación: Android exige el permiso de Ubicación
para buscar por Bluetooth.

Para comprobar que un APK proviene de este proyecto, compara el resumen SHA-256 del certificado que da `apksigner verify --print-certs` con este:

```text
fec9c12dc325dd08272a9163c60e033519d74ebd98a31e133b7b145b259bd59b
```

La primera vez que Quak se une al Wi-Fi de la cámara, Android te pide que confirmes la conexión.

Las descargas se quedan dentro de la app hasta que tocas una para compartirla o guardarla. Al desinstalar la app se eliminan las que no hayas compartido.

## Encontrar tu cámara

```bash
quak scan                   # lists cameras nearby, with their addresses
quak pair                   # pairs the one on its pairing screen
quak pair --address AA:BB:CC:DD:EE:FF    # or choose one
```

Para abrir la pantalla de emparejamiento en la cámara: **Preferencias > Conexiones > Conectar dispositivo > GoPro Quik App**.

## Compilar los paquetes

La mayoría de la gente no lo necesita: las versiones empaquetadas están en la página de [Releases](https://github.com/ADCDS/quak/releases), con un archivo `SHA256SUMS`
para comprobar las descargas. Para compilar las tuyas:

```bash
git clone https://github.com/ADCDS/quak.git && cd quak
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[gui]'
tools/build_app.sh linux      # needs clang, cmake, ninja, pkg-config, GTK 3 headers, liblzma and libmpv
tools/build_app.sh windows    # on Windows, from Git Bash
tools/build_app.sh apk        # needs Java 17
```

Flet descarga Flutter y el SDK de Android la primera vez. El resultado queda en `app/build/`.

Los responsables del proyecto también pueden ejecutar el flujo de trabajo `build` desde la pestaña Actions del repositorio.

Los APK firmados con claves distintas no pueden actualizarse entre sí, así que para cambiar de una versión a otra, desinstala primero la antigua. Eso elimina
el emparejamiento de la app y sus descargas.
