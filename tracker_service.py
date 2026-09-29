# -*- coding: utf-8 -*-
"""
============================================================================
 SERVICE DE SUIVI GPS EN PREMIER PLAN (foreground service) â tracker_service.py

 Processus SÃPARÃ de l'application : lancÃ© par buildozer via
     services = Tracker:tracker_service.py:foreground
 il continue d'enregistrer les points GPS quand l'Ã©cran est Ã©teint ou
 que l'application est fermÃ©e/reculÃ©e en arriÃ¨re-plan (c'est exactement
 ce que faisait GPSLogger).

 Fonctionnement (volontairement sans Kivy, pour un dÃ©marrage rapide) :
   1. au lancement : notification de premier plan (obligatoire, sinon
      Android 8+ tue le service) â avec le TYPE location sur Android 14+
      (startForeground(id, notif, FOREGROUND_SERVICE_TYPE_LOCATION)) ;
   2. requestLocationUpdates (gps, Looper principal) force
      Android Ã  calculer des positions ;
   3. un thread Python sonde getLastKnownLocation() chaque seconde
      (mÃªme technique Ã©prouvÃ©e que gps_natif.py v5 â aucun callback
      Java->Python, qui ne fonctionne pas sous pyjnius) ;
   4. chaque NOUVELLE position est ajoutÃ©e en fin du fichier JSON :
      /storage/emulated/0/GPX_Files/Bubu_GPS_Files/live_service_points.json
      une ligne par point : {"lat":..., "lon":..., "ele":..., "time":iso, "source":...}

 L'application (main.py) lit ce fichier pendant le live et absorbe les
 points manquants (dÃ©doublonnÃ©s en aval par _ajouter_point_live).

 ArrÃªt : main.py appelle ServiceTracker.stop() (jnius), ce qui arrÃªte
 le service Android et donc ce script.
============================================================================
"""

import json
import os
import threading
import time
from datetime import datetime

from jnius import autoclass, cast

DOSSIER_SORTIE = "/storage/emulated/0/GPX_Files/Bubu_GPS_Files"
CHEMIN_POINTS = os.path.join(DOSSIER_SORTIE, "live_service_points.json")

_PERMISSION_FINE = "android.permission.ACCESS_FINE_LOCATION"
_PERMISSION_COARSE = "android.permission.ACCESS_COARSE_LOCATION"

_etat = {"actif": False, "dernier": None, "erreurs": 0}


def _service():
    """Le Service Android courant (Ã©quivalent de mActivity cÃ´tÃ© service)."""
    return autoclass("org.kivy.android.PythonService").mService


def _permission_ok(contexte):
    try:
        if contexte.checkSelfPermission(_PERMISSION_FINE) == 0:
            return True
        return contexte.checkSelfPermission(_PERMISSION_COARSE) == 0
    except Exception:
        return True  # Android < 6


def _passer_premier_plan():
    """Notification de premier plan : SANS elle, Android tue le service
    quelques secondes aprÃ¨s l'extinction de l'Ã©cran. Sur Android 14+
    (API 34, cible du Redmi), startForeground DOIT prÃ©ciser le type
    location, sinon ForegroundServiceTypeNotSpecifiedException."""
    service = _service()
    try:
        Context = autoclass("android.content.Context")
        NotificationChannel = autoclass("android.app.NotificationChannel")
        NotificationManager = autoclass("android.app.NotificationManager")
        Notification = autoclass("android.app.Notification")

        if service.getApplicationInfo().targetSdkVersion >= 26:
            gestionnaire = service.getSystemService(Context.NOTIFICATION_SERVICE)
            canal = NotificationChannel(
                "suivi_gps", "Suivi GPS Bubu", NotificationManager.IMPORTANCE_LOW)
            gestionnaire.createNotificationChannel(canal)

        builder = Notification.Builder(service, "suivi_gps")
        builder.setContentTitle("Bubu GPS â enregistrement en cours")
        builder.setContentText("Le suivi GPS de la trace live est actif.")
        builder.setSmallIcon(service.getApplicationInfo().icon)
        notification = builder.build()

        try:
            # Android 14+ : type location (25) OBLIGATOIRE.
            service.startForeground(1, notification, 25)
        except Exception:
            # Android plus anciens : variante Ã  2 arguments.
            service.startForeground(1, notification)
    except Exception as e:
        # On continue mÃªme si la notification Ã©choue : l'enregistrement
        # restera valable tant que l'appli est Ã  l'Ã©cran, et l'erreur est
        # consignÃ©e dans le fichier de points (ligne "erreur").
        try:
            with open(CHEMIN_POINTS, "a", encoding="utf-8") as f:
                f.write(json.dumps({"erreur": f"notification: {e}",
                                    "time": datetime.now().isoformat()}) + "\n")
        except Exception:
            pass


