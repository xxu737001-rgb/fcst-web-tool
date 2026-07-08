import hmac
import os
import re
from datetime import date
from io import BytesIO

import pandas as pd
import streamlit as st
from dateutil.relativedelta import relativedelta


st.set_page_config(page_title="FCST 总表生成工具", layout="wide")


BRANDS = ["MOVA", "追觅", "Arçelik", "Beko", "光子跃迁"]
MASTER_COLUMNS = ["品牌", "BU", "项目号", "物料编码", "系列", "版本"]


class WorkbookError(ValueError):
    pass


def require_password() -> bool:
    app_password = os.getenv("APP_PASSWORD", "").strip()
    if not app_password:
        try:
            app_password = str(st.secrets.get("APP_PASSWORD", "")).strip()
        except Exception:
            app_password = ""

    if not app_password:
        st.warning("当前未设置访问密码。公网部署前请在服务器环境变量中设置 APP_PASSWORD。")
        return True

    if st.session_state.get("authenticated"):
        return True

    st.title("FCST 总表生成工具")
    with st.form("login_form"):
        password = st.text_input("访问密码", type="password")
        submitted = st.form_submit_button("进入", type="primary")

    if submitted:
        if hmac.compare_digest(password, app_password):
            st.session_state["authenticated"] = True
            st.rerun()
        st.error("密码不正确")

    return False


def assert_columns(frame: pd.DataFrame, required_columns: list[str], workbook_name: str) -> None:
    missing = [column for column in required_columns if column not in frame.columns]
    if missing:
        missing_text = "、".join(missing)
        raise WorkbookError(f"{workbook_name} 缺少必需列：{missing_text}")


def extract_sku_from_key(key):
    if pd.isna(key):
        return None

    key_str = str(key)
    for brand in BRANDS:
        if key_str.startswith(brand):
            return key_str[len(brand):]

    return None


def month_text(base_date: date, offset: int = 0) -> str:
    target = base_date + relativedelta(months=offset)
    return f"{target.year}年{target.month}月"


def find_previous_total_column(
    columns: pd.Index,
    run_date: date,
    previous_mmdd: str | None,
) -> str | None:
    month = run_date.month

    if previous_mmdd:
        exact_name = f"{month}月需求总和(包含安全库存)({previous_mmdd})"
        if exact_name in columns:
            return exact_name

    pattern = re.compile(rf"^{month}月需求总和\(包含安全库存\)\(\d{{4}}\)$")
    matches = [column for column in columns if pattern.match(str(column))]
    return matches[-1] if matches else None


