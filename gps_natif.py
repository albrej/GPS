# -*- coding: utf-8 -*-
"""
============================================================================
 GPS NATIF ANDROID (pyjnius) — remplace GPSLogger pour le suivi en direct.

 Chaque fix du LocationManager est converti au MÊME format de
 dictionnaire que les points GPSLogger et déposé dans la file
 thread-safe consommée par main.py (_traiter_file_points_live).

 Version 4 :
   - requestLocationUpdates/removeUpdates passés avec le LOOPER
     PRINCIPAL (getMainLooper) : obligatoire, Kivy exécute Python dans
     SDLThread qui n'a pas de Looper ;
   - LES DEUX FOURNISSEURS (gps et network) sont enregistrés : le GPS
     seul peut mettre plusieurs minutes à fournir son premier fix
     (démarrage à froid), le fournisseur network donne un premier point
     approximatif bien plus vite ;
   - plus de filtre de précision par défaut : les premiers fixes
     grossiers (précision > 100 m) étaient rejetés silencieusement —
     main.py élimine déjà les doublons exacts via _ajouter_point_live ;
   - compteurs de diagnostic (fixes_recus / fixes_rejetes) consultables
     par l'UI pour distinguer « GPS pas encore de fix » et « fix reçu
     mais ignoré ».
============================================================================
"""

from datetime import datetime

from kivy.utils import platform

# --- État global du module ------------------------------------------------
# 'inactif' : aucun suivi | 'attente' : permission demandée / GPS en acquisition
# 'actif'   : listener enregistré       | 'refuse' : permission réellement absente
etat = "inactif"
derniere_erreur = ""
_fixes_recus = 0        # diagnostics : fixes arrivés à onLocationChanged
_fixes_deposes = 0       # diagnostics : points réellement mis dans la file
_dernier_fix_heure = ""  # horodatage du dernier fix reçu ("HH:MM:SS")

# Références fortes : empêchent Python de libérer le listener avant
# que le callback ne soit appelé (même principe que _ECOUTEURS_SCAN_PHOTO).
_listener = None
_gestionnaire = None
_file_points = None
_intervalle_ms = 1000
_distance_min_m = 0.0
_fournisseurs_enregistres = []

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
    """True si la permission de localisation fine est VRAIMENT accordée,
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
        derniere_erreur = f"checkSelfPermission indisponible (Android < 6 ? accordée par le manifeste) : {e}"
        return True


def _demander_permission_runtime(callback_ok=None):
    """Demande la permission via l'API officielle (requestPermissions).
    La réponse est asynchrone ; l'appelant re-vérifie toutes les 4 s via
    permission_accordee() — les résultats bruts du callback Android sont
    ignorés (parfois incohérents sur MIUI/HyperOS)."""
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
    """LocationListener Python (PythonJavaClass) : appelé sur chaque fix,
    sur le thread principal Android (Looper principal) — on ne touche à
    AUCUN objet Kivy ici, on dépose le point dans la file thread-safe."""
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
            global _fixes_recus, _fixes_deposes, _dernier_fix_heure
            try:
                if localisation is None or _file_points is None:
                    return
                _fixes_recus += 1
                _dernier_fix_heure = datetime.now().strftime("%H:%M:%S")

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
                _fixes_deposes += 1
            except Exception as e:
                global derniere_erreur
                derniere_erreur = f"traitement d'un fix GPS : {e}"

        @java_method("(Ljava/lang/String;Landroid/os/Bundle;)V")
        def onStatusChanged(self, fournisseur, statut, extras):
            pass

    return ListenerGPS()


def demarrer(file_points, intervalle_ms=None):
    """Démarre le suivi GPS natif. Renvoie (True, "") ou (False, raison).

    Enregistre le listener sur TOUS les fournisseurs activés (gps et
    network) : le GPS seul peut être lent à donner son premier fix ;
    network fournit un point approximatif presque immédiatement. Les
    doublons/dérives sont gérés en aval par _ajouter_point_live."""
    global etat, derniere_erreur, _listener, _gestionnaire
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
        # Références fortes AVANT l'enregistrement (sinon GC -> crash).
        _listener = listener
        _gestionnaire = gestionnaire
        _fournisseurs_enregistres = []

        au_moins_un = False
        for nom, essai in (("gps", LocationManager.GPS_PROVIDER),
                           ("network", LocationManager.NETWORK_PROVIDER)):
            try:
                if not gestionnaire.isProviderEnabled(essai):
                    continue
            except Exception:
                continue
            try:
                # Le Looper principal en 5e argument est OBLIGATOIRE :
                # Python/Kivy tourne dans SDLThread (sans Looper).
                gestionnaire.requestLocationUpdates(
                    essai, _intervalle_ms, _distance_min_m, listener,
                    Looper.getMainLooper())
                _fournisseurs_enregistres.append(nom)
                au_moins_un = True
            except Exception as e_secu:
                if "security" in str(e_secu).lower():
                    # SecurityException alors que.checkSelfPermission disait
                    # accordé (bug MIUI) : re-demande runtime pour
                    # resynchroniser l'état interne d'Android.
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
    _fournisseurs_enregistres = []
    etat = "inactif"


def est_actif():
    return etat == "actif"


def fixes_recus():
    """Diagnostic : nombre de fixes reçus du système depuis demarrer()."""
    return _fixes_recus


def fixes_deposes():
    """Diagnostic : nombre de points réellement déposés dans la file."""
    return _fixes_deposes


def heure_dernier_fix():
    """Diagnostic : horodatage ("HH:MM:SS") du dernier fix reçu, "" sinon."""
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