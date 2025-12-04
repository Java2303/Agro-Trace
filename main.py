from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from typing import List, Optional, Generator, Annotated
import random
import os
from datetime import datetime, timezone, timedelta
import json
import io
from fastapi.responses import StreamingResponse
import logging
import uuid
from dotenv import load_dotenv

# --- Módulo de Seguridad ---
from jose import JWTError, jwt
from passlib.context import CryptContext

# --- Fin Módulo de Seguridad ---

from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Text, Boolean, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
from sqlalchemy.dialects.postgresql import JSONB

# Cargar variables de entorno desde un archivo .env (para desarrollo local)
# Solo cargar si DATABASE_URL no está ya definida (ej. por Render)

# --- CONFIGURACIÓN DE SEGURIDAD ---
SECRET_KEY = os.getenv("SECRET_KEY", "a_very_secret_key_that_should_be_in_env") # ¡Cambia esto en producción!
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 # 24 horas

# Contexto para hashing de contraseñas
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Esquema OAuth2
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# --- Configuración de Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


# --- FIN CONFIGURACIÓN DE SEGURIDAD ---

if "DATABASE_URL" not in os.environ:
    load_dotenv(dotenv_path='database.env')

# --- Configuración de SQLAlchemy ---
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("FATAL ERROR: La variable de entorno DATABASE_URL no está configurada.")
 
# Forzar el uso de SSL para la conexión a PostgreSQL, necesario en Render
engine = create_engine(DATABASE_URL, connect_args={"sslmode": "require"} if DATABASE_URL.startswith("postgresql://") else {})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# MODELS
class Coordinate(BaseModel):
    x: float
    y: float

    # Validación: x e y deben ser números finitos (no NaN ni ±Infinity)
    @classmethod
    def __get_validators__(cls):
        yield from super().__get_validators__()
        yield cls._validate_finite

    @classmethod
    def _validate_finite(cls, v):
        # Pydantic llama al validador con la instancia ya construida para modelos
        # Por compatibilidad, soportamos dicts o instancias.
        import math
        if isinstance(v, dict):
            x = v.get('x')
            y = v.get('y')
        else:
            x = getattr(v, 'x', None)
            y = getattr(v, 'y', None)
        try:
            if not (isinstance(x, (int, float)) and isinstance(y, (int, float))):
                raise ValueError('x y y deben ser numéricos')
            if not (math.isfinite(float(x)) and math.isfinite(float(y))):
                raise ValueError('Las coordenadas deben ser números finitos (no NaN/Infinity)')
        except Exception as e:
            raise ValueError(str(e))
        return v

class PlotBase(BaseModel):
    name: str
    crop_type: str
    area_hectares: float
    certification_standard: Optional[str] = None
    coordinates: List[Coordinate]

    # Validación: mínimo 3 vértices en coordinates
    @classmethod
    def __get_validators__(cls):
        yield from super().__get_validators__()
        yield cls._validate_min_vertices

    @classmethod
    def _validate_min_vertices(cls, v):
        coords = getattr(v, 'coordinates', None) if not isinstance(v, dict) else v.get('coordinates')
        if coords is None:
            raise ValueError('coordinates es requerido')
        try:
            if len(coords) < 3:
                raise ValueError('Se requieren al menos 3 vértices en coordinates')
        except TypeError:
            raise ValueError('coordinates debe ser una lista de coordenadas')
        return v

class Plot(PlotBase):
    id: int
    status: str = "PENDIENTE"
    ph_level: Optional[float] = None
    nitrogen_level: Optional[float] = None

    class Config:
        orm_mode = True

class SoilAnalysisDataCreate(BaseModel):
    plot_id: int
    ph: float = Field(..., ge=0.0, le=14.0, description="Nivel de pH del suelo (0-14)")
    nitrogen: float = Field(..., ge=0.0, description="Nivel de Nitrógeno (N) en ppm")
    phosphorus: Optional[float] = Field(None, ge=0.0, description="Nivel de Fósforo (P) en ppm")
    potassium: Optional[float] = Field(None, ge=0.0, description="Nivel de Potasio (K) en ppm")
    organic_matter: Optional[float] = Field(None, ge=0.0, le=100.0, description="Materia Orgánica (%)")
    texture: Optional[str] = Field(None, description="Textura del suelo (ej. 'arenoso', 'arcilloso', 'limoso')")
    density: Optional[float] = Field(None, ge=0.0, description="Densidad aparente (g/cm³)")
    electrical_conductivity: Optional[float] = Field(None, ge=0.0, description="Conductividad eléctrica (dS/m)")

