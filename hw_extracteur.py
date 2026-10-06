#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Extracteur de héros et titans — Hero Wars (version web : Facebook, OK.ru, hero-wars.com...)
Sortie : fichier CSV ouvrable dans Excel.

──────────────────────────────────────────────────────────────────────────────
 COMMENT OBTENIR LES DONNÉES DU JEU (2 minutes, aucune compétence requise)
──────────────────────────────────────────────────────────────────────────────
 1. Ouvre Hero Wars dans Opera et connecte-toi.
 2. Appuie sur F12 (ou Ctrl+Maj+I) → onglet « Réseau » (Network).
 3. Recharge la page (F5) et laisse le jeu se charger.
 4. Dans la liste des requêtes, cherche une réponse volumineuse contenant
    tes données de héros (souvent liée au lancement de session du jeu).
 5. Clic droit n'importe où dans la liste → « Enregistrer tout au format
    HAR avec le contenu » (Save all as HAR with content).
 6. Enregistre le fichier, par exemple : C:\\Users\\bubu\\Downloads\\herowars.har
 7. Lance le script :

      python hw_extracteur.py C:\\Users\\bubu\\Downloads\\herowars.har
      python hw_extracteur.py herowars.har -o mes_heros.csv
      python hw_extracteur.py dossier_de_json/        # analyse tous les .json/.har d'un dossier

 Le script accepte : un fichier .har, un fichier .json (données brutes),
 ou un dossier entier. Il cherche automatiquement les héros et les titans.

──────────────────────────────────────────────────────────────────────────────
 PRÉ-REQUIS : Python 3.8+ uniquement (aucune librairie externe à installer).
──────────────────────────────────────────────────────────────────────────────
"""

import argparse
import base64
import csv
import json
import os
import re
import sys

# ---------------------------------------------------------------------------
# Champs utilisés pour décider si un objet JSON ressemble à un héros/titan.
# ---------------------------------------------------------------------------
MARQUEURS_HEROS = {"power", "color", "xp", "skills", "glyphs", "level"}
MARQUEURS_OBLIGATOIRES = {"level", "power"}

# Colonnes mises en avant en tête du CSV (les autres suivent, triées).
COLONNES_PREFEREES = [
    "id", "type", "nom", "niveau", "puissance", "couleur", "etoiles",
    "vie", "force", "intelligence", "agilite",
]


def iterer_objets(node, chemin="$"):
    """Parcourt récursivement le JSON en produisant (chemin, dictionnaire)."""
    if isinstance(node, dict):
        yield chemin, node
        for cle, valeur in node.items():
            yield from iterer_objets(valeur, f"{chemin}.{cle}")
    elif isinstance(node, list):
        for index, valeur in enumerate(node):
            yield from iterer_objets(valeur, f"{chemin}[{index}]")


def ressemble_a_un_heros(obj):
    """Vrai si l'objet contient les marqueurs typiques d'un héros ou titan."""
    cles = set(obj.keys())
    if not (cles & MARQUEURS_OBLIGATOIRES):
        return False
    return len(cles & MARQUEURS_HEROS) >= 2


def est_titan(chemin, obj):
    """Détecte un titan via le chemin JSON ou un champ spécifique."""
    chemin_bas = chemin.lower()
    if "titan" in chemin_bas:
        return True
    return any("titan" in str(cle).lower() for cle in obj.keys())


def extraire_nom(obj):
    """Récupère un nom s'il est présent dans les données."""
    for cle in ("name", "nom", "heroName", "title"):
        valeur = obj.get(cle)
        if isinstance(valeur, str) and valeur.strip():
            return valeur.strip()
    return ""


def aplatischir(obj, prefixe=""):
    """Transforme les valeurs scalaires en colonnes simples."""
    resultat = {}
    for cle, valeur in obj.items():
        if isinstance(valeur, (dict, list)):
            continue  # les structures complexes partent dans la colonne json_brut
        if isinstance(valeur, bool):
            valeur = int(valeur)
        resultat[str(cle)] = valeur
    return resultat


def traduire_champs(champs):
    """Renomme les champs usuels du jeu en colonnes lisibles."""
    table = {
        "level": "niveau",
        "power": "puissance",
        "color": "couleur",
        "star": "etoiles",
        "stars": "etoiles",
        "hp": "vie",
        "strength": "force",
        "intelligence": "intelligence",
        "agility": "agilite",
        "name": "nom",
    }
    return {table.get(k, k): v for k, v in champs.items()}


def analyser_document(donnees, source, trouves):
    """Cherche tous les héros/titans dans une structure JSON déjà chargée."""
    for chemin, obj in iterer_objets(donnees):
        if not ressemble_a_un_heros(obj):
            continue
        type_unite = "Titan" if est_titan(chemin, obj) else "Héros"
        id_unite = chemin.rsplit(".", 1)[-1].split("[")[-1].rstrip("]")
        champs = aplatischir(obj)
        champs = traduire_champs(champs)
        champs["id"] = champs.get("id", id_unite)
        champs["type"] = type_unite
        if not champs.get("nom"):
            champs["nom"] = extraire_nom(obj)
        champs["source"] = os.path.basename(source)
        champs["json_brut"] = json.dumps(obj, ensure_ascii=False, sort_keys=True)
        cle_doublon = (champs["id"], type_unite,
                       champs.get("niveau"), champs.get("puissance"))
        trouves.setdefault(cle_doublon, champs)


def charger_fichier(chemin_fichier):
    """Charge un .json ou un .har (export réseau de Opera DevTools)."""
    with open(chemin_fichier, "r", encoding="utf-8", errors="replace") as f:
        contenu = f.read()

    racine = json.loads(contenu)
    documents = []
    if isinstance(racine, dict) and "log" in racine and "entries" in racine.get("log", {}):
        # Fichier HAR : on fouille le corps de chaque requête/réponse,
        # en gérant le contenu encodé en base64 et le texte posté (postData).
        for entree in racine["log"]["entries"]:
            try:
                reponse = entree.get("response", {})
                contenu_rep = reponse.get("content", {})
                for brut in (contenu_rep.get("text", ""),
                             entree.get("request", {}).get("postData", {}).get("text", "")):
                    if not brut or len(brut) < 20:
                        continue
                    if contenu_rep.get("encoding") == "base64":
                        try:
                            brut = base64.b64decode(brut).decode("utf-8", errors="replace")
                        except Exception:
                            continue
                    documents.extend(tenter_json(brut))
            except Exception:
                continue
        if not documents:
            # Dernier recours : le HAR peut contenir du JSON "aplati" ou purgé
            # (export sanitized). On balaie tout le texte brut du fichier.
            documents = tenter_json(contenu, complet=False)
        return documents
    return [racine] if isinstance(racine, (dict, list)) else []


def tenter_json(texte, complet=True):
    """Trouve tous les objets/tableaux JSON exploitables dans un texte."""
    resultats = []
    decodeur = json.JSONDecoder()
    if complet:
        texte = texte.strip()
        if texte[0:1] in "[{":
            try:
                return [json.loads(texte)]
            except ValueError:
                pass
    # Balayage : chaque '{' ou '[' peut amorcer un document JSON.
    for amorce in re.finditer(r"[\[{]", texte):
        try:
            valeur, _ = decodeur.raw_decode(texte[amorce.start():])
        except ValueError:
            continue
        if isinstance(valeur, (dict, list)):
            resultats.append(valeur)
    return resultats


def collecter_fichiers(cible):
    """Renvoie la liste des fichiers .json/.har à analyser."""
    if os.path.isdir(cible):
        fichiers = []
        for nom in sorted(os.listdir(cible)):
            if nom.lower().endswith((".json", ".har")):
                fichiers.append(os.path.join(cible, nom))
        return fichiers
    return [cible]


def main():
    parseur = argparse.ArgumentParser(
        description="Extrait les héros et titans de Hero Wars vers un CSV.")
    parseur.add_argument("cible",
                         help="Fichier .har / .json ou dossier à analyser.")
    parseur.add_argument("-o", "--sortie", default="heros_hero_wars.csv",
                         help="Nom du fichier CSV de sortie (défaut : heros_hero_wars.csv).")
    arguments = parseur.parse_args()

    fichiers = collecter_fichiers(arguments.cible)
    if not fichiers:
        print("Aucun fichier .json ou .har trouvé.")
        sys.exit(1)

    trouves = {}
    for fichier in fichiers:
        try:
            documents = charger_fichier(fichier)
        except (OSError, ValueError) as erreur:
            print(f"  !! {fichier} ignoré ({erreur})")
            continue
        print(f"Analyse de {fichier} ({len(documents)} document(s) JSON)...")
        for document in documents:
            analyser_document(document, fichier, trouves)

    if not trouves:
        print("\nAucun héros détecté. Vérifie que le fichier HAR a bien été "
              "enregistré APRÈS le chargement du jeu (étapes 3-4 de l'aide), "
              "et qu'il contient les réponses réseau (option « avec le contenu »).")
        sys.exit(2)

    # Tri : héros d'abord, puis par puissance décroissante.
    lignes = sorted(trouves.values(),
                    key=lambda r: (r["type"], -int(r.get("puissance") or 0)))

    # Colonnes : les préférées d'abord, puis le reste par ordre alphabétique.
    toutes_colonnes = set()
    for ligne in lignes:
        toutes_colonnes.update(ligne.keys())
    colonnes = [c for c in COLONNES_PREFEREES if c in toutes_colonnes]
    colonnes += sorted(toutes_colonnes - set(colonnes))

    # utf-8-sig : les accents s'affichent correctement dans Excel.
    with open(arguments.sortie, "w", newline="", encoding="utf-8-sig") as f:
        ecrivain = csv.DictWriter(f, fieldnames=colonnes, extrasaction="ignore")
        ecrivain.writeheader()
        ecrivain.writerows(lignes)

    nb_heros = sum(1 for r in lignes if r["type"] == "Héros")
    nb_titans = sum(1 for r in lignes if r["type"] == "Titan")
    print(f"\n✔ {nb_heros} héros et {nb_titans} titans extraits.")
    print(f"✔ Fichier créé : {os.path.abspath(arguments.sortie)}")


if __name__ == "__main__":
    main()