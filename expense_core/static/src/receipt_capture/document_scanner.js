/** @odoo-module **/

/**
 * Détection des bords d'un ticket et redressement, avec OpenCV.js.
 *
 * Fonctions pures : l'objet `cv` d'OpenCV.js est passé en paramètre, ce qui permet de les essayer hors du navigateur.
 * Les points sont des objets {x, y}, rangés dans l'ordre haut-gauche, haut-droit, bas-droit, bas-gauche.
 */

const DETECTION_SIDE = 480; // plus grand côté de l'image sur laquelle on cherche les bords
const MIN_AREA_RATIO = 0.1; // le ticket doit couvrir au moins 10 % de l'image
const OUTPUT_MAX_SIDE = 2000; // plus grand côté de l'image redressée

export function orderPoints(points) {
    const bySum = [...points].sort((a, b) => a.x + a.y - (b.x + b.y));
    const byDiff = [...points].sort((a, b) => a.y - a.x - (b.y - b.x));
    return [bySum[0], byDiff[0], bySum[3], byDiff[3]];
}

function distance(a, b) {
    return Math.hypot(a.x - b.x, a.y - b.y);
}

/** Deux détections successives désignent-elles le même quadrilatère (écart toléré en fraction de la diagonale) ? */
export function quadsClose(a, b, tolerance, diagonal) {
    if (!a || !b) {
        return false;
    }
    return a.every((point, index) => distance(point, b[index]) <= tolerance * diagonal);
}

function medianOf(mat) {
    const values = Array.from(mat.data).sort((a, b) => a - b);
    return values[Math.floor(values.length / 2)] || 0;
}

/** Parmi les contours, le plus grand qui ressemble à un quadrilatère convexe (aire minimale `minArea`). */
function bestQuadrilateral(cv, contours, minArea) {
    let best = null;
    let bestArea = 0;
    for (let i = 0; i < contours.size(); i++) {
        const contour = contours.get(i);
        const area = cv.contourArea(contour);
        if (area >= minArea && area > bestArea) {
            let candidate = null;
            const perimeter = cv.arcLength(contour, true);
            // Un ticket froissé donne plus de 4 sommets : on relâche la tolérance, puis on prend le rectangle englobant.
            for (const epsilon of [0.02, 0.04, 0.06]) {
                const approx = new cv.Mat();
                cv.approxPolyDP(contour, approx, epsilon * perimeter, true);
                if (approx.rows === 4 && cv.isContourConvex(approx)) {
                    candidate = [];
                    for (let k = 0; k < 4; k++) {
                        candidate.push({ x: approx.data32S[k * 2], y: approx.data32S[k * 2 + 1] });
                    }
                }
                approx.delete();
                if (candidate) {
                    break;
                }
            }
            if (!candidate) {
                const rect = cv.minAreaRect(contour);
                const hull = new cv.Mat();
                cv.convexHull(contour, hull);
                const hullArea = cv.contourArea(hull);
                hull.delete();
                if (hullArea > 0 && area / hullArea > 0.85 && rect.size.width * rect.size.height > 0) {
                    candidate = cv.RotatedRect.points(rect).map((p) => ({ x: p.x, y: p.y }));
                }
            }
            if (candidate) {
                best = candidate;
                bestArea = area;
            }
        }
        contour.delete();
    }
    return best;
}

/**
 * Cherche le plus grand quadrilatère convexe de l'image (une matrice cv.Mat RGBA).
 * Retourne les 4 coins dans les coordonnées de l'image d'origine, ou null.
 */
