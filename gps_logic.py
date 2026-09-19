# -*- coding: utf-8 -*-
"""
============================================================================
 LOGIQUE MÉTIER — OUTILS TRACES & PHOTOS
 Repris depuis start.py : aucune dépendance tkinter/matplotlib ici.
 Utilisable tel quel sur desktop ET sur Android (via Kivy).
============================================================================
"""

import os
import re
import zipfile
import math
from datetime import datetime, timedelta, timezone

import gpxpy
import gpxpy.gpx
import xml.etree.ElementTree as ET
import piexif

KML_NS = "http://www.opengis.net/kml/2.2"
GX_NS = "http://www.google.com/kml/ext/2.2"
ET.register_namespace("", KML_NS)
ET.register_namespace("gx", GX_NS)

GPX_NS = "http://www.topografix.com/GPX/1/1"
GPXX_NS = "http://www.garmin.com/xmlschemas/GpxExtensions/v3"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
ET.register_namespace("", GPX_NS)
ET.register_namespace("gpxx", GPXX_NS)
ET.register_namespace("xsi", XSI_NS)


def _localname(tag):
    """Nom d'une balise XML sans son préfixe de namespace (équivalent à
    local-name() en XPath, mais sans dépendre de lxml)."""
    return tag.split('}', 1)[-1] if '}' in tag else tag


def _findall_localname(root, name):
    """Cherche tous les éléments d'un nom donné, quel que soit le
    namespace/préfixe utilisé dans le fichier source."""
    return [el for el in root.iter() if _localname(el.tag) == name]


def _children_localname(el, name):
    """Enfants directs d'un élément correspondant à un nom donné."""
    return [c for c in el if _localname(c.tag) == name]


def calculer_distance_haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def nettoyer_points_parasites(points):
    if len(points) < 2:
        return points
    pts_uniques = [points[0]]
    for p in points[1:]:
        p_prec = pts_uniques[-1]
        if abs(p['lat'] - p_prec['lat']) > 1e-7 or abs(p['lon'] - p_prec['lon']) > 1e-7:
            pts_uniques.append(p)
    if len(pts_uniques) > 2:
        p_prem = pts_uniques[0]
        p_dern = pts_uniques[-1]
        dist_fermeture = calculer_distance_haversine(p_prem['lat'], p_prem['lon'], p_dern['lat'], p_dern['lon'])
        if dist_fermeture < 2.0:
            pts_uniques.pop()
    return pts_uniques


def lisser_altitudes(points, fenetre=3):
    if len(points) < 3:
        return points
    eles = [p['ele'] for p in points]
    if any(e is None for e in eles):
        return points
    eles_lissees = list(eles)
    for i in range(len(eles)):
        debut = max(0, i - fenetre)
        fin = min(len(eles), i + fenetre + 1)
        sous_ensemble = eles[debut:fin]
        eles_lissees[i] = round(sum(sous_ensemble) / len(sous_ensemble), 1)
    for i, p in enumerate(points):
        p['ele'] = eles_lissees[i]
    return points


def interpoler_altitudes_et_temps(points):
    if not points:
        return []
    points = nettoyer_points_parasites(points)

    distances = [0.0]
    for i in range(1, len(points)):
        d = calculer_distance_haversine(points[i - 1]['lat'], points[i - 1]['lon'], points[i]['lat'], points[i]['lon'])
        distances.append(distances[-1] + d)

    indices_ele = [i for i, p in enumerate(points) if p['ele'] is not None]
    if indices_ele:
        first_idx, last_idx = indices_ele[0], indices_ele[-1]
        for i in range(first_idx):
            points[i]['ele'] = points[first_idx]['ele']
        for i in range(last_idx + 1, len(points)):
            points[i]['ele'] = points[last_idx]['ele']
        for idx in range(len(indices_ele) - 1):
            i_start, i_end = indices_ele[idx], indices_ele[idx + 1]
            d_start, d_end = distances[i_start], distances[i_end]
            e_start, e_end = points[i_start]['ele'], points[i_end]['ele']
            for k in range(i_start + 1, i_end):
                if d_end > d_start:
                    ratio = (distances[k] - d_start) / (d_end - d_start)
                    points[k]['ele'] = round(e_start + ratio * (e_end - e_start), 1)
                else:
                    points[k]['ele'] = round(e_start, 1)

    indices_time = [i for i, p in enumerate(points) if p['time'] is not None]
    if indices_time:
        first_idx, last_idx = indices_time[0], indices_time[-1]
        for i in range(first_idx):
            points[i]['time'] = points[first_idx]['time']
        for i in range(last_idx + 1, len(points)):
            points[i]['time'] = points[last_idx]['time']
        for idx in range(len(indices_time) - 1):
            i_start, i_end = indices_time[idx], indices_time[idx + 1]
            d_start, d_end = distances[i_start], distances[i_end]
            t_start = points[i_start]['time'].timestamp()
            t_end = points[i_end]['time'].timestamp()
            for k in range(i_start + 1, i_end):
                if d_end > d_start:
                    ratio = (distances[k] - d_start) / (d_end - d_start)
                    t_k = t_start + ratio * (t_end - t_start)
                    points[k]['time'] = datetime.fromtimestamp(t_k, tz=timezone.utc)
                else:
                    points[k]['time'] = points[i_start]['time']

    return lisser_altitudes(points)


