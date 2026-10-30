"""FraudShield FastAPI backend: PostgreSQL, JWT, ML and investigation APIs."""
import io
import os
from dotenv import load_dotenv
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Header
from pydantic import BaseModel, Field, EmailStr
from sqlalchemy import create_engine, Integer, Float, String, DateTime, select, func, Text, text, inspect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from passlib.context import CryptContext
from jose import jwt, JWTError
from datetime import timedelta
import shap

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
MODEL_PATH = Path(os.getenv("MODEL_PATH", str(ROOT / "artifacts/fraud_model.joblib")))
# Default is PostgreSQL from Docker; URL.create handles special chars in passwords.
from sqlalchemy.engine import URL
DB_URL = os.getenv("DATABASE_URL")
if not DB_URL:
    DB_URL = URL.create(
        "postgresql+psycopg2",
        username=os.getenv("POSTGRES_USER", "fraudshield"),
        password=os.getenv("POSTGRES_PASSWORD", ""),
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT", "5432")),
        database=os.getenv("POSTGRES_DB", "fraudshield"),
    )
if isinstance(DB_URL, str) and DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)
engine = create_engine(DB_URL, connect_args={"check_same_thread": False} if str(DB_URL).startswith("sqlite") else {}, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)

class Base(DeclarativeBase):
    pass

class Prediction(Base):
    __tablename__ = "predictions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    amount: Mapped[float] = mapped_column(Float)
    risk_score: Mapped[float] = mapped_column(Float)
    flagged: Mapped[int] = mapped_column(Integer)
    review_status: Mapped[str] = mapped_column(String(24), default="pending")
    feature_json: Mapped[str] = mapped_column(Text, default="{}")
    owner_username: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default="analyst")

class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(128))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

app = FastAPI(title="FraudShield AI API", version="0.1.0")

# JWT secret and initial admin credentials are required through environment variables.
SECRET = os.getenv("JWT_SECRET", "")
if len(SECRET) < 32:
    raise RuntimeError("Set JWT_SECRET to a random string of at least 32 characters")
pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

@app.on_event("startup")
def initialize():
    # Existing enhanced-version databases already have `predictions`; create_all
    # does not add columns to existing tables. Add the nullable owner column safely.
    if inspect(engine).has_table("predictions"):
        existing_columns = {c["name"] for c in inspect(engine).get_columns("predictions")}
        if "owner_username" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE predictions ADD COLUMN owner_username VARCHAR(80)"))
    Base.metadata.create_all(engine)
    # Optional bootstrap admin: public users instead register with their own email.
    admin_name = os.getenv("ADMIN_EMAIL", "").strip().lower()
    admin_password = os.getenv("ADMIN_PASSWORD", "")
    if admin_name or admin_password:
        if "@" not in admin_name or len(admin_password) < 12:
            raise RuntimeError("ADMIN_EMAIL must be an email and ADMIN_PASSWORD 12+ chars")
        with SessionLocal() as db:
            if not db.scalar(select(User).where(User.username == admin_name)):
                db.add(User(username=admin_name, password_hash=pwd.hash(admin_password), role="admin"))
                db.commit()

class Login(BaseModel):
    # JSON uses an email key for the frontend and Swagger API.
    email: EmailStr
    password: str

class Register(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)

@app.post("/auth/register", status_code=201)
def register(data: Register):
    email = str(data.email).strip().lower()
    with SessionLocal() as db:
        if db.scalar(select(User).where(User.username == email)):
            raise HTTPException(status_code=409, detail="Email already registered")
        db.add(User(username=email, password_hash=pwd.hash(data.password), role="user"))
        db.commit()
    return {"message": "Account created. You can sign in.", "email": email}

