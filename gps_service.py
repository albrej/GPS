# -*- coding: utf-8 -*-
from kivy.utils import platform

class NativeGPSManager:
    """Gestionnaire GPS natif Android via LocationManager."""

    def __init__(self, callback_position):
        """
        callback_position: fonction appelée à chaque nouveau point GPS.
        Prend en argument un dictionnaire:
        {'lat': float, 'lon': float, 'ele': float, 'speed': float, 'time': datetime}
        """
        self.callback_position = callback_position
        self.is_running = False
        self._location_listener = None
        self._location_manager = None

        if platform == "android":
            self._init_android_gps()

    def _init_android_gps(self):
        from jnius import autoclass, PythonJavaClass, java_method
        from android.permissions import request_permissions, Permission, check_permission

        # Demander l'autorisation à l'utilisateur si non accordée
        if not check_permission(Permission.ACCESS_FINE_LOCATION):
            request_permissions([Permission.ACCESS_FINE_LOCATION, Permission.ACCESS_COARSE_LOCATION])

        # Import des classes Java
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        Context = autoclass('android.content.Context')
        LocationManager = autoclass('android.location.LocationManager')

        activity = PythonActivity.mActivity
        self._location_manager = activity.getSystemService(Context.LOCATION_SERVICE)

        # Implémentation de l'interface Java LocationListener
        class LocationListener(PythonJavaClass):
            __javainterfaces__ = ['android/location/LocationListener']

            def __init__(self, outer):
                super().__init__()
                self.outer = outer

            @java_method('(Landroid/location/Location;)V')
            def onLocationChanged(self, location):
                if location is None:
                    return
                
                lat = location.getLatitude()
                lon = location.getLongitude()
                ele = location.getAltitude() if location.hasAltitude() else None
                speed = location.getSpeed() * 3.6 if location.hasSpeed() else 0.0 # m/s -> km/h

                from datetime import datetime, timezone
                time_val = datetime.fromtimestamp(location.getTime() / 1000.0, tz=timezone.utc)

                data = {
                    'lat': lat,
                    'lon': lon,
                    'ele': round(ele, 1) if ele is not None else None,
                    'speed': round(speed, 1),
                    'time': time_val
                }
                
                # Exécution du callback sur le thread principal Kivy
                from kivy.clock import Clock
                Clock.schedule_once(lambda dt: self.outer.callback_position(data))

            @java_method('(Ljava/lang/String;)V')
            def onProviderDisabled(self, provider):
                pass

            @java_method('(Ljava/lang/String;)V')
            def onProviderEnabled(self, provider):
                pass

            @java_method('(Ljava/lang/String;ILandroid/os/Bundle;)V')
            def onStatusChanged(self, provider, status, extras):
                pass

        self._listener_class = LocationListener

    def start(self, min_time_ms=1000, min_distance_m=1):
        """Démarre l'écoute du GPS_PROVIDER."""
        if platform != "android" or self.is_running:
            return

        from jnius import autoclass
        LocationManager = autoclass('android.location.LocationManager')

        self._location_listener = self._listener_class(self)
        try:
            # Demande les mises à jour via GPS_PROVIDER
            self._location_manager.requestLocationUpdates(
                LocationManager.GPS_PROVIDER,
                int(min_time_ms),
                float(min_distance_m),
                self._location_listener
            )
            self.is_running = True
        except Exception as e:
            print(f"Erreur démarrage GPS_PROVIDER : {e}")

    def stop(self):
        """Arrête l'écoute du GPS."""
        if platform == "android" and self.is_running and self._location_listener:
            self._location_manager.removeUpdates(self._location_listener)
            self.is_running = False
            self._location_listener = None