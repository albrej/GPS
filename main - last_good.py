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
    
        def on_touch_down(self, touch):
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
            # Empêche le zoom par pincement en neutralisant l'effet multi-touch de la carte
            if touch.grab_current is not self and len(getattr(self, 'touches', [])) > 1:
                return True
            return super().on_touch_move(touch)
    
        def scale_at(self, *args, **kwargs):
            # Désactive l'ajustement d'échelle par pincement tactile
            return

    class TraceLayer(MapLayer):
        """Dessine la trace (polyligne cyan) par-dessus les tuiles,
        équivalent de map_widget.set_path(...) sous tkintermapview."""

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.points = []

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
                Color(0, 1, 1, 1)
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

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.distances_km = []
        self.distances_ele = []
        self.altitudes = []
        self.vitesses_kmh = []
        self.distance_selection = None
        self.callback_clic = None
        self.bind(pos=self._redessiner, size=self._redessiner)

    def set_donnees(self, distances_km, distances_ele, altitudes, vitesses_kmh):
        self.distances_km = distances_km
        self.distances_ele = distances_ele
        self.altitudes = altitudes
        self.vitesses_kmh = vitesses_kmh
        self.distance_selection = distances_km[0] if distances_km else None
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
        if not self.distances_km or self.width < dp(30) or self.height < dp(30):
            return

        ROUGE = (0.8, 0.1, 0.1, 1)
        BLEU = (0.12, 0.53, 0.90, 1)
        VERT = (0.18, 0.49, 0.20, 1)
        GRIS_TEXTE = (0.25, 0.25, 0.25, 1)

        zx, zy, zw, zh = self._zone_graphique()
        d_min, d_max = self.distances_km[0], self.distances_km[-1]
        d_span = max(d_max - d_min, 1e-6)

        # Décalage horizontal (en pixels) pour laisser place aux labels min/max rouges à gauche
        decalage_x = dp(42)
        zx_courbe = zx + decalage_x
        zw_courbe = max(1.0, zw - decalage_x)

        def x_ecran(d):
            return zx_courbe + (d - d_min) / d_span * zw_courbe

        a_ele = len(self.altitudes) >= 2
        a_vit = a_ele and any(v > 0 for v in self.vitesses_kmh)

        if a_ele:
            a_min, a_max = min(self.altitudes), max(self.altitudes)
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

            if a_ele:
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
                # Tracé de la courbe d'altitude
                points_ligne = []
                for d, a in zip(self.distances_ele, self.altitudes):
                    points_ligne.extend([x_ecran(d), y_alt(a)])
                Color(*BLEU)
                KivyLine(points=points_ligne, width=1.6)

                if a_vit:
                    for valeur in self._graduations(v_bas, v_haut, 4):
                        gy = y_vit(valeur)
                        self._poser_texte(f"{int(round(valeur))}", zx + zw + dp(4), gy, VERT,
                                           taille_sp=9, centre_v=True, gras=False)

                    # Tracé de la courbe de vitesse
                    points_vit = []
                    for d, v in zip(self.distances_km, self.vitesses_kmh):
                        points_vit.extend([x_ecran(d), y_vit(v)])
                    Color(*VERT)
                    KivyLine(points=points_vit, width=1.6)

            if self.distance_selection is not None:
                cx = x_ecran(self.distance_selection)
                Color(0.85, 0.1, 0.1, 0.9)
                KivyLine(points=[cx, zy, cx, zy + zh], width=1.4, dash_length=6, dash_offset=4)

            self._poser_texte("Distance (km)", zx + zw / 2, self.y, GRIS_TEXTE,
                               taille_sp=10, centre_h=True)
            if a_ele:
                self._poser_texte("Altitude (m)", zx, zy + zh + dp(4), BLEU, taille_sp=9)
            if a_vit:
                tex_v = self._texte_texture("Vitesse (km/h)", taille_sp=9)
                self._poser_texte("Vitesse (km/h)", zx + zw - tex_v.width, zy + zh + dp(4), VERT, taille_sp=9)

    def on_touch_down(self, touch):
        if not self.collide_point(*touch.pos) or not self.distances_km:
            return super().on_touch_down(touch)
    
        zx, zy, zw, zh = self._zone_graphique()
        decalage_x = dp(42)
        zx_courbe = zx + decalage_x
        zw_courbe = max(1.0, zw - decalage_x)
    
        d_min, d_max = self.distances_km[0], self.distances_km[-1]
        
        # Ratio du clic par rapport à la zone utile de la courbe
        rel_x = touch.x - zx_courbe
        ratio = max(0.0, min(1.0, rel_x / zw_courbe))
        distance_km_tapee = d_min + ratio * (d_max - d_min)
    
        if self.callback_clic:
            self.callback_clic(distance_km_tapee)
        return True

