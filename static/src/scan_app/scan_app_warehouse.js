/** @odoo-module **/

import { ScanAppPostTri } from "./scan_app_post_tri";
import { registry } from "@web/core/registry";

export class ScanAppWarehouse extends ScanAppPostTri {
    setup() {
        super.setup();
        this.mode = "warehouse";
    }
}

registry.category("actions").add("ar_atelier_scan_app_warehouse", ScanAppWarehouse);
