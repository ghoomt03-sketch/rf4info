from __future__ import annotations

import os

import pandas as pd
import streamlit as st

from src.analytics import (
    build_recommendation_detail,
    condition_history_coverage,
    community_spot_details,
    data_source_inventory,
    history_coverage,
    historical_catches,
    latest_earnings,
    latest_map_assets,
    latest_weather,
    recent_catches,
    recommendation_horizons,
    refresh_coverage_status,
    refresh_status,
    select_earnings_sample,
    select_map_render_sample,
    weekly_evidence_details,
)
from src.config import MAPS, PUBLIC_MODE, WEATHER_ZH
from src.catalog import rf4db_fish_catalog
from src.db import ensure_database
from src.map_render import build_interactive_map_figure, spot_table
from src.user_settings import load_user_settings, save_user_settings
from src.trophy_periods import trophy_period_evidence

if not PUBLIC_MODE:
    from src.refresh_process import get_refresh_state, start_refresh, stop_refresh
    from src.refresh_reporting import SOURCE_LABELS
    from src.scheduler_status import scheduler_status
else:
    SOURCE_LABELS = {}


if os.environ.get("RF4_PAGE_CONFIGURED") != "1":
    st.set_page_config(page_title="俄钓4筛选推荐", page_icon="🎣", layout="wide")
ensure_database()


def selected_spot_index(event) -> int | None:
    selection = getattr(event, "selection", None)
    if selection is None and isinstance(event, dict):
        selection = event.get("selection", {})
    points = getattr(selection, "points", None)
    if points is None and isinstance(selection, dict):
        points = selection.get("points", [])
    if not points:
        return None
    point = points[0]
    customdata = point.get("customdata") if isinstance(point, dict) else getattr(point, "customdata", None)
    if isinstance(customdata, (list, tuple)) and customdata:
        return int(customdata[0])
    if customdata is not None:
        return int(customdata)
    return None


def window_counts_text(row, prefix: str) -> str:
    return " / ".join(f"{hours}h {int(row.get(f'{prefix}_{hours}h', 0) or 0)}" for hours in (1, 3, 6, 12, 24, 48))


RF4STAT_TROPHY_COUNT_COLUMNS = tuple(
    f"rf4stat_trophies_{hours}h" for hours in (3, 6, 12, 24, 48, 72)
)


def rf4stat_trophy_schema_ready(row) -> bool:
    return all(column in row.index for column in RF4STAT_TROPHY_COUNT_COLUMNS)


def rf4stat_trophy_counts_text(row) -> str:
    if not rf4stat_trophy_schema_ready(row):
        return "分析模块未重载，请重启本地工具"
    return " / ".join(
        f"{hours}h {int(row[f'rf4stat_trophies_{hours}h'] or 0)}"
        for hours in (3, 6, 12, 24, 48, 72)
    )


@st.cache_data(ttl=300, show_spinner=False)
def load_all():
    return latest_weather(), latest_earnings(), latest_map_assets(), recent_catches(), refresh_status()


@st.cache_data(ttl=300, show_spinner=False)
def load_recommendations(
    player_level: int,
    objective: str,
    fish_keyword: str,
    blocked_fish: tuple[str, ...],
):
    return recommendation_horizons(player_level, objective, fish_keyword, blocked_fish)


weather, earnings, map_assets, catches, status = load_all()
user_settings = load_user_settings()
if os.environ.get("RF4_SHELL_RENDERED") != "1":
    st.title("俄罗斯钓鱼4 · 筛选推荐工具")
    if PUBLIC_MODE:
        st.caption("公开只读版 · 数据由本地处理端采集、校验和发布；页面不会访问来源网站或修改数据库。")
    else:
        st.caption("当前版本先可靠缓存公开数据；温度校准、用户日上星强度和习性漂移模型将在历史样本达到门槛后启用。")

