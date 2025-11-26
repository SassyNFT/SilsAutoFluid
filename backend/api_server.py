from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Literal
import os
import json
import requests

# ---------------------------
# Data Models
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
    notes: str | None = None

class FluidMatch(BaseModel):
    product: FluidProduct
    match_reason: str

class FluidWarning(BaseModel):
    message: str
    products: List[str] = []

class FluidRequirement(BaseModel):
    system: FluidSystem
    required_spec: str
    oem_name: str | None = None
    matches: List[FluidMatch]
    warnings: List[FluidWarning]

class VehicleInfo(BaseModel):
    vin: str
    year: int | None
    make: str | None
    model: str | None
    trim: str | None = None
    engine: str | None = None

class FluidResponse(BaseModel):
    vehicle: VehicleInfo
    fluids: List[FluidRequirement]


# ---------------------------
# Load Fluid Inventory
# ---------------------------

INVENTORY_FILE = os.path.join(os.path.dirname(__file__), "fluid_inventory.json")

def load_inventory() -> List[FluidProduct]:
    try:
        with open(INVENTORY_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return [FluidProduct(**item) for item in raw]
    except Exception as e:
        raise RuntimeError(f"Error loading fluid inventory: {e}")

FLUID_INVENTORY = load_inventory()


# ---------------------------
# FREE NHTSA VIN Decoder
# ---------------------------

def call_nhtsa(vin: str):
    url = f"https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValuesExtended/{vin}?format=json"
    resp = requests.get(url, timeout=8)

    if resp.status_code != 200:
        raise RuntimeError(f"NHTSA request failed: {resp.status_code}")

    data = resp.json()
    if "Results" not in data or not data["Results"]:
        raise RuntimeError("No VIN info returned from NHTSA")

    return data["Results"][0]


# ---------------------------
# TEMPORARY FLUID GENERATION (test mode)
# ---------------------------

def extract_vehicle_and_fluids(nhtsa):
    # ---- Vehicle Info ----
    vehicle = VehicleInfo(
        vin=nhtsa.get("VIN", "UNKNOWN"),
        year=int(nhtsa.get("ModelYear") or 0) or None,
        make=nhtsa.get("Make"),
        model=nhtsa.get("Model"),
        trim=nhtsa.get("Trim"),
        engine=nhtsa.get("EngineModel") or nhtsa.get("EngineCylinders")
    )

    # ---- TEMPORARY FLUID SPECS ----
    make = (vehicle.make or "").upper()

    if make == "FORD":
        required = {
            "transmission": "MERCON ULV",
            "coolant": "MOTORCRAFT YELLOW",
            "front_diff": "75W-85",
            "rear_diff": "75W-140",
            "transfer_case": "MERCON LV",
            "power_steering": "CHF 11S",
            "engine_oil": "5W-30"
        }
    elif make == "HONDA":
        required = {
            "transmission": "HONDA ATF DW-1",
            "coolant": "HONDA TYPE 2",
            "front_diff": "75W-90",
            "rear_diff": "75W-90",
            "transfer_case": "HONDA DPSF",
            "power_steering": "HONDA PSF",
            "engine_oil": "0W-20"
        }
    else:
        required = {
            "transmission": "UNKNOWN ATF",
            "coolant": "UNKNOWN COOLANT",
            "front_diff": "75W-90",
            "rear_diff": "75W-140",
            "transfer_case": "DEXRON III",
            "power_steering": "PSF GENERIC",
            "engine_oil": "5W-30"
        }

    # convert to FluidRequirement list
    fluid_reqs = [
        FluidRequirement(
            system=system,
            required_spec=spec,
            oem_name=None,
            matches=[],
            warnings=[]
        )
        for system, spec in required.items()
    ]

    return vehicle, fluid_reqs


# ---------------------------
# Match Fluids in Inventory
# ---------------------------

def normalize(s: str) -> str:
    return s.strip().upper().replace(" ", "")

def enrich_with_matches(fluid_reqs):
    for req in fluid_reqs:
        needed = normalize(req.required_spec)

        matches = []
        bad = []

        for product in FLUID_INVENTORY:
            if product.type != req.system:
                continue

            compat = [normalize(x) for x in product.compatible_specs]
            forbid = [normalize(x) for x in product.not_for_specs]

            if needed in forbid:
                bad.append(product.name)
                continue

            if needed in compat:
                matches.append(
                    FluidMatch(
                        product=product,
                        match_reason=f"Matched spec: {req.required_spec}"
                    )
                )

        req.matches = matches

        warnings = []
        if not matches:
            warnings.append(
                FluidWarning(
                    message=f"No compatible fluid found for {req.system.replace('_',' ')} spec '{req.required_spec}'."
                )
            )
        if bad:
            warnings.append(
                FluidWarning(
                    message=f"DO NOT USE for {req.system.replace('_',' ')}:",
                    products=bad
                )
            )
        req.warnings = warnings

    return fluid_reqs


# ---------------------------
# FastAPI Server
# ---------------------------

app = FastAPI(title="Sils Auto Fluid API (Test Mode)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/fluids/{vin}", response_model=FluidResponse)
def get_fluids(vin: str):
    vin = vin.strip().upper()
    if len(vin) != 17:
        raise HTTPException(status_code=400, detail="VIN must be 17 characters.")

    try:
        nhtsa = call_nhtsa(vin)
        vehicle, fluid_reqs = extract_vehicle_and_fluids(nhtsa)
        enriched = enrich_with_matches(fluid_reqs)
        return FluidResponse(vehicle=vehicle, fluids=enriched)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
