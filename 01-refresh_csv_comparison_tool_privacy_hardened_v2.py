import io
import os
from pathlib import Path
import re
import zipfile

import pandas as pd
import streamlit as st

st.set_page_config(layout="wide", page_title="csv set comparator")

LOCAL_SCRIPT_PATH = Path(__file__).with_name("local_csv_tool_privacy_hardened_v2.py")
try:
    LOCAL_SCRIPT_CONTENT = LOCAL_SCRIPT_PATH.read_text(encoding="utf-8")
except OSError:
    LOCAL_SCRIPT_CONTENT = ""

MAX_ZIP_UPLOAD_MB = 200
MAX_TOTAL_UNCOMPRESSED_MB = 500
MAX_CSV_FILE_MB = 100
MAX_CSV_COUNT = 500
MAX_COMPRESSION_RATIO = 100
OLD_ROLE_SUFFIX_TOKENS = {"old", "reference", "ref", "before", "previous", "baseline"}
NEW_ROLE_SUFFIX_TOKENS = {"new", "target", "after", "updated", "current"}


def mb_to_bytes(value):
    return value * 1024 * 1024


def uploaded_name(uploaded_file):
    return getattr(uploaded_file, "name", "uploaded.zip")


def sanitize_filename_part(value):
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value)
    return cleaned.strip("_") or "report"


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
        st.title("login required")
        st.text_input(
            "to use the cloud version, enter the password:",
            type="password",
            on_change=password_entered,
            key="password",
        )
        return False

    if not st.session_state["password_correct"]:
        st.title("login required")
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


def strip_role_suffix_tokens(normalized_name, role):
    role_tokens = OLD_ROLE_SUFFIX_TOKENS if role == "old" else NEW_ROLE_SUFFIX_TOKENS
    parts = [part for part in re.split(r"[_\-\s]+", normalized_name) if part]
    if not parts:
        return normalized_name

    while len(parts) > 1 and parts[-1].lower() in role_tokens:
        parts.pop()

    stripped_name = "_".join(parts)
    return stripped_name or normalized_name


def strip_leading_date_token(normalized_name):
    return re.sub(r"^\d{8}[_\-\s]+", "", normalized_name)


def build_normalized_object_map(objects, name_getter, prefix="", suffix_delimiter=""):
    object_map = {}
    collisions = {}

    for obj in objects:
        object_name = name_getter(obj)
        normalized_name = normalize_filename(object_name, prefix, suffix_delimiter)
        if normalized_name in object_map:
            collisions.setdefault(normalized_name, [name_getter(object_map[normalized_name])]).append(object_name)
            object_map.pop(normalized_name, None)
        elif normalized_name not in collisions:
            object_map[normalized_name] = obj

    return object_map, collisions


def build_zip_pair_maps(old_zip_files, new_zip_files, old_zip_prefix, new_zip_prefix, zip_suffix_sep):
    old_zip_map, old_zip_collisions = build_normalized_object_map(
        old_zip_files,
        uploaded_name,
        old_zip_prefix,
        zip_suffix_sep,
    )
    new_zip_map, new_zip_collisions = build_normalized_object_map(
        new_zip_files,
        uploaded_name,
        new_zip_prefix,
        zip_suffix_sep,
    )
    common_zip_keys = sorted(set(old_zip_map.keys()).intersection(set(new_zip_map.keys())))

    if common_zip_keys:
        return {
            "old_zip_map": old_zip_map,
            "new_zip_map": new_zip_map,
            "old_zip_collisions": old_zip_collisions,
            "new_zip_collisions": new_zip_collisions,
            "common_zip_keys": common_zip_keys,
            "match_mode": "exact",
        }

    old_zip_map, old_zip_collisions = build_normalized_object_map(
        old_zip_files,
        lambda item: strip_leading_date_token(
            normalize_filename(uploaded_name(item), old_zip_prefix, zip_suffix_sep)
        ),
    )
    new_zip_map, new_zip_collisions = build_normalized_object_map(
        new_zip_files,
        lambda item: strip_leading_date_token(
            normalize_filename(uploaded_name(item), new_zip_prefix, zip_suffix_sep)
        ),
    )
    common_zip_keys = sorted(set(old_zip_map.keys()).intersection(set(new_zip_map.keys())))

    if common_zip_keys:
        return {
            "old_zip_map": old_zip_map,
            "new_zip_map": new_zip_map,
            "old_zip_collisions": old_zip_collisions,
            "new_zip_collisions": new_zip_collisions,
            "common_zip_keys": common_zip_keys,
            "match_mode": "date_prefix_fallback",
        }

    old_zip_map, old_zip_collisions = build_normalized_object_map(
        old_zip_files,
        lambda item: strip_role_suffix_tokens(
            strip_leading_date_token(normalize_filename(uploaded_name(item), old_zip_prefix, zip_suffix_sep)),
            "old",
        ),
    )
    new_zip_map, new_zip_collisions = build_normalized_object_map(
        new_zip_files,
        lambda item: strip_role_suffix_tokens(
            strip_leading_date_token(normalize_filename(uploaded_name(item), new_zip_prefix, zip_suffix_sep)),
            "new",
        ),
    )
    common_zip_keys = sorted(set(old_zip_map.keys()).intersection(set(new_zip_map.keys())))

    return {
        "old_zip_map": old_zip_map,
        "new_zip_map": new_zip_map,
        "old_zip_collisions": old_zip_collisions,
        "new_zip_collisions": new_zip_collisions,
        "common_zip_keys": common_zip_keys,
        "match_mode": "role_suffix_fallback" if common_zip_keys else "none",
    }