with st.sidebar:
    st.header("筛选条件")
    catalog_names = set(rf4db_fish_catalog().values())
    observed_names = set(catches["fish_name"].dropna().astype(str)) if not catches.empty else set()
    fish_options = sorted(
        name for name in catalog_names | observed_names
        if name.strip() and not name.startswith("未收录中文鱼种")
    )
    if "blocked_fish" in user_settings:
        default_blocked = [name for name in user_settings.get("blocked_fish", []) if name in fish_options]
    else:
        default_blocked = [
            name for name in ("银鲫", "金鲫", "鲤鲫", "普通鲤鱼", "镜鲤") if name in fish_options
        ]

    def persist_blocked_fish() -> None:
        updated = list(st.session_state.get("blocked_fish_selector", []))
        save_user_settings(blocked_fish=updated)
        if st.session_state.get("selected_target_fish") in set(updated):
            st.session_state["selected_target_fish"] = None

    blocked_fish = st.multiselect(
        "蹲星屏蔽鱼种",
        options=fish_options,
        default=default_blocked,
        help="修改后立即更新指定鱼种下拉候选；不改变搬砖、综合推荐或原始渔获数据。",
        key="blocked_fish_selector",
        on_change=persist_blocked_fish,
    )
    with st.form("filter_form", border=False):
        player_level = st.number_input("玩家等级", min_value=0, max_value=100, value=34, step=1)
        objective = st.selectbox("目标", ["综合推荐", "搬砖（地图+点位）", "蹲星（地图+鱼种）", "指定鱼种"])
        selected_maps = st.multiselect(
            "水域",
            options=list(MAPS),
            format_func=lambda key: f"{MAPS[key][0]}（{MAPS[key][2]}级）",
        )
        selectable_fish = [name for name in fish_options if name not in set(blocked_fish)]
        selected_fish = st.selectbox(
            "鱼种关键词（可搜索）",
            options=selectable_fish,
            index=None,
            placeholder="输入名称片段搜索，或点击箭头选择",
            help="例如输入“长”可选择长拟庸鲽、长体西鲱；下拉列表只显示未屏蔽鱼种。",
            filter_mode="contains",
            key="selected_target_fish",
        )
        fish_keyword = selected_fish or ""
        weather_filter = st.multiselect("天气", options=list(WEATHER_ZH), format_func=lambda x: WEATHER_ZH[x])
        starred_only = st.checkbox("只看星级渔获")
        coordinate_only = st.checkbox("必须有坐标")
        filters_submitted = st.form_submit_button("应用筛选", type="primary", width="stretch")
    if PUBLIC_MODE:
        st.caption("屏蔽鱼种仅在当前网页会话中生效；其余选项在点击“应用筛选”后一次生效。")
    else:
        st.caption("屏蔽鱼种会立即更新并保存；其余选项在点击“应用筛选”后一次生效。")

    st.divider()

    @st.fragment(run_every=2)
    def render_refresh_controls():
        st.subheader("数据刷新")
        refresh_state = get_refresh_state()
        st.caption("RF4-STAT帖子与上星列表随统一刷新自动采集；手动书签、剪贴板导入和单独采集按钮已退役。")
        refresh_cols = st.columns(2)
        start_clicked = refresh_cols[0].button(
            "开始刷新", disabled=refresh_state["running"], width="stretch"
        )
        stop_clicked = refresh_cols[1].button(
            "中止刷新", disabled=not refresh_state["running"], width="stretch"
        )
        if start_clicked:
            start_refresh()
            st.rerun(scope="fragment")
        if stop_clicked:
            stop_refresh()
            load_all.clear()
            load_recommendations.clear()
            st.rerun(scope="fragment")
        refresh_state = get_refresh_state()
        refresh_labels = {
            "idle": "未运行",
            "running": "后台刷新中，可继续浏览或中止",
            "completed": "刷新完成",
            "partial": "部分来源刷新失败，失败来源使用缓存",
            "cancelled": "已中止，正在使用原有缓存",
            "failed": "刷新失败，正在使用原有缓存",
        }
        st.caption(f"状态：{refresh_labels.get(refresh_state['state'], refresh_state['state'])}")
        if refresh_state["running"]:
            completed = int(refresh_state.get("completed_sources", 0))
            total = max(int(refresh_state.get("total_sources", 4)), 1)
            stage = refresh_state.get("stage", "刷新中")
            st.progress(min(completed / total, 1.0), text=f"{SOURCE_LABELS.get(stage, stage)} · {completed}/{total}")
        source_status = refresh_status()
        if not source_status.empty:
            failures = source_status[source_status["success"] == 0]
            for _, failed in failures.iterrows():
                name = SOURCE_LABELS.get(failed["source"], failed["source"])
                last_ok = failed.get("last_success_at")
                cache_note = f"最近成功：{pd.Timestamp(last_ok).strftime('%m-%d %H:%M')}；正在使用旧缓存。" if pd.notna(last_ok) else "尚无成功缓存。"
                st.warning(f"{name} 刷新失败。{cache_note}")
                reason = str(failed["message"] or "未返回具体错误").split("地址：", 1)[0]
                st.caption(f"原因：{reason}")
            with st.expander("各来源刷新详情", expanded=False):
                overview = source_status.copy()
                overview["source"] = overview["source"].map(SOURCE_LABELS).fillna(overview["source"])
                overview["success"] = overview["success"].map({1: "成功", 0: "失败"})
                overview["last_success_at"] = overview["last_success_at"].fillna("尚无成功缓存")
                overview = overview.rename(columns={
                    "source": "来源", "finished_at": "最近尝试", "success": "结果",
                    "row_count": "记录数", "last_success_at": "最近成功", "message": "原因",
                })
                st.dataframe(overview[["来源", "结果", "最近尝试", "最近成功", "记录数", "原因"]], hide_index=True, width="stretch")
        interval_status = refresh_coverage_status()
        if not interval_status.empty:
            status_labels = {
                "complete": "完整", "backfilling": "补采中", "gap": "存在缺口",
                "snapshot": "快照源", "unknown": "等待判断",
            }
            stream_labels = {
                "rf4db_catches_normal": "RF4DB 普通渔获",
                "rf4db_catches_star": "RF4DB 带条件星鱼",
                "rf4db_catches_normal_history": "RF4DB 普通渔获旧缺口",
                "rf4db_weather": "RF4DB 天气",
                "rf4db_earnings": "RF4DB 收益",
                "rf4db_map_assets": "RF4DB 地图资源",
                "rf4stat_activity": "RF4-STAT 活跃渔获",
                "rf4stat_posts": "RF4-STAT 帖子",
                "rf4map_points": "RF4MAP 点位",
                "official_weekly_cn": "中国区普通周榜",
            }
            compact = interval_status.copy()
            compact["stream"] = compact["stream"].map(stream_labels).fillna(compact["stream"])
            compact["status"] = compact["status"].map(status_labels).fillna(compact["status"])
            with st.expander("刷新区间完整性", expanded=True):
                for _, row in compact.iterrows():
                    icon = {"完整": "✅", "补采中": "🔄", "存在缺口": "⚠️", "快照源": "📷"}.get(row["status"], "◻️")
                    st.caption(f"{icon} {row['stream']}：{row['status']} · {row['note']}")
        seen_run_id = st.session_state.get("seen_refresh_run_id", 0)
        run_id = int(refresh_state.get("run_id", 0))
        if run_id > seen_run_id and refresh_state["state"] in {"completed", "partial", "failed", "cancelled"}:
            load_all.clear()
            load_recommendations.clear()
            st.session_state["seen_refresh_run_id"] = run_id
            st.rerun()

    if PUBLIC_MODE:
        st.subheader("公开数据")
        st.caption("云端仅展示已经发布的只读快照；数据采集、刷新和历史补采在发布主机上完成。")
    else:
        render_refresh_controls()

latest_text = "暂无缓存"
if not status.empty:
    successful = status[status["success"] == 1]
    if not successful.empty:
        latest_text = pd.to_datetime(successful["finished_at"], errors="coerce").max().strftime("%Y-%m-%d %H:%M")
data_location_label = "公开数据" if PUBLIC_MODE else "本地数据"
st.caption(f"{data_location_label}最近成功更新：{latest_text} · 数据仅作出发前参考")

tab_now, tab_catches, tab_income, tab_weather, tab_status = st.tabs(
    ["当前/下一小时", "渔获筛选", "收益榜", "天气时间轴", "数据状态与方法"]
)

