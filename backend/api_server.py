from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Literal, Dict, Any
import os
import json
import requests

# ---------------------------
# Config
# ---------------------------

DATAONE_BASE_URL = os.getenv("DATAONE_BASE_URL", "").strip()
DATAONE_API_KEY = os.getenv("DATAONE_API_KEY", "").strip()
DATAONE_CLIENT_ID = os.getenv("DATAONE_CLIENT_ID", "").strip()  # or account id, depending on their docs

INVENTORY_FILE = os.path.join(os.path.dirname(__file__), "fluid_inventory.json")

# ---------------------------
# Data models
# ---------------------------

FluidSystem = Literal[
    "transmission",
    "front_diff",
    "rear_diff",
    "transfer_case",
    "coolant",
    "power_steering",
    "engine_oil",
]

class FluidProduct(BaseModel):
    id: str
    name: str
    type: FluidSystem
    compatible_specs: List[str]
    not_for_specs: List[str] = []
    notes: Optional[str] = None

class FluidMatch(BaseModel):
    product: FluidProduct
    match_reason: str  # e.g. "explicit match: MERCON ULV in compatible_specs"

class FluidWarning(BaseModel):
    message: str
    products: List[str] = []

class FluidRequirement(BaseModel):
    system: FluidSystem
    required_spec: str
    oem_name: Optional[str] = None
    matches: List[FluidMatch]
    warnings: List[FluidWarning]

class VehicleInfo(BaseModel):
    vin: str
    year: Optional[int]
    make: Optional[str]
    model: Optional[str]
    trim: Optional[str] = None
    engine: Optional[str] = None

class FluidResponse(BaseModel):
    vehicle: VehicleInfo
    fluids: List[FluidRequirement]

# ---------------------------
# Load fluid inventory
# ---------------------------

def load_inventory() -> List[FluidProduct]:
    try:
        with open(INVENTORY_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return [FluidProduct(**item) for item in raw]
    except FileNotFoundError:
        raise RuntimeError(f"Missing fluid_inventory.json at {INVENTORY_FILE}")
    except Exception as e:
        raise RuntimeError(f"Error loading inventory: {e}")

FLUID_INVENTORY: List[FluidProduct] = load_inventory()

# ---------------------------
# DataOne VIN decode integration
# ---------------------------

def call_dataone(vin: str) -> Dict[str, Any]:
    """
    Call DataOne VIN decoder.

    NOTE:
      - You MUST fill in DATAONE_BASE_URL and the exact params
        according to their developer docs.
      - This function assumes a JSON response.

    Example (you will likely change this):

        params = {
            "client_id": DATAONE_CLIENT_ID,
            "access_key": DATAONE_API_KEY,
            "vin": vin,
            "format": "json",
            "include": "tech_specs,service_data"
        }
    """
    if not (DATAONE_BASE_URL and DATAONE_API_KEY and DATAONE_CLIENT_ID):
        raise RuntimeError("DataOne config missing. Set DATAONE_BASE_URL, DATAONE_API_KEY, DATAONE_CLIENT_ID.")

    params = {
        "client_id": DATAONE_CLIENT_ID,
        "access_key": DATAONE_API_KEY,
        "vin": vin,
        "format": "json",
        # TODO: adjust according to DataOne docs – include packs that contain fluids/tech specs
        "include": "tech_specs,service_data"
    }

    resp = requests.get(DATAONE_BASE_URL, params=params, timeout=8)
    if resp.status_code != 200:
        raise RuntimeError(f"DataOne error: HTTP {resp.status_code} - {resp.text}")

    return resp.json()

def extract_vehicle_and_fluids(data: Dict[str, Any]) -> (VehicleInfo, List[FluidRequirement]):
    """
    This function maps DataOne's JSON into:
      - VehicleInfo
      - a list of FluidRequirement (one per system)

    You will need to adapt the JSON paths once you see a real DataOne response.

    For now, this uses dummy keys and demonstrates the structure.
    """
    # ---- Vehicle basic info (adjust keys to match DataOne) ----
    # These keys are examples – inspect your DataOne response and update.
    vehicle_record = data.get("data", {}).get("vehicle", {})
    year = vehicle_record.get("year")
    make = vehicle_record.get("make")
    model = vehicle_record.get("model")
    trim = vehicle_record.get("trim")
    engine_desc = vehicle_record.get("engine", {}).get("description")

    vehicle = VehicleInfo(
        vin=vehicle_record.get("vin") or data.get("request", {}).get("vin", "UNKNOWN"),
        year=int(year) if year else None,
        make=make,
        model=model,
        trim=trim,
        engine=engine_desc,
    )

    # ---- Fluid specs (examples – adjust to real paths) ----
    # The idea: pull one string per system, e.g. "MERCON ULV"
    tech = data.get("data", {}).get("tech_specs", {})
    service = data.get("data", {}).get("service_data", {})

    # These are placeholders – you'll change them to real DataOne fields
    transmission_fluid = tech.get("transmission_fluid_spec") or service.get("transmission_fluid")
    coolant_fluid = tech.get("coolant_spec") or service.get("coolant")
    front_diff_fluid = tech.get("front_diff_fluid") or service.get("front_axle_fluid")
    rear_diff_fluid = tech.get("rear_diff_fluid") or service.get("rear_axle_fluid")
    tcase_fluid = tech.get("tcase_fluid") or service.get("transfer_case_fluid")
    ps_fluid = tech.get("ps_fluid") or service.get("power_steering_fluid")
    engine_oil = tech.get("engine_oil_spec") or service.get("engine_oil")

    fluid_requirements: List[FluidRequirement] = []

    def add_req(system: FluidSystem, spec: Optional[str], oem_name: Optional[str] = None):
        if not spec:
            return
        fluid_requirements.append(
            FluidRequirement(
                system=system,
                required_spec=spec.strip(),
                oem_name=oem_name,
                matches=[],
                warnings=[],
            )
        )

    add_req("transmission", transmission_fluid)
    add_req("coolant", coolant_fluid)
    add_req("front_diff", front_diff_fluid)
    add_req("rear_diff", rear_diff_fluid)
    add_req("transfer_case", tcase_fluid)
    add_req("power_steering", ps_fluid)
    add_req("engine_oil", engine_oil)

    return vehicle, fluid_requirements

# ---------------------------
# Matching logic
# ---------------------------

def normalize_spec(s: str) -> str:
    return s.strip().upper().replace(" ", "")

def enrich_with_matches(fluid_reqs: List[FluidRequirement]) -> List[FluidRequirement]:
    for req in fluid_reqs:
        required_norm = normalize_spec(req.required_spec)
        matches: List[FluidMatch] = []
        explicit_bad: List[str] = []

        for product in FLUID_INVENTORY:
            if product.type != req.system:
                continue

            # Normalize product specs
            compat_norm = [normalize_spec(x) for x in product.compatible_specs]
            not_for_norm = [normalize_spec(x) for x in product.not_for_specs]

            if required_norm in not_for_norm:
                explicit_bad.append(product.name)
                continue

            if required_norm in compat_norm:
                matches.append(
                    FluidMatch(
                        product=product,
                        match_reason=f"explicit match: {req.required_spec} in {product.name}.compatible_specs"
                    )
                )

        warnings: List[FluidWarning] = []

        if not matches:
            warnings.append(
                FluidWarning(
                    message=f"No compatible {req.system.replace('_', ' ')} fluid in inventory "
                            f"for spec '{req.required_spec}'.",
                    products=[]
                )
            )

        if explicit_bad:
            warnings.append(
                FluidWarning(
                    message=(f"DO NOT use these products for {req.system.replace('_', ' ')} "
                             f"with spec '{req.required_spec}'."),
                    products=explicit_bad
                )
            )

        req.matches = matches
        req.warnings = warnings

    return fluid_reqs

# ---------------------------
# FastAPI app
# ---------------------------

app = FastAPI(title="Shop Fluid Advisor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # you can restrict to your LAN later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/fluids/{vin}", response_model=FluidResponse)
def get_fluids_for_vin(vin: str):
    vin = vin.strip().upper()
    if len(vin) != 17:
        raise HTTPException(status_code=400, detail="VIN must be 17 characters.")

    try:
        raw = call_dataone(vin)
        vehicle, fluid_reqs = extract_vehicle_and_fluids(raw)
        fluid_reqs = enrich_with_matches(fluid_reqs)
        return FluidResponse(vehicle=vehicle, fluids=fluid_reqs)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")
