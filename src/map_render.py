from __future__ import annotations

import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _load_json(value, fallback):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except (TypeError, json.JSONDecodeError):
        return fallback


def _font(size: int):
    for path in (Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/simhei.ttf")):
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def render_spot_map(sample) -> Image.Image | None:
    """在本地缓存地图上绘制点列、方向和卡线；不执行联网请求。"""
    image_path = Path(str(sample.get("map_image_path") or ""))
    if not image_path.is_file():
        return None
    image = Image.open(image_path).convert("RGBA")
    calibration = _load_json(sample.get("map_calibration"), {})
    spots = _load_json(sample.get("spot_details"), [])
    if not spots:
        spots = [{"coordinate": value} for value in str(sample.get("coordinates") or "").split(" / ") if value]
    required = ("minX", "minY", "maxX", "maxY")
    if not spots or not all(key in calibration for key in required):
        return image
    min_x, min_y = float(calibration["minX"]), float(calibration["minY"])
    max_x, max_y = float(calibration["maxX"]), float(calibration["maxY"])
    width, height = image.size

    def project(coordinate: str):
        try:
            x, y = (float(part) for part in coordinate.split(":"))
        except (AttributeError, TypeError, ValueError):
            return None
        return ((x - min_x) / (max_x - min_x) * width, (max_y - y) / (max_y - min_y) * height)

    points = [(spot, project(str(spot.get("coordinate") or ""))) for spot in spots]
    points = [(spot, point) for spot, point in points if point is not None]
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    if len(points) > 1:
        draw.line([point for _, point in points], fill=(0, 235, 255, 210), width=max(3, width // 220))
    radius = max(7, width // 90)
    font = _font(max(15, width // 54))
    for index, (spot, (x, y)) in enumerate(points, start=1):
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(255, 94, 60, 235), outline="white", width=2)
        bearing = spot.get("bearingDegrees")
        if bearing is not None:
            angle = math.radians(float(bearing))
            length = radius * 3.2
            end = (x + math.sin(angle) * length, y - math.cos(angle) * length)
            draw.line((x, y, *end), fill=(255, 235, 70, 255), width=max(3, radius // 3))
        label = [f"{index}. {spot.get('coordinate', '')}"]
        if bearing is not None:
            label.append(f"方向 {float(bearing):g}°")
        if spot.get("clipMeters") is not None:
            label.append(f"卡线 {float(spot['clipMeters']):g}米")
        depth_min, depth_max = spot.get("depthMin"), spot.get("depthMax")
        if depth_min is not None:
            depth = f"{float(depth_min):g}" if depth_max is None or depth_min == depth_max else f"{float(depth_min):g}-{float(depth_max):g}"
            label.append(f"深度 {depth}米")
        text = " · ".join(label)
        box = draw.textbbox((0, 0), text, font=font)
        text_width = box[2] - box[0]
        tx = min(max(2, x + radius + 3), max(2, width - text_width - 8))
        ty = min(max(2, y - radius), height - (box[3] - box[1]) - 8)
        draw.rounded_rectangle((tx - 3, ty - 2, tx + text_width + 4, ty + box[3] - box[1] + 3), radius=4, fill=(3, 22, 29, 210))
        draw.text((tx, ty), text, font=font, fill="white")
    return Image.alpha_composite(image, overlay).convert("RGB")


def build_interactive_map_figure(sample):
    """构建仅显示标点/路线的交互地图；详细字段由点击选择后在图外展示。"""
    import plotly.graph_objects as go

    image_path = Path(str(sample.get("map_image_path") or ""))
    if not image_path.is_file():
        return None
    image = Image.open(image_path).convert("RGB")
    calibration = _load_json(sample.get("map_calibration"), {})
    spots = _load_json(sample.get("spot_details"), [])
    if not spots:
        spots = [{"coordinate": value} for value in str(sample.get("coordinates") or "").split(" / ") if value]
    required = ("minX", "minY", "maxX", "maxY")
    if not spots or not all(key in calibration for key in required):
        return None
    min_x, min_y = float(calibration["minX"]), float(calibration["minY"])
    max_x, max_y = float(calibration["maxX"]), float(calibration["maxY"])
    width, height = image.size

    def project(coordinate: str):
        try:
            x, y = (float(part) for part in coordinate.split(":"))
        except (AttributeError, TypeError, ValueError):
            return None
        return ((x - min_x) / (max_x - min_x) * width, (y - min_y) / (max_y - min_y) * height)

    projected = [(index, spot, project(str(spot.get("coordinate") or ""))) for index, spot in enumerate(spots)]
    projected = [(index, spot, point) for index, spot, point in projected if point is not None]
    figure = go.Figure()
    if len(projected) > 1 and sample.get("connect_points", True):
        figure.add_trace(go.Scatter(
            x=[point[0] for _, _, point in projected],
            y=[point[1] for _, _, point in projected],
            mode="lines",
            line={"color": "#00e5ff", "width": 3},
            hoverinfo="skip",
            showlegend=False,
        ))
    for _, spot, (x, y) in projected:
        bearing = spot.get("bearingDegrees")
        if bearing is None:
            continue
        angle = math.radians(float(bearing))
        length = max(18, width / 35)
        figure.add_shape(
            type="line",
            x0=x,
            y0=y,
            x1=x + math.sin(angle) * length,
            y1=y + math.cos(angle) * length,
            line={"color": "#ffe54c", "width": 3},
        )
    figure.add_trace(go.Scatter(
        x=[point[0] for _, _, point in projected],
        y=[point[1] for _, _, point in projected],
        mode="markers",
        marker={"size": 13, "color": "#ff5e3c", "line": {"color": "white", "width": 2}},
        selected={"marker": {"size": 18, "color": "#ffe54c"}},
        unselected={"marker": {"opacity": 0.75}},
        customdata=[[index] for index, _, _ in projected],
        hovertemplate="标点 %{pointNumber} · 点击查看详情<extra></extra>",
        showlegend=False,
    ))
    figure.add_layout_image({
        "source": image,
        "xref": "x",
        "yref": "y",
        "x": 0,
        "y": height,
        "sizex": width,
        "sizey": height,
        "xanchor": "left",
        "yanchor": "top",
        "sizing": "stretch",
        "layer": "below",
    })
    figure.update_layout(
        margin={"l": 0, "r": 0, "t": 0, "b": 0},
        height=min(760, max(460, height)),
        clickmode="event+select",
        dragmode="pan",
        hovermode="closest",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
    )
    figure.update_xaxes(visible=False, range=[0, width], fixedrange=False)
    figure.update_yaxes(visible=False, range=[0, height], scaleanchor="x", scaleratio=1, fixedrange=False)
    return figure


def spot_table(sample) -> list[dict]:
    spots = _load_json(sample.get("spot_details"), [])
    rows = []
    for index, spot in enumerate(spots, start=1):
        depth_min, depth_max = spot.get("depthMin"), spot.get("depthMax")
        depth = ""
        if depth_min is not None:
            depth = f"{float(depth_min):g}米" if depth_max is None or depth_min == depth_max else f"{float(depth_min):g}～{float(depth_max):g}米"
        rows.append({
            "序号": index,
            "坐标": spot.get("coordinate") or "",
            "方向": "" if spot.get("bearingDegrees") is None else f"{float(spot['bearingDegrees']):g}°",
            "卡线": "" if spot.get("clipMeters") is None else f"{float(spot['clipMeters']):g}米",
            "深度": depth,
            "渔获数": spot.get("catches") or 0,
        })
        if spot.get("source"):
            rows[-1].update({"来源": spot["source"], "来源链接": spot.get("sourceUrl", ""), "来源卡线标记": spot.get("clipRaw", "")})
            if spot.get("catches") is None:
                rows[-1]["渔获数"] = ""
    return rows