with tab_now:
    if weather.empty:
        if PUBLIC_MODE:
            st.info("当前公开快照尚无天气数据，请等待发布端更新。")
        else:
            st.info("尚无天气缓存。点击左侧“联网刷新全部数据”，或运行 refresh_data.py。")
    else:
        recommendation_frames = load_recommendations(
            int(player_level), objective, fish_keyword, tuple(blocked_fish)
        )
        now_col, next_col = st.columns(2)
        for container, horizon, title in ((now_col, 0, "当前小时去哪"), (next_col, 1, "下一小时去哪")):
            with container:
                st.subheader(title)
                rec = recommendation_frames[horizon]
                if selected_maps:
                    rec = rec[rec["map_id"].isin(selected_maps)]
                if weather_filter:
                    rec = rec[rec["weather_code"].isin(weather_filter)]
                if rec.empty:
                    st.info("当前条件下没有可用地图。")
                else:
                    top = rec.iloc[0]
                    slot = pd.to_datetime(top["forecast_slot"], errors="coerce")
                    st.metric(top["recommendation_label"], f"{top['天气']} · {top['forecast_day_temperature']:.0f}°C", f"推荐分 {top['score']:.1f}")
                    if objective in ("搬砖收益", "搬砖（地图+点位）"):
                        st.write(
                            f"点位分享收益约 **{top['reference_income']:.0f} 银/小时**；代表收益小时共 **{int(top['recent_catches'])}** 尾、"
                            f"包含 **{int(top.get('point_shared_species', 0))}** 个鱼种；地图普通流当前小时 **{int(top['current_hour_catches'])}** 条。"
                        )
                    elif objective in ("蹲星", "蹲星（地图+鱼种）", "指定鱼种"):
                        baseline = top.get("normal_forecast_temperature")
                        baseline_text = f"{float(baseline):.1f}°C" if pd.notna(baseline) else "暂无"
                        st.write(
                            f"RF4-STAT本周上星更新 **{rf4stat_trophy_counts_text(top)}**；"
                            f"当前温档 **{top.get('temperature_regime', '暂无常温基线')}**，地图预报常温中位数 **{baseline_text}**；"
                            f"评分分项：近期上星 **{float(top.get('rf4stat_trophy_score', 0)):.1f}**、"
                            f"点位 **{float(top.get('point_evidence_score', 0)):.1f}**、RF4DB周榜背景 **{float(top.get('weekly_background_score', 0)):.1f}**、"
                            f"普通鱼背景 **{float(top.get('ordinary_background_score', 0)):.1f}**、天气温度 **{float(top.get('condition_match_score', 0)):.1f}**；"
                            f"可信度：**{top['confidence']}**。"
                        )
                    else:
                        st.write(
                            f"收益参考：约 **{top['reference_income']:.0f} 银/小时**（{top['income_scope']}）；"
                            f"普通流当前小时 **{int(top['current_hour_catches'])}** 条、此前3小时 **{int(top['previous_3h_catches'])}** 条，"
                            f"状态 **{top['activity_status']}**；1/3/6/12/24/48小时普通渔获 **{window_counts_text(top, 'catches')}**；"
                            f"RF4DB已观测星鱼 **{window_counts_text(top, 'stars')}**；可信度：**{top['confidence']}**。"
                        )
                    st.caption(f"排序依据：{top['score_basis']}。公开展示流不是完整鱼口样本。")
                    if pd.notna(slot):
                        scope_note = "收益来自点位分享的一小时样本，不代表持续保底" if objective in ("搬砖收益", "搬砖（地图+点位）") else "收益为地图级样本，不等同于某坐标保底收益"
                        st.caption(f"预报窗口：{slot.strftime('%Y-%m-%d %H:00')} · {scope_note}。")
        st.divider()
        st.subheader("候选地图")
        if objective in ("蹲星", "蹲星（地图+鱼种）", "指定鱼种"):
            with st.expander("查看蹲星临时推荐分公式"):
                st.code(
                    "A = 0.35×S3/3 + 0.22×S6/6 + 0.16×S12/12 + 0.12×S24/24 + 0.09×S48/48 + 0.06×S72/72\n"
                    "推荐分 = 65×A/(A+0.35) + 10×P/(P+2) + 10×W/(W+3) + 5×B/(B+2) + C",
                    language="text",
                )
                st.caption(
                    "S3…S72 为 RF4-STAT 本周上星记录在对应现实小时窗口的新增数；P 为近期公开有效点位数；"
                    "W 为 RF4DB 镜像普通周榜按目录门槛推定达标数；B 为 RF4DB 普通流多窗口加权小时速率；"
                    "C 为正式天气温度匹配分。当前用户日暴露和温度校准未达门槛，所以 C=0。"
                )
        rec = recommendation_frames[0]
        if selected_maps:
            rec = rec[rec["map_id"].isin(selected_maps)]
        if weather_filter:
            rec = rec[rec["weather_code"].isin(weather_filter)]
        view = rec.rename(columns={
            "recommendation_label": "推荐项", "map_name": "地图", "天气": "天气", "forecast_day_temperature": "预报参考温度",
            "wind_speed": "风速", "reference_income": "收益参考", "income_scope": "收益证据范围", "recent_catches": "近期渔获样本",
            "recent_stars": "星鱼/点位样本", "current_hour_catches": "当前小时普通渔获", "previous_3h_catches": "此前3小时普通渔获",
            "top3_baits": "6小时热门饵Top3",
            "weekly_record_count": "同地图鱼种周榜记录",
            "weekly_inferred_trophies": "RF4DB周榜推定达标",
            "weekly_threshold_known": "周榜门槛已知",
            "community_records": "社区交叉记录", "community_points": "社区有效点位",
            "community_sources": "社区来源",
            "point_shared_species": "收益小时鱼种数",
            "activity_status": "活跃状态", "score": "临时推荐分", "confidence": "可信度",
            "normal_forecast_temperature": "地图预报常温中位数",
            "temperature_baseline_samples": "常温基线时点数", "temperature_regime": "当前温档",
            "rf4stat_trophy_score": "RF4-STAT近期上星分", "point_evidence_score": "点位证据分",
            "weekly_background_score": "RF4DB周榜背景分", "ordinary_background_score": "普通鱼背景分",
            "condition_match_score": "天气温度匹配分",
        })
        if not view.empty:
            view["1/3/6/12/24/48小时普通渔获"] = rec.apply(lambda row: window_counts_text(row, "catches"), axis=1)
            view["RF4DB 1/3/6/12/24/48小时已观测星鱼"] = rec.apply(lambda row: window_counts_text(row, "stars"), axis=1)
            view["RF4-STAT本周上星3/6/12/24/48/72小时更新"] = rec.apply(rf4stat_trophy_counts_text, axis=1)
        if objective in ("搬砖收益", "搬砖（地图+点位）"):
            view = view.rename(columns={"近期渔获样本": "代表小时尾数"})
            columns = ["推荐项", "天气", "预报参考温度", "风速", "收益参考", "收益证据范围", "代表小时尾数", "收益小时鱼种数", "当前小时普通渔获", "社区交叉记录", "社区有效点位", "社区来源", "活跃状态", "临时推荐分", "可信度", "source_url"]
        elif objective in ("蹲星", "蹲星（地图+鱼种）", "指定鱼种"):
            columns = ["推荐项", "天气", "预报参考温度", "地图预报常温中位数", "当前温档",
                       "RF4-STAT本周上星3/6/12/24/48/72小时更新", "RF4-STAT近期上星分", "点位证据分",
                       "RF4DB周榜背景分", "普通鱼背景分", "天气温度匹配分", "RF4DB周榜推定达标",
                       "社区有效点位", "社区来源", "临时推荐分", "可信度", "source_url"]
        else:
            columns = ["推荐项", "天气", "预报参考温度", "风速", "收益参考", "收益证据范围", "当前小时普通渔获", "此前3小时普通渔获", "活跃状态", "1/3/6/12/24/48小时普通渔获", "RF4DB 1/3/6/12/24/48小时已观测星鱼", "6小时热门饵Top3", "同地图鱼种周榜记录", "周榜门槛已知", "RF4DB周榜推定达标", "社区交叉记录", "社区有效点位", "社区来源", "星鱼/点位样本", "临时推荐分", "可信度", "source_url"]
        st.dataframe(view[[c for c in columns if c in view]], width="stretch", hide_index=True)
        if objective in ("蹲星", "蹲星（地图+鱼种）", "指定鱼种"):
            st.warning("蹲星天气温度匹配分当前为0：用户日暴露、预报温度校准和行为阶段漂移尚未满足正式启用门槛。低温/常温/高温只表示该地图当前预报相对自身历史中位数的位置，不等于鱼种已确认偏好。")
        else:
            st.warning("临时推荐分尚未使用用户日上星强度、温度校准和行为阶段漂移；历史积累达到开发文档门槛后才会启用正式模型。")

        st.subheader("候选地图详情")
        st.caption("从候选列表选择一张地图，展开查看推荐原因、目标鱼种天气/温度观察、公开点位和渔获内容。")
        detail_options = [None] + rec["candidate_id"].tolist()
        detail_names = rec.set_index("candidate_id")["recommendation_label"].to_dict()
        detail_candidate_id = st.selectbox(
            "详情选项",
            detail_options,
            format_func=lambda value: "请选择地图" if value is None else detail_names.get(value, value),
            key="candidate_detail_map",
        )
        if detail_candidate_id is not None:
            detail_row = rec[rec["candidate_id"] == detail_candidate_id].iloc[0]
            is_farm_detail = objective in ("搬砖收益", "搬砖（地图+点位）")
            detail_fish = "" if is_farm_detail else str(detail_row.get("target_fish") or fish_keyword).strip()
            detail_point = str(detail_row.get("point_label") or "").strip()
            detail_catches_source = historical_catches(detail_row["map_id"])
            detail = build_recommendation_detail(detail_catches_source, detail_row["map_id"], detail_fish, detail_point, earnings)
            target_text = f"目标鱼种“{detail_fish}”" if detail_fish else "该地图近期主要鱼种"
            with st.expander(f"{detail_row['recommendation_label']} · 详情", expanded=True):
                st.markdown("#### 推荐原因")
                reasons = [
                    f"当前游戏日天气为 **{detail_row['天气']}**，预报参考温度 **{detail_row['forecast_day_temperature']:.0f}°C**，风速 **{detail_row['wind_speed']:.1f}**。",
                    f"本次按“**{detail_row['score_basis']}**”排序，临时推荐分 **{detail_row['score']:.1f}**，可信度 **{detail_row['confidence']}**。",
                ]
                if objective in ("蹲星", "蹲星（地图+鱼种）", "指定鱼种"):
                    baseline = detail_row.get("normal_forecast_temperature")
                    baseline_text = f"{float(baseline):.1f}°C" if pd.notna(baseline) else "暂无"
                    reasons.extend([
                        f"RF4-STAT本周上星更新窗口为 **{rf4stat_trophy_counts_text(detail_row)}**；这些数字按每条记录自带的现实上报时间计算。",
                        f"评分分项：RF4-STAT近期上星 **{float(detail_row.get('rf4stat_trophy_score', 0)):.1f}**、公开点位 **{float(detail_row.get('point_evidence_score', 0)):.1f}**、RF4DB镜像普通周榜背景 **{float(detail_row.get('weekly_background_score', 0)):.1f}**、普通鱼开口背景 **{float(detail_row.get('ordinary_background_score', 0)):.1f}**、天气温度匹配 **{float(detail_row.get('condition_match_score', 0)):.1f}**。",
                        f"该地图历史预报常温中位数为 **{baseline_text}**，当前标记 **{detail_row.get('temperature_regime', '暂无常温基线')}**；这是地图温档，不是目标鱼种已确认偏好。",
                    ])
                if is_farm_detail:
                    reasons.extend([
                        f"本候选来自 **{detail_point}** 的点位分享收益小时，代表样本共 **{int(detail_row['recent_catches'])}** 尾、约 **{detail_row['reference_income']:.0f} 银/小时**。",
                        "搬砖点位、鱼种比例和评分只使用收益小时明细；下方跨来源近期样本仅作背景，不改变该收益样本比例。",
                    ])
                else:
                    reasons.append(
                        f"{target_text}累计对齐 **{int(detail['sample_count'])}** 条公开证据，来源构成为 **{detail.get('source_breakdown', '暂无')}**；"
                        f"有效天气 **{int(detail.get('weather_sample_count', 0))}**、游戏时间 **{int(detail.get('game_time_sample_count', 0))}**、即时温度 **{int(detail.get('temperature_sample_count', 0))}** 条，"
                        f"公开坐标 **{int(detail['point_count'])}** 个。另有同地图鱼种周榜 **{int(detail_row.get('weekly_record_count', 0))}** 条，仅在详细板块独立展示。"
                    )
                window_summary = detail.get("window_summary")
                if isinstance(window_summary, pd.DataFrame) and not window_summary.empty:
                    window_text = "；".join(
                        f"{row['时间窗口']}普通/帖子 **{int(row['普通/帖子样本'])}**、上星 **{int(row['上星样本'])}**"
                        for _, row in window_summary.iterrows()
                    )
                    reasons.append(
                        f"{detail.get('reason_scope', '跨来源公开证据')}合并窗口：{window_text}。"
                        "按各来源稳定记录键去重；帖子按发布时间进入窗口，数量不是独立渔获数或真实咬口率。"
                    )
                else:
                    reasons.append(f"{detail.get('reason_scope', '跨来源公开证据')}当前没有带可用时间的近期样本。")

                reason_baits = detail.get("reason_baits")
                if isinstance(reason_baits, pd.DataFrame) and not reason_baits.empty:
                    bait_text = "；".join(
                        f"{row['鱼饵组合']}（{row['组合完整度']}，钓组：{row['钓组']}，{int(row['合计样本'])}条）"
                        for _, row in reason_baits.iterrows()
                    )
                    reasons.append(f"按最近7天组合完整度优先、同级再按公开样本数排序的鱼饵组合 Top 3：**{bait_text}**。周榜只参与鱼饵证据，不进入小时窗口；组合不拆分，仅作尝试顺序。")

                reason_points = detail.get("reason_points")
                if isinstance(reason_points, pd.DataFrame) and not reason_points.empty:
                    point_items = []
                    for _, row in reason_points.iterrows():
                        clip_note = f"，卡线 {row['卡线']}" if row.get("卡线") else ""
                        point_items.append(f"{row['点位']}（{row['点位口径']}，{int(row['样本'])}条{clip_note}）")
                    point_text = "；".join(point_items)
                    reasons.append(f"按合并公开样本数排序的点位 Top 3：**{point_text}**。这是公开样本排序，不代表稳定出鱼点。")
                if detail_point:
                    reasons.append(f"本候选点位为 **{detail_point}**；挪威海的“工具网格”是临时空间聚类，不是游戏官方坑位名称。")
                preferences = detail["preferences"]
                if not is_farm_detail and isinstance(preferences, pd.DataFrame) and not preferences.empty:
                    leading = preferences.iloc[0]
                    if int(leading.get("有效天气样本", 0)):
                        weather_relation = "一致" if leading["weather_code"] == detail_row["weather_code"] else "不一致"
                        reasons.append(
                            f"样本最多的鱼种为 **{leading['鱼种']}**；跨来源有效天气样本中出现最多的是 **{leading['出现最多天气']}**，与当前天气 **{weather_relation}**。"
                        )
                    else:
                        reasons.append(f"样本最多的鱼种为 **{leading['鱼种']}**；当前没有可用的天气字段或可匹配历史天气快照。")
                    if int(leading.get("温度样本", 0)):
                        reasons.append(
                            f"其渔获发生时的即时温度常见范围为 **{leading['即时温度常见范围（P25-P75）']}**。当前显示的 {detail_row['forecast_day_temperature']:.0f}°C 是游戏日预报参考温度，校准完成前不直接判定温度匹配。"
                        )
                    else:
                        reasons.append("当前没有有效即时温度样本；已匹配到的天气预报参考温度不会冒充渔获即时温度。")
                elif not is_farm_detail:
                    reasons.append(f"{target_text}当前没有可按稳定地图和鱼种对齐的公开证据记录。")
                bait_recommendations = detail.get("bait_recommendations")
                st.markdown("\n".join(f"- {reason}" for reason in reasons))

                st.markdown("#### 当前推荐鱼饵")
                if isinstance(bait_recommendations, pd.DataFrame) and not bait_recommendations.empty:
                    st.dataframe(bait_recommendations, width="stretch", hide_index=True)
                    if is_farm_detail:
                        st.caption("仅针对该收益小时鱼种构成中的主要鱼种分别列出，每种最多8项。证据来自同地图最近7天的明确上星记录、周榜与RF4-STAT帖子；完整组合优先于来源只提供单项的记录。不参与搬砖评分，也不代表该坐标或工具网格的稳定配方。")
                    else:
                        st.caption("按同地图、同鱼种最近7天统计，每套饵料组合与钓组在同一来源记录内只计一次；完整组合优先，再参考明确上星、周榜和RF4-STAT帖子。周榜日期不进入小时、天气或游戏时间分析；旧版单项记录不会被跨记录拼成组合。每种最多8项，原始次数不是咬口率。")
                else:
                    st.info("该地图目标鱼种最近7天暂无可用鱼饵证据；不会用长期历史或其他鱼种自动填补。")

                if not is_farm_detail:
                    if detail_fish and detail_row.get('fish_id'):
                        st.markdown('#### 上星游戏时段样本占比')
                        periods = trophy_period_evidence(detail_row['map_id'], detail_row['fish_id'])
                        st.caption('按同地图、同鱼种汇集RF4DB明确星鱼游戏时间与RF4-STAT明确上星记录；RF4-STAT先按鱼种搏鱼耗时回推实际中鱼时间，再换算游戏时间。近期与历史分开，普通帖子发布时间不参与。')
                        for block in periods['blocks']:
                            with st.expander(f"{block['title']} · 有效{block['eligible']}条 · {block['status']}", expanded=block['title'] == '近期7天'):
                                if block['eligible']:
                                    st.write(f"原始样本数最多时段：{block['leaders']}；仅作分布观察，不判定真实上星概率。")
                                else:
                                    st.info('没有有效上星游戏时间，暂无时段占比。')
                                st.dataframe(block['table'].rename(columns={'样本占比_pct': '样本占比（%）'}), width='stretch', hide_index=True)
                                st.caption(f"游戏时间缺失/无效排除 {block['missing']} 条；该阶段记录时间（UTC）：{block['start']} ～ {block['end']}。")
                        st.caption(f"捕获日期缺失/无效/未来异常另排除 {periods['excluded_date']} 条。来源构成：{periods.get('source_breakdown', '暂无')}。早晨、午间各6小时，傍晚4小时，夜间8小时；每游戏小时样本数只是按时段长度归一，并非每小时作钓上星率。分母缺少各时段作钓暴露，不参与推荐评分。来源：{periods['source_url']}")
                        st.info(f"本地图鱼种已检查全部缓存的RF4-STAT帖子：{periods['post_count']}篇。当前帖子无逐鱼种上星标记及实际捕获游戏时间，不能生成帖子上星时段占比，也不将发帖时间替代捕获时间。")
                    weekly_details = weekly_evidence_details(detail_row['map_id'], detail_row.get('fish_id', ''))
                    st.markdown('#### 同地图鱼种周榜证据（独立来源）')
                    if weekly_details.empty:
                        st.caption('当前周榜缓存没有该地图鱼种的可对齐记录；不等于游戏没有星鱼。')
                    else:
                        known = int(weekly_details['目录奖杯线kg'].notna().sum())
                        inferred = int((weekly_details['达标判定'] == '按重量推定达标').sum())
                        st.write(f'周榜 {len(weekly_details)} 条；门槛已知 {known} 条；按目录重量门槛推定达标 {inferred} 条；门槛未知 {len(weekly_details)-known} 条。')
                        st.dataframe(weekly_details, width='stretch', hide_index=True)
                    st.caption('镜像仅覆盖中国区普通纪录本周榜，榜单日期不是精确捕获时间。推定达标不是榜单直接上星标记；不与RF4DB星鱼或RF4-STAT社区证据相加，不推断坐标、天气或推荐准确率。')

                if not is_farm_detail:
                    st.markdown("#### 目标鱼种天气与温度观察")
                if not is_farm_detail and isinstance(preferences, pd.DataFrame) and not preferences.empty:
                    preference_columns = [
                        "鱼种", "公开证据记录", "来源构成", "有效天气样本", "出现最多天气", "该天气样本",
                        "相对地图背景指数", "游戏时间样本", "即时温度常见范围（P25-P75）", "温度样本",
                        "历史覆盖天数", "天气结论", "温度结论",
                    ]
                    st.dataframe(preferences[preference_columns], width="stretch", hide_index=True)
                    st.info("推荐详情按全部本地来源、按字段独立准入：有可靠上鱼时间的RF4-STAT记录会先回推中鱼时间并匹配当时已缓存的天气；缺天气只排除天气分析，缺即时温度只排除温度分析，坐标、饵料和游戏时间仍可分别参与。不同来源不会互相拼字段；缺少用户日作钓暴露时，观察分布不能称为已确认喜好。")
                elif not is_farm_detail:
                    st.info("当前地图和鱼种没有可按稳定地图和鱼种对齐的公开证据记录。")

                if is_farm_detail:
                    st.markdown("#### 搬砖鱼种构成")
                    earnings_species = detail["earnings_species"]
                    if isinstance(earnings_species, pd.DataFrame) and not earnings_species.empty:
                        st.dataframe(earnings_species, width="stretch", hide_index=True)
                        st.caption("鱼种尾数与数量占比来自该点位的一小时收益明细；价值占比是各鱼种对该小时估算总价值的贡献，不等于尾数占比。")
                    else:
                        st.info("当前点位暂无可解析的收益小时鱼种明细。")
                st.markdown("#### 地图与点位标注")
                map_sample = (
                    select_earnings_sample(earnings, detail_row["map_id"], detail_point)
                    if is_farm_detail
                    else select_map_render_sample(map_assets, earnings, detail_row["map_id"])
                )
                if map_sample is not None:
                    if not is_farm_detail:
                        point_rows = detail.get("points")
                        map_sample = map_sample.copy()
                        map_sample["spot_details"] = "[]"
                        map_sample["coordinates"] = ""
                        if isinstance(point_rows, pd.DataFrame) and not point_rows.empty:
                            coordinate_rows = point_rows[point_rows["坐标"].astype(str).str.strip().ne("")]
                            map_sample["spot_details"] = coordinate_rows.rename(columns={"坐标": "coordinate"})[["coordinate"]].to_json(orient="records", force_ascii=False)
                    community_map = community_spot_details(detail_row["map_id"], detail_fish) if not is_farm_detail else pd.DataFrame()
                    if not is_farm_detail:
                        import json
                        map_sample = map_sample.copy()
                        map_sample["connect_points"] = False
                        existing_spots = json.loads(map_sample.get("spot_details") or "[]")
                        if not community_map.empty:
                            for _, point in community_map[community_map["坐标"].notna()].drop_duplicates(["来源", "坐标"]).iterrows():
                                existing_spots.append({"coordinate": point["坐标"], "source": point["来源"], "sourceUrl": point["来源链接"], "clipRaw": point["卡线"] or ""})
                        map_sample["spot_details"] = json.dumps(existing_spots, ensure_ascii=False)
                    map_figure = build_interactive_map_figure(map_sample)
                    if map_figure is not None:
                        map_event = st.plotly_chart(
                            map_figure,
                            key=f"recommendation_map_{detail_candidate_id}",
                            width="stretch",
                            on_select="rerun",
                            selection_mode="points",
                            config={"displayModeBar": False, "scrollZoom": True},
                        )
                        map_spots = spot_table(map_sample)
                        selected_index = selected_spot_index(map_event)
                        if selected_index is not None and 0 <= selected_index < len(map_spots):
                            st.markdown("##### 选中标点详情")
                            st.dataframe(pd.DataFrame([map_spots[selected_index]]), width="stretch", hide_index=True)
                        else:
                            st.info("点击地图上的红色标点，查看该点坐标、方向、卡线、深度和渔获数。")
                        st.caption("方向角以地图正北为 0°；图面只显示标点、路线和方向指示，详细文字仅在点击标点后显示。")
                    else:
                        st.info("该地图底图已缓存，但当前候选只有坑位名称或缺少可投影的 x:y 坐标，暂无地图标点。")
                else:
                    st.info("该地图资源尚未缓存；完成一次统一刷新后即可显示有坐标的公开点位。")
                if not is_farm_detail:
                    st.markdown("#### RF4MAP / RF4-STAT 社区证据")
                    community_detail = community_spot_details(detail_row["map_id"], detail_fish)
                    if isinstance(community_detail, pd.DataFrame) and not community_detail.empty:
                        st.dataframe(community_detail, width="stretch", hide_index=True)
                        st.caption("字段已转换为当前统一格式。具有可靠上鱼时间的RF4-STAT渔获可回推游戏时间并匹配历史天气；帖子发布时间、周榜日期和RF4MAP快照不能冒充上鱼时间。缺少某字段只排除对应维度，也不参与缺少有效用户日分母的上星强度。")
                    else:
                        st.info("当前候选暂无可匹配的 RF4MAP / RF4-STAT 社区证据。")

                    st.markdown("#### 公开点位")
                    points = detail["points"]
                    if isinstance(points, pd.DataFrame) and not points.empty:
                        st.dataframe(points, width="stretch", hide_index=True)
                        st.caption("点位汇集各公开来源中实际提供的坐标；缺少天气或温度不影响点位字段参与，但不同记录之间不会互相补字段。点位证据不参与搬砖收益归因。")
                    else:
                        st.info("当前条件下暂无带坐标的公开渔获。")

                    st.markdown("#### 近期公开渔获")
                    detail_catches = detail["catches"]
                    if isinstance(detail_catches, pd.DataFrame) and not detail_catches.empty:
                        st.dataframe(detail_catches, width="stretch", hide_index=True)
                    else:
                        st.info("当前条件下暂无公开渔获内容。")

