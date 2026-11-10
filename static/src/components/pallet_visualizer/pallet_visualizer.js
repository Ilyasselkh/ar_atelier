/** @odoo-module **/

import { Component, useState, useRef, onMounted, onWillUnmount, onWillStart, useEffect } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

/**
 * Visualisation 2D (isométrique SVG) et 3D (Three.js) des 3 palettes de scan.
 *
 *  - Mode 2D : SVG isométrique, vue avant droite, on voit les boxes empilées
 *              comme un dessin technique. Pas de %, juste un compteur "X/Y boxes".
 *              Alerte rouge "PALETTE PLEINE" quand capacité atteinte.
 *  - Mode 3D : Three.js, scène 3D avec OrbitControls.
 *
 *  L'input scan est en haut, focus auto, capture Entrée.
 */
export class PalletVisualizer extends Component {
    static template = "ar_atelier.PalletVisualizer";
    static props = { ...standardFieldProps };

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");

        this.scanInputRef = useRef("scanInput");
        this.threeContainerRef = useRef("threeContainer");

        // Couleurs principales par palette (hex)
        this.palletColorsHex = {
            1: "#1976d2",
            2: "#388e3c",
            3: "#f57c00",
        };
        this.palletColorsInt = {
            1: 0x1976d2,
            2: 0x388e3c,
            3: 0xf57c00,
        };

        this.state = useState({
            mode: "2d",
            pallets: [],
            message: "",
            messageType: "info",
            scanning: false,
        });

        this.three = {
            scene: null, camera: null, renderer: null, controls: null,
            animationId: null, cubeGroups: { 1: null, 2: null, 3: null },
        };

        onWillStart(async () => { await this.loadPalletState(); });
        onMounted(() => { this.focusScan(); });

        useEffect(
            () => {
                if (this.state.mode === "3d") this.init3D();
                else this.dispose3D();
                return () => {};
            },
            () => [this.state.mode]
        );

