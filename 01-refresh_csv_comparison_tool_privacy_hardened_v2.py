import io
import os
import zipfile

import pandas as pd
import streamlit as st

st.set_page_config(layout="wide", page_title="csv set comparator")

LOCAL_SCRIPT_CONTENT = 'import io\nimport os\nimport zipfile\n\nimport pandas as pd\nimport streamlit as st\n\nst.set_page_config(layout="wide", page_title="csv set comparator (local)")\n\nMAX_ZIP_UPLOAD_MB = 200\nMAX_TOTAL_UNCOMPRESSED_MB = 500\nMAX_CSV_FILE_MB = 100\nMAX_CSV_COUNT = 500\nMAX_COMPRESSION_RATIO = 100\n\n\ndef mb_to_bytes(value):\n    return value * 1024 * 1024\n\n\ndef reset_uploads():\n    st.session_state["upload_version"] = st.session_state.get("upload_version", 0) + 1\n\n\ndef get_upload_size(uploaded_file):\n    size = getattr(uploaded_file, "size", None)\n    if size is not None:\n        return size\n    try:\n        return uploaded_file.getbuffer().nbytes\n    except Exception:\n        return 0\n\n\ndef validate_zip_upload(zip_file, label):\n    """Validate a ZIP before reading CSV data from it.\n\n    The app never extracts files to disk, but these checks reduce exposure to ZIP bombs,\n    unexpectedly large uploads, encrypted archives, and confusing archive paths.\n    """\n    upload_size = get_upload_size(zip_file)\n    if upload_size > mb_to_bytes(MAX_ZIP_UPLOAD_MB):\n        return False, f"{label} is larger than {MAX_ZIP_UPLOAD_MB} MB."\n\n    try:\n        zip_file.seek(0)\n        with zipfile.ZipFile(zip_file) as archive:\n            csv_infos = []\n            total_uncompressed_size = 0\n\n            for info in archive.infolist():\n                name = info.filename\n                normalized_path = os.path.normpath(name)\n\n                if info.flag_bits & 0x1:\n                    return False, f"{label} contains an encrypted file, which is not supported."\n\n                if normalized_path.startswith("..") or os.path.isabs(name):\n                    return False, f"{label} contains an unsafe file path."\n\n                if info.is_dir() or name.startswith("__MACOSX") or not name.lower().endswith(".csv"):\n                    continue\n\n                csv_infos.append(info)\n                total_uncompressed_size += info.file_size\n\n                if info.file_size > mb_to_bytes(MAX_CSV_FILE_MB):\n                    return False, f"{label} contains a CSV larger than {MAX_CSV_FILE_MB} MB: {os.path.basename(name)}"\n\n                compression_ratio = info.file_size / max(info.compress_size, 1)\n                if compression_ratio > MAX_COMPRESSION_RATIO and info.file_size > mb_to_bytes(10):\n                    return False, f"{label} contains a suspiciously compressed CSV: {os.path.basename(name)}"\n\n            if len(csv_infos) > MAX_CSV_COUNT:\n                return False, f"{label} contains more than {MAX_CSV_COUNT} CSV files."\n\n            if total_uncompressed_size > mb_to_bytes(MAX_TOTAL_UNCOMPRESSED_MB):\n                return False, f"{label} expands to more than {MAX_TOTAL_UNCOMPRESSED_MB} MB of CSV data."\n\n            if not csv_infos:\n                return False, f"{label} does not contain any CSV files."\n\n        zip_file.seek(0)\n        return True, ""\n    except zipfile.BadZipFile:\n        return False, f"{label} is not a valid ZIP file."\n    except Exception:\n        return False, f"{label} could not be validated."\n\n\ndef get_file_list(zip_file):\n    try:\n        zip_file.seek(0)\n        with zipfile.ZipFile(zip_file) as archive:\n            return sorted(\n                info.filename\n                for info in archive.infolist()\n                if not info.is_dir()\n                and info.filename.lower().endswith(".csv")\n                and not info.filename.startswith("__MACOSX")\n            )\n    except Exception:\n        return []\n\n\ndef normalize_filename(filename, prefix="", suffix_delimiter=""):\n    base = os.path.basename(filename)\n    name_no_ext = os.path.splitext(base)[0]\n    if prefix and name_no_ext.startswith(prefix):\n        name_no_ext = name_no_ext[len(prefix):]\n    if suffix_delimiter and suffix_delimiter in name_no_ext:\n        name_no_ext = name_no_ext.rsplit(suffix_delimiter, 1)[0]\n    return name_no_ext\n\n\ndef build_normalized_map(file_list, prefix="", suffix_delimiter=""):\n    file_map = {}\n    collisions = {}\n\n    for file_name in file_list:\n        normalized_name = normalize_filename(file_name, prefix, suffix_delimiter)\n        if normalized_name in file_map:\n            collisions.setdefault(normalized_name, [file_map[normalized_name]]).append(file_name)\n            file_map.pop(normalized_name, None)\n        elif normalized_name not in collisions:\n            file_map[normalized_name] = file_name\n\n    return file_map, collisions\n\n\ndef read_csv_from_zip(zip_file, full_path):\n    try:\n        zip_file.seek(0)\n        with zipfile.ZipFile(zip_file) as archive:\n            info = archive.getinfo(full_path)\n            if info.file_size == 0:\n                return pd.DataFrame()\n            if info.file_size > mb_to_bytes(MAX_CSV_FILE_MB):\n                return None\n\n            raw_bytes = archive.read(full_path)\n            try:\n                return pd.read_csv(io.BytesIO(raw_bytes))\n            except pd.errors.EmptyDataError:\n                return pd.DataFrame()\n            except UnicodeDecodeError:\n                return pd.read_csv(io.BytesIO(raw_bytes), encoding="latin1")\n    except Exception:\n        return None\n\n\ndef compare_dataframes(df1, df2, ignore_cols):\n    if df1 is None or df2 is None:\n        return "❌ Error", None\n\n    df1 = df1.drop(columns=[c for c in ignore_cols if c in df1.columns], errors="ignore").copy()\n    df2 = df2.drop(columns=[c for c in ignore_cols if c in df2.columns], errors="ignore").copy()\n\n    if set(df1.columns) != set(df2.columns):\n        return "⚠️ Schema Diff", None\n\n    df1 = df1.sort_index(axis=1)\n    df2 = df2.sort_index(axis=1)\n    compare_cols = list(df1.columns)\n\n    old_counts = df1.value_counts(dropna=False).rename("old_count").reset_index()\n    new_counts = df2.value_counts(dropna=False).rename("new_count").reset_index()\n    merged = old_counts.merge(new_counts, on=compare_cols, how="outer").fillna(0)\n    merged["old_count"] = merged["old_count"].astype(int)\n    merged["new_count"] = merged["new_count"].astype(int)\n\n    changed_rows = merged[merged["old_count"] != merged["new_count"]]\n    if changed_rows.empty:\n        return "✅ Match", pd.DataFrame()\n\n    diff_rows = []\n    for _, row in changed_rows.iterrows():\n        values = {col: row[col] for col in compare_cols}\n        removed_count = max(row["old_count"] - row["new_count"], 0)\n        added_count = max(row["new_count"] - row["old_count"], 0)\n        diff_rows.extend([{**values, "_source": "OLD"} for _ in range(removed_count)])\n        diff_rows.extend([{**values, "_source": "NEW"} for _ in range(added_count)])\n\n    return "⚠️ Data Mismatch", pd.DataFrame(diff_rows)\n\n\ndef display_diff_results(status, diff_df, rows_old, rows_new):\n    m1, m2, m3 = st.columns(3)\n    m1.metric("rows in old file", rows_old)\n    m2.metric("rows in new file", rows_new, delta=(rows_new - rows_old))\n\n    if status == "✅ Match":\n        m3.metric("status", "match")\n        st.success("✅ files match after applying the selected ignore columns.")\n    elif status == "⚠️ Schema Diff":\n        m3.metric("status", "schema diff", delta_color="inverse")\n        st.error("⚠️ column names do not match.")\n    elif diff_df is not None:\n        diff_count = len(diff_df)\n        m3.metric("rows with differences", diff_count, delta_color="inverse")\n        st.warning(f"⚠️ found {diff_count} differing row instance(s).")\n        removed = diff_df[diff_df["_source"] == "OLD"].drop(columns=["_source"])\n        added = diff_df[diff_df["_source"] == "NEW"].drop(columns=["_source"])\n        t1, t2 = st.tabs([f"rows removed ({len(removed)})", f"rows added ({len(added)})"])\n        with t1:\n            st.dataframe(removed, use_container_width=True)\n        with t2:\n            st.dataframe(added, use_container_width=True)\n\n\nst.title("📊 refresh csv comparison tool")\n\nwith st.expander("how to use this app", expanded=True):\n    st.markdown(\n        """\n        1. upload the **old/reference ZIP** and the **new/target ZIP** in the sidebar.\n        2. adjust the auto-match settings if filenames include prefixes or suffixes that should be ignored.\n        3. list columns to ignore, such as load timestamps or run IDs.\n        4. review the global status report, then inspect any mismatches in detail.\n        5. use **clear uploaded data** when finished to reset upload widgets and session state.\n\n        **privacy and security best practices**\n        - the local version keeps processing on the computer running this script.\n        - uploaded files are read in memory and are not intentionally written to disk by this app.\n        - do not add caching, logging, external API calls, or analytics that could retain file contents.\n        - keep ZIPs reasonably small and only include CSVs needed for the comparison.\n        - close the browser tab and terminal process after comparing sensitive files.\n        """\n    )\n\nwith st.sidebar:\n    st.title("csv tool")\n    st.button("clear uploaded data", on_click=reset_uploads)\n    st.divider()\n    st.header("1. upload files")\n    upload_version = st.session_state.get("upload_version", 0)\n    zip_ref = st.file_uploader("upload reference / old zip", type="zip", key=f"zip_ref_{upload_version}")\n    zip_target = st.file_uploader("upload target / new zip", type="zip", key=f"zip_target_{upload_version}")\n    st.divider()\n    st.header("2. auto-match logic")\n    ref_prefix = st.text_input("remove from old:", placeholder="e.g. kaplan-")\n    tgt_prefix = st.text_input("remove from new:", placeholder="e.g. newkaplan-")\n    suffix_sep = st.text_input("split character:", placeholder="e.g. _ or -")\n    st.divider()\n    st.header("3. ignore columns")\n    global_ignore_str = st.text_area("global ignore:", "LoadDate, Timestamp, RunID")\n    ignore_list = [x.strip() for x in global_ignore_str.split(",") if x.strip()]\n\nif zip_ref and zip_target:\n    valid_ref, ref_message = validate_zip_upload(zip_ref, "old ZIP")\n    valid_target, target_message = validate_zip_upload(zip_target, "new ZIP")\n\n    if not valid_ref or not valid_target:\n        if ref_message:\n            st.error(ref_message)\n        if target_message:\n            st.error(target_message)\n        st.stop()\n\n    ref_files_raw = get_file_list(zip_ref)\n    tgt_files_raw = get_file_list(zip_target)\n    ref_map, ref_collisions = build_normalized_map(ref_files_raw, ref_prefix, suffix_sep)\n    tgt_map, tgt_collisions = build_normalized_map(tgt_files_raw, tgt_prefix, suffix_sep)\n    common_keys = sorted(set(ref_map.keys()).intersection(set(tgt_map.keys())))\n\n    if ref_collisions or tgt_collisions:\n        st.warning(\n            "⚠️ some files collapse to the same normalized name and were excluded from auto-matching. "\n            "use manual force-pairing for those cases."\n        )\n        if ref_collisions:\n            st.caption(f"old-side collisions: {\\', \\'.join(sorted(ref_collisions.keys()))}")\n        if tgt_collisions:\n            st.caption(f"new-side collisions: {\\', \\'.join(sorted(tgt_collisions.keys()))}")\n\n    st.divider()\n    st.subheader(f"global status report ({len(common_keys)} pairs)")\n\n    if common_keys:\n        status_data = []\n        p_bar = st.progress(0)\n        for i, key in enumerate(common_keys):\n            d1 = read_csv_from_zip(zip_ref, ref_map[key])\n            d2 = read_csv_from_zip(zip_target, tgt_map[key])\n            status, diff_df = compare_dataframes(d1, d2, ignore_list)\n            status_data.append(\n                {\n                    "normalized name": key,\n                    "status": status,\n                    "rows old": d1.shape[0] if d1 is not None else 0,\n                    "rows new": d2.shape[0] if d2 is not None else 0,\n                    "diff rows": len(diff_df) if diff_df is not None else 0,\n                    "old file": ref_map[key],\n                    "new file": tgt_map[key],\n                }\n            )\n            p_bar.progress((i + 1) / len(common_keys))\n        p_bar.empty()\n\n        def color_status(value):\n            if "Match" in value:\n                return "background-color: #d4edda"\n            if "Error" in value:\n                return "background-color: #f8d7da"\n            return "background-color: #fff3cd"\n\n        st.dataframe(pd.DataFrame(status_data).style.map(color_status, subset=["status"]), use_container_width=True)\n    else:\n        st.warning("⚠️ no auto-matches found. check settings or use manual pairing.")\n\n    st.divider()\n    st.header("🔍 detailed inspection")\n    t_auto, t_manual = st.tabs(["auto-matched files", "manual force-pairing"])\n\n    with t_auto:\n        if common_keys:\n            selected_key = st.selectbox("select pair:", common_keys)\n            if st.button(f"compare: {selected_key}"):\n                d1 = read_csv_from_zip(zip_ref, ref_map[selected_key])\n                d2 = read_csv_from_zip(zip_target, tgt_map[selected_key])\n                display_diff_results(\n                    *compare_dataframes(d1, d2, ignore_list),\n                    d1.shape[0] if d1 is not None else 0,\n                    d2.shape[0] if d2 is not None else 0,\n                )\n\n    with t_manual:\n        if ref_files_raw and tgt_files_raw:\n            c1, c2 = st.columns(2)\n            manual_ref = c1.selectbox("select old file:", ref_files_raw)\n            manual_target = c2.selectbox("select new file:", tgt_files_raw)\n            if st.button("compare selected files"):\n                d1 = read_csv_from_zip(zip_ref, manual_ref)\n                d2 = read_csv_from_zip(zip_target, manual_target)\n                display_diff_results(\n                    *compare_dataframes(d1, d2, ignore_list),\n                    d1.shape[0] if d1 is not None else 0,\n                    d2.shape[0] if d2 is not None else 0,\n                )\n        else:\n            st.info("both ZIP files need at least one CSV for manual pairing.")\nelse:\n    st.info("👈 please upload ZIP files to start.")\n'


