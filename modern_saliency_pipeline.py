from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np


EPS = 1e-8


@dataclass
class SampleResult:
    category: str
    stem: str
    mae: float
    precision: float
    recall: float
    f1: float
    iou: float
    dice: float


def normalize01(arr: np.ndarray) -> np.ndarray:
    arr = arr.astype(np.float32, copy=False)
    min_val = float(arr.min())
    max_val = float(arr.max())
    if max_val - min_val < EPS:
        return np.zeros_like(arr, dtype=np.float32)
    return (arr - min_val) / (max_val - min_val)


def adaptive_darker_prior(gray: np.ndarray) -> np.ndarray:
    darker = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_MEAN_C,
        cv2.THRESH_BINARY_INV,
        35,
        15,
    )
    return darker.astype(np.float32) / 255.0


def structure_tensor_saliency(gray: np.ndarray, downsample: int = 4) -> np.ndarray:
    small = cv2.resize(
        gray,
        (max(1, gray.shape[1] // downsample), max(1, gray.shape[0] // downsample)),
        interpolation=cv2.INTER_AREA,
    )
    small = small.astype(np.float32)
    ix = cv2.Sobel(small, cv2.CV_32F, 1, 0, ksize=3, scale=1, delta=1, borderType=cv2.BORDER_DEFAULT)
    iy = cv2.Sobel(small, cv2.CV_32F, 0, 1, ksize=3, scale=1, delta=1, borderType=cv2.BORDER_DEFAULT)

    ix2 = cv2.GaussianBlur(ix * ix, (5, 5), sigmaX=-1)
    iy2 = cv2.GaussianBlur(iy * iy, (5, 5), sigmaX=0)
    ixy = cv2.GaussianBlur(ix * iy, (5, 5), sigmaX=0)

    a = (ix2 - iy2) ** 2 + 4.0 * (ixy ** 2)
    b = ix2 + iy2
    tensor = 0.5 * normalize01(a) + 0.5 * normalize01(b)
    return cv2.resize(tensor, (gray.shape[1], gray.shape[0]), interpolation=cv2.INTER_LINEAR)


def phot_saliency(image_bgr: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY).astype(np.float64)
    dft = cv2.dft(gray, flags=cv2.DFT_COMPLEX_OUTPUT)
    real = dft[:, :, 0]
    imag = dft[:, :, 1]
    magnitude = cv2.magnitude(real, imag)
    magnitude[magnitude < EPS] = 1.0
    unit = np.dstack((real / magnitude, imag / magnitude))
    inverse = cv2.idft(unit, flags=cv2.DFT_SCALE | cv2.DFT_COMPLEX_OUTPUT)
    response = cv2.GaussianBlur(inverse[:, :, 0], (7, 7), sigmaX=-1)
    centered = (response - float(response.mean())) ** 2
    return normalize01(centered)


def ac_saliency(image_bgr: np.ndarray, scales: int = 3) -> np.ndarray:
    img3f = image_bgr.astype(np.float32) / 255.0
    mean_r1 = cv2.GaussianBlur(img3f, (3, 3), sigmaX=-1)
    mean_r1 = cv2.cvtColor(mean_r1, cv2.COLOR_BGR2LAB)

    height, width = mean_r1.shape[:2]
    min_r2 = max(1, min(width, height) // 8)
    max_r2 = max(min_r2, min(width, height) // 2)
    sal = np.zeros((height, width), dtype=np.float32)

    for z in range(scales):
        if scales == 1:
            radius = min_r2
        else:
            radius = int((max_r2 - min_r2) * z / (scales - 1) + min_r2)
        if radius % 2 == 0:
            radius += 1
        mean_r2 = cv2.blur(mean_r1, (radius, radius))
        diff = mean_r2 - mean_r1
        sal += np.sqrt(np.sum(diff * diff, axis=2))
    return normalize01(sal)


def whiten_feature_maps(image_3c: np.ndarray, reg: float = 50.0) -> list[np.ndarray]:
    samples = image_3c.reshape(-1, 3).astype(np.float32)
    cov = np.cov(samples, rowvar=False, bias=True).astype(np.float32)
    cov += np.eye(3, dtype=np.float32) * reg
    _, singular_values, vt = np.linalg.svd(cov, full_matrices=False)
    transform = vt.T @ np.diag(1.0 / np.sqrt(singular_values + EPS))
    whitened = (samples @ transform).reshape(image_3c.shape)

    feature_maps: list[np.ndarray] = []
    for channel in cv2.split(whitened):
        normalized = (normalize01(channel) * 255.0).astype(np.uint8)
        feature_maps.append(cv2.medianBlur(normalized, 3))
    return feature_maps


def flood_fill_from_border(binary_map: np.ndarray) -> np.ndarray:
    ret = binary_map.copy()
    h, w = ret.shape
    mask = np.zeros((h + 2, w + 2), dtype=np.uint8)

    def fill_if_needed(x: int, y: int) -> None:
        if ret[y, x] != 1:
            cv2.floodFill(ret, mask, (x, y), 1)

    for y in range(h):
        fill_if_needed(0, y)
        fill_if_needed(w - 1, y)
    for x in range(w):
        fill_if_needed(x, 0)
        fill_if_needed(x, h - 1)
    return ret


def l2_normalize(mat: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(mat))
    if norm < EPS:
        return np.zeros_like(mat, dtype=np.float32)
    return mat.astype(np.float32) / norm


def bms_saliency(image_bgr: np.ndarray, sample_step: int = 3, dilation_width: int = 3) -> np.ndarray:
    lab = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2LAB)
    feature_maps = whiten_feature_maps(lab)
    saliency = np.zeros(image_bgr.shape[:2], dtype=np.float32)

    for feature_map in feature_maps:
        min_val = int(feature_map.min())
        max_val = int(feature_map.max())
        for thresh in range(min_val, max_val, sample_step):
            bm = np.where(feature_map > thresh, 255, 0).astype(np.uint8)
            ret = flood_fill_from_border(bm)
            ret = np.where(ret != 1, 255, 0).astype(np.uint8)
            map1 = cv2.bitwise_and(ret, bm)
            map2 = cv2.bitwise_and(ret, cv2.bitwise_not(bm))
            if dilation_width > 0:
                map1 = cv2.dilate(map1, None, iterations=dilation_width)
                map2 = cv2.dilate(map2, None, iterations=dilation_width)
            saliency += l2_normalize(map1) + l2_normalize(map2)
    return normalize01(saliency)


def mcue_saliency(image_bgr: np.ndarray) -> dict[str, np.ndarray]:
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    darker = adaptive_darker_prior(gray)
    tensor = structure_tensor_saliency(gray)
    phot = phot_saliency(image_bgr)
    ac = ac_saliency(image_bgr)
    bms = bms_saliency(image_bgr)

    darker_weight = darker * 3.0 + 1.0
    mcue = (phot * 3.0 + ac + tensor + bms * 3.0) * darker_weight / 16.0
    mcue2 = bms * (darker_weight * (phot * 3.0 + ac + tensor)) / 4.0
    return {
        "darker": normalize01(darker),
        "tensor": normalize01(tensor),
        "phot": normalize01(phot),
        "ac": normalize01(ac),
        "bms": normalize01(bms),
        "mcue": normalize01(mcue),
        "mcue2": normalize01(mcue2),
    }


def read_mask(mask_path: Path) -> np.ndarray:
    mask = cv2.imread(str(mask_path), cv2.IMREAD_GRAYSCALE)
    if mask is None:
        raise FileNotFoundError(f"Mask not found: {mask_path}")
    return (mask > 0).astype(np.uint8)


def saliency_to_binary(saliency: np.ndarray) -> np.ndarray:
    sal_u8 = np.clip(saliency * 255.0, 0, 255).astype(np.uint8)
    _, binary = cv2.threshold(sal_u8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return (binary > 0).astype(np.uint8)


def compute_metrics(saliency: np.ndarray, gt_mask: np.ndarray) -> tuple[SampleResult, np.ndarray]:
    pred_mask = saliency_to_binary(saliency)
    gt = gt_mask.astype(bool)
    pred = pred_mask.astype(bool)

    tp = float(np.logical_and(pred, gt).sum())
    fp = float(np.logical_and(pred, ~gt).sum())
    fn = float(np.logical_and(~pred, gt).sum())
    union = float(np.logical_or(pred, gt).sum())

    precision = tp / (tp + fp + EPS)
    recall = tp / (tp + fn + EPS)
    f1 = 2 * precision * recall / (precision + recall + EPS)
    iou = tp / (union + EPS)
    dice = 2 * tp / (pred.sum() + gt.sum() + EPS)
    mae = float(np.mean(np.abs(saliency.astype(np.float32) - gt_mask.astype(np.float32))))
    metrics = SampleResult("", "", mae, precision, recall, f1, iou, dice)
    return metrics, pred_mask


def build_panel(image_bgr: np.ndarray, gt_mask: np.ndarray, saliency: np.ndarray, pred_mask: np.ndarray) -> np.ndarray:
    gt_vis = np.dstack([gt_mask * 255] * 3).astype(np.uint8)
    sal_u8 = np.clip(saliency * 255.0, 0, 255).astype(np.uint8)
    heat = cv2.applyColorMap(sal_u8, cv2.COLORMAP_TURBO)

    overlay = image_bgr.copy()
    overlay[pred_mask.astype(bool)] = (0.3 * overlay[pred_mask.astype(bool)] + np.array([0, 0, 255]) * 0.7).astype(np.uint8)

    return np.hstack([image_bgr, gt_vis, heat, overlay])


def iter_samples(dataset_root: Path, categories: Iterable[str], limit_per_class: int | None) -> Iterable[tuple[str, Path, Path]]:
    for category in categories:
        img_dir = dataset_root / category / "Imgs"
        jpgs = sorted(img_dir.glob("*.jpg"))
        if limit_per_class is not None:
            jpgs = jpgs[:limit_per_class]
        for img_path in jpgs:
            mask_path = img_path.with_suffix(".png")
            if mask_path.exists():
                yield category, img_path, mask_path


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Modern Python reimplementation of the MCue saliency pipeline.")
    parser.add_argument("--dataset-root", type=Path, default=Path("git-magnetic-tile-datasets"))
    parser.add_argument("--output-root", type=Path, default=Path("outputs") / "mcue_modern")
    parser.add_argument("--categories", nargs="*", default=None)
    parser.add_argument("--limit-per-class", type=int, default=4)
    parser.add_argument("--use-mcue2", action="store_true")
    args = parser.parse_args()

    dataset_root = args.dataset_root.resolve()
    output_root = args.output_root.resolve()
    categories = args.categories or sorted(p.name for p in dataset_root.glob("MT_*") if p.is_dir())

    ensure_dir(output_root)
    vis_dir = output_root / "visualizations"
    ensure_dir(vis_dir)

    rows: list[dict[str, object]] = []
    for category, img_path, mask_path in iter_samples(dataset_root, categories, args.limit_per_class):
        image = cv2.imread(str(img_path), cv2.IMREAD_COLOR)
        if image is None:
            print(f"skip unreadable image: {img_path}")
            continue
        gt_mask = read_mask(mask_path)
        outputs = mcue_saliency(image)
        saliency = outputs["mcue2" if args.use_mcue2 else "mcue"]
        metrics, pred_mask = compute_metrics(saliency, gt_mask)
        metrics.category = category
        metrics.stem = img_path.stem

        sample_out = output_root / category / img_path.stem
        ensure_dir(sample_out)
        for name, arr in outputs.items():
            cv2.imwrite(str(sample_out / f"{name}.png"), np.clip(arr * 255.0, 0, 255).astype(np.uint8))
        cv2.imwrite(str(sample_out / "pred_mask.png"), pred_mask.astype(np.uint8) * 255)
        cv2.imwrite(str(sample_out / "panel.png"), build_panel(image, gt_mask, saliency, pred_mask))

        rows.append(
            {
                "category": metrics.category,
                "stem": metrics.stem,
                "mae": metrics.mae,
                "precision": metrics.precision,
                "recall": metrics.recall,
                "f1": metrics.f1,
                "iou": metrics.iou,
                "dice": metrics.dice,
            }
        )
        print(
            f"{category}/{img_path.name}: "
            f"MAE={metrics.mae:.4f} F1={metrics.f1:.4f} IoU={metrics.iou:.4f} Dice={metrics.dice:.4f}"
        )

    summary_path = output_root / "summary.csv"
    with summary_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["category", "stem", "mae", "precision", "recall", "f1", "iou", "dice"])
        writer.writeheader()
        writer.writerows(rows)

    by_category: dict[str, dict[str, float]] = {}
    for category in categories:
        subset = [row for row in rows if row["category"] == category]
        if not subset:
            continue
        by_category[category] = {
            metric: float(np.mean([float(row[metric]) for row in subset]))
            for metric in ["mae", "precision", "recall", "f1", "iou", "dice"]
        }

    overall = {}
    if rows:
        overall = {
            metric: float(np.mean([float(row[metric]) for row in rows]))
            for metric in ["mae", "precision", "recall", "f1", "iou", "dice"]
        }

    report = {"categories": by_category, "overall": overall, "samples": len(rows)}
    (output_root / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
