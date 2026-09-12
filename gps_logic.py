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
from datetime import datetime, timezone

import gpxpy
import gpxpy.gpx
import xml.etree.ElementTree as ET

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


def exporter_vers_gpx(points, chemin_sortie, garder_temps=True):
    gpx = gpxpy.gpx.GPX()
    trk = gpxpy.gpx.GPXTrack()
    gpx.tracks.append(trk)
    seg = gpxpy.gpx.GPXTrackSegment()
    trk.segments.append(seg)
    for p in points:
        t_val = p['time'] if garder_temps else None
        pt = gpxpy.gpx.GPXTrackPoint(p['lat'], p['lon'], elevation=p['ele'], time=t_val, name=p.get('name'))
        seg.points.append(pt)
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
    ("inverse", "#8E24AA", "Trace inversée"),
    ("ajoute", "#43A047", "Numéro ajouté"),
    ("modifie", "#1E88E5", "Numéro renuméroté"),
    ("retire", "#FB8C00", "Numéro retiré"),
    ("inchange", "#FBC02D", "Numéro inchangé"),
    ("supprime", "#E53935", "Point supprimé"),
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