MAX_ZIP_UPLOAD_MB = 200
MAX_TOTAL_UNCOMPRESSED_MB = 500
MAX_CSV_FILE_MB = 100
MAX_CSV_COUNT = 500
MAX_COMPRESSION_RATIO = 100


def mb_to_bytes(value):
    return value * 1024 * 1024


def reset_uploads():
    st.session_state["upload_version"] = st.session_state.get("upload_version", 0) + 1


def get_upload_size(uploaded_file):
    size = getattr(uploaded_file, "size", None)
    if size is not None:
        return size
    try:
        return uploaded_file.getbuffer().nbytes
    except Exception:
        return 0


def check_password():
    if "app_password" not in st.secrets:
        return True

    def password_entered():
        if st.session_state["password"] == st.secrets["app_password"]:
            st.session_state["password_correct"] = True
            del st.session_state["password"]
        else:
            st.session_state["password_correct"] = False

    if "password_correct" not in st.session_state:
        st.title("🔒 login required")
        st.text_input(
            "to use the cloud version, enter the password:",
            type="password",
            on_change=password_entered,
            key="password",
        )
        return False

    if not st.session_state["password_correct"]:
        st.title("🔒 login required")
        st.text_input(
            "password incorrect. try again:",
            type="password",
            on_change=password_entered,
            key="password",
        )
        return False

    return True


