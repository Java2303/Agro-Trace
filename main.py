from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional, Generator
import random
import os
from datetime import datetime, timezone
import json
import io
from fastapi.responses import StreamingResponse
from dotenv import load_dotenv

from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Text, Boolean, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
from sqlalchemy.dialects.postgresql import JSONB

# Cargar variables de entorno desde un archivo .env (para desarrollo local)
load_dotenv()

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

# --- Configuración de SQLAlchemy ---
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL no está configurada en el archivo .env")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

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
    
    soil_analyses = relationship("SoilAnalysisDB", back_populates="plot", cascade="all, delete-orphan")
    alerts = relationship("AlertDB", back_populates="plot", cascade="all, delete-orphan")

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

# --- Creación de Tablas ---
def init_db():
    Base.metadata.create_all(bind=engine)

init_db()

# --- Gestión de Sesión de DB ---
def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

app = FastAPI(title="Agro Trace API")

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

@app.get("/plots/", response_model=List[Plot])
def list_plots(db: Session = Depends(get_db)):
    """Retorna la lista completa de parcelas registradas en Agro Trace."""
    plots = db.query(PlotDB).order_by(PlotDB.id.desc()).all()
    return plots

@app.post("/plots/", response_model=Plot, status_code=201)
def create_plot(plot_data: PlotBase, db: Session = Depends(get_db)):
    """Crea una nueva parcela con el polígono georreferenciado."""
    coordinates_json = [c.dict() for c in plot_data.coordinates]
    new_plot_db = PlotDB(**plot_data.dict(exclude={'coordinates'}), coordinates=coordinates_json)
    db.add(new_plot_db)
    db.commit()
    db.refresh(new_plot_db)

    log_audit(db, action='CREAR_PARCELA', target_type='parcela', target_id=str(new_plot_db.id), details={
        'name': new_plot_db.name,
        'crop_type': new_plot_db.crop_type,
        'area_hectares': new_plot_db.area_hectares
    })
    
    return new_plot_db

@app.post("/plots/{plot_id}/analyze", response_model=SoilAnalysisData)
def analyze_plot(plot_id: int, db: Session = Depends(get_db)):
    """Simula un análisis geofísico y actualiza el estado de la parcela."""
    plot_to_analyze = db.query(PlotDB).filter(PlotDB.id == plot_id).first()
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

    log_audit(db, action='ANALIZAR_SUELO', target_type='parcela', target_id=str(plot_id), details={
        'analysis_id': new_soil_analysis_db.id, 'ph': ph, 'nitrogen': nitrogen,
        'status_result': analysis_result_status, 'applied_thresholds': cfg
    })

    # check_for_alerts(db, new_soil_analysis_db, plot_to_analyze.crop_type)
    
    return new_soil_analysis_db

@app.delete("/plots/{plot_id}", status_code=200)
def delete_plot(plot_id: int, db: Session = Depends(get_db)):
    """Elimina una parcela por su ID."""
    plot_to_delete = db.query(PlotDB).filter(PlotDB.id == plot_id).first()
    if not plot_to_delete:
        raise HTTPException(status_code=404, detail="Parcela no encontrada")

    log_audit(db, action='ELIMINAR_PARCELA', target_type='parcela', target_id=str(plot_id), details={
        'name': plot_to_delete.name, 'crop_type': plot_to_delete.crop_type
    })

    db.delete(plot_to_delete)
    db.commit()
    return {"message": f"Parcela {plot_id} eliminada correctamente."}

@app.get('/history')
def get_history(plot_id: Optional[int] = None, limit: int = 200, db: Session = Depends(get_db)):
    """Retorna las entradas de auditoría. Opcionalmente filtra por `plot_id`."""
    query = db.query(AuditDB).order_by(AuditDB.id.desc())
    if plot_id is not None:
        query = query.filter(AuditDB.target_id == str(plot_id))
    return query.limit(limit).all()

@app.get('/plots/{plot_id}/history')
def get_plot_history(plot_id: int, limit: int = 200, db: Session = Depends(get_db)):
    """Retorna el historial de auditoría para una parcela específica."""
    return db.query(AuditDB).filter(AuditDB.target_id == str(plot_id)).order_by(AuditDB.id.desc()).limit(limit).all()

@app.get('/plots/{plot_id}/certificate')
def get_plot_certificate(plot_id: int, db: Session = Depends(get_db)):
    """Return PDF certificate for a plot."""
    plot = db.query(PlotDB).filter(PlotDB.id == plot_id).first()
    if not plot:
        raise HTTPException(status_code=404, detail="Parcela no encontrada")

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
    except ImportError:
        raise HTTPException(status_code=503, detail="Dependencia 'reportlab' no instalada.")

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    c.setFont('Helvetica-Bold', 18)
    c.drawString(48, height - 72, 'Certificado de Parcela - Agro Trace')

    c.setFont('Helvetica', 11)
    c.drawString(48, height - 100, f'ID Parcela: {plot.id}')
    c.drawString(48, height - 118, f'Nombre: {plot.name}')
    c.drawString(48, height - 136, f'Cultivo: {plot.crop_type}')
    c.drawString(48, height - 154, f'Área (Ha): {plot.area_hectares:.2f}')
    c.drawString(48, height - 172, f'Estado: {plot.status}')

    latest_analysis = db.query(SoilAnalysisDB).filter(SoilAnalysisDB.plot_id == plot_id).order_by(SoilAnalysisDB.id.desc()).first()

    c.setFont('Helvetica-Bold', 11)
    c.drawString(48, height - 200, 'Último Análisis de Suelo:')
    c.setFont('Helvetica', 11)
    if latest_analysis:
        c.drawString(48, height - 218, f'Fecha: {latest_analysis.timestamp.strftime("%Y-%m-%d %H:%M")}')
        c.drawString(48, height - 236, f'pH: {latest_analysis.ph}')
        c.drawString(48, height - 254, f'Nitrógeno: {latest_analysis.nitrogen} ppm')
        y_coords_start = height - 272
    else:
        c.drawString(48, height - 218, 'No hay análisis de suelo registrados.')
        y_coords_start = height - 236

    c.drawString(48, y_coords_start, 'Coordenadas (lat, lng):')
    y = y_coords_start - 18
    coordinates = plot.coordinates if isinstance(plot.coordinates, list) else []
    for coord in coordinates:
        line = f'- {coord.get("x", 0):.6f}, {coord.get("y", 0):.6f}'
        c.drawString(64, y, line)
        y -= 14
        if y < 60:
            c.showPage()
            y = height - 72

    c.setFont('Helvetica-Oblique', 9)
    c.drawString(48, 48, f'Generado: {datetime.now(timezone.utc).isoformat()} UTC')
    c.save()

    buffer.seek(0)
    filename = f'certificado_parcela_{plot.id}.pdf'
    return StreamingResponse(buffer, media_type='application/pdf', headers={'Content-Disposition': f'attachment; filename="{filename}"'})

@app.get("/")
def read_root():
    """Endpoint de bienvenida para verificar que la API está funcionando."""
    return {"message": "Agro Trace API está corriendo! Dirígete a /docs para ver la documentación."}