def lire_fichier_pour_conversion(chemin_fichier):
    extension = os.path.splitext(chemin_fichier)[1].lower()
    points = []

    if extension == ".gpx":
        with open(chemin_fichier, "r", encoding="utf-8") as f:
            gpx = gpxpy.parse(f)
        for trk in gpx.tracks:
            for seg in trk.segments:
                for pt in seg.points:
                    t_val = pt.time
                    if t_val is not None and t_val.tzinfo is not None:
                        t_val = t_val.astimezone().replace(tzinfo=None)
                    points.append({
                        'lat': pt.latitude,
                        'lon': pt.longitude,
                        'ele': round(pt.elevation, 1) if pt.elevation is not None else None,
                        'time': t_val,
                        'name': pt.name
                    })
    elif extension in [".kmz", ".kml"]:
        if extension == ".kmz":
            with zipfile.ZipFile(chemin_fichier, 'r') as z:
                kml_name = next((nom for nom in z.namelist() if nom.lower().endswith('.kml')), None)
                if not kml_name:
                    return []
                root = ET.fromstring(z.read(kml_name))
        else:
            root = ET.parse(chemin_fichier).getroot()

        # Recherche par nom local de balise (indépendant du préfixe de
        # namespace utilisé dans le fichier source), équivalent à
        # local-name() en XPath mais sans dépendre de lxml.
        tracks = _findall_localname(root, 'Track')
        if tracks:
            for track in tracks:
                whens = [c.text for c in _children_localname(track, 'when')]
                coords = [c.text for c in _children_localname(track, 'coord')]
                for idx, c_text in enumerate(coords):
                    if not c_text:
                        continue
                    parts = c_text.strip().split()
                    if len(parts) >= 2:
                        lon, lat = float(parts[0]), float(parts[1])
                        ele = round(float(parts[2]), 1) if len(parts) >= 3 else None
                        t_str = whens[idx].strip() if idx < len(whens) and whens[idx] else None
                        t_val = None
                        if t_str:
                            try:
                                if 'Z' in t_str or '+00:00' in t_str:
                                    dt_parsed = datetime.fromisoformat(t_str.replace("Z", "+00:00"))
                                    t_val = dt_parsed.astimezone().replace(tzinfo=None) if dt_parsed.tzinfo else dt_parsed
                                else:
                                    t_val = datetime.fromisoformat(t_str)
                            except Exception:
                                pass
                        points.append({'lat': lat, 'lon': lon, 'ele': ele, 'time': t_val, 'name': None})

        if not points:
            coord_nodes = _findall_localname(root, 'coordinates')
            meilleur_noeud = None
            max_pts = 0
            for node in coord_nodes:
                if node.text:
                    nb = len(node.text.strip().split())
                    if nb > max_pts:
                        max_pts = nb
                        meilleur_noeud = node
            if meilleur_noeud is not None and meilleur_noeud.text:
                for bloc in meilleur_noeud.text.strip().split():
                    comp = bloc.split(',')
                    if len(comp) >= 2:
                        lon, lat = float(comp[0]), float(comp[1])
                        ele = round(float(comp[2]), 1) if len(comp) >= 3 else None
                        points.append({'lat': lat, 'lon': lon, 'ele': ele, 'time': None, 'name': None})

    return nettoyer_points_parasites(points)


_RE_TRKPT = re.compile(
    r'<trkpt\s+lat="([\-0-9.]+)"\s+lon="([\-0-9.]+)"\s*>(.*?)</trkpt>',
    re.DOTALL,
)
_RE_ELE = re.compile(r'<ele>\s*([\-0-9.]+)\s*</ele>')
_RE_TIME = re.compile(r'<time>\s*([^<]+?)\s*</time>')


def lire_gpx_tolerant(chemin_fichier):
    """Lit un fichier GPX en tolérant un document XML incomplet ou mal
    fermé — typiquement un fichier encore en cours d'écriture par
    GPSLogger au moment de la lecture, dont les balises de fermeture
    </trkseg></trk></gpx> n'ont pas encore été écrites (GPSLogger ne les
    écrit qu'à l'arrêt de l'enregistrement).

    Essaie d'abord une lecture normale et stricte (lire_fichier_pour_
    conversion, gpxpy) ; si celle-ci échoue à cause du document
    incomplet, extrait directement les blocs <trkpt>...</trkpt>
    complets du texte brut par expression régulière (un <trkpt> tronqué
    en toute fin de fichier, sans balise de fermeture, est alors
    naturellement ignoré, sans lever d'exception).

    Renvoie toujours une liste de points (éventuellement vide)."""
    try:
        return lire_fichier_pour_conversion(chemin_fichier)
    except Exception:
        pass

    points = []
    try:
        with open(chemin_fichier, "r", encoding="utf-8", errors="ignore") as f:
            contenu = f.read()
    except OSError:
        return points

    for m in _RE_TRKPT.finditer(contenu):
        try:
            lat = float(m.group(1))
            lon = float(m.group(2))
        except ValueError:
            continue

        bloc = m.group(3)

        ele = None
        m_ele = _RE_ELE.search(bloc)
        if m_ele:
            try:
                ele = round(float(m_ele.group(1)), 1)
            except ValueError:
                ele = None

        t_val = None
        m_time = _RE_TIME.search(bloc)
        if m_time:
            t_str = m_time.group(1).strip()
            try:
                if t_str.endswith("Z"):
                    t_val = datetime.fromisoformat(t_str[:-1] + "+00:00")
                else:
                    t_val = datetime.fromisoformat(t_str)
                if t_val.tzinfo is not None:
                    t_val = t_val.astimezone().replace(tzinfo=None)
            except ValueError:
                t_val = None

        points.append({'lat': lat, 'lon': lon, 'ele': ele, 'time': t_val, 'name': None})

    return nettoyer_points_parasites(points)


