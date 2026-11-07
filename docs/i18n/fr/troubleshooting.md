> Cette page est une traduction automatique et peut contenir des erreurs. La [version anglaise](https://github.com/ADCDS/quak/blob/main/docs/troubleshooting.md) fait référence. [Les corrections sont les bienvenues](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md#translations).

# Dépannage

## Association et connexion

**« Aucune caméra n'est sur son écran d'association » ("No camera is on its pairing screen").** Une caméra ignore les appareils qu'elle ne connaît pas, sauf si cet écran est ouvert : **Préférences > Connexions sans fil > Connecter appareil > GoPro Quik (Preferences > Connections > Connect Device > GoPro Quik App)**. Ouvrez-le, puis relancez `quak pair`.

**Associer le téléphone puis l'ordinateur échoue.** Se connecter depuis un autre appareil ferme l'écran d'association de la caméra. Associez un
appareil à la fois, et rouvrez l'écran avant d'associer le suivant.

**La caméra est « non détectée en 45 s » ("not seen within 45 s").** Vérifiez qu'elle est allumée et à proximité. Quand elle n'est pas en mode association, une HERO11 peut ne se signaler que
toutes les 10 à 20 secondes, et une caméra en veille peut mettre plus longtemps. Réessayez, ou appuyez sur un bouton de la caméra pour la réveiller.

**Elle se connecte, mais la configuration ne se termine jamais.** Une fois, après une coupure d'alimentation alors qu'elle était branchée en USB, une caméra a cessé de proposer ses
services Bluetooth jusqu'à son redémarrage. Éteignez la caméra puis rallumez-la.

**La caméra refuse d'enregistrer ou de modifier des réglages, sans donner de raison.** Elle est peut-être bloquée en mode Turbo Transfer, qui bloque
l'enregistrement, les réglages et l'aperçu. Quak active ce mode pour les téléchargements : une connexion coupée pendant un téléchargement peut donc le laisser activé. L'application le désactive
à sa connexion suivante, et la ligne de commande le signale dans son message d'erreur : lancez `quak turbo off`.

## Erreurs Bluetooth

**« Le Bluetooth a cessé de répondre » ("Bluetooth stopped responding").** Vérifiez que le Bluetooth est activé. Si cela continue sous Linux, consultez le journal du noyau :

```bash
journalctl -k | grep "tx timeout"
```

Une ligne telle que `command 0x2041 tx timeout`, suivie d'une déconnexion USB, signifie que l'adaptateur Bluetooth a planté. Quak ne peut pas l'empêcher.
Une clé USB Bluetooth 5.4 l'a fait plus de 20 fois en une journée, sur différents ports, même sans que rien d'autre ne l'utilise. L'adaptateur
revient désactivé : activez-le avec `bluetoothctl power on`. Si cela arrive souvent, essayez un autre adaptateur.

**« Non détectée en 45 s » ("Not seen within 45 s") après un plantage de l'adaptateur.** La même clé revenait parfois en se disant activée, tout en ne captant absolument rien,
sans aucune erreur dans le journal. Si votre caméra est allumée et que rien d'autre à proximité n'apparaît non plus dans une recherche Bluetooth, désactivez puis réactivez
le Bluetooth : `bluetoothctl power off`, puis `bluetoothctl power on`.

## Wi-Fi, médias et aperçu

**« L'adaptateur Wi-Fi X assure la connexion réseau de cet ordinateur » ("Wi-Fi adapter X is this computer's network connection").** Quak ne prendra pas votre connexion sans demander. Branchez un second
adaptateur, ou autorisez-le avec `--wifi-adapter X`. L'application vous pose la question à la place.

**« Connexion au Wi-Fi de la caméra » ("Joining the camera's Wi-Fi") prend longtemps sur un téléphone.** Si le Wi-Fi de la caméra était désactivé, le téléphone peut mettre 30 secondes ou plus
à le voir.

**Le téléphone dit qu'il n'a pas rejoint le réseau, « la demande de connexion a-t-elle été refusée ? » ("was the Connect prompt declined?")** Android demande une fois avant de rejoindre le réseau de la caméra. Si
vous n'avez pas vu la demande, réessayez : cela peut aussi signifier que le téléphone n'a pas trouvé le réseau à temps.

**L'aperçu reste vide.** La caméra arrête son flux pendant l'enregistrement, et c'est pourquoi le panneau l'indique. Après l'arrêt de l'enregistrement, ou
après un changement de mode, le flux revient en 10 secondes environ et l'image en 20 secondes environ.

**« La caméra n'a pas lancé son aperçu » ("The camera didn't start its preview").** La caméra a refusé. Réessayez dans un instant. Quak arrête déjà un flux laissé en cours par
une application arrêtée de force.

**« Aucune image reçue de la caméra » ("No picture came from the camera").** Quak a lancé l'aperçu, mais aucune vidéo n'est arrivée en 30 secondes. Réessayez. Si cela continue
sur un ordinateur, vérifiez qu'un pare-feu autorise l'UDP entrant sur le port 8554, par lequel la caméra envoie sa vidéo.

**Le Wi-Fi de la caméra reste activé.** Une application arrêtée de force pendant l'aperçu ne peut pas le désactiver. Lancez `quak ap off`.

**Mon ordinateur a perdu son Wi-Fi après l'arrêt forcé de Quak.** Voir [Si Quak est arrêté de force](safety.md#si-quak-est-arrêté-de-force).

## L'application

**`quak-gui` indique qu'il n'y a pas d'écran.** Vous êtes sur une machine sans écran, par exemple via SSH. `quak-gui --web` sert l'application à un navigateur
sur la même machine.

**Le premier lancement semble bloqué.** Le client de bureau fait environ 45 Mo et n'est téléchargé qu'une fois.

**L'application Android ne se met pas à jour.** Les APK signés avec des clés différentes ne peuvent pas se mettre à jour entre eux. Désinstallez d'abord l'ancien, ce qui
supprime son association et ses téléchargements.

## Problèmes connus

**La diffusion en direct peut se bloquer en rejoignant un réseau (expérimental).** La caméra se connecte au réseau Wi-Fi et termine la négociation,
mais ne demande jamais d'adresse IP : l'opération expire donc au bout de 60 secondes. La diffusion en direct a fonctionné de bout en bout sur la caméra de test, y compris
deux fois le jour où le problème a commencé, puis toutes les tentatives suivantes se sont bloquées. Sur la caméra concernée :

| Essai | Résultat |
|---|---|
| Mettre en veille et réveiller la caméra | Toujours bloqué |
| Redémarrer la caméra | Toujours bloqué |
| Débrancher et rebrancher l'adaptateur Wi-Fi de l'ordinateur | Toujours bloqué |
| Un nom de réseau que la caméra n'avait jamais vu | Toujours bloqué |
| Un téléphone rejoignant le même réseau | Fonctionne |

Le réseau lui-même fonctionne, donc le silence vient de la caméra. On ne sait pas pourquoi, ni si quelque chose envoyé par Quak en est la cause.
**Réinitialiser les connexions (Reset Connections)** sur la caméra n'a pas été essayé, car cela supprime aussi toutes les associations. Si vous rencontrez ce problème ou parvenez à le contourner, merci
d'[ouvrir un ticket](https://github.com/ADCDS/quak/issues).

**Un appui peut parfois être manqué.** Certains écrans se redessinent entièrement quand la caméra signale un changement, et un appui à ce moment-là
est perdu. Appuyez de nouveau. Médias et Diagnostic ne le font plus.

**Windows n'a jamais été utilisé avec une caméra.** C'est expérimental : merci de signaler ce que vous constatez.