def drop_ignored_columns(df, ignore_cols):
    return df.drop(columns=[c for c in ignore_cols if c in df.columns], errors="ignore").copy()


def make_comparable_dataframe(df):
    comparable_df = df.sort_index(axis=1).copy()
    for column in comparable_df.columns:
        comparable_df[column] = comparable_df[column].astype("string").fillna("<blank>")
    return comparable_df


def build_row_count_diff(row_count_old, row_count_new):
    note = "all comparable columns were ignored; only row counts can be compared"
    removed_count = max(row_count_old - row_count_new, 0)
    added_count = max(row_count_new - row_count_old, 0)
    diff_rows = []
    diff_rows.extend([{"comparison note": note, "_source": "OLD"} for _ in range(removed_count)])
    diff_rows.extend([{"comparison note": note, "_source": "NEW"} for _ in range(added_count)])
    return pd.DataFrame(diff_rows)


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
        return "Error", None

    df1 = drop_ignored_columns(df1, ignore_cols)
    df2 = drop_ignored_columns(df2, ignore_cols)

    if set(df1.columns) != set(df2.columns):
        return "Schema Diff", None

    if len(df1.columns) == 0:
        if len(df1) == len(df2):
            return "Match", pd.DataFrame()
        return "Data Mismatch", build_row_count_diff(len(df1), len(df2))

    df1 = make_comparable_dataframe(df1)
    df2 = make_comparable_dataframe(df2)
    compare_cols = list(df1.columns)

    old_counts = df1.value_counts(dropna=False).rename("old_count").reset_index()
    new_counts = df2.value_counts(dropna=False).rename("new_count").reset_index()
    merged = old_counts.merge(new_counts, on=compare_cols, how="outer").fillna(0)
    merged["old_count"] = merged["old_count"].astype(int)
    merged["new_count"] = merged["new_count"].astype(int)

    changed_rows = merged[merged["old_count"] != merged["new_count"]]
    if changed_rows.empty:
        return "Match", pd.DataFrame()

    diff_rows = []
    for _, row in changed_rows.iterrows():
        values = {col: row[col] for col in compare_cols}
        removed_count = max(row["old_count"] - row["new_count"], 0)
        added_count = max(row["new_count"] - row["old_count"], 0)
        diff_rows.extend([{**values, "_source": "OLD"} for _ in range(removed_count)])
        diff_rows.extend([{**values, "_source": "NEW"} for _ in range(added_count)])

    return "Data Mismatch", pd.DataFrame(diff_rows)