def validate_zip_upload(zip_file, label):
    """Validate a ZIP before reading CSV data from it.

    The app never extracts files to disk, but these checks reduce exposure to ZIP bombs,
    unexpectedly large uploads, encrypted archives, and confusing archive paths.
    """
    upload_size = get_upload_size(zip_file)
    if upload_size > mb_to_bytes(MAX_ZIP_UPLOAD_MB):
        return False, f"{label} is larger than {MAX_ZIP_UPLOAD_MB} MB."

    try:
        zip_file.seek(0)
        with zipfile.ZipFile(zip_file) as archive:
            csv_infos = []
            total_uncompressed_size = 0

            for info in archive.infolist():
                name = info.filename
                normalized_path = os.path.normpath(name)

                if info.flag_bits & 0x1:
                    return False, f"{label} contains an encrypted file, which is not supported."

                if normalized_path.startswith("..") or os.path.isabs(name):
                    return False, f"{label} contains an unsafe file path."

                if info.is_dir() or name.startswith("__MACOSX") or not name.lower().endswith(".csv"):
                    continue

                csv_infos.append(info)
                total_uncompressed_size += info.file_size

                if info.file_size > mb_to_bytes(MAX_CSV_FILE_MB):
                    return False, f"{label} contains a CSV larger than {MAX_CSV_FILE_MB} MB: {os.path.basename(name)}"

                compression_ratio = info.file_size / max(info.compress_size, 1)
                if compression_ratio > MAX_COMPRESSION_RATIO and info.file_size > mb_to_bytes(10):
                    return False, f"{label} contains a suspiciously compressed CSV: {os.path.basename(name)}"

            if len(csv_infos) > MAX_CSV_COUNT:
                return False, f"{label} contains more than {MAX_CSV_COUNT} CSV files."

            if total_uncompressed_size > mb_to_bytes(MAX_TOTAL_UNCOMPRESSED_MB):
                return False, f"{label} expands to more than {MAX_TOTAL_UNCOMPRESSED_MB} MB of CSV data."

            if not csv_infos:
                return False, f"{label} does not contain any CSV files."

        zip_file.seek(0)
        return True, ""
    except zipfile.BadZipFile:
        return False, f"{label} is not a valid ZIP file."
    except Exception:
        return False, f"{label} could not be validated."


