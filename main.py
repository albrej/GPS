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
from kivy.properties import StringProperty, BooleanProperty, ListProperty
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
                height: dp(50)
                text_size: self.width, self.height
                halign: "left"
                valign: "middle"
                color: 0.2, 0.5, 0.2, 1

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
                height: dp(44)
                text_size: self.width, self.height
                halign: "left"
                valign: "middle"
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

            for cle, couleur, libelle in gps_logic.LEGENDE_NUMEROTATION:
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
        for nom in SCREENS_A_VENIR:
            self.sm.add_widget(EcranAVenir(nom, name=nom))

        # --- Barre du haut : menu déroulant (gauche) + titre + Quitter (droite) ---
        barre = BoxLayout(size_hint_y=None, height=dp(60), padding=(8, 4), spacing=dp(8))

        self.dropdown = DropDown(auto_width=False, width=dp(220))
        self._ecrans_menu = [("conversion", "Conversion"), ("numerotation", "Numérotation")]
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