with tab_catches:
    frame = catches.copy()
    if not frame.empty:
        frame = frame[frame["unlock_level"].fillna(999) <= player_level]
        if selected_maps:
            frame = frame[frame["map_id"].isin(selected_maps)]
        if fish_keyword.strip():
            key = fish_keyword.strip()
            frame = frame[frame["fish_name"].str.casefold().eq(key.casefold())]
        if weather_filter:
            wanted = {WEATHER_ZH[x] for x in weather_filter} | set(weather_filter)
            frame = frame[frame["weather_code"].isin(wanted)]
        if starred_only:
            frame = frame[frame["fish_grade"] != "normal"]
        if coordinate_only:
            frame = frame[frame["coordinate"].notna() & ~frame["coordinate"].isin(["", "��", "—"])]
        display = frame.rename(columns={
            "fish_name": "鱼种", "map_name": "地图", "coordinate": "坐标", "game_time": "游戏时间",
            "catch_temperature": "即时温度", "weather_code": "天气", "rig": "钓组", "baits": "饵料",
            "fish_grade": "等级", "caught_at": "捕获时间", "source_url": "来源",
        })
        st.caption(f"符合条件：{len(display)} 条公开渔获。普通流用于地图+鱼种活跃度，星鱼流用于点位/条件证据；即时温度不会直接当作天气预报的游戏日参考温度。")
        cols = ["鱼种", "地图", "坐标", "游戏时间", "即时温度", "天气", "钓组", "饵料", "等级", "捕获时间", "来源"]
        st.dataframe(display[[c for c in cols if c in display]], width="stretch", hide_index=True, height=600)
    else:
        st.info("暂无渔获缓存。")

