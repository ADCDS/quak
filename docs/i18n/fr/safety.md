> Cette page est une traduction automatique et peut contenir des erreurs. La [version anglaise](https://github.com/ADCDS/quak/blob/main/docs/safety.md) fait référence. [Les corrections sont les bienvenues](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md#translations).

# Ce que Quak modifie, stocke et peut supprimer

Quak contrôle une vraie caméra et peut toucher au Wi-Fi de votre ordinateur. Cette page indique exactement ce qu'il fait.

## Sur votre ordinateur

**Bluetooth :** Quak s'associe à la caméra de la manière habituelle, via le Bluetooth de votre système. Il ne modifie rien d'autre.

**Wi-Fi :** l'aperçu, les médias et les téléchargements ont besoin du réseau Wi-Fi de la caméra elle-même.

- **Téléphone :** Android se connecte uniquement pour l'application, et vos autres applications restent sur votre réseau habituel.
- **Ordinateur :** un adaptateur Wi-Fi rejoint le réseau de la caméra pendant un moment, et quitte le réseau sur lequel il se trouvait jusqu'à ce que Quak ait terminé.

Sur un ordinateur, Quak :

- Préfère un adaptateur par lequel vous n'êtes pas en ligne. Si le seul adaptateur est celui que vous utilisez, l'application demande d'abord, et la ligne
  de commande s'arrête avec une erreur tant que vous ne le choisissez pas avec `--wifi-adapter NAME`.
- Sous Linux, crée un profil NetworkManager temporaire nommé `quak-camap`, qui ne prend jamais votre route par défaut ni votre DNS, et le
  supprime ensuite. Il ne modifie pas vos autres profils.
- Sous Linux, désactive la connexion automatique sur cet adaptateur pendant qu'il l'utilise, car un profil enregistré pour la caméra pourrait sinon reprendre
  l'adaptateur. La connexion automatique est rétablie ensuite.
- Reconnecte votre réseau précédent quand il a terminé. L'application le fait environ une minute après que vous avez quitté Médias ou arrêté l'aperçu.

### Si Quak est arrêté de force

La ligne de commande fait le ménage sur Ctrl-C et sur `kill`, et remet en place ce qu'elle a modifié. S'il est arrêté plus brutalement, ou si la machine perd son
alimentation en cours d'utilisation, Quak se souvient de ce qu'il a modifié et l'annule à son prochain démarrage sous Linux. Sous Windows, il n'y a pas de récupération
automatique.

Pour tout rétablir à la main sous Linux, où `DEVICE` est votre adaptateur Wi-Fi :

```bash
nmcli connection delete quak-camap
nmcli device set DEVICE autoconnect yes
nmcli device connect DEVICE
```

Un redémarrage annule aussi le changement de connexion automatique, et ne laisse que le profil `quak-camap` à supprimer.

### Ce qui est stocké

| Quoi | Où | Contenu |
|---|---|---|
| Caméras associées, réglages | `~/.config/quak` sous Linux, `%APPDATA%\quak` sous Windows, le stockage privé de l'application sous Android | Adresses des caméras, modèles, préférences de l'application |
| Téléchargements | `~/Videos/Quak` pour l'application de bureau, le stockage privé de l'application sous Android, et `--out` ou le dossier courant pour la ligne de commande | Vos clips |
| Données de test | `~/.local/state/quak` sous Linux, `%LOCALAPPDATA%\quak` sous Windows | Uniquement quand vous lancez les suites de tests |

Quak ne stocke pas vos mots de passe Wi-Fi ni vos clés de diffusion. Il les envoie à la caméra quand vous lui demandez de rejoindre un réseau ou de diffuser.

### Ce que contiennent les journaux

L'écran Diagnostic, son journal exporté et `quak -v` montrent les octets échangés avec la caméra. Ils omettent les mots de passe, les clés
de diffusion, les noms de réseau, le numéro de série de la caméra et le nom de votre ordinateur, et ils indiquent combien d'octets ont été masqués.

L'adresse Bluetooth de la caméra peut quand même apparaître dans les messages d'erreur et les journaux : relisez donc un journal avant de le partager publiquement.

### L'accès réseau de l'application

`quak-gui --web` sert l'application dans un navigateur, sur cet ordinateur uniquement. **Il n'y a pas d'authentification**, et toute personne qui peut y accéder peut enregistrer
avec votre caméra et en supprimer des fichiers : `--host` n'a donc de sens que sur un réseau auquel vous faites entièrement confiance.

## Sur la caméra

**Quak indique le nom de votre ordinateur à la caméra** lors de l'association, comme nom que la caméra affiche pour cet appareil.

**Une capture programmée modifie des réglages et les laisse modifiés.** L'armer :

- Fait passer la caméra en Commandes professionnelles, car la caméra masque la capture programmée en Commandes faciles
- Charge le préréglage vidéo que vous choisissez
- Règle Durée vidéo, ce qui **limite aussi les enregistrements que vous lancez à la main**
- Synchronise l'horloge de la caméra

Quak vous le signale quand l'armement modifie ces réglages. Rétablissez Durée dans les réglages de la caméra, ou dans Quak, quand vous avez terminé.

**Diffusion en direct :**

- La caméra rejoint le réseau Wi-Fi que vous choisissez et le mémorise. Seule l'option **Réinitialiser les connexions (Reset Connections)** de la caméra supprime un réseau mémorisé,
  et elle supprime aussi toutes ses associations.
- Le mot de passe de votre réseau et l'URL de votre flux, qui contient votre clé de diffusion, sont envoyés à la caméra via Bluetooth.
- Avec une simple URL `rtmp://`, la clé de diffusion et la vidéo voyagent sans chiffrement jusqu'au serveur.

**Le Wi-Fi de la caméra** est activé quand c'est nécessaire, et désactivé quand Quak a terminé, si c'est Quak qui l'a activé. Une application arrêtée de force
pendant l'aperçu peut le laisser activé : lancez `quak ap off`, ou désactivez-le sur la caméra.

## Ce que Quak peut supprimer

Uniquement les clips que vous choisissez. Supprimer un clip demande toujours une confirmation dans l'application, et nécessite `--yes` en ligne de commande. Les clips supprimés
ne peuvent pas être récupérés.

Quak n'a aucun moyen de formater une carte, de tout supprimer ou de rétablir les réglages d'usine d'une caméra.

Les téléchargements ne remplacent jamais un fichier existant : un second `GX010001.MP4` est enregistré sous le nom `GX010001_1.MP4`. Un téléchargement interrompu est
signalé comme une erreur, et n'est pas enregistré comme s'il était complet.

## Pour les développeurs

`QUAK_DEV=1` affiche l'autotest de l'application et le mode test de Direct. Ils modifient beaucoup plus de choses : lisez donc d'abord [testing.md](https://github.com/ADCDS/quak/blob/main/docs/testing.md) :

- **Autotest :** dure environ 30 minutes, enregistre des clips, modifie des réglages puis les restaure, et utilise l'adaptateur Wi-Fi.
- **Mode test :** transforme cet ordinateur en point d'accès, et la caméra mémorise ce réseau.