def get_file_list(zip_file):
    try:
        zip_file.seek(0)
        with zipfile.ZipFile(zip_file) as archive:
            return sorted(
                info.filename
                for info in archive.infolist()
                if not info.is_dir()
                and info.filename.lower().endswith(".csv")
                and not info.filename.startswith("__MACOSX")
            )
    except Exception:
        return []


def normalize_filename(filename, prefix="", suffix_delimiter=""):
    base = os.path.basename(filename)
    name_no_ext = os.path.splitext(base)[0]
    if prefix and name_no_ext.startswith(prefix):
        name_no_ext = name_no_ext[len(prefix):]
    if suffix_delimiter and suffix_delimiter in name_no_ext:
        name_no_ext = name_no_ext.rsplit(suffix_delimiter, 1)[0]
    return name_no_ext


def build_normalized_map(file_list, prefix="", suffix_delimiter=""):
    file_map = {}
    collisions = {}

    for file_name in file_list:
        normalized_name = normalize_filename(file_name, prefix, suffix_delimiter)
        if normalized_name in file_map:
            collisions.setdefault(normalized_name, [file_map[normalized_name]]).append(file_name)
            file_map.pop(normalized_name, None)
        elif normalized_name not in collisions:
            file_map[normalized_name] = file_name

    return file_map, collisions


