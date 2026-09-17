"""Planning Team 统一入口；与现有四个工具放在同一个目录。

启动：streamlit run planning_workbench.py
密码：沿用环境变量或 Streamlit Secrets 中的 APP_PASSWORD。
依赖：原 requirements.txt 即可，Streamlit 需为 1.46 或更新的 1.x 版本。
本文件只负责界面和输入路由，计算仍调用原脚本的 build_forecast_workbook。
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import logging
import os
from datetime import date
from io import BytesIO
from pathlib import Path

import pandas as pd
import streamlit as st


ROOT = Path(__file__).resolve().parent
MASTER_COLUMNS = ["SKU", "品牌", "BU", "项目号", "物料编码", "系列", "版本"]
TOOLS = {
    "regular": {
        "number": "01", "title": "常规总表", "tag": "汇总口径",
        "description": "当月提货 + 未来 5 个月需求",
        "color": "#2358D8", "tint": "#EDF3FF", "month_end": False, "split": False,
        "scripts": ("app.py",),
        "input_names": {"psi": "psi", "current": "delivery", "template": "template", "master": "master"},
    },
    "month_end": {
        "number": "02", "title": "月末总表", "tag": "双月提货",
        "description": "当月与次月提货 + 后续 4 个月需求",
        "color": "#087F6F", "tint": "#E9F7F2", "month_end": True, "split": False,
        "scripts": ("month_end_app.py",),
        "input_names": {"psi": "psi", "current": "delivery_m0", "next": "delivery_m1", "template": "template", "master": "master"},
    },
    "customer": {
        "number": "03", "title": "常规 · 客户类型", "tag": "TO-B / TO-C / TO-MKT",
        "description": "按客户类型拆分当月预计提货",
        "color": "#7349C2", "tint": "#F3EEFC", "month_end": False, "split": True,
        "scripts": ("Separation of TOB and TOC dmd app.py", "Separation_of_TOB_and_TOC_dmd_app.py"),
        "input_names": {"psi": "psi", "current": "delivery", "template": "template", "master": "master"},
    },
    "customer_month_end": {
        "number": "04", "title": "月末 · 客户类型", "tag": "双月提货 · 客户类型",
        "description": "按客户类型拆分当月与次月预计提货",
        "color": "#B65917", "tint": "#FFF2E7", "month_end": True, "split": True,
        "scripts": ("Separation of TOB and TOC dmd app month_end.py", "Separation_of_TOB_and_TOC_dmd_app_month_end.py"),
        "input_names": {"psi": "psi", "current": "delivery", "next": "delivery_1", "template": "template", "master": "master"},
    },
}


def file_specs(tool: dict) -> list[dict]:
    months = range(2 if tool["month_end"] else 1, 6)
    customer = ["客户类型"] if tool["split"] else []
    specs = [
        {"key": "psi", "label": "PSI 文件", "columns": ["SKU", "品牌", *[f"m_{m}" for m in months]]},
        {"key": "current", "label": "当月提货跟进表", "columns": ["SKU", "品牌", *customer, "m_0", "m_0安全库存"]},
    ]
    if tool["month_end"]:
        specs.append({"key": "next", "label": "次月提货跟进表", "columns": ["SKU", "品牌", *customer, "m_1", "m_1安全库存"]})
    specs.extend([
        {"key": "template", "label": "FCST 总表模板", "columns": ["Key"]},
        {"key": "master", "label": "主数据", "columns": MASTER_COLUMNS},
    ])
    return specs


def find_script(tool: dict) -> Path:
    for name in tool["scripts"]:
        path = ROOT / name
        if path.is_file():
            return path
    raise FileNotFoundError(f"请把 {tool['scripts'][0]} 与 planning_workbench.py 放在同一目录。")


def load_processor(tool_id: str):
    """按文件路径导入，兼容原文件名中的空格；不会执行原脚本的 main()。"""
    path = find_script(TOOLS[tool_id])
    spec = importlib.util.spec_from_file_location(f"planning_processor_{tool_id}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载工具：{path.name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def execute_tool(tool_id: str, documents: dict, run_date: date, previous_mmdd: str | None):
    """每次计算都使用新的 BytesIO，避免文件指针被上一次读取耗尽。"""
    tool = TOOLS[tool_id]
    files = {
        core_name: BytesIO(documents[ui_name]["data"])
        for ui_name, core_name in tool["input_names"].items()
    }
    module = load_processor(tool_id)
    result, output, warnings = module.build_forecast_workbook(files, run_date, previous_mmdd)
    return result, output.getvalue(), warnings


def new_tool_state() -> dict:
    return {"files": {}, "run_date": date.today(), "previous_mmdd": "", "result": None}


def tool_state(tool_id: str) -> dict:
    states = st.session_state.setdefault("wb_tools", {})
    if tool_id not in states:
        states[tool_id] = new_tool_state()
    return states[tool_id]


def set_active_tool(tool_id: str) -> None:
    st.session_state["wb_active"] = tool_id


def save_upload(tool_id: str, file_key: str, widget_key: str) -> None:
    uploaded = st.session_state.get(widget_key)
    files = tool_state(tool_id)["files"]
    if uploaded is None:
        files.pop(file_key, None)
        return
    data = uploaded.getvalue()
    files[file_key] = {
        "name": uploaded.name,
        "data": data,
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def remove_upload(tool_id: str, file_key: str) -> None:
    tool_state(tool_id)["files"].pop(file_key, None)


def clear_tool(tool_id: str) -> None:
    st.session_state["wb_tools"][tool_id] = new_tool_state()
    for key in list(st.session_state):
        if key.startswith(f"wb_input_{tool_id}_"):
            del st.session_state[key]


def logout() -> None:
    for key in list(st.session_state):
        if key.startswith("wb_"):
            del st.session_state[key]


def input_signature(tool_id: str, state: dict) -> tuple:
    path = find_script(TOOLS[tool_id])
    return (
        tool_id, state["run_date"].isoformat(), state["previous_mmdd"].strip(),
        tuple((key, state["files"].get(key, {}).get("sha256")) for key in TOOLS[tool_id]["input_names"]),
        path.stat().st_mtime_ns,
    )


def inject_style() -> None:
    selected = st.session_state.get("wb_active", "regular")
    card_styles = []
    for tool_id, tool in TOOLS.items():
        active = tool_id == selected
        card_styles.append(f"""
        .st-key-card_{tool_id} {{
            background: {tool['tint']}; border: 1px solid {tool['color'] if active else 'transparent'};
            border-top: 4px solid {tool['color']}; border-radius: 14px;
            padding: 19px 19px 16px; min-height: 215px;
            box-shadow: {'0 5px 16px #1e365012' if active else 'none'};
        }}
        .st-key-card_{tool_id} .card-number, .st-key-card_{tool_id} .card-tag {{ color: {tool['color']}; }}
        .st-key-card_{tool_id} [data-testid="stButton"] button {{
            background: {'#ffffff' if not active else tool['color']};
            color: {'#172945' if not active else '#ffffff'}; border: 0;
            font-weight: 600; min-height: 40px; border-radius: 8px;
        }}
        .st-key-card_{tool_id} [data-testid="stButton"] button:disabled {{ opacity: 1; }}
        """)
    st.markdown("""
    <style>
    :root { color-scheme: light; }
    .stApp { background: #F4F6FA; color: #18263E; }
    [data-testid="stAppViewContainer"] { background: #F4F6FA; }
    [data-testid="stHeader"] { background: transparent; }
    .block-container { max-width: 1360px; padding: 2.4rem 2.5rem 3rem; }
    h1,h2,h3 { font-family: -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif; color:#142640; }
    .wb-brand { display:flex; align-items:center; gap:12px; font-size:14px; font-weight:650; letter-spacing:1.4px; padding-top:6px; }
    .wb-logo { display:grid; place-items:center; width:32px; height:32px; border-radius:8px; background:#163456; color:white; font-size:18px; letter-spacing:0; }
    .wb-heading { margin:20px 0 8px; font-size:34px; line-height:1.3; font-weight:750; letter-spacing:-.8px; }
    .wb-intro { color:#657186; font-size:16px; margin-bottom:23px; line-height:1.6; }
    .card-top { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:17px; }
    .card-number { font-size:14px; font-weight:750; }
    .card-tag { font-size:12px; font-weight:600; }
    .card-title { font-size:20px; font-weight:700; margin:0 0 9px; line-height:1.4; }
    .card-description { font-size:14px; line-height:1.6; color:#536175; min-height:45px; margin:0 0 11px; }
    .st-key-workspace, .st-key-result_panel, .st-key-login_panel { background:white; border:1px solid #E1E6EE; border-radius:16px; padding:25px 28px; }
    .st-key-workspace { margin-top:18px; }
    .section-title { margin:0 0 6px; font-size:23px; font-weight:700; line-height:1.5; }
    .section-description { font-size:14px; color:#657186; margin:0 0 14px; line-height:1.7; }
    .section-eyebrow { font-size:12px; font-weight:750; letter-spacing:1px; margin-bottom:5px; }
    [data-testid="stFileUploaderDropzone"] { background:#F8FAFD; border:1px dashed #CCD5E2; border-radius:10px; }
    [data-testid="stFileUploaderDropzone"] button { background:#FFFFFF; }
    [data-testid="stWidgetLabel"] p { font-size:15px; font-weight:550; }
    [data-testid="stTextInput"] input, [data-testid="stDateInput"] input { background:#F8FAFD; color:#172945; }
    [data-testid="stButton"] button, [data-testid="stDownloadButton"] button { min-height:42px; }
    .st-key-generate_action [data-testid="stButton"] button { background:#173B67; color:#FFFFFF; border:0; }
    .st-key-generate_action [data-testid="stButton"] button:disabled { background:#DCE4ED; color:#718096; }
    .empty-result { padding:24px 4px 14px; color:#728095; font-size:15px; line-height:1.8; }
    .wb-footer { color:#768297; font-size:12px; text-align:center; padding-top:24px; }
    @media (max-width:1100px) and (min-width:641px) {
      .st-key-tools [data-testid="stHorizontalBlock"] { flex-wrap:wrap; }
      .st-key-tools [data-testid="stColumn"] { flex:1 1 calc(50% - 16px); min-width:calc(50% - 16px); }
    }
    @media (max-width:640px) {
      .block-container { padding:1.5rem 1rem 2rem; }
      .wb-heading { font-size:29px; }
      .st-key-workspace,.st-key-result_panel,.st-key-login_panel { padding:20px 16px; }
      .card-description { min-height:0; }
    }
    """ + "\n".join(card_styles) + "</style>", unsafe_allow_html=True)


def authenticate() -> bool:
    password = os.environ.get("APP_PASSWORD", "").strip()
    if not password:
        try:
            password = str(st.secrets.get("APP_PASSWORD", "")).strip()
        except Exception:
            password = ""
    if not password:
        st.error("请在 Streamlit 的 Settings → Secrets 中设置 APP_PASSWORD 后重新打开工作台。")
        return False
    if st.session_state.get("wb_authenticated", False):
        return True
    left, middle, right = st.columns([1, 2, 1])
    with middle, st.container(key="login_panel"):
        st.markdown('<div class="section-title">进入计划工作台</div><div class="section-description">使用团队访问密码登录。</div>', unsafe_allow_html=True)
        with st.form("wb_login_form"):
            entered = st.text_input("访问密码", type="password", key="wb_login_password")
            submitted = st.form_submit_button("进入工作台", type="primary", use_container_width=True)
        if submitted:
            if hmac.compare_digest(entered.encode("utf-8"), password.encode("utf-8")):
                st.session_state["wb_authenticated"] = True
                st.session_state.pop("wb_login_password", None)
                st.rerun()
            st.error("密码不正确，请重新输入。")
    return False


def render_cards(active: str) -> None:
    with st.container(key="tools"):
        columns = st.columns(4, gap="small")
        for column, (tool_id, tool) in zip(columns, TOOLS.items()):
            with column, st.container(key=f"card_{tool_id}"):
                st.markdown(
                    f'<div class="card-top"><span class="card-number">{tool["number"]}</span><span class="card-tag">{tool["tag"]}</span></div>'
                    f'<div class="card-title">{tool["title"]}</div><p class="card-description">{tool["description"]}</p>',
                    unsafe_allow_html=True,
                )
                st.button("正在使用" if active == tool_id else "打开工具", key=f"wb_select_{tool_id}",
                          disabled=active == tool_id, on_click=set_active_tool, args=(tool_id,), use_container_width=True)


def render_upload(tool_id: str, spec: dict) -> None:
    state = tool_state(tool_id)
    widget_key = f"wb_input_{tool_id}_{spec['key']}"
    uploaded = st.file_uploader(
        spec["label"], type=["xlsx", "xlsm", "xls"], key=widget_key,
        help="必需列：" + "、".join(spec["columns"]),
        on_change=save_upload, args=(tool_id, spec["key"], widget_key),
    )
    # 文件上传控件离开页面后会被Streamlit清理；独立状态中的文件仍可继续使用。
    saved = state["files"].get(spec["key"])
    if uploaded is None and saved:
        info, remove = st.columns([5, 1])
        with info:
            st.caption(f"已保留：{saved['name']}")
        with remove:
            st.button("移除", key=f"wb_remove_{tool_id}_{spec['key']}",
                      on_click=remove_upload, args=(tool_id, spec["key"]))


def render_result(tool_id: str, state: dict) -> None:
    with st.container(key="result_panel"):
        st.markdown('<div class="section-title">结果预览</div>', unsafe_allow_html=True)
        saved = state.get("result")
        if not saved:
            st.markdown('<div class="empty-result">上传所需文件并生成总表后，在这里查看结果和下载 Excel。</div>', unsafe_allow_html=True)
            return
        if saved["signature"] != input_signature(tool_id, state):
            st.info("文件或日期已变更，请重新生成总表。")
            return
        frame = saved["frame"]
        summary, download = st.columns([3, 2])
        with summary:
            st.caption(f"{TOOLS[tool_id]['title']} · {saved['run_date']} · {len(frame):,} 行 · {len(frame.columns):,} 列")
        with download:
            st.download_button("下载 Excel 总表", data=saved["bytes"], file_name=saved["filename"],
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               key=f"wb_download_{tool_id}", use_container_width=True, type="primary", on_click="ignore")
        if saved["warnings"]:
            with st.expander(f"数据提示（{len(saved['warnings'])}）", expanded=True):
                for warning in saved["warnings"]:
                    st.warning(warning)
        st.dataframe(frame, use_container_width=True, hide_index=True)


def render_workspace(tool_id: str) -> None:
    tool = TOOLS[tool_id]
    state = tool_state(tool_id)
    specs = file_specs(tool)
    try:
        find_script(tool)
    except FileNotFoundError as exc:
        st.error(str(exc))
        return
    with st.container(key="workspace"):
        heading, clear = st.columns([4, 1])
        with heading:
            st.markdown(f'<div class="section-eyebrow" style="color:{tool["color"]}">TOOL {tool["number"]}</div>'
                        f'<div class="section-title">{tool["title"]}</div>'
                        f'<div class="section-description">{tool["description"]}，含安全库存与版本差异。</div>', unsafe_allow_html=True)
        with clear:
            st.button("清空当前工具", key=f"wb_clear_{tool_id}", on_click=clear_tool, args=(tool_id,), use_container_width=True)
        date_col, previous_col = st.columns(2)
        with date_col:
            state["run_date"] = st.date_input("生成日期", value=state["run_date"], key=f"wb_input_{tool_id}_date")
        with previous_col:
            state["previous_mmdd"] = st.text_input("上次版本日期（可选）", value=state["previous_mmdd"],
                                                  placeholder="例如 0901；留空自动查找", max_chars=4, key=f"wb_input_{tool_id}_previous")
        with st.expander("查看文件要求与月份口径"):
            st.table(pd.DataFrame([{"文件": spec["label"], "必需列": "、".join(spec["columns"])} for spec in specs]))
            if tool["month_end"]:
                st.caption("当月读取 m_0，次月读取 m_1；PSI 提供 m_2～m_5。")
            else:
                st.caption("当月读取提货表的 m_0；PSI 提供 m_1～m_5。")
            if tool["split"]:
                st.caption("提货量统计 TO-B、TO-C、TO-MKT；安全库存保留原工具的全量汇总口径。")
        for index in range(0, len(specs), 2):
            columns = st.columns(2, gap="large")
            for column, spec in zip(columns, specs[index:index + 2]):
                with column:
                    render_upload(tool_id, spec)
        present = sum(spec["key"] in state["files"] for spec in specs)
        status, action = st.columns([3, 2])
        with status:
            st.caption(f"文件已就绪 {present} / {len(specs)} · 各工具的文件与结果在本次会话内分别保留。")
        with action, st.container(key="generate_action"):
            generate = st.button("检查并生成总表", key=f"wb_generate_{tool_id}", type="primary",
                                 disabled=present != len(specs), use_container_width=True)
        if generate:
            state["result"] = None
            try:
                with st.spinner("正在检查文件并生成总表…"):
                    result, data, warnings = execute_tool(tool_id, state["files"], state["run_date"], state["previous_mmdd"].strip() or None)
                    state["result"] = {
                        "frame": result, "bytes": data, "warnings": warnings,
                        "signature": input_signature(tool_id, state), "run_date": state["run_date"].isoformat(),
                        "filename": f"FCST-{tool['title'].replace(' · ', '-')}-{state['run_date']:%Y%m%d}.xlsx",
                    }
                st.success("总表已生成，可以预览并下载。")
            except (ValueError, KeyError, FileNotFoundError) as exc:
                st.error(f"生成失败：{exc}")
            except Exception:
                logging.exception("Planning tool failed: %s", tool_id)
                st.error("生成失败，请确认 Excel 可正常打开、文件未加密且格式正确。")
            finally:
                # 原脚本导入时会设置页面标题；统一入口恢复工作台标题。
                st.set_page_config(page_title="Planning Team · 计划工作台", page_icon="▦", layout="wide")
    render_result(tool_id, state)


def main() -> None:
    st.set_page_config(page_title="Planning Team · 计划工作台", page_icon="▦", layout="wide")
    if tuple(map(int, st.__version__.split(".")[:2])) < (1, 46):
        st.error("工作台需要 Streamlit 1.46 或更新版本。请将 requirements.txt 中的 Streamlit 下限改为 1.46 后重新部署。")
        return
    st.session_state.setdefault("wb_active", "regular")
    inject_style()
    brand, account = st.columns([5, 1])
    with brand:
        st.markdown('<div class="wb-brand"><span class="wb-logo">P</span> PLANNING TEAM</div>', unsafe_allow_html=True)
    with account:
        if st.session_state.get("wb_authenticated"):
            st.button("退出登录", key="wb_logout", on_click=logout, use_container_width=True)
    st.markdown('<div class="wb-heading">计划工作台</div><div class="wb-intro">选择工具，上传文件，生成 FCST 总表。</div>', unsafe_allow_html=True)
    if not authenticate():
        return
    active = st.session_state["wb_active"]
    render_cards(active)
    render_workspace(active)
    st.markdown('<div class="wb-footer">Planning Team · FCST Workspace</div>', unsafe_allow_html=True)


if __name__ == "__main__":
    main()
