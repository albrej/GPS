# -*- coding: utf-8 -*-
"""
============================================================================
 GPS NATIF ANDROID (pyjnius) — remplace GPSLogger pour le suivi en direct.

 Chaque fix est converti au MÊME format de dictionnaire que les points
 GPSLogger et déposé dans la file thread-safe consommée par main.py
 (_traiter_file_points_live).

 Version 5 — lecture par SONDAGE (polling) au lieu du callback :
   Le listener Java (onLocationChanged) n'était JAMAIS rappelé côté
   Python : pyjnius ne peut exécuter un callback Python que depuis un
   thread créé par Python, or Android appelle le listener sur le thread
   UI (main looper), qui n'est pas un thread Python -> aucun point,
   même après 30 min, alors que le GPS calculait bien des fixes.

   La parade, éprouvée et sans callback :
     1. requestLocationUpdates reste enregistré (gps) : il
        FORCE Android à calculer des positions et rafraîchir son cache
        (c'est ce qui rend getLastKnownLocation vivant) ;
     2. un THREAD PYTHON (daemon) interroge getLastKnownLocation()
        toutes les secondes et dépose chaque NOUVEAU fix dans la file.
        C'est un appel Java direct depuis Python : fiable à 100 %.

  getLastKnownLocation est exactement ce que GPSLogger exploite en
   intérieur : le système conserve le dernier fix calculé (GPS, réseau,
   fused), même sans vue du ciel.
============================================================================
"""

import threading
import time
from datetime import datetime

from kivy.utils import platform

# --- État global du module ------------------------------------------------
# 'inactif' : aucun suivi | 'attente' : permission demandée / GPS en acquisition
# 'actif'   : suivi en cours  | 'refuse' : permission réellement absente
etat = "inactif"
derniere_erreur = ""
_fixes_recus = 0        # diagnostics : fixes lus et déposés dans la file
_dernier_fix_heure = ""

# Références fortes.
_listener = None            # conservé : garde l'enregistrement système vivant
_gestionnaire = None
_file_points = None
_thread_poll = None
_intervalle_ms = 1000

# ============================================================================
#  *** CONSEIL IMPORTANT — ENREGISTREMENT « PAR DISTANCE » RECOMMANDÉ ***
# ============================================================================
#  Par défaut, un suivi « par temps » demanderait des fixes TOUS LES
#  1000 ms SANS seuil de distance (_distance_min_m = 0). Pour des
#  traces de RANDONNÉE, le
#  comportement le PLUS PROPRE est le mode « PAR DISTANCE » : on n'enregistre
#  un NOUVEAU point QUE LORSQU'ON S'EST DÉPLACÉ D'AU MOINS 5 MÈTRES.
#
#  C'est très facile ici : il suffit de régler _distance_min_m CI-DESSOUS
#  sur 5.0 (c'est le 3e argument minDistance de requestLocationUpdates,
#  exprimé en mètres). Avantages :
#    - trace lisse, sans « grappes » de points quand on est à l'arrêt
#      (photo, pause, bivouac) ;
#    - fichier GPX beaucoup plus léger (des centaines de points au lieu
#      de milliers pour une même rando) ;
#    - batterie économisée (moins d'écritures et de rafraîchissements) ;
#    - cohérent avec le réglage « Log every 5 m » de GPSLogger.
#
#  C'est pourquoi cette valeur est réglée sur 5.0 par défaut. Remettez
#  0.0 uniquement si vous voulez absolument un point par seconde (mode
#  « par temps »), p.ex. pour un enregistrement routier détaillé.
# ============================================================================
_distance_min_m = 5.0
_fournisseurs_enregistres = []
_dernier_fix_depose = None  # (lat, lon) du dernier point déposé

_PERMISSION_FINE = "android.permission.ACCESS_FINE_LOCATION"
_PERMISSION_COARSE = "android.permission.ACCESS_COARSE_LOCATION"


def _activite():
    """Renvoie l'activité Android courante, ou None."""
    try:
        from jnius import autoclass
        return autoclass("org.kivy.android.PythonActivity").mActivity
    except Exception as e:
        global derniere_erreur
        derniere_erreur = f"activité Android introuvable : {e}"
        return None


def permission_accordee():
    """True si la permission de localisation est VRAIMENT accordée,
    vérifié directement auprès d'Android (checkSelfPermission == 0)."""
    activite = _activite()
    if activite is None:
        return False
    try:
        if activite.checkSelfPermission(_PERMISSION_FINE) == 0:
            return True
        return activite.checkSelfPermission(_PERMISSION_COARSE) == 0
    except Exception as e:
        # Android < 6 : permission accordée par le manifeste.
        global derniere_erreur
        derniere_erreur = f"checkSelfPermission indisponible (Android < 6 ?) : {e}"
        return True