def exporter_vers_gpx(points, chemin_sortie, garder_temps=True, waypoints=None):
    gpx = gpxpy.gpx.GPX()
    trk = gpxpy.gpx.GPXTrack()
    gpx.tracks.append(trk)
    seg = gpxpy.gpx.GPXTrackSegment()
    trk.segments.append(seg)
    for p in points:
        t_val = p['time'] if garder_temps else None
        pt = gpxpy.gpx.GPXTrackPoint(p['lat'], p['lon'], elevation=p['ele'], time=t_val, name=p.get('name'))
        seg.points.append(pt)

    # Annotations (photos prises pendant un suivi live via un clic long
    # sur le graphique, voir LiveScreen._ouvrir_camera_Android) :
    # ajoutées comme waypoints GPX ("<wpt>"), visibles comme repères
    # dans n'importe quel logiciel GPS.
    if waypoints:
        for w in waypoints:
            wpt = gpxpy.gpx.GPXWaypoint(
                w['lat'], w['lon'],
                elevation=w.get('ele'),
                time=w['time'] if garder_temps else None,
                name=w.get('name'),
                description=w.get('description'),
            )
            gpx.waypoints.append(wpt)

    with open(chemin_sortie, "w", encoding="utf-8") as f:
        f.write(gpx.to_xml())


def exporter_vers_kml(points, chemin_sortie, garder_temps=True):
    kml = ET.Element("{%s}kml" % KML_NS)
    doc = ET.SubElement(kml, "{%s}Document" % KML_NS)

    style_id = "customTrackStyle"
    style = ET.SubElement(doc, "{%s}Style" % KML_NS, id=style_id)
    line_style = ET.SubElement(style, "{%s}LineStyle" % KML_NS)
    ET.SubElement(line_style, "{%s}color" % KML_NS).text = "99ffac59"
    ET.SubElement(line_style, "{%s}width" % KML_NS).text = "6"

    pm = ET.SubElement(doc, "{%s}Placemark" % KML_NS)
    ET.SubElement(pm, "{%s}name" % KML_NS).text = os.path.splitext(os.path.basename(chemin_sortie))[0]
    ET.SubElement(pm, "{%s}styleUrl" % KML_NS).text = f"#{style_id}"

    a_temps = garder_temps and any(p['time'] is not None for p in points)
    a_altitudes = any(p['ele'] is not None for p in points)

    if a_temps:
        track = ET.SubElement(pm, "{%s}Track" % GX_NS)
        mode_alt = "absolute" if a_altitudes else "clampToGround"
        ET.SubElement(track, "{%s}altitudeMode" % GX_NS).text = mode_alt
        for p in points:
            if p['time']:
                t_str = p['time'].strftime("%Y-%m-%dT%H:%M:%SZ")
                ET.SubElement(track, "{%s}when" % KML_NS).text = t_str
            ele_str = str(round(p['ele'], 1)) if p['ele'] is not None else "0"
            ET.SubElement(track, "{%s}coord" % GX_NS).text = f"{p['lon']} {p['lat']} {ele_str}"
    else:
        ls = ET.SubElement(pm, "{%s}LineString" % KML_NS)
        mode_alt = "absolute" if a_altitudes else "clampToGround"
        ET.SubElement(ls, "{%s}altitudeMode" % GX_NS).text = mode_alt
        coords_str = []
        for p in points:
            ele_str = str(round(p['ele'], 1)) if p['ele'] is not None else "0"
            coords_str.append(f"{p['lon']},{p['lat']},{ele_str}")
        ET.SubElement(ls, "{%s}coordinates" % KML_NS).text = "\n".join(coords_str)

    tree = ET.ElementTree(kml)
    ET.indent(tree, space="  ")
    tree.write(chemin_sortie, xml_declaration=True, encoding="UTF-8")


def exporter_vers_kmz(points, chemin_sortie, garder_temps=True):
    kml_temp = chemin_sortie + ".kml"
    exporter_vers_kml(points, kml_temp, garder_temps=garder_temps)
    with zipfile.ZipFile(chemin_sortie, 'w', zipfile.ZIP_DEFLATED) as z:
        z.write(kml_temp, arcname="doc.kml")
    if os.path.exists(kml_temp):
        os.remove(kml_temp)


EXT_PAR_FORMAT = {"gpx": ".gpx", "kmz": ".kmz", "kml": ".kml"}


def convertir_fichier(fichier_entree, format_sortie, garder_temps, dossier_sortie=None):
    """Fonction de haut niveau utilisée par l'interface Kivy : lit, traite
    (interpolation/lissage) et exporte un fichier de trace. Retourne le
    chemin du fichier créé."""
    points = lire_fichier_pour_conversion(fichier_entree)
    if not points:
        raise ValueError("Aucun point GPS n'a pu être extrait de ce fichier.")

    points_traites = interpoler_altitudes_et_temps(points)
    ext = EXT_PAR_FORMAT[format_sortie]

    if dossier_sortie is None:
        dossier_sortie = os.path.dirname(fichier_entree)
    os.makedirs(dossier_sortie, exist_ok=True)

    nom_defaut = os.path.splitext(os.path.basename(fichier_entree))[0] + f"_converti{ext}"
    chemin_sortie = os.path.join(dossier_sortie, nom_defaut)

    if format_sortie == "gpx":
        exporter_vers_gpx(points_traites, chemin_sortie, garder_temps=garder_temps)
    elif format_sortie == "kml":
        exporter_vers_kml(points_traites, chemin_sortie, garder_temps=garder_temps)
    elif format_sortie == "kmz":
        exporter_vers_kmz(points_traites, chemin_sortie, garder_temps=garder_temps)

    return chemin_sortie


# ============================================================================
# ONGLET 2 : NUMÉROTATION ET NETTOYAGE
# Repris depuis start.py (init_onglet2_numerotation et fonctions associées),
# logique métier uniquement — aucune dépendance tkinter.
# ============================================================================

