# Outils Traces & Photos — version Android (Kivy)

Réécriture de `start.py` (appli desktop tkinter) en application Android
**autonome** (APK), à l'aide de [Kivy](https://kivy.org) + [Buildozer](https://buildozer.readthedocs.io).

## État actuel

| Fonctionnalité              | État                                   |
|------------------------------|-----------------------------------------|
| Conversion GPX/KMZ/KML       | ✅ Fonctionnelle                        |
| Numérotation                 | ⏳ En attente du code Python source     |
| Fusion                       | ⏳ En attente du code Python source     |
| Carte / Découpe              | ⏳ En attente du code Python source     |
| Statistiques                 | ⏳ En attente du code Python source     |
| Photos                       | ⏳ En attente du code Python source     |
| Live (suivi GPSLogger)       | ⏳ En attente du code Python source     |

Les 6 fonctionnalités en attente apparaissent déjà dans le menu déroulant
(bouton **Menu ▾** en haut de l'écran) et affichent un écran "à venir".
Quand tu m'enverras le code Python de chacune, je créerai son propre
`Screen` Kivy (fichier dédié dans `screens/`) et je l'accrocherai au menu
à la place de l'écran "à venir" — sans toucher au reste de l'appli.

## Pourquoi une réécriture et pas juste "packager" start.py ?

`tkinter`, `matplotlib` (backend TkAgg) et `tkintermapview` ne fonctionnent
pas sur Android (pas de serveur graphique X11 disponible nativement).
Kivy est le framework qui permet de compiler un vrai `.py` en APK
installable, avec :
- `FileChooserListView` à la place de `tkinter.filedialog`
- des `Screen`/`ScreenManager` à la place des onglets `ttk.Notebook`
- (à prévoir plus tard) `kivy_garden.mapview` à la place de `tkintermapview`
- (à prévoir plus tard) un graphique Kivy natif ou `matplotlib` avec le
  backend `Agg` (image statique) à la place du canvas Tkinter interactif

Toute la logique **non graphique** de `start.py` (calculs, lecture/écriture
GPX/KML/KMZ, Haversine, interpolation, lissage) a été reprise **sans
modification fonctionnelle** dans `gps_logic.py` — c'est exactement le
même comportement, seule l'interface change.

## Compiler l'APK — méthode recommandée : GitHub Actions (cloud, rien à installer)

Buildozer est capricieux à installer localement sous Windows. La solution
la plus fiable est de laisser un serveur GitHub s'en charger via le
fichier `.github/workflows/build-apk.yml` déjà inclus dans ce projet :

1. Crée un nouveau dépôt sur [github.com](https://github.com) (public ou privé).
2. Envoie (push) tout le contenu de ce dossier dedans :
   ```bash
   git init
   git add .
   git commit -m "Première version Kivy"
   git branch -M main
   git remote add origin https://github.com/TON-COMPTE/NOM-DU-DEPOT.git
   git push -u origin main
   ```
3. Va dans l'onglet **Actions** de ton dépôt GitHub : le workflow
   "Build APK Android" se lance automatiquement (ou clique sur
   "Run workflow" pour le relancer manuellement).
4. Compte 10-20 minutes. Une fois terminé (coche verte), ouvre le run
   terminé → section **Artifacts** en bas de page → télécharge
   `outilstraces-apk` (un .zip contenant l'APK).
5. Décompresse, récupère le `.apk`, transfère-le sur le Redmi Note 15 Pro
   et installe-le comme d'habitude.

Chaque fois que tu modifies le code et que tu fais un nouveau `git push`,
une nouvelle version de l'APK est recompilée automatiquement.

## Compiler l'APK en local (alternative, si tu préfères)

Buildozer ne fonctionne que sous Linux (nativement, ou via WSL2 sous
Windows). Depuis le dossier du projet :

```bash
pip install buildozer cython
sudo apt install -y git zip unzip openjdk-17-jdk python3-pip autoconf \
    libtool pkg-config zlib1g-dev libncurses5-dev libncursesw5-dev \
    libtinfo5 cmake libffi-dev libssl-dev
buildozer -v android debug
```

La première compilation télécharge le SDK/NDK Android (~10-15 minutes,
plusieurs Go). L'APK généré se trouve dans `bin/outilstraces-0.1-arm64-v8a-debug.apk`.

Transfère-le sur le Redmi Note 15 Pro (câble, ou `adb install bin/*.apk`
si le débogage USB est activé), puis installe-le en autorisant les
"sources inconnues" si demandé.

## Après l'installation sur le téléphone

Android bloque l'octroi automatique de l'accès complet au stockage. Au
premier lancement, l'appli tente d'ouvrir directement l'écran de réglage
correspondant. Si ce n'est pas le cas, autorise-le manuellement :

**Réglages → Applications → Outils Traces & Photos → Autorisations →
Fichiers et contenus multimédias → Autoriser la gestion de tous les fichiers**

Sans cette autorisation, l'appli ne pourra ni lire ni écrire de fichiers
en dehors de son propre dossier privé.

## Prochaines étapes

Envoie-moi, dans l'ordre que tu veux, le code Python de :
`init_onglet2_numerotation`, `init_onglet3_fusion`, `init_onglet4_carte`,
`init_onglet5_statistiques`, `init_onglet6_photos`, `init_onglet7_live`
(et les fonctions associées, ex. `analyser_liste_suppression`,
`get_exif_data`, le serveur HTTP live...). Je les intégrerai un par un
comme nouveaux écrans du menu déroulant.
