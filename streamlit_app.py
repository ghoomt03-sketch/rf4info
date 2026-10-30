from __future__ import annotations

import logging
import os
import time

import streamlit as st

from rf4_public_runtime import prepare_public_runtime


st.set_page_config(page_title="俄钓4筛选推荐", page_icon="🎣", layout="wide")
os.environ["RF4_PAGE_CONFIGURED"] = "1"
os.environ["RF4_APP_MODE"] = "public"
os.environ["RF4_SHELL_RENDERED"] = "1"

st.title("俄罗斯钓鱼4 · 筛选推荐工具")
st.caption("公开只读版 · 数据由本地处理端采集、校验和发布；页面不会访问来源网站或修改数据库。")
startup_message = st.empty()
startup_message.info("正在加载公开数据，首次启动可能需要几十秒……")
startup_progress = st.progress(0, text="正在启动")
time.sleep(0.05)


def report_startup_progress(value: float, text: str) -> None:
    startup_progress.progress(min(round(value * 60), 60), text=text)

try:
    runtime_database = prepare_public_runtime(progress_callback=report_startup_progress)
except Exception:
    logging.exception("公开数据校验或运行库组装失败")
    startup_progress.empty()
    startup_message.empty()
    st.error("公开数据暂时无法加载。请稍后重新打开页面；若持续出现，请联系维护者检查云端日志。")
    st.stop()

os.environ["RF4_DB_PATH"] = str(runtime_database)
startup_progress.progress(70, text="正在读取筛选与推荐数据")
time.sleep(0.05)

import app  # noqa: E402,F401  Streamlit executes app.py at import time.

startup_progress.progress(100, text="页面加载完成")
startup_progress.empty()
startup_message.empty()