def valider_numero_point(val) -> str:
    """
    Règle pour le champ 'N° Point' :
    - Retourne '-' si le champ n'est pas explicitement nommé (vide, inconnu, etc.).
    - Retourne '-' si le champ comporte 2 noms/numéros (ex: '12 / 13', '10 & 11', '14-15').
    - Conservé uniquement s'il possède un identifiant / numéro unique.
    """
    if val is None:
        return "-"

    val_str = str(val).strip()
    if not val_str:
        return "-"

    unnamed_keywords = ["none", "null", "nan", "unnamed", "inconnu", "sans nom", "-"]
    if val_str.lower() in unnamed_keywords:
        return "-"

    separators_pattern = r'[/&,;]|\bet\b'
    if re.search(separators_pattern, val_str, re.IGNORECASE):
        return "-"

    digits_found = re.findall(r'\d+', val_str)
    if len(digits_found) >= 2:
        return "-"

    return val_str


def analyser_liste_suppression(texte_saisie, max_points):
    """Analyse les numéros et tranches (ex: 5, 12, 20-35) à supprimer."""
    if not texte_saisie.strip():
        return None, "Veuillez entrer au moins un numéro ou une tranche à supprimer."

    indices = set()
    blocs = texte_saisie.split(",")

    for bloc in blocs:
        bloc = bloc.strip()
        if not bloc:
            continue

        match_tranche = re.match(r"^(\d+)\s*-\s*(\d+)$", bloc)
        if match_tranche:
            debut = int(match_tranche.group(1))
            fin = int(match_tranche.group(2))
            if debut > fin:
                debut, fin = fin, debut
            for i in range(debut, fin + 1):
                if 1 <= i <= max_points:
                    indices.add(i)
            continue

        if bloc.isdigit():
            num = int(bloc)
            if 1 <= num <= max_points:
                indices.add(num)
            continue

        return None, f"Format invalide détecté : '{bloc}'.\nUtilisez des numéros séparés par des virgules ou des tranches (ex: 5, 12, 20-35)."

    if not indices:
        return None, f"Aucun numéro valide dans la plage 1 à {max_points} n'a été saisi."

    return indices, None


def extraire_donnees_gpx_kmz(fichier_entree):
    """Retourne (segments_lus, deja_numerote).
    segments_lus est une liste de segments, chaque segment une liste de
    tuples (lat:str, lon:str, ele:str|None, time_str:str|None, nom:str|None)."""
    segments_lus = []
    deja_numerote = False
    extension = os.path.splitext(fichier_entree)[1].lower()

    if extension == ".gpx":
        try:
            with open(fichier_entree, "r", encoding="utf-8") as f:
                gpx_data = gpxpy.parse(f)
            for track in gpx_data.tracks:
                for segment in track.segments:
                    coords_segment = []
                    for point in segment.points:
                        lat = str(point.latitude)
                        lon = str(point.longitude)
                        ele = str(round(point.elevation, 1)) if point.elevation is not None else None

                        time_str = point.time.isoformat() if point.time is not None else None
                        if time_str and time_str.endswith("+00:00"):
                            time_str = time_str[:-6] + "Z"

                        if point.name and point.name.strip().isdigit():
                            deja_numerote = True

                        coords_segment.append((lat, lon, ele, time_str, point.name))
                    if coords_segment:
                        segments_lus.append(coords_segment)
        except Exception as e:
            raise RuntimeError(f"Impossible de lire le fichier GPX :\n{e}")

    elif extension in [".kmz", ".kml"]:
        try:
            pts = lire_fichier_pour_conversion(fichier_entree)
            if pts:
                segment = []
                for p in pts:
                    t_str = p['time'].strftime("%Y-%m-%dT%H:%M:%SZ") if p['time'] else None
                    e_str = str(round(p['ele'], 1)) if p['ele'] is not None else None
                    segment.append((str(p['lat']), str(p['lon']), e_str, t_str, p.get('name')))
                segments_lus.append(segment)
        except Exception as e:
            raise RuntimeError(f"Impossible de lire le fichier KMZ/KML :\n{e}")

    return segments_lus, deja_numerote


def calculer_legende_numerotation(segments_lus, mode_choisi, est_inverse, entree_suppr=""):
    """Calcule un aperçu des changements (compteurs) avant exécution."""
    segments_a_traiter = segments_lus
    if est_inverse:
        segments_a_traiter = [list(reversed(seg)) for seg in reversed(segments_lus)]

    indices_suppr_apercu = set()
    if mode_choisi == "supprimer_points":
        total_pts_calc = sum(len(seg) for seg in segments_a_traiter)
        indices_suppr_apercu, _ = analyser_liste_suppression(entree_suppr, total_pts_calc)
        if not indices_suppr_apercu:
            indices_suppr_apercu = set()

    compteurs = {"inverse": 1 if est_inverse else 0, "ajoute": 0, "modifie": 0, "retire": 0, "inchange": 0, "supprime": 0}
    compteur_lecture_global = 0
    compteur_ecriture_global = 0

    for segment in segments_a_traiter:
        for item in segment:
            compteur_lecture_global += 1
            ancien_numero = valider_numero_point(item[4] if len(item) > 4 else None)

            if mode_choisi == "supprimer_points" and compteur_lecture_global in indices_suppr_apercu:
                compteurs["supprime"] += 1
                continue

            compteur_ecriture_global += 1
            nouveau_numero = None
            if mode_choisi in ["numeroter", "supprimer_points"]:
                nouveau_numero = str(compteur_ecriture_global)
            elif mode_choisi == "aucun" and ancien_numero != "-":
                nouveau_numero = ancien_numero

            if mode_choisi == "denumero":
                statut = "retire" if ancien_numero != "-" else "inchange"
            elif ancien_numero == "-" and nouveau_numero is not None:
                statut = "ajoute"
            elif ancien_numero != "-" and nouveau_numero is not None and ancien_numero != nouveau_numero:
                statut = "modifie"
            else:
                statut = "inchange"

            compteurs[statut] += 1

    return compteurs


