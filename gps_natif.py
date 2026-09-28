# -*- coding: utf-8 -*-
"""
============================================================================
 GPS NATIF ANDROID (pyjnius) — remplace GPSLogger pour le suivi en direct.

 Principe : s'enregistre auprès du service de localisation d'Android
 (LocationManager) via un LocationListener écrit en Python (PythonJavaClass).
 Chaque fix GPS est converti au MÊME format de dictionnaire que les points
 envoyés autrefois par GPSLogger au serveur local de main.py :

     {'lat': float, 'lon': float, 'ele': float|None,
      'time': datetime, 'name': None, 'source': 'gps'}

 et déposé dans une file thread-safe (queue.Queue) que main.py consomme
 déjà (_traiter_file_points_live / _ajouter_point_live, inchangés).

 Aucune modification du pipeline d'affichage/enregistrement de main.py :
 ce module ne fait QUE produire les points.

 Permissions :
   - buildozer.spec : android.permissions = ACCESS_FINE_LOCATION,ACCESS_COARSE_LOCATION
   - exécution : demande automatique à la première utilisation
     (Android 6+ exige une demande à l'exécution).

 Limitation connue : Android suspend le GPS d'une application sans
 service de premier plan quand l'écran s'éteint. Pour enregistrer écran
 éteint, il faut un foreground service (voir discussion) — en attendant,
 garder l'écran allumé pendant le suivi (comme pour la navigation).
============================================================================
"""

import os
import queue
from datetime import datetime

from kivy.utils import platform

# --- État global du module ------------------------------------------------
# 'inactif'       : aucun suivi
# 'attente'       : permission demandée ou GPS en cours d'acquisition
# 'actif'         : le LocationListener est enregistré
# 'refuse'        : l'utilisateur a refusé la permission
etat = "inactif"
derniere_erreur = ""

# Références fortes : empêchent Python de libérer le listener Java/Python
# avant que le callback ne soit appelé (même principe que
# _ECOUTEURS_SCAN_PHOTO dans main.py).
_listener = None
_gestionnaire = None
_file_points = None
_precision_max_m = 100.0        # fix ignoré si précision GPS > cette valeur (None = tout garder)
_intervalle_ms = 1000          # fréquence de demande de fix (1 s, comme GPSLogger par défaut)
_distance_min_m = 0.0           # pas de filtrage par distance (tout point suffisant est gardé)


def _contexte():
    """Renvoie l'activité Android courante (Contexte), ou None."""
    try:
        from jnius import autoclass
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        return PythonActivity.mActivity
    except Exception:
        return None


def permission_accordee():
    """True si la permission de localisation fine est déjà accordée."""
    try:
        from android.permissions import check_permission
        from android.permissions import Permission
        return check_permission(Permission.ACCESS_FINE_LOCATION)
    except Exception:
        # Module indisponible (PC, APK ancien) : considérer accordé pour ne
        # pas bloquer ; le LocationManager échouera de toute façon sinon.
        return True


def demander_permission(callback=None):
    """Demande la permission à l'exécution (Android 6+). callback(ok) est
    appelé avec True/False une fois la réponse de l'utilisateur reçue.
    Sur PC / module absent, appelle immédiatement callback(True)."""
    global etat, derniere_erreur
    if platform != "android" or permission_accordee():
        etat = "inactif"
        if callback:
            callback(True)
        return
    try:
        from android.permissions import request_permissions, Permission

        def _reponse(permissions, resultats):
            ok = bool(resultats) and all(resultats)
            if not ok:
                global etat, derniere_erreur
                etat = "refuse"
                derniere_erreur = "permission de localisation refusée"
            if callback:
                callback(ok)

        etat = "attente"
        request_permissions(
            [Permission.ACCESS_FINE_LOCATION, Permission.ACCESS_COARSE_LOCATION],
            _reponse,
        )
    except Exception as e:
        etat = "refuse"
        derniere_erreur = f"demande de permission impossible : {e}"
        if callback:
            callback(False)


def _fabriquer_listener():
    """Crée le LocationListener Python (PythonJavaClass) : appelé par
    Android sur CHAQUE fix. Le callback tourne dans le thread Java de
    l'API de localisation : on ne touche à AUCUN objet Kivy ici, on se
    contente de déposer le point dans la file thread-safe."""
    from jnius import PythonJavaClass, java_method

    class ListenerGPS(PythonJavaClass):
        __javainterfaces__ = ["android/location/LocationListener"]

        @java_method("()V")
        def onProviderDisabled(self, fournisseur):
            pass

        @java_method("()V")
        def onProviderEnabled(self, fournisseur):
            pass

        @java_method("(Landroid/location/Location;)V")
        def onLocationChanged(self, localisation):
            try:
                if localisation is None or _file_points is None:
                    return
                # Filtre de précision : un fix imprécis (intérieur, couverture)
                # est ignoré plutôt que de polluer la trace.
                try:
                    precision = localisation.getAccuracy()
                except Exception:
                    precision = None
                if _precision_max_m is not None and precision is not None and precision > _precision_max_m:
                    return

                ele = None
                try:
                    if localisation.hasAltitude():
                        ele = round(float(localisation.getAltitude()), 1)
                except Exception:
                    ele = None

                source = str(localisation.getProvider() or "gps")

                _file_points.put({
                    "lat": float(localisation.getLatitude()),
                    "lon": float(localisation.getLongitude()),
                    "ele": ele,
                    "time": datetime.now(),
                    "name": None,
                    "source": source,
                })
            except Exception as e:
                global derniere_erreur
                derniere_erreur = f"traitement d'un fix GPS : {e}"

        @java_method("(Ljava/lang/String;Landroid/os/Bundle;)V")
        def onStatusChanged(self, fournisseur, statut, extras):
            pass

    return ListenerGPS()


