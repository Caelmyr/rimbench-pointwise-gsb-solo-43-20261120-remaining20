"""斜放复检：用单应（透视）模型检验九点标板能否解释为同一平面的姿态变化。

线性检查只拟合仿射（6 参数）模型；标板临时斜靠支架时，同一块板的近端和远端
点距不同，属于平面透视（8 参数单应）现象。本模块复用既有圆点定位结果，通过
RANSAC 稳健拟合把个别偏点排除在拟合之外，再回答两件事：

1. 多数有效圆点能否可靠地解释为同一个平面的透视变化；
2. 哪些圆点在透视模型下仍然偏离，需要单独排查或复拍。
"""
from __future__ import annotations

from itertools import combinations
import math

import numpy as np


MIN_PLANE_INLIERS = 6
MAX_CONDITION = 1.0e7
IMPROVEMENT_FLOOR = 0.2
# 近远端尺度差异小于该值时，认为透视分量很弱，斜放解释没有实际依据。
NOTABLE_PERSPECTIVE = 0.01


def _normalization(points):
    """Hartley 归一化：质心移到原点、平均距原点距离为 sqrt(2)。"""
    points = np.asarray(points, dtype=float)
    mean = points.mean(axis=0)
    centered = points-mean
    scale = math.sqrt(2)/float(np.mean(np.linalg.norm(centered, axis=1)) or 1.0)
    matrix = np.array([
        [scale, 0, -scale*mean[0]],
        [0, scale, -scale*mean[1]],
        [0, 0, 1],
    ])
    return matrix


def homography_fit(source, destination):
    """归一化 DLT 求 source(mm) -> destination(px) 的 3×3 单应，失败返回 None。"""
    source = np.asarray(source, dtype=float)
    destination = np.asarray(destination, dtype=float)
    if len(source) < 4:
        return None
    t_source = _normalization(source)
    t_dest = _normalization(destination)
    normalized_source = (t_source@np.column_stack([source, np.ones(len(source))]).T).T
    normalized_dest = (t_dest@np.column_stack([destination, np.ones(len(destination))]).T).T
    rows = []
    for (x, y, _), (u, v, _) in zip(normalized_source, normalized_dest):
        rows.append([-x, -y, -1, 0, 0, 0, u*x, u*y, u])
        rows.append([0, 0, 0, -x, -y, -1, v*x, v*y, v])
    matrix = np.asarray(rows, dtype=float)
    if not np.isfinite(matrix).all():
        return None
    try:
        _, singular, vt = np.linalg.svd(matrix, full_matrices=False)
    except np.linalg.LinAlgError:
        return None
    # 归一化 DLT 在精确拟合时末奇异值本就接近机器精度（约 1e-16 量级）；
    # 真正的共线退化由调用处的非共线采样过滤，这里只拦截数值完全坍缩。
    if not np.isfinite(singular).all() or singular[0] <= 0 or singular[-1] < singular[0]*1e-18:
        return None
    normalized_h = vt[-1].reshape(3, 3)
    # q = T_dest q'，p = T_source p'，q' = H' p'  =>  H = T_dest^-1 H' T_source
    homography = np.linalg.inv(t_dest)@normalized_h@t_source
    if abs(homography[2, 2]) < 1e-12:
        return None
    return homography/homography[2, 2]


def homography_apply(homography, points):
    points = np.asarray(points, dtype=float)
    homogeneous = (homography@np.column_stack([points, np.ones(len(points))]).T).T
    denominators = homogeneous[:, 2]
    if np.any(np.abs(denominators) < 1e-9):
        return None
    return homogeneous[:, :2]/denominators[:, None]


def _well_conditioned(homography, nominal):
    if homography is None or not np.isfinite(homography).all():
        return False
    if np.linalg.cond(homography) > MAX_CONDITION:
        return False
    predictions = homography_apply(homography, nominal)
    if predictions is None or not np.isfinite(predictions).all():
        return False
    # 单应必须保持朝向：所有标板点位于投影线同一侧（齐次分母同号），
    # 等价于物理上整块平面都在相机前方，没有翻到投影线另一侧。
    denominators = (homography[2, :2]@nominal.T)+homography[2, 2]
    if np.any(np.abs(denominators) < 1e-9):
        return False
    if len(set(np.sign(denominators))) != 1:
        return False
    return True


def _spread_ok(indices, rows):
    chosen = [rows[index] for index in indices]
    if len({row["row"] for row in chosen}) < 3 or len({row["column"] for row in chosen}) < 3:
        return False
    positions = np.asarray([[row["row"], row["column"]] for row in chosen], dtype=float)
    return float(np.ptp(positions[:, 0])) >= 1 and float(np.ptp(positions[:, 1])) >= 1


def _candidate_samples(rows):
    """生成非共线采样，优先返回 5/6 点超定集（4 点 DLT 对弱透视数值不稳）。

    分布充分性放到内点集合上判断，而不是采样集。
    """
    indexes = list(range(len(rows)))
    samples = []
    for size in (6, 5, 4):
        for combo in combinations(indexes, size):
            chosen = [rows[index] for index in combo]
            coords = np.asarray([[row["nominal_mm"][0], row["nominal_mm"][1]] for row in chosen])
            # 采样点不能近似共线，否则 DLT 退化。
            if np.linalg.matrix_rank(coords-coords.mean(axis=0), tol=1e-8) < 2:
                continue
            samples.append(combo)
    return samples


def _residuals(homography, rows):
    nominal = np.asarray([row["nominal_mm"] for row in rows], dtype=float)
    observed = np.asarray([row["observed_px"] for row in rows], dtype=float)
    predicted = homography_apply(homography, nominal)
    if predicted is None:
        return None
    return np.linalg.norm(observed-predicted, axis=1), predicted


def _rms(values):
    values = np.asarray(values, dtype=float)
    return float(np.sqrt(np.mean(values**2))) if len(values) else None


def robust_perspective(rows, residual_limit):
    """RANSAC 稳健单应拟合，返回 (homography, inlier_indexes, rms) 或 None。

    样本只需非共线；三行三列的分布充分性在内点集合上判断，避免个别偏点
    让所有采样都无法成模。
    """
    valid = [row for row in rows if row["observed_px"] is not None]
    if len(valid) < MIN_PLANE_INLIERS:
        return None
    nominal_all = np.asarray([row["nominal_mm"] for row in valid], dtype=float)
    observed_all = np.asarray([row["observed_px"] for row in valid], dtype=float)
    best = None
    for combo in _candidate_samples(valid):
        source = [valid[index]["nominal_mm"] for index in combo]
        destination = [valid[index]["observed_px"] for index in combo]
        seed = homography_fit(source, destination)
        if not _well_conditioned(seed, nominal_all):
            continue
        residual = np.linalg.norm(observed_all-homography_apply(seed, nominal_all), axis=1)
        inliers = tuple(index for index, value in enumerate(residual) if value <= residual_limit)
        if len(inliers) < MIN_PLANE_INLIERS or not _spread_ok(inliers, valid):
            continue
        # 先比内点数量，再偏好更大的超定采样（弱透视下更稳定），最后比内点 RMS。
        score = (len(inliers), len(combo), -_rms(residual[list(inliers)]))
        if best is None or score > best[0]:
            best = (score, seed, inliers)
    if best is None:
        return None
    # 用全部内点重新拟合，并迭代让阈值边界上的点稳定归属。
    homography, inliers = best[1], set(best[2])
    for _ in range(3):
        source = [valid[index]["nominal_mm"] for index in sorted(inliers)]
        destination = [valid[index]["observed_px"] for index in sorted(inliers)]
        refined = homography_fit(source, destination)
        if not _well_conditioned(refined, nominal_all):
            break
        residual = np.linalg.norm(observed_all-homography_apply(refined, nominal_all), axis=1)
        refreshed = {index for index, value in enumerate(residual) if value <= residual_limit}
        if len(refreshed) < MIN_PLANE_INLIERS or not _spread_ok(refreshed, valid):
            break
        homography = refined
        if refreshed == inliers:
            break
        inliers = refreshed
    residual, _ = _residuals(homography, valid)
    inlier_indexes = tuple(sorted(inliers))
    return homography, inlier_indexes, _rms(residual[list(inlier_indexes)])


def _scale_ratio(rows):
    """比较标板近端行与远端行的实测点距，体现近大远小。"""
    pitches = {}
    for row_index in (0, 2):
        members = sorted(
            (row for row in rows if row["row"] == row_index and row["observed_px"] is not None),
            key=lambda row: row["column"],
        )
        if len(members) < 2:
            continue
        first, last = members[0], members[-1]
        column_span = last["column"]-first["column"]
        if column_span == 0:
            continue
        length = float(np.linalg.norm(
            np.asarray(last["observed_px"])-np.asarray(first["observed_px"])
        ))
        pitches[row_index] = length/column_span
    if set(pitches) != {0, 2} or min(pitches.values()) <= 0:
        return None
    near, far = max(pitches.values()), min(pitches.values())
    return near/far-1


def perspective_review(rows, residual_limit):
    """对既有圆点定位结果做斜放复检，结果不改动线性检查字段。"""
    valid = [row for row in rows if row["observed_px"] is not None]
    missing = [row for row in rows if row["observed_px"] is None]
    distribution = (len({row["row"] for row in valid}), len({row["column"] for row in valid}))
    result = {
        "state": "insufficient",
        "enabled": True,
        "residual_limit_px": residual_limit,
        "detected": len(valid),
        "row_span": distribution[0],
        "column_span": distribution[1],
        "homography": None,
        "inlier_count": 0,
        "inlier_positions": [],
        "plane_rms_px": None,
        "plane_max_residual_px": None,
        "plane_max_at": None,
        "perspective_strength": None,
        "near_far_scale_difference": None,
        "linear_rms_px": _rms([row["residual_px"] for row in valid if row.get("residual_px") is not None]),
        "rms_reduction": None,
        "explained_points": [],
        "remaining_outliers": [],
        "notable_perspective": False,
        "reliable": False,
        "warnings": [],
        "reasons": [],
    }
    if len(valid) < MIN_PLANE_INLIERS or min(distribution) < 3:
        result["reasons"].append("有效圆点不足 6 个或未覆盖三行三列，无法稳定检验平面透视，请摆正或补光后复拍。")
        result["warnings"].append("斜放复检因有效点数量或分布不足而不可用。")
        return result

    fitted = robust_perspective(rows, residual_limit)
    if fitted is None:
        result["reasons"].append("找不到满足三行三列分布的一致平面，单应拟合不稳定，请复拍确认标板姿态。")
        result["warnings"].append("斜放复检未能形成可靠的平面拟合。")
        return result
    homography, inlier_indexes, plane_rms = fitted
    valid_rows = valid
    residual, predicted = _residuals(homography, valid_rows)

    inlier_set = set(inlier_indexes)
    points = []
    remaining = []
    explained = []
    for index, row in enumerate(valid_rows):
        entry = {
            "row": row["row"],
            "column": row["column"],
            "observed_px": row["observed_px"],
            "plane_fit_px": predicted[index].tolist(),
            "plane_residual_px": float(residual[index]),
            "linear_residual_px": row.get("residual_px"),
            "plane_inlier": index in inlier_set,
        }
        points.append(entry)
        if index in inlier_set:
            explained.append(entry)
        else:
            remaining.append(entry)
    result.update(
        homography=homography.tolist(),
        inlier_count=len(inlier_indexes),
        inlier_positions=[[valid_rows[index]["row"], valid_rows[index]["column"]] for index in inlier_indexes],
        plane_rms_px=plane_rms,
        plane_max_residual_px=float(np.max(residual[list(inlier_indexes)])),
        plane_max_at=[
            valid_rows[max(inlier_indexes, key=lambda index: residual[index])]["row"],
            valid_rows[max(inlier_indexes, key=lambda index: residual[index])]["column"],
        ],
        explained_points=explained,
        remaining_outliers=sorted(remaining, key=lambda entry: -entry["plane_residual_px"]),
        points=points,
    )

    strength = float(np.linalg.norm(homography[2, :2]))
    ratio = _scale_ratio(valid_rows)
    result["perspective_strength"] = strength
    result["near_far_scale_difference"] = ratio
    result["notable_perspective"] = ratio is not None and ratio >= NOTABLE_PERSPECTIVE
    linear_rms = result["linear_rms_px"]
    if linear_rms and linear_rms > 1e-9:
        result["rms_reduction"] = max(0.0, 1-plane_rms/linear_rms)

    inlier_distribution = (
        len({valid_rows[index]["row"] for index in inlier_indexes}),
        len({valid_rows[index]["column"] for index in inlier_indexes}),
    )
    warnings = []
    reasons = []
    distribution_ok = len(inlier_indexes) >= MIN_PLANE_INLIERS and min(inlier_distribution) >= 3
    fit_ok = plane_rms <= residual_limit and result["plane_max_residual_px"] <= residual_limit
    improvement_ok = result["rms_reduction"] is None or result["rms_reduction"] >= IMPROVEMENT_FLOOR

    if not distribution_ok:
        reasons.append("参与平面拟合的圆点未覆盖三行三列，透视结论可能被局部点主导，请复拍。")
        warnings.append("平面拟合有效点分布不足。")
    if not fit_ok:
        reasons.append("平面内点的拟合残差仍超过定位残差限值，斜放不能解释当前偏差。")
        warnings.append("透视平面自身的拟合残差超限。")
    if not improvement_ok:
        reasons.append("透视模型相对线性模型的残差改善不足 20%，斜放解释缺乏数据支撑。")
        warnings.append("透视模型相对线性检查没有明显改善。")
    if not result["notable_perspective"]:
        reasons.append("近远端尺度差异不明显，影像更接近正对拍摄；若线性检查已通过，无需按斜放处理。")
    if remaining:
        labels = "、".join(f"{entry['row']+1} 行 {entry['column']+1} 列" for entry in result["remaining_outliers"])
        reasons.append(f"以下圆点在透视模型下仍超出 {residual_limit:g} px，需单独排查：{labels}。")
        warnings.append("个别圆点偏离同一平面，未参与最终拟合。")
    if missing:
        warnings.append("部分圆点无法读取，平面结论只基于已定位圆点。")

    result["reliable"] = bool(distribution_ok and fit_ok and improvement_ok and result["notable_perspective"])
    if not distribution_ok or not fit_ok:
        state = "insufficient"
    elif result["reliable"] and not remaining:
        state = "checked"
    else:
        state = "review"
    result["state"] = state
    result["warnings"] = warnings
    result["reasons"] = reasons
    return result
