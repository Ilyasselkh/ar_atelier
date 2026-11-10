# -*- coding: utf-8 -*-
{
    "name": "ARaymond Atelier — Scan Datamatrix & Palettisation",
    "version": "19.0.2.5.24",
    "summary": "Interface atelier plein écran de scan datamatrix avec visualisation 2D / 3D",
    "description": """
ARaymond Atelier
================
Interface plein écran de scan datamatrix pour atelier de palettisation.

* Scan de codes datamatrix (compatible douchette)
* Parsing GS1 (GTIN, lot, quantite, date, serial)
* Routing automatique en 3 palettes selon le prefixe (1, 2 ou 3)
* Visualisation temps reel 2D isometrique + 3D Three.js
* Capacite 40 boxes par palette (5 couches x 8 boxes en 4x2)
* Alerte palette pleine
* Traçabilite complete en BDD (sessions, palettes, boxes)
""",
    "author": "ARaymond",
    "website": "https://www.araymond.com",
    "category": "Operations/Inventory",
    "license": "LGPL-3",
    "depends": ["base", "web", "mail"],
    "data": [
        "security/atelier_security.xml",
        "security/ir.model.access.csv",
        "data/atelier_sequence.xml",
        "data/atelier_cron.xml",
        "views/atelier_ref_views.xml",
        "views/atelier_box_views.xml",
        "views/atelier_pallet_views.xml",
        "views/atelier_session_views.xml",
        "views/atelier_menus.xml",
        "views/atelier_closure_settings_views.xml",
        "views/atelier_reset_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "ar_atelier/static/src/stock_overview/stock_overview.js",
            "ar_atelier/static/src/stock_overview/stock_overview.xml",
            "ar_atelier/static/src/stock_overview/stock_overview.scss",
            "ar_atelier/static/lib/three/three.min.js",
            "ar_atelier/static/lib/three/OrbitControls.js",
            "ar_atelier/static/src/scan_app/scan_app.scss",
            "ar_atelier/static/src/scan_app/reset_dialog.scss",
            "ar_atelier/static/src/scan_app/scan_app.xml",
            "ar_atelier/static/src/scan_app/scan_app.js",
            "ar_atelier/static/src/scan_app/scan_app_post_tri.js",
            "ar_atelier/static/src/scan_app/scan_app_warehouse.js",
            "ar_atelier/static/src/scan_app/scan_app_warehouse_out.js",
        ],
    },
    "installable": True,
    "application": True,
    "auto_install": False,
}