def demarrer(file_points, intervalle_ms=None, precision_max_m=None):
    """Démarre le suivi GPS natif.

    Renvoie (True, "") si l'enregistrement du listener est lancé (ou
    déjà actif), (False, raison) sinon. La permission est demandée au
    besoin ; tant qu'elle n'est pas accordée, renvoie (False, "permission
    en attente...") — l'appelant peut retenter demarrer() après la
    réponse utilisateur (le callback de demande ne peut pas relancer
    seul le suivi sans toucher à Kivy depuis un thread Java).
    """
    global etat, derniere_erreur, _listener, _gestionnaire
    global _file_points, _intervalle_ms, _precision_max_m

    if platform != "android":
        etat = "refuse"
        return False, "GPS natif disponible uniquement sur Android"

    if etat == "actif":
        # Déjà en cours : on se contente de rebrancher la file (l'appelant
        # a pu purger/réinitialiser ses listes).
        _file_points = file_points
        return True, ""

    if not permission_accordee():
        if etat != "attente":
            demander_permission()
            derniere_erreur = "permission de localisation en attente..."
        return False, derniere_erreur

    try:
        from jnius import autoclass, cast

        activite = _contexte()
        if activite is None:
            raise RuntimeError("activité Android introuvable")

        if intervalle_ms is not None:
            _intervalle_ms = intervalle_ms
        if precision_max_m is not None:
            _precision_max_m = precision_max_m
        _file_points = file_points

        LocationManager = autoclass("android.location.LocationManager")
        contexte = cast("android.content.Context", activite)
        gestionnaire = contexte.getSystemService(activite.LOCATION_SERVICE)

        # Fournisseurs testés dans l'ordre : GPS puis network (repli en
        # intérieur). FUSED est évité car indisponible sans Google Play
        # Services récents et non déclaré par défaut.
        fournisseur_choisi = None
        for nom, essai in (("gps", LocationManager.GPS_PROVIDER),
                           ("network", LocationManager.NETWORK_PROVIDER)):
            try:
                if gestionnaire.isProviderEnabled(essai):
                    fournisseur_choisi = essai
                    break
            except Exception:
                continue
        if fournisseur_choisi is None:
            etat = "refuse"
            return False, "aucun fournisseur de localisation activé (GPS et réseau désactivés ?)"

        listener = _fabriquer_listener()
        # Références fortes AVANT l'enregistrement (sinon GC → crash au callback).
        _listener = listener
        _gestionnaire = gestionnaire

        gestionnaire.requestLocationUpdates(
            fournisseur_choisi, _intervalle_ms, _distance_min_m, listener)
        etat = "actif"
        derniere_erreur = ""
        return True, ""
    except Exception as e:
        etat = "refuse"
        derniere_erreur = f"impossible de démarrer le GPS natif : {e}"
        return False, derniere_erreur


def arreter():
    """Arrête le suivi (désenregistre le listener). Ne lève jamais."""
    global etat, _listener, _gestionnaire
    if _gestionnaire is not None and _listener is not None:
        try:
            _gestionnaire.removeUpdates(_listener)
        except Exception:
            pass
    _listener = None
    _gestionnaire = None
    etat = "inactif"


def est_actif():
    return etat == "actif"


def ouvrir_reglages():
    """Ouvre la page Réglages Android de l'application (Permissions),
    seule issue quand l'utilisateur a coché « Ne plus demander » :
    Android n'affichera PLUS jamais la popup, la permission doit être
    accordée ici à la main. Ne lève jamais."""
    try:
        from jnius import autoclass, cast

        activite = _contexte()
        if activite is None:
            return
        Intent = autoclass("android.content.Intent")
        Uri = autoclass("android.net.Uri")
        contexte = cast("android.content.Context", activite)

        intention = Intent(
            "android.settings.APPLICATION_DETAILS_SETTINGS",
            Uri.parse("package:" + contexte.getPackageName()),
        )
        intention.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        contexte.startActivity(intention)
    except Exception as e:
        global derniere_erreur
        derniere_erreur = f"impossible d'ouvrir les réglages : {e}"