def read_csv_from_zip(zip_file, full_path):
    try:
        zip_file.seek(0)
        with zipfile.ZipFile(zip_file) as archive:
            info = archive.getinfo(full_path)
            if info.file_size == 0:
                return pd.DataFrame()
            if info.file_size > mb_to_bytes(MAX_CSV_FILE_MB):
                return None

            raw_bytes = archive.read(full_path)
            try:
                return pd.read_csv(io.BytesIO(raw_bytes))
            except pd.errors.EmptyDataError:
                return pd.DataFrame()
            except UnicodeDecodeError:
                return pd.read_csv(io.BytesIO(raw_bytes), encoding="latin1")
    except Exception:
        return None


def compare_dataframes(df1, df2, ignore_cols):
    if df1 is None or df2 is None:
        return "❌ Error", None

    df1 = df1.drop(columns=[c for c in ignore_cols if c in df1.columns], errors="ignore").copy()
    df2 = df2.drop(columns=[c for c in ignore_cols if c in df2.columns], errors="ignore").copy()

    if set(df1.columns) != set(df2.columns):
        return "⚠️ Schema Diff", None

    df1 = df1.sort_index(axis=1)
    df2 = df2.sort_index(axis=1)
    compare_cols = list(df1.columns)

    old_counts = df1.value_counts(dropna=False).rename("old_count").reset_index()
    new_counts = df2.value_counts(dropna=False).rename("new_count").reset_index()
    merged = old_counts.merge(new_counts, on=compare_cols, how="outer").fillna(0)
    merged["old_count"] = merged["old_count"].astype(int)
    merged["new_count"] = merged["new_count"].astype(int)

    changed_rows = merged[merged["old_count"] != merged["new_count"]]
    if changed_rows.empty:
        return "✅ Match", pd.DataFrame()

    diff_rows = []
    for _, row in changed_rows.iterrows():
        values = {col: row[col] for col in compare_cols}
        removed_count = max(row["old_count"] - row["new_count"], 0)
        added_count = max(row["new_count"] - row["old_count"], 0)
        diff_rows.extend([{**values, "_source": "OLD"} for _ in range(removed_count)])
        diff_rows.extend([{**values, "_source": "NEW"} for _ in range(added_count)])

    return "⚠️ Data Mismatch", pd.DataFrame(diff_rows)


