from __future__ import annotations

import logging
import os

import streamlit as st

from public_runtime import prepare_public_runtime


st.set_page_config(page_title="俄钓4筛选推荐", page_icon="🎣", layout="wide")
os.environ["RF4_PAGE_CONFIGURED"] = "1"
os.environ["RF4_APP_MODE"] = "public"

startup_message = st.empty()
with startup_message.container():
    st.info("正在校验并加载公开数据，首次启动可能需要几十秒……")

try:
    runtime_database = prepare_public_runtime()
except Exception:
    logging.exception("公开数据校验或运行库组装失败")
    startup_message.empty()
    st.error("公开数据暂时无法加载。请稍后重新打开页面；若持续出现，请联系维护者检查云端日志。")
    st.stop()

os.environ["RF4_DB_PATH"] = str(runtime_database)
startup_message.empty()

import app  # noqa: E402,F401  Streamlit executes app.py at import time.