class SoilAnalysisData(SoilAnalysisDataCreate):
    id: int
    timestamp: datetime
    status_at_analysis: str # Estado de la parcela cuando se registró este análisis
    analysis_result_status: str # Estado de certificación derivado de este análisis

    class Config:
        orm_mode = True

class Alert(BaseModel):
    id: int
    plot_id: int
    timestamp: datetime
    type: str # e.g., "DEGRADACION", "EXCESO_USO", "PH_ANORMAL", "DEFICIENCIA_NITROGENO"
    message: str
    severity: str # e.g., "BAJA", "MEDIA", "ALTA"
    is_resolved: bool = False

    class Config:
        orm_mode = True

class AlertResolve(BaseModel):
    is_resolved: bool

class LandUseEventCreate(BaseModel):
    plot_id: int
    event_type: str = Field(..., description="Tipo de evento: SIEMBRA, FERTILIZACION, APLICACION_PESTICIDA, COSECHA, OTRO")
    event_date: datetime
    details: dict = Field(..., description="Detalles del evento en formato JSON")

class LandUseEvent(LandUseEventCreate):
    id: int
    
    class Config:
        orm_mode = True

class CertificateData(BaseModel):
    id: int
    uuid: uuid.UUID
    plot_id: int
    generated_at: datetime
    class Config:
        orm_mode = True

class UserBase(BaseModel):
    email: str = Field(..., example="user@example.com")
    full_name: str = Field(..., example="Juan Pérez")

class UserCreate(UserBase):
    password: str = Field(..., min_length=8)
    role: str = Field("productor", enum=["productor", "tecnico", "certificador", "administrador"])

class User(UserBase):
    id: int
    role: str
    is_active: bool

    class Config:
        orm_mode = True

class Token(BaseModel):
    access_token: str
    token_type: str


# --- Modelos ORM de SQLAlchemy ---
class PlotDB(Base):
    __tablename__ = "plots"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    crop_type = Column(String)
    area_hectares = Column(Float)
    coordinates = Column(JSONB)
    status = Column(String, default="PENDIENTE")
    ph_level = Column(Float, nullable=True)
    nitrogen_level = Column(Float, nullable=True)
    certification_standard = Column(String, nullable=True)
    
    soil_analyses = relationship("SoilAnalysisDB", back_populates="plot", cascade="all, delete-orphan")
    alerts = relationship("AlertDB", back_populates="plot", cascade="all, delete-orphan")
    land_use_events = relationship("LandUseEventDB", back_populates="plot", cascade="all, delete-orphan")
    certificates = relationship("CertificateDB", back_populates="plot", cascade="all, delete-orphan")