def display_diff_results(status, diff_df, rows_old, rows_new):
    m1, m2, m3 = st.columns(3)
    m1.metric("rows in old file", rows_old)
    m2.metric("rows in new file", rows_new, delta=(rows_new - rows_old))

    if status == "✅ Match":
        m3.metric("status", "match")
        st.success("✅ files match after applying the selected ignore columns.")
    elif status == "⚠️ Schema Diff":
        m3.metric("status", "schema diff", delta_color="inverse")
        st.error("⚠️ column names do not match.")
    elif diff_df is not None:
        diff_count = len(diff_df)
        m3.metric("rows with differences", diff_count, delta_color="inverse")
        st.warning(f"⚠️ found {diff_count} differing row instance(s).")
        removed = diff_df[diff_df["_source"] == "OLD"].drop(columns=["_source"])
        added = diff_df[diff_df["_source"] == "NEW"].drop(columns=["_source"])
        t1, t2 = st.tabs([f"rows removed ({len(removed)})", f"rows added ({len(added)})"])
        with t1:
            st.dataframe(removed, use_container_width=True)
        with t2:
            st.dataframe(added, use_container_width=True)


def show_local_download_panel():
    st.header("run locally for stronger privacy")
    st.markdown(
        """
        The cloud/community version is convenient, but uploaded files are still processed by the remote server.
        For sensitive CSVs, download the standalone script below and run it on your own machine instead.
        """
    )
    st.download_button(
        label="download standalone local script",
        data=LOCAL_SCRIPT_CONTENT,
        file_name="local_csv_tool.py",
        mime="text/x-python",
    )
    st.code("pip install streamlit pandas\nstreamlit run local_csv_tool.py", language="bash")


