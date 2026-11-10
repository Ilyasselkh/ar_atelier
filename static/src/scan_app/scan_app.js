/** @odoo-module **/

import { Component, useState, useRef, onMounted, onWillUnmount, onWillStart, useEffect } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Dialog } from "@web/core/dialog/dialog";

class ClosureBlockedDialog extends Component {
    static template = "ar_atelier.ClosureBlockedDialog";
    static components = { Dialog };
    static props = { close: Function, count: Number, onReturn: Function };

    returnToAnomalies() {
        this.props.close();
        this.props.onReturn();
    }
}

export class ScanApp extends Component {
    static template = "ar_atelier.ScanApp";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.actionService = useService("action");
        this.dialog = useService("dialog");
        // Mode de la session : 'initial' (par défaut) ou 'post_tri' (override dans sous-classe)
        this.mode = "initial";

        this.scanInputRef = useRef("scanInput");
        this.threeContainerRef = useRef("threeContainer");
        this.correctionScanRef = useRef("correctionScan");
        this.correctionActionsRef = useRef("correctionActions");

        this.colorsHex = { 1: "#1976d2", 2: "#388e3c", 3: "#f57c00" };
        this.colorsInt = { 1: 0x1976d2, 2: 0x388e3c, 3: 0xf57c00 };
        this.palletLabels = {
            1: "Palette pour stock",
            2: "Palette pour tri",
            3: "Palette en anomalie",
        };

        this.sessionId = null;

        this.state = useState({
            sessionName: "",
            sessionState: "",
            operatorCode: "",
            operatorName: "",
            warehouseOutReason: "",
            mode: "2d",
            adminMenuOpen: false,
            pallets: [
                { number: 1, capacity: 40, rows: 2, cols: 4, layers: 5, boxes: [] },
                { number: 2, capacity: 40, rows: 2, cols: 4, layers: 5, boxes: [] },
                { number: 3, capacity: 40, rows: 2, cols: 4, layers: 5, boxes: [] },
            ],
            message: "",
            messageType: "info",
            detailBox: null,
            correctionMode: false,
            refStatus: { uc: {}, tri: {}, of: {} },
            refStatusError: false,
        });

        this.three = {
            scene: null, camera: null, renderer: null, controls: null,
            animationId: null, cubeGroups: { 1: null, 2: null, 3: null },
        };

        onWillStart(async () => {
            const requestedSession = this.props.action?.params?.session_id;
            if (requestedSession) {
                const action = await this.orm.call(
                    'ar.atelier.session', 'action_resume_scan', [[requestedSession]]
                );
                this.sessionId = action.params.session_id;
                this.mode = action.params.mode;
            } else {
                this.sessionId = await this.orm.call(
                    "ar.atelier.session", "get_or_create_active", [], { mode: this.mode }
                );
            }
            await this.loadRefStatus();
            await this.loadState();
        });

        onMounted(() => { this.focusScan(); });

        useEffect(
            () => {
                if (this.state.mode === "3d") this.init3D();
                else this.dispose3D();
                return () => {};
            },
            () => [this.state.mode]
        );

        // Refocus auto sur le scan input quand modal/correction se ferment
        useEffect(
            () => {
                if (!this.state.detailBox && !this.state.correctionMode) {
                    setTimeout(() => this.focusScan(), 50);
                }
                return () => {};
            },
            () => [this.state.detailBox, this.state.correctionMode]
        );

