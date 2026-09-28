# -*- coding: utf-8 -*-
"""
============================================================================
 GPS NATIF ANDROID (pyjnius) — remplace GPSLogger pour le suivi en direct.

 Chaque fix du LocationManager est converti au MÊME format de
 dictionnaire que les points GPSLogger et déposé dans la file
 thread-safe consommée par main.py (_traiter_file_points_live).

 Version 2 : la vérification de permission utilise directement
 Context.checkSelfPermission (API Android officielle) via pyjnius —
 le module kivy android.permissions renvoie des faux négatifs sur
 MIUI/HyperOS, ce qui bloquait le démarrage même après accord de la
 permission dans les réglages. La demande runtime utilise pareillement
 Activity.requestPermissions (pyjnius). Toute erreur est consignée dans
 derniere_erreur et affichable à l'écran.
============================================================================
"""

from datetime import datetime

from kivy.utils import platform

# --- État global du module ------------------------------------------------
# 'inactif' : aucun suivi | 'attente' : permission demandée / GPS en acquisition
# 'actif'   : listener enregistré       | 'refuse' : permission réellement absente
etat = "inactif"
derniere_erreur = ""

# Références fortes : empêchent Python de libérer le listener avant
# que le callback ne soit appelé (même principe que _ECOUTEURS_SCAN_PHOTO).
_listener = None
_gestionnaire = None
_file_points = None
_precision_max_m = 100.0
_intervalle_ms = 1000
_distance_min_m = 0.0

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
    vérifié directement auprès d'Android (checkSelfPermission == 0).
    C'est la seule source de vérité : le réglage utilisateur, le module
    android.permissions de Kivy et les résultats de callback peuvent
    diverger sur MIUI/HyperOS."""
    activite = _activite()
    if activite is None:
        return False
    try:
        resultat = activite.checkSelfPermission(_PERMISSION_FINE)
        if resultat == 0:  # PERMISSION_GRANTED
            return True
        # Repli sur COARSE (localisation approximative) si FINE refusée.
        return activite.checkSelfPermission(_PERMISSION_COARSE) == 0
    except Exception as e:
        # Ancien Android (< 6) : checkSelfPermission n'existe pas,
        # la permission est déclarée dans le manifeste → accordée.
        global derniere_erreur
        derniere_erreur = f"checkSelfPermission indisponible (Android < 6 ? accordée par le manifeste) : {e}"
        return True


def _demander_permission_runtime(callback_ok=None):
    """Demande la permission via l'API officielle (Activity.requestPermissions).
    La popup système s'affiche ; la réponse est asynchrone. L'appelant
    (_verifier_demarrage_gps_natif de main.py) re-vérifie toutes les 4 s
    via permission_accordee() : dès que l'accord est effectif CÔTÉ
    ANDROID, le suivi démarre — les résultats bruts du callback Android
    sont ignorés car parfois incohérents sur Xiaomi (accord affiché mais
    tableau de résultats vide, etc.)."""
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
    dans un thread Java — on ne touche à AUCUN objet Kivy ici, on dépose
    le point dans la file thread-safe."""
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
    """Démarre le suivi GPS natif. Renvoie (True, "") ou (False, raison).

    Ordre :
      1. permission réellement accordée (checkSelfPermission) ? Sinon :
         demande runtime popup (l'appelant re-vérifie toutes les 4 s) ;
      2. LocationManager.requestLocationUpdates sur le premier
         fournisseur disponible (gps, puis network). Une
         SecurityException ici ALORS QUE la permission est accordée
         (bug MIUI) entraîne une re-demande runtime au lieu d'un refus.
    """
    global etat, derniere_erreur, _listener, _gestionnaire
    global _file_points, _intervalle_ms, _precision_max_m

    if platform != "android":
        etat = "refuse"
        derniere_erreur = "GPS natif disponible uniquement sur Android"
        return False, derniere_erreur

    if etat == "actif":
        _file_points = file_points
        return True, ""

    # 1. Permission : vérité terrain via l'API Android.
    if not permission_accordee():
        etat = "attente"
        derniere_erreur = ("permission de localisation en attente : "
                           "accordez-la dans la popup ou dans les réglages de l'application")
        _demander_permission_runtime()
        return False, derniere_erreur

    # 2. Enregistrement du listener.
    try:
        from jnius import autoclass, cast

        activite = _activite()
        if activite is None:
            raise RuntimeError(derniere_erreur or "activité introuvable")

        if intervalle_ms is not None:
            _intervalle_ms = intervalle_ms
        if precision_max_m is not None:
            _precision_max_m = precision_max_m
        _file_points = file_points

        LocationManager = autoclass("android.location.LocationManager")
        contexte = cast("android.content.Context", activite)
        gestionnaire = contexte.getSystemService(activite.LOCATION_SERVICE)

        fournisseur_choisi = None
        for essai in (LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER):
            try:
                if gestionnaire.isProviderEnabled(essai):
                    fournisseur_choisi = essai
                    break
            except Exception:
                continue
        if fournisseur_choisi is None:
            etat = "refuse"
            derniere_erreur = ("aucun fournisseur de localisation activé : "
                               "activez la localisation dans la barre de notifications Android")
            return False, derniere_erreur

        listener = _fabriquer_listener()
        _listener = listener
        _gestionnaire = gestionnaire

        try:
            gestionnaire.requestLocationUpdates(
                fournisseur_choisi, _intervalle_ms, _distance_min_m, listener)
        except Exception as e_secu:
            # SecurityException alors que.checkSelfPermission disait accordé :
            # bug rencontré sur MIUI après accord manuel via les réglages.
            # La parade : re-demander la permission runtime UNE fois, ce qui
            # « resynchronise » l'état interne d'Android.
            if "security" in str(e_secu).lower():
                etat = "attente"
                derniere_erreur = ("permission accordée mais non vue par Android "
                                   f"(erreur de sécurité : {e_secu}) — nouvelle demande runtime lancée")
                _demander_permission_runtime()
                return False, derniere_erreur
            raise

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