def _scale_pair3(v, f):
    if v is None:
        return
    v[0] *= f
    v[1] *= f
    v[2] *= f


def _scale_vec(v, f):
    if v is None:
        return v
    return [c * f for c in v]


def _scale_node_animations(anim, factor):
    """Scale all position keyframes in an AnimationInterface."""
    for node_anim in anim.node_animations:
        positions = node_anim.positions
        if not positions:
            continue
        for frame in list(positions.keys()):
            positions[frame] = [c * factor for c in positions[frame]]


def rescale_gfs_for_blender(gfs, factor):
    """
    Mutate a GFSInterface in-place by multiplying every *position* (长度尺寸)
    component by `factor`. Rotations and scales are unit-less and left alone.

    Applies to:
      - Bone local translations (node.position)
      - Bone bind-pose matrix translation column
      - Mesh vertex positions and morph position deltas
      - All node animation position keyframes (base / blend / lookat)
      - Model bounding box & bounding sphere overrides

    Only do this when importing into Blender; if you ever need to round-trip
    back to a GMD/GAP that the game understands, the export side must divide
    positions by the same factor (currently not implemented).
    """
    if factor == 1.0:
        return

    # 1. Bones: local translation & bind-pose matrix translation column
    for node in gfs.bones:
        node.position = _scale_vec(node.position, factor)
        bpm = node.bind_pose_matrix
        if bpm is not None and len(bpm) >= 12:
            bpm[3]  *= factor
            bpm[7]  *= factor
            bpm[11] *= factor

    # 2. Meshes: per-vertex positions and morph deltas
    for mesh in gfs.meshes:
        for vert in mesh.vertices:
            if vert.position is not None:
                vert.position = [c * factor for c in vert.position]
        for morph in mesh.morphs:
            # morph is a list of per-vertex deltas (each a 3-vector)
            for i, delta in enumerate(morph):
                morph[i] = [c * factor for c in delta]

    # 3. Animation position keyframes
    for anim in gfs.animations:
        _scale_node_animations(anim, factor)
    for anim in gfs.blend_animations:
        _scale_node_animations(anim, factor)
    if gfs.lookat_animations is not None:
        # LookAt has 4 sub-anims
        for attr in ("up", "down", "left", "right"):
            sub = getattr(gfs.lookat_animations, attr, None)
            if sub is not None:
                _scale_node_animations(sub, factor)

    # 4. Model overrides: bounding box / sphere
    bb = gfs.overrides.bounding_box
    if bb is not None:
        if bb.min_dims is not None:
            bb.min_dims = [c * factor for c in bb.min_dims]
        if bb.max_dims is not None:
            bb.max_dims = [c * factor for c in bb.max_dims]
    bs = gfs.overrides.bounding_sphere
    if bs is not None:
        if bs.center is not None:
            bs.center = [c * factor for c in bs.center]
        if bs.radius is not None:
            bs.radius *= factor