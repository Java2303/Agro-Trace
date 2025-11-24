from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import random
import sqlite3
import os
from datetime import datetime, timezone
import json
import io
from fastapi.responses import StreamingResponse

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

# IN-MEMORY DB
in_memory_db: List[Plot] = []
next_plot_id = 1

app = FastAPI(title="Agro Trace API")

# CORS (DEV)
origins = ["*"]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# -----------------------------------------------------
# Endpoints de la API
# -----------------------------------------------------

# AUDIT (SQLite)
DB_PATH = os.path.join(os.path.dirname(__file__), 'audit.db')

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, actor TEXT, action TEXT NOT NULL, target_type TEXT, target_id TEXT, details TEXT)''')
    conn.commit()
    conn.close()

def log_audit(action: str, target_type: Optional[str] = None, target_id: Optional[str] = None, details: Optional[dict] = None, actor: Optional[str] = None):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute('INSERT INTO audit (timestamp, actor, action, target_type, target_id, details) VALUES (?, ?, ?, ?, ?, ?)', (datetime.now(timezone.utc).isoformat(), actor or 'system', action, target_type, str(target_id) if target_id is not None else None, json.dumps(details or {})))
        conn.commit()
    except Exception as e:
        print('Audit log failed:', e)
    finally:
        try: conn.close()
        except: pass

def query_audit(plot_id: Optional[str] = None, limit: int = 200):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    if plot_id is not None:
        cur.execute('SELECT id, timestamp, actor, action, target_type, target_id, details FROM audit WHERE target_id = ? ORDER BY id DESC LIMIT ?', (str(plot_id), limit))
    else:
        cur.execute('SELECT id, timestamp, actor, action, target_type, target_id, details FROM audit ORDER BY id DESC LIMIT ?', (limit,))
    rows = cur.fetchall()
    conn.close()
    result = []
    for r in rows:
        try: details = json.loads(r[6]) if r[6] else {}
        except: details = {}
        result.append({'id': r[0], 'timestamp': r[1], 'actor': r[2], 'action': r[3], 'target_type': r[4], 'target_id': r[5], 'details': details})
    return result

init_db()


@app.get("/plots/", response_model=List[Plot])
def list_plots():
    """Retorna la lista completa de parcelas registradas en Agro Trace."""
    return in_memory_db

@app.post("/plots/", response_model=Plot, status_code=201)
def create_plot(plot_data: PlotBase):
    """Crea una nueva parcela con el polígono georreferenciado."""
    global next_plot_id
    
    # Crear la nueva parcela con el ID y el estado inicial
    new_plot = Plot(
        id=next_plot_id,
        name=plot_data.name,
        crop_type=plot_data.crop_type,
        area_hectares=plot_data.area_hectares,
        coordinates=plot_data.coordinates,
        status="PENDIENTE"
    )
    
    in_memory_db.append(new_plot)
    next_plot_id += 1

    # Registrar auditoría: creación de parcela
    try:
        log_audit(action='CREAR_PARCELA', target_type='parcela', target_id=str(new_plot.id), details={
            'name': new_plot.name,
            'crop_type': new_plot.crop_type,
            'area_hectares': new_plot.area_hectares
        })
    except Exception:
        pass
    
    return new_plot

@app.post("/plots/{plot_id}/analyze")
def analyze_plot(plot_id: int):
    """Simula un análisis geofísico y actualiza el estado de la parcela."""
    
    # Buscar la parcela por ID
    plot_to_analyze = next((p for p in in_memory_db if p.id == plot_id), None)
    
    if plot_to_analyze is None:
        raise HTTPException(status_code=404, detail="Parcela no encontrada")

    # --- SIMULACIÓN DEL ANÁLISIS ---
    # Genera resultados de análisis aleatorios y determina el estado

    # Generar niveles de suelo (simulados)
    ph = round(random.uniform(5.5, 7.5), 2)
    nitrogen = round(random.uniform(50.0, 300.0), 2)

    # Umbrales por cultivo (valores propuestos para Santa Cruz, Bolivia — base orientativa)
    # Las claves se comparan en minúsculas; si no hay coincidencia se usa 'default'.
    thresholds = {
        'default': {'pH_min': 5.8, 'pH_max': 7.2, 'n_min': 150},
        'maiz':    {'pH_min': 5.8, 'pH_max': 7.0, 'n_min': 140},
        'maíz':    {'pH_min': 5.8, 'pH_max': 7.0, 'n_min': 140},
        'soja':    {'pH_min': 5.5, 'pH_max': 7.2, 'n_min': 120},
        'soya':    {'pH_min': 5.5, 'pH_max': 7.2, 'n_min': 120},
        'arroz':   {'pH_min': 5.0, 'pH_max': 6.8, 'n_min': 100}
    }

    crop_key = (plot_to_analyze.crop_type or '').strip().lower()
    cfg = thresholds.get(crop_key, thresholds['default'])

    # Decisión de certificación basada en los umbrales:
    if cfg['pH_min'] <= ph <= cfg['pH_max'] and nitrogen >= cfg['n_min']:
        status = 'CERTIFICADO'
    else:
        # Si el pH está claramente fuera del rango, marcar como OBSERVADO
        if ph < cfg['pH_min'] or ph > cfg['pH_max']:
            status = 'OBSERVADO'
        else:
            status = 'PENDIENTE'

    # Actualizar la parcela
    plot_to_analyze.status = status
    plot_to_analyze.ph_level = ph
    plot_to_analyze.nitrogen_level = nitrogen

    # Registrar auditoría: análisis ejecutado (acción en español)
    try:
        log_audit(action='ANALIZAR', target_type='parcela', target_id=str(plot_id), details={
            'ph': ph,
            'nitrogen': nitrogen,
            'status': status,
            'applied_thresholds': cfg
        })
    except Exception:
        pass
    
    return {"message": f"Análisis completado para la Parcela {plot_id}", "status": status, "ph_level": ph, "nitrogen_level": nitrogen}


@app.delete("/plots/{plot_id}")
def delete_plot(plot_id: int):
    """Elimina una parcela por su ID."""
    # Buscar la parcela por ID
    plot_to_delete = next((p for p in in_memory_db if p.id == plot_id), None)
    if plot_to_delete is None:
        raise HTTPException(status_code=404, detail="Parcela no encontrada")

    in_memory_db.remove(plot_to_delete)

    # Registrar auditoría: eliminación de parcela
    try:
        log_audit(action='ELIMINAR_PARCELA', target_type='parcela', target_id=str(plot_id), details={
            'name': plot_to_delete.name,
            'crop_type': plot_to_delete.crop_type
        })
    except Exception:
        pass
    return {"message": f"Parcela {plot_id} eliminada correctamente."}


@app.get('/history')
def get_history(plot_id: Optional[int] = None, limit: int = 200):
    """Retorna las entradas de auditoría. Opcionalmente filtra por `plot_id`."""
    try:
        entries = query_audit(str(plot_id) if plot_id is not None else None, limit=limit)
        return entries
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get('/plots/{plot_id}/history')
def get_plot_history(plot_id: int, limit: int = 200):
    """Retorna el historial de auditoría para una parcela específica."""
    try:
        entries = query_audit(str(plot_id), limit=limit)
        return entries
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# PDF Certificate
@app.get('/plots/{plot_id}/certificate')
def get_plot_certificate(plot_id: int):
    """Return PDF certificate for a plot."""
    plot = next((p for p in in_memory_db if p.id == plot_id), None)
    if plot is None:
        raise HTTPException(status_code=404, detail="Parcela no encontrada")

    # Import reportlab lazily so app doesn't crash at startup if it's not installed
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
    except ImportError:
        raise HTTPException(status_code=503, detail="Dependencia 'reportlab' no instalada. Ejecuta 'pip install reportlab' y vuelve a desplegar.")

    # Crear PDF en memoria
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    # Header
    c.setFont('Helvetica-Bold', 18)
    c.drawString(48, height - 72, 'Certificado de Parcela - Agro Trace')

    c.setFont('Helvetica', 11)
    c.drawString(48, height - 100, f'ID Parcela: {plot.id}')
    c.drawString(48, height - 118, f'Nombre: {plot.name}')
    c.drawString(48, height - 136, f'Cultivo: {plot.crop_type}')
    c.drawString(48, height - 154, f'Área (Ha): {plot.area_hectares:.2f}')
    c.drawString(48, height - 172, f'Estado: {plot.status}')

    if plot.ph_level is not None:
        c.drawString(48, height - 190, f'pH: {plot.ph_level}')
    if plot.nitrogen_level is not None:
        c.drawString(48, height - 208, f'Nivel N: {plot.nitrogen_level}')

    # Lista de coordenadas
    c.drawString(48, height - 236, 'Coordenadas (lat, lng):')
    y = height - 254
    for coord in plot.coordinates:
        line = f'- {coord.x:.6f}, {coord.y:.6f}'
        c.drawString(64, y, line)
        y -= 14
        if y < 60:
            c.showPage()
            y = height - 60

    # Footer
    c.setFont('Helvetica-Oblique', 9)
    c.drawString(48, 48, f'Generado: {datetime.now(timezone.utc).isoformat()} UTC')
    c.save()

    buffer.seek(0)
    filename = f'certificado_parcela_{plot.id}.pdf'
    return StreamingResponse(buffer, media_type='application/pdf', headers={'Content-Disposition': f'attachment; filename="{filename}"'})

# -----------------------------------------------------
# ENDPOINT DE SALUD
# -----------------------------------------------------

@app.get("/")
def read_root():
    """Endpoint de bienvenida para verificar que la API está funcionando."""
    return {"message": "Agro Trace API está corriendo! Dirígete a /docs para ver la documentación."}