def read_workbooks(files: dict[str, BytesIO]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    psi = pd.read_excel(files["psi"])
    delivery = pd.read_excel(files["delivery"])
    template = pd.read_excel(files["template"])
    master = pd.read_excel(files["master"])

    assert_columns(psi, ["SKU", "品牌", "m_1", "m_2", "m_3", "m_4", "m_5"], "PSI 文件")
    assert_columns(delivery, ["SKU", "品牌", "m_0", "m_0安全库存"], "提货跟进表")
    assert_columns(template, ["Key"], "FCST 总表模板")
    assert_columns(master, ["SKU", *MASTER_COLUMNS], "主数据")

    return psi, delivery, template, master


def build_forecast_workbook(
    files: dict[str, BytesIO],
    run_date: date,
    previous_mmdd: str | None,
) -> tuple[pd.DataFrame, BytesIO, list[str]]:
    warnings: list[str] = []
    today_mmdd = run_date.strftime("%m%d")
    psi, delivery, template, master = read_workbooks(files)

    psi = psi[psi["SKU"].notna() & (psi["SKU"] != "")].copy()
    delivery = delivery[delivery["SKU"].notna() & (delivery["SKU"] != "")].copy()

    psi.columns = psi.columns.astype(str)
    psi["Key"] = psi["品牌"].astype(str) + psi["SKU"].astype(str)

    pivot_psi = (
        psi.groupby("Key", as_index=False)[["m_1", "m_2", "m_3", "m_4", "m_5"]]
        .sum()
    )
    demand_columns = ["m_1", "m_2", "m_3", "m_4", "m_5"]
    pivot_psi = pivot_psi.loc[~(pivot_psi[demand_columns] == 0).all(axis=1)]

    delivery["Key"] = delivery["品牌"].astype(str) + delivery["SKU"].astype(str)
    pivot_delivery = delivery.groupby("Key", as_index=False)[["m_0", "m_0安全库存"]].sum()
    pivot_delivery["m_0+m_0安全库存"] = (
        pivot_delivery["m_0"] + pivot_delivery["m_0安全库存"]
    )

    result = template.merge(pivot_delivery, on="Key", how="outer")
    result = result.merge(pivot_psi, on="Key", how="outer")

    if "SKU" not in result.columns:
        result["SKU"] = result["Key"].apply(extract_sku_from_key)
    else:
        result["SKU"] = result.apply(
            lambda row: extract_sku_from_key(row["Key"]) if pd.isna(row["SKU"]) else row["SKU"],
            axis=1,
        )

    result["销售大区"] = "新兴区"
    for column in MASTER_COLUMNS:
        if column not in result.columns:
            result[column] = pd.NA

    master_subset = (
        master[["SKU", *MASTER_COLUMNS]]
        .drop_duplicates(subset=["SKU"], keep="first")
        .copy()
    )
    result = result.merge(master_subset, on="SKU", how="left", suffixes=("", "_new"))

    for column in MASTER_COLUMNS:
        result[column] = result[f"{column}_new"].combine_first(result[column])
        result.drop(columns=[f"{column}_new"], inplace=True)

    sheet_brand = (
        result["品牌"]
        .apply(lambda value: value if value in ["追觅", "光子跃迁"] else "MOVA")
        .fillna("")
        .astype(str)
    )
    sheet_bu = (
        result["BU"]
        .apply(lambda value: "个护" if value in ["造型器", "吹风机", "脱毛仪"] else value)
        .fillna("")
        .astype(str)
    )
    result["key for sheet"] = (
        result["销售大区"].astype(str)
        + "-"
        + sheet_brand
        + "-"
        + sheet_bu
        + "预测"
        + today_mmdd
    )

    result = result.drop_duplicates(subset=["Key"], keep="first")
    result = result.fillna(0)

    previous_total_column = find_previous_total_column(result.columns, run_date, previous_mmdd)
    if previous_total_column:
        result[f"{run_date.month}月与上次的差异"] = (
            result["m_0+m_0安全库存"] - result[previous_total_column]
        )
    else:
        warnings.append(f"未找到 {run_date.month}月上次需求总和列，已跳过差异列。")

    result.rename(
        columns={
            "m_0": f"{run_date.month}月预计总提货({today_mmdd})",
            "m_0安全库存": f"{run_date.month}月安全库存({today_mmdd})",
            "m_0+m_0安全库存": f"{run_date.month}月需求总和(包含安全库存)({today_mmdd})",
            "m_1": f"{month_text(run_date, 1)}需求({today_mmdd})",
            "m_2": f"{month_text(run_date, 2)}需求({today_mmdd})",
            "m_3": f"{month_text(run_date, 3)}需求({today_mmdd})",
            "m_4": f"{month_text(run_date, 4)}需求({today_mmdd})",
            "m_5": f"{month_text(run_date, 5)}需求({today_mmdd})",
        },
        inplace=True,
    )

    output = BytesIO()
    result.to_excel(output, index=False)
    output.seek(0)

    return result, output, warnings


def main() -> None:
    if not require_password():
        return

    st.title("FCST 总表生成工具")

    settings_col, action_col = st.columns([1, 2])
    with settings_col:
        run_date = st.date_input("生成日期", value=date.today())
        previous_mmdd = st.text_input("上次版本日期", placeholder="例如 0605").strip() or None

    with action_col:
        psi_file = st.file_uploader("PSI 文件", type=["xlsx", "xlsm", "xls"])
        delivery_file = st.file_uploader("提货跟进表", type=["xlsx", "xlsm", "xls"])
        template_file = st.file_uploader("FCST 总表模板", type=["xlsx", "xlsm", "xls"])
        master_file = st.file_uploader("主数据", type=["xlsx", "xlsm", "xls"])

    uploaded_files = {
        "psi": psi_file,
        "delivery": delivery_file,
        "template": template_file,
        "master": master_file,
    }

    ready = all(uploaded_files.values())
    generate = st.button("生成总表", type="primary", disabled=not ready)

    if generate:
        try:
            result, output, warnings = build_forecast_workbook(
                uploaded_files,
                run_date,
                previous_mmdd,
            )
        except WorkbookError as exc:
            st.error(str(exc))
            return
        except Exception as exc:
            st.error("生成失败，请检查 Excel 文件格式。")
            st.exception(exc)
            return

        for warning in warnings:
            st.warning(warning)

        st.success("生成成功")
        st.download_button(
            label="下载总表",
            data=output,
            file_name=f"fsct-总表({run_date.strftime('%m%d')}).xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        st.dataframe(result, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()