LEGENDE_NUMEROTATION = [
    ("inverse", "Trace inversée"),
    ("ajoute", "Numéro ajouté"),
    ("modifie", "Numéro renuméroté"),
    ("retire", "Numéro retiré"),
    ("inchange", "Numéro inchangé"),
    ("supprime", "Point supprimé"),
]


def traiter_numerotation(fichier_entree, segments_lus, mode_choisi, est_inverse, entree_suppr=""):
    """Applique inversion/numérotation/dénumérotation/suppression et écrit
    un nouveau fichier GPX. Retourne (chemin_sortie, message_resume)."""
    if not est_inverse and mode_choisi == "aucun":
        raise ValueError("Sélectionnez au moins une action (Inverser ou Traitement).")

    base_path = os.path.splitext(fichier_entree)[0]
    segments_a_traiter = segments_lus
    if est_inverse:
        segments_a_traiter = [list(reversed(seg)) for seg in reversed(segments_lus)]

    indices_a_supprimer = set()
    if mode_choisi == "supprimer_points":
        total_pts_calc = sum(len(seg) for seg in segments_a_traiter)
        indices_a_supprimer, err_msg = analyser_liste_suppression(entree_suppr, total_pts_calc)
        if err_msg:
            raise ValueError(err_msg)

    sufixes = []
    if est_inverse:
        sufixes.append("inverse")
    if mode_choisi == "numeroter":
        sufixes.append("numerote")
    elif mode_choisi == "denumero":
        sufixes.append("denumerote")
    elif mode_choisi == "supprimer_points":
        sufixes.append("nettoye")
    sufixe = "_" + "_".join(sufixes) if sufixes else "_traite"

    nom_base_fichier = os.path.basename(base_path) + sufixe
    gpx_root = ET.Element("{%s}gpx" % GPX_NS, attrib={
        "version": "1.1",
        "creator": "GPX_Track_Processor",
        "{%s}schemaLocation" % XSI_NS: (
            f"{GPX_NS} http://www.topografix.com/GPX/1/1/gpx.xsd "
            f"{GPXX_NS} http://www.garmin.com/xmlschemas/GpxExtensionsv3.xsd"
        ),
    })
    meta = ET.SubElement(gpx_root, "{%s}metadata" % GPX_NS)
    ET.SubElement(meta, "{%s}name" % GPX_NS).text = nom_base_fichier
    ext_node = ET.SubElement(gpx_root, "{%s}extensions" % GPX_NS)
    route_ext = ET.SubElement(ext_node, "{%s}Extension" % GPXX_NS)
    ET.SubElement(route_ext, "{%s}Name" % GPXX_NS).text = nom_base_fichier

    trk = ET.SubElement(gpx_root, "{%s}trk" % GPX_NS)
    ET.SubElement(trk, "{%s}name" % GPX_NS).text = nom_base_fichier

    compteur_lecture_global = 0
    compteur_ecriture_global = 0

    for segment in segments_a_traiter:
        trkseg = ET.SubElement(trk, "{%s}trkseg" % GPX_NS)
        for item in segment:
            lat, lon, ele, time_str = item[0], item[1], item[2], item[3]
            nom_orig = item[4] if len(item) > 4 else None
            compteur_lecture_global += 1
            if mode_choisi == "supprimer_points" and compteur_lecture_global in indices_a_supprimer:
                continue
            compteur_ecriture_global += 1
            trkpt = ET.SubElement(trkseg, "{%s}trkpt" % GPX_NS, attrib={"lat": lat, "lon": lon})

            if mode_choisi in ["numeroter", "supprimer_points"]:
                ET.SubElement(trkpt, "{%s}name" % GPX_NS).text = str(compteur_ecriture_global)
            elif mode_choisi == "aucun" and nom_orig:
                ET.SubElement(trkpt, "{%s}name" % GPX_NS).text = str(nom_orig)

            if ele:
                ET.SubElement(trkpt, "{%s}ele" % GPX_NS).text = ele
            if time_str:
                ET.SubElement(trkpt, "{%s}time" % GPX_NS).text = time_str

    fichier_sortie = f"{base_path}{sufixe}.gpx"
    c = 1
    while os.path.exists(fichier_sortie):
        fichier_sortie = f"{base_path}{sufixe}_{c}.gpx"
        c += 1

    tree_gpx = ET.ElementTree(gpx_root)
    ET.indent(tree_gpx, space="  ")
    tree_gpx.write(fichier_sortie, xml_declaration=True, encoding="UTF-8")

    detail_msg = []
    if est_inverse:
        detail_msg.append("Trace inversée")
    if mode_choisi == "numeroter":
        detail_msg.append(f"{compteur_ecriture_global} points numérotés")
    elif mode_choisi == "denumero":
        detail_msg.append("Numéros retirés")
    elif mode_choisi == "supprimer_points":
        detail_msg.append(f"{len(indices_a_supprimer)} point(s) supprimé(s), {compteur_ecriture_global} points restants renumérotés")

    msg = " - ".join(detail_msg) if detail_msg else "Traitement effectué"
    return fichier_sortie, msg


# ----------------------------------------------------------------------
# FUSION DE TRACES (onglet 3)
# ----------------------------------------------------------------------

def nettoyer_points_fusion(points):
    resultat = []
    precedent = None
    for p in points:
        if precedent == p:
            continue
        resultat.append(p)
        precedent = p
    return resultat


def signature_segment(segment):
    return tuple(
        (round(p[0], 6), round(p[1], 6), round(p[2], 1) if p[2] is not None else None)
        for p in segment
    )


def supprimer_doublons_segments(segments):
    vus = set()
    resultat = []
    for s in segments:
        sig = signature_segment(s)
        if sig not in vus:
            vus.add(sig)
            resultat.append(s)
    return resultat


