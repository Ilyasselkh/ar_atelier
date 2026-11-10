# -*- coding: utf-8 -*-
"""
Parseur GS1 pour codes datamatrix.

Application Identifiers (AI) GS1 supportés :
    01  GTIN-14            longueur fixe 14 chiffres
    10  Lot / Batch        longueur variable jusqu'à 20 caractères
    11  Date production    YYMMDD
    13  Date conditionnement YYMMDD
    15  Best before        YYMMDD
    17  Date expiration    YYMMDD
    21  Numéro de série    longueur variable jusqu'à 20 caractères
    30  Quantité variable  longueur variable jusqu'à 8 chiffres
    37  Quantité totale    longueur variable jusqu'à 8 chiffres
    310y Poids net (kg)    longueur fixe 6 chiffres + décimales y

Le séparateur FNC1 (ASCII 29 / GS) délimite les AI de longueur variable.
On accepte aussi le caractère '|' comme séparateur si la douchette ne transmet pas le FNC1.
"""

import re
from datetime import datetime

# Caractère séparateur GS1 FNC1
FNC1 = chr(29)
# Séparateurs alternatifs tolérés
ALT_SEPARATORS = (FNC1, "|", "\x1d")

# Définition des AI : code -> (label, longueur fixe ou None, type)
AI_FIXED_LENGTH = {
    "01": ("gtin", 14, "str"),
    "11": ("production_date", 6, "date"),
    "13": ("packaging_date", 6, "date"),
    "15": ("best_before_date", 6, "date"),
    "17": ("expiration_date", 6, "date"),
}

AI_VARIABLE_LENGTH = {
    "10": ("lot", 20, "str"),
    "21": ("serial", 20, "str"),
    "30": ("variable_qty", 8, "int"),
    "37": ("total_qty", 8, "int"),
}


def _parse_date_yymmdd(value):
    """Parse une date GS1 YYMMDD. JJ=00 traité comme dernier jour du mois."""
    if not value or len(value) != 6:
        return False
    try:
        yy = int(value[0:2])
        mm = int(value[2:4])
        dd = int(value[4:6])
        # Convention GS1 : 49 et avant -> 20xx, 50 et après -> 19xx
        year = 2000 + yy if yy < 50 else 1900 + yy
        if dd == 0:
            dd = 28  # par sécurité
        return datetime(year, mm, dd).date()
    except (ValueError, TypeError):
        return False


def parse_gs1(raw_code):
    """
    Parse un code GS1 brut.

    :param raw_code: chaîne brute scannée
    :return: dict avec les champs détectés + 'raw' + 'section' (1/2/3 d'après le 1er digit)
    """
    if not raw_code:
        return {}

    result = {"raw": raw_code}

    # Section : 1er caractère numérique pour le routing palette
    first_char = raw_code[0] if raw_code else ""
    if first_char in ("1", "2", "3"):
        result["section"] = int(first_char)
    else:
        result["section"] = 0  # non routé

    # Normalisation des séparateurs
    code = raw_code
    for sep in ALT_SEPARATORS[1:]:
        code = code.replace(sep, FNC1)

    # Parcours séquentiel
    i = 0
    n = len(code)
    while i < n:
        # Sauter un éventuel FNC1 résiduel
        if code[i] == FNC1:
            i += 1
            continue

        # Lire les 2 prochains caractères comme AI
        if i + 2 > n:
            break
        ai = code[i:i + 2]
        i += 2

        # AI à longueur fixe
        if ai in AI_FIXED_LENGTH:
            label, length, dtype = AI_FIXED_LENGTH[ai]
            value = code[i:i + length]
            i += length
            if dtype == "date":
                result[label] = _parse_date_yymmdd(value)
            else:
                result[label] = value
            continue

        # AI à longueur variable (jusqu'à FNC1 ou fin de chaîne)
        if ai in AI_VARIABLE_LENGTH:
            label, max_length, dtype = AI_VARIABLE_LENGTH[ai]
            # Trouver le prochain FNC1
            end = code.find(FNC1, i)
            if end == -1 or end > i + max_length:
                end = min(i + max_length, n)
            value = code[i:end]
            i = end
            if dtype == "int":
                try:
                    result[label] = int(value)
                except ValueError:
                    result[label] = 0
            else:
                result[label] = value
            continue

        # AI inconnu : on stoppe le parsing pour éviter une cascade d'erreurs
        # On garde la partie restante en raw_remainder
        result["raw_remainder"] = code[i - 2:]
        break

    return result