with tab_income:
    frame = earnings.copy()
    if not frame.empty:
        frame = frame[frame["unlock_level"].fillna(999) <= player_level]
        if selected_maps:
            frame = frame[frame["map_id"].isin(selected_maps)]
        display = frame.rename(columns={
            "rank_no": "排名", "map_name": "地图", "silver_per_hour": "估算银/小时",
            "catch_count": "尾数", "coverage_pct": "估值覆盖率", "composition": "收益构成",
            "collected_at": "采集时间", "source_url": "来源",
        })
        st.dataframe(display[["排名", "地图", "估算银/小时", "尾数", "估值覆盖率", "收益构成", "采集时间", "来源"]], width="stretch", hide_index=True, height=600)
        st.info("此表是地图级匿名玩家小时样本；高收益可能由稀有鱼抬高，不能直接作为某个点位的保证收益。")
    else:
        st.info("暂无收益缓存。")

with tab_weather:
    frame = weather.copy()
    if not frame.empty:
        frame = frame[frame["unlock_level"] <= player_level]
        if selected_maps:
            frame = frame[frame["map_id"].isin(selected_maps)]
        if weather_filter:
            frame = frame[frame["weather_code"].isin(weather_filter)]
        display = frame.rename(columns={
            "map_name": "地图", "horizon": "D", "forecast_slot": "现实窗口",
            "天气": "天气", "forecast_day_temperature": "预报参考温度", "wind_speed": "风速",
            "collected_at": "采集时间", "source_url": "来源",
        })
        st.dataframe(display[["地图", "D", "现实窗口", "天气", "预报参考温度", "风速", "采集时间", "来源"]], width="stretch", hide_index=True, height=650)
        st.caption("预报参考温度与渔获即时温度分开保存；温度日内曲线尚未达到启用门槛。")
    else:
        st.info("暂无天气缓存。")

