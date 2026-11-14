/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useRef, useState } from "@odoo/owl";
import { loadJS } from "@web/core/assets";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { findDocumentQuad, quadsClose, stretchContrast, warpDocument } from "./document_scanner";

const OPENCV_URL = "/expense_core/static/lib/opencv/opencv.js";
const DETECTION_INTERVAL = 200; // ms entre deux détections sur l'image de la caméra
const STABLE_FRAMES = 6; // détections identiques d'affilée avant le déclenchement automatique (environ 1,2 s)
const STABLE_TOLERANCE = 0.02; // écart toléré des coins, en fraction de la diagonale
const JPEG_QUALITY = 0.88;

let opencvPromise = null;

/** Charge OpenCV.js une seule fois ; l'objet `cv` se comporte comme une promesse : il n'est jamais attendu directement. */
function loadOpenCV() {
    if (!opencvPromise) {
        opencvPromise = loadJS(OPENCV_URL).then(
            () =>
                new Promise((resolve) => {
                    const ready = () => resolve({ cv: window.cv });
                    if (window.cv && window.cv.Mat) {
                        return ready();
                    }
                    window.cv.onRuntimeInitialized = ready;
                })
        );
        opencvPromise.catch(() => {
            opencvPromise = null;
        });
    }
    return opencvPromise;
}

/**
 * Écran « Photographier un ticket » : caméra arrière, cadre qui suit les bords du ticket, déclenchement automatique
 * quand il est stable, redressement et recadrage, puis création du reçu en brouillon (voir expense_create_from_photo).
 */
export class ReceiptCapture extends Component {
    static template = "expense_core.ReceiptCapture";
    static props = { "*": true };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.videoRef = useRef("video");
        this.overlayRef = useRef("overlay");
        this.fileRef = useRef("file");
        this.state = useState({
            step: "starting", // starting, live, preview, saving, error
            hint: _t("Démarrage de la caméra…"),
            detected: false,
            scannerReady: false,
            error: "",
            previewUrl: "",
        });
        this.cv = null;
        this.stream = null;
        this.timer = null;
        this.lastQuad = null;
        this.stableCount = 0;
        this.resultCanvas = null;