st.title("📊 refresh csv comparison tool")

with st.expander("how to use this app", expanded=True):
    st.markdown(
        """
        1. choose the safest run mode for your data:
           - **cloud/community mode:** convenient, but uploaded ZIP/CSV content is processed on the server running this app.
           - **local mode:** preferred for confidential or sensitive CSVs; download the standalone script from the sidebar and run it on your own computer.
        2. upload the **old/reference ZIP** and the **new/target ZIP** in the sidebar.
        3. adjust the auto-match settings if filenames include prefixes or suffixes that should be ignored.
        4. list columns to ignore, such as load timestamps or run IDs.
        5. review the global status report, then inspect any mismatches in detail.
        6. use **clear uploaded data** when finished to reset upload widgets and session state.

        **privacy and security best practices**
        - use the local version for highly sensitive, regulated, client, financial, HR, or legal data.
        - do not upload files unless you are comfortable with the server processing their plaintext contents in memory.
        - this app does not intentionally write uploaded files to disk, does not cache uploaded file contents, and does not make outbound API calls.
        - ZIP validation blocks encrypted ZIP entries, unsafe archive paths, oversized CSVs, too many CSVs, and suspicious compression ratios.
        - avoid adding logging, analytics, caching, or third-party integrations that could retain file contents.
        - keep the Streamlit Community Cloud app private where possible, and use `st.secrets` for the optional app password.
        """
    )

with st.expander("privacy note", expanded=False):
    st.markdown(
        """
        This app reduces retention by processing uploaded files in memory and avoiding Streamlit caching for uploaded content.
        That is not the same as end-to-end or zero-knowledge privacy: in the cloud version, the server still receives and processes the uploaded data.
        """
    )

with st.sidebar:
    st.title("csv tool")
    with st.expander("run locally", expanded=True):
        st.caption("best option for sensitive files")
        st.download_button(
            label="download standalone local script",
            data=LOCAL_SCRIPT_CONTENT,
            file_name="local_csv_tool.py",
            mime="text/x-python",
            key="sidebar_local_download",
        )
        st.markdown("`pip install streamlit pandas`")
        st.markdown("`streamlit run local_csv_tool.py`")
    st.divider()

if not check_password():
    st.stop()

with st.sidebar:
    st.button("clear uploaded data", on_click=reset_uploads)
    st.divider()
    st.header("1. upload files")
    upload_version = st.session_state.get("upload_version", 0)
    zip_ref = st.file_uploader("upload reference / old zip", type="zip", key=f"zip_ref_{upload_version}")
    zip_target = st.file_uploader("upload target / new zip", type="zip", key=f"zip_target_{upload_version}")
    st.divider()
    st.header("2. auto-match logic")
    ref_prefix = st.text_input("remove from old:", placeholder="e.g. kaplan-")
    tgt_prefix = st.text_input("remove from new:", placeholder="e.g. newkaplan-")
    suffix_sep = st.text_input("split character:", placeholder="e.g. _ or -")
    st.divider()
    st.header("3. ignore columns")
    global_ignore_str = st.text_area("global ignore:", "LoadDate, Timestamp, RunID")
    ignore_list = [x.strip() for x in global_ignore_str.split(",") if x.strip()]

