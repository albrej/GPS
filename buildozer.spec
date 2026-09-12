[app]
title = Outils Traces & Photos
package.name = outilstraces
package.domain = org.perso

source.dir = .
source.include_exts = py,kv,png,jpg,ttf

version = 0.1

# Dépendances Python nécessaires à l'onglet Conversion.
# lxml a été remplacé par xml.etree.ElementTree (bibliothèque standard) :
# aucune compilation C nécessaire, beaucoup plus fiable sur Android.
# Ajoute pillow, piexif quand tu intègres l'onglet Photos ;
# tkintermapview n'a pas d'équivalent direct sous Kivy (voir README).
requirements = python3,kivy==2.3.0,gpxpy

orientation = portrait
fullscreen = 0

# Permissions : accès large au stockage (comme le faisait le script
# desktop avec DOSSIER_GPSLOGGER en chemin absolu). MANAGE_EXTERNAL_STORAGE
# doit être activé manuellement par l'utilisateur dans les réglages Android
# après l'installation (voir README.md).
android.permissions = READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE,MANAGE_EXTERNAL_STORAGE,INTERNET,ACCESS_FINE_LOCATION,ACCESS_COARSE_LOCATION

# Redmi Note 15 Pro (2026) : Android récent -> viser une API cible actuelle.
android.api = 34
android.minapi = 24
android.ndk_api = 24
android.archs = arm64-v8a

[buildozer]
log_level = 2
warn_on_root = 1
