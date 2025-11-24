from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import random
import sqlite3
import os
from datetime import datetime
import json

# Definición de Modelos Pydantic para la estructura de datos

class Coordinate(BaseModel):
    """Representa un punto de Latitud (x) y Longitud (y)."""
    # En el contexto geográfico, 'x' se usa a menudo para Latitud y 'y' para Longitud
    x: float  # Latitud
    y: float  # Longitud

class PlotBase(BaseModel):
    """Modelo base para crear una nueva parcela."""
    name: str
    crop_type: str
    area_hectares: float
    coordinates: List[Coordinate]

class Plot(PlotBase):
    """Modelo completo de la parcela, incluyendo su estado y resultados de análisis."""
    id: int
    status: str = "PENDIENTE"
    ph_level: Optional[float] = None
    nitrogen_level: Optional[float] = None

# -----------------------------------------------------
# Base de Datos Simulada (En memoria)
# -----------------------------------------------------

# Inicialización de la lista de parcelas y contador de IDs
in_memory_db: List[Plot] = []
next_plot_id = 1

# Inicialización de la aplicación FastAPI con el nuevo nombre
app = FastAPI(title="Agro Trace API")

# Configuración de CORS para permitir la comunicación con el frontend
# Esto es CRÍTICO para que el frontend pueda llamar a esta API
origins = [
    "*", # Permite cualquier origen (necesario en desarrollo o entornos de prueba)
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------------------------------
# Endpoints de la API
# -----------------------------------------------------

# --------------------------
# Auditoría (SQLite simple)
# --------------------------
DB_PATH = os.path.join(os.path.dirname(__file__), 'audit.db')

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            actor TEXT,
            action TEXT NOT NULL,
            target_type TEXT,
            target_id TEXT,
            details TEXT
        )
    ''')
    conn.commit()
    conn.close()

def log_audit(action: str, target_type: Optional[str] = None, target_id: Optional[str] = None, details: Optional[dict] = None, actor: Optional[str] = None):
    """Guarda una entrada de auditoría en la base SQLite."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            'INSERT INTO audit (timestamp, actor, action, target_type, target_id, details) VALUES (?, ?, ?, ?, ?, ?)',
            (datetime.utcnow().isoformat(), actor or 'system', action, target_type, str(target_id) if target_id is not None else None, json.dumps(details or {}))
        )
        conn.commit()
    except Exception as e:
        print('Audit log failed:', e)
    finally:
        try:
            conn.close()
        except:
            pass

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
        try:
            details = json.loads(r[6]) if r[6] else {}
        except Exception:
            details = {}
        result.append({
            'id': r[0],
            'timestamp': r[1],
            'actor': r[2],
            'action': r[3],
            'target_type': r[4],
            'target_id': r[5],
            'details': details
        })
    return result

# Inicializar DB de auditoría
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
        log_audit(action='CREATE_PLOT', target_type='plot', target_id=str(new_plot.id), details={
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

    # Generar niveles de suelo
    ph = round(random.uniform(5.5, 7.5), 2)
    nitrogen = round(random.uniform(50.0, 300.0), 2)

    # Lógica de certificación simulada
    # El umbral para ser 'CERTIFICADO' es pH casi neutro y alto nitrógeno
    if 6.0 <= ph <= 7.0 and nitrogen >= 150:
        status = "CERTIFICADO"
    elif ph < 5.8:
        status = "OBSERVADO" # El suelo es demasiado ácido
    else:
        status = "PENDIENTE" 

    # Actualizar la parcela
    plot_to_analyze.status = status
    plot_to_analyze.ph_level = ph
    plot_to_analyze.nitrogen_level = nitrogen

    # Registrar auditoría: análisis ejecutado
    try:
        log_audit(action='ANALYZE', target_type='plot', target_id=str(plot_id), details={
            'ph': ph,
            'nitrogen': nitrogen,
            'status': status
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
        log_audit(action='DELETE_PLOT', target_type='plot', target_id=str(plot_id), details={
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

# -----------------------------------------------------
# ENDPOINT DE SALUD
# -----------------------------------------------------

@app.get("/")
def read_root():
    """Endpoint de bienvenida para verificar que la API está funcionando."""
    return {"message": "Agro Trace API está corriendo! Dirígete a /docs para ver la documentación."}
