/** @odoo-module **/

import { ScanApp } from "./scan_app";
import { registry } from "@web/core/registry";

/**
 * Client action "Sortie de tri" : même UI que ScanApp, mais en mode post_tri.
 * - Session avec mode='post_tri' (récupérée/créée via get_or_create_active(mode='post_tri'))
 * - 2 palettes : Stock (1) + Anomalie (3) — pas de palette tri (2)
 * - Côté serveur : routing différent (UC + already_in_tri, pas d'OF, pas de tri hebdo)
 */
export class ScanAppPostTri extends ScanApp {
    static template = "ar_atelier.ScanApp";

    setup() {
        super.setup();
        // Override du mode : on est en sortie de tri
        this.mode = "post_tri";

        // Override des palettes par défaut : 2 seulement (1=stock, 3=anomalie)
        this.state.pallets = [
            { number: 1, capacity: 40, rows: 2, cols: 4, layers: 5, boxes: [] },
            { number: 3, capacity: 40, rows: 2, cols: 4, layers: 5, boxes: [] },
        ];

        // Adapter les labels affichés
        this.palletLabels = {
            1: "Palette pour stock",
            3: "Palette en anomalie",
        };
    }
}

registry.category("actions").add("ar_atelier_scan_app_post_tri", ScanAppPostTri);
