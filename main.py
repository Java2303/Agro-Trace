from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import random

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
    
    return {"message": f"Análisis completado para la Parcela {plot_id}", "status": status, "ph_level": ph, "nitrogen_level": nitrogen}

# -----------------------------------------------------
# ENDPOINT DE SALUD
# -----------------------------------------------------

@app.get("/")
def read_root():
    """Endpoint de bienvenida para verificar que la API está funcionando."""
    return {"message": "Agro Trace API está corriendo! Dirígete a /docs para ver la documentación."}