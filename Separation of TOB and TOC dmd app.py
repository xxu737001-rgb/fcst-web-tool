## AI coding Modify
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
DEMAND_COLUMNS = ["m_1", "m_2", "m_3", "m_4", "m_5"]
DELIVERY_TYPES = ["TO-B", "TO-C"]


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
        st.error(
            "当前未设置访问密码。"
            "请在环境变量或 Streamlit secrets 中设置 APP_PASSWORD。"
        )
        return False

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


def assert_columns(
    frame: pd.DataFrame,
    required_columns: list[str],
    workbook_name: str,
) -> None:
    missing = [column for column in required_columns if column not in frame.columns]

    if missing:
        raise WorkbookError(
            f"{workbook_name} 缺少必需列：{'、'.join(missing)}"
        )


def normalize_identifier(value):
    if pd.isna(value):
        return pd.NA

    if isinstance(value, float) and value.is_integer():
        return str(int(value))

    text = str(value).strip()
    return text if text else pd.NA


def make_key(frame: pd.DataFrame) -> pd.Series:
    brand = frame["品牌"].map(normalize_identifier).astype("string")
    sku = frame["SKU"].map(normalize_identifier).astype("string")

    key = brand + sku
    return key.mask(brand.isna() | sku.isna())


def coerce_numeric_columns(
    frame: pd.DataFrame,
    columns: list[str],
    workbook_name: str,
) -> None:
    for column in columns:
        original = frame[column]
        converted = pd.to_numeric(original, errors="coerce")

        nonempty = (
            original.notna()
            & original.astype(str).str.strip().ne("")
        )

        invalid = nonempty & converted.isna()

        if invalid.any():
            examples = "、".join(
                original.loc[invalid]
                .astype(str)
                .drop_duplicates()
                .head(3)
            )

            raise WorkbookError(
                f"{workbook_name} 的 {column} 列包含非数字内容，"
                f"例如：{examples}"
            )

        frame[column] = converted.fillna(0)


def extract_sku_from_key(key):
    if pd.isna(key):
        return pd.NA

    key_text = str(key).strip()

    for brand in BRANDS:
        if key_text.startswith(brand):
            sku = key_text[len(brand):].strip()
            return sku if sku else pd.NA

    return pd.NA


def month_text(base_date: date, offset: int = 0) -> str:
    target = base_date + relativedelta(months=offset)
    return f"{target.year}年{target.month}月"


def validate_mmdd(mmdd: str) -> None:
    if not re.fullmatch(r"\d{4}", mmdd):
        raise WorkbookError(
            "上次版本日期必须是 4 位 MMDD，例如 0605。"
        )

    try:
        date(2000, int(mmdd[:2]), int(mmdd[2:]))
    except ValueError as exc:
        raise WorkbookError(
            "上次版本日期不是有效的 MMDD 日期。"
        ) from exc


def find_previous_total_column(
    columns: pd.Index,
    run_date: date,
    previous_mmdd: str | None,
) -> str | None:
    prefix = f"{run_date.month}月需求总和(包含安全库存)"

    if previous_mmdd:
        exact_name = f"{prefix}({previous_mmdd})"
        return exact_name if exact_name in columns else None

    pattern = re.compile(
        rf"^{run_date.month}月需求总和"
        rf"\(包含安全库存\)\((\d{{4}})\)$"
    )

    candidates: list[tuple[int, str]] = []

    for column in columns:
        match = pattern.match(str(column))

        if not match:
            continue

        mmdd = match.group(1)

        try:
            candidate_date = date(
                2000,
                int(mmdd[:2]),
                int(mmdd[2:]),
            )
        except ValueError:
            continue

        if (
            candidate_date.month == run_date.month
            and candidate_date.day < run_date.day
        ):
            candidates.append((candidate_date.day, str(column)))

    return max(
        candidates,
        default=(0, ""),
        key=lambda item: item[0],
    )[1] or None


def read_workbooks(files):
    psi = pd.read_excel(files["psi"])
    delivery = pd.read_excel(files["delivery"])
    template = pd.read_excel(files["template"])
    master = pd.read_excel(files["master"])

    assert_columns(
        psi,
        ["SKU", "品牌", *DEMAND_COLUMNS],
        "PSI 文件",
    )

    assert_columns(
        delivery,
        ["SKU", "品牌", "客户类型", "m_0", "m_0安全库存"],
        "提货跟进表",
    )

    assert_columns(
        template,
        ["Key"],
        "FCST 总表模板",
    )

    assert_columns(
        master,
        ["SKU", *MASTER_COLUMNS],
        "主数据",
    )

    return psi, delivery, template, master