@app.post("/auth/login")
def login(data: Login):
    identifier = str(data.email).strip().lower()
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == identifier))
        if not user or not pwd.verify(data.password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        token = jwt.encode({"sub": user.username, "role": user.role,
            "exp": datetime.now(timezone.utc) + timedelta(hours=2)}, SECRET, algorithm="HS256")
        return {"access_token": token, "token_type": "bearer", "role": user.role}

def require_user(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Login required")
    try:
        payload = jwt.decode(authorization[7:], SECRET, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == payload.get("sub")))
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        return {"username": user.username, "role": user.role}

@app.get("/auth/me")
def me(user=Depends(require_user)):
    return {"email": user["username"], "role": user["role"]}

def require_admin(user=Depends(require_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user

class Transaction(BaseModel):
    Time: float = Field(ge=0)
    Amount: float = Field(ge=0)
    # Features V1..V28 are supplied in a separate object, matching Kaggle schema exactly.
    features: dict[str, float]

class StatusUpdate(BaseModel):
    status: str


def get_model():
    if not MODEL_PATH.exists():
        raise HTTPException(status_code=503, detail="Model missing: run FraudShield_ML_Training_Testing.ipynb first")
    return joblib.load(MODEL_PATH)


def score_frame(frame: pd.DataFrame):
    bundle = get_model()
    feats = bundle["features"]
    if any(c not in frame.columns for c in feats):
        raise HTTPException(status_code=422, detail="Missing required Time, Amount, or V1..V28 fields")
    try:
        X = frame[feats].apply(pd.to_numeric, errors="raise")
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Features must be numeric")
    if X.isna().any().any() or not np.isfinite(X.to_numpy()).all():
        raise HTTPException(status_code=422, detail="Missing/infinite values not allowed")
    scores = bundle["pipeline"].predict_proba(X)[:, 1]
    return scores, float(bundle["threshold"])


def persist(frame, scores, threshold, actor):
    with SessionLocal() as session:
        rows = []
        for amount, score in zip(frame["Amount"], scores):
            record = Prediction(amount=float(amount), risk_score=float(score), flagged=int(score >= threshold), feature_json=frame.iloc[len(rows)].to_json(), owner_username=actor)
            session.add(record)
            rows.append(record)
        session.flush()
        payload = [{"id": row.id, "risk_score": row.risk_score, "flagged": bool(row.flagged), "status": row.review_status} for row in rows]
        session.add(AuditLog(actor=actor, action=f"scored {len(rows)} transactions"))
        session.commit()
    return payload

@app.get("/health")
def health():
    return {"status": "ok", "model_loaded": MODEL_PATH.exists()}

@app.post("/predict")
def predict(tx: Transaction, user=Depends(require_user)):
    expected = {f"V{i}" for i in range(1, 29)}
    if set(tx.features) != expected:
        raise HTTPException(status_code=422, detail="features must contain exactly V1 through V28")
    frame = pd.DataFrame([{**tx.features, "Time": tx.Time, "Amount": tx.Amount}])
    scores, threshold = score_frame(frame)
    return {"threshold": threshold, "prediction": persist(frame, scores, threshold, user["username"])[0]}

@app.post("/predict-csv")
async def predict_csv(file: UploadFile = File(...), user=Depends(require_user)):
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Upload a .csv file")
    content = await file.read(8_000_001)
    if len(content) > 8_000_000:
        raise HTTPException(status_code=413, detail="CSV exceeds 8 MB demo limit")
    try:
        frame = pd.read_csv(io.BytesIO(content))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid CSV")
    if not 0 < len(frame) <= 10000:
        raise HTTPException(status_code=400, detail="Upload 1 to 10,000 rows")
    scores, threshold = score_frame(frame)
    return {"count": len(frame), "threshold": threshold, "results": persist(frame, scores, threshold, user["username"])}

@app.get("/predictions")
def predictions(limit: int = 100, user=Depends(require_user)):
    if not 1 <= limit <= 500:
        raise HTTPException(status_code=422, detail="limit must be 1..500")
    with SessionLocal() as session:
        query = select(Prediction)
        if user["role"] != "admin":
            query = query.where(Prediction.owner_username == user["username"])
        rows = session.scalars(query.order_by(Prediction.id.desc()).limit(limit)).all()
        return [{"id": r.id, "time": r.timestamp.isoformat(), "amount": r.amount,
                 "risk_score": r.risk_score, "flagged": bool(r.flagged), "status": r.review_status} for r in rows]

@app.patch("/predictions/{prediction_id}/status")
def update_status(prediction_id: int, update: StatusUpdate, user=Depends(require_user)):
    allowed = {"pending", "under_review", "confirmed_fraud", "false_alarm"}
    if update.status not in allowed:
        raise HTTPException(status_code=422, detail=f"Allowed: {sorted(allowed)}")
    if user["role"] not in {"admin", "analyst"}:
        raise HTTPException(status_code=403, detail="Only investigators can update case status")
    with SessionLocal() as session:
        row = session.get(Prediction, prediction_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Prediction not found")
        # A non-admin investigator cannot modify other owners' cases.
        if user["role"] != "admin" and row.owner_username != user["username"]:
            raise HTTPException(status_code=404, detail="Prediction not found")
        row.review_status = update.status
        session.add(AuditLog(actor=user["username"], action=f"case {prediction_id}: {update.status}"))
        session.commit()
    return {"id": prediction_id, "status": update.status}

@app.get("/stats")
def stats(user=Depends(require_user)):
    with SessionLocal() as session:
        query_count = select(func.count(Prediction.id))
        query_flagged = select(func.count(Prediction.id)).where(Prediction.flagged == 1)
        if user["role"] != "admin":
            query_count = query_count.where(Prediction.owner_username == user["username"])
            query_flagged = query_flagged.where(Prediction.owner_username == user["username"])
        count = session.scalar(query_count) or 0
        flagged = session.scalar(query_flagged) or 0
    return {"total_predictions": count, "flagged_for_review": flagged, "not_flagged": count - flagged}

@app.get("/explain/{prediction_id}")
def explain(prediction_id: int, user=Depends(require_user)):
    import json
    with SessionLocal() as db:
        row = db.get(Prediction, prediction_id)
        if row is None or (user["role"] != "admin" and row.owner_username != user["username"]):
            raise HTTPException(status_code=404, detail="Prediction not found")
        values = json.loads(row.feature_json)
    bundle = get_model()
    model = bundle["pipeline"]
    frame = pd.DataFrame([values])[bundle["features"]]
    # Explain in transformed feature space for logistic regression;
    # native tree SHAP for XGBoost. Scores are *contributions*, not causal evidence.
    try:
        if bundle.get("model_name") == "XGBoost":
            explanation = shap.TreeExplainer(model)(frame)
            contributions = explanation.values[0]
        else:
            scaled = model.named_steps["scaler"].transform(frame)
            explanation = shap.LinearExplainer(model.named_steps["classifier"], np.zeros((1, scaled.shape[1])))
            contributions = explanation.values[0]
        ranked = sorted(zip(bundle["features"], map(float, contributions)),
                        key=lambda item: abs(item[1]), reverse=True)[:10]
        return {"prediction_id": prediction_id, "model": bundle.get("model_name"),
                "top_contributions": [{"feature": k, "contribution": v} for k,v in ranked],
                "note": "SHAP contributions are in raw model output space, not probability percentages or causal proof."}
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Explanation unavailable; verify SHAP compatibility") from exc

@app.get("/admin/audit")
def audit(user=Depends(require_admin)):
    with SessionLocal() as db:
        entries = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(100)).all()
        return [{"actor": x.actor, "action": x.action, "time": x.timestamp.isoformat()} for x in entries]

@app.get("/model/metrics")
def metrics(user=Depends(require_user)):
    import json
    path = ROOT / "artifacts/metrics.json"
    if not path.exists():
        raise HTTPException(status_code=503, detail="Train model first")
    return json.loads(path.read_text())