def _boucle(contexte, gestionnaire):
    """Thread de sondage : mÃªme logique que gps_natif.py v5."""
    dernier = None
    tours = 0
    while _etat["actif"]:
        # Battement de cÅur (une ligne toutes les 60 s) : permet de
        # vÃ©rifier dans le fichier que le service a SURVÃCU Ã  l'Ã©cran
        # Ã©teint (l'appli ignore ces lignes, cf. _absorber_points_service).
        tours += 1
        if tours % 60 == 0:
            try:
                with open(CHEMIN_POINTS, "a", encoding="utf-8") as f:
                    f.write(json.dumps(
                        {"battement": tours // 60,
                         "time": datetime.now().isoformat()}) + "\n")
            except Exception:
                pass
        try:
            meilleur = None
            for nom_fournisseur in ("fused", "gps", "passive"):
                try:
                    loc = gestionnaire.getLastKnownLocation(nom_fournisseur)
                except Exception:
                    continue
                if loc is None:
                    continue
                if meilleur is None or loc.getTime() > meilleur.getTime():
                    meilleur = loc
            # Fournisseur "network" ignorÃ© : position de cache
            # toujours identique (Redmi/MIUI), trÃ¨s Ã©loignÃ©e de la
            # trace rÃ©elle -> points parasites dans le GPX.
            if meilleur is not None:
                try:
                    if str(meilleur.getProvider() or "") == "network":
                        meilleur = None
                except Exception:
                    pass
            if meilleur is not None:
                lat = float(meilleur.getLatitude())
                lon = float(meilleur.getLongitude())
                if dernier is None or abs(dernier[0] - lat) >= 1e-5 or abs(dernier[1] - lon) >= 1e-5:
                    ele = None
                    try:
                        if meilleur.hasAltitude():
                            ele = round(float(meilleur.getAltitude()), 1)
                    except Exception:
                        pass
                    try:
                        source = str(meilleur.getProvider() or "gps")
                    except Exception:
                        source = "gps"
                    point = {"lat": lat, "lon": lon, "ele": ele,
                             "time": datetime.now().isoformat(), "source": source}
                    with open(CHEMIN_POINTS, "a", encoding="utf-8") as f:
                        f.write(json.dumps(point) + "\n")
                    dernier = (lat, lon)
        except Exception:
            _etat["erreurs"] += 1
        time.sleep(1.0)


def main():
    os.makedirs(DOSSIER_SORTIE, exist_ok=True)
    # Marqueur de session : l'appli sait que le service tourne.
    try:
        with open(CHEMIN_POINTS, "a", encoding="utf-8") as f:
            f.write(json.dumps({"debut_session": datetime.now().isoformat()}) + "\n")
    except Exception:
        pass

    service = _service()
    contexte = cast("android.content.Context", service)

    if not _permission_ok(contexte):
        # Sans permission, le service ne peut rien enregistrer : il
        # s'arrÃªte proprement (l'appli affichera l'erreur de permission).
        return

    LocationManager = autoclass("android.location.LocationManager")
    Looper = autoclass("android.os.Looper")
    gestionnaire = contexte.getSystemService(service.LOCATION_SERVICE)

    # Listener Java factice (le callback Python n'est jamais exÃ©cutÃ©,
    # cf. gps_natif.py) : on l'enregistre uniquement pour FORCER le
    # calcul de positions. Construit via PythonJavaClass.
    from jnius import PythonJavaClass, java_method

    class ListenerFactice(PythonJavaClass):
        __javainterfaces__ = ["android/location/LocationListener"]

        @java_method("()V")
        def onProviderDisabled(self, fournisseur):
            pass

        @java_method("()V")
        def onProviderEnabled(self, fournisseur):
            pass

        @java_method("(Landroid/location/Location;)V")
        def onLocationChanged(self, localisation):
            pass

        @java_method("(Ljava/lang/String;Landroid/os/Bundle;)V")
        def onStatusChanged(self, fournisseur, statut, extras):
            pass

    listener = ListenerFactice()
    try:
        # Seul le fournisseur GPS est enregistrÃ© : le fournisseur
        # "network" renvoie une position de cache grossiÃ¨re et toujours
        # identique sur ce tÃ©lÃ©phone (points parasites dans la trace).
        # Les fixes "fused" restent lus par le sondage (dernier fix
        # calculÃ© par Google Play Services, rafraÃ®chi en continu).
        # Mode Â« PAR DISTANCE Â» (5 m) : voir le commentaire dÃ©taillÃ© de
        # gps_natif.py â comportement le plus propre pour la randonnÃ©e.
        essai = LocationManager.GPS_PROVIDER
        if gestionnaire.isProviderEnabled(essai):
            gestionnaire.requestLocationUpdates(
                essai, 1000, 5.0, listener, Looper.getMainLooper())

    _passer_premier_plan()

    _etat["actif"] = True
    sondage = threading.Thread(target=_boucle, args=(contexte, gestionnaire), daemon=True)
    sondage.start()
    # Le thread principal du service attend : le service vit tant que
    # l'appli ne l'arrÃªte pas (ServiceTracker.stop -> stopService).
    while _etat["actif"]:
        time.sleep(10.0)


main()