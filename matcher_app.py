# matcher_app.py
#
# Streamlit front-end for matcher.py.
# Requirements:
#   pip install streamlit python-dotenv pandas requests
#
# Secrets:
#   - Locally: .env (ignored by git)
#   - Streamlit Cloud: st.secrets, copied into os.environ

import os
import traceback
from urllib.parse import urlparse

import pandas as pd
import streamlit as st

# When running on Streamlit Cloud, st.secrets will contain your secrets.
# Copy them into environment variables so matcher.py can keep using os.getenv().
if st.secrets:
    for key, value in st.secrets.items():
        os.environ[str(key)] = str(value)

import matcher  # import AFTER secrets are pushed into os.environ


def extract_ids_from_spoke_url(url: str) -> tuple[str, str]:
    """
    Given a Spoke route URL like:
        https://dispatch.spoke.com/plans/KzhZnaFlYDdvjS4Uf2Ex/route/ARodhGeDwZkvr0EjoWKQ

    Return:
        (PLAN_ID, ROUTE_SID)
    """
    parsed = urlparse(url.strip())
    parts = [p for p in parsed.path.split("/") if p]

    try:
        plan_idx = parts.index("plans")
        plan_id = parts[plan_idx + 1]
    except (ValueError, IndexError):
        raise ValueError("Could not find PLAN_ID after 'plans/' in the URL path.")

    try:
        route_idx = parts.index("route")
        route_sid = parts[route_idx + 1]
    except (ValueError, IndexError):
        raise ValueError("Could not find ROUTE_SID after 'route/' in the URL path.")

    if not plan_id or not route_sid:
        raise ValueError("PLAN_ID or ROUTE_SID parsed as empty strings.")

    return plan_id, route_sid


def run_matching(plan_id: str, route_sid: str):
    """
    Wrapper around matcher.run_matching() to keep the app code clean.
    """
    (
        present,
        ambiguous,
        not_found,
        prepared,
        stops,
    ) = matcher.run_matching(plan_id.strip(), route_sid.strip())

    return present, ambiguous, not_found, prepared, stops


def render_table_with_links(df: pd.DataFrame):
    """
    Drop stopId and, if zoho_url exists, render it as a clickable link.
    """
    if "stopId" in df.columns:
        df = df.drop(columns=["stopId"])

    if "zoho_url" in df.columns:
        df = df.copy()

        def make_link(u):
            if isinstance(u, str) and u.strip():
                return f'<a href="{u}" target="_blank">Open in Zoho</a>'
            return ""

        df["zoho_url"] = df["zoho_url"].apply(make_link)
        st.markdown(df.to_html(escape=False, index=False), unsafe_allow_html=True)
    else:
        st.dataframe(df, use_container_width=True)


# ---------------------------
# Streamlit UI
# ---------------------------

st.set_page_config(page_title="Circuit → Zoho Matcher", layout="wide")

st.title("Circuit → Zoho Locatings Matcher")

st.markdown(
    """
This app:

1. Takes a **Spoke route URL** (from dispatch.spoke.com)  
2. Extracts the underlying **PLAN_ID** and **ROUTE_SID**  
3. Fetches **stops** from Circuit/Spoke  
4. Normalizes and parses addresses  
5. Queries **Zoho CRM** via COQL  
6. Classifies results into:
   - **Present** (exact match)
   - **Ambiguous** (multiple matches)
   - **Not Found**  
7. Displays the results in interactive tables (with direct Zoho links where available)
"""
)

with st.sidebar:
    st.header("Input")

    route_url = st.text_input(
        "Spoke route URL",
        help=(
            "Paste the full route URL from Spoke, e.g. "
            "https://dispatch.spoke.com/plans/KzhZnaFlYDdvjS4Uf2Ex/route/ARodhGeDwZkvr0EjoWKQ"
        ),
    )

    st.markdown("---")
    st.caption(
        "Zoho / Circuit credentials come from environment variables or Streamlit secrets. "
        "Users only need to paste the Spoke route URL."
    )

    run_button = st.button("Run matching")


if run_button:
    if not route_url:
        st.error("Please paste a Spoke route URL.")
    else:
        try:
            plan_id, route_sid = extract_ids_from_spoke_url(route_url)
        except ValueError as e:
            st.error(f"Could not parse PLAN_ID / ROUTE_SID from the URL: {e}")
        else:
            try:
                with st.spinner(
                    f"Running Circuit → Zoho matching pipeline for PLAN_ID={plan_id}, ROUTE_SID={route_sid}..."
                ):
                    present, ambiguous, not_found, prepared, stops = run_matching(
                        plan_id, route_sid
                    )

                # SUMMARY METRICS
                st.subheader("Summary")

                total_stops = len(stops)
                total_prepared = len(prepared)

                c0, c1, c2, c3, c4 = st.columns(5)
                with c0:
                    st.metric("Stops from Circuit", total_stops)
                with c1:
                    st.metric("Prepared for matching", total_prepared)
                with c2:
                    st.metric("Present (exact)", len(present))
                with c3:
                    st.metric("Ambiguous", len(ambiguous))
                with c4:
                    st.metric("Not Found", len(not_found))

                st.caption(f"PLAN_ID: {plan_id} • ROUTE_SID: {route_sid}")

                # TABS: detailed tables
                tab1, tab2, tab3, tab4 = st.tabs(
                    ["Present (exact)", "Ambiguous", "Not Found", "Prepared stops"]
                )

                with tab1:
                    st.markdown("#### Exact matches (Present)")
                    if present:
                        df_present = pd.DataFrame(present)
                        render_table_with_links(df_present)
                    else:
                        st.info("No exact matches.")

                with tab2:
                    st.markdown("#### Ambiguous matches")
                    if ambiguous:
                        df_amb = pd.DataFrame(ambiguous)
                        render_table_with_links(df_amb)
                    else:
                        st.info("No ambiguous matches.")

                with tab3:
                    st.markdown("#### Not found")
                    if not_found:
                        df_nf = pd.DataFrame(not_found)
                        if "stopId" in df_nf.columns:
                            df_nf = df_nf.drop(columns=["stopId"])
                        st.dataframe(df_nf, use_container_width=True)
                    else:
                        st.info("No not-found rows.")

                with tab4:
                    st.markdown("#### Prepared stops (normalized Circuit data)")
                    if prepared:
                        df_prepared = pd.DataFrame(prepared)
                        if "stopId" in df_prepared.columns:
                            df_prepared = df_prepared.drop(columns=["stopId"])
                        st.dataframe(df_prepared, use_container_width=True)
                    else:
                        st.info("No prepared data; check Circuit response.")

            except Exception as e:
                st.error("An error occurred during matching.")
                st.text_area(
                    "Details",
                    value="".join(
                        traceback.format_exception(type(e), e, e.__traceback__)
                    ),
                    height=300,
                )
else:
    st.info("Paste a Spoke route URL in the sidebar, then click **Run matching**.")