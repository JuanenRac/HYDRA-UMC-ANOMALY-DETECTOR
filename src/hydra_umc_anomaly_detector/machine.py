# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - src/hydra_umc_anomaly_detector/machine.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Characteristic frequencies of a machine: what a peak in the spectrum may mean.

From the geometry of a machine (rolling bearings, a belt, gears) and the shaft speed, the frequencies at which
each defect shows up are known. `diagnose` looks for a peak at those frequencies (and their harmonics) and says
"possible outer-race defect" instead of "bin 137 Hz". Bearing orders, with n balls of diameter d on a pitch
diameter D and a contact angle a, as multiples of the shaft speed:

    BPFO = n/2 * (1 - d/D cos a)           outer race
    BPFI = n/2 * (1 + d/D cos a)           inner race
    BSF  = D/(2d) * (1 - (d/D cos a)^2)    ball (twice per turn of the ball)
    FTF  = 1/2 * (1 - d/D cos a)           cage

A belt turns at pi * pulley diameter / belt length orders; a gear pair meshes at its teeth count in orders.
This is a hint for a person, never a verdict: other causes put peaks in the same places. Checked with synthetic
signals only, never on a real machine. The machines are described in the configuration file (`diagnosis`).
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from .fft import Spectrum

LANGUAGES = ("en", "es", "de", "fr", "it", "ja", "zh", "pt")
MAX_MACHINES = 64
MAX_BEARINGS = 16
MAX_GEARS = 16

# What each kind of line is called, per language (pt is European Portuguese). Keys are the codes the answer carries.
MESSAGES: dict[str, dict[str, str]] = {
    "bpfo": {
        "en": "possible outer-race defect of bearing {name}",
        "es": "posible defecto de la pista exterior del rodamiento {name}",
        "de": "möglicher Außenringschaden des Lagers {name}",
        "fr": "défaut possible de la bague extérieure du roulement {name}",
        "it": "possibile difetto della pista esterna del cuscinetto {name}",
        "ja": "ベアリング {name} の外輪の欠陥の可能性",
        "zh": "轴承 {name} 外圈可能存在缺陷",
        "pt": "possível defeito da pista exterior do rolamento {name}",
    },
    "bpfi": {
        "en": "possible inner-race defect of bearing {name}",
        "es": "posible defecto de la pista interior del rodamiento {name}",
        "de": "möglicher Innenringschaden des Lagers {name}",
        "fr": "défaut possible de la bague intérieure du roulement {name}",
        "it": "possibile difetto della pista interna del cuscinetto {name}",
        "ja": "ベアリング {name} の内輪の欠陥の可能性",
        "zh": "轴承 {name} 内圈可能存在缺陷",
        "pt": "possível defeito da pista interior do rolamento {name}",
    },
    "bsf": {
        "en": "possible rolling-element defect of bearing {name}",
        "es": "posible defecto de un elemento rodante del rodamiento {name}",
        "de": "möglicher Wälzkörperschaden des Lagers {name}",
        "fr": "défaut possible d'un élément roulant du roulement {name}",
        "it": "possibile difetto di un elemento volvente del cuscinetto {name}",
        "ja": "ベアリング {name} の転動体の欠陥の可能性",
        "zh": "轴承 {name} 滚动体可能存在缺陷",
        "pt": "possível defeito de um elemento rolante do rolamento {name}",
    },
    "ftf": {
        "en": "possible cage defect of bearing {name}",
        "es": "posible defecto de la jaula del rodamiento {name}",
        "de": "möglicher Käfigschaden des Lagers {name}",
        "fr": "défaut possible de la cage du roulement {name}",
        "it": "possibile difetto della gabbia del cuscinetto {name}",
        "ja": "ベアリング {name} の保持器の欠陥の可能性",
        "zh": "轴承 {name} 保持架可能存在缺陷",
        "pt": "possível defeito da gaiola do rolamento {name}",
    },
    "belt": {
        "en": "possible belt problem (worn, loose or damaged)",
        "es": "posible problema de la correa (desgastada, floja o dañada)",
        "de": "mögliches Riemenproblem (verschlissen, locker oder beschädigt)",
        "fr": "problème possible de courroie (usée, lâche ou abîmée)",
        "it": "possibile problema della cinghia (usurata, lenta o danneggiata)",
        "ja": "ベルトの問題の可能性（摩耗、緩み、損傷）",
        "zh": "皮带可能存在问题（磨损、松动或损坏）",
        "pt": "possível problema da correia (gasta, solta ou danificada)",
    },
    "gear_mesh": {
        "en": "possible gear problem (mesh frequency)",
        "es": "posible problema del engranaje (frecuencia de engrane)",
        "de": "mögliches Zahnradproblem (Zahneingriffsfrequenz)",
        "fr": "problème possible d'engrenage (fréquence d'engrènement)",
        "it": "possibile problema dell'ingranaggio (frequenza di ingranamento)",
        "ja": "ギアの問題の可能性（かみ合い周波数）",
        "zh": "齿轮可能存在问题（啮合频率）",
        "pt": "possível problema da engrenagem (frequência de engrenamento)",
    },
    "shaft": {
        "en": "energy at the shaft speed (possible imbalance or misalignment)",
        "es": "energía a la velocidad del eje (posible desequilibrio o desalineación)",
        "de": "Energie bei der Wellendrehzahl (mögliche Unwucht oder Fluchtungsfehler)",
        "fr": "énergie à la vitesse de l'arbre (déséquilibre ou désalignement possible)",
        "it": "energia alla velocità dell'albero (possibile squilibrio o disallineamento)",
        "ja": "シャフト回転数のエネルギー（アンバランスまたはミスアライメントの可能性）",
        "zh": "轴转速处存在能量（可能不平衡或不对中）",
        "pt": "energia à velocidade do veio (possível desequilíbrio ou desalinhamento)",
    },
}
NO_MATCH: dict[str, str] = {
    "en": "no peak at a characteristic frequency of this machine",
    "es": "ningún pico en una frecuencia característica de esta máquina",
    "de": "kein Peak bei einer charakteristischen Frequenz dieser Maschine",
    "fr": "aucun pic à une fréquence caractéristique de cette machine",
    "it": "nessun picco a una frequenza caratteristica di questa macchina",
    "ja": "この機械の特性周波数にピークはありません",
    "zh": "此机器的特征频率处没有峰值",
    "pt": "nenhum pico numa frequência característica desta máquina",
}


class MachineError(ValueError):
    """The machine description, the settings or the signal cannot be used."""


def message(code: str, lang: str, name: str = "") -> str:
    """The text of a finding in one of the eight languages."""
    if lang not in LANGUAGES:
        raise MachineError("lang must be one of " + ", ".join(LANGUAGES))
    return MESSAGES[code][lang].format(name=name)


@dataclass(frozen=True)
class BearingSpec:
    name: str
    balls: int
    ball_diameter: float
    pitch_diameter: float
    contact_angle_deg: float = 0.0

    def orders(self) -> dict[str, float]:
        ratio = self.ball_diameter / self.pitch_diameter * math.cos(math.radians(self.contact_angle_deg))
        return {
            "bpfo": self.balls / 2.0 * (1.0 - ratio),
            "bpfi": self.balls / 2.0 * (1.0 + ratio),
            "bsf": self.pitch_diameter / (2.0 * self.ball_diameter) * (1.0 - ratio * ratio),
            "ftf": 0.5 * (1.0 - ratio),
        }


@dataclass(frozen=True)
class Machine:
    name: str
    bearings: tuple[BearingSpec, ...] = ()
    belt_pulley_diameter: float = 0.0  # same unit as the belt length; 0 means no belt
    belt_length: float = 0.0
    gear_teeth: tuple[int, ...] = ()
    # gears that sit on a shaft turning at a multiple of the main one (1.0 = the same shaft)
    gear_shaft_ratio: float = 1.0


@dataclass(frozen=True)
class Line:
    code: str
    name: str
    order: float


def characteristic_lines(machine: Machine, include_shaft: bool = True) -> list[Line]:
    """Every characteristic line of the machine, in orders (multiples of the shaft speed)."""
    lines: list[Line] = []
    if include_shaft:
        lines.append(Line("shaft", "", 1.0))
    for bearing in machine.bearings:
        for code, order in bearing.orders().items():
            lines.append(Line(code, bearing.name, order))
    if machine.belt_pulley_diameter > 0 and machine.belt_length > 0:
        lines.append(Line("belt", "", math.pi * machine.belt_pulley_diameter / machine.belt_length))
    for teeth in machine.gear_teeth:
        lines.append(Line("gear_mesh", str(teeth), teeth * machine.gear_shaft_ratio))
    return lines


@dataclass(frozen=True)
class DiagnosisSettings:
    """Everything off until `enabled` is true; the values are the ones to tune against real recordings."""

    enabled: bool = False
    tolerance_pct: float = 2.0
    harmonics: int = 3
    peak_ratio: float = 5.0
    min_rpm: float = 30.0
    machines: dict[str, Machine] = field(default_factory=dict)


def _number(value: object, what: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise MachineError(f"{what} must be a number from {low:g} to {high:g}")
    return float(value)


def _bearing(raw: object) -> BearingSpec:
    if not isinstance(raw, dict):
        raise MachineError("a bearing must be an object")
    unknown = sorted(set(raw) - {"name", "balls", "ballDiameter", "pitchDiameter", "contactAngleDeg"})
    if unknown:
        raise MachineError("a bearing has unknown keys: " + ", ".join(unknown))
    name = raw.get("name")
    if not isinstance(name, str) or not name or len(name) > 64:
        raise MachineError("a bearing needs a name of up to 64 characters")
    balls = raw.get("balls")
    if isinstance(balls, bool) or not isinstance(balls, int) or not 3 <= balls <= 64:
        raise MachineError(f"bearing {name}: balls must be an integer from 3 to 64")
    ball_d = _number(raw.get("ballDiameter"), f"bearing {name}: ballDiameter", 1e-6, 1e6)
    pitch_d = _number(raw.get("pitchDiameter"), f"bearing {name}: pitchDiameter", 1e-6, 1e6)
    if ball_d >= pitch_d:
        raise MachineError(f"bearing {name}: ballDiameter must be smaller than pitchDiameter")
    angle = _number(raw.get("contactAngleDeg", 0.0), f"bearing {name}: contactAngleDeg", 0.0, 60.0)
    return BearingSpec(name, balls, ball_d, pitch_d, angle)


def _machine(name: str, raw: object) -> Machine:
    if not isinstance(raw, dict):
        raise MachineError(f"machine {name} must be an object")
    unknown = sorted(set(raw) - {"bearings", "belt", "gears"})
    if unknown:
        raise MachineError(f"machine {name} has unknown keys: " + ", ".join(unknown))
    bearings_raw = raw.get("bearings", [])
    if not isinstance(bearings_raw, list) or len(bearings_raw) > MAX_BEARINGS:
        raise MachineError(f"machine {name}: bearings must be a list of at most {MAX_BEARINGS}")
    bearings = tuple(_bearing(b) for b in bearings_raw)
    pulley = length = 0.0
    belt = raw.get("belt")
    if belt is not None:
        if not isinstance(belt, dict) or set(belt) - {"pulleyDiameter", "length"}:
            raise MachineError(f"machine {name}: belt takes pulleyDiameter and length")
        pulley = _number(belt.get("pulleyDiameter"), f"machine {name}: belt.pulleyDiameter", 1e-6, 1e6)
        length = _number(belt.get("length"), f"machine {name}: belt.length", 1e-6, 1e6)
    teeth: tuple[int, ...] = ()
    ratio = 1.0
    gears = raw.get("gears")
    if gears is not None:
        if not isinstance(gears, dict) or set(gears) - {"teeth", "shaftRatio"}:
            raise MachineError(f"machine {name}: gears takes teeth and shaftRatio")
        teeth_raw = gears.get("teeth")
        if not isinstance(teeth_raw, list) or not 1 <= len(teeth_raw) <= MAX_GEARS or any(isinstance(t, bool) or not isinstance(t, int) or not 2 <= t <= 1000 for t in teeth_raw):
            raise MachineError(f"machine {name}: gears.teeth must be a list of integers from 2 to 1000")
        teeth = tuple(teeth_raw)
        ratio = _number(gears.get("shaftRatio", 1.0), f"machine {name}: gears.shaftRatio", 1e-3, 1e3)
    if not bearings and not pulley and not teeth:
        raise MachineError(f"machine {name} has no bearings, belt or gears")
    return Machine(name, bearings, pulley, length, teeth, ratio)


def parse_diagnosis_settings(raw: object) -> DiagnosisSettings:
    if raw is None:
        return DiagnosisSettings()
    if not isinstance(raw, dict):
        raise MachineError("diagnosis must be an object")
    unknown = sorted(set(raw) - {"enabled", "tolerancePct", "harmonics", "peakRatio", "minRpm", "machines"})
    if unknown:
        raise MachineError("diagnosis has unknown keys: " + ", ".join(unknown))
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise MachineError("diagnosis.enabled must be true or false")
    harmonics = raw.get("harmonics", 3)
    if isinstance(harmonics, bool) or not isinstance(harmonics, int) or not 1 <= harmonics <= 10:
        raise MachineError("diagnosis.harmonics must be an integer from 1 to 10")
    machines_raw = raw.get("machines", {})
    if not isinstance(machines_raw, dict) or len(machines_raw) > MAX_MACHINES:
        raise MachineError(f"diagnosis.machines must be an object of at most {MAX_MACHINES} machines")
    machines = {str(name): _machine(str(name), spec) for name, spec in machines_raw.items()}
    if enabled and not machines:
        raise MachineError("diagnosis is enabled but diagnosis.machines is empty")
    return DiagnosisSettings(
        enabled=enabled,
        tolerance_pct=_number(raw.get("tolerancePct", 2.0), "diagnosis.tolerancePct", 0.1, 20.0),
        harmonics=harmonics,
        peak_ratio=_number(raw.get("peakRatio", 5.0), "diagnosis.peakRatio", 1.5, 1000.0),
        min_rpm=_number(raw.get("minRpm", 30.0), "diagnosis.minRpm", 1.0, 1e6),
        machines=machines,
    )


def diagnose(
    spectrum: Spectrum,
    rpm: float,
    machine: Machine,
    settings: DiagnosisSettings,
    lang: str = "en",
    order_axis: bool = False,
) -> dict[str, object]:
    """Finds the characteristic lines of `machine` in `spectrum`.

    With `order_axis` the frequencies of the spectrum are orders (see orders.py) and `rpm` only labels the answer;
    otherwise they are hertz and the lines are placed at order * rpm / 60. A line is reported when a peak at least
    `peak_ratio` times the median of the spectrum lies within `tolerance_pct` (and one bin) of it or of one of its
    harmonics; two or more harmonics give confidence "high", one gives "low"."""
    if lang not in LANGUAGES:
        raise MachineError("lang must be one of " + ", ".join(LANGUAGES))
    if isinstance(rpm, bool) or not isinstance(rpm, (int, float)) or not math.isfinite(rpm) or rpm < settings.min_rpm:
        raise MachineError(f"rpm must be a number of at least {settings.min_rpm:g}")
    freqs = spectrum.freqs
    mags = spectrum.magnitudes
    if len(freqs) < 4:
        raise MachineError("the spectrum is too short to diagnose")
    floor = float(np.median(mags))
    if floor <= 0.0:
        floor = float(np.mean(mags))
    if floor <= 0.0:
        raise MachineError("the spectrum has no energy")
    bin_width = float(freqs[1] - freqs[0])
    scale = 1.0 if order_axis else rpm / 60.0
    top = float(freqs[-1])
    findings: list[dict[str, object]] = []
    lines = characteristic_lines(machine)
    # every (line, harmonic) place in the spectrum: a peak belongs only to the place nearest to it, so the third harmonic of one
    # line does not also count as the second harmonic of another that happens to lie within the tolerance
    places = np.array([ln.order * scale * h for ln in lines for h in range(1, settings.harmonics + 1)])
    owners = [(i, h) for i in range(len(lines)) for h in range(1, settings.harmonics + 1)]
    for line_index, line in enumerate(lines):
        hits: list[dict[str, object]] = []
        for harmonic in range(1, settings.harmonics + 1):
            target = line.order * scale * harmonic
            if target > top:
                break
            half = max(target * settings.tolerance_pct / 100.0, bin_width)
            index = np.flatnonzero((freqs >= target - half) & (freqs <= target + half))
            if len(index) == 0:
                continue
            best = int(index[int(np.argmax(mags[index]))])
            ratio = float(mags[best]) / floor
            if owners[int(np.argmin(np.abs(places - freqs[best])))] != (line_index, harmonic):
                continue
            if ratio >= settings.peak_ratio:
                hits.append({"harmonic": harmonic, "expected": round(target, 6), "peak": float(freqs[best]), "magnitude": float(mags[best]), "ratio": round(ratio, 3)})
        if hits:
            findings.append({
                "code": line.code,
                "name": line.name,
                "order": round(line.order, 6),
                "expectedHz": round(line.order * rpm / 60.0, 6),
                "confidence": "high" if len(hits) >= 2 else "low",
                "strength": max(float(h["ratio"]) for h in hits),  # type: ignore[arg-type]
                "hits": hits,
                "message": message(line.code, lang, line.name),
            })
    findings.sort(key=lambda f: (f["confidence"] != "high", -float(f["strength"])))  # type: ignore[arg-type]
    return {
        "machine": machine.name,
        "rpm": float(rpm),
        "axis": "orders" if order_axis else "hz",
        "findings": findings,
        "summary": findings[0]["message"] if findings else NO_MATCH[lang],
    }


def lines_summary(machine: Machine, rpm: float) -> list[dict[str, object]]:
    """The expected characteristic frequencies (Hz) of a machine at a speed, for a screen or a test."""
    return [{"code": ln.code, "name": ln.name, "order": round(ln.order, 6), "hz": round(ln.order * rpm / 60.0, 6)} for ln in characteristic_lines(machine)]


def machine_names(settings: DiagnosisSettings) -> Sequence[str]:
    return sorted(settings.machines)