def build_row_by_row_change_table(old_df, new_df, ignore_cols):
    if old_df is None or new_df is None:
        return pd.DataFrame()

    old_df = drop_ignored_columns(old_df, ignore_cols)
    new_df = drop_ignored_columns(new_df, ignore_cols)
    columns = sorted(set(old_df.columns).union(set(new_df.columns)))
    max_rows = max(len(old_df), len(new_df))
    change_rows = []

    for row_index in range(max_rows):
        has_old = row_index < len(old_df)
        has_new = row_index < len(new_df)

        for column in columns:
            old_has_column = column in old_df.columns
            new_has_column = column in new_df.columns
            old_value = old_df.iloc[row_index][column] if has_old and old_has_column else ""
            new_value = new_df.iloc[row_index][column] if has_new and new_has_column else ""

            if pd.isna(old_value):
                old_value = ""
            if pd.isna(new_value):
                new_value = ""

            if str(old_value) == str(new_value):
                continue

            change_rows.append(
                {
                    "row #": row_index + 1,
                    "column": column,
                    "old value": old_value,
                    "new value": new_value,
                }
            )

    return pd.DataFrame(change_rows)


def build_simple_change_table(old_diff, new_diff):
    columns = list(old_diff.columns.union(new_diff.columns, sort=False))
    old_aligned = old_diff.reindex(columns=columns).reset_index(drop=True)
    new_aligned = new_diff.reindex(columns=columns).reset_index(drop=True)
    max_rows = max(len(old_aligned), len(new_aligned))
    change_rows = []

    for row_index in range(max_rows):
        has_old = row_index < len(old_aligned)
        has_new = row_index < len(new_aligned)
        old_row = old_aligned.iloc[row_index] if has_old else pd.Series(dtype="object")
        new_row = new_aligned.iloc[row_index] if has_new else pd.Series(dtype="object")

        if has_old and has_new:
            change_type = "changed or rebalanced row"
        elif has_old:
            change_type = "only in old file"
        else:
            change_type = "only in new file"

        row_had_visible_change = False
        for column in columns:
            old_value = old_row.get(column, "") if has_old else ""
            new_value = new_row.get(column, "") if has_new else ""

            if pd.isna(old_value):
                old_value = ""
            if pd.isna(new_value):
                new_value = ""

            if has_old and has_new and str(old_value) == str(new_value):
                continue

            row_had_visible_change = True
            change_rows.append(
                {
                    "change #": row_index + 1,
                    "change type": change_type,
                    "column": column,
                    "old value": old_value,
                    "new value": new_value,
                }
            )

        if not row_had_visible_change:
            change_rows.append(
                {
                    "change #": row_index + 1,
                    "change type": change_type,
                    "column": "full row",
                    "old value": "duplicate count changed",
                    "new value": "duplicate count changed",
                }
            )

    return pd.DataFrame(change_rows)


def color_status(value):
    if "Match" in value:
        return "background-color: #d4edda"
    if "Error" in value:
        return "background-color: #f8d7da"
    return "background-color: #fff3cd"


def display_diff_results(status, diff_df, rows_old, rows_new, row_by_row_changes=None):
    m1, m2, m3 = st.columns(3)
    m1.metric("rows in old file", rows_old)
    m2.metric("rows in new file", rows_new, delta=(rows_new - rows_old))

    if status == "Match":
        m3.metric("status", "match")
        st.success("Files match after applying the selected ignore columns.")
    elif status == "Schema Diff":
        m3.metric("status", "schema diff", delta_color="inverse")
        st.error("Column names do not match.")
    elif diff_df is not None:
        diff_count = len(diff_df)
        m3.metric("rows with differences", diff_count, delta_color="inverse")
        st.warning(f"Found {diff_count} differing row instance(s).")
        old_diff = diff_df[diff_df["_source"] == "OLD"].drop(columns=["_source"])
        new_diff = diff_df[diff_df["_source"] == "NEW"].drop(columns=["_source"])
        st.subheader("changed cells")
        if row_by_row_changes is not None and not row_by_row_changes.empty:
            st.caption("Changed cells by original CSV row number, similar to a side-by-side text comparison.")
            st.dataframe(row_by_row_changes, use_container_width=True, hide_index=True)
        else:
            simple_change_table = build_simple_change_table(old_diff, new_diff)
            st.caption("Changed row instances after treating the CSV as an unordered set of rows.")
            st.dataframe(simple_change_table, use_container_width=True, hide_index=True)
        t1, t2 = st.tabs([f"old differing rows ({len(old_diff)})", f"new differing rows ({len(new_diff)})"])
        with t1:
            st.dataframe(old_diff, use_container_width=True)
        with t2:
            st.dataframe(new_diff, use_container_width=True)