export function findDocumentQuad(cv, src) {
    const scale = Math.min(1, DETECTION_SIDE / Math.max(src.cols, src.rows));
    const toDelete = [];
    const track = (mat) => {
        toDelete.push(mat);
        return mat;
    };
    try {
        const small = track(new cv.Mat());
        cv.resize(src, small, new cv.Size(Math.round(src.cols * scale), Math.round(src.rows * scale)), 0, 0, cv.INTER_AREA);
        const gray = track(new cv.Mat());
        cv.cvtColor(small, gray, cv.COLOR_RGBA2GRAY);
        cv.GaussianBlur(gray, gray, new cv.Size(5, 5), 0);
        // Seuils de Canny : d'abord tirés de la luminosité médiane (fond sombre ou contrasté), puis plus bas pour un
        // ticket blanc sur un fond clair, où les contours sont faibles.
        const median = medianOf(gray);
        const thresholds = [
            [Math.max(10, 0.66 * median), Math.min(255, 1.33 * median + 20)],
            [30, 90],
            [12, 36],
        ];
        const kernel = track(cv.getStructuringElement(cv.MORPH_RECT, new cv.Size(3, 3)));
        const minArea = MIN_AREA_RATIO * small.cols * small.rows;
        let best = null;
        for (const [low, high] of thresholds) {
            const edges = new cv.Mat();
            cv.Canny(gray, edges, low, high);
            cv.dilate(edges, edges, kernel, new cv.Point(-1, -1), 2);
            const contours = new cv.MatVector();
            const hierarchy = new cv.Mat();
            cv.findContours(edges, contours, hierarchy, cv.RETR_EXTERNAL, cv.CHAIN_APPROX_SIMPLE);
            best = bestQuadrilateral(cv, contours, minArea);
            contours.delete();
            hierarchy.delete();
            edges.delete();
            if (best) {
                break;
            }
        }
        if (!best) {
            return null;
        }
        return orderPoints(best).map((p) => ({ x: p.x / scale, y: p.y / scale }));
    } finally {
        toDelete.forEach((mat) => mat.delete());
    }
}

/** Redresse le quadrilatère `quad` de la matrice `src` ; retourne une nouvelle cv.Mat (à libérer par l'appelant). */
export function warpDocument(cv, src, quad) {
    const [tl, tr, br, bl] = quad;
    let width = Math.max(distance(tl, tr), distance(bl, br));
    let height = Math.max(distance(tl, bl), distance(tr, br));
    const ratio = Math.min(1, OUTPUT_MAX_SIDE / Math.max(width, height));
    width = Math.max(1, Math.round(width * ratio));
    height = Math.max(1, Math.round(height * ratio));
    const from = cv.matFromArray(4, 1, cv.CV_32FC2, [tl.x, tl.y, tr.x, tr.y, br.x, br.y, bl.x, bl.y]);
    const to = cv.matFromArray(4, 1, cv.CV_32FC2, [0, 0, width, 0, width, height, 0, height]);
    const matrix = cv.getPerspectiveTransform(from, to);
    const dst = new cv.Mat();
    try {
        cv.warpPerspective(src, dst, matrix, new cv.Size(width, height), cv.INTER_LINEAR, cv.BORDER_REPLICATE);
        return dst;
    } finally {
        from.delete();
        to.delete();
        matrix.delete();
    }
}

/**
 * Étire le contraste d'une image (ImageData) entre ses percentiles 1 % et 99 % de luminosité : un ticket pris à
 * l'ombre ou en plein soleil retrouve un fond clair et un texte foncé, ce qui aide la lecture.
 */
export function stretchContrast(imageData) {
    const data = imageData.data;
    const histogram = new Array(256).fill(0);
    const total = data.length / 4;
    for (let i = 0; i < data.length; i += 4) {
        histogram[Math.round(0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2])]++;
    }
    let low = 0;
    let high = 255;
    let acc = 0;
    for (let v = 0; v < 256; v++) {
        acc += histogram[v];
        if (acc >= total * 0.01) {
            low = v;
            break;
        }
    }
    acc = 0;
    for (let v = 255; v >= 0; v--) {
        acc += histogram[v];
        if (acc >= total * 0.01) {
            high = v;
            break;
        }
    }
    if (high - low < 30) {
        return imageData;
    }
    const factor = 255 / (high - low);
    for (let i = 0; i < data.length; i += 4) {
        data[i] = Math.max(0, Math.min(255, (data[i] - low) * factor));
        data[i + 1] = Math.max(0, Math.min(255, (data[i + 1] - low) * factor));
        data[i + 2] = Math.max(0, Math.min(255, (data[i + 2] - low) * factor));
    }
    return imageData;
}