def lire_gpx_fusion(fichier):
    segments = []
    with open(fichier, encoding="utf-8") as f:
        gpx = gpxpy.parse(f)
    for trk in gpx.tracks:
        for seg in trk.segments:
            pts = [(p.latitude, p.longitude, round(p.elevation, 1) if p.elevation is not None else None) for p in seg.points]
            pts = nettoyer_points_fusion(pts)
            if len(pts) > 1:
                segments.append(pts)
    return supprimer_doublons_segments(segments)


def lire_kmz_fusion(fichier):
    pts = lire_fichier_pour_conversion(fichier)
    if len(pts) > 1:
        segment = [(p['lat'], p['lon'], p['ele']) for p in pts]
        return [segment]
    return []


def charger_segment_fusion(fichier, inverser=False):
    if os.path.splitext(fichier)[1].lower() == ".gpx":
        segments = lire_gpx_fusion(fichier)
    else:
        segments = lire_kmz_fusion(fichier)

    if inverser:
        segments_inverses = []
        for seg in reversed(segments):
            segments_inverses.append(list(reversed(seg)))
        return segments_inverses
    return segments


def fusionner_tous_segments(segments):
    if not segments:
        return []
    fusion = []
    for seg in segments:
        if not fusion:
            fusion.extend(seg)
            continue
        if fusion[-1] == seg[0]:
            fusion.extend(seg[1:])
        else:
            fusion.extend(seg)
    return [fusion]


def sauver_fusion_gpx(segments, sortie):
    gpx = gpxpy.gpx.GPX()
    trk = gpxpy.gpx.GPXTrack()
    gpx.tracks.append(trk)

    for segment in segments:
        seg = gpxpy.gpx.GPXTrackSegment()
        trk.segments.append(seg)
        for lat, lon, ele in segment:
            pt = gpxpy.gpx.GPXTrackPoint(lat, lon, elevation=ele)
            pt.time = None
            seg.points.append(pt)

    with open(sortie, "w", encoding="utf-8") as f:
        f.write(gpx.to_xml())


def traiter_fusion(fichiers_fusion, dossier_sortie, nom_sortie="fusion.gpx"):
    """Fonction de haut niveau utilisée par l'interface Kivy :
    fichiers_fusion est une liste de dicts {"path": ..., "inverser": bool}.
    Retourne le chemin du fichier GPX fusionné créé."""
    if len(fichiers_fusion) < 2:
        raise ValueError("Ajoute au moins 2 fichiers pour fusionner.")

    tous_segments = []
    for item in fichiers_fusion:
        tous_segments.extend(charger_segment_fusion(item["path"], inverser=item["inverser"]))

    final_segments = fusionner_tous_segments(tous_segments)

    os.makedirs(dossier_sortie, exist_ok=True)
    sortie = os.path.join(dossier_sortie, nom_sortie)
    c = 1
    base, ext = os.path.splitext(sortie)
    while os.path.exists(sortie):
        sortie = f"{base}_{c}{ext}"
        c += 1

    sauver_fusion_gpx(final_segments, sortie)
    return sortie


# ----------------------------------------------------------------------
# DÉCOUPE DE TRACE (onglet 4, partie "Découpe" seulement pour l'instant —
# la carte interactive et le graphique altitude/vitesse sont prévus pour
# une prochaine étape, voir README.md)
# ----------------------------------------------------------------------

TAILLE_TUILE = 256


def projeter_mercator(lat, lon, zoom):
    """Convertit une coordonnée GPS en position pixel (Web Mercator),
    à un niveau de zoom donné. Utilisé pour placer la trace et
    convertir un point tapé sur la carte, indépendamment des méthodes
    internes de kivy_garden.mapview (non vérifiables sans Kivy)."""
    taille_monde = TAILLE_TUILE * (2 ** zoom)
    x = (lon + 180.0) / 360.0 * taille_monde
    siny = math.sin(math.radians(lat))
    siny = min(max(siny, -0.9999), 0.9999)
    y = (0.5 - math.log((1 + siny) / (1 - siny)) / (4 * math.pi)) * taille_monde
    return x, y


def deprojeter_mercator(x, y, zoom):
    """Opération inverse de projeter_mercator : pixel -> GPS."""
    taille_monde = TAILLE_TUILE * (2 ** zoom)
    lon = x / taille_monde * 360.0 - 180.0
    n = math.pi - 2.0 * math.pi * y / taille_monde
    lat = math.degrees(math.atan(math.sinh(n)))
    return lat, lon


def calculer_profil(points):
    """Calcule les distances cumulées (km), l'altitude (en filtrant les
    points sans altitude) et la vitesse (km/h) le long de la trace.
    Repris de afficher_profils() dans la version desktop (sans la partie
    matplotlib). Retourne (distances_km, distances_avec_ele, altitudes,
    vitesses_kmh)."""
    if not points:
        return [], [], [], []

    distances_km = [0.0]
    for i in range(1, len(points)):
        d = calculer_distance_haversine(
            points[i - 1]['lat'], points[i - 1]['lon'], points[i]['lat'], points[i]['lon']
        )
        distances_km.append(distances_km[-1] + d / 1000.0)

    eles_brutes = [p['ele'] for p in points]
    distances_avec_ele = [distances_km[i] for i, e in enumerate(eles_brutes) if e is not None]
    altitudes = [e for e in eles_brutes if e is not None]

    vitesses_kmh = [0.0]
    for i in range(1, len(points)):
        p1, p2 = points[i - 1], points[i]
        if p1['time'] and p2['time']:
            dt = (p2['time'] - p1['time']).total_seconds()
            if dt > 0:
                d_m = (distances_km[i] - distances_km[i - 1]) * 1000.0
                vitesses_kmh.append(round((d_m / dt) * 3.6, 1))
            else:
                vitesses_kmh.append(0.0)
        else:
            vitesses_kmh.append(0.0)

    return distances_km, distances_avec_ele, altitudes, vitesses_kmh


