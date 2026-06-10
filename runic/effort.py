"""Build the effort timeline: route segments + pace -> time slices with music
targets (target energy + target tempo) for each moment of the run."""

from __future__ import annotations

from .models import EffortSlot, RouteProfile, Terrain
from .pace import PaceModel, segment_time_s

# Terrain -> desired song energy (0..1). Tunable.
_TERRAIN_ENERGY: dict[Terrain, float] = {
    Terrain.STEEP_UP: 0.90,
    Terrain.UP: 0.80,
    Terrain.FLAT: 0.65,
    Terrain.DOWN: 0.50,
    Terrain.STEEP_DOWN: 0.55,
}

# Extra tempo (BPM) added to the base cadence on climbs, to lift the push.
_TERRAIN_TEMPO_BONUS: dict[Terrain, float] = {
    Terrain.STEEP_UP: 6.0,
    Terrain.UP: 3.0,
    Terrain.FLAT: 0.0,
    Terrain.DOWN: -2.0,
    Terrain.STEEP_DOWN: -1.0,
}


def build_effort_timeline(
    route: RouteProfile,
    model: PaceModel,
    *,
    finish_kick: float = 0.08,
    finish_fraction: float = 0.15,
) -> tuple[list[EffortSlot], float]:
    """Turn a route + pace model into time-ordered :class:`EffortSlot`s.

    Each route segment becomes one slot on the time axis with a target energy
    (from terrain) and a target tempo (cadence + terrain bonus). An optional
    "finish kick" ramps energy up over the final ``finish_fraction`` of the run.

    Returns ``(slots, total_time_s)``.
    """
    # First pass: per-segment times to know the total.
    times = [segment_time_s(seg, model) for seg in route.segments]
    total_time = sum(times)
    if total_time <= 0:
        raise ValueError("Run has zero predicted duration.")

    kick_starts_at = total_time * (1.0 - finish_fraction)

    slots: list[EffortSlot] = []
    clock = 0.0
    for seg, dt in zip(route.segments, times):
        start_s, end_s = clock, clock + dt
        energy = _TERRAIN_ENERGY[seg.terrain]
        tempo = model.cadence_spm + _TERRAIN_TEMPO_BONUS[seg.terrain]

        # Finish kick: scale energy up toward the end (capped at 1.0).
        mid = (start_s + end_s) / 2
        if mid >= kick_starts_at and finish_kick > 0:
            progress = (mid - kick_starts_at) / max(1e-6, total_time - kick_starts_at)
            energy = min(1.0, energy + finish_kick * progress)

        slots.append(
            EffortSlot(
                start_s=start_s,
                end_s=end_s,
                terrain=seg.terrain,
                target_energy=energy,
                target_tempo=tempo,
            )
        )
        clock = end_s

    return slots, total_time


def dominant_target(
    slots: list[EffortSlot], start_s: float, end_s: float
) -> tuple[Terrain, float, float]:
    """Aggregate the effort timeline over a [start, end) window.

    Used to ask "what does the terrain demand during the window this song will
    occupy?" Returns time-weighted ``(terrain, target_energy, target_tempo)``.
    """
    overlap_energy = 0.0
    overlap_tempo = 0.0
    total_overlap = 0.0
    terrain_time: dict[Terrain, float] = {}

    for slot in slots:
        lo = max(start_s, slot.start_s)
        hi = min(end_s, slot.end_s)
        if hi <= lo:
            continue
        w = hi - lo
        overlap_energy += slot.target_energy * w
        overlap_tempo += slot.target_tempo * w
        total_overlap += w
        terrain_time[slot.terrain] = terrain_time.get(slot.terrain, 0.0) + w

    if total_overlap <= 0:
        # Window past the end of the run: fall back to the last slot.
        last = slots[-1]
        return last.terrain, last.target_energy, last.target_tempo

    terrain = max(terrain_time.items(), key=lambda kv: kv[1])[0]
    return terrain, overlap_energy / total_overlap, overlap_tempo / total_overlap
