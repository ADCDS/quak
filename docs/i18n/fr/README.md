> Cette page est une traduction automatique et peut contenir des erreurs. La [version anglaise](https://github.com/ADCDS/quak/blob/main/README.md) fait référence. [Les corrections sont les bienvenues](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md#translations).

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/src/quak/gui/assets/logo.png" width="128" alt="Logo de Quak : un canard jaune avec un objectif de caméra à la place de l'œil"></p>

<h1 align="center">Quak</h1>

<p align="center">Une télécommande open source pour la GoPro HERO11 Black : un outil en ligne de commande et une application pour ordinateur et Android.</p>

<p align="center"><a href="https://github.com/ADCDS/quak/blob/main/README.md">English</a> · <a href="../pt-BR/README.md">Português (Brasil)</a> · <a href="../es/README.md">Español</a> · <b>Français</b> · <a href="../de/README.md">Deutsch</a> · <a href="../ru/README.md">Русский</a> · <a href="../zh-CN/README.md">简体中文</a> · <a href="../ja/README.md">日本語</a></p>

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/hero.png" alt="Quak sur ordinateur et sur téléphone, avec un aperçu en direct d'une caméra simulée"></p>

Quak communique avec la caméra via Bluetooth et le Wi-Fi de la caméra elle-même, en utilisant l'[API Open GoPro](https://gopro.github.io/OpenGoPro/)
publique de GoPro. Sans compte ni cloud.

> **État : projet à ses débuts, testé sur une seule caméra.** Il est conçu autour d'une seule HERO11 Black (firmware 2.20). Les
> autres modèles reçoivent un avertissement et peuvent ne pas fonctionner. Consultez [ce qui a été testé](#ce-qui-a-été-testé) avant de vous y fier.

## Essayer sans caméra

Quak inclut une caméra simulée qui parle le vrai protocole : vous pouvez donc tout essayer d'abord. Il vous faut Python 3.11 ou
plus récent.

```bash
pipx install 'quak-camera[gui]'               # or pip, in a virtual environment

quak --sim info                               # the command line
quak --sim schedule arm +2m --duration 15s
quak-gui --sim                                # the app (the first launch downloads about 45 MB)
```

Ou téléchargez une application prête à l'emploi pour Linux, Windows ou Android depuis [Releases](https://github.com/ADCDS/quak/releases). Le
[guide d'installation](install.md) donne les détails pour chaque système.

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/terminal.gif" width="720" alt="La commande quak qui interroge une caméra simulée et arme une capture programmée"></p>

## Ce que fait Quak

- **Contrôle à distance :** filmez, prenez des photos, marquez des HiLights, changez de préréglage et modifiez tous les réglages de la HERO11.
- **Capture programmée :** armez la caméra pour une heure et une durée, après avoir synchronisé son horloge. Elle démarre et s'arrête toute seule,
  même si vous l'éteignez après l'avoir armée.
- **Aperçu en direct** via le Wi-Fi de la caméra. La caméra ne peut pas diffuser pendant qu'elle enregistre : l'aperçu se met donc en pause jusqu'à ce que vous arrêtiez.
- **Médias :** parcourez la carte, téléchargez des clips et supprimez-les, avec une confirmation qui nomme le fichier.
- **Dans votre langue :** l'application suit la langue de votre système, ou vous pouvez la choisir dans **Plus** : English, Português (Brasil),
  Español, Français, Deutsch, Русский, 简体中文 ou 日本語. La ligne de commande reste en anglais.
- **Diffusion en direct** vers un serveur RTMP comme YouTube ou Twitch. Cette fonction est **expérimentale** : voir les [problèmes connus](troubleshooting.md#problèmes-connus).

<p align="center">
  <img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/phone-cameras.png" width="230" alt="L'écran Caméras, avec la batterie, l'espace sur la carte et l'horloge">
  <img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/phone-schedule.png" width="230" alt="Une capture programmée armée, avec un compte à rebours">
  <img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/phone-settings.png" width="230" alt="Les réglages de la caméra, avec les réglages réservés aux Commandes professionnelles grisés et la raison indiquée">
</p>

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/desktop-media.png" width="760" alt="L'écran Médias sur ordinateur : une grille de clips et les détails du clip sélectionné"></p>

Les captures d'écran proviennent de la caméra simulée, avec une scène dessinée à la place de vraies images.

## Utiliser Quak avec votre caméra

Installez-le comme ci-dessus, en ajoutant ce que [le guide d'installation](install.md) indique pour votre système. Associez ensuite la caméra, une seule fois. Sur la caméra, ouvrez
**Préférences > Connexions sans fil > Connecter appareil > GoPro Quik (Preferences > Connections > Connect Device > GoPro Quik App)** et laissez cet écran ouvert pendant que vous lancez :

```bash
quak pair          # finds the camera on its pairing screen and remembers it
quak info
quak record --seconds 5
quak schedule arm +2m --duration 15s
quak media list
```

`quak --help` liste toutes les commandes. Dans l'application, utilisez **Ajouter une caméra**, qui vous guide à travers les mêmes étapes.

**Android :** téléchargez l'APK depuis [Releases](https://github.com/ADCDS/quak/releases). Il nécessite Android 10 ou plus récent.

Avant de l'utiliser pour de vrai, lisez [ce que Quak modifie sur votre ordinateur et votre caméra](safety.md). En bref :

- Sur un ordinateur, afficher les médias ou l'aperçu fait passer un adaptateur Wi-Fi sur le réseau de la caméra pendant un moment. Quak évite
  l'adaptateur par lequel vous êtes en ligne, et demande d'abord si c'est le seul.
- Une capture programmée modifie le réglage Durée de la caméra et le laisse modifié, ce qui limite aussi les enregistrements que vous lancez
  à la main. Quak vous le signale quand cela se produit.

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/how-it-connects.svg" width="760" alt="Le Bluetooth transporte les commandes et les réglages. Le Wi-Fi de la caméra transporte l'aperçu et les médias. Pour une diffusion en direct, la caméra rejoint votre Wi-Fi."></p>

## Ce qui a été testé

Testé sur une seule vraie HERO11 Black, avec les suites de tests de bout en bout sous Linux et à la main sur un seul téléphone Android :

| Fonction | Linux | Android | Windows |
|---|---|---|---|
| Association et reconnexion | Testé | Testé | Non testé |
| État, synchronisation de l'horloge | Testé | Testé | Non testé |
| Enregistrement | Testé | Testé | Non testé |
| HiLights, préréglages, réglages | Testé | Non testé | Non testé |
| Capture programmée | Testé | Non testé | Non testé |
| Aperçu en direct | Testé | Testé | Non testé |
| Médias : liste, téléchargement, suppression | Testé | Testé | Non testé |
| Diffusion en direct | Expérimental | Non testé | Non testé |

**Windows est expérimental.** La version Windows compile et ses tests unitaires passent, mais elle n'a jamais été lancée ni utilisée avec une
caméra. La diffusion en direct a fonctionné de bout en bout, puis a cessé de fonctionner sur la caméra de test pour une raison encore inconnue.
[`docs/testing.md`](https://github.com/ADCDS/quak/blob/main/docs/testing.md) donne les détails.

## Documentation

- [Guide d'installation](install.md) : Linux, Windows et Android, et compilation des paquets
- [Sécurité](safety.md) : ce que Quak modifie et stocke, ce qu'il peut supprimer, et comment revenir en arrière
- [Dépannage](troubleshooting.md) : problèmes courants et problèmes connus
- [Architecture](https://github.com/ADCDS/quak/blob/main/docs/architecture.md) : l'organisation du code
- [Tests](https://github.com/ADCDS/quak/blob/main/docs/testing.md) : les suites de tests et les résultats sur le matériel
- [Notes sur le protocole](https://github.com/ADCDS/quak/blob/main/docs/protocol.md) : ce que nous avons appris sur la caméra, au-delà de la documentation officielle
- [Contribuer](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md)

## Comment Quak a été construit

Quak a été construit avec une forte assistance de l'IA. Claude a écrit la majeure partie du code, des tests et de la documentation, ainsi que le logo, et a exécuté les
suites de tests sur une vraie caméra, sous la direction d'une personne qui l'a aussi essayé sur la caméra et le téléphone. Les affirmations ci-dessus sur le matériel proviennent de
ces tests, et de rien d'autre.

## Licence et avertissement

MIT : voir [LICENSE](https://github.com/ADCDS/quak/blob/main/LICENSE). Les applications empaquetées incluent d'autres logiciels sous leurs propres licences, dont des bibliothèques LGPL : voir
[THIRD_PARTY_NOTICES.md](https://github.com/ADCDS/quak/blob/main/THIRD_PARTY_NOTICES.md).

Quak est un projet indépendant, qui n'est ni affilié à GoPro, Inc., ni approuvé ou sponsorisé par cette société. GoPro, HERO et Quik sont
des marques commerciales de GoPro, Inc. Voir [NOTICE](https://github.com/ADCDS/quak/blob/main/NOTICE).