def calculer_statistiques(points):
    """Calcule les statistiques globales d'une trace (repris de
    mettre_a_jour_statistiques_globales dans la version desktop).
    Retourne un dict avec les mêmes clés que valeurs_dict sur desktop :
    alt_depart, alt_max, distance, den_pos, km_effort, temps_total,
    temps_marche, vit_moy, allure."""
    if not points:
        return {}

    alt_depart = points[0].get('ele')
    altitudes = [p['ele'] for p in points if p.get('ele') is not None]
    alt_max = max(altitudes) if altitudes else None

    distance_totale = 0.0
    denivele_positif = 0.0

    for i in range(1, len(points)):
        p1, p2 = points[i - 1], points[i]
        d = calculer_distance_haversine(p1['lat'], p1['lon'], p2['lat'], p2['lon'])
        distance_totale += d

        if p1.get('ele') is not None and p2.get('ele') is not None:
            diff_ele = p2['ele'] - p1['ele']
            if diff_ele > 0:
                denivele_positif += diff_ele

    dist_km = distance_totale / 1000.0
    km_effort = dist_km + (denivele_positif / 100.0)

    temps_total_sec = 0.0
    temps_marche_sec = 0.0

    if points[0].get('time') and points[-1].get('time'):
        t_debut = points[0]['time']
        t_fin = points[-1]['time']
        if t_debut.tzinfo:
            t_debut = t_debut.replace(tzinfo=None)
        if t_fin.tzinfo:
            t_fin = t_fin.replace(tzinfo=None)
        temps_total_sec = max(0.0, (t_fin - t_debut).total_seconds())

    for i in range(1, len(points)):
        p1, p2 = points[i - 1], points[i]
        if p1.get('time') and p2.get('time'):
            t1, t2 = p1['time'], p2['time']
            if t1.tzinfo:
                t1 = t1.replace(tzinfo=None)
            if t2.tzinfo:
                t2 = t2.replace(tzinfo=None)
            dt = (t2 - t1).total_seconds()
            if dt > 0:
                d = calculer_distance_haversine(p1['lat'], p1['lon'], p2['lat'], p2['lon'])
                v_kmh = (d / dt) * 3.6
                if v_kmh >= 0.5:
                    temps_marche_sec += dt

    if temps_marche_sec == 0 and dist_km > 0:
        temps_marche_sec = (dist_km / 4.0) * 3600.0

    if temps_total_sec == 0:
        temps_total_sec = temps_marche_sec

    vit_moy = (dist_km / (temps_marche_sec / 3600.0)) if temps_marche_sec > 0 else 0.0
    allure_min_km = (60.0 / vit_moy) if vit_moy > 0 else 0.0

    str_alt_dep = f"{alt_depart:.1f} m" if alt_depart is not None else "N/A"
    str_alt_max = f"{alt_max:.1f} m" if alt_max is not None else "N/A"
    str_dist = f"{dist_km:.2f} km"
    str_den = f"{denivele_positif:.1f} m"
    str_effort = f"{km_effort:.2f} km-effort"

    str_t_total = str(timedelta(seconds=int(temps_total_sec)))
    str_t_marche = str(timedelta(seconds=int(temps_marche_sec)))
    str_vit = f"{vit_moy:.2f} km/h"

    if allure_min_km > 0:
        m_al = int(allure_min_km)
        s_al = int((allure_min_km - m_al) * 60)
        str_allure = f"{m_al} min {s_al:02d} s / km"
    else:
        str_allure = "N/A"

    return {
        "alt_depart": str_alt_dep,
        "alt_max": str_alt_max,
        "distance": str_dist,
        "den_pos": str_den,
        "km_effort": str_effort,
        "temps_total": str_t_total,
        "temps_marche": str_t_marche,
        "vit_moy": str_vit,
        "allure": str_allure,
    }


def decouper_trace(fichier_entree, points, point_coupure, dossier_sortie=None):
    """Découpe une trace déjà chargée (liste de points issue de
    lire_fichier_pour_conversion) en 2 fichiers GPX de part et d'autre du
    point de coupure (numéro 1-indexé, inclus dans les deux parties, comme
    dans la version desktop). Retourne (chemin_partie1, chemin_partie2)."""
    max_pts = len(points)
    if not (1 <= point_coupure <= max_pts):
        raise ValueError(f"Le numéro doit être compris entre 1 et {max_pts}.")

    part1 = points[:point_coupure]
    part2 = points[point_coupure - 1:]

    if dossier_sortie is None:
        dossier_sortie = os.path.dirname(fichier_entree)
    os.makedirs(dossier_sortie, exist_ok=True)
    nom_base = os.path.splitext(os.path.basename(fichier_entree))[0]

    chemin1 = os.path.join(dossier_sortie, f"{nom_base}_part_1.gpx")
    chemin2 = os.path.join(dossier_sortie, f"{nom_base}_part_2.gpx")
    exporter_vers_gpx(part1, chemin1)
    exporter_vers_gpx(part2, chemin2)
    return chemin1, chemin2


# ----------------------------------------------------------------------
# ONGLET PHOTOS : lecture/écriture des tags EXIF (date/heure, GPS) et
# recherche du point de trace le plus proche d'un horodatage. Logique
# reprise sans modification fonctionnelle de start.py (le formulaire
# Tkinter est remplacé, côté main.py, par des champs Kivy).
# ----------------------------------------------------------------------

