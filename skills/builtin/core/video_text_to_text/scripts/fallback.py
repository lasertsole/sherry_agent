"""
Video frame extraction tool
- extract_frames:    Sample frames at fixed intervals, then deduplicate by visual similarity
"""

import cv2
import os
import time
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from skimage.metrics import structural_similarity as ssim


@dataclass
class FrameResult:
    """Result of one extracted frame: image path and timestamp"""
    image_path: str
    timestamp_sec: float


def _compute_diff_ratio(frame_a: np.ndarray, frame_b: np.ndarray) -> float:
    """
    Compute dissimilarity ratio between two frames (0.0 ~ 1.0).

    Uses SSIM (structural similarity) which is robust to motion blur
    and lighting changes — ideal for action scenes where pixel-level
    diff would falsely flag moving subjects as "different scenes".
    Returns 1.0 - SSIM so that 0 = identical, 1 = completely different.
    """
    if frame_a.shape != frame_b.shape:
        return 1.0
    # SSIM expects uint8 or float in [0, 1]; convert to grayscale for efficiency
    gray_a = cv2.cvtColor(frame_a, cv2.COLOR_BGR2GRAY)
    gray_b = cv2.cvtColor(frame_b, cv2.COLOR_BGR2GRAY)
    score, _ = ssim(gray_a, gray_b, full=True)
    return float(1.0 - score)


def extract_frames(
    video_path: str,
    output_dir: str,
    threshold: float = 0.3,
    interval_sec: float = 1.0,
    prefix: str = "frame",
    img_format: str = "jpg",
    start_sec: float = 0.0,
    end_sec: float | None = None,
) -> list[FrameResult]:
    """
    Extract frames at fixed time intervals, then deduplicate by visual similarity.

    Phase 1 — Interval sampling: read one frame every `interval_sec` seconds.
    Phase 2 — Similarity dedup: compare consecutive sampled frames; if the
    difference ratio between frame[i] and frame[i+1] is below `threshold`,
    discard frame[i+1] (keeping the earlier one).

    Args:
        video_path:   Video file path
        output_dir:   Output directory for images (auto-created)
        threshold:    Dissimilarity threshold (0.0 ~ 1.0, SSIM-based).
                      Higher = more change required to keep both frames.
                      Default 0.3 (works well for both action and static scenes).
        interval_sec: Sampling interval in seconds. Default 1.0.
        prefix:       Output filename prefix, default "frame"
        img_format:   Image format "jpg" or "png", default "jpg"
        start_sec:    Start timestamp in seconds, default 0
        end_sec:      End timestamp in seconds, default None (until video end)

    Returns:
        list[FrameResult]: List of (image_path, timestamp_sec)

    Raises:
        FileNotFoundError: Video file does not exist
        ValueError:        Cannot open video or invalid parameters
    """
    # --- Validation ---
    if not os.path.isfile(video_path):
        raise FileNotFoundError(f"Video file not found: {video_path}")
    if not 0.05 <= threshold <= 0.8:
        raise ValueError(f"threshold must be in [0.05, 0.8], got: {threshold}")
    if interval_sec < 0.5 or interval_sec > 3.0:
        raise ValueError(f"interval_sec must be in [0.5, 3.0], got: {interval_sec}")
    if start_sec < 0:
        raise ValueError(f"start_sec must be >= 0, got: {start_sec}")

    # --- Open video ---
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError(f"Cannot open video file: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration_sec = total_frames / fps if fps > 0 else 0.0

    if end_sec is None or end_sec > duration_sec:
        end_sec = duration_sec

    # --- Create output directory ---
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    ext = "jpg" if img_format.lower() in ("jpg", "jpeg") else "png"
    save_params = [cv2.IMWRITE_JPEG_QUALITY, 95] if ext == "jpg" else []

    interval_frames = max(1, int(interval_sec * fps))

    # ──────────────────────────────────────────────
    # Phase 1: Interval-based frame sampling
    # ──────────────────────────────────────────────
    sampled_frames: list[tuple[int, np.ndarray]] = []

    cap.set(cv2.CAP_PROP_POS_FRAMES, int(start_sec * fps))
    frame_idx = int(start_sec * fps)

    while frame_idx < int(end_sec * fps):
        ret, frame = cap.read()
        if not ret:
            break
        sampled_frames.append((frame_idx, frame))
        frame_idx += interval_frames
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)

    cap.release()

    if not sampled_frames:
        return []

    # ──────────────────────────────────────────────
    # Phase 2: Similarity-based dedup
    # ──────────────────────────────────────────────
    dedup_indices: list[int] = [0]
    for i in range(1, len(sampled_frames)):
        prev_frame = sampled_frames[dedup_indices[-1]][1]
        curr_frame = sampled_frames[i][1]
        diff = _compute_diff_ratio(prev_frame, curr_frame)
        if diff >= threshold:
            dedup_indices.append(i)

    # ──────────────────────────────────────────────
    # Phase 3: Save retained frames
    # ──────────────────────────────────────────────
    results: list[FrameResult] = []
    for save_idx, sample_idx in enumerate(dedup_indices):
        frame_idx, frame = sampled_frames[sample_idx]
        time_sec = frame_idx / fps

        img_filename = f"{prefix}_{int(time.time() * 1000)}.{ext}"
        img_path = os.path.join(output_dir, img_filename)
        cv2.imwrite(img_path, frame, save_params)

        results.append(FrameResult(
            image_path=img_path,
            timestamp_sec=time_sec,
        ))

    return results