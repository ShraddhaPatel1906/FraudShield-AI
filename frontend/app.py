import os
from pathlib import Path
from dotenv import load_dotenv
import pandas as pd
import plotly.express as px
import requests
import streamlit as st

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
API = os.getenv("API_URL", "http://127.0.0.1:8000").rstrip("/")
st.set_page_config(page_title="FraudShield AI", page_icon="🛡️", layout="wide")
st.title("🛡️ FraudShield AI")
st.write("Machine Learning Fraud Detection & Investigation Dashboard")
st.caption("Educational benchmark using anonymized ULB transactions — not for live banking decisions")

if "token" not in st.session_state:
    st.session_state.token = None
if not st.session_state.token:
    st.subheader("Welcome to FraudShield AI")
    signin_tab, signup_tab = st.tabs(["Sign in", "Create account"])
    with signin_tab:
        with st.form("login"):
            identifier = st.text_input("Your email address")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in", type="primary")
        if submitted:
            try:
                resp = requests.post(API + "/auth/login", json={"email": identifier, "password": password}, timeout=15)
                if resp.status_code == 401:
                    st.error("Incorrect email or password.")
                else:
                    resp.raise_for_status()
                    st.session_state.token = resp.json()["access_token"]
                    st.session_state.role = resp.json()["role"]
                    st.rerun()
            except requests.RequestException as exc:
                st.error(f"Backend connection failed: {exc}")
    with signup_tab:
        with st.form("register"):
            new_email = st.text_input("Your email address")
            new_password = st.text_input("Create password (minimum 12 characters)", type="password")
            confirm_password = st.text_input("Confirm password", type="password")
            create = st.form_submit_button("Create account")
        if create:
            if new_password != confirm_password:
                st.error("Passwords do not match.")
            else:
                try:
                    resp = requests.post(API + "/auth/register", json={"email": new_email, "password": new_password}, timeout=15)
                    if resp.status_code == 409:
                        st.error("This email is already registered. Sign in instead.")
                    elif resp.status_code == 422:
                        st.error("Enter a valid email and a password of at least 12 characters.")
                    else:
                        resp.raise_for_status()
                        st.success("Account created! Open the Sign in tab and use your email and password.")
                except requests.RequestException as exc:
                    st.error(f"Registration failed: {exc}")
    st.stop()
st.sidebar.caption("Role: " + str(st.session_state.get("role", "user")))
if st.sidebar.button("Log out"):
    st.session_state.token = None
    st.rerun()

def call(method, path, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = "Bearer " + st.session_state.token
    response = requests.request(method, API + path, headers=headers, timeout=120, **kwargs)
    if response.status_code == 401:
        st.session_state.token = None
        st.error("Session expired. Sign in again.")
        st.stop()
    response.raise_for_status()
    return response.json()

try:
    stats = call("GET", "/stats")
    a,b,c = st.columns(3)
    a.metric("Transactions scored", stats["total_predictions"])
    b.metric("Flagged for review", stats["flagged_for_review"])
    c.metric("Not flagged", stats["not_flagged"])
except requests.RequestException as exc:
    st.error(f"Backend unavailable: {exc}")
    st.stop()

tabs = st.tabs(["📊 Analytics", "📁 Bulk scoring", "🔎 Investigation", "🧠 Model evaluation", "🔐 Admin audit"])
with tabs[0]:
    rows = pd.DataFrame(call("GET", "/predictions?limit=500"))
    if not rows.empty:
        left,right = st.columns(2)
        left.plotly_chart(px.histogram(rows, x="risk_score", nbins=20, title="Fraud risk distribution"), use_container_width=True)
        right.plotly_chart(px.pie(rows, names="status", title="Investigation statuses"), use_container_width=True)
        st.dataframe(rows, use_container_width=True)
    else:
        st.info("Score some transactions first to populate analytics.")
with tabs[1]:
    st.write("Upload transaction CSV with Time, V1–V28, Amount. Max 10,000 rows / 8 MB. Class is optional and ignored for scoring.")
    st.caption("Tip: First train model, then try artifacts/demo_transactions.csv (curated demo, not representative of real fraud prevalence).")
    uploaded = st.file_uploader("Upload transaction CSV", type="csv")
    if uploaded and st.button("Analyze transactions", type="primary"):
        try:
            data = call("POST", "/predict-csv", files={"file": (uploaded.name, uploaded.getvalue(), "text/csv")})
            result = pd.DataFrame(data["results"])
            st.success(f"Scored {data['count']} rows")
            st.dataframe(result, use_container_width=True)
            st.download_button("Download predictions", result.to_csv(index=False), "predictions.csv", "text/csv")
        except requests.RequestException as exc:
            st.error(str(exc))
with tabs[2]:
    rows = pd.DataFrame(call("GET", "/predictions?limit=500"))
    if not rows.empty:
        st.dataframe(rows, use_container_width=True)
        choice = st.selectbox("Transaction ID", rows["id"].tolist())
        st.write("Feature contributions (SHAP) are explanations of model output, not causal evidence.")
        if st.button("Explain selected transaction"):
            try:
                explanation = call("GET", f"/explain/{choice}")
                fig = px.bar(pd.DataFrame(explanation["top_contributions"]), x="contribution", y="feature", orientation="h", title="Top SHAP contributions")
                st.plotly_chart(fig, use_container_width=True)
                st.caption(explanation["note"])
            except requests.RequestException as exc:
                st.error(f"SHAP unavailable: {exc}")
        if st.session_state.get("role") not in {"admin", "analyst"}:
            st.info("Only investigators can update cases.")
        else:
            with st.form("review"):
                status = st.selectbox("Investigation status", ["pending", "under_review", "confirmed_fraud", "false_alarm"])
                if st.form_submit_button("Save review"):
                    try:
                        call("PATCH", f"/predictions/{choice}/status", json={"status": status})
                        st.success("Review saved to PostgreSQL")
                    except requests.RequestException as exc:
                        st.error(str(exc))
    else:
        st.info("No transactions to investigate")
with tabs[3]:
    try:
        metrics = call("GET", "/model/metrics")
        st.write("Selected model:", metrics["model"])
        st.write("Validation-set comparison (no test-set model selection)")
        comparison = pd.DataFrame.from_dict(metrics["model_comparison"], orient="index")
        st.dataframe(comparison, use_container_width=True)
        st.metric("Test PR-AUC", f"{metrics['test_pr_auc']:.4f}")
        st.write("Test confusion matrix:", metrics["test_confusion_matrix"])
        st.caption(metrics["notice"])
    except requests.RequestException:
        st.info("Train the model to generate metrics")
with tabs[4]:
    if st.session_state.get("role") == "admin":
        st.dataframe(pd.DataFrame(call("GET", "/admin/audit")), use_container_width=True)
    else:
        st.info("Admin access required")