def _dms_vers_degres(dms, ref):
    """Convertit un triplet EXIF ((deg,1),(min,1),(sec,100)) en degrés
    décimaux signés (négatif pour S/W)."""
    def _frac(x):
        num, den = x
        return num / den if den else 0.0

    degres = _frac(dms[0]) + _frac(dms[1]) / 60.0 + _frac(dms[2]) / 3600.0
    ref_str = ref.decode("utf-8") if isinstance(ref, bytes) else ref
    if ref_str in ("S", "W"):
        degres = -degres
    return round(degres, 6)


def _degres_vers_dms(valeur):
    """Convertit des degrés décimaux en triplet EXIF ((deg,1),(min,1),(sec,100))."""
    abs_val = abs(valeur)
    deg = int(abs_val)
    min_float = (abs_val - deg) * 60
    minute = int(min_float)
    sec = int((min_float - minute) * 60 * 100)
    return ((deg, 1), (minute, 1), (sec, 100))


def get_exif_data(chemin_photo):
    """Lit les tags EXIF Date/Heure et GPS d'une photo JPEG. Renvoie un
    dict {'datetime', 'latitude', 'longitude', 'altitude'} avec None
    pour toute valeur absente ou illisible (photo sans EXIF, fichier
    corrompu...)."""
    resultat = {"datetime": None, "latitude": None, "longitude": None, "altitude": None}
    try:
        exif_dict = piexif.load(chemin_photo)
    except Exception:
        return resultat

    try:
        dt_brut = exif_dict.get("Exif", {}).get(piexif.ExifIFD.DateTimeOriginal)
        if not dt_brut:
            dt_brut = exif_dict.get("0th", {}).get(piexif.ImageIFD.DateTime)
        if dt_brut:
            resultat["datetime"] = dt_brut.decode("utf-8", errors="ignore") if isinstance(dt_brut, bytes) else str(dt_brut)
    except Exception:
        pass

    gps = exif_dict.get("GPS", {})
    try:
        if piexif.GPSIFD.GPSLatitude in gps and piexif.GPSIFD.GPSLatitudeRef in gps:
            resultat["latitude"] = _dms_vers_degres(gps[piexif.GPSIFD.GPSLatitude], gps[piexif.GPSIFD.GPSLatitudeRef])
        if piexif.GPSIFD.GPSLongitude in gps and piexif.GPSIFD.GPSLongitudeRef in gps:
            resultat["longitude"] = _dms_vers_degres(gps[piexif.GPSIFD.GPSLongitude], gps[piexif.GPSIFD.GPSLongitudeRef])
        if piexif.GPSIFD.GPSAltitude in gps:
            num, den = gps[piexif.GPSIFD.GPSAltitude]
            resultat["altitude"] = round(num / den, 1) if den else None
    except Exception:
        pass

    return resultat


def find_closest_point(points, dt_str, tolerance_secondes=900):
    """Cherche, dans 'points' (issus de lire_fichier_pour_conversion), le
    point dont l'horodatage ('time') est le plus proche de dt_str (heure
    EXIF de la photo, formats "AAAA:MM:JJ HH:MM:SS" ou
    "AAAA-MM-JJ HH:MM:SS" acceptés). Renvoie None si dt_str est vide/
    invalide, si aucun point de la trace n'a d'horodatage, OU si le point
    le plus proche trouvé est distant de plus de 'tolerance_secondes'
    (15 minutes par défaut) de l'horodatage demandé — la photo est alors
    considérée comme ne correspondant pas à la trace chargée, plutôt que
    de renvoyer un point sans rapport avec elle."""
    if not dt_str or not points:
        return None

    dt_cible = None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M", "%Y-%m-%d %H:%M"):
        try:
            dt_cible = datetime.strptime(dt_str.strip(), fmt)
            break
        except ValueError:
            continue
    if dt_cible is None:
        return None

    meilleur = None
    meilleur_delta = None
    for p in points:
        t = p.get("time")
        if t is None:
            continue
        delta = abs((t - dt_cible).total_seconds())
        if meilleur_delta is None or delta < meilleur_delta:
            meilleur_delta = delta
            meilleur = p

    if meilleur is None or meilleur_delta > tolerance_secondes:
        return None

    return meilleur


def enregistrer_exif_gps(chemin_photo, latitude, longitude, altitude=None, date_heure=None):
    """Écrit les tags EXIF GPS (et éventuellement Date/Heure) dans une
    photo JPEG, en conservant le reste des EXIF existants. Reprend sans
    modification fonctionnelle la logique d'enregistrer_exif() de la
    version desktop (le formulaire Tkinter devient de simples
    paramètres)."""
    exif_dict = piexif.load(chemin_photo)

    gps_ifd = {
        piexif.GPSIFD.GPSLatitudeRef: 'S' if latitude < 0 else 'N',
        piexif.GPSIFD.GPSLatitude: _degres_vers_dms(latitude),
        piexif.GPSIFD.GPSLongitudeRef: 'W' if longitude < 0 else 'E',
        piexif.GPSIFD.GPSLongitude: _degres_vers_dms(longitude),
    }
    if altitude is not None:
        gps_ifd[piexif.GPSIFD.GPSAltitudeRef] = 0
        gps_ifd[piexif.GPSIFD.GPSAltitude] = (int(round(altitude * 100)), 100)
    exif_dict['GPS'] = gps_ifd

    if date_heure:
        valeur = date_heure.encode('utf-8')
        exif_dict.setdefault('0th', {})[piexif.ImageIFD.DateTime] = valeur
        exif_dict.setdefault('Exif', {})[piexif.ExifIFD.DateTimeOriginal] = valeur
        exif_dict.setdefault('Exif', {})[piexif.ExifIFD.DateTimeDigitized] = valeur

    exif_bytes = piexif.dump(exif_dict)
    piexif.insert(exif_bytes, chemin_photo)
