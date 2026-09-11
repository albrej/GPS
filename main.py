# -*- coding: utf-8 -*-
"""
============================================================================
 OUTILS TRACES & PHOTOS — Application Android (Kivy)
 Réécriture de start.py (tkinter) pour fonctionner en APK autonome.

 - Onglet "Conversion" : entièrement fonctionnel.
 - Les 6 autres fonctionnalités (Numérotation, Fusion, Carte/Découpe,
   Statistiques, Photos, Live) sont déjà présentes dans le menu déroulant
   mais affichent un écran "à venir" tant que leur code n'est pas fourni
   et intégré. Voir screens/a_venir.py et SCREENS_A_VENIR ci-dessous.
============================================================================
"""

import os
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
from kivy.properties import StringProperty, BooleanProperty
from kivy.utils import platform

import gps_logic

# ----------------------------------------------------------------------
# Dossier racine utilisé pour parcourir/enregistrer les fichiers.
# Sur Android, nécessite la permission "Accès à tous les fichiers"
# (voir README.md + buildozer.spec).
# ----------------------------------------------------------------------
if platform == "android":
    DOSSIER_RACINE = "/storage/emulated/0"
else:
    DOSSIER_RACINE = os.path.expanduser("~")

DOSSIER_SORTIE = os.path.join(DOSSIER_RACINE, "TracesConverties")

# Fonctionnalités qui restent à intégrer (affichées dans le menu déroulant
# avec un écran "à venir" en attendant leur code Python).
SCREENS_A_VENIR = [
    "Numérotation",
    "Fusion",
    "Carte / Découpe",
    "Statistiques",
    "Photos",
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
            text: "Choisir une trace (GPX, KMZ, KML)"
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

        BoxLayout:
            size_hint_y: None
            height: dp(48)
            spacing: dp(6)
            Label:
                text: "Format de sortie :"
                color: 0, 0, 0, 1
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
            height: dp(48)
            CheckBox:
                id: chk_temps
                active: root.garder_temps
                on_active: root.garder_temps = self.active
            Label:
                text: "Conserver les heures / temps de passage"
                color: 0, 0, 0, 1
                text_size: self.width, None
                halign: "left"

        Button:
            text: "Convertir & enregistrer"
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
        self.info_fichier = f"Trace sélectionnée :\n{os.path.basename(chemin)}"
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
    title = "Outils Traces & Photos"

    def build(self):
        Builder.load_string(KV)

        self.sm = ScreenManager()
        self.sm.add_widget(ConversionScreen(name="conversion"))
        for nom in SCREENS_A_VENIR:
            self.sm.add_widget(EcranAVenir(nom, name=nom))

        # --- Barre du haut : titre + bouton menu déroulant ---
        barre = BoxLayout(size_hint_y=None, height=56, padding=(8, 4))
        barre.add_widget(Label(text="Outils Traces & Photos", bold=True, color=(1, 1, 1, 1)))

        self.dropdown = DropDown()
        for nom_ecran in ["conversion"] + SCREENS_A_VENIR:
            libelle = "Conversion" if nom_ecran == "conversion" else nom_ecran
            btn = Button(text=libelle, size_hint_y=None, height=44)
            btn.bind(on_release=lambda b, n=nom_ecran: self._changer_ecran(n))
            self.dropdown.add_widget(btn)

        btn_menu = Button(text="Menu \u25be", size_hint_x=None, width=110)
        btn_menu.bind(on_release=self.dropdown.open)
        barre.add_widget(btn_menu)

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
