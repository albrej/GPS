# -*- coding: utf-8 -*-
"""
============================================================================
 GPS NATIF ANDROID (pyjnius) â remplace GPSLogger pour le suivi en direct.

 Chaque fix est converti au MÃME format de dictionnaire que les points
 GPSLogger et dÃ©posÃ© dans la file thread-safe consommÃ©e par main.py
 (_traiter_file_points_live).

 Version 5 â lecture par SONDAGE (polling) au lieu du callback :
   Le listener Java (onLocationChanged) n'Ã©tait JAMAIS rappelÃ© cÃ´tÃ©
   Python : pyjnius ne peut exÃ©cuter un callback Python que depuis un
   thread crÃ©Ã© par Python, or Android appelle le listener sur le thread
   UI (main looper), qui n'est pas un thread Python -> aucun point,
   mÃªme aprÃ¨s 30 min, alors que le GPS calculait bien des fixes.

   La parade, Ã©prouvÃ©e et sans callback :
     1. requestLocationUpdates reste enregistrÃ© (gps) : il
        FORCE Android Ã  calculer des positions et rafraÃ®chir son cache
        (c'est ce qui rend getLastKnownLocation vivant) ;
     2. un THREAD PYTHON (daemon) interroge getLastKnownLocation()
        toutes les secondes et dÃ©pose chaque NOUVEAU fix dans la file.
        C'est un appel Java direct depuis Python : fiable Ã  100 %.

  getLastKnownLocation est exactement ce que GPSLogger exploite en
   intÃ©rieur : le systÃ¨me conserve le dernier fix calculÃ© (GPS, rÃ©seau,
   fused), mÃªme sans vue du ciel.
============================================================================
"""

import threading
import time
from datetime import datetime

from kivy.utils import platform

# --- Ãtat global du module ------------------------------------------------
# 'inactif' : aucun suivi | 'attente' : permission demandÃ©e / GPS en acquisition
# 'actif'   : suivi en cours  | 'refuse' : permission rÃ©ellement absente
etat = "inactif"
derniere_erreur = ""
_fixes_recus = 0        # diagnostics : fixes lus et dÃ©posÃ©s dans la file
_dernier_fix_heure = ""

# RÃ©fÃ©rences fortes.
_listener = None            # conservÃ© : garde l'enregistrement systÃ¨me vivant
_gestionnaire = None
_file_points = None
_thread_poll = None
_intervalle_ms = 1000

# ============================================================================
#  *** CONSEIL IMPORTANT â ENREGISTREMENT Â« PAR DISTANCE Â» RECOMMANDÃ ***
# ============================================================================
#  Par dÃ©faut, un suivi Â« par temps Â» demanderait des fixes TOUS LES
#  1000 ms SANS seuil de distance (_distance_min_m = 0). Pour des
#  traces de RANDONNÃE, le
#  comportement le PLUS PROPRE est le mode Â« PAR DISTANCE Â» : on n'enregistre
#  un NOUVEAU point QUE LORSQU'ON S'EST DÃPLACÃ D'AU MOINS 5 MÃTRES.
#
#  C'est trÃ¨s facile ici : il suffit de rÃ©gler _distance_min_m CI-DESSOUS
#  sur 5.0 (c'est le 3e argument minDistance de requestLocationUpdates,
#  exprimÃ© en mÃ¨tres). Avantages :
#    - trace lisse, sans Â« grappes Â» de points quand on est Ã  l'arrÃªt
#      (photo, pause, bivouac) ;
#    - fichier GPX beaucoup plus lÃ©ger (des centaines de points au lieu
#      de milliers pour une mÃªme rando) ;
#    - batterie Ã©conomisÃ©e (moins d'Ã©critures et de rafraÃ®chissements) ;
#    - cohÃ©rent avec le rÃ©glage Â« Log every 5 m Â» de GPSLogger.
#
#  C'est pourquoi cette valeur est rÃ©glÃ©e sur 5.0 par dÃ©faut. Remettez
#  0.0 uniquement si vous voulez absolument un point par seconde (mode
#  Â« par temps Â»), p.ex. pour un enregistrement routier dÃ©taillÃ©.
# ============================================================================
_distance_min_m = 5.0
_fournisseurs_enregistres = []
_dernier_fix_depose = None  # (lat, lon) du dernier point dÃ©posÃ©

_PERMISSION_FINE = "android.permission.ACCESS_FINE_LOCATION"
_PERMISSION_COARSE = "android.permission.ACCESS_COARSE_LOCATION"


def _activite():
    """Renvoie l'activitÃ© Android courante, ou None."""
    try:
        from jnius import autoclass
        return autoclass("org.kivy.android.PythonActivity").mActivity
    except Exception as e:
        global derniere_erreur
        derniere_erreur = f"activitÃ© Android introuvable : {e}"
        return None


def permission_accordee():
    """True si la permission de localisation est VRAIMENT accordÃ©e,
    vÃ©rifiÃ© directement auprÃ¨s d'Android (checkSelfPermission == 0)."""
    activite = _activite()
    if activite is None:
        return False
    try:
        if activite.checkSelfPermission(_PERMISSION_FINE) == 0:
            return True
        return activite.checkSelfPermission(_PERMISSION_COARSE) == 0
    except Exception as e:
        # Android < 6 : permission accordÃ©e par le manifeste.
        global derniere_erreur
        derniere_erreur = f"checkSelfPermission indisponible (Android < 6 ?) : {e}"
        return True


