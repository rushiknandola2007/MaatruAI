# =============================================================================
# MaatriCare CDST — FastAPI Backend
# main.py
#
# Clinical basis:
#   - MoHFW MCH Guidelines 2022
#   - FOGSI Hypertension in Pregnancy Guidelines 2021
#   - WHO Safe Motherhood Protocol
#   - WHO Intrapartum Care for a Positive Childbirth Experience (2018)
#   - NHM MoHFW Obstetric Emergency Protocol
#   - LSAS Module 4
#
# Architecture:
#   - Deterministic threshold-based logic. Zero ML.
#   - "Worst-case input wins" aggregation (single REFERRAL param → REFERRAL)
#   - Compound-risk escalation (≥2 MONITOR params → REFERRAL)
#   - Fully explainable rationale generated per triggered finding
# =============================================================================

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator
from typing import Optional, List
from enum import Enum


# =============================================================================
# ENUMS
# =============================================================================

class RiskTier(str, Enum):
    """The three possible output tiers for the CDST assessment."""
    ROUTINE  = "ROUTINE"
    MONITOR  = "MONITOR"
    REFERRAL = "REFERRAL"


# =============================================================================
# INPUT MODELS (Pydantic)
# =============================================================================

class BloodPressure(BaseModel):
    """
    Systolic / Diastolic blood pressure in mmHg.

    Thresholds (Blueprint §1.1):
      ROUTINE  : SBP < 140  AND  DBP < 90
      MONITOR  : SBP 140–159  OR  DBP 90–109  (without severe features)
      REFERRAL : SBP ≥ 160   OR  DBP ≥ 110
    """
    systolic: float = Field(..., ge=40, le=300, description="Systolic BP in mmHg")
    diastolic: float = Field(..., ge=20, le=200, description="Diastolic BP in mmHg")


class SymptomHistory(BaseModel):
    """
    Boolean flags for all reportable symptoms.

    Minor symptoms → MONITOR if exactly 1 present and no major symptoms.
    Major symptoms → REFERRAL immediately (≥1 is sufficient).

    Minor (Blueprint §1.1):
      mild_headache, ankle_oedema, dysuria, persistent_vomiting, mild_fever
      (mild fever = temp < 38.5°C as reported by patient / ANM observation)

    Major (Blueprint §1.1):
      severe_headache, visual_changes, convulsions, vaginal_bleeding,
      absent_reduced_fetal_movements, severe_abdominal_pain,
      high_fever (≥ 38.5°C), difficulty_breathing
    """
    # ── Minor symptoms ──────────────────────────────────────────────────────
    mild_headache:         bool = Field(False, description="Mild headache")
    ankle_oedema:          bool = Field(False, description="Ankle oedema (non-pitting)")
    dysuria:               bool = Field(False, description="Painful urination")
    persistent_vomiting:   bool = Field(False, description="Persistent vomiting")
    mild_fever:            bool = Field(False, description="Mild fever (< 38.5°C)")

    # ── Major (red-flag) symptoms ────────────────────────────────────────────
    severe_headache:                bool = Field(False, description="Severe / thunderclap headache")
    visual_changes:                 bool = Field(False, description="Visual disturbances / blurring")
    convulsions:                    bool = Field(False, description="Seizures / convulsions")
    vaginal_bleeding:               bool = Field(False, description="Antepartum haemorrhage")
    absent_reduced_fetal_movements: bool = Field(False, description="Absent or reduced fetal movements")
    severe_abdominal_pain:          bool = Field(False, description="Severe abdominal / epigastric pain")
    high_fever:                     bool = Field(False, description="High fever ≥ 38.5°C")
    difficulty_breathing:           bool = Field(False, description="Dyspnoea / difficulty breathing")


class MUAC(BaseModel):
    """
    Mid-Upper Arm Circumference in centimetres.

    Thresholds (Blueprint §1.1 — WHO/NRC criteria):
      ROUTINE  : ≥ 23.0 cm  (normal nutritional status)
      MONITOR  : 18.5–22.9 cm  (Moderate Acute Malnutrition / MAM)
      REFERRAL : < 18.5 cm   (Severe Acute Malnutrition / SAM)
    """
    value: float = Field(..., ge=5.0, le=50.0, description="MUAC measurement in cm")


class FHS(BaseModel):
    """
    Foetal Heart Sounds — rate in beats per minute and absent flag.

    Thresholds (Blueprint §1.1 — WHO Intrapartum Care 2018):
      ROUTINE  : 120–160 bpm (regular, clearly audible)
      MONITOR  : 100–119 bpm OR 161–180 bpm (borderline — reassess in 15 min)
      REFERRAL : < 100 bpm OR > 180 bpm OR absent/inaudible
    """
    bpm:    Optional[float] = Field(None, ge=0, le=300, description="Foetal heart rate in bpm")
    absent: bool            = Field(False, description="True if FHS is absent / inaudible")

    @validator("bpm", always=True)
    def bpm_required_when_not_absent(cls, v, values):
        # If FHS is not absent, a BPM value must be provided.
        if not values.get("absent") and v is None:
            raise ValueError("bpm is required when absent=False")
        return v


# =============================================================================
# AGGREGATE INPUT MODEL
# =============================================================================

class AssessmentInput(BaseModel):
    """All four clinical parameters bundled for a single POST /api/assess call."""
    blood_pressure:   BloodPressure
    symptom_history:  SymptomHistory
    muac:             MUAC
    fhs:              FHS


# =============================================================================
# OUTPUT MODELS (Pydantic)
# =============================================================================

class FindingDetail(BaseModel):
    """A single numbered finding included in the rationale."""
    index:       int         # 1-based display number
    parameter:   str         # Human-readable parameter name
    value_text:  str         # Raw value as displayed to ANM (e.g. "164/106 mmHg")
    tier:        RiskTier    # Tier triggered by this finding
    explanation: str         # One-sentence clinical explanation
    guideline:   str         # Cited guideline abbreviation


class RiskResult(BaseModel):
    """
    The complete output model returned by POST /api/assess.
    Designed to map directly to the UI rationale screen described in §1.3.
    """
    overall_tier:       RiskTier           # The final aggregated risk classification
    alert_headline:     str                # Top-line alert text shown prominently in the UI
    summary_text:       str                # One-sentence summary (e.g. "Patient classified HIGH RISK based on N findings")
    findings:           List[FindingDetail] # Numbered list of triggered findings (only non-ROUTINE findings)
    actions:            List[str]          # Ordered action steps for the ANM
    clinical_basis:     str                # Guideline citations string
    compound_risk_note: Optional[str]      # Set if compound-risk rule (≥2 MONITOR) triggered escalation
    rule_engine_version: str               # SemVer for OTA rule versioning (Blueprint §4.1)


# =============================================================================
# CLINICAL LOGIC HELPERS
# =============================================================================

def _classify_bp(bp: BloodPressure) -> tuple[RiskTier, str, str]:
    """
    Classify blood pressure into a risk tier.

    Returns (tier, value_text, explanation).
    """
    sbp, dbp = bp.systolic, bp.diastolic

    # ── REFERRAL: Severe hypertension ─────────────────────────────────────
    # SBP ≥ 160 OR DBP ≥ 110 → pre-eclampsia / eclampsia protocol (MoHFW 2022)
    if sbp >= 160 or dbp >= 110:
        explanation = (
            f"{'Systolic' if sbp >= 160 else 'Diastolic'} meets criteria for severe hypertension "
            "(pre-eclampsia/eclampsia protocol — MoHFW MCH Guidelines 2022)."
        )
        return RiskTier.REFERRAL, f"{sbp:.0f}/{dbp:.0f} mmHg", explanation

    # ── MONITOR: Mild–moderate hypertension ───────────────────────────────
    # SBP 140–159 OR DBP 90–109 (without severe features)
    if sbp >= 140 or dbp >= 90:
        explanation = (
            "Mild-to-moderate hypertension detected. "
            "Reassess in 15–30 minutes; escalates to REFERRAL if severe symptoms co-exist (FOGSI 2021)."
        )
        return RiskTier.MONITOR, f"{sbp:.0f}/{dbp:.0f} mmHg", explanation

    # ── ROUTINE ───────────────────────────────────────────────────────────
    return RiskTier.ROUTINE, f"{sbp:.0f}/{dbp:.0f} mmHg", "Blood pressure within normal limits."


def _classify_symptoms(sx: SymptomHistory) -> tuple[RiskTier, str, str]:
    """
    Classify symptom history into a risk tier.

    Major symptom present → REFERRAL (red-flag; impending eclampsia or obstetric emergency).
    Exactly 1 minor symptom → MONITOR.
    No symptoms → ROUTINE.
    """
    # Map major symptom booleans to display labels
    major_map = {
        "severe_headache":                "Severe headache",
        "visual_changes":                 "Visual disturbance",
        "convulsions":                    "Convulsions / seizure",
        "vaginal_bleeding":               "Vaginal bleeding",
        "absent_reduced_fetal_movements": "Absent/reduced fetal movements",
        "severe_abdominal_pain":          "Severe abdominal pain",
        "high_fever":                     "High fever (≥38.5°C)",
        "difficulty_breathing":           "Difficulty breathing",
    }
    # Map minor symptom booleans to display labels
    minor_map = {
        "mild_headache":       "Mild headache",
        "ankle_oedema":        "Ankle oedema (non-pitting)",
        "dysuria":             "Dysuria",
        "persistent_vomiting": "Persistent vomiting",
        "mild_fever":          "Mild fever (<38.5°C)",
    }

    triggered_major = [label for attr, label in major_map.items() if getattr(sx, attr)]
    triggered_minor = [label for attr, label in minor_map.items() if getattr(sx, attr)]

    # ── REFERRAL: Any major (red-flag) symptom ────────────────────────────
    if triggered_major:
        joined = " + ".join(triggered_major)
        explanation = (
            f"Red-flag symptom(s) detected: {joined}. "
            "These indicate possible impending eclampsia or obstetric emergency. "
            "Immediate clinical intervention required (WHO Safe Motherhood Protocol)."
        )
        return RiskTier.REFERRAL, joined, explanation

    # ── MONITOR: At least 1 minor symptom ────────────────────────────────
    if triggered_minor:
        joined = " + ".join(triggered_minor)
        explanation = (
            f"Minor symptom(s) reported: {joined}. "
            "Monitor closely; document and schedule follow-up within 48 hours."
        )
        return RiskTier.MONITOR, joined, explanation

    # ── ROUTINE ──────────────────────────────────────────────────────────
    return RiskTier.ROUTINE, "No symptoms reported", "No reportable symptoms. Routine care continues."


def _classify_muac(muac: MUAC) -> tuple[RiskTier, str, str]:
    """
    Classify MUAC measurement into a risk tier.

    < 18.5 cm → SAM (REFERRAL) — high-risk for LBW, preterm, maternal mortality
    18.5–22.9 cm → MAM (MONITOR) — refer to NRC + iron/folate supplementation
    ≥ 23.0 cm → ROUTINE
    """
    v = muac.value

    # ── REFERRAL: Severe Acute Malnutrition ───────────────────────────────
    if v < 18.5:
        explanation = (
            f"MUAC {v:.1f} cm indicates Severe Acute Malnutrition (SAM). "
            "Significantly increases risk of LBW, preterm birth, and maternal mortality (NHM MoHFW)."
        )
        return RiskTier.REFERRAL, f"{v:.1f} cm (SAM)", explanation

    # ── MONITOR: Moderate Acute Malnutrition ──────────────────────────────
    if v < 23.0:
        explanation = (
            f"MUAC {v:.1f} cm indicates Moderate Acute Malnutrition (MAM). "
            "Refer to NRC; ensure iron/folate supplementation and dietary counselling."
        )
        return RiskTier.MONITOR, f"{v:.1f} cm (MAM)", explanation

    # ── ROUTINE ──────────────────────────────────────────────────────────
    return RiskTier.ROUTINE, f"{v:.1f} cm", "MUAC within normal nutritional range (≥23.0 cm)."


def _classify_fhs(fhs: FHS) -> tuple[RiskTier, str, str]:
    """
    Classify foetal heart sounds into a risk tier.

    Absent / inaudible → REFERRAL (foetal distress)
    < 100 bpm          → REFERRAL (severe bradycardia — hypoxia / cord compression)
    > 180 bpm          → REFERRAL (severe tachycardia)
    100–119 bpm        → MONITOR (mild bradycardia — reassess in 15 min)
    161–180 bpm        → MONITOR (mild tachycardia)
    120–160 bpm        → ROUTINE (normal range)
    """
    # ── REFERRAL: Absent / inaudible ─────────────────────────────────────
    if fhs.absent:
        explanation = (
            "FHS absent/inaudible. This constitutes a foetal emergency. "
            "Place mother in left lateral position immediately (WHO Intrapartum Care 2018)."
        )
        return RiskTier.REFERRAL, "Absent / Inaudible", explanation

    bpm = fhs.bpm  # Validator ensures bpm is not None when absent=False

    # ── REFERRAL: Severe bradycardia (< 100 bpm) ─────────────────────────
    if bpm < 100:
        deviation = 120 - bpm
        explanation = (
            f"FHR {bpm:.0f} bpm is {deviation:.0f} bpm below the safe lower limit (120 bpm). "
            "Foetal bradycardia at this level may indicate hypoxia, cord compression, or placental insufficiency. "
            "Do NOT wait for a second reading (NHM Obstetric Emergency Protocol)."
        )
        return RiskTier.REFERRAL, f"{bpm:.0f} bpm (severe bradycardia)", explanation

    # ── REFERRAL: Severe tachycardia (> 180 bpm) ─────────────────────────
    if bpm > 180:
        explanation = (
            f"FHR {bpm:.0f} bpm exceeds the upper danger threshold (180 bpm). "
            "Severe foetal tachycardia may indicate infection, hyperthyroidism, or cord compromise (WHO 2018)."
        )
        return RiskTier.REFERRAL, f"{bpm:.0f} bpm (severe tachycardia)", explanation

    # ── MONITOR: Borderline bradycardia (100–119 bpm) ────────────────────
    if bpm < 120:
        explanation = (
            f"FHR {bpm:.0f} bpm is borderline low (normal: 120–160 bpm). "
            "Reassess in 15 minutes with mother in left lateral position."
        )
        return RiskTier.MONITOR, f"{bpm:.0f} bpm (borderline bradycardia)", explanation

    # ── MONITOR: Borderline tachycardia (161–180 bpm) ────────────────────
    if bpm > 160:
        explanation = (
            f"FHR {bpm:.0f} bpm is borderline high (normal: 120–160 bpm). "
            "Reassess in 15 minutes; check for maternal pyrexia or dehydration."
        )
        return RiskTier.MONITOR, f"{bpm:.0f} bpm (borderline tachycardia)", explanation

    # ── ROUTINE: Normal range (120–160 bpm) ──────────────────────────────
    return RiskTier.ROUTINE, f"{bpm:.0f} bpm", "Foetal heart rate within normal range (120–160 bpm)."


# =============================================================================
# CORE RISK EVALUATION ENGINE
# =============================================================================

def evaluate_risk(inputs: AssessmentInput) -> RiskResult:
    """
    Translate the decision matrix (Blueprint §1.1 and §1.2) into a
    deterministic RiskResult.

    Aggregation logic (Blueprint §1.2 — "worst-case input wins"):
      Rule 1 — ANY parameter at REFERRAL tier → overall = REFERRAL
      Rule 2 — COUNT of MONITOR-tier parameters ≥ 2 → escalate to REFERRAL
               (compound-risk rule: e.g. MAM + mild hypertension)
      Rule 3 — Exactly 1 MONITOR, all others ROUTINE → overall = MONITOR
      Rule 4 — ALL ROUTINE → overall = ROUTINE
    """

    # ── Step 1: Classify each of the four parameters ─────────────────────
    bp_tier,   bp_value,   bp_explanation   = _classify_bp(inputs.blood_pressure)
    sx_tier,   sx_value,   sx_explanation   = _classify_symptoms(inputs.symptom_history)
    mu_tier,   mu_value,   mu_explanation   = _classify_muac(inputs.muac)
    fhs_tier,  fhs_value,  fhs_explanation  = _classify_fhs(inputs.fhs)

    parameter_results = [
        ("Blood Pressure",    bp_tier,  bp_value,  bp_explanation,  "MoHFW MCH Guidelines 2022 · FOGSI 2021"),
        ("Symptom History",   sx_tier,  sx_value,  sx_explanation,  "WHO Safe Motherhood Protocol"),
        ("MUAC",              mu_tier,  mu_value,  mu_explanation,  "NHM MoHFW Nutritional Protocol"),
        ("Foetal Heart Sounds", fhs_tier, fhs_value, fhs_explanation, "WHO Intrapartum Care 2018 · NHM Obstetric Emergency Protocol"),
    ]

    # ── Step 2: Collect non-ROUTINE findings for the rationale ────────────
    # Only findings that crossed a threshold are shown to the ANM.
    findings: List[FindingDetail] = []
    idx = 1
    for (param, tier, value, explanation, guideline) in parameter_results:
        if tier != RiskTier.ROUTINE:
            findings.append(FindingDetail(
                index=idx,
                parameter=param,
                value_text=value,
                tier=tier,
                explanation=explanation,
                guideline=guideline,
            ))
            idx += 1

    # ── Step 3: Apply aggregation rules ──────────────────────────────────
    tiers = [bp_tier, sx_tier, mu_tier, fhs_tier]
    referral_count = tiers.count(RiskTier.REFERRAL)
    monitor_count  = tiers.count(RiskTier.MONITOR)

    compound_risk_note: Optional[str] = None

    # Rule 1: Any single REFERRAL parameter drives the whole result.
    if referral_count >= 1:
        overall_tier = RiskTier.REFERRAL

    # Rule 2: Two or more MONITOR parameters compound to REFERRAL.
    elif monitor_count >= 2:
        overall_tier = RiskTier.REFERRAL
        compound_risk_note = (
            f"{monitor_count} concurrent moderate-risk findings detected "
            "(two MONITOR-tier parameters compound to critical risk per Blueprint §1.2). "
            "Do not monitor — transfer immediately."
        )

    # Rule 3: Exactly one MONITOR, rest ROUTINE → MONITOR.
    elif monitor_count == 1:
        overall_tier = RiskTier.MONITOR

    # Rule 4: All clear.
    else:
        overall_tier = RiskTier.ROUTINE

    # ── Step 4: Generate rationale text, headline, and action plan ────────
    return _build_result(overall_tier, findings, compound_risk_note, inputs)


# =============================================================================
# RATIONALE & ACTION PLAN BUILDER  (Blueprint §1.3)
# =============================================================================

def _build_result(
    overall_tier: RiskTier,
    findings: List[FindingDetail],
    compound_risk_note: Optional[str],
    inputs: AssessmentInput,
) -> RiskResult:
    """
    Construct the RiskResult with exact UI copy as described in Blueprint §1.3.
    Text is designed to be read aloud by an ANM in under 30 seconds.
    """
    n = len(findings)

    if overall_tier == RiskTier.REFERRAL:
        alert_headline = "⚠ IMMEDIATE REFERRAL RECOMMENDED"
        summary_text   = (
            f"This patient has been classified as HIGH RISK based on "
            f"{'compound moderate risk (no single REFERRAL finding, but ≥2 MONITOR findings)' if compound_risk_note else f'{n} finding(s)'}. "
            "Transfer to FRU / District Hospital within 60 minutes."
        )
        # Determine the dominant clinical concern to tailor action steps.
        fhs_referral = inputs.fhs.absent or (inputs.fhs.bpm is not None and (inputs.fhs.bpm < 100 or inputs.fhs.bpm > 180))
        has_eclampsia_risk = (
            inputs.blood_pressure.systolic >= 160 or inputs.blood_pressure.diastolic >= 110
            or inputs.symptom_history.severe_headache
            or inputs.symptom_history.visual_changes
            or inputs.symptom_history.convulsions
        )
        has_haemorrhage = inputs.symptom_history.vaginal_bleeding

        if fhs_referral and not has_eclampsia_risk:
            # Foetal distress — specific protocol (Case B in Blueprint §1.3)
            actions = [
                "Place mother in LEFT LATERAL POSITION immediately.",
                "Do NOT wait for a second FHS measurement.",
                "Call 108 / arrange emergency transport now.",
                "Inform referral facility: 'Foetal [bradycardia/tachycardia], transferring now.'",
                "Keep mother calm; avoid supine position during transport.",
            ]
            clinical_basis = (
                "WHO Intrapartum Care for a Positive Childbirth Experience (2018) · "
                "NHM MoHFW Obstetric Emergency Protocol"
            )
        elif has_eclampsia_risk:
            # Pre-eclampsia / eclampsia — LSAS protocol (Case A in Blueprint §1.3)
            actions = [
                "Administer MgSO₄ 4g IV loading dose per LSAS protocol NOW (if trained and equipped).",
                "Call 108 / referral transport immediately.",
                "Alert receiving FRU / District Hospital before departure.",
                "Do NOT leave patient alone at any time.",
                "Monitor airway; turn to left lateral position if convulsions occur.",
            ]
            clinical_basis = (
                "WHO Safe Motherhood Protocol · "
                "FOGSI Hypertension in Pregnancy Guidelines 2021 · "
                "LSAS Module 4 · MoHFW MCH Guidelines 2022"
            )
        elif has_haemorrhage:
            actions = [
                "Do NOT perform vaginal examination at sub-centre level.",
                "Call 108 immediately — antepartum haemorrhage is a Category-I emergency.",
                "Establish IV access and maintain fluids if trained.",
                "Alert referral facility: 'APH case, gestational age [X] weeks, transferring.'",
                "Keep patient supine with legs elevated; monitor pulse and consciousness.",
            ]
            clinical_basis = (
                "NHM MoHFW Obstetric Emergency Protocol · "
                "WHO Safe Motherhood Protocol"
            )
        else:
            # Generic REFERRAL actions (SAM compound risk, etc.)
            actions = [
                "Call 108 / arrange emergency transport immediately.",
                "Alert receiving FRU / District Hospital prior to departure.",
                "Do NOT leave patient alone.",
                "Document all findings and this referral decision in the patient register.",
                "Accompany patient or ensure a responsible adult is present during transfer.",
            ]
            clinical_basis = (
                "MoHFW MCH Guidelines 2022 · "
                "WHO Safe Motherhood Protocol · "
                "NHM MoHFW Obstetric Emergency Protocol"
            )

    elif overall_tier == RiskTier.MONITOR:
        alert_headline = "🟡 MONITOR — INCREASED VIGILANCE"
        summary_text   = (
            f"One moderate-risk finding detected. "
            "No immediate transfer required, but reassess in 15–30 minutes and schedule follow-up within 48 hours."
        )
        actions = [
            "Reassess all parameters in 15–30 minutes.",
            "Document current findings in the patient register.",
            "Schedule follow-up visit within 48 hours.",
            "Counsel patient on warning signs that require immediate return.",
            "If any parameter worsens or a new symptom appears, escalate to REFERRAL immediately.",
        ]
        clinical_basis = "MoHFW MCH Guidelines 2022 · FOGSI 2021 · NHM Standard ANC Protocol"

    else:  # ROUTINE
        alert_headline = "✅ ROUTINE CARE"
        summary_text   = (
            "All four parameters are within normal limits. "
            "Continue standard ANC protocol. Log to ABDM sync queue."
        )
        actions = [
            "Proceed with standard ANC visit protocol.",
            "Reinforce counselling on nutrition, rest, and danger signs.",
            "Schedule next routine ANC visit as per gestational age.",
            "Log assessment to ABDM sync queue for facility records.",
        ]
        clinical_basis = "MoHFW MCH Guidelines 2022 · Standard ANC Protocol"

    return RiskResult(
        overall_tier        = overall_tier,
        alert_headline      = alert_headline,
        summary_text        = summary_text,
        findings            = findings,
        actions             = actions,
        clinical_basis      = clinical_basis,
        compound_risk_note  = compound_risk_note,
        rule_engine_version = "1.0.0",   # SemVer — OTA-updatable per Blueprint §4.1
    )


# =============================================================================
# FASTAPI APP
# =============================================================================

app = FastAPI(
    title="MaatriCare CDST API",
    description=(
        "Clinical Decision Support Tool for maternal health risk stratification. "
        "Deterministic rule-based engine. Zero ML. "
        "Clinical basis: MoHFW MCH 2022 · FOGSI 2021 · WHO Safe Motherhood."
    ),
    version="1.0.0",
)

# ── CORS ──────────────────────────────────────────────────────────────────────
# Allow the Vite dev server (5173) and Create-React-App / alternative (3000).
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",   # Vite default
        "http://localhost:3000",   # CRA / alternative dev server
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =============================================================================
# ENDPOINTS
# =============================================================================

@app.get("/", tags=["Health"])
def root():
    """Health check — confirms the API is running."""
    return {
        "service": "MaatriCare CDST API",
        "status": "operational",
        "version": "1.0.0",
        "disclaimer": (
            "This recommendation supports — not replaces — clinical judgment. "
            "Any override must be logged with a mandatory free-text reason for supervisor review."
        ),
    }


@app.post(
    "/api/assess",
    response_model=RiskResult,
    summary="Run CDST assessment",
    description=(
        "Accepts four clinical inputs (Blood Pressure, Symptom History, MUAC, FHS), "
        "runs the deterministic rule engine, and returns a structured RiskResult "
        "with tier classification, explainable rationale, and actionable steps."
    ),
    tags=["Clinical"],
)
def assess(inputs: AssessmentInput) -> RiskResult:
    """
    POST /api/assess

    Example minimum request body:
    {
      "blood_pressure":  { "systolic": 164, "diastolic": 106 },
      "symptom_history": { "severe_headache": true, "visual_changes": true },
      "muac":            { "value": 18.2 },
      "fhs":             { "bpm": 142, "absent": false }
    }
    """
    return evaluate_risk(inputs)


# =============================================================================
# ENTRY POINT  (run with: uvicorn main:app --reload --port 8000)
# =============================================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