class UserDB(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    full_name = Column(String)
    hashed_password = Column(String, nullable=False)
    role = Column(String, default="productor") # productor, tecnico, certificador, administrador
    is_active = Column(Boolean, default=True)

class SoilAnalysisDB(Base):
    __tablename__ = "soil_analyses"
    id = Column(Integer, primary_key=True, index=True)
    plot_id = Column(Integer, ForeignKey("plots.id"))
    timestamp = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    ph = Column(Float)
    nitrogen = Column(Float)
    phosphorus = Column(Float, nullable=True)
    potassium = Column(Float, nullable=True)
    organic_matter = Column(Float, nullable=True)
    texture = Column(String, nullable=True)
    density = Column(Float, nullable=True)
    electrical_conductivity = Column(Float, nullable=True)
    status_at_analysis = Column(String)
    analysis_result_status = Column(String)
    
    plot = relationship("PlotDB", back_populates="soil_analyses")

class AlertDB(Base):
    __tablename__ = "alerts"
    id = Column(Integer, primary_key=True, index=True)
    plot_id = Column(Integer, ForeignKey("plots.id"))
    timestamp = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    type = Column(String, index=True)
    message = Column(Text)
    severity = Column(String)
    is_resolved = Column(Boolean, default=False)

    plot = relationship("PlotDB", back_populates="alerts")

class AuditDB(Base):
    __tablename__ = "audit"
    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    actor = Column(String, nullable=True)
    action = Column(String)
    target_type = Column(String, nullable=True)
    target_id = Column(String, nullable=True)
    details = Column(JSONB, nullable=True)

class LandUseEventDB(Base):
    __tablename__ = "land_use_events"
    id = Column(Integer, primary_key=True, index=True)
    plot_id = Column(Integer, ForeignKey("plots.id"), nullable=False)
    event_type = Column(String, index=True, nullable=False)
    event_date = Column(DateTime(timezone=True), nullable=False)
    details = Column(JSONB)

    plot = relationship("PlotDB", back_populates="land_use_events")

class CertificateDB(Base):
    __tablename__ = "certificates"
    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(String, unique=True, index=True, default=lambda: str(uuid.uuid4()), nullable=False)
    plot_id = Column(Integer, ForeignKey("plots.id", ondelete="CASCADE"), nullable=False)
    generated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    snapshot_data = Column(JSONB) # Guarda una copia de los datos al momento de generar

    plot = relationship("PlotDB", back_populates="certificates")

# --- Creación de Tablas ---
def init_db():
    Base.metadata.create_all(bind=engine)
# --- Gestión de Sesión de DB ---
def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

app = FastAPI(title="Agro Trace API")

# --- FUNCIONES DE SEGURIDAD ---
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def get_user(db: Session, email: str):
    return db.query(UserDB).filter(UserDB.email == email).first()

def authenticate_user(db: Session, email: str, password: str):
    user = get_user(db, email)
    if not user or not verify_password(password, user.hashed_password):
        return False
    return user
# --- Configuración de Umbrales ---
SOIL_THRESHOLDS = {
    'default': {'pH_min': 5.8, 'pH_max': 7.2, 'n_min': 150, 'p_min': 20, 'k_min': 150, 'mo_min': 1.5},
    'maiz':    {'pH_min': 5.8, 'pH_max': 7.0, 'n_min': 140, 'p_min': 25, 'k_min': 180, 'mo_min': 2.0},
    'maíz':    {'pH_min': 5.8, 'pH_max': 7.0, 'n_min': 140, 'p_min': 25, 'k_min': 180, 'mo_min': 2.0},
    'soja':    {'pH_min': 5.5, 'pH_max': 7.2, 'n_min': 120, 'p_min': 15, 'k_min': 100, 'mo_min': 1.8},
    'soya':    {'pH_min': 5.5, 'pH_max': 7.2, 'n_min': 120, 'p_min': 15, 'k_min': 100, 'mo_min': 1.8},
    'arroz':   {'pH_min': 5.0, 'pH_max': 6.8, 'n_min': 100, 'p_min': 10, 'k_min': 80, 'mo_min': 1.0}
}

# CORS (DEV)
origins = ["*"]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# --- Dependencia para obtener el usuario actual ---
async def get_current_user(token: Annotated[str, Depends(oauth2_scheme)], db: Session = Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=401,
        detail="No se pudieron validar las credenciales",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    user = get_user(db, email=email)
    if user is None:
        raise credentials_exception
    return user


def log_audit(db: Session, action: str, target_type: Optional[str] = None, target_id: Optional[str] = None, details: Optional[dict] = None, actor: Optional[str] = None):
    audit_log = AuditDB(
        actor=actor or 'system',
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        details=details or {}
    )
    db.add(audit_log)
    db.commit()

def check_for_alerts(db: Session, plot_id: int, analysis_data: SoilAnalysisDB, plot_crop_type: str):
    """
    Verifica si los resultados del análisis de suelo justifican la creación de una alerta.
    """
    crop_key = (plot_crop_type or '').strip().lower()
    cfg = SOIL_THRESHOLDS.get(crop_key, SOIL_THRESHOLDS['default'])

    alerts_to_add = []

    # Alerta por pH anormal
    if analysis_data.ph < cfg['pH_min'] - 0.5 or analysis_data.ph > cfg['pH_max'] + 0.5:
        alerts_to_add.append(AlertDB(
            plot_id=plot_id,
            type='PH_ANORMAL',
            message=f"pH ({analysis_data.ph}) muy fuera del rango óptimo ({cfg['pH_min']}-{cfg['pH_max']}) para {plot_crop_type}.",
            severity='ALTA'
        ))
    elif analysis_data.ph < cfg['pH_min'] or analysis_data.ph > cfg['pH_max']:
        alerts_to_add.append(AlertDB(
            plot_id=plot_id,
            type='PH_OBSERVADO',
            message=f"pH ({analysis_data.ph}) ligeramente fuera del rango óptimo ({cfg['pH_min']}-{cfg['pH_max']}) para {plot_crop_type}.",
            severity='MEDIA'
        ))

    # Alerta por deficiencia de Nitrógeno
    if analysis_data.nitrogen < cfg['n_min'] * 0.7:
        alerts_to_add.append(AlertDB(
            plot_id=plot_id,
            type='DEFICIENCIA_NITROGENO',
            message=f"Nivel de Nitrógeno ({analysis_data.nitrogen} ppm) muy bajo para {plot_crop_type} (mínimo {cfg['n_min']} ppm).",
            severity='ALTA'
        ))
    elif analysis_data.nitrogen < cfg['n_min']:
        alerts_to_add.append(AlertDB(
            plot_id=plot_id,
            type='NITROGENO_BAJO',
            message=f"Nivel de Nitrógeno ({analysis_data.nitrogen} ppm) bajo para {plot_crop_type} (mínimo {cfg['n_min']} ppm).",
            severity='MEDIA'
        ))

    # Alerta por baja materia orgánica
    if analysis_data.organic_matter is not None and analysis_data.organic_matter < cfg['mo_min'] * 0.8:
        alerts_to_add.append(AlertDB(
            plot_id=plot_id,
            type='BAJA_MATERIA_ORGANICA',
            message=f"Materia orgánica ({analysis_data.organic_matter}%) baja para {plot_crop_type} (mínimo {cfg['mo_min']}%).",
            severity='MEDIA'
        ))

    if alerts_to_add:
        db.add_all(alerts_to_add)
        db.commit()
        for alert in alerts_to_add:
            db.refresh(alert)
            log_audit(db, action='GENERAR_ALERTA', target_type='alerta', target_id=str(alert.id), details={'plot_id': plot_id, 'message': alert.message})

    return alerts_to_add

# --- ENDPOINTS DE AUTENTICACIÓN Y USUARIOS ---

@app.post("/token", response_model=Token)
async def login_for_access_token(form_data: Annotated[OAuth2PasswordRequestForm, Depends()], db: Session = Depends(get_db)):
    logging.info(f"Intento de inicio de sesión para el usuario: {form_data.username}")
    user = authenticate_user(db, email=form_data.username, password=form_data.password)
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Email o contraseña incorrectos",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token = create_access_token(data={"sub": user.email})
    logging.info(f"Inicio de sesión exitoso y token generado para: {form_data.username}")
    return {"access_token": access_token, "token_type": "bearer"}

@app.post("/users/register", response_model=User, status_code=201)
def register_user(user_data: UserCreate, db: Session = Depends(get_db)):
    db_user = get_user(db, email=user_data.email)
    logging.info(f"Intento de registro para el nuevo usuario: {user_data.email}")
    if db_user:
        raise HTTPException(status_code=400, detail="El email ya está registrado")
    
    db_user = UserDB(
        email=user_data.email,
        full_name=user_data.full_name,
        hashed_password=get_password_hash(user_data.password),
        role=user_data.role
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    logging.info(f"Usuario {user_data.email} registrado exitosamente con rol {user_data.role}.")
    return db_user

@app.get("/users/me", response_model=User)
async def read_users_me(current_user: Annotated[User, Depends(get_current_user)]):
    return current_user

# --- ENDPOINTS DE PARCELAS (Ahora protegidos) ---

@app.get("/plots/", response_model=List[Plot])
def list_plots(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Retorna la lista completa de parcelas registradas en Agro Trace."""
    plots = db.query(PlotDB).order_by(PlotDB.id.desc()).all()
    return plots

@app.post("/plots/", response_model=Plot, status_code=201)
def create_plot(plot_data: PlotBase, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Crea una nueva parcela con el polígono georreferenciado."""
    logging.info(f"Usuario '{current_user.email}' está creando una nueva parcela llamada '{plot_data.name}'.")
    coordinates_json = [c.dict() for c in plot_data.coordinates]
    new_plot_db = PlotDB(**plot_data.dict(exclude={'coordinates'}), coordinates=coordinates_json)
    db.add(new_plot_db)
    db.commit()
    db.refresh(new_plot_db)

    log_audit(db, actor=current_user.email, action='CREAR_PARCELA', target_type='parcela', target_id=str(new_plot_db.id), details={
        'name': new_plot_db.name,
        'crop_type': new_plot_db.crop_type,
        'area_hectares': new_plot_db.area_hectares
    })
    logging.info(f"Parcela '{new_plot_db.name}' (ID: {new_plot_db.id}) creada y registrada en auditoría.")
    
    return new_plot_db

@app.post("/plots/{plot_id}/analyze", response_model=SoilAnalysisData)
def analyze_plot(plot_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Simula un análisis geofísico y actualiza el estado de la parcela."""
    plot_to_analyze = db.query(PlotDB).filter(PlotDB.id == plot_id).first()
    logging.info(f"Usuario '{current_user.email}' ha solicitado un análisis para la parcela ID: {plot_id}.")
    if not plot_to_analyze:
        raise HTTPException(status_code=404, detail="Parcela no encontrada")

    # --- SIMULACIÓN DEL ANÁLISIS ---
    ph = round(random.uniform(4.5, 8.5), 2)
    nitrogen = round(random.uniform(30.0, 350.0), 2)
    phosphorus = round(random.uniform(10.0, 50.0), 2)
    potassium = round(random.uniform(50.0, 250.0), 2)
    organic_matter = round(random.uniform(0.5, 5.0), 2)
    texture = random.choice(["arenoso", "arcilloso", "limoso", "franco"])
    density = round(random.uniform(1.0, 1.8), 2)
    electrical_conductivity = round(random.uniform(0.1, 2.0), 2)

    crop_key = (plot_to_analyze.crop_type or '').strip().lower()
    cfg = SOIL_THRESHOLDS.get(crop_key, SOIL_THRESHOLDS['default'])

    analysis_result_status = 'PENDIENTE'
    if cfg['pH_min'] <= ph <= cfg['pH_max'] and nitrogen >= cfg['n_min']:
        analysis_result_status = 'CERTIFICADO'
    elif ph < cfg['pH_min'] or ph > cfg['pH_max']:
        analysis_result_status = 'OBSERVADO'

    analysis_data = {
        "plot_id": plot_id, "ph": ph, "nitrogen": nitrogen, "phosphorus": phosphorus,
        "potassium": potassium, "organic_matter": organic_matter, "texture": texture,
        "density": density, "electrical_conductivity": electrical_conductivity,
        "status_at_analysis": plot_to_analyze.status,
        "analysis_result_status": analysis_result_status
    }
    new_soil_analysis_db = SoilAnalysisDB(**analysis_data)
    db.add(new_soil_analysis_db)

    plot_to_analyze.status = analysis_result_status
    plot_to_analyze.ph_level = ph
    plot_to_analyze.nitrogen_level = nitrogen
    db.commit()
    db.refresh(new_soil_analysis_db)

    log_audit(db, actor=current_user.email, action='ANALIZAR_SUELO', target_type='parcela', target_id=str(plot_id), details={
        'analysis_id': new_soil_analysis_db.id, 'ph': ph, 'nitrogen': nitrogen,
        'status_result': analysis_result_status, 'applied_thresholds': cfg
    })

    check_for_alerts(db, plot_id, new_soil_analysis_db, plot_to_analyze.crop_type)
    logging.info(f"Análisis completado para la parcela ID: {plot_id}. Nuevo estado: {analysis_result_status}.")
    return new_soil_analysis_db

@app.delete("/plots/{plot_id}", status_code=200)
def delete_plot(plot_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Elimina una parcela por su ID."""
    plot_to_delete = db.query(PlotDB).filter(PlotDB.id == plot_id).first()
    if not plot_to_delete:
        logging.error(f"Intento de eliminar parcela no existente. ID: {plot_id} por usuario {current_user.email}")
        raise HTTPException(status_code=404, detail="Parcela no encontrada")

    log_audit(db, actor=current_user.email, action='ELIMINAR_PARCELA', target_type='parcela', target_id=str(plot_id), details={
        'name': plot_to_delete.name, 'crop_type': plot_to_delete.crop_type
    })

    db.delete(plot_to_delete)
    db.commit()
    logging.info(f"Usuario '{current_user.email}' eliminó la parcela '{plot_to_delete.name}' (ID: {plot_id}).")
    return {"message": f"Parcela {plot_id} eliminada correctamente."}

@app.get('/history')
def get_history(plot_id: Optional[int] = None, limit: int = 200, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Retorna las entradas de auditoría. Opcionalmente filtra por `plot_id`."""
    query = db.query(AuditDB).order_by(AuditDB.id.desc())
    if plot_id is not None:
        query = query.filter(AuditDB.target_id == str(plot_id))
    return query.limit(limit).all()

@app.get('/plots/{plot_id}/history')
def get_plot_history(plot_id: int, limit: int = 200, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Retorna el historial de auditoría para una parcela específica."""
    return db.query(AuditDB).filter(AuditDB.target_id == str(plot_id)).order_by(AuditDB.id.desc()).limit(limit).all()

@app.post("/plots/{plot_id}/land_use_events", response_model=LandUseEvent, status_code=201)
def create_land_use_event(plot_id: int, event: LandUseEventCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Registra un evento de uso de suelo (trazabilidad) para una parcela."""
    if event.plot_id != plot_id:
        raise HTTPException(status_code=400, detail="El plot_id en el cuerpo no coincide con el de la URL.")
    
    db_event = LandUseEventDB(**event.dict())
    db.add(db_event)
    db.commit()
    db.refresh(db_event)
    log_audit(db, actor=current_user.email, action=f"REGISTRAR_EVENTO_{event.event_type}", target_type='parcela', target_id=str(plot_id), details=event.details)
    return db_event

@app.post("/plots/{plot_id}/soil_analyses", response_model=SoilAnalysisData, status_code=201)
def create_manual_soil_analysis(plot_id: int, analysis_data: SoilAnalysisDataCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """
    Registra manualmente un análisis de suelo para una parcela (ej. datos de laboratorio).
    """
    if analysis_data.plot_id != plot_id:
        raise HTTPException(status_code=400, detail="El plot_id en el cuerpo no coincide con el plot_id de la URL.")

    plot = db.query(PlotDB).filter(PlotDB.id == plot_id).first()
    if not plot:
        raise HTTPException(status_code=404, detail="Parcela no encontrada")

    crop_key = (plot.crop_type or '').strip().lower()
    cfg = SOIL_THRESHOLDS.get(crop_key, SOIL_THRESHOLDS['default'])
    
    analysis_result_status = 'PENDIENTE'
    if cfg['pH_min'] <= analysis_data.ph <= cfg['pH_max'] and analysis_data.nitrogen >= cfg['n_min']:
        analysis_result_status = 'CERTIFICADO'
    elif analysis_data.ph < cfg['pH_min'] or analysis_data.ph > cfg['pH_max']:
        analysis_result_status = 'OBSERVADO'

    new_analysis_db = SoilAnalysisDB(
        **analysis_data.dict(),
        status_at_analysis=plot.status,
        analysis_result_status=analysis_result_status
    )
    db.add(new_analysis_db)
    
    plot.status = analysis_result_status
    plot.ph_level = analysis_data.ph
    plot.nitrogen_level = analysis_data.nitrogen
    
    db.commit()
    db.refresh(new_analysis_db)

    log_audit(db, actor=current_user.email, action='REGISTRAR_ANALISIS_MANUAL', target_type='parcela', target_id=str(plot_id), details={'analysis_id': new_analysis_db.id, 'ph': new_analysis_db.ph, 'nitrogen': new_analysis_db.nitrogen})
    check_for_alerts(db, plot_id, new_analysis_db, plot.crop_type)

    return new_analysis_db

@app.get("/plots/{plot_id}/soil_analyses", response_model=List[SoilAnalysisData])
def get_plot_soil_analyses(plot_id: int, limit: int = 100, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """
    Retorna el historial de análisis de suelo para una parcela (línea de tiempo).
    """
    return db.query(SoilAnalysisDB).filter(SoilAnalysisDB.plot_id == plot_id).order_by(SoilAnalysisDB.timestamp.desc()).limit(limit).all()

@app.get("/plots/{plot_id}/land_use_events", response_model=List[LandUseEvent])
def get_land_use_events(plot_id: int, limit: int = 200, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Retorna el historial de uso de suelo para una parcela."""
    plot = db.query(PlotDB).filter(PlotDB.id == plot_id).first()
    if not plot:
        raise HTTPException(status_code=404, detail="Parcela no encontrada")
    return db.query(LandUseEventDB).filter(LandUseEventDB.plot_id == plot_id).order_by(LandUseEventDB.event_date.desc()).limit(limit).all()

@app.get('/plots/{plot_id}/certificate')
def get_plot_certificate(plot_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Return PDF certificate for a plot."""
    plot = db.query(PlotDB).filter(PlotDB.id == plot_id).first()
    if not plot:
        raise HTTPException(status_code=404, detail="Parcela no encontrada")
    
    # Dependencias para QR y PDF
    try:
        import qrcode
        from qrcode.image.pil import PilImage
        from svglib.svglib import svg2rlg
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        from reportlab.lib.utils import ImageReader
        from reportlab.lib import colors
    except ImportError:
        raise HTTPException(status_code=503, detail="Dependencias 'reportlab', 'qrcode' o 'svglib' no instaladas.")

    # Crear registro del certificado en la DB
    snapshot = {
        "plot_name": plot.name,
        "crop_type": plot.crop_type,
        "area": plot.area_hectares,
        "status": plot.status,
        "ph": plot.ph_level,
        "nitrogen": plot.nitrogen_level,
        "standard": plot.certification_standard
    }
    new_cert_db = CertificateDB(plot_id=plot.id, snapshot_data=snapshot)
    db.add(new_cert_db)
    db.commit()
    db.refresh(new_cert_db)

    verification_url = f"https://agro-trace.onrender.com/verify/certificate/{new_cert_db.uuid}"

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    
    # --- Colores y Estilos ---
    COLOR_PRIMARY = colors.HexColor('#065F46') # Verde oscuro
    COLOR_SECONDARY = colors.HexColor('#10B981') # Verde brillante
    COLOR_TEXT = colors.HexColor('#1F2937') # Gris oscuro
    COLOR_LIGHT_TEXT = colors.HexColor('#6B7280') # Gris claro

    # --- Cabecera ---
    c.setFillColor(COLOR_PRIMARY)
    c.rect(0, height - 100, width, 100, stroke=0, fill=1)
    
    # Logo (usando svglib para leer el SVG)
    try:
        # Construir una ruta absoluta al archivo del logo
        script_dir = os.path.dirname(os.path.abspath(__file__))
        logo_path = os.path.join(script_dir, "www", "assets", "logo2.svg")

        logo = svg2rlg(logo_path)
        logo.width, logo.height = 60, 60
        logo.drawOn(c, 40, height - 80)
    except Exception as e:
        print(f"Error al cargar el logo SVG: {e}")
        c.setFont('Helvetica-Bold', 20)
        c.setFillColor(colors.white)
        c.drawString(40, height - 65, "Agro Trace")

    c.setFont('Helvetica-Bold', 24)
    c.setFillColor(colors.white)
    c.drawRightString(width - 40, height - 65, 'Certificado de Parcela')

    # --- Contenido Principal ---
    y_pos = height - 140
    c.setFillColor(COLOR_TEXT)
    
    def draw_field(label, value, y):
        c.setFont('Helvetica-Bold', 12)
        c.drawString(50, y, label)
        c.setFont('Helvetica', 12)
        c.drawString(200, y, str(value))
        return y - 25

    y_pos = draw_field('ID de Parcela:', plot.id, y_pos)
    y_pos = draw_field('Nombre de Parcela:', plot.name, y_pos)
    y_pos = draw_field('Tipo de Cultivo:', plot.crop_type, y_pos)
    y_pos = draw_field('Área:', f"{plot.area_hectares:.2f} Ha", y_pos)
    y_pos = draw_field('Estándar de Certificación:', plot.certification_standard or "No especificado", y_pos)
    y_pos = draw_field('Estado Actual:', plot.status, y_pos)

    y_pos -= 15 # Espacio extra
    c.setStrokeColor(COLOR_SECONDARY)
    c.line(50, y_pos, width - 50, y_pos)
    y_pos -= 30

    c.setFont('Helvetica-Bold', 14)
    c.drawString(50, y_pos, 'Resultados del Último Análisis')
    y_pos -= 25
    
    latest_analysis = db.query(SoilAnalysisDB).filter(SoilAnalysisDB.plot_id == plot_id).order_by(SoilAnalysisDB.id.desc()).first()
    if latest_analysis:
        y_pos = draw_field('Fecha de Análisis:', latest_analysis.timestamp.strftime("%d/%m/%Y %H:%M"), y_pos)
        y_pos = draw_field('Nivel de pH:', latest_analysis.ph, y_pos)
        y_pos = draw_field('Nivel de Nitrógeno:', f"{latest_analysis.nitrogen} ppm", y_pos)
    else:
        c.setFont('Helvetica-Oblique', 11)
        c.drawString(50, y_pos, 'No hay análisis de suelo registrados para esta parcela.')

    # Generar y añadir QR code
    qr_img = qrcode.make(verification_url, image_factory=PilImage)
    qr_buffer = io.BytesIO()
    qr_img.save(qr_buffer, format='PNG')
    qr_buffer.seek(0)
    
    qr_reader = ImageReader(qr_buffer)
    c.drawImage(qr_reader, width - 140, 50, width=100, height=100, mask='auto')
    c.setFont('Helvetica', 9)
    c.drawCentredString(width - 90, 40, "Verificar Autenticidad")

    # --- Pie de página ---
    c.setFillColor(COLOR_LIGHT_TEXT)
    c.setFont('Helvetica-Oblique', 8)
    c.drawString(50, 60, f'UUID del Certificado: {new_cert_db.uuid}')
    c.drawString(50, 50, f'Generado el: {new_cert_db.generated_at.strftime("%d/%m/%Y a las %H:%M:%S")} UTC')

    c.save()

    buffer.seek(0)
    filename = f'certificado_parcela_{plot.id}.pdf'
    return StreamingResponse(buffer, media_type='application/pdf', headers={'Content-Disposition': f'attachment; filename="{filename}"'})

@app.get("/verify/certificate/{cert_uuid}", response_model=CertificateData)
def verify_certificate(cert_uuid: uuid.UUID, db: Session = Depends(get_db)):
    """Endpoint público para verificar la autenticidad de un certificado por su UUID."""
    certificate = db.query(CertificateDB).filter(CertificateDB.uuid == str(cert_uuid)).first()
    if not certificate:
        raise HTTPException(status_code=404, detail="Certificado no encontrado o inválido.")
    
    # En una app real, aquí devolverías una página HTML bonita con los detalles.
    # Por ahora, devolvemos los datos del certificado.
    return certificate

@app.get("/alerts/", response_model=List[Alert])
def get_all_active_alerts(limit: int = 100, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Retorna todas las alertas activas en el sistema."""
    return db.query(AlertDB).filter(AlertDB.is_resolved == False).order_by(AlertDB.timestamp.desc()).limit(limit).all()

@app.get("/plots/{plot_id}/alerts", response_model=List[Alert])
def get_plot_alerts(plot_id: int, resolved: Optional[bool] = False, limit: int = 100, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Retorna las alertas para una parcela específica, filtrando por estado de resolución."""
    plot = db.query(PlotDB).filter(PlotDB.id == plot_id).first()
    if not plot:
        raise HTTPException(status_code=404, detail="Parcela no encontrada")
    return db.query(AlertDB).filter(AlertDB.plot_id == plot_id, AlertDB.is_resolved == resolved).order_by(AlertDB.timestamp.desc()).limit(limit).all()

@app.put("/alerts/{alert_id}/resolve", status_code=200)
def resolve_alert(alert_id: int, resolution: AlertResolve, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Marca una alerta como resuelta o no resuelta."""
    alert_to_update = db.query(AlertDB).filter(AlertDB.id == alert_id).first()
    if not alert_to_update:
        raise HTTPException(status_code=404, detail="Alerta no encontrada")
    alert_to_update.is_resolved = resolution.is_resolved
    db.commit()
    return {"message": f"Alerta {alert_id} actualizada a resuelta: {resolution.is_resolved}"}

@app.get("/")
def read_root():
    """Endpoint de bienvenida para verificar que la API está funcionando."""
    return {"message": "Agro Trace API está corriendo! Dirígete a /docs para ver la documentación."}

# --- Inicialización de la Base de Datos ---
# Se llama al final para asegurar que todos los modelos ORM estén definidos.
init_db()
