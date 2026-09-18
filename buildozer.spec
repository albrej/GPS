[app]
title = Bubu GPS
package.name = outilstraces
package.domain = org.perso

source.dir = .
source.include_exts = py,kv,png,jpg,gpx,kml,kmz,xml

version = 0.1

# Dépendances Python nécessaires à l'onglet Conversion.
# lxml a été remplacé par xml.etree.ElementTree (bibliothèque standard) :
# aucune compilation C nécessaire, beaucoup plus fiable sur Android.
# python3==3.11.8 : on fige la version Python EMBARQUÉE DANS L'APK
# (différente du Python du serveur de build). Sans ce pin, buildozer
# prend la dernière version disponible (3.14), trop récente pour le
# code Cython généré par Kivy 2.3.0 -> plantage à la compilation.
# hostpython3 doit être fixé À LA MÊME VERSION EXACTE que python3 :
# python-for-android utilise ce second interpréteur en interne pendant
# la compilation croisée, et refuse de continuer si les deux diffèrent.
# piexif (onglet Photos) : lecture/écriture des tags EXIF GPS et
# Date/Heure. Pure Python, aucune compilation nécessaire.
requirements = python3==3.11.8,hostpython3==3.11.8,kivy==2.3.1,gpxpy,kivy_garden.mapview,piexif

orientation = portrait
icon.filename = %(source.dir)s/Icone.png
fullscreen = 0

# Permissions : accès large au stockage (comme le faisait le script
# desktop avec DOSSIER_GPSLOGGER en chemin absolu). MANAGE_EXTERNAL_STORAGE
# doit être activé manuellement par l'utilisateur dans les réglages Android
# après l'installation (voir README.md).
android.permissions = READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE,MANAGE_EXTERNAL_STORAGE,INTERNET,ACCESS_FINE_LOCATION,ACCESS_COARSE_LOCATION

# Associe l'appli aux fichiers .gpx/.kml/.kmz ("Ouvrir avec" -> Bubu GPS).
# Voir intent_filters.xml et OutilsTracesApp._sur_nouvel_intent dans main.py.
android.manifest_intent_filters = intent_filters.xml

# Redmi Note 15 Pro (2026) : Android récent -> viser une API cible actuelle.
# android.ndk : le NDK auto-téléchargé le plus récent (r28c) embarque un
# Clang trop strict pour le code SDL2/OpenGL de Kivy 2.3.0 (voir README).
# On fige donc une version de NDK plus ancienne, bien plus éprouvée avec
# cette version de Kivy.
android.api = 34
android.minapi = 24
android.ndk = 25b
android.ndk_api = 24
android.archs = arm64-v8a

[buildozer]
log_level = 2
warn_on_root = 1