def validate_uploaded_group(uploaded_files, label_prefix):
    errors = []
    valid_files = []

    for index, zip_file in enumerate(uploaded_files, start=1):
        label = f"{label_prefix} {index}: {uploaded_name(zip_file)}"
        is_valid, message = validate_zip_upload(zip_file, label)
        if is_valid:
            valid_files.append(zip_file)
        else:
            errors.append(message)

    return valid_files, errors


def analyze_zip_pair(
    old_zip,
    new_zip,
    zip_pair_key,
    old_csv_prefix,
    new_csv_prefix,
    csv_suffix_sep,
    ignore_list,
):
    old_csv_files = get_file_list(old_zip)
    new_csv_files = get_file_list(new_zip)
    old_csv_map, old_csv_collisions = build_normalized_object_map(
        old_csv_files,
        lambda item: item,
        old_csv_prefix,
        csv_suffix_sep,
    )
    new_csv_map, new_csv_collisions = build_normalized_object_map(
        new_csv_files,
        lambda item: item,
        new_csv_prefix,
        csv_suffix_sep,
    )
    common_csv_keys = sorted(set(old_csv_map.keys()).intersection(set(new_csv_map.keys())))

    status_rows = []
    for csv_key in common_csv_keys:
        old_df = read_csv_from_zip(old_zip, old_csv_map[csv_key])
        new_df = read_csv_from_zip(new_zip, new_csv_map[csv_key])
        status, diff_df = compare_dataframes(old_df, new_df, ignore_list)
        status_rows.append(
            {
                "zip pair": zip_pair_key,
                "normalized csv name": csv_key,
                "status": status,
                "rows old": old_df.shape[0] if old_df is not None else 0,
                "rows new": new_df.shape[0] if new_df is not None else 0,
                "diff rows": len(diff_df) if diff_df is not None else 0,
                "old zip": uploaded_name(old_zip),
                "new zip": uploaded_name(new_zip),
                "old file": old_csv_map[csv_key],
                "new file": new_csv_map[csv_key],
            }
        )

    return {
        "zip_pair_key": zip_pair_key,
        "old_zip": old_zip,
        "new_zip": new_zip,
        "old_zip_name": uploaded_name(old_zip),
        "new_zip_name": uploaded_name(new_zip),
        "old_csv_files": old_csv_files,
        "new_csv_files": new_csv_files,
        "old_csv_map": old_csv_map,
        "new_csv_map": new_csv_map,
        "old_csv_collisions": old_csv_collisions,
        "new_csv_collisions": new_csv_collisions,
        "common_csv_keys": common_csv_keys,
        "status_rows": status_rows,
    }


def build_summary_csv_bytes(status_rows):
    if not status_rows:
        return b""
    return pd.DataFrame(status_rows).to_csv(index=False).encode("utf-8")


def build_detailed_report_zip_bytes(pair_results, ignore_list):
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        summary_rows = []
        for pair_result in pair_results:
            summary_rows.extend(pair_result["status_rows"])

        if summary_rows:
            summary_csv = pd.DataFrame(summary_rows).to_csv(index=False)
            archive.writestr("summary_report.csv", summary_csv)

        for pair_result in pair_results:
            pair_folder = sanitize_filename_part(pair_result["zip_pair_key"])

            if pair_result["old_csv_collisions"] or pair_result["new_csv_collisions"]:
                collision_lines = []
                if pair_result["old_csv_collisions"]:
                    collision_lines.append("Old-side CSV name collisions:")
                    for key, values in sorted(pair_result["old_csv_collisions"].items()):
                        collision_lines.append(f"{key}: {', '.join(values)}")
                if pair_result["new_csv_collisions"]:
                    collision_lines.append("New-side CSV name collisions:")
                    for key, values in sorted(pair_result["new_csv_collisions"].items()):
                        collision_lines.append(f"{key}: {', '.join(values)}")
                archive.writestr(f"{pair_folder}/collision_notes.txt", "\n".join(collision_lines))

            for csv_key in pair_result["common_csv_keys"]:
                old_path = pair_result["old_csv_map"][csv_key]
                new_path = pair_result["new_csv_map"][csv_key]
                old_df = read_csv_from_zip(pair_result["old_zip"], old_path)
                new_df = read_csv_from_zip(pair_result["new_zip"], new_path)
                status, diff_df = compare_dataframes(old_df, new_df, ignore_list)
                csv_stub = sanitize_filename_part(csv_key)

                if status == "Data Mismatch" and diff_df is not None and not diff_df.empty:
                    old_diff = diff_df[diff_df["_source"] == "OLD"].drop(columns=["_source"])
                    new_diff = diff_df[diff_df["_source"] == "NEW"].drop(columns=["_source"])
                    archive.writestr(
                        f"{pair_folder}/{csv_stub}_old_differing_rows.csv",
                        old_diff.to_csv(index=False),
                    )
                    archive.writestr(
                        f"{pair_folder}/{csv_stub}_new_differing_rows.csv",
                        new_diff.to_csv(index=False),
                    )
                elif status == "Schema Diff" and old_df is not None and new_df is not None:
                    old_cols = pd.DataFrame({"old_columns": sorted(drop_ignored_columns(old_df, ignore_list).columns)})
                    new_cols = pd.DataFrame({"new_columns": sorted(drop_ignored_columns(new_df, ignore_list).columns)})
                    archive.writestr(
                        f"{pair_folder}/{csv_stub}_old_columns.csv",
                        old_cols.to_csv(index=False),
                    )
                    archive.writestr(
                        f"{pair_folder}/{csv_stub}_new_columns.csv",
                        new_cols.to_csv(index=False),
                    )

    buffer.seek(0)
    return buffer.getvalue()


