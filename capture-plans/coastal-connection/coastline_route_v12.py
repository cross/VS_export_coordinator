"""Proposed shorelines, in relative Vintage Story X/Z blocks."""

WEST_SHORE = [
    ('M', [(-34500, 59750)]),
    ('C', [(-34490, 59750), (-34420, 59750), (-34390, 59780)]),
    ('L', [(-34270, 59900)]),
    ('L', [(-34150, 60500)]),
    ('L', [(-34150, 60950)]),
    ('C', [(-34150, 60990), (-34125, 61050), (-34100, 61075)]),
    # Broader convex eastward turn: keep its start near Z=61450, ease the
    # eastward motion through Z=61600, and end about 30 blocks farther east
    # than v11. The end tangent matches the long southward run below it.
    ('L', [(-33747, 61450)]),
    ('C', [(-33728, 61470), (-33678, 61503), (-33665, 61525)]),
    ('C', [(-33652, 61547), (-33640, 61575), (-33637, 61600)]),
    ('L', [(-33550, 62280)]),
    ('C', [(-33545, 62306), (-33568, 62350), (-33576, 62360)]),
]

# The initial bend follows the mapped northern ocean edge. The remaining
# geometry is conceptual, joining the connected southern sea near Z=61994.
EAST_SHORE = [
    ('M', [(-34281, 59375)]),
    ('C', [(-34248, 59405), (-34194, 59460), (-34163, 59500)]),
    ('C', [(-34143, 59520), (-34126, 59545), (-34111, 59590)]),
    # Broad eastern sweep: eastward motion slows, stops, and slightly reverses.
    ('C', [(-33975, 59710), (-33810, 59850), (-33785, 59970)]),
    ('C', [(-33775, 60035), (-33782, 60105), (-33800, 60150)]),
    ('L', [(-33800, 60200)]),
    # Broader concave indentation: begins 30 Z earlier, reaches slightly
    # farther east, and turns west about 10 Z before the previous draft.
    ('C', [(-33800, 60280), (-33735, 60340), (-33745, 60390)]),
    # WSW turn and trader bend. About 22–24 horizontal blocks separate the
    # coast from the trader's western edge at Z=60633–60647.
    ('C', [(-33755, 60430), (-33830, 60455), (-33900, 60480)]),
    ('C', [(-33965, 60510), (-33990, 60615), (-33955, 60700)]),
    # Preserve the old southeast route through approximately Z=61180, then
    # turn more eastward. The first cubic is a De Casteljau split of v10.
    ('C', [(-33928.93, 60765.175), (-33604.571, 61011.589),
           (-33388.79, 61180.028)]),
    ('C', [(-33337.5, 61220), (-33152, 61300), (-33100, 61330)]),
    # Ease from bearing ~120° to ~170° over a wide arc, then hold the
    # near-south bearing through Z=61750.
    ('C', [(-33048, 61360), (-32994, 61420), (-32980, 61500)]),
    ('L', [(-32936, 61750)]),
    # Turn to ~135° and retain that diagonal through Z=61950.
    ('C', [(-32929, 61790), (-32915, 61805), (-32880, 61840)]),
    ('L', [(-32770, 61950)]),
    # Ease to an eastward tangent at the mapped southern sea edge.
    ('C', [(-32735, 61985), (-32710, 61994), (-32680, 61994)]),
]


def svg_path(parts, project):
    """Format a coordinate path using project(x, z) -> 'svg-x,svg-y'."""
    return ' '.join(op + ' ' + ' '.join(project(x, z) for x, z in points)
                    for op, points in parts)