def _demander_permission_runtime(callback_ok=None):
    """Demande la permission via l'API officielle (requestPermissions).
    RÃ©ponse asynchrone : l'appelant re-vÃ©rifie via permission_accordee()."""
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
    """Listener Java factice : le callback Python n'est de toute faÃ§on
    pas exÃ©cutÃ© (thread UI Android, pas un thread Python) â on l'enregistre
    UNIQUEMENT pour forcer Android Ã  calculer des fixes et alimenter
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
            # Ne devrait jamais Ãªtre appelÃ© cÃ´tÃ© Python (voir docstring
            # du module) ; si Ã§a l'est malgrÃ© tout (thread chanceux),
            # le sondage dÃ©posera de toute faÃ§on le mÃªme fix.
            pass

        @java_method("(Ljava/lang/String;Landroid/os/Bundle;)V")
        def onStatusChanged(self, fournisseur, statut, extras):
            pass

    return ListenerGPS()


def _deposer_fix(localisation):
    """DÃ©pose un fix Java (android.location.Location) dans la file, s'il
    est NOUVEAU (coordonnÃ©es diffÃ©rentes du dernier point dÃ©posÃ©).
    AppelÃ© depuis le thread de sondage : aucune manip Kivy ici."""
    global _fixes_recus, _dernier_fix_heure, _dernier_fix_depose
    if localisation is None or _file_points is None:
        return False
    # Le fournisseur "network" est IGNORÃ : sur ce tÃ©lÃ©phone (Redmi /
    # MIUI), il renvoie une position de CACHE TOUJOURS IDENTIQUE
    # (arrondie, altitude figÃ©e Ã  178.4 m) toutes les 20-60 s. Ces
    # points "network" sont trÃ¨s Ã©loignÃ©s de la trace rÃ©elle et
    # crÃ©aient des pointes parasites dans le GPX. Seuls les fixes
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

    # Anti-doublon : mÃªme position (Ã  ~1 m) que le dernier point dÃ©posÃ© ?
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
    fournisseur chaque seconde et dÃ©pose tout nouveau fix. getLastKnown-
    Location est l'appel Java qui fonctionne TOUJOURS depuis Python
    (pas de callback) ; il est rafraÃ®chi par notre requestLocationUpdates
    restÃ© enregistrÃ©, et par tout autre consommateur du systÃ¨me
    (Google Play Services, GPSLogger...), mÃªme en intÃ©rieur."""
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
    """DÃ©marre le suivi GPS natif. Renvoie (True, "") ou (False, raison).

    1. Enregistre requestLocationUpdates (gps, Looper principal
       obligatoire car Python tourne dans SDLThread) : force Android Ã 
       calculer des positions et Ã  alimenter getLastKnownLocation ;
    2. Lance le thread de sondage qui lit ces positions chaque seconde
       et les dÃ©pose dans la file â sans aucun callback Java->Python,
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
                           "accordez-la dans la popup ou dans les rÃ©glages de l'application")
        _demander_permission_runtime()
        return False, derniere_erreur

    try:
        from jnius import autoclass, cast

        activite = _activite()
        if activite is None:
            raise RuntimeError(derniere_erreur or "activitÃ© introuvable")

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
                    derniere_erreur = ("permission accordÃ©e mais non vue par Android "
                                       f"(erreur de sÃ©curitÃ© : {e_secu}) â nouvelle demande runtime lancÃ©e")
                    _demander_permission_runtime()
                    return False, derniere_erreur
                raise

        if not au_moins_un:
            etat = "refuse"
            derniere_erreur = ("aucun fournisseur de localisation activÃ© : "
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
        derniere_erreur = f"impossible de dÃ©marrer le GPS natif : {e}"
        return False, derniere_erreur


def arreter():
    """ArrÃªte le suivi : dÃ©senregistre le listener et termine le thread
    de sondage (il sort de sa boucle en voyant etat != actif). Ne lÃ¨ve
    jamais."""
    global etat, _listener, _gestionnaire, _thread_poll
    etat = "inactif"  # posÃ© AVANT : le thread s'arrÃªte de lui-mÃªme
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
    """Diagnostic : nombre de points dÃ©posÃ©s dans la file depuis demarrer()."""
    return _fixes_recus


def heure_dernier_fix():
    """Diagnostic : horodatage ("HH:MM:SS") du dernier point dÃ©posÃ©."""
    return _dernier_fix_heure


def fournisseurs():
    """Diagnostic : fournisseurs enregistrÃ©s (ex. ['gps', 'network'])."""
    return list(_fournisseurs_enregistres)


def ouvrir_reglages():
    """Ouvre la page RÃ©glages Android de l'application (Permissions),
    seule issue quand Â« Ne plus demander Â» est cochÃ©. Ne lÃ¨ve jamais."""
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
        derniere_erreur = f"impossible d'ouvrir les rÃ©glages : {e}"