def format_pair_label(pair_result):
    return f"{pair_result['zip_pair_key']} | {pair_result['old_zip_name']} vs {pair_result['new_zip_name']}"


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
        disabled=not LOCAL_SCRIPT_CONTENT,
    )
#>>>>>>>>>>>>>>>>
    st.markdown(
        """
        **How to run locally**

        1. Download the standalone script.
        2. If your browser saves it with extra numbers or parentheses such as `(1)` or `(2)`, rename it to `local_csv_tool.py`.
        3. Open a terminal in the folder containing the file.
        4. Run:

        ```bash
        streamlit run local_csv_tool.py
        ```
        """
    )
#<<<<<<<<<<<<<<<<

st.title("refresh csv comparison tool")

with st.expander("how to use this app", expanded=True):
    st.markdown(
        """
        1. choose the safest run mode for your data:
           - **cloud/community mode:** convenient, but uploaded ZIP/CSV content is processed on the server running this app.
           - **local mode:** preferred for confidential or sensitive CSVs; download the standalone script from the sidebar and run it on your own computer.
        2. upload one or more **old/reference ZIPs** and the matching **new/target ZIPs** in the sidebar.
        3. use the **ZIP auto-match logic** settings if old and new ZIP filenames need normalization before pairing.
           The app can also fall back to common trailing role words such as `old`, `reference`, `new`, and `target`.
        4. use the **CSV auto-match logic** settings if filenames inside the ZIPs need normalization before pairing.
        5. list columns to ignore, such as load timestamps, run IDs, or batch IDs.
        6. review the **global status report** across all auto-paired ZIP sets.
        7. download the summary CSV or full report ZIP if you want to share or archive the results.
        8. inspect individual CSV comparisons in detail, or use manual ZIP/CSV pairing when needed.
        9. use **clear uploaded data** when finished to reset upload widgets and session state.

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

#>>>>>>>>>>>>>>>>
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
            disabled=not LOCAL_SCRIPT_CONTENT,
        )
        st.markdown(
            """
            **run locally**

            1. download the script.
            2. if the filename includes extra numbers or parentheses such as `(1)` or `(2)`, rename it to `local_csv_tool.py` (or just remove the parantheses and keep that number).
            3. open a terminal in the folder containing the file.
            4. run `streamlit run local_csv_tool.py`, or `local_csv_tool3.py` if you removed the parantheses around the file copy number that windows adds when you have multiple files with the same name
            """
        )
    st.divider()
#<<<<<<<<<<<<<<<<

if not check_password():
    st.stop()

with st.sidebar:
    st.button("clear uploaded data", on_click=reset_uploads)
    st.divider()
    st.header("1. upload files")
    upload_version = st.session_state.get("upload_version", 0)
    old_zip_files = st.file_uploader(
        "upload reference / old zip files",
        type="zip",
        accept_multiple_files=True,
        key=f"old_zip_files_{upload_version}",
    )
    new_zip_files = st.file_uploader(
        "upload target / new zip files",
        type="zip",
        accept_multiple_files=True,
        key=f"new_zip_files_{upload_version}",
    )
    st.divider()
    st.header("2. ZIP auto-match logic")
    old_zip_prefix = st.text_input("remove from old ZIP names:", placeholder="e.g. old-")
    new_zip_prefix = st.text_input("remove from new ZIP names:", placeholder="e.g. new-")
    zip_suffix_sep = st.text_input("ZIP split character:", placeholder="e.g. _ or -")
    st.divider()
    st.header("3. CSV auto-match logic")
    old_csv_prefix = st.text_input("remove from old CSV names:", placeholder="e.g. kaplan-")
    new_csv_prefix = st.text_input("remove from new CSV names:", placeholder="e.g. newkaplan-")
    csv_suffix_sep = st.text_input("CSV split character:", placeholder="e.g. _ or -")
    st.divider()
    st.header("4. ignore columns")
    global_ignore_str = st.text_area("global ignore:", "LoadDate, Timestamp, RunID, LastModified")
    ignore_list = [item.strip() for item in global_ignore_str.split(",") if item.strip()]

if old_zip_files and new_zip_files:
    valid_old_zip_files, old_validation_errors = validate_uploaded_group(old_zip_files, "old ZIP")
    valid_new_zip_files, new_validation_errors = validate_uploaded_group(new_zip_files, "new ZIP")

    if old_validation_errors or new_validation_errors:
        for message in old_validation_errors + new_validation_errors:
            st.error(message)
        st.stop()

    zip_pairing = build_zip_pair_maps(
        valid_old_zip_files,
        valid_new_zip_files,
        old_zip_prefix,
        new_zip_prefix,
        zip_suffix_sep,
    )
    old_zip_map = zip_pairing["old_zip_map"]
    new_zip_map = zip_pairing["new_zip_map"]
    old_zip_collisions = zip_pairing["old_zip_collisions"]
    new_zip_collisions = zip_pairing["new_zip_collisions"]
    common_zip_keys = zip_pairing["common_zip_keys"]
    unmatched_old_zip_keys = sorted(set(old_zip_map.keys()).difference(set(new_zip_map.keys())))
    unmatched_new_zip_keys = sorted(set(new_zip_map.keys()).difference(set(old_zip_map.keys())))

    if zip_pairing["match_mode"] == "date_prefix_fallback":
        st.info("ZIP pairs were matched after removing leading backup dates such as `20260519-` and `20260529-`.")
    elif zip_pairing["match_mode"] == "role_suffix_fallback":
        st.info(
            "ZIP pairs were matched using fallback cleanup for leading backup dates and trailing role words such as "
            "`old`/`reference` and `new`/`target`."
        )

    if old_zip_collisions or new_zip_collisions:
        st.warning(
            "Some ZIP files collapse to the same normalized name and were excluded from auto-pairing. "
            "Use manual ZIP/CSV pairing for those cases."
        )
        if old_zip_collisions:
            st.caption(f"old-side ZIP collisions: {', '.join(sorted(old_zip_collisions.keys()))}")
        if new_zip_collisions:
            st.caption(f"new-side ZIP collisions: {', '.join(sorted(new_zip_collisions.keys()))}")

    if unmatched_old_zip_keys or unmatched_new_zip_keys:
        st.warning("Some ZIP files could not be auto-paired and were excluded from the batch report.")
        if unmatched_old_zip_keys:
            st.caption(f"unmatched old ZIP keys: {', '.join(unmatched_old_zip_keys)}")
        if unmatched_new_zip_keys:
            st.caption(f"unmatched new ZIP keys: {', '.join(unmatched_new_zip_keys)}")
        if zip_pairing["match_mode"] == "none":
            st.caption(
                "Tip: if your ZIP names start with different backup dates or end with phrases like "
                "`_old_reference` and `_new_target`, either use the ZIP auto-match fields or rely on fallback cleanup."
            )

    pair_results = []
    for zip_key in common_zip_keys:
        pair_results.append(
            analyze_zip_pair(
                old_zip_map[zip_key],
                new_zip_map[zip_key],
                zip_key,
                old_csv_prefix,
                new_csv_prefix,
                csv_suffix_sep,
                ignore_list,
            )
        )

    all_status_rows = []
    for pair_result in pair_results:
        all_status_rows.extend(pair_result["status_rows"])

    total_csv_pairs = len(all_status_rows)
    st.divider()
    st.subheader(f"global status report ({total_csv_pairs} csv pairs across {len(pair_results)} zip pairs)")

    if all_status_rows:
        summary_df = pd.DataFrame(all_status_rows)
        st.dataframe(summary_df.style.map(color_status, subset=["status"]), use_container_width=True)

        summary_csv_bytes = build_summary_csv_bytes(all_status_rows)
        detailed_report_zip_bytes = build_detailed_report_zip_bytes(pair_results, ignore_list)
        d1, d2 = st.columns(2)
        with d1:
            st.download_button(
                label="download summary CSV",
                data=summary_csv_bytes,
                file_name="comparison_summary.csv",
                mime="text/csv",
            )
        with d2:
            st.download_button(
                label="download detailed report ZIP",
                data=detailed_report_zip_bytes,
                file_name="comparison_report.zip",
                mime="application/zip",
            )
    else:
        st.warning("No auto-matched CSV pairs were found across the auto-paired ZIP sets.")

    st.divider()
    st.header("detailed inspection")
    t_auto, t_manual = st.tabs(["auto-matched zip pairs", "manual ZIP/CSV pairing"])

    with t_auto:
        if pair_results:
            pair_lookup = {pair_result["zip_pair_key"]: pair_result for pair_result in pair_results}
            selected_pair_key = st.selectbox(
                "select zip pair:",
                common_zip_keys,
                format_func=lambda key: format_pair_label(pair_lookup[key]),
            )
            selected_pair = pair_lookup[selected_pair_key]
            st.caption(f"old ZIP: {selected_pair['old_zip_name']}")
            st.caption(f"new ZIP: {selected_pair['new_zip_name']}")

            if selected_pair["old_csv_collisions"] or selected_pair["new_csv_collisions"]:
                st.info("This ZIP pair has CSV name collisions excluded from auto-matching.")

            if selected_pair["common_csv_keys"]:
                selected_csv_key = st.selectbox("select csv pair:", selected_pair["common_csv_keys"])
                if st.button(f"compare: {selected_csv_key}"):
                    old_df = read_csv_from_zip(selected_pair["old_zip"], selected_pair["old_csv_map"][selected_csv_key])
                    new_df = read_csv_from_zip(selected_pair["new_zip"], selected_pair["new_csv_map"][selected_csv_key])
                    display_diff_results(
                        *compare_dataframes(old_df, new_df, ignore_list),
                        old_df.shape[0] if old_df is not None else 0,
                        new_df.shape[0] if new_df is not None else 0,
                        build_row_by_row_change_table(old_df, new_df, ignore_list),
                    )
            else:
                st.info("No CSV files were auto-matched inside this ZIP pair.")
        else:
            st.info("No ZIP pairs were auto-matched.")

    with t_manual:
        if valid_old_zip_files and valid_new_zip_files:
            c1, c2 = st.columns(2)
            manual_old_zip = c1.selectbox(
                "select old ZIP:",
                valid_old_zip_files,
                format_func=uploaded_name,
            )
            manual_new_zip = c2.selectbox(
                "select new ZIP:",
                valid_new_zip_files,
                format_func=uploaded_name,
            )
            manual_old_csv_files = get_file_list(manual_old_zip)
            manual_new_csv_files = get_file_list(manual_new_zip)

            if manual_old_csv_files and manual_new_csv_files:
                c3, c4 = st.columns(2)
                manual_old_csv = c3.selectbox("select old CSV file:", manual_old_csv_files)
                manual_new_csv = c4.selectbox("select new CSV file:", manual_new_csv_files)
                if st.button("compare selected ZIP/CSV files"):
                    old_df = read_csv_from_zip(manual_old_zip, manual_old_csv)
                    new_df = read_csv_from_zip(manual_new_zip, manual_new_csv)
                    display_diff_results(
                        *compare_dataframes(old_df, new_df, ignore_list),
                        old_df.shape[0] if old_df is not None else 0,
                        new_df.shape[0] if new_df is not None else 0,
                        build_row_by_row_change_table(old_df, new_df, ignore_list),
                    )
            else:
                st.info("Both selected ZIP files need at least one CSV for manual pairing.")
else:
    show_local_download_panel()
    st.info("Please upload old/reference ZIP files and new/target ZIP files to start, or download the local script to run privately on your computer.")