def quick_section(raw_code):
    """Retourne uniquement la section (1, 2 ou 3) à partir du 1er caractère."""
    if not raw_code:
        return 0
    first = raw_code[0]
    if first in ("1", "2", "3"):
        return int(first)
    return 0


# =============================================================================
# Parseur format custom ARaymond — étiquette palette
# =============================================================================
# Exemple :
# {BTYP:UID;BVER:3.0;MAT:505075566;PWG:216.4;PLT:1051;PO:14785885;
#  POMAT:505075566;SLOC:FG01;QTY:16;WGHTG:3462.4;SUT:A5;HU:1001;
#  BSD:2025-02-28 16\:03\:57;BAT:C5F000011I;PAL:0}
# Format : key:value séparés par ';' entourés de '{}'.
# Les ':' dans les valeurs sont échappés par '\:'
# =============================================================================

def parse_barcode_label(raw_code):
    """Fixed fields: MAT(9), BAT(10), QTY(7), PO(8), then HU including '+'."""
    if not raw_code:
        return None
    code = raw_code.strip()
    match = re.fullmatch(r'([0-9]{9})([A-Za-z0-9]{10})([0-9]{7})([0-9]{8})([A-Za-z0-9]+\+)', code)
    if not match:
        return None
    mat, bat, qty, po, hu = match.groups()
    return {'raw': code, '_format': 'barcode', 'mat': mat, 'bat': bat,
            'qty': int(qty), 'po': po, 'hu': hu}


def parse_label(raw_code):
    """Recognize either supported label and return common business fields."""
    return parse_custom_label(raw_code) or parse_barcode_label(raw_code)


def parse_custom_label(raw_code):
    """
    Parse une étiquette au format {KEY:VALUE;KEY:VALUE;...}.
    Retourne un dict des champs détectés (string keys mappés vers nos champs).
    Retourne None si le format ne correspond pas.
    """
    if not raw_code:
        return None
    code = raw_code.strip()
    if not (code.startswith("{") and code.endswith("}")):
        return None
    inner = code[1:-1]
    if not inner:
        return None

    # Split sur ';' non échappés (pas de \; à gérer ici car ';' n'est pas dans les valeurs)
    parts = inner.split(";")
    parsed = {}
    for token in parts:
        if not token:
            continue
        # Split sur le premier ':' non échappé
        idx = -1
        i = 0
        while i < len(token):
            if token[i] == "\\" and i + 1 < len(token):
                i += 2
                continue
            if token[i] == ":":
                idx = i
                break
            i += 1
        if idx == -1:
            continue
        key = token[:idx].strip()
        value = token[idx + 1:].strip()
        # Unescape \:
        value = value.replace("\\:", ":")
        parsed[key.upper()] = value

    if not parsed:
        return None

    # Mapping vers nos champs métier
    result = {"raw": raw_code, "_format": "custom"}
    if "MAT" in parsed:
        result["mat"] = parsed["MAT"]
    if "PWG" in parsed:
        try: result["pwg"] = float(parsed["PWG"])
        except (ValueError, TypeError): pass
    if "PLT" in parsed:
        result["plt"] = parsed["PLT"]
    if "PO" in parsed:
        result["po"] = parsed["PO"]
    if "POMAT" in parsed:
        result["pomat"] = parsed["POMAT"]
    if "SLOC" in parsed:
        result["sloc"] = parsed["SLOC"]
    if "QTY" in parsed:
        try: result["qty"] = int(parsed["QTY"])
        except (ValueError, TypeError): pass
    if "WGHTG" in parsed:
        try: result["wghtg"] = float(parsed["WGHTG"])
        except (ValueError, TypeError): pass
    if "SUT" in parsed:
        result["sut"] = parsed["SUT"]
    if "HU" in parsed:
        result["hu"] = parsed["HU"]
    if "BSD" in parsed:
        result["bsd"] = parsed["BSD"]  # date string brute, on stocke en char
    if "BAT" in parsed:
        result["bat"] = parsed["BAT"]
    if "PAL" in parsed:
        result["pal"] = parsed["PAL"]
    if "BTYP" in parsed:
        result["btyp"] = parsed["BTYP"]
    if "BVER" in parsed:
        result["bver"] = parsed["BVER"]
    return result