        onWillUnmount(() => { this.dispose3D(); });
    }

    get sessionId() {
        return this.props.record.resId;
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

    focusScan() {
        if (this.scanInputRef.el) this.scanInputRef.el.focus();
    }

    async loadPalletState() {
        if (!this.sessionId) {
            this.state.pallets = [
                { number: 1, capacity: 40, rows: 2, cols: 4, layers: 5, boxes: [] },
                { number: 2, capacity: 40, rows: 2, cols: 4, layers: 5, boxes: [] },
                { number: 3, capacity: 40, rows: 2, cols: 4, layers: 5, boxes: [] },
            ];
            return;
        }
        try {
            const result = await this.orm.call(
                "ar.atelier.session", "get_pallet_state", [[this.sessionId]]
            );
            this.state.pallets = result.pallets || [];
            if (this.state.mode === "3d") this.refresh3DBoxes();
        } catch (e) {
            console.error("Erreur chargement palettes", e);
        }
    }

    setMode(mode) { this.state.mode = mode; }

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
        if (!this.sessionId) {
            this.showMessage("Sauvegarde la session avant de scanner.", "error");
            return;
        }
        this.state.scanning = true;
        try {
            const result = await this.orm.call(
                "ar.atelier.session", "action_scan", [[this.sessionId], rawCode]
            );
            if (result.success) {
                this.showMessage(result.message, "success");
                await this.loadPalletState();
                if (this.props.record && this.props.record.load) {
                    await this.props.record.load();
                }
            } else {
                this.showMessage(result.message, "error");
            }
        } catch (e) {
            this.showMessage("Erreur : " + (e.message || e), "error");
        } finally {
            this.state.scanning = false;
            this.focusScan();
        }
    }

    showMessage(msg, type = "info") {
        this.state.message = msg;
        this.state.messageType = type;
        clearTimeout(this._msgTimeout);
        this._msgTimeout = setTimeout(() => { this.state.message = ""; }, 4000);
    }

    // -------------------------------------------------------------------------
    // 2D — projection isométrique en SVG
    // -------------------------------------------------------------------------

    getCurrentLayer(pallet) {
        const perLayer = pallet.rows * pallet.cols;
        const lastBox = pallet.boxes[pallet.boxes.length - 1];
        if (!lastBox) return 0;
        return Math.min(Math.floor(lastBox.position / perLayer), pallet.layers - 1);
    }

    /**
     * Convertit hex "#RRGGBB" en {r,g,b} et applique un facteur de luminosité.
     */
    _shadeHex(hex, factor) {
        const h = hex.replace("#", "");
        const r = parseInt(h.substring(0, 2), 16);
        const g = parseInt(h.substring(2, 4), 16);
        const b = parseInt(h.substring(4, 6), 16);
        const clamp = (v) => Math.max(0, Math.min(255, Math.round(v)));
        return `rgb(${clamp(r * factor)},${clamp(g * factor)},${clamp(b * factor)})`;
    }

    /**
     * Calcule toute la projection isométrique pour une palette :
     * - dimensions du SVG
     * - polygones du sol palette
     * - liste des cellules (vides ou avec box) avec leurs 3 polygones de faces
     */
    getIsoSvg(pallet) {
        const cols = pallet.cols || 4;
        const rows = pallet.rows || 2;
        const layers = pallet.layers || 5;

        // Tailles en pixels (vue iso)
        const boxW = 44;   // largeur face avant
        const boxH = 32;   // hauteur face avant
        const isoX = 22;   // décalage horizontal par rangée (profondeur)
        const isoY = 14;   // décalage vertical par rangée (profondeur)

        const palletThickness = 12;
        const padding = 12;

        // Dimensions SVG totales
        const totalIsoX = (rows - 1) * isoX;
        const totalIsoY = (rows - 1) * isoY;
        const w = padding * 2 + cols * boxW + totalIsoX + isoX; // +isoX pour la face droite des dernières
        const h = padding * 2 + layers * boxH + totalIsoY + palletThickness + 8;

        // Origine : coin avant gauche du sol palette (au niveau du dessus de la palette)
        const baseX = padding;
        const baseY = h - padding - palletThickness;

        // Polygones du sol palette (parallélogramme bois)
        // Dessus :
        const palletTopPoints = [
            [baseX, baseY],
            [baseX + cols * boxW, baseY],
            [baseX + cols * boxW + totalIsoX, baseY - totalIsoY],
            [baseX + totalIsoX, baseY - totalIsoY],
        ].map((p) => p.join(",")).join(" ");

        // Face avant + face droite (en un seul polygone L-shape pour simplifier on en fait 1)
        const palletPoints = [
            [baseX, baseY],
            [baseX + cols * boxW, baseY],
            [baseX + cols * boxW + totalIsoX, baseY - totalIsoY],
            [baseX + cols * boxW + totalIsoX, baseY - totalIsoY + palletThickness],
            [baseX + cols * boxW, baseY + palletThickness],
            [baseX, baseY + palletThickness],
        ].map((p) => p.join(",")).join(" ");

        // Map position -> box
        const boxesByPos = {};
        for (const b of pallet.boxes) boxesByPos[b.position] = b;

        const baseColor = this.palletColorsHex[pallet.number] || "#888";

        // Cellules : on parcourt dans l'ordre d'occlusion (du fond/bas vers l'avant/haut)
        // layer croissant (bas -> haut), row décroissant (fond -> avant), col croissant
        const cells = [];
        for (let layer = 0; layer < layers; layer++) {
            for (let row = rows - 1; row >= 0; row--) {
                for (let col = 0; col < cols; col++) {
                    const pos = layer * (rows * cols) + row * cols + col;
                    const box = boxesByPos[pos];

                    // Coin bas-gauche de la face avant de la box
                    const x = baseX + col * boxW + row * isoX;
                    const y = baseY - layer * boxH - row * isoY;
                    // x, y = coin haut-gauche de la face avant
                    const fx = x;
                    const fy = y - boxH;

                    // Face avant : rect
                    const frontPoints = [
                        [fx, fy], [fx + boxW, fy],
                        [fx + boxW, fy + boxH], [fx, fy + boxH],
                    ].map((p) => p.join(",")).join(" ");

                    // Face dessus : parallélogramme
                    const topPoints = [
                        [fx, fy],
                        [fx + isoX, fy - isoY],
                        [fx + boxW + isoX, fy - isoY],
                        [fx + boxW, fy],
                    ].map((p) => p.join(",")).join(" ");

                    // Face droite : parallélogramme
                    const rightPoints = [
                        [fx + boxW, fy],
                        [fx + boxW + isoX, fy - isoY],
                        [fx + boxW + isoX, fy + boxH - isoY],
                        [fx + boxW, fy + boxH],
                    ].map((p) => p.join(",")).join(" ");

                    // Couleurs : variation par couche (plus haut = un peu plus clair)
                    const layerFactor = 0.95 + layer * 0.04;
                    const colorFront = this._shadeHex(baseColor, layerFactor);
                    const colorTop = this._shadeHex(baseColor, layerFactor * 1.18);
                    const colorRight = this._shadeHex(baseColor, layerFactor * 0.78);

                    cells.push({
                        id: `${pallet.number}-${pos}`,
                        pos, layer, row, col, box,
                        frontPoints, topPoints, rightPoints,
                        colorFront, colorTop, colorRight,
                        labelX: fx + boxW / 2,
                        labelY: fy + boxH / 2 + 3,
                    });
                }
            }
        }

        return { w, h, palletPoints, palletTopPoints, cells };
    }

    // -------------------------------------------------------------------------
    // 3D — Three.js
    // -------------------------------------------------------------------------

    init3D() {
        if (!window.THREE) { console.error("Three.js non chargé"); return; }
        const container = this.threeContainerRef.el;
        if (!container) return;

        const scene = new THREE.Scene();
        scene.background = new THREE.Color(0xeef2f5);

        const w = container.clientWidth || 800;
        const h = container.clientHeight || 500;
        const camera = new THREE.PerspectiveCamera(45, w / h, 0.1, 10000);
        camera.position.set(800, 700, 1000);
        camera.lookAt(0, 100, 0);

        const renderer = new THREE.WebGLRenderer({ antialias: true });
        renderer.setSize(w, h);
        renderer.shadowMap.enabled = true;
        renderer.shadowMap.type = THREE.PCFSoftShadowMap;
        container.appendChild(renderer.domElement);

        scene.add(new THREE.AmbientLight(0xffffff, 0.55));
        const dirLight = new THREE.DirectionalLight(0xffffff, 0.85);
        dirLight.position.set(400, 800, 400);
        dirLight.castShadow = true;
        dirLight.shadow.mapSize.set(1024, 1024);
        Object.assign(dirLight.shadow.camera, {
            left: -1000, right: 1000, top: 1000, bottom: -1000,
        });
        scene.add(dirLight);

        // Sol
        const ground = new THREE.Mesh(
            new THREE.PlaneGeometry(4000, 4000),
            new THREE.MeshStandardMaterial({ color: 0xc8cdd2, roughness: 0.95 })
        );
        ground.rotation.x = -Math.PI / 2;
        ground.receiveShadow = true;
        scene.add(ground);

        const grid = new THREE.GridHelper(3000, 30, 0x999999, 0xcccccc);
        grid.position.y = 0.5;
        scene.add(grid);

        let controls = null;
        if (window.THREE && window.THREE.OrbitControls) {
            controls = new THREE.OrbitControls(camera, renderer.domElement);
            controls.enableDamping = true;
            controls.dampingFactor = 0.08;
            controls.target.set(0, 200, 0);
        }

        Object.assign(this.three, { scene, camera, renderer, controls });

        this.buildPallet3D(1, -1000);
        this.buildPallet3D(2, 0);
        this.buildPallet3D(3, 1000);
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
        const scene = this.three.scene;
        if (!scene) return;
        const palletW = 800, palletD = 1200, palletH = 150;
        const palletMesh = new THREE.Mesh(
            new THREE.BoxGeometry(palletW, palletH, palletD),
            new THREE.MeshStandardMaterial({ color: 0xb87a3d, roughness: 0.85 })
        );
        palletMesh.position.set(xCenter, palletH / 2, 0);
        palletMesh.castShadow = true;
        palletMesh.receiveShadow = true;
        scene.add(palletMesh);

        const label = this.makeLabelSprite("Palette " + number);
        label.position.set(xCenter, palletH + 30, palletD / 2 + 60);
        label.scale.set(300, 75, 1);
        scene.add(label);

        const group = new THREE.Group();
        group.position.set(xCenter, palletH, 0);
        scene.add(group);
        this.three.cubeGroups[number] = group;
    }

    makeLabelSprite(text) {
        const canvas = document.createElement("canvas");
        canvas.width = 512; canvas.height = 128;
        const ctx = canvas.getContext("2d");
        ctx.fillStyle = "rgba(0,0,0,0.65)";
        ctx.fillRect(0, 0, 512, 128);
        ctx.font = "bold 64px Arial, sans-serif";
        ctx.fillStyle = "#fff";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(text, 256, 64);
        const tex = new THREE.CanvasTexture(canvas);
        return new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true }));
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
            const color = this.palletColorsInt[pallet.number] || 0x888888;

            for (const box of pallet.boxes) {
                const layer = Math.floor(box.position / (rows * cols));
                const inLayer = box.position % (rows * cols);
                const r = Math.floor(inLayer / cols);
                const c = inLayer % cols;
                const geo = new THREE.BoxGeometry(boxW * 0.95, boxH * 0.95, boxD * 0.95);
                const colorObj = new THREE.Color(color).multiplyScalar(1 - layer * 0.06);
                const mat = new THREE.MeshStandardMaterial({
                    color: colorObj, roughness: 0.55, metalness: 0.05,
                });
                const cube = new THREE.Mesh(geo, mat);
                cube.position.set(
                    -palletW / 2 + boxW / 2 + c * boxW,
                    boxH / 2 + layer * boxH,
                    -palletD / 2 + boxD / 2 + r * boxD
                );
                cube.castShadow = true;
                cube.receiveShadow = true;
                group.add(cube);
                const wire = new THREE.LineSegments(
                    new THREE.EdgesGeometry(geo),
                    new THREE.LineBasicMaterial({ color: 0x222222 })
                );
                wire.position.copy(cube.position);
                group.add(wire);
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
            const canvas = this.three.renderer.domElement;
            if (canvas && canvas.parentNode) canvas.parentNode.removeChild(canvas);
            this.three.renderer.dispose();
        }
        this.three = {
            scene: null, camera: null, renderer: null, controls: null,
            animationId: null, cubeGroups: { 1: null, 2: null, 3: null },
        };
    }
}

export const palletVisualizerField = {
    component: PalletVisualizer,
    supportedTypes: ["integer", "many2one"],
};

registry.category("fields").add("ar_pallet_visualizer", palletVisualizerField);