def _demander_permission_runtime(callback_ok=None):
    """Demande la permission via l'API officielle (requestPermissions).
    Réponse asynchrone : l'appelant re-vérifie via permission_accordee()."""
    activite = _activite()
    if activite is None:
        if callback_ok:
            callback_ok(False)
        return
    try:
        activite.requestPermissions([_PERMISSION_FINE, _PERMISSION_COARSE], 1)
        if callback_ok:
            callback_ok(permission_accordee())
    except Exception as e:
        global etat, derniere_erreur
        etat = "refuse"
        derniere_erreur = f"requestPermissions impossible : {e}"
        if callback_ok:
            callback_ok(False)


def _fabriquer_listener():
    """Listener Java factice : le callback Python n'est de toute façon
    pas exécuté (thread UI Android, pas un thread Python) — on l'enregistre
    UNIQUEMENT pour forcer Android à calculer des fixes et alimenter
    getLastKnownLocation. Les points sont lus par le thread de sondage."""
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
            # Ne devrait jamais être appelé côté Python (voir docstring
            # du module) ; si ça l'est malgré tout (thread chanceux),
            # le sondage déposera de toute façon le même fix.
            pass

        @java_method("(Ljava/lang/String;Landroid/os/Bundle;)V")
        def onStatusChanged(self, fournisseur, statut, extras):
            pass

    return ListenerGPS()


def _deposer_fix(localisation):
    """Dépose un fix Java (android.location.Location) dans la file, s'il
    est NOUVEAU (coordonnées différentes du dernier point déposé).
    Appelé depuis le thread de sondage : aucune manip Kivy ici."""
    global _fixes_recus, _dernier_fix_heure, _dernier_fix_depose
    if localisation is None or _file_points is None:
        return False
    # Le fournisseur "network" est IGNORÉ : sur ce téléphone (Redmi /
    # MIUI), il renvoie une position de CACHE TOUJOURS IDENTIQUE
    # (arrondie, altitude figée à 178.4 m) toutes les 20-60 s. Ces
    # points "network" sont très éloignés de la trace réelle et
    # créaient des pointes parasites dans le GPX. Seuls les fixes
    # "fused" (Google Play Services) et "gps" sont de confiance.
    try:
        if str(localisation.getProvider() or "") == "network":
            return False
    except Exception:
        pass
    try:
        lat = float(localisation.getLatitude())
        lon = float(localisation.getLongitude())
    except Exception:
        return False

    # Anti-doublon : même position (à ~1 m) que le dernier point déposé ?
    if _dernier_fix_depose is not None:
        dlat, dlon = _dernier_fix_depose
        if abs(dlat - lat) < 1e-5 and abs(dlon - lon) < 1e-5:
            return False

    ele = None
    try:
        if localisation.hasAltitude():
            ele = round(float(localisation.getAltitude()), 1)
    except Exception:
        ele = None

    source = "gps"
    try:
        source = str(localisation.getProvider() or "gps")
    except Exception:
        pass

    _file_points.put({
        "lat": lat,
        "lon": lon,
        "ele": ele,
        "time": datetime.now(),
        "name": None,
        "source": source,
    })
    _fixes_recus += 1
    _dernier_fix_heure = datetime.now().strftime("%H:%M:%S")
    _dernier_fix_depose = (lat, lon)
    return True


def _boucle_sondage():
    """Thread daemon : interroge getLastKnownLocation() de chaque
    fournisseur chaque seconde et dépose tout nouveau fix. getLastKnown-
    Location est l'appel Java qui fonctionne TOUJOURS depuis Python
    (pas de callback) ; il est rafraîchi par notre requestLocationUpdates
    resté enregistré, et par tout autre consommateur du système
    (Google Play Services, GPSLogger...), même en intérieur."""
    global _gestionnaire
    while etat == "actif":
        try:
            gestionnaire = _gestionnaire
            if gestionnaire is not None:
                meilleur = None
                for nom_fournisseur in ("fused", "gps", "passive"):
                    try:
                        loc = gestionnaire.getLastKnownLocation(nom_fournisseur)
                    except Exception:
                        continue
                    if loc is None:
                        continue
                    if meilleur is None:
                        meilleur = loc
                    else:
                        try:
                            if loc.getTime() > meilleur.getTime():
                                meilleur = loc
                        except Exception:
                            pass
                if meilleur is not None:
                    _deposer_fix(meilleur)
        except Exception as e:
            global derniere_erreur
            derniere_erreur = f"sondage GPS : {e}"
        time.sleep(1.0)