# ----------------------------------------------------------------------
# Dossier racine utilisé pour parcourir/enregistrer les fichiers.
# Sur Android, nécessite la permission "Accès à tous les fichiers"
# (voir README.md + buildozer.spec).
# ----------------------------------------------------------------------
if platform == "android":
    DOSSIER_RACINE = "/storage/emulated/0"
else:
    DOSSIER_RACINE = os.path.join(os.path.expanduser("~"), "Desktop", "GPX-Speed_ok")

DOSSIER_SORTIE = os.path.join(DOSSIER_RACINE, "TracesConverties")

# Fonctionnalités qui restent à intégrer (affichées dans le menu déroulant
# avec un écran "à venir" en attendant leur code Python).
SCREENS_A_VENIR = [
    "Live",
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
                text: "Conserver les heures / temps de passage"
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
                    text: "Inverser le sens de la trace (premier <-> dernier point)"
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
                    text: "Aucune action sur les numéros (garder tel quel)"
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
                    text: "Numéroter les points de trace (1, 2, 3...)"
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
                    text: "Tout dénuméroter (conserver tous les points sans numéro)"
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
                    text: "Supprimer des numéros précis (et leurs points GPS)"
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
                    text: "^ Monter"
                    on_release: root.monter()
                Button:
                    text: "v Descendre"
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
                    text: "Inverser le sens de cette trace (premier <-> dernier point)"
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

            BoxLayout:
                size_hint_y: None
                height: dp(215)
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

                BoxLayout:
                    size_hint_x: None
                    width: dp(140)
                    canvas.before:
                        Color:
                            rgba: 0.92, 0.92, 0.92, 1
                        Rectangle:
                            pos: self.pos
                            size: self.size
                    Image:
                        source: root.miniature_source
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
        self.info_fichier = f"Trace :\n{os.path.basename(chemin)}"
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
        self.info_trace = f"Trace : {os.path.basename(chemin)} ({len(points)} pts)"
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
        # Par défaut, Kivy affiche un fond NOIR uni tant qu'on ne le
        # change pas explicitement : tous les libellés en texte noir
        # étaient donc invisibles dessus. On passe à un fond clair.
        Window.clearcolor = (0.96, 0.97, 0.98, 1)

        Builder.load_string(KV)

        self.sm = ScreenManager()
        self.sm.add_widget(ConversionScreen(name="conversion"))
        self.sm.add_widget(NumerotationScreen(name="numerotation"))
        self.sm.add_widget(FusionScreen(name="fusion"))
        self.sm.add_widget(CarteScreen(name="carte"))
        self.sm.add_widget(StatistiquesScreen(name="statistiques"))
        self.sm.add_widget(PhotosScreen(name="photos"))
        for nom in SCREENS_A_VENIR:
            self.sm.add_widget(EcranAVenir(nom, name=nom))

        # --- Barre du haut : menu déroulant (gauche) + titre + Quitter (droite) ---
        barre = BoxLayout(size_hint_y=None, height=dp(60), padding=(8, 4), spacing=dp(8))

        self.dropdown = DropDown(auto_width=False, width=dp(220))
        self._ecrans_menu = [("conversion", "Conversion"), ("numerotation", "Numérotation"), ("fusion", "Fusion"), ("carte", "Carte / Découpe"), ("statistiques", "Statistiques"), ("photos", "Photos")]
        self._ecrans_menu += [(nom, nom) for nom in SCREENS_A_VENIR]
        self._boutons_menu = {}
        for nom_ecran, libelle in self._ecrans_menu:
            btn = Button(text=libelle, size_hint_y=None, height=dp(48), font_size="16sp")
            btn.bind(on_release=lambda b, n=nom_ecran: self._changer_ecran(n))
            self.dropdown.add_widget(btn)
            self._boutons_menu[nom_ecran] = btn

        btn_menu = Button(text="Menu", size_hint_x=None, width=dp(110))
        btn_menu.bind(on_release=self._ouvrir_menu)
        barre.add_widget(btn_menu)

        barre.add_widget(Label(text="Bubu GPS", bold=True, color=(1, 1, 1, 1)))

        btn_quitter = Button(text="Quitter", size_hint_x=None, width=dp(110))
        btn_quitter.bind(on_release=lambda inst: self.stop())
        barre.add_widget(btn_quitter)

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

        return racine

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