with tab_status:
    st.subheader("数据源接入核验")
    source_inventory = data_source_inventory().rename(columns={
        "source": "数据源", "integration_status": "接入状态", "local_rows": "本地记录数",
        "coverage": "实际覆盖", "usage": "当前用途",
    })
    st.dataframe(source_inventory, width="stretch", hide_index=True)
    st.caption("“已接入”要求同时存在采集器、数据库写入和刷新任务；仅写在开发计划中的来源显示为“尚未接入”。")
    if not PUBLIC_MODE:
        st.subheader("自动刷新计划")
        schedule = scheduler_status().rename(columns={
            "scope": "范围", "interval_minutes": "间隔（分钟）", "installed": "已安装",
            "run_mode": "运行方式",
            "state": "任务状态", "next_run": "下次运行", "last_run": "上次运行",
            "last_result": "上次结果码",
        })
        st.dataframe(schedule[["范围", "间隔（分钟）", "已安装", "运行方式", "任务状态", "下次运行", "上次运行", "上次结果码"]], width="stretch", hide_index=True)
        st.caption("计划任务仅在Windows当前用户会话可运行时启动；电脑关机或休眠期间不会采集，恢复后由系统尽快补触发。任务重叠时跳过本次，不并发访问来源。")
    interval_status = refresh_coverage_status()
    if not interval_status.empty:
        status_labels = {
            "complete": "完整", "backfilling": "补采中", "gap": "存在缺口",
            "snapshot": "快照源", "unknown": "等待判断",
        }
        stream_labels = {
            "rf4db_catches_normal": "RF4DB 普通渔获", "rf4db_catches_star": "RF4DB 带条件星鱼",
            "rf4db_catches_normal_history": "RF4DB 普通渔获旧缺口",
            "rf4db_weather": "RF4DB 天气", "rf4db_earnings": "RF4DB 收益",
            "rf4db_map_assets": "RF4DB 地图资源",
            "rf4stat_activity": "RF4-STAT 活跃渔获", "rf4stat_posts": "RF4-STAT 帖子",
            "rf4map_points": "RF4MAP 点位", "official_weekly_cn": "中国区普通周榜",
        }
        interval_display = interval_status.copy()
        interval_display["stream"] = interval_display["stream"].map(stream_labels).fillna(interval_display["stream"])
        interval_display["status"] = interval_display["status"].map(status_labels).fillna(interval_display["status"])
        interval_display = interval_display.rename(columns={
            "stream": "数据流", "status": "区间状态", "previous_success_at": "上次成功",
            "earliest_fetched_at": "本批最早事件", "latest_fetched_at": "本批最新事件",
            "fetched_rows": "本批记录", "overlap_rows": "既有ID重叠", "note": "判断说明",
            "updated_at": "判断时间",
        })
        st.subheader("刷新区间完整性")
        st.dataframe(interval_display[["数据流", "区间状态", "上次成功", "本批最早事件", "本批最新事件", "本批记录", "既有ID重叠", "判断说明", "判断时间"]], width="stretch", hide_index=True)
        st.caption("“完整”只表示本批已遇到上次保存的稳定ID，不表示来源全部历史完整；快照源无法恢复刷新间隔内出现后又消失的内容。")
    if not status.empty:
        display = status.rename(columns={
            "source": "来源", "finished_at": "完成时间", "success": "成功", "row_count": "记录数", "message": "信息"
        })
        st.dataframe(display[["来源", "完成时间", "成功", "记录数", "信息"]], width="stretch", hide_index=True)
    st.subheader("历史样本覆盖")
    coverage = history_coverage()
    if not coverage.empty:
        coverage["数据流"] = coverage["data_stream"].map({"normal": "普通渔获流", "star": "星鱼流"}).fillna(coverage["data_stream"])
        coverage = coverage.rename(columns={
            "sample_count": "本地样本", "oldest_record": "最早记录", "newest_record": "最新记录",
            "map_count": "地图数", "fish_count": "鱼种数",
        })
        st.dataframe(coverage[["数据流", "本地样本", "最早记录", "最新记录", "地图数", "鱼种数"]], width="stretch", hide_index=True)
    st.caption("实时刷新只负责最近窗口；30/90 日习性模型使用本地持续累积及可续传历史补采。未达到实际覆盖天数前仍会显示数据不足。")
    condition_coverage = condition_history_coverage()
    if not condition_coverage.empty:
        condition_coverage = condition_coverage.rename(columns={
            "valid_weather_samples": "有效天气样本",
            "valid_temperature_samples": "即时温度样本",
            "valid_game_time_samples": "游戏时间样本",
            "oldest_evidence_record": "证据最早时间",
            "newest_evidence_record": "证据最新时间",
            "weather_covered_dates": "天气覆盖日期数",
            "temperature_covered_dates": "温度覆盖日期数",
            "map_fish_groups": "地图+鱼种组数",
            "weather_groups_ge_25": "天气达到25条组数",
            "temperature_groups_ge_25": "温度达到25条组数",
            "weather_ready_groups": "天气达到15日且25条组数",
            "temperature_ready_groups": "温度达到15日且25条组数",
            "source_breakdown": "来源构成",
        })
        st.dataframe(condition_coverage, width="stretch", hide_index=True)
    st.caption("天气、游戏时间、即时温度分别统计，不再要求同一条记录同时具备全部字段。有可靠上鱼时间的RF4-STAT记录会回推中鱼时间并匹配当时已有天气快照；天气与温度的观察门槛分别为15个日期且至少25条。周榜日期和帖子发布时间不冒充上鱼时间。")
    if not PUBLIC_MODE:
        st.code(".venv\\Scripts\\python.exe backfill_history.py --batch-pages 100 --target-days 30", language="powershell")
    st.info("中国区普通本周榜采自RF4DB公开镜像，并非直接官方采集或全游戏渔获。旧无稳定ID缓存不参与推荐；仅从成功采集起逐周累积，不承诺近90天完整。周榜没有天气、即时温度、精确时间或坐标；按目录重量推定达标独立展示，不补入小时上星窗口，也不替代用户日分母。")
    st.subheader("已确定的统计规则")
    st.markdown(
        """
- 上星主指标采用“总上星数 / 有效用户日”，不采用“星鱼 / 全部鱼获”。
- 预报游戏日参考温度与渔获即时温度分开存储，模型校准通过后才统一比较。
- 近期习性与历史显著冲突时创建新行为阶段，禁止混合计算。
- 当前阶段数据不足时只展示事实和低可信临时排序，不输出伪精确概率。
        """
    )
    st.caption("完整规则见项目根目录 DEVELOPMENT.md；所有后续功能开发前必须先阅读该文档。")

