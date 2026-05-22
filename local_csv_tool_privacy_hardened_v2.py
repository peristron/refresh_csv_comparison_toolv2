import io
import os
import zipfile

import pandas as pd
import streamlit as st

st.set_page_config(layout="wide", page_title="csv set comparator (local)")

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


st.title("📊 refresh csv comparison tool")

with st.expander("how to use this app", expanded=True):
    st.markdown(
        """
        1. upload the **old/reference ZIP** and the **new/target ZIP** in the sidebar.
        2. adjust the auto-match settings if filenames include prefixes or suffixes that should be ignored.
        3. list columns to ignore, such as load timestamps or run IDs.
        4. review the global status report, then inspect any mismatches in detail.
        5. use **clear uploaded data** when finished to reset upload widgets and session state.

        **privacy and security best practices**
        - the local version keeps processing on the computer running this script.
        - uploaded files are read in memory and are not intentionally written to disk by this app.
        - do not add caching, logging, external API calls, or analytics that could retain file contents.
        - keep ZIPs reasonably small and only include CSVs needed for the comparison.
        - close the browser tab and terminal process after comparing sensitive files.
        """
    )

with st.sidebar:
    st.title("csv tool")
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
    st.info("👈 please upload ZIP files to start.")
