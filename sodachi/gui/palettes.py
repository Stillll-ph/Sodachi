"""The palette registry, and the solver that turns six hexes into ten roles.

A palette arrives as six colours with no role attached. The skin needs ten, and
the difference is not decoration: `paper` is a field the size of a panel and
`accent` is a few pixels of active state, so a colour that is wrong for one may
be the only right answer for the other. Sorting the six by lightness and taking
the top three as surfaces fails on about half of the sets in `NAMED_PALETTES` --
it hands Baroque a saturated yellow panel, and it hands Plum & Slate's red to a
button face because the palette has only two light values.

So roles are assigned by measurement. Every threshold below is a contrast ratio
or a luminance computed from WCAG relative luminance, and where a palette cannot
meet one with its six values alone the solver blends two of them, thins one with
alpha, or -- in the cases listed in `_OVERRIDES` -- takes a hand-written answer.
Nothing is ranked by its position in the list.

The solver does not pick a surface set and then live with it. It enumerates
every cohesive set of surfaces at both polarities, solves each one all the way
through, and ranks the finished results, because whether a choice was right is
only visible once the other nine roles have been fitted around it.

Two of the ten roles are deliberately pushed rather than merely chosen. The
surfaces are lifted toward the light end before anything is measured against
them -- criterion 6 -- because a saturated colour on a mid-toned panel fights
it and the same colour on a pale one reads as deliberate; on a dark palette
there is no light end to move toward, so the same judgement becomes a gap
between the panel and the backdrop it floats on. And `fill` is required to
carry hue -- criterion 7 -- because taking whichever quiet value was left over
leaves button faces and slider knobs colourless in most of the sets, which is
the one place a palette's own colour is looked at every time a control is used.

Nothing here imports Qt. `theme.py` converts a `PaletteSpec` to QColor; this
module holds only the hex strings and the measurements over them. Run
`python -m sodachi.gui.palettes` for the measured table.

Three places where a stated criterion had to be restated to be testable:

* Surface cohesion. "Within about 0.3 luminance" is a sound test on a light
  palette and vacuous on a dark one -- every value in Dark Matter sits below
  0.014, so any three of them pass. Cohesion is therefore also tested as a
  contrast ratio between the surfaces, `SURFACE_COHESION_MAX`, which is the same
  judgement made scale-free. Both numbers are reported.
* The fill/paper separation of 0.03 luminance has the same problem, so it is
  applied as written on light palettes and as a contrast ratio on dark ones.
* Criteria 6 and 7 are targets, not floors, and `check` does not test them. A
  palette that cannot lift its panel to 0.86 without dropping the ink under
  7:1, or cannot find 0.06 of chroma anywhere in its six, has not failed --
  there is nothing to take instead. Both are reported as columns in the table
  and as notes on the spec, which is where a shortfall is meant to be visible.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

RGB = tuple[int, int, int]

# --- criteria -----------------------------------------------------------------

INK_TARGET = 7.0  # 1. body type on the panel
INK_FLOOR = 4.5  # below this the solver has to say so out loud
INK_SPLIT_MIN = 1.20  # ink and ink_strong only separate if the eye can tell
# Type is quieter than anything else in the set. Orchid's pink clears 7:1 on its
# own panel and is still not a colour to set body text in, so a value has to be
# this close to neutral before it can take the ink, whatever its contrast.
INK_CHROMA_MAX = 0.20
SURFACE_CHROMA_MAX = 0.35  # 2. a surface is low-chroma, not merely light
SURFACE_SPREAD_MAX = 0.30  # linear luminance, as stated
SURFACE_COHESION_MAX = 2.0  # the same judgement as a contrast ratio
FILL_GAP_MIN = 0.03  # linear luminance, light palettes
FILL_CONTRAST_MIN = 1.06  # the same test on a dark palette
ACCENT_MIN = 3.0  # 3. an accent has to be legible against the panel
ACCENT_INK_LUM = 1.50  # and has to be distinguishable from the type
ACCENT_INK_CHROMA = 0.15
ACCENT_INK_HUE = 30.0
# 4. Held at the stated band rather than widened to admit Spice Market, the one
# palette whose only quiet mid-tone falls just under it. That palette is handled
# in `_OVERRIDES`, where the shortfall is visible, instead of by a threshold
# quietly tuned until nothing fails. Tech Organic is the near miss on the other
# side: its grey clears the floor at 1.64:1 and needs no help.
RULE_MIN = 1.60
RULE_MAX = 3.50
RULE_TARGET = 2.2  # where a synthesised rule aims
INK_SOFT_MIN = 3.0  # 5. secondary type
INK_SOFT_MAX = 4.5
INK_SOFT_TARGET = 3.6  # the tuning point the shipped palette was built on

# 6. Air. Where the panel sits, in luminance rather than in contrast, because
# the question is how light the surface is and not how far it is from something
# else. The light numbers are where the three surfaces stop competing with the
# type and the accent for attention. The dark one is the same judgement with
# nowhere to go: a near-black palette has no light end, so the panel earns its
# air by clearing the backdrop instead. Both are aims -- `check` does not test
# them, and a set that cannot reach them keeps its floors and says so.
PAPER_LUM_MIN = 0.86
SURFACE_LUM_MIN = 0.90
SURFACE_STEP = 1.05  # and the backdrop still has to be tellable from the panel
DARK_PANEL_LIFT = 0.02

# 7. Colour in the button faces. 0.06 is where the naive chroma below starts to
# be visible across a field the size of a button rather than reading as a warm
# grey. The ceiling is the other half of the same criterion: a fill is a
# surface, so it may sit this far under the panel and no further, or the
# control stops being a raised face and becomes a hole with an accent in it.
FILL_CHROMA_MIN = 0.06
FILL_CONTRAST_MAX = 1.45
FILL_CONTRAST_MAX_DARK = 1.70
# Below about a tenth, a blend has not taken any of the colour it was blended
# with: the fill is the paper under a different name. Where a tint is needed at
# all it starts here.
FILL_TINT_MIN = 0.10

DEFAULT_PALETTE = "Riddle"

NAMED_PALETTES: dict[str, tuple[str, ...]] = {
    "Riddle": ("#E3DBE7", "#A296AF", "#77835F", "#F4E6E2", "#2B3252", "#F5F3EC"),
    "Plum & Slate": ("#BEAEDB", "#7A68A2", "#5F6E8C", "#F6E3D9", "#2B3252", "#9E3140"),
    "Harvest": ("#333A56", "#1C2033", "#7E4536", "#F8E6D8", "#F4F1E8", "#D9A93F"),
    "Fjords": ("#2A3554", "#181F35", "#5288B3", "#EFD1B6", "#34589B", "#EDE7DA"),
    "Cherry Blossom Mocha": ("#553D39", "#33231F", "#8A5147", "#FBEAE0", "#F3B7C4", "#FAF3E6"),
    "Highlands": ("#1D2029", "#0F1116", "#4A6A5B", "#F8E5D9", "#FAFAF6", "#8E949E"),
    "Baroque": ("#EFC24C", "#B8862A", "#FFD84D", "#F2E1CD", "#17151B", "#6E1F2A"),
    "Cafe": ("#C39A63", "#8E6839", "#A0662E", "#F0D4B8", "#E9A53E", "#F2EFE7"),
    "Orchid": ("#2F2A38", "#1A1720", "#8D5563", "#F7E3D6", "#F09BB1", "#6FA35C"),
    "Dark Matter": ("#101014", "#080809", "#15151C", "#F6F1F1", "#FFFFFF", "#1D1D26"),
    "Arctic Shoreline": ("#2C3547", "#181F2C", "#4E8CA6", "#F5EAE3", "#6EB2C0", "#FBF6F0"),
    "Banquet": ("#F0C33F", "#A87C1F", "#E8A93B", "#EBD8BE", "#100D13", "#7C0F1C"),
    "Tech Organic": ("#F5F3EF", "#CBC5BA", "#8DD14F", "#F8E5D9", "#FAFAF6", "#17181C"),
    "Spice Market": ("#F8F6F1", "#CFCCC4", "#E0B23F", "#FBEAE0", "#FDFBF6", "#B8332B"),
    "Maritime Beacon": ("#BAD4DE", "#7C9FB0", "#44768D", "#F3DFCF", "#3E6E8E", "#D14545"),
}

ROLES: tuple[str, ...] = (
    "paper",
    "surface",
    "fill",
    "rule",
    "ink_soft",
    "ink",
    "ink_strong",
    "white",
    "shadow",
    "accent",
)


@dataclass(frozen=True, slots=True)
class PaletteSpec:
    """Ten roles as hex strings, plus the two facts a widget cannot measure.

    A value is ``#RRGGBB`` or, where the role needs thinning, ``#RRGGBBAA`` in
    CSS order. Qt's own string form is ``#AARRGGBB``, so consumers should go
    through `parse_hex` rather than handing these to QColor as strings.

    ``notes`` records every role the six palette colours could not fill on their
    own, and what was done instead. It is documentation, not state; nothing
    reads it but `report`.
    """

    name: str
    is_dark: bool
    paper: str
    surface: str
    fill: str
    rule: str
    ink_soft: str
    ink: str
    ink_strong: str
    white: str
    shadow: str
    accent: str
    notes: tuple[str, ...] = ()

    def role(self, name: str) -> str:
        if name not in ROLES:
            raise KeyError(f"no such palette role: {name!r}")
        return getattr(self, name)

    def as_dict(self) -> dict[str, str]:
        return {r: getattr(self, r) for r in ROLES}


# --- colour arithmetic --------------------------------------------------------


def parse_hex(value: str) -> tuple[int, int, int, int]:
    """``#RGB``/``#RRGGBB``/``#RRGGBBAA`` to (r, g, b, a). Alpha defaults to 255."""
    text = value.strip()
    if not text.startswith("#"):
        raise ValueError(f"hex colour must start with '#': {value!r}")
    body = text[1:]
    if len(body) == 3:
        body = "".join(ch * 2 for ch in body)
    if len(body) not in (6, 8):
        raise ValueError(f"hex colour must have 3, 6 or 8 digits: {value!r}")
    try:
        n = int(body, 16)
    except ValueError as exc:
        raise ValueError(f"not a hex colour: {value!r}") from exc
    if len(body) == 6:
        return (n >> 16) & 255, (n >> 8) & 255, n & 255, 255
    return (n >> 24) & 255, (n >> 16) & 255, (n >> 8) & 255, n & 255


def _rgb(value: str) -> RGB:
    r, g, b, _ = parse_hex(value)
    return r, g, b


def _hex(rgb: RGB, alpha: int = 255) -> str:
    r, g, b = (max(0, min(255, int(round(c)))) for c in rgb)
    if alpha >= 255:
        return f"#{r:02X}{g:02X}{b:02X}"
    return f"#{r:02X}{g:02X}{b:02X}{max(0, min(255, int(alpha))):02X}"


def _linear(channel: float) -> float:
    c = channel / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(value: str | RGB) -> float:
    """WCAG relative luminance, 0.0 to 1.0. Alpha is ignored; composite first."""
    r, g, b = _rgb(value) if isinstance(value, str) else value
    return 0.2126 * _linear(r) + 0.7152 * _linear(g) + 0.0722 * _linear(b)


def contrast(a: str | RGB, b: str | RGB) -> float:
    """WCAG contrast ratio, 1.0 to 21.0."""
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def chroma(value: str | RGB) -> float:
    """max-min over the sRGB channels, 0.0 to 1.0.

    Deliberately naive rather than perceptual. It answers the only question the
    surface test asks -- how far from grey is this -- and it can be checked
    against the hex by hand when a result looks wrong, which a trip through
    CIELAB cannot.
    """
    r, g, b = _rgb(value) if isinstance(value, str) else value
    return (max(r, g, b) - min(r, g, b)) / 255.0


def hue(value: str | RGB) -> float | None:
    """Hue in degrees, or None for a colour too neutral for the angle to mean anything."""
    r, g, b = _rgb(value) if isinstance(value, str) else value
    hi, lo = max(r, g, b), min(r, g, b)
    span = hi - lo
    if span < 13:  # about 0.05 chroma; below this the angle is rounding noise
        return None
    if hi == r:
        h = 60.0 * (((g - b) / span) % 6.0)
    elif hi == g:
        h = 60.0 * (((b - r) / span) + 2.0)
    else:
        h = 60.0 * (((r - g) / span) + 4.0)
    return h % 360.0


def hue_gap(a: float, b: float) -> float:
    """Angular distance in degrees, 0 to 180."""
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def mix(a: str, b: str, t: float) -> str:
    """``a`` blended ``t`` of the way to ``b``, in sRGB."""
    ar, ag, ab = _rgb(a)
    br, bg, bb = _rgb(b)
    t = max(0.0, min(1.0, t))
    return _hex((ar + (br - ar) * t, ag + (bg - ag) * t, ab + (bb - ab) * t))


def composite(fg: str, bg: str) -> str:
    """``fg``, possibly carrying alpha, painted over opaque ``bg``."""
    r, g, b, a = parse_hex(fg)
    if a >= 255:
        return _hex((r, g, b))
    br, bg_, bb = _rgb(bg)
    k = a / 255.0
    return _hex((r * k + br * (1 - k), g * k + bg_ * (1 - k), b * k + bb * (1 - k)))


BLACK = "#000000"
WHITE = "#FFFFFF"


def _seek(base: str, toward: str, background: str, target: float) -> str:
    """``base`` pushed toward ``toward`` until it reaches ``target`` on ``background``.

    ``toward`` is always black or white, so contrast is monotone in the blend
    fraction and a bisection is exact to within one step in 256.
    """
    if contrast(base, background) >= target:
        return _hex(_rgb(base))
    if contrast(toward, background) < target:
        return _hex(_rgb(toward))  # unreachable; the caller reports the shortfall
    lo, hi = 0.0, 1.0
    for _ in range(24):
        mid = (lo + hi) / 2.0
        if contrast(mix(base, toward, mid), background) >= target:
            hi = mid
        else:
            lo = mid
    return mix(base, toward, hi)


def _seek_between(base: str, toward: str, background: str, target: float) -> str:
    """As `_seek`, for a target the blend passes through rather than exceeds."""
    best, best_err = _hex(_rgb(base)), abs(contrast(base, background) - target)
    for i in range(1, 129):
        candidate = mix(base, toward, i / 128.0)
        err = abs(contrast(candidate, background) - target)
        if err < best_err:
            best, best_err = candidate, err
    return best


def _lift(base: str, target: float) -> str:
    """``base`` tinted toward white until its luminance reaches ``target``.

    The bisection is on the blend fraction, which luminance is monotone in, so
    the answer is the least white that will do -- the point of a lift is to move
    a colour, not to replace it.
    """
    if luminance(base) >= target:
        return _hex(_rgb(base))
    lo, hi = 0.0, 1.0
    for _ in range(24):
        mid = (lo + hi) / 2.0
        if luminance(mix(base, WHITE, mid)) >= target:
            hi = mid
        else:
            lo = mid
    return mix(base, WHITE, hi)


def _above(base: str, background: str, target: float) -> str:
    """The first point on ``base``-to-white that clears ``target`` above ``background``.

    Used where the background is itself a point on that line, which is the one
    case `_seek` cannot answer: it would see that the base already contrasts with
    the background and hand back a colour on the wrong side of it. Bisecting on
    "lighter than the background and far enough from it" is monotone in the blend
    fraction and gives a single mix off one palette value, which is what keeps a
    lifted panel's backdrop a tint of that value rather than a tint of a tint.
    """
    lum = luminance(background)
    if contrast(WHITE, background) < target:
        return WHITE  # unreachable; the panel is already all but white
    lo, hi = 0.0, 1.0
    for _ in range(24):
        mid = (lo + hi) / 2.0
        step = mix(base, WHITE, mid)
        if luminance(step) >= lum and contrast(step, background) >= target:
            hi = mid
        else:
            lo = mid
    return mix(base, WHITE, hi)


def _thin(fg: str, background: str, target: float) -> str:
    """``fg`` at the alpha whose composite lands nearest ``target`` on ``background``."""
    best, best_err = 255, 99.0
    for alpha in range(255, 15, -1):
        err = abs(contrast(composite(_hex(_rgb(fg), alpha), background), background) - target)
        if err < best_err:
            best, best_err = alpha, err
    return _hex(_rgb(fg), best)


# --- the solver ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Colour:
    hex: str
    lum: float
    chroma: float
    hue: float | None


def _prepare(hexes: Sequence[str]) -> list[_Colour]:
    out: list[_Colour] = []
    for h in hexes:
        canonical = _hex(_rgb(h))
        out.append(_Colour(canonical, luminance(canonical), chroma(canonical), hue(canonical)))
    return out


def _accentness(c: _Colour, others: Iterable[_Colour]) -> float:
    """How much a colour reads as meaning something rather than as material.

    Chroma alone picks the wrong colour on the warm palettes: Cherry Blossom
    Mocha's brick is more saturated than its pink but shares a hue with two
    other members, so it reads as part of the set. Weighting by the *nearest*
    other hue rather than the mean is what "least like the rest" has to mean --
    a colour with a twin is never the accent, however saturated it is.
    """
    if c.hue is None:
        return 0.0
    gaps = [hue_gap(c.hue, o.hue) for o in others if o.hue is not None and o.hex != c.hex]
    nearest = min(gaps) if gaps else 180.0
    return c.chroma * (0.10 + 0.90 * (nearest / 180.0))


def _distinct_from_ink(candidate: _Colour, ink: str) -> bool:
    """An accent must be tellable from the type in lightness, saturation or hue.

    Only one of the three has to hold. Baroque's yellow sits at 1.07:1 against
    its cream ink and is still obviously not the ink; Highlands' cream sits at
    1.13:1 against its off-white ink and obviously is.
    """
    if contrast(candidate.hex, ink) >= ACCENT_INK_LUM:
        return True
    if abs(candidate.chroma - chroma(ink)) >= ACCENT_INK_CHROMA:
        return True
    ink_hue = hue(ink)
    if candidate.hue is not None and ink_hue is not None:
        return hue_gap(candidate.hue, ink_hue) >= ACCENT_INK_HUE
    return False


def _cohesive_sets(anchors: list[_Colour]) -> list[tuple[_Colour, ...]]:
    """Every subset of one to three anchors whose members read as one material."""

    def together(a: _Colour, b: _Colour) -> bool:
        return (
            contrast(a.hex, b.hex) <= SURFACE_COHESION_MAX
            and abs(a.lum - b.lum) <= SURFACE_SPREAD_MAX
        )

    sets: list[tuple[_Colour, ...]] = []
    n = len(anchors)
    for i in range(n):
        sets.append((anchors[i],))
        for j in range(i + 1, n):
            if not together(anchors[i], anchors[j]):
                continue
            sets.append((anchors[i], anchors[j]))
            for k in range(j + 1, n):
                if together(anchors[i], anchors[k]) and together(anchors[j], anchors[k]):
                    sets.append((anchors[i], anchors[j], anchors[k]))
    return sets


def _paper_of(members: tuple[_Colour, ...], dark: bool) -> _Colour:
    """The panel: the middle value of three, or the one furthest from the ink of two."""
    ordered = sorted(members, key=lambda c: c.lum)
    if len(ordered) >= 3:
        return ordered[len(ordered) // 2]
    if len(ordered) == 2:
        return ordered[0] if dark else ordered[-1]
    return ordered[0]


def _choose_ink(cols: list[_Colour], paper: _Colour) -> tuple[str, bool, float]:
    """The type colour. Returns (hex, derived, best ratio the palette offered).

    A saturated value is only taken when nothing quieter reaches the target,
    because body type in cherry red is a different decision from a red accent.
    """
    ranked = sorted(
        (c for c in cols if c.hex != paper.hex),
        key=lambda c: -contrast(c.hex, paper.hex),
    )
    best_offered = contrast(ranked[0].hex, paper.hex) if ranked else 1.0
    for c in ranked:
        if c.chroma <= INK_CHROMA_MAX and contrast(c.hex, paper.hex) >= INK_TARGET:
            return c.hex, False, best_offered
    # Nothing quiet enough reaches the target. Deepen (or lighten) the value that
    # is furthest from the paper without being a colour in its own right, which
    # keeps the hue of the set: Cafe's ink is its own brown taken down.
    tolerable = [c for c in ranked if c.chroma <= SURFACE_CHROMA_MAX]
    base = (tolerable or ranked)[0]
    toward = WHITE if paper.lum < 0.35 else BLACK
    return _seek(base.hex, toward, paper.hex, INK_TARGET), True, best_offered


def _airy_surfaces(
    cols: list[_Colour], members: tuple[_Colour, ...], paper: _Colour, dark: bool, pinned: bool
) -> tuple[str, str, str | None]:
    """The panel and the backdrop it sits on. Returns (paper, surface, surface base).

    ``surface`` moves away from the ink, which is where the shipped palette puts
    its window backdrop and its recessed readouts. On a light palette both are
    then lifted toward white until they clear criterion 6; on a dark one there is
    nothing to lift toward, so only the panel moves, and it moves until it is
    clear of the backdrop by `DARK_PANEL_LIFT`. The lift is bounded by the ink:
    a dark panel that rises far enough to put the palette's lightest value under
    7:1 has traded readable type for air, which is not the trade being made here.

    ``pinned`` is set when a hand-written override supplied the paper. An
    override is an answer, not a starting point, so it is left where it is.
    """
    others = [c for c in members if c.hex != paper.hex]

    if dark:
        below = [c for c in others if c.lum < paper.lum]
        if below:
            surface = max(below, key=lambda c: contrast(c.hex, paper.hex))
            surface_hex, surface_base = surface.hex, surface.hex
        else:
            surface_hex, surface_base = _seek(paper.hex, BLACK, paper.hex, SURFACE_STEP), None
        if pinned:
            return paper.hex, surface_hex, surface_base
        lightest = max(c.lum for c in cols)
        ceiling = (lightest + 0.05) / INK_TARGET - 0.05
        want = min(luminance(surface_hex) + DARK_PANEL_LIFT, max(ceiling, paper.lum))
        return _lift(paper.hex, want), surface_hex, surface_base

    paper_hex = paper.hex if pinned else _lift(paper.hex, PAPER_LUM_MIN)
    step = (luminance(paper_hex) + 0.05) * SURFACE_STEP - 0.05
    above = [c for c in others if c.lum > luminance(paper_hex)]
    if above:
        candidate = max(above, key=lambda c: c.lum)
        lifted = _lift(candidate.hex, max(SURFACE_LUM_MIN, step))
        if contrast(lifted, paper_hex) >= SURFACE_STEP - 0.005:
            return paper_hex, lifted, candidate.hex
    return paper_hex, _above(paper.hex, paper_hex, SURFACE_STEP), None


def _fill_ok(candidate: str, paper: str, surface: str, dark: bool) -> bool:
    """Whether a value can be a button face on this panel at all.

    Everything criterion 2 asks of a surface, plus the separation from the panel
    that stops a button vanishing into it, plus the ceiling from criterion 7.
    """
    lum_paper, lum_fill, lum_surface = luminance(paper), luminance(candidate), luminance(surface)
    if dark:
        if lum_fill <= lum_paper or contrast(candidate, paper) < FILL_CONTRAST_MIN:
            return False
        if contrast(candidate, paper) > FILL_CONTRAST_MAX_DARK:
            return False
    else:
        if lum_fill >= lum_paper or lum_paper - lum_fill < FILL_GAP_MIN:
            return False
        if contrast(candidate, paper) > FILL_CONTRAST_MAX:
            return False
    if chroma(candidate) > SURFACE_CHROMA_MAX:
        return False
    if contrast(candidate, surface) > SURFACE_COHESION_MAX:
        return False
    return max(abs(lum_fill - lum_surface), abs(lum_fill - lum_paper)) <= SURFACE_SPREAD_MAX


def _choose_fill(
    cols: list[_Colour],
    paper: str,
    paper_base: str,
    surface: str,
    dark: bool,
    spent: set[str],
    quiet: set[str],
) -> tuple[str, str, bool]:
    """The button face. Returns (hex, the palette colour it carries, derived).

    A member that already reads as a chromatic surface on this panel is the
    answer where the set has one: it is the colour the palette was given rather
    than one worked out from it, and swapping it for a mixture is how a palette
    stops looking like itself. Only when no member carries hue is the panel
    tinted with the set's most chromatic value, far enough to carry it and no
    further -- the first blend that clears `FILL_CHROMA_MIN`, not the most
    colourful one the ceiling allows. A member under the target still beats that
    tint unless the tint carries twice its chroma; a tenth more hue is not worth
    a colour the user never picked. Where the set has no chroma to give at all,
    the fill falls back to a shade of the panel, as it was before.

    ``spent`` bars a colour from being taken whole; ``quiet`` bars it from being
    the tint source as well. The two are not the same list. An accent diluted
    into the panel and the accent itself are a face and its active state, not one
    colour used twice, so the accent stays a legal source -- but the ink does
    not, because tinting a panel with its own type colour is how the fills ended
    up grey in the first place.
    """
    legal = [c for c in cols if c.hex not in spent and _fill_ok(c.hex, paper, surface, dark)]
    qualified = [c for c in legal if c.chroma >= FILL_CHROMA_MIN]
    if qualified:
        pick = max(qualified, key=lambda c: c.chroma)
        return pick.hex, pick.hex, False

    member = max(legal, key=lambda c: c.chroma) if legal else None
    floor = 2.0 * member.chroma if member else 0.0
    sources = [c for c in cols if c.hex not in quiet and c.chroma > 0.0]
    source = max(sources, key=lambda c: c.chroma) if sources else None
    best_hex, best_chroma = (member.hex, member.chroma) if member else ("", 0.0)
    if source is not None:
        steps = 128
        for i in range(round(FILL_TINT_MIN * steps), steps + 1):
            candidate = mix(paper_base, source.hex, i / steps)
            if not _fill_ok(candidate, paper, surface, dark):
                continue
            found = chroma(candidate)
            if found < floor:  # not enough more hue than the member to be worth it
                continue
            if found >= FILL_CHROMA_MIN:
                return candidate, source.hex, True
            if found > best_chroma:
                best_hex, best_chroma = candidate, found
    if best_hex:
        own = best_hex in {c.hex for c in cols}
        return best_hex, best_hex if own else (source.hex if source else ""), not own
    # Nothing in the set can be a button face. A dark palette lifts the panel
    # again, which stays on the line the panel was already lifted along; a light
    # one takes the panel's own colour down. Either way the fill has to end up on
    # the side of the panel a raised face belongs on, which is why the two
    # directions start from different places.
    target = 1.0 + (0.12 if dark else 0.055)
    shade = _above(paper_base, paper, target) if dark else _seek(paper_base, BLACK, paper, target)
    return shade, "", shade != paper_base


def _choose_accent(
    cols: list[_Colour], paper: _Colour, ink: str, used: set[str]
) -> tuple[str, bool, str]:
    """The most distinctive legible colour. Returns (hex, derived, the value it came from)."""
    pool = [
        c
        for c in cols
        if c.hex not in used and c.hex != ink and _distinct_from_ink(c, ink)
    ]
    qualified = [c for c in pool if contrast(c.hex, paper.hex) >= ACCENT_MIN]
    if qualified:
        ranked = sorted(qualified, key=lambda c: -_accentness(c, cols))
        pick = ranked[0]
        # An accent and a rule can want the same colour. A rule can be
        # synthesised convincingly and an accent cannot, so if taking the top
        # pick empties the rule band and the runner-up is nearly as distinctive,
        # leave the band-filling colour behind.
        if len(ranked) > 1 and RULE_MIN <= contrast(pick.hex, paper.hex) <= RULE_MAX:
            rest = [c for c in pool if c.hex != pick.hex]
            if not any(RULE_MIN <= contrast(c.hex, paper.hex) <= RULE_MAX for c in rest):
                second = ranked[1]
                if _accentness(second, cols) >= 0.75 * _accentness(pick, cols):
                    return second.hex, False, second.hex
        return pick.hex, False, pick.hex
    if not pool:
        return (WHITE if paper.lum < 0.35 else BLACK), True, ""
    # Everything chromatic is too close to the paper in lightness to be seen on
    # it. Deepen the most distinctive one rather than promoting a duller colour.
    best = max(pool, key=lambda c: _accentness(c, cols))
    toward = WHITE if paper.lum < 0.35 else BLACK
    return _seek(best.hex, toward, paper.hex, ACCENT_MIN), True, best.hex


def _choose_rule(
    cols: list[_Colour], paper: _Colour, paper_base: str, ink: str, used: set[str]
) -> tuple[str, bool]:
    band = [
        c
        for c in cols
        if c.hex not in used and RULE_MIN <= contrast(c.hex, paper.hex) <= RULE_MAX
    ]
    if band:
        # Landing near the middle of the band matters, but not as much as having
        # some hue: an outline drawn in a dead grey is what makes a pale
        # interface read as washed out rather than as airy. The accent is
        # already spent by the time this runs, so the most chromatic value still
        # in the band is the palette's second colour, not its loudest.
        pick = max(band, key=lambda c: (c.chroma, -abs(contrast(c.hex, paper.hex) - RULE_TARGET)))
        return pick.hex, False
    # Mixed off the panel's own palette value rather than off the lifted panel,
    # so the hairline is a blend of two of the six and not a blend of a tint.
    return _seek_between(paper_base, ink, paper.hex, RULE_TARGET), True


def _choose_ink_soft(cols: list[_Colour], paper: _Colour, ink: str, used: set[str]) -> tuple[str, bool]:
    band = [
        c
        for c in cols
        if c.hex not in used and INK_SOFT_MIN <= contrast(c.hex, paper.hex) <= INK_SOFT_MAX
    ]
    if band:
        pick = min(band, key=lambda c: abs(contrast(c.hex, paper.hex) - INK_SOFT_TARGET))
        return pick.hex, False
    return _thin(ink, paper.hex, INK_SOFT_TARGET), True


def _choose_white(fill: str, dark: bool) -> str:
    """The bevel highlight.

    Opaque white on a dark panel is a scratch, not a highlight, so on a dark
    palette it is thinned until the lit edge sits about 1.6:1 above the button
    face, which is roughly the step it makes on a light one.
    """
    if not dark:
        return WHITE
    best, best_err = 60, 99.0
    for alpha in range(24, 121):
        err = abs(contrast(composite(_hex((255, 255, 255), alpha), fill), fill) - 1.6)
        if err < best_err:
            best, best_err = alpha, err
    return _hex((255, 255, 255), best)


def _choose_shadow(paper: str, paper_base: str, fill: str, dark: bool) -> str:
    """A light palette drops the fill colour; a dark one has to go below the panel.

    The dark case is taken down from the panel's own palette value rather than
    from the lifted panel, so that what lands under a raised edge is a shade of
    one of the six and not a shade of a tint of one.
    """
    return fill if not dark else _seek(paper_base, BLACK, paper, 1.35)


# --- overrides ----------------------------------------------------------------
# One entry per palette the solver could not finish honestly: the criterion that
# failed, and the value taken instead. Overrides are applied as each role is
# chosen, so anything derived from a role sees the overridden value. Nothing
# else in this module knows a palette by name.

_OVERRIDES: dict[str, dict[str, str]] = {
    # Criterion 3. Six near-neutrals: no member has a hue, so `_accentness` is
    # zero for all of them and there is nothing to rank. Pure white becomes the
    # accent and the off-white stays the ink, which is the only distinction this
    # palette has to offer. Without this the solver deepens a near-black.
    "Dark Matter": {"accent": "#FFFFFF", "ink": "#F6F1F1", "ink_strong": "#F6F1F1"},
    # Criterion 4. The rule band is unreachable. #CFCCC4 is the only mid-tone in
    # the set and it measures 1.49:1 on this paper -- 1.55:1 against the lightest
    # of the three near-whites, still short. The one value that does land in the
    # band is the 0.63-chroma gold, which criterion 4 would now take: the rule
    # prefers hue, and on Cafe and Banquet it takes a gold of much the same
    # saturation. It is refused here for the reason that has nothing to do with
    # saturation -- the gold is this palette's second colour and its only warm
    # one, and spending it on a hairline leaves the grey with nothing to do and
    # the set with no colour anywhere but the accent. Eleven hundredths under the
    # floor is the cheaper of the two shortfalls.
    "Spice Market": {"rule": "#CFCCC4"},
}

_OVERRIDE_NOTES: dict[str, str] = {
    "Dark Matter": (
        "criterion 3: no chromatic colour in the set; accent is pure white against "
        "the off-white ink"
    ),
    "Spice Market": (
        "criterion 4: rule #CFCCC4 measures 1.49:1, under the band floor; the only "
        "in-band alternative is the 0.63-chroma gold"
    ),
}


def _solve_from(
    name: str, cols: list[_Colour], members: tuple[_Colour, ...], dark: bool
) -> tuple[PaletteSpec, str, frozenset[str], float] | None:
    """Fit all ten roles around one candidate surface set, or give up on it.

    Returns the spec, the value the accent came from, every other palette colour
    a derived role was worked out of, and how much white the panel had to take.
    All three are things `_rank` cannot read back off a finished spec, and all
    three are the difference between a palette that was used and one that was
    merely sampled.
    """
    paper_of = _paper_of(members, dark)
    if dark != (paper_of.lum < 0.35):
        return None
    forced = _OVERRIDES.get(name, {})
    unknown = set(forced) - set(ROLES)
    if unknown:
        raise ValueError(f"override for {name!r} names unknown roles: {sorted(unknown)}")
    notes: list[str] = []

    paper_base = forced.get("paper", paper_of.hex)
    if paper_base != paper_of.hex:
        paper_of = _Colour(paper_base, luminance(paper_base), chroma(paper_base), hue(paper_base))

    paper_hex, surface, surface_base = _airy_surfaces(
        cols, members, paper_of, dark, pinned="paper" in forced
    )
    surface = forced.get("surface", surface)
    paper = _Colour(paper_hex, luminance(paper_hex), chroma(paper_hex), hue(paper_hex))
    if paper_hex != paper_base:
        notes.append(
            f"criterion 6: paper {paper_base} sat at {luminance(paper_base):.3f} luminance; "
            f"lifted to {paper_hex} at {paper.lum:.3f}"
        )
    if surface_base is None:
        notes.append(
            f"criterion 2: too few cohesive low-chroma values; "
            f"surface {surface} is a tint of the paper"
        )
    elif surface != surface_base:
        notes.append(f"criterion 6: surface {surface_base} lifted to {surface}")
    if not dark and paper.lum < PAPER_LUM_MIN - 0.005:
        notes.append(f"criterion 6: paper reached only {paper.lum:.3f} of {PAPER_LUM_MIN}")
    if dark and luminance(surface) + DARK_PANEL_LIFT > paper.lum + 0.0005:
        notes.append(
            f"criterion 6: panel clears the backdrop by "
            f"{paper.lum - luminance(surface):.3f}, short of {DARK_PANEL_LIFT}; "
            f"lifting further would put the ink under {INK_TARGET:.1f}:1"
        )

    # ink_strong is the deepest value on the panel. ink is a second value that
    # also clears the target, when the palette has one far enough away from the
    # first to be worth separating; otherwise the two roles share a colour, as
    # the shipped palette does.
    deepest, ink_derived, best_offered = _choose_ink(cols, paper)
    ink_strong = forced.get("ink_strong", deepest)
    if ink_derived:
        notes.append(
            f"criterion 1: nothing on the panel reached {INK_TARGET:.1f}:1 "
            f"(best {best_offered:.2f}:1); ink deepened to {ink_strong}"
        )

    used = {c.hex for c in members} | {ink_strong, surface, paper.hex, paper_base}
    if surface_base:
        used.add(surface_base)
    accent, accent_derived, accent_base = _choose_accent(cols, paper, ink_strong, used)
    if "accent" in forced:
        accent, accent_derived, accent_base = forced["accent"], False, forced["accent"]
    if accent_derived:
        notes.append(
            f"criterion 3: no colour reached {ACCENT_MIN:.1f}:1 on the panel; "
            f"accent is {accent_base} deepened to {accent}"
        )
    # The value an accent was deepened from is spent, not spare: drawing the
    # rules in raw #8DD14F and the active state in the same green two shades
    # down is one colour used twice, not two colours.
    used.add(accent)
    if accent_base:
        used.add(accent_base)

    ink = ink_strong
    seconds = [
        c
        for c in cols
        if c.hex not in used
        and contrast(c.hex, paper.hex) >= INK_TARGET
        and c.chroma <= INK_CHROMA_MAX
        and contrast(c.hex, ink_strong) >= INK_SPLIT_MIN
    ]
    if seconds:
        ink = min(seconds, key=lambda c: contrast(c.hex, paper.hex)).hex
    ink = forced.get("ink", ink)
    used.add(ink)

    # A button face is not chrome: the fill may take any member no other role has
    # been given, including one the surface set left on the floor, which is where
    # most of the sets keep their one quiet chromatic value.
    assigned = {paper.hex, paper_base, surface, ink, ink_strong, accent, accent_base}
    if surface_base:
        assigned.add(surface_base)
    fill, fill_base, fill_derived = _choose_fill(
        cols,
        paper.hex,
        paper_base,
        surface,
        dark,
        spent=assigned,
        quiet={ink, ink_strong, paper_base, paper.hex},
    )
    fill = forced.get("fill", fill)
    if fill_derived and fill_base:
        notes.append(
            f"criterion 7: no member is a chromatic surface on this panel; "
            f"fill {fill} is {paper_base} tinted with {fill_base}"
        )
    elif fill_derived:
        notes.append(
            f"criterion 2: no member clears the fill/paper separation; "
            f"fill {fill} is a shade of the paper"
        )
    if chroma(fill) < FILL_CHROMA_MIN:
        notes.append(
            f"criterion 7: fill holds {chroma(fill):.3f} chroma of {FILL_CHROMA_MIN}; "
            f"the set offers nothing more chromatic that still reads as a surface here"
        )
    used.add(fill)

    rule, rule_derived = _choose_rule(cols, paper, paper_base, ink, used)
    if "rule" in forced:
        rule, rule_derived = forced["rule"], False
    if rule_derived:
        notes.append(
            f"criterion 4: no colour landed in {RULE_MIN:.2f}-{RULE_MAX:.2f}:1; "
            f"rule {rule} is paper mixed with ink"
        )
    used.add(rule)

    ink_soft, soft_derived = _choose_ink_soft(cols, paper, ink, used)
    ink_soft = forced.get("ink_soft", ink_soft)
    if soft_derived:
        notes.append(
            f"criterion 5: no colour landed in {INK_SOFT_MIN:.1f}-{INK_SOFT_MAX:.1f}:1; "
            f"ink thinned to alpha {parse_hex(ink_soft)[3]}"
        )

    if name in _OVERRIDE_NOTES:
        notes.append("override: " + _OVERRIDE_NOTES[name])

    spec = PaletteSpec(
        name=name,
        is_dark=dark,
        paper=paper.hex,
        surface=surface,
        fill=fill,
        rule=rule,
        ink_soft=ink_soft,
        ink=ink,
        ink_strong=ink_strong,
        white=forced.get("white", _choose_white(fill, dark)),
        shadow=forced.get("shadow", _choose_shadow(paper.hex, paper_base, fill, dark)),
        accent=accent,
        notes=tuple(notes),
    )
    # A lifted surface is still the colour it was lifted from, so it counts as
    # that colour having been used. A tint source does not: a tenth of the gold
    # mixed into a panel is not the gold employed, and crediting it would let a
    # solve buy the same score by sampling a colour instead of taking it.
    bases = frozenset(h for h in (paper_base, surface_base) if h)
    return spec, accent_base, bases, chroma(paper_base) - chroma(paper.hex)


def _rank(
    spec: PaletteSpec, cols: list[_Colour], accent_base: str, bases: frozenset[str], bleach: float
) -> float:
    """How good a finished fit is.

    Meeting the criteria dominates. After that the measure is how much of the
    palette actually got used: a solve that leaves two of the six on the floor
    and paints their replacements out of tints of the paper has answered a
    different question from the one the palette was chosen to answer.

    The last term is the one that settles polarity, which criteria 1 to 5 do not
    speak to. Cherry Blossom Mocha reads either way on contrast alone, but only
    the dark reading lets its pink be the accent rather than a faint dotted rule
    at 1.5:1, so the tiebreak is whether the palette's most distinctive colour
    got the role that means something.
    """
    palette = {c.hex for c in cols}
    used = palette & (
        {
            spec.paper,
            spec.surface,
            spec.fill,
            spec.rule,
            spec.ink,
            spec.ink_strong,
            spec.accent,
            accent_base,  # a deepened accent is still that colour doing the work
            _hex(_rgb(spec.ink_soft)),
        }
        | set(bases)  # nor does a lifted panel stop being the colour it was lifted from
    )
    surfaces = (spec.paper, spec.surface, spec.fill)
    score = -4.0 * len(check(spec))
    score += 0.35 * len(used)
    score -= 1.5 * (sum(chroma(s) for s in surfaces) / 3.0) / SURFACE_CHROMA_MAX
    # Criterion 6 is a repair, and a repair is worth what it cost. What a lift
    # costs is the panel's own colour, washed out of it on the way to white, so a
    # set that was already light enough beats one that had to be bleached into
    # it: Plum & Slate can make a panel out of its lilac, but only by taking it
    # from 0.18 chroma to 0.03, while the peach it was given arrives at 0.86
    # luminance still recognisably the peach. Chroma rather than blend fraction,
    # because a lift off a near-black takes a lot of white and loses almost
    # nothing, and a cost that reads differently at the two polarities would
    # settle the light/dark question by arithmetic that has no opinion about it.
    score -= 4.0 * bleach
    score += 0.8 if spec.ink_strong in palette else 0.0
    score += 0.5 * min(contrast(spec.ink, spec.paper), 10.0) / 10.0
    best_accent = max((_accentness(c, cols) for c in cols), default=0.0)
    if best_accent > 0.0 and accent_base:
        chosen = next((c for c in cols if c.hex == accent_base), None)
        score += 2.0 * (_accentness(chosen, cols) / best_accent if chosen else 0.0)
    return score


def solve_palette(name: str, hexes: Sequence[str]) -> PaletteSpec:
    """Assign the ten roles from six hexes by measurement.

    Every cohesive surface set is solved through to a finished spec at both
    polarities and the results are ranked, because a surface choice can only be
    judged by what it leaves for the other nine roles.
    """
    if len(hexes) != 6:
        raise ValueError(f"palette {name!r} needs exactly 6 colours, got {len(hexes)}")
    cols = _prepare(hexes)

    anchors = [c for c in cols if c.chroma <= SURFACE_CHROMA_MAX]
    if len(anchors) < 2:
        # No palette here needs this, but a future one might: fall back to the
        # two least saturated values rather than refusing to solve.
        anchors = sorted(cols, key=lambda c: c.chroma)[:2]

    best: PaletteSpec | None = None
    best_score = -1e9
    for members in _cohesive_sets(anchors):
        for dark in (False, True):
            solved = _solve_from(name, cols, members, dark)
            if solved is None:
                continue
            spec, accent_base, bases, bleach = solved
            score = _rank(spec, cols, accent_base, bases, bleach)
            if score > best_score:
                best, best_score = spec, score
    if best is None:  # pragma: no cover - every set of six admits one surface
        raise ValueError(f"palette {name!r} has no usable surface colour")
    return best


def palette_named(name: str) -> PaletteSpec:
    """The solved spec for a registered palette."""
    try:
        hexes = NAMED_PALETTES[name]
    except KeyError:
        raise KeyError(f"unknown palette: {name!r}; known: {', '.join(NAMED_PALETTES)}") from None
    return solve_palette(name, hexes)


def list_palettes() -> list[str]:
    """Registered palette names, the default first."""
    order = list(NAMED_PALETTES)
    return sorted(order, key=lambda n: (n != DEFAULT_PALETTE, order.index(n)))


# --- measurement --------------------------------------------------------------


def measure(spec: PaletteSpec) -> dict[str, float]:
    """Every role's measured contrast against the panel it is painted on."""
    paper = spec.paper
    surfaces = (spec.paper, spec.surface, spec.fill)
    out = {role: contrast(composite(spec.role(role), paper), paper) for role in ROLES}
    out["paper"] = 1.0
    out["_spread"] = max(abs(luminance(a) - luminance(b)) for a in surfaces for b in surfaces)
    out["_cohesion"] = max(contrast(a, b) for a in surfaces for b in surfaces)
    out["_bevel"] = contrast(composite(spec.white, spec.fill), spec.fill)
    # The two numbers criteria 6 and 7 are about. Neither is a contrast, so
    # neither belongs in the loop above, but both are read off the same table.
    out["_paper_lum"] = luminance(paper)
    out["_fill_chroma"] = chroma(spec.fill)
    return out


def check(spec: PaletteSpec) -> list[str]:
    """Criterion failures in priority order. Empty means the spec meets 1 to 5."""
    m = measure(spec)
    fails: list[str] = []
    if m["ink"] < INK_FLOOR:
        fails.append(f"1 ink {m['ink']:.2f} below floor {INK_FLOOR}")
    elif m["ink"] < INK_TARGET - 0.005:
        fails.append(f"1 ink {m['ink']:.2f} short of {INK_TARGET}")
    for role in ("paper", "surface", "fill"):
        c = chroma(spec.role(role))
        if c > SURFACE_CHROMA_MAX:
            fails.append(f"2 {role} chroma {c:.2f}")
    if m["_cohesion"] > SURFACE_COHESION_MAX:
        fails.append(f"2 surfaces {m['_cohesion']:.2f}:1 apart")
    if m["_spread"] > SURFACE_SPREAD_MAX:
        fails.append(f"2 surface luminance spread {m['_spread']:.2f}")
    gap = abs(luminance(spec.fill) - luminance(spec.paper))
    if spec.is_dark:
        if m["fill"] < FILL_CONTRAST_MIN:
            fails.append(f"2 fill vs paper {m['fill']:.3f}:1")
    elif gap < FILL_GAP_MIN:
        fails.append(f"2 fill vs paper gap {gap:.3f}")
    if m["accent"] < ACCENT_MIN:
        fails.append(f"3 accent {m['accent']:.2f}")
    if not RULE_MIN <= m["rule"] <= RULE_MAX:
        fails.append(f"4 rule {m['rule']:.2f}")
    if not INK_SOFT_MIN <= m["ink_soft"] <= INK_SOFT_MAX:
        fails.append(f"5 ink_soft {m['ink_soft']:.2f}")
    return fails


_SHORT = {
    "surface": "surf",
    "fill": "fill",
    "rule": "rule",
    "ink_soft": "soft",
    "ink": "ink",
    "ink_strong": "strong",
    "accent": "accent",
}


def report() -> str:
    """The measured table: every palette, every role, every contrast."""
    specs = [palette_named(n) for n in NAMED_PALETTES]
    lines: list[str] = ["ROLES", ""]
    head = (
        f"{'palette':<21} {'mode':<5} {'paper':<9} {'surface':<9} {'fill':<9} {'rule':<9} "
        f"{'ink_soft':<10} {'ink':<9} {'strong':<9} {'accent':<9} {'white':<10} {'shadow':<9}"
    )
    lines += [head, "-" * len(head)]
    for s in specs:
        lines.append(
            f"{s.name:<21} {'dark' if s.is_dark else 'light':<5} {s.paper:<9} {s.surface:<9} "
            f"{s.fill:<9} {s.rule:<9} {s.ink_soft:<10} {s.ink:<9} {s.ink_strong:<9} "
            f"{s.accent:<9} {s.white:<10} {s.shadow:<9}"
        )

    lines += ["", "CONTRAST AGAINST PAPER (bevel is against fill; coh/spread are the surface set)", ""]
    lines += ["paperL is the panel's luminance and fillC the button face's chroma:", ""]
    head2 = (
        f"{'palette':<21} {'paperL':>6} {'fillC':>5} {'surf':>5} {'fill':>5} {'rule':>5} "
        f"{'soft':>5} {'ink':>6} {'strong':>6} {'accent':>6} {'bevel':>5} {'coh':>5} "
        f"{'spread':>6}  verdict"
    )
    lines += [head2, "-" * len(head2)]
    for s in specs:
        m = measure(s)
        fails = check(s)
        if fails:
            verdict = "; ".join(fails)
            if s.name in _OVERRIDE_NOTES:
                verdict += " (override)"
        else:
            verdict = "ok" if not s.notes else "ok, derived"
        lines.append(
            f"{s.name:<21} {m['_paper_lum']:>6.3f} {m['_fill_chroma']:>5.3f} "
            f"{m['surface']:>5.2f} {m['fill']:>5.2f} {m['rule']:>5.2f} "
            f"{m['ink_soft']:>5.2f} {m['ink']:>6.2f} {m['ink_strong']:>6.2f} "
            f"{m['accent']:>6.2f} {m['_bevel']:>5.2f} {m['_cohesion']:>5.2f} "
            f"{m['_spread']:>6.3f}  {verdict}"
        )

    failing = [s.name for s in specs if check(s)]
    undocumented = [n for n in failing if n not in _OVERRIDE_NOTES]
    lines.append("")
    if undocumented:
        lines.append(f"UNDOCUMENTED FAILURES: {', '.join(undocumented)}")
    elif failing:
        lines.append(
            f"All {len(specs)} palettes meet criteria 1 to 5, except "
            f"{', '.join(failing)} under a documented override."
        )
    else:
        lines.append(f"All {len(specs)} palettes meet criteria 1 to 5.")

    lines += ["", "WHERE THE SIX WERE NOT ENOUGH", ""]
    for s in specs:
        if not s.notes:
            continue
        lines.append(f"  {s.name}")
        for note in s.notes:
            lines.append(f"      {note}")
    return "\n".join(lines)


def main() -> int:
    sys.stdout.write(report() + "\n")
    return 0


__all__ = [
    "NAMED_PALETTES",
    "DEFAULT_PALETTE",
    "ROLES",
    "PaletteSpec",
    "solve_palette",
    "palette_named",
    "list_palettes",
    "parse_hex",
    "luminance",
    "contrast",
    "chroma",
    "hue",
    "mix",
    "composite",
    "measure",
    "check",
    "report",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