if zip_ref and zip_target:
    valid_ref, ref_message = validate_zip_upload(zip_ref, "old ZIP")
    valid_target, target_message = validate_zip_upload(zip_target, "new ZIP")

    if not valid_ref or not valid_target:
        if ref_message:
            st.error(ref_message)
        if target_message:
            st.error(target_message)
        st.stop()

    ref_files_raw = get_file_list(zip_ref)
    tgt_files_raw = get_file_list(zip_target)
    ref_map, ref_collisions = build_normalized_map(ref_files_raw, ref_prefix, suffix_sep)
    tgt_map, tgt_collisions = build_normalized_map(tgt_files_raw, tgt_prefix, suffix_sep)
    common_keys = sorted(set(ref_map.keys()).intersection(set(tgt_map.keys())))

    if ref_collisions or tgt_collisions:
        st.warning(
            "⚠️ some files collapse to the same normalized name and were excluded from auto-matching. "
            "use manual force-pairing for those cases."
        )
        if ref_collisions:
            st.caption(f"old-side collisions: {', '.join(sorted(ref_collisions.keys()))}")
        if tgt_collisions:
            st.caption(f"new-side collisions: {', '.join(sorted(tgt_collisions.keys()))}")

    st.divider()
    st.subheader(f"global status report ({len(common_keys)} pairs)")

    if common_keys:
        status_data = []
        p_bar = st.progress(0)
        for i, key in enumerate(common_keys):
            d1 = read_csv_from_zip(zip_ref, ref_map[key])
            d2 = read_csv_from_zip(zip_target, tgt_map[key])
            status, diff_df = compare_dataframes(d1, d2, ignore_list)
            status_data.append(
                {
                    "normalized name": key,
                    "status": status,
                    "rows old": d1.shape[0] if d1 is not None else 0,
                    "rows new": d2.shape[0] if d2 is not None else 0,
                    "diff rows": len(diff_df) if diff_df is not None else 0,
                    "old file": ref_map[key],
                    "new file": tgt_map[key],
                }
            )
            p_bar.progress((i + 1) / len(common_keys))
        p_bar.empty()

        def color_status(value):
            if "Match" in value:
                return "background-color: #d4edda"
            if "Error" in value:
                return "background-color: #f8d7da"
            return "background-color: #fff3cd"

        st.dataframe(pd.DataFrame(status_data).style.map(color_status, subset=["status"]), use_container_width=True)
    else:
        st.warning("⚠️ no auto-matches found. check settings or use manual pairing.")

    st.divider()
    st.header("🔍 detailed inspection")
    t_auto, t_manual = st.tabs(["auto-matched files", "manual force-pairing"])

    with t_auto:
        if common_keys:
            selected_key = st.selectbox("select pair:", common_keys)
            if st.button(f"compare: {selected_key}"):
                d1 = read_csv_from_zip(zip_ref, ref_map[selected_key])
                d2 = read_csv_from_zip(zip_target, tgt_map[selected_key])
                display_diff_results(
                    *compare_dataframes(d1, d2, ignore_list),
                    d1.shape[0] if d1 is not None else 0,
                    d2.shape[0] if d2 is not None else 0,
                )

    with t_manual:
        if ref_files_raw and tgt_files_raw:
            c1, c2 = st.columns(2)
            manual_ref = c1.selectbox("select old file:", ref_files_raw)
            manual_target = c2.selectbox("select new file:", tgt_files_raw)
            if st.button("compare selected files"):
                d1 = read_csv_from_zip(zip_ref, manual_ref)
                d2 = read_csv_from_zip(zip_target, manual_target)
                display_diff_results(
                    *compare_dataframes(d1, d2, ignore_list),
                    d1.shape[0] if d1 is not None else 0,
                    d2.shape[0] if d2 is not None else 0,
                )
        else:
            st.info("both ZIP files need at least one CSV for manual pairing.")
else:
    show_local_download_panel()
    st.info("👈 please upload ZIP files to start, or download the local script to run privately on your computer.")