        // Listener global : refocus sur scan si on clique en dehors d'un input/textarea/button
        this._globalClickHandler = (ev) => {
            if (this.state.detailBox || this.state.correctionMode) return;
            const tag = ev.target.tagName;
            if (tag === "INPUT" || tag === "TEXTAREA" || tag === "BUTTON" || tag === "SELECT") return;
            // Pas dans un menu admin ou autre contrôle
            if (ev.target.closest && ev.target.closest(".o_ar_sa_admin_dropdown")) return;
            setTimeout(() => this.focusScan(), 0);
        };
        onMounted(() => {
            document.addEventListener("click", this._globalClickHandler, true);
            this._refTimer = setInterval(() => this.loadRefStatus(), 10000);
            this._activityHandler = (ev) => {
                if (!ev.isTrusted || !ev.target.closest?.('.o_ar_scan_app') || !this.sessionId) return;
                clearTimeout(this._activityTimer);
                const sessionId = this.sessionId;
                this._activityTimer = setTimeout(() => {
                    this.orm.call('ar.atelier.session', 'record_activity', [[sessionId]])
                        .catch(() => {});
                }, 500);
            };
            document.addEventListener('click', this._activityHandler, true);
            document.addEventListener('input', this._activityHandler, true);
            this._idleTimer = setInterval(() => this.checkInactivity(), 10000);
        });
        onWillUnmount(() => {
            clearInterval(this._refTimer);
            clearInterval(this._idleTimer);
            clearTimeout(this._activityTimer);
            document.removeEventListener('click', this._activityHandler, true);
            document.removeEventListener('input', this._activityHandler, true);
            document.removeEventListener("click", this._globalClickHandler, true);
            this.dispose3D();
        });
    }

    async checkInactivity() {
        if (!this.sessionId || this._checkingIdle || this.state.sessionState !== 'in_progress') return;
        this._checkingIdle = true;
        const sessionId = this.sessionId;
        try {
            const result = await this.orm.call('ar.atelier.session', 'check_inactivity', [[sessionId]]);
            if (sessionId === this.sessionId && ['done', 'reset'].includes(result.state)) {
                await this.loadState();
                this.showMessage(result.state === 'reset' ? 'Session réinitialisée. Scannez votre matricule pour commencer.' : `Session clôturée après ${result.idle_timeout_minutes} minutes sans activité.`, 'info');
            }
        } catch (e) {
            // Retry on the next check; the server scheduler remains active.
        } finally {
            this._checkingIdle = false;
        }
    }

    get counters() {
        const c = { p1: 0, p2: 0, p3: 0, cap1: 40, cap2: 40, cap3: 40, total: 0 };
        for (const p of this.state.pallets) {
            const n = p.boxes.length;
            c.total += n;
            if (p.number === 1) { c.p1 = n; c.cap1 = p.capacity; }
            if (p.number === 2) { c.p2 = n; c.cap2 = p.capacity; }
            if (p.number === 3) { c.p3 = n; c.cap3 = p.capacity; }
        }
        return c;
    }

    getLabel(pallet) {
        return this.palletLabels[pallet.number] || ("Palette " + pallet.number);
    }

    focusScan() {
        if (this._closureDialogOpen) return;
        if (this.scanInputRef.el) this.scanInputRef.el.focus();
    }

    get referentialsBlocked() {
        return this.state.refStatusError || !Array.isArray(this.state.refStatus.missing) || this.state.refStatus.missing.length > 0;
    }

    get isSessionOpen() {
        return this.state.sessionState === "in_progress" && Boolean(this.state.operatorCode.trim() && this.state.operatorName.trim());
    }

    get pageTitle() {
        return { initial: "Sortie PROD", post_tri: "Sortie de tri", warehouse: "Entré Magasin LOG", warehouse_out: "Sortie Magasin LOG" }[this.mode];
    }

    get scanBlocked() {
        return !this.isSessionOpen || this.referentialsBlocked;
    }

    get scanBlockedMessage() {
        return !this.isSessionOpen ? "Identifiez-vous pour commencer le scan." : this.referentialsBlockedMessage;
    }

    get referentialsBlockedMessage() {
        if (this.state.refStatusError) return "Impossible de vérifier UC, TRI et OF. Nouvelle tentative automatique dans quelques instants.";
        const missing = this.state.refStatus.missing;
        if (!missing) return "Vérification des référentiels UC, TRI et OF…";
        const labels = missing.length > 1 ? `${missing.slice(0, -1).join(', ')} et ${missing[missing.length - 1]}` : missing[0];
        return `Merci d’alimenter ${labels} pour autoriser le scan.`;
    }

    async loadState() {
        if (!this.sessionId) {
            this.state.sessionName = '';
            this.state.sessionState = '';
            this.state.operatorCode = '';
            this.state.operatorName = '';
            this.state.warehouseOutReason = '';
            this.state.pallets = this.state.pallets.map(p => ({ ...p, boxes: [] }));
            if (this.state.mode === '3d') this.refresh3DBoxes();
            return;
        }
        try {
            const result = await this.orm.call(
                "ar.atelier.session", "get_pallet_state", [[this.sessionId]]
            );
            this.state.sessionName = result.session_name || "";
            this.state.sessionState = result.session_state || "";
            this.state.operatorCode = result.operator_code || "";
            this.state.operatorName = result.operator_name || "";
            this.state.warehouseOutReason = result.warehouse_out_reason || '';
            this.state.pallets = result.pallets || this.state.pallets;
            if (this.state.mode === "3d") this.refresh3DBoxes();
        } catch (e) {
            console.error("Erreur chargement palettes", e);
        }
    }

    async loadRefStatus() {
        if (this._loadingRefs) return;
        this._loadingRefs = true;
        const wasBlocked = this.scanBlocked;
        try {
            const s = await this.orm.call(
                "ar.atelier.session", "get_referential_status", [], { mode: this.mode }
            );
            this.state.refStatus = s || { uc: {}, tri: {}, of: {} };
            this.state.refStatusError = false;
            if (wasBlocked && !this.scanBlocked) setTimeout(() => this.focusScan(), 0);
        } catch (e) {
            this.state.refStatusError = true;
            console.error("Erreur chargement réf status", e);
        } finally {
            this._loadingRefs = false;
        }
    }

    setMode(m) { this.state.mode = m; }

    onScanKeyDown(ev) {
        if (ev.key === "Enter") {
            ev.preventDefault();
            const code = ev.target.value.trim();
            if (code) {
                this.processScan(code);
                ev.target.value = "";
            }
        }
    }

    async processScan(rawCode) {
        if (this._closureDialogOpen) return;
        const command = rawCode.trim().toUpperCase();
        const isSessionCommand = command.startsWith("START;") || command === "END;SCAN";
        if (this.scanBlocked && !isSessionCommand) {
            this.showMessage(this.scanBlockedMessage, "error");
            return;
        }
        try {
            if (!this.sessionId || ['done', 'cancelled', 'reset'].includes(this.state.sessionState)) {
                this.sessionId = await this.orm.call('ar.atelier.session', 'get_or_create_active', [],
                    { mode: this.mode, raw_code: rawCode });
                await this.loadState();
                this.showMessage('Opérateur identifié. Session ouverte.', 'success');
                return;
            }
            const result = await this.orm.call(
                "ar.atelier.session", "action_scan", [[this.sessionId], rawCode]
            );
            if (result.closure_blocked) {
                if (result.reason_required) {
                    this.showMessage(result.message, 'error');
                    return;
                }
                this.showClosureBlocked(result.anomaly_count);
                return;
            }
            this.showMessage(result.message, result.success ? "success" : "error");
            if (result.referential_blocked) await this.loadRefStatus();
            if (result.identification_required) await this.loadState();
            if (result.success) {
                if (result.action === "session_changed" && result.new_session_id) {
                    this.sessionId = result.new_session_id;
                } else if (result.action === "session_closed_empty") {
                    // Revenir à l'attente d'identification après la clôture.
                    this.sessionId = await this.orm.call(
                        "ar.atelier.session", "get_or_create_active", [], { mode: this.mode }
                    );
                }
                await this.loadState();
            }
        } catch (e) {
            this.showMessage("Erreur : " + (e.message || e), "error");
        } finally {
            this.focusScan();
        }
    }

    async saveWarehouseOutReason() {
        if (!this.sessionId || this.mode !== 'warehouse_out') return;
        try {
            await this.orm.call('ar.atelier.session', 'save_warehouse_out_reason',
                [[this.sessionId], this.state.warehouseOutReason]);
        } catch (e) {
            this.showMessage('Erreur lors de l’enregistrement du motif : ' + (e.message || e), 'error');
        }
    }

    async closeAndNew() {
        if (!this.sessionId || this._closureDialogOpen) return;
        if (this.mode === 'warehouse_out') await this.saveWarehouseOutReason();
        await this.loadState();
        if (this.counters.p3 > 0) {
            this.showClosureBlocked(this.counters.p3);
            return;
        }
        if (!window.confirm("Clôturer la session courante ?")) return;
        try {
            const newId = await this.orm.call(
                "ar.atelier.session", "action_close_and_new", [[this.sessionId]]
            );
            if (newId && newId.closure_blocked) {
                if (newId.reason_required) {
                    this.showMessage(newId.message, 'error');
                    return;
                }
                this.showClosureBlocked(newId.anomaly_count);
                return;
            }
            this.sessionId = newId;
            await this.loadState();
            this.showMessage("Session clôturée. Scannez votre matricule pour commencer.", "success");
        } catch (e) {
            this.showMessage("Erreur : " + (e.message || e), "error");
        }
    }

    showClosureBlocked(count) {
        if (this._closureDialogOpen) return;
        this._closureDialogOpen = true;
        this.dialog.add(ClosureBlockedDialog, {
            count,
            onReturn: () => {
                this.setMode("2d");
                this.loadState();
            },
        }, {
            onClose: () => {
                this._closureDialogOpen = false;
                this.focusScan();
            },
        });
    }

    showMessage(msg, type = "info") {
        this.state.message = msg;
        this.state.messageType = type;
        clearTimeout(this._msgTimeout);
        this._msgTimeout = setTimeout(() => { this.state.message = ""; }, 4000);
    }

    // -------------------------------------------------------- Modal détail boxe

    async openBoxDetail(boxId) {
        try {
            const detail = await this.orm.call(
                "ar.atelier.session", "get_box_detail", [boxId]
            );
            if (detail.success) {
                this.state.detailBox = detail;
                this.state.correctionMode = false;
            } else {
                this.showMessage(detail.message || "Erreur détail", "error");
            }
        } catch (e) {
            this.showMessage("Erreur : " + (e.message || e), "error");
        }
    }

    closeBoxDetail() {
        this.state.detailBox = null;
        this.state.correctionMode = false;
        this.focusScan();
    }

    enterCorrectionMode() {
        this.state.correctionMode = true;
        setTimeout(() => {
            if (this.correctionActionsRef.el) this.correctionActionsRef.el.focus();
        }, 100);
    }

    cancelCorrection() {
        this.state.correctionMode = false;
    }

    onCorrectionKeyDown(ev) {
        if (ev.key === "Enter") {
            ev.preventDefault();
            this.confirmCorrection();
        }
    }

    async confirmCorrection() {
        const newCode = this.correctionScanRef.el ? this.correctionScanRef.el.value.trim() : "";
        const actions = this.correctionActionsRef.el ? this.correctionActionsRef.el.value.trim() : "";
        if (!actions) {
            window.alert("Description des actions obligatoire.");
            return;
        }
        if (!newCode) {
            window.alert("Scanner la nouvelle étiquette.");
            return;
        }
        const boxId = this.state.detailBox.id;
        try {
            const result = await this.orm.call(
                "ar.atelier.session", "correct_box",
                [boxId, newCode, actions]
            );
            if (result.success) {
                this.showMessage(result.message, "success");
                this.closeBoxDetail();
                await this.loadState();
            } else {
                window.alert(result.message);
            }
        } catch (e) {
            window.alert("Erreur : " + (e.message || e));
        }
    }

    enterCancellationMode() {
        this.state.correctionMode = 'cancel';
        setTimeout(() => this.correctionActionsRef.el?.focus(), 100);
    }

    async cancelDuplicateBox() {
        if (!this.state.detailBox) return;
        const actions = this.correctionActionsRef.el?.value.trim() || '';
        if (!actions) {
            window.alert('Description des actions obligatoire.');
            return;
        }
        try {
            const result = await this.orm.call('ar.atelier.session', 'cancel_duplicate_box',
                [[this.sessionId], this.state.detailBox.id, actions]);
            this.showMessage(result.message, result.success ? 'success' : 'error');
            if (result.success) {
                this.closeBoxDetail();
                await this.loadState();
            }
        } catch (e) {
            window.alert('Erreur : ' + (e.message || e));
        }
    }

    async deleteBox() {
        if (!this.state.detailBox) return;
        if (!window.confirm("Supprimer définitivement cette boxe ? Cette action est irréversible.")) return;
        try {
            const result = await this.orm.call(
                "ar.atelier.session", "delete_box", [this.state.detailBox.id]
            );
            if (result.success) {
                this.showMessage(result.message, "success");
                this.closeBoxDetail();
                await this.loadState();
            } else {
                window.alert(result.message);
            }
        } catch (e) {
            window.alert("Erreur : " + (e.message || e));
        }
    }

    // ------------------------------------------------------------ Menu admin

    toggleAdminMenu() { this.state.adminMenuOpen = !this.state.adminMenuOpen; }

    async _openAct(name, model, context = {}) {
        this.state.adminMenuOpen = false;
        await this.actionService.doAction({
            type: "ir.actions.act_window",
            name, res_model: model,
            context,
            view_mode: "list,form",
            views: [[false, "list"], [false, "form"]],
            target: "current",
        });
    }
    async openClosureSettings() {
        this.state.adminMenuOpen = false;
        await this.actionService.doAction({
            type: 'ir.actions.act_window', name: 'Durée de clôture',
            res_model: 'ar.atelier.closure.settings', views: [[false, 'form']], target: 'new',
        });
    }

    async openSessions() { return this._openAct("Sessions", "ar.atelier.session"); }
    async openResetSessions() {
        this.state.adminMenuOpen = false;
        await this.actionService.doAction('ar_atelier.action_atelier_reset', {
            onClose: async () => { await this.loadState(); },
        });
    }
    async openPallets() { return this._openAct("Palettes", "ar.atelier.pallet"); }
    async openBoxes()   { return this._openAct("Boxes", "ar.atelier.box"); }
    async openTriRef()  { return this._openAct("Référentiel TRI", "ar.atelier.tri.ref"); }
    async openUcRef()   { return this._openAct("Référentiel UC", "ar.atelier.uc.ref", { active_test: false }); }
    async openTriList() { return this._openAct("Listes TRI", "ar.atelier.tri.list", { active_test: false }); }
    async openOfList()  { return this._openAct("Listes OF", "ar.atelier.of.list", { active_test: false }); }

    // -------------------------------------------------------------- 2D iso

    _shade(hex, factor) {
        const h = hex.replace("#", "");
        const r = parseInt(h.substring(0, 2), 16);
        const g = parseInt(h.substring(2, 4), 16);
        const b = parseInt(h.substring(4, 6), 16);
        const c = (v) => Math.max(0, Math.min(255, Math.round(v)));
        return `rgb(${c(r * factor)},${c(g * factor)},${c(b * factor)})`;
    }

    getIso(pallet) {
        const cols = pallet.cols || 4, rows = pallet.rows || 2, layers = pallet.layers || 5;
        const boxW = 50, boxH = 36, isoX = 25, isoY = 16;
        const palletThick = 14, padding = 14;
        const totalIsoX = (rows - 1) * isoX, totalIsoY = (rows - 1) * isoY;
        const w = padding * 2 + cols * boxW + totalIsoX + isoX;
        const h = padding * 2 + layers * boxH + totalIsoY + palletThick + 8;
        const baseX = padding;
        const baseY = h - padding - palletThick;

        const palletTopPts = [
            [baseX, baseY],
            [baseX + cols * boxW, baseY],
            [baseX + cols * boxW + totalIsoX, baseY - totalIsoY],
            [baseX + totalIsoX, baseY - totalIsoY],
        ].map((p) => p.join(",")).join(" ");

        const palletPts = [
            [baseX, baseY],
            [baseX + cols * boxW, baseY],
            [baseX + cols * boxW + totalIsoX, baseY - totalIsoY],
            [baseX + cols * boxW + totalIsoX, baseY - totalIsoY + palletThick],
            [baseX + cols * boxW, baseY + palletThick],
            [baseX, baseY + palletThick],
        ].map((p) => p.join(",")).join(" ");

        const byPos = {};
        for (const b of pallet.boxes) byPos[b.position] = b;
        const baseColor = this.colorsHex[pallet.number] || "#888";
        const cells = [];

        for (let layer = 0; layer < layers; layer++) {
            for (let row = rows - 1; row >= 0; row--) {
                for (let col = 0; col < cols; col++) {
                    const pos = layer * (rows * cols) + row * cols + col;
                    const box = byPos[pos];
                    const fx = baseX + col * boxW + row * isoX;
                    const fy = baseY - layer * boxH - row * isoY - boxH;
                    const front = [[fx, fy], [fx + boxW, fy], [fx + boxW, fy + boxH], [fx, fy + boxH]]
                        .map((p) => p.join(",")).join(" ");
                    const top = [[fx, fy], [fx + isoX, fy - isoY], [fx + boxW + isoX, fy - isoY], [fx + boxW, fy]]
                        .map((p) => p.join(",")).join(" ");
                    const right = [[fx + boxW, fy], [fx + boxW + isoX, fy - isoY], [fx + boxW + isoX, fy + boxH - isoY], [fx + boxW, fy + boxH]]
                        .map((p) => p.join(",")).join(" ");
                    const lf = 0.95 + layer * 0.04;
                    cells.push({
                        id: `${pallet.number}-${pos}`, pos, layer, row, col, box,
                        front, top, right,
                        cF: this._shade(baseColor, lf),
                        cT: this._shade(baseColor, lf * 1.18),
                        cR: this._shade(baseColor, lf * 0.78),
                        lx: fx + boxW / 2, ly: fy + boxH / 2 + 3,
                    });
                }
            }
        }
        return { w, h, palletPts, palletTopPts, cells };
    }

    // -------------------------------------------------------------- 3D Three.js

    init3D() {
        if (!window.THREE) return;
        const container = this.threeContainerRef.el;
        if (!container) return;
        const scene = new THREE.Scene();
        scene.background = new THREE.Color(0xeef2f5);
        const w = container.clientWidth || 800;
        const h = container.clientHeight || 500;
        const camera = new THREE.PerspectiveCamera(45, w / h, 0.1, 10000);
        camera.position.set(800, 700, 1100);
        camera.lookAt(0, 100, 0);
        const renderer = new THREE.WebGLRenderer({ antialias: true });
        renderer.setSize(w, h);
        renderer.shadowMap.enabled = true;
        renderer.shadowMap.type = THREE.PCFSoftShadowMap;
        container.appendChild(renderer.domElement);
        scene.add(new THREE.AmbientLight(0xffffff, 0.55));
        const dir = new THREE.DirectionalLight(0xffffff, 0.85);
        dir.position.set(400, 800, 400);
        dir.castShadow = true;
        dir.shadow.mapSize.set(1024, 1024);
        Object.assign(dir.shadow.camera, { left: -1000, right: 1000, top: 1000, bottom: -1000 });
        scene.add(dir);
        const ground = new THREE.Mesh(
            new THREE.PlaneGeometry(4000, 4000),
            new THREE.MeshStandardMaterial({ color: 0xc8cdd2, roughness: 0.95 })
        );
        ground.rotation.x = -Math.PI / 2;
        ground.receiveShadow = true;
        scene.add(ground);
        scene.add(new THREE.GridHelper(3000, 30, 0x999999, 0xcccccc));
        let controls = null;
        if (THREE.OrbitControls) {
            controls = new THREE.OrbitControls(camera, renderer.domElement);
            controls.enableDamping = true; controls.dampingFactor = 0.08;
            controls.target.set(0, 200, 0);
        }
        Object.assign(this.three, { scene, camera, renderer, controls });
        [1, 2, 3].forEach((n, idx) => this.buildPallet3D(n, (idx - 1) * 1000));
        this.refresh3DBoxes();
        const animate = () => {
            this.three.animationId = requestAnimationFrame(animate);
            if (controls) controls.update();
            renderer.render(scene, camera);
        };
        animate();
        this._onResize = () => {
            if (!container || !renderer || !camera) return;
            renderer.setSize(container.clientWidth, container.clientHeight);
            camera.aspect = container.clientWidth / container.clientHeight;
            camera.updateProjectionMatrix();
        };
        window.addEventListener("resize", this._onResize);
    }

    buildPallet3D(number, xCenter) {
        const palletW = 800, palletD = 1200, palletH = 150;
        const mesh = new THREE.Mesh(
            new THREE.BoxGeometry(palletW, palletH, palletD),
            new THREE.MeshStandardMaterial({ color: 0xb87a3d, roughness: 0.85 })
        );
        mesh.position.set(xCenter, palletH / 2, 0);
        mesh.castShadow = true; mesh.receiveShadow = true;
        this.three.scene.add(mesh);
        const label = this.makeLabel(this.palletLabels[number] || ("Palette " + number));
        label.position.set(xCenter, palletH + 30, palletD / 2 + 60);
        label.scale.set(420, 75, 1);
        this.three.scene.add(label);
        const group = new THREE.Group();
        group.position.set(xCenter, palletH, 0);
        this.three.scene.add(group);
        this.three.cubeGroups[number] = group;
    }

    makeLabel(text) {
        const c = document.createElement("canvas");
        c.width = 768; c.height = 128;
        const ctx = c.getContext("2d");
        ctx.fillStyle = "rgba(0,0,0,0.65)"; ctx.fillRect(0, 0, 768, 128);
        ctx.font = "bold 56px Arial"; ctx.fillStyle = "#fff";
        ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.fillText(text, 384, 64);
        const tex = new THREE.CanvasTexture(c);
        return new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true }));
    }

    makeBoxLabel(posNum, hu) {
        const c = document.createElement("canvas");
        c.width = 256; c.height = 96;
        const ctx = c.getContext("2d");
        ctx.fillStyle = "rgba(0,0,0,0.78)";
        ctx.fillRect(0, 0, 256, 96);
        ctx.fillStyle = "#fff";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        // Ligne 1 : #N
        ctx.font = "bold 36px Arial";
        ctx.fillText("#" + posNum, 128, 30);
        // Ligne 2 : HU
        if (hu) {
            ctx.font = "24px Arial";
            ctx.fillText("HU " + hu, 128, 68);
        }
        const tex = new THREE.CanvasTexture(c);
        return new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true, depthTest: false }));
    }

    refresh3DBoxes() {
        if (!this.three.scene) return;
        for (const pallet of this.state.pallets) {
            const group = this.three.cubeGroups[pallet.number];
            if (!group) continue;
            while (group.children.length > 0) {
                const child = group.children[0];
                group.remove(child);
                if (child.geometry) child.geometry.dispose();
                if (child.material) child.material.dispose();
            }
            const palletW = 800, palletD = 1200, boxH = 150;
            const cols = pallet.cols || 4, rows = pallet.rows || 2;
            const boxW = palletW / cols, boxD = palletD / rows;
            const color = this.colorsInt[pallet.number] || 0x888888;
            for (const box of pallet.boxes) {
                const layer = Math.floor(box.position / (rows * cols));
                const inLayer = box.position % (rows * cols);
                const r = Math.floor(inLayer / cols);
                const c = inLayer % cols;
                const geo = new THREE.BoxGeometry(boxW * 0.95, boxH * 0.95, boxD * 0.95);
                const colorObj = new THREE.Color(color).multiplyScalar(1 - layer * 0.06);
                const cube = new THREE.Mesh(
                    geo, new THREE.MeshStandardMaterial({ color: colorObj, roughness: 0.55 })
                );
                const cubeX = -palletW / 2 + boxW / 2 + c * boxW;
                const cubeY = boxH / 2 + layer * boxH;
                const cubeZ = -palletD / 2 + boxD / 2 + r * boxD;
                cube.position.set(cubeX, cubeY, cubeZ);
                cube.castShadow = true; cube.receiveShadow = true;
                group.add(cube);
                const wire = new THREE.LineSegments(
                    new THREE.EdgesGeometry(geo),
                    new THREE.LineBasicMaterial({ color: 0x222222 })
                );
                wire.position.copy(cube.position);
                group.add(wire);

                // Sprite label : numéro de position + HU au-dessus du cube
                const sprite = this.makeBoxLabel(box.position + 1, box.hu);
                sprite.position.set(cubeX, cubeY + boxH / 2 + 30, cubeZ);
                sprite.scale.set(160, 60, 1);
                group.add(sprite);
            }
        }
    }

    dispose3D() {
        if (this.three.animationId) {
            cancelAnimationFrame(this.three.animationId);
            this.three.animationId = null;
        }
        if (this._onResize) {
            window.removeEventListener("resize", this._onResize);
            this._onResize = null;
        }
        if (this.three.controls && this.three.controls.dispose) this.three.controls.dispose();
        if (this.three.renderer) {
            const cv = this.three.renderer.domElement;
            if (cv && cv.parentNode) cv.parentNode.removeChild(cv);
            this.three.renderer.dispose();
        }
        this.three = {
            scene: null, camera: null, renderer: null, controls: null,
            animationId: null, cubeGroups: { 1: null, 2: null, 3: null },
        };
    }
}

registry.category("actions").add("ar_atelier_scan_app", ScanApp);