        onMounted(() => this.start());
        onWillUnmount(() => this.stop());
    }

    // ------------------------------------------------------------------
    // Caméra
    // ------------------------------------------------------------------
    async start() {
        this.state.step = "starting";
        this.state.previewUrl = "";
        this.lastQuad = null;
        this.stableCount = 0;
        loadOpenCV().then(
            ({ cv }) => {
                this.cv = cv;
                this.state.scannerReady = true;
            },
            () => {
                // Sans OpenCV, la caméra reste utilisable : le cliché est pris tel quel.
                this.state.scannerReady = false;
            }
        );
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            return this.fail(
                _t("La caméra n'est pas disponible sur cette page (il faut une adresse en HTTPS). Importez une photo à la place.")
            );
        }
        try {
            this.stream = await navigator.mediaDevices.getUserMedia({
                video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 }, height: { ideal: 1080 } },
                audio: false,
            });
        } catch (error) {
            return this.fail(
                _t("L'accès à la caméra a été refusé ou n'est pas possible. Autorisez-le dans le navigateur, ou importez une photo.")
            );
        }
        const video = this.videoRef.el;
        if (!video) {
            return this.stop();
        }
        video.srcObject = this.stream;
        try {
            await video.play();
        } catch (error) {
            return this.fail(_t("La caméra ne démarre pas. Importez une photo à la place."));
        }
        this.state.step = "live";
        this.state.hint = _t("Cadrez le ticket en entier");
        this.scheduleDetection();
    }

    stop() {
        clearTimeout(this.timer);
        this.timer = null;
        if (this.stream) {
            this.stream.getTracks().forEach((track) => track.stop());
            this.stream = null;
        }
    }

    fail(message) {
        this.stop();
        this.state.step = "error";
        this.state.error = message;
    }

    scheduleDetection() {
        clearTimeout(this.timer);
        this.timer = setTimeout(() => this.detect(), DETECTION_INTERVAL);
    }

    // ------------------------------------------------------------------
    // Détection en direct
    // ------------------------------------------------------------------
    detect() {
        const video = this.videoRef.el;
        if (this.state.step !== "live" || !video || !video.videoWidth) {
            return this.state.step === "live" ? this.scheduleDetection() : undefined;
        }
        const overlay = this.overlayRef.el;
        overlay.width = video.videoWidth;
        overlay.height = video.videoHeight;
        let quad = null;
        if (this.cv) {
            const frame = document.createElement("canvas");
            const scale = Math.min(1, 640 / video.videoWidth);
            frame.width = Math.round(video.videoWidth * scale);
            frame.height = Math.round(video.videoHeight * scale);
            frame.getContext("2d").drawImage(video, 0, 0, frame.width, frame.height);
            const mat = this.cv.imread(frame);
            try {
                quad = findDocumentQuad(this.cv, mat);
            } catch (error) {
                quad = null;
            } finally {
                mat.delete();
            }
            if (quad) {
                quad = quad.map((p) => ({ x: p.x / scale, y: p.y / scale }));
            }
        }
        this.drawQuad(quad, overlay);
        const diagonal = Math.hypot(video.videoWidth, video.videoHeight);
        if (quad && quadsClose(quad, this.lastQuad, STABLE_TOLERANCE, diagonal)) {
            this.stableCount++;
        } else {
            this.stableCount = 0;
        }
        this.lastQuad = quad;
        this.state.detected = Boolean(quad);
        if (!this.cv) {
            this.state.hint = this.state.scannerReady ? "" : _t("Cadrez le ticket puis appuyez sur le bouton");
        } else if (!quad) {
            this.state.hint = _t("Cadrez le ticket en entier, sur un fond uni");
        } else if (this.stableCount >= STABLE_FRAMES) {
            return this.capture();
        } else {
            this.state.hint = _t("Ne bougez plus…");
        }
        this.scheduleDetection();
    }

    drawQuad(quad, overlay) {
        const ctx = overlay.getContext("2d");
        ctx.clearRect(0, 0, overlay.width, overlay.height);
        if (!quad) {
            return;
        }
        const ready = this.stableCount >= 2;
        ctx.lineWidth = Math.max(4, overlay.width / 160);
        ctx.strokeStyle = ready ? "#28a745" : "#ffc107";
        ctx.fillStyle = ready ? "rgba(40, 167, 69, 0.18)" : "rgba(255, 193, 7, 0.15)";
        ctx.beginPath();
        quad.forEach((p, index) => (index ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
        ctx.closePath();
        ctx.fill();
        ctx.stroke();
    }

    // ------------------------------------------------------------------
    // Cliché, redressement, aperçu
    // ------------------------------------------------------------------
    capture() {
        const video = this.videoRef.el;
        const shot = document.createElement("canvas");
        shot.width = video.videoWidth;
        shot.height = video.videoHeight;
        shot.getContext("2d").drawImage(video, 0, 0);
        this.stop();
        this.showResult(shot, this.lastQuad);
    }

    /** Redresse `source` (un canvas) selon `quad` s'il y en a un, étire le contraste et affiche l'aperçu. */
    showResult(source, quad) {
        let output = source;
        if (this.cv) {
            let quadToUse = quad;
            if (!quadToUse) {
                try {
                    const mat = this.cv.imread(source);
                    try {
                        quadToUse = findDocumentQuad(this.cv, mat);
                    } finally {
                        mat.delete();
                    }
                } catch (error) {
                    quadToUse = null;
                }
            }
            if (quadToUse) {
                const mat = this.cv.imread(source);
                try {
                    const warped = warpDocument(this.cv, mat, quadToUse);
                    output = document.createElement("canvas");
                    this.cv.imshow(output, warped);
                    warped.delete();
                } finally {
                    mat.delete();
                }
            }
        }
        const ctx = output.getContext("2d");
        ctx.putImageData(stretchContrast(ctx.getImageData(0, 0, output.width, output.height)), 0, 0);
        this.resultCanvas = output;
        this.state.previewUrl = output.toDataURL("image/jpeg", JPEG_QUALITY);
        this.state.step = "preview";
    }

    async onFileChosen(ev) {
        const file = ev.target.files && ev.target.files[0];
        ev.target.value = "";
        if (!file) {
            return;
        }
        try {
            const bitmap = await createImageBitmap(file);
            const scale = Math.min(1, 2400 / Math.max(bitmap.width, bitmap.height));
            const canvas = document.createElement("canvas");
            canvas.width = Math.round(bitmap.width * scale);
            canvas.height = Math.round(bitmap.height * scale);
            canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
            this.stop();
            this.showResult(canvas, null);
        } catch (error) {
            this.notification.add(_t("Cette image ne peut pas être lue."), { type: "danger" });
        }
    }

    onShootClick() {
        if (this.state.step === "live") {
            this.capture();
        }
    }

    onImportClick() {
        this.fileRef.el.click();
    }

    onRetake() {
        this.start();
    }

    // ------------------------------------------------------------------
    // Enregistrement
    // ------------------------------------------------------------------
    async onValidate() {
        if (this.state.step !== "preview") {
            return;
        }
        this.state.step = "saving";
        const image = this.state.previewUrl.split(",")[1];
        try {
            const action = await this.orm.call("account.move", "expense_create_from_photo", [image]);
            await this.action.doAction(action);
        } catch (error) {
            this.state.step = "preview";
            throw error;
        }
    }
}

registry.category("actions").add("expense_core.receipt_capture", ReceiptCapture);
