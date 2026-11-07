> Esta página es una traducción automática y puede contener errores. La [versión en inglés](https://github.com/ADCDS/quak/blob/main/README.md) es la referencia. [Las correcciones son bienvenidas](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md#translations).

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/src/quak/gui/assets/logo.png" width="128" alt="Logo de Quak: un pato amarillo con una lente de cámara como ojo"></p>

<h1 align="center">Quak</h1>

<p align="center">Un control remoto de código abierto para la GoPro HERO11 Black: una herramienta de línea de comandos y una app para escritorio y Android.</p>

<p align="center"><a href="https://github.com/ADCDS/quak/blob/main/README.md">English</a> · <a href="../pt-BR/README.md">Português (Brasil)</a> · <b>Español</b> · <a href="../fr/README.md">Français</a> · <a href="../de/README.md">Deutsch</a> · <a href="../ru/README.md">Русский</a> · <a href="../zh-CN/README.md">简体中文</a> · <a href="../ja/README.md">日本語</a></p>

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/hero.png" alt="Quak en el escritorio y en el teléfono, con una vista previa en vivo de una cámara simulada"></p>

Quak se comunica con la cámara por Bluetooth y por el propio Wi-Fi de la cámara, usando la
[API pública Open GoPro](https://gopro.github.io/OpenGoPro/) de GoPro. Sin cuenta y sin nube.

> **Estado: fase inicial y probado en una sola cámara.** Está hecho en torno a una HERO11 Black (firmware 2.20). Otros modelos reciben una
> advertencia y puede que no funcionen. Consulta [qué se ha probado](#qué-se-ha-probado) antes de confiar en él.

## Pruébalo sin cámara

Quak incluye una cámara simulada que habla el protocolo real, así que puedes probarlo todo primero. Necesitas Python 3.11 o
posterior.

```bash
pipx install 'quak-camera[gui]'               # or pip, in a virtual environment

quak --sim info                               # the command line
quak --sim schedule arm +2m --duration 15s
quak-gui --sim                                # the app (the first launch downloads about 45 MB)
```

O descarga una app ya lista para Linux, Windows o Android desde [Releases](https://github.com/ADCDS/quak/releases). La
[guía de instalación](install.md) tiene los detalles para cada sistema.

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/terminal.gif" width="720" alt="El comando quak leyendo una cámara simulada y dejando lista una captura programada"></p>

## Qué hace

- **Control remoto:** graba, toma fotos, marca HiLights, cambia preajustes y todos los ajustes de la HERO11.
- **Captura programada:** programa la cámara para una hora y una duración, con su reloj sincronizado antes. Empieza y se detiene sola,
  incluso si la apagas después de programarla.
- **Vista previa en vivo** por el Wi-Fi de la cámara. La cámara no puede transmitir mientras graba, así que la vista previa se pausa hasta que detengas la grabación.
- **Clips:** explora la tarjeta, descarga clips y elimínalos, con una confirmación que nombra el archivo.
- **En tu idioma:** la app sigue el idioma de tu sistema, o puedes elegirlo en **Más**: English, Português (Brasil),
  Español, Français, Deutsch, Русский, 简体中文 o 日本語. La línea de comandos sigue en inglés.
- **Transmisión en vivo** a un servidor RTMP como YouTube o Twitch. Es **experimental**: consulta los [problemas conocidos](troubleshooting.md#problemas-conocidos).

<p align="center">
  <img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/phone-cameras.png" width="230" alt="La pantalla Cámaras, con la batería, el espacio de la tarjeta y el reloj">
  <img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/phone-schedule.png" width="230" alt="Una captura programada lista, con el tiempo que falta para que empiece">
  <img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/phone-settings.png" width="230" alt="Ajustes de la cámara, con los ajustes exclusivos de Pro en gris y el motivo a la vista">
</p>

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/desktop-media.png" width="760" alt="La pantalla Clips en el escritorio: una cuadrícula de clips y los detalles del seleccionado"></p>

Las capturas de pantalla provienen de la cámara simulada, con una escena dibujada en lugar de imágenes reales.

## Úsalo con tu cámara

Instálalo como arriba, junto con lo que [la guía de instalación](install.md) indica para tu sistema. Luego empareja una vez. En la cámara, abre
**Preferencias > Conexiones > Conectar dispositivo > GoPro Quik App** y deja esa pantalla abierta mientras ejecutas:

```bash
quak pair          # finds the camera on its pairing screen and remembers it
quak info
quak record --seconds 5
quak schedule arm +2m --duration 15s
quak media list
```

`quak --help` muestra todos los comandos. En la app, usa **Añadir cámara**, que te guía por los mismos pasos.

**Android:** descarga el APK desde [Releases](https://github.com/ADCDS/quak/releases). Necesita Android 10 o posterior.

Antes de usarlo de verdad, lee [qué cambia Quak en tu computadora y en tu cámara](safety.md). En resumen:

- En una computadora, ver los clips o la vista previa pasa un adaptador Wi-Fi a la red de la cámara durante un rato. Quak evita el
  adaptador que te da acceso a internet, y pregunta antes si es el único.
- Una captura programada cambia el ajuste Duración de la cámara y lo deja cambiado, lo que también limita las grabaciones que inicies
  a mano. Quak te avisa cuando esto ocurre.

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/how-it-connects.svg" width="760" alt="Bluetooth lleva los controles y los ajustes. El propio Wi-Fi de la cámara lleva la vista previa y los clips. Para una transmisión en vivo, la cámara se une a tu Wi-Fi."></p>

## Qué se ha probado

Probado en una sola HERO11 Black real, con las suites de extremo a extremo en Linux y a mano en un solo teléfono Android:

| Función | Linux | Android | Windows |
|---|---|---|---|
| Emparejamiento y reconexión | Probado | Probado | Sin probar |
| Estado, sincronización del reloj | Probado | Probado | Sin probar |
| Grabación | Probado | Probado | Sin probar |
| HiLights, preajustes, ajustes | Probado | Sin probar | Sin probar |
| Captura programada | Probado | Sin probar | Sin probar |
| Vista previa en vivo | Probado | Probado | Sin probar |
| Clips: listar, descargar, eliminar | Probado | Probado | Sin probar |
| Transmisión en vivo | Experimental | Sin probar | Sin probar |

**Windows es experimental.** La versión para Windows se compila y sus pruebas unitarias pasan, pero nunca se ha abierto ni se ha usado con una
cámara. La transmisión en vivo funcionó de principio a fin y luego dejó de funcionar en la cámara de pruebas por un motivo que aún no se conoce.
[`docs/testing.md`](https://github.com/ADCDS/quak/blob/main/docs/testing.md) tiene los detalles.

## Documentación

- [Guía de instalación](install.md): Linux, Windows y Android, y cómo compilar los paquetes
- [Seguridad](safety.md): qué cambia y guarda Quak, qué puede eliminar y cómo recuperarte
- [Solución de problemas](troubleshooting.md): problemas comunes y problemas conocidos
- [Arquitectura](https://github.com/ADCDS/quak/blob/main/docs/architecture.md): cómo está organizado el código
- [Pruebas](https://github.com/ADCDS/quak/blob/main/docs/testing.md): las suites de pruebas y los resultados con hardware
- [Notas del protocolo](https://github.com/ADCDS/quak/blob/main/docs/protocol.md): lo que aprendimos sobre la cámara, más allá de la documentación oficial
- [Cómo contribuir](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md)

## Cómo se hizo

Quak se hizo con mucha ayuda de IA. Claude escribió la mayor parte del código, las pruebas, la documentación y el logo, y ejecutó las suites
de pruebas en una cámara real, bajo la dirección de una persona que también lo probó en la cámara y el teléfono. Las afirmaciones sobre hardware
de arriba provienen de esas pruebas, y de nada más.

## Licencia y aviso legal

MIT: consulta [LICENSE](https://github.com/ADCDS/quak/blob/main/LICENSE). Las apps empaquetadas incluyen otro software bajo sus propias licencias, incluidas bibliotecas LGPL: consulta
[THIRD_PARTY_NOTICES.md](https://github.com/ADCDS/quak/blob/main/THIRD_PARTY_NOTICES.md).

Quak es un proyecto independiente, sin afiliación, respaldo ni patrocinio de GoPro, Inc. GoPro, HERO y Quik son
marcas comerciales de GoPro, Inc. Consulta [NOTICE](https://github.com/ADCDS/quak/blob/main/NOTICE).
