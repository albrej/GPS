# -*- coding: utf-8 -*-
"""
============================================================================
 OUTILS TRACES ET PHOTOS — Application Android (Kivy)
 Réécriture de start.py (tkinter) pour fonctionner en APK autonome.

 - Onglets "Conversion" et "Numérotation" : entièrement fonctionnels.
 - Les 4 autres fonctionnalités (Fusion, Carte/Découpe, Photos, Live)
   sont déjà présentes dans le menu déroulant mais affichent un écran
   "à venir" tant que leur code n'est pas fourni et intégré. Voir
   SCREENS_A_VENIR ci-dessous.
============================================================================
"""

import os
import json
import math
import threading
import queue
import subprocess
import urllib.parse
import gpxpy
import xml.etree.ElementTree as ET
import zipfile

from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

# SUR PC UNIQUEMENT : désactive le SIMULATEUR MULTITOUCH de Kivy, qui
# matérialise chaque clic droit (et chaque molette) par un « disque
# rouge » déplaçable dessiné PAR-DESSUS toute l'application (donc
# visible quel que soit l'onglet actif, et persistant d'un onglet à
# l'autre). Le token « disable_multitouch » est la désactivation
# RADICALE côté fournisseur souris : plus aucune simulation, donc
# aucun disque n'est jamais créé. Les clics droits restent de vrais
# événements (button == "right", utilisés par l'onglet Ajout pour la
# suppression de points), la molette continue de défiler/déplacer la
# carte.
#
# ⚠️ GARDE « PC UNIQUEMENT » OBLIGATOIRE : sur Android, cette ligne
# Config.set ENREGISTRAIT un fournisseur d'entrée « souris » dans la
# configuration — fournisseur normalement absent sur téléphone. Sur
# des ROM qui dispatchent déjà les touchers en double (MIUI/HyperOS),
# il générait des touchers synthétiques DOUBLONS à chaque geste : Kivy
# voyait alors deux doigts là où il n'y en avait qu'un (pincement
# fantôme), et le déplacement de la carte (grab + suivi du glissement)
# était bloqué sur TOUS les onglets. D'où le test de plateforme AVANT
# toute inscription dans la configuration.
from kivy.utils import platform as _plateforme
if _plateforme != "android":
    from kivy.config import Config
    Config.set("input", "mouse", "mouse,disable_multitouch")

from kivy.app import App
from kivy.lang import Builder
from kivy.uix.screenmanager import ScreenManager, Screen
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.dropdown import DropDown
from kivy.uix.button import Button
from kivy.uix.popup import Popup
from kivy.uix.filechooser import FileChooserListView
from kivy.uix.scrollview import ScrollView
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.metrics import dp
from kivy.graphics import Color, Line as KivyLine, Rectangle
from kivy.core.text import Label as CoreLabel
from kivy.uix.widget import Widget
from kivy.properties import StringProperty, BooleanProperty, ListProperty, ObjectProperty, NumericProperty
from kivy.utils import platform
from kivy.utils import escape_markup
from kivy.uix.textinput import TextInput
from kivy.properties import BooleanProperty

import gps_logic
import gps_natif

# Extensions considérées comme des photos pour le nom d'un waypoint
# (<name> d'un <wpt> ou d'un Placemark KML) : dans ce cas, le nom affiché
# dans le popup du waypoint est cliquable et ouvre la photo dans la Galerie.
EXTENSIONS_IMAGE = (".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".bmp", ".gif")


def est_nom_image(nom):
    """True si nom (ex. 'IMG_20260922_012604.jpg') a une extension d'image."""
    return bool(nom) and str(nom).strip().lower().endswith(EXTENSIONS_IMAGE)


_ECOUTEURS_SCAN_PHOTO = []  # empêche Python de libérer le listener Android avant le callback


def _chemins_photo_candidats(nom_fichier):
    """Chemins où chercher nom_fichier sur le stockage partagé si la
    médiathèque Android ne le connaît pas encore (photo très récente,
    pas encore indexée). DCIM/Camera est cherché en premier."""
    chemins = []
    try:
        from jnius import autoclass
        Environment = autoclass('android.os.Environment')
        dcim = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DCIM).getAbsolutePath()
        pictures = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_PICTURES).getAbsolutePath()
        chemins.append(os.path.join(dcim, "Camera", nom_fichier))
        chemins.append(os.path.join(dcim, nom_fichier))
        chemins.append(os.path.join(pictures, nom_fichier))
    except Exception:
        pass
    # Repli si Environment n'est pas accessible : chemin standard connu.
    chemins.append(f"/storage/emulated/0/DCIM/Camera/{nom_fichier}")
    return chemins

# Paquets des applications Galerie connues, dans l'ordre de preference.
# Le premier paquet installe sur l'appareil ouvrira la photo DIRECTEMENT
# dans la Galerie, sans le selecteur ("Visualiseur d'images (Natif)" /
# "Afficher les photos"). Sur Xiaomi/Redmi (MIUI/HyperOS) c'est
# com.miui.gallery ; les autres entrees couvrent Samsung, Google et la
# galerie AOSP, pour que le comportement reste correct sur un autre
# appareil. Si aucun ne fonctionne, on retombe sur le ACTION_VIEW
# classique (selecteur Android).
PAQUETS_GALERIE = [
    "com.miui.gallery",            # Xiaomi / Redmi / POCO (MIUI, HyperOS)
    "com.sec.android.gallery3d",   # Samsung Gallery
    "com.google.android.gallery3d",  # Galerie Google (anciens Nexus/Pixel)
    "com.android.gallery3d",       # Galerie AOSP (Androids nus)
    "com.coloros.gallery",         # Oppo
    "com.vivo.gallery",            # Vivo
]


def _ouvrir_uri_image(uri):
    """Lance un Intent ACTION_VIEW sur une URI d'image deja connue
    (content:// issue de MediaStore ou d'un scan).

    La photo est ouverte DIRECTEMENT dans la Galerie de l'appareil
    (sans selecteur d'application). On passe en revue les paquets de
    PAQUETS_GALERIE et on lance l'Intent cible sur chacun :
      - si le paquet est installe et sait afficher l'image -> ouverture
        immediate, c'est fini ;
      - sinon Android leve une exception (ActivityNotFoundException)
        que l'on intercepte pour essayer le paquet suivant.

    On N'utilise PAS resolveActivity() : depuis Android 11 (API 30),
    la "visibilite des paquets" fait que resolveActivity renvoie null
    pour des applis pourtant installees mais non declarees dans le
    manifeste de l'app (balise <queries>) - c'est exactement pourquoi
    le selecteur apparaissait encore sur le Redmi malgre setPackage.
    Si aucun paquet connu ne marche, on retombe sur le ACTION_VIEW
    classique (Android affichera alors son selecteur)."""
    try:
        from jnius import autoclass
        Intent = autoclass('android.content.Intent')
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        activite = PythonActivity.mActivity

        def _lancer(intent):
            intent.setDataAndType(uri, "image/*")
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            activite.startActivity(intent)

        # 1) Ouverture directe dans la Galerie : Intent cible sur chaque
        #    paquet connu ; l'echec (paquet absent) se traduit par une
        #    exception interceptee pour essayer le suivant.
        for paquet in PAQUETS_GALERIE:
            try:
                intent = Intent(Intent.ACTION_VIEW)
                intent.setPackage(paquet)
                _lancer(intent)
                print("[Waypoint] Photo ouverte via la galerie : " + paquet)
                return
            except Exception as e:
                print("[Waypoint] Galerie " + paquet + " indisponible : " + str(e))

        # 2) Repli : aucune galerie connue n'a fonctionne -> ACTION_VIEW
        #    classique (Android affichera le selecteur si besoin).
        print("[Waypoint] Galerie specifique introuvable : ouverture classique.")
        _lancer(Intent(Intent.ACTION_VIEW))
    except Exception as e:
        print(f"[Waypoint] Impossible d'ouvrir la photo : {e}")


def _signaler_erreur(message):
    """Affiche le message d'erreur a l'ecran (Popup) ET dans les logs.
    Indispensable pour diagnostiquer sur l'appareil : sans cela, les
    echecs d'ouverture de photo etaient invisibles (logcat uniquement)."""
    print("[Waypoint] " + str(message))
    try:
        def _afficher(dt):
            contenu = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(10))
            lbl = Label(text=str(message), size_hint_y=None,
                        text_size=(dp(280), None), halign="left", valign="middle")
            lbl.bind(texture_size=lambda w, v: setattr(w, "height", v[1]))
            btn = Button(text="Fermer", size_hint_y=None, height=dp(44))
            contenu.add_widget(lbl)
            contenu.add_widget(btn)
            pop = Popup(title="Ouverture photo", content=contenu,
                        size_hint=(0.85, 0.45))
            btn.bind(on_release=pop.dismiss)
            pop.open()
        Clock.schedule_once(_afficher, 0)
    except Exception:
        pass


def _tableau_chaines(liste):
    """Convertit une liste Python de chaines en tableau Java String[].

    Compatible avec TOUTES les versions de pyjnius :
      - versions recentes : via jnius.JArray si present ;
      - versions anciennes (empaquetees dans les APK Kivy, qui n'ont
        pas JArray - source du bug "cannot import name 'JArray'") :
        pyjnius convertit tout seul une liste Python passee en
        argument de methode Java ; on la passe telle quelle."""
    try:
        from jnius import JArray
        return JArray('java.lang.String')(liste)
    except ImportError:
        return liste
    except Exception:
        return liste


def _uri_content_pour(chemin_complet, nom_fichier):
    """Interroge la mediatheque Android (MediaStore) et renvoie une URI
    content:// pour la photo, ou None si la mediatheque ne la connait pas.

    Obligatoire depuis Android 7 (API 24) : un Intent ACTION_VIEW sur une
    URI file:// (Uri.fromFile) leve FileUriExposedException et la photo
    ne s'ouvre pas. Seule une URI content:// fournie par MediaStore
    fonctionne.

    Deux recherches, dans l'ordre :
      1. par chemin complet (colonne _data) ;
      2. par nom de fichier seul (colonne _display_name) - retrouve la
         photo meme si elle a ete deplacee/renommee."""
    try:
        from jnius import autoclass
        ImagesMedia = autoclass('android.provider.MediaStore$Images$Media')
        ContentUris = autoclass('android.content.ContentUris')
        PythonActivity = autoclass('org.kivy.android.PythonActivity')

        resolver = PythonActivity.mActivity.getContentResolver()
        table = ImagesMedia.EXTERNAL_CONTENT_URI

        def _requete(colonne, valeur):
            curseur = None
            try:
                curseur = resolver.query(
                    table,
                    _tableau_chaines(["_id"]),
                    colonne + "=?",
                    _tableau_chaines([valeur]),
                    None,
                )
                if curseur is not None and curseur.moveToFirst():
                    return ContentUris.withAppendedId(table, curseur.getLong(0))
                return None
            except Exception as e:
                _signaler_erreur("MediaStore (" + colonne + ") : " + str(e))
                return None
            finally:
                if curseur is not None:
                    try:
                        curseur.close()
                    except Exception:
                        pass

        if chemin_complet:
            uri = _requete("_data", chemin_complet)
            if uri is not None:
                return uri
        if nom_fichier:
            uri = _requete("_display_name", nom_fichier)
            if uri is not None:
                return uri
        return None
    except Exception as e:
        _signaler_erreur("MediaStore indisponible : " + str(e))
        return None


def _fabriquer_listener_scan():
    """Cree un listener Java (MediaScannerConnection$OnScanCompletedListener)
    en Python via pyjnius : appele par Android quand le scan du fichier
    est termine, avec l'URI content:// a jour. La reference est conservee
    dans _ECOUTEURS_SCAN_PHOTO (voir declaration en tete de fichier) pour
    empecher Python de liberer l'objet avant le callback."""
    from jnius import PythonJavaClass, java_method

    class ListenerScan(PythonJavaClass):
        __javainterfaces__ = ['android/media/MediaScannerConnection$OnScanCompletedListener']

        @java_method('(Ljava/lang/String;Landroid/net/Uri;)V')
        def onScanCompleted(self, chemin, uri):
            try:
                if uri is not None:
                    print("[Waypoint] Scan termine, URI : " + str(uri))
                    _ouvrir_uri_image(uri)
                else:
                    _signaler_erreur(
                        "Photo toujours absente de la mediatheque apres scan :\n"
                        + str(chemin))
            except Exception as e:
                _signaler_erreur("Erreur apres scan : " + str(e))

    return ListenerScan()


def ouvrir_photo_dans_galerie(chemin_ou_nom):
    """Ouvre la photo dans la Galerie d'Android a partir de son chemin
    complet ou de son nom de fichier.

    Ordre de tentative :
      1. MediaStore (URI content:// - seul type accepte depuis Android 7),
         recherche par chemin complet puis par nom de fichier ;
      2. si la photo n'est pas encore indexee (tres recente) : scan
         MediaScannerConnection, puis ouverture automatique via le
         callback avec l'URI content:// fraichement creee.
    Tout echec est affiche dans un Popup a l'ecran (cf. _signaler_erreur)."""
    if platform != "android" or not chemin_ou_nom:
        return
    try:
        from jnius import autoclass

        nom_fichier = os.path.basename(chemin_ou_nom)

        # Si on a un chemin absolu complet (ex: /storage/emulated/0/DCIM/...)
        if chemin_ou_nom.startswith("/"):
            chemin_cible = chemin_ou_nom
        else:
            # Sinon, on cherche via les candidats habituels (DCIM/Camera...)
            chemin_cible = next(
                (c for c in _chemins_photo_candidats(chemin_ou_nom) if os.path.exists(c)), None
            )

        if not chemin_cible or not os.path.exists(chemin_cible):
            # Le fichier n'est pas trouve au chemin attendu : la
            # mediatheque peut quand meme le connaitre (photo deplacee).
            uri = _uri_content_pour(None, nom_fichier)
            if uri is not None:
                print("[Waypoint] Ouverture via MediaStore (nom seul) : " + str(uri))
                _ouvrir_uri_image(uri)
            else:
                _signaler_erreur(
                    "Fichier image introuvable sur le disque :\n" + str(chemin_ou_nom))
            return

        print("[Waypoint] Ouverture directe du fichier : " + str(chemin_cible))

        # 1) URI content:// via MediaStore (methode valide Android 7+)
        uri = _uri_content_pour(chemin_cible, nom_fichier)
        if uri is not None:
            print("[Waypoint] Ouverture via MediaStore : " + str(uri))
            _ouvrir_uri_image(uri)
            return

        # 2) Photo pas encore indexee : scan, puis ouverture via callback
        MediaScannerConnection = autoclass('android.media.MediaScannerConnection')
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        try:
            listener = _fabriquer_listener_scan()
            _ECOUTEURS_SCAN_PHOTO.clear()
            _ECOUTEURS_SCAN_PHOTO.append(listener)
            MediaScannerConnection.scanFile(
                PythonActivity.mActivity,
                _tableau_chaines([chemin_cible]),
                _tableau_chaines(["image/*"]),
                listener,
            )
            print("[Waypoint] Photo non indexee : scan MediaScanner lance.")
        except Exception as e:
            _signaler_erreur("Scan MediaScanner impossible : " + str(e))

    except Exception as e:
        _signaler_erreur("Impossible d'ouvrir la photo :\n" + str(e))


# ----------------------------------------------------------------------
# Carte interactive (onglet Carte/Découpe) : kivy_garden.mapview est
# l'équivalent Kivy le plus proche de tkintermapview (tuiles OSM/
# satellite, marqueurs). Import protégé : si la bibliothèque n'est pas
# encore installée, le reste de l'appli continue de fonctionner et
# l'écran Carte affiche un message au lieu de planter.
# Installation : pip install kivy_garden.mapview
# ----------------------------------------------------------------------
try:
    from kivy_garden.mapview import MapView, MapMarker, MapSource, MapLayer, MarkerMapLayer
    CARTE_DISPONIBLE = True
except Exception:
    CARTE_DISPONIBLE = False

if CARTE_DISPONIBLE:
    SOURCE_SATELLITE = MapSource(
        url="https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}",
        cache_key="google_satellite",
        min_zoom=0, max_zoom=22,
        attribution="Google",
    )
    SOURCE_PLAN = MapSource(
        url="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        cache_key="osm_plan",
        min_zoom=0, max_zoom=19,
        attribution="(c) OpenStreetMap contributors",
    )
    # Fond topographique OpenTopoMap : courbes de niveau + ombrage.
    # Serveur gratuit pour un usage leger (appli personnelle) ;
    # attribution OpenStreetMap/OpenTopoMap requise.
    SOURCE_TOPO = MapSource(
        url="https://tile.opentopomap.org/{z}/{x}/{y}.png",
        cache_key="opentopomap",
        min_zoom=0, max_zoom=17,
        attribution="(c) OpenStreetMap contributors, SRTM | Style: OpenTopoMap (CC-BY-SA)",
    )
    # Fond topographique Esri World Topo Map. Attention : ordre des
    # coordonnees propre a ESRI ({z}/{y}/{x} et non {z}/{x}/{y}).
    SOURCE_ESRI_TOPO = MapSource(
        url="https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
        cache_key="esri_world_topo",
        min_zoom=0, max_zoom=19,
        attribution="Esri, HERE, Garmin, USGS, NGA",
    )

    # Correspondance valeur du selecteur -> fond de carte, pour tous
    # les onglets (carte, photos, live).
    SOURCES_FONDS_CARTES = {
        "satellite": SOURCE_SATELLITE,
        "plan": SOURCE_PLAN,
        "topo": SOURCE_TOPO,
        "esri_topo": SOURCE_ESRI_TOPO,
    }

    class MapViewMolette(MapView):
        """MapView identique, sauf que la molette/le défilement trackpad
        (PC) DÉPLACE la carte au lieu de zoomer — le zoom ne se plus
        que via les boutons +/- dédiés. Le glisser déplace la carte,
        sans zoom tactile ni pincement."""
    
        PAS_DEPLACEMENT_PX = 60
        freeze_callback = ObjectProperty(None, allownone=True)
        
        # ---> TRANSFORMATION ICI : Utilisation d'une BooleanProperty Kivy
        freeze_actif = BooleanProperty(False)

        # ---> PAN DIFFÉRÉ (utilisé UNIQUEMENT par l'onglet Ajout) :
        # durée (secondes) pendant laquelle l'appui doit être MAINTENU
        # avant que le glisser ne déplace la carte. 0 (défaut) = aucun
        # changement pour tous les autres onglets et le Live : la
        # carte y suit le doigt dès l'appui, exactement comme avant.
        # Pourquoi : sur les autres onglets, la carte vit dans un
        # ScrollView qui CAPTE les glissers courts (défilement de la
        # page) — la carte ne glisse qu'après un appui LONG maintenu,
        # puis glisser. La carte de l'onglet Ajout est la seule HORS
        # ScrollView : sans ce délai, son Scatter interne grabbe le
        # toucher dès l'appui et la carte glisse au moindre clic
        # (court) maintenu + glisser.
        delai_avant_pan = NumericProperty(0)

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            # self.freeze_actif = False # Plus nécessaire ici car géré par la propriété ci-dessus

        # ---> AJOUT DE CETTE MÉTHODE MAGIQUE KIVY
        def on_freeze_actif(self, instance, value):
            """Déclenché automatiquement dès que freeze_actif change."""
            if not value:  # Si value passe à False (dégel)
                # Force le rechargement immédiat et complet des tuiles manquantes
                self.trigger_update(True)

        # CLIC LONG (0.6 s) DE GEL/DEGEL : detection au niveau de la
        # carte ELLE-MEME (pas au niveau Window) : sur PC comme sur
        # Android, un toucher sur la carte est TOUJOURS consomme (par
        # le Scatter de la carte quand elle est active, par le
        # ScrollView ancetre quand elle est gelee si on renvoyait
        # False) - or Kivy ne declenche PAS les callbacks Window.bind
        # quand un widget consomme le toucher. Les handlers Window ne
        # voyaient donc jamais les clics sur la carte. Meme duree que
        # le clic long du graphique (appareil photo).
        DUREE_CLIC_LONG_FREEZE = 0.6
        SEUIL_DEPLACEMENT_FREEZE_DP = 10

        def _bascule_freeze_clic_long(self, touch):
            """Bascule le gel UNE SEULE fois par geste : declenchee par
            le timer de 0.6 s (doigt pose sans bouger) OU au
            relachement d'un appui d'au moins 0.6 s (chemin de repli
            independant du timer)."""
            if touch.ud.get("bascule_freeze_effectuee"):
                return
            if touch.ud.get("appui_long_annule"):
                return
            touch.ud["bascule_freeze_effectuee"] = True
            timer = touch.ud.get("timer_clic_long_freeze")
            if timer is not None:
                timer.cancel()
                touch.ud["timer_clic_long_freeze"] = None
            if self.freeze_callback:
                self.freeze_callback()

        def _annuler_clic_long(self, touch):
            timer = touch.ud.get("timer_clic_long_freeze")
            if timer is not None:
                timer.cancel()
                touch.ud["timer_clic_long_freeze"] = None
            touch.ud["appui_long_annule"] = True

        def on_touch_down(self, touch):
            if not self.collide_point(*touch.pos):
                return super().on_touch_down(touch)

            # Armement du clic long de gel/degel, AVANT tout test de
            # gel : doit fonctionner dans les DEUX sens (geler une
            # carte active ET degeler une carte gelee). IDEMPOTENT :
            # le handler Window (LiveScreen._debut_touch_carte) arme
            # AUSSI un timer pour ce toucher, AVANT le dispatch widget ;
            # si un timer existe deja (cles touch.ud partagees), on ne
            # rearme rien - un second timer ecraserait la reference du
            # premier, qui continuerait de vivre et de tirer.
            touch.ud["clic_long_carte_actif"] = True
            if touch.ud.get("timer_clic_long_freeze") is None:
                touch.ud["carte_pos_depart"] = (touch.x, touch.y)
                touch.ud["temps_depart_freeze"] = Clock.get_time()
                touch.ud["bascule_freeze_effectuee"] = False
                touch.ud["appui_long_annule"] = False
                touch.ud["timer_clic_long_freeze"] = Clock.schedule_once(
                    lambda dt: self._bascule_freeze_clic_long(touch),
                    self.DUREE_CLIC_LONG_FREEZE)

            bouton = getattr(touch, "button", "")
            if bouton in ("scrollup", "scrolldown", "scrollleft", "scrollright"):
                # Molette (PC) = deplacement de la carte, pas un clic
                # long : timer annule.
                self._annuler_clic_long(touch)
                dx = dy = 0
                if bouton == "scrollup":
                    dy = -self.PAS_DEPLACEMENT_PX
                elif bouton == "scrolldown":
                    dy = self.PAS_DEPLACEMENT_PX
                elif bouton == "scrollright":
                    dx = self.PAS_DEPLACEMENT_PX
                elif bouton == "scrollleft":
                    dx = -self.PAS_DEPLACEMENT_PX

                cx, cy = gps_logic.projeter_mercator(self.lat, self.lon, self.zoom)
                nouvelle_lat, nouvelle_lon = gps_logic.deprojeter_mercator(cx + dx, cy + dy, self.zoom)
                self.center_on(nouvelle_lat, nouvelle_lon)
                return True

            if getattr(self, 'freeze_actif', False):
                # Gelee : on CONSOMME le toucher (return True, sans le
                # "grabber"). Si on renvoyait False, le ScrollView
                # ancetre le grabberait pour son defilement, et les
                # evenements move/up deviendraient incoherents pour la
                # carte. Le timer de clic long (degel) reste actif.
                return True

            # PAN DIFFÉRÉ (onglet Ajout uniquement — delai_avant_pan > 0 ;
            # 0 partout ailleurs : ce bloc n'est JAMAIS exécuté sur les
            # autres onglets ni sur le Live). On ne laisse PAS le
            # Scatter interne grabber le toucher dès l'appui : sinon la
            # carte glisserait au moindre clic (court) maintenu +
            # glisser, alors que sur tous les autres onglets il faut
            # un appui LONG maintenu (le ScrollView ancêtre y capte
            # les glissers courts). On ne dispatche donc le toucher
            # qu'aux autres enfants — les marqueurs (points orange
            # déplaçables, disques rouges cliquables) restent
            # immédiatement actifs — et le Scatter ne prendra le
            # relais qu'une fois le délai écoulé, doigt toujours posé
            # (voir on_touch_move).
            if self.delai_avant_pan > 0:
                touch.ud["pan_differe_actif"] = True
                touch.ud["pan_differe_debut"] = Clock.get_time()
                scatter = getattr(self, "_scatter", None)
                # Un enfant (calque de marqueurs) consomme-t-il
                # l'appui ? NB : on NE PEUT PAS tester grab_current
                # ici — Kivy ne remplit grab_current qu'AU MOMENT du
                # dispatch des mouvements grabbés, pas au moment du
                # touch.grab(). Seul le retour du dispatch dit si un
                # marqueur (point orange à déplacer, disque rouge
                # cliquable) a attrapé l'appui.
                consomme = False
                for enfant in self.children:
                    if enfant is scatter:
                        continue
                    if enfant.dispatch("on_touch_down", touch):
                        consomme = True
                        break
                # EXEMPTION DES MARQUEURS : si un point a été attrapé
                # au down, le pan différé est désarmé NET — le délai
                # de ½ s ne s'appliquera PAS à ce geste : le point
                # suit le doigt immédiatement (même après un appui
                # long), la carte ne bouge pas.
                if consomme:
                    touch.ud["pan_differe_actif"] = False
                return True

            return super().on_touch_down(touch)

        def on_touch_move(self, touch):
            # PAN DIFFÉRÉ : le délai est écoulé, le doigt est toujours
            # posé et aucun marqueur ne l'a capturé — le Scatter interne
            # prend le relais MAINTENANT : on lui rejoue un toucher
            # down pour qu'il grabbe et suive le glisser à partir de la
            # position COURANTE du doigt (le super().on_touch_move
            # ci-dessous lui transmettra ce mouvement et les suivants).
            if (touch.ud.get("pan_differe_actif")
                    and self.delai_avant_pan > 0
                    # Double verrou : grab_current n'est rempli par
                    # Kivy qu'au premier dispatch du mouvement grabbé
                    # (le premier move peut donc encore le voir à
                    # None alors qu'un marqueur a le toucher) — d'où
                    # le second test sur point_existant_touche, posé
                    # par les marqueurs au moment du grab, AVANT tout
                    # mouvement.
                    and touch.grab_current is None
                    and not touch.ud.get("point_existant_touche")
                    and (Clock.get_time()
                         - touch.ud.get("pan_differe_debut", 0.0)
                         >= self.delai_avant_pan)):
                touch.ud["pan_differe_actif"] = False
                scatter = getattr(self, "_scatter", None)
                if scatter is not None:
                    scatter.dispatch("on_touch_down", touch)

            # Le doigt se deplace : au-dela du seuil, ce n'est plus un
            # clic long mais un glisser de carte -> timer annule.
            if touch.ud.get("clic_long_carte_actif"):
                depart = touch.ud.get("carte_pos_depart")
                if depart is not None and (
                        abs(touch.x - depart[0]) > dp(self.SEUIL_DEPLACEMENT_FREEZE_DP)
                        or abs(touch.y - depart[1]) > dp(self.SEUIL_DEPLACEMENT_FREEZE_DP)):
                    self._annuler_clic_long(touch)

            # ---> Bloque net le glisser-deplacer (pan) de la carte si le gel est actif
            if getattr(self, 'freeze_actif', False):
                return True

            # Empeche le zoom par pincement en neutralisant l'effet multi-touch de la carte
            if touch.grab_current is not self and len(getattr(self, 'touches', [])) > 1:
                return True
            return super().on_touch_move(touch)

        def on_touch_up(self, touch):
            if touch.ud.pop("clic_long_carte_actif", False):
                # Ce toucher avait demarre sur la carte : on annule le
                # timer s'il pend encore (relachement avant 0.6 s), et
                # s'il a dure au moins 0.6 s sans bouger et sans bascule
                # deja effectuee, on bascule AU RELACHEMENT (repli
                # independant du timer).
                duree = Clock.get_time() - touch.ud.get("temps_depart_freeze", 0.0)
                timer = touch.ud.get("timer_clic_long_freeze")
                if timer is not None:
                    timer.cancel()
                    touch.ud["timer_clic_long_freeze"] = None
                if (not touch.ud.get("bascule_freeze_effectuee")
                        and not touch.ud.get("appui_long_annule")
                        and duree >= self.DUREE_CLIC_LONG_FREEZE):
                    self._bascule_freeze_clic_long(touch)

            if not self.collide_point(*touch.pos):
                return super().on_touch_up(touch)

            if getattr(self, 'freeze_actif', False):
                # Si ce toucher avait ete "grabbe" par la classe de base
                # MapView avant que le gel ne s'active (ex: gel declenche
                # par le clic long pendant que le doigt est encore pose,
                # ou gele pendant un glisser en cours), on la laisse le
                # "degrabber" correctement (elle redescend _touch_count
                # a 0 et repasse _pause a False) - sinon _pause resterait
                # bloque a True pour toujours et load_tile_for_source()
                # (kivy_garden.mapview) ne chargerait plus aucune nouvelle
                # tuile ensuite. On renvoie toujours True nous-memes.
                if touch.grab_current is self:
                    super().on_touch_up(touch)
                return True

            return super().on_touch_up(touch)

        def scale_at(self, *args, **kwargs):
            if getattr(self, 'freeze_actif', False):
                return
            # Désactive l'ajustement d'échelle par pincement tactile
            return

    class TraceLayer(MapLayer):
        """Dessine la trace (polyligne) par-dessus les tuiles, équivalent
        de map_widget.set_path(...) sous tkintermapview. Cyan par défaut
        (comportement inchangé partout où c'était déjà utilisé) ; un
        onglet peut passer une autre couleur pour distinguer plusieurs
        traces sur la même carte (ex. rouge pour la trace live de
        l'onglet Live, à côté d'une trace chargée cyan)."""

        def __init__(self, couleur=(0, 1, 1, 1), **kwargs):
            super().__init__(**kwargs)
            self.points = []
            self.couleur = couleur

        def set_points(self, points_lat_lon):
            self.points = points_lat_lon
            self.reposition()

        def reposition(self):
            self.canvas.clear()
            if not self.points or len(self.points) < 2 or self.parent is None:
                return
            mapview = self.parent
            zoom = mapview.zoom
            # On utilise la fonction officielle de kivy_garden.mapview (celle
            # qui positionne aussi les tuiles et les marqueurs D/A) plutôt
            # qu'une projection Mercator "maison" : elle seule tient compte
            # du facteur d'échelle interne du Scatter de la carte (mapview.
            # scale). Sur PC ce facteur reste toujours à 1.0 pendant un
            # glisser (souris = un seul point de contact), donc l'ancien
            # calcul semblait correct ; sur Android, un léger bruit tactile
            # multi-doigts pendant le glisser peut faire dériver ce facteur,
            # et une trace qui l'ignorait se désynchronisait de la carte.
            coords = []
            for lat, lon in self.points:
                x, y = mapview.get_window_xy_from(lat, lon, zoom)
                coords.extend([x, y])
            with self.canvas:
                Color(*self.couleur)
                KivyLine(points=coords, width=2)

    class MarqueurTexte(MapMarker):
        """Marqueur avec une lettre affichée dessus (D, A, ou D/A),
        équivalent des marqueurs texte de tkintermapview."""

        def __init__(self, texte="", **kwargs):
            super().__init__(**kwargs)
            self._label = Label(text=texte, bold=True, font_size="12sp", color=(1, 1, 1, 1))
            self.add_widget(self._label)
            self.bind(pos=self._maj_label, size=self._maj_label)
            self._maj_label()

        def _maj_label(self, *args):
            self._label.center_x = self.center_x
            self._label.center_y = self.center_y + dp(6)

    # Curseur rond et bleu des waypoints (onglet Photos). L'image est cherchée
    # à côté de main.py : images/blue_dot.png.
    CHEMIN_BLUE_DOT = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "images", "blue_dot.png")

    def taille_marqueur_waypoint(zoom):
        """Côté (en pixels) du curseur des waypoints selon le zoom de la
        carte : petit quand on est loin (16 dp), plus gros quand on zoome
        (jusqu'à 44 dp). Diamètre doublé par rapport à la première version
        (onglets Photos et Live)."""
        return dp(max(16, min(44, 16 + 3.5 * (zoom - 10))))

    # Couleurs des flags départ/arrivée.
    COULEUR_FLAG_DEPART = (0.13, 0.60, 0.22, 1)     # vert
    COULEUR_FLAG_ARRIVEE = (0.80, 0.20, 0.15, 1)    # rouge
    COULEUR_FLAG_FERMETURE = (0.95, 0.55, 0.05, 1)  # orange (boucle fermée)

    class MarqueurFlag(MapMarker):
        """Triangle 100 % dessiné pour marquer le départ et l'arrivée
        d'une trace : triangle plein pointant vers le HAUT, centré sur
        le point GPS (les flags d'origine ont été remplacés par des
        triangles, mêmes conditions et couleurs). Construit comme
        MarqueurDisqueRouge : canvas du MapMarker effacé (plus de carré
        blanc), Triangle dessiné à la place, source neutralisée.
        Taille fixe, indépendante du zoom. couleur : remplissage du
        triangle (vert départ, rouge arrivée, orange boucle fermée)."""

        def __init__(self, couleur=None, **kwargs):
            super().__init__(**kwargs)
            self.canvas.clear()
            from kivy.graphics import Color, Triangle
            self._couleur_flag = couleur if couleur is not None else COULEUR_FLAG_DEPART
            with self.canvas:
                Color(*self._couleur_flag)
                self._triangle = Triangle(points=[0, 0, 0, 0, 0, 0])
            self.bind(pos=self._maj_flag, size=self._maj_flag)
            self.bind(source=self._neutraliser_source)
            # Le CENTRE du triangle tombe sur le point GPS.
            self.anchor_x = 0.5
            self.anchor_y = 0.5
            self.size_hint = (None, None)
            # Triangle équilatéral ~20 dp de côté, hauteur ~18 dp
            # (taille redescendue à la moitié des flags doublés).
            self.size = (dp(20), dp(18))
            self._maj_flag()

        def _neutraliser_source(self, instance, valeur):
            if valeur:
                try:
                    self.source = ""
                except Exception:
                    pass

        def maj_taille(self, zoom):
            """Taille FIXE, indépendante du zoom : méthode présente
            pour que le changement de zoom des cartes (qui appelle
            maj_taille sur tous les marqueurs de waypoints, y compris
            les triangles « Point de passage 1/2 » rangés dans la même
            liste) ne lève pas d'AttributeError. Ne fait rien."""
            pass

        def _maj_flag(self, *args):
            try:
                # Sommet au milieu-haut, base en bas : pointe vers le
                # haut, centré sur le marqueur (donc sur le point GPS).
                x, y = self.pos
                w, h = self.size
                self._triangle.points = [
                    x + w / 2, y + h,          # sommet
                    x + w, y,                  # coin bas-droit
                    x, y,                      # coin bas-gauche
                ]
            except Exception:
                pass

    def _poser_triangles_points_passage(ecran, waypoints):
        """Sur les traces chargées, les annotations « Point de passage 1 »
        et « Point de passage 2 » sont à considérer comme les points de
        DÉPART et d'ARRIVÉE : on leur applique les triangles (mêmes
        conditions et couleurs que les flags D/A) — vert pour le point
        de passage 1 (départ), rouge pour le point de passage 2
        (arrivée), et un SEUL triangle orange posé sur l'arrivée si les
        deux points sont à moins de 20 m l'un de l'autre (boucle
        fermée). Les autres waypoints restent des disques jaunes."""
        wpt_dep = wpt_arr = None
        for wpt in (waypoints or []):
            nom = (wpt.get('name') or '').strip()
            if nom == "Point de passage 1":
                wpt_dep = wpt
            elif nom == "Point de passage 2":
                wpt_arr = wpt
        if wpt_dep is None and wpt_arr is None:
            return

        def _poser(wpt, couleur):
            m = MarqueurFlag(couleur=couleur, lat=wpt['lat'], lon=wpt['lon'])
            ecran.map_view.add_marker(m)
            ecran.marqueurs_waypoints.append(m)

        # Condition « boucle fermée » : un seul triangle orange, sur le
        # DÉPART (point de passage 1).
        if wpt_dep is not None and wpt_arr is not None:
            dist = gps_logic.calculer_distance_haversine(
                wpt_dep['lat'], wpt_dep['lon'], wpt_arr['lat'], wpt_arr['lon']
            )
            if dist <= 20.0:
                _poser(wpt_dep, COULEUR_FLAG_FERMETURE)
                return
        if wpt_dep is not None:
            _poser(wpt_dep, COULEUR_FLAG_DEPART)
        if wpt_arr is not None:
            _poser(wpt_arr, COULEUR_FLAG_ARRIVEE)

    # (L'ancien CHEMIN_BLUE_DOT / images/blue_dot.png n'est plus
    # utilisé : les waypoints sont des disques jaunes dessinés, voir
    # MarqueurWaypoint.)
    def taille_marqueur_waypoint(zoom):
        """Côté (en pixels) du curseur des waypoints selon le zoom de la
        carte : petit quand on est loin (16 dp), plus gros quand on zoome
        (jusqu'à 44 dp). Diamètre doublé par rapport à la première version
        (onglets Photos et Live)."""
        return dp(max(16, min(44, 16 + 3.5 * (zoom - 10))))

    # Couleurs des disques dessinés sur les cartes :
    # - rose du curseur mobile de sélection sur les traces ;
    # - jaune des waypoints/annotations photos.
    COULEUR_ROSE_CURSEUR = (0.95, 0.40, 0.65, 1)
    COULEUR_JAUNE_WAYPOINT = (1.0, 0.84, 0.05, 1)

    class MarqueurDisqueRouge(MapMarker):
        """Marqueur 100 % dessiné : un disque SANS image de fond.
        Le MapMarker standard de kivy_garden.mapview pose à la
        construction, DANS SON PROPRE canvas, une instruction Rectangle
        avec la texture par défaut (carré blanc default_marker.png) :
        dessiner dans canvas.before passait DESSOUS (le carré restait
        visible), et vider « source » n'enlève pas une instruction déjà
        créée — le Rectangle garde sa texture. La seule parade fiable :
        EFFACER le canvas du marqueur juste après la construction, puis
        dessiner le disque à la place. La taille suit le zoom
        comme MarqueurWaypoint (maj_taille), à MOITIE de celle des
        waypoints pour les points aberrants (cote_dp=None), ou fixe
        pour le curseur de sélection (cote_dp donné en dp).
        La couleur est paramétrable : rouge par défaut (points
        aberrants), bleu pour le curseur mobile (couleur=...),
        jaune pour les waypoints (voir MarqueurWaypointJaune)."""

        def __init__(self, zoom=10, cote_dp=None, couleur=None, **kwargs):
            super().__init__(**kwargs)
            # 1. Retire l'instruction Rectangle blanche du MapMarker
            #    (et toute autre instruction posée à la construction).
            self.canvas.clear()
            # 2. Dessine le disque dans le canvas du marqueur.
            from kivy.graphics import Color, Ellipse
            self._couleur = couleur if couleur is not None else (0.80, 0.10, 0.10, 1)
            with self.canvas:
                Color(*self._couleur)
                self._disque = Ellipse(pos=self.pos, size=self.size)
            self.bind(pos=self._maj_disque, size=self._maj_disque)
            # 3. Empêche tout retour de texture : mapview peut
            #    recharger une source par défaut à divers moments du
            #    cycle de vie (ajout à la carte, recyclage...).
            self.bind(source=self._neutraliser_source)
            self._cote = None
            self._cote_dp = cote_dp
            self.anchor_x = 0.5
            self.anchor_y = 0.5
            self.size_hint = (None, None)
            self.maj_taille(zoom)

        def _neutraliser_source(self, instance, valeur):
            if valeur:
                try:
                    self.source = ""
                except Exception:
                    pass

        def _maj_disque(self, *args):
            try:
                self._disque.pos = self.pos
                self._disque.size = self.size
            except Exception:
                pass

        def maj_taille(self, zoom):
            if self._cote_dp is not None:
                self._cote = dp(self._cote_dp)
            else:
                self._cote = max(dp(8), taille_marqueur_waypoint(zoom) / 2.0)
            self._reappliquer_taille()

        def _reappliquer_taille(self, *args):
            if self._cote is None:
                return
            if tuple(self.size) != (self._cote, self._cote):
                cx, cy = self.center
                self.size = (self._cote, self._cote)
                self.center = (cx, cy)

        def _toucher_sur_marqueur(self, touch):
            """Test de collision TOLLÉRANT, utilisé uniquement par les
            sous-classes interactives de l'onglet Ajout (points ajoutés
            orange, disques rouges des points de trace) : les disques
            sont petits (14-18 dp), trop petits pour être tapés au
            doigt avec le collide_point standard. Le test compare la
            position du toucher au CENTRE du marqueur et accepte tout
            tap à moins de demi-taille + 12 dp. Les autres onglets
            (Nettoyage...) posent des MarqueurDisqueRouge sans jamais
            appeler cette méthode : aucun effet pour eux.

            CORRECTIF (2e) : la comparaison se fait dans l'ESPACE DU
            TOUCHER REÇU, SANS AUCUNE CONVERSION. Dans la version pip
            de kivy_garden.mapview, les marqueurs sont des enfants
            d'un MarkerMapLayer (Widget SIMPLE, pas RelativeLayout) :
            le toucher leur parvient dans l'espace des coordonnées du
            marqueur lui-même (marker.pos est posé par set_marker_position
            avec get_window_xy_from, qui rend des coordonnées LOCALES
            à la carte). Comparer touch.x/y à self.center directement
            est donc EXACT. Les conversions essayées avant (to_local
            puis to_window) décalaient toutes deux la zone d'attrape
            du POS des RelativeLayout ANCÊTRES de la carte (l'onglet
            Ajout est le seul à emboîter sa carte dans un RelativeLayout
            sous ~250 dp de boutons) : chaque appui dans la moitié
            basse de la carte « tombait » sur un marqueur fantôme, qui
            capturait le toucher au on_touch_down. Or les marqueurs
            reçoivent le toucher AVANT le Scatter interne de la carte
            (ordre de dispatch inverse des enfants) : un marqueur qui
            renvoie True empêche le Scatter de grabber, et le PAN de
            la carte devient impossible partout ailleurs. Avec la
            comparaison directe, un marqueur ne consomme le toucher
            QUE s'il est réellement touché ; partout ailleurs le
            toucher atteint le Scatter et la carte glisse normalement."""
            dx = touch.x - self.center_x
            dy = touch.y - self.center_y
            rayon = max(self.width, self.height) / 2.0 + dp(12)
            return (dx * dx + dy * dy) <= rayon * rayon

    class MarqueurWaypoint(MapMarker):
        """Curseur des waypoints/annotations photos : un disque JAUNE
        dessiné, 100 % identique au disque rouge des points aberrants
        (MarqueurDisqueRouge) — même construction (canvas du MapMarker
        effacé, Ellipse dessinée, source neutralisée), même suivi du
        zoom via maj_taille(zoom) — seule la couleur change (l'ancien
        images/blue_dot.png n'est plus utilisé). Centré sur le point.
        Un tap dessus ouvre un popup avec son nom (<name>) et sa
        description (<desc>). Si un callback on_waypoint_clic est
        branché (onglet Statistiques), le tap SÉLECTIONNE AUSSI le point
        de trace le plus proche (curseurs des graphiques + bloc
        d'infos), tout en ouvrant le popup comme avant."""

        def __init__(self, zoom=10, nom=None, description=None, on_waypoint_clic=None, **kwargs):
            super().__init__(**kwargs)
            self.nom = nom
            self.description = description
            # Callback optionnel (lat, lon) appelé au tap AVANT le
            # popup : utilisé par l'onglet Statistiques pour sélectionner
            # le point de trace le plus proche du waypoint. None
            # partout ailleurs : comportement inchangé.
            self.on_waypoint_clic = on_waypoint_clic
            self._cote = None
            self.anchor_x = 0.5
            self.anchor_y = 0.5
            self.size_hint = (None, None)
            # Disque jaune dessiné, comme MarqueurDisqueRouge :
            # efface le carré blanc posé par le MapMarker standard.
            self.canvas.clear()
            from kivy.graphics import Color, Ellipse
            with self.canvas:
                Color(*COULEUR_JAUNE_WAYPOINT)
                self._disque = Ellipse(pos=self.pos, size=self.size)
            self.bind(pos=self._maj_disque, size=self._maj_disque)
            # Empêche tout retour de la texture par défaut.
            self.bind(source=self._neutraliser_source)
            # La taille suit le zoom, pas la taille native d'une image.
            self.maj_taille(zoom)

        def _neutraliser_source(self, instance, valeur):
            if valeur:
                try:
                    self.source = ""
                except Exception:
                    pass

        def _maj_disque(self, *args):
            try:
                self._disque.pos = self.pos
                self._disque.size = self.size
            except Exception:
                pass

        def maj_taille(self, zoom):
            # Disque 100 % identique au point rouge (MarqueurDisqueRouge)
            # : diamètre à MOITIÉ de la taille des anciens curseurs
            # # waypoint (la taille entière donnait un disque trop
            # grand par rapport au blue_dot d'origine).
            self._cote = max(dp(8), taille_marqueur_waypoint(zoom) / 2.0)
            self._reappliquer_taille()

        def _reappliquer_taille(self, *args):
            if self._cote is None:
                return
            if tuple(self.size) != (self._cote, self._cote):
                cx, cy = self.center       # on garde le centre sur le point
                self.size = (self._cote, self._cote)
                self.center = (cx, cy)

        def on_touch_down(self, touch):
            if self.collide_point(*touch.pos):
                touch.grab(self)
                return True
            return super().on_touch_down(touch)

        def on_touch_up(self, touch):
            if touch.grab_current is self:
                touch.ungrab(self)
                if self.collide_point(*touch.pos):
                    # Sélection du point de trace le plus proche
                    # (onglet Statistiques uniquement) AVANT le popup.
                    if self.on_waypoint_clic is not None:
                        try:
                            self.on_waypoint_clic(self.lat, self.lon)
                        except Exception:
                            pass
                    self._afficher_popup()
                return True
            return super().on_touch_up(touch)

        def _afficher_popup(self):
            contenu = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(10),
                                 size_hint_y=None)
            contenu.bind(minimum_height=contenu.setter("height"))
            
            if self.description:
                label_desc = Label(
                    text=escape_markup(self.description),
                    markup=True,
                    halign="center",
                    valign="middle",
                    size_hint_y=None,
                )
                label_desc.bind(width=lambda w, val: setattr(w, "text_size", (val, None)))
                label_desc.bind(texture_size=lambda w, val: setattr(w, "height", val[1]))
                contenu.add_widget(label_desc)
            
            # Une annotation peut contenir PLUSIEURS photos : le <name>
            # GPX les liste séparées par des virgules
            # (« photo1.jpg,photo2.jpg,photo3.jpg »). On découpe le nom
            # en photos individuelles et on rend CHACUNE cliquable avec
            # son propre lien [ref=photoN] — un lien unique sur le nom
            # fusionné ne pouvait pas ouvrir la galerie.
            parties_nom = ([p.strip() for p in self.nom.split(",") if p.strip()]
                           if self.nom else [])
            if not parties_nom:
                texte_nom = "Waypoint"
                photos_du_waypoint = []
            else:
                photos_du_waypoint = [p for p in parties_nom if est_nom_image(p)]
                morceaux = []
                for p in parties_nom:
                    if est_nom_image(p) and p in photos_du_waypoint:
                        idx = photos_du_waypoint.index(p)
                        morceaux.append(
                            f"[ref=photo{idx}][u][color=2fa7d4ff]"
                            f"{escape_markup(p)}[/color][/u][/ref]"
                        )
                    else:
                        morceaux.append(escape_markup(p))
                # Une photo par ligne (retour à la ligne), pas de
                # virgule de séparation.
                texte_nom = "\n".join(morceaux)
            # Couleur « bleu Kivy » des liens, comme le libellé
            # « Supprimer les waypoints » : nom(s) cliquable(s).

            # Hauteur adaptative : une ligne par photo (30 dp chacune)
            # pour que la liste verticale ne soit pas tronquée.
            nb_lignes_nom = max(1, len(parties_nom))
            label_nom = Label(
                text=texte_nom,
                markup=True,
                halign="center",
                valign="middle",
                size_hint_y=None,
                height=dp(30 * nb_lignes_nom),
            )
            label_nom.bind(width=lambda w, val: setattr(w, "text_size", (val, None)))
            contenu.add_widget(label_nom)
            
            btn_fermer = Button(text="Fermer", size_hint_y=None, height=dp(44))
            contenu.add_widget(btn_fermer)
            
            exterieur = BoxLayout(orientation="vertical")
            exterieur.add_widget(Widget())
            exterieur.add_widget(contenu)
            exterieur.add_widget(Widget())
            
            # --- LE POPUP EST CRÉÉ ICI EN PREMIER ---
            popup = Popup(title="", separator_height=0, content=exterieur, size_hint=(0.85, 0.4))
            btn_fermer.bind(on_release=popup.dismiss)

            # --- ENSUITE ON BIND LE CLIC DES PHOTOS EN CONNAISSANT LE
            # POPUP : le ref pressé (« photoN ») donne l'index de la
            # photo cliquée dans photos_du_waypoint. ---
            if photos_du_waypoint:
                def _clic_photo(instance, ref):
                    try:
                        idx = int(str(ref).replace("photo", ""))
                        nom_photo = photos_du_waypoint[idx]
                    except (ValueError, IndexError):
                        nom_photo = photos_du_waypoint[0]
                    print(f"DEBUG: Tentative d'ouverture de la photo -> {nom_photo}")
                    popup.dismiss()
                    try:
                        ouvrir_photo_dans_galerie(nom_photo)
                    except Exception as e:
                        print(f"ERREUR lors de l'ouverture de la galerie: {e}")
                label_nom.bind(on_ref_press=_clic_photo)

            popup.open()

    class MarqueurPointAjout(MarqueurDisqueRouge):
        """Disque ORANGE d'un point AJOUTÃ par l'utilisateur (onglet
        Ajout) : mÃªme construction que les disques rouges des points
        aberrants (MarqueurDisqueRouge), mais il se laisse DÃPLACER au
        doigt : un appui dessus le CAPTURE (la carte ne glisse pas
        derriÃ¨re), le glisser le dÃ©place en direct, et au relÃ¢chement :
          - aprÃ¨s un dÃ©placement : on_fin_deplacement(marqueur) â
            l'onglet recalcule l'horodatage interpolÃ© entre les deux
            points de trace les plus proches de la nouvelle position ;
          - sans dÃ©placement (simple tap) : on_clic(marqueur) â popup
            d'information (position + horodatage)."""

        def __init__(self, map_view=None, on_fin_deplacement=None,
                     on_clic=None, on_deplacement=None, on_suppression=None, **kwargs):
            super().__init__(**kwargs)
            self.map_view = map_view
            self.on_fin_deplacement = on_fin_deplacement
            self.on_clic = on_clic
            # Callback (marqueur) appelÃ© au CLIC DROIT (souris, PC) :
            # supprime entiÃ¨rement le point de la trace.
            self.on_suppression = on_suppression
            # Callback (marqueur) appelÃ© Ã  CHAQUE frame du glisser :
            # utilisÃ© par l'onglet Ajout pour dÃ©former la trace EN
            # TEMPS RÃEL pendant le dÃ©placement du point.
            self.on_deplacement = on_deplacement

        def _latlon_depuis_touch(self, touch):
            """Position (lat, lon) de la carte sous le doigt â mÃªme
            projection Mercator que le tap de l'onglet Carte. Pendant
            le glisser du point, la carte est immobile (le toucher a
            Ã©tÃ© capturÃ© par le marqueur), son centre est donc stable."""
            mv = self.map_view
            if mv is None:
                return None, None
            zoom = mv.zoom
            cx, cy = gps_logic.projeter_mercator(mv.lat, mv.lon, zoom)
            px = cx + (touch.x - mv.center_x)
            py = cy - (touch.y - mv.center_y)
            return gps_logic.deprojeter_mercator(px, py, zoom)

        def on_touch_down(self, touch):
            if self._toucher_sur_marqueur(touch):
                # CLIC DROIT (souris, PC) : suppression immÃ©diate du
                # point, sans capture ni dÃ©placement.
                if getattr(touch, "button", "") == "right" and self.on_suppression is not None:
                    # Marque AUSSI le toucher comme Â« sur un point
                    # existant Â» : le handler Window de l'onglet ne
                    # doit pas traiter ce clic droit comme un tap
                    # d'ajout de point sur la trace.
                    touch.ud["point_existant_touche"] = True
                    try:
                        self.on_suppression(self)
                    except Exception:
                        pass
                    return True
                touch.grab(self)
                touch.ud["point_ajout_deplace"] = False
                # Marque le toucher : il ne doit PAS dÃ©clencher l'ajout
                # d'un nouveau point (le tap est sur un point existant).
                touch.ud["point_existant_touche"] = True
                # PAN DIFFÉRÉ (onglet Ajout) : le point est attrapé, la
                # carte ne doit PLUS pouvoir prendre le relais, même si
                # l'appui se prolonge au-delà du délai de pan (verrou
                # posé au grab, où grab_current n'est pas encore rempli
                # par Kivy — voir MapViewMolette.on_touch_down).
                touch.ud["pan_differe_actif"] = False
                return True
            return super().on_touch_down(touch)

        def on_touch_move(self, touch):
            if touch.grab_current is self:
                lat, lon = self._latlon_depuis_touch(touch)
                if lat is not None:
                    self.lat = lat
                    self.lon = lon
                    touch.ud["point_ajout_deplace"] = True
                    # Repositionne le marqueur Ã  l'Ã©cran immÃ©diatement :
                    # le calque de marqueurs de mapview ne repositionne
                    # de lui-mÃªme qu'au dÃ©placement de la carte.
                    try:
                        self.parent.reposition()
                    except Exception:
                        pass
                    # DÃ©formation de la trace EN DIRECT (l'onglet se
                    # charge de la frÃ©quence de rafraÃ®chissement).
                    if self.on_deplacement is not None:
                        try:
                            self.on_deplacement(self)
                        except Exception:
                            pass
                return True
            return super().on_touch_move(touch)

        def on_touch_up(self, touch):
            if touch.grab_current is self:
                touch.ungrab(self)
                deplace = touch.ud.pop("point_ajout_deplace", False)
                if deplace and self.on_fin_deplacement is not None:
                    try:
                        self.on_fin_deplacement(self)
                    except Exception:
                        pass
                elif not deplace and self.on_clic is not None:
                    try:
                        self.on_clic(self)
                    except Exception:
                        pass
                return True
            return super().on_touch_up(touch)

    class MarqueurPointTrace(MarqueurDisqueRouge):
        """Disque rouge d'un POINT DE TRACE de l'onglet Ajout,
        INTERACTIF (contrairement aux points aberrants de l'onglet
        Nettoyage qui restent de simples MarqueurDisqueRouge) :
          - CLIC GAUCHE (tap) : on_clic(marqueur) â l'onglet affiche
            les coordonnÃ©es GPS, l'horodatage EXIF et l'altitude des
            points ENTOURANT ce point dans le bloc central de
            l'onglet (intitulÃ© changÃ©, les deux autres blocs effacÃ©s) ;
          - CLIC DROIT (souris, PC) : on_suppression(marqueur) â
            supprime entiÃ¨rement ce point de la trace.
        Le marqueur garde une rÃ©fÃ©rence au DICT du point de trace
        (self._point) : son index est retrouvÃ© PAR IDENTITÃ au moment
        du clic, donc il reste correct mÃªme aprÃ¨s des insertions ou
        suppressions d'autres points."""

        def __init__(self, point=None, on_clic=None, on_suppression=None, **kwargs):
            super().__init__(**kwargs)
            self._point = point
            self.on_clic = on_clic
            self.on_suppression = on_suppression

        def on_touch_down(self, touch):
            if self._toucher_sur_marqueur(touch):
                # PRIORITÉ AUX POINTS AJOUTÉS (orange, DÉPLAÇABLES) :
                # si le toucher est aussi dans la zone d'attrape d'un
                # marqueur de point ajouté (les disques orange et
                # rouges se côtoient le long de la trace, et leurs
                # zones d'attrape de demi-taille + 12 dp se
                # recouvrent facilement), on NE CONSOMME PAS le
                # toucher : le disque orange doit l'attraper pour se
                # laisser déplacer. Ce code ne concerne que l'onglet
                # Ajout (seul endroit où coexistent MarqueurPointAjout
                # et MarqueurPointTrace).
                parent = self.parent
                if parent is not None:
                    for enfant in parent.children:
                        if (isinstance(enfant, MarqueurPointAjout)
                                and enfant._toucher_sur_marqueur(touch)):
                            return False
                # CLIC DROIT (souris, PC) : suppression immÃ©diate.
                if getattr(touch, "button", "") == "right" and self.on_suppression is not None:
                    # Marque AUSSI le toucher comme Â« sur un point
                    # existant Â» : le handler Window de l'onglet ne
                    # doit pas traiter ce clic droit comme un tap
                    # d'ajout de point sur la trace.
                    touch.ud["point_existant_touche"] = True
                    try:
                        self.on_suppression(self)
                    except Exception:
                        pass
                    return True
                touch.grab(self)
                touch.ud["point_trace_deplace"] = False
                touch.ud["point_trace_depart"] = (touch.x, touch.y)
                # Marque le toucher : il ne doit PAS dÃ©clencher l'ajout
                # d'un nouveau point (le tap est sur un point existant).
                touch.ud["point_existant_touche"] = True
                # PAN DIFFÉRÉ (onglet Ajout) : le point est attrapé, la
                # carte ne doit PLUS pouvoir prendre le relais, même si
                # l'appui se prolonge au-delà du délai de pan.
                touch.ud["pan_differe_actif"] = False
                return True
            return super().on_touch_down(touch)

        def on_touch_move(self, touch):
            if touch.grab_current is self:
                # Un lÃ©ger glissement n'est pas un clic : le disque ne
                # se dÃ©place pas (ce n'est pas un point ajoutÃ©), on
                # note juste que ce n'Ã©tait pas un tap.
                depart = touch.ud.get("point_trace_depart")
                if depart is not None and (
                        abs(touch.x - depart[0]) > dp(8)
                        or abs(touch.y - depart[1]) > dp(8)):
                    touch.ud["point_trace_deplace"] = True
                return True
            return super().on_touch_move(touch)

        def on_touch_up(self, touch):
            if touch.grab_current is self:
                touch.ungrab(self)
                deplace = touch.ud.pop("point_trace_deplace", False)
                if not deplace and self.on_clic is not None:
                    try:
                        self.on_clic(self)
                    except Exception:
                        pass
                return True
            return super().on_touch_up(touch)

class GrapheProfil(Widget):
    """Graphique altitude/vitesse redessiné nativement avec les outils
    de dessin de Kivy (équivalent, sans matplotlib, de afficher_profils()
    dans la version desktop). Un tap dans la zone du graphique appelle
    callback_clic(distance_km_tapee)."""

    def _calculer_distance_depuis_touch(self, touch):
        """Méthode utilitaire pour calculer la distance km depuis la position du toucher."""
        zx, zy, zw, zh = self._zone_graphique()
        decalage_x = dp(42)
        zx_courbe = zx + decalage_x
        zw_courbe = max(1.0, zw - decalage_x)
    
        d_min, d_max = self.distances_km[0], self.distances_km[-1]
        rel_x = touch.x - zx_courbe
        ratio = max(0.0, min(1.0, rel_x / zw_courbe))
        return d_min + ratio * (d_max - d_min)
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.distances_km = []
        self.distances_ele = []
        self.altitudes = []
        self.vitesses_kmh = []
        # --- Série secondaire (optionnelle) : une seconde courbe
        # d'altitude, dessinée en rouge par-dessus celle de set_donnees()
        # (bleue). Utilisée uniquement par l'onglet Live pour superposer
        # la trace live (rouge) à la trace chargée manuellement (bleue).
        # Aucun autre écran n'appelle set_donnees_secondaires() : ces
        # listes restent vides et rien ne change pour eux.
        self.distances_km_secondaire = []
        self.distances_ele_secondaire = []
        self.altitudes_secondaire = []
        self.distance_selection = None
        self.callback_clic = None
        # --- Marqueurs de points aberrants (onglet Nettoyage) : liste de
        # tuples (distance_km, couleur) dessinés comme de petits ronds
        # posés sur la courbe d'altitude — même graphisme que les
        # curseurs de waypoints de la carte, mais 2 fois plus petits.
        self.marqueurs_graphiques = []
        # Bloque toute interaction tactile (sélection de point) quand
        # True — même principe et même nom que sur MapViewMolette,
        # que le gel/dégel s'applique de la même façon partout. False
        # par défaut : aucun effet pour les écrans qui ne le touchent
        # jamais (Carte/Découpe, Photos).
        self.freeze_actif = False
        self.afficher_courbe_vitesse = True  # <--- AJOUT ICI
        # Axe/graduations/légende de vitesse : masquables séparément
        # de la courbe (utilisé par l'onglet Statistiques).
        self.afficher_axe_vitesse = True
        # --- Pentes optionnelles (onglet Statistiques, case à cocher
        # « Pentes ») : bandes de couleur par classe de pente dessinées
        # sous la courbe d'altitude, alimentées par set_pentes() et
        # visibles uniquement si afficher_pentes est True.
        self.afficher_pentes = False
        self.tranches_pentes = []
        self.points_courbe_pentes = []
        self.afficher_curseur = True
        # Couleur de la ligne pointillée de sélection : rouge par
        # défaut ; l'onglet Statistiques la passe en rose
        # (COULEUR_ROSE_CURSEUR) pour ses deux graphiques.
        self.couleur_curseur = (0.85, 0.1, 0.1, 0.9)
        self.bind(pos=self._redessiner, size=self._redessiner)

    def set_donnees(self, distances_km, distances_ele, altitudes, vitesses_kmh):
        self.distances_km = distances_km
        self.distances_ele = distances_ele
        self.altitudes = altitudes
        self.vitesses_kmh = vitesses_kmh
        self.distance_selection = distances_km[0] if distances_km else None
        self._redessiner()

    def set_donnees_secondaires(self, distances_km, distances_ele, altitudes):
        """Ajoute (ou remplace) une SECONDE courbe d'altitude, dessinée
        en rouge par-dessus celle de set_donnees() (toujours bleue) :
        utilisé par l'onglet Live pour superposer la trace live (rouge)
        à la trace chargée manuellement (bleue), sans jamais toucher au
        comportement des autres onglets."""
        self.distances_km_secondaire = distances_km
        self.distances_ele_secondaire = distances_ele
        self.altitudes_secondaire = altitudes
        self._redessiner()

    def effacer_donnees_secondaires(self):
        self.distances_km_secondaire = []
        self.distances_ele_secondaire = []
        self.altitudes_secondaire = []
        self._redessiner()

    def set_pentes(self, tranches, points_courbe):
        """Fournit les tranches de pente (liste [(dist_km_debut,
        pente_pct, alt_debut, alt_fin)] calculée comme pour
        GraphePentes) et les points réels (distance_km, altitude) de
        la courbe : dessinées en bandes colorées SOUS la courbe
        d'altitude, mais uniquement si afficher_pentes est True
        (case à cocher « Pentes » de l'onglet Statistiques)."""
        self.tranches_pentes = list(tranches)
        self.points_courbe_pentes = list(points_courbe)
        self._preparer_bandes_pentes()
        self._redessiner()

    def _preparer_bandes_pentes(self):
        """Précalcule les bandes verticales des pentes UNE SEULE FOIS
        (au set_pentes), en un parcours unique des points GPS triés
        par distance (bisect) — au lieu de rescanner tous les points
        pour chaque borne à chaque redessin (O(N²) : c'est ce qui
        rendait l'onglet peu réactif). Le redessin n'a plus qu'à
        projeter les bandes précalculées (O(nb bandes)).
        Résultat : self._bandes_pentes = liste de
        (couleur, d1, a1, d2, a2), distances en km / altitudes en m,
        bornes incluses dans [début de tranche, fin réelle]."""
        self._bandes_pentes = []
        if not self.tranches_pentes or not self.points_courbe_pentes:
            return
        from bisect import bisect_right
        debut_tranches = [t[0] for t in self.tranches_pentes]
        # Fin réelle de l'axe X : dernière distance du profil
        # (set_donnees précède toujours set_pentes au chargement),
        # sinon dernière distance de la courbe des pentes.
        if self.distances_km:
            dist_fin = max(self.distances_km)
        else:
            dist_fin = max(d for d, _ in self.points_courbe_pentes)
        # Points de la courbe rangés par tranche (parcours unique :
        # les points sont déjà triés par distance croissante).
        par_tranche = [[] for _ in self.tranches_pentes]
        for d_pc, a_pc in self.points_courbe_pentes:
            i = bisect_right(debut_tranches, d_pc) - 1
            if i >= 0 and d_pc < debut_tranches[i] + 0.5:
                par_tranche[i].append((d_pc, a_pc))
        # Bandes de chaque tranche : de (début, alt_dep) jusqu'à
        # (fin réelle, alt_arr), en suivant les points intermédiaires.
        # DÉCIMATION : au-delà de 12 points dans une tranche, on
        # échantillonne régulièrement — le rendu reste identique à
        # l'œil (500 m de large), mais le nombre d'instructions
        # graphiques (2 triangles + 1 Color PAR point) n'explose
        # plus : c'est lui qui rendait le glissement du curseur
        # irréactif sur les traces denses (toutes les instructions
        # sont reconstruites à chaque frame du glissement).
        MAX_PTS_PAR_TRANCHE = 12
        for i, (dist_km, pente, alt_dep, alt_arr) in enumerate(self.tranches_pentes):
            couleur = GraphePentes._couleur_pente(pente)
            d_fin = min(dist_km + 0.5, dist_fin)
            pts = par_tranche[i]
            if len(pts) > MAX_PTS_PAR_TRANCHE:
                pas = (len(pts) - 1) / (MAX_PTS_PAR_TRANCHE - 1)
                pts = [pts[int(round(j * pas))] for j in range(MAX_PTS_PAR_TRANCHE)]
            d_prec, a_prec = dist_km, alt_dep
            for d_pc, a_pc in pts:
                if d_pc > d_fin:
                    break
                self._bandes_pentes.append((couleur, d_prec, a_prec, d_pc, a_pc))
                d_prec, a_prec = d_pc, a_pc
            if d_prec < d_fin:
                self._bandes_pentes.append((couleur, d_prec, a_prec, d_fin, alt_arr))

    def set_selection(self, distance_km):
        self.distance_selection = distance_km
        self._redessiner()

    def set_marqueurs(self, marqueurs):
        """Remplace la liste des marqueurs de points aberrants (onglet
        Nettoyage) : marqueurs = liste de tuples (distance_km, couleur).
        Vide / None : aucun marqueur (comportement des autres onglets)."""
        self.marqueurs_graphiques = list(marqueurs or [])
        self._redessiner()

    def _altitude_a_la_distance(self, distance_km):
        """Altitude de la courbe principale à une distance donnée, par
        interpolation linéaire entre les points dotés d'une altitude.
        Renvoie None si la courbe n'a pas d'altitudes."""
        if not self.distances_ele or not self.altitudes:
            return None
        if distance_km <= self.distances_ele[0]:
            return self.altitudes[0]
        if distance_km >= self.distances_ele[-1]:
            return self.altitudes[-1]
        for i in range(1, len(self.distances_ele)):
            d1, d2 = self.distances_ele[i - 1], self.distances_ele[i]
            if d1 <= distance_km <= d2:
                if d2 > d1:
                    ratio = (distance_km - d1) / (d2 - d1)
                    return self.altitudes[i - 1] + ratio * (self.altitudes[i] - self.altitudes[i - 1])
                return self.altitudes[i]
        return self.altitudes[-1]

    def _zone_graphique(self):
        # Marge interne pour ne pas coller aux bords du widget
        marge_g, marge_d, marge_h, marge_b = dp(48), dp(48), dp(22), dp(38)
        zx = self.x + marge_g
        zy = self.y + marge_b
        zw = max(1.0, self.width - marge_g - marge_d)
        zh = max(1.0, self.height - marge_h - marge_b)
        return zx, zy, zw, zh

    def _texte_texture(self, texte, taille_sp=10, gras=True):
        core_lbl = CoreLabel(text=texte, font_size=dp(taille_sp), bold=gras)
        core_lbl.refresh()
        return core_lbl.texture

    def _poser_texte(self, texte, x, y, couleur, taille_sp=10, centre_h=False, centre_v=False,
                      gras=True, aligne_droite=False):
        tex = self._texte_texture(texte, taille_sp=taille_sp, gras=gras)
        if aligne_droite:
            px = x - tex.width
        elif centre_h:
            px = x - tex.width / 2
        else:
            px = x
        py = y - tex.height / 2 if centre_v else y
        Color(*couleur)
        Rectangle(texture=tex, pos=(px, py), size=tex.size)

    @staticmethod
    def _graduations(v_min, v_max, nb=4):
        if nb <= 1 or v_max <= v_min:
            return [v_min]
        pas = (v_max - v_min) / (nb - 1)
        return [v_min + i * pas for i in range(nb)]

    def _redessiner(self, *args):
        self.canvas.clear()
        toutes_distances_km = list(self.distances_km) + list(self.distances_km_secondaire)
        if not toutes_distances_km or self.width < dp(30) or self.height < dp(30):
            return

        ROUGE = (0.8, 0.1, 0.1, 1)
        BLEU = (0.12, 0.53, 0.90, 1)
        VERT = (0.18, 0.49, 0.20, 1)
        GRIS_TEXTE = (0.25, 0.25, 0.25, 1)

        zx, zy, zw, zh = self._zone_graphique()
        d_min, d_max = min(toutes_distances_km), max(toutes_distances_km)
        d_span = max(d_max - d_min, 1e-6)

        # Décalage horizontal (en pixels) pour laisser place aux labels min/max rouges à gauche
        decalage_x = dp(42)
        zx_courbe = zx + decalage_x
        zw_courbe = max(1.0, zw - decalage_x)

        # Fonction de conversion de coordonnées (distance -> abscisse écran)
        def x_ecran(d):
            return zx_courbe + (d - d_min) / d_span * zw_courbe

        # --- Calculs des échelles ---
        a_ele = len(self.altitudes) >= 2
        a_ele_sec = len(self.altitudes_secondaire) >= 2
        a_vit = a_ele and any(v > 0 for v in self.vitesses_kmh)

        if a_ele or a_ele_sec:
            toutes_altitudes = list(self.altitudes) + list(self.altitudes_secondaire)
            a_min, a_max = min(toutes_altitudes), max(toutes_altitudes)
            # Ajout du padding d'altitude pour éviter le chevauchement
            marge_alt = max((a_max - a_min) * 0.12, 10.0)
            a_bas, a_haut = a_min - marge_alt, a_max + marge_alt
            a_span = max(a_haut - a_bas, 1e-6)

            def y_alt(a):
                return zy + (a - a_bas) / a_span * zh

        if a_vit:
            v_min, v_max = min(self.vitesses_kmh), max(self.vitesses_kmh)
            marge_vit = max((v_max - v_min) * 0.1, 5.0)
            v_bas, v_haut = max(0.0, v_min - marge_vit), v_max + marge_vit
            v_span = max(v_haut - v_bas, 1e-6)

            def y_vit(v):
                return zy + (v - v_bas) / v_span * zh

        with self.canvas:
            Color(1, 1, 1, 1)
            Rectangle(pos=(zx, zy), size=(zw, zh))

            if a_ele or a_ele_sec:
                # Quadrillage d'altitude et valeurs sur l'axe Y
                for valeur in self._graduations(a_bas, a_haut, 5):
                    gy = y_alt(valeur)
                    Color(0.88, 0.88, 0.88, 1)
                    KivyLine(points=[zx, gy, zx + zw, gy], width=1)
                    self._poser_texte(f"{int(round(valeur))}", zx - dp(4), gy, BLEU,
                                       taille_sp=9, centre_v=True, gras=False, aligne_droite=True)

                # Altitudes min et max (en rouge) placées dans l'espace décalé à gauche de la courbe
                for valeur in (a_min, a_max):
                    self._poser_texte(f"{int(round(valeur))}", zx + dp(4), y_alt(valeur), ROUGE,
                                       taille_sp=9, centre_v=True, gras=True)

            Color(0.55, 0.55, 0.55, 1)
            KivyLine(points=[zx, zy, zx + zw, zy, zx + zw, zy + zh, zx, zy + zh], width=1.2)

            # Graduations de l'axe X des distances
            for valeur in self._graduations(d_min, d_max, 5):
                gx = x_ecran(valeur)
                Color(0.88, 0.88, 0.88, 1)
                KivyLine(points=[gx, zy, gx, zy + zh], width=1)
                self._poser_texte(f"{valeur:.1f}", gx, zy - dp(16), GRIS_TEXTE,
                                   taille_sp=9, centre_h=True, gras=False)

            # --- Bandes de pentes optionnelles (case « Pentes » de
            # l'onglet Statistiques) : mêmes classes de couleurs que
            # le graphique des pentes (GraphePentes). Les bandes sont
            # PRÉCALCULÉES par _preparer_bandes_pentes (set_pentes) :
            # le redessin ne fait que les projeter à l'écran — rapide
            # même sur les longues traces (le calcul O(N²) par
            # redessin rendait l'onglet peu réactif).
            if self.afficher_pentes and getattr(self, "_bandes_pentes", None) and a_ele:
                from kivy.graphics import Triangle
                for couleur, d1, a1, d2, a2 in self._bandes_pentes:
                    Color(*couleur)
                    Triangle(points=[x_ecran(d1), zy, x_ecran(d1), y_alt(a1),
                                     x_ecran(d2), y_alt(a2)])
                    Triangle(points=[x_ecran(d1), zy, x_ecran(d2), y_alt(a2),
                                     x_ecran(d2), zy])
            if a_ele:
                # Tracé de la courbe d'altitude (trace chargée, bleu)
                points_ligne = []
                for d, a in zip(self.distances_ele, self.altitudes):
                    points_ligne.extend([x_ecran(d), y_alt(a)])
                Color(*BLEU)
                KivyLine(points=points_ligne, width=1.6)

            if a_ele_sec:
                # Tracé de la seconde courbe d'altitude (trace live,
                # rouge), superposée à celle ci-dessus (onglet Live
                # uniquement — voir set_donnees_secondaires()).
                points_ligne_sec = []
                for d, a in zip(self.distances_ele_secondaire, self.altitudes_secondaire):
                    points_ligne_sec.extend([x_ecran(d), y_alt(a)])
                Color(*ROUGE)
                KivyLine(points=points_ligne_sec, width=1.8)

            # --- Marqueurs de points aberrants (onglet Nettoyage) :
            # petits ronds posés sur la courbe d'altitude aux distances
            # données par set_marqueurs(). Même graphisme que les
            # curseurs de waypoints (bleu) de la carte, 2 fois plus
            # petits : côté dp(8) contre dp(16) minimum sur la carte.
            if self.marqueurs_graphiques:
                from kivy.graphics import Ellipse
                cote_m = dp(8)
                for d_m, couleur_m in self.marqueurs_graphiques:
                    x_m = x_ecran(d_m)
                    if a_ele or a_ele_sec:
                        a_m = self._altitude_a_la_distance(d_m)
                        y_m = y_alt(a_m) if a_m is not None else zy + zh / 2.0
                    else:
                        y_m = zy + zh / 2.0
                    Color(*couleur_m)
                    Ellipse(pos=(x_m - cote_m / 2.0, y_m - cote_m / 2.0),
                            size=(cote_m, cote_m))

            if a_ele or a_ele_sec:
                if a_vit and self.afficher_axe_vitesse:
                    for valeur in self._graduations(v_bas, v_haut, 4):
                        gy = y_vit(valeur)
                        self._poser_texte(f"{int(round(valeur))}", zx + zw + dp(4), gy, VERT,
                                           taille_sp=9, centre_v=True, gras=False)

                    # Tracé de la courbe de vitesse (masqué si self.afficher_courbe_vitesse
                    # est False, cf. LiveScreen (onglet 7) : seul l'axe/les graduations de
                    # vitesse ci-dessus restent visibles dans ce cas).
                    if a_vit and self.afficher_courbe_vitesse:
                        points_vit = []
                        for d, v in zip(self.distances_km, self.vitesses_kmh):
                            points_vit.extend([x_ecran(d), y_vit(v)])
                        Color(*VERT)
                        KivyLine(points=points_vit, width=1.6)

            if self.afficher_curseur and self.distance_selection is not None:
                cx = x_ecran(self.distance_selection)
                Color(*self.couleur_curseur)
                longueur_trait = dp(5)
                longueur_espace = dp(4)
                y = zy
                while y < zy + zh:
                    y_fin = min(y + longueur_trait, zy + zh)
                    KivyLine(points=[cx, y, cx, y_fin], width=1.4)
                    y += longueur_trait + longueur_espace

            self._poser_texte("Distance (km)", zx + zw / 2, self.y, GRIS_TEXTE,
                               taille_sp=10, centre_h=True)
            if a_ele or a_ele_sec:
                self._poser_texte("Altitude (m)", zx, zy + zh + dp(4), BLEU, taille_sp=9)
            if a_vit and self.afficher_axe_vitesse:
                tex_v = self._texte_texture("Vitesse (km/h)", taille_sp=9)
                self._poser_texte("Vitesse (km/h)", zx + zw - tex_v.width, zy + zh + dp(4), VERT, taille_sp=9)

    def on_touch_down(self, touch):
        # Gel/dégel (propagé par LiveScreen.basculer_freeze(), même
        # principe que sur MapViewMolette) : bloque toute interaction
        # tactile sur le graphique quand actif.
        if getattr(self, 'freeze_actif', False):
            return True
        if not self.collide_point(*touch.pos):
            return super().on_touch_down(touch)
        if not self.distances_km:
            return super().on_touch_down(touch)

        # Capture le toucher pour suivre le glissement
        touch.grab(self)

        if not self.distances_km:
            return True

        distance_km_tapee = self._calculer_distance_depuis_touch(touch)
        self.set_selection(distance_km_tapee)  # Met à jour le curseur visuel
        if self.callback_clic:
            self.callback_clic(distance_km_tapee)  # Met à jour la carte dès l'appui
        return True

    def on_touch_move(self, touch):
        if getattr(self, 'freeze_actif', False):
            return True
        if touch.grab_current is self:
            if not self.distances_km:
                return True
            distance_km_tapee = self._calculer_distance_depuis_touch(touch)
            self.set_selection(distance_km_tapee)  # Suit le mouvement du curseur
            if self.callback_clic:
                self.callback_clic(distance_km_tapee)  # Met à jour la carte en temps réel pendant le glissement
            return True
        return super().on_touch_move(touch)

    def on_touch_up(self, touch):
        if touch.grab_current is self:
            touch.ungrab(self)
            if getattr(self, 'freeze_actif', False) or not self.distances_km:
                return True

            distance_km_tapee = self._calculer_distance_depuis_touch(touch)
            self.set_selection(distance_km_tapee)
            if self.callback_clic:
                self.callback_clic(distance_km_tapee)  # Assure la position finale au lâcher
            return True

class GraphePentes(Widget):
    """Graphique des pentes (tranches de 500 m) : la trace est
    découpée en tranches de 500 m ; chaque tranche est dessinée comme
    un rectangle vertical (barre) partant du ZÉRO de l'axe des
    abscisses (base du graphique) et montant jusqu'à la courbe
    d'altitude. La couleur suit des CLASSES de pente fixes : descentes
    en TONS BLEUS de plus en plus sombres (bleu céleste clair #9EE0FF
    de -10 à 0 % jusqu'au bleu nuit profond #00264D au-delà de
    -30 %), PLAT beige/blanc cassé (#EEEDE9) autour de 0, montées du
    ROSE CORAIL CLAIR (#FF9E9E, 0 à +10 %) au ROUGE VIF (#D90429),
    au ROUGE FONCÉ (#990000) puis au ROUGE PROFOND (#660000) au-delà
    de +30 %. La valeur de la pente (ex. « +12.4 ») est inscrite
    au-dessus de la courbe, et le titre affiche le nombre de tranches
    en montée et en descente. Reproduit le style des profils « pentes
    sur 500 m » des applis de rando."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tranches = []  # [(dist_km_debut, pente_pct, alt_debut, alt_fin)]
        # Altitudes min/max réelles de la trace (sur tous les points) :
        # servent d'échelle au graphique pour rester cohérent avec le
        # tableau de statistiques (le sommet réel peut se trouver ENTRE
        # deux bornes de tranches interpolées).
        self.alt_min_pts = float("inf")
        self.alt_max_pts = float("-inf")
        # Sélection interconnectée (onglet Statistiques) : distance (km)
        # du point sélectionné — dessinée comme une ligne verticale
        # pointillée rouge, comme le curseur de GrapheProfil.
        self.distance_selection = None
        # Distance cumulée TOTALE de la trace (km) : borne exacte de
        # l'axe X. Sans elle, l'axe s'arrêterait à « début de la
        # dernière tranche + 0,5 km », ce qui allonge artificiellement
        # l'axe (trace de 10,3 km → axe jusqu'à 10,5 km) et décale le
        # curseur de sélection avec un retard croissant en fin de trace.
        self.dist_fin_km = None
        # Points RÉELS de la courbe d'altitude : liste (distance_km,
        # altitude) de tous les points GPS dotés d'une altitude. La
        # courbe est tracée à partir d'eux (et non des seules bornes
        # de tranches interpolées tous les 500 m) pour être
        # EXACTEMENT identique à celle du profil d'altitude — sans
        # cela, elle paraît « lissée » par l'échantillonnage.
        self.points_courbe = []
        # Callback appelé au tap dans la zone du graphique, avec la
        # distance (km) tapée — même contrat que GrapheProfil.
        self.callback_clic = None
        # Couleur de la ligne pointillée de sélection : rouge par
        # défaut ; l'onglet Statistiques la passe en rose.
        self.couleur_curseur = (0.85, 0.1, 0.1, 0.9)
        self.bind(size=self._redessiner, pos=self._redessiner)

    def set_selection(self, distance_km):
        """Déplace (ou retire si None) le curseur vertical de
        sélection, puis redessine."""
        self.distance_selection = distance_km
        self._redessiner()

    def _distance_depuis_touch(self, touch):
        """Distance (km) correspondant à la position tapée, en
        replaçant les marges exactes de _redessiner (renvoie None si
        le tap est hors zone utile)."""
        marge_g, marge_d, marge_h, marge_b = dp(48), dp(48), dp(22), dp(38)
        zx, zy = self.x + marge_g, self.y + marge_b
        zw, zh = max(1.0, self.width - marge_g - marge_d), max(1.0, self.height - marge_h - marge_b)
        gx, gw = zx + dp(42), max(1.0, zw - dp(42))
        if not (gx <= touch.x <= gx + gw and zy <= touch.y <= zy + zh):
            return None
        if not self.tranches:
            return None
        # Même borne d'axe que _redessiner : vraie longueur de trace
        # si fournie, sinon dernière tranche + 0,5 km.
        dist_fin = self.dist_fin_km if self.dist_fin_km else self.tranches[-1][0] + 0.5
        ratio = max(0.0, min(1.0, (touch.x - gx) / gw))
        return ratio * dist_fin

    def on_touch_down(self, touch):
        """Appui dans le graphique des pentes : sélectionne la
        distance tapée et CAPTURE le toucher pour suivre le
        glissement (même mécanisme que GrapheProfil : le curseur
        suit le doigt en temps réel). Ne consomme l'événement que si
        le tap est dans la zone utile."""
        # Même mécanisme que GrapheProfil : pas de garde sur
        # grab_current (le ScrollView du parent capture déjà le
        # toucher à l'appui — il faut RE-CAPTURER le toucher pour
        # que le simple clic sélectionne, sinon seul le
        # glissement fonctionne).
        if self.callback_clic is None:
            return super().on_touch_down(touch)
        if not self.collide_point(*touch.pos):
            return super().on_touch_down(touch)
        distance = self._distance_depuis_touch(touch)
        if distance is None:
            return super().on_touch_down(touch)
        touch.grab(self)
        self.set_selection(distance)
        self.callback_clic(distance)
        return True

    def on_touch_move(self, touch):
        """Glissement : le curseur suit le doigt et la sélection est
        mise à jour en temps réel (carte, graphique d'altitude, bloc
        d'infos) — même comportement que GrapheProfil."""
        if touch.grab_current is self:
            distance = self._distance_depuis_touch(touch)
            if distance is not None:
                self.set_selection(distance)
                self.callback_clic(distance)
            return True
        return super().on_touch_move(touch)

    def on_touch_up(self, touch):
        """Fin du toucher : relâche la capture."""
        if touch.grab_current is self:
            touch.ungrab(self)
            return True
        return super().on_touch_up(touch)

    # Couleurs/textes locaux (BLEU/GRIS_TEXTE de GrapheProfil sont des
    # variables LOCALES à son _redessiner : on redéfinit ici).
    BLEU = (0.12, 0.53, 0.90, 1)
    GRIS_TEXTE = (0.25, 0.25, 0.25, 1)

    @staticmethod
    def _texte_texture(texte, taille_sp=10, gras=True):
        core_lbl = CoreLabel(text=texte, font_size=dp(taille_sp), bold=gras)
        core_lbl.refresh()
        return core_lbl.texture

    def _poser_texte(self, texte, x, y, couleur, taille_sp=10, centre_h=False, centre_v=False,
                     gras=True, aligne_droite=False):
        tex = self._texte_texture(texte, taille_sp=taille_sp, gras=gras)
        if aligne_droite:
            px = x - tex.width
        elif centre_h:
            px = x - tex.width / 2
        else:
            px = x
        py = y - tex.height / 2 if centre_v else y
        from kivy.graphics import Color, Rectangle as KivyRect
        with self.canvas:
            Color(*couleur)
            KivyRect(texture=tex, pos=(px, py), size=tex.size)

    def set_tranches(self, tranches, alt_min_pts=None, alt_max_pts=None, dist_fin_km=None,
                     points_courbe=None):
        """Alimente le graphique : liste de tranches de 500 m, chacune
        (distance_debut_km, pente_pct, altitude_debut, altitude_fin).
        alt_min_pts / alt_max_pts : altitudes min/max RÉELLES de la
        trace (calculées sur tous les points, comme le tableau), car
        le sommet peut se trouver entre deux bornes de tranches
        interpolées. dist_fin_km : distance cumulée TOTALE de la trace
        (borne exacte de l'axe X, pour que le curseur de sélection
        soit aligné avec le graphique d'altitude, dont l'axe
        s'arrête à la vraie fin de trace). points_courbe : liste
        (distance_km, altitude) de TOUS les points GPS avec altitude
        — la courbe d'altitude est tracée à partir d'eux pour être
        identique à celle du profil d'altitude (sinon, échantillonnée
        toutes les bornes de 500 m, elle paraît lissée). Déclenche le
        redessin."""
        self.tranches = tranches or []
        if alt_min_pts is not None:
            self.alt_min_pts = alt_min_pts
        if alt_max_pts is not None:
            self.alt_max_pts = alt_max_pts
        if dist_fin_km is not None:
            self.dist_fin_km = dist_fin_km
        if points_courbe is not None:
            self.points_courbe = list(points_courbe)
        self._redessiner()

    # Palette de classes de pente (couleurs fixes par palier) :
    DESC_TRES_FORTE = (0.0, 0.149, 0.302, 1)     # < -30 %   : bleu nuit profond #00264D
    DESC_FORTE = (0.0, 0.298, 0.6, 1)          # -30 à -20 : bleu foncé #004C99
    DESC_MARQUEE = (0.0, 0.502, 1.0, 1)         # -20 à -10 : bleu vif #0080FF
    DESC_MODEREE = (0.620, 0.878, 1.0, 1)      # -10 à 0   : bleu céleste clair #9EE0FF
    PLAT = (0.933, 0.929, 0.914, 1)             # autour de 0 : beige/blanc cassé #EEEDE9
    MONT_LEGERE = (1.0, 0.620, 0.620, 1)      # 0 à +10   : rose corail clair #FF9E9E
    MONT_MODEREE = (0.851, 0.016, 0.161, 1)   # +10 à +20 : rouge vif #D90429
    MONT_RAIDE = (0.6, 0.0, 0.0, 1)           # +20 à +30 : rouge foncé #990000
    MONT_MUR = (0.4, 0.0, 0.0, 1)             # > +30     : rouge profond #660000

    # Classmethod : appelée aussi par GrapheProfil (bandes de pentes
    # optionnelles du graphique d'altitude, case « Pentes » de
    # l'onglet Statistiques).
    @classmethod
    def _couleur_pente(cls, pente):
        """Couleur d'une tranche selon sa classe de pente :
        DESCENTES en BLEUS de plus en plus sombres (bleu céleste
        clair #9EE0FF de -10 à 0 %, jusqu'au bleu nuit profond
        #00264D au-delà de -30 %), PLAT beige (#EEEDE9)
        uniquement autour de 0, MONTÉES du rose corail clair
        (#FF9E9E, 0 à +10 %) au rouge profond (#660000)
        au-delà de +30 %. Les bornes sont EXACTEMENT celles de la
        légende : descente modérée de -10 à 0 %, montée légère de
        0 à +10 %, le beige ne s'appliquant qu'à une pente
        strictement quasi nulle (±0,1 %)."""
        if pente < -30.0:
            return cls.DESC_TRES_FORTE
        if pente < -20.0:
            return cls.DESC_FORTE
        if pente < -10.0:
            return cls.DESC_MARQUEE
        if pente < -0.1:
            return cls.DESC_MODEREE
        if pente <= 0.1:
            return cls.PLAT
        if pente < 10.0:
            return cls.MONT_LEGERE
        if pente < 20.0:
            return cls.MONT_MODEREE
        if pente <= 30.0:
            return cls.MONT_RAIDE
        return cls.MONT_MUR

    @staticmethod
    def _graduations(v_min, v_max, nb=4):
        if nb <= 1 or v_max <= v_min:
            return [v_min]
        pas = (v_max - v_min) / (nb - 1)
        return [v_min + i * pas for i in range(nb)]

    def _redessiner(self, *args):
        self.canvas.clear()
        if not self.tranches or self.width < dp(30) or self.height < dp(30):
            return
        from kivy.graphics import Color, Line, Rectangle as KivyRectangle, Triangle

        # Mêmes couleurs que GrapheProfil (onglet 4) pour un visuel
        # identique : axes, courbe d'altitude et altitudes min/max.
        ROUGE = (0.8, 0.1, 0.1, 1)

        # Marges identiques à GrapheProfil._zone_graphique().
        marge_g, marge_d, marge_h, marge_b = dp(48), dp(48), dp(22), dp(38)
        zx, zy = self.x + marge_g, self.y + marge_b
        zw, zh = max(1.0, self.width - marge_g - marge_d), max(1.0, self.height - marge_h - marge_b)

        # Décalage horizontal (en pixels) pour laisser place aux labels
        # min/max rouges à gauche de la courbe — comme GrapheProfil.
        decalage_x = dp(42)
        gx, gy = zx + decalage_x, zy
        gw, gh = max(1.0, zw - decalage_x), zh

        dists = [t[0] for t in self.tranches]
        # Borne de l'axe X : distance TOTALE réelle de la trace si
        # fournie, sinon repli sur « dernière tranche + 0,5 km ».
        dist_fin = self.dist_fin_km if self.dist_fin_km else dists[-1] + 0.5
        alt_min = min(self.alt_min_pts, min(t[2] for t in self.tranches))
        alt_max = max(self.alt_max_pts, max(t[3] for t in self.tranches))
        marge_alt = max((alt_max - alt_min) * 0.12, 10.0)
        a_bas, a_haut = alt_min - marge_alt, alt_max + marge_alt
        a_span = max(a_haut - a_bas, 1e-6)

        def x_km(d):
            return gx + d / dist_fin * gw

        def y_alt(a):
            return gy + (a - a_bas) / a_span * gh

        with self.canvas:
            Color(1, 1, 1, 1)
            KivyRectangle(pos=(zx, zy), size=(zw, zh))

            # Quadrillage d'altitude et valeurs sur l'axe Y (bleu),
            # comme GrapheProfil.
            for valeur in self._graduations(a_bas, a_haut, 5):
                gyv = y_alt(valeur)
                Color(0.88, 0.88, 0.88, 1)
                Line(points=[zx, gyv, zx + zw, gyv], width=1)
                self._poser_texte(f"{int(round(valeur))}", zx - dp(4), gyv, self.BLEU,
                                  taille_sp=9, centre_v=True, gras=False, aligne_droite=True)

            # Altitudes min et max (en ROUGE) placées dans l'espace
            # décalé à gauche de la courbe — comme GrapheProfil.
            for valeur in (alt_min, alt_max):
                self._poser_texte(f"{int(round(valeur))}", zx + dp(4), y_alt(valeur), ROUGE,
                                  taille_sp=9, centre_v=True, gras=True)

            # Cadre gris du graphique — comme GrapheProfil.
            Color(0.55, 0.55, 0.55, 1)
            Line(points=[zx, zy, zx + zw, zy, zx + zw, zy + zh, zx, zy + zh], width=1.2)

            # Graduations de l'axe X des distances (gris) — comme
            # GrapheProfil.
            for valeur in self._graduations(0.0, dist_fin, 5):
                gxv = x_km(valeur)
                Color(0.88, 0.88, 0.88, 1)
                Line(points=[gxv, zy, gxv, zy + zh], width=1)
                self._poser_texte(f"{valeur:.1f}", gxv, zy - dp(16), self.GRIS_TEXTE,
                                  taille_sp=9, centre_h=True, gras=False)

            # Barres : une par tranche de 500 m, partant du ZÉRO de
            # l'axe des abscisses (gy, base du graphique) et montant
            # jusqu'à la courbe d'altitude. Pour épouser EXACTEMENT
            # la courbe (tracée sur les points GPS réels) sans vides
            # ni débordements, chaque tranche est découpée en BANDES
            # VERTICALES aux points réels qu'elle contient : la couleur
            # reste celle de la classe de pente de la tranche, mais le
            # sommet de chaque bande suit la courbe point à point.
            for dist_km, pente, alt_dep, alt_arr in self.tranches:
                x0, x1 = x_km(dist_km), x_km(min(dist_km + 0.5, dist_fin))
                couleur = self._couleur_pente(pente)
                # Bornes verticales internes : les points réels situés
                # STRICTEMENT à l'intérieur de la tranche.
                bornes = [d_pc for d_pc, a_pc in self.points_courbe
                          if dist_km < d_pc < dist_km + 0.5]
                bornes.append(min(dist_km + 0.5, dist_fin))
                d_prec = dist_km
                a_prec = alt_dep
                for d_b in bornes:
                    # Altitude de la courbe à la borne : celle du point
                    # réel s'il existe, sinon l'altitude interpolée de
                    # fin de tranche.
                    a_b = alt_arr
                    for d_pc, a_pc in self.points_courbe:
                        if abs(d_pc - d_b) < 1e-9:
                            a_b = a_pc
                    Color(*couleur)
                    Triangle(points=[x_km(d_prec), gy, x_km(d_prec), y_alt(a_prec),
                                     x_km(d_b), y_alt(a_b)])
                    Triangle(points=[x_km(d_prec), gy, x_km(d_b), y_alt(a_b), x_km(d_b), gy])
                    d_prec, a_prec = d_b, a_b
                # Valeur de la pente inscrite au-dessus de la courbe,
                # si elle tient horizontalement. Optionnelle
                # (afficher_valeurs_pentes = False la retire).
                if getattr(self, "afficher_valeurs_pentes", True):
                    texte = f"{pente:+.1f}"
                    tex = self._texte_texture(texte, taille_sp=8, gras=False)
                    w_barre = x1 - x0
                    if tex.width < w_barre - dp(2):
                        Color(*self.GRIS_TEXTE)
                        KivyRectangle(texture=tex,
                                     pos=(x0 + (w_barre - tex.width) / 2, y_alt(max(alt_dep, alt_arr)) + dp(1)),
                                     size=tex.size)

            # Courbe d'altitude : tracée à partir des TOUS les points
            # GPS réels (self.points_courbe) pour être EXACTEMENT la
            # même que sur le profil d'altitude — BLEUE, comme
            # GrapheProfil (onglet 4). Repli sur les bornes de
            # tranches si les points réels n'ont pas été fournis.
            if self.points_courbe:
                pts_courbe = []
                for d_pc, a_pc in self.points_courbe:
                    pts_courbe.append(x_km(min(d_pc, dist_fin)))
                    pts_courbe.append(y_alt(a_pc))
            else:
                pts_courbe = []
                for dist_km, pente, alt_dep, alt_arr in self.tranches:
                    pts_courbe.append(x_km(dist_km))
                    pts_courbe.append(y_alt(alt_dep))
                dernier = self.tranches[-1]
                pts_courbe.append(x_km(min(dernier[0] + 0.5, dist_fin)))
                pts_courbe.append(y_alt(dernier[3]))
            Color(*self.BLEU)
            Line(points=pts_courbe, width=1.6)

            # Curseur de sélection interconnecté : ligne verticale
            # pointillée rouge, même graphisme que GrapheProfil.
            if self.distance_selection is not None:
                cx = x_km(self.distance_selection)
                Color(*self.couleur_curseur)
                longueur_trait = dp(5)
                longueur_espace = dp(4)
                y = gy
                while y < gy + gh:
                    y_fin = min(y + longueur_trait, gy + gh)
                    Line(points=[cx, y, cx, y_fin], width=1.4)
                    y += longueur_trait + longueur_espace

        # Légendes d'axes — mêmes positions/couleurs que GrapheProfil.
        self._poser_texte("Distance (km)", zx + zw / 2, self.y, self.GRIS_TEXTE,
                          taille_sp=10, centre_h=True)
        # Titre « Pentes sur 500 m ... » : optionnel, masquable
        # (afficher_titre = False, utilisé par l'onglet Statistiques).
        if getattr(self, "afficher_titre", True):
            nb_montees = sum(1 for t in self.tranches if t[1] > 0)
            nb_descentes = sum(1 for t in self.tranches if t[1] < 0)
            self._poser_texte(
                f"Pentes sur 500 m   ·   {nb_montees} en pentes positives   ·   {nb_descentes} en pentes négatives",
                gx + gw / 2, gy + gh + dp(8),
                (0.16, 0.2, 0.26, 1), taille_sp=11, centre_h=True, gras=True)


# ----------------------------------------------------------------------
# Dossier racine utilisé pour parcourir/enregistrer les fichiers.
# ----------------------------------------------------------------------
if platform == "android":
    DOSSIER_CHARGEMENT = "/storage/emulated/0/GPX_Files/"
    # Nouveau dossier de sortie demandé
    DOSSIER_SORTIE = "/storage/emulated/0/GPX_Files/Bubu_GPS_Files"
    
    # S'assure que le dossier de sortie existe sur l'appareil Android
    try:
        os.makedirs(DOSSIER_SORTIE, exist_ok=True)
    except Exception:
        pass
else:
    DOSSIER_CHARGEMENT = os.path.join(os.path.expanduser("~"), "Desktop", "GPX-Speed_ok")
    DOSSIER_SORTIE = DOSSIER_CHARGEMENT

# Rétrocompatibilité si d'autres parties du code utilisent encore DOSSIER_RACINE
DOSSIER_RACINE = DOSSIER_CHARGEMENT

# Fonctionnalités qui restent à intégrer (affichées dans le menu déroulant
# avec un écran "à venir" en attendant leur code Python).
SCREENS_A_VENIR = [
]

KV = """
<ConversionScreen>:
    BoxLayout:
        orientation: "vertical"
        padding: dp(16)
        spacing: dp(12)

        Label:
            text: "Conversion GPX / KMZ / KML"
            font_size: "20sp"
            bold: True
            size_hint_y: None
            height: dp(40)
            color: 0, 0, 0, 1

        Button:
            text: "Charger une trace (GPX, KMZ, KML)"
            size_hint_y: None
            height: dp(56)
            background_color: 0.2, 0.6, 0.86, 1
            on_release: root.ouvrir_selecteur_fichier()

        Label:
            id: lbl_info
            text: root.info_fichier
            size_hint_y: None
            height: dp(60)
            text_size: self.width, None
            color: 0.2, 0.5, 0.2, 1
            halign: "left"
            valign: "top"
            italic: True

        AnchorLayout:
            size_hint_y: None
            height: dp(32)
            anchor_x: "center"
            Label:
                text: "Format de sortie :"
                size_hint: None, None
                size: self.texture_size
                bold: True
                color: 0, 0, 0, 1

        AnchorLayout:
            size_hint_y: None
            height: dp(48)
            anchor_x: "center"
            BoxLayout:
                size_hint: None, None
                size: dp(240), dp(48)
                spacing: dp(6)
                ToggleButton:
                    text: "GPX"
                    group: "format"
                    state: "down"
                    on_state: if self.state == "down": root.format_sortie = "gpx"
                ToggleButton:
                    text: "KML"
                    group: "format"
                    on_state: if self.state == "down": root.format_sortie = "kml"
                ToggleButton:
                    text: "KMZ"
                    group: "format"
                    on_state: if self.state == "down": root.format_sortie = "kmz"

        BoxLayout:
            size_hint_y: None
            height: dp(64)
            spacing: dp(8)
            CheckBox:
                size_hint: None, None
                size: dp(24), dp(24)
                pos_hint: {"center_y": 0.5}
                active: root.garder_temps
                on_active: root.garder_temps = self.active
                canvas.before:
                    Color:
                        rgba: 0, 0, 0, 1
                    Line:
                        width: 1.2
                        rectangle: (self.x, self.y, self.width, self.height)
            Label:
                text: "Conserver l'horodatage"
                color: 0, 0, 0, 1
                text_size: self.width, self.height
                halign: "left"
                valign: "middle"

        Button:
            text: "Convertir et enregistrer"
            size_hint_y: None
            height: dp(56)
            disabled: not root.fichier_source
            background_color: 0.15, 0.68, 0.38, 1
            on_release: root.lancer_conversion()

        Label:
            id: lbl_status
            text: root.status_text
            size_hint_y: None
            height: max(dp(30), self.texture_size[1] + dp(10))
            text_size: self.width, None
            halign: "left"
            valign: "top"
            color: root.status_color

        Widget:

<NumerotationScreen>:
    ScrollView:
        BoxLayout:
            orientation: "vertical"
            size_hint_y: None
            height: self.minimum_height
            padding: dp(16)
            spacing: dp(10)

            Label:
                text: "Numérotation et nettoyage"
                font_size: "20sp"
                bold: True
                size_hint_y: None
                height: dp(40)
                color: 0, 0, 0, 1

            Button:
                text: "Charger une trace (GPX, KMZ, KML)"
                size_hint_y: None
                height: dp(56)
                background_color: 0.2, 0.6, 0.86, 1
                on_release: root.ouvrir_selecteur_fichier()

            Label:
                text: root.info_fichier
                size_hint_y: None
                height: max(dp(40), self.texture_size[1] + dp(10))
                text_size: self.width, None
                halign: "left"
                valign: "top"
                color: 0.2, 0.5, 0.2, 1
                italic: True

            BoxLayout:
                size_hint_y: None
                height: dp(56)
                spacing: dp(8)
                CheckBox:
                    size_hint: None, None
                    size: dp(24), dp(24)
                    pos_hint: {"center_y": 0.5}
                    disabled: not root.trace_chargee
                    active: root.inverser
                    on_active: root.inverser = self.active
                    canvas.before:
                        Color:
                            rgba: 0, 0, 0, 1
                        Line:
                            width: 1.2
                            rectangle: (self.x, self.y, self.width, self.height)
                Label:
                    text: "Inverser le sens de la trace"
                    text_size: self.width, self.height
                    halign: "left"
                    valign: "middle"
                    color: 0.18, 0.49, 0.2, 1
                    bold: True
                CheckBox:
                    size_hint: None, None
                    size: dp(24), dp(24)
                    pos_hint: {"center_y": 0.5}
                    disabled: not root.trace_chargee
                    active: root.supprimer_waypoints
                    on_active: root.supprimer_waypoints = self.active
                    canvas.before:
                        Color:
                            rgba: 0, 0, 0, 1
                        Line:
                            width: 1.2
                            rectangle: (self.x, self.y, self.width, self.height)
                Label:
                    text: "Supprimer les waypoints"
                    text_size: self.width, self.height
                    halign: "left"
                    valign: "middle"
                    color: 0.1843, 0.6549, 0.8313, 1
                    bold: True

            Label:
                text: "Action sur les numéros :"
                size_hint_y: None
                height: dp(26)
                color: 0, 0, 0, 1
                bold: True

            BoxLayout:
                size_hint_y: None
                height: dp(56)
                spacing: dp(8)
                CheckBox:
                    size_hint: None, None
                    size: dp(24), dp(24)
                    pos_hint: {"center_y": 0.5}
                    group: "mode_num"
                    disabled: not root.trace_chargee
                    active: root.mode == "aucun"
                    on_active: if self.active: root.mode = "aucun"
                    canvas.before:
                        Color:
                            rgba: 0, 0, 0, 1
                        Line:
                            width: 1.2
                            rectangle: (self.x, self.y, self.width, self.height)
                Label:
                    text: "Aucune action sur les numéros"
                    text_size: self.width, self.height
                    halign: "left"
                    valign: "middle"
                    color: 0, 0, 0, 1

            BoxLayout:
                size_hint_y: None
                height: dp(56)
                spacing: dp(8)
                CheckBox:
                    size_hint: None, None
                    size: dp(24), dp(24)
                    pos_hint: {"center_y": 0.5}
                    group: "mode_num"
                    disabled: not root.trace_chargee or root.deja_numerote
                    active: root.mode == "numeroter"
                    on_active: if self.active: root.mode = "numeroter"
                    canvas.before:
                        Color:
                            rgba: 0, 0, 0, 1
                        Line:
                            width: 1.2
                            rectangle: (self.x, self.y, self.width, self.height)
                Label:
                    text: "Numéroter les points de trace"
                    text_size: self.width, self.height
                    halign: "left"
                    valign: "middle"
                    color: 0, 0, 0, 1

            BoxLayout:
                size_hint_y: None
                height: dp(56)
                spacing: dp(8)
                CheckBox:
                    size_hint: None, None
                    size: dp(24), dp(24)
                    pos_hint: {"center_y": 0.5}
                    group: "mode_num"
                    disabled: not root.trace_chargee or not root.deja_numerote
                    active: root.mode == "denumero"
                    on_active: if self.active: root.mode = "denumero"
                    canvas.before:
                        Color:
                            rgba: 0, 0, 0, 1
                        Line:
                            width: 1.2
                            rectangle: (self.x, self.y, self.width, self.height)
                Label:
                    text: "Tout dénuméroter"
                    text_size: self.width, self.height
                    halign: "left"
                    valign: "middle"
                    color: 0, 0, 0, 1

            BoxLayout:
                size_hint_y: None
                height: dp(56)
                spacing: dp(8)
                CheckBox:
                    size_hint: None, None
                    size: dp(24), dp(24)
                    pos_hint: {"center_y": 0.5}
                    group: "mode_num"
                    disabled: not root.trace_chargee or not root.deja_numerote
                    active: root.mode == "supprimer_points"
                    on_active: if self.active: root.mode = "supprimer_points"
                    canvas.before:
                        Color:
                            rgba: 0, 0, 0, 1
                        Line:
                            width: 1.2
                            rectangle: (self.x, self.y, self.width, self.height)
                Label:
                    text: "Supprimer des points GPS (indiquer les numéros)"
                    text_size: self.width, self.height
                    halign: "left"
                    valign: "middle"
                    color: 0, 0, 0, 1

            TextInput:
                id: entree_suppr
                hint_text: "Numéros à supprimer (ex: 5, 12, 20-35)"
                multiline: False
                size_hint_y: None
                height: dp(44)
                disabled: root.mode != "supprimer_points"
                text: root.texte_suppr
                on_text: root.texte_suppr = self.text

            Label:
                text: root.status_text
                size_hint_y: None
                height: max(dp(30), self.texture_size[1] + dp(10))
                text_size: self.width, None
                halign: "left"
                valign: "top"
                color: root.status_color

            Button:
                text: root.btn_executer_text
                size_hint_y: None
                height: dp(56)
                disabled: not root.btn_executer_actif
                background_color: 0.15, 0.68, 0.38, 1
                on_release: root.executer()

            Label:
                text: "Résumé des changements (avant exécution)"
                size_hint_y: None
                height: dp(30)
                bold: True
                color: 0, 0, 0, 1

            Label:
                markup: True
                text: root.legende_text
                size_hint_y: None
                height: self.texture_size[1] + dp(20)
                text_size: self.width, None
                halign: "left"
                valign: "top"
                color: 0, 0, 0, 1

<FusionScreen>:
    ScrollView:
        BoxLayout:
            orientation: "vertical"
            size_hint_y: None
            height: self.minimum_height
            padding: dp(16)
            spacing: dp(10)

            Label:
                text: "Fusion de traces"
                font_size: "20sp"
                bold: True
                size_hint_y: None
                height: dp(40)
                color: 0, 0, 0, 1

            Button:
                text: "Charger les traces à fusionner"
                size_hint_y: None
                height: dp(56)
                background_color: 0.2, 0.6, 0.86, 1
                on_release: root.ajouter_fichiers()

            # Suppression du ScrollView interne à hauteur fixe pour un affichage dynamique
            BoxLayout:
                id: box_liste
                orientation: "vertical"
                size_hint_y: None
                height: self.minimum_height
                spacing: dp(4)

            BoxLayout:
                size_hint_y: None
                height: dp(48)
                spacing: dp(6)
                Button:
                    text: "Monter"
                    on_release: root.monter()
                Button:
                    text: "Descendre"
                    on_release: root.descendre()
                Button:
                    text: "Retirer"
                    color: 1, 1, 1, 1
                    background_color: 0.8, 0.2, 0.2, 1
                    on_release: root.retirer()

            BoxLayout:
                size_hint_y: None
                height: dp(56)
                spacing: dp(8)
                CheckBox:
                    size_hint: None, None
                    size: dp(24), dp(24)
                    pos_hint: {"center_y": 0.5}
                    disabled: root.index_selectionne is None
                    active: root.inverser_selection
                    on_active: root.basculer_inversion(self.active)
                    canvas.before:
                        Color:
                            rgba: 0, 0, 0, 1
                        Line:
                            width: 1.2
                            rectangle: (self.x, self.y, self.width, self.height)
                Label:
                    text: "Inverser le sens de la trace sélectionnée"
                    text_size: self.width, self.height
                    halign: "left"
                    valign: "middle"
                    color: 0, 0, 0, 1

            Label:
                text: root.status_text
                size_hint_y: None
                height: max(dp(30), self.texture_size[1] + dp(10))
                color: root.status_color
                text_size: self.width, None
                halign: "left"
                valign: "top"

            Button:
                text: "Fusionner et enregistrer"
                size_hint_y: None
                height: dp(56)
                disabled: not root.peut_fusionner or root.en_cours
                background_color: 0.15, 0.68, 0.38, 1
                on_release: root.executer()

<CarteScreen>:
    ScrollView:
        do_scroll_x: False
        BoxLayout:
            orientation: "vertical"
            size_hint_y: None
            height: self.minimum_height
            padding: dp(16)
            spacing: dp(8)

            Label:
                text: "Découpe"
                font_size: "20sp"
                bold: True
                size_hint_y: None
                height: dp(36)
                color: 0, 0, 0, 1

            BoxLayout:
                size_hint_y: None
                height: dp(48)
                spacing: dp(6)
                Button:
                    text: "Charger une trace"
                    background_color: 0.2, 0.6, 0.86, 1
                    on_release: root.ouvrir_selecteur_fichier()
                # Fond de carte : bouton carre ouvrant le menu deroulant
                # des 4 vues (satellite par defaut), affichant l'icone
                # images/Layer.png en 48 x 48 dp.
                Button:
                    id: btn_layer
                    size_hint_x: None
                    width: dp(48)
                    padding: 0, 0
                    on_release: root.ouvrir_menu_fonds(self)
                    Image:
                        source: app.CHEMIN_ICONE_LAYER
                        size_hint: None, None
                        size: dp(48), dp(48)
                        center_x: self.parent.center_x
                        center_y: self.parent.center_y
                        allow_stretch: True
                        keep_ratio: True

            Label:
                text: root.info_fichier
                size_hint_y: None
                height: max(dp(30), self.texture_size[1] + dp(8))
                text_size: self.width, None
                halign: "left"
                valign: "top"
                color: 0.2, 0.5, 0.2, 1
                italic: True

            RelativeLayout:
                size_hint_y: None
                height: dp(220)

                BoxLayout:
                    id: map_container
                    pos_hint: {"x": 0, "y": 0}
                    size_hint: 1, 1

                Button:
                    text: "-"
                    font_size: "24sp"
                    bold: True
                    color: 0, 0, 0, 1
                    size_hint: None, None
                    size: dp(36), dp(36)
                    pos_hint: {"x": 0.03, "top": 0.95}
                    background_normal: ""
                    background_color: 0, 0, 0, 0
                    on_release: root.dezoomer_carte()

                    canvas.before:
                        Color:
                            rgba: 1, 1, 1, 1
                        Ellipse:
                            pos: self.pos
                            size: self.size
                            
                Button:
                    text: "+"
                    font_size: "24sp"
                    bold: True
                    color: 0, 0, 0, 1
                    size_hint: None, None
                    size: dp(36), dp(36)
                    pos_hint: {"right": 0.97, "top": 0.95}
                    background_normal: ""
                    background_color: 0, 0, 0, 0
                    on_release: root.zoomer_carte()

                    canvas.before:
                        Color:
                            rgba: 1, 1, 1, 0.9
                        Ellipse:
                            pos: self.pos
                            size: self.size

            Label:
                text: root.info_point_text
                size_hint_y: None
                height: (max(dp(20), self.texture_size[1] + dp(4)) if root.info_point_text else 0)
                text_size: self.width, None
                halign: "left"
                valign: "top"
                font_size: "12sp"
                color: 0, 0, 0, 1

            AnchorLayout:
                anchor_x: "center"
                size_hint_y: None
                height: ligne_info_point_carte.height

                BoxLayout:
                    id: ligne_info_point_carte
                    orientation: "horizontal"
                    size_hint: None, None
                    size: self.minimum_size
                    spacing: dp(16)

                    BoxLayout:
                        orientation: "vertical"
                        size_hint: None, None
                        size: self.minimum_size
                        spacing: dp(2)

                        Label:
                            text: root.info_point_num
                            size_hint: None, None
                            size: self.texture_size
                            font_size: "12sp"
                            color: 0, 0, 0, 1
                        Label:
                            text: root.info_point_dist
                            size_hint: None, None
                            size: self.texture_size
                            font_size: "12sp"
                            color: 0, 0, 0, 1
                        Label:
                            text: root.info_point_heure
                            size_hint: None, None
                            size: self.texture_size
                            font_size: "12sp"
                            color: 0, 0, 0, 1

                    BoxLayout:
                        orientation: "vertical"
                        size_hint: None, None
                        size: self.minimum_size
                        spacing: dp(2)

                        Label:
                            text: root.info_point_gps
                            size_hint: None, None
                            size: self.texture_size
                            font_size: "12sp"
                            color: 0, 0, 0, 1
                        Label:
                            text: root.info_point_alt
                            size_hint: None, None
                            size: self.texture_size
                            font_size: "12sp"
                            color: 0, 0, 0, 1
                        Label:
                            text: root.info_point_vit
                            size_hint: None, None
                            size: self.texture_size
                            font_size: "12sp"
                            color: 0, 0, 0, 1

            BoxLayout:
                id: zone_graphique
                size_hint_y: None
                height: dp(175)


            Label:
                text: root.status_text
                size_hint_y: None
                height: max(dp(30), self.texture_size[1] + dp(10))
                color: root.status_color
                text_size: self.width, None
                halign: "left"
                valign: "top"

            Button:
                # Bouton visible uniquement une fois la trace chargée
                # (même règle que le graphique et les blocs de
                # l'onglet Nettoyage).
                height: (dp(56) if root.trace_chargee else 0)
                opacity: (1 if root.trace_chargee else 0)
                disabled: not root.trace_chargee or root.en_cours
                background_color: 0.15, 0.68, 0.38, 1
                on_release: root.executer_decoupe()

<NettoyageScreen>:
    ScrollView:
        do_scroll_x: False
        BoxLayout:
            orientation: "vertical"
            size_hint_y: None
            height: self.minimum_height
            padding: dp(16)
            spacing: dp(8)

            Label:
                text: "Nettoyage"
                font_size: "20sp"
                bold: True
                size_hint_y: None
                height: dp(36)
                color: 0, 0, 0, 1

            BoxLayout:
                size_hint_y: None
                height: dp(48)
                spacing: dp(6)
                Button:
                    text: "Charger une trace"
                    background_color: 0.2, 0.6, 0.86, 1
                    on_release: root.ouvrir_selecteur_fichier()
                Button:
                    id: btn_layer
                    size_hint_x: None
                    width: dp(48)
                    padding: 0, 0
                    on_release: root.ouvrir_menu_fonds(self)
                    Image:
                        source: app.CHEMIN_ICONE_LAYER
                        size_hint: None, None
                        size: dp(48), dp(48)
                        center_x: self.parent.center_x
                        center_y: self.parent.center_y
                        allow_stretch: True
                        keep_ratio: True

            Label:
                text: root.info_fichier
                size_hint_y: None
                height: max(dp(30), self.texture_size[1] + dp(8))
                text_size: self.width, None
                halign: "left"
                valign: "top"
                color: 0.2, 0.5, 0.2, 1
                italic: True

            RelativeLayout:
                size_hint_y: None
                height: dp(220)

                BoxLayout:
                    id: map_container
                    pos_hint: {"x": 0, "y": 0}
                    size_hint: 1, 1

                Button:
                    text: "-"
                    font_size: "24sp"
                    bold: True
                    color: 0, 0, 0, 1
                    size_hint: None, None
                    size: dp(36), dp(36)
                    pos_hint: {"x": 0.03, "top": 0.95}
                    background_normal: ""
                    background_color: 0, 0, 0, 0
                    on_release: root.dezoomer_carte()

                    canvas.before:
                        Color:
                            rgba: 1, 1, 1, 1
                        Ellipse:
                            pos: self.pos
                            size: self.size

                Button:
                    text: "+"
                    font_size: "24sp"
                    bold: True
                    color: 0, 0, 0, 1
                    size_hint: None, None
                    size: dp(36), dp(36)
                    pos_hint: {"right": 0.97, "top": 0.95}
                    background_normal: ""
                    background_color: 0, 0, 0, 0
                    on_release: root.zoomer_carte()

                    canvas.before:
                        Color:
                            rgba: 1, 1, 1, 1
                        Ellipse:
                            pos: self.pos
                            size: self.size

            Label:
                text: root.info_point_text
                size_hint_y: None
                height: (max(dp(20), self.texture_size[1] + dp(4)) if root.info_point_text else 0)
                text_size: self.width, None
                halign: "left"
                valign: "top"
                font_size: "12sp"
                color: 0, 0, 0, 1

            AnchorLayout:
                anchor_x: "center"
                size_hint_y: None
                height: ligne_info_point_nettoyage.height

                BoxLayout:
                    id: ligne_info_point_nettoyage
                    orientation: "horizontal"
                    size_hint: None, None
                    size: self.minimum_size
                    spacing: dp(16)

                    BoxLayout:
                        orientation: "vertical"
                        size_hint: None, None
                        size: self.minimum_size
                        spacing: dp(2)

                        Label:
                            text: root.info_point_num
                            size_hint: None, None
                            size: self.texture_size
                            font_size: "12sp"
                            color: 0, 0, 0, 1
                        Label:
                            text: root.info_point_dist
                            size_hint: None, None
                            size: self.texture_size
                            font_size: "12sp"
                            color: 0, 0, 0, 1
                        Label:
                            text: root.info_point_heure
                            size_hint: None, None
                            size: self.texture_size
                            font_size: "12sp"
                            color: 0, 0, 0, 1

                    BoxLayout:
                        orientation: "vertical"
                        size_hint: None, None
                        size: self.minimum_size
                        spacing: dp(2)

                        Label:
                            text: root.info_point_gps
                            size_hint: None, None
                            size: self.texture_size
                            font_size: "12sp"
                            color: 0, 0, 0, 1
                        Label:
                            text: root.info_point_alt
                            size_hint: None, None
                            size: self.texture_size
                            font_size: "12sp"
                            color: 0, 0, 0, 1
                        Label:
                            text: root.info_point_vit
                            size_hint: None, None
                            size: self.texture_size
                            font_size: "12sp"
                            color: 0, 0, 0, 1

            Label:
                text: "Vitesse (km/h) au-dessus de laquelle un point est considéré comme aberrant :"
                size_hint_y: None
                height: (dp(28) if root.trace_chargee else 0)
                opacity: (1 if root.trace_chargee else 0)
                color: 0, 0, 0, 1
                bold: True
                font_size: "15sp"
                text_size: self.width, None
                halign: "center"

            # Les blocs suivants (zone de saisie + Détecter, compteur
            # + Supprimer de la trace, Enregistrer) sont centrés
            # horizontalement et bornés à dp(350).
            BoxLayout:
                size_hint_y: None
                height: (dp(44) if root.trace_chargee else 0)
                opacity: (1 if root.trace_chargee else 0)
                size_hint_x: None
                width: dp(350)
                pos_hint: {"center_x": 0.5}
                spacing: dp(8)
                TextInput:
                    id: entree_seuil_nettoyage
                    hint_text: "Seuil en km/h (ex: 10)"
                    multiline: False
                    input_filter: "float"
                    font_size: "18sp"
                    halign: "center"
                    padding: [dp(4), dp(8), dp(4), dp(8)]
                    text: root.seuil_text
                    on_text: root.changer_seuil(self.text)
                Button:
                    text: "Détecter"
                    size_hint_x: None
                    width: dp(110)
                    background_color: 0.15, 0.68, 0.38, 1
                    on_release: root.appliquer_detection()

            # Graphique visible uniquement une fois la trace chargée
            # (comme le bloc compteur et le bouton Enregistrer).
            BoxLayout:
                id: zone_graphique
                size_hint_y: None
                height: (dp(175) if root.trace_chargee else 0)

            BoxLayout:
                size_hint_y: None
                height: (dp(40) if root.trace_chargee else 0)
                size_hint_x: None
                width: dp(350)
                pos_hint: {"center_x": 0.5}
                opacity: (1 if root.trace_chargee else 0)
                disabled: not root.trace_chargee
                Label:
                    text: root.compteur_aberrants_text
                    size_hint_x: 1
                    color: 0, 0, 0, 1
                    bold: True
                    font_size: "15sp"
                    text_size: self.width, None
                    valign: "middle"
                Button:
                    text: "Supprimer de la trace"
                    size_hint_x: None
                    width: dp(160)
                    disabled: not root.aberrants_present
                    background_color: 0.776, 0.157, 0.157, 1
                    on_release: root.supprimer_aberrants()

            Button:
                text: "Enregistrer la trace nettoyée"
                size_hint_y: None
                height: (dp(52) if root.trace_chargee else 0)
                size_hint_x: None
                width: dp(350)
                pos_hint: {"center_x": 0.5}
                opacity: (1 if root.trace_chargee else 0)
                disabled: not root.trace_nettoyee
                background_color: 0.15, 0.68, 0.38, 1
                on_release: root.enregistrer_trace_nettoyee()

<AjoutScreen>:
    # Onglet "Ajout" : VOLONTAIREMENT hors ScrollView (contrairement
    # aux autres onglets) : la carte occupe TOUTE la hauteur restante
    # de l'Ã©cran sous les boutons (size_hint_y: 1, sans limite de
    # taille verticale fixe) â c'est tout l'intÃ©rÃªt de cet onglet.
    BoxLayout:
        orientation: "vertical"
        padding: dp(16)
        spacing: dp(10)

        Label:
            text: "Ajout"
            font_size: "20sp"
            bold: True
            size_hint_y: None
            height: dp(36)
            color: 0, 0, 0, 1

        BoxLayout:
            size_hint_y: None
            height: dp(48)
            spacing: dp(6)
            Button:
                text: "Charger une trace"
                background_color: 0.2, 0.6, 0.86, 1
                on_release: root.ouvrir_selecteur_fichier()
            # Fond de carte : bouton carrÃ© ouvrant le menu dÃ©roulant
            # des 4 vues (satellite par dÃ©faut), mÃªme gabarit que le
            # bouton "Layer" des onglets Photos/Live (48 dp), affichant
            # l'icÃ´ne images/Layer.png en 48 x 48 dp.
            Button:
                id: btn_layer
                size_hint_x: None
                width: dp(48)
                padding: 0, 0
                on_release: root.ouvrir_menu_fonds(self)
                Image:
                    source: app.CHEMIN_ICONE_LAYER
                    size_hint: None, None
                    size: dp(48), dp(48)
                    center_x: self.parent.center_x
                    center_y: self.parent.center_y
                    allow_stretch: True
                    keep_ratio: True

        # Enregistrement de la trace modifiÃ©e (points ajoutÃ©s /
        # dÃ©placÃ©s par l'utilisateur) dans un nouveau fichier GPX
        # Â« <nom>_modifie.gpx Â», sans toucher au fichier source.
        # Actif dÃ¨s qu'une trace est chargÃ©e.
        Button:
            text: "Enregistrer la trace modifiÃ©e"
            background_color: 0.15, 0.68, 0.38, 1
            size_hint_y: None
            height: dp(48)
            disabled: not root.trace_chargee
            on_release: root.enregistrer_trace_modifiee()

        # Annulation UNE Ã UNE des suppressions (clic droit) : chaque
        # appui restaure la derniÃ¨re suppression. GrisÃ© quand la pile
        # des suppressions annulables est vide.
        Button:
            text: "Annuler la suppression"
            background_color: 0.85, 0.45, 0.10, 1
            size_hint_y: None
            height: dp(48)
            disabled: not root.suppression_annulable
            on_release: root.annuler_derniere_suppression()

        Label:
            text: root.info_fichier
            size_hint_y: None
            height: max(dp(30), self.texture_size[1] + dp(8))
            text_size: self.width, None
            halign: "left"
            valign: "top"
            color: 0.2, 0.5, 0.2, 1
            italic: True

        # Infos PERMANENTES du dernier point ajoutÃ©/sÃ©lectionnÃ© :
        # TROIS BLOCS CÃTE Ã CÃTE, dans l'ordre point PRÃCÃDENT,
        # POINT AJOUTÃ, point SUIVANT. Chaque bloc affiche GPS,
        # horodatage EXIF et altitude. Hauteur fixe quand un point
        # est affichÃ©, repliÃ©e Ã  zÃ©ro sinon (la carte rÃ©cupÃ¨re la
        # place).
        BoxLayout:
            orientation: "horizontal"
            size_hint_y: None
            height: dp(96) if root.info_ajout_text else 0
            opacity: 1 if root.info_ajout_text else 0
            spacing: dp(6)

            Label:
                text: root.info_avant_text
                text_size: self.width, None
                halign: "left"
                valign: "top"
                font_size: "11sp"
                size_hint: 1, 1
                color: 0.15, 0.15, 0.15, 1

            Label:
                text: root.info_ajout_text
                text_size: self.width, None
                halign: "left"
                valign: "top"
                font_size: "11sp"
                size_hint: 1, 1
                bold: True
                color: 0.55, 0.27, 0.02, 1

            Label:
                text: root.info_apres_text
                text_size: self.width, None
                halign: "left"
                valign: "top"
                font_size: "11sp"
                size_hint: 1, 1
                color: 0.15, 0.15, 0.15, 1

        # La carte remplit TOUT l'espace vertical restant : hauteur
        # libre, sans limite fixe. Boutons de zoom + / - par-dessus.
        RelativeLayout:

            BoxLayout:
                id: map_container
                pos_hint: {"x": 0, "y": 0}
                size_hint: 1, 1

            Button:
                text: "-"
                font_size: "24sp"
                bold: True
                color: 0, 0, 0, 1
                size_hint: None, None
                size: dp(36), dp(36)
                pos_hint: {"x": 0.03, "top": 0.95}
                background_normal: ""
                background_color: 0, 0, 0, 0
                on_release: root.dezoomer_carte()

                canvas.before:
                    Color:
                        rgba: 1, 1, 1, 1
                    Ellipse:
                        pos: self.pos
                        size: self.size

            Button:
                text: "+"
                font_size: "24sp"
                bold: True
                color: 0, 0, 0, 1
                size_hint: None, None
                size: dp(36), dp(36)
                pos_hint: {"right": 0.97, "top": 0.95}
                background_normal: ""
                background_color: 0, 0, 0, 0
                on_release: root.zoomer_carte()

                canvas.before:
                    Color:
                        rgba: 1, 1, 1, 0.9
                    Ellipse:
                        pos: self.pos
                        size: self.size

<StatistiquesScreen>:
    ScrollView:
        do_scroll_x: False
        BoxLayout:
            orientation: "vertical"
            size_hint_y: None
            height: self.minimum_height
            padding: dp(16)
            spacing: dp(8)

            Label:
                text: "Statistiques"
                font_size: "20sp"
                bold: True
                size_hint_y: None
                height: dp(36)
                color: 0, 0, 0, 1

            BoxLayout:
                size_hint_y: None
                height: dp(48)
                spacing: dp(6)
                Button:
                    text: "Charger une trace"
                    background_color: 0.2, 0.6, 0.86, 1
                    on_release: root.ouvrir_selecteur_fichier()
                # Fond de carte : bouton carre ouvrant le menu deroulant
                # des 4 vues (satellite par defaut), affichant l'icone
                # images/Layer.png en 48 x 48 dp.
                Button:
                    id: btn_layer
                    size_hint_x: None
                    width: dp(48)
                    padding: 0, 0
                    on_release: root.ouvrir_menu_fonds(self)
                    Image:
                        source: app.CHEMIN_ICONE_LAYER
                        size_hint: None, None
                        size: dp(48), dp(48)
                        center_x: self.parent.center_x
                        center_y: self.parent.center_y
                        allow_stretch: True
                        keep_ratio: True

            Label:
                text: root.info_fichier
                size_hint_y: None
                height: max(dp(30), self.texture_size[1] + dp(8))
                text_size: self.width, None
                halign: "left"
                valign: "top"
                color: 0.2, 0.5, 0.2, 1
                italic: True

            # Statistiques de la trace (calculs de
            # gps_logic.calculer_statistiques), mise en forme du bloc
            # point sélectionné » : libellé + valeur sur la même ligne
            # (ex. « Altitude de départ : 1250 m »), police 12sp,
            # 2 colonnes de 5 lignes. Centré horizontalement comme le
            # bloc d'infos du point (AnchorLayout). Affiché dès le
            # chargement de la trace.
            Label:
                text: "Statistiques générales"
                font_size: "15sp"
                bold: True
                size_hint_y: None
                height: (dp(26) if root.trace_chargee else 0)
                opacity: (1 if root.trace_chargee else 0)
                color: 0, 0, 0, 1

            AnchorLayout:
                anchor_x: "center"
                size_hint_y: None
                height: (stats_lignes.height if root.trace_chargee else 0)
                opacity: (1 if root.trace_chargee else 0)

                BoxLayout:
                    id: stats_lignes
                    orientation: "horizontal"
                    size_hint: None, None
                    size: self.minimum_size
                    spacing: dp(16)

                    GridLayout:
                        id: stats_gauche
                        cols: 1
                        size_hint: None, None
                        size: self.minimum_size
                        spacing: dp(2)

                    GridLayout:
                        id: stats_droite
                        cols: 1
                        size_hint: None, None
                        size: self.minimum_size
                        spacing: dp(2)

            # Espace entre le tableau de stats et la carte.
            Widget:
                size_hint_y: None
                height: dp(10)

            RelativeLayout:
                size_hint_y: None
                height: dp(220)

                BoxLayout:
                    id: map_container
                    pos_hint: {"x": 0, "y": 0}
                    size_hint: 1, 1

                Button:
                    text: "-"
                    font_size: "24sp"
                    bold: True
                    color: 0, 0, 0, 1
                    size_hint: None, None
                    size: dp(36), dp(36)
                    pos_hint: {"x": 0.03, "top": 0.95}
                    background_normal: ""
                    background_color: 0, 0, 0, 0
                    on_release: root.dezoomer_carte()

                    canvas.before:
                        Color:
                            rgba: 1, 1, 1, 1
                        Ellipse:
                            pos: self.pos
                            size: self.size
                            
                Button:
                    text: "+"
                    font_size: "24sp"
                    bold: True
                    color: 0, 0, 0, 1
                    size_hint: None, None
                    size: dp(36), dp(36)
                    pos_hint: {"right": 0.97, "top": 0.95}
                    background_normal: ""
                    background_color: 0, 0, 0, 0
                    on_release: root.zoomer_carte()

                    canvas.before:
                        Color:
                            rgba: 1, 1, 1, 0.9
                        Ellipse:
                            pos: self.pos
                            size: self.size

            Label:
                text: "Informations sur le point sélectionné"
                font_size: "15sp"
                bold: True
                size_hint_y: None
                height: (dp(26) if root.trace_chargee else 0)
                opacity: (1 if root.trace_chargee else 0)
                color: 0, 0, 0, 1

            Label:
                text: root.info_point_text
                size_hint_y: None
                height: (max(dp(20), self.texture_size[1] + dp(4)) if root.info_point_text else 0)
                text_size: self.width, None
                halign: "left"
                valign: "top"
                font_size: "12sp"
                color: 0, 0, 0, 1

            AnchorLayout:
                anchor_x: "center"
                size_hint_y: None
                height: ligne_info_point_carte.height

                BoxLayout:
                    id: ligne_info_point_carte
                    orientation: "horizontal"
                    size_hint: None, None
                    size: self.minimum_size
                    spacing: dp(16)

                    BoxLayout:
                        orientation: "vertical"
                        size_hint: None, None
                        size: self.minimum_size
                        spacing: dp(2)

                        Label:
                            text: root.info_point_num
                            size_hint: None, None
                            size: self.texture_size
                            font_size: "12sp"
                            color: 0, 0, 0, 1
                        Label:
                            text: root.info_point_dist
                            size_hint: None, None
                            size: self.texture_size
                            font_size: "12sp"
                            color: 0, 0, 0, 1
                        Label:
                            text: root.info_point_heure
                            size_hint: None, None
                            size: self.texture_size
                            font_size: "12sp"
                            color: 0, 0, 0, 1
                        Label:
                            text: root.info_point_pente
                            size_hint: None, None
                            size: self.texture_size
                            font_size: "12sp"
                            color: 0, 0, 0, 1

                    BoxLayout:
                        orientation: "vertical"
                        size_hint: None, None
                        size: self.minimum_size
                        spacing: dp(2)

                        Label:
                            text: root.info_point_gps
                            size_hint: None, None
                            size: self.texture_size
                            font_size: "12sp"
                            color: 0, 0, 0, 1
                        Label:
                            text: root.info_point_alt
                            size_hint: None, None
                            size: self.texture_size
                            font_size: "12sp"
                            color: 0, 0, 0, 1
                        Label:
                            id: lbl_vit_stats
                            text: root.info_point_vit
                            size_hint: None, None
                            size: self.texture_size
                            font_size: "12sp"
                            color: 0, 0, 0, 1
                        Label:
                            # Label vide de même hauteur que les autres :
                            # la colonne de droite compte alors 4 lignes
                            # comme celle de gauche et reste alignée
                            # ligne à ligne avec elle.
                            text: ""
                            size_hint: None, None
                            size: 0, lbl_vit_stats.height
                            font_size: "12sp"

            BoxLayout:
                id: zone_graphique
                size_hint_y: None
                height: dp(175)

            # Options du graphique d'altitude : deux cases à cocher
            # (décochées par défaut) sur une même ligne, même design
            # que l'onglet Conversion (case 24x24 dp à cadre carré
            # noir). « Vitesses » rajoute la courbe de vitesse avec
            # son axe et sa légende ; « Pentes » rajoute les bandes
            # de pente par classes de couleurs.
            # Centrées horizontalement comme le bloc « Statistiques
            # générales » (AnchorLayout + contenu à taille minimum).
            # INVISIBLES tant qu'aucune trace n'est chargée (rien à
            # montrer) : elles apparaissent en même temps que la trace
            # (même gabarit que les autres blocs conditionnels :
            # hauteur repliée à 0 + opacité 0, qui libère la place).
            AnchorLayout:
                anchor_x: "center"
                size_hint_y: None
                height: (dp(48) if root.trace_chargee else 0)
                opacity: (1 if root.trace_chargee else 0)
                disabled: not root.trace_chargee

                BoxLayout:
                    size_hint: None, None
                    size: self.minimum_size
                    spacing: dp(24)

                    BoxLayout:
                        size_hint: None, None
                        size: self.minimum_size
                        spacing: dp(8)
                        CheckBox:
                            size_hint: None, None
                            size: dp(24), dp(24)
                            pos_hint: {"center_y": 0.5}
                            on_active: root._basculer_vitesses(self.active)
                            canvas.before:
                                Color:
                                    rgba: 0, 0, 0, 1
                                Line:
                                    width: 1.2
                                    rectangle: (self.x, self.y, self.width, self.height)
                        Label:
                            text: "Vitesses"
                            color: 0, 0, 0, 1
                            size_hint: None, None
                            size: self.texture_size
                            font_size: "16sp"

                    BoxLayout:
                        size_hint: None, None
                        size: self.minimum_size
                        spacing: dp(8)
                        CheckBox:
                            size_hint: None, None
                            size: dp(24), dp(24)
                            pos_hint: {"center_y": 0.5}
                            on_active: root._basculer_pentes(self.active)
                            canvas.before:
                                Color:
                                    rgba: 0, 0, 0, 1
                                Line:
                                    width: 1.2
                                    rectangle: (self.x, self.y, self.width, self.height)
                        Label:
                            text: "Pentes"
                            color: 0, 0, 0, 1
                            size_hint: None, None
                            size: self.texture_size
                            font_size: "16sp"

<PhotosScreen>:
    ScrollView:
        BoxLayout:
            orientation: "vertical"
            size_hint_y: None
            height: self.minimum_height
            padding: dp(16)
            spacing: dp(10)

            Label:
                text: "Photos"
                font_size: "20sp"
                bold: True
                size_hint_y: None
                height: dp(40)
                color: 0, 0, 0, 1

            BoxLayout:
                size_hint_y: None
                height: dp(48)
                spacing: dp(6)
                Button:
                    text: "Charger une trace"
                    background_color: 0.2, 0.6, 0.86, 1
                    on_release: root.ouvrir_selecteur_trace()
                Button:
                    text: "Charger une photo"
                    background_color: 0.61, 0.35, 0.71, 1
                    on_release: root.ouvrir_selecteur_photo()
                # Fond de carte : bouton carre ouvrant le menu deroulant
                # des 4 vues (satellite par defaut), meme gabarit que le
                # bouton "Cam" de l'onglet Live (48 dp), affichant
                # l'icone images/Layer.png en 48 x 48 dp.
                Button:
                    id: btn_layer
                    size_hint_x: None
                    width: dp(48)
                    padding: 0, 0
                    on_release: root.ouvrir_menu_fonds(self)
                    Image:
                        source: app.CHEMIN_ICONE_LAYER
                        size_hint: None, None
                        size: dp(48), dp(48)
                        center_x: self.parent.center_x
                        center_y: self.parent.center_y
                        allow_stretch: True
                        keep_ratio: True

            Label:
                text: root.info_trace
                size_hint_y: None
                height: max(dp(24), self.texture_size[1] + dp(6))
                text_size: self.width, None
                halign: "left"
                valign: "top"
                color: 0.2, 0.5, 0.2, 1
                italic: True

            Label:
                text: root.info_photo
                size_hint_y: None
                height: max(dp(24), self.texture_size[1] + dp(6))
                text_size: self.width, None
                halign: "left"
                valign: "top"
                color: 0.4, 0.2, 0.5, 1
                italic: True

# Ligne 1 : Date/Heure et Altitude
            BoxLayout:
                size_hint_y: None
                height: (dp(60) if root.photo_chargee else 0)
                opacity: (1 if root.photo_chargee else 0)
                spacing: dp(10)

                BoxLayout:
                    orientation: "vertical"
                    spacing: dp(3)
                    Label:
                        text: "Date/Heure"
                        size_hint_y: None
                        height: dp(18)
                        text_size: self.width, None
                        halign: "left"
                        font_size: "11sp"
                        color: 0, 0, 0, 1
                    TextInput:
                        multiline: False
                        size_hint_y: None
                        height: dp(36)
                        text: root.champ_date
                        on_text: root.champ_date = self.text

                BoxLayout:
                    orientation: "vertical"
                    spacing: dp(3)
                    Label:
                        text: "Altitude"
                        size_hint_y: None
                        height: dp(18)
                        text_size: self.width, None
                        halign: "left"
                        font_size: "11sp"
                        color: 0, 0, 0, 1
                    TextInput:
                        multiline: False
                        size_hint_y: None
                        height: dp(36)
                        text: root.champ_alt
                        on_text: root.champ_alt = self.text

            # Ligne 2 : Latitude et Longitude
            BoxLayout:
                size_hint_y: None
                height: (dp(60) if root.photo_chargee else 0)
                opacity: (1 if root.photo_chargee else 0)
                spacing: dp(10)

                BoxLayout:
                    orientation: "vertical"
                    spacing: dp(3)
                    Label:
                        text: "Latitude"
                        size_hint_y: None
                        height: dp(18)
                        text_size: self.width, None
                        halign: "left"
                        font_size: "11sp"
                        color: 0, 0, 0, 1
                    TextInput:
                        multiline: False
                        size_hint_y: None
                        height: dp(36)
                        text: root.champ_lat
                        on_text: root.champ_lat = self.text

                BoxLayout:
                    orientation: "vertical"
                    spacing: dp(3)
                    Label:
                        text: "Longitude"
                        size_hint_y: None
                        height: dp(18)
                        text_size: self.width, None
                        halign: "left"
                        font_size: "11sp"
                        color: 0, 0, 0, 1
                    TextInput:
                        multiline: False
                        size_hint_y: None
                        height: dp(36)
                        text: root.champ_lon
                        on_text: root.champ_lon = self.text

            # Bloc photo à hauteur dynamique pour repousser correctement les éléments du dessous
            BoxLayout:
                size_hint_x: 1
                size_hint_y: None
                # La hauteur s'adapte automatiquement à la largeur réelle du parent divisée par le ratio de l'image (4:3)
                height: (self.width / (photo_img.image_ratio if photo_img.image_ratio else (4/3)) if root.photo_chargee else 0)
                
                canvas.before:
                    Color:
                        rgba: 0.92, 0.92, 0.92, 1
                    Rectangle:
                        pos: self.pos
                        size: self.size

                Image:
                    id: photo_img
                    source: root.miniature_source
                    size_hint: 1, 1
                    allow_stretch: True
                    keep_ratio: True

            Label:
                text: root.titre_carte
                size_hint_y: None
                height: (dp(26) if root.trace_chargee else 0)
                opacity: (1 if root.trace_chargee else 0)
                bold: True
                color: root.titre_carte_color

            RelativeLayout:
                size_hint_y: None
                height: (dp(220) if root.trace_chargee else 0)
                opacity: (1 if root.trace_chargee else 0)

                BoxLayout:
                    id: map_container
                    pos_hint: {"x": 0, "y": 0}
                    size_hint: 1, 1

                Button:
                    text: "-"
                    font_size: "24sp"
                    bold: True
                    color: 0, 0, 0, 1
                    size_hint: None, None
                    size: dp(36), dp(36)
                    pos_hint: {"x": 0.03, "top": 0.95}
                    background_normal: ""
                    background_color: 0, 0, 0, 0
                    on_release: root.dezoomer_carte()

                    canvas.before:
                        Color:
                            rgba: 1, 1, 1, 1
                        Ellipse:
                            pos: self.pos
                            size: self.size

                Button:
                    text: "+"
                    font_size: "24sp"
                    bold: True
                    color: 0, 0, 0, 1
                    size_hint: None, None
                    size: dp(36), dp(36)
                    pos_hint: {"right": 0.97, "top": 0.95}
                    background_normal: ""
                    background_color: 0, 0, 0, 0
                    on_release: root.zoomer_carte()

                    canvas.before:
                        Color:
                            rgba: 1, 1, 1, 0.9
                        Ellipse:
                            pos: self.pos
                            size: self.size

            Button:
                text: "Situer (Horodatage)"
                size_hint_y: None
                height: (dp(48) if root.photo_chargee and root.trace_chargee else 0)
                opacity: (1 if root.photo_chargee and root.trace_chargee else 0)
                background_color: 0.16, 0.5, 0.73, 1
                on_release: root.situer()

            Button:
                text: "Enregistrer EXIF"
                size_hint_y: None
                height: (dp(48) if root.photo_chargee and root.trace_chargee else 0)
                opacity: (1 if root.photo_chargee and root.trace_chargee else 0)
                background_color: 0.90, 0.49, 0.13, 1
                on_release: root.enregistrer_exif()

            Label:
                text: root.status_text
                size_hint_y: None
                height: max(dp(30), self.texture_size[1] + dp(10))
                text_size: self.width, None
                halign: "left"
                valign: "top"
                color: root.status_color

<LiveScreen>:
    ScrollView:
        do_scroll_x: False
        BoxLayout:
            orientation: "vertical"
            size_hint_y: None
            height: self.minimum_height
            padding: dp(16)
            spacing: dp(8)

            Label:
                text: "Live"
                font_size: "20sp"
                bold: True
                size_hint_y: None
                height: dp(36)
                color: 0, 0, 0, 1

            BoxLayout:
                size_hint_y: None
                height: dp(48)
                spacing: dp(6)
                Button:
                    text: "Charger une trace"
                    disabled: root.freeze_actif
                    background_color: 0.2, 0.6, 0.86, 1
                    on_release: root.ouvrir_selecteur_fichier()
                # Fond de carte : bouton carre ouvrant le menu deroulant
                # des 4 vues (satellite par defaut), affichant l'icone
                # images/Layer.png en 48 x 48 dp. Soumis au gel.
                Button:
                    id: btn_layer
                    size_hint_x: None
                    width: dp(48)
                    padding: 0, 0
                    disabled: root.freeze_actif
                    on_release: root.ouvrir_menu_fonds(self)
                    Image:
                        source: app.CHEMIN_ICONE_LAYER
                        size_hint: None, None
                        size: dp(48), dp(48)
                        center_x: self.parent.center_x
                        center_y: self.parent.center_y
                        allow_stretch: True
                        keep_ratio: True
                        opacity: 0.35 if self.parent.disabled else 1

            BoxLayout:
                size_hint_y: None
                height: dp(48)
                spacing: dp(6)
                Button:
                    text: "Live"
                    disabled: root.freeze_actif
                    on_release: root.on_click_live_pydroid()
                    background_color: 0.15, 0.68, 0.38, 1
                Button:
                    # Ouverture de l'appareil photo par SIMPLE CLIC
                    # (on_release, aucune duree minimale d'appui). Voir
                    # _verifier_et_ouvrir_camera : le message d'absence
                    # de live s'affiche toujours. Bouton carre (48 dp,
                    # comme la hauteur de la rangee) affichant l'icone
                    # images/Camera.png en 48 x 48 dp (elle remplace
                    # l'ancien texte "Cam").
                    id: btn_cam
                    size_hint_x: None
                    width: dp(48)
                    padding: 0, 0
                    disabled: root.freeze_actif
                    on_release: root._verifier_et_ouvrir_camera()
                    background_color: 0.39, 0.58, 0.93, 1
                    Image:
                        source: root.CHEMIN_ICONE_CAM
                        size_hint: None, None
                        size: dp(48), dp(48)
                        center_x: self.parent.center_x
                        center_y: self.parent.center_y
                        allow_stretch: True
                        keep_ratio: True
                        opacity: 0.35 if self.parent.disabled else 1
                Button:
                    text: "Terminer"
                    disabled: root.freeze_actif
                    on_release: root.on_click_terminer_live()
                    background_color: 0.8, 0.2, 0.2, 1

            Label:
                text: root.statut_live_text
                size_hint_y: None
                height: max(dp(24), self.texture_size[1] + dp(6))
                text_size: self.width, None
                halign: "left"
                valign: "top"
                italic: True
                color: root.statut_live_color

            Label:
                text: root.temp_live_text
                size_hint_y: None
                height: max(dp(24), self.texture_size[1] + dp(6)) if self.text else 0
                opacity: 1 if self.text else 0
                text_size: self.width, None
                halign: "left"
                valign: "top"
                italic: True
                color: 0.10, 0.30, 0.60, 1

            Label:
                text: root.info_fichier
                size_hint_y: None
                height: max(dp(30), self.texture_size[1] + dp(8))
                text_size: self.width, None
                halign: "left"
                valign: "top"
                color: 0.2, 0.5, 0.2, 1
                italic: True

            RelativeLayout:
                size_hint_y: None
                height: dp(220)

                BoxLayout:
                    id: map_container
                    pos_hint: {"x": 0, "y": 0}
                    size_hint: 1, 1

                Button:
                    text: "-"
                    disabled: root.freeze_actif
                    font_size: "24sp"
                    bold: True
                    color: 0, 0, 0, 1
                    size_hint: None, None
                    size: dp(36), dp(36)
                    pos_hint: {"x": 0.03, "top": 0.95}
                    background_normal: ""
                    background_color: 0, 0, 0, 0
                    on_release: root.dezoomer_carte()

                    canvas.before:
                        Color:
                            rgba: 1, 1, 1, 1
                        Ellipse:
                            pos: self.pos
                            size: self.size
                            
                Button:
                    text: "+"
                    disabled: root.freeze_actif
                    font_size: "24sp"
                    bold: True
                    color: 0, 0, 0, 1
                    size_hint: None, None
                    size: dp(36), dp(36)
                    pos_hint: {"right": 0.97, "top": 0.95}
                    background_normal: ""
                    background_color: 0, 0, 0, 0
                    on_release: root.zoomer_carte()

                    canvas.before:
                        Color:
                            rgba: 1, 1, 1, 0.9
                        Ellipse:
                            pos: self.pos
                            size: self.size

            # --- AJOUT : Bloc Informations du point sélectionné ---
            Label:
                text: root.info_point_text
                size_hint_y: None
                height: (max(dp(20), self.texture_size[1] + dp(4)) if root.info_point_text else 0)
                text_size: self.width, None
                halign: "left"
                valign: "top"
                font_size: "12sp"
                color: 0, 0, 0, 1

            AnchorLayout:
                anchor_x: "center"
                size_hint_y: None
                height: ligne_info_point_live.height

                BoxLayout:
                    id: ligne_info_point_live
                    orientation: "horizontal"
                    size_hint: None, None
                    size: self.minimum_size
                    spacing: dp(16)

                    BoxLayout:
                        orientation: "vertical"
                        size_hint: None, None
                        size: self.minimum_size
                        spacing: dp(2)

                        Label:
                            text: root.info_point_num
                            size_hint: None, None
                            size: self.texture_size
                            font_size: "12sp"
                            color: 0, 0, 0, 1
                        Label:
                            text: root.info_point_dist
                            size_hint: None, None
                            size: self.texture_size
                            font_size: "12sp"
                            color: 0, 0, 0, 1
                        Label:
                            text: root.info_point_heure
                            size_hint: None, None
                            size: self.texture_size
                            font_size: "12sp"
                            color: 0, 0, 0, 1

                    BoxLayout:
                        orientation: "vertical"
                        size_hint: None, None
                        size: self.minimum_size
                        spacing: dp(2)

                        Label:
                            text: root.info_point_gps
                            size_hint: None, None
                            size: self.texture_size
                            font_size: "12sp"
                            color: 0, 0, 0, 1
                        Label:
                            text: root.info_point_alt
                            size_hint: None, None
                            size: self.texture_size
                            font_size: "12sp"
                            color: 0, 0, 0, 1
                        Label:
                            text: root.info_point_vit
                            size_hint: None, None
                            size: self.texture_size
                            font_size: "12sp"
                            color: 0, 0, 0, 1

            # --- AJOUT : Emplacement du graphique d'altitude/vitesse ---
            BoxLayout:
                id: zone_graphique
                size_hint_y: None
                height: dp(175)
"""


def _ouvrir_popup_selection(ecran, titre, contenu, taille=(0.95, 0.95)):
    """CrÃ©e et ouvre le popup de sÃ©lection de fichier d'un Ã©cran, AVEC
    DEBOUNCE : sur certains tÃ©lÃ©phones Android (ROM MIUI/HyperOS en
    particulier), un tap sur un bouton peut dispatcher on_release DEUX
    FOIS (double Ã©vÃ©nement tactile â mÃªme cause que le bug du menu
    dÃ©roulant). Sans garde, chaque appel crÃ©ait un NOUVEAU popup par-
    dessus le prÃ©cÃ©dent : le choix refermait le dernier (la rÃ©fÃ©rence
    self._popup pointait dessus), mais le PREMIER restait ouvert,
    dessinÃ© par-dessus l'Ã©cran â il fallait le bouton Â« retour Â»
    d'Android pour s'en dÃ©barrasser. DÃ©bounce : toute nouvelle demande
    d'ouverture moins de 0,5 s aprÃ¨s la prÃ©cÃ©dente est IGNORÃE ; au
    passage, si un popup de sÃ©lection traÃ®ne encore, il est refermÃ©.
    La rÃ©fÃ©rence est rangÃ©e dans ecran._popup comme avant (les
    _fichier_choisi des Ã©crans continuent de la dismiss())."""
    maintenant = Clock.get_time()
    if maintenant - getattr(ecran, "_t_dernier_popup_selection", -10.0) < 0.5:
        return  # double dispatch du mÃªme tap : le premier popup reste
    ecran._t_dernier_popup_selection = maintenant
    # Garde-fou : referme un Ã©ventuel popup de sÃ©lection encore ouvert
    # (changement d'Ã©cran pendant la sÃ©lection, par exemple).
    ancien = getattr(ecran, "_popup", None)
    if ancien is not None and ancien.parent is not None:
        try:
            ancien.dismiss()
        except Exception:
            pass
    ecran._popup = Popup(title=titre, content=contenu, size_hint=taille)
    ecran._popup.open()


class ConversionScreen(Screen):
    fichier_source = StringProperty("")
    info_fichier = StringProperty("Aucune trace chargée.")
    format_sortie = StringProperty("gpx")
    garder_temps = BooleanProperty(True)
    status_text = StringProperty("")
    status_color = ListProperty([0.15, 0.5, 0.15, 1])
    en_cours = BooleanProperty(False)

    def ouvrir_selecteur_fichier(self):
        contenu = _construire_selecteur_fichier(self._fichier_choisi)
        if contenu is not None:
            self._popup = Popup(title="Choisir un fichier", content=contenu, size_hint=(0.95, 0.95))
            self._popup.open()

    def _fichier_choisi(self, chemin):
        if hasattr(self, '_popup'):
            self._popup.dismiss()
        if not chemin:
            return
        self.fichier_source = chemin
        self.info_fichier = f"Trace : {os.path.basename(chemin)}"
        self.status_text = ""

    def lancer_conversion(self):
        if not self.fichier_source or self.en_cours:
            return
        self.en_cours = True
        self.status_text = "Conversion en cours..."
        threading.Thread(target=self._conversion_thread, daemon=True).start()

    def _conversion_thread(self):
        try:
            # Appel de la fonction de conversion dans gps_logic 
            # (qui gère désormais la copie silencieuse des waypoints)
            chemin_sortie = gps_logic.convertir_fichier(
                self.fichier_source,
                self.format_sortie,
                self.garder_temps,
                dossier_sortie=DOSSIER_SORTIE,
            )
            message = f"Action réussie !\nFichier généré : {os.path.basename(chemin_sortie)}"
            couleur = [0.15, 0.5, 0.15, 1]
        except Exception as e:
            message = f"Échec de la conversion : {e}"
            couleur = [0.8, 0.1, 0.8, 1]

        def _maj_ui(dt):
            self.en_cours = False
            self.status_text = message
            self.status_color = couleur

        Clock.schedule_once(_maj_ui, 0)


class NumerotationScreen(Screen):
    fichier_source = StringProperty("")
    info_fichier = StringProperty("Aucune trace chargée.")
    trace_chargee = BooleanProperty(False)
    deja_numerote = BooleanProperty(False)
    total_points = 0  # attribut simple (pas besoin d'être une Property Kivy)
    segments_lus = []
    # True juste après un traitement (réussi ou en échec) : empêche
    # _maj_etat() d'écraser le message de résultat par l'aperçu
    # "Prêt à effectuer...", en particulier au retour sur cet onglet
    # (on_enter), où le message disparaissait auparavant.
    _resultat_affiche = False

    mode = StringProperty("aucun")
    inverser = BooleanProperty(False)
    supprimer_waypoints = BooleanProperty(False)
    waypoints_lus = []  # waypoints du fichier chargé (pour le résumé des changements)
    texte_suppr = StringProperty("")

    status_text = StringProperty("Chargez une trace pour commencer.")
    status_color = ListProperty([0.33, 0.33, 0.33, 1])
    btn_executer_text = StringProperty("Exécuter")
    btn_executer_actif = BooleanProperty(False)
    legende_text = StringProperty("")

    en_cours = BooleanProperty(False)

    def on_enter(self, *args):
        # Force la mise à jour dès que l'écran devient visible
        self._maj_etat()

    def on_mode(self, *args):
        self._resultat_affiche = False
        self._maj_etat()

    def on_inverser(self, *args):
        self._resultat_affiche = False
        self._maj_etat()

    def on_supprimer_waypoints(self, *args):
        self._resultat_affiche = False
        self._maj_etat()

    def on_texte_suppr(self, *args):
        self._resultat_affiche = False
        self._maj_etat()

    def ouvrir_selecteur_fichier(self):
        contenu = _construire_selecteur_fichier(self._fichier_choisi)
        if contenu is not None:
            self._popup = Popup(title="Choisir un fichier", content=contenu, size_hint=(0.95, 0.95))
            self._popup.open()

    def _fichier_choisi(self, chemin):
        if hasattr(self, '_popup'):
            self._popup.dismiss()
        if not chemin:
            return
        self._resultat_affiche = False
        try:
            self.segments_lus, deja_num = gps_logic.extraire_donnees_gpx_kmz(chemin)
            self.waypoints_lus = gps_logic.lire_waypoints_source(chemin, heure_locale=False)
            self.fichier_source = chemin
            self.deja_numerote = deja_num
            self.total_points = sum(len(seg) for seg in self.segments_lus)
            nom_f = os.path.basename(chemin)
            
            # Calcul du nombre de points déjà numérotés
            nb_points_numerotes = 0
            for segment in self.segments_lus:
                for item in segment:
                    # item[4] correspond au nom/numéro du point dans le tuple de segment
                    nom_pt = item[4] if len(item) > 4 else None
                    if gps_logic.valider_numero_point(nom_pt) != "-":
                        nb_points_numerotes += 1

            # Calcul du nombre de waypoints présents
            # Même règle que les autres onglets : ni n° de points (nom
            # uniquement en chiffres), ni waypoints superposés au départ
            # ou à l'arrivée de la trace.
            nb_waypoints = len(gps_logic.vrais_waypoints(
                self.waypoints_lus, gps_logic.extremites_segments(self.segments_lus)))
            
            # Affichage demandé
            self.info_fichier = f"Trace : {nom_f}\n{nb_points_numerotes} points déjà numérotés; {nb_waypoints} waypoints."

            if self.total_points == 0:
                self.trace_chargee = False
                self.status_text = "Aucun point GPS détecté."
                self.status_color = [0.8, 0.1, 0.1, 1]
                self.btn_executer_actif = False
                self.legende_text = ""
                return

            self.trace_chargee = True
            self.mode = "denumero" if deja_num else "numeroter"
            self.inverser = False
            self.supprimer_waypoints = False
            self.texte_suppr = ""
            self._maj_etat()
        except Exception as e:
            self.trace_chargee = False
            self.status_text = f"Erreur de lecture : {e}"
            self.status_color = [0.8, 0.1, 0.1, 1]

    def _maj_etat(self):
        if not self.trace_chargee or self.total_points == 0:
            self.legende_text = ""
            return

        # On calcule toujours la légende dès qu'une trace est chargée
        self._maj_legende()

        if self._resultat_affiche:
            # Un message de résultat (réussite/échec) est affiché : on ne
            # le remplace pas par l'aperçu "Prêt à effectuer...", mais le
            # bouton reste correctement activé/désactivé.
            self.btn_executer_actif = self.inverser or self.supprimer_waypoints or self.mode != "aucun"
            return

        if not self.inverser and not self.supprimer_waypoints and self.mode == "aucun":
            self.btn_executer_actif = False
            self.btn_executer_text = "Exécuter"
            self.status_text = "Sélectionnez au moins une action (Inverser, Traitement ou Supprimer les waypoints)."
            self.status_color = [0.33, 0.33, 0.33, 1]
            return

        self.btn_executer_actif = True
        actions = []
        if self.inverser:
            actions.append("Inverser")
        if self.mode == "numeroter":
            actions.append("Numéroter")
        elif self.mode == "denumero":
            actions.append("Dénuméroter")
        elif self.mode == "supprimer_points":
            actions.append("Supprimer et renuméroter")

        if self.supprimer_waypoints:
            actions.append("Supprimer les waypoints")

        titre = " et ".join(actions)
        self.btn_executer_text = titre
        self.status_text = f"Prêt à effectuer : {titre}."
        self.status_color = [0.15, 0.5, 0.15, 1]

    def _maj_legende(self):
        try:
            compteurs = gps_logic.calculer_legende_numerotation(
                self.segments_lus, self.mode, self.inverser, self.texte_suppr,
                waypoints=self.waypoints_lus, supprimer_waypoints=self.supprimer_waypoints
            )
            lignes = []
            
            # Codes couleur BBCode pour Kivy (sans dièse)
            COULEUR_ACTIF = "000000"     # Noir
            COULEUR_INACTIF = "888888"   # Gris clair lisible

            for cle, libelle in gps_logic.LEGENDE_NUMEROTATION:
                nb = compteurs.get(cle, 0)
                valeur = ("Oui" if nb else "Non") if cle == "inverse" else str(nb)

                # Condition pour déterminer si l'option interagit positivement
                est_actif = False
                if cle == "inverse" and self.inverser:
                    est_actif = True
                elif cle in ["ajoute", "modifie", "retire", "inchange", "supprime", "waypoint"] and nb > 0:
                    est_actif = True

                couleur_texte = COULEUR_ACTIF if est_actif else COULEUR_INACTIF

                # Formatage avec balise de couleur dynamique
                lignes.append(f"[color={couleur_texte}]{libelle} : [b]{valeur}[/b][/color]")

            self.legende_text = "\n".join(lignes)
        except Exception as e:
            self.legende_text = f"Erreur de calcul du résumé : {e}"
            
    def executer(self):
        if not self.fichier_source or not self.segments_lus or self.en_cours:
            return
        if not self.inverser and not self.supprimer_waypoints and self.mode == "aucun":
            return
        self.en_cours = True
        self.status_text = "Traitement en cours..."
        self.status_color = [0.33, 0.33, 0.33, 1]
        threading.Thread(target=self._traitement_thread, daemon=True).start()

    def _traitement_thread(self):
        titre = self.btn_executer_text  # ex. "Inverser et Numéroter"
        try:
            chemin_sortie, resume = gps_logic.traiter_numerotation(
                self.fichier_source, self.segments_lus, self.mode, self.inverser, self.texte_suppr,
                dossier_sortie=DOSSIER_SORTIE, supprimer_waypoints=self.supprimer_waypoints,
            )
            # Le détail (ex. "134 points numérotés") reste visible dans le
            # résumé des changements ci-dessous ; le message de statut suit
            # le même gabarit que les autres onglets.
            message = f"Action réussie !\nFichier généré : {os.path.basename(chemin_sortie)}"
            couleur = [0.15, 0.5, 0.15, 1]
        except Exception as e:
            message = f"Échec du traitement : {e}"
            couleur = [0.8, 0.1, 0.8, 1]

        def _maj_ui(dt):
            self.en_cours = False
            self.status_text = message
            self.status_color = couleur
            self._resultat_affiche = True

        Clock.schedule_once(_maj_ui, 0)
        
def _dialogue_natif_fichier(filtres, multiple=False):
    """Ouvre l'explorateur de fichiers natif du système (Explorateur
    Windows, ou l'équivalent macOS/Linux) via tkinter.filedialog.
    Utilisé uniquement sur PC : sur Android, tkinter n'est pas
    disponible/pertinent, on garde le FileChooserListView de Kivy (voir
    les fonctions _construire_selecteur_* ci-dessous)."""
    import tkinter as tk
    from tkinter import filedialog

    racine = tk.Tk()
    racine.withdraw()
    racine.attributes("-topmost", True)
    try:
        if multiple:
            resultat = filedialog.askopenfilenames(filetypes=filtres, initialdir=DOSSIER_RACINE)
            return list(resultat) if resultat else None
        else:
            resultat = filedialog.askopenfilename(filetypes=filtres, initialdir=DOSSIER_RACINE)
            return resultat if resultat else None
    finally:
        racine.destroy()


def _construire_menu_fonds_carte(screen):
    """Construit le menu déroulant compact des fonds de carte du bouton
    carré "Layer" (onglets Carte, Photos et Live). Plus discret que le
    menu principal : 4 entrées de 40 dp, largeur 150 dp. La vue
    courante est marquée d'un point "• " en tête ; le satellite est le
    fond par défaut (l'attribut _vue_carte_actuelle de l'écran vaut
    alors "satellite", mis à jour à chaque sélection)."""
    menu = DropDown(auto_width=False, width=dp(150))
    actuelle = getattr(screen, "_vue_carte_actuelle", "satellite")
    vues = [("satellite", "Satellite"), ("plan", "Plan"),
            ("topo", "Topo"), ("esri_topo", "Topo+")]
    for valeur, libelle in vues:
        btn = Button(
            text=libelle,
            size_hint_y=None, height=dp(40), font_size="14sp")
        # Vue courante mise en evidence par la couleur de fond (meme
        # principe que le marquage de l'ecran actif dans le "Menu"
        # principal), les autres restent sur le fond standard.
        if valeur == actuelle:
            btn.background_color = (0.15, 0.68, 0.38, 1)  # vert #2E7D32
        btn.bind(on_release=lambda b, v=valeur: (
            setattr(screen, "_vue_carte_actuelle", v),
            screen.changer_vue_carte(v),
            menu.dismiss()))
        menu.add_widget(btn)
    return menu


def _construire_selecteur_fichier(callback, filtre_extensions=(".gpx", ".kmz", ".kml")):
    """Explorateur de fichiers (dossiers + fichiers) : dialogue natif sur
    PC, explorateur maison sur Android (voir _construire_explorateur_android)."""
    if platform != "android":
        # Conserve l'explorateur natif sur PC
        callback(_dialogue_natif_fichier(
            filtres=[("Traces GPS", "*.gpx *.kmz *.kml"), ("Tous les fichiers", "*.*")]
        ))
        return None

    return _construire_explorateur_android(
        callback,
        filtre_extensions=filtre_extensions,
        multiple=False,
    )


# ----------------------------------------------------------------------
# Explorateur de fichiers Android : icônes et style
# ----------------------------------------------------------------------
# La police par défaut de Kivy (Roboto) ne contient pas les emojis dossier
# et fichier : ils s'affichent en carrés. On utilise donc une petite police
# monochrome qui ne contient que ces deux pictogrammes (sous-ensemble de
# GNU Unifont Upper), livrée avec l'appli : dossier "fonts" à côté de
# main.py, et extension "otf" dans source.include_exts de buildozer.spec.
# Si le fichier est absent, l'explorateur s'affiche simplement sans icônes
# (plus de carrés).
POLICE_ICONES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "icones_explorateur.otf")
if not os.path.isfile(POLICE_ICONES):
    POLICE_ICONES = None

ICONE_DOSSIER = "\U0001F4C1"
ICONE_FICHIER = "\U0001F4C4"

COULEUR_BLANC = (1, 1, 1, 1)
COULEUR_TEXTE = (0.1, 0.1, 0.1, 1)
COULEUR_SEPARATEUR = (0.86, 0.86, 0.86, 1)
COULEUR_APPUI = (0.80, 0.88, 0.97, 1)
COULEUR_SELECTION = (0.2, 0.6, 0.86, 1)
COULEUR_SELECTION_APPUI = (0.15, 0.5, 0.75, 1)


def _texte_avec_icone(icone, texte):
    """Texte de bouton (markup) : icône dans la police dédiée, puis le
    nom (échappé pour que [ ] ou & dans un nom de fichier ne cassent pas
    le balisage). Sans police d'icônes : le nom seul."""
    if POLICE_ICONES:
        return f"[font={POLICE_ICONES}]{icone}[/font]  {escape_markup(texte)}"
    return escape_markup(texte)


def _fond_uni(widget, couleur):
    """Peint un fond uni derrière un widget (suit sa position et sa taille)."""
    with widget.canvas.before:
        Color(*couleur)
        rect = Rectangle(pos=widget.pos, size=widget.size)
    widget.bind(
        pos=lambda w, v: setattr(rect, "pos", v),
        size=lambda w, v: setattr(rect, "size", v),
    )


def _bouton_plat(texte, hauteur, fond=COULEUR_BLANC, couleur_texte=COULEUR_TEXTE):
    """Bouton à fond uni (sans la texture grise par défaut de Kivy), texte
    aligné à gauche, avec un léger changement de couleur à l'appui."""
    b = Button(
        text=texte,
        markup=True,
        size_hint_y=None,
        height=hauteur,
        background_normal="",
        background_down="",
        background_color=fond,
        color=couleur_texte,
        halign="left",
        valign="middle",
    )
    b._fond = fond
    b._fond_appui = COULEUR_APPUI
    b.bind(state=lambda inst, etat: setattr(
        inst, "background_color", inst._fond_appui if etat == "down" else inst._fond))
    b.bind(size=lambda inst, val: setattr(inst, "text_size", (max(1, val[0] - dp(20)), val[1])))
    return b


def _construire_explorateur_android(callback, filtre_extensions, multiple=False, dossier_depart=None):
    """Explorateur de fichiers Android (dossiers + fichiers) :
      - multiple=False : un clic sur un fichier le renvoie aussitôt
        (callback(chemin)) ;
      - multiple=True  : un clic coche/décoche le fichier (surligné en
        bleu) et le bouton "Valider (N)" renvoie la liste
        (callback([chemins])). La sélection est conservée quand on change
        de dossier.
    Annuler renvoie callback(None) dans les deux cas."""
    dossier_initial = DOSSIER_CHARGEMENT
    if dossier_depart and os.path.isdir(dossier_depart):
        dossier_initial = dossier_depart
    if not os.path.exists(dossier_initial):
        try:
            os.makedirs(dossier_initial, exist_ok=True)
        except Exception:
            dossier_initial = "/storage/emulated/0/"

    dossier_actuel = [dossier_initial]
    selection = []            # chemins cochés (mode multiple), dans l'ordre des clics
    boutons_fichiers = {}     # chemin -> Button, pour restyler sans tout reconstruire

    layout_principal = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))

    lbl_chemin = Label(
        text=dossier_actuel[0],
        size_hint_y=None,
        height=dp(36),
        bold=True,
        color=(0.1, 0.1, 0.1, 1),
        halign="left",
        valign="middle"
    )
    lbl_chemin.bind(size=lambda inst, val: setattr(inst, 'text_size', (max(1, val[0]), val[1])))
    layout_principal.add_widget(lbl_chemin)

    btn_haut = _bouton_plat(_texte_avec_icone(ICONE_DOSSIER, ".. (Dossier parent)"), dp(44))

    # Zone de liste : fond blanc, avec un filet gris clair entre les lignes
    # (visible grâce au spacing du conteneur, peint en gris sous les boutons).
    scroll = ScrollView(size_hint=(1, 1))
    _fond_uni(scroll, COULEUR_BLANC)
    box_contenu = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(1))
    _fond_uni(box_contenu, COULEUR_SEPARATEUR)
    box_contenu.bind(minimum_height=box_contenu.setter('height'))
    scroll.add_widget(box_contenu)

    def styler_fichier(btn, coche):
        if coche:
            btn._fond = COULEUR_SELECTION
            btn._fond_appui = COULEUR_SELECTION_APPUI
            btn.color = (1, 1, 1, 1)
        else:
            btn._fond = COULEUR_BLANC
            btn._fond_appui = COULEUR_APPUI
            btn.color = (0, 0, 0, 1)
        btn.background_color = btn._fond

    def maj_bouton_valider():
        if btn_valider is not None:
            btn_valider.text = f"Valider ({len(selection)})"
            btn_valider.disabled = (len(selection) == 0)

    def basculer_fichier(chemin):
        if chemin in selection:
            selection.remove(chemin)
        else:
            selection.append(chemin)
        btn = boutons_fichiers.get(chemin)
        if btn is not None:
            styler_fichier(btn, chemin in selection)
        maj_bouton_valider()

    def clic_fichier(chemin):
        if multiple:
            basculer_fichier(chemin)
        else:
            callback(chemin)

    def rafraichir_liste():
        box_contenu.clear_widgets()
        boutons_fichiers.clear()
        chemin_courant = dossier_actuel[0]
        lbl_chemin.text = chemin_courant

        if chemin_courant != "/" and os.path.dirname(chemin_courant) != chemin_courant:
            box_contenu.add_widget(btn_haut)

        try:
            elements = sorted(os.listdir(chemin_courant))
        except Exception as e:
            lbl_erreur = Label(
                text=f"Erreur d'accès ou permissions requises : {e}",
                color=(0.8, 0.2, 0.2, 1),
                size_hint_y=None,
                height=dp(60),
                text_size=(Window.width - dp(40), None)
            )
            _fond_uni(lbl_erreur, COULEUR_BLANC)
            box_contenu.add_widget(lbl_erreur)
            return

        dossiers = []
        fichiers = []

        for nom in elements:
            if nom.startswith('.'):
                continue
            chemin_complet = os.path.join(chemin_courant, nom)
            try:
                if os.path.isdir(chemin_complet):
                    dossiers.append((nom, chemin_complet))
                elif os.path.isfile(chemin_complet):
                    if not filtre_extensions or nom.lower().endswith(filtre_extensions):
                        fichiers.append((nom, chemin_complet))
            except Exception:
                continue

        for nom, chemin_complet in dossiers:
            b = _bouton_plat(_texte_avec_icone(ICONE_DOSSIER, nom), dp(48))
            b.bind(on_release=lambda inst, c=chemin_complet: changer_dossier(c))
            box_contenu.add_widget(b)

        for nom, chemin_complet in fichiers:
            b = _bouton_plat(_texte_avec_icone(ICONE_FICHIER, nom), dp(48))
            styler_fichier(b, chemin_complet in selection)
            b.bind(on_release=lambda inst, c=chemin_complet: clic_fichier(c))
            boutons_fichiers[chemin_complet] = b
            box_contenu.add_widget(b)

    def changer_dossier(nouveau_chemin):
        if os.path.exists(nouveau_chemin) and os.access(nouveau_chemin, os.R_OK):
            dossier_actuel[0] = nouveau_chemin
            rafraichir_liste()

    def remonter_parent(instance):
        parent = os.path.dirname(dossier_actuel[0])
        if parent and os.path.exists(parent):
            changer_dossier(parent)

    btn_haut.bind(on_release=remonter_parent)

    # Bouton Valider (mode multiple uniquement) : créé avant le premier
    # rafraichir_liste() car maj_bouton_valider() y fait référence.
    btn_valider = None
    if multiple:
        btn_valider = Button(
            text="Valider (0)",
            disabled=True,
            background_color=(0.2, 0.6, 0.86, 1),
            color=(1, 1, 1, 1)
        )
        btn_valider.bind(on_release=lambda inst: callback(list(selection)) if selection else None)

    rafraichir_liste()
    layout_principal.add_widget(scroll)

    btn_annuler = Button(
        text="Annuler",
        background_color=(0.8, 0.2, 0.2, 1),
        color=(1, 1, 1, 1)
    )
    btn_annuler.bind(on_release=lambda inst: callback(None))

    if multiple:
        barre = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        barre.add_widget(btn_annuler)
        barre.add_widget(btn_valider)
        layout_principal.add_widget(barre)
    else:
        btn_annuler.size_hint_y = None
        btn_annuler.height = dp(48)
        layout_principal.add_widget(btn_annuler)

    return layout_principal


def _construire_selecteur_fichiers_multiples(callback):
    """Variante du sélecteur ci-dessus permettant de choisir plusieurs
    fichiers d'un coup (nécessaire pour l'onglet Fusion)."""
    if platform != "android":
        callback(_dialogue_natif_fichier(
            filtres=[("Traces GPS", "*.gpx *.kmz *.kml"), ("Tous les fichiers", "*.*")],
            multiple=True,
        ))
        return None

    return _construire_explorateur_android(
        callback,
        filtre_extensions=(".gpx", ".kmz", ".kml"),
        multiple=True,
    )


def _construire_selecteur_fichier_photo(callback):
    """Variante du sélecteur de fichier ci-dessus filtrée sur les photos
    JPEG (nécessaire pour l'onglet Photos). Démarre dans GPX_Files si
    ce dossier existe, sinon dans le dossier de chargement habituel."""
    if platform != "android":
        callback(_dialogue_natif_fichier(
            filtres=[("Photos JPEG", "*.jpg *.jpeg *.JPG *.JPEG"), ("Tous les fichiers", "*.*")]
        ))
        return None

    return _construire_explorateur_android(
        callback,
        filtre_extensions=(".jpg", ".jpeg"),
        multiple=False,
        dossier_depart="/storage/emulated/0/GPX_Files/",
    )


def _construire_confirmation_oui_non_annuler(message, callback):
    """Boîte de dialogue à 3 réponses (Oui / Non / Annuler), harmonisée
    avec les standards graphiques Android de l'application."""
    layout = BoxLayout(orientation="vertical", spacing=dp(12), padding=dp(16))

    lbl_message = Label(
        text=message,
        halign="center",
        valign="middle",
        color=(1, 1, 1, 1),
        font_size="15sp"
    )
    lbl_message.bind(width=lambda inst, w: setattr(inst, "text_size", (w, None)))
    layout.add_widget(lbl_message)

    boutons = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(8))
    
    btn_annuler = Button(
        text="Annuler",
        background_color=(0.7, 0.7, 0.7, 1),
        color=(0, 0, 0, 1)
    )
    btn_non = Button(
        text="Non", 
        background_color=(0.8, 0.2, 0.2, 1),
        color=(1, 1, 1, 1)
    )
    btn_oui = Button(
        text="Oui", 
        background_color=(0.15, 0.68, 0.38, 1),
        color=(1, 1, 1, 1)
    )
    
    boutons.add_widget(btn_annuler)
    boutons.add_widget(btn_non)
    boutons.add_widget(btn_oui)
    layout.add_widget(boutons)

    btn_oui.bind(on_release=lambda inst: callback(True))
    btn_non.bind(on_release=lambda inst: callback(False))
    btn_annuler.bind(on_release=lambda inst: callback(None))
    return layout

class FusionScreen(Screen):
    inverser_selection = BooleanProperty(False)
    status_text = StringProperty("Aucune trace chargée.")
    status_color = ListProperty([0.33, 0.33, 0.33, 1])
    peut_fusionner = BooleanProperty(False)
    en_cours = BooleanProperty(False)
    index_selectionne = ObjectProperty(None, allownone=True)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.fichiers_fusion = []

    def ajouter_fichiers(self):
        contenu = _construire_selecteur_fichiers_multiples(self._fichiers_choisis)
        if contenu is not None:
            self._popup = Popup(title="Choisir les traces à fusionner", content=contenu, size_hint=(0.95, 0.95))
            self._popup.open()

    def _fichiers_choisis(self, chemins):
        if hasattr(self, '_popup'):
            self._popup.dismiss()
        if not chemins:
            return
        chemins_existants = {item["path"] for item in self.fichiers_fusion}
        for f in chemins:
            if f not in chemins_existants:
                self.fichiers_fusion.append({"path": f, "inverser": False})
        self.fichiers_fusion.sort(key=lambda x: os.path.basename(x["path"]).lower())
        self.index_selectionne = None
        self.inverser_selection = False
        self._rafraichir_liste()

    def _rafraichir_liste(self):
        box = self.ids.box_liste
        box.clear_widgets()
        for i, item in enumerate(self.fichiers_fusion):
            nom = os.path.basename(item["path"])
            if item["inverser"]:
                nom += "  [INVERSÉ]"
            selectionne = (i == self.index_selectionne)
            btn = Button(
                text=nom,
                size_hint_y=None,
                padding=(dp(10), dp(5)),
                background_color=(0.2, 0.6, 0.86, 1) if selectionne else (0.9, 0.9, 0.9, 1),
                color=(1, 1, 1, 1) if selectionne else (0, 0, 0, 1),
                halign="left",
                valign="middle",
            )
            # Permet le retour à la ligne et adapte la hauteur du bouton au contenu
            btn.bind(width=lambda instance, w: setattr(instance, 'text_size', (w - dp(20), None)))
            btn.bind(texture_size=lambda instance, size: setattr(instance, 'height', max(dp(40), size[1] + dp(10))))
            btn.bind(on_release=lambda inst, idx=i: self._selectionner(idx))
            box.add_widget(btn)

        nb = len(self.fichiers_fusion)
        if nb >= 2:
            self.status_text = f"{nb} fichiers prêts à être fusionnés."
            self.status_color = [0.15, 0.5, 0.15, 1]
            self.peut_fusionner = True
        else:
            self.status_text = "Ajoutez au moins 2 fichiers pour fusionner."
            self.status_color = [0.33, 0.33, 0.33, 1]
            self.peut_fusionner = False
            
    def _selectionner(self, idx):
        self.index_selectionne = idx
        self.inverser_selection = self.fichiers_fusion[idx]["inverser"]
        self._rafraichir_liste()

    def basculer_inversion(self, actif):
        if self.index_selectionne is None:
            return
        self.fichiers_fusion[self.index_selectionne]["inverser"] = actif
        self._rafraichir_liste()

    def monter(self):
        i = self.index_selectionne
        if i is None or i == 0:
            return
        self.fichiers_fusion[i], self.fichiers_fusion[i - 1] = self.fichiers_fusion[i - 1], self.fichiers_fusion[i]
        self.index_selectionne = i - 1
        self._rafraichir_liste()

    def descendre(self):
        i = self.index_selectionne
        if i is None or i >= len(self.fichiers_fusion) - 1:
            return
        self.fichiers_fusion[i], self.fichiers_fusion[i + 1] = self.fichiers_fusion[i + 1], self.fichiers_fusion[i]
        self.index_selectionne = i + 1
        self._rafraichir_liste()

    def retirer(self):
        i = self.index_selectionne
        if i is None:
            return
        del self.fichiers_fusion[i]
        self.index_selectionne = None
        self.inverser_selection = False
        self._rafraichir_liste()

    def executer(self):
        if not self.peut_fusionner or self.en_cours:
            return
        self.en_cours = True
        self.status_text = "Fusion en cours..."
        self.status_color = [0.33, 0.33, 0.33, 1]
        threading.Thread(target=self._fusion_thread, daemon=True).start()

    def _fusion_thread(self):
        try:
            chemin_sortie = gps_logic.traiter_fusion(list(self.fichiers_fusion), DOSSIER_SORTIE)
            message = f"Action réussie !\nFichier généré : {os.path.basename(chemin_sortie)}"
            couleur = [0.15, 0.5, 0.15, 1]
        except Exception as e:
            message = f"Échec de la fusion : {e}"
            couleur = [0.8, 0.1, 0.8, 1]

        def _maj_ui(dt):
            self.en_cours = False
            self.status_text = message
            self.status_color = couleur

        Clock.schedule_once(_maj_ui, 0)

class LiveScreen(Screen):
    freeze_actif = BooleanProperty(False)
    info_fichier = StringProperty("Aucune trace à suivre chargée.")
    # Icone du bouton "Cam" (ouverture de l'appareil photo). L'image est
    # cherchee a cote de main.py : images/Camera.png (meme principe que
    # CHEMIN_BLUE_DOT, fonctionnel sur PC comme dans l'APK).
    CHEMIN_ICONE_CAM = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "images", "Camera.png")
    info_point_text = StringProperty("")
    # Bloc "Informations du point sélectionné" (grille 3 lignes x 2
    # colonnes : Point/GPS, Distance/Altitude, Heure/Vitesse).
    info_point_num = StringProperty("")
    info_point_gps = StringProperty("")
    info_point_dist = StringProperty("")
    info_point_alt = StringProperty("")
    info_point_heure = StringProperty("")
    info_point_vit = StringProperty("")
    status_text = StringProperty("")
    status_color = ListProperty([0.33, 0.33, 0.33, 1])

    # --- Bloc statut propre au suivi EN DIRECT (rouge), indépendant de
    # info_fichier/status_text ci-dessus qui concernent la trace
    # "chargée" manuellement (bleue).
    statut_live_text = StringProperty("Aucun live en cours.")
    statut_live_color = ListProperty([0.33, 0.33, 0.33, 1])
    # Message persistant sur le fichier temporaire des annotations photo
    # (nom + emplacement) ; vide tant qu'aucune photo n'a été prise.
    temp_live_text = StringProperty("")

    # Identifiants propres à l'intégration GPSLogger, utilisés uniquement
    # par cet onglet : les garder ici les isole totalement des autres
    # onglets (les déplacer ou les supprimer avec l'onglet n'affecte
    # aucun autre onglet).
    PORT_SERVEUR_LIVE = 8765
    PACKAGE_GPSLOGGER = "com.mendhak.gpslogger"
    ACTION_TASKER_GPSLOGGER = "com.mendhak.gpslogger.TASKER_COMMAND"
    RECEIVER_TASKER_GPSLOGGER = "com.mendhak.gpslogger.TaskerReceiver"
    # Broadcast d'ETAT envoye par GPSLogger lui-meme a chaque
    # demarrage/arret d'enregistrement (feature "automation events")
    # : lire ses extras (started/stopped) suffit a savoir si une
    # trace est en cours — remplace la verification par croissance
    # de fichier (20 s) comme detection principale.
    ACTION_EVENT_GPSLOGGER = "com.mendhak.gpslogger.EVENT"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.map_view = None
        self.trace_layer = None
        self.marqueurs_actifs = []
        self.marqueurs_waypoints = []   # curseurs bleus des waypoints (comme l'onglet Photos)
        self.points_courants = []

        # --- Trace EN DIRECT (rouge) : totalement indépendante de la
        # trace "chargée" manuellement ci-dessus (bleue). Réinitialisée
        # par on_click_live_pydroid() (bouton "Live").
        self.pause_traitement_live = False
        self.points_trace_live = []
        self.trace_layer_live = None
        self.marqueurs_actifs_live = []
        self.fichier_gpx_actif_live = None
        self.compteur_sources_live = {}
        # Journal de post-mortem des points directs (voir
        # _ajouter_point_live / _arreter_gpslogger).
        self._journal_points_live = []

        # --- Etat d'enregistrement de GPSLogger (icone de notification
        # "presente" = started) : mis a jour en temps reel par le
        # broadcast com.mendhak.gpslogger.EVENT (voir
        # _enregistrer_receiver_etat_gpslogger / _sur_event_gpslogger)
        # et persiste dans un petit fichier pour survivre aux
        # redemarrages de l'appli.
        self._gpslogger_actif = None  # None = inconnu, True = started, False = stopped
        self._receiver_etat_gpslogger = None  # recepteur broadcast EVENT (Android)
        self._charger_etat_gpslogger()
        self._enregistrer_receiver_etat_gpslogger()

        self.annotations_live = []  # photos prises pendant le live (voir _ouvrir_camera_Android)
        # Balise <wpt> "en attente" : ouverte par _verifier_et_ouvrir_camera
        # au lancement de l'appareil photo, refermée par
        # _fermer_waypoint_photo au retour sur l'appli (voir
        # OutilsTracesApp.on_resume). None = aucune balise en attente.
        self._wpt_en_attente = None
        # Anti-chevauchement pour _resynchroniser_avec_gpslogger : évite
        # de lancer une seconde vérification (20 s) tant que la
        # précédente n'est pas terminée (rallumages d'écran rapprochés).
        self._resync_gpslogger_en_cours = False
        # Fichier temporaire des annotations photo du live (waypoints,
        # noms des photos, nom de la trace) : créé à la première photo,
        # supprimé à la fin de l'enregistrement (voir
        # _ecrire_fichier_temp_live / _supprimer_fichier_temp_live).
        self.fichier_temp_live = None
        self.journal_temp_live = []
        self.debut_live_temp = None

        # --- Serveur d'écoute live (HTTP local) + file thread-safe des
        # points reçus, consommée côté thread principal (Kivy, comme
        # Tkinter, n'est pas thread-safe) par _traiter_file_points_live(),
        # planifiée ci-dessous via Clock (pas besoin de se replanifier à
        # la main comme avec after() sous Tkinter : schedule_interval se
        # répète de lui-même).
        self.serveur_live = None
        self.thread_serveur_live = None
        self.file_points_live = queue.Queue()
        Clock.schedule_interval(self._traiter_file_points_live, 1.0)

        self.profil = ([], [], [], [])
        # --- Profil de la trace live (rouge), tenu à part de self.profil
        # (chargée, bleue, ci-dessus) : sert uniquement à calculer la
        # distance/vitesse du dernier point live pour le bloc
        # "Informations du point sélectionné" (voir _ajouter_point_live),
        # sans jamais écraser le profil de la trace chargée sur le
        # graphique.
        self.profil_live = ([], [], [], [])
        self.graphe = GrapheProfil()
        self.graphe.afficher_courbe_vitesse = False  # <--- AJOUT : Masque la courbe verte
        self.graphe.afficher_curseur = False  # aucun point n'est sélectionnable sur ce graphique
        self.ids.zone_graphique.add_widget(self.graphe)
        
        self.en_cours_live = False  # Indique si le live est actif ou non

        if CARTE_DISPONIBLE:
            self.map_view = MapViewMolette(zoom=6, lat=46.603354, lon=1.888334, map_source=SOURCE_SATELLITE)
            self.map_view.freeze_callback = self.basculer_freeze
            # AJOUT : Lier le suivi tactile global de la fenêtre comme sur l'onglet 4
            # AJOUT : Lier le suivi tactile global de la fenêtre comme sur l'onglet 4.
            # Les handlers peuvent cesser de recevoir les touchers après un cycle
            # pause/reprise d'Android (ex : retour de l'appareil photo) : on les
            # rebranche donc aussi depuis OutilsTracesApp.on_resume (méthode
            # _relier_touchers_fenetre).
            self._relier_touchers_fenetre()
            self.ids.map_container.add_widget(self.map_view)
            # La taille des curseurs de waypoints suit le zoom de la carte.
            self.map_view.bind(zoom=self._maj_taille_waypoints)
        else:
            self.ids.map_container.add_widget(Label(
                text=(
                    "Carte indisponible : le module kivy_garden.mapview\n"
                    "n'est pas installe.\n\nInstalle-le avec :\n"
                    "pip install kivy_garden.mapview"
                ),
                color=(0.6, 0.1, 0.1, 1),
                halign="center",
            ))

    def _maj_taille_waypoints(self, instance, zoom):
        for mw in self.marqueurs_waypoints:
            mw.maj_taille(zoom)

    def dezoomer_carte(self):
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        min_z = getattr(getattr(self.map_view, "map_source", None), "min_zoom", 0)
        if self.map_view.zoom > min_z:
            self.map_view.zoom -= 1
            self.map_view.center_on(self.map_view.lat, self.map_view.lon)
            # AJOUT : Force le rechargement immédiat des tuiles après un dézoom
            self.map_view.trigger_update(True)

    def zoomer_carte(self):
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        max_z = getattr(getattr(self.map_view, "map_source", None), "max_zoom", 19)
        if self.map_view.zoom < max_z:
            self.map_view.zoom += 1
            self.map_view.center_on(self.map_view.lat, self.map_view.lon)
            # AJOUT : Force le rechargement immédiat des tuiles après un zoom
            self.map_view.trigger_update(True)

    def changer_vue_carte(self, valeur):
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        self.map_view.map_source = SOURCES_FONDS_CARTES[valeur]
        # Indispensable pour éviter les zones grises ou non redessinées au zoom/dézoom
        self.map_view.trigger_update(True)

    def ouvrir_menu_fonds(self, bouton):
        """Ouvre le menu déroulant compact des fonds de carte sous le
        bouton carré "Layer" (satellite par défaut, vue courante
        marquée d'un point). Voir _construire_menu_fonds_carte."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        menu = _construire_menu_fonds_carte(self)
        menu.open(bouton)

    def ouvrir_selecteur_fichier(self):
        contenu = _construire_selecteur_fichier(self._fichier_choisi)
        if contenu is not None:
            self._popup = Popup(title="Choisir un fichier", content=contenu, size_hint=(0.95, 0.95))
            self._popup.open()

    def _fichier_choisi(self, chemin):
        if hasattr(self, '_popup'):
            self._popup.dismiss()
        if not chemin:
            return
        try:
            points = gps_logic.lire_fichier_pour_conversion(chemin)
            # Waypoints de la trace : mêmes « vrais » waypoints que dans
            # (même règle que les autres onglets : ni n° de points, ni waypoints
            # superposés au départ/à l'arrivée).
            waypoints_bruts = gps_logic.lire_waypoints_source(chemin, heure_locale=False)
        except Exception as e:
            self.info_fichier = f"Erreur de lecture : {e}"
            return

        if not points:
            self.info_fichier = "Aucun point GPS trouvé dans ce fichier."
            return

        try:
            waypoints = gps_logic.vrais_waypoints(
                waypoints_bruts,
                [(points[0]['lat'], points[0]['lon']), (points[-1]['lat'], points[-1]['lon'])],
            )
        except Exception:
            waypoints = []

        self.points_courants = points
        self.info_fichier = f"Trace à suivre : {os.path.basename(chemin)}."
        
        self.profil = gps_logic.calculer_profil(points)
        self.graphe.set_donnees(*self.profil)
        
        self._afficher_trace_sur_carte(points, waypoints=waypoints)

    def _afficher_trace_sur_carte(self, points, waypoints=None):
        """Affiche la polyligne de la trace, les marqueurs D/A et les waypoints."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return

        if self.trace_layer is not None:
            self.map_view.remove_layer(self.trace_layer)
            self.trace_layer = None
            
        for m in self.marqueurs_actifs:
            self.map_view.remove_marker(m)
        self.marqueurs_actifs = []

        for mw in self.marqueurs_waypoints:
            self.map_view.remove_marker(mw)
        self.marqueurs_waypoints = []

        if not points:
            return

        liste_coords = [(p['lat'], p['lon']) for p in points]
        # Le calque de la trace est posé APRÈS les marqueurs (D/A et
        # waypoints) : ajouté en dernier, il s'affiche par-dessus eux,
        # comme sur les onglets Carte (4) et Photos (6) — dans
        # kivy_garden.mapview, le dernier élément ajouté s'affiche
        # par-dessus les précédents.
        self.trace_layer = TraceLayer()
        self.trace_layer.set_points(liste_coords)

        # Gestion des points de départ et d'arrivée : mêmes triangles
        # que les onglets Carte/Nettoyage (vert départ, rouge arrivée,
        # orange unique si boucle fermée <= 20 m) - affichage uniquement,
        # aucune incidence sur la mécanique d'enregistrement du live.
        dist_dep_arr = gps_logic.calculer_distance_haversine(
            points[0]['lat'], points[0]['lon'], points[-1]['lat'], points[-1]['lon']
        )
        if dist_dep_arr <= 20.0:
            m_unique = MarqueurFlag(couleur=COULEUR_FLAG_FERMETURE, lat=points[0]['lat'], lon=points[0]['lon'])
            self.map_view.add_marker(m_unique)
            self.marqueurs_actifs.append(m_unique)
        else:
            m_depart = MarqueurFlag(couleur=COULEUR_FLAG_DEPART, lat=points[0]['lat'], lon=points[0]['lon'])
            m_arrivee = MarqueurFlag(couleur=COULEUR_FLAG_ARRIVEE, lat=points[-1]['lat'], lon=points[-1]['lon'])
            self.map_view.add_marker(m_depart)
            self.map_view.add_marker(m_arrivee)
            self.marqueurs_actifs.extend([m_depart, m_arrivee])

        # Waypoints : disques jaunes dessinés (MarqueurWaypoint),
        # comme dans les onglets Carte/Nettoyage ; leur taille suit le
        # zoom de la carte. « Point de passage 1/2 » traités à part
        # (triangles) juste après la boucle.
        for wpt in (waypoints or []):
            lat_w, lon_w = wpt.get('lat'), wpt.get('lon')
            if lat_w is None or lon_w is None:
                continue
            nom_w = (wpt.get('name') or '').strip()
            if nom_w in ("Point de passage 1", "Point de passage 2"):
                continue
            mw = MarqueurWaypoint(
                zoom=self.map_view.zoom, lat=lat_w, lon=lon_w,
                nom=wpt.get('name'), description=wpt.get('description'),
            )
            self.map_view.add_marker(mw)
            self.marqueurs_waypoints.append(mw)

        # Triangles départ/arrivée pour « Point de passage 1/2 »
        # (vert / rouge, orange unique si boucle fermée <= 20 m),
        # même règle que Carte/Nettoyage.
        _poser_triangles_points_passage(self, waypoints)

        # Ajout du calque de trace EN DERNIER (après tous les
        # marqueurs) pour qu'il s'affiche par-dessus les curseurs
        # des waypoints.
        self.map_view.add_layer(self.trace_layer)

        # REMONTÉE DU CALQUE DE MARQUEURS AU-DESSUS DE LA TRACE (même
        # mécanique que Carte/Nettoyage/temp) : sans elle, les disques
        # jaunes et les triangles passent SOUS la trace chargée.
        self._remonte_calque_marqueurs()

        lats = [c[0] for c in liste_coords]
        lons = [c[1] for c in liste_coords]
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)

        self.map_view.center_on((min_lat + max_lat) / 2, (min_lon + max_lon) / 2)
        max_delta = max(max_lat - min_lat, max_lon - min_lon)
        if max_delta > 0:
            zoom = int(12 - math.log2(max_delta * 10))
            self.map_view.zoom = max(2, min(zoom, 18))
        
    def _remonte_calque_marqueurs(self):
        """Remonte le calque des marqueurs AU-DESSUS du calque de trace
        (même mécanique que les onglets Carte/Nettoyage/temp) : retirer
        puis re-poser le calque de marqueurs via l'API PUBLIQUE de
        MapView (remove_layer/add_layer) le renvoie en fin de pile,
        au-dessus de tout. AFFICHAGE uniquement : cette méthode n'a
        AUCUNE incidence sur la mécanique d'enregistrement du live
        (points, fichier, service, arrêt). À appeler après TOUTE pose
        de marqueurs suivant un add_layer."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        couche = getattr(self.map_view, "_marker_layer", None)
        if couche is None:
            for l in getattr(self.map_view, "_layers", []) or []:
                if isinstance(l, MarkerMapLayer):
                    couche = l
                    break
        if couche is None:
            return
        try:
            self.map_view.remove_layer(couche)
            self.map_view.add_layer(couche)
        except Exception:
            pass

    def _trouver_dernier_gpx_gpslogger(self):
        """Trouve le fichier .gpx le plus récemment modifié dans les
        dossiers de sortie habituels de GPSLogger, sans présumer s'il
        est encore activement écrit ou non — cette question est
        tranchée séparément par on_click_live_pydroid, en surveillant
        s'il continue de grossir (voir _verifier_gpslogger_actif_suite).

        Renvoie le chemin trouvé, ou None si aucun fichier .gpx n'existe
        dans ces dossiers. Si GPSLogger a été configuré avec un dossier
        de sortie personnalisé (différent de ceux listés ci-dessous), ce
        fichier ne sera pas trouvé : vérifier le dossier réellement
        utilisé dans GPSLogger (Réglages → Général → Dossier de
        stockage / "Log file directory") et l'ajouter à la liste si
        besoin."""
        dossiers_candidats = [
            "/storage/emulated/0/GPX_Files/GPSLoggerTraces",
"""            "/storage/emulated/0/GPSLoggerTraces","""
        ]

        meilleur_chemin = None
        meilleure_date = None

        for dossier in dossiers_candidats:
            if not os.path.isdir(dossier):
                continue
            try:
                for nom in os.listdir(dossier):
                    if not nom.lower().endswith(".gpx"):
                        continue
                    chemin = os.path.join(dossier, nom)
                    try:
                        date_modif = os.path.getmtime(chemin)
                    except OSError:
                        continue
                    if meilleure_date is None or date_modif > meilleure_date:
                        meilleure_date = date_modif
                        meilleur_chemin = chemin
            except OSError:
                continue

        return meilleur_chemin

    def _compter_lignes(self, chemin):
        with open(chemin, "r", encoding="utf-8", errors="ignore") as f:
            return sum(1 for _ in f)

    def _reprendre_trace_gpslogger_active(self, chemin):
        """GPSLogger est déjà à l'état actif et enregistre déjà une
        trace (détecté par on_click_live_pydroid/_verifier_gpslogger_
        actif_suite : le fichier grossit toujours 20 secondes après une
        première lecture) : affiche directement tous ses points déjà
        enregistrés sur la carte et le graphique (rouge), puis poursuit
        l'affichage live à partir de là — le serveur d'écoute est
        démarré pour les points suivants, sans relancer GPSLogger (déjà
        actif)."""
        self.pause_traitement_live = False
        self.en_cours_live = True

        while not self.file_points_live.empty():
            try:
                self.file_points_live.get_nowait()
            except queue.Empty:
                break

        try:
            points = gps_logic.lire_gpx_tolerant(chemin)
        except Exception as e:
            print(f"[Live GPSLogger] Erreur de lecture de la trace déjà active ({chemin}) : {e}")
            points = []

        self.points_trace_live = points
        self.fichier_gpx_actif_live = chemin
        self._journaliser_evenement_live(
            f"reprise;gpx={os.path.basename(chemin)};points_lus={len(points)}")

        # Journal silencieux des sources, redémarré à partir de
        # maintenant : les points déjà présents dans le fichier n'ont
        # pas d'information de source disponible (elle ne nous parvient
        # que via le serveur d'écoute live) — seuls les nouveaux points
        # reçus en direct à partir d'ici seront comptés.
        self.compteur_sources_live = {}
        self._journal_points_live = []

        # NE PAS vider annotations_live ici : les photos/waypoints pris
        # pendant le live doivent survivre à la reprise. S'ils sont
        # perdus en mémoire (redémarrage à froid), ils sont restaurés
        # depuis le journal du fichier temporaire ci-dessous.
        # NE PAS réinitialiser le fichier temporaire ici : une simple
        # resynchronisation (réveil d'écran, ou redémarrage après un
        # plantage) doit au contraire le CONSERVER s'il est déjà suivi
        # en mémoire, ou le RETROUVER sur le disque si l'appli vient de
        # redémarrer à froid (voir _recuperer_fichier_temp_live_orphelin),
        # pour ne perdre aucune photo déjà associée à cette trace.
        self._recuperer_fichier_temp_live_orphelin()
        self._restaurer_annotations_depuis_journal()

        if points:
            self._afficher_trace_live_sur_carte()

            self.profil_live = gps_logic.calculer_profil(points)
            distances_km, distances_ele, altitudes, vitesses_kmh = self.profil_live
            self.graphe.set_donnees_secondaires(distances_km, distances_ele, altitudes)

            dernier = points[-1]
            idx = len(points) - 1
            self._maj_info_point_live(dernier, idx, distances_km, vitesses_kmh)

        # Démarre le serveur d'écoute live AVANT le message final
        # ci-dessous, pour la même raison que dans
        # _demarrer_nouveau_suivi_live : demarrer_serveur_live() affiche
        # son propre message transitoire, aussitôt remplacé par
        # celui-ci qui doit rester affiché.
        self.demarrer_serveur_live()
        self._maj_statut_live(
            f"Trace GPSLogger déjà en cours reprise : {os.path.basename(chemin)} ({len(points)} points).",
            (0.180, 0.490, 0.196, 1)  # #2E7D32
        )
        # Retour au statut live standard après 2,5 s (même mécanisme
        # que le retour après une photo) : le message de reprise est
        # informatif mais ne doit pas rester figé tant que GPSLogger
        # n'envoie pas de nouveau point (ex. fix GPS perdu en
        # intérieur) — le statut standard, lui, se met à jour à
        # chaque point reçu.
        Clock.schedule_once(
            lambda dt: self._maj_statut_live(self._texte_statut_live(), (0.180, 0.490, 0.196, 1)),
            2.5,
        )

    def _resynchroniser_avec_gpslogger(self):
        return  # GPS natif : la resynchronisation GPSLogger est inutile (les points arrivent directement)
        """Appelée automatiquement au retour au premier plan de l'appli
        (redémarrage après un plantage ou un clic involontaire sur
        "Quitter", ou simple réveil de l'écran) : si GPSLogger est en
        train d'enregistrer une trace dans son dossier de sortie,
        réinitialise la trace live affichée et la recharge intégralement
        depuis ce fichier, pour que le nombre de points affiché
        corresponde exactement à celui de GPSLogger ("Vue détaillée"
        -> "Parcouru").

        Si l'état de GPSLogger est CONNU "started" (broadcast EVENT ou
        fichier d'état persiste) : rechargement IMMÉDIAT du fichier —
        pas de délai. Sinon (état inconnu) : vérification par
        croissance de fichier (comptage, 20 s, re-comptage) avant de
        recharger, comme avant.

        Contrairement à on_click_live_pydroid, cette méthode ne démarre
        JAMAIS un nouveau suivi ni GPSLogger : si aucun fichier n'est
        activement écrit, elle ne fait rien et laisse l'écran tel quel
        (pas de faux positif au réveil de l'écran sans live en cours)."""
        if self._resync_gpslogger_en_cours:
            return
        if self.pause_traitement_live:
            # Une décision "Terminer" (Oui/Non/Annuler) est en cours :
            # ne pas interférer avec la trace pendant ce temps-là.
            return

        # PURGE IMMÉDIATE de la file des points directs : pendant la
        # suspension (écran noir / mise en veille), les envois de
        # GPSLogger vers le serveur local s'accumulent dans le socket ;
        # au réveil ils seraient déversés d'un coup dans la trace sous
        # forme de points ANCIENS déjà enregistrés — d'où les allers-
        # retours en "rayons de roue" observés lors des reprises, avant
        # que la resynchronisation (20 s plus bas) ne nettoie. On jette
        # ces points périmés TOUT DE SUIT : ils sont tous déjà dans le
        # fichier GPX de GPSLogger, que la resync relira de toute façon.
        while not self.file_points_live.empty():
            try:
                self.file_points_live.get_nowait()
            except queue.Empty:
                break

        chemin_candidat = self._trouver_dernier_gpx_gpslogger()
        if chemin_candidat is None:
            return

        # État CONNU "started" (broadcast EVENT / fichier d'état) :
        # GPSLogger enregistre, le fichier GPX est la source de vérité
        # — rechargement IMMÉDIAT, sans attendre la vérification de
        # croissance (qui n'existe que pour DEVINER l'état inconnu).
        # C'est ce qui rendait la reprise après mise en veille plus
        # lente qu'après un "Quitter" (20 s de comptage inutiles).
        if self._gpslogger_actif is True:
            self._journaliser_evenement_live(
                f"resync_immediate;gpx={os.path.basename(chemin_candidat)}")
            self._reprendre_trace_gpslogger_active(chemin_candidat)
            return

        try:
            nb_lignes_reference = self._compter_lignes(chemin_candidat)
        except OSError:
            return

        self._resync_gpslogger_en_cours = True
        Clock.schedule_once(
            lambda dt: self._resynchroniser_avec_gpslogger_suite(chemin_candidat, nb_lignes_reference),
            20,
        )

    def _resynchroniser_avec_gpslogger_suite(self, chemin, nb_lignes_reference):
        """Suite (20 secondes plus tard) de _resynchroniser_avec_gpslogger :
        si le fichier a grossi entre-temps, GPSLogger est bien en train
        d'enregistrer -> réinitialisation et rechargement intégral de la
        trace live. Sinon (fichier immobile), ne touche à rien."""
        self._resync_gpslogger_en_cours = False

        if self.pause_traitement_live:
            return

        try:
            nb_lignes_actuel = self._compter_lignes(chemin)
        except OSError:
            nb_lignes_actuel = nb_lignes_reference

        if nb_lignes_actuel != nb_lignes_reference:
            self._reprendre_trace_gpslogger_active(chemin)

    def on_click_live_pydroid(self):
        """Bouton "Live" (onglet 7) — GPS NATIF : démarre (ou confirme)
        l'enregistrement de la trace par le capteur GPS de l'appareil,
        SANS GPSLogger. Les points sont produits par le module gps_natif
        (LocationManager Android, listener pyjnius) et suivent le même
        pipeline qu'avant : file thread-safe -> _traiter_file_points_live
        -> _ajouter_point_live (carte, profil, statut), puis "Terminer"
        exporte en GPX comme d'habitude.

        Ne touche jamais à la trace "chargée" manuellement (bleue)
        ni à aucun autre onglet."""
        self._journaliser_evenement_live(
            f"clic_live;gps_natif={gps_natif.etat}")

        if self.en_cours_live:
            # Suivi déjà en cours (bouton "Live" recliqué, ou retour
            # d'un autre onglet) : simple confirmation, rien à relancer,
            # aucun point perdu.
            self._maj_statut_live(
                self._texte_statut_live(),
                (0.180, 0.490, 0.196, 1)  # #2E7D32
            )
            return

        self._demarrer_nouveau_suivi_live()

    def _demarrer_nouveau_suivi_live(self):
        """Séquence normale de démarrage du suivi en direct (bouton
        "Live") — appelée par on_click_live_pydroid quand GPSLogger
        n'est pas déjà détecté comme étant en train d'enregistrer une
        trace :
        Phase 1 : réinitialise le suivi EN DIRECT (rouge) de cet onglet.
        Phase 2 : démarre le GPS NATIF de l'appareil (module gps_natif,
        LocationManager Android) ; chaque fix est déposé directement
        dans la file self.file_points_live, consommée par
        _traiter_file_points_live -> _ajouter_point_live.

        Ne touche jamais à la trace "chargée" manuellement (bleue,
        gérée par ouvrir_selecteur_fichier/_fichier_choisi ci-dessus) ni
        à aucun autre onglet."""
        # --- Phase 1 : réinitialisation de la trace live (rouge) uniquement ---
        # a. Le drapeau de pause repasse à False.
        self.pause_traitement_live = False

        # b. Les listes internes de la trace live (points, marqueurs) sont vidées.
        self.points_trace_live = []
        self.fichier_gpx_actif_live = None
        self.profil_live = ([], [], [], [])
        self.graphe.effacer_donnees_secondaires()

        # --- AJOUT (silencieux) : compteur de points par source de
        # géolocalisation (gps/network/fused...), écrit dans un fichier
        # log au moment de l'arrêt (_arreter_gpslogger), sans aucun
        # message ni indicateur visible pendant le suivi.
        self.compteur_sources_live = {}
        self._journal_points_live = []

        self.annotations_live = []
        self._reinitialiser_temp_live()
        
        self.en_cours_live = True  # Le live est maintenant actif
        
        # --- AJOUT : Vider la file d'attente pour purger les points obsolètes ---
        while not self.file_points_live.empty():
            try:
                self.file_points_live.get_nowait()
            except queue.Empty:
                break

        # c. Le tracé rouge et ses marqueurs sur la carte de l'onglet 7 sont supprimés.
        if CARTE_DISPONIBLE and self.map_view is not None:
            if self.trace_layer_live is not None:
                self.map_view.remove_layer(self.trace_layer_live)
                self.trace_layer_live = None
            for m in self.marqueurs_actifs_live:
                self.map_view.remove_marker(m)
            self.marqueurs_actifs_live = []

        # d. Le texte de statut passe à l'orange.
        self._maj_statut_live("Démarrage du suivi en direct : activation du GPS de l'appareil...", (0.937, 0.424, 0.0, 1))  # #EF6C00

        # --- Démarrage du GPS NATIF (module gps_natif) ---
        # Les points sont déposés directement dans la file thread-safe
        # self.file_points_live, déjà consommée par
        # _traiter_file_points_live : le petit serveur HTTP local qui
        # recevait les points de GPSLogger n'est plus nécessaire.
        ok, message = gps_natif.demarrer(self.file_points_live)
        if ok:
            # --- Service de premier plan (foreground service) : il garde
            # l'enregistrement actif écran éteint / appli en arrière-plan
            # (tracker_service.py). L'appli absorbe ses points via
            # _absorber_points_service (fichier live_service_points.json).
            self._demarrer_service_tracker()
            self._maj_statut_live(
                self._texte_statut_live(),
                (0.180, 0.490, 0.196, 1)  # #2E7D32
            )
        else:
            # Permission en cours de demande (popup système), fix en
            # acquisition, localisation système désactivée... : le
            # contrôle différé re-vérifie toutes les 4 s et démarre le
            # suivi dès que possible, avec le message exact du module.
            self._maj_statut_live(
                str(message),
                (0.937, 0.424, 0.0, 1)  # #EF6C00
            )
            Clock.schedule_once(self._verifier_demarrage_gps_natif, 4)

    def _verifier_demarrage_gps_natif(self, dt):
        """Contrôle différé du démarrage du GPS natif : met à jour le
        statut live avec l'erreur EXACTE du module gps_natif, sans
        jamais relancer un suivi par erreur. Si la permission vient
        d'être accordée, redémarre le GPS natif puis reprogramme un
        contrôle tant qu'il n'est pas actif."""
        if not self.en_cours_live:
            return
        if gps_natif.est_actif():
            self._maj_statut_live(
                self._texte_statut_live(),
                (0.180, 0.490, 0.196, 1)  # #2E7D32
            )
            return
        if gps_natif.etat == "refuse":
            self.en_cours_live = False
            if not gps_natif.permission_accordee():
                self._maj_statut_live(
                    "Enregistrement impossible : permission de localisation refusée. Accordez la permission via le bouton Réglages affiché.",
                    (0.776, 0.157, 0.157, 1)  # #C62828
                )
                self._popup_permission_refusee()
            else:
                self._maj_statut_live(
                    f"Enregistrement impossible : {gps_natif.derniere_erreur}",
                    (0.776, 0.157, 0.157, 1)  # #C62828
                )
            return
        gps_natif.demarrer(self.file_points_live)
        if not gps_natif.est_actif() and gps_natif.derniere_erreur:
            self._maj_statut_live(
                f"En attente du GPS : {gps_natif.derniere_erreur}",
                (0.937, 0.424, 0.0, 1)  # #EF6C00
            )
        Clock.schedule_once(self._verifier_demarrage_gps_natif, 4)

    def _popup_permission_refusee(self):
        """Popup affichée quand la permission de localisation a été
        REFUSÉE : Android (11+, et MIUI/HyperOS dès le premier refus)
        ne montrera plus jamais la popup système (« Ne plus demander »
        implicite). Deux boutons : « Ouvrir les réglages » (page
        Permissions de l'appli) et « Réessayer » (relance le suivi ;
        le GPS démarre dès que la permission est accordée)."""
        contenu = BoxLayout(orientation="vertical", padding=dp(14), spacing=dp(12))
        lbl = Label(
            text=("La permission de localisation a été refusée.\n\n"
                  "Android ne redemandera plus automatiquement.\n"
                  "Accordez la permission Position dans les réglages,\n"
                  "puis revenez et appuyez sur Réessayer."),
            text_size=(dp(280), None), halign="left", valign="middle",
            size_hint_y=None,
        )
        lbl.bind(texture_size=lambda w, v: setattr(w, "height", v[1]))
        contenu.add_widget(lbl)

        boutons = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        btn_reglages = Button(text="Ouvrir les réglages", background_color=(0.2, 0.6, 0.86, 1))
        btn_reessayer = Button(text="Réessayer", background_color=(0.15, 0.68, 0.38, 1))
        boutons.add_widget(btn_reglages)
        boutons.add_widget(btn_reessayer)
        contenu.add_widget(boutons)

        popup = Popup(title="Permission de localisation", content=contenu,
                      size_hint=(0.9, 0.5))
        btn_reglages.bind(on_release=lambda *a: gps_natif.ouvrir_reglages())
        btn_reessayer.bind(on_release=lambda *a: (popup.dismiss(), self._demarrer_nouveau_suivi_live()))
        popup.open()


    def _texte_statut_live(self):
        """Texte du statut live : nombre de points, et nombre de
        waypoints (photos) des qu'il y en a au moins un.

        Si des points du service (écran éteint) viennent d'être
        absorbés (rattrapage), une mention distincte les comptabilise
        séparément : sans elle, impossible de différencier le
        rattrapage de l'affichage en direct des nouveaux points."""
        nb_points = len(self.points_trace_live)
        nb_waypoints = len(self.annotations_live)
        texte = f"Live en cours... ({nb_points} points"
        if nb_waypoints:
            texte += f", {nb_waypoints} waypoint{'s' if nb_waypoints > 1 else ''}"
        texte += ")"
        if self._rattrapage_actif():
            texte += f" — dont {self._rattrapage_points} point(s) rattrapé(s)"
        return texte

    def _rattrapage_actif(self):
        """Vrai si des points du service viennent d'être absorbés
        (rattrapage après écran éteint) il y a moins de 15 s : le statut
        les affiche alors séparément, en bleu au lieu du vert, pour
        distinguer clairement le rattrapage du flux live normal."""
        nb = getattr(self, "_rattrapage_points", 0)
        heure = getattr(self, "_rattrapage_heure", None)
        if nb <= 0 or heure is None:
            return False
        return (datetime.now() - heure).total_seconds() < 15.0

    def _maj_statut_live(self, texte, couleur=(0.33, 0.33, 0.33, 1)):
        """Affiche un message à la fois dans la console et dans le label
        de statut de cet onglet, pour rester visible même si la console
        n'est pas accessible (usage mobile). Equivalent de
        _maj_statut_live() dans la version desktop."""
        print(f"[Live GPSLogger] {texte}")
        self.statut_live_text = texte
        self.statut_live_color = list(couleur)

    def _maj_info_point_live(self, point, idx, distances_km, vitesses_kmh):
        """Remplit le bloc "Informations du point sélectionné" (grille
        3 lignes x 2 colonnes : Point/GPS, Distance/Altitude,
        Heure/Vitesse) à partir d'un point de la trace live."""
        dist = distances_km[idx] if idx < len(distances_km) else 0.0
        vit = vitesses_kmh[idx] if idx < len(vitesses_kmh) else 0.0
        heure = point['time'].strftime("%H:%M:%S") if point.get('time') else "-"
        ele_txt = f"{point['ele']} m" if point.get('ele') is not None else "-"

        self.info_point_text = ""
        self.info_point_num = f"Point {idx + 1} (live)"
        self.info_point_gps = f"GPS: {point['lat']:.5f}, {point['lon']:.5f}"
        self.info_point_dist = f"Distance: {dist:.2f} km"
        self.info_point_alt = f"Altitude: {ele_txt}"
        self.info_point_heure = f"Heure: {heure}"
        self.info_point_vit = f"Vitesse: {vit} km/h"

    def _effacer_info_point_live(self, message=""):
        """Vide le bloc "Informations du point sélectionné" (et affiche
        éventuellement un message ponctuel à la place, ex. "Aucun point
        live enregistré.")."""
        self.info_point_text = message
        self.info_point_num = ""
        self.info_point_gps = ""
        self.info_point_dist = ""
        self.info_point_alt = ""
        self.info_point_heure = ""
        self.info_point_vit = ""

    # ------------------------------------------------------------------
    # Etat d'enregistrement de GPSLogger (icone de notification)
    # ------------------------------------------------------------------
    def _chemin_etat_gpslogger(self):
        """Petit fichier persistant ou l'etat started/stopped de
        GPSLogger est memorise, pour survivre aux redemarrages de
        l'appli (le broadcast EVENT n'etant emis qu'aux changements
        d'etat, un demarrage de GPSLogger pendant que notre appli est
        morte ne serait pas vu sans lui)."""
        dossier = DOSSIER_SORTIE if os.path.exists(DOSSIER_SORTIE) else DOSSIER_RACINE
        try:
            os.makedirs(dossier, exist_ok=True)
        except Exception:
            pass
        return os.path.join(dossier, "etat_gpslogger.txt")

    def _charger_etat_gpslogger(self):
        """Relit l'etat persiste au demarrage de l'appli (None si
        absent = inconnu)."""
        try:
            with open(self._chemin_etat_gpslogger(), "r", encoding="utf-8") as f:
                contenu = f.read().strip()
            self._gpslogger_actif = contenu == "started"
        except Exception:
            self._gpslogger_actif = None

    def _enregistrer_etat_gpslogger(self, actif):
        """Memorise l'etat (en memoire ET dans le fichier persiste)."""
        self._gpslogger_actif = actif
        try:
            with open(self._chemin_etat_gpslogger(), "w", encoding="utf-8") as f:
                f.write("started" if actif else "stopped")
        except Exception:
            pass

    def _enregistrer_receiver_etat_gpslogger(self):
        """Enregistre (une seule fois) le recepteur Android du broadcast
        com.mendhak.gpslogger.EVENT : GPSLogger l'emet a chaque
        demarrage (extra started=true) et arret (extra stopped=true)
        d'enregistrement. C'est la detection "icone presente/absente"
        demandee : immediate, sans permission speciale, sans lecture
        des notifications. Sur PC ou si pyjnius echoue (broadcast
        bloque par le systeme, etc.) : silencieusement sans effet —
        l'etat reste alors celui du fichier persiste / inconnu, et le
        clic "Live" bascule sur la verification de croissance de
        fichier (repli, voir on_click_live_pydroid)."""
        if self._receiver_etat_gpslogger is not None:
            return
        if platform != "android":
            return
        try:
            from jnius import autoclass, PythonJavaClass, java_method

            contexte = None
            for chemin_classe in ("org.kivy.android.PythonActivity", "org.kivy.android.PythonService"):
                try:
                    contexte = autoclass(chemin_classe).mActivity
                    if contexte:
                        break
                except Exception:
                    continue
            if contexte is None:
                return

            action_event = self.ACTION_EVENT_GPSLOGGER
            sur_event = self._sur_event_gpslogger

            # Recepteur Android implemente en Python (pyjnius) : a
            # chaque broadcast EVENT de GPSLogger, lit l'extra
            # started/stopped et met a jour l'etat. La closure
            # capture les references necessaires : onReceive n'a pas
            # acces au LiveScreen via self (self = le recepteur).
            class RecepteurEtat(PythonJavaClass):
                __javainterfaces__ = ["org/broadcast/RecepteurEtat"]
                __javacontext__ = "app"

                @java_method("(Landroid/content/Context;Landroid/content/Intent;)V")
                def onReceive(self, contexte_android, intent):
                    try:
                        if intent.getAction() != action_event:
                            return
                        if intent.getBooleanExtra("started", False):
                            sur_event(True)
                        elif intent.getBooleanExtra("stopped", False):
                            sur_event(False)
                    except Exception:
                        pass

            recepteur = RecepteurEtat()
            IntentFilter = autoclass("android.content.IntentFilter")
            filtre = IntentFilter(action_event)

            # Android 13+ exige RECEIVER_EXPORTED pour un broadcast
            # emis par une AUTRE appli ; si l'appel echoue (Android
            # plus ancien), on retombe sur l'enregistrement simple.
            Context = autoclass("android.content.Context")
            try:
                contexte.registerReceiver(recepteur, filtre, Context.RECEIVER_EXPORTED)
            except Exception:
                contexte.registerReceiver(recepteur, filtre)

            self._receiver_etat_gpslogger = recepteur
        except Exception as e:
            # Pyjnius indisponible, module android absent, ou
            # enregistrement refuse : repli silencieux (verification
            # de croissance de fichier lors du clic "Live").
            print(f"[Live GPSLogger] Recepteur d'etat non installe ({e}) : repli sur la verification de fichier.")

    def _sur_event_gpslogger(self, actif):
        """Callback du broadcast EVENT : memorise le nouvel etat.
        Appelle depuis le thread Android du recepteur — tout est
        simple (ecriture fichier + booleen), thread-safe ici."""
        self._enregistrer_etat_gpslogger(actif)

    def demarrer_serveur_live(self):
        """Démarre (une seule fois) le petit serveur HTTP local qui
        reçoit, en temps réel, chaque nouveau point envoyé par GPSLogger
        via son URL personnalisée :
            http://127.0.0.1:8765/gps?lat=%LAT&lon=%LON&alt=%ALT&acc=%ACC&prov=%PROV

        Le paramètre "prov" (variable %PROV de GPSLogger) correspond à
        la source de géolocalisation affichée entre parenthèses dans
        "Affichage du journal > Localisation uniquement" de GPSLogger
        (gps, network, fused...) — utilisé pour le comptage silencieux
        de points par source (voir _ajouter_point_live/_arreter_gpslogger).

        Le serveur tourne dans un thread séparé ; les points reçus sont
        déposés dans une file thread-safe (self.file_points_live),
        consommée côté thread principal par _traiter_file_points_live()
        (Kivy n'est pas thread-safe)."""
        if self.serveur_live is not None:
            return

        file_points = self.file_points_live

        class GestionnaireLive(BaseHTTPRequestHandler):
            def do_GET(self):
                try:
                    url_analysee = urllib.parse.urlparse(self.path)
                    if url_analysee.path != "/gps":
                        self.send_response(404)
                        self.end_headers()
                        return

                    params = urllib.parse.parse_qs(url_analysee.query)
                    lat_brut = params.get("lat", [None])[0]
                    lon_brut = params.get("lon", [None])[0]
                    if lat_brut is None or lon_brut is None:
                        self.send_response(400)
                        self.end_headers()
                        return

                    lat = float(lat_brut)
                    lon = float(lon_brut)
                    alt_brut = params.get("alt", [None])[0]
                    ele = None
                    if alt_brut not in (None, ""):
                        try:
                            ele = round(float(alt_brut), 1)
                        except ValueError:
                            ele = None

                    source_brut = params.get("prov", [None])[0]
                    source = source_brut if source_brut not in (None, "") else "inconnue"

                    file_points.put({
                        'lat': lat, 'lon': lon, 'ele': ele,
                        'time': datetime.now(), 'name': None,
                        'source': source,
                    })

                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"OK")
                except Exception:
                    try:
                        self.send_response(400)
                        self.end_headers()
                    except Exception:
                        pass

            def log_message(self, format, *args):
                pass  # Silence le log console par défaut de http.server

        try:
            self.serveur_live = HTTPServer(("127.0.0.1", self.PORT_SERVEUR_LIVE), GestionnaireLive)
        except OSError as e:
            print(f"[Live GPSLogger] Impossible de démarrer le serveur local sur le port {self.PORT_SERVEUR_LIVE} : {e}")
            self.serveur_live = None
            return

        self.thread_serveur_live = threading.Thread(target=self.serveur_live.serve_forever, daemon=True)
        self.thread_serveur_live.start()
        self._maj_statut_live(f"Serveur d'écoute live démarré sur 127.0.0.1:{self.PORT_SERVEUR_LIVE}.", (0.180, 0.490, 0.196, 1))

    def _lancer_gpslogger_et_demarrer_enregistrement(self):
        """Tente, par les moyens disponibles sous Android, de :
           a) porter l'application GPSLogger au premier plan (la lancer
              si elle n'est pas déjà ouverte) ;
           b) lui envoyer l'ordre de démarrer immédiatement
              l'enregistrement (extra Android "immediatestart", reconnu
              nativement par GPSLogger pour l'automatisation externe,
              ex. Tasker/Automate).

        Renvoie (True, détail) en cas de succès, (False, raison) sinon.
        Chaque mécanisme est essayé indépendamment et n'importe quel
        échec est intercepté : cette méthode ne lève jamais d'exception
        et ne bloque jamais l'affichage live, qui fonctionne dès que
        GPSLogger envoie effectivement des points, quelle que soit la
        façon dont il a été démarré (automatique ici, ou manuel par
        l'utilisateur)."""

        # --- Tentative 1 : pyjnius (accès natif à l'API Android) ---
        try:
            from jnius import autoclass, cast

            activite_courante = None
            for chemin_classe in ("org.kivy.android.PythonActivity", "org.kivy.android.PythonService"):
                try:
                    activite_courante = autoclass(chemin_classe).mActivity
                    if activite_courante:
                        break
                except Exception:
                    continue

            if activite_courante is None:
                raise RuntimeError("activité Android introuvable via pyjnius")

            Intent = autoclass("android.content.Intent")
            contexte = cast("android.content.Context", activite_courante)

            # a) Porter GPSLogger au premier plan (son activité principale).
            gestionnaire_paquets = contexte.getPackageManager()
            intent_lancement = gestionnaire_paquets.getLaunchIntentForPackage(self.PACKAGE_GPSLOGGER)
            if intent_lancement is not None:
                contexte.startActivity(intent_lancement)

            # b) Ordonner à GPSLogger de démarrer l'enregistrement.
            intent_demarrage = Intent(self.ACTION_TASKER_GPSLOGGER)
            intent_demarrage.setClassName(self.PACKAGE_GPSLOGGER, self.RECEIVER_TASKER_GPSLOGGER)
            intent_demarrage.putExtra("immediatestart", True)
            contexte.sendBroadcast(intent_demarrage)

            return True, "(méthode : pyjnius)"
        except Exception as e_jnius:
            # Détail technique complet réservé à la console (utile en
            # debug), jamais affiché tel quel à l'écran.
            print(f"[Live GPSLogger] Échec pyjnius (lancement) : {e_jnius}")
            raison_jnius = "méthode pyjnius indisponible"

        # --- Tentative 2 (secours) : commande Android "am", si disponible ---
        try:
            subprocess.run(
                ["am", "start", "-n", f"{self.PACKAGE_GPSLOGGER}/.GpsMainActivity"],
                check=False, timeout=5,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            resultat = subprocess.run(
                [
                    "am", "broadcast",
                    "-a", self.ACTION_TASKER_GPSLOGGER,
                    "-n", f"{self.PACKAGE_GPSLOGGER}/{self.RECEIVER_TASKER_GPSLOGGER}",
                    "--ez", "immediatestart", "true"
                ],
                check=False, timeout=5,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            if resultat.returncode == 0:
                return True, "(méthode : commande am)"
            print(f"[Live GPSLogger] Échec commande am (lancement), code {resultat.returncode} : "
                  f"{resultat.stderr.decode(errors='ignore').strip()}")
            raison_am = "commande am indisponible ou refusée"
        except Exception as e_am:
            print(f"[Live GPSLogger] Échec commande am (lancement) : {e_am}")
            raison_am = "commande am indisponible ou refusée"

        return False, f"{raison_jnius} ; {raison_am}"

    def _traiter_file_points_live(self, dt):
        """Boucle planifiée (Clock.schedule_interval, toutes les
        secondes) : vide la file des points reçus en direct par le
        serveur local et les applique un par un sur la carte et le
        profil altimétrique de cet onglet. Equivalent de
        traiter_file_points_live() dans la version desktop — ici,
        Clock se replanifie lui-même : pas besoin de le refaire à la
        main comme avec after() sous Tkinter.

        Un point individuel qui provoquerait une erreur est ignoré sans
        interrompre le traitement des points suivants ni la
        planification de cette boucle.

        Si self.pause_traitement_live est actif, la file n'est PAS
        vidée ici, pour que les points reçus entre-temps ne soient
        jamais perdus."""
        if self.pause_traitement_live:
            return

        nouveaux_points = []
        try:
            while True:
                nouveaux_points.append(self.file_points_live.get_nowait())
        except queue.Empty:
            pass

        for point in nouveaux_points:
            try:
                self._ajouter_point_live(point)
            except Exception as e:
                print(f"[Live GPSLogger] Erreur lors de l'ajout d'un point live (point ignoré) : {e}")

        # --- Diagnostic GPS natif : tant que le suivi est actif mais
        # qu'aucun point n'est arrivé, affiche chaque seconde l'état
        # réel du module (fixes reçus, dernier fix, fournisseurs).
        if (self.en_cours_live and not self.pause_traitement_live
                and not self.points_trace_live and gps_natif.est_actif()):
            nb_recus = gps_natif.fixes_recus()
            if nb_recus == 0:
                texte = ("Live en cours... (0 point) - GPS actif, en attente du "
                         "premier fix (peut prendre 1 a 2 min a l'exterieur). "
                         "Fournisseurs : " + (", ".join(gps_natif.fournisseurs()) or "aucun") + ".")
            else:
                texte = ("Live en cours... (0 point) - " + str(nb_recus)
                         + " fix(es) recus du systeme (dernier a "
                         + gps_natif.heure_dernier_fix() + "), points en attente de traitement.")
            self._maj_statut_live(texte, (0.937, 0.424, 0.0, 1))  # #EF6C00

        # Absorption des points du service de premier plan (écrits pendant
        # un écran éteint ou un arrière-plan prolongé) : le service tourne
        # dans son propre processus et dépose ses points dans un fichier.
        # UNIQUEMENT pendant un live actif : après « Terminer » (Oui ou
        # Non), tout doit s'arrêter — si le service tarde à mourir ou
        # redémarre (MIUI), ses derniers points ne doivent PAS revenir
        # s'afficher ni être « à rattraper » : « Terminer » est le seul
        # chemin qui purge et clôt la session.
        if self.en_cours_live:
            try:
                self._absorber_points_service()
            except Exception:
                pass

            # BATTEMENT DE STATUT : pendant un live actif avec des
            # points, le compteur se rafraîchit chaque seconde. Sans
            # cela, le statut ne bougeait qu'à l'arrivée d'un NOUVEAU
            # point (mode 5 m : rien si on ne bouge pas), et restait
            # « figé » après « Terminer » → « Annuler ».
            if (not self.pause_traitement_live and self.points_trace_live
                    and (self.statut_live_text or "").startswith("Live en cours")):
                if self._rattrapage_actif():
                    couleur_battement = (0.086, 0.396, 0.753, 1)  # bleu
                else:
                    couleur_battement = (0.180, 0.490, 0.196, 1)  # vert
                self._maj_statut_live(self._texte_statut_live(), couleur_battement)

    # -------------------------------------------------------------
    # Service de premier plan (foreground service "Tracker") : garde
    # l'enregistrement GPS actif écran éteint / appli fermée
    # (tracker_service.py, déclaré dans buildozer.spec via
    # services = Tracker:tracker_service.py:foreground). Il écrit les
    # points dans live_service_points.json (une ligne JSON par point),
    # que l'appli absorbe ici par simple lecture incrémentale.
    # -------------------------------------------------------------
    CHEMIN_POINTS_SERVICE = os.path.join(
        DOSSIER_SORTIE, "live_service_points.json")
    # Marqueur de session live : contient le numéro de la ligne du
    # fichier de points où a commencé la session. Écrit au clic "Live",
    # supprimé à "Terminer". S'il est encore présent au démarrage
    # suivant, c'est que l'appli a été quittée/tuée SANS Terminer :
    # la session continue — ses points (y compris ceux enregistrés par
    # le service pendant l'arrêt de l'appli) sont réabsorbés.
    CHEMIN_MARQUEUR_SESSION = os.path.join(
        DOSSIER_SORTIE, "live_session_en_cours.json")

    def _demarrer_service_tracker(self):
        """Lance le service de premier plan (Android uniquement). Ne
        lève jamais ; en cas d'échec, le live à l'écran continue de
        fonctionner (gps_natif), seule la capture écran éteint manque.

        Gestion des sessions : si un marqueur de session survit (appli
        quittée/tuée sans "Terminer"), on REPART DE LUI — tous les
        points de la session interrompue seront réabsorbés. Sinon,
        nouvelle session : le marqueur est créé au point courant du
        fichier (l'historique des sessions précédentes est ignoré)."""
        if platform != "android":
            return
        try:
            # Marqueur survivant = session interrompue : la reprendre.
            offset_session = None
            try:
                with open(self.CHEMIN_MARQUEUR_SESSION, "r", encoding="utf-8") as f:
                    donnees = json.load(f)
                offset_session = int(donnees.get("offset", 0))
            except Exception:
                offset_session = None

            try:
                with open(self.CHEMIN_POINTS_SERVICE, "r", encoding="utf-8") as f:
                    nb_lignes = sum(1 for _ in f)
            except OSError:
                nb_lignes = 0

            if offset_session is not None:
                # Session interrompue (quitter/kill) : on reprend ses
                # points depuis le début de la session — y compris ceux
                # écrits par le service pendant l'absence de l'appli.
                self._lignes_service_lues = min(offset_session, nb_lignes)
                print(f"[Live service] Session interrompue reprise : "
                      f"points depuis la ligne {self._lignes_service_lues}.")

                # --- AVERTISSEMENT DE TROU : si le service a été tué
                # pendant l'absence (force-stop, kill MIUI, swipe des
                # tâches), les points de cette période n'existent pas.
                # On le détecte en comparant l'horodatage du DERNIER
                # point du fichier avec l'instant présent : un dernier
                # point récent = le service vivait (rien à signaler) ;
                # un dernier point ancien = trou dans la trace.
                try:
                    heure_dernier_point = None
                    with open(self.CHEMIN_POINTS_SERVICE, "r", encoding="utf-8") as f:
                        for ligne in f:
                            try:
                                d = json.loads(ligne)
                            except ValueError:
                                continue
                            t = d.get("time")
                            if t:
                                heure_dernier_point = t
                    if heure_dernier_point is not None:
                        dernier = datetime.fromisoformat(heure_dernier_point)
                        trou_minutes = (datetime.now() - dernier).total_seconds() / 60.0
                        if trou_minutes > 2.0:
                            message_trou = (
                                f"Reprise de session : trou d'environ "
                                f"{int(round(trou_minutes))} min dans la trace "
                                f"(appli/service arrêté(s) entre-temps - points non enregistrés)."
                            )
                            print(f"[Live service] {message_trou}")
                            self._journaliser_evenement_live(message_trou)
                            # Affiché 8 s après le lancement (le temps que
                            # le statut standard s'installe), en orange
                            # d'avertissement, puis remplacé par le statut
                            # normal au prochain point/battement.
                            Clock.schedule_once(
                                lambda dt: self._maj_statut_live(
                                    message_trou, (0.937, 0.424, 0.0, 1)),  # #EF6C00
                                8.0,
                            )
                except Exception:
                    pass
            else:
                # Nouvelle session : ignorer l'historique, noter le point.
                self._lignes_service_lues = nb_lignes
                try:
                    with open(self.CHEMIN_MARQUEUR_SESSION, "w", encoding="utf-8") as f:
                        json.dump({"offset": nb_lignes,
                                   "debut": datetime.now().isoformat()}, f)
                except OSError:
                    pass

            from jnius import autoclass
            service = autoclass("org.perso.outilstraces.ServiceTracker")
            activite = autoclass("org.kivy.android.PythonActivity").mActivity
            service.start(activite, "")
            # Redémarrage automatique si Android/MIUI tue le service
            # (doit continuer à enregistrer après un "Quitter").
            try:
                autoclass("org.kivy.android.PythonService").mService.setAutoRestartService(True)
            except Exception:
                pass
            print("[Live service] Service tracker lancé (premier plan).")
        except Exception as e:
            print(f"[Live service] Impossible de lancer le service tracker : {e}")

    def _arreter_service_tracker(self):
        """Arrête le service de premier plan ET CLÔT la session (le
        marqueur de session est supprimé : un futur "Live" repartira
        d'une trace vierge). Ne lève jamais."""
        if platform != "android":
            # Clôturer aussi la session hors Android (tests PC).
            self._clore_marqueur_session()
            return
        self._clore_marqueur_session()
        try:
            from jnius import autoclass
            activite = autoclass("org.kivy.android.PythonActivity").mActivity
            # 1) Désactiver l'auto-redémarrage AVANT l'arrêt : sinon le
            #    service se relance tout seul après sa destruction.
            try:
                autoclass("org.kivy.android.PythonService").mService.setAutoRestartService(False)
            except Exception:
                pass
            # 2) stop() exige l'activité (Context) en argument dans p4a :
            #    sans elle, l'appel échouait silencieusement (exception
            #    attrapée) et le service continuait — le point vert de
            #    localisation restait allumé après « Terminer ».
            service = autoclass("org.perso.outilstraces.ServiceTracker")
            try:
                service.stop(activite)
            except Exception:
                service.stop()
            print("[Live service] Service tracker arrêté.")
        except Exception as e:
            print(f"[Live service] Erreur à l'arrêt du service tracker : {e}")

    def _clore_marqueur_session(self):
        """Supprime le marqueur de session (fin de session : "Terminer")."""
        try:
            if os.path.exists(self.CHEMIN_MARQUEUR_SESSION):
                os.remove(self.CHEMIN_MARQUEUR_SESSION)
        except OSError:
            pass

    def _absorber_points_service(self):
        """Lit les NOUVELLES lignes de live_service_points.json (écrites
        par le service pendant un écran éteint / un arrière-plan) et les
        dépose dans la file live : le pipeline habituel les affiche et
        les dédoublonne (_ajouter_point_live rejette une position déjà
        présente à 1e-6 près). Les lignes non-ponctuelles (début_session,
        erreur) sont ignorées.

        Compte les points absorbés (self._rattrapage_points) : le statut
        live les affiche alors en BLEU et séparément (« dont N point(s)
        rattrapé(s) pendant l'écran éteint ») pour distinguer le
        rattrapage du flux live normal."""
        chemin = self.CHEMIN_POINTS_SERVICE
        if not os.path.exists(chemin):
            return
        if not hasattr(self, "_lignes_service_lues"):
            self._lignes_service_lues = 0
        try:
            with open(chemin, "r", encoding="utf-8") as f:
                lignes = f.readlines()
        except OSError:
            return
        nb_absorbes = 0
        for ligne in lignes[self._lignes_service_lues:]:
            self._lignes_service_lues += 1
            ligne = ligne.strip()
            if not ligne:
                continue
            try:
                donnees = json.loads(ligne)
            except ValueError:
                continue
            if "lat" not in donnees or "lon" not in donnees:
                continue  # ligne début_session / erreur : ignorée
            try:
                heure = datetime.fromisoformat(donnees["time"])
            except Exception:
                heure = datetime.now()
            self.file_points_live.put({
                "lat": float(donnees["lat"]),
                "lon": float(donnees["lon"]),
                "ele": donnees.get("ele"),
                "time": heure,
                "name": None,
                "source": donnees.get("source", "service"),
            })
            # RATTRAPAGE = seulement les points ANCIENS (> 20 s) : ceux
            # enregistrés pendant une absence (écran éteint, arrière-
            # plan, pause). Les points récents (< 20 s) sont des doublons
            # normaux du service pendant que l'appli est ouverte : ils
            # sont déposés (le dédoublonnage en aval les ignore) mais ne
            # comptent PAS comme rattrapage — sinon le message « dont N
            # rattrapés » s'afficherait même l'écran allumé.
            if (datetime.now() - heure).total_seconds() > 20.0:
                nb_absorbes += 1

        # Compteur de rattrapage : cumule les points absorbés de la
        # salve en cours ; une NOUVELLE salve (plus de 15 s après la
        # précédente, ex. retour d'un nouvel écran éteint) repart de
        # zéro pour n'afficher que les points du dernier rattrapage.
        if nb_absorbes > 0:
            ancienne_heure = getattr(self, "_rattrapage_heure", None)
            if (ancienne_heure is None
                    or (datetime.now() - ancienne_heure).total_seconds() >= 15.0):
                self._rattrapage_points = nb_absorbes
            else:
                self._rattrapage_points = getattr(self, "_rattrapage_points", 0) + nb_absorbes
            self._rattrapage_heure = datetime.now()
            self._journaliser_evenement_live(
                f"rattrapage : {nb_absorbes} point(s) absorbé(s) du service "
                f"(salve : {self._rattrapage_points})")

    def _journaliser_evenement_live(self, texte):
        """Ajoute une ligne d'EVENEMENT au journal de post-mortem des
        points live (debug_points_*.txt, voir _arreter_gpslogger) :
        clic "Live" avec l'état détecté, chemin de reprise emprunté,
        nombre de points lus au rechargement, etc. Permet de
        reconstituer une reprise problématique (ex. compteur reparti
        de zéro alors que le fichier GPX contenait déjà des points)."""
        try:
            self._journal_points_live.append(
                ("EVENT", datetime.now().strftime("%H:%M:%S.%f")[:-3], texte))
        except Exception:
            pass

    def _ajouter_point_live(self, point):
        """Ajoute un nouveau point reçu en direct à la trace de cet
        onglet : étend le tracé sur la carte (rouge) et sa courbe
        d'altitude sur le graphique (rouge, superposée à celle de la
        trace chargée en bleu — voir set_donnees_secondaires), et met à
        jour le bloc d'informations avec ce dernier point."""
        # Journal de post-mortem (silencieux) : chaque point recu en
        # direct, accepte OU rejete, avec son horodatage de RECEPTION.
        # Ecrit dans debug_points_*.txt a l'arret du live (voir
        # _arreter_gpslogger) : permet de reconstituer exactement ce
        # qui s'est passe lors d'une reprise problematique (ordre
        # d'arrivee des points apres un reveil d'ecran, etc.).
        try:
            self._journal_points_live.append(
                (datetime.now().strftime("%H:%M:%S.%f")[:-3],
                 point['lat'], point['lon'], point.get('ele'),
                 point.get('source', 'inconnue')))
        except Exception:
            pass

        if self.points_trace_live:
            dernier = self.points_trace_live[-1]
            if abs(dernier['lat'] - point['lat']) < 1e-6 and abs(dernier['lon'] - point['lon']) < 1e-6:
                return  # Point identique au dernier déjà affiché (doublon) : ignoré.

        # Garde anti-"rayons de roue" : lors d'une reprise (écran noir,
        # mise en veille, redémarrage de l'appli), GPSLogger RE-ENVOIE
        # vers le serveur local des points déjà enregistrés (salve
        # des requêtes mises en file pendant la suspension) : ce sont
        # les MÊMES fixes GPS, donc des coordonnées identiques au
        # 6e décimal (~0,1 m) à des points DÉJÀ dans la trace (chargée
        # depuis le fichier GPX). Le filtre ci-dessus ne compare qu'au
        # DERNIER point ; ici on rejette donc tout point identique
        # (à 1e-6 pres, comme ci-dessus) à un point QUELCONQUE de la
        # trace. Un VRAI nouveau point, meme quasi immobile (bruit GPS
        # de plusieurs metres), ne coincide jamais a 0,1 m pres avec
        # un point existant : ce filtre ne le rejette donc jamais.
        for p in self.points_trace_live:
            if abs(p['lat'] - point['lat']) < 1e-6 and abs(p['lon'] - point['lon']) < 1e-6:
                return  # Point déjà présent dans la trace (re-envoi post-reprise) : ignoré.

        # --- GARDE CHRONOLOGIQUE : une trace est strictement croissante
        # dans le temps. Pendant une mise en veille, DEUX sources
        # enregistrent la même période : le thread de sondage gps_natif
        # (file mémoire, alive même appli en veille) ET le service de
        # premier plan (fichier live_service_points.json). Au réveil, la
        # file se vide d'abord (chronologique), PUIS l'absorption du
        # fichier rejoue les mêmes fixes : leurs coordonnées diffèrent
        # de plus de 1e-6 des points de la file (déduplication à des
        # seuils différents) et créaient un aller-retour visuel
        # « arrivée -> milieu de trace -> arrivée ». On rejette donc
        # tout point daté d'AVANT le dernier point accepté (tolérance
        # 5 s pour les fixes quasi simultanés des deux sources).
        heure_point = point.get('time')
        if self.points_trace_live and isinstance(heure_point, datetime):
            try:
                heure_dernier = self.points_trace_live[-1].get('time')
                if isinstance(heure_dernier, datetime):
                    if (heure_dernier - heure_point).total_seconds() > 5.0:
                        return  # Point plus ancien que la fin de trace : rejeu d'une source redondante.
            except Exception:
                pass

        self.points_trace_live.append(point)

        # Comptage silencieux par source de géolocalisation (gps/network/
        # fused...), aucun affichage — voir demarrer_serveur_live et
        # _arreter_gpslogger pour l'écriture du log correspondant.
        source_point = point.get('source', 'inconnue')
        self.compteur_sources_live[source_point] = self.compteur_sources_live.get(source_point, 0) + 1

        self._afficher_trace_live_sur_carte()

        # self.profil_live est tenu à part de self.profil (trace chargée,
        # bleue) : ne l'écrase jamais, la courbe et le graphique de la
        # trace chargée restent affichés pendant tout le suivi live.
        self.profil_live = gps_logic.calculer_profil(self.points_trace_live)
        distances_km, distances_ele, altitudes, vitesses_kmh = self.profil_live
        self.graphe.set_donnees_secondaires(distances_km, distances_ele, altitudes)

        # Couleur du statut : BLEU pendant un rattrapage de points du
        # service (écran éteint) — pour distinguer visuellement le
        # rattrapage du vert du flux live normal.
        if self._rattrapage_actif():
            couleur_statut = (0.086, 0.396, 0.753, 1)  # #1665C0 (bleu)
        else:
            couleur_statut = (0.180, 0.490, 0.196, 1)  # #2E7D32 (vert)

        self._maj_statut_live(
            self._texte_statut_live(),
            couleur_statut
        )

        idx = len(self.points_trace_live) - 1
        self._maj_info_point_live(point, idx, distances_km, vitesses_kmh)


    def _afficher_trace_live_sur_carte(self):
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        points = self.points_trace_live
        if not points:
            return

        # Supprimer l'ancien calque live s'il existe
        if self.trace_layer_live is not None:
            self.map_view.remove_layer(self.trace_layer_live)
            self.trace_layer_live = None
        for m in self.marqueurs_actifs_live:
            self.map_view.remove_marker(m)
        self.marqueurs_actifs_live = []

        liste_coords = [(p['lat'], p['lon']) for p in points]
        
        # Utilisation de TraceLayer avec la couleur rouge pour le Live (Référence identique à l'onglet 4)
        self.trace_layer_live = TraceLayer(couleur=(0.8, 0.1, 0.1, 1))
        self.map_view.add_layer(self.trace_layer_live)
        self.trace_layer_live.set_points(liste_coords)

        # Marqueur de position actuelle / départ
        if len(points) > 0:
            m_depart = MarqueurTexte(texte="D", lat=points[0]['lat'], lon=points[0]['lon'])
            self.map_view.add_marker(m_depart)
            self.marqueurs_actifs_live.append(m_depart)
            
        if len(points) > 1:
            m_actuel = MarqueurTexte(texte="A", lat=points[-1]['lat'], lon=points[-1]['lon'])
            self.map_view.add_marker(m_actuel)
            self.marqueurs_actifs_live.append(m_actuel)

        # Centrage fluide sur le dernier point enregistré
        dernier = points[-1]
        self.map_view.center_on(dernier['lat'], dernier['lon'])
        
    def _fusionner_avec_gpslogger_avant_finalisation(self):
        """Appelée juste avant de proposer d'enregistrer (bouton
        "Terminer") : relit une dernière fois le fichier de GPSLogger et
        ne l'adopte que s'il est PLUS complet que ce qui est déjà
        affiché (plus de points). Contrairement à
        _resynchroniser_avec_gpslogger (qui ne fait que rattraper un
        réveil d'écran ou un redémarrage), l'objectif ici est d'éviter
        que le fichier final reflète un instant figé pendant
        l'enregistrement : GPSLogger reste la référence, mais les points
        déjà reçus en direct par le serveur d'écoute local (potentiellement
        plus récents que ce que GPSLogger a déjà écrit sur le disque,
        qui n'écrit que par intervalles) ne sont jamais perdus non plus,
        puisqu'on ne bascule sur le fichier que s'il apporte strictement
        plus de points que ce qui est déjà en mémoire.

        Limite connue : la comparaison se fait sur le NOMBRE de points,
        pas sur leur contenu point par point ; un cas très improbable où
        le fichier et la mémoire auraient chacun des points que l'autre
        n'a pas, en nombre équivalent, ne serait pas fusionné parfaitement."""
        chemin = self.fichier_gpx_actif_live or self._trouver_dernier_gpx_gpslogger()
        if not chemin:
            return

        try:
            points_fichier = gps_logic.lire_gpx_tolerant(chemin)
        except Exception as e:
            print(f"[Live] Relecture finale de GPSLogger avant enregistrement impossible : {e}")
            return

        if len(points_fichier) <= len(self.points_trace_live):
            return  # ce qui est déjà affiché est au moins aussi complet

        self.points_trace_live = points_fichier
        self.fichier_gpx_actif_live = chemin

        self._afficher_trace_live_sur_carte()
        self.profil_live = gps_logic.calculer_profil(points_fichier)
        distances_km, distances_ele, altitudes, vitesses_kmh = self.profil_live
        self.graphe.set_donnees_secondaires(distances_km, distances_ele, altitudes)

        dernier = points_fichier[-1]
        idx = len(points_fichier) - 1
        self._maj_info_point_live(dernier, idx, distances_km, vitesses_kmh)

    def on_click_terminer_live(self, *args):
        """Bouton "Terminer" (onglet 7) :
        1. Met en pause le traitement des points live (ceux reçus
           entre-temps par le serveur local restent en file d'attente,
           sans être perdus, voir _traiter_file_points_live).
        2. Propose d'enregistrer la trace en direct dans un fichier GPX
           (Oui / Non / Annuler) :
           - Annuler : lève la pause, reprend comme si "Terminer"
             n'avait jamais été cliqué.
           - Oui : exporte la trace vers DOSSIER_SORTIE — même
             convention que les autres onglets (Conversion, Fusion,
             Carte/Découpe) : pas de sélecteur "Enregistrer sous", qui
             n'existe pas nativement sous Android/Kivy.
           - Non : n'enregistre rien.
        3. Tente ensuite d'arrêter l'enregistrement dans GPSLogger (best
           effort), puis soit invite à fermer GPSLogger manuellement
           (trace enregistrée), soit réinitialise entièrement l'onglet
           (trace abandonnée)."""
        # Les points du GPS natif arrivent directement dans la file :
        # rien à rattraper avant de figer la trace (l'ancienne
        # resynchronisation GPSLogger n'a plus d'objet).
        # Dernière absorption des points du service de premier plan
        # (écran éteint depuis le dernier cycle d'absorption) avant
        # de figer la trace qui sera proposée à l'enregistrement.
        try:
            self._absorber_points_service()
        except Exception:
            pass

        self.pause_traitement_live = True
        self._maj_statut_live("Suivi en direct mis en pause...", (0.937, 0.424, 0.0, 1))  # #EF6C00

        contenu = _construire_confirmation_oui_non_annuler(
            "Voulez-vous enregistrer la trace en cours dans un fichier GPX ?",
            self._reponse_terminer_live,
        )
        self._popup_terminer = Popup(title="Terminer le suivi en direct", content=contenu, size_hint=(0.9, 0.4))
        self._popup_terminer.open()
        
        self.en_cours_live = False  # Le live est arrêté

    def _annuler_et_reprendre_live(self):
        """Annule la demande de "Terminer" et reprend le suivi en direct
        normalement — que le bouton "Annuler" ait été cliqué directement
        dans la boîte Oui/Non/Annuler, ou après avoir choisi "Oui" puis
        annulé la saisie du nom de fichier : dans les deux cas, on
        revient exactement à l'état d'avant le clic sur "Terminer" (la
        pause est levée, GPSLogger n'est jamais arrêté ici)."""
        self.pause_traitement_live = False
        if not self.points_trace_live:
            # Aucun point live n'a jamais été reçu (GPSLogger éteint, ou
            # jamais démarré) : il n'y a rien à "reprendre", on affiche
            # simplement le message neutre par défaut.
            self._maj_statut_live("Aucun live en cours.", (0.33, 0.33, 0.33, 1))
            return

        self.en_cours_live = True

        self._maj_statut_live("Reprise du suivi en direct.", (0.180, 0.490, 0.196, 1))  # #2E7D32
        Clock.schedule_once(
            lambda dt: self._maj_statut_live(
                self._texte_statut_live(),
                (0.180, 0.490, 0.196, 1)  # #2E7D32
            ),
            1.5,
        )

    def _reponse_terminer_live(self, reponse):
        """reponse : True (Oui), False (Non) ou None (Annuler) — même
        convention que messagebox.askyesnocancel() dans la version
        desktop."""
        self._popup_terminer.dismiss()

        if reponse is None:
            self._annuler_et_reprendre_live()
            return

        if reponse:
            # Suggérer un nom par défaut basé sur l'heure actuelle
            nom_defaut = (
                os.path.basename(self.fichier_gpx_actif_live) if self.fichier_gpx_actif_live
                else f"trace_live_{datetime.now().strftime('%Y%m%d_%H%M%S')}.gpx"
            )

            # Fonction de callback appelée lors de la validation ou annulation du choix du nom
            def _valider_enregistrement_nom(nouveau_nom):
                self._popup_sauvegarde.dismiss()

                # Si l'utilisateur a annulé la saisie du nom : on revient
                # exactement à l'état d'avant le clic sur "Terminer", ni
                # plus ni moins que l'Annuler direct de la boîte
                # Oui/Non/Annuler (même reprise, même message).
                if not nouveau_nom:
                    self._annuler_et_reprendre_live()
                    return

                # S'assurer que le fichier se termine bien par .gpx
                if not nouveau_nom.lower().endswith(".gpx"):
                    nouveau_nom += ".gpx"

                try:
                    # Construction du chemin final dans le dossier de sortie habituel
                    dossier_cible = DOSSIER_SORTIE if os.path.exists(DOSSIER_SORTIE) else DOSSIER_RACINE
                    os.makedirs(dossier_cible, exist_ok=True)
                    chemin_sortie = os.path.join(dossier_cible, nouveau_nom)

                    gps_logic.exporter_vers_gpx(
                        self.points_trace_live, chemin_sortie, garder_temps=True,
                        waypoints=self._construire_waypoints_pour_export(),
                    )
                    self._maj_statut_live(f"Trace enregistrée : {os.path.basename(chemin_sortie)}", (0.180, 0.490, 0.196, 1))
                    # Enregistrement du GPX validé : le fichier temporaire
                    # des annotations n'a plus de raison d'être.
                    self._supprimer_fichier_temp_live()
                except Exception as e:
                    self._maj_statut_live(f"Erreur lors de l'enregistrement de la trace : {e}", (0.776, 0.157, 0.157, 1))
                    # Échec : on GARDE le fichier temporaire (filet de sécurité).
                    self._signaler_fichier_temp_conserve()

                self._arreter_gpslogger()
                # Remise à zéro de l'onglet, SYMÉTRIQUE de la branche
                # « Non » : sans elle, points_trace_live gardait la trace
                # enregistrée en mémoire et le « Live » suivant la
                # RECHARGEAIT au lieu de repartir de 0 point.
                self._reinitialiser_onglet7_vierge()
                self._maj_statut_live("Aucun live en cours.", (0.937, 0.424, 0.0, 1)) # #EF6C00

            # Construction de la boîte de dialogue simple avec un TextInput pour le nom
            layout_sauvegarde = BoxLayout(orientation="vertical", spacing=12, padding=12)

            lbl = Label(text="Nom du fichier de sortie :", size_hint_y=None, height=dp(30), halign="left")
            lbl.bind(width=lambda inst, w: setattr(inst, "text_size", (w, None)))
            layout_sauvegarde.add_widget(lbl)

            champ_saisie = TextInput(
                text=nom_defaut,
                multiline=False,
                size_hint_y=None,
                height=dp(44)
            )
            layout_sauvegarde.add_widget(champ_saisie)

            boutons_sv = BoxLayout(size_hint_y=None, height=dp(48), spacing=6)
            btn_annul_sv = Button(text="Annuler")
            btn_val_sv = Button(text="Enregistrer", background_color=(0.15, 0.68, 0.38, 1))
            boutons_sv.add_widget(btn_annul_sv)
            boutons_sv.add_widget(btn_val_sv)
            layout_sauvegarde.add_widget(boutons_sv)

            # Liaison des boutons du pop-up de saisie
            btn_val_sv.bind(on_release=lambda inst: _valider_enregistrement_nom(champ_saisie.text.strip()))
            btn_annul_sv.bind(on_release=lambda inst: _valider_enregistrement_nom(None))

            self._popup_sauvegarde = Popup(title="Nommer le fichier GPX", content=layout_sauvegarde, size_hint=(0.9, 0.45))
            self._popup_sauvegarde.open()
            return
        else:
            self._maj_statut_live("Trace non enregistrée.", (0.33, 0.33, 0.33, 1))
            # Non-enregistrement validé : suppression du fichier temporaire.
            self._supprimer_fichier_temp_live()

        # --- Arrêt automatique de l'enregistrement (si "Non" a été choisi)
        self._arreter_gpslogger()
        self._reinitialiser_onglet7_vierge()

    def _arreter_gpslogger(self):
        """Arrêt du GPS NATIF (symétrique de gps_natif.demarrer, appelé
        par _demarrer_nouveau_suivi_live) + écriture des logs silencieux
        (comptage par source, journal post-mortem des points live),
        exactement comme dans l'ancienne version GPSLogger.

        Renvoie (True, True, détail) — signature conservée pour ne pas
        toucher aux appelants (on_click_terminer_live). Ne lève jamais."""
        try:
            dossier_cible = DOSSIER_SORTIE if os.path.exists(DOSSIER_SORTIE) else DOSSIER_RACINE
            os.makedirs(dossier_cible, exist_ok=True)
            # Journal « log_YYYYMMDD_HHMMSS.txt » désactivé (demande
            # explicite : ce fichier ne doit plus être généré).
            # nom_log = f"log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            # chemin_log = os.path.join(dossier_cible, nom_log)
            # with open(chemin_log, "w", encoding="utf-8") as f:
            #     for source, nb in sorted(self.compteur_sources_live.items()):
            #         f.write(f"{source} : {nb}\n")

            # Journal « debug_points_YYYYMMDD_HHMMSS.txt » désactivé
            # (demande explicite : ce fichier ne doit plus être généré).
            # if self._journal_points_live:
            #     nom_debug = f"debug_points_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            #     chemin_debug = os.path.join(dossier_cible, nom_debug)
            #     with open(chemin_debug, "w", encoding="utf-8") as f:
            #         f.write("TYPE;HEURE;LAT/TEXTE;LON;ELE;SOURCE\n")
            #         for entree in self._journal_points_live:
            #             if entree and entree[0] == "EVENT":
            #                 f.write(f"EVENT;{entree[1]};{entree[2]}\n")
            #             else:
            #                 f.write("POINT;" + ";".join(str(v) for v in entree) + "\n")
        except Exception:
            pass
        finally:
            self.compteur_sources_live = {}
            self.annotations_live = []

        details = []
        try:
            # Absorber les derniers points écrits par le service de premier
            # plan (écran éteint...) AVANT l'arrêt, pour une trace complète.
            self._absorber_points_service()
        except Exception:
            pass
        try:
            gps_natif.arreter()
            details.append("GPS natif arrêté")
        except Exception as e:
            details.append(f"échec de l'arrêt du GPS natif : {e}")
        # Arrêt du service de premier plan (écran éteint).
        try:
            self._arreter_service_tracker()
            details.append("service tracker arrêté")
        except Exception as e:
            details.append(f"échec de l'arrêt du service tracker : {e}")

        # PURGE FINALE : vide la file des points en attente (les derniers
        # points absorbés juste avant l'arrêt ne doivent PAS revenir
        # s'afficher après la remise à zéro de l'onglet), et remet le
        # compteur de rattrapage à zéro. « Terminer » (Oui ou Non) est le
        # SEUL chemin qui purge tout : après lui, rien ne doit être « à
        # rattraper ».
        try:
            while True:
                self.file_points_live.get_nowait()
        except Exception:
            pass
        self._rattrapage_points = 0
        self._rattrapage_heure = None

        # Purge du fichier de points du service (live_service_points.json)
        # après « Terminer » (Oui ou Non) : la session étant clôturée
        # (marqueur supprimé), ce fichier n'a plus d'usage — le prochain
        # « Live » repartira d'un fichier vierge créé par le service.
        # Seul « Terminer » passe ici : « Annuler » (reprise du live) et
        # la reprise de session interrompue (appli tuée sans Terminer)
        # ne doivent PAS le supprimer.
        try:
            if os.path.exists(self.CHEMIN_POINTS_SERVICE):
                os.remove(self.CHEMIN_POINTS_SERVICE)
        except OSError:
            pass
        self._lignes_service_lues = 0
        return True, True, " / ".join(details)

    def _reinitialiser_onglet7_vierge(self):
        """Remet l'onglet Live dans son état initial "vierge", identique
        à celui affiché avant toute trace live : carte sans trace ni
        marqueur (live ET chargée), profil altimétrique vide, bloc
        d'informations vidé, messages de statut par défaut.

        Efface aussi la trace "à suivre" chargée manuellement (cyan) sur
        cet onglet : après un abandon ("Non"), l'onglet doit repartir
        entièrement vierge, y compris la trace de référence
        éventuellement chargée avant le suivi live."""
        self.fichier_gpx_actif_live = None

        # Vide la trace live (chemin + marqueurs sur la carte).
        self.points_trace_live = []
        self._afficher_trace_live_sur_carte()

        # Efface également la trace chargée manuellement (cyan).
        self.points_courants = []
        self.info_fichier = "Aucune trace à suivre chargée."
        if CARTE_DISPONIBLE and self.map_view is not None:
            if self.trace_layer is not None:
                self.map_view.remove_layer(self.trace_layer)
                self.trace_layer = None
            for m in self.marqueurs_actifs:
                self.map_view.remove_marker(m)
        self.marqueurs_actifs = []

        # Efface aussi les curseurs bleus des waypoints photo pris
        # pendant le live : sans cela, ils restaient affichés sur la
        # carte après "Terminer" alors que la trace, elle, disparaissait.
        if CARTE_DISPONIBLE and self.map_view is not None:
            for mw in self.marqueurs_waypoints:
                self.map_view.remove_marker(mw)
        self.marqueurs_waypoints = []

        self.profil = ([], [], [], [])
        self.profil_live = ([], [], [], [])
        self.graphe.set_donnees(*self.profil)
        self.graphe.effacer_donnees_secondaires()
        self._effacer_info_point_live()

        self._maj_statut_live("Aucun live en cours.", (0.33, 0.33, 0.33, 1))

    # ------------------------------------------------------------------
    # Fichier temporaire des annotations photo du live
    # ------------------------------------------------------------------
    def _recuperer_fichier_temp_live_orphelin(self):
        """Si aucun fichier temporaire n'est suivi en mémoire (ex. juste
        après un redémarrage à froid de l'appli suite à un plantage,
        qui a perdu tout l'état Python), tente de retrouver un fichier
        live_temp_*.json laissé par la session précédente dans le
        dossier de sortie, pour ne pas perdre les waypoints/photos déjà
        enregistrés avant le plantage. Prend le plus récent s'il y en a
        plusieurs (cas normalement rare, un seul fichier temporaire
        existant à la fois en usage normal). Ne lève jamais d'exception."""
        if self.fichier_temp_live is not None:
            return  # déjà suivi (resynchronisation "à chaud", rien à faire)

        dossier = DOSSIER_SORTIE if os.path.exists(DOSSIER_SORTIE) else DOSSIER_RACINE
        try:
            candidats = [
                os.path.join(dossier, nom)
                for nom in os.listdir(dossier)
                if nom.startswith("live_temp_") and nom.endswith(".json")
            ]
        except OSError:
            return
        if not candidats:
            return

        chemin = max(candidats, key=lambda c: os.path.getmtime(c))
        try:
            with open(chemin, "r", encoding="utf-8") as f:
                donnees = json.load(f)
            self.fichier_temp_live = chemin
            self.journal_temp_live = donnees.get("waypoints", [])
            debut = donnees.get("debut_live")
            if debut:
                try:
                    self.debut_live_temp = datetime.fromisoformat(debut)
                except ValueError:
                    pass
            self.temp_live_text = (
                "Fichier temporaire retrouvé après redémarrage :\n"
                f"{os.path.basename(chemin)}\nEmplacement : {dossier}"
            )
        except Exception as e:
            print(f"[Live] Récupération du fichier temporaire impossible : {e}")

    def _restaurer_annotations_depuis_journal(self):
        """Après une reprise (resynchronisation à chaud ou redémarrage à
        froid), reconstruit self.annotations_live (compteur de waypoints
        du statut, marqueurs bleus sur la carte) à partir du journal du
        fichier temporaire (journal_temp_live), qui est la source de
        vérité persistée. Ne fait rien si les annotations sont déjà en
        mémoire (reprise à chaud : tout est déjà affiché)."""
        if self.annotations_live:
            return  # déjà en mémoire (reprise à chaud) : rien à restaurer
        if not self.journal_temp_live:
            return  # aucun waypoint à restaurer

        for w in self.journal_temp_live:
            temps = w.get('time')
            if isinstance(temps, str):
                try:
                    temps = datetime.fromisoformat(temps)
                except ValueError:
                    temps = None
            self.annotations_live.append({
                'lat': w.get('lat'), 'lon': w.get('lon'),
                'ele': w.get('ele'), 'time': temps,
                'name': w.get('name'), 'description': w.get('description'),
            })

        # Marqueurs bleus sur la carte, s'ils n'y sont pas déjà.
        if CARTE_DISPONIBLE and self.map_view is not None and not self.marqueurs_waypoints:
            for w in self.annotations_live:
                if w.get('lat') is None or w.get('lon') is None:
                    continue
                mw = MarqueurWaypoint(
                    zoom=self.map_view.zoom, lat=w['lat'], lon=w['lon'],
                    nom=w.get('name'), description=w.get('description'),
                )
                self.map_view.add_marker(mw)
                self.marqueurs_waypoints.append(mw)

        # Le statut est rafraîchi par le prochain _ajouter_point_live ou
        # par la bascule post-reprise (2,5 s) : le compteur de waypoints
        # y apparaîtra désormais correctement.

    def _construire_waypoints_pour_export(self):
        """Construit la liste de waypoints à intégrer dans le GPX final à
        partir du JOURNAL DU FICHIER TEMPORAIRE (self.journal_temp_live,
        tenu à jour en mémoire en même temps que le fichier sur le
        disque — voir _ecrire_fichier_temp_live), plutôt que de
        self.annotations_live directement : c'est ce journal, relu ou
        retrouvé sur le disque si besoin, qui reste fiable même après
        une resynchronisation. Convertit au passage l'heure (texte ISO
        dans le fichier temporaire) en objet datetime, comme l'attend
        gps_logic.exporter_vers_gpx."""
        waypoints = []
        for w in self.journal_temp_live:
            temps = w.get('time')
            if isinstance(temps, str):
                try:
                    temps = datetime.fromisoformat(temps)
                except ValueError:
                    temps = None
            waypoints.append({
                'lat': w.get('lat'),
                'lon': w.get('lon'),
                'ele': w.get('ele'),
                'time': temps,
                'name': w.get('name'),
                'description': w.get('description'),
            })

        if not waypoints and self.annotations_live:
            # Filet de sécurité : le journal est vide (ex. écriture du
            # fichier temporaire ayant échoué) mais des annotations
            # existent tout de même en mémoire pour cette session : on
            # les utilise plutôt que de perdre les photos.
            return list(self.annotations_live)

        return waypoints

    def _reinitialiser_temp_live(self):
        """Repart à zéro au démarrage d'un live. Ne supprime AUCUN fichier
        sur le disque : un fichier temporaire resté d'un live précédent
        non terminé (plantage, appli fermée) est volontairement conservé."""
        self.fichier_temp_live = None
        self.journal_temp_live = []
        self.debut_live_temp = datetime.now()
        self.temp_live_text = ""

    def _nom_trace_live_courant(self):
        """Nom de la trace en cours : celui du fichier GPSLogger repris si
        connu, sinon un nom provisoire daté du début du live (le nom
        définitif est saisi à l'enregistrement)."""
        if self.fichier_gpx_actif_live:
            return os.path.basename(self.fichier_gpx_actif_live)
        if self.debut_live_temp is None:
            self.debut_live_temp = datetime.now()
        return f"trace_live_{self.debut_live_temp.strftime('%Y%m%d_%H%M%S')}.gpx"

    def _ecrire_fichier_temp_live(self):
        """(Ré)écrit le fichier temporaire : nom de la trace, waypoints et
        noms des photos. Créé à la première photo, mis à jour à chaque
        suivante ; écriture atomique (fichier .part puis renommage) pour
        ne jamais laisser un fichier tronqué. Affiche son nom et son
        emplacement dans le label persistant de l'onglet. Renvoie True si
        l'écriture a réussi ; ne lève jamais d'exception."""
        try:
            if self.debut_live_temp is None:
                self.debut_live_temp = datetime.now()
            if self.fichier_temp_live is None:
                dossier = DOSSIER_SORTIE if os.path.exists(DOSSIER_SORTIE) else DOSSIER_RACINE
                os.makedirs(dossier, exist_ok=True)
                self.fichier_temp_live = os.path.join(
                    dossier, f"live_temp_{self.debut_live_temp.strftime('%Y%m%d_%H%M%S')}.json"
                )

            donnees = {
                "fichier_temporaire": "annotations photo du live (supprimé après l'enregistrement de la trace)",
                "trace": self._nom_trace_live_courant(),
                "debut_live": self.debut_live_temp.isoformat(),
                "derniere_mise_a_jour": datetime.now().isoformat(),
                "nb_points_trace": len(self.points_trace_live),
                "waypoints": self.journal_temp_live,
            }
            chemin_part = self.fichier_temp_live + ".part"
            with open(chemin_part, "w", encoding="utf-8") as f:
                json.dump(donnees, f, ensure_ascii=False, indent=2, default=str)
            os.replace(chemin_part, self.fichier_temp_live)

            self.temp_live_text = ""
            return True
        except Exception as e:
            print(f"[Live] Écriture du fichier temporaire impossible : {e}")
            self.temp_live_text = f"Fichier temporaire non écrit : {e}"
            return False

    def _supprimer_fichier_temp_live(self):
        """Supprime le fichier temporaire (s'il existe) une fois
        l'enregistrement de la trace validé, ou le non-enregistrement
        validé, et l'indique dans le label persistant. Ne lève jamais
        d'exception ; en cas d'échec de suppression, le fichier et son
        emplacement restent affichés."""
        chemin = self.fichier_temp_live
        if not chemin:
            return
        nom = os.path.basename(chemin)
        dossier = os.path.dirname(chemin)
        try:
            for f in (chemin, chemin + ".part"):
                if os.path.exists(f):
                    os.remove(f)
            self.fichier_temp_live = None
            self.journal_temp_live = []
            self.temp_live_text = ""
        except Exception as e:
            print(f"[Live] Suppression du fichier temporaire impossible : {e}")
            self.temp_live_text = (
                f"Fichier temporaire NON supprimé : {nom}\nEmplacement : {dossier}\n({e})"
            )

    def _signaler_fichier_temp_conserve(self):
        """Échec de l'enregistrement du GPX : le fichier temporaire est
        gardé, et son nom/emplacement restent affichés pour pouvoir
        récupérer les waypoints et les noms de photos."""
        if self.fichier_temp_live:
            self.temp_live_text = (
                "Trace non enregistrée : waypoints et photos conservés dans le fichier temporaire :\n"
                f"{os.path.basename(self.fichier_temp_live)}\n"
                f"Emplacement : {os.path.dirname(self.fichier_temp_live)}"
            )

    def _verifier_et_ouvrir_camera(self):
        """Vérifie si un live est en cours avant d'autoriser la prise de
        photo (bouton "Cam"), puis ouvre une balise <wpt> "en attente"
        sur le dernier point GPS connu de la trace en cours — refermée
        par _fermer_waypoint_photo dès que l'utilisateur revient sur
        l'appli après avoir quitté l'appareil photo (voir
        OutilsTracesApp.on_resume, qui détecte ce retour)."""
        if not getattr(self, 'en_cours_live', False):
            self._maj_statut_live("Impossible de prendre une photo : aucun live en cours.", (0.776, 0.157, 0.157, 1))
            return
        if not self.points_trace_live:
            self._maj_statut_live(
                "Impossible de prendre une photo : aucun point GPS enregistré pour l'instant.",
                (0.776, 0.157, 0.157, 1)
            )
            return

        dernier_point = self.points_trace_live[-1]
        self._wpt_en_attente = {
            'lat': dernier_point['lat'],
            'lon': dernier_point['lon'],
            'ele': dernier_point.get('ele'),
            'time': datetime.now(),
        }
        self._ouvrir_camera_Android()

    def _fermer_waypoint_photo(self):
        """Appelée par OutilsTracesApp.on_resume dès que l'utilisateur
        revient sur l'appli après avoir ouvert l'appareil photo :
        "referme" la balise <wpt> ouverte par _verifier_et_ouvrir_camera
        en y inscrivant le nom de la ou des photo(s) prise(s) depuis
        (interrogation du MediaStore Android), puis l'ajoute aux
        annotations de la trace en cours."""
        wpt_en_attente = self._wpt_en_attente
        self._wpt_en_attente = None
        if wpt_en_attente is None:
            return

        noms_photos = self._lister_photos_depuis(wpt_en_attente['time'])
        if noms_photos:
            nom_annotation = ", ".join(noms_photos)
            description = f"{len(noms_photos)} photo(s) prise(s) pendant le suivi en direct"
        else:
            nom_annotation = f"Photo_{wpt_en_attente['time'].strftime('%H%M%S')}"
            description = "Photo prise pendant le suivi en direct (nom non confirmé)"

        self.annotations_live.append({
            'lat': wpt_en_attente['lat'],
            'lon': wpt_en_attente['lon'],
            'ele': wpt_en_attente.get('ele'),
            'time': wpt_en_attente['time'],
            'name': nom_annotation,
            'description': description,
        })
        self.journal_temp_live.append({
            'lat': wpt_en_attente['lat'],
            'lon': wpt_en_attente['lon'],
            'ele': wpt_en_attente.get('ele'),
            'time': wpt_en_attente['time'].isoformat(),
            'name': nom_annotation,
            'description': description,
            'photos': list(noms_photos),
        })
        ok_temp = self._ecrire_fichier_temp_live()
        suffixe = " — fichier temporaire mis à jour." if ok_temp else ""
        self._maj_statut_live(f"Photo(s) enregistrée(s) : {nom_annotation}{suffixe}", (0.180, 0.490, 0.196, 1))
        # Retour au statut live standard apres 2,5 s : il affiche des
        # lors le nombre de waypoints ("(n points, x waypoints)").
        Clock.schedule_once(
            lambda dt: self._maj_statut_live(self._texte_statut_live(), (0.180, 0.490, 0.196, 1)),
            2.5,
        )

    def _lister_photos_depuis(self, temps_ouverture):
        """Interroge le MediaStore Android pour lister le nom de toutes
        les photos ajoutées à la galerie depuis temps_ouverture (avec 2
        secondes de marge en arrière, pour absorber un léger écart
        d'horloge) — c'est-à-dire, dans les faits, celles prises pendant
        que l'appareil photo était ouvert. Renvoie une liste de noms de
        fichier (vide si rien de pertinent trouvé, ou hors Android)."""
        try:
            from jnius import autoclass
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            Images = autoclass('android.provider.MediaStore$Images$Media')
            activite = PythonActivity.mActivity
            resolveur = activite.getContentResolver()

            seuil = int(temps_ouverture.timestamp()) - 2
            curseur = resolveur.query(
                Images.EXTERNAL_CONTENT_URI, None,
                "date_added >= ?", [str(seuil)],
                "date_added ASC"
            )
            if curseur is None:
                return []
            noms = []
            try:
                idx_data = curseur.getColumnIndex("_data")
                if curseur.moveToFirst():
                    while True:
                        if idx_data >= 0:
                            chemin = curseur.getString(idx_data)
                            if chemin:
                                noms.append(os.path.basename(chemin))
                        if not curseur.moveToNext():
                            break
            finally:
                curseur.close()
            return noms
        except Exception as e:
            print(f"[Caméra] Impossible de lister les photos prises : {e}")
            return []

    def _ouvrir_camera_Android(self):
        """Ouvre l'application Appareil photo du système de manière classique sous Android."""
        self._maj_statut_live("Ouverture de la caméra...", (0.937, 0.424, 0.0, 1))
        
        if platform == 'android':
            try:
                from jnius import autoclass
                from android.permissions import request_permissions, Permission
                
                def callback(permissions, grant_results):
                    if all(grant_results):
                        try:
                            Intent = autoclass('android.content.Intent')
                            PythonActivity = autoclass('org.kivy.android.PythonActivity')
                            current_activity = PythonActivity.mActivity
                            package_manager = current_activity.getPackageManager()
                            
                            # Recherche de l'application caméra principale du système
                            # On crée un intent générique de capture ou d'action principale
                            intent = package_manager.getLaunchIntentForPackage("com.android.camera")
                            
                            if not intent:
                                # Fallback sur d'autres packages constructeurs courants si "com.android.camera" n'est pas trouvé
                                for pkg in ["com.sec.android.app.camera", "com.huawei.camera", "com.google.android.GoogleCamera", "com.oneplus.camera"]:
                                    intent = package_manager.getLaunchIntentForPackage(pkg)
                                    if intent:
                                        break
                                        
                            if not intent:
                                # Si aucun package spécifique n'est trouvé, on utilise l'intent global de démarrage d'application media
                                intent = Intent(Intent.ACTION_MAIN)
                                intent.addCategory(Intent.CATEGORY_APP_CAMERA)
                            
                            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                            current_activity.startActivity(intent)
                            
                            self._maj_statut_live("Appareil photo lancé.", (0.180, 0.490, 0.196, 1))
                        except Exception as e:
                            self._wpt_en_attente = None
                            self._maj_statut_live(f"Erreur lancement : {e}", (0.776, 0.157, 0.157, 1))
                    else:
                        self._wpt_en_attente = None
                        self._maj_statut_live("Permission caméra refusée.", (0.776, 0.157, 0.157, 1))

                request_permissions([Permission.CAMERA], callback)
                
            except Exception as e:
                self._wpt_en_attente = None
                self._maj_statut_live(f"Erreur permission : {e}", (0.776, 0.157, 0.157, 1))
        else:
            print("[Live GPSLogger] Simulation : Caméra non disponible sur PC.")
            Clock.schedule_once(lambda dt: self._maj_statut_live("Live en cours... (Caméra simulée sur PC)", (0.180, 0.490, 0.196, 1)), 2.0)
            
    def basculer_freeze(self):
        # Bascule l'état du gel
        self.freeze_actif = not self.freeze_actif

        if getattr(self.map_view, 'freeze_actif', None) is not None:
            self.map_view.freeze_actif = self.freeze_actif

        if getattr(self.graphe, 'freeze_actif', None) is not None:
            self.graphe.freeze_actif = self.freeze_actif

        # --- MODIFICATION ICI : Au dégel de l'onglet ---
        if not self.freeze_actif:
            # AJOUT : Force le rechargement immédiat et complet des tuiles de la carte
            if self.map_view and hasattr(self.map_view, 'trigger_update'):
                self.map_view.trigger_update(True)

            if self.points_trace_live:
                # Récupère le dernier point enregistré
                dernier_point = self.points_trace_live[-1]
                idx = len(self.points_trace_live) - 1

                # Recalcule les données du profil pour s'assurer d'avoir les bonnes valeurs à jour
                distances_km, _, _, vitesses_kmh = self.profil_live

                # Met à jour le bloc "Informations du point sélectionné"
                self._maj_info_point_live(dernier_point, idx, distances_km, vitesses_kmh)
            else:
                self._effacer_info_point_live("Aucun point live enregistré.")

    # Constantes du clic long de gel/degel (identiques a celles de la
    # carte MapViewMolette ; utilisees par les handlers Window de
    # secours ci-dessous).
    DUREE_CLIC_LONG_FREEZE = 0.6
    SEUIL_DEPLACEMENT_FREEZE_DP = 10

    def _relier_touchers_fenetre(self):
        """(Re)branche les handlers Window de suivi des touchers sur la
        carte (rafraichissement des tuiles apres un zoom ou un
        deplacement - meme principe que sur l'onglet 4). Appele a la
        construction de l'ecran ET depuis OutilsTracesApp.on_resume :
        apres un cycle pause/reprise d'Android (retour de l'appareil
        photo, de la galerie...), les bindings Window peuvent cesser de
        recevoir les touchers. On debbranche puis rebranche, pour
        eviter tout doublon d'appel.

        NOTE : la bascule gel/degel par CLIC LONG n'est PAS geree ici :
        elle vit dans la carte elle-meme (MapViewMolette.on_touch_down),
        car un toucher sur la carte est toujours consomme par un widget
        (Scatter de la carte, ou ScrollView ancetre si on laissait
        filer le toucher) et Kivy ne declenche alors PAS les callbacks
        Window.bind - les handlers Window ne voient donc jamais les
        clics sur la carte, seulement ceux sur les zones sans widget
        interactif."""
        Window.unbind(
            on_touch_down=self._debut_touch_carte,
            on_touch_move=self._mouvement_touch_carte,
            on_touch_up=self._sur_touch_carte,
        )
        Window.bind(
            on_touch_down=self._debut_touch_carte,
            on_touch_move=self._mouvement_touch_carte,
            on_touch_up=self._sur_touch_carte,
        )

    def _rect_carte_ecran(self):
        """Rectangle REELLEMENT AFFICHE de la carte, en coordonnees
        fenetre : map_view.pos est (0,0) et ne reflete pas sa position
        a l'ecran (le ScrollView deplace le rendu sans mettre a jour
        pos, et le plein ecran change l'echelle) - l'ancien test
        collide_point(*touch.pos) comparait donc le clic a un rectangle
        fictif coin bas-gauche de la fenetre, d'ou la "zone d'action
        deplacee" observee apres defilement. to_window() convertit la
        position locale du widget en coordonnees fenetre reelles."""
        mv = self.map_view
        mx, my = mv.to_window(mv.x, mv.y)
        return mx, my, mv.width, mv.height

    def _clic_sur_carte_ecran(self, touch):
        """True si le toucher (coordonnees fenetre) tombe sur le
        rectangle reellement affiche de la carte (voir
        _rect_carte_ecran)."""
        if self.map_view is None:
            return False
        mx, my, mw, mh = self._rect_carte_ecran()
        return (mx <= touch.x <= mx + mw) and (my <= touch.y <= my + mh)

    def _debut_touch_carte(self, window, touch):
        if self.manager is not None and self.manager.current == self.name:
            if self._clic_sur_carte_ecran(touch):
                # SECOURS du clic long de gel/degel : si la carte ELLE-MEME
                # n.a pas ete distribuee pour ce toucher (cas observe :
                # carte gelee, aucun evenement on_touch_down),
                # la Window, elle, voit le toucher. Armement IDEMPOTENT :
                # on n'arme un timer QUE si aucun n'est deja arme pour ce
                # toucher par la carte (cles touch.ud partagees).
                if touch.ud.get("timer_clic_long_freeze") is None:
                    touch.ud["carte_pos_depart"] = (touch.x, touch.y)
                    touch.ud["temps_depart_freeze"] = Clock.get_time()
                    touch.ud["bascule_freeze_effectuee"] = False
                    touch.ud["appui_long_annule"] = False
                    touch.ud["timer_clic_long_freeze"] = Clock.schedule_once(
                        lambda dt: self.map_view._bascule_freeze_clic_long(touch),
                        self.DUREE_CLIC_LONG_FREEZE)
                else:
                    touch.ud["carte_pos_depart"] = (touch.x, touch.y)
        return False
        return False

    def _mouvement_touch_carte(self, window, touch):
        # Doigt qui bouge : annule le clic long (seuil partage avec la
        # carte, cles touch.ud identiques). Sans effet si le timer a
        # deja ete consomme par la carte.
        timer = touch.ud.get("timer_clic_long_freeze")
        if timer is None:
            return False
        depart = touch.ud.get("carte_pos_depart")
        if depart is not None and (
                abs(touch.x - depart[0]) > dp(self.SEUIL_DEPLACEMENT_FREEZE_DP)
                or abs(touch.y - depart[1]) > dp(self.SEUIL_DEPLACEMENT_FREEZE_DP)):
            timer.cancel()
            touch.ud["timer_clic_long_freeze"] = None
            touch.ud["appui_long_annule"] = True
        return False

    def _sur_touch_carte(self, window, touch):
        # Chemin de repli au relachement : si le doigt est reste pose
        # au moins 0.6 s sans bouger et sans bascule deja effectuee,
        # on bascule (meme logique que dans la carte, cles partagees).
        timer = touch.ud.get("timer_clic_long_freeze")
        if timer is not None:
            timer.cancel()
            touch.ud["timer_clic_long_freeze"] = None
        if (self.manager is not None and self.manager.current == self.name
                and touch.ud.get("carte_pos_depart") is not None
                and not touch.ud.get("appui_long_annule")
                and not touch.ud.get("bascule_freeze_effectuee")
                and Clock.get_time() - touch.ud.get("temps_depart_freeze", 0.0)
                >= self.DUREE_CLIC_LONG_FREEZE):
            self.map_view._bascule_freeze_clic_long(touch)
        if self.manager is None or self.manager.current != self.name:
            return False
        depart = touch.ud.get("carte_pos_depart")
        if CARTE_DISPONIBLE and self.map_view is not None and depart is not None:
            # Force la mise a jour des tuiles apres un zoom ou un deplacement
            self.map_view.trigger_update(True)
        return False
        return False


class CarteScreen(Screen):
    fichier_source = StringProperty("")
    info_fichier = StringProperty("Aucune trace chargée.")
    trace_chargee = BooleanProperty(False)
    point_coupure_text = StringProperty("")
    status_text = StringProperty("")
    status_color = ListProperty([0.33, 0.33, 0.33, 1])
    en_cours = BooleanProperty(False)
    info_point_text = StringProperty("")
    # Bloc "Informations du point sélectionné" (grille 3 lignes x 2
    # colonnes : Point/GPS, Distance/Altitude, Heure/Vitesse).
    info_point_num = StringProperty("")
    info_point_gps = StringProperty("")
    info_point_dist = StringProperty("")
    info_point_alt = StringProperty("")
    info_point_heure = StringProperty("")
    info_point_vit = StringProperty("")

    def dezoomer_carte(self):
        """Réduit le niveau de zoom de la carte si la carte est chargée."""
        # 1. Vérifie si self.mapview existe déjà
        mapview = getattr(self, "mapview", None)

        # 2. Sinon, cherche l'instance de la carte directement dans l'un des enfants du container
        if not mapview and "map_container" in self.ids:
            for child in self.ids.map_container.children:
                if hasattr(child, "zoom"):
                    mapview = child
                    break

        # 3. Applique le dézoom si la carte est trouvée
        if mapview and hasattr(mapview, "zoom"):
            min_z = getattr(getattr(mapview, "map_source", None), "min_zoom", 0)
            if mapview.zoom > min_z:
                mapview.zoom -= 1
                mapview.center_on(mapview.lat, mapview.lon)

    def zoomer_carte(self):
        """Augmente le niveau de zoom de la carte si la carte est chargée."""
        mapview = getattr(self, "mapview", None)

        if not mapview and "map_container" in self.ids:
            for child in self.ids.map_container.children:
                if hasattr(child, "zoom"):
                    mapview = child
                    break

        if mapview and hasattr(mapview, "zoom"):
            max_z = getattr(getattr(mapview, "map_source", None), "max_zoom", 19)
            if mapview.zoom < max_z:
                mapview.zoom += 1
                mapview.center_on(mapview.lat, mapview.lon)
                
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.points_courants = []
        self.marqueurs_actifs = []
        self.marqueurs_waypoints = []   # curseurs bleus des waypoints (comme Photos/Live)
        self.marqueur_curseur = None
        self.trace_layer = None
        self.map_view = None
        self.profil = ([], [], [], [])

        self.graphe = GrapheProfil()
        self.graphe.callback_clic = self._sur_clic_graphique
        self.ids.zone_graphique.add_widget(self.graphe)
        # Onglet Découpe : PAS de courbe de vitesse sur le
        # graphique, ni son axe/graduations ni sa légende
        # (altitude seule).
        self.graphe.afficher_courbe_vitesse = False
        self.graphe.afficher_axe_vitesse = False

        if CARTE_DISPONIBLE:
            self.map_view = MapViewMolette(zoom=6, lat=46.603354, lon=1.888334, map_source=SOURCE_SATELLITE)
            # On écoute les touchers au niveau de la Window, complètement
            # à l'écart du Scatter interne de MapView (qui gère lui-même
            # le glisser/pincement). Un binding ou un grab sur le Scatter
            # ou sur MapView empêcherait ce dernier de recevoir l'événement
            # et bloquerait le glisser — ce qu'on a observé en pratique.
            Window.bind(on_touch_down=self._debut_touch_carte, on_touch_up=self._sur_touch_carte)
            self.ids.map_container.add_widget(self.map_view)
            # La taille des curseurs de waypoints suit le zoom de la carte.
            self.map_view.bind(zoom=self._maj_taille_waypoints)
        else:
            self.ids.map_container.add_widget(Label(
                text=(
                    "Carte indisponible : le module kivy_garden.mapview\n"
                    "n'est pas installe.\n\nInstalle-le avec :\n"
                    "pip install kivy_garden.mapview"
                ),
                color=(0.6, 0.1, 0.1, 1),
                halign="center",
            ))

    def changer_vue_carte(self, valeur):
        """Change le fond de carte (satellite ou plan), equivalent de
        changer_vue_carte() dans la version desktop."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        self.map_view.map_source = SOURCES_FONDS_CARTES[valeur]
        # L'affectation seule ne suffit pas toujours à relancer le
        # chargement des tuiles : on force explicitement un rafraîchissement
        # complet (sinon le fond peut rester gris-bleu / ne pas revenir).
        self.map_view.trigger_update(True)

    def ouvrir_menu_fonds(self, bouton):
        """Ouvre le menu déroulant compact des fonds de carte sous le
        bouton carré "Layer" (satellite par défaut, vue courante
        marquée d'un point). Voir _construire_menu_fonds_carte."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        menu = _construire_menu_fonds_carte(self)
        menu.open(bouton)

    def ouvrir_selecteur_fichier(self):
        contenu = _construire_selecteur_fichier(self._fichier_choisi)
        if contenu is not None:
            self._popup = Popup(title="Choisir un fichier", content=contenu, size_hint=(0.95, 0.95))
            self._popup.open()

    def _fichier_choisi(self, chemin):
        if hasattr(self, '_popup'):
            self._popup.dismiss()
        if not chemin:
            return
        self.charger_trace(chemin)

    def charger_trace(self, chemin):
        """Charge une trace GPX/KMZ/KML dans cet onglet. Utilisée à la
        fois par le sélecteur de fichier interne (_fichier_choisi
        ci-dessus) et par l'ouverture d'un fichier externe via Android
        (association de fichiers .gpx/.kml/.kmz, "Ouvrir avec" → Bubu
        GPS), voir OutilsTracesApp._sur_nouvel_intent."""
        if not chemin:
            return
        try:
            points = gps_logic.lire_fichier_pour_conversion(chemin)
            
            # ---> AJOUT : Lecture des waypoints de la source (nécessaire pour l'affichage)
            waypoints = gps_logic.lire_waypoints_source(chemin, heure_locale=False)
        except Exception as e:
            self.trace_chargee = False
            self.info_fichier = f"Erreur de lecture : {e}"
            return

        if not points:
            self.trace_chargee = False
            self.info_fichier = "Aucun point GPS trouvé dans ce fichier."
            return

        self.fichier_source = chemin
        self.points_courants = points
        self.trace_chargee = True
        self.point_coupure_text = ""
        self.status_text = ""
        
        # Même règle que les onglets Photos/Live : ni n° de
        # points (nom uniquement en chiffres), ni waypoints superposés au
        # départ ou à l'arrivée de la trace.
        nb_points = len(points)
        vrais_wpts = gps_logic.vrais_waypoints(
            waypoints, [(points[0]['lat'], points[0]['lon']), (points[-1]['lat'], points[-1]['lon'])])
        nb_waypoints = len(vrais_wpts)
        self.info_fichier = f"Trace : {os.path.basename(chemin)}\n{nb_points} points; {nb_waypoints} waypoints."

        self.info_point_text = "Tape sur la carte ou le graphique pour voir le détail d'un point."
        self.info_point_num = ""
        self.info_point_gps = ""
        self.info_point_dist = ""
        self.info_point_alt = ""
        self.info_point_heure = ""
        self.info_point_vit = ""
        self.profil = gps_logic.calculer_profil(points)
        self.graphe.set_donnees(*self.profil)
        self._afficher_trace_sur_carte(points, waypoints=vrais_wpts)

    def _afficher_trace_sur_carte(self, points, waypoints=None):
        """Equivalent de afficher_trace_sur_carte() dans la version
        desktop : trace la polyligne, place les marqueurs D/A, centre
        et zoome la carte sur l'emprise de la trace. Les waypoints
        éventuels sont indiqués par un petit curseur rond et bleu
        (MarqueurWaypoint), comme dans les onglets Photos et Live."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return

        if self.trace_layer is not None:
            self.map_view.remove_layer(self.trace_layer)
            self.trace_layer = None
        for m in self.marqueurs_actifs:
            self.map_view.remove_marker(m)
        self.marqueurs_actifs = []
        for mw in self.marqueurs_waypoints:
            self.map_view.remove_marker(mw)
        self.marqueurs_waypoints = []
        if self.marqueur_curseur is not None:
            self.map_view.remove_marker(self.marqueur_curseur)
            self.marqueur_curseur = None

        if not points:
            return

        liste_coords = [(p['lat'], p['lon']) for p in points]
        # Le calque de la trace est posé APRÈS les marqueurs (D/A et
        # waypoints) : ajouté en dernier, il s'affiche par-dessus eux,
        # comme sur l'onglet Live (7). Sinon les curseurs bleus des
        # waypoints passaient par-dessus la trace.
        self.trace_layer = TraceLayer()
        self.trace_layer.set_points(liste_coords)

        for wpt in (waypoints or []):
            lat_w, lon_w = wpt.get('lat'), wpt.get('lon')
            if lat_w is None or lon_w is None:
                continue
            # « Point de passage 1/2 » : ce sont le départ et l'arrivée,
            # traités à part (triangles) juste après la boucle — pas
            # de disque jaune pour eux (même règle que Nettoyage).
            nom_w = (wpt.get('name') or '').strip()
            if nom_w in ("Point de passage 1", "Point de passage 2"):
                continue
            mw = MarqueurWaypoint(
                zoom=self.map_view.zoom, lat=lat_w, lon=lon_w,
                nom=wpt.get('name'), description=wpt.get('description'),
            )
            self.map_view.add_marker(mw)
            self.marqueurs_waypoints.append(mw)

        # Triangles départ/arrivée pour « Point de passage 1/2 »
        # (vert / rouge, orange unique si boucle fermée ≤ 20 m),
        # même règle que Nettoyage.
        _poser_triangles_points_passage(self, waypoints)

        dist_dep_arr = gps_logic.calculer_distance_haversine(
            points[0]['lat'], points[0]['lon'], points[-1]['lat'], points[-1]['lon']
        )
        if dist_dep_arr <= 20.0:
            m_unique = MarqueurFlag(couleur=COULEUR_FLAG_FERMETURE, lat=points[0]['lat'], lon=points[0]['lon'])
            self.map_view.add_marker(m_unique)
            self.marqueurs_actifs.append(m_unique)
        else:
            m_depart = MarqueurFlag(couleur=COULEUR_FLAG_DEPART, lat=points[0]['lat'], lon=points[0]['lon'])
            m_arrivee = MarqueurFlag(couleur=COULEUR_FLAG_ARRIVEE, lat=points[-1]['lat'], lon=points[-1]['lon'])
            self.map_view.add_marker(m_depart)
            self.map_view.add_marker(m_arrivee)
            self.marqueurs_actifs.extend([m_depart, m_arrivee])

        self.map_view.add_layer(self.trace_layer)

        # REMONTÉE DU CALQUE DE MARQUEURS AU-DESSUS DE LA TRACE (même
        # mécanique que les onglets Nettoyage et temp) : sans elle,
        # les disques jaunes et les triangles D/A passent SOUS la
        # trace dès qu'un add_layer de trace suit la pose des marqueurs.
        self._remonte_calque_marqueurs()

        lats = [c[0] for c in liste_coords]
        lons = [c[1] for c in liste_coords]
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)

        self.map_view.center_on((min_lat + max_lat) / 2, (min_lon + max_lon) / 2)
        max_delta = max(max_lat - min_lat, max_lon - min_lon)
        if max_delta > 0:
            zoom = int(12 - math.log2(max_delta * 10))
            self.map_view.zoom = max(2, min(zoom, 18))

    def _remonte_calque_marqueurs(self):
        """Remonte le calque des marqueurs AU-DESSUS du calque de trace
        (même mécanique que les onglets Nettoyage et temp) : retirer
        puis re-poser le calque de marqueurs via l'API PUBLIQUE de
        MapView (remove_layer/add_layer) le renvoie en fin de pile,
        au-dessus de tout. À appeler après TOUTE pose de marqueurs
        suivant un add_layer."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        couche = getattr(self.map_view, "_marker_layer", None)
        if couche is None:
            for l in getattr(self.map_view, "_layers", []) or []:
                if isinstance(l, MarkerMapLayer):
                    couche = l
                    break
        if couche is None:
            return
        try:
            self.map_view.remove_layer(couche)
            self.map_view.add_layer(couche)
        except Exception:
            pass

    def _maj_taille_waypoints(self, instance, zoom):
        for mw in self.marqueurs_waypoints:
            mw.maj_taille(zoom)

        # Le curseur mobile (disque rose, comme l'onglet Nettoyage)
        # suit aussi le zoom (même formule de taille que les disques).
        if getattr(self, "marqueur_curseur", None) is not None:
            try:
                self.marqueur_curseur.maj_taille(zoom)
            except Exception:
                pass

    def _debut_touch_carte(self, window, touch):
        """Mémorise la position de l'appui si le toucher démarre sur la
        carte, SANS jamais consommer l'événement (pas de grab, pas de
        return True) pour ne surtout pas empêcher MapView de gérer
        normalement le glisser/pincement lui-même."""
        if (
            self.manager is not None and self.manager.current == self.name
            and self.map_view is not None and self.map_view.collide_point(*touch.pos)
        ):
            touch.ud["carte_pos_depart"] = (touch.x, touch.y)
        return False

    def _sur_touch_carte(self, window, touch):
        if self.manager is None or self.manager.current != self.name:
            return False

        depart = touch.ud.get("carte_pos_depart")
        if not CARTE_DISPONIBLE or self.map_view is None or not self.points_courants or depart is None:
            return False
        if abs(touch.x - depart[0]) > dp(8) or abs(touch.y - depart[1]) > dp(8):
            return False  # c'était un glissement (pan/zoom), pas un tap

        zoom = self.map_view.zoom
        cx, cy = gps_logic.projeter_mercator(self.map_view.lat, self.map_view.lon, zoom)
        px = cx + (depart[0] - self.map_view.center_x)
        py = cy - (depart[1] - self.map_view.center_y)
        lat, lon = gps_logic.deprojeter_mercator(px, py, zoom)
        meilleur_idx = None
        meilleure_dist = None
        for i, p in enumerate(self.points_courants):
            d = gps_logic.calculer_distance_haversine(lat, lon, p['lat'], p['lon'])
            if meilleure_dist is None or d < meilleure_dist:
                meilleure_dist = d
                meilleur_idx = i

        if meilleur_idx is None:
            return False

        self._selectionner_point(meilleur_idx, recentrer_carte=False)
        return True

    def _sur_clic_graphique(self, distance_km):
        """Appelé au tap sur le graphique : sélectionne le point dont la
        distance cumulée est la plus proche de la distance tapée
        (équivalent de sur_clic_graphique dans la version desktop, qui
        recentre aussi la carte contrairement à un tap sur la carte)."""
        distances_km = self.profil[0]
        if not distances_km:
            return
        idx = min(range(len(distances_km)), key=lambda i: abs(distances_km[i] - distance_km))
        self._selectionner_point(idx, recentrer_carte=True)

    def _selectionner_point(self, idx, recentrer_carte):
        """Met à jour, en un seul endroit, tout ce qui doit refléter le
        point sélectionné : marqueur curseur sur la carte, numéro de
        découpe, texte d'info, et curseur du graphique."""
        if not (0 <= idx < len(self.points_courants)):
            return
        p = self.points_courants[idx]
        self.point_coupure_text = str(idx + 1)

        if CARTE_DISPONIBLE and self.map_view is not None:
            if self.marqueur_curseur is not None:
                self.map_view.remove_marker(self.marqueur_curseur)
            # Curseur de sélection : disque ROSE exactement comme
            # l'onglet Nettoyage (MarqueurDisqueRouge : texture du
            # MapMarker neutralisée, disque dessiné, taille suivant le
            # zoom via maj_taille).
            self.marqueur_curseur = MarqueurDisqueRouge(
                zoom=self.map_view.zoom,
                couleur=COULEUR_ROSE_CURSEUR,
                lat=p['lat'], lon=p['lon'],
            )
            self.map_view.add_marker(self.marqueur_curseur)
            if recentrer_carte:
                self.map_view.center_on(p['lat'], p['lon'])

        distances_km, _, _, vitesses_kmh = self.profil
        dist = distances_km[idx] if idx < len(distances_km) else 0.0
        vit = vitesses_kmh[idx] if idx < len(vitesses_kmh) else 0.0
        heure = p['time'].strftime("%H:%M:%S") if p['time'] else "-"
        ele_txt = f"{p['ele']} m" if p['ele'] is not None else "-"
        self.info_point_text = ""
        self.info_point_num = f"Point {idx + 1}/{len(self.points_courants)}"
        self.info_point_gps = f"GPS: {p['lat']:.5f}, {p['lon']:.5f}"
        self.info_point_dist = f"Distance: {dist:.2f} km"
        self.info_point_alt = f"Altitude: {ele_txt}"
        self.info_point_heure = f"Heure: {heure}"
        self.info_point_vit = f"Vitesse: {vit} km/h"
        self.graphe.set_selection(dist)

    def executer_decoupe(self):
        if not self.trace_chargee or self.en_cours:
            return
        saisie = self.point_coupure_text.strip()
        if not saisie.isdigit():
            self.status_text = "Numéro de point invalide."
            self.status_color = [0.8, 0.1, 0.1, 1]
            return

        self.en_cours = True
        self.status_text = "Découpe en cours..."
        self.status_color = [0.33, 0.33, 0.33, 1]
        threading.Thread(target=self._decoupe_thread, args=(int(saisie),), daemon=True).start()

    def _decoupe_thread(self, point_coupure):
        try:
            c1, c2 = gps_logic.decouper_trace(
                self.fichier_source, self.points_courants, point_coupure, dossier_sortie=DOSSIER_SORTIE
            )
            message = f"Action réussie !\nFichiers générés :\n{os.path.basename(c1)}\n{os.path.basename(c2)}"
            couleur = [0.15, 0.5, 0.15, 1]
        except Exception as e:
            message = f"Échec de la découpe : {e}"
            couleur = [0.8, 0.1, 0.1, 1]

        def _maj_ui(dt):
            self.en_cours = False
            self.status_text = message
            self.status_color = couleur

        Clock.schedule_once(_maj_ui, 0)

def _nom_est_numero_point(nom):
    """True si le nom (<name> GPX ou <ns0:name> KML) ne contient que des
    chiffres : c'est un n° de point, pas un vrai waypoint."""
    if nom is None:
        return False
    txt = str(nom).strip()
    return txt.isascii() and txt.isdigit()


class NettoyageScreen(Screen):
    """Onglet Nettoyage : charge une trace GPX horodatée, l'affiche sur
    la carte et sur le graphique d'altitude (comme les autres onglets),
    et y marque les points aberrants détectés par
    gps_logic.detecter_points_aberrants avec le SEUIL (km/h) saisi par
    l'utilisateur : tout point extrémité d'un segment plus rapide que
    le seuil est aberrant (règle randonnée : > 5 km/h suspect, 10 km/h
    par défaut). Les marqueurs reprennent le curseur rond des
    annotations/waypoints (MarqueurWaypoint), en DEUX FOIS PLUS PETIT.
    Lecture uniquement : cet onglet ne supprime rien (le nettoyage
    efficace se fait ensuite dans l'onglet Numérotation, qui sait
    supprimer des points par indices)."""

    fichier_source = StringProperty("")
    info_fichier = StringProperty("Aucune trace chargée.")
    # Seuil de détection (km/h) saisi par l'utilisateur : tout point
    # extrémité d'un segment plus rapide que ce seuil est aberrant.
    seuil_text = StringProperty("10")
    # Compteur des points aberrants affiché dans le titre « Points
    # aberrants (N) : » (à la place de l'ancienne liste de numéros).
    compteur_aberrants_text = StringProperty("Points aberrants (0) :")
    # Vrai dès qu'une trace est chargée : le graphique, le bloc compteur
    # et le bouton Enregistrer n'apparaissent qu'à partir de là (ils
    # sont masqués via trace_chargee dans le KV).
    trace_chargee = BooleanProperty(False)
    # Vrai si des points aberrants sont affichés (active « Supprimer »).
    aberrants_present = BooleanProperty(False)
    # Vrai si la trace a été nettoyée (active « Enregistrer »).
    trace_nettoyee = BooleanProperty(False)
    # Bloc « Informations du point sélectionné » (même gabarit que
    # l'onglet Carte/Découpe : grille 3 lignes x 2 colonnes).
    info_point_text = StringProperty("")
    info_point_num = StringProperty("")
    info_point_gps = StringProperty("")
    info_point_dist = StringProperty("")
    info_point_alt = StringProperty("")
    info_point_heure = StringProperty("")
    info_point_vit = StringProperty("")

    def dezoomer_carte(self):
        """Réduit le zoom de la carte (même garde-fou que CarteScreen)."""
        mapview = getattr(self, "map_view", None)
        if mapview and hasattr(mapview, "zoom"):
            min_z = getattr(getattr(mapview, "map_source", None), "min_zoom", 0)
            if mapview.zoom > min_z:
                mapview.zoom -= 1
                mapview.center_on(mapview.lat, mapview.lon)

    def zoomer_carte(self):
        """Augmente le zoom de la carte (même garde-fou que CarteScreen)."""
        mapview = getattr(self, "map_view", None)
        if mapview and hasattr(mapview, "zoom"):
            max_z = getattr(getattr(mapview, "map_source", None), "max_zoom", 19)
            if mapview.zoom < max_z:
                mapview.zoom += 1
                mapview.center_on(mapview.lat, mapview.lon)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.points_courants = []
        self.profil = ([], [], [], [])
        self.marqueurs_actifs = []        # D / A
        self.marqueurs_waypoints = []     # curseurs bleus des waypoints
        self.marqueurs_nettoyage = []     # curseurs rouges (2x plus petits) des points aberrants DURS
        self.marqueur_curseur = None      # curseur de sélection (tap graphique), comme l'onglet Carte
        self.trace_layer = None
        self.map_view = None

        self.graphe = GrapheProfil()
        self.graphe.callback_clic = self._sur_clic_graphique
        self.ids.zone_graphique.add_widget(self.graphe)

        if CARTE_DISPONIBLE:
            self.map_view = MapViewMolette(zoom=6, lat=46.603354, lon=1.888334, map_source=SOURCE_SATELLITE)
            self.ids.map_container.add_widget(self.map_view)
            # La taille des curseurs (waypoints ET points aberrants)
            # suit le zoom de la carte.
            self.map_view.bind(zoom=self._maj_taille_waypoints)
        else:
            self.ids.map_container.add_widget(Label(
                text="Carte indisponible : le module kivy_garden.mapview\nn'est pas installe.",
                color=(0.6, 0.1, 0.1, 1), halign="center"))

    def changer_vue_carte(self, valeur):
        """Change le fond de carte (satellite ou plan) — même logique
        que CarteScreen.changer_vue_carte."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        self.map_view.map_source = SOURCES_FONDS_CARTES[valeur]
        self.map_view.trigger_update(True)

    def ouvrir_menu_fonds(self, bouton):
        """Ouvre le menu déroulant des fonds de carte sous le bouton
        Layer — même logique que CarteScreen.ouvrir_menu_fonds."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        menu = _construire_menu_fonds_carte(self)
        menu.open(bouton)

    def ouvrir_selecteur_fichier(self):
        contenu = _construire_selecteur_fichier(self._fichier_choisi)
        if contenu is not None:
            self._popup = Popup(title="Choisir un fichier", content=contenu, size_hint=(0.95, 0.95))
            self._popup.open()

    def _fichier_choisi(self, chemin):
        if hasattr(self, '_popup'):
            self._popup.dismiss()
        if not chemin:
            return
        try:
            points = gps_logic.lire_fichier_pour_conversion(chemin)
            waypoints_bruts = gps_logic.lire_waypoints_source(chemin, heure_locale=False)
        except Exception as e:
            self.info_fichier = f"Erreur de lecture : {e}"
            return

        if not points:
            self.info_fichier = "Aucun point GPS trouvé dans ce fichier."
            return

        try:
            waypoints = gps_logic.vrais_waypoints(
                waypoints_bruts,
                [(points[0]['lat'], points[0]['lon']), (points[-1]['lat'], points[-1]['lon'])],
            )
        except Exception:
            waypoints = []

        self.fichier_source = chemin
        self.points_courants = points
        self.trace_chargee = True
        self.info_point_text = ("Tape sur un graphique pour voir "
                                "le detail d'un point.")
        self.info_point_num = ""
        self.info_point_gps = ""
        self.info_point_dist = ""
        self.info_point_alt = ""
        self.info_point_heure = ""
        self.info_point_vit = ""

        # --- Carte et graphique : profil de la trace, comme d'habitude.
        self.profil = gps_logic.calculer_profil(points)
        self.graphe.set_donnees(*self.profil)
        self._afficher_trace_sur_carte(points, waypoints=waypoints)

        # --- Détection avec le seuil courant de l'utilisateur.
        self.appliquer_detection()

    def changer_seuil(self, texte):
        """Appelé à chaque frappe dans la zone de saisie du seuil :
        mémorise le texte (le retour KV affiche root.seuil_text)."""
        self.seuil_text = texte

    def appliquer_detection(self):
        """Relance la détection des points aberrants sur la trace
        chargée avec le seuil courant (km/h), et met à jour : marqueurs
        rouges sur la carte, marqueurs sur le graphique, rapport dans
        l'info de fichier. Sans effet si aucune trace n'est chargée."""
        points = self.points_courants
        if not points:
            return

        # Seuil : la saisie en cours, sinon 10 km/h par défaut.
        try:
            seuil = float(self.seuil_text.replace(",", "."))
        except (ValueError, AttributeError):
            seuil = 10.0
        if seuil <= 0:
            seuil = 10.0

        detection = gps_logic.detecter_points_aberrants(
            points, seuil_doux=seuil, seuil_dur=seuil)
        indices_dur = detection["dur"]

        try:
            waypoints = gps_logic.vrais_waypoints(
                gps_logic.lire_waypoints_source(self.fichier_source, heure_locale=False),
                [(points[0]['lat'], points[0]['lon']), (points[-1]['lat'], points[-1]['lon'])],
            )
            nb_waypoints = len(waypoints)
        except Exception:
            nb_waypoints = 0

        self.info_fichier = (
            f"Trace : {os.path.basename(self.fichier_source)}\n"
            f"{len(points)} points; {nb_waypoints} waypoints."
        )

        # Nettoyage des marqueurs aberrants de la carte avant re-pose.
        if CARTE_DISPONIBLE and self.map_view is not None:
            for mw in self.marqueurs_nettoyage:
                self.map_view.remove_marker(mw)
            self.marqueurs_nettoyage = []

        for idx in indices_dur:
            self._poser_marqueur_aberrant(points, idx)
        # La remontée doit suivre la re-pose des disques (le « Supprimer »
        # redessine la carte via _afficher_trace_sur_carte PUIS repose
        # les marqueurs ici : sans ce rappel, ils repassaient sous la
        # trace après une suppression).
        self._remonte_calque_marqueurs()
        distances_km = self.profil[0]
        marqueurs_dur = [(distances_km[idx], (0.80, 0.10, 0.10, 1))
                         for idx in indices_dur if idx < len(distances_km)]
        self.graphe.set_marqueurs(marqueurs_dur)

        # --- Compteur des points aberrants (remplace l'ancienne liste
        # de numéros) : « Points aberrants (N) : ».
        self.compteur_aberrants_text = f"Points aberrants ({len(indices_dur)}) :"
        self.aberrants_present = bool(indices_dur)
        # Une nouvelle détection sur la trace COURANTE (déjà nettoyée ou
        # non) ne change pas le drapeau trace_nettoyee : il ne devient
        # vrai qu'après un « Supprimer » effectif.
        self._indices_aberrants = list(indices_dur)

    def supprimer_aberrants(self):
        """Supprime TOTALEMENT de la trace chargée les points aberrants
        affichés (ceux de la dernière détection) : la trace affichée,
        le graphique, la carte et les marqueurs sont refaits sans eux.
        Ne touche à aucun fichier — l'écriture passe par
        « Enregistrer la trace nettoyée ». Confirmation par popup."""
        points = self.points_courants
        indices = getattr(self, "_indices_aberrants", [])
        if not points or not indices:
            return
        a_suppr = set(indices)

        contenu = _construire_confirmation_oui_non_annuler(
            (f"Supprimer définitivement {len(a_suppr)} point(s) aberrant(s) "
             f"de la trace affichée ?\n(le fichier source n'est pas modifié ; "
             f"utilisez « Enregistrer » ensuite pour écrire la trace nettoyée)"),
            self._reponse_suppression_aberrants,
        )
        self._popup_suppression = Popup(title="Supprimer les points aberrants",
                                        content=contenu, size_hint=(0.9, 0.45))
        self._popup_suppression.open()

    def _reponse_suppression_aberrants(self, reponse):
        """Suite de la confirmation : True = supprime, sinon rien."""
        if hasattr(self, "_popup_suppression"):
            self._popup_suppression.dismiss()
        if not reponse:
            return
        points = self.points_courants
        a_suppr = set(getattr(self, "_indices_aberrants", []))
        if not points or not a_suppr:
            return

        self.points_courants = [p for i, p in enumerate(points) if i not in a_suppr]
        self.trace_nettoyee = True

        # Rafraîchit tout l'affichage avec la trace nettoyée, puis
        # relance la détection au seuil courant (de nouveaux points
        # peuvent devenir aberrants une fois les pics retirés : les
        # segments fusionnés redeviennent mesurables).
        points_nettoyes = self.points_courants
        self.profil = gps_logic.calculer_profil(points_nettoyes)
        self.graphe.set_donnees(*self.profil)
        self.graphe.set_marqueurs([])
        try:
            waypoints = gps_logic.vrais_waypoints(
                gps_logic.lire_waypoints_source(self.fichier_source, heure_locale=False),
                [(points_nettoyes[0]['lat'], points_nettoyes[0]['lon']),
                 (points_nettoyes[-1]['lat'], points_nettoyes[-1]['lon'])],
            )
        except Exception:
            waypoints = []
        self._afficher_trace_sur_carte(points_nettoyes, waypoints=waypoints)
        self.appliquer_detection()

    def enregistrer_trace_nettoyee(self):
        """Écrit la trace nettoyée dans un nouveau fichier GPX nommé
        <nom_source>_vit<seuil>.gpx dans le même dossier que le fichier
        source (ex: rando.gpx + seuil 10 -> rando_vit10.gpx).
        Enregistrement validé par popup Oui/Non/Annuler."""
        if not self.points_courants or not self.fichier_source:
            return
        try:
            seuil = float(self.seuil_text.replace(",", "."))
        except (ValueError, AttributeError):
            seuil = 10.0

        base = os.path.splitext(os.path.basename(self.fichier_source))[0]
        # Seuil sans décimale inutile (10.0 -> "10", 7.5 -> "7.5").
        seuil_txt = f"{seuil:g}"
        nom_sortie = f"{base}_vit{seuil_txt}.gpx"
        dossier = os.path.dirname(self.fichier_source) or DOSSIER_SORTIE

        contenu = _construire_confirmation_oui_non_annuler(
            (f"Enregistrer la trace nettoyée ({len(self.points_courants)} points) "
             f"dans le fichier :\n{nom_sortie} ?"),
            self._reponse_enregistrement_nettoyage,
        )
        self._popup_enregistrement = Popup(title="Enregistrer la trace nettoyée",
                                           content=contenu, size_hint=(0.9, 0.45))
        self._popup_enregistrement.open()

    def _reponse_enregistrement_nettoyage(self, reponse):
        if hasattr(self, "_popup_enregistrement"):
            self._popup_enregistrement.dismiss()
        if not reponse:
            return
        try:
            base = os.path.splitext(os.path.basename(self.fichier_source))[0]
            seuil_txt = self.seuil_text.replace(",", ".").strip() or "10"
            try:
                seuil_txt = f"{float(seuil_txt):g}"
            except ValueError:
                seuil_txt = "10"
            dossier = os.path.dirname(self.fichier_source) or DOSSIER_SORTIE
            os.makedirs(dossier, exist_ok=True)
            chemin_sortie = os.path.join(dossier, f"{base}_vit{seuil_txt}.gpx")

            # Waypoints du fichier source, conservés tels quels (les
            # points aberrants supprimés sont des <trkpt>, jamais des
            # waypoints ; on conserve donc l'intégralité des <wpt>).
            try:
                waypoints = gps_logic.lire_waypoints_source(
                    self.fichier_source, heure_locale=False)
            except Exception:
                waypoints = []
            gps_logic.exporter_vers_gpx(
                self.points_courants, chemin_sortie, garder_temps=True,
                waypoints=waypoints,
            )
            self.info_fichier = (
                f"Trace : {os.path.basename(self.fichier_source)}\n"
                f"{len(self.points_courants)} points; {len(waypoints)} waypoints.\n"
                f"Trace nettoyée enregistrée : {os.path.basename(chemin_sortie)}"
            )
        except Exception as e:
            self.info_fichier = f"Erreur à l'enregistrement : {e}"

    def _sur_clic_graphique(self, distance_km):
        """Appelé au tap sur l'UN OU L'AUTRE graphique : sélectionne le
        point de distance cumulée la plus proche et synchronise le
        curseur partout — marqueur sur la carte (comme l'onglet
        Carte/Découpe), ligne pointillée des DEUX graphiques."""
        distances_km = self.profil[0]
        if not distances_km:
            return
        idx = min(range(len(distances_km)), key=lambda i: abs(distances_km[i] - distance_km))
        p = self.points_courants[idx]
        dist = distances_km[idx]

        # 1. Curseur sur la carte : petit disque ROSE sans fond blanc
        # (MarqueurDisqueRouge : texture du MapMarker neutralisée,
        # disque dessiné).
        if CARTE_DISPONIBLE and self.map_view is not None:
            if self.marqueur_curseur is not None:
                self.map_view.remove_marker(self.marqueur_curseur)
                self.marqueur_curseur = None
            self.marqueur_curseur = MarqueurDisqueRouge(
                zoom=self.map_view.zoom,
                couleur=COULEUR_ROSE_CURSEUR,
                lat=p['lat'], lon=p['lon'],
            )
            self.map_view.add_marker(self.marqueur_curseur)
            self.map_view.center_on(p['lat'], p['lon'])

        # 2. Ligne de sélection sur le graphique.
        self.graphe.set_selection(dist)

        # 3. Bloc « Informations du point sélectionné » (même gabarit
        # que l'onglet Carte/Découpe).
        _, _, _, vitesses_kmh = self.profil
        vit = vitesses_kmh[idx] if idx < len(vitesses_kmh) else 0.0
        heure = p['time'].strftime("%H:%M:%S") if p.get('time') else "-"
        ele_txt = f"{p['ele']} m" if p.get('ele') is not None else "-"
        self.info_point_text = ""
        self.info_point_num = f"Point {idx + 1}/{len(self.points_courants)}"
        self.info_point_gps = f"GPS: {p['lat']:.5f}, {p['lon']:.5f}"
        self.info_point_dist = f"Distance: {dist:.2f} km"
        self.info_point_alt = f"Altitude: {ele_txt}"
        self.info_point_heure = f"Heure: {heure}"
        self.info_point_vit = f"Vitesse: {vit} km/h"

    def _poser_marqueur_aberrant(self, points, idx):
        """Pose sur la carte le petit disque ROUGE d'un point aberrant
        (MarqueurDisqueRouge : canvas du MapMarker effacé, donc ni
        carré blanc ni texture, disque dessiné à la place, demi-taille
        gérée par la classe elle-même)."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        p = points[idx]
        mw = MarqueurDisqueRouge(
            zoom=self.map_view.zoom, lat=p['lat'], lon=p['lon'],
        )
        mw.nom = f"Point aberrant n°{idx + 1}"
        mw.description = (f"Vitesse aberrante au point {idx + 1} "
                          f"(au-dessus du seuil choisi) : fix GPS dégradé probable.")
        self.map_view.add_marker(mw)
        self.marqueurs_nettoyage.append(mw)

    def _remonte_calque_marqueurs(self):
        """Remonte le calque des marqueurs AU-DESSUS du calque de trace.
        Au CHARGEMENT d'une trace, l'ordre est correct NATURELLEMENT : le
        calque de marqueurs n'existe pas encore quand la trace est posée
        (mapview ne le crée qu'au premier add_marker). Mais dès que la
        carte est REDRESSÉE avec des marqueurs déjà posés (bouton
        « Supprimer » : remove_layer puis add_layer de la trace alors que
        le calque de marqueurs existe), la trace repasse au-dessus.
        Solution : retirer puis re-poser le calque de marqueurs via
        l'API PUBLIQUE de MapView (remove_layer/add_layer — la même qui
        fonctionne pour la trace), ce qui le renvoie en fin de pile,
        au-dessus de tout. À appeler après TOUTE pose de marqueurs
        suivant un add_layer (chargement, détection, suppression)."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        # Référence au calque de marqueurs : attribut interne de mapview,
        # sinon recherche dans la liste publique des calques.
        couche = getattr(self.map_view, "_marker_layer", None)
        if couche is None:
            for l in getattr(self.map_view, "_layers", []) or []:
                if isinstance(l, MarkerMapLayer):
                    couche = l
                    break
        if couche is None:
            return
        try:
            self.map_view.remove_layer(couche)
            self.map_view.add_layer(couche)
        except Exception:
            pass

    def _afficher_trace_sur_carte(self, points, waypoints=None):
        """Trace + marqueurs D/A + waypoints + cadrage automatique —
        même logique que CarteScreen._afficher_trace_sur_carte."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return

        if self.trace_layer is not None:
            self.map_view.remove_layer(self.trace_layer)
            self.trace_layer = None
        for m in self.marqueurs_actifs:
            self.map_view.remove_marker(m)
        self.marqueurs_actifs = []
        for mw in self.marqueurs_waypoints:
            self.map_view.remove_marker(mw)
        self.marqueurs_waypoints = []
        for mw in self.marqueurs_nettoyage:
            self.map_view.remove_marker(mw)
        self.marqueurs_nettoyage = []
        if self.marqueur_curseur is not None:
            self.map_view.remove_marker(self.marqueur_curseur)
            self.marqueur_curseur = None

        if not points:
            return

        liste_coords = [(p['lat'], p['lon']) for p in points]
        self.trace_layer = TraceLayer()
        self.trace_layer.set_points(liste_coords)
        # Dans mapview, les marqueurs vivent dans un calque de marqueurs
        # DISTINCT du calque de trace : tout calque ajouté après recouvre
        # TOUS les marqueurs, peu importe leur ordre de pose. Pour que
        # les disques rouges (points aberrants, curseur de sélection)
        # passent PAR-DESSUS la trace — demande explicite de l'onglet
        # Nettoyage — on inverse ici l'ordre des autres onglets : le
        # calque de trace est posé EN PREMIER, avant tous les marqueurs.
        # Conséquence acceptée : les curseurs de waypoints passent aussi
        # au-dessus de la trace (au lieu de dessous comme ailleurs).
        self.map_view.add_layer(self.trace_layer)

        for wpt in (waypoints or []):
            lat_w, lon_w = wpt.get('lat'), wpt.get('lon')
            if lat_w is None or lon_w is None:
                continue
            # « Point de passage 1/2 » : ce sont le départ et l'arrivée,
            # traités à part (triangles) juste après la boucle — pas
            # de disque jaune pour eux.
            nom_w = (wpt.get('name') or '').strip()
            if nom_w in ("Point de passage 1", "Point de passage 2"):
                continue
            mw = MarqueurWaypoint(
                zoom=self.map_view.zoom, lat=lat_w, lon=lon_w,
                nom=wpt.get('name'), description=wpt.get('description'),
            )
            self.map_view.add_marker(mw)
            self.marqueurs_waypoints.append(mw)

        # Triangles départ/arrivée pour « Point de passage 1/2 »
        # (vert / rouge, orange unique si boucle fermée ≤ 20 m).
        _poser_triangles_points_passage(self, waypoints)

        dist_dep_arr = gps_logic.calculer_distance_haversine(
            points[0]['lat'], points[0]['lon'], points[-1]['lat'], points[-1]['lon']
        )
        if dist_dep_arr <= 20.0:
            m_unique = MarqueurFlag(couleur=COULEUR_FLAG_FERMETURE, lat=points[0]['lat'], lon=points[0]['lon'])
            self.map_view.add_marker(m_unique)
            self.marqueurs_actifs.append(m_unique)
        else:
            m_depart = MarqueurFlag(couleur=COULEUR_FLAG_DEPART, lat=points[0]['lat'], lon=points[0]['lon'])
            m_arrivee = MarqueurFlag(couleur=COULEUR_FLAG_ARRIVEE, lat=points[-1]['lat'], lon=points[-1]['lon'])
            self.map_view.add_marker(m_depart)
            self.map_view.add_marker(m_arrivee)
            self.marqueurs_actifs.extend([m_depart, m_arrivee])

        # REMONTÉE DU CALQUE DE MARQUEURS AU-DESSUS DE LA TRACE :
        # dans cette version de mapview, le calque des marqueurs est
        # créé dès l'initialisation du MapView — donc TOUJOURS posé
        # avant notre calque de trace, quel que soit l'ordre des
        # add_layer/add_marker. Les marqueurs (dont les disques
        # rouges) restaient ainsi sous la trace. On le remonte donc
        # explicitement en fin de pile du Scatter interne de la carte
        # (et on le refait après TOUTE pose ultérieure de marqueurs,
        # voir _remonte_calque_marqueurs).
        self._remonte_calque_marqueurs()

        lats = [c[0] for c in liste_coords]
        lons = [c[1] for c in liste_coords]
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)

        self.map_view.center_on((min_lat + max_lat) / 2, (min_lon + max_lon) / 2)
        max_delta = max(max_lat - min_lat, max_lon - min_lon)
        if max_delta > 0:
            zoom = int(12 - math.log2(max_delta * 10))
            self.map_view.zoom = max(2, min(zoom, 18))

    def _maj_taille_waypoints(self, instance, zoom):
        """Suit le zoom de la carte : les curseurs de waypoints gardent
        leur taille habituelle ; les disques rouges des points
        aberrants gèrent eux-mêmes leur demi-taille (MarqueurDisqueRouge
        .maj_taille : ne PAS rediviser ici, elle serait doublée)."""
        for mw in self.marqueurs_waypoints:
            try:
                mw.maj_taille(zoom)
            except Exception:
                pass

        # Le curseur mobile (disque rose) suit aussi le zoom depuis
        # qu'il est passé sur la même formule de taille que les
        # disques jaunes/rouges (plus de cote_dp fixe).
        if getattr(self, "marqueur_curseur", None) is not None:
            try:
                self.marqueur_curseur.maj_taille(zoom)
            except Exception:
                pass

        for mw in self.marqueurs_nettoyage:
            try:
                mw.maj_taille(zoom)
            except Exception:
                pass


    """Onglet Nettoyage : charge une trace GPX horodatée, l'affiche sur
    la carte et sur le graphique d'altitude (comme les autres onglets),
    et y marque les points aberrants détectés par
    gps_logic.detecter_points_aberrants avec le SEUIL (km/h) saisi par
    l'utilisateur : tout point extrémité d'un segment plus rapide que
    le seuil est aberrant (règle randonnée : > 5 km/h suspect, 10 km/h
    par défaut). Les marqueurs reprennent le curseur rond des
    annotations/waypoints (MarqueurWaypoint), en DEUX FOIS PLUS PETIT.
    Lecture uniquement : cet onglet ne supprime rien (le nettoyage
    efficace se fait ensuite dans l'onglet Numérotation, qui sait
    supprimer des points par indices)."""

    fichier_source = StringProperty("")
    info_fichier = StringProperty("Aucune trace chargée.")
    # Seuil de détection (km/h) saisi par l'utilisateur : tout point
    # extrémité d'un segment plus rapide que ce seuil est aberrant.
    seuil_text = StringProperty("10")
    # Compteur des points aberrants affiché dans le titre « Points
    # aberrants (N) : » (à la place de l'ancienne liste de numéros).
    compteur_aberrants_text = StringProperty("Points aberrants (0) :")
    # Vrai dès qu'une trace est chargée : le graphique, le bloc compteur
    # et le bouton Enregistrer n'apparaissent qu'à partir de là (ils
    # sont masqués via trace_chargee dans le KV).
    trace_chargee = BooleanProperty(False)
    # Vrai si des points aberrants sont affichés (active « Supprimer »).
    aberrants_present = BooleanProperty(False)
    # Vrai si la trace a été nettoyée (active « Enregistrer »).
    trace_nettoyee = BooleanProperty(False)
    # Bloc « Informations du point sélectionné » (même gabarit que
    # l'onglet Carte/Découpe : grille 3 lignes x 2 colonnes).
    info_point_text = StringProperty("")
    info_point_num = StringProperty("")
    info_point_gps = StringProperty("")
    info_point_dist = StringProperty("")
    info_point_alt = StringProperty("")
    info_point_heure = StringProperty("")
    info_point_vit = StringProperty("")

    def dezoomer_carte(self):
        """Réduit le zoom de la carte (même garde-fou que CarteScreen)."""
        mapview = getattr(self, "map_view", None)
        if mapview and hasattr(mapview, "zoom"):
            min_z = getattr(getattr(mapview, "map_source", None), "min_zoom", 0)
            if mapview.zoom > min_z:
                mapview.zoom -= 1
                mapview.center_on(mapview.lat, mapview.lon)

    def zoomer_carte(self):
        """Augmente le zoom de la carte (même garde-fou que CarteScreen)."""
        mapview = getattr(self, "map_view", None)
        if mapview and hasattr(mapview, "zoom"):
            max_z = getattr(getattr(mapview, "map_source", None), "max_zoom", 19)
            if mapview.zoom < max_z:
                mapview.zoom += 1
                mapview.center_on(mapview.lat, mapview.lon)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.points_courants = []
        self.profil = ([], [], [], [])
        self.marqueurs_actifs = []        # D / A
        self.marqueurs_waypoints = []     # curseurs bleus des waypoints
        self.marqueurs_nettoyage = []     # curseurs rouges (2x plus petits) des points aberrants DURS
        self.marqueur_curseur = None      # curseur de sélection (tap graphique), comme l'onglet Carte
        self.trace_layer = None
        self.map_view = None

        self.graphe = GrapheProfil()
        self.graphe.callback_clic = self._sur_clic_graphique
        self.ids.zone_graphique.add_widget(self.graphe)

        if CARTE_DISPONIBLE:
            self.map_view = MapViewMolette(zoom=6, lat=46.603354, lon=1.888334, map_source=SOURCE_SATELLITE)
            self.ids.map_container.add_widget(self.map_view)
            # La taille des curseurs (waypoints ET points aberrants)
            # suit le zoom de la carte.
            self.map_view.bind(zoom=self._maj_taille_waypoints)
        else:
            self.ids.map_container.add_widget(Label(
                text="Carte indisponible : le module kivy_garden.mapview\nn'est pas installe.",
                color=(0.6, 0.1, 0.1, 1), halign="center"))

    def changer_vue_carte(self, valeur):
        """Change le fond de carte (satellite ou plan) — même logique
        que CarteScreen.changer_vue_carte."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        self.map_view.map_source = SOURCES_FONDS_CARTES[valeur]
        self.map_view.trigger_update(True)

    def ouvrir_menu_fonds(self, bouton):
        """Ouvre le menu déroulant des fonds de carte sous le bouton
        Layer — même logique que CarteScreen.ouvrir_menu_fonds."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        menu = _construire_menu_fonds_carte(self)
        menu.open(bouton)

    def ouvrir_selecteur_fichier(self):
        contenu = _construire_selecteur_fichier(self._fichier_choisi)
        if contenu is not None:
            self._popup = Popup(title="Choisir un fichier", content=contenu, size_hint=(0.95, 0.95))
            self._popup.open()

    def _fichier_choisi(self, chemin):
        if hasattr(self, '_popup'):
            self._popup.dismiss()
        if not chemin:
            return
        try:
            points = gps_logic.lire_fichier_pour_conversion(chemin)
            waypoints_bruts = gps_logic.lire_waypoints_source(chemin, heure_locale=False)
        except Exception as e:
            self.info_fichier = f"Erreur de lecture : {e}"
            return

        if not points:
            self.info_fichier = "Aucun point GPS trouvé dans ce fichier."
            return

        try:
            waypoints = gps_logic.vrais_waypoints(
                waypoints_bruts,
                [(points[0]['lat'], points[0]['lon']), (points[-1]['lat'], points[-1]['lon'])],
            )
        except Exception:
            waypoints = []

        self.fichier_source = chemin
        self.points_courants = points
        self.trace_chargee = True
        self.info_point_text = ("Tape sur un graphique pour voir "
                                "le detail d'un point.")
        self.info_point_num = ""
        self.info_point_gps = ""
        self.info_point_dist = ""
        self.info_point_alt = ""
        self.info_point_heure = ""
        self.info_point_vit = ""

        # --- Carte et graphique : profil de la trace, comme d'habitude.
        self.profil = gps_logic.calculer_profil(points)
        self.graphe.set_donnees(*self.profil)
        self._afficher_trace_sur_carte(points, waypoints=waypoints)

        # --- Détection avec le seuil courant de l'utilisateur.
        self.appliquer_detection()

    def changer_seuil(self, texte):
        """Appelé à chaque frappe dans la zone de saisie du seuil :
        mémorise le texte (le retour KV affiche root.seuil_text)."""
        self.seuil_text = texte

    def appliquer_detection(self):
        """Relance la détection des points aberrants sur la trace
        chargée avec le seuil courant (km/h), et met à jour : marqueurs
        rouges sur la carte, marqueurs sur le graphique, rapport dans
        l'info de fichier. Sans effet si aucune trace n'est chargée."""
        points = self.points_courants
        if not points:
            return

        # Seuil : la saisie en cours, sinon 10 km/h par défaut.
        try:
            seuil = float(self.seuil_text.replace(",", "."))
        except (ValueError, AttributeError):
            seuil = 10.0
        if seuil <= 0:
            seuil = 10.0

        detection = gps_logic.detecter_points_aberrants(
            points, seuil_doux=seuil, seuil_dur=seuil)
        indices_dur = detection["dur"]

        try:
            waypoints = gps_logic.vrais_waypoints(
                gps_logic.lire_waypoints_source(self.fichier_source, heure_locale=False),
                [(points[0]['lat'], points[0]['lon']), (points[-1]['lat'], points[-1]['lon'])],
            )
            nb_waypoints = len(waypoints)
        except Exception:
            nb_waypoints = 0

        self.info_fichier = (
            f"Trace : {os.path.basename(self.fichier_source)}\n"
            f"{len(points)} points; {nb_waypoints} waypoints."
        )

        # Nettoyage des marqueurs aberrants de la carte avant re-pose.
        if CARTE_DISPONIBLE and self.map_view is not None:
            for mw in self.marqueurs_nettoyage:
                self.map_view.remove_marker(mw)
            self.marqueurs_nettoyage = []

        for idx in indices_dur:
            self._poser_marqueur_aberrant(points, idx)
        # La remontée doit suivre la re-pose des disques (le « Supprimer »
        # redessine la carte via _afficher_trace_sur_carte PUIS repose
        # les marqueurs ici : sans ce rappel, ils repassaient sous la
        # trace après une suppression).
        self._remonte_calque_marqueurs()
        distances_km = self.profil[0]
        marqueurs_dur = [(distances_km[idx], (0.80, 0.10, 0.10, 1))
                         for idx in indices_dur if idx < len(distances_km)]
        self.graphe.set_marqueurs(marqueurs_dur)

        # --- Compteur des points aberrants (remplace l'ancienne liste
        # de numéros) : « Points aberrants (N) : ».
        self.compteur_aberrants_text = f"Points aberrants ({len(indices_dur)}) :"
        self.aberrants_present = bool(indices_dur)
        # Une nouvelle détection sur la trace COURANTE (déjà nettoyée ou
        # non) ne change pas le drapeau trace_nettoyee : il ne devient
        # vrai qu'après un « Supprimer » effectif.
        self._indices_aberrants = list(indices_dur)

    def supprimer_aberrants(self):
        """Supprime TOTALEMENT de la trace chargée les points aberrants
        affichés (ceux de la dernière détection) : la trace affichée,
        le graphique, la carte et les marqueurs sont refaits sans eux.
        Ne touche à aucun fichier — l'écriture passe par
        « Enregistrer la trace nettoyée ». Confirmation par popup."""
        points = self.points_courants
        indices = getattr(self, "_indices_aberrants", [])
        if not points or not indices:
            return
        a_suppr = set(indices)

        contenu = _construire_confirmation_oui_non_annuler(
            (f"Supprimer définitivement {len(a_suppr)} point(s) aberrant(s) "
             f"de la trace affichée ?\n(le fichier source n'est pas modifié ; "
             f"utilisez « Enregistrer » ensuite pour écrire la trace nettoyée)"),
            self._reponse_suppression_aberrants,
        )
        self._popup_suppression = Popup(title="Supprimer les points aberrants",
                                        content=contenu, size_hint=(0.9, 0.45))
        self._popup_suppression.open()

    def _reponse_suppression_aberrants(self, reponse):
        """Suite de la confirmation : True = supprime, sinon rien."""
        if hasattr(self, "_popup_suppression"):
            self._popup_suppression.dismiss()
        if not reponse:
            return
        points = self.points_courants
        a_suppr = set(getattr(self, "_indices_aberrants", []))
        if not points or not a_suppr:
            return

        self.points_courants = [p for i, p in enumerate(points) if i not in a_suppr]
        self.trace_nettoyee = True

        # Rafraîchit tout l'affichage avec la trace nettoyée, puis
        # relance la détection au seuil courant (de nouveaux points
        # peuvent devenir aberrants une fois les pics retirés : les
        # segments fusionnés redeviennent mesurables).
        points_nettoyes = self.points_courants
        self.profil = gps_logic.calculer_profil(points_nettoyes)
        self.graphe.set_donnees(*self.profil)
        self.graphe.set_marqueurs([])
        try:
            waypoints = gps_logic.vrais_waypoints(
                gps_logic.lire_waypoints_source(self.fichier_source, heure_locale=False),
                [(points_nettoyes[0]['lat'], points_nettoyes[0]['lon']),
                 (points_nettoyes[-1]['lat'], points_nettoyes[-1]['lon'])],
            )
        except Exception:
            waypoints = []
        self._afficher_trace_sur_carte(points_nettoyes, waypoints=waypoints)
        self.appliquer_detection()

    def enregistrer_trace_nettoyee(self):
        """Écrit la trace nettoyée dans un nouveau fichier GPX nommé
        <nom_source>_vit<seuil>.gpx dans le même dossier que le fichier
        source (ex: rando.gpx + seuil 10 -> rando_vit10.gpx).
        Enregistrement validé par popup Oui/Non/Annuler."""
        if not self.points_courants or not self.fichier_source:
            return
        try:
            seuil = float(self.seuil_text.replace(",", "."))
        except (ValueError, AttributeError):
            seuil = 10.0

        base = os.path.splitext(os.path.basename(self.fichier_source))[0]
        # Seuil sans décimale inutile (10.0 -> "10", 7.5 -> "7.5").
        seuil_txt = f"{seuil:g}"
        nom_sortie = f"{base}_vit{seuil_txt}.gpx"
        dossier = os.path.dirname(self.fichier_source) or DOSSIER_SORTIE

        contenu = _construire_confirmation_oui_non_annuler(
            (f"Enregistrer la trace nettoyée ({len(self.points_courants)} points) "
             f"dans le fichier :\n{nom_sortie} ?"),
            self._reponse_enregistrement_nettoyage,
        )
        self._popup_enregistrement = Popup(title="Enregistrer la trace nettoyée",
                                           content=contenu, size_hint=(0.9, 0.45))
        self._popup_enregistrement.open()

    def _reponse_enregistrement_nettoyage(self, reponse):
        if hasattr(self, "_popup_enregistrement"):
            self._popup_enregistrement.dismiss()
        if not reponse:
            return
        try:
            base = os.path.splitext(os.path.basename(self.fichier_source))[0]
            seuil_txt = self.seuil_text.replace(",", ".").strip() or "10"
            try:
                seuil_txt = f"{float(seuil_txt):g}"
            except ValueError:
                seuil_txt = "10"
            dossier = os.path.dirname(self.fichier_source) or DOSSIER_SORTIE
            os.makedirs(dossier, exist_ok=True)
            chemin_sortie = os.path.join(dossier, f"{base}_vit{seuil_txt}.gpx")

            # Waypoints du fichier source, conservés tels quels (les
            # points aberrants supprimés sont des <trkpt>, jamais des
            # waypoints ; on conserve donc l'intégralité des <wpt>).
            try:
                waypoints = gps_logic.lire_waypoints_source(
                    self.fichier_source, heure_locale=False)
            except Exception:
                waypoints = []
            gps_logic.exporter_vers_gpx(
                self.points_courants, chemin_sortie, garder_temps=True,
                waypoints=waypoints,
            )
            self.info_fichier = (
                f"Trace : {os.path.basename(self.fichier_source)}\n"
                f"{len(self.points_courants)} points; {len(waypoints)} waypoints.\n"
                f"Trace nettoyée enregistrée : {os.path.basename(chemin_sortie)}"
            )
        except Exception as e:
            self.info_fichier = f"Erreur à l'enregistrement : {e}"

    def _sur_clic_graphique(self, distance_km):
        """Appelé au tap sur l'UN OU L'AUTRE graphique : sélectionne le
        point de distance cumulée la plus proche et synchronise le
        curseur partout — marqueur sur la carte (comme l'onglet
        Carte/Découpe), ligne pointillée des DEUX graphiques."""
        distances_km = self.profil[0]
        if not distances_km:
            return
        idx = min(range(len(distances_km)), key=lambda i: abs(distances_km[i] - distance_km))
        p = self.points_courants[idx]
        dist = distances_km[idx]

        # 1. Curseur sur la carte : petit disque ROSE sans fond blanc
        # (MarqueurDisqueRouge : texture du MapMarker neutralisée,
        # disque dessiné).
        if CARTE_DISPONIBLE and self.map_view is not None:
            if self.marqueur_curseur is not None:
                self.map_view.remove_marker(self.marqueur_curseur)
                self.marqueur_curseur = None
            self.marqueur_curseur = MarqueurDisqueRouge(
                zoom=self.map_view.zoom,
                couleur=COULEUR_ROSE_CURSEUR,
                lat=p['lat'], lon=p['lon'],
            )
            self.map_view.add_marker(self.marqueur_curseur)
            self.map_view.center_on(p['lat'], p['lon'])

        # 2. Ligne de sélection sur le graphique.
        self.graphe.set_selection(dist)

        # 3. Bloc « Informations du point sélectionné » (même gabarit
        # que l'onglet Carte/Découpe).
        _, _, _, vitesses_kmh = self.profil
        vit = vitesses_kmh[idx] if idx < len(vitesses_kmh) else 0.0
        heure = p['time'].strftime("%H:%M:%S") if p.get('time') else "-"
        ele_txt = f"{p['ele']} m" if p.get('ele') is not None else "-"
        self.info_point_text = ""
        self.info_point_num = f"Point {idx + 1}/{len(self.points_courants)}"
        self.info_point_gps = f"GPS: {p['lat']:.5f}, {p['lon']:.5f}"
        self.info_point_dist = f"Distance: {dist:.2f} km"
        self.info_point_alt = f"Altitude: {ele_txt}"
        self.info_point_heure = f"Heure: {heure}"
        self.info_point_vit = f"Vitesse: {vit} km/h"

    def _poser_marqueur_aberrant(self, points, idx):
        """Pose sur la carte le petit disque ROUGE d'un point aberrant
        (MarqueurDisqueRouge : canvas du MapMarker effacé, donc ni
        carré blanc ni texture, disque dessiné à la place, demi-taille
        gérée par la classe elle-même)."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        p = points[idx]
        mw = MarqueurDisqueRouge(
            zoom=self.map_view.zoom, lat=p['lat'], lon=p['lon'],
        )
        mw.nom = f"Point aberrant n°{idx + 1}"
        mw.description = (f"Vitesse aberrante au point {idx + 1} "
                          f"(au-dessus du seuil choisi) : fix GPS dégradé probable.")
        self.map_view.add_marker(mw)
        self.marqueurs_nettoyage.append(mw)

    def _remonte_calque_marqueurs(self):
        """Remonte le calque des marqueurs AU-DESSUS du calque de trace.
        Au CHARGEMENT d'une trace, l'ordre est correct NATURELLEMENT : le
        calque de marqueurs n'existe pas encore quand la trace est posée
        (mapview ne le crée qu'au premier add_marker). Mais dès que la
        carte est REDRESSÉE avec des marqueurs déjà posés (bouton
        « Supprimer » : remove_layer puis add_layer de la trace alors que
        le calque de marqueurs existe), la trace repasse au-dessus.
        Solution : retirer puis re-poser le calque de marqueurs via
        l'API PUBLIQUE de MapView (remove_layer/add_layer — la même qui
        fonctionne pour la trace), ce qui le renvoie en fin de pile,
        au-dessus de tout. À appeler après TOUTE pose de marqueurs
        suivant un add_layer (chargement, détection, suppression)."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        # Référence au calque de marqueurs : attribut interne de mapview,
        # sinon recherche dans la liste publique des calques.
        couche = getattr(self.map_view, "_marker_layer", None)
        if couche is None:
            for l in getattr(self.map_view, "_layers", []) or []:
                if isinstance(l, MarkerMapLayer):
                    couche = l
                    break
        if couche is None:
            return
        try:
            self.map_view.remove_layer(couche)
            self.map_view.add_layer(couche)
        except Exception:
            pass

    def _afficher_trace_sur_carte(self, points, waypoints=None):
        """Trace + marqueurs D/A + waypoints + cadrage automatique —
        même logique que CarteScreen._afficher_trace_sur_carte."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return

        if self.trace_layer is not None:
            self.map_view.remove_layer(self.trace_layer)
            self.trace_layer = None
        for m in self.marqueurs_actifs:
            self.map_view.remove_marker(m)
        self.marqueurs_actifs = []
        for mw in self.marqueurs_waypoints:
            self.map_view.remove_marker(mw)
        self.marqueurs_waypoints = []
        for mw in self.marqueurs_nettoyage:
            self.map_view.remove_marker(mw)
        self.marqueurs_nettoyage = []
        if self.marqueur_curseur is not None:
            self.map_view.remove_marker(self.marqueur_curseur)
            self.marqueur_curseur = None

        if not points:
            return

        liste_coords = [(p['lat'], p['lon']) for p in points]
        self.trace_layer = TraceLayer()
        self.trace_layer.set_points(liste_coords)
        # Dans mapview, les marqueurs vivent dans un calque de marqueurs
        # DISTINCT du calque de trace : tout calque ajouté après recouvre
        # TOUS les marqueurs, peu importe leur ordre de pose. Pour que
        # les disques rouges (points aberrants, curseur de sélection)
        # passent PAR-DESSUS la trace — demande explicite de l'onglet
        # Nettoyage — on inverse ici l'ordre des autres onglets : le
        # calque de trace est posé EN PREMIER, avant tous les marqueurs.
        # Conséquence acceptée : les curseurs de waypoints passent aussi
        # au-dessus de la trace (au lieu de dessous comme ailleurs).
        self.map_view.add_layer(self.trace_layer)

        for wpt in (waypoints or []):
            lat_w, lon_w = wpt.get('lat'), wpt.get('lon')
            if lat_w is None or lon_w is None:
                continue
            # « Point de passage 1/2 » : ce sont le départ et l'arrivée,
            # traités à part (triangles) juste après la boucle — pas
            # de disque jaune pour eux.
            nom_w = (wpt.get('name') or '').strip()
            if nom_w in ("Point de passage 1", "Point de passage 2"):
                continue
            mw = MarqueurWaypoint(
                zoom=self.map_view.zoom, lat=lat_w, lon=lon_w,
                nom=wpt.get('name'), description=wpt.get('description'),
            )
            self.map_view.add_marker(mw)
            self.marqueurs_waypoints.append(mw)

        # Triangles départ/arrivée pour « Point de passage 1/2 »
        # (vert / rouge, orange unique si boucle fermée ≤ 20 m).
        _poser_triangles_points_passage(self, waypoints)

        dist_dep_arr = gps_logic.calculer_distance_haversine(
            points[0]['lat'], points[0]['lon'], points[-1]['lat'], points[-1]['lon']
        )
        if dist_dep_arr <= 20.0:
            m_unique = MarqueurFlag(couleur=COULEUR_FLAG_FERMETURE, lat=points[0]['lat'], lon=points[0]['lon'])
            self.map_view.add_marker(m_unique)
            self.marqueurs_actifs.append(m_unique)
        else:
            m_depart = MarqueurFlag(couleur=COULEUR_FLAG_DEPART, lat=points[0]['lat'], lon=points[0]['lon'])
            m_arrivee = MarqueurFlag(couleur=COULEUR_FLAG_ARRIVEE, lat=points[-1]['lat'], lon=points[-1]['lon'])
            self.map_view.add_marker(m_depart)
            self.map_view.add_marker(m_arrivee)
            self.marqueurs_actifs.extend([m_depart, m_arrivee])

        # REMONTÉE DU CALQUE DE MARQUEURS AU-DESSUS DE LA TRACE :
        # dans cette version de mapview, le calque des marqueurs est
        # créé dès l'initialisation du MapView — donc TOUJOURS posé
        # avant notre calque de trace, quel que soit l'ordre des
        # add_layer/add_marker. Les marqueurs (dont les disques
        # rouges) restaient ainsi sous la trace. On le remonte donc
        # explicitement en fin de pile du Scatter interne de la carte
        # (et on le refait après TOUTE pose ultérieure de marqueurs,
        # voir _remonte_calque_marqueurs).
        self._remonte_calque_marqueurs()

        lats = [c[0] for c in liste_coords]
        lons = [c[1] for c in liste_coords]
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)

        self.map_view.center_on((min_lat + max_lat) / 2, (min_lon + max_lon) / 2)
        max_delta = max(max_lat - min_lat, max_lon - min_lon)
        if max_delta > 0:
            zoom = int(12 - math.log2(max_delta * 10))
            self.map_view.zoom = max(2, min(zoom, 18))

    def _maj_taille_waypoints(self, instance, zoom):
        """Suit le zoom de la carte : les curseurs de waypoints gardent
        leur taille habituelle ; les disques rouges des points
        aberrants gèrent eux-mêmes leur demi-taille (MarqueurDisqueRouge
        .maj_taille : ne PAS rediviser ici, elle serait doublée)."""
        for mw in self.marqueurs_waypoints:
            try:
                mw.maj_taille(zoom)
            except Exception:
                pass

        # Le curseur mobile (disque rose) suit aussi le zoom depuis
        # qu'il est passé sur la même formule de taille que les
        # disques jaunes/rouges (plus de cote_dp fixe).
        if getattr(self, "marqueur_curseur", None) is not None:
            try:
                self.marqueur_curseur.maj_taille(zoom)
            except Exception:
                pass

        for mw in self.marqueurs_nettoyage:
            try:
                mw.maj_taille(zoom)
            except Exception:
                pass


class AjoutScreen(Screen):
    """Onglet "Ajout" : Ã©cran TOTALEMENT INDÃPENDANT des autres onglets
    (aucune rÃ©fÃ©rence croisÃ©e avec eux, aucun Ã©tat partagÃ©) :
      - bouton Â« Charger une trace Â» (GPX/KMZ/KML) via le mÃªme
        explorateur que les autres onglets ;
      - bouton carrÃ© Layer (icÃ´ne images/Layer.png) ouvrant le menu
        dÃ©roulant des 4 vues : satellite, plan, topo, topo+ ;
      - le nom de la trace chargÃ©e s'affiche comme d'habitude
        (info_fichier : nom du fichier + nombre de points et de
        waypoints) ;
      - la carte occupe TOUTE la hauteur restante de l'Ã©cran sous les
        boutons (pas de ScrollView, pas de hauteur fixe) ;
      - trace (polyligne cyan), triangles dÃ©part/arrivÃ©e (vert/rouge,
        orange si boucle fermÃ©e â¤ 20 m), waypoints en disques jaunes
        cliquables (popup nom/description, ouverture photo le cas
        Ã©chÃ©ant), et boutons + / - de zoom.
    Aucune sÃ©lection de point, pas de graphique : volontairement
    minimal pour rester indÃ©pendant."""

    fichier_source = StringProperty("")
    info_fichier = StringProperty("Aucune trace chargÃ©e.")
    # Infos du dernier point ajoutÃ©/sÃ©lectionnÃ©, en TROIS BLOCS
    # affichÃ©s cÃ´te Ã  cÃ´te dans l'onglet : point PRÃCÃDENT, POINT
    # AJOUTÃ, point SUIVANT (GPS / EXIF / altitude pour chacun).
    info_avant_text = StringProperty("")
    info_ajout_text = StringProperty("")
    info_apres_text = StringProperty("")
    # Vrai tant qu'il reste des suppressions annulables : pilote le
    # bouton Â« Annuler la suppression Â» du kv.
    suppression_annulable = BooleanProperty(False)
    trace_chargee = BooleanProperty(False)
    # Délai (secondes) d'appui MAINTENU avant que le glisser ne
    # déplace la carte de cet onglet — même geste « clic long
    # maintenu + glisser » que sur toutes les autres cartes (où c'est
    # le ScrollView ancêtre qui impose ce délai en captant les
    # glissers courts). Réglable ICI en un seul endroit.
    DELAI_PAN_AJOUT = 0.5

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._vue_carte_actuelle = "satellite"
        self.points_courants = []
        self.marqueurs_actifs = []
        self.marqueurs_waypoints = []
        self.marqueurs_points = []
        self.points_ajoutes = []       # points crÃ©Ã©s par l'utilisateur : {'lat','lon','time','marqueur'}
        # Pile des suppressions annulables : une entrÃ©e par point
        # supprimÃ© (clic droit), dans l'ordre â le bouton Â« Annuler la
        # suppression Â» restaure la DERNIÃRE (une Ã  une). Chaque
        # entrÃ©e : {'index': int, 'point': dict, 'type': 'trace'|'ajoute'}.
        self._suppressions_annulables = []
        self._horloge_disques = None
        self.trace_layer = None
        self.map_view = None

        if CARTE_DISPONIBLE:
            self.map_view = MapViewMolette(
                zoom=6, lat=46.603354, lon=1.888334, map_source=SOURCE_SATELLITE)
            self.ids.map_container.add_widget(self.map_view)
            # PAN de la carte au CLIC LONG MAINTENU + GLISSER, comme
            # sur toutes les autres cartes (où c'est le ScrollView
            # ancêtre qui impose ce délai en captant les glissers
            # courts). Voir MapViewMolette.delai_avant_pan et
            # AjoutScreen.DELAI_PAN_AJOUT (réglable en un seul
            # endroit). Rien à faire pour les autres onglets : délai à
            # 0 par défaut, comportement strictement inchangé.
            self.map_view.delai_avant_pan = self.DELAI_PAN_AJOUT
            # La taille des curseurs de waypoints suit le zoom de la carte.
            self.map_view.bind(zoom=self._maj_taille_waypoints)
            # Repose des disques rouges au DÃPLACEMENT de la carte :
            # les points qui sortent du cadre sont remplacÃ©s pour
            # garder 20 disques visibles (debounce, voir
            # _planifier_maj_disques ; le zoom passe par
            # _maj_taille_waypoints ci-dessus).
            self.map_view.bind(lat=self._planifier_maj_disques,
                               lon=self._planifier_maj_disques)
            # GESTION DES TUILES (mÃªme remÃ¨de que le zoom, Ã©tendu au
            # DÃPLACEMENT de la carte) : l'affectation de lat/lon ne
            # relance pas toujours le chargement des tuiles dans
            # kivy_garden.mapview â le fond peut rester gris ou
            # afficher des tuiles pÃ©rimÃ©es quand on se dÃ©place. On
            # force donc, en diffÃ©rÃ© (debounce 0,25 s), un
            # rechargement complet des tuiles aprÃ¨s chaque pan, comme
            # le font dÃ©jÃ  changer_vue_carte et les boutons +/-.
            self.map_view.bind(lat=self._planifier_recharge_tuiles,
                               lon=self._planifier_recharge_tuiles,
                               zoom=self._planifier_recharge_tuiles)
            # AJOUT DE POINTS : tap court sur la trace (dÃ©tection au
            # niveau de la Window, comme le tap de sÃ©lection de
            # l'onglet Carte). Le dÃ©placement des points ajoutÃ©s se
            # fait lui-mÃªme dans MarqueurPointAjout (grab au doigt).
            Window.bind(on_touch_down=self._debut_touch_carte,
                        on_touch_up=self._sur_touch_carte)
        else:
            self.ids.map_container.add_widget(Label(
                text=(
                    "Carte indisponible : le module kivy_garden.mapview\n"
                    "n'est pas installe.\n\nInstalle-le avec :\n"
                    "pip install kivy_garden.mapview"
                ),
                color=(0.6, 0.1, 0.1, 1),
                halign="center",
            ))

    def zoomer_carte(self):
        """Augmente le niveau de zoom de la carte (bouton Â« + Â»).
        MÃªme gestion des tuiles que l'onglet DÃ©coupe : aprÃ¨s le
        changement de zoom, on force le rechargement complet des
        tuiles (trigger_update(True)) â l'affectation de Â« zoom Â»
        seule ne suffit pas toujours, le fond peut rester gris ou
        afficher des tuiles pÃ©rimÃ©es du niveau prÃ©cÃ©dent."""
        mapview = getattr(self, "map_view", None)
        if mapview is not None and hasattr(mapview, "zoom"):
            max_z = getattr(getattr(mapview, "map_source", None), "max_zoom", 19)
            if mapview.zoom < max_z:
                mapview.zoom += 1
                mapview.center_on(mapview.lat, mapview.lon)
                mapview.trigger_update(True)

    def dezoomer_carte(self):
        """RÃ©duit le niveau de zoom de la carte (bouton Â« - Â»).
        MÃªme gestion des tuiles que l'onglet DÃ©coupe (voir
        zoomer_carte) : rechargement complet des tuiles aprÃ¨s le
        changement de zoom."""
        mapview = getattr(self, "map_view", None)
        if mapview is not None and hasattr(mapview, "zoom"):
            min_z = getattr(getattr(mapview, "map_source", None), "min_zoom", 0)
            if mapview.zoom > min_z:
                mapview.zoom -= 1
                mapview.center_on(mapview.lat, mapview.lon)
                mapview.trigger_update(True)

    def changer_vue_carte(self, valeur):
        """Change le fond de carte (satellite, plan, topo, esri_topo),
        comme les onglets Carte/Photos/Live."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        self.map_view.map_source = SOURCES_FONDS_CARTES[valeur]
        self.map_view.trigger_update(True)

    def ouvrir_menu_fonds(self, bouton):
        """Ouvre le menu dÃ©roulant compact des fonds de carte sous le
        bouton carrÃ© Â« Layer Â» (vue courante marquÃ©e en vert)."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        menu = _construire_menu_fonds_carte(self)
        menu.open(bouton)

    def ouvrir_selecteur_fichier(self):
        contenu = _construire_selecteur_fichier(self._fichier_choisi)
        if contenu is not None:
            _ouvrir_popup_selection(self, "Choisir un fichier", contenu)

    def _fichier_choisi(self, chemin):
        if hasattr(self, '_popup'):
            self._popup.dismiss()
        if not chemin:
            return
        self.charger_trace(chemin)

    def charger_trace(self, chemin):
        """Charge une trace GPX/KMZ/KML dans cet onglet : lecture des
        points et des waypoints, affichage du nom du fichier comme
        d'habitude, tracÃ© sur la carte et recentrage automatique."""
        if not chemin:
            return
        try:
            points = gps_logic.lire_fichier_pour_conversion(chemin)
            waypoints = gps_logic.lire_waypoints_source(chemin, heure_locale=False)
        except Exception as e:
            self.trace_chargee = False
            self.info_fichier = f"Erreur de lecture : {e}"
            return

        if not points:
            self.trace_chargee = False
            self.info_fichier = "Aucun point GPS trouvÃ© dans ce fichier."
            return

        self.fichier_source = chemin
        self.points_courants = points
        self.trace_chargee = True
        # Les infos du point prÃ©cÃ©demment ajoutÃ© ne concernent plus
        # cette nouvelle trace, et les suppressions de l'ancienne
        # trace ne sont plus annulables.
        self.info_avant_text = ""
        self.info_ajout_text = ""
        self.info_apres_text = ""
        self._suppressions_annulables = []
        self.suppression_annulable = False

        # MÃªme rÃ¨gle que les onglets Carte/Photos/Live : seuls les
        # vrais waypoints (nom non numÃ©rique, non superposÃ©s au
        # dÃ©part/arrivÃ©e) sont affichÃ©s et comptÃ©s.
        vrais_wpts = gps_logic.vrais_waypoints(
            waypoints, [(points[0]['lat'], points[0]['lon']), (points[-1]['lat'], points[-1]['lon'])])
        self.info_fichier = (
            f"Trace : {os.path.basename(chemin)}\n"
            f"{len(points)} points; {len(vrais_wpts)} waypoints."
        )

        self._afficher_trace_sur_carte(points, waypoints=vrais_wpts)

    def _afficher_trace_sur_carte(self, points, waypoints=None):
        """Trace la polyligne, pose les triangles dÃ©part/arrivÃ©e, les
        disques jaunes des waypoints, centre et zoome la carte sur
        l'emprise de la trace (mÃªme graphisme que l'onglet Carte)."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return

        if self.trace_layer is not None:
            self.map_view.remove_layer(self.trace_layer)
            self.trace_layer = None
        for m in self.marqueurs_actifs:
            self.map_view.remove_marker(m)
        self.marqueurs_actifs = []
        for mw in self.marqueurs_waypoints:
            self.map_view.remove_marker(mw)
        self.marqueurs_waypoints = []
        for mp in self.marqueurs_points:
            self.map_view.remove_marker(mp)
        self.marqueurs_points = []
        # Les points AJOUTÃS par l'utilisateur ne survivent pas au
        # chargement d'une nouvelle trace : on repart de zÃ©ro.
        for point in self.points_ajoutes:
            try:
                self.map_view.remove_marker(point['marqueur'])
            except Exception:
                pass
        self.points_ajoutes = []

        if not points:
            return

        liste_coords = [(p['lat'], p['lon']) for p in points]
        # Le calque de la trace est posÃ© APRÃS les marqueurs : ajoutÃ©
        # en dernier il s'affiche par-dessus eux, puis on remonte le
        # calque des marqueurs au-dessus de la trace (mÃªme mÃ©canique
        # que l'onglet Carte).
        self.trace_layer = TraceLayer()
        self.trace_layer.set_points(liste_coords)

        for wpt in (waypoints or []):
            lat_w, lon_w = wpt.get('lat'), wpt.get('lon')
            if lat_w is None or lon_w is None:
                continue
            nom_w = (wpt.get('name') or '').strip()
            if nom_w in ("Point de passage 1", "Point de passage 2"):
                continue
            mw = MarqueurWaypoint(
                zoom=self.map_view.zoom, lat=lat_w, lon=lon_w,
                nom=wpt.get('name'), description=wpt.get('description'),
            )
            self.map_view.add_marker(mw)
            self.marqueurs_waypoints.append(mw)

        # Triangles dÃ©part/arrivÃ©e pour Â« Point de passage 1/2 Â»
        # (vert / rouge, orange unique si boucle fermÃ©e â¤ 20 m).
        _poser_triangles_points_passage(self, waypoints)

        dist_dep_arr = gps_logic.calculer_distance_haversine(
            points[0]['lat'], points[0]['lon'], points[-1]['lat'], points[-1]['lon']
        )
        if dist_dep_arr <= 20.0:
            m_unique = MarqueurFlag(couleur=COULEUR_FLAG_FERMETURE,
                                    lat=points[0]['lat'], lon=points[0]['lon'])
            self.map_view.add_marker(m_unique)
            self.marqueurs_actifs.append(m_unique)
        else:
            m_depart = MarqueurFlag(couleur=COULEUR_FLAG_DEPART,
                                    lat=points[0]['lat'], lon=points[0]['lon'])
            m_arrivee = MarqueurFlag(couleur=COULEUR_FLAG_ARRIVEE,
                                     lat=points[-1]['lat'], lon=points[-1]['lon'])
            self.map_view.add_marker(m_depart)
            self.map_view.add_marker(m_arrivee)
            self.marqueurs_actifs.extend([m_depart, m_arrivee])

        # DISQUES ROUGES sur les points de la trace : toujours
        # NB_MAX_DISQUES_POINTS (20) VISIBLES DANS LE CADRE, quel que
        # soit le zoom ou la position (voir _maj_disques_points) :
        # EXACTEMENT les mÃªmes que les points aberrants de l'onglet
        # Nettoyage (MarqueurDisqueRouge : canvas du MapMarker effacÃ©,
        # disque rouge dessinÃ©, source neutralisÃ©e, demi-taille gÃ©rÃ©e
        # par la classe via maj_taille â couleur rouge par dÃ©faut).
        # La pose est faite Ã  la fin du chargement, APRÃS le zoom
        # automatique sur l'emprise de la trace.
        self.map_view.add_layer(self.trace_layer)
        self._remonte_calque_marqueurs()

        lats = [c[0] for c in liste_coords]
        lons = [c[1] for c in liste_coords]
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)

        self.map_view.center_on((min_lat + max_lat) / 2, (min_lon + max_lon) / 2)
        max_delta = max(max_lat - min_lat, max_lon - min_lon)
        if max_delta > 0:
            zoom = int(12 - math.log2(max_delta * 10))
            self.map_view.zoom = max(2, min(zoom, 18))
        self._maj_taille_waypoints(self.map_view, self.map_view.zoom)
        # Pose initiale des disques rouges : 20 (ou moins si la trace
        # en compte moins) rÃ©partis parmi les points VISIBLES dans le
        # cadre, aprÃ¨s le zoom automatique sur l'emprise de la trace.
        self._maj_disques_points()

    def _remonte_calque_marqueurs(self):
        """Remonte le calque des marqueurs AU-DESSUS du calque de trace
        (mÃªme mÃ©canique que l'onglet Carte)."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        couche = getattr(self.map_view, "_marker_layer", None)
        if couche is None:
            for l in getattr(self.map_view, "_layers", []) or []:
                if isinstance(l, MarkerMapLayer):
                    couche = l
                    break
        if couche is None:
            return
        try:
            self.map_view.remove_layer(couche)
            self.map_view.add_layer(couche)
        except Exception:
            pass

    # Nombre de disques rouges affichÃ©s DANS LE CADRE de la carte, quel
    # que soit le niveau de zoom ou la position : toujours 20 visibles
    # (moins si la trace contient moins de points). Ã chaque zoom /
    # dÃ©placement, les points qui sortent du cadre sont remplacÃ©s par
    # d'autres, rÃ©partis uniformÃ©ment parmi les points VISIBLES.
    NB_MAX_DISQUES_POINTS = 20

    def _retirer_disques_points(self):
        """Retire de la carte tous les disques rouges des points."""
        if self.map_view is not None:
            for mp in self.marqueurs_points:
                try:
                    self.map_view.remove_marker(mp)
                except Exception:
                    pass
        self.marqueurs_points = []

    def _bornes_visibles(self):
        """Bornes (lat_min, lat_max, lon_min, lon_max) de la partie de
        la carte actuellement visible Ã  l'Ã©cran, calculÃ©es avec la
        mÃªme projection Mercator maison que le tap sur la carte
        (cf. _sur_touch_carte de l'onglet Carte)."""
        if self.map_view is None:
            return None
        zoom = self.map_view.zoom
        cx, cy = gps_logic.projeter_mercator(self.map_view.lat, self.map_view.lon, zoom)
        demi_w = self.map_view.width / 2.0
        demi_h = self.map_view.height / 2.0
        lat_b, lon_g = gps_logic.deprojeter_mercator(cx - demi_w, cy - demi_h, zoom)
        lat_h, lon_d = gps_logic.deprojeter_mercator(cx + demi_w, cy + demi_h, zoom)
        return (min(lat_b, lat_h), max(lat_b, lat_h),
                min(lon_g, lon_d), max(lon_g, lon_d))

    def _indices_points_affiches(self):
        """Indices des points qui recevront un disque rouge : au plus
        NB_MAX_DISQUES_POINTS points, RÃPARTIS UNIFORMÃMENT parmi les
        points de la trace actuellement DANS LE CADRE de la carte
        (premier et dernier visibles toujours inclus). Si le cadre
        contient moins de points que le maximum, tous les points
        visibles sont affichÃ©s (au zoom max, une portion de trace de 5
        points affiche ces 5 points)."""
        bornes = self._bornes_visibles()
        if bornes is None:
            return []
        lat_min, lat_max, lon_min, lon_max = bornes
        visibles = [i for i, p in enumerate(self.points_courants)
                    if lat_min <= p['lat'] <= lat_max and lon_min <= p['lon'] <= lon_max]
        n = len(visibles)
        if n == 0:
            return []
        if n <= self.NB_MAX_DISQUES_POINTS:
            return visibles
        # RÃ©partition uniforme de NB_MAX_DISQUES_POINTS indices parmi
        # les visibles : premier et derniers visibles inclus, espacement
        # rÃ©gulier entre eux.
        m = self.NB_MAX_DISQUES_POINTS
        indices = []
        deja_vus = set()
        for k in range(m):
            i = visibles[int(round(k * (n - 1) / (m - 1)))]
            if i not in deja_vus:
                deja_vus.add(i)
                indices.append(i)
        return indices

    def _maj_disques_points(self):
        """(Re)pose les disques rouges pour qu'il y en ait TOUJOURS
        NB_MAX_DISQUES_POINTS (20) visibles dans le cadre, quel que
        soit le zoom ou la position de la carte : Ã  chaque changement,
        les points sortis de l'Ã©cran sont remplacÃ©s par d'autres pris
        uniformÃ©ment parmi les points visibles. Le coÃ»t reste constant
        (retire + repose 20 marqueurs au plus), la carte reste fluide.
        Sans trace ou sans carte : ne fait rien."""
        if not CARTE_DISPONIBLE or self.map_view is None or not self.points_courants:
            return
        self._retirer_disques_points()
        # Les points AJOUTÉS (disques orange déplaçables) ne reçoivent
        # JAMAIS de disque rouge : ils sont déjà dans points_courants
        # (insérés à la création), et sans cette exclusion la repose
        # posait un MarqueurPointTrace rouge NON DÉPLAÇABLE par-dessus
        # le marqueur orange — le point paraissait « devenir rouge » et
        # refusait d'être déplacé (l'appui attrapait le disque rouge).
        indices_ajoutes = {p['index'] for p in self.points_ajoutes}
        for i in self._indices_points_affiches():
            if i in indices_ajoutes:
                continue
            p = self.points_courants[i]
            # Disque rouge INTERACTIF (MarqueurPointTrace) : clic
            # gauche = infos des points entourant, clic droit (PC) =
            # suppression du point. Le marqueur garde une rÃ©fÃ©rence au
            # DICT du point : son index est retrouvÃ© par identitÃ© au
            # moment du clic, donc reste correct aprÃ¨s insertions ou
            # suppressions d'autres points.
            mp = MarqueurPointTrace(
                point=p,
                zoom=self.map_view.zoom, lat=p['lat'], lon=p['lon'],
                on_clic=self._clic_point_trace,
                on_suppression=self._supprimer_point_trace,
            )
            self.map_view.add_marker(mp)
            self.marqueurs_points.append(mp)
        # PAS de _remonte_calque_marqueurs() ici : le calque des
        # marqueurs est dÃ©jÃ  au-dessus du calque de trace (remontÃ© au
        # chargement et Ã  chaque pose de trace), et retirer/reposer
        # le calque Ã  chaque repose de disques perturbait le
        # chargement des tuiles pendant les dÃ©placements de la carte.

    def _planifier_maj_disques(self, *args):
        """Repose diffÃ©rÃ©e (debounce 0,15 s) des disques rouges :
        dÃ©clenchÃ©e Ã  chaque changement de zoom OU de position (lat/lon)
        de la carte. Le dÃ©lai Ã©vite de retirer/reposer 20 marqueurs Ã 
        chaque frame pendant un glisser continu : on ne repose qu'une
        fois le mouvement stabilisÃ© (ou toutes les 0,15 s s'il dure)."""
        if getattr(self, "_horloge_disques", None) is not None:
            self._horloge_disques.cancel()
        self._horloge_disques = Clock.schedule_once(
            lambda dt: self._maj_disques_points(), 0.15)

    def _planifier_recharge_tuiles(self, *args):
        """Rechargement diffÃ©rÃ© (debounce 0,25 s) des TUILES de la
        carte, dÃ©clenchÃ© Ã  chaque changement de position (lat/lon) ou
        de zoom : aprÃ¨s un pan, kivy_garden.mapview ne recharge pas
        toujours les tuiles de la zone nouvellement visible (fond
        gris, tuiles pÃ©rimÃ©es). On force trigger_update(True) â le
        mÃªme rechargement complet que changer_vue_carte, les boutons
        +/- et le dÃ©gel â une seule fois le mouvement stabilisÃ© (le
        debounce Ã©vite de le relancer Ã  chaque frame du glisser)."""
        if getattr(self, "_horloge_tuiles", None) is not None:
            self._horloge_tuiles.cancel()
        self._horloge_tuiles = Clock.schedule_once(
            lambda dt: self._recharger_tuiles(), 0.25)

    def _recharger_tuiles(self, *args):
        """Rechargement complet des TUILES de la carte (voir
        _planifier_recharge_tuiles) : trigger_update(True) force
        MapView Ã  recalculer et recharger toutes les tuiles de la
        zone visible â mÃªmes mÃ©canismes internes que les onglets
        Carte et DÃ©coupe (aucun hack des tuiles : les interventions
        sur les structures internes de mapview cassaient l'affichage,
        elles ont toutes Ã©tÃ© retirÃ©es)."""
        if self.map_view is None:
            return
        try:
            self.map_view.trigger_update(True)
        except Exception:
            pass

    def _maj_taille_waypoints(self, instance, zoom):
        """La taille des disques (waypoints jaunes ET points rouges)
        suit le zoom de la carte ; et comme le zoom change les bornes
        visibles, la repose des disques rouges est REPLANIFIÃE (pour
        garder 20 disques visibles dans le cadre)."""
        for mw in self.marqueurs_waypoints:
            mw.maj_taille(zoom)
        for mp in self.marqueurs_points:
            mp.maj_taille(zoom)
        if self.trace_chargee:
            self._planifier_maj_disques()

    # ------------------------------------------------------------------
    # AJOUT DE POINTS : tap sur la trace -> point orange draggable.
    # ------------------------------------------------------------------
    def _debut_touch_carte(self, window, touch):
        """MÃ©morise la position de l'appui si le toucher dÃ©marre sur la
        carte (pour distinguer plus tard le simple tap du glisser),
        SANS consommer l'Ã©vÃ©nement (pas de grab, pas de return True)
        â mÃªme principe que l'onglet Carte. Si la carte est GELÃE
        (clic long), pas d'ajout de point : tout est verrouillÃ©."""
        if (self.manager is not None and self.manager.current == self.name
                and self.map_view is not None
                and not getattr(self.map_view, "freeze_actif", False)
                and self.map_view.collide_point(*self._touch_vers_carte(touch))):
            touch.ud["carte_pos_depart_ajout"] = (touch.x, touch.y)
        return False

    def _touch_vers_carte(self, touch):
        """Convertit la position (FENÊTRE) d'un toucher reçu par un
        handler Window vers l'espace LOCAL de la carte, utilisé par
        toute la géométrie de l'onglet (mv.center, positions des
        points via projeter_mercator). Les handlers Window reçoivent
        des coordonnées fenêtre « brutes » ; sur les autres onglets la
        carte est posée en (0, 0) de l'écran et les deux espaces
        coïncident. Mais l'onglet Ajout est le seul à emboîter sa
        carte dans un RelativeLayout placé sous ~250 dp de boutons :
        l'origine locale de la carte est donc décalée de celle de la
        fenêtre. Sans cette conversion, tout tap d'ajout de point
        tombait d'autant trop haut."""
        if self.map_view is None:
            return (touch.x, touch.y)
        dx, dy = self.map_view.to_window(0, 0)
        return (touch.x - dx, touch.y - dy)

    def _sur_touch_carte(self, window, touch):
        """Fin du toucher sur la carte : si c'est un TAP COURT (pas un
        glisser, pas un clic long de gel) tombant SUR LA TRACE (Ã 
        moins de 35 dp d'un segment de la trace), AJOUTE un point
        orange Ã  cet endroit, avec par dÃ©faut l'horodatage interpolÃ©
        entre les deux points de la TRACE COMPLÃTE qui l'encadrent
        (pas seulement les points affichÃ©s)."""
        if self.manager is None or self.manager.current != self.name:
            return False
        depart = touch.ud.get("carte_pos_depart_ajout")
        if (not CARTE_DISPONIBLE or self.map_view is None
                or not self.points_courants or depart is None):
            return False
        # CLIC DROIT (souris, PC) : JAMAIS d'ajout de point â le clic
        # droit est rÃ©servÃ© Ã  la SUPPRESSION (disques rouges et
        # points orange). Sans ce filtre, un clic droit prÃ¨s de la
        # trace ajoutait un point non demandÃ©.
        if getattr(touch, "button", "") == "right":
            return False
        # Le clic long de gel/dÃ©gel de la carte ne doit PAS ajouter de
        # point : le marqueur a dÃ©jÃ  basculÃ© le gel pendant l'appui.
        if touch.ud.get("bascule_freeze_effectuee"):
            return False
        # Le tap a commencÃ© sur un POINT EXISTANT (disque rouge de la
        # trace ou point ajoutÃ© orange) : c'est un clic/suppression de
        # ce point, PAS un ajout de nouveau point.
        if touch.ud.get("point_existant_touche"):
            return False
        if abs(touch.x - depart[0]) > dp(8) or abs(touch.y - depart[1]) > dp(8):
            return False  # c'Ã©tait un glisser (pan/zoom), pas un tap

        mv = self.map_view
        zoom = mv.zoom
        cx, cy = gps_logic.projeter_mercator(mv.lat, mv.lon, zoom)
        # Le tap est converti dans l'espace LOCAL de la carte : le
        # handler Window reçoit des coordonnées fenêtre, or toute la
        # géométrie ci-dessous (mv.center, écrans des points) est en
        # coordonnées carte (voir _touch_vers_carte).
        tap_x, tap_y = self._touch_vers_carte(touch)

        def _ecran(p):
            gx, gy = gps_logic.projeter_mercator(p['lat'], p['lon'], zoom)
            return (mv.center_x + gx - cx, mv.center_y - (gy - cy))

        # Segment de la trace le plus proche du tap (distance Ã©cran en
        # dp, indÃ©pendante du zoom).
        meilleur = None  # (distance_px, index_segment, ratio)
        for i in range(len(self.points_courants) - 1):
            x1, y1 = _ecran(self.points_courants[i])
            x2, y2 = _ecran(self.points_courants[i + 1])
            d, ratio = self._distance_tap_segment(tap_x, tap_y, x1, y1, x2, y2)
            if meilleur is None or d < meilleur[0]:
                meilleur = (d, i, ratio)
        if meilleur is None or meilleur[0] > dp(35):
            return False  # tap hors de la trace : rien Ã  ajouter

        # Position GPS exacte du tap.
        px = cx + (tap_x - mv.center_x)
        py = cy - (tap_y - mv.center_y)
        lat, lon = gps_logic.deprojeter_mercator(px, py, zoom)
        self.ajouter_point_sur_trace(lat, lon, meilleur[1], meilleur[2])
        return True

    @staticmethod
    def _distance_tap_segment(x, y, x1, y1, x2, y2):
        """Distance du point (x, y) au segment [P1, P2], et ratio (0..1)
        du projetÃ© sur le segment (utilisÃ© pour interpoler l'horodatage
        entre les deux extrÃ©mitÃ©s du segment)."""
        dx, dy = x2 - x1, y2 - y1
        long2 = dx * dx + dy * dy
        if long2 <= 1e-9:
            return math.hypot(x - x1, y - y1), 0.0
        t = ((x - x1) * dx + (y - y1) * dy) / long2
        t = max(0.0, min(1.0, t))
        proj_x, proj_y = x1 + t * dx, y1 + t * dy
        return math.hypot(x - proj_x, y - proj_y), t

    def _horodatage_interpole(self, index_segment, ratio):
        """Horodatage interpolÃ© entre le point index_segment et le
        point index_segment+1 de la TRACE COMPLÃTE (self.points_courants,
        pas seulement les points affichÃ©s) : t1 + (t2 - t1) * ratio.
        Renvoie None si l'un des deux points n'a pas d'horodatage."""
        t1 = self.points_courants[index_segment]['time']
        t2 = self.points_courants[index_segment + 1]['time']
        if t1 is not None and t2 is not None:
            return t1 + (t2 - t1) * ratio
        return None

    def _altitude_interpolee(self, index_segment, ratio):
        """Altitude interpolÃ©e entre le point index_segment et le point
        index_segment+1 de la trace complÃ¨te : ele1 + (ele2 - ele1) *
        ratio. Renvoie None si l'un des deux points n'a pas
        d'altitude."""
        e1 = self.points_courants[index_segment]['ele']
        e2 = self.points_courants[index_segment + 1]['ele']
        if e1 is not None and e2 is not None:
            return e1 + (e2 - e1) * ratio
        return e1 if e1 is not None else e2

    def _segment_le_plus_proche(self, lat, lon):
        """(index_segment, ratio) du segment de la trace complÃ¨te le
        plus proche de (lat, lon) â projection en degrÃ©s lat/lon,
        suffisante pour dÃ©partager deux points voisins."""
        meilleur = None
        for i in range(len(self.points_courants) - 1):
            p1, p2 = self.points_courants[i], self.points_courants[i + 1]
            d, ratio = self._distance_tap_segment(
                lat, lon, p1['lat'], p1['lon'], p2['lat'], p2['lon'])
            if meilleur is None or d < meilleur[0]:
                meilleur = (d, i, ratio)
        if meilleur is None:
            return None, None
        return meilleur[1], meilleur[2]

    def ajouter_point_sur_trace(self, lat, lon, index_segment, ratio):
        """CrÃ©e un point AJOUTÃ (disque orange draggable) Ã  (lat, lon)
        et l'INSÃRE DANS LA TRACE elle-mÃªme (self.points_courants),
        entre le point index_segment et le point index_segment+1 : la
        trace possÃ¨de donc un VRAI sommet Ã  cet endroit, et dÃ©placer
        le point orange DÃFORME la trace (elle suit le doigt, en
        direct). Horodatage et altitude par dÃ©faut Ã MI-CHEMIN entre
        les deux points encadrants (ratio 0,5), indÃ©pendamment de la
        position exacte du tap sur le segment."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        if index_segment < 0 or index_segment + 1 >= len(self.points_courants):
            index_segment = max(0, len(self.points_courants) - 2)
        # Horodatage et altitude TOUJOURS Ã MI-CHEMIN (ratio 0,5)
        # entre les deux points encadrants â quelle que soit la
        # position exacte du tap sur le segment (choix demandÃ© : le
        # Â« ratio Â» du tap ne sert qu'Ã  INSÃRER le point au bon endroit
        # dans la trace, pas Ã  horodater).
        heure = self._horodatage_interpole(index_segment, 0.5)
        # ALTITUDE interpolÃ©e elle aussi Ã MI-CHEMIN entre les deux
        # points encadrants (mÃªme rÃ¨gle que l'horodatage) : le point
        # ajoutÃ© possÃ¨de une altitude cohÃ©rente avec la trace,
        # affichÃ©e dans son popup et Ã©crite dans le GPX enregistrÃ©.
        # None si les deux voisins n'ont pas d'altitude.
        altitude = self._altitude_interpolee(index_segment, 0.5)
        nouveau_point = {'lat': lat, 'lon': lon, 'ele': altitude, 'time': heure}
        position = index_segment + 1
        self.points_courants.insert(position, nouveau_point)
        # Les autres points ajoutÃ©s situÃ©s APRÃS l'insertion voient
        # leur index dÃ©calÃ© d'un cran.
        for point in self.points_ajoutes:
            if point['index'] >= position:
                point['index'] += 1
        marqueur = MarqueurPointAjout(
            map_view=self.map_view,
            zoom=self.map_view.zoom,
            cote_dp=18,
            couleur=(0.95, 0.55, 0.05, 1),  # orange, comme le flag de boucle
            lat=lat, lon=lon,
            on_deplacement=self._sur_deplacement_point,
            on_fin_deplacement=self._sur_fin_deplacement_point,
            on_clic=self._clic_point_ajoute,
            # Clic droit (PC) : suppression totale du point ajoutÃ©.
            on_suppression=self._supprimer_point_ajoute,
        )
        self.map_view.add_marker(marqueur)
        self.points_ajoutes.append({'index': position, 'marqueur': marqueur})
        # La trace est redessinÃ©e AVEC le nouveau sommet ; les
        # marqueurs restent au-dessus du calque de trace.
        self._rafraichir_trace()
        # Affiche immÃ©diatement les infos (GPS/EXIF/altitude) du point
        # crÃ©Ã© et de ses voisins dans le label de l'onglet.
        self._maj_infos_point(position)

    def _point_ajoute_de(self, marqueur):
        """EntrÃ©e de self.points_ajoutes correspondant au marqueur."""
        return next((p for p in self.points_ajoutes
                      if p.get('marqueur') is marqueur), None)

    def _rafraichir_trace(self):
        """Redessine la polyligne de la trace avec les sommets actuels
        de self.points_courants (points du GPX + points ajoutÃ©s), et
        remonte le calque des marqueurs au-dessus."""
        if self.trace_layer is not None and self.map_view is not None:
            self.trace_layer.set_points(
                [(p['lat'], p['lon']) for p in self.points_courants])
        self._remonte_calque_marqueurs()

    def _sur_deplacement_point(self, marqueur):
        """Le point orange est EN TRAIN d'Ãªtre glissÃ© : son sommet dans
        la trace est mis Ã  jour au doigt, et la polyligne est
        redessinÃ©e de faÃ§on THROTLÃE (au plus toutes les 0,06 s) â la
        trace SUIVT le doigt en direct, sans coÃ»ter un redessin complet
        Ã  chaque frame sur les traces longues."""
        entree = self._point_ajoute_de(marqueur)
        if entree is None:
            return
        idx = entree['index']
        if not (0 <= idx < len(self.points_courants)):
            return
        self.points_courants[idx]['lat'] = marqueur.lat
        self.points_courants[idx]['lon'] = marqueur.lon
        maintenant = Clock.get_time()
        if maintenant - getattr(self, "_dernier_trace_redraw", 0.0) >= 0.06:
            self._dernier_trace_redraw = maintenant
            self._rafraichir_trace()

    def _sur_fin_deplacement_point(self, marqueur):
        """Fin du glisser d'un point ajoutÃ© : le sommet est dÃ©jÃ  Ã  jour
        dans la trace (mis Ã  jour frame par frame) ; on redessine la
        trace une derniÃ¨re fois en garanti, et on RÃ-HORODATE le point
        par rapport aux deux points de trace VOISINS (index-1 et
        index+1 : ses nouveaux encadrants dans la trace dÃ©formÃ©e)."""
        entree = self._point_ajoute_de(marqueur)
        if entree is None:
            return
        idx = entree['index']
        if not (0 <= idx < len(self.points_courants)):
            return
        self.points_courants[idx]['lat'] = marqueur.lat
        self.points_courants[idx]['lon'] = marqueur.lon
        self._rafraichir_trace()
        # RÃ©-horodatage (et rÃ©-altitude) entre les nouveaux voisins
        # de la trace dÃ©formÃ©e.
        if 0 < idx < len(self.points_courants) - 1:
            p_avant = self.points_courants[idx - 1]
            p_apres = self.points_courants[idx + 1]
            if p_avant['time'] is not None and p_apres['time'] is not None:
                # Ratio 0,5 : le point ajoutÃ© reste Ã  mi-chemin
                # temporellement entre ses deux nouveaux voisins.
                self.points_courants[idx]['time'] = \
                    p_avant['time'] + (p_apres['time'] - p_avant['time']) * 0.5
            if p_avant['ele'] is not None and p_apres['ele'] is not None:
                # Idem pour l'altitude : mi-chemin entre les voisins.
                self.points_courants[idx]['ele'] = \
                    p_avant['ele'] + (p_apres['ele'] - p_avant['ele']) * 0.5
        elif idx > 0 and self.points_courants[idx - 1]['time'] is not None:
            # Point en bout de trace : mÃªme horodatage que son voisin.
            self.points_courants[idx]['time'] = self.points_courants[idx - 1]['time']
            if self.points_courants[idx - 1]['ele'] is not None:
                self.points_courants[idx]['ele'] = self.points_courants[idx - 1]['ele']
        # RafraÃ®chit le label permanent avec les nouvelles infos du
        # point dÃ©placÃ© et de ses voisins.
        self._maj_infos_point(idx)

    def _infos_autour_du_point(self, idx):
        """CLIC GAUCHE sur un point (ajoutÃ© OU point de trace) :
        affiche dans le SEUL BLOC CENTRAL (intitulÃ© changÃ©) les
        coordonnÃ©es GPS, l'horodatage EXIF et l'altitude du POINT
        SÃLECTIONNÃ uniquement â pas ce qu'il y a autour. Les deux
        autres blocs sont effacÃ©s."""
        if not (0 <= idx < len(self.points_courants)):
            return
        p = self.points_courants[idx]
        self.info_avant_text = ""
        self.info_apres_text = ""
        self.info_ajout_text = (
            "Point sÃ©lectionnÃ©\n"
            f"(nÂ°{idx + 1}/{len(self.points_courants)})\n"
            + self._texte_point(p))

    # ------------------------------------------------------------------
    # CLIC GAUCHE / CLIC DROIT sur les points de trace (disques rouges).
    # ------------------------------------------------------------------
    def _index_du_point(self, point_dict):
        """Index (par IDENTITÃ, pas par valeur : deux points peuvent
        avoir des coordonnÃ©es identiques) du dict de point dans la
        trace, ou None s'il n'en fait plus partie (dÃ©jÃ  supprimÃ©)."""
        for i, p in enumerate(self.points_courants):
            if p is point_dict:
                return i
        return None

    def _clic_point_trace(self, marqueur):
        """CLIC GAUCHE sur un disque rouge de la trace : affiche les
        infos des points entourant ce point dans le bloc central."""
        idx = self._index_du_point(marqueur._point)
        if idx is not None:
            self._infos_autour_du_point(idx)

    def _retirer_marqueur_sans_fantome(self, marqueur):
        """Retire un marqueur de la carte en garantissant qu'il ne
        reste AUCUN rÃ©sidu visuel : le retrait standard, PLUS une
        purge de la liste interne du calque de marqueurs (c'est elle
        qui repose Ã  l'Ã©cran les marqueurs Â« disparus Â» quand elle
        n'est pas vidÃ©e), PLUS l'effacement du canvas du marqueur â
        mÃªme si une structure interne de mapview rÃ©sistait au retrait,
        il ne resterait alors plus rien Ã  dessiner. DÃ©fensif : ne
        lÃ¨ve jamais."""
        if marqueur is None or self.map_view is None:
            return
        # 1. Retrait standard.
        try:
            self.map_view.remove_marker(marqueur)
        except Exception:
            pass
        # 2. Purge de la liste de marqueurs de TOUS les calques.
        for couche in list(getattr(self.map_view, "_layers", None) or []):
            marqueurs = getattr(couche, "markers", None)
            if marqueurs is not None and marqueur in marqueurs:
                try:
                    marqueurs.remove(marqueur)
                except Exception:
                    pass
            try:
                if marqueur.parent is couche:
                    couche.remove_widget(marqueur)
            except Exception:
                pass
        try:
            marqueur._layer = None
        except Exception:
            pass
        # 3. Plus rien Ã  dessiner, quoi qu'il arrive.
        try:
            marqueur.canvas.clear()
        except Exception:
            pass

    def _supprimer_point_trace(self, marqueur):
        """CLIC DROIT sur un disque rouge : supprime entiÃ¨rement ce
        point de la trace, retire son disque et redessine la
        polyligne. Les infos affichÃ©es sont effacÃ©es."""
        point_dict = marqueur._point
        idx = self._index_du_point(point_dict)
        if idx is None:
            return
        # MÃ©morise la suppression pour le bouton Â« Annuler Â».
        self._suppressions_annulables.append(
            {'index': idx, 'point': point_dict, 'type': 'trace'})
        self.suppression_annulable = True
        # Retire le point de la trace...
        self.points_courants.pop(idx)
        # ...retire son disque de la carte...
        self._retirer_marqueur_sans_fantome(marqueur)
        if marqueur in self.marqueurs_points:
            self.marqueurs_points.remove(marqueur)
        # ...les index des points AJOUTÃS situÃ©s aprÃ¨s sont dÃ©calÃ©s.
        for point in self.points_ajoutes:
            if point['index'] > idx:
                point['index'] -= 1
        # Redessine la trace sans le point supprimÃ©.
        self._rafraichir_trace()
        self.info_avant_text = ""
        self.info_ajout_text = ""
        self.info_apres_text = ""

    def _supprimer_point_ajoute(self, marqueur):
        """CLIC DROIT sur un point AJOUTÃ (disque orange) : le retire
        totalement â de la trace, de la carte et de la liste des
        points ajoutÃ©s â puis redessine la polyligne et efface les
        infos affichÃ©es."""
        entree = self._point_ajoute_de(marqueur)
        if entree is None:
            return
        idx = entree['index']
        # MÃ©morise la suppression pour le bouton Â« Annuler Â» (le point
        # dict est conservÃ© : le marqueur sera recrÃ©Ã© Ã  l'annulation).
        if 0 <= idx < len(self.points_courants):
            self._suppressions_annulables.append(
                {'index': idx, 'point': self.points_courants[idx], 'type': 'ajoute'})
            self.suppression_annulable = True
        if 0 <= idx < len(self.points_courants):
            self.points_courants.pop(idx)
        # DÃ©cale les index des autres points ajoutÃ©s situÃ©s aprÃ¨s.
        for point in self.points_ajoutes:
            if point['index'] > idx:
                point['index'] -= 1
        self.points_ajoutes.remove(entree)
        self._retirer_marqueur_sans_fantome(marqueur)
        self._rafraichir_trace()
        self.info_avant_text = ""
        self.info_ajout_text = ""
        self.info_apres_text = ""

    def annuler_derniere_suppression(self):
        """Bouton Â« Annuler la suppression Â» : restaure la DERNIÃRE
        suppression UNE Ã UNE (pas toutes d'un coup) : le point est
        rÃ©insÃ©rÃ© Ã  son index d'origine dans la trace, les index des
        autres points ajoutÃ©s sont recalÃ©s, et s'il s'agissait d'un
        point AJOUTÃ (orange), son marqueur est recrÃ©Ã©. La trace est
        redessinÃ©e et le point restaurÃ© est affichÃ© dans le bloc
        central. Vide la pile pile vide : ne fait rien."""
        if not self._suppressions_annulables:
            self.suppression_annulable = False
            return
        suppression = self._suppressions_annulables.pop()
        self.suppression_annulable = bool(self._suppressions_annulables)

        point_dict = suppression['point']
        idx = min(suppression['index'], len(self.points_courants))
        # RÃ©insÃ¨re le point Ã  sa place d'origine.
        self.points_courants.insert(idx, point_dict)
        # Recale les index des points AJOUTÃS situÃ©s aprÃ¨s.
        for point in self.points_ajoutes:
            if point['index'] >= idx:
                point['index'] += 1
        # S'il s'agissait d'un point AJOUTÃ : recrÃ©e son marqueur
        # orange draggable et sa entrÃ©e dans points_ajoutes.
        if suppression['type'] == 'ajoute' and self.map_view is not None:
            marqueur = MarqueurPointAjout(
                map_view=self.map_view,
                zoom=self.map_view.zoom,
                cote_dp=18,
                couleur=(0.95, 0.55, 0.05, 1),
                lat=point_dict['lat'], lon=point_dict['lon'],
                on_deplacement=self._sur_deplacement_point,
                on_fin_deplacement=self._sur_fin_deplacement_point,
                on_clic=self._clic_point_ajoute,
                on_suppression=self._supprimer_point_ajoute,
            )
            self.map_view.add_marker(marqueur)
            self.points_ajoutes.append({'index': idx, 'marqueur': marqueur})
        # Redessine la trace avec le point restaurÃ© et repose les
        # disques rouges (le point restaurÃ© en rÃ©cupÃ¨re un).
        self._rafraichir_trace()
        if self.map_view is not None:
            self._maj_disques_points()
        # Affiche le point restaurÃ© dans le bloc central.
        self._infos_autour_du_point(idx)

    @staticmethod
    def _texte_point(p, numero=None):
        """Ligne d'info d'un point : GPS, horodatage EXIF, altitude.
        UtilisÃ© par le popup des points ajoutÃ©s (point lui-mÃªme et
        points voisins de la trace) et par les blocs de l'onglet."""
        prefixe = f"Point {numero}\n" if numero is not None else ""
        heure = (p['time'].strftime("%d/%m/%Y %H:%M:%S")
                 if p['time'] else "inconnu (sans horodatage)")
        alt = f"{p['ele']:.1f} m" if p['ele'] is not None else "inconnue"
        return (f"{prefixe}"
                f"GPS : {p['lat']:.5f}, {p['lon']:.5f}\n"
                f"Horodatage : {heure}\n"
                f"Altitude : {alt}")

    def _maj_infos_point(self, idx):
        """Remplit les TROIS BLOCS cÃ´te Ã  cÃ´te de l'onglet, dans
        l'ordre : point PRÃCÃDENT (colonne de gauche), POINT AJOUTÃ
        (colonne centrale, en gras), point SUIVANT (colonne de
        droite). Chaque bloc donne les coordonnÃ©es GPS, l'horodatage
        EXIF et l'altitude. AppelÃ© Ã  la crÃ©ation du point, Ã  la fin
        de son dÃ©placement, et au tap dessus (en plus du popup)."""
        if not (0 <= idx < len(self.points_courants)):
            return
        p = self.points_courants[idx]
        # Bloc central : le point ajoutÃ©.
        self.info_ajout_text = self._texte_point(
            p, numero=f"ajoutÃ©\n(nÂ°{idx + 1}/{len(self.points_courants)})")
        # Bloc gauche : le point prÃ©cÃ©dent (vide si premier point).
        self.info_avant_text = (
            "Point prÃ©cÃ©dent\n" +
            self._texte_point(self.points_courants[idx - 1], numero=idx)
            if idx > 0 else "Point prÃ©cÃ©dent\n(aucun : dÃ©but de trace)")
        # Bloc droit : le point suivant (vide si dernier point).
        self.info_apres_text = (
            "Point suivant\n" +
            self._texte_point(self.points_courants[idx + 1], numero=idx + 2)
            if idx < len(self.points_courants) - 1
            else "Point suivant\n(aucun : fin de trace)")

    def _clic_point_ajoute(self, marqueur):
        """Simple tap sur un point AJOUTÃ (disque orange) : PAS de
        popup â les popups restent rÃ©servÃ©s aux vraies annotations
        (waypoints avec photos). Comme pour un clic sur un disque
        rouge de la trace, seule la zone d'infos de l'onglet est mise
        Ã  jour avec ce point (bloc central, autres blocs effacÃ©s)."""
        entree = self._point_ajoute_de(marqueur)
        if entree is None:
            return
        self._infos_autour_du_point(entree['index'])

    # ------------------------------------------------------------------
    # ENREGISTREMENT DE LA TRACE MODIFIÃE (GPX).
    # ------------------------------------------------------------------
    def enregistrer_trace_modifiee(self):
        """Bouton Â« Enregistrer la trace modifiÃ©e Â» : Ã©crit la trace
        actuelle de l'onglet (points du fichier source + points
        ajoutÃ©s/dÃ©placÃ©s par l'utilisateur, avec leurs horodatages et
        altitudes interpolÃ©es) dans un fichier GPX
        Â« <nom>_modifie.gpx Â» du dossier de sortie, SANS toucher au
        fichier source. L'Ã©criture se fait dans un thread pour ne pas
        figer l'interface sur les longues traces."""
        if not self.trace_chargee or not self.points_courants:
            return
        threading.Thread(target=self._enregistrement_thread, daemon=True).start()

    def _enregistrement_thread(self):
        try:
            base = os.path.splitext(os.path.basename(self.fichier_source))[0]
            dossier = DOSSIER_SORTIE if os.path.isdir(DOSSIER_SORTIE) \
                else os.path.dirname(self.fichier_source)
            chemin = os.path.join(dossier, f"{base}_modifie.gpx")
            compteur = 1
            while os.path.exists(chemin):  # ne jamais Ã©craser un fichier existant
                chemin = os.path.join(dossier, f"{base}_modifie_{compteur}.gpx")
                compteur += 1

            gpx = ET.Element("gpx", {
                "version": "1.1",
                "creator": "Bubu GPS â onglet Ajout",
                "xmlns": "http://www.topografix.com/GPX/1/1",
                "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
                "xsi:schemaLocation":
                    "http://www.topografix.com/GPX/1/1 "
                    "http://www.topografix.com/GPX/1/1/gpx.xsd",
            })
            trk = ET.SubElement(gpx, "trk")
            ET.SubElement(trk, "name").text = base + " (modifiÃ©e)"
            seg = ET.SubElement(trk, "trkseg")
            for p in self.points_courants:
                attrs = {"lat": f"{p['lat']:.7f}", "lon": f"{p['lon']:.7f}"}
                trkpt = ET.SubElement(seg, "trkpt", attrs)
                if p.get('ele') is not None:
                    ET.SubElement(trkpt, "ele").text = f"{p['ele']:.1f}"
                if p.get('time') is not None:
                    # Horodatage GPX standard (ISO 8601, UTC Â« Z Â»
                    # comme dans les fichiers GPSLogger).
                    heure = p['time']
                    if heure.tzinfo is not None:
                        # RamÃ¨ne l'horodatage en UTC avant l'Ã©criture.
                        from datetime import timezone as _tz
                        heure = heure.astimezone(_tz.utc)
                    ET.SubElement(trkpt, "time").text = \
                        heure.strftime("%Y-%m-%dT%H:%M:%S") + "Z"

            ET.indent(gpx, space="  ")  # Python 3.9+ : fichier lisible
            ET.ElementTree(gpx).write(chemin, encoding="utf-8",
                                      xml_declaration=True)
            message = f"Trace enregistrÃ©e :\n{chemin}"
        except Exception as e:
            message = f"Ãchec de l'enregistrement : {e}"

        def _afficher(dt):
            contenu = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(10))
            lbl = Label(text=message, size_hint_y=None,
                        text_size=(dp(280), None), halign="left", valign="middle")
            lbl.bind(texture_size=lambda w, v: setattr(w, "height", v[1]))
            btn = Button(text="Fermer", size_hint_y=None, height=dp(44))
            contenu.add_widget(lbl)
            contenu.add_widget(btn)
            pop = Popup(title="Enregistrer la trace", content=contenu,
                        size_hint=(0.85, 0.45))
            btn.bind(on_release=pop.dismiss)
            pop.open()
        Clock.schedule_once(_afficher, 0)


class StatistiquesScreen(Screen):
    """Onglet Statistiques : copie de travail de l'onglet Découpe,
    SANS le bloc de découpe de trace (zone de saisie + bouton
    « Couper ici »). Le reste est identique : carte, graphique
    d'altitude + vitesse, infos du point sélectionné."""
    fichier_source = StringProperty("")
    info_fichier = StringProperty("Aucune trace chargée.")
    trace_chargee = BooleanProperty(False)
    point_coupure_text = StringProperty("")
    status_text = StringProperty("")
    status_color = ListProperty([0.33, 0.33, 0.33, 1])
    en_cours = BooleanProperty(False)
    info_point_text = StringProperty("")
    # Bloc "Informations du point sélectionné" (grille 2 colonnes :
    # Point, Distance, Heure et Pente à gauche ; GPS, Altitude,
    # et Vitesse à droite.)
    info_point_num = StringProperty("")
    info_point_gps = StringProperty("")
    info_point_dist = StringProperty("")
    info_point_alt = StringProperty("")
    info_point_heure = StringProperty("")
    info_point_vit = StringProperty("")
    info_point_pente = StringProperty("")

    # Libellés du tableau de statistiques (mêmes clés que
    # gps_logic.calculer_statistiques) : 10 lignes, 2 colonnes de 5.
    LIBELLES_STATS = [
        ("alt_depart", "Altitude de départ :"),
        ("alt_max", "Altitude maximale :"),
        ("distance", "Distance parcourue :"),
        ("den_pos", "Dénivelé positif :"),
        ("km_effort", "Kilomètre-Effort :"),
        ("temps_total", "Temps total :"),
        ("temps_marche", "Temps sans pauses :"),
        ("vit_moy", "Vitesse moyenne :"),
        ("allure", "Allure moyenne :"),
        ("waypoints", "Waypoints :"),
    ]

    def dezoomer_carte(self):
        """Réduit le niveau de zoom de la carte si la carte est chargée."""
        # 1. Vérifie si self.mapview existe déjà
        mapview = getattr(self, "mapview", None)

        # 2. Sinon, cherche l'instance de la carte directement dans l'un des enfants du container
        if not mapview and "map_container" in self.ids:
            for child in self.ids.map_container.children:
                if hasattr(child, "zoom"):
                    mapview = child
                    break

        # 3. Applique le dézoom si la carte est trouvée
        if mapview and hasattr(mapview, "zoom"):
            min_z = getattr(getattr(mapview, "map_source", None), "min_zoom", 0)
            if mapview.zoom > min_z:
                mapview.zoom -= 1
                mapview.center_on(mapview.lat, mapview.lon)

    def zoomer_carte(self):
        """Augmente le niveau de zoom de la carte si la carte est chargée."""
        mapview = getattr(self, "mapview", None)

        if not mapview and "map_container" in self.ids:
            for child in self.ids.map_container.children:
                if hasattr(child, "zoom"):
                    mapview = child
                    break

        if mapview and hasattr(mapview, "zoom"):
            max_z = getattr(getattr(mapview, "map_source", None), "max_zoom", 19)
            if mapview.zoom < max_z:
                mapview.zoom += 1
                mapview.center_on(mapview.lat, mapview.lon)
                
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.points_courants = []
        self.marqueurs_actifs = []
        self.marqueurs_waypoints = []   # curseurs bleus des waypoints (comme Photos/Live)
        self.marqueur_curseur = None
        self.trace_layer = None
        self.map_view = None
        self.profil = ([], [], [], [])

        self.graphe = GrapheProfil()
        self.graphe.callback_clic = self._sur_clic_graphique
        self.ids.zone_graphique.add_widget(self.graphe)
        # Courbe de vitesse (avec son axe et sa légende) et bandes
        # de pentes : MASQUÉES par défaut sur le graphique
        # d'altitude ; rajoutées par les cases à cocher
        # « Vitesses » / « Pentes » placées juste en dessous.
        self.graphe.afficher_courbe_vitesse = False
        self.graphe.afficher_axe_vitesse = False
        # Lignes pointillées de sélection en ROSE (couleur du disque
        # curseur de la carte) sur les DEUX graphiques de cet onglet.
        self.graphe.couleur_curseur = COULEUR_ROSE_CURSEUR

        if CARTE_DISPONIBLE:
            self.map_view = MapViewMolette(zoom=6, lat=46.603354, lon=1.888334, map_source=SOURCE_SATELLITE)
            # On écoute les touchers au niveau de la Window, complètement
            # à l'écart du Scatter interne de MapView (qui gère lui-même
            # le glisser/pincement). Un binding ou un grab sur le Scatter
            # ou sur MapView empêcherait ce dernier de recevoir l'événement
            # et bloquerait le glisser — ce qu'on a observé en pratique.
            Window.bind(on_touch_down=self._debut_touch_carte, on_touch_up=self._sur_touch_carte)
            self.ids.map_container.add_widget(self.map_view)
            # La taille des curseurs de waypoints suit le zoom de la carte.
            self.map_view.bind(zoom=self._maj_taille_waypoints)
        else:
            self.ids.map_container.add_widget(Label(
                text=(
                    "Carte indisponible : le module kivy_garden.mapview\n"
                    "n'est pas installe.\n\nInstalle-le avec :\n"
                    "pip install kivy_garden.mapview"
                ),
                color=(0.6, 0.1, 0.1, 1),
                halign="center",
            ))

    def changer_vue_carte(self, valeur):
        """Change le fond de carte (satellite ou plan), equivalent de
        changer_vue_carte() dans la version desktop."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        self.map_view.map_source = SOURCES_FONDS_CARTES[valeur]
        # L'affectation seule ne suffit pas toujours à relancer le
        # chargement des tuiles : on force explicitement un rafraîchissement
        # complet (sinon le fond peut rester gris-bleu / ne pas revenir).
        self.map_view.trigger_update(True)

    def ouvrir_menu_fonds(self, bouton):
        """Ouvre le menu déroulant compact des fonds de carte sous le
        bouton carré "Layer" (satellite par défaut, vue courante
        marquée d'un point). Voir _construire_menu_fonds_carte."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        menu = _construire_menu_fonds_carte(self)
        menu.open(bouton)

    def ouvrir_selecteur_fichier(self):
        contenu = _construire_selecteur_fichier(self._fichier_choisi)
        if contenu is not None:
            self._popup = Popup(title="Choisir un fichier", content=contenu, size_hint=(0.95, 0.95))
            self._popup.open()

    def _fichier_choisi(self, chemin):
        if hasattr(self, '_popup'):
            self._popup.dismiss()
        if not chemin:
            return
        self.charger_trace(chemin)

    def _basculer_vitesses(self, active):
        """Case à cocher « Vitesses » : rajoute (ou enlève) la courbe
        de vitesse, son axe et sa légende sur le graphique
        d'altitude. Décochée par défaut : altitude seule."""
        self.graphe.afficher_courbe_vitesse = bool(active)
        self.graphe.afficher_axe_vitesse = bool(active)
        self.graphe._redessiner()

    def _basculer_pentes(self, active):
        """Case à cocher « Pentes » : rajoute (ou enlève) les bandes
        de pente — mêmes classes de couleurs que le graphique des
        pentes — sous la courbe d'altitude du premier graphique.
        Décochée par défaut."""
        self.graphe.afficher_pentes = bool(active)
        self.graphe._redessiner()

    def charger_trace(self, chemin):
        """Charge une trace GPX/KMZ/KML dans cet onglet. Utilisée à la
        fois par le sélecteur de fichier interne (_fichier_choisi
        ci-dessus) et par l'ouverture d'un fichier externe via Android
        (association de fichiers .gpx/.kml/.kmz, "Ouvrir avec" → Bubu
        GPS), voir OutilsTracesApp._sur_nouvel_intent."""
        if not chemin:
            return
        try:
            points = gps_logic.lire_fichier_pour_conversion(chemin)
            
            # ---> AJOUT : Lecture des waypoints de la source (nécessaire pour l'affichage)
            waypoints = gps_logic.lire_waypoints_source(chemin, heure_locale=False)
        except Exception as e:
            self.trace_chargee = False
            self.info_fichier = f"Erreur de lecture : {e}"
            return

        if not points:
            self.trace_chargee = False
            self.info_fichier = "Aucun point GPS trouvé dans ce fichier."
            return

        self.fichier_source = chemin
        self.points_courants = points
        self.trace_chargee = True
        self.point_coupure_text = ""
        self.status_text = ""
        
        # Même règle que les onglets Photos/Live : ni n° de
        # points (nom uniquement en chiffres), ni waypoints superposés au
        # départ ou à l'arrivée de la trace.
        nb_points = len(points)
        vrais_wpts = gps_logic.vrais_waypoints(
            waypoints, [(points[0]['lat'], points[0]['lon']), (points[-1]['lat'], points[-1]['lon'])])
        nb_waypoints = len(vrais_wpts)
        self.info_fichier = f"Trace : {os.path.basename(chemin)}"

        self.info_point_text = "Tape sur la carte ou le graphique pour voir le détail d'un point."
        self.info_point_num = ""
        self.info_point_gps = ""
        self.info_point_dist = ""
        self.info_point_alt = ""
        self.info_point_heure = ""
        self.info_point_vit = ""
        self.info_point_pente = ""
        # Statistiques de la trace (calculs de gps_logic
        # gps_logic.calculer_statistiques), tableau 2 colonnes x 5
        # lignes au-dessus de la carte.
        self._afficher_stats_trace(points, waypoints)
        self.profil = gps_logic.calculer_profil(points)
        self.graphe.set_donnees(*self.profil)
        self._afficher_trace_sur_carte(points, waypoints=vrais_wpts)
        self._afficher_pentes(points)

    def _afficher_stats_trace(self, points, waypoints):
        """Remplit le tableau de statistiques : 2 colonnes de 5 lignes,
        chaque ligne « libellé : valeur » reprenant EXACTEMENT la mise
        en forme du bloc « Informations du point sélectionné » (labels
        12sp, texte noir, taille ajustée au contenu). Mêmes calculs
        gps_logic.calculer_statistiques)."""
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.label import Label as LabelKv

        stats = gps_logic.calculer_statistiques(points, waypoints)
        # Le kilomètre-effort est renvoyé par gps_logic sous la forme
        # « 38.36 km-effort » : on garde le nombre et on abrège
        # l'unité en KE (affichage : Kilomètre-Effort : 38.36 KE).
        valeurs = []
        for cle, libelle in self.LIBELLES_STATS:
            val = str(stats.get(cle, "-"))
            if cle == "km_effort" and val != "-":
                try:
                    val = f"{float(val.split()[0]):.2f} KE"
                except (ValueError, IndexError):
                    val = val + " KE"
            valeurs.append((libelle, val))
        # 5 premières stats à gauche, 5 suivantes à droite.
        for colonne_id, trio in zip(
            ("stats_gauche", "stats_droite"),
            (valeurs[:5], valeurs[5:]),
        ):
            colonne = self.ids[colonne_id]
            colonne.clear_widgets()
            for libelle, valeur in trio:
                # Libellé + valeur sur la même ligne, comme les labels
                # « Point 1/230 », « Distance : 12.4 km » du bloc
                # d'infos du point (12sp, noir). La texture est
                # rafraîchie AVANT de lire texture_size, sinon la
                # taille vaut (0, 0) et les labels se superposent.
                lbl = LabelKv(
                    text=f"{libelle} {valeur}",
                    size_hint=(None, None), font_size="12sp",
                    color=(0, 0, 0, 1),
                )
                lbl.texture_update()
                lbl.size = lbl.texture_size
                colonne.add_widget(lbl)
    def _afficher_pentes(self, points):

        """Calcule les tranches de 500 m et alimente le PREMIER
        graphique (option « Pentes » : bandes sous la courbe
        d'altitude)."""
        tranches = self._calculer_tranches_pentes(points, pas_m=500.0)
        self._tranches_pentes = tranches
        # Altitudes min/max réelles sur TOUS les points de la trace
        # (cohérence avec le graphique d'altitude au-dessus).
        altitudes = [p['ele'] for p in points if p.get('ele') is not None]
        alt_min = min(altitudes) if altitudes else None
        alt_max = max(altitudes) if altitudes else None
        # Distance cumulée TOTALE de la trace (km) : borne exacte de
        # l'axe X du graphique, pour que le curseur de sélection reste
        # aligné avec le graphique d'altitude (la dernière tranche est
        # le plus souvent tronquée : 10,3 km de trace ≠ axe de 10,5 km).
        # En même temps : liste (distance_km, altitude) de TOUS les
        # points GPS avec altitude — la courbe du graphique des pentes
        # est tracée à partir d'eux pour être identique au profil
        # (sinon, échantillonnée aux bornes de 500 m, elle paraît lissée).
        dist_totale = 0.0
        points_courbe = []
        precedent = None
        for p in points:
            if precedent is not None:
                dist_totale += gps_logic.calculer_distance_haversine(
                    precedent['lat'], precedent['lon'], p['lat'], p['lon'])
            precedent = p
            if p.get('ele') is not None:
                points_courbe.append((dist_totale / 1000.0, p['ele']))
        # Mêmes tranches pour le PREMIER graphique (option
        # « Pentes » : bandes sous la courbe d'altitude) : la
        # case à cocher décide si elles sont dessinées ou non.
        self.graphe.set_pentes(tranches, points_courbe)

    def _calculer_tranches_pentes(self, points, pas_m=500.0):
        """Découpe la trace en tranches de 500 m (dernière tronquée).
        Pour chaque tranche : distance cumulée de début (km), pente
        MOYENNE (%, = (alt_fin - alt_début) / distance réelle), altitude
        de début et de fin. Retourne une liste
        [(dist_debut_km, pente_pct, alt_dep, alt_arr), ...]."""
        # Points avec altitude, distances cumulées.
        pts = []
        dist_cum = 0.0
        precedent = None
        for p in points:
            if precedent is not None:
                dist_cum += gps_logic.calculer_distance_haversine(
                    precedent['lat'], precedent['lon'], p['lat'], p['lon'])
            precedent = p
            if p.get('ele') is not None:
                pts.append((dist_cum, p['ele']))
        if len(pts) < 2:
            return []

        def _alt_a(distance_m):
            """Altitude interpolée au mètre donné (les traces ont peu
            de points, un balayage simple suffit)."""
            for i in range(1, len(pts)):
                if pts[i][0] >= distance_m:
                    d0, a0 = pts[i - 1]
                    d1, a1 = pts[i]
                    if d1 > d0:
                        ratio = (distance_m - d0) / (d1 - d0)
                        return a0 + (a1 - a0) * ratio
                    return a0
            return pts[-1][1]

        tranches = []
        dist_fin = pts[-1][0]
        nb_tranches = max(1, int(dist_fin // pas_m) + (1 if dist_fin % pas_m > 1.0 else 0))
        for i in range(nb_tranches):
            d0 = i * pas_m
            d1 = min((i + 1) * pas_m, dist_fin)
            if d1 - d0 < 1.0:
                break
            a0 = _alt_a(d0)
            a1 = _alt_a(d1)
            pente = (a1 - a0) / (d1 - d0) * 100.0
            tranches.append((d0 / 1000.0, round(pente, 1), round(a0, 1), round(a1, 1)))
        return tranches

    def _remonte_calque_marqueurs(self):
        """Remonte le calque des marqueurs AU-DESSUS du calque de trace,
        via l'API publique de MapView (remove_layer/add_layer). Voir la
        version commentée identique dans NettoyageScreen. Nécessaire dès
        qu'une trace est re-posée alors que le calque de marqueurs
        existe déjà (re-chargement d'une trace dans l'onglet)."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        couche = getattr(self.map_view, "_marker_layer", None)
        if couche is None:
            for l in getattr(self.map_view, "_layers", []) or []:
                if isinstance(l, MarkerMapLayer):
                    couche = l
                    break
        if couche is None:
            return
        try:
            self.map_view.remove_layer(couche)
            self.map_view.add_layer(couche)
        except Exception:
            pass

    def _afficher_trace_sur_carte(self, points, waypoints=None):
        """Equivalent de afficher_trace_sur_carte() dans la version
        desktop : trace la polyligne, place les marqueurs D/A, centre
        et zoome la carte sur l'emprise de la trace. Les waypoints
        éventuels sont indiqués par un petit curseur rond et bleu
        (MarqueurWaypoint), comme dans les onglets Photos et Live."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return

        if self.trace_layer is not None:
            self.map_view.remove_layer(self.trace_layer)
            self.trace_layer = None
        for m in self.marqueurs_actifs:
            self.map_view.remove_marker(m)
        self.marqueurs_actifs = []
        for mw in self.marqueurs_waypoints:
            self.map_view.remove_marker(mw)
        self.marqueurs_waypoints = []
        if self.marqueur_curseur is not None:
            self.map_view.remove_marker(self.marqueur_curseur)
            self.marqueur_curseur = None

        if not points:
            return

        liste_coords = [(p['lat'], p['lon']) for p in points]
        # Le calque de la trace est posé AVANT les marqueurs : dans
        # mapview, les marqueurs vivent dans un calque distinct et tout
        # calque ajouté après les recouvre TOUS. Posé en premier, le
        # calque de trace passe sous les marqueurs — le disque rouge du
        # curseur de sélection (et les curseurs de waypoints) s'affichent
        # donc PAR-DESSUS la trace, comme demandé (même choix que
        # l'onglet Nettoyage).
        self.trace_layer = TraceLayer()
        self.trace_layer.set_points(liste_coords)
        self.map_view.add_layer(self.trace_layer)

        for wpt in (waypoints or []):
            lat_w, lon_w = wpt.get('lat'), wpt.get('lon')
            if lat_w is None or lon_w is None:
                continue
            # « Point de passage 1/2 » : ce sont le départ et l'arrivée,
            # traités à part (triangles) juste après la boucle — pas
            # de disque jaune pour eux.
            nom_w = (wpt.get('name') or '').strip()
            if nom_w in ("Point de passage 1", "Point de passage 2"):
                continue
            mw = MarqueurWaypoint(
                zoom=self.map_view.zoom, lat=lat_w, lon=lon_w,
                nom=wpt.get('name'), description=wpt.get('description'),
                on_waypoint_clic=self._sur_clic_waypoint,
            )
            self.map_view.add_marker(mw)
            self.marqueurs_waypoints.append(mw)

        # Triangles départ/arrivée pour « Point de passage 1/2 »
        # (vert / rouge, orange unique si boucle fermée ≤ 20 m).
        _poser_triangles_points_passage(self, waypoints)

        dist_dep_arr = gps_logic.calculer_distance_haversine(
            points[0]['lat'], points[0]['lon'], points[-1]['lat'], points[-1]['lon']
        )
        if dist_dep_arr <= 20.0:
            m_unique = MarqueurFlag(couleur=COULEUR_FLAG_FERMETURE, lat=points[0]['lat'], lon=points[0]['lon'])
            self.map_view.add_marker(m_unique)
            self.marqueurs_actifs.append(m_unique)
        else:
            m_depart = MarqueurFlag(couleur=COULEUR_FLAG_DEPART, lat=points[0]['lat'], lon=points[0]['lon'])
            m_arrivee = MarqueurFlag(couleur=COULEUR_FLAG_ARRIVEE, lat=points[-1]['lat'], lon=points[-1]['lon'])
            self.map_view.add_marker(m_depart)
            self.map_view.add_marker(m_arrivee)
            self.marqueurs_actifs.extend([m_depart, m_arrivee])

        # REMONTÉE DU CALQUE DE MARQUEURS AU-DESSUS DE LA TRACE (même
        # correction que l'onglet Nettoyage, via l'API publique
        # remove_layer/add_layer de MapView — voir là-bas la méthode
        # _remonte_calque_marqueurs pour l'explication complète).
        self._remonte_calque_marqueurs()

        lats = [c[0] for c in liste_coords]
        lons = [c[1] for c in liste_coords]
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)

        self.map_view.center_on((min_lat + max_lat) / 2, (min_lon + max_lon) / 2)
        max_delta = max(max_lat - min_lat, max_lon - min_lon)
        if max_delta > 0:
            zoom = int(12 - math.log2(max_delta * 10))
            self.map_view.zoom = max(2, min(zoom, 18))

    def _maj_taille_waypoints(self, instance, zoom):
        for mw in self.marqueurs_waypoints:
            mw.maj_taille(zoom)
        # Le curseur mobile (disque rose) suit aussi le zoom depuis
        # qu'il est passé sur la même formule de taille que les
        # disques jaunes/rouges (plus de cote_dp fixe).
        if getattr(self, "marqueur_curseur", None) is not None:
            self.marqueur_curseur.maj_taille(zoom)

    def _debut_touch_carte(self, window, touch):
        """Mémorise la position de l'appui si le toucher démarre sur la
        carte, SANS jamais consommer l'événement (pas de grab, pas de
        return True) pour ne surtout pas empêcher MapView de gérer
        normalement le glisser/pincement lui-même."""
        if (
            self.manager is not None and self.manager.current == self.name
            and self.map_view is not None and self.map_view.collide_point(*touch.pos)
        ):
            touch.ud["carte_pos_depart"] = (touch.x, touch.y)
        return False

    def _sur_touch_carte(self, window, touch):
        if self.manager is None or self.manager.current != self.name:
            return False

        depart = touch.ud.get("carte_pos_depart")
        if not CARTE_DISPONIBLE or self.map_view is None or not self.points_courants or depart is None:
            return False
        if abs(touch.x - depart[0]) > dp(8) or abs(touch.y - depart[1]) > dp(8):
            return False  # c'était un glissement (pan/zoom), pas un tap

        zoom = self.map_view.zoom
        cx, cy = gps_logic.projeter_mercator(self.map_view.lat, self.map_view.lon, zoom)
        px = cx + (depart[0] - self.map_view.center_x)
        py = cy - (depart[1] - self.map_view.center_y)
        lat, lon = gps_logic.deprojeter_mercator(px, py, zoom)
        meilleur_idx = None
        meilleure_dist = None
        for i, p in enumerate(self.points_courants):
            d = gps_logic.calculer_distance_haversine(lat, lon, p['lat'], p['lon'])
            if meilleure_dist is None or d < meilleure_dist:
                meilleure_dist = d
                meilleur_idx = i

        if meilleur_idx is None:
            return False

        self._selectionner_point(meilleur_idx, recentrer_carte=False)
        return True

    def _sur_clic_graphique(self, distance_km):
        """Appelé au tap sur le graphique : sélectionne le point dont la
        distance cumulée est la plus proche de la distance tapée
        (équivalent de sur_clic_graphique dans la version desktop, qui
        recentre aussi la carte contrairement à un tap sur la carte)."""
        distances_km = self.profil[0]
        if not distances_km:
            return
        idx = min(range(len(distances_km)), key=lambda i: abs(distances_km[i] - distance_km))
        self._selectionner_point(idx, recentrer_carte=True)


    def _sur_clic_waypoint(self, lat, lon):
        """Tap sur un waypoint (disque jaune) : sélectionne le point de
        la trace le PLUS PROCHE du waypoint — les curseurs des deux
        graphiques et le bloc d'infos se placent dessus — SANS
        recentrer la carte et SANS toucher au popup du waypoint, qui
        s'ouvre ensuite exactement comme avant (le callback est
        appelé AVANT _afficher_popup par MarqueurWaypoint)."""
        if not self.points_courants:
            return
        meilleur_idx = None
        meilleure_dist = None
        for i, p in enumerate(self.points_courants):
            d = gps_logic.calculer_distance_haversine(lat, lon, p['lat'], p['lon'])
            if meilleure_dist is None or d < meilleure_dist:
                meilleure_dist = d
                meilleur_idx = i
        if meilleur_idx is not None:
            self._selectionner_point(meilleur_idx, recentrer_carte=False)

    def _selectionner_point(self, idx, recentrer_carte):
        """Met à jour, en un seul endroit, tout ce qui doit refléter le
        point sélectionné : marqueur curseur sur la carte, numéro de
        découpe, texte d'info, et curseur du graphique."""
        if not (0 <= idx < len(self.points_courants)):
            return
        p = self.points_courants[idx]
        self.point_coupure_text = str(idx + 1)

        if CARTE_DISPONIBLE and self.map_view is not None:
            if self.marqueur_curseur is not None:
                self.map_view.remove_marker(self.marqueur_curseur)
            # Même curseur que l'onglet Nettoyage : disque ROSE dessiné,
            # sans le carré blanc du MapMarker standard.
            self.marqueur_curseur = MarqueurDisqueRouge(
                zoom=self.map_view.zoom,
                couleur=COULEUR_ROSE_CURSEUR,
                lat=p['lat'], lon=p['lon'],
            )
            self.map_view.add_marker(self.marqueur_curseur)
            if recentrer_carte:
                self.map_view.center_on(p['lat'], p['lon'])

        distances_km, _, _, vitesses_kmh = self.profil
        dist = distances_km[idx] if idx < len(distances_km) else 0.0
        vit = vitesses_kmh[idx] if idx < len(vitesses_kmh) else 0.0
        heure = p['time'].strftime("%H:%M:%S") if p['time'] else "-"
        ele_txt = f"{p['ele']} m" if p['ele'] is not None else "-"
        self.info_point_text = ""
        self.info_point_num = f"Point {idx + 1}/{len(self.points_courants)}"
        self.info_point_gps = f"GPS: {p['lat']:.5f}, {p['lon']:.5f}"
        self.info_point_dist = f"Distance: {dist:.2f} km"
        self.info_point_alt = f"Altitude: {ele_txt}"
        self.info_point_heure = f"Heure: {heure}"
        self.info_point_vit = f"Vitesse: {vit} km/h"
        # Pente du point sélectionné : celle de la tranche de 500 m du
        # graphique des pentes qui contient ce point.
        pente_txt = "-"
        tranches_p = getattr(self, "_tranches_pentes", None)
        if tranches_p:
            pente_val = None
            for t in tranches_p:
                if t[0] <= dist + 1e-9:
                    pente_val = t[1]
                else:
                    break
            if pente_val is not None:
                pente_txt = f"{pente_val:+.1f} %"
        self.info_point_pente = f"Pente: {pente_txt}"
        self.graphe.set_selection(dist)


class PhotosScreen(Screen):
    """Onglet Photos : associe une photo JPEG à un point de la trace en
    se basant sur son horodatage EXIF, puis permet d'écrire/corriger les
    tags GPS de la photo. Reprend sans modification fonctionnelle la
    logique de init_onglet6_photos() de la version desktop (le
    formulaire Tkinter devient un écran Kivy)."""

    info_trace = StringProperty("Aucune trace chargée.")
    info_photo = StringProperty("Aucune photo chargée.")
    champ_date = StringProperty("")
    champ_lat = StringProperty("")
    champ_lon = StringProperty("")
    champ_alt = StringProperty("")
    miniature_source = StringProperty("")
    status_text = StringProperty("")
    status_color = ListProperty([0.33, 0.33, 0.33, 1])
    titre_carte = StringProperty("Emplacement de la photo sur la trace")
    titre_carte_color = ListProperty([0, 0, 0, 1])
    photo_chargee = BooleanProperty(False)
    trace_chargee = BooleanProperty(False)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.points_trace = []
        self.fichier_photo = ""
        self.trace_layer = None
        self.marqueurs_actifs = []
        self.marqueurs_waypoints = []   # curseurs bleus des waypoints
        self.marqueur_photo = None
        self.map_view = None

        if CARTE_DISPONIBLE:
            self.map_view = MapViewMolette(zoom=6, lat=46.603354, lon=1.888334, map_source=SOURCE_SATELLITE)
            self.ids.map_container.add_widget(self.map_view)
            # La taille des curseurs de waypoints suit le zoom de la carte.
            self.map_view.bind(zoom=self._maj_taille_waypoints)
        else:
            self.ids.map_container.add_widget(Label(
                text=(
                    "Carte indisponible : le module kivy_garden.mapview\n"
                    "n'est pas installe.\n\nInstalle-le avec :\n"
                    "pip install kivy_garden.mapview"
                ),
                color=(0.6, 0.1, 0.1, 1),
                halign="center",
            ))

    def _maj_taille_waypoints(self, instance, zoom):
        for mw in self.marqueurs_waypoints:
            mw.maj_taille(zoom)

    def dezoomer_carte(self):
        """Réduit le niveau de zoom de la carte (bouton "-", même
        comportement que sur l'onglet Carte/Découpe)."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        min_z = self.map_view.map_source.get_min_zoom()
        if self.map_view.zoom > min_z:
            self.map_view.zoom -= 1
            self.map_view.center_on(self.map_view.lat, self.map_view.lon)

    def zoomer_carte(self):
        """Augmente le niveau de zoom de la carte (bouton "+")."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        max_z = self.map_view.map_source.get_max_zoom()
        if self.map_view.zoom < max_z:
            self.map_view.zoom += 1
            self.map_view.center_on(self.map_view.lat, self.map_view.lon)

    def changer_vue_carte(self, valeur):
        """Change le fond de carte (satellite ou plan), équivalent de
        changer_fond_carte_photo() dans la version desktop."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        self.map_view.map_source = SOURCES_FONDS_CARTES[valeur]
        self.map_view.trigger_update(True)

    def ouvrir_menu_fonds(self, bouton):
        """Ouvre le menu déroulant compact des fonds de carte sous le
        bouton carré "Layer" (satellite par défaut, vue courante
        marquée d'un point). Voir _construire_menu_fonds_carte."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        menu = _construire_menu_fonds_carte(self)
        menu.open(bouton)

    def ouvrir_selecteur_trace(self):
        contenu = _construire_selecteur_fichier(self._trace_choisie)
        if contenu is not None:
            self._popup = Popup(title="Choisir une trace", content=contenu, size_hint=(0.95, 0.95))
            self._popup.open()

    def _trace_choisie(self, chemin):
        if hasattr(self, '_popup'):
            self._popup.dismiss()
        if not chemin:
            return
        try:
            points = gps_logic.lire_fichier_pour_conversion(chemin)
        except Exception as e:
            self.info_trace = f"Erreur de lecture : {e}"
            return

        if not points:
            self.info_trace = "Aucun point GPS valide trouvé dans ce fichier."
            return

        self.points_trace = points
        # La carte et la trace n'apparaissent qu'au chargement
        # de la trace.
        self.trace_chargee = True
        self.info_trace = f"Trace : {os.path.basename(chemin)}."
        # Waypoints de la trace : mêmes « vrais » waypoints que dans les
        # autres onglets (ni n° de points, ni waypoints superposés au
        # départ/à l'arrivée).
        try:
            waypoints = gps_logic.vrais_waypoints(
                gps_logic.lire_waypoints_source(chemin, heure_locale=False),
                [(points[0]['lat'], points[0]['lon']), (points[-1]['lat'], points[-1]['lon'])],
            )
        except Exception:
            waypoints = []
        self._afficher_trace_sur_carte(points, waypoints=waypoints)

    def ouvrir_selecteur_photo(self):
        contenu = _construire_selecteur_fichier_photo(self._photo_choisie)
        if contenu is not None:
            self._popup = Popup(title="Choisir une photo", content=contenu, size_hint=(0.95, 0.95))
            self._popup.open()

    def _photo_choisie(self, chemin):
        if hasattr(self, '_popup'):
            self._popup.dismiss()
        if not chemin:
            return

        self.fichier_photo = chemin
        self.info_photo = f"Photo : {os.path.basename(chemin)}"
        self.status_text = ""

        exif_data = gps_logic.get_exif_data(chemin)
        self.champ_date = exif_data["datetime"] or ""
        self.champ_lat = str(exif_data["latitude"]) if exif_data["latitude"] is not None else ""
        self.champ_lon = str(exif_data["longitude"]) if exif_data["longitude"] is not None else ""
        self.champ_alt = str(exif_data["altitude"]) if exif_data["altitude"] is not None else ""

        # Force le rechargement de la miniature même si on recharge la
        # même photo (Kivy ne redéclenche pas "source" si la valeur ne
        # change pas).
        self.miniature_source = ""
        self.miniature_source = chemin
        # Le tableau et le bloc photo (et plus tard les boutons)
        # n'apparaissent qu'au chargement de la photo.
        self.photo_chargee = True

    def situer(self):
        """Cherche dans la trace le point le plus proche de la date/heure
        EXIF saisie et pré-remplit latitude/longitude/altitude,
        équivalent de situer_exif_edite() dans la version desktop."""
        if not self.champ_date.strip():
            self.titre_carte = "Renseigne une date/heure pour la photo"
            self.titre_carte_color = [0.8, 0.1, 0.1, 1]
            return
        if not self.points_trace:
            self.status_text = "Charge d'abord une trace pour y chercher l'horodatage."
            self.status_color = [0.8, 0.1, 0.1, 1]
            return

        self.status_text = ""
        pt = gps_logic.find_closest_point(self.points_trace, self.champ_date)
        if not pt:
            self.titre_carte = "Position non trouvée sur la trace"
            self.titre_carte_color = [0.8, 0.1, 0.1, 1]
            if CARTE_DISPONIBLE and self.map_view is not None and self.marqueur_photo is not None:
                self.map_view.remove_marker(self.marqueur_photo)
                self.marqueur_photo = None
            return

        self.champ_lat = str(pt["lat"])
        self.champ_lon = str(pt["lon"])
        if pt["ele"] is not None:
            self.champ_alt = str(pt["ele"])

        self.titre_carte = "Emplacement de la photo sur la trace"
        self.titre_carte_color = [0, 0, 0, 1]

        if CARTE_DISPONIBLE and self.map_view is not None:
            self.map_view.center_on(pt["lat"], pt["lon"])
            self.map_view.zoom = 16
            if self.marqueur_photo is not None:
                self.map_view.remove_marker(self.marqueur_photo)
            self.marqueur_photo = MapMarker(lat=pt["lat"], lon=pt["lon"])
            self.map_view.add_marker(self.marqueur_photo)

    def enregistrer_exif(self):
        """Écrit les tags EXIF GPS (et date/heure) dans la photo
        chargée, équivalent de enregistrer_exif() dans la version
        desktop."""
        if not self.fichier_photo:
            self.status_text = "Aucune photo chargée."
            self.status_color = [0.8, 0.1, 0.1, 1]
            return
        try:
            lat_val = float(self.champ_lat)
            lon_val = float(self.champ_lon)
            alt_str = self.champ_alt.strip()
            alt_val = float(alt_str) if alt_str and alt_str != "N/A" else None
            gps_logic.enregistrer_exif_gps(
                self.fichier_photo, lat_val, lon_val, alt_val, self.champ_date.strip() or None
            )
            self.status_text = "EXIF enregistré avec succès."
            self.status_color = [0.15, 0.5, 0.15, 1]
        except Exception as e:
            self.status_text = f"Échec de l'enregistrement : {e}"
            self.status_color = [0.8, 0.1, 0.1, 1]

    def _afficher_trace_sur_carte(self, points, waypoints=None):
        """Trace la polyligne sur la carte et recadre dessus, équivalent
        de afficher_trace_sur_carte_photo() dans la version desktop.
        Les waypoints éventuels sont indiqués par un petit curseur rond
        et bleu (MarqueurWaypoint) dont la taille suit le zoom."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return

        if self.trace_layer is not None:
            self.map_view.remove_layer(self.trace_layer)
            self.trace_layer = None
        for m in self.marqueurs_actifs:
            self.map_view.remove_marker(m)
        self.marqueurs_actifs = []
        for mw in self.marqueurs_waypoints:
            self.map_view.remove_marker(mw)
        self.marqueurs_waypoints = []
        if self.marqueur_photo is not None:
            self.map_view.remove_marker(self.marqueur_photo)
            self.marqueur_photo = None

        if not points:
            return

        liste_coords = [(p["lat"], p["lon"]) for p in points]
        # Le calque de la trace est posé APRÈS les marqueurs de
        # waypoints : ajouté en dernier, il s'affiche par-dessus eux,
        # comme sur les onglets Live (7) et Carte (4).
        self.trace_layer = TraceLayer()
        self.trace_layer.set_points(liste_coords)

        for wpt in (waypoints or []):
            lat_w, lon_w = wpt.get('lat'), wpt.get('lon')
            if lat_w is None or lon_w is None:
                continue
            mw = MarqueurWaypoint(
                zoom=self.map_view.zoom, lat=lat_w, lon=lon_w,
                nom=wpt.get('name'), description=wpt.get('description'),
            )
            self.map_view.add_marker(mw)
            self.marqueurs_waypoints.append(mw)

        self.map_view.add_layer(self.trace_layer)

        # REMONTÉE DU CALQUE DE MARQUEURS AU-DESSUS DE LA TRACE (même
        # mécanique que les onglets Nettoyage et temp) : sans elle,
        # les disques jaunes des waypoints passent SOUS la trace.
        self._remonte_calque_marqueurs()

        lats = [c[0] for c in liste_coords]
        lons = [c[1] for c in liste_coords]
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)
        self.map_view.center_on((min_lat + max_lat) / 2, (min_lon + max_lon) / 2)
        max_delta = max(max_lat - min_lat, max_lon - min_lon)
        if max_delta > 0:
            zoom = int(12 - math.log2(max_delta * 10))
            self.map_view.zoom = max(2, min(zoom, 18))

    def _remonte_calque_marqueurs(self):
        """Remonte le calque des marqueurs AU-DESSUS du calque de trace
        (même mécanique que les onglets Nettoyage et temp) : retirer
        puis re-poser le calque de marqueurs via l'API PUBLIQUE de
        MapView (remove_layer/add_layer) le renvoie en fin de pile,
        au-dessus de tout. À appeler après TOUTE pose de marqueurs
        suivant un add_layer."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        couche = getattr(self.map_view, "_marker_layer", None)
        if couche is None:
            for l in getattr(self.map_view, "_layers", []) or []:
                if isinstance(l, MarkerMapLayer):
                    couche = l
                    break
        if couche is None:
            return
        try:
            self.map_view.remove_layer(couche)
            self.map_view.add_layer(couche)
        except Exception:
            pass


class EcranAVenir(Screen):
    """Écran affiché pour les fonctionnalités pas encore intégrées."""

    def __init__(self, nom_fonction, **kwargs):
        super().__init__(**kwargs)
        layout = BoxLayout(orientation="vertical", padding=24, spacing=16)
        layout.add_widget(Label(text=nom_fonction, font_size="20sp", bold=True, color=(0, 0, 0, 1)))
        layout.add_widget(Label(
            text="Cette fonctionnalité sera activée dès que\nson code Python sera intégré à l'application.",
            color=(0.3, 0.3, 0.3, 1),
        ))
        self.add_widget(layout)


def _purger_disques_simulateur(*args):
    """FILET DE SÉCURITÉ (PC uniquement, aucun effet ailleurs) : retire
    du canvas ARRIÈRE de la fenêtre tout « disque rouge » laissé par le
    simulateur multitouch de Kivy (clic droit / molette). Le simulateur
    pose EXACTEMENT, dans Window.canvas.after, une instruction
    Color(0.8, 0.2, 0.2, 0.7) suivie d'une Ellipse de 20 x 20 — c'est
    cette paire, et elle seule, que l'on retire : aucun autre élément
    de l'application ne dessine dans ce canvas, les onglets ne sont
    jamais touchés. Défensif : ne lève jamais. Appelée périodiquement
    par Clock (voir OutilsTracesApp.build)."""
    try:
        apres = Window.canvas.after
        enfants = list(apres.children)
        i = 0
        while i < len(enfants) - 1:
            instr = enfants[i]
            suivant = enfants[i + 1]
            if (instr.__class__.__name__ == "Color"
                    and abs(float(instr.r) - 0.8) < 0.02
                    and abs(float(instr.g) - 0.2) < 0.02
                    and abs(float(instr.b) - 0.2) < 0.02
                    and suivant.__class__.__name__ == "Ellipse"
                    and float(suivant.size[0]) <= 30.0
                    and float(suivant.size[1]) <= 30.0):
                apres.remove(instr)
                apres.remove(suivant)
            i += 1
    except Exception:
        pass


class OutilsTracesApp(App):
    title = "Bubu GPS"

    # Icone du bouton des fonds de carte : accessible dans le kv via
    # "app.CHEMIN_ICONE_LAYER" (le kv ne voit pas les globales du
    # module, seulement app, root et les fonctions comme dp()).
    # L'image est cherchee a cote de main.py : images/Layer.png.
    CHEMIN_ICONE_LAYER = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "images", "Layer.png")

    def build(self):
        # --- VOTRE CODE D'INITIALISATION EXISTANT ---
        # (chargement des écrans, builder, etc.)
        return ...

    def on_start(self):
        """Méthode exécutée automatiquement au démarrage de l'application."""
        if platform == 'android':
            from android.permissions import request_permissions, Permission
            request_permissions([
                Permission.WRITE_EXTERNAL_STORAGE, 
                Permission.READ_EXTERNAL_STORAGE
            ])
            
    def on_resume(self):
        """Appelé automatiquement par Kivy/Android quand l'appli repasse
        au premier plan (ex: retour depuis l'appareil photo, ou depuis
        n'importe quelle autre appli/l'écran d'accueil). Si l'onglet
        Live a une balise <wpt> en attente (voir LiveScreen._verifier_
        et_ouvrir_camera), la referme maintenant — sinon (retour au
        premier plan sans rapport avec l'appareil photo), ne fait rien.
        Un court délai laisse le temps au MediaStore Android d'indexer
        la photo tout juste prise avant qu'on l'interroge."""
        try:
            ecran_live = self.sm.get_screen("Live")
        except Exception:
            return True
        if getattr(ecran_live, '_wpt_en_attente', None) is not None:
            Clock.schedule_once(lambda dt: ecran_live._fermer_waypoint_photo(), 0.5)
        # Réveil de l'écran / retour au premier plan : resynchronise la
        # trace live avec GPSLogger si un enregistrement est actif.
        ecran_live._resynchroniser_avec_gpslogger()
        # Rebranche les handlers Window du clic long de gel : ils peuvent
        # cesser de recevoir les touchers après un cycle pause/reprise
        # d'Android (ex : retour de l'appareil photo). La détection de
        # secours au niveau widget (MapViewMolette) couvre le cas où ce
        # rebranchement ne suffirait pas.
        try:
            ecran_live._relier_touchers_fenetre()
        except Exception:
            pass
        return True

    def build(self):
        # Par défaut, Kivy affiche un fond NOIR uni tant qu'on ne le
        # change pas explicitement : tous les libellés en texte noir
        # étaient donc invisibles dessus. On passe à un fond clair.
        Window.clearcolor = (0.96, 0.97, 0.98, 1)

        # Filet de sécurité PC (voir _purger_disques_simulateur) :
        # balaye le canvas de la fenêtre toutes les 0,2 s pour retirer
        # tout disque du simulateur multitouch qui aurait malgré tout
        # été posé. Un disque éventuel disparaît donc en 0,2 s au
        # maximum, quel que soit l'onglet affiché. PC UNIQUEMENT :
        # inutile sur Android (pas de simulateur souris) et l'on
        # n'y touche à rien.
        if platform != "android":
            Clock.schedule_interval(_purger_disques_simulateur, 0.2)

        Builder.load_string(KV)

        self.sm = ScreenManager()
        self.sm.add_widget(StatistiquesScreen(name="statistiques"))
        self.sm.add_widget(ConversionScreen(name="conversion"))
        self.sm.add_widget(NumerotationScreen(name="numerotation"))
        self.sm.add_widget(FusionScreen(name="fusion"))
        self.sm.add_widget(CarteScreen(name="carte"))
        self.sm.add_widget(NettoyageScreen(name="nettoyage"))
        self.sm.add_widget(AjoutScreen(name="ajout"))
        self.sm.add_widget(PhotosScreen(name="photos"))
        self.sm.add_widget(LiveScreen(name="Live"))

        # --- Barre du haut : menu déroulant (gauche) + titre + Quitter (droite) ---
        barre = BoxLayout(size_hint_y=None, height=dp(60), padding=(8, 4), spacing=dp(8))

        self.dropdown = DropDown(auto_width=False, width=dp(220))
        self._ecrans_menu = [("statistiques", "Statistiques"), ("conversion", "Conversion"), ("numerotation", "Numérotation"), ("fusion", "Fusion"), ("carte", "Découpe"), ("nettoyage", "Nettoyage"), ("ajout", "Ajout"), ("photos", "Photos"), ("Live", "Live")]
        self._ecrans_menu += [(nom, nom) for nom in SCREENS_A_VENIR]
        self._boutons_menu = {}
        for nom_ecran, libelle in self._ecrans_menu:
            btn = Button(text=libelle, size_hint_y=None, height=dp(48), font_size="16sp")
            btn.bind(on_release=lambda b, n=nom_ecran: self._changer_ecran(n))
            self.dropdown.add_widget(btn)
            self._boutons_menu[nom_ecran] = btn

        self.btn_menu = Button(text="Menu", size_hint_x=None, width=dp(110))
        self.btn_menu.bind(on_release=self._ouvrir_menu)
        barre.add_widget(self.btn_menu)

        barre.add_widget(Label(text="Bubu GPS", bold=True, color=(1, 1, 1, 1)))

        self.btn_quitter = Button(text="Quitter", size_hint_x=None, width=dp(110))
        self.btn_quitter.bind(on_release=self._quitter_application)
        barre.add_widget(self.btn_quitter)

        from kivy.graphics import Color, Rectangle
        with barre.canvas.before:
            Color(0.16, 0.2, 0.26, 1)
            self._rect = Rectangle(pos=barre.pos, size=barre.size)

        def _maj_rect(instance, value):
            self._rect.pos = instance.pos
            self._rect.size = instance.size

        barre.bind(pos=_maj_rect, size=_maj_rect)

        racine = BoxLayout(orientation="vertical")
        racine.add_widget(barre)
        racine.add_widget(self.sm)

        if platform == "android":
            self._demander_permissions_android()
            try:
                from android import activity
                activity.bind(on_new_intent=self._sur_nouvel_intent)
            except Exception:
                pass
            
            # --- AJOUT : Vérification d'un fichier ouvert au démarrage ---
            Clock.schedule_once(self._verifier_intent_lancement, 1)

        return racine
        
    def _sur_nouvel_intent(self, intent):
        """Déclenché si l'app tourne déjà et qu'on clique sur un autre fichier."""
        if platform == "android":
            try:
                action = intent.getAction()
                if action == "android.intent.action.VIEW":
                    uri = intent.getData()
                    if uri:
                        chemin = self._convertir_uri_en_chemin(uri.toString())
                        if chemin:
                            Clock.schedule_once(lambda dt: self._traiter_fichier_externe(chemin), 0.5)
            except Exception as e:
                print(f"Erreur on_new_intent : {e}")

    def _verifier_intent_lancement(self, dt):
        """Vérifie si l'application a été lancée en cliquant sur un fichier."""
        try:
            from jnius import autoclass
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            activity = PythonActivity.mActivity
            intent = activity.getIntent()
            action = intent.getAction()
            
            if action == "android.intent.action.VIEW":
                uri = intent.getData()
                if uri:
                    chemin = self._convertir_uri_en_chemin(uri.toString())
                    if chemin:
                        self._traiter_fichier_externe(chemin)
        except Exception as e:
            print(f"Erreur vérification intent au lancement : {e}")

    def _traiter_fichier_externe(self, chemin):
        """Bascule sur l'écran 'carte' et charge le fichier de trace."""
        import os
        if os.path.exists(chemin):
            # 1. Basculer sur l'écran "carte" (l'onglet 4)
            self.sm.current = "carte"
            
            # 2. Récupérer l'écran carte et charger la trace directement
            ecran_carte = self.sm.get_screen("carte")
            if hasattr(ecran_carte, "charger_trace"):
                ecran_carte.charger_trace(chemin)
                
    def _convertir_uri_en_chemin(self, uri_string):
        """Convertit une URI content:// ou file:// en chemin de fichier exploitable."""
        if uri_string.startswith("file://"):
            return urllib.parse.unquote(uri_string[7:])
        elif uri_string.startswith("content://"):
            try:
                from jnius import autoclass
                PythonActivity = autoclass('org.kivy.android.PythonActivity')
                activity = PythonActivity.mActivity
                context = activity.getApplicationContext()
                contentResolver = context.getContentResolver()
                
                # Utilisation d'un curseur pour récupérer le vrai chemin ou copie temporaire
                # Astuce robuste sous Android pour les providers de documents :
                Cursor = autoclass('android.database.Cursor')
                OpenableColumns = autoclass('provider.OpenableColumns') # ou méthode alternative par flux
                
                # Méthode universelle de copie vers un fichier cache temporaire si content://
                InputStream = contentResolver.openInputStream(uri)
                File = autoclass('java.io.File')
                FileOutputStream = autoclass('java.io.FileOutputStream')
                
                cache_dir = context.getCacheDir().getAbsolutePath()
                fichier_tmp = os.path.join(cache_dir, "trace_importee_temp.gpx")
                
                fos = FileOutputStream(File(fichier_tmp))
                buffer = android.jarray('byte', 1024) # ou équivalent octets
                # Copie du flux InputStream vers le fichier local temporaire
                # ...
                # (Alternative plus simple si getPath() fonctionne via StorageUtils, 
                # sinon la copie par flux garantit la lecture peu importe l'origine Google Drive/Gestionnaire)
                
                # Pour faire au plus simple et direct si l'URI pointe vers un fichier géré par le provider :
                import shutil
                with open(fichier_tmp, 'wb') as f_out:
                    # Lecture octet par octet via jnius InputStream si besoin, 
                    # ou utilisation directe si l'URI est résolue par le système.
                    pass
                return fichier_tmp
            except Exception as e:
                print(f"Erreur conversion content:// : {e}")
                return None
        return None

    def on_start(self):
        """Si l'appli vient d'être lancée en cliquant sur un fichier
        .gpx/.kml/.kmz (association de fichiers, "Ouvrir avec" -> Bubu
        GPS), l'intention de départ contient ce fichier. Le cas où
        l'appli est déjà ouverte est géré par _sur_nouvel_intent
        (branché juste au-dessus, dans build())."""
        if platform != "android":
            return
        try:
            from jnius import autoclass
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            intent = PythonActivity.mActivity.getIntent()
            if intent is not None:
                self._traiter_intent_fichier(intent)
        except Exception as e:
            print(f"[Intent] Erreur au démarrage : {e}")

        # Démarrage à froid (après un plantage ou un clic sur
        # "Quitter") : resynchronise la trace live avec GPSLogger si
        # un enregistrement est actif dans son dossier de sortie.
        try:
            self.sm.get_screen("Live")._resynchroniser_avec_gpslogger()
        except Exception as e:
            print(f"[Live] Resynchronisation au démarrage impossible : {e}")

    def _sur_nouvel_intent(self, intent):
        """Appelée quand l'appli est déjà ouverte et que l'utilisateur
        clique sur un autre fichier .gpx/.kml/.kmz depuis un
        gestionnaire de fichiers (l'appli n'est pas relancée, Android
        envoie simplement un nouvel intent à l'activité existante)."""
        self._traiter_intent_fichier(intent)

    def _traiter_intent_fichier(self, intent):
        """Si cet intent correspond à l'ouverture d'un fichier de trace
        (action VIEW avec une donnée associée), le charge directement
        dans l'onglet Carte/Découpe, comme avec le bouton "Charger une
        trace". Ignore silencieusement tout intent qui ne correspond
        pas à ce cas (ex. relance normale de l'appli)."""
        try:
            from jnius import autoclass
            Intent = autoclass('android.content.Intent')
            action = intent.getAction()
            uri = intent.getData()
            if action != Intent.ACTION_VIEW or uri is None:
                return

            chemin = self._uri_vers_chemin_local(uri)
            if not chemin:
                print("[Intent] Impossible de résoudre le fichier ouvert.")
                return

            ecran_carte = self.sm.get_screen("carte")
            self.sm.current = "carte"
            ecran_carte.charger_trace(chemin)
        except Exception as e:
            print(f"[Intent] Erreur de traitement du fichier ouvert : {e}")

    def _uri_vers_chemin_local(self, uri):
        """Résout une Uri Android (file:// ou content://) vers un chemin
        de fichier local exploitable par gps_logic.lire_fichier_pour_
        conversion. Pour un content:// (la majorité des gestionnaires de
        fichiers modernes, Google Drive...), le contenu est copié dans
        le dossier de cache privé de l'appli, sous son nom d'origine si
        celui-ci est disponible."""
        from jnius import autoclass

        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        activite = PythonActivity.mActivity
        schema = uri.getScheme()

        if schema == "file":
            return uri.getPath()

        if schema != "content":
            return None

        resolveur = activite.getContentResolver()

        # Récupère le nom d'origine du fichier si possible (colonne
        # DISPLAY_NAME), pour garder la bonne extension et un nom
        # lisible dans l'onglet Carte/Découpe.
        nom_fichier = "trace_ouverte.gpx"
        try:
            OpenableColumns = autoclass('android.provider.OpenableColumns')
            curseur = resolveur.query(uri, None, None, None, None)
            if curseur is not None:
                if curseur.moveToFirst():
                    idx = curseur.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                    if idx >= 0:
                        nom_fichier = curseur.getString(idx)
                curseur.close()
        except Exception:
            pass

        flux_entree = resolveur.openInputStream(uri)
        if flux_entree is None:
            return None

        dossier_cache = activite.getCacheDir().getAbsolutePath()
        chemin_local = os.path.join(dossier_cache, nom_fichier)

        try:
            tampon = bytearray(8192)
            with open(chemin_local, "wb") as f:
                while True:
                    n_lus = flux_entree.read(tampon)
                    if n_lus == -1:
                        break
                    f.write(bytes(tampon[:n_lus]))
        finally:
            flux_entree.close()

        return chemin_local

    def _ouvrir_menu(self, instance):
        BLEU_KIVY = (0.12, 0.58, 0.95, 1)
        COULEUR_NORMAL = (1, 1, 1, 1)
        
        for nom_ecran, libelle in self._ecrans_menu:
            btn = self._boutons_menu[nom_ecran]
            btn.text = libelle
            
            if nom_ecran == self.sm.current:
                btn.background_color = BLEU_KIVY
            else:
                btn.background_color = COULEUR_NORMAL
        self.dropdown.open(instance)

    def _quitter_application(self, *args):
        """Bouton « Quitter » : pendant un live, NE PAS quitter vraiment —
        quitter l'appli (App.stop) tue AUSSI le service de suivi (le
        processus de la tâche reçoit un SIGKILL, service p4a inclus),
        et l'enregistrement s'arrête net (le point vert de localisation
        s'éteint). On propose donc :
          - « Continuer en arrière-plan » : moveTaskToBack — l'appli
            passe derrière l'écran d'accueil, EXACTEMENT comme appuyer
            sur Accueil ; le live et le service continuent (c'est le
            mode éprouvé avec l'écran éteint) ;
          - « Quitter quand même » : arrête réellement (l'enregistrement
            sera interrompu, mais le marqueur de session permet de
            reprendre les points déjà écrits par le service) ;
          - « Annuler » : revient à l'appli sans rien changer.
        Hors live : quitte directement, comme avant."""
        live_actif = False
        try:
            if "Live" in self.sm.screen_names:
                live_actif = bool(self.sm.get_screen("Live").en_cours_live)
        except Exception:
            live_actif = False

        if not live_actif:
            self.stop()
            return

        contenu = BoxLayout(orientation="vertical", padding=dp(14), spacing=dp(12))
        lbl = Label(
            text=("Un enregistrement live est en cours.\n\n"
                  "Quitter l'application arrêterait aussi\n"
                  "l'enregistrement GPS (service tué).\n\n"
                  "Pour garder l'enregistrement actif,\n"
                  "choisissez « Continuer en arrière-plan »."),
            text_size=(dp(290), None), halign="left", valign="middle",
        )
        lbl.bind(texture_size=lambda w, v: setattr(w, "height", v[1]))
        contenu.add_widget(lbl)

        # Boutons empilés verticalement : côte à côte, les libellés
        # longs (« Continuer en arrière-plan ») étaient tronqués et
        # devenaient illisibles sur l'écran du téléphone.
        boutons = BoxLayout(orientation="vertical",
                            size_hint_y=None, height=dp(160), spacing=dp(8))
        btn_fond = Button(text="Continuer en arriere-plan",
                          background_color=(0.15, 0.68, 0.38, 1))
        btn_quit = Button(text="Quitter quand meme",
                          background_color=(0.776, 0.157, 0.157, 1))
        btn_annul = Button(text="Annuler",
                           background_color=(0.4, 0.4, 0.4, 1))
        boutons.add_widget(btn_fond)
        boutons.add_widget(btn_quit)
        boutons.add_widget(btn_annul)
        contenu.add_widget(boutons)

        popup = Popup(title="Live en cours", content=contenu,
                      size_hint=(0.92, 0.62))

        def _arriere_plan(*_):
            popup.dismiss()
            try:
                from jnius import autoclass
                activite = autoclass("org.kivy.android.PythonActivity").mActivity
                activite.moveTaskToBack(True)
            except Exception:
                # Hors Android (tests PC) : simple minimisation impossible,
                # on ne fait rien (l'appli reste ouverte).
                pass

        btn_fond.bind(on_release=_arriere_plan)
        btn_quit.bind(on_release=lambda *_: (popup.dismiss(), self.stop()))
        btn_annul.bind(on_release=lambda *_: popup.dismiss())
        popup.open()

    def _changer_ecran(self, nom_ecran):
        self.dropdown.dismiss()
        self.sm.current = nom_ecran
        

        # Récupération de l'écran Live
        live_screen = self.sm.get_screen("Live") if "Live" in self.sm.screen_names else None

        if nom_ecran == "Live" and live_screen:
            # Si on est sur le Live, on lie l'état 'disabled' des boutons globaux 
            # à la variable 'freeze_actif' du LiveScreen
            # (On évite de lier plusieurs fois si on clique plusieurs fois)
            live_screen.unbind(freeze_actif=self._mettre_a_jour_gel_barre)
            live_screen.bind(freeze_actif=self._mettre_a_jour_gel_barre)
            # Application immédiate de l'état actuel
            self._mettre_a_jour_gel_barre(live_screen, live_screen.freeze_actif)
        else:
            # Sur tous les autres écrans, les boutons de la barre du haut doivent être actifs
            if live_screen:
                live_screen.unbind(freeze_actif=self._mettre_a_jour_gel_barre)
            self.btn_menu.disabled = False
            self.btn_quitter.disabled = False

    def _mettre_a_jour_gel_barre(self, instance_live, est_gele):
        """Met à jour l'état désactivé/activé de la barre globale en fonction du gel Live."""
        self.btn_menu.disabled = est_gele
        self.btn_quitter.disabled = est_gele
    
    def _demander_permissions_android(self):
        """Sur Android 11+, l'accès complet au stockage (nécessaire pour
        retrouver les traces GPSLogger et enregistrer les conversions un
        peu n'importe où) doit être accordé manuellement dans les réglages.
        On ouvre directement cet écran si besoin."""
        try:
            from android.permissions import request_permissions, Permission
            from jnius import autoclass

            request_permissions([Permission.READ_EXTERNAL_STORAGE, Permission.WRITE_EXTERNAL_STORAGE])

            Environment = autoclass('android.os.Environment')
            if hasattr(Environment, "isExternalStorageManager") and not Environment.isExternalStorageManager():
                Intent = autoclass('android.content.Intent')
                Settings = autoclass('android.provider.Settings')
                Uri = autoclass('android.net.Uri')
                PythonActivity = autoclass('org.kivy.android.PythonActivity')

                intent = Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION)
                uri = Uri.parse("package:" + PythonActivity.mActivity.getPackageName())
                intent.setData(uri)
                PythonActivity.mActivity.startActivity(intent)
        except Exception:
            # Sur desktop (tests) ces modules n'existent pas : on ignore.
            pass


if __name__ == "__main__":
    OutilsTracesApp().run()