from __future__ import annotations

from statistics import median


MIN_STABLE = 2
EDGE_WINDOW = 3
TOLERANCE = 1e-9
NOISE_FACTOR = 2


def analyze_steps(samples, *, metric, units, direction):
    """Read shoulders off an axial curve using distinguishable stable levels.

    Neighbouring samples belong to the same stable level while their values differ by
    no more than the measurement resolution. A shoulder is the half-height position
    between the stable levels on either side of a transition. A transition contained
    in one sampled interval is reported as a shoulder; a transition with sampled
    heights inside it cannot decide between one wide chamfer and two close shoulders,
    so the span is flagged for a denser re-measurement instead of being claimed as a
    step. A sample that stands out from two agreeing neighbours is a spike and does
    not make a step on its own. Where unmeasured heights split the curve, the shoulder
    stays missing unless both sides still carry enough stable samples to establish it.
    """
    heights = [row["z"] for row in samples]
    result = {
        "metric": metric, "units": units, "direction": direction,
        "sample_spacing": _spacing(heights),
        "resolution": None, "resolution_basis": None, "typical_interval": None,
        "segments": [], "shoulders": [], "pairs": [], "spikes": [],
        "review_spans": [], "counts": {},
        "state": "no-curve", "detail": "选定高度内没有可测量的截线。",
    }
    if metric == "roundness":
        result.update(state="not-applicable", detail="圆度为无量纲比值，不用于台阶与肩位判读。")
        return result
    measured = [
        {
            "index": index, "z": float(row["z"]), "value": float(row["value"]),
            "interval": _interval(row.get("interval")),
        }
        for index, row in enumerate(samples)
        if row.get("state") == "measured" and row.get("value") is not None
    ]
    if len(measured) < 4:
        if measured:
            result.update(state="insufficient", detail="有效采样不足四个高度，无法辨认稳定段与过渡。")
        return result
    resolution, typical, basis = _resolution(measured)
    result.update(
        resolution=_round(resolution), resolution_basis=basis,
        typical_interval=_round(typical),
    )

    spikes = _spikes(measured, resolution)
    spike_indexes = {item["index"] for item in spikes}
    result["spikes"] = [
        {
            "z": item["z"], "value": _round(item["value"]),
            "interval": _number_list(item["interval"]),
            "neighbours": [item["previous"]["z"], item["following"]["z"]],
            "detail": "单个采样偏离上下相邻层，按尖峰排除，不构成台阶。",
        }
        for item in spikes
    ]
    runs = _runs(measured)
    segments = []
    shoulders = []
    for run in runs:
        filtered = [
            {**row, "position": position}
            for position, row in enumerate(run["samples"])
            if row["index"] not in spike_indexes
        ]
        build = _scan_run(filtered, resolution)
        base = len(segments)
        for item in build["segments"]:
            segments.append(_segment_record(len(segments), run["id"], item, resolution))
        for transition in build["transitions"]:
            shoulders.append(_shoulder_record(transition, segments, base, resolution, units))
    shoulders.extend(_gap_shoulders(runs, segments, resolution, units))
    shoulders.sort(key=lambda item: (item["transition"]["start_z"], item["transition"]["end_z"]))
    for identity, shoulder in enumerate(shoulders):
        shoulder["id"] = identity
        shoulder["left"]["side"] = "left"
        shoulder["right"]["side"] = "right"
    used = {
        side["segment"] for shoulder in shoulders
        for side in (shoulder.get("left"), shoulder.get("right"))
        if side.get("segment") is not None
    }
    for segment in segments:
        segment["used"] = segment["id"] in used
    pairs = _pairs(shoulders)
    review = _review(shoulders)
    counts = {
        "segments": sum(segment["used"] for segment in segments),
        "shoulders": sum(item["state"] == "shoulder" for item in shoulders),
        "ambiguous": sum(item["state"] == "ambiguous" for item in shoulders),
        "missing": sum(item["state"] == "missing" for item in shoulders),
        "pairs": len(pairs), "spikes": len(spikes),
    }
    result.update(
        segments=segments, shoulders=shoulders, pairs=pairs,
        review_spans=review, counts=counts,
    )
    result.update(_summary(counts))
    return result


def _resolution(measured):
    widths = [
        (row["interval"][1]-row["interval"][0])/2
        for row in measured if row["interval"] is not None
    ]
    if widths:
        typical = median(widths)
        if typical > TOLERANCE:
            return 2*typical, typical, "measurement-interval"
    links = [
        abs(second["value"]-first["value"])
        for first, second in zip(measured, measured[1:])
        if second["index"] == first["index"]+1
    ]
    noise = median(links) if links else 0.0
    return max(NOISE_FACTOR*noise, 1e-9), None, "link-noise"


def _spikes(measured, resolution):
    found = []
    for position in range(1, len(measured)-1):
        previous, current, following = measured[position-1], measured[position], measured[position+1]
        if current["index"] != previous["index"]+1 or following["index"] != current["index"]+1:
            continue
        if _overshoots(previous, current, following, resolution):
            found.append(_spike(previous, current, following))
            continue
        if abs(following["value"]-previous["value"]) > resolution+TOLERANCE:
            continue
        centre = (previous["value"]+following["value"])/2
        if abs(current["value"]-centre) <= resolution+TOLERANCE:
            continue
        if abs(current["value"]-previous["value"]) <= resolution+TOLERANCE:
            continue
        if abs(current["value"]-following["value"]) <= resolution+TOLERANCE:
            continue
        found.append(_spike(previous, current, following))
    return found


def _overshoots(previous, current, following, resolution):
    # A sample outside the span of both neighbours cannot sit on a monotonic shoulder;
    # it is a stray reading even when the neighbours themselves differ by a step.
    low = min(previous["value"], following["value"])
    high = max(previous["value"], following["value"])
    return current["value"] < low-resolution-TOLERANCE or current["value"] > high+resolution+TOLERANCE


def _spike(previous, current, following):
    return {"index": current["index"], "z": current["z"], "value": current["value"],
            "interval": current["interval"], "previous": previous, "following": following}


def _runs(measured):
    runs = []
    current = []
    for row in measured:
        if current and row["index"] != current[-1]["index"]+1:
            runs.append(current)
            current = []
        current.append(row)
    if current:
        runs.append(current)
    return [
        {"id": identity, "first": run[0]["index"], "samples": run}
        for identity, run in enumerate(runs)
    ]


def _scan_run(run, resolution):
    if len(run) < MIN_STABLE:
        return {"segments": [], "transitions": []}
    links = [
        abs(second["value"]-first["value"]) > resolution+TOLERANCE
        for first, second in zip(run, run[1:])
    ]
    pieces = []
    start = 0
    for index, changed in enumerate(links):
        if changed:
            pieces.append((start, index))
            start = index+1
    pieces.append((start, len(run)-1))
    stable = [
        (first, last) for first, last in pieces
        if last-first+1 >= MIN_STABLE and _flat(run[first:last+1], resolution)
    ]
    segments = [run[first:last+1] for first, last in stable]
    transitions = []
    for index in range(len(segments)-1):
        left, right = segments[index], segments[index+1]
        first = left[-1]["position"]
        last = right[0]["position"]
        between = run[first+1:last]
        transitions.append({
            "left": left, "right": right, "between": between,
            "left_index": index, "right_index": index+1,
        })
    return {"segments": segments, "transitions": transitions}


def _flat(samples, resolution):
    # A run linked only by sub-resolution changes may still creep across many samples;
    # taper is not a stable level, and its end must not be read as a step.
    values = [row["value"] for row in samples]
    return max(values)-min(values) <= 2*resolution+TOLERANCE


def _segment_record(identity, run_id, samples, resolution):
    values = [row["value"] for row in samples]
    drift = max(values)-min(values)
    return {
        "id": identity, "run": run_id,
        "start_z": samples[0]["z"], "end_z": samples[-1]["z"],
        "samples": len(samples), "level": _round(median(values)),
        "interval": _bounds(samples),
        "drift": _round(drift), "steady": bool(drift <= 2*resolution+TOLERANCE),
        "used": False,
    }


def _bounds(samples):
    low = [row["interval"][0] for row in samples if row["interval"] is not None]
    high = [row["interval"][1] for row in samples if row["interval"] is not None]
    if not low or not high:
        return None
    return [_round(median(low)), _round(median(high))]


def _side(samples, edge):
    window = samples[-EDGE_WINDOW:] if edge == "left" else samples[:EDGE_WINDOW]
    return {
        "level": _round(median(row["value"] for row in window)),
        "interval": _bounds(window),
        "used_start_z": window[0]["z"], "used_end_z": window[-1]["z"],
        "used_samples": len(window),
    }


def _shoulder_record(transition, segments, base, resolution, units):
    left_samples, right_samples = transition["left"], transition["right"]
    left_segment = segments[base+transition["left_index"]]
    right_segment = segments[base+transition["right_index"]]
    left = {**_side(left_samples, "left"), "segment": left_segment["id"]}
    right = {**_side(right_samples, "right"), "segment": right_segment["id"]}
    start_z, end_z = left_samples[-1]["z"], right_samples[0]["z"]
    entry = {
        "state": "missing", "z": None, "interval": None, "spacing_range": None,
        "half_level": None, "left": left, "right": right, "step": None,
        "transition": {
            "start_z": start_z, "end_z": end_z, "span": _round(end_z-start_z),
            "interior_samples": len(transition["between"]),
            "links": 1+len(transition["between"]),
        },
        "flags": [], "detail": "", "units": units,
    }
    if not _distinguishable(left, right, resolution):
        entry.update(
            state="rejected",
            detail="两侧稳定段的尺寸在现有分辨力下不可区分，不作为台阶。",
            flags=["not-distinguishable"],
        )
        return entry
    half = (left["level"]+right["level"])/2
    chain = [left_samples[-1], *transition["between"], right_samples[0]]
    crossing, count = _crossing(chain, half)
    entry["half_level"] = _round(half)
    entry["step"] = _step(left, right)
    if crossing is None:
        entry.update(
            state="ambiguous", flags=["no-crossing"],
            detail="过渡采样没有跨过半高值，肩位不唯一，需要补测。",
        )
        return entry
    first, second, share = crossing
    entry.update(
        z=first["z"]+share*(second["z"]-first["z"]),
        spacing_range=[first["z"], second["z"]],
        interval=_interval_at(first, second, half, left["interval"], right["interval"]),
    )
    entry["transition"]["half_z"] = entry["z"]
    if transition["between"]:
        entry.update(
            state="ambiguous", flags=["wide-transition"],
            detail="过渡跨越多个采样高度，一个宽倒角与两个相邻肩都能解释现有采样，需要补测。",
        )
        if count > 1:
            entry["flags"].append("multiple-crossings")
    elif count > 1:
        entry.update(
            state="ambiguous", flags=["multiple-crossings"],
            detail="过渡内出现多个半高交点，肩位不唯一，需要补测。",
        )
    else:
        entry.update(
            state="shoulder",
            detail="由两侧稳定段尺寸的半高位置认定肩位。",
        )
    return entry


def _crossing(chain, half):
    candidates = [
        (abs(second["value"]-first["value"]), first, second,
         (half-first["value"])/(second["value"]-first["value"]))
        for first, second in zip(chain, chain[1:])
        if abs(second["value"]-first["value"]) > TOLERANCE
        and min(first["value"], second["value"])-TOLERANCE <= half <= max(first["value"], second["value"])+TOLERANCE
    ]
    if not candidates:
        return None, 0
    _, first, second, share = max(candidates, key=lambda item: item[0])
    return (first, second, min(1.0, max(0.0, share))), len(candidates)


def _interval_at(first, second, half, left_interval, right_interval):
    # The half-height level is uncertain because both levels are intervals; sweep the
    # level across the two interval midpoints, then the sampled endpoints at each.
    if left_interval and right_interval:
        levels = [(left_interval[0]+right_interval[0])/2, (left_interval[1]+right_interval[1])/2]
    else:
        levels = [half, half]
    bounds = _crossing_positions(first, second, levels[0])
    bounds += _crossing_positions(first, second, levels[1])
    if not bounds:
        return [first["z"], second["z"]]
    return [_round(min(bounds)), _round(max(bounds))]


def _crossing_positions(first, second, level):
    values_a = first["interval"] or [first["value"], first["value"]]
    values_b = second["interval"] or [second["value"], second["value"]]
    positions = []
    for value_a in values_a:
        for value_b in values_b:
            if abs(value_b-value_a) <= TOLERANCE:
                continue
            if (value_a-level)*(value_b-level) <= TOLERANCE:
                share = (level-value_a)/(value_b-value_a)
                if -TOLERANCE <= share <= 1+TOLERANCE:
                    positions.append(first["z"]+min(1.0, max(0.0, share))*(second["z"]-first["z"]))
    return positions


def _envelope(first, second):
    if first is None:
        return list(second) if second else None
    if second is None:
        return list(first)
    return [_round(min(first[0], second[0])), _round(max(first[1], second[1]))]


def _distinguishable(left, right, resolution):
    if left["level"] is None or right["level"] is None:
        return False
    if abs(right["level"]-left["level"]) <= resolution+TOLERANCE:
        return False
    if left["interval"] and right["interval"]:
        overlap = min(left["interval"][1], right["interval"][1])-max(left["interval"][0], right["interval"][0])
        if overlap > TOLERANCE:
            return False
    return True


def _step(left, right):
    height = right["level"]-left["level"]
    interval = None
    if left["interval"] and right["interval"]:
        interval = [right["interval"][0]-left["interval"][1],
                    right["interval"][1]-left["interval"][0]]
    return {
        "height": _round(height),
        "interval": [_round(interval[0]), _round(interval[1])] if interval else None,
    }


def _gap_shoulders(runs, segments, resolution, units):
    result = []
    for left_run, right_run in zip(runs, runs[1:]):
        left_samples = left_run["samples"]
        right_samples = right_run["samples"]
        left_segment = _last_segment(segments, left_run["id"])
        right_segment = _first_segment(segments, right_run["id"])
        left = {
            **_side(_segment_window(left_samples, left_segment, "left"), "left"),
            "segment": None if left_segment is None else left_segment["id"],
        }
        right = {
            **_side(_segment_window(right_samples, right_segment, "right"), "right"),
            "segment": None if right_segment is None else right_segment["id"],
        }
        if not _distinguishable(left, right, resolution):
            continue
        start_z, end_z = left_samples[-1]["z"], right_samples[0]["z"]
        solid = left["segment"] is not None and right["segment"] is not None
        result.append({
            "state": "missing", "z": None, "interval": None, "spacing_range": None,
            "half_level": _round((left["level"]+right["level"])/2),
            "left": left, "right": right, "step": _step(left, right),
            "transition": {
                "start_z": start_z, "end_z": end_z, "span": _round(end_z-start_z),
                "interior_samples": None, "links": None, "half_z": None,
            },
            "flags": ["missing-gap"] + ([] if solid else ["short-side"]),
            "detail": (
                "缺测区两侧的稳定段尺寸可区分，但肩位落在缺测高度内，保留为缺测。"
                if solid else
                "缺测断开后一侧没有足够的稳定段，无法认定肩位，保留为缺测。"
            ),
            "units": units,
        })
    return result


def _last_segment(segments, run):
    found = [segment for segment in segments if segment["run"] == run]
    return found[-1] if found else None


def _first_segment(segments, run):
    found = [segment for segment in segments if segment["run"] == run]
    return found[0] if found else None


def _segment_window(samples, segment, edge):
    if segment is None:
        return samples[-EDGE_WINDOW:] if edge == "left" else samples[:EDGE_WINDOW]
    window = [
        row for row in samples
        if segment["start_z"]-TOLERANCE <= row["z"] <= segment["end_z"]+TOLERANCE
    ]
    return window[-EDGE_WINDOW:] if edge == "left" else window[:EDGE_WINDOW]


def _pairs(shoulders):
    result = []
    for first, second in zip(shoulders, shoulders[1:]):
        if first["state"] != "shoulder" or second["state"] != "shoulder":
            continue
        if first["z"] is None or second["z"] is None:
            continue
        interval = None
        if first["interval"] and second["interval"]:
            interval = [second["interval"][0]-first["interval"][1],
                        second["interval"][1]-first["interval"][0]]
        sample_range = None
        if first["spacing_range"] and second["spacing_range"]:
            sample_range = [second["spacing_range"][0]-first["spacing_range"][1],
                            second["spacing_range"][1]-first["spacing_range"][0]]
        result.append({
            "first": first["id"], "second": second["id"],
            "distance": _round(second["z"]-first["z"]),
            "interval": [_round(interval[0]), _round(interval[1])] if interval else None,
            "sample_range": [_round(sample_range[0]), _round(sample_range[1])] if sample_range else None,
            "spacing_range": _envelope(interval, sample_range),
            "state": "measured",
        })
    return result


def _review(shoulders):
    spans = []
    for shoulder in shoulders:
        span = shoulder["transition"]
        if shoulder["state"] == "ambiguous":
            spans.append({
                "kind": "dense", "shoulder": shoulder["id"],
                "start_z": span["start_z"], "end_z": span["end_z"],
                "span": round(span["end_z"]-span["start_z"], 7),
                "suggested_spacing": round((span["end_z"]-span["start_z"])/4, 7),
                "detail": "过渡跨越多个采样高度，一个宽倒角与两个相邻肩都能解释现有采样，需要更密的截面补测。",
            })
        elif shoulder["state"] == "missing":
            spans.append({
                "kind": "missing", "shoulder": shoulder["id"],
                "start_z": span["start_z"], "end_z": span["end_z"],
                "span": span["span"], "suggested_spacing": None,
                "detail": shoulder["detail"],
            })
    return spans


def _summary(counts):
    if counts["ambiguous"]:
        detail = f"发现 {counts['ambiguous']} 处过渡可由一个宽倒角或两个相邻肩解释，需要补测。"
        if counts["missing"]:
            detail += f"另有 {counts['missing']} 处因缺测无法认定。"
        return {"state": "review-required", "detail": detail}
    if counts["missing"] and counts["shoulders"]:
        return {
            "state": "partial",
            "detail": f"认定 {counts['shoulders']} 处肩位，另有 {counts['missing']} 处因缺测无法认定。",
        }
    if counts["missing"]:
        return {"state": "missing", "detail": f"{counts['missing']} 处肩位因缺测无法认定。"}
    if counts["shoulders"]:
        return {
            "state": "measured",
            "detail": f"由两侧稳定段尺寸的半高位置认定 {counts['shoulders']} 处肩位。",
        }
    return {"state": "no-step", "detail": "选定高度内未发现尺寸可区分且被采样到的台阶。"}


def _spacing(heights):
    ordered = sorted(float(value) for value in heights)
    gaps = [second-first for first, second in zip(ordered, ordered[1:]) if second-first > TOLERANCE]
    if not gaps:
        return None
    return {
        "spacing": _round(median(gaps)), "minimum": _round(min(gaps)),
        "maximum": _round(max(gaps)), "samples": len(ordered),
    }


def _interval(values):
    if not values:
        return None
    return [float(values[0]), float(values[1])]


def _number_list(values):
    return None if values is None else [float(values[0]), float(values[1])]


def _round(value):
    return None if value is None else round(float(value), 7)