def build_forecast_workbook(
    files,
    run_date: date,
    previous_mmdd: str | None,
) -> tuple[pd.DataFrame, BytesIO, list[str]]:
    warnings: list[str] = []
    today_mmdd = run_date.strftime("%m%d")

    if previous_mmdd:
        validate_mmdd(previous_mmdd)

    psi, delivery, template, master = read_workbooks(files)

    for frame in (psi, delivery, master):
        frame["SKU"] = (
            frame["SKU"]
            .map(normalize_identifier)
            .astype("string")
        )
        frame["品牌"] = (
            frame["品牌"]
            .map(normalize_identifier)
            .astype("string")
        )

    template["Key"] = (
        template["Key"]
        .map(normalize_identifier)
        .astype("string")
    )

    psi = psi.loc[
        psi["SKU"].notna()
        & psi["品牌"].notna()
    ].copy()

    delivery = delivery.loc[
        delivery["SKU"].notna()
        & delivery["品牌"].notna()
    ].copy()

    master = master.loc[
        master["SKU"].notna()
        & master["品牌"].notna()
    ].copy()

    template = template.loc[
        template["Key"].notna()
    ].copy()

    if template["Key"].duplicated().any():
        warnings.append(
            "FCST 总表模板存在重复 Key，"
            "已保留每个 Key 的第一行。"
        )

        template = template.drop_duplicates(
            subset=["Key"],
            keep="first",
        )

    coerce_numeric_columns(
        psi,
        DEMAND_COLUMNS,
        "PSI 文件",
    )

    coerce_numeric_columns(
        delivery,
        ["m_0", "m_0安全库存"],
        "提货跟进表",
    )

    # PSI 汇总
    psi["Key"] = make_key(psi)

    pivot_psi = (
        psi.groupby("Key", as_index=False)[DEMAND_COLUMNS]
        .sum()
    )

    pivot_psi = pivot_psi.loc[
        ~(pivot_psi[DEMAND_COLUMNS] == 0).all(axis=1)
    ]

    # 提货数据汇总
    delivery["Key"] = make_key(delivery)
    delivery["客户类型"] = (
        delivery["客户类型"]
        .astype("string")
        .str.strip()
    )

    pivot_delivery = (
        delivery.pivot_table(
            index="Key",
            columns="客户类型",
            values="m_0",
            aggfunc="sum",
            fill_value=0,
        )
        .reset_index()
        .rename_axis(columns=None)
    )

    for customer_type in DELIVERY_TYPES:
        if customer_type not in pivot_delivery.columns:
            pivot_delivery[customer_type] = 0

    pivot_delivery["m_0预计总提货"] = (
        pivot_delivery[DELIVERY_TYPES].sum(axis=1)
    )

    pivot_safety_stock = (
        delivery.groupby("Key", as_index=False)["m_0安全库存"]
        .sum()
    )

    # 连续合并，不能从 template 重新开始合并
    result = template.merge(
        pivot_delivery,
        on="Key",
        how="outer",
    )

    result = result.merge(
        pivot_safety_stock,
        on="Key",
        how="outer",
    )

    result = result.merge(
        pivot_psi,
        on="Key",
        how="outer",
    )

    numeric_columns = [
        *DELIVERY_TYPES,
        "m_0预计总提货",
        "m_0安全库存",
        *DEMAND_COLUMNS,
    ]

    for column in numeric_columns:
        if column not in result.columns:
            result[column] = 0

        result[column] = (
            pd.to_numeric(
                result[column],
                errors="coerce",
            )
            .fillna(0)
        )

    result["m_0+m_0安全库存"] = (
        result["m_0预计总提货"]
        + result["m_0安全库存"]
    )

    # 提取 SKU
    if "SKU" not in result.columns:
        result["SKU"] = result["Key"].apply(
            extract_sku_from_key
        )
    else:
        result["SKU"] = result["SKU"].map(
            normalize_identifier
        )

        missing_sku = result["SKU"].isna()

        result.loc[missing_sku, "SKU"] = (
            result.loc[missing_sku, "Key"]
            .apply(extract_sku_from_key)
        )

    for column in MASTER_COLUMNS:
        if column not in result.columns:
            result[column] = pd.NA

    # 主数据按 品牌 + SKU 合并，避免不同品牌的同 SKU 串数据
    master["Key"] = make_key(master)

    if master["Key"].duplicated().any():
        warnings.append(
            "主数据存在重复的品牌 + SKU，"
            "已保留第一条记录。"
        )

    master_fields = ["SKU", *MASTER_COLUMNS]

    master_subset = (
        master[["Key", *master_fields]]
        .drop_duplicates(
            subset=["Key"],
            keep="first",
        )
        .rename(
            columns={
                column: f"__master_{column}"
                for column in master_fields
            }
        )
    )

    result = result.merge(
        master_subset,
        on="Key",
        how="left",
    )

    for column in master_fields:
        master_column = f"__master_{column}"

        result[column] = (
            result[master_column]
            .combine_first(result[column])
        )

        result.drop(
            columns=[master_column],
            inplace=True,
        )

    result["销售大区"] = "新兴区"

    sheet_brand = result["品牌"].apply(
        lambda value: ""
        if pd.isna(value)
        else str(value)
        if value in ["追觅", "光子跃迁"]
        else "MOVA"
    )

    sheet_bu = result["BU"].apply(
        lambda value: ""
        if pd.isna(value)
        else "个护"
        if value in ["造型器", "吹风机", "脱毛仪"]
        else str(value)
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

    # 计算与上个版本的差异
    previous_total_column = find_previous_total_column(
        result.columns,
        run_date,
        previous_mmdd,
    )

    if previous_total_column:
        coerce_numeric_columns(
            result,
            [previous_total_column],
            "FCST 总表模板",
        )

        result[f"{run_date.month}月与上次的差异"] = (
            result["m_0+m_0安全库存"]
            - result[previous_total_column]
        )
    elif previous_mmdd:
        warnings.append(
            f"未找到 "
            f"{run_date.month}月需求总和(包含安全库存)"
            f"({previous_mmdd})，已跳过差异列。"
        )
    else:
        warnings.append(
            f"未找到 {run_date.month}月可用的上次需求总和列，"
            "已跳过差异列。"
        )

    # TO-B、TO-C 分开输出，同时保留预计总提货合计
    rename_map = {
        "TO-B": f"{run_date.month}月TO-B预计提货({today_mmdd})",
        "TO-C": f"{run_date.month}月TO-C预计提货({today_mmdd})",
        "m_0预计总提货": f"{run_date.month}月预计总提货({today_mmdd})",
        "m_0安全库存": f"{run_date.month}月安全库存({today_mmdd})",
        "m_0+m_0安全库存": (
            f"{run_date.month}月需求总和"
            f"(包含安全库存)({today_mmdd})"
        ),
        "m_1": f"{month_text(run_date, 1)}需求({today_mmdd})",
        "m_2": f"{month_text(run_date, 2)}需求({today_mmdd})",
        "m_3": f"{month_text(run_date, 3)}需求({today_mmdd})",
        "m_4": f"{month_text(run_date, 4)}需求({today_mmdd})",
        "m_5": f"{month_text(run_date, 5)}需求({today_mmdd})",
    }

    # 防止重复生成同一天时产生重复列
    existing_targets = [
        target
        for source, target in rename_map.items()
        if source != target
        and target in result.columns
    ]

    if existing_targets:
        result.drop(
            columns=existing_targets,
            inplace=True,
        )

    result.rename(
        columns=rename_map,
        inplace=True,
    )

    # 只对文本列填空字符串
    for column in ["销售大区", *MASTER_COLUMNS]:
        if column in result.columns:
            result[column] = result[column].fillna("")

    output = BytesIO()

    result.to_excel(
        output,
        index=False,
        engine="openpyxl",
    )

    output.seek(0)

    return result, output, warnings


def main() -> None:
    if not require_password():
        return

    st.title("FCST 总表生成工具")

    settings_col, action_col = st.columns([1, 2])

    with settings_col:
        run_date = st.date_input(
            "生成日期",
            value=date.today(),
        )

        previous_mmdd = st.text_input(
            "上次版本日期",
            placeholder="例如 0605",
        ).strip() or None

    with action_col:
        psi_file = st.file_uploader(
            "PSI 文件",
            type=["xlsx", "xlsm", "xls"],
        )

        delivery_file = st.file_uploader(
            "提货跟进表",
            type=["xlsx", "xlsm", "xls"],
        )

        template_file = st.file_uploader(
            "FCST 总表模板",
            type=["xlsx", "xlsm", "xls"],
        )

        master_file = st.file_uploader(
            "主数据",
            type=["xlsx", "xlsm", "xls"],
        )

    uploaded_files = {
        "psi": psi_file,
        "delivery": delivery_file,
        "template": template_file,
        "master": master_file,
    }

    ready = all(uploaded_files.values())

    generate = st.button(
        "生成总表",
        type="primary",
        disabled=not ready,
    )

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
            file_name=f"fcst-总表({run_date.strftime('%m%d')}).xlsx",
            mime=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
        )

        st.dataframe(
            result,
            use_container_width=True,
            hide_index=True,
        )


if __name__ == "__main__":
    main()
