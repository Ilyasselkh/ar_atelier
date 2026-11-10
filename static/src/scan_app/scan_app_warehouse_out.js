/** @odoo-module **/

import { ScanApp } from "./scan_app";
import { registry } from "@web/core/registry";

export class ScanAppWarehouseOut extends ScanApp {
    setup() {
        super.setup();
        this.mode = "warehouse_out";
        this.state.pallets = [
            { number: 2, capacity: 40, rows: 2, cols: 4, layers: 5, boxes: [] },
            { number: 3, capacity: 40, rows: 2, cols: 4, layers: 5, boxes: [] },
        ];
        this.palletLabels = { 2: "Palette pour tri", 3: "Palette en anomalie" };
    }
}

registry.category("actions").add("ar_atelier_scan_app_warehouse_out", ScanAppWarehouseOut);
