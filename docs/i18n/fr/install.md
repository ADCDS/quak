> Cette page est une traduction automatique et peut contenir des erreurs. La [version anglaise](https://github.com/ADCDS/quak/blob/main/docs/install.md) fait référence. [Les corrections sont les bienvenues](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md#translations).

# Installer Quak

Vous n'avez pas besoin de caméra pour l'essayer : chaque commande accepte `--sim`, qui utilise une caméra simulée.

## Linux

Il vous faut :

- Python 3.11 ou plus récent
- Pour une vraie caméra : un adaptateur Bluetooth et BlueZ (la plupart des distributions de bureau l'incluent), et NetworkManager avec `nmcli` pour rejoindre
  le Wi-Fi de la caméra
- FFmpeg, pour l'aperçu en direct dans l'application de bureau et pour `quak preview --play`, qui nécessite aussi `ffplay`

```bash
pipx install 'quak-camera[gui]'     # or 'quak-camera' for just the command line
```

Le paquet s'appelle `quak-camera` parce que `quak` sur PyPI est un projet sans rapport. Les commandes sont `quak` et `quak-gui`.

Ou téléchargez le `.tar.gz` depuis [Releases](https://github.com/ADCDS/quak/releases), décompressez-le et lancez `./quak`. Il nécessite glibc 2.34 ou
plus récent (Ubuntu 22.04, Debian 12), GTK 3 et libmpv : sur Debian et Ubuntu, `sudo apt install libgtk-3-0 libmpv2`.

Le premier lancement de `quak-gui` télécharge le client de bureau Flet, environ 45 Mo, dans `~/.flet`.

Un second adaptateur Wi-Fi est utile, par exemple un adaptateur USB bon marché, car l'adaptateur qui rejoint la caméra quitte le réseau
sur lequel il se trouvait. Choisissez-le avec `--wifi-adapter NAME` ou `QUAK_WIFI_ADAPTER`. Avec un seul adaptateur, Quak demande d'abord.

## Windows (expérimental)

Cette version n'a jamais été utilisée avec une caméra, ni même lancée : attendez-vous à des problèmes et merci de les signaler.

```powershell
py -m venv quak-env
quak-env\Scripts\activate
pip install "quak-camera[gui]"
```

Ou téléchargez le `.zip` depuis [Releases](https://github.com/ADCDS/quak/releases), décompressez-le et lancez `quak.exe`. Il n'est pas signé,
donc Windows peut afficher un avertissement.

## Android

Téléchargez l'APK depuis [Releases](https://github.com/ADCDS/quak/releases) et installez-le. Android vous demande d'autoriser l'installation depuis
votre navigateur ou votre gestionnaire de fichiers. Il nécessite Android 10 ou plus récent, et demande les autorisations Bluetooth et Localisation : Android exige l'autorisation de localisation
pour une recherche Bluetooth.

Pour vérifier qu'un APK provient bien de ce projet, comparez l'empreinte SHA-256 du certificat donnée par `apksigner verify --print-certs` avec celle-ci :

```text
fec9c12dc325dd08272a9163c60e033519d74ebd98a31e133b7b145b259bd59b
```

La première fois que Quak rejoint le Wi-Fi de la caméra, Android vous demande de confirmer la connexion.

Les téléchargements restent dans l'application jusqu'à ce que vous en touchiez un pour le partager ou l'enregistrer. Désinstaller l'application supprime ceux que vous n'avez pas partagés.

## Trouver votre caméra

```bash
quak scan                   # lists cameras nearby, with their addresses
quak pair                   # pairs the one on its pairing screen
quak pair --address AA:BB:CC:DD:EE:FF    # or choose one
```

Pour ouvrir l'écran d'association sur la caméra : **Préférences > Connexions sans fil > Connecter appareil > GoPro Quik (Preferences > Connections > Connect Device > GoPro Quik App)**.

## Compiler les paquets

La plupart des gens n'en ont pas besoin : des versions prêtes à l'emploi se trouvent sur la page [Releases](https://github.com/ADCDS/quak/releases), avec un fichier `SHA256SUMS`
pour vérifier les téléchargements. Pour compiler les vôtres :

```bash
git clone https://github.com/ADCDS/quak.git && cd quak
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[gui]'
tools/build_app.sh linux      # needs clang, cmake, ninja, pkg-config, GTK 3 headers, liblzma and libmpv
tools/build_app.sh windows    # on Windows, from Git Bash
tools/build_app.sh apk        # needs Java 17
```

Flet télécharge Flutter et le SDK Android la première fois. Le résultat se trouve dans `app/build/`.

Les mainteneurs peuvent aussi lancer le workflow `build` depuis l'onglet Actions du dépôt.

Les APK signés avec des clés différentes ne peuvent pas se mettre à jour entre eux : pour passer d'une version à l'autre, désinstallez d'abord l'ancienne. Cela supprime
l'association de l'application et ses téléchargements.