def demarrer(file_points, intervalle_ms=None):
    """Démarre le suivi GPS natif. Renvoie (True, "") ou (False, raison).

    1. Enregistre requestLocationUpdates (gps, Looper principal
       obligatoire car Python tourne dans SDLThread) : force Android à
       calculer des positions et à alimenter getLastKnownLocation ;
    2. Lance le thread de sondage qui lit ces positions chaque seconde
       et les dépose dans la file — sans aucun callback Java->Python,
       qui ne fonctionne pas sous Kivy/pyjnius."""
    global etat, derniere_erreur, _listener, _gestionnaire, _thread_poll
    global _file_points, _intervalle_ms, _fournisseurs_enregistres

    if platform != "android":
        etat = "refuse"
        derniere_erreur = "GPS natif disponible uniquement sur Android"
        return False, derniere_erreur

    if etat == "actif":
        _file_points = file_points
        return True, ""

    if not permission_accordee():
        etat = "attente"
        derniere_erreur = ("permission de localisation en attente : "
                           "accordez-la dans la popup ou dans les réglages de l'application")
        _demander_permission_runtime()
        return False, derniere_erreur

    try:
        from jnius import autoclass, cast

        activite = _activite()
        if activite is None:
            raise RuntimeError(derniere_erreur or "activité introuvable")

        if intervalle_ms is not None:
            _intervalle_ms = intervalle_ms
        _file_points = file_points

        LocationManager = autoclass("android.location.LocationManager")
        Looper = autoclass("android.os.Looper")
        contexte = cast("android.content.Context", activite)
        gestionnaire = contexte.getSystemService(activite.LOCATION_SERVICE)

        listener = _fabriquer_listener()
        _listener = listener
        _gestionnaire = gestionnaire
        _fournisseurs_enregistres = []

        au_moins_un = False
        for nom, essai in (("gps", LocationManager.GPS_PROVIDER),):
            try:
                if not gestionnaire.isProviderEnabled(essai):
                    continue
            except Exception:
                continue
            try:
                # Looper principal en 5e argument OBLIGATOIRE :
                # Python/Kivy tourne dans SDLThread (sans Looper).
                gestionnaire.requestLocationUpdates(
                    essai, _intervalle_ms, _distance_min_m, listener,
                    Looper.getMainLooper())
                _fournisseurs_enregistres.append(nom)
                au_moins_un = True
            except Exception as e_secu:
                if "security" in str(e_secu).lower():
                    etat = "attente"
                    derniere_erreur = ("permission accordée mais non vue par Android "
                                       f"(erreur de sécurité : {e_secu}) — nouvelle demande runtime lancée")
                    _demander_permission_runtime()
                    return False, derniere_erreur
                raise

        if not au_moins_un:
            etat = "refuse"
            derniere_erreur = ("aucun fournisseur de localisation activé : "
                               "activez la localisation dans la barre de notifications Android")
            return False, derniere_erreur

        # Thread de sondage : c'est LUI qui fournit les points.
        _thread_poll = threading.Thread(target=_boucle_sondage, daemon=True)
        etat = "actif"
        derniere_erreur = ""
        _thread_poll.start()
        return True, ""
    except Exception as e:
        etat = "refuse"
        derniere_erreur = f"impossible de démarrer le GPS natif : {e}"
        return False, derniere_erreur


def arreter():
    """Arrête le suivi : désenregistre le listener et termine le thread
    de sondage (il sort de sa boucle en voyant etat != actif). Ne lève
    jamais."""
    global etat, _listener, _gestionnaire, _thread_poll
    etat = "inactif"  # posé AVANT : le thread s'arrête de lui-même
    if _gestionnaire is not None and _listener is not None:
        try:
            from jnius import autoclass
            try:
                Looper = autoclass("android.os.Looper")
                _gestionnaire.removeUpdates(_listener, Looper.getMainLooper())
            except Exception:
                _gestionnaire.removeUpdates(_listener)
        except Exception:
            pass
    _listener = None
    _gestionnaire = None
    _thread_poll = None
    _fournisseurs_enregistres = []


def est_actif():
    return etat == "actif"


def fixes_recus():
    """Diagnostic : nombre de points déposés dans la file depuis demarrer()."""
    return _fixes_recus


def heure_dernier_fix():
    """Diagnostic : horodatage ("HH:MM:SS") du dernier point déposé."""
    return _dernier_fix_heure


def fournisseurs():
    """Diagnostic : fournisseurs enregistrés (ex. ['gps', 'network'])."""
    return list(_fournisseurs_enregistres)


def ouvrir_reglages():
    """Ouvre la page Réglages Android de l'application (Permissions),
    seule issue quand « Ne plus demander » est coché. Ne lève jamais."""
    try:
        from jnius import autoclass, cast

        activite = _activite()
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