# -*- coding: utf-8 -*-
"""
============================================================================
 OUTILS TRACES ET PHOTOS — Application Android (Kivy)
 Réécriture de start.py (tkinter) pour fonctionner en APK autonome.

 - Onglets "Conversion" et "Numérotation" : entièrement fonctionnels.
 - Les 5 autres fonctionnalités (Fusion, Carte/Découpe, Statistiques,
   Photos, Live) sont déjà présentes dans le menu déroulant mais
   affichent un écran "à venir" tant que leur code n'est pas fourni et
   intégré. Voir SCREENS_A_VENIR ci-dessous.
============================================================================
"""

import os
import math
import threading
import queue
import subprocess
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

from kivy.app import App
from kivy.lang import Builder
from kivy.uix.screenmanager import ScreenManager, Screen
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.dropdown import DropDown
from kivy.uix.button import Button
from kivy.uix.popup import Popup
from kivy.uix.filechooser import FileChooserListView
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.metrics import dp
from kivy.graphics import Color, Line as KivyLine, Rectangle
from kivy.core.text import Label as CoreLabel
from kivy.uix.widget import Widget
from kivy.properties import StringProperty, BooleanProperty, ListProperty, ObjectProperty
from kivy.utils import platform
from kivy.uix.textinput import TextInput

import gps_logic

# ----------------------------------------------------------------------
# Carte interactive (onglet Carte/Découpe) : kivy_garden.mapview est
# l'équivalent Kivy le plus proche de tkintermapview (tuiles OSM/
# satellite, marqueurs). Import protégé : si la bibliothèque n'est pas
# encore installée, le reste de l'appli continue de fonctionner et
# l'écran Carte affiche un message au lieu de planter.
# Installation : pip install kivy_garden.mapview
# ----------------------------------------------------------------------
try:
    from kivy_garden.mapview import MapView, MapMarker, MapSource, MapLayer
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

    class MapViewMolette(MapView):
        """MapView identique, sauf que la molette/le défilement trackpad
        (PC) DÉPLACE la carte au lieu de zoomer — le zoom ne se fait plus
        que via les boutons +/- dédiés. Le glisser déplace la carte,
        sans zoom tactile ni pincement."""
    
        PAS_DEPLACEMENT_PX = 60
        freeze_callback = ObjectProperty(None, allownone=True)
    
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.freeze_actif = False  # <--- Assure l'initialisation de l'attribut

        def on_touch_down(self, touch):
            # Le double-tap déclenche le freeze/unfreeze dans tous les cas
            if touch.is_double_tap:
                if self.freeze_callback:
                    self.freeze_callback()
                return True
    
            if getattr(self, 'freeze_actif', False):
                return True  # Bloque tous les clics et l'amorce de glisser sur la carte en mode freeze
                
            bouton = getattr(touch, "button", "")
            if bouton in ("scrollup", "scrolldown", "scrollleft", "scrollright"):
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
            return super().on_touch_down(touch)
    
        def on_touch_move(self, touch):
            # ---> Bloque net le glisser-déposer (pan) de la carte si le gel est actif
            if getattr(self, 'freeze_actif', False):
                return True
                
            # Empêche le zoom par pincement en neutralisant l'effet multi-touch de la carte
            if touch.grab_current is not self and len(getattr(self, 'touches', [])) > 1:
                return True
            return super().on_touch_move(touch)
    
        def on_touch_up(self, touch):
            if getattr(self, 'freeze_actif', False):
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
        # Appelé (sans argument) sur un appui long (0.6 s) dans la zone
        # du graphique — utilisé uniquement par l'onglet Live pour
        # ouvrir l'appareil photo Android. None par défaut : aucun
        # comportement ajouté pour les autres écrans.
        self.callback_long_press = None
        self.afficher_courbe_vitesse = True  # <--- AJOUT ICI
        self.afficher_curseur = True
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

    def set_selection(self, distance_km):
        self.distance_selection = distance_km
        self._redessiner()

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

            if a_ele or a_ele_sec:
                if a_vit:
                    for valeur in self._graduations(v_bas, v_haut, 4):
                        gy = y_vit(valeur)
                        self._poser_texte(f"{int(round(valeur))}", zx + zw + dp(4), gy, VERT,
                                           taille_sp=9, centre_v=True, gras=False)

                    # Tracé de la courbe de vitesse (masqué si self.afficher_courbe_vitesse
                    # est False, cf. LiveScreen (onglet 7) : seul l'axe/les graduations de
                    # vitesse ci-dessus restent visibles dans ce cas).
                    if self.afficher_courbe_vitesse:
                        points_vit = []
                        for d, v in zip(self.distances_km, self.vitesses_kmh):
                            points_vit.extend([x_ecran(d), y_vit(v)])
                        Color(*VERT)
                        KivyLine(points=points_vit, width=1.6)

            if self.afficher_curseur and self.distance_selection is not None:
                cx = x_ecran(self.distance_selection)
                Color(0.85, 0.1, 0.1, 0.9)
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
            if a_vit:
                tex_v = self._texte_texture("Vitesse (km/h)", taille_sp=9)
                self._poser_texte("Vitesse (km/h)", zx + zw - tex_v.width, zy + zh + dp(4), VERT, taille_sp=9)

    def on_touch_down(self, touch):
        # Si un parent gèle l'interaction (ex: LiveScreen en mode freeze)
        if hasattr(self.parent, 'parent') and getattr(self.parent.parent, 'freeze_actif', False):
            return True
        if not self.collide_point(*touch.pos):
            return super().on_touch_down(touch)
        if not self.distances_km and not self.callback_long_press:
            return super().on_touch_down(touch)

        # Capture le toucher pour suivre le glissement
        touch.grab(self)

        # Appui long (0.6 s) dans la zone du graphique : ouvre l'appareil
        # photo Android (voir callback_long_press ; None sur les écrans
        # autres que l'onglet Live, donc sans effet pour eux). Fonctionne
        # même sans trace chargée sur le graphique (contrairement à la
        # sélection de point ci-dessous).
        if self.callback_long_press:
            touch.ud['long_press_clock'] = Clock.schedule_once(lambda dt: self.callback_long_press(), 0.6)

        if not self.distances_km:
            return True

        distance_km_tapee = self._calculer_distance_depuis_touch(touch)
        self.set_selection(distance_km_tapee)  # Met à jour le curseur visuel
        if self.callback_clic:
            self.callback_clic(distance_km_tapee)  # Met à jour la carte dès l'appui
        return True

    def on_touch_move(self, touch):
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
            if 'long_press_clock' in touch.ud:
                touch.ud['long_press_clock'].cancel()
            if not self.distances_km:
                return True

            distance_km_tapee = self._calculer_distance_depuis_touch(touch)
            self.set_selection(distance_km_tapee)
            if self.callback_clic:
                self.callback_clic(distance_km_tapee)  # Assure la position finale au lâcher
            return True
        return super().on_touch_up(touch)


# ----------------------------------------------------------------------
# Dossier racine utilisé pour parcourir/enregistrer les fichiers.
# Sur Android, cible directement la carte SD physique "2EBA-9AD9".
# ----------------------------------------------------------------------
if platform == "android":
    # Le chargement pointe toujours vers les Téléchargements sur Android
    DOSSIER_CHARGEMENT = "/storage/emulated/0/Download/"
    
    # L'enregistrement conserve votre logique initiale avec la carte SD
    sd_physique = "/storage/2EBA-9AD9"
    # On vérifie si la carte SD est bien montée/présente, sinon on bascule sur la mémoire interne
    if os.path.exists(sd_physique):
        DOSSIER_SORTIE = os.path.join(sd_physique, "Bubu_GPS_files")
    else:
        DOSSIER_SORTIE = "/storage/emulated/0/Bubu_GPS_files"
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
            height: dp(40)
            color: 0.15, 0.5, 0.15, 1

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
                text: "Carte / Découpe"
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
                ToggleButton:
                    text: "Satellite"
                    group: "vue_carte"
                    state: "down"
                    size_hint_x: None
                    width: dp(100)
                    on_state: if self.state == "down": root.changer_vue_carte("satellite")
                ToggleButton:
                    text: "Plan"
                    group: "vue_carte"
                    size_hint_x: None
                    width: dp(90)
                    on_state: if self.state == "down": root.changer_vue_carte("plan")

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
                height: max(dp(36), self.texture_size[1] + dp(8))
                text_size: self.width, None
                halign: "left"
                valign: "top"
                font_size: "12sp"
                color: 0, 0, 0, 1

            BoxLayout:
                id: zone_graphique
                size_hint_y: None
                height: dp(175)

            Label:
                text: "Decoupe de trace"
                size_hint_y: None
                height: dp(26)
                color: 0, 0, 0, 1
                bold: True

            TextInput:
                id: entree_coupure
                hint_text: "Numero du point de coupure (ex: 42)"
                multiline: False
                input_filter: "int"
                size_hint_y: None
                height: dp(44)
                disabled: not root.trace_chargee
                text: root.point_coupure_text
                on_text: root.point_coupure_text = self.text

            Label:
                text: root.status_text
                size_hint_y: None
                height: max(dp(30), self.texture_size[1] + dp(10))
                color: root.status_color
                text_size: self.width, None
                halign: "left"
                valign: "top"

            Button:
                text: "Couper ici"
                size_hint_y: None
                height: dp(56)
                disabled: not root.trace_chargee or root.en_cours
                background_color: 0.15, 0.68, 0.38, 1
                on_release: root.executer_decoupe()

<LigneStatistique>:
    size_hint_y: None
    height: dp(38)
    canvas.before:
        Color:
            rgba: self.couleur_fond
        Rectangle:
            pos: self.pos
            size: self.size
    Label:
        text: root.libelle
        bold: True
        color: 0.2, 0.2, 0.2, 1
        font_size: "13sp"
        text_size: self.width, self.height
        halign: "left"
        valign: "middle"
        padding_x: dp(8)
    Label:
        text: root.valeur
        bold: True
        color: 0.0, 0.48, 0.8, 1
        font_size: "13sp"
        text_size: self.width, self.height
        halign: "right"
        valign: "middle"
        padding_x: dp(8)

<StatistiquesScreen>:
    BoxLayout:
        orientation: "vertical"
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

        Label:
            text: root.info_fichier
            size_hint_y: None
            height: max(dp(30), self.texture_size[1] + dp(8))
            text_size: self.width, None
            halign: "left"
            valign: "top"
            color: 0.2, 0.5, 0.2, 1
            italic: True

        ScrollView:
            BoxLayout:
                id: tableau_stats
                orientation: "vertical"
                size_hint_y: None
                height: self.minimum_height

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

            BoxLayout:
                size_hint_y: None
                height: dp(40)
                spacing: dp(6)
                ToggleButton:
                    text: "Satellite"
                    group: "vue_carte_photo"
                    state: "down"
                    on_state: if self.state == "down": root.changer_vue_carte("satellite")
                ToggleButton:
                    text: "Plan"
                    group: "vue_carte_photo"
                    on_state: if self.state == "down": root.changer_vue_carte("plan")

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
                height: dp(60)
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
                height: dp(60)
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
                height: self.width / (photo_img.image_ratio if photo_img.image_ratio else (4/3))
                
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

            Button:
                text: "Situer (Horodatage)"
                size_hint_y: None
                height: dp(48)
                background_color: 0.16, 0.5, 0.73, 1
                on_release: root.situer()

            Button:
                text: "Enregistrer EXIF"
                size_hint_y: None
                height: dp(48)
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

            Label:
                text: root.titre_carte
                size_hint_y: None
                height: dp(26)
                bold: True
                color: root.titre_carte_color

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
                ToggleButton:
                    text: "Satellite"
                    group: "vue_carte_live"
                    state: "down"
                    disabled: root.freeze_actif
                    size_hint_x: None
                    width: dp(100)
                    on_state: if self.state == "down": root.changer_vue_carte("satellite")
                ToggleButton:
                    text: "Plan"
                    group: "vue_carte_live"
                    disabled: root.freeze_actif
                    size_hint_x: None
                    width: dp(90)
                    on_state: if self.state == "down": root.changer_vue_carte("plan")

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
                height: max(dp(36), self.texture_size[1] + dp(8))
                text_size: self.width, None
                halign: "left"
                valign: "top"
                font_size: "12sp"
                color: 0, 0, 0, 1

            # --- AJOUT : Emplacement du graphique d'altitude/vitesse ---
            BoxLayout:
                id: zone_graphique
                size_hint_y: None
                height: dp(175)
"""


class ConversionScreen(Screen):
    fichier_source = StringProperty("")
    info_fichier = StringProperty("Aucune trace chargée.")
    format_sortie = StringProperty("gpx")
    garder_temps = BooleanProperty(True)
    status_text = StringProperty("")
    en_cours = BooleanProperty(False)

    def ouvrir_selecteur_fichier(self):
        contenu = _construire_selecteur_fichier(self._fichier_choisi)
        self._popup = Popup(title="Choisir un fichier", content=contenu, size_hint=(0.95, 0.95))
        self._popup.open()

    def _fichier_choisi(self, chemin):
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
            chemin_sortie = gps_logic.convertir_fichier(
                self.fichier_source,
                self.format_sortie,
                self.garder_temps,
                dossier_sortie=DOSSIER_SORTIE,
            )
            message = f"Conversion réussie !\nEnregistré dans :\n{chemin_sortie}"
            erreur = False
        except Exception as e:
            message = f"Échec de la conversion :\n{e}"
            erreur = True

        def _maj_ui(dt):
            self.en_cours = False
            self.status_text = ("[ERREUR] " + message) if erreur else message

        Clock.schedule_once(_maj_ui, 0)


class NumerotationScreen(Screen):
    fichier_source = StringProperty("")
    info_fichier = StringProperty("Aucune trace chargée.")
    trace_chargee = BooleanProperty(False)
    deja_numerote = BooleanProperty(False)
    total_points = 0  # attribut simple (pas besoin d'être une Property Kivy)
    segments_lus = []

    mode = StringProperty("aucun")
    inverser = BooleanProperty(False)
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
        self._maj_etat()

    def on_inverser(self, *args):
        self._maj_etat()

    def on_texte_suppr(self, *args):
        self._maj_etat()

    def ouvrir_selecteur_fichier(self):
        contenu = _construire_selecteur_fichier(self._fichier_choisi)
        self._popup = Popup(title="Choisir un fichier", content=contenu, size_hint=(0.95, 0.95))
        self._popup.open()

    def _fichier_choisi(self, chemin):
        self._popup.dismiss()
        if not chemin:
            return
        try:
            self.segments_lus, deja_num = gps_logic.extraire_donnees_gpx_kmz(chemin)
            self.fichier_source = chemin
            self.deja_numerote = deja_num
            self.total_points = sum(len(seg) for seg in self.segments_lus)
            nom_f = os.path.basename(chemin)
            statut_str = "déjà numéroté" if deja_num else "non numéroté"
            self.info_fichier = f"Trace : {nom_f}\n({statut_str})"

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

        if not self.inverser and self.mode == "aucun":
            self.btn_executer_actif = False
            self.btn_executer_text = "Exécuter"
            self.status_text = "Sélectionnez au moins une action (Inverser ou Traitement)."
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

        titre = " et ".join(actions)
        self.btn_executer_text = titre
        self.status_text = f"Prêt à effectuer : {titre}."
        self.status_color = [0.15, 0.5, 0.15, 1]

    def _maj_legende(self):
        try:
            compteurs = gps_logic.calculer_legende_numerotation(
                self.segments_lus, self.mode, self.inverser, self.texte_suppr
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
                elif cle in ["ajoute", "modifie", "retire", "inchange", "supprime"] and nb > 0:
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
        if not self.inverser and self.mode == "aucun":
            return
        self.en_cours = True
        self.status_text = "Traitement en cours..."
        self.status_color = [0.33, 0.33, 0.33, 1]
        threading.Thread(target=self._traitement_thread, daemon=True).start()

    def _traitement_thread(self):
        try:
            chemin_sortie, resume = gps_logic.traiter_numerotation(
                self.fichier_source, self.segments_lus, self.mode, self.inverser, self.texte_suppr
            )
            message = f"{resume}.\nFichier généré : {os.path.basename(chemin_sortie)}"
            couleur = [0.15, 0.5, 0.15, 1]
        except Exception as e:
            message = f"Échec du traitement : {e}"
            couleur = [0.8, 0.1, 0.8, 1]

        def _maj_ui(dt):
            self.en_cours = False
            self.status_text = message
            self.status_color = couleur

        Clock.schedule_once(_maj_ui, 0)
        
def _construire_selecteur_fichier(callback):
    """Sélecteur de fichier basé sur FileChooserListView (aucune dépendance
    supplémentaire, fonctionne pareil sur desktop et Android une fois la
    permission de stockage accordée)."""
    layout = BoxLayout(orientation="vertical", spacing=6, padding=6)
    chooser = FileChooserListView(path=DOSSIER_RACINE, filters=["*.gpx", "*.kmz", "*.kml"])
    layout.add_widget(chooser)

    boutons = BoxLayout(size_hint_y=None, height=48, spacing=6)
    btn_annuler = Button(text="Annuler")
    btn_valider = Button(text="Valider")
    boutons.add_widget(btn_annuler)
    boutons.add_widget(btn_valider)
    layout.add_widget(boutons)

    btn_valider.bind(on_release=lambda inst: callback(chooser.selection[0] if chooser.selection else None))
    btn_annuler.bind(on_release=lambda inst: callback(None))
    return layout


def _construire_selecteur_fichiers_multiples(callback):
    """Variante du sélecteur ci-dessus permettant de choisir plusieurs
    fichiers d'un coup (nécessaire pour l'onglet Fusion)."""
    layout = BoxLayout(orientation="vertical", spacing=6, padding=6)
    chooser = FileChooserListView(path=DOSSIER_RACINE, filters=["*.gpx", "*.kmz", "*.kml"], multiselect=True)
    layout.add_widget(chooser)

    boutons = BoxLayout(size_hint_y=None, height=48, spacing=6)
    btn_annuler = Button(text="Annuler")
    btn_valider = Button(text="Valider")
    boutons.add_widget(btn_annuler)
    boutons.add_widget(btn_valider)
    layout.add_widget(boutons)

    btn_valider.bind(on_release=lambda inst: callback(list(chooser.selection) if chooser.selection else None))
    btn_annuler.bind(on_release=lambda inst: callback(None))
    return layout


def _construire_selecteur_fichier_photo(callback):
    """Variante du sélecteur de fichier ci-dessus filtrée sur les photos
    JPEG (nécessaire pour l'onglet Photos)."""
    layout = BoxLayout(orientation="vertical", spacing=6, padding=6)
    chooser = FileChooserListView(path=DOSSIER_RACINE, filters=["*.jpg", "*.jpeg", "*.JPG", "*.JPEG"])
    layout.add_widget(chooser)

    boutons = BoxLayout(size_hint_y=None, height=48, spacing=6)
    btn_annuler = Button(text="Annuler")
    btn_valider = Button(text="Valider")
    boutons.add_widget(btn_annuler)
    boutons.add_widget(btn_valider)
    layout.add_widget(boutons)

    btn_valider.bind(on_release=lambda inst: callback(chooser.selection[0] if chooser.selection else None))
    btn_annuler.bind(on_release=lambda inst: callback(None))
    return layout


def _construire_confirmation_oui_non_annuler(message, callback):
    """Boîte de dialogue à 3 réponses (Oui / Non / Annuler), harmonisée
    avec les standards graphiques Android de l'application."""
    layout = BoxLayout(orientation="vertical", spacing=dp(12), padding=dp(16))

    lbl_message = Label(
        text=message,
        halign="center",
        valign="middle",
        color=(0, 0, 0, 1),
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
        self._popup = Popup(title="Choisir les traces à fusionner", content=contenu, size_hint=(0.95, 0.95))
        self._popup.open()

    def _fichiers_choisis(self, chemins):
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
            message = f"Fusion réussie !\nFichier généré : {os.path.basename(chemin_sortie)}"
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
    info_point_text = StringProperty("")
    status_text = StringProperty("")
    status_color = ListProperty([0.33, 0.33, 0.33, 1])

    # --- Bloc statut propre au suivi EN DIRECT (rouge), indépendant de
    # info_fichier/status_text ci-dessus qui concernent la trace
    # "chargée" manuellement (bleue).
    statut_live_text = StringProperty("Aucun live en cours.")
    statut_live_color = ListProperty([0.33, 0.33, 0.33, 1])

    # Identifiants propres à l'intégration GPSLogger, utilisés uniquement
    # par cet onglet : les garder ici les isole totalement des autres
    # onglets (les déplacer ou les supprimer avec l'onglet n'affecte
    # aucun autre onglet).
    PORT_SERVEUR_LIVE = 8765
    PACKAGE_GPSLOGGER = "com.mendhak.gpslogger"
    ACTION_TASKER_GPSLOGGER = "com.mendhak.gpslogger.TASKER_COMMAND"
    RECEIVER_TASKER_GPSLOGGER = "com.mendhak.gpslogger.TaskerReceiver"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.map_view = None
        self.trace_layer = None
        self.marqueurs_actifs = []
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
        # Appui long sur le graphique -> appareil photo, uniquement si
        # un live est actif (voir _verifier_et_ouvrir_camera). Limité au
        # widget du graphique lui-même (et non plus à tout l'écran, y
        # compris la carte).
        self.graphe.callback_long_press = self._verifier_et_ouvrir_camera
        self.ids.zone_graphique.add_widget(self.graphe)
        
        self.en_cours_live = False  # Indique si le live est actif ou non

        if CARTE_DISPONIBLE:
            self.map_view = MapViewMolette(zoom=6, lat=46.603354, lon=1.888334, map_source=SOURCE_SATELLITE)
            self.map_view.freeze_callback = self.basculer_freeze
            self.ids.map_container.add_widget(self.map_view)
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

    def dezoomer_carte(self):
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        min_z = getattr(getattr(self.map_view, "map_source", None), "min_zoom", 0)
        if self.map_view.zoom > min_z:
            self.map_view.zoom -= 1
            self.map_view.center_on(self.map_view.lat, self.map_view.lon)

    def zoomer_carte(self):
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        max_z = getattr(getattr(self.map_view, "map_source", None), "max_zoom", 19)
        if self.map_view.zoom < max_z:
            self.map_view.zoom += 1
            self.map_view.center_on(self.map_view.lat, self.map_view.lon)

    def changer_vue_carte(self, valeur):
        if not CARTE_DISPONIBLE or self.map_view is None:
            return
        self.map_view.map_source = SOURCE_SATELLITE if valeur == "satellite" else SOURCE_PLAN
        self.map_view.trigger_update(True)

    def ouvrir_selecteur_fichier(self):
        contenu = _construire_selecteur_fichier(self._fichier_choisi)
        self._popup = Popup(title="Choisir un fichier", content=contenu, size_hint=(0.95, 0.95))
        self._popup.open()

    def _fichier_choisi(self, chemin):
        self._popup.dismiss()
        if not chemin:
            return
        try:
            points = gps_logic.lire_fichier_pour_conversion(chemin)
        except Exception as e:
            self.info_fichier = f"Erreur de lecture : {e}"
            return

        if not points:
            self.info_fichier = "Aucun point GPS trouvé dans ce fichier."
            return

        self.points_courants = points
        self.info_fichier = f"Trace à suivre : {os.path.basename(chemin)}."
        
        # --- AJOUT : Calcul et affichage du profil (sans courbe de vitesse) ---
        self.profil = gps_logic.calculer_profil(points)
        self.graphe.set_donnees(*self.profil)
        
        self._afficher_trace_sur_carte(points)

    def _afficher_trace_sur_carte(self, points):
        """Affiche la polyligne de la trace et les marqueurs D/A, puis zoome sur l'emprise."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return

        if self.trace_layer is not None:
            self.map_view.remove_layer(self.trace_layer)
            self.trace_layer = None
        for m in self.marqueurs_actifs:
            self.map_view.remove_marker(m)
        self.marqueurs_actifs = []

        if not points:
            return

        liste_coords = [(p['lat'], p['lon']) for p in points]
        self.trace_layer = TraceLayer()
        self.map_view.add_layer(self.trace_layer)
        self.trace_layer.set_points(liste_coords)

        dist_dep_arr = gps_logic.calculer_distance_haversine(
            points[0]['lat'], points[0]['lon'], points[-1]['lat'], points[-1]['lon']
        )
        if dist_dep_arr <= 20.0:
            m_unique = MarqueurTexte(texte="D/A", lat=points[0]['lat'], lon=points[0]['lon'])
            self.map_view.add_marker(m_unique)
            self.marqueurs_actifs.append(m_unique)
        else:
            m_depart = MarqueurTexte(texte="D", lat=points[0]['lat'], lon=points[0]['lon'])
            m_arrivee = MarqueurTexte(texte="A", lat=points[-1]['lat'], lon=points[-1]['lon'])
            self.map_view.add_marker(m_depart)
            self.map_view.add_marker(m_arrivee)
            self.marqueurs_actifs.extend([m_depart, m_arrivee])

        lats = [c[0] for c in liste_coords]
        lons = [c[1] for c in liste_coords]
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)

        self.map_view.center_on((min_lat + max_lat) / 2, (min_lon + max_lon) / 2)
        max_delta = max(max_lat - min_lat, max_lon - min_lon)
        if max_delta > 0:
            zoom = int(12 - math.log2(max_delta * 10))
            self.map_view.zoom = max(2, min(zoom, 18))
        
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
            "/storage/emulated/0/GPSLoggerTraces",
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

        # Journal silencieux des sources, redémarré à partir de
        # maintenant : les points déjà présents dans le fichier n'ont
        # pas d'information de source disponible (elle ne nous parvient
        # que via le serveur d'écoute live) — seuls les nouveaux points
        # reçus en direct à partir d'ici seront comptés.
        self.compteur_sources_live = {}

        if points:
            self._afficher_trace_live_sur_carte()

            self.profil_live = gps_logic.calculer_profil(points)
            distances_km, distances_ele, altitudes, vitesses_kmh = self.profil_live
            self.graphe.set_donnees_secondaires(distances_km, distances_ele, altitudes)

            dernier = points[-1]
            idx = len(points) - 1
            dist = distances_km[idx] if idx < len(distances_km) else 0.0
            vit = vitesses_kmh[idx] if idx < len(vitesses_kmh) else 0.0
            heure = dernier['time'].strftime("%H:%M:%S") if dernier.get('time') else "-"
            ele_txt = f"{dernier['ele']} m" if dernier.get('ele') is not None else "-"
            self.info_point_text = (
                f"Point {idx + 1} (live)  |  GPS: {dernier['lat']:.5f}, {dernier['lon']:.5f}\n"
                f"Distance: {dist:.2f} km  |  Altitude: {ele_txt}  |  "
                f"Heure: {heure}  |  Vitesse: {vit} km/h"
            )

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

    def on_click_live_pydroid(self):
        """Bouton "Live" (onglet 7) :
        Étape 0 : vérifie d'abord si GPSLogger n'est pas déjà à l'état
        actif (trace déjà en cours d'enregistrement, bouton vert
        "Arrêter l'enregistrement"). GPSLogger n'offrant aucune API pour
        interroger directement son état, la détection se fait en
        observant si son dernier fichier .gpx continue de grossir : on
        compte ses lignes maintenant, puis on recompte 20 secondes plus
        tard (voir _verifier_gpslogger_actif_suite). Un nombre de lignes
        qui a grossi signifie qu'un enregistrement est en cours ; sinon,
        on considère qu'il n'y a pas d'enregistrement en cours.

        - Si un enregistrement est en cours : tous les points déjà
          enregistrés de cette trace sont affichés (carte + graphique)
          et l'affichage live se poursuit à partir de là.
        - Sinon (ou si aucun fichier .gpx n'existe) : la séquence
          habituelle démarre un nouveau suivi (_demarrer_nouveau_suivi_
          live), exactement comme avant.

        Ne touche jamais à la trace "chargée" manuellement (bleue,
        gérée par ouvrir_selecteur_fichier/_fichier_choisi ci-dessus) ni
        à aucun autre onglet."""
        chemin_candidat = self._trouver_dernier_gpx_gpslogger()
        if chemin_candidat is None:
            self._demarrer_nouveau_suivi_live()
            return

        try:
            nb_lignes_reference = self._compter_lignes(chemin_candidat)
        except OSError as e:
            print(f"[Live GPSLogger] Impossible de lire {chemin_candidat} pour la détection ({e}) : nouveau suivi.")
            self._demarrer_nouveau_suivi_live()
            return

        # Le nom du fichier candidat est affiché ici (temporairement) :
        # s'il n'apparaît jamais à l'écran après un clic sur "Live",
        # c'est que _trouver_dernier_gpx_gpslogger() ne trouve aucun
        # fichier dans les dossiers surveillés (GPSLogger utilise
        # probablement un dossier de sortie différent de ceux listés
        # dans cette méthode).
        self._maj_statut_live(
            f"Vérification de GPSLogger... ({os.path.basename(chemin_candidat)})",
            (0.33, 0.33, 0.33, 1)
        )
        Clock.schedule_once(
            lambda dt: self._verifier_gpslogger_actif_suite(chemin_candidat, nb_lignes_reference),
            20,
        )

    def _verifier_gpslogger_actif_suite(self, chemin, nb_lignes_reference):
        """Suite (unique, 20 secondes plus tard) de la détection démarrée
        par on_click_live_pydroid : si le fichier a grossi depuis le
        premier comptage (nb_lignes_reference), GPSLogger est bien en
        train d'enregistrer une trace. Sinon, démarre un nouveau suivi
        normalement."""
        try:
            nb_lignes_actuel = self._compter_lignes(chemin)
        except OSError:
            nb_lignes_actuel = nb_lignes_reference

        if nb_lignes_actuel != nb_lignes_reference:
            self._reprendre_trace_gpslogger_active(chemin)
        else:
            self._demarrer_nouveau_suivi_live()

    def _demarrer_nouveau_suivi_live(self):
        """Séquence normale de démarrage du suivi en direct (bouton
        "Live") — appelée par on_click_live_pydroid quand GPSLogger
        n'est pas déjà détecté comme étant en train d'enregistrer une
        trace :
        Phase 1 : réinitialise le suivi EN DIRECT (rouge) de cet onglet.
        Phase 2 : démarre (ou confirme déjà démarré) le serveur d'écoute
        live local qui reçoit les points GPS envoyés par GPSLogger.
        Phase 3 : tente de lancer GPSLogger et d'y démarrer
        automatiquement l'enregistrement (best effort : pyjnius, puis
        commande "am" en secours).

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
        self._maj_statut_live("Démarrage du suivi en direct : lancement de GPSLogger...", (0.937, 0.424, 0.0, 1))  # #EF6C00

        # --- Phase 2 : démarrage (ou confirmation) du serveur d'écoute live ---
        # Fait AVANT la phase 3 : demarrer_serveur_live() affiche son
        # propre message transitoire ("Serveur d'écoute live démarré
        # sur ...") aussitôt remplacé par celui de la phase 3 ci-dessous,
        # qui doit rester le message final visible après un clic sur
        # "Live".
        self.demarrer_serveur_live()

        # --- Phase 3 : lancement de GPSLogger + démarrage de l'enregistrement ---
        ok, message = self._lancer_gpslogger_et_demarrer_enregistrement()
        if ok:
            self._maj_statut_live(
                f"Live en cours... ({len(self.points_trace_live)} points)",
                (0.180, 0.490, 0.196, 1)  # #2E7D32
            )
        else:
            self._maj_statut_live(
                f"Enregistrement impossible. Veuillez installer l'application << GPSLogger for Android (Mendhak) >> pour continuer.",
                (0.776, 0.157, 0.157, 1)  # #C62828
            )

    def _maj_statut_live(self, texte, couleur=(0.33, 0.33, 0.33, 1)):
        """Affiche un message à la fois dans la console et dans le label
        de statut de cet onglet, pour rester visible même si la console
        n'est pas accessible (usage mobile). Equivalent de
        _maj_statut_live() dans la version desktop."""
        print(f"[Live GPSLogger] {texte}")
        self.statut_live_text = texte
        self.statut_live_color = list(couleur)

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

    def _ajouter_point_live(self, point):
        """Ajoute un nouveau point reçu en direct à la trace de cet
        onglet : étend le tracé sur la carte (rouge) et sa courbe
        d'altitude sur le graphique (rouge, superposée à celle de la
        trace chargée en bleu — voir set_donnees_secondaires), et met à
        jour le bloc d'informations avec ce dernier point."""
        if self.points_trace_live:
            dernier = self.points_trace_live[-1]
            if abs(dernier['lat'] - point['lat']) < 1e-6 and abs(dernier['lon'] - point['lon']) < 1e-6:
                return  # Point identique au dernier déjà affiché (doublon) : ignoré.

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

        self._maj_statut_live(
            f"Live en cours... ({len(self.points_trace_live)} points)",
            (0.180, 0.490, 0.196, 1)  # #2E7D32
        )

        idx = len(self.points_trace_live) - 1
        dist = distances_km[idx] if idx < len(distances_km) else 0.0
        vit = vitesses_kmh[idx] if idx < len(vitesses_kmh) else 0.0
        heure = point['time'].strftime("%H:%M:%S") if point.get('time') else "-"
        ele_txt = f"{point['ele']} m" if point.get('ele') is not None else "-"
        self.info_point_text = (
            f"Point {idx + 1} (live)  |  GPS: {point['lat']:.5f}, {point['lon']:.5f}\n"
            f"Distance: {dist:.2f} km  |  Altitude: {ele_txt}  |  "
            f"Heure: {heure}  |  Vitesse: {vit} km/h"
        )


    def _afficher_trace_live_sur_carte(self):
        """Affiche, sur la carte de cet onglet, la trace suivie EN
        DIRECT (rouge) : chemin et marqueurs qui lui sont propres, sans
        jamais toucher au chemin/marqueurs de la trace chargée
        manuellement (cyan, voir _fichier_choisi/_afficher_trace_sur_carte
        ci-dessus)."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return

        if self.trace_layer_live is not None:
            self.map_view.remove_layer(self.trace_layer_live)
            self.trace_layer_live = None
        for m in self.marqueurs_actifs_live:
            self.map_view.remove_marker(m)
        self.marqueurs_actifs_live = []

        points = self.points_trace_live
        if not points:
            return

        liste_coords = [(p['lat'], p['lon']) for p in points]
        self.trace_layer_live = TraceLayer(couleur=(0.898, 0.224, 0.208, 1))  # rouge #E53935
        self.map_view.add_layer(self.trace_layer_live)
        self.trace_layer_live.set_points(liste_coords)

        if len(points) >= 2:
            dist_dep_arr = gps_logic.calculer_distance_haversine(
                points[0]['lat'], points[0]['lon'], points[-1]['lat'], points[-1]['lon']
            )
            if dist_dep_arr <= 20.0:
                m_unique = MarqueurTexte(texte="D/A", lat=points[0]['lat'], lon=points[0]['lon'])
                self.map_view.add_marker(m_unique)
                self.marqueurs_actifs_live.append(m_unique)
            else:
                m_depart = MarqueurTexte(texte="D", lat=points[0]['lat'], lon=points[0]['lon'])
                m_arrivee = MarqueurTexte(texte="A", lat=points[-1]['lat'], lon=points[-1]['lon'])
                self.map_view.add_marker(m_depart)
                self.map_view.add_marker(m_arrivee)
                self.marqueurs_actifs_live.extend([m_depart, m_arrivee])
        else:
            m_unique = MarqueurTexte(texte="D", lat=points[0]['lat'], lon=points[0]['lon'])
            self.map_view.add_marker(m_unique)
            self.marqueurs_actifs_live.append(m_unique)

        self.map_view.center_on(points[-1]['lat'], points[-1]['lon'])

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

        self._maj_statut_live("Reprise du suivi en direct.", (0.180, 0.490, 0.196, 1))  # #2E7D32
        Clock.schedule_once(
            lambda dt: self._maj_statut_live(
                f"Live en cours... ({len(self.points_trace_live)} points)",
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

                    gps_logic.exporter_vers_gpx(self.points_trace_live, chemin_sortie, garder_temps=True)
                    self._maj_statut_live(f"Trace enregistrée : {os.path.basename(chemin_sortie)}", (0.180, 0.490, 0.196, 1))
                except Exception as e:
                    self._maj_statut_live(f"Erreur lors de l'enregistrement de la trace : {e}", (0.776, 0.157, 0.157, 1))

                self._arreter_gpslogger()
                self._maj_statut_live("Enregistrement terminé. Arrêtez GPSLogger manuellement.", (0.937, 0.424, 0.0, 1)) # #EF6C00

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

        # --- Arrêt automatique de l'enregistrement (si "Non" a été choisi)
        self._arreter_gpslogger()
        self._reinitialiser_onglet7_vierge()

    def _arreter_gpslogger(self):
        """Opération inverse de _lancer_gpslogger_et_demarrer_enregistrement :
           a) ordonne à GPSLogger d'arrêter l'enregistrement en cours
              (extra Android "immediatestop", symétrique de
              "immediatestart") ;
           b) tente ensuite de fermer l'application (best effort :
              Android n'autorise pas une appli tierce non-rootée à
              forcer l'arrêt d'une autre application de façon garantie ;
              killBackgroundProcesses est tenté, mais peut ne pas
              fonctionner selon l'appareil/la version d'Android,
              notamment si GPSLogger est encore au premier plan).

        Renvoie (ok_arret_enregistrement, ok_fermeture, détail). Ne lève
        jamais d'exception."""
        # --- Écriture silencieuse du log de comptage par source de
        # géolocalisation (aucun message, comme demandé). Toujours
        # tentée en tout premier, indépendamment du succès du reste de
        # cette méthode (automatisation Android best-effort ci-dessous).
        try:
            dossier_cible = DOSSIER_SORTIE if os.path.exists(DOSSIER_SORTIE) else DOSSIER_RACINE
            os.makedirs(dossier_cible, exist_ok=True)
            nom_log = f"log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            chemin_log = os.path.join(dossier_cible, nom_log)
            with open(chemin_log, "w", encoding="utf-8") as f:
                for source, nb in sorted(self.compteur_sources_live.items()):
                    f.write(f"{source} : {nb}\n")
        except Exception:
            pass
        finally:
            self.compteur_sources_live = {}

        ok_stop = False
        ok_fermeture = False
        details = []

        # --- a) Arrêt de l'enregistrement (fiable, documenté par GPSLogger) ---
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

            intent_arret = Intent(self.ACTION_TASKER_GPSLOGGER)
            intent_arret.setClassName(self.PACKAGE_GPSLOGGER, self.RECEIVER_TASKER_GPSLOGGER)
            intent_arret.putExtra("immediatestop", True)
            contexte.sendBroadcast(intent_arret)
            ok_stop = True
            details.append("enregistrement arrêté (pyjnius)")

            # --- b) Tentative de fermeture de l'application (best effort) ---
            try:
                gestionnaire_activites = cast(
                    "android.app.ActivityManager",
                    contexte.getSystemService(activite_courante.ACTIVITY_SERVICE)
                )
                gestionnaire_activites.killBackgroundProcesses(self.PACKAGE_GPSLOGGER)
                ok_fermeture = True
                details.append("fermeture tentée (killBackgroundProcesses)")
            except Exception:
                # On évite volontairement d'afficher le détail technique
                # brut de l'exception Android (souvent une longue trace
                # Java/Parcel illisible et sans intérêt pour
                # l'utilisateur) : un message court et indicatif suffit,
                # l'essentiel (l'arrêt de l'enregistrement, lui, réussi)
                # étant déjà remonté à part.
                details.append("fermeture non autorisée par Android sur cet appareil")

            return ok_stop, ok_fermeture, " / ".join(details)
        except Exception as e_jnius:
            print(f"[Live GPSLogger] Échec pyjnius (arrêt) : {e_jnius}")
            raison_jnius = "méthode pyjnius indisponible"

        # --- Secours : commande Android "am", si disponible ---
        try:
            resultat = subprocess.run(
                [
                    "am", "broadcast",
                    "-a", self.ACTION_TASKER_GPSLOGGER,
                    "-n", f"{self.PACKAGE_GPSLOGGER}/{self.RECEIVER_TASKER_GPSLOGGER}",
                    "--ez", "immediatestop", "true"
                ],
                check=False, timeout=5,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            if resultat.returncode == 0:
                # La fermeture complète via "am force-stop" nécessite des
                # privilèges (root/ADB) qu'une appli normale n'a pas :
                # non tentée ici pour éviter un échec silencieux trompeur.
                return True, False, "enregistrement arrêté (commande am), fermeture non tentée (nécessite root)"
            print(f"[Live GPSLogger] Échec commande am (arrêt), code {resultat.returncode} : "
                  f"{resultat.stderr.decode(errors='ignore').strip()}")
            raison_am = "commande am indisponible ou refusée"
        except Exception as e_am:
            print(f"[Live GPSLogger] Échec commande am (arrêt) : {e_am}")
            raison_am = "commande am indisponible ou refusée"

        return False, False, f"{raison_jnius} ; {raison_am}"

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

        self.profil = ([], [], [], [])
        self.profil_live = ([], [], [], [])
        self.graphe.set_donnees(*self.profil)
        self.graphe.effacer_donnees_secondaires()
        self.info_point_text = ""

        self._maj_statut_live("Aucun live en cours.", (0.33, 0.33, 0.33, 1))

    def ouvrir_camera_android(self):
        """Ouvre l'application caméra de l'appareil Android."""
        if platform == "android":
            try:
                from jnius import autoclass
                Intent = autoclass('android.content.Intent')
                MediaStore = autoclass('android.provider.MediaStore')
                PythonActivity = autoclass('org.kivy.android.PythonActivity')
                
                intent = Intent(MediaStore.ACTION_IMAGE_CAPTURE)
                current_activity = PythonActivity.mActivity
                current_activity.startActivity(intent)
                self.status_text = "Caméra ouverte."
            except Exception as e:
                self.status_text = f"Erreur ouverture caméra : {e}"
        else:
            self.status_text = "Fonction caméra disponible uniquement sur Android."

    def _verifier_et_ouvrir_camera(self):
        """Vérifie si un live est en cours avant d'ouvrir l'appareil photo."""
        # --- AJOUT : Bloque l'ouverture de la caméra si l'écran est gelé ---
        if getattr(self, 'freeze_actif', False):
            return
            
        if not self.en_cours_live:
            self._maj_statut_live(
                "Impossible d'ouvrir l'appareil photo : aucun live n'est en cours.",
                (0.776, 0.157, 0.157, 1)  # #C62828
            )
            return

    def basculer_freeze(self):
        # Bascule l'état du gel
        self.freeze_actif = not self.freeze_actif
        
        if getattr(self.map_view, 'freeze_actif', None) is not None:
            self.map_view.freeze_actif = self.freeze_actif

        # --- MODIFICATION ICI : Au dégel de l'onglet ---
        if not self.freeze_actif:
            if self.points_trace_live:
                # Récupère le dernier point enregistré
                dernier_point = self.points_trace_live[-1]
                idx = len(self.points_trace_live) - 1
                
                # Recalcule les données du profil pour s'assurer d'avoir les bonnes valeurs à jour
                distances_km, _, _, vitesses_kmh = self.profil_live
                
                dist = distances_km[idx] if idx < len(distances_km) else 0.0
                vit = vitesses_kmh[idx] if idx < len(vitesses_kmh) else 0.0
                heure = dernier_point['time'].strftime("%H:%M:%S") if dernier_point.get('time') else "-"
                ele_txt = f"{dernier_point['ele']} m" if dernier_point.get('ele') is not None else "-"
                
                # Met à jour la première ligne de texte séquentiel avec le nouveau compte de points mis à jour
                self.info_point_text = (
                    f"Point {idx + 1} (live)  |  GPS: {dernier_point['lat']:.5f}, {dernier_point['lon']:.5f}\n"
                    f"Distance: {dist:.2f} km  |  Altitude: {ele_txt}  |  "
                    f"Heure: {heure}  |  Vitesse: {vit} km/h"
                )
            else:
                self.info_point_text = "Aucun point live enregistré."
            
class CarteScreen(Screen):
    fichier_source = StringProperty("")
    info_fichier = StringProperty("Aucune trace chargée.")
    trace_chargee = BooleanProperty(False)
    point_coupure_text = StringProperty("")
    status_text = StringProperty("")
    status_color = ListProperty([0.33, 0.33, 0.33, 1])
    en_cours = BooleanProperty(False)
    info_point_text = StringProperty("")

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
        self.marqueur_curseur = None
        self.trace_layer = None
        self.map_view = None
        self.profil = ([], [], [], [])

        self.graphe = GrapheProfil()
        self.graphe.callback_clic = self._sur_clic_graphique
        self.ids.zone_graphique.add_widget(self.graphe)

        if CARTE_DISPONIBLE:
            self.map_view = MapViewMolette(zoom=6, lat=46.603354, lon=1.888334, map_source=SOURCE_SATELLITE)
            # On écoute les touchers au niveau de la Window, complètement
            # à l'écart du Scatter interne de MapView (qui gère lui-même
            # le glisser/pincement). Un binding ou un grab sur le Scatter
            # ou sur MapView empêcherait ce dernier de recevoir l'événement
            # et bloquerait le glisser — ce qu'on a observé en pratique.
            Window.bind(on_touch_down=self._debut_touch_carte, on_touch_up=self._sur_touch_carte)
            self.ids.map_container.add_widget(self.map_view)
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
        self.map_view.map_source = SOURCE_SATELLITE if valeur == "satellite" else SOURCE_PLAN
        # L'affectation seule ne suffit pas toujours à relancer le
        # chargement des tuiles : on force explicitement un rafraîchissement
        # complet (sinon le fond peut rester gris-bleu / ne pas revenir).
        self.map_view.trigger_update(True)

    def ouvrir_selecteur_fichier(self):
        contenu = _construire_selecteur_fichier(self._fichier_choisi)
        self._popup = Popup(title="Choisir un fichier", content=contenu, size_hint=(0.95, 0.95))
        self._popup.open()

    def _fichier_choisi(self, chemin):
        self._popup.dismiss()
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
        self.info_fichier = f"Trace : {os.path.basename(chemin)}\n{len(points)} points."
        self.info_point_text = "Tape sur la carte ou le graphique pour voir le détail d'un point."
        self.profil = gps_logic.calculer_profil(points)
        self.graphe.set_donnees(*self.profil)
        self._afficher_trace_sur_carte(points)

    def _afficher_trace_sur_carte(self, points):
        """Equivalent de afficher_trace_sur_carte() dans la version
        desktop : trace la polyligne, place les marqueurs D/A, centre
        et zoome la carte sur l'emprise de la trace."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return

        if self.trace_layer is not None:
            self.map_view.remove_layer(self.trace_layer)
            self.trace_layer = None
        for m in self.marqueurs_actifs:
            self.map_view.remove_marker(m)
        self.marqueurs_actifs = []
        if self.marqueur_curseur is not None:
            self.map_view.remove_marker(self.marqueur_curseur)
            self.marqueur_curseur = None

        if not points:
            return

        liste_coords = [(p['lat'], p['lon']) for p in points]
        self.trace_layer = TraceLayer()
        self.map_view.add_layer(self.trace_layer)
        self.trace_layer.set_points(liste_coords)

        dist_dep_arr = gps_logic.calculer_distance_haversine(
            points[0]['lat'], points[0]['lon'], points[-1]['lat'], points[-1]['lon']
        )
        if dist_dep_arr <= 20.0:
            m_unique = MarqueurTexte(texte="D/A", lat=points[0]['lat'], lon=points[0]['lon'])
            self.map_view.add_marker(m_unique)
            self.marqueurs_actifs.append(m_unique)
        else:
            m_depart = MarqueurTexte(texte="D", lat=points[0]['lat'], lon=points[0]['lon'])
            m_arrivee = MarqueurTexte(texte="A", lat=points[-1]['lat'], lon=points[-1]['lon'])
            self.map_view.add_marker(m_depart)
            self.map_view.add_marker(m_arrivee)
            self.marqueurs_actifs.extend([m_depart, m_arrivee])

        lats = [c[0] for c in liste_coords]
        lons = [c[1] for c in liste_coords]
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)

        self.map_view.center_on((min_lat + max_lat) / 2, (min_lon + max_lon) / 2)
        max_delta = max(max_lat - min_lat, max_lon - min_lon)
        if max_delta > 0:
            zoom = int(12 - math.log2(max_delta * 10))
            self.map_view.zoom = max(2, min(zoom, 18))

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
            self.marqueur_curseur = MapMarker(lat=p['lat'], lon=p['lon'])
            self.map_view.add_marker(self.marqueur_curseur)
            if recentrer_carte:
                self.map_view.center_on(p['lat'], p['lon'])

        distances_km, _, _, vitesses_kmh = self.profil
        dist = distances_km[idx] if idx < len(distances_km) else 0.0
        vit = vitesses_kmh[idx] if idx < len(vitesses_kmh) else 0.0
        heure = p['time'].strftime("%H:%M:%S") if p['time'] else "-"
        ele_txt = f"{p['ele']} m" if p['ele'] is not None else "-"
        self.info_point_text = (
            f"Point {idx + 1}/{len(self.points_courants)}  |  "
            f"GPS: {p['lat']:.5f}, {p['lon']:.5f}\n"
            f"Distance: {dist:.2f} km  |  Altitude: {ele_txt}  |  "
            f"Heure: {heure}  |  Vitesse: {vit} km/h"
        )
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
            message = f"Découpe réussie en 2 fichiers :\n{os.path.basename(c1)}\n{os.path.basename(c2)}"
            couleur = [0.15, 0.5, 0.15, 1]
        except Exception as e:
            message = f"Échec de la découpe : {e}"
            couleur = [0.8, 0.1, 0.1, 1]

        def _maj_ui(dt):
            self.en_cours = False
            self.status_text = message
            self.status_color = couleur

        Clock.schedule_once(_maj_ui, 0)

class LigneStatistique(BoxLayout):
    libelle = StringProperty("")
    valeur = StringProperty("")
    couleur_fond = ListProperty([1, 1, 1, 1])


class StatistiquesScreen(Screen):
    info_fichier = StringProperty("Aucune trace chargée.")

    LIBELLES = [
        ("alt_depart", "Altitude de départ :"),
        ("alt_max", "Altitude maximale :"),
        ("distance", "Distance parcourue :"),
        ("den_pos", "Dénivelé positif :"),
        ("km_effort", "Kilomètre-Effort :"),
        ("temps_total", "Temps total :"),
        ("temps_marche", "Temps sans pauses :"),
        ("vit_moy", "Vitesse moyenne :"),
        ("allure", "Allure moyenne :"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._afficher_tableau({cle: "-" for cle, _ in self.LIBELLES})

    def ouvrir_selecteur_fichier(self):
        contenu = _construire_selecteur_fichier(self._fichier_choisi)
        self._popup = Popup(title="Choisir un fichier", content=contenu, size_hint=(0.95, 0.95))
        self._popup.open()

    def _fichier_choisi(self, chemin):
        self._popup.dismiss()
        if not chemin:
            return
        try:
            points = gps_logic.lire_fichier_pour_conversion(chemin)
        except Exception as e:
            self.info_fichier = f"Erreur de lecture : {e}"
            return

        if not points:
            self.info_fichier = "Aucun point GPS valide n'a pu être extrait de ce fichier."
            return

        self.info_fichier = f"Trace : {os.path.basename(chemin)}"
        stats = gps_logic.calculer_statistiques(points)
        self._afficher_tableau(stats)

    def _afficher_tableau(self, valeurs):
        conteneur = self.ids.tableau_stats
        conteneur.clear_widgets()
        couleurs = [(0.973, 0.976, 0.980, 1), (0.925, 0.933, 0.945, 1)]
        for i, (cle, libelle) in enumerate(self.LIBELLES):
            ligne = LigneStatistique(
                libelle=libelle,
                valeur=valeurs.get(cle, "-"),
                couleur_fond=couleurs[i % 2],
            )
            conteneur.add_widget(ligne)


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

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.fichier_trace = ""
        self.points_trace = []
        self.fichier_photo = ""
        self.trace_layer = None
        self.marqueurs_actifs = []
        self.marqueur_photo = None
        self.map_view = None

        if CARTE_DISPONIBLE:
            self.map_view = MapViewMolette(zoom=6, lat=46.603354, lon=1.888334, map_source=SOURCE_SATELLITE)
            self.ids.map_container.add_widget(self.map_view)
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
        self.map_view.map_source = SOURCE_SATELLITE if valeur == "satellite" else SOURCE_PLAN
        self.map_view.trigger_update(True)

    def ouvrir_selecteur_trace(self):
        contenu = _construire_selecteur_fichier(self._trace_choisie)
        self._popup = Popup(title="Choisir une trace", content=contenu, size_hint=(0.95, 0.95))
        self._popup.open()

    def _trace_choisie(self, chemin):
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

        self.fichier_trace = chemin
        self.points_trace = points
        self.info_trace = f"Trace : {os.path.basename(chemin)}."
        self._afficher_trace_sur_carte(points)

    def ouvrir_selecteur_photo(self):
        contenu = _construire_selecteur_fichier_photo(self._photo_choisie)
        self._popup = Popup(title="Choisir une photo", content=contenu, size_hint=(0.95, 0.95))
        self._popup.open()

    def _photo_choisie(self, chemin):
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

    def situer(self):
        """Cherche dans la trace le point le plus proche de la date/heure
        EXIF saisie et pré-remplit latitude/longitude/altitude,
        équivalent de situer_exif_edite() dans la version desktop."""
        if not self.champ_date.strip():
            self.status_text = "Renseigne une date/heure pour la photo."
            self.status_color = [0.8, 0.1, 0.1, 1]
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

    def _afficher_trace_sur_carte(self, points):
        """Trace la polyligne sur la carte et recadre dessus, équivalent
        de afficher_trace_sur_carte_photo() dans la version desktop."""
        if not CARTE_DISPONIBLE or self.map_view is None:
            return

        if self.trace_layer is not None:
            self.map_view.remove_layer(self.trace_layer)
            self.trace_layer = None
        for m in self.marqueurs_actifs:
            self.map_view.remove_marker(m)
        self.marqueurs_actifs = []
        if self.marqueur_photo is not None:
            self.map_view.remove_marker(self.marqueur_photo)
            self.marqueur_photo = None

        if not points:
            return

        liste_coords = [(p["lat"], p["lon"]) for p in points]
        self.trace_layer = TraceLayer()
        self.map_view.add_layer(self.trace_layer)
        self.trace_layer.set_points(liste_coords)

        lats = [c[0] for c in liste_coords]
        lons = [c[1] for c in liste_coords]
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)
        self.map_view.center_on((min_lat + max_lat) / 2, (min_lon + max_lon) / 2)
        max_delta = max(max_lat - min_lat, max_lon - min_lon)
        if max_delta > 0:
            zoom = int(12 - math.log2(max_delta * 10))
            self.map_view.zoom = max(2, min(zoom, 18))


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


class OutilsTracesApp(App):
    title = "Bubu GPS"

    def build(self):
        Window.clearcolor = (0.96, 0.97, 0.98, 1)

        Builder.load_string(KV)

        self.sm = ScreenManager()
        self.sm.add_widget(ConversionScreen(name="conversion"))
        self.sm.add_widget(NumerotationScreen(name="numerotation"))
        self.sm.add_widget(FusionScreen(name="fusion"))
        self.sm.add_widget(CarteScreen(name="carte"))
        self.sm.add_widget(StatistiquesScreen(name="statistiques"))
        self.sm.add_widget(PhotosScreen(name="photos"))
        self.sm.add_widget(LiveScreen(name="Live"))

        # --- Barre du haut : menu déroulant (gauche) + titre + Quitter (droite) ---
        barre = BoxLayout(size_hint_y=None, height=60, padding=(8, 4), spacing=8)

        self.dropdown = DropDown(auto_width=False, width=220)
        self._ecrans_menu = [
            ("conversion", "Conversion"), 
            ("numerotation", "Numérotation"), 
            ("fusion", "Fusion"), 
            ("carte", "Carte / Découpe"), 
            ("statistiques", "Statistiques"), 
            ("photos", "Photos"), 
            ("Live", "Live")
        ]
        if 'SCREENS_A_VENIR' in globals():
            self._ecrans_menu += [(nom, nom) for nom in SCREENS_A_VENIR]
            
        self._boutons_menu = {}
        for nom_ecran, libelle in self._ecrans_menu:
            btn = Button(text=libelle, size_hint_y=None, height=48, font_size="16sp")
            btn.bind(on_release=lambda b, n=nom_ecran: self._changer_ecran(n))
            self.dropdown.add_widget(btn)
            self._boutons_menu[nom_ecran] = btn

        self.btn_menu = Button(text="Menu", size_hint_x=None, width=110)
        self.btn_menu.bind(on_release=self._ouvrir_menu)
        barre.add_widget(self.btn_menu)

        barre.add_widget(Label(text="Bubu GPS", bold=True, color=(1, 1, 1, 1)))

        self.btn_quitter = Button(text="Quitter", size_hint_x=None, width=110)
        self.btn_quitter.bind(on_release=lambda inst: self.stop())
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

        return racine

    def on_start(self):
        """Vérifie si l'application a été lancée en cliquant sur un fichier au démarrage."""
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

    def _sur_nouvel_intent(self, intent):
        """Appelée quand l'appli tourne déjà et qu'on clique sur un nouveau fichier."""
        self._traiter_intent_fichier(intent)

    def _traiter_intent_fichier(self, intent):
        """Traite l'intent pour récupérer le fichier et l'envoyer sur l'écran carte."""
        try:
            from jnius import autoclass
            Intent = autoclass('android.content.Intent')
            action = intent.getAction()
            uri = intent.getData()
            
            if action != Intent.ACTION_VIEW or uri is None:
                return

            chemin = self._uri_vers_chemin_local(uri)
            if not chemin or not os.path.exists(chemin):
                print("[Intent] Impossible de résoudre ou de trouver le fichier ouvert.")
                return

            # Basculement vers l'écran carte et chargement de la trace
            self.sm.current = "carte"
            ecran_carte = self.sm.get_screen("carte")
            if ecran_carte and hasattr(ecran_carte, "charger_trace"):
                ecran_carte.charger_trace(chemin)
            elif ecran_carte and hasattr(ecran_carte, "_fichier_choisi"):
                ecran_carte._fichier_choisi(chemin)
                
        except Exception as e:
            print(f"[Intent] Erreur de traitement du fichier ouvert : {e}")

    def _uri_vers_chemin_local(self, uri):
        """Convertit une URI Android (file:// ou content://) vers un fichier local en cache."""
        from jnius import autoclass

        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        activite = PythonActivity.mActivity
        schema = uri.getScheme()

        if schema == "file":
            return uri.getPath()

        if schema != "content":
            return None

        resolveur = activite.getContentResolver()
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

    def _changer_ecran(self, nom_ecran):
        self.dropdown.dismiss()
        self.sm.current = nom_ecran
        
        live_screen = self.sm.get_screen("Live") if "Live" in self.sm.screen_names else None

        if nom_ecran == "Live" and live_screen:
            live_screen.unbind(freeze_actif=self._mettre_a_jour_gel_barre)
            live_screen.bind(freeze_actif=self._mettre_a_jour_gel_barre)
            self._mettre_a_jour_gel_barre(live_screen, live_screen.freeze_actif)
        else:
            if live_screen:
                live_screen.unbind(freeze_actif=self._mettre_a_jour_gel_barre)
            self.btn_menu.disabled = False
            self.btn_quitter.disabled = False

    def _mettre_a_jour_gel_barre(self, instance_live, est_gele):
        self.btn_menu.disabled = est_gele
        self.btn_quitter.disabled = est_gele
    
    def _demander_permissions_android(self):
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
            pass


if __name__ == "__main__":
    OutilsTracesApp().run()