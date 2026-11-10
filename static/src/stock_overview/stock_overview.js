/** @odoo-module **/

import { Component, onMounted, onWillDestroy, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

export class StockOverview extends Component {
    static template = "ar_atelier.StockOverview";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.state = useState({ counters: null, loading: false, error: "", auto: true, updated: "", selected: "initial" });
        this.cards = [
            { key: "initial", label: "Sortie PROD", icon: "fa-industry" },
            { key: "post_tri", label: "Sortie de tri", icon: "fa-filter" },
            { key: "warehouse", label: "Entrée Magasin LOG", icon: "fa-sign-in" },
            { key: "warehouse_out", label: "Sortie Magasin LOG", icon: "fa-sign-out" },
        ];
        this.destroyed = false;
        onWillStart(() => this.refresh());
        onMounted(() => {
            this.timer = setInterval(() => {
                if (this.state.auto && !document.hidden) this.refresh();
            }, 30000);
        });
        onWillDestroy(() => { this.destroyed = true; clearInterval(this.timer); });
    }

    format(value) { return Number(value || 0).toLocaleString("fr-FR"); }
    dailyValue(value) { return value === null ? '—' : this.format(value); }
    dateLabel(value) {
        return new Date(value + 'T12:00:00').toLocaleDateString('fr-FR', { weekday: 'long', day: '2-digit', month: '2-digit' });
    }

    share(key) {
        const total = this.state.counters?.total || 0;
        return total ? Math.round((this.state.counters[key] || 0) / total * 1000) / 10 : 0;
    }

    barWidth(key) {
        const max = Math.max(1, ...this.cards.map(card => this.state.counters?.[card.key] || 0));
        return (this.state.counters?.[key] || 0) / max * 100;
    }

    get selectedCard() { return this.cards.find(card => card.key === this.state.selected); }
    selectStage(key) { this.state.selected = key; }
    toggleAuto() { this.state.auto = !this.state.auto; }

    async refresh() {
        if (this.state.loading) return;
        this.state.loading = true;
        this.state.error = "";
        try {
            const counters = await this.orm.call("ar.atelier.stock.overview", "get_weekly_counters", []);
            if (this.destroyed) return;
            this.state.counters = counters;
            this.state.updated = new Date().toLocaleTimeString("fr-FR");
        } catch (error) {
            if (!this.destroyed) this.state.error = error.data?.message || error.message || "Impossible de charger les compteurs.";
        } finally {
            if (!this.destroyed) this.state.loading = false;
        }
    }
}

registry.category("actions").add("ar_atelier_stock_overview", StockOverview